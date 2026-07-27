"""
بوت خدمة الزبائن (TOTP + ردود FAQ) — يرد على رسائل حسابك الشخصي
(Telegram Business) تلقائياً.

الفكرة:
- الأساس مطابقة كلمات مفتاحية مباشرة (سريع وموثوق، بدون ذكاء اصطناعي)
  لكل الفئات: سلام، ترحيب، شكر، طرق الدفع، وكل المنتجات.
- الذكاء الاصطناعي (Groq) يتفعل بس بحالة وحدة: لما الرسالة فيها ذكر
  chatgpt + كلمة شكوى بنفس الوقت، عشان يميز هل هذا طلب شراء فعلي أو
  شكوى بمشكلة باشتراك موجود اصلا. لو شكوى، البوت يسكت وينبه الأونر.
- منع تكرار نفس رد الـ FAQ لنفس الزبون خلال ساعة (ما عدا الكود).
- طلب الكود (TOTP) حصري للزبائن المربوطين مسبقاً بأمر /link من الأونر،
  وله نظام عداد محاولات منفصل (ريستارت بعد 3، توقف بعد 5).
- أنت (owner) تضيف حساب جديد بأمر /addaccount
- أنت تربط زبون معين بحساب معين بأمر /link داخل محادثته
- أنت تصفر عداد محاولات الكود بأمر /resetcode داخل محادثة الزبون
"""

import os
import re
import asyncio
import logging
import pyotp
import httpx
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    filters,
)

# ------------------------------------------------------------------
# توافق Python 3.14: بعض إصدارات python-telegram-bot تعتمد على وجود
# event loop جاهز بالـ Main Thread عبر asyncio.get_event_loop().
# بايثون 3.14 ألغى هذا السلوك الضمني، فنجهز event loop يدوياً هنا
# قبل ما تشتغل المكتبة.
# ------------------------------------------------------------------
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# إعدادات (من Environment Variables)
# ------------------------------------------------------------------
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_USER_ID = int(os.environ["OWNER_USER_ID"])  # الـ Telegram User ID تبعك انت (owner)
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------------------------------------------------------------
# منع تكرار ردود الـ FAQ لنفس الزبون خلال ساعة — نفس نظام النظام
# القديم. مخزن بذاكرة البرنامج (ينمحي عند اعادة تشغيل البوت، وهذا
# مقبول). المفتاح: (chat_id, category) → آخر وقت انبعث فيه هذا الرد.
# ------------------------------------------------------------------
FAQ_REPEAT_COOLDOWN_SECONDS = 60 * 60  # ساعة كاملة
_faq_reply_log: dict[tuple[int, str], datetime] = {}


def should_send_faq_reply(chat_id: int, category: str) -> bool:
    """
    يتحقق هل نرسل رد هذي الفئة لهذا الزبون الحين، أو انتظرناها خلال
    آخر ساعة (ونتجاهلها منعا للتكرار). يحدّث الطابع الزمني لو رح نرسل.
    """
    key = (chat_id, category)
    now = datetime.now(timezone.utc)
    last_sent = _faq_reply_log.get(key)

    if last_sent is not None and (now - last_sent) < timedelta(seconds=FAQ_REPEAT_COOLDOWN_SECONDS):
        return False

    _faq_reply_log[key] = now
    return True


