"""Baghdad daytime reception policy, independent of money and subscriptions."""
import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps
import logging

from login_assistance import normalize, text_signal, login_reply_authorized
from runtime_scheduler import background_job, run_io

BAGHDAD = timezone(timedelta(hours=3))
WAIT_TEXT = ('أهلاً وسهلاً، حياكم وتنورونا 🌷\n'
             'تفضل اكتب طلبك، وعند توفر أحد الإداريين سيتم الرد عليك.\n'
             'علماً أن دوامنا الرسمي يبدأ بعد الرابعة مساءً.')
_services = None
_manual_reply = ContextVar('work_hours_manual_reply', default=False)
_reply_context = ContextVar('work_hours_reply_context', default=None)
_waiting_reply = ContextVar('work_hours_waiting_reply', default=False)
_cancelled = set()
_timers = {}
log = logging.getLogger(__name__)


class WorkHoursReplyBlocked(RuntimeError):
    """A reply prepared earlier is no longer permitted at sending time."""


@dataclass
class WorkHoursServices:
    client: object
    owner_id: int
    paused: object
    archive: object
    owner_replied_since: object
    clock: object = lambda: datetime.now(timezone.utc)


def configure_work_hours(config):
    global _services
    _services = config


def create_timer(coroutine):
    # Application.create_task is awaited by Application.stop; a 15-minute
    # sleep there would hold deployments open. These timers are cancelled by
    # the post_stop hook, while their durable deadlines stay in Supabase.
    return asyncio.create_task(coroutine)


def stop_work_hours(drain):
    async def stopped(application):
        await drain(application)
        tasks = list(_timers.values())
        _timers.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    return stopped


def in_work_hours(now):
    local = now.astimezone(BAGHDAD)
    return 8 <= local.hour < 16


def day_key(chat_id, now):
    return int(chat_id), now.astimezone(BAGHDAD).date().isoformat()


def table():
    return _services.client.table('customer_work_hours')


def event(chat_id, business_id, message_id, kind, now):
    return _services.client.rpc('work_hours_event', dict(p_owner=_services.owner_id,
        p_chat=int(chat_id), p_business=business_id, p_message=message_id,
        p_kind=kind, p_at=now.isoformat())).execute().data


def get_row(chat_id, now):
    rows = table().select('*').eq('owner_id', _services.owner_id).eq('chat_id', int(chat_id))\
        .eq('work_day', day_key(chat_id, now)[1]).execute().data
    return rows[0] if rows else None


def cancel_immediately(chat_id, now=None):
    if not _services:
        return
    now = now or _services.clock()
    key = day_key(chat_id, now)
    _cancelled.add(key)
    task = _timers.pop(key, None)
    if task and task is not asyncio.current_task():
        task.cancel()
    # Bound memory to today's cancellations; persistent records remain in DB.
    for old in tuple(_cancelled):
        if old[1] != key[1]:
            _cancelled.discard(old)


async def failure(context):
    log.error('Work-hours persistence unavailable; general replies remain blocked')
    # One alert per process, rather than one notification per customer message.
    if getattr(failure, 'reported', False):
        return
    failure.reported = True
    try:
        await context.bot.send_message(chat_id=_services.owner_id,
            text='⚠️ تعذر حفظ وضع الدوام. الردود العامة خلال ٨–٤ محجوبة؛ رسالة الانتظار تحتاج مراجعة تهيئة supabase_work_hours.sql أو الاتصال.')
    except Exception:
        log.error('Could not report work-hours persistence failure')


async def owner_answered(context, chat_id, business_id, message_id=0):
    if not _services:
        return
    now = _services.clock()
    if not in_work_hours(now):
        return
    cancel_immediately(chat_id, now)
    try:
        await run_io(event, chat_id, business_id, message_id, 'owner', now)
    except Exception:
        await failure(context)


def is_direct_reply(bm, owner_id):
    if not bm.from_user or bm.from_user.id != owner_id or getattr(bm, 'sender_business_bot', None):
        return False
    value = (getattr(bm, 'text', None) or '').strip()
    # Administrative commands are not customer-facing answers. Confirmed
    # replies generated from them are tracked by the Bot adapter below.
    if value.startswith('/') or value.lower().split(' ', 1)[0] in {
        'دين', 'صرف', 'قبض', 'ac', 'accept', 'س', 'ع', 'ج', 'كود', 'دفع', 'راضي'}:
        return False
    return bool(value or getattr(bm, 'photo', None) or getattr(bm, 'voice', None)
                or getattr(bm, 'document', None) or getattr(bm, 'video', None)
                or getattr(bm, 'sticker', None))


