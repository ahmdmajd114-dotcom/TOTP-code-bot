"""ChatGPT sign-in guidance. Never stores credentials or generates codes.

Code authority, subscription checks and retry limits remain in the store adapter.
Only an explicit authenticator challenge can open that path.
"""
import asyncio
import logging
import re
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from runtime_scheduler import run_io, background_job

log = logging.getLogger(__name__)
PASSWORD = 'Continue with password — المتابعة باستخدام كلمة المرور'
ALTERNATIVE = 'Try another method — جرّب طريقة أخرى'
INTRO = (f'حتى تدخل للحساب، اكتب الإيميل، وبعدها اختَر «{PASSWORD}» واكتب كلمة المرور اللي أرسلتها إلك. '
         'إذا طلب رمز تطبيق المصادقة، دزلي حتى أرسله إلك.')
EMAIL = (f'هذي الشاشة تطلب رمز الإيميل. اختَر «{PASSWORD}» أسفل الصفحة، '
         'وبعدها اكتب كلمة المرور اللي أرسلتها إلك. إذا الخيار مو ظاهر، سكّر لوحة المفاتيح.')
PASSKEY = (f'إذا ظهرت نافذة مفتاح المرور، اختَر «{ALTERNATIVE}» إذا موجود، '
           'وبعدها كلمة المرور. وإذا ظهر بعد كلمة المرور، اختَر تطبيق المصادقة من طرق التحقق المتاحة. '
           'إذا الخيارات مختلفة، دزلي صورة الشاشة.')
CREATE_PASSKEY = 'إذا هذا اقتراح لإنشاء مفتاح مرور جديد، اختَر «ليس الآن» أو «تخطي» إذا موجود. إذا ماكو، دزلي صورة الشاشة.'
CLARIFY = 'هسه يطلب رمز الإيميل، لو رمز تطبيق المصادقة؟ إذا تحب دزلي صورة الشاشة.'
SCREENSHOT = 'دزلي صورة الشاشة اللي واقف عليها هسه حتى أوضحلك الخطوة المناسبة.'
DONE = 'تمام، هنيئاً إلك 🌷'
LOGIN_PHOTOS = {'email_code', 'authenticator_code', 'passkey', 'create_passkey',
                'password_entry', 'login_unknown', 'code_verification'}
_services = None
login_reply_authorized = ContextVar('confirmed_login_reply', default=False)


@dataclass
class LoginServices:
    client: object
    owner_id: int
    accounts: object
    selected: object
    select: object
    paused: object


def configure_login(config):
    global _services
    _services = config


def normalize(text):
    text = str(text or '').lower().strip()
    text = re.sub('[ًٌٍَُِّْـ]', '', text)
    return text.translate(str.maketrans('أإآىة', 'ااايه'))


def text_signal(text, code_request=False):
    value = normalize(text)
    if any(x in value for x in ('باسكي', 'باس كي', 'passkey', 'مفتاح المرور')):
        if any(x in value for x in ('انشاء', 'اضاف', 'create', 'set up')):
            return 'create_passkey'
        return 'passkey'
    # Problems must win over success or an authenticator keyword.
    if any(x in value for x in ('ما ظهر', 'مو ظاهر', 'ماكو خيار', 'ما لكيت', 'ما لقيت',
                                'بعده يطلب', 'رجع يطلب', 'ما يطلب', 'ما طلب', 'مو رمز', 'مو كود',
                                'مو تطبيق', 'ليس رمز', 'لا يطلب', 'بدون رمز', 'ما اريد',
                                'ما دخل', 'ما اشتغل', 'غلط', 'incorrect', 'invalid', 'not asking')):
        return 'problem'
    if any(x in value for x in ('رمز الايميل', 'كود الايميل', 'رمز البريد', 'كود البريد', 'check your inbox', 'email code')):
        return 'email_code'
    if any(x in value for x in ('تطبيق المصادقه', 'تطبيق مصادقه', 'رمز المصادقه', 'كود المصادقه',
                                'authenticator', 'google auth', 'توثيق ثنائي')):
        if any(x in value for x in ('شنو', 'شلون', 'وين', 'كيف', 'ليش', 'what is', 'how to', 'where')):
            return 'login_unknown'
        return 'authenticator_code'
    if value.rstrip(' .!،؟🌷') in {'دخلت', 'اشتغل', 'تم الدخول', 'دخل الحساب', 'it worked', 'logged in'}:
        return 'success'
    if code_request or re.search(r'\b(otp|code)\b', value) or re.search(r'(?<!\w)(?:ال)?(?:كود|رمز)(?!\w)', value):
        return 'login_unknown'
    return None


@dataclass(frozen=True)
class Decision:
    text: str = ''
    stage: str = ''
    code: bool = False