# ------------------------------------------------------------------
# نظام الردود التلقائية (FAQ) — لكل الزبائن بدون شرط ربط. الأساس هو
# مطابقة الكلمات المفتاحية مباشرة (بدون ذكاء اصطناعي) — نفس الأسلوب
# الموثوق اللي كان يشتغل بالنظام القديم. كل عنصر: (اسم الفئة، قائمة
# كلمات مفتاحية واسعة، نص الرد). الكلمات المفتاحية كثيرة عمداً لتغطية
# أكبر قدر من الصياغات واللهجات والأخطاء الإملائية الشائعة.
# ------------------------------------------------------------------
FAQ_RULES = [
    (
        "سلام",
        [
            "السلام عليكم", "سلام عليكم", "سلام", "السلام", "سلامو عليكم", "سلامة عليكم",
            "السلام عليكم ورحمة الله", "assalamu alaikum", "salam alaikum",
        ],
        "وعليكم السلام ورحمة الله وبركاته اهلا وسهلا",
    ),
    (
        "ترحيب",
        [
            "هلا", "مرحبا", "مرحبتين", "هاي", "هلو", "hi", "hello", "hey",
            "هلابيك", "هلا بيك", "صباح الخير", "مساء الخير", "شلونك",
            "شلونكم", "اهلين", "مرحب",
        ],
        "اهلا وسهلا",
    ),
    (
        "شكر",
        [
            "شكرا", "شكراً", "شكرا جزيلا", "شكرا جزيلاً", "مشكور", "مشكورين",
            "تسلم", "تسلمين", "يعطيك العافية", "الله يعطيك العافية",
            "عاشت ايدك", "عاشت ايدج", "ما قصرت", "ما قصرتوا", "يسلمو",
            "يسلمولي", "الله يخليك", "الله يخليج", "تسلملي", "مشكوره",
            "ثانكيو", "thanks", "thank you", "thx",
        ],
        "اهلا وسهلا",
    ),
    (
        "chatgpt",
        [
            "chatgpt", "chat gpt", "جات", "چات", "جي بي تي", "شات جي بي تي",
            "شات", "چات جي بي تي", "شات جيبيتي", "جيبيتي", "gpt",
            "open ai", "openai", "اوبن اي اي", "چاتجيبيتي", "جاتي",
        ],
        "بلي موجود هاي الباقات المتوفرة Chat GPT\n"
        "اشتراك خاص شهرين 39\n"
        "اشتراك شهر مشترك 8\n"
        "شهرين مشترك 15",
    ),
    (
        "طرق_الدفع",
        [
            "طرق الدفع", "طريقة الدفع", "شلون ادفع", "كيف ادفع", "وين ادفع",
            "شلون الدفع", "طرق التسديد", "ماستر", "احولك", "احول", "كيفية الدفع", "شنو طرق الدفع",
            "زين كاش", "سوبر كي", "زد كاش",
        ],
        "طرق الدفع\n"
        "رقم زين كاش التالي\n"
        "07818103404\n\n"
        "ورقم السوبر كي الرقم التالي\n"
        "917390524895\n"
        "باسم احمد عبد الماجد",
    ),
    (
        "دفع_رصيد",
        [
            "رصيد", "كارت الرصيد", "كارت رصيد", "بالرصيد", "ادفع رصيد",
            "اثير", "كروت رصيد", "كارت اثير",
        ],
        "تمام لا بأس رصيد اثير (زين)",
    ),
    (
        "anki",
        ["انكي", "anki", "آنكي", "انچي"],
        "متوفر تنزيل تطبيق بواسطة حساب اب ستور سعره 5 يبقى موجود دائمي (الا اذا حذفته)",
    ),
    (
        "freenote",
        ["فرينوت", "freenote", "free note", "فري نوت"],
        "متوفر سعره 5 حساب مُفعل المدة سنة",
    ),
    (
        "goodnote",
        [
            "گودنوت", "كودنوت", "كود نوت", "goodnote", "good note",
            "جودنوت", "غودنوت",
        ],
        "بلي موجود سعره 5 مدة سنة حساب تسجلوا يمكم",
    ),
    (
        "canva",
        ["كانفا", "canva", "كنفا"],
        "نعم متوفر اشتراك سنة سعره 25 الف",
    ),
    (
        "تليجرام_مميز",
        [
            "تلي مميز", "تليجرام مميز", "تليكرام مميز", "تليجرام بريميوم",
            "telegram premium", "premium",
        ],
        "متوفر تلث اشهر ب 25 وسنة ب55 الف",
    ),
]

