"""Customer-first execution without sharing owner flows or blocking Telegram's loop.

Only synchronous store services run in threads. Telegram coroutines and update
coordination stay on the main event loop. Bounded pools reserve customer capacity.
"""
from __future__ import annotations

import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar, copy_context
from functools import partial, wraps
import logging
from pathlib import Path
from time import monotonic
from weakref import WeakKeyDictionary
from catalog_read_cache import customer_catalog_reads
from performance_diagnostics import record_timing

logger = logging.getLogger(__name__)
_lane = ContextVar('store_work_lane', default='owner')
_pools = {}
_limits = {'code': 2, 'customer': 4, 'owner': 2, 'background': 1}
_gates = WeakKeyDictionary()


async def run_io(function, *args, **kwargs):
    """Await blocking work in its lane; cancellation never abandons a write."""
    lane = _lane.get()
    loop = asyncio.get_running_loop()
    gates = _gates.setdefault(loop, {})
    gate = gates.setdefault(lane, asyncio.Semaphore(_limits[lane]))
    queued_at = monotonic()
    async with gate:
        return await _execute_io(lane,function,args,kwargs,monotonic()-queued_at)


async def _execute_io(lane, function, args, kwargs,waiting):
    if lane not in _pools:
        _pools[lane] = ThreadPoolExecutor(max_workers=_limits[lane], thread_name_prefix='store-'+lane)
    ctx = copy_context()
    start = monotonic()
    future = asyncio.get_running_loop().run_in_executor(_pools[lane], ctx.run, partial(function, *args, **kwargs))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        # The thread cannot be stopped safely. Keep the caller's serial slot
        # until it finishes, so the next command cannot race an in-flight write.
        try:
            await asyncio.shield(future)
        except Exception:
            logger.exception('Cancelled %s I/O also failed', lane)
        raise
    finally:
        elapsed = monotonic()-start
        code=getattr(function,'__code__',None)
        site=f'{Path(code.co_filename).name}:{code.co_firstlineno}' if code else type(function).__name__
        record_timing('io',lane,site,elapsed,waiting)
        if elapsed >= 2:
            logger.warning('Slow store I/O lane=%s site=%s seconds=%.2f',lane,site,elapsed)


def background_job(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        token = _lane.set('background')
        try:
            return await function(*args, **kwargs)
        finally:
            _lane.reset(token)
    return wrapped


class CustomerFirstDispatcher:
    """Admit quickly; run independent customers concurrently, owners serially.

    Queued owner clicks don't consume customer slots. Business updates for the
    same conversation (including manual owner replies) never run together.
    """
    def __init__(self, owner_id, customer_slots=4, code_request=None, before_update=None):
        self.owner_id = owner_id
        self.code_request = code_request
        self.before_update = before_update
        self.capacity = {'code': 2, 'customer': customer_slots, 'owner': 1}
        self.active = {lane:0 for lane in self.capacity}
        self.pending = {lane:deque() for lane in self.capacity}
        self.keys = set()
        self.tasks = set()
        self.idle = asyncio.Event()
        self.idle.set()

    def submit(self, update, coroutine):
        if self.before_update:
            self.before_update(update)
        user = getattr(update, 'effective_user', None)
        chat = getattr(update, 'effective_chat', None)
        owner = user is None or user.id == self.owner_id
        lane = 'owner' if owner else 'customer'
        message = getattr(update,'effective_message',None)
        text = getattr(message,'text',None)
        if not owner and text and self.code_request and self.code_request(text):
            lane = 'code'
        keys = {('chat', chat.id)} if chat is not None else {('update', id(update))}
        if owner:
            keys.add(('owner', self.owner_id))
        self.pending[lane].append((keys, coroutine, monotonic()))
        self.idle.clear()
        self._pump()

    def _pump(self):
        for lane in ('code', 'customer', 'owner'):
            queue = self.pending[lane]
            while self.active[lane] < self.capacity[lane]:
                job = next((item for item in queue if self._eligible(item)), None)
                if job is None:
                    break
                queue.remove(job)
                self.keys.update(job[0])
                self.active[lane] += 1
                task = asyncio.create_task(self._run(lane, *job))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
        if not any(self.active.values()) and not any(self.pending.values()):
            self.idle.set()

    def _eligible(self, job):
        if job[0] & self.keys:
            return False
        # Customer priority may bypass unrelated admin work, never an earlier
        # manual owner reply or update for this exact conversation.
        return not any(earlier[2] < job[2] and earlier[0] & job[0]
                       for queue in self.pending.values() for earlier in queue)

    async def _run(self, lane, keys, coroutine, queued_at):
        token = _lane.set(lane)
        start = monotonic()
        try:
            with customer_catalog_reads(enabled=lane in {'customer','code'}):
                await coroutine
        except Exception:
            logger.exception('Store update failed in %s lane',lane)
        finally:
            elapsed = monotonic()-start
            record_timing('update',lane,'Telegram update',elapsed,start-queued_at)
            if elapsed >= 5 or start-queued_at >= 2:
                logger.warning('Slow store update lane=%s queue=%.2f seconds=%.2f',lane,start-queued_at,elapsed)
            _lane.reset(token)
            self.keys.difference_update(keys)
            self.active[lane] -= 1
            self._pump()

    async def drain(self):
        await self.idle.wait()


def customer_first_processor(owner_id, code_request=None, before_update=None):
    # Import only at app setup, so isolated scheduler tests need no Telegram.
    from telegram.ext import BaseUpdateProcessor

    class Processor(BaseUpdateProcessor):
        def __init__(self):
            super().__init__(32)
            self.dispatcher = CustomerFirstDispatcher(owner_id,code_request=code_request,before_update=before_update)
            self.watchdog = None

        async def initialize(self):
            self.watchdog = asyncio.create_task(_watch_loop())

        async def shutdown(self):
            await self.dispatcher.drain()
            if self.watchdog:
                self.watchdog.cancel()
                try:
                    await self.watchdog
                except asyncio.CancelledError:
                    pass

        async def do_process_update(self, update, coroutine):
            self.dispatcher.submit(update, coroutine)

    return Processor()


async def drain_customer_updates(application):
    """PTB post_stop runs before Bot.shutdown closes its HTTP connections."""
    await application.update_processor.dispatcher.drain()


async def dispatch_deferred_reply(update, context, coroutine):
    """Rejoin conversation ordering after debounce instead of running alongside it."""
    app = getattr(context,'application',None)
    dispatcher = getattr(getattr(app,'update_processor',None),'dispatcher',None)
    if dispatcher is None:
        await coroutine
    else:
        dispatcher.submit(update,coroutine)


async def _watch_loop():
    while True:
        start = monotonic()
        await asyncio.sleep(1)
        lag = monotonic()-start-1
        if lag >= 2:
            logger.warning('Store event loop blocked seconds=%.2f',lag)