def decide(signal, stage=None):
    if signal == 'authenticator_code':
        return Decision(stage='authenticator', code=True)
    if signal == 'email_code':
        return Decision(EMAIL if stage != 'email' else SCREENSHOT, 'email')
    if signal == 'passkey':
        return Decision(PASSKEY, 'passkey')
    if signal == 'create_passkey':
        return Decision(CREATE_PASSKEY, 'passkey')
    if signal == 'password_entry':
        return Decision('اكتب كلمة المرور اللي أرسلتها إلك ويا بيانات الحساب، وإذا طلب رمز تطبيق المصادقة دزلي.', 'password')
    if signal == 'problem' and stage and stage != 'complete':
        return Decision(SCREENSHOT, 'review')
    if signal == 'success' and stage and stage != 'complete':
        return Decision(DONE, 'complete')
    if signal in {'login_unknown', 'code_verification'}:
        if stage == 'authenticator':
            return Decision(stage='authenticator', code=True)
        return Decision(CLARIFY, 'clarify')
    return Decision()


def table():
    return _services.client.table('chatgpt_login_sessions')


def load(chat_id, account_id):
    rows = table().select('*').eq('owner_id', _services.owner_id).eq('chat_id', chat_id).eq('account_id', str(account_id)).execute().data
    return rows[0] if rows else None


def recent_stage(row):
    if not row:
        return None
    when = datetime.fromisoformat(row['updated_at'].replace('Z', '+00:00'))
    # A confirmed challenge is short-lived; general journey state can survive
    # a longer pause without authorizing yesterday's challenge.
    ttl = timedelta(minutes=30) if row['stage'] == 'authenticator' else timedelta(hours=24)
    return row['stage'] if datetime.now(timezone.utc) - when < ttl else None


async def notify_failure(context, chat_id):
    log.error('Login assistance unavailable for chat %s; no automatic code authorized', chat_id)
    try:
        await context.bot.send_message(chat_id=_services.owner_id,
            text=f'⚠️ تعذرت متابعة دخول الزبون ({chat_id}). راجع التهيئة/سجل الخدمة؛ لم أرسل رمزاً تلقائياً من هذا المسار.')
    except Exception:
        log.error('Could not notify owner about login assistance')


async def start_delivery(context, request, message_id):
    """Called only after Telegram acknowledged the credentials, not on renewal.

    It cannot fail or retry the underlying delivery or change its funding.
    """
    try:
        account = await run_io(lambda: _services.client.table('delivery_accounts').select('legacy_id')
            .eq('id', request['account_id']).eq('owner_id', _services.owner_id).single().execute().data)
        started = await run_io(lambda: _services.client.rpc('start_chatgpt_login', {
            'p_owner': _services.owner_id, 'p_chat': request['customer_id'],
            'p_account': str(account['legacy_id']), 'p_business': request['business_id'],
            'p_message': message_id, 'p_sent': request.get('sent_at') or datetime.now(timezone.utc).isoformat()}).execute().data)
        if not started:
            return
        if not await run_io(_services.select, request['customer_id'], str(account['legacy_id'])):
            raise RuntimeError('Could not select delivered account')
        # An exact five-second timer for this delivery; the worker recovers
        # queued notes across restart. Atomic claim prevents duplicate timers.
        sent_at = datetime.fromisoformat((request.get('sent_at') or datetime.now(timezone.utc).isoformat()).replace('Z', '+00:00'))
        remaining = max(0, 5 - (datetime.now(timezone.utc) - sent_at).total_seconds())
        context.application.create_task(delayed_note(context, request['customer_id'], str(account['legacy_id']), remaining))
    except Exception:
        await notify_failure(context, request['customer_id'])


@background_job
async def delayed_note(context, chat_id, account_id, delay=5):
    await asyncio.sleep(delay)
    await send_note(context, chat_id, account_id)