# كلمات تدل على وجود شكوى/مشكلة تقنية — تستخدم مع كلمات chatgpt عشان
# نقرر هل نفعّل فحص الذكاء الاصطناعي (شوف classify_chatgpt_context)
COMPLAINT_KEYWORDS = [
    "مشكلة", "مشكله", "ما يشتغل", "خربان", "وقف", "ما يفتح",
    "خطأ", "غلط", "ما يدخل", "ما يقبل", "طلع", "طلعت", "رفض", "عطل",
    "معطل", "خرب",
]

SEEN_DELAY_SECONDS = 5       # فترة قبل ما البوت "يشوف" الرسالة (قبل علامة الصح الزرقاء)
PRE_TYPING_PAUSE_SECONDS = 3  # فترة صمت بعد علامة الصح، قبل ما يبدأ "يكتب..."
TYPING_DURATION_SECONDS = 6   # مدة ظهور "يكتب..." قبل إرسال الرد

LINK_PATTERN = re.compile(r"^/link\s+(\S+)$", re.IGNORECASE)
ADD_PATTERN = re.compile(r"^/addaccount\s+(\S+)\s+(\S+)(?:\s+(.+))?$", re.IGNORECASE)
RESETCODE_PATTERN = re.compile(r"^/resetcode$", re.IGNORECASE)

# كلمات مفتاحية لطلب الكود
CODE_REQUEST_KEYWORDS = [
    "كود", "رمز", "code", "otp", "الكود", "الرمز",
]

CODE_RETRY_RESET_HOURS = 12  # يصفر عداد محاولات الكود تلقائياً بعد هالمدة

# ------------------------------------------------------------------
# طبقة الذكاء الاصطناعي — محدودة جداً ومستخدمة بس بحالة وحدة:
# لما الرسالة فيها كلمة مفتاحية لـ chatgpt، نحتاج نميز هل هذا طلب
# شراء/سؤال سعر فعلي، أو شكوى بمشكلة باشتراك موجود اصلا (يوقف الرد،
# وينبه الأونر). كل باقي التصنيف يعتمد على الكلمات المفتاحية مباشرة.
# ------------------------------------------------------------------
CHATGPT_CONTEXT_PROMPT = (
    "رسالة زبون بمتجر عراقي تحتوي ذكر ChatGPT (جات/چات/جي بي تي). "
    "حدد هل هذي شكوى بمشكلة باشتراك ChatGPT موجود اصلا عنده، او طلب "
    "شراء/سؤال سعر/توفر فعلي. رد بكلمة وحدة بالضبط: 'شكوى' او 'شراء'. "
    "امثلة: 'عندي مشكلة بجات' → شكوى. 'جات ما يشتغل' → شكوى. "
    "'اريد جات' → شراء. 'اشتراك جات موجود؟' → شراء. "
    "'كم سعر جات' → شراء."
)


async def classify_chatgpt_context(text: str) -> str:
    """
    يستخدم الذكاء الاصطناعي (Groq) بس لتمييز شكوى عن شراء وقت وجود
    ذكر لـ chatgpt بالرسالة. يرجع 'شكوى' او 'شراء' (افتراضي 'شراء' لو
    فشل الاتصال، عشان البوت يرد بأسعار chatgpt بدل ما يسكت — الاحتمال
    الأكثر شيوعاً هو طلب شراء فعلي).
    """
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": GROQ_MODEL,
                    "temperature": 0,
                    "max_tokens": 10,
                    "messages": [
                        {"role": "system", "content": CHATGPT_CONTEXT_PROMPT},
                        {"role": "user", "content": text},
                    ],
                },
            )
        resp.raise_for_status()
        data = resp.json()
        raw = data["choices"][0]["message"]["content"].strip()
        if "شكوى" in raw:
            return "شكوى"
        return "شراء"
    except Exception:
        logger.exception("Groq chatgpt-context check failed — defaulting to شراء")
        return "شراء"