async def hold(context, bm, text):
    await run_io(_services.archive, bm.chat.id, bm.chat.full_name or bm.chat.first_name or 'غير معروف',
                 bm.chat.username, sender_type='customer', message_text=text or '[رسالة خلال الدوام]')


async def route(context, bm, text=None, photo_intent=None):
    """Return normal, hold, or login. Never flush old general messages at 16:00."""
    now = _services.clock()
    signal = photo_intent or text_signal(text)
    login_start = signal in {'login_unknown', 'code_verification', 'authenticator_code',
                            'email_code', 'passkey', 'create_passkey', 'password_entry'}
    arrived = getattr(bm, 'date', None) or now
    if not in_work_hours(now):
        # A deferred 15:59 request must not become a general bot reply at 16:00.
        return 'hold' if in_work_hours(arrived) and not login_start else 'normal'
    if arrived.astimezone(BAGHDAD).date() != now.astimezone(BAGHDAD).date():
        return 'hold'
    try:
        if await run_io(_services.paused):
            return 'hold'
        if login_start:
            cancel_immediately(bm.chat.id, now)
            await run_io(event, bm.chat.id, bm.business_connection_id, bm.message_id, 'code', now)
            return 'login'
        row = await run_io(event, bm.chat.id, bm.business_connection_id, bm.message_id, 'customer', arrived)
        if not row:
            return 'hold'
        # Only sign-in continuations are allowed after requesting a code, never
        # general FAQ/sales messages from the same customer.
        answer = normalize(text).rstrip(' .!،؟')
        if row['status'] == 'code' and (signal in {'success', 'problem'} or answer in {
            'ايميل', 'الايميل', 'بريد', 'البريد', 'email', 'مصادقه', 'المصادقه', 'التطبيق', 'تطبيق'}):
            return 'login'
        if row['status'] == 'waiting' and day_key(bm.chat.id, now) not in _cancelled:
            key = day_key(bm.chat.id, now)
            if key not in _timers:
                due = datetime.fromisoformat(row['due_at'].replace('Z', '+00:00'))
                _timers[key] = create_timer(wait_for_reply(context, row, max(0, (due-now).total_seconds())))
        return 'hold'
    except Exception:
        await failure(context)
        return 'hold'


@background_job
async def wait_for_reply(context, row, delay):
    key = (int(row['chat_id']), str(row['work_day']))
    try:
        await asyncio.sleep(delay)
        await send_waiting(context, row)
    except asyncio.CancelledError:
        raise
    except Exception:
        await failure(context)
    finally:
        if _timers.get(key) is asyncio.current_task():
            _timers.pop(key, None)


async def send_waiting(context, row):
    now = _services.clock()
    chat_id = int(row['chat_id'])
    key = day_key(chat_id, now)
    if not in_work_hours(now) or key[1] != str(row['work_day']):
        await run_io(lambda: table().update({'status': 'expired'}).eq('owner_id', _services.owner_id)
            .eq('chat_id', chat_id).eq('work_day', row['work_day']).eq('status', 'waiting').execute())
        return
    if key in _cancelled or await run_io(_services.paused):
        return
    try:
        # Re-read revision: additional messages never move the due time.
        fresh = await run_io(get_row, chat_id, now)
        if not fresh or fresh['status'] != 'waiting':
            return
        claimed = await run_io(lambda: table().update({'status': 'sending', 'revision': fresh['revision']+1})
            .eq('owner_id', _services.owner_id).eq('chat_id', chat_id).eq('work_day', row['work_day'])
            .eq('status', 'waiting').eq('revision', fresh['revision']).lte('due_at', now.isoformat()).execute().data)
        if not claimed:
            return
        if await run_io(_services.owner_replied_since, chat_id, fresh['first_at']):
            await owner_answered(context, chat_id, fresh['business_id'])
            return
        latest = await run_io(get_row, chat_id, now)
        if (not latest or latest['status'] != 'sending' or key in _cancelled
                or not in_work_hours(_services.clock()) or await run_io(_services.paused)):
            return
        token = _waiting_reply.set(True)
        try:
            await context.bot.send_message(chat_id=chat_id, business_connection_id=latest['business_id'], text=WAIT_TEXT)
        finally:
            _waiting_reply.reset(token)
        await run_io(lambda: table().update({'status': 'sent'})
            .eq('owner_id', _services.owner_id).eq('chat_id', chat_id).eq('work_day', row['work_day'])
            .eq('status', 'sending').execute())
        await run_io(_services.archive, chat_id, None, None, sender_type='bot', message_text=WAIT_TEXT)
    except Exception:
        # No retries of an unknown Telegram outcome. A committed claim remains
        # sending for owner review and prevents duplicate waiting messages.
        await failure(context)


