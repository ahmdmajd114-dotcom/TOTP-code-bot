import asyncio
import ast
import __future__
from datetime import datetime,timezone
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest

from runtime_scheduler import CustomerFirstDispatcher, run_io, background_job, customer_first_processor, dispatch_deferred_reply, _lane


def update(user, chat):
    return SimpleNamespace(effective_user=SimpleNamespace(id=user),effective_chat=SimpleNamespace(id=chat))


class RuntimeSchedulerTests(unittest.IsolatedAsyncioTestCase):
    def test_real_owner_arrival_suppresses_only_message_seen_at_arrival(self):
        tree=ast.parse(Path(__file__).resolve().parents[1].joinpath('bot.py').read_text())
        names={'suppress_customer_reply_on_owner_arrival','observe_business_update_arrival'}
        selected=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
        ns={'OWNER_USER_ID':99,'datetime':datetime,'timezone':timezone,
            '_latest_customer_message_ids':{},'_owner_arrival_reply_targets':{},
            '_support_context_until':{},'_support_context_resolved_at':{},
            '_suppressed_auto_reply_keys':set(),'_pending_customer_text_batches':{},'_pending_initial_greetings':{}}
        exec(compile(ast.Module(body=selected,type_ignores=[]),'owner-arrival','exec',flags=__future__.annotations.compiler_flag),ns)
        def business(user,message_id):
            bm=SimpleNamespace(from_user=SimpleNamespace(id=user),chat=SimpleNamespace(id=1),message_id=message_id,text='hello')
            return SimpleNamespace(business_message=bm,edited_business_message=None)
        ns['observe_business_update_arrival'](business(1,10))
        ns['observe_business_update_arrival'](business(99,20))
        ns['observe_business_update_arrival'](business(1,21))
        self.assertEqual(ns['_suppressed_auto_reply_keys'],{(1,10)})
        self.assertEqual(ns['_owner_arrival_reply_targets'][(1,20)],(True,10))
        self.assertEqual(ns['_latest_customer_message_ids'][1],21)

    async def test_owner_backlog_never_uses_customer_capacity(self):
        dispatcher=CustomerFirstDispatcher(99)
        release=asyncio.Event();customer=asyncio.Event();owners=[]
        async def owner(n):
            owners.append(n)
            await release.wait()
        async def serve():
            self.assertEqual(_lane.get(),'customer')
            customer.set()
        for n in range(100):dispatcher.submit(update(99,99),owner(n))
        dispatcher.submit(update(123,123),serve())
        try:
            await asyncio.wait_for(customer.wait(),1)
            self.assertEqual(owners,[0])
        finally:
            release.set();await dispatcher.drain()
        self.assertEqual(owners,list(range(100)))

    async def test_same_customer_ordered_other_customer_can_finish(self):
        dispatcher=CustomerFirstDispatcher(99,customer_slots=2)
        release=asyncio.Event();other=asyncio.Event();seen=[]
        async def first():
            seen.append('first');await release.wait();seen.append('first-done')
        async def second():seen.append('second')
        async def independent():other.set()
        dispatcher.submit(update(1,1),first())
        dispatcher.submit(update(1,1),second())
        dispatcher.submit(update(2,2),independent())
        try:
            await asyncio.wait_for(other.wait(),1)
            self.assertEqual(seen,['first'])
        finally:
            release.set();await dispatcher.drain()
        self.assertEqual(seen,['first','first-done','second'])

    async def test_code_has_reserved_slot_when_all_customer_slots_are_busy(self):
        dispatcher=CustomerFirstDispatcher(99,customer_slots=1,code_request=lambda text:text=='كود')
        release=asyncio.Event();started=asyncio.Event();served=asyncio.Event()
        async def image():started.set();await release.wait()
        async def code():
            self.assertEqual(_lane.get(),'code');served.set()
        dispatcher.submit(update(1,1),image());await started.wait()
        request=update(2,2);request.effective_message=SimpleNamespace(text='كود')
        dispatcher.submit(request,code())
        try:await asyncio.wait_for(served.wait(),1)
        finally:release.set();await dispatcher.drain()

    async def test_manual_owner_reply_keeps_its_place_in_customer_chat(self):
        dispatcher=CustomerFirstDispatcher(99)
        release=asyncio.Event();started=asyncio.Event();seen=[]
        async def first():
            started.set();await release.wait();seen.append('customer1')
        async def manual():seen.append('owner')
        async def next_customer():seen.append('customer2')
        dispatcher.submit(update(1,1),first())
        await started.wait()
        dispatcher.submit(update(99,1),manual())
        dispatcher.submit(update(1,1),next_customer())
        release.set();await dispatcher.drain()
        self.assertEqual(seen,['customer1','owner','customer2'])

    async def test_deferred_customer_reply_rejoins_same_chat_order(self):
        dispatcher=CustomerFirstDispatcher(99);release=asyncio.Event();started=asyncio.Event();seen=[]
        context=SimpleNamespace(application=SimpleNamespace(update_processor=SimpleNamespace(dispatcher=dispatcher)))
        async def first():started.set();await release.wait();seen.append('incoming')
        async def deferred():seen.append('reply')
        dispatcher.submit(update(1,1),first());await started.wait()
        await dispatch_deferred_reply(update(1,1),context,deferred())
        self.assertEqual(seen,[])
        release.set();await dispatcher.drain();self.assertEqual(seen,['incoming','reply'])

    async def test_slow_background_io_leaves_customer_pool_and_loop_available(self):
        loop=asyncio.get_running_loop();started=asyncio.Event();release=threading.Event()
        def slow_sheet():
            loop.call_soon_threadsafe(started.set)
            release.wait(3)
        @background_job
        async def sync():await run_io(slow_sheet)
        task=asyncio.create_task(sync())
        dispatcher=CustomerFirstDispatcher(99);served=asyncio.Event()
        async def customer():
            self.assertEqual(await run_io(lambda:'code'),'code');served.set()
        try:
            await asyncio.wait_for(started.wait(),1)
            dispatcher.submit(update(1,1),customer())
            await asyncio.wait_for(served.wait(),1)
            self.assertFalse(task.done())
        finally:
            release.set();await task;await dispatcher.drain()

    async def test_cancelled_io_keeps_serial_slot_until_write_finishes(self):
        loop=asyncio.get_running_loop();started=asyncio.Event();release=threading.Event();written=[]
        def write():
            loop.call_soon_threadsafe(started.set);release.wait(3);written.append('saved')
        task=asyncio.create_task(run_io(write))
        try:
            await asyncio.wait_for(started.wait(),1)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(written,['saved'])

    async def test_failed_update_releases_customer_slot(self):
        dispatcher=CustomerFirstDispatcher(99,customer_slots=1);seen=[]
        async def failed():raise RuntimeError('offline')
        async def next_one():seen.append('served')
        with self.assertLogs('runtime_scheduler',level='ERROR'):
            dispatcher.submit(update(1,1),failed());dispatcher.submit(update(1,1),next_one())
            await dispatcher.drain()
        self.assertEqual(seen,['served'])

    @unittest.skipUnless(importlib.util.find_spec('telegram'),'Requires project Telegram dependency')
    async def test_real_telegram_processor_tracks_and_drains_admitted_updates(self):
        processor=customer_first_processor(99);done=asyncio.Event()
        async def work():await asyncio.sleep(0);done.set()
        await processor.initialize()
        await processor.process_update(update(1,1),work())
        await processor.shutdown()
        self.assertTrue(done.is_set())


if __name__=='__main__':unittest.main()