def keyword_match_categories(text: str) -> list[str]:
    """
    المصنف الأساسي — مطابقة كلمات مفتاحية مباشرة (بدون ذكاء اصطناعي).
    يرجع قائمة فئات مرتبة حسب موقع ظهور الكلمة المفتاحية بالرسالة.
    """
    normalized = text.strip().lower()
    matches: list[tuple[int, str]] = []  # (موقع الظهور، اسم الفئة)

    for category, keywords, _ in FAQ_RULES:
        best_position = None
        for kw in keywords:
            pos = normalized.find(kw.lower())
            if pos != -1 and (best_position is None or pos < best_position):
                best_position = pos
        if best_position is not None:
            matches.append((best_position, category))

    for kw in CODE_REQUEST_KEYWORDS:
        pos = normalized.find(kw.lower())
        if pos != -1:
            matches.append((pos, "طلب_كود"))
            break  # فئة وحدة كافية لطلب الكود

    matches.sort(key=lambda m: m[0])
    return [category for _, category in matches]


def has_complaint_keyword(text: str) -> bool:
    normalized = text.strip().lower()
    return any(kw in normalized for kw in COMPLAINT_KEYWORDS)


async def classify_intent(text: str) -> tuple[list[str], bool]:
    """
    المصنف الرئيسي المستخدم بكل رسالة. الأساس مطابقة كلمات مفتاحية
    مباشرة (سريع وموثوق، بدون ذكاء اصطناعي). الذكاء الاصطناعي يتفعل
    بس لو الرسالة تحتوي كلمة مفتاحية لـ chatgpt، عشان يميز شكوى عن
    شراء (شي ما تقدر الكلمات المفتاحية وحدها تميزه بدقة).

    يرجع (categories, is_chatgpt_complaint):
    - categories: قائمة فئات (ممكن فيها "شكوى_منتج" اذا كانت شكوى
      chatgpt — هذي فئة خاصة تخلي البوت يسكت وينبه الأونر).
    - is_chatgpt_complaint: True لو الذكاء الاصطناعي أكد انها شكوى.
    """
    categories = keyword_match_categories(text)

    if "chatgpt" in categories and has_complaint_keyword(text):
        # فيه ذكر chatgpt + كلمة شكوى بنفس الرسالة — هذا بالضبط الحالة
        # اللي تحتاج فهم AI للتمييز (الكلمات المفتاحية وحدها ما تكفي)
        verdict = await classify_chatgpt_context(text)
        if verdict == "شكوى":
            # نشيل chatgpt من الفئات ونضيف فئة خاصة "شكوى_منتج"
            categories = [c for c in categories if c != "chatgpt"]
            categories.append("شكوى_منتج")
            return categories, True

    return categories, False


def get_reply_for_category(category: str) -> str | None:
    """يرجع نص الرد الجاهز المطابق لفئة FAQ، أو None اذا مو فئة FAQ (كود/شكوى)."""
    for cat_name, _, reply in FAQ_RULES:
        if cat_name == category:
            return reply
    return None


def get_secret_for_chat(chat_id: int) -> tuple[str, str] | None:
    """يرجع (secret, label) للحساب المربوط بهذا الزبون، أو None اذا مو مربوط."""
    link_res = (
        supabase.table("totp_links")
        .select("account_id")
        .eq("chat_id", chat_id)
        .execute()
    )
    if not link_res.data:
        return None

    account_id = link_res.data[0]["account_id"]
    acc_res = (
        supabase.table("totp_accounts")
        .select("secret, label")
        .eq("id", account_id)
        .execute()
    )
    if not acc_res.data:
        return None

    return acc_res.data[0]["secret"], acc_res.data[0].get("label") or ""


def generate_totp_code(secret: str) -> str:
    totp = pyotp.TOTP(secret)
    return totp.now()