async def send_note(context, chat_id, account_id):
    try:
        if await run_io(_services.paused):
            return
        # Claim before sending. Unknown Telegram outcomes are never repeated.
        rows = await run_io(lambda: table().update({'note_status': 'sending'})
            .eq('owner_id', _services.owner_id).eq('chat_id', chat_id).eq('account_id', account_id)
            .eq('note_status', 'queued').eq('stage', 'delivered')
            .gte('due_at', (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat())
            .lte('due_at', datetime.now(timezone.utc).isoformat()).execute().data)
        if not rows:
            return
        row = rows[0]
        accounts = await run_io(_services.accounts, chat_id)
        if not any(str(a['id']) == account_id for a in accounts):
            await run_io(lambda: table().update({'note_status': 'skipped'}).eq('id', row['id']).execute())
            return
        await context.bot.send_message(chat_id=chat_id, business_connection_id=row['business_id'], text=INTRO)
        await run_io(lambda: table().update({'note_status': 'sent'}).eq('id', row['id']).execute())
    except Exception:
        await notify_failure(context, chat_id)


@background_job
async def login_worker(context):
    try:
        rows = await run_io(lambda: table().select('id,chat_id,account_id,due_at').eq('owner_id', _services.owner_id)
            .eq('note_status', 'queued').eq('stage', 'delivered')
            .lte('due_at', datetime.now(timezone.utc).isoformat()).limit(20).execute().data)
        for row in rows or []:
            if datetime.fromisoformat(row['due_at'].replace('Z', '+00:00')) < datetime.now(timezone.utc) - timedelta(minutes=15):
                await run_io(lambda: table().update({'note_status': 'skipped'}).eq('id', row['id']).eq('note_status', 'queued').execute())
                continue
            await send_note(context, row['chat_id'], row['account_id'])
    except Exception:
        log.error('Login guidance worker unavailable; check chatgpt_login_sessions migration')


async def handle_login(context, bm, *, text=None, photo=None, code_request=False):
    """Return True when handled, 'code' for confirmed authenticator, else False.

    Unknown photo classifications never grant code permission. No state or
    authorization error is converted into an empty account list.
    """
    signal = photo.intent if photo else text_signal(text, code_request)
    if not signal and not photo and normalize(text).rstrip(' .!،؟') in {
        'ايميل', 'الايميل', 'بريد', 'البريد', 'email', 'مصادقه', 'المصادقه', 'التطبيق', 'تطبيق'}:
        signal = 'context_answer'
    if not signal or (photo and signal not in LOGIN_PHOTOS):
        return False
    if photo and photo.confidence < .82:
        signal = 'login_unknown'
    try:
        if await run_io(_services.paused):
            return True
        accounts = await run_io(_services.accounts, bm.chat.id)
        if not accounts:
            return True if photo or code_request else False
        selected = _services.selected(accounts)
        # A screenshot replying to a credentials message can name another
        # account. The adapter handles explicit selections; don't switch here.
        account_id = str(selected['id'])
        row = await run_io(load, bm.chat.id, account_id)
        stage = recent_stage(row)
        if stage == 'clarify' and not photo:
            answer = normalize(text).rstrip(' .!،؟')
            if answer in {'ايميل', 'الايميل', 'بريد', 'البريد', 'email'}:
                signal = 'email_code'
            elif answer in {'مصادقه', 'المصادقه', 'التطبيق', 'تطبيق'}:
                signal = 'authenticator_code'
        # A new ambiguous screenshot invalidates prior challenge evidence.
        # Never infer its type from a previous authenticator step.
        choice = decide(signal, None if photo and signal in {'login_unknown', 'code_verification'} else stage)
        if not choice.stage:
            return False
        if row and bm.message_id <= row['last_message_id']:
            return True
        payload = dict(owner_id=_services.owner_id, chat_id=bm.chat.id, account_id=account_id,
                       business_id=bm.business_connection_id, stage=choice.stage,
                       last_message_id=bm.message_id, updated_at=datetime.now(timezone.utc).isoformat(),
                       note_status='skipped')
        if row:
            changed = await run_io(lambda: table().update(payload).eq('id', row['id'])
                .eq('last_message_id', row['last_message_id']).execute().data)
            if not changed:
                return True
        else:
            await run_io(lambda: table().insert(payload).execute())
        if choice.code:
            # Fix the account used by the existing retry/permission path.
            if not await run_io(_services.select, bm.chat.id, account_id):
                raise RuntimeError('Could not select login account')
            login_reply_authorized.set(True)
            return 'code'
        login_reply_authorized.set(True)
        await context.bot.send_message(chat_id=bm.chat.id, business_connection_id=bm.business_connection_id, text=choice.text)
        return True
    except Exception:
        await notify_failure(context, bm.chat.id)
        return True


async def account_chosen(context, message, chat_id, account_id, business_id):
    """Selecting an account is not evidence that its authenticator was requested.

    Picker messages predate the selection, so they must not be used as a new
    customer message ID or consume a code attempt. The next customer response
    passes through the usual state/permission/retry path.
    """
    try:
        if await run_io(_services.paused):
            return
        row = await run_io(load, chat_id, account_id)
        payload = dict(owner_id=_services.owner_id, chat_id=chat_id, account_id=str(account_id),
                       business_id=business_id, stage='clarify', note_status='skipped',
                       updated_at=datetime.now(timezone.utc).isoformat())
        if row:
            await run_io(lambda: table().update(payload).eq('id', row['id']).execute())
        else:
            payload['last_message_id'] = message.message_id
            await run_io(lambda: table().insert(payload).execute())
        login_reply_authorized.set(True)
        await context.bot.send_message(chat_id=chat_id, business_connection_id=business_id, text=CLARIFY)
    except Exception:
        await notify_failure(context, chat_id)


async def owner_takeover(chat_id):
    if not _services:
        return
    try:
        await run_io(lambda: table().update({'note_status': 'skipped'}).eq('owner_id', _services.owner_id)
            .eq('chat_id', chat_id).eq('note_status', 'queued').execute())
    except Exception:
        log.error('Could not suppress pending login note for owner takeover')