@background_job
async def work_hours_worker(context):
    try:
        now = _services.clock()
        if not in_work_hours(now):
            return
        rows = await run_io(lambda: table().select('*').eq('owner_id', _services.owner_id)
            .eq('work_day', day_key(0, now)[1]).eq('status', 'waiting')
            .lte('due_at', now.isoformat()).limit(50).execute().data)
        for row in rows or []:
            await send_waiting(context, row)
    except Exception:
        await failure(context)


def track_handler(callback):
    @wraps(callback)
    async def wrapped(update, context, *args, **kwargs):
        user = getattr(update, 'effective_user', None)
        bm = getattr(update, 'business_message', None) or getattr(update, 'edited_business_message', None)
        user = getattr(bm, 'from_user', None) or user
        manual = bool(user and user.id == _services.owner_id)
        token = _manual_reply.set(manual)
        origin = _reply_context.set((context,bm) if bm and not manual else None)
        login_token = login_reply_authorized.set(False)
        try:
            return await callback(update, context, *args, **kwargs)
        finally:
            login_reply_authorized.reset(login_token)
            _reply_context.reset(origin)
            _manual_reply.reset(token)
    return wrapped


def install_handler_tracking(app):
    for group in app.handlers.values():
        for handler in group:
            handler.callback = track_handler(handler.callback)


def build_work_hours_bot(token, base_bot=None):
    if base_bot is None:
        from telegram.ext import ExtBot
        base_bot = ExtBot

    class WorkHoursBot(base_bot):
        async def record_owner_message(self, sent, args, kwargs):
            if _manual_reply.get() and kwargs.get('business_connection_id'):
                chat_id = kwargs.get('chat_id', args[0] if args else None)
                if chat_id is not None:
                    await owner_answered(type('Context', (), {'bot': self})(), chat_id,
                                         kwargs['business_connection_id'], sent.message_id)
            return sent

        async def send_message(self, *args, **kwargs):
            origin = _reply_context.get()
            if (origin and kwargs.get('business_connection_id') and not _manual_reply.get()
                    and not _waiting_reply.get() and not login_reply_authorized.get()):
                context, bm = origin
                target = kwargs.get('chat_id', args[0] if args else None)
                now = _services.clock()
                arrived = getattr(bm, 'date', None) or now
                if int(target) == bm.chat.id and (in_work_hours(now) or in_work_hours(arrived)):
                    await route(context, bm, text=getattr(bm, 'text', None))
                    await hold(context, bm, getattr(bm, 'text', None))
                    raise WorkHoursReplyBlocked('General reply blocked by work-hours policy')
            sent = await super().send_message(*args, **kwargs)
            # Recording failure cannot cause a retry of this successful send.
            return await self.record_owner_message(sent, args, kwargs)

        async def send_photo(self, *args, **kwargs):
            return await self.record_owner_message(await super().send_photo(*args, **kwargs), args, kwargs)

        async def send_document(self, *args, **kwargs):
            return await self.record_owner_message(await super().send_document(*args, **kwargs), args, kwargs)

        async def send_voice(self, *args, **kwargs):
            return await self.record_owner_message(await super().send_voice(*args, **kwargs), args, kwargs)

        async def send_video(self, *args, **kwargs):
            return await self.record_owner_message(await super().send_video(*args, **kwargs), args, kwargs)
    return WorkHoursBot(token=token)