# ------------------------------------------------------------------
# نظام تتبع محاولات الكود الفاشلة (code_retry_tracker بقاعدة Supabase)
# التسلسل المتفق عليه لما الزبون يقول "ما صار" بشكل متكرر:
#   محاولة 1، 2  → كود جديد تلقائياً
#   محاولة 3     → رسالة "سوي ريستارت" بدون كود
#   بعد تأكيد الريستارت → كود (تعتبر محاولة 4)
#   محاولة 5     → كود أخير
#   محاولة 6+    → توقف، تنبيه للأونر فقط، بدون كود
# العداد يصفر تلقائياً بعد CODE_RETRY_RESET_HOURS ساعة من آخر محاولة.
# ------------------------------------------------------------------


def _get_retry_state(chat_id: int) -> dict:
    """يرجع حالة العداد الحالية لهذا الزبون، أو حالة ابتدائية اذا ما موجودة."""
    res = (
        supabase.table("code_retry_tracker")
        .select("attempt_count, last_attempt_at, awaiting_restart_confirmation")
        .eq("chat_id", chat_id)
        .execute()
    )
    if not res.data:
        return {
            "attempt_count": 0,
            "last_attempt_at": None,
            "awaiting_restart_confirmation": False,
        }

    row = res.data[0]
    last_attempt_at = row.get("last_attempt_at")

    # تصفير تلقائي بعد مرور CODE_RETRY_RESET_HOURS من آخر محاولة
    if last_attempt_at:
        last_dt = datetime.fromisoformat(last_attempt_at)
        if datetime.now(timezone.utc) - last_dt > timedelta(hours=CODE_RETRY_RESET_HOURS):
            return {
                "attempt_count": 0,
                "last_attempt_at": None,
                "awaiting_restart_confirmation": False,
            }

    return {
        "attempt_count": row.get("attempt_count", 0),
        "last_attempt_at": last_attempt_at,
        "awaiting_restart_confirmation": row.get("awaiting_restart_confirmation", False),
    }


def _save_retry_state(chat_id: int, attempt_count: int, awaiting_restart: bool) -> None:
    supabase.table("code_retry_tracker").upsert(
        {
            "chat_id": chat_id,
            "attempt_count": attempt_count,
            "last_attempt_at": datetime.now(timezone.utc).isoformat(),
            "awaiting_restart_confirmation": awaiting_restart,
        }
    ).execute()


def reset_retry_state(chat_id: int) -> None:
    """يصفر عداد المحاولات يدوياً (يستخدمه أمر owner /resetcode)."""
    supabase.table("code_retry_tracker").upsert(
        {
            "chat_id": chat_id,
            "attempt_count": 0,
            "last_attempt_at": datetime.now(timezone.utc).isoformat(),
            "awaiting_restart_confirmation": False,
        }
    ).execute()


RESTART_MESSAGE = (
    "يبدو انه الكود ما يشتغل معك بشكل صحيح.\n"
    "جرب تسوي التالي: احذف الحساب من تطبيق المصادقة (Authenticator) "
    "وابدأ عملية التسجيل من جديد من الأول، وبعدها راسلني وبعطيك كود جديد."
)

STOPPED_MESSAGE = (
    "يبدو انه فيه مشكلة مستمرة، حولت طلبك لصاحب المتجر مباشرة "
    "وراح يتواصل معك قريباً."
)


def process_code_request(chat_id: int) -> tuple[str | None, bool]:
    """
    يقرر شنو الرد المناسب لطلب كود، حسب حالة عداد المحاولات.

    يرجع (نص الرد أو None، هل نبعث تنبيه خاص "توقف" للأونر).
    نص الرد يكون: كود فعلي، أو رسالة ريستارت، أو None لو نوقف كلياً.
    """
    state = _get_retry_state(chat_id)
    attempt_count = state["attempt_count"]
    awaiting_restart = state["awaiting_restart_confirmation"]

    result = get_secret_for_chat(chat_id)
    if result is None:
        # مو مربوط اصلاً — نفس السلوك القديم، تجاهل صامت
        return None, False

    secret, label = result

    # لو كنا ننتظر تأكيد الريستارت، وهذي رسالة جديدة تطلب كود تعتبر
    # تأكيد ضمني للريستارت → نبعث كود (محاولة 4) ونطفي علامة الانتظار
    if awaiting_restart:
        code = generate_totp_code(secret)
        _save_retry_state(chat_id, attempt_count=4, awaiting_restart=False)
        return f"الكود: {code}\nصالح لمدة 30 ثانية تقريبا", False

    new_count = attempt_count + 1

    if new_count <= 2:
        code = generate_totp_code(secret)
        _save_retry_state(chat_id, attempt_count=new_count, awaiting_restart=False)
        return f"الكود: {code}\nصالح لمدة 30 ثانية تقريبا", False

    if new_count == 3:
        _save_retry_state(chat_id, attempt_count=new_count, awaiting_restart=True)
        return RESTART_MESSAGE, False

    if new_count == 5:
        code = generate_totp_code(secret)
        _save_retry_state(chat_id, attempt_count=new_count, awaiting_restart=False)
        return f"الكود: {code}\nصالح لمدة 30 ثانية تقريبا", False

    # new_count >= 6 (أو أي حالة بعد المحاولة الخامسة) — نوقف ونبلغ الأونر
    _save_retry_state(chat_id, attempt_count=new_count, awaiting_restart=False)
    return None, True


async def _show_typing(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    business_connection_id: str,
    seconds: float,
) -> None:
    """
    يعرض مؤشر 'يكتب...' طول المدة المحددة. مؤشر الكتابة بتليجرام
    يختفي تلقائياً بعد 5 ثواني، فنجدده كل 4 ثواني لحد ما تخلص المدة.
    """
    elapsed = 0.0
    interval = 4.0
    while elapsed < seconds:
        try:
            await context.bot.send_chat_action(
                chat_id=chat_id,
                action=ChatAction.TYPING,
                business_connection_id=business_connection_id,
            )
        except Exception:
            logger.exception("Failed to send typing action")
        step = min(interval, seconds - elapsed)
        await asyncio.sleep(step)
        elapsed += step


async def human_like_reply_sequence(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    business_connection_id: str,
    message_id: int,
) -> None:
    """
    يحاكي تسلسل رد إنسان حقيقي، بالترتيب الزمني التالي:
    1) 5 ثواني: البوت "ما يشوف" الرسالة بعد (ما يسوي شي)
    2) بعد الـ5 ثواني: تنعلّم الرسالة كمقروءة (✓✓ زرقاء تظهر عند الزبون)
    3) 3 ثواني: صمت بعد علامة الصح، قبل ما يبدأ الكتابة
    4) 6 ثواني: يظهر مؤشر 'يكتب...'
    بعدها الكود المستدعي يرسل الرد الفعلي.
    المجموع: 5 + 3 + 6 = 14 ثانية قبل وصول الرد.
    """
    # 1) فترة قبل الرؤية
    await asyncio.sleep(SEEN_DELAY_SECONDS)

    # 2) علّم الرسالة كمقروءة
    try:
        await context.bot.read_business_message(
            business_connection_id=business_connection_id,
            chat_id=chat_id,
            message_id=message_id,
        )
    except Exception:
        logger.exception("Failed to mark business message as read")

    # 3) صمت قبل الكتابة
    await asyncio.sleep(PRE_TYPING_PAUSE_SECONDS)

    # 4) مؤشر الكتابة
    await _show_typing(context, chat_id, business_connection_id, TYPING_DURATION_SECONDS)


async def handle_owner_command(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str) -> bool:
    """يعالج أوامر الأونر: /addaccount و /link. يرجع True اذا كانت الرسالة أمر تم التعامل معه."""

    # /addaccount <link_code> <secret> [label]
    add_match = ADD_PATTERN.match(text.strip())
    if add_match:
        link_code, secret, label = add_match.groups()
        try:
            supabase.table("totp_accounts").insert(
                {"link_code": link_code, "secret": secret, "label": label or ""}
            ).execute()
            await context.bot.send_message(
                chat_id=OWNER_USER_ID,
                text=f"✅ تمت اضافة الحساب.\nرمز الربط: {link_code}\nملاحظة: {label or '—'}",
            )
        except Exception as e:
            logger.exception("addaccount failed")
            await context.bot.send_message(
                chat_id=OWNER_USER_ID,
                text=f"⚠️ فشلت الاضافة — تأكد ان رمز الربط '{link_code}' غير مستخدم سابقاً.\n{e}",
            )
        return True

    # /link <link_code>  (يُرسل داخل محادثة الزبون نفسه)
    link_match = LINK_PATTERN.match(text.strip())
    if link_match:
        link_code = link_match.group(1)
        acc_res = (
            supabase.table("totp_accounts")
            .select("id, label")
            .eq("link_code", link_code)
            .execute()
        )
        if not acc_res.data:
            await context.bot.send_message(
                chat_id=OWNER_USER_ID,
                text=f"⚠️ ما لكيت حساب برمز الربط '{link_code}'. تأكد أضفته بـ /addaccount أول.",
            )
            return True

        account_id = acc_res.data[0]["id"]
        label = acc_res.data[0].get("label") or ""

        supabase.table("totp_links").upsert(
            {"chat_id": chat_id, "account_id": account_id}
        ).execute()

        await context.bot.send_message(
            chat_id=OWNER_USER_ID,
            text=f"✅ تم ربط هذا الزبون بالحساب ({label or link_code}).",
        )
        return True

    # /resetcode  (يُرسل داخل محادثة الزبون نفسه — يصفر عداد محاولات الكود)
    if RESETCODE_PATTERN.match(text.strip()):
        reset_retry_state(chat_id)
        await context.bot.send_message(
            chat_id=OWNER_USER_ID,
            text="✅ تم تصفير عداد محاولات الكود لهذا الزبون، راح يقدر يطلب كود من جديد بشكل طبيعي.",
        )
        return True

    return False


async def notify_owner(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    customer_name: str,
    customer_username: str | None,
    customer_message: str,
    bot_reply: str,
) -> None:
    """
    يرسل تنبيه حقيقي (بإشعار عادي) للأونر يوضح اسم الزبون، chat_id،
    شنو كتب، وشنو رد البوت — لأن ردود البوت نفسها ما توصل إشعار
    (لأنها تنرسل بحساب الأونر نفسه عبر business_connection_id).
    """
    username_part = f" (@{customer_username})" if customer_username else ""
    notification = (
        f"📨 رسالة من: {customer_name}{username_part}\n"
        f"chat_id: {chat_id}\n\n"
        f"💬 كتب:\n{customer_message}\n\n"
        f"🤖 رد البوت:\n{bot_reply}"
    )
    try:
        await context.bot.send_message(chat_id=OWNER_USER_ID, text=notification)
    except Exception:
        logger.exception("Failed to notify owner")


async def on_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعالج كل الرسائل الجاية عن طريق Telegram Business (محادثتك الشخصية)."""
    bm = update.business_message
    if not bm or not bm.text:
        return

    chat_id = bm.chat.id
    text = bm.text
    sender_id = bm.from_user.id if bm.from_user else None

    # اسم الزبون واسم المستخدم (لو موجود) — نستخدمهن بالتنبيه للأونر
    customer_name = bm.chat.full_name or bm.chat.first_name or "غير معروف"
    customer_username = bm.chat.username

    is_from_owner = sender_id == OWNER_USER_ID

    # 1) اذا الرسالة منك انت (owner) — تحقق اذا هي أمر ربط/اضافة
    if is_from_owner:
        handled = await handle_owner_command(update, context, chat_id, text)
        if handled:
            return
        return

    # 2) تصنيف الرسالة — الأساس كلمات مفتاحية مباشرة، والذكاء الاصطناعي
    #    يتفعل بس لو فيه ذكر chatgpt + كلمة شكوى بنفس الرسالة
    categories, is_chatgpt_complaint = await classify_intent(text)
    logger.info(f"Classification for chat_id={chat_id}: {categories}")

    replies_to_send: list[str] = []
    should_notify_stopped = False
    should_notify_complaint = False

    for category in categories:
        if category == "شكوى_منتج":
            # شكوى مؤكدة (AI) بمشكلة باشتراك chatgpt موجود اصلا — البوت
            # ما يرد، بس ينبه الأونر يتدخل شخصياً
            should_notify_complaint = True
            continue

        if category == "طلب_كود":
            # طلب كود — الشرط الأساسي يضل الربط المسبق بـ /link، وبعده
            # عداد المحاولات (process_code_request) يقرر شنو الرد بالضبط
            reply_text, stopped = process_code_request(chat_id)
            if reply_text:
                replies_to_send.append(reply_text)
            if stopped:
                should_notify_stopped = True
            continue

        # فئة FAQ عادية — منع تكرار نفس الرد لنفس الزبون خلال ساعة
        if not should_send_faq_reply(chat_id, category):
            continue
        reply_text = get_reply_for_category(category)
        if reply_text:
            replies_to_send.append(reply_text)

    if should_notify_complaint:
        complaint_notification = (
            f"تنبيه: شكوى محتملة باشتراك ChatGPT\n"
            f"الزبون: {customer_name}" + (f" (@{customer_username})" if customer_username else "") + "\n"
            f"chat_id: {chat_id}\n\n"
            f"كتب: {text}\n\n"
            f"البوت ما رد تلقائياً — يحتاج تدخلك المباشر."
        )
        try:
            await context.bot.send_message(chat_id=OWNER_USER_ID, text=complaint_notification)
        except Exception:
            logger.exception("Failed to send complaint notification to owner")

    if should_notify_stopped:
        stopped_notification = (
            f"توقف الرد التلقائي على الكود!\n"
            f"الزبون: {customer_name}" + (f" (@{customer_username})" if customer_username else "") + "\n"
            f"chat_id: {chat_id}\n\n"
            f"طلب الكود عدة مرات وقال انه ما يشتغل، وتجاوز الحد المسموح "
            f"للمحاولات التلقائية. يحتاج تدخلك المباشر."
        )
        try:
            await context.bot.send_message(chat_id=OWNER_USER_ID, text=stopped_notification)
        except Exception:
            logger.exception("Failed to send stopped-retry notification to owner")

    if not replies_to_send:
        return

    await human_like_reply_sequence(
        context, chat_id, bm.business_connection_id, bm.message_id
    )
    for reply_text in replies_to_send:
        await context.bot.send_message(
            business_connection_id=bm.business_connection_id,
            chat_id=chat_id,
            text=reply_text,
        )
    logger.info(f"Sent {len(replies_to_send)} reply(ies) to chat_id={chat_id}")
    combined_reply = "\n---\n".join(replies_to_send)
    await notify_owner(context, chat_id, customer_name, customer_username, text, combined_reply)


def start_health_server() -> None:
    """
    سيرفر HTTP بسيط جداً بالخلفية، وظيفته الوحيدة الرد بـ 200 OK
    على أي طلب. هذا يخلي Render يفتح بورت (متطلب أساسي عندهم)
    ويخلي خدمات مثل cron-job.org تكدر توصله فتبقيه صاحي (keep-alive).
    ما إله أي علاقة بمنطق البوت نفسه.
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, format, *args):
            pass  # تجاهل لوغات HTTP الروتينية عشان ما تغرق لوغات البوت

    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logger.info(f"Health check server running on port {port}")


def main() -> None:
    start_health_server()

    app = Application.builder().token(BOT_TOKEN).build()

    # فقط تحديثات business_message — نستثني الرسائل العادية بالكامل
    app.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, on_business_message))

    app.run_polling(
        allowed_updates=["business_message", "business_connection", "edited_business_message"]
    )


if __name__ == "__main__":
    main()
