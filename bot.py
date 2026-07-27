"""
بوت خدمة الزبائن (TOTP + ردود FAQ) — يرد على رسائل حسابك الشخصي
(Telegram Business) تلقائياً، باستخدام ذكاء اصطناعي (Groq) لفهم قصد
الزبون الحقيقي من رسالته، بدل مطابقة كلمات مفتاحية بسيطة.

الفكرة:
- كل رسالة زبون تُبعث لـ Groq عشان يحدد القصد (تحية، سؤال شراء، طلب كود...)
- الرد المُرسل يكون دائماً من نص جاهز مسبقاً (FAQ_RULES) — الذكاء الاصطناعي
  يصنّف فقط، ولا يكتب رد حر بنفسه.
- طلب الكود (TOTP) يبقى حصري للزبائن المربوطين مسبقاً بأمر /link من الأونر،
  حتى لو الذكاء الاصطناعي تأكد انه طلب فعلي.
- أنت (owner) تضيف حساب جديد بأمر /addaccount
- أنت تربط زبون معين بحساب معين بأمر /link داخل محادثته
"""

import os
import re
import json
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
# ذاكرة مؤقتة (بذاكرة البرنامج، تنمحي عند اعادة تشغيل البوت) —
# تحفظ آخر رسالة توصل من كل زبون، عشان نقدر نمررها كسياق لـ Groq
# وقت تصنيف رسائل متابعة قصيرة وغامضة مثل "كم سعره؟" أو "متوفر؟"
# اللي ما تحدد المنتج المقصود الا بالرجوع للرسالة اللي قبلها.
# ------------------------------------------------------------------
LAST_MESSAGE_CACHE: dict[int, str] = {}
LAST_MESSAGE_CACHE_MAX_SIZE = 2000  # حد أعلى بسيط لمنع تضخم الذاكرة

# آخر وقت أبلغنا فيه الأونر إن Groq غير متوفر — نستخدمه لمنع إزعاجه
# برسالة مكررة كل رسالة زبون توصل أثناء فترة انقطاع Groq الواحدة
_LAST_AI_FAILURE_NOTIFICATION: datetime | None = None
AI_FAILURE_NOTIFICATION_COOLDOWN_MINUTES = 60

# ------------------------------------------------------------------
# نظام الردود التلقائية (FAQ) — لكل الزبائن بدون شرط ربط
# كل عنصر: (اسم الفئة، قائمة كلمات مفتاحية، نص الرد)
# القصد الأساسي يتحدد عن طريق تصنيف الذكاء الاصطناعي (classify_intent).
# الكلمات المفتاحية هنا تستخدم بطريقتين: (1) لاختيار نص الرد الجاهز بعد
# ما الذكاء الاصطناعي يحدد الفئة، (2) كـ احتياط (fallback) للمطابقة
# المباشرة اذا فشل الاتصال بـ Groq بالكامل (بدل ما البوت يسكت تماماً).
# ------------------------------------------------------------------
FAQ_RULES = [
    (
        "سلام",
        ["السلام عليكم", "سلام عليكم"],
        "وعليكم السلام ورحمة الله وبركاته أهلا وسهلا",
    ),
    (
        "ترحيب",
        ["هلا", "مرحبا", "مرحبتين", "هاي"],
        "أهلا وسهلا",
    ),
    (
        "شكر",
        ["شكرا", "شكراً", "مشكور", "تسلم", "يعطيك العافية", "الله يعطيك العافية"],
        "أهلا وسهلا",
    ),
    (
        "chatgpt",
        ["chatgpt", "chat gpt", "جات", "چات", "جي بي تي", "شات جي بي تي", "شات"],
        "بلي موجود هاي الباقات المتوفرة Chat GPT\n"
        "اشتراك خاص شهرين 39\n"
        "اشتراك شهر مشترك 8\n"
        "شهرين مشترك 15",
    ),
    (
        "طرق_الدفع",
        ["طرق الدفع", "طريقة الدفع", "شلون ادفع", "كيف ادفع"],
        "طرق الدفع\n"
        "رقم زين كاش التالي\n"
        "07818103404\n\n"
        "ورقم السوبر كي الرقم التالي\n"
        "917390524895\n"
        "باسم احمد عبد الماجد",
    ),
    (
        "دفع_رصيد",
        ["رصيد", "كارت الرصيد", "كارت رصيد"],
        "تمام لا بأس رصيد اثير (زين)",
    ),
    (
        "anki",
        ["انكي", "anki"],
        "متوفر تنزيل تطبيق بواسطة حساب اب ستور سعره 5 يبقى موجود دائمي (الا اذا حذفته)",
    ),
    (
        "freenote",
        ["فرينوت", "freenote", "free note"],
        "متوفر سعره 5 حساب مُفعل المدة سنة",
    ),
    (
        "goodnote",
        ["گودنوت", "كودنوت", "كود نوت", "goodnote", "good note"],
        "بلي موجود سعره 5 مدة سنة حساب تسجلوا يمكم",
    ),
    (
        "canva",
        ["كانفا", "canva"],
        "نعم متوفر اشتراك سنة سعره 25 الف",
    ),
    (
        "تليجرام_مميز",
        ["تلي مميز", "تليجرام مميز", "تليكرام مميز"],
        "متوفر تلث اشهر ب 25 وسنة ب55 الف",
    ),
]

SEEN_DELAY_SECONDS = 5       # فترة قبل ما البوت "يشوف" الرسالة (قبل علامة الصح الزرقاء)
PRE_TYPING_PAUSE_SECONDS = 3  # فترة صمت بعد علامة الصح، قبل ما يبدأ "يكتب..."
TYPING_DURATION_SECONDS = 6   # مدة ظهور "يكتب..." قبل إرسال الرد

LINK_PATTERN = re.compile(r"^/link\s+(\S+)$", re.IGNORECASE)
ADD_PATTERN = re.compile(r"^/addaccount\s+(\S+)\s+(\S+)(?:\s+(.+))?$", re.IGNORECASE)
RESETCODE_PATTERN = re.compile(r"^/resetcode$", re.IGNORECASE)

# كل الفئات الممكنة اللي الذكاء الاصطناعي يختار منها — تُبنى تلقائياً
# من أسماء الفئات بـ FAQ_RULES، بالإضافة لثلاث فئات خاصة:
# "طلب_كود" (الزبون يطلب الكود فعلاً الحين لأول مرة)،
# "مشكلة_كود" (الزبون يقول الكود السابق ما اشتغل / صار خطأ)،
# و"لا_شي" (ما فيه قصد واضح).
FAQ_CATEGORY_NAMES = [category for category, _, _ in FAQ_RULES]
ALL_CATEGORIES = FAQ_CATEGORY_NAMES + ["طلب_كود", "مشكلة_كود", "لا_شي"]

CODE_RETRY_RESET_HOURS = 12  # يصفر عداد محاولات الكود تلقائياً بعد هالمدة

CLASSIFIER_SYSTEM_PROMPT = (
    "مصنف نية لبوت متجر عراقي (اشتراكات رقمية). صنّف رسالة الزبون لفئة واحدة "
    "بالضبط من هذي القائمة، بدون شرح:\n\n"
    + "\n".join(f"- {c}" for c in ALL_CATEGORIES)
    + "\n\n"
    "قواعد:\n"
    "- سلام = 'السلام عليكم' فقط. ترحيب = تحية عادية بدون طلب. شكر = اي "
    "صيغة شكر/دعاء (شكرا، تسلم، عاشت ايدك، الله يعطيك العافية).\n"
    "- chatgpt/anki/freenote/goodnote/canva/تليجرام_مميز = طلب شراء/سعر "
    "فعلي وحالي لهذا المنتج بالتحديد. لو الرسالة فيها كلمة شكوى (مشكلة، "
    "ما يشتغل، خربان، وقف، ما يفتح، خطأ) مع اسم المنتج، فهذا اشتراك موجود "
    "وبيه عطل — صنفها لا_شي دائما، مو طلب شراء.\n"
    "- طلب_كود = طلب صريح وفوري للكود الحين لأول مرة (ابعثلي الكود، ودني "
    "اسجل هسه). لو فيه اشارة زمنية مستقبلية (باجر، بعدين، بوقتها، لما، "
    "اذا احتجته) أو الرسالة مجرد سؤال عن آلية العمل (شلون افعل الرمز؟) "
    "بدون طلب فوري صريح، صنفها لا_شي مو طلب_كود.\n"
    "- مشكلة_كود = فقط لو وصلتك ملاحظة 'انبعث كود قريبا' والزبون يقول "
    "الكود فشل (ما صار، صار خطأ، ما يفتح، رفضه).\n"
    "- لا_شي = اي شي ثاني، شكوى، او كلام عام.\n\n"
    "لو وصلتك ملاحظة برسالة الزبون السابقة، واستخدمها لفهم اسئلة متابعة "
    "قصيرة غامضة (مثل: كم سعره؟ متوفر؟ شلون اشتريه؟) وتحديد المنتج "
    "المقصود منها، وصنفها على هذا الاساس.\n\n"
    "امثلة:\n"
    "'باجر اسجل وبوكتها اريد الكود' → لا_شي (مستقبلي)\n"
    "'شلون افعل الرمز؟' → لا_شي (سؤال آلية)\n"
    "'ابعثلي الكود الحين' → طلب_كود\n"
    "'اريد جات' → chatgpt\n"
    "'عندي مشكلة بجات' → لا_شي (شكوى مو شراء)\n"
    "مع ملاحظة كود سابق: 'ما صار' → مشكلة_كود\n"
    "مع ملاحظة 'رسالة الزبون قبل هذي كانت: عندي مشكلة باشتراك جات': "
    "'كم سعره' → لا_شي (السياق يوضح انه سؤال متابعة على مشكلة، مو شراء)\n"
    "مع ملاحظة 'رسالة الزبون قبل هذي كانت: اريد انكي': "
    "'كم سعره' → anki (السياق يوضح المنتج المقصود)\n\n"
    "لو اكثر من قصد حقيقي بنفس الرسالة، ارجعهم مفصولين بفاصلة انكليزية "
    "\",\" فقط (مو '،') بترتيب ظهورهم. استخدم فقط اسماء الفئات اعلاه "
    "بالضبط، بدون اي كلام اضافي."
)


async def classify_intent(
    text: str, recent_code_sent: bool = False, previous_message: str | None = None
) -> tuple[list[str], bool]:
    """
    يبعث نص الزبون لـ Groq (نموذج مجاني وسريع) عشان يحدد القصد.

    يرجع (categories, ai_failed):
    - categories: قائمة أسماء فئات من ALL_CATEGORIES.
    - ai_failed: True اذا فشل الاتصال بالكامل (429 مستمر، خطأ شبكة، إلخ)
      ولازم نستخدم المصنف الاحتياطي بالكلمات المفتاحية بدلاً منه.
      False اذا Groq رد بنجاح (حتى لو رد لا_شي فعلياً كتصنيف صحيح).

    recent_code_sent: اذا True، معناته انبعث كود لهذا الزبون قريباً — نمرر
    هذي المعلومة كسياق للنموذج حتى يقدر يفهم رسائل مثل "ما صار" كإشارة
    لمشكلة بالكود السابق (فئة مشكلة_كود) بدل ما يصنفها لا_شي.

    previous_message: آخر رسالة سابقة من نفس الزبون (لو موجودة بالذاكرة
    المؤقتة) — تساعد النموذج يفهم رسائل متابعة قصيرة وغامضة مثل
    "كم سعره؟" اللي محتاجة سياق الرسالة اللي قبلها لتحديد المنتج المقصود.
    """
    context_notes = []
    if recent_code_sent:
        context_notes.append("انبعث كود لهذا الزبون قبل قليل")
    if previous_message:
        context_notes.append(f"رسالة الزبون قبل هذي كانت: '{previous_message}'")

    user_content = text
    if context_notes:
        note_line = "[ملاحظة: " + " | ".join(context_notes) + "]\n"
        user_content = note_line + text

    request_payload = {
        "model": GROQ_MODEL,
        "temperature": 0,
        "max_tokens": 60,
        "messages": [
            {"role": "system", "content": CLASSIFIER_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    }

    # نحاول مرتين: لو المحاولة الأولى فشلت بـ 429 (تجاوز الحد المسموح
    # بالدقيقة)، ننتظر شوي ونعيد المحاولة مرة وحدة قبل ما نستسلم —
    # هذا يعالج الحالات العابرة (burst قصير)، مو تجاوز الحد اليومي الكامل.
    raw = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {GROQ_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json=request_payload,
                )
            if resp.status_code == 429 and attempt == 0:
                logger.warning("Groq rate-limited (429) — retrying once after short delay")
                await asyncio.sleep(2.0)
                continue
            resp.raise_for_status()
            data = resp.json()
            raw = data["choices"][0]["message"]["content"].strip()
            break
        except Exception:
            logger.exception("Groq classification failed")
            break  # فشل غير 429 (مثل timeout) — لا داعي نعيد المحاولة

    if raw is None:
        # فشل حقيقي بالاتصال — نعلم الطالب يستخدم المصنف الاحتياطي
        return ["لا_شي"], True


    # Groq أحياناً يرجع الفئات مفصولة بفاصلة عربية "،" بدل الفاصلة
    # الإنكليزية العادية "," — نطبّع النص أول (نحول الفاصلة العربية
    # لإنكليزية) قبل التقسيم، حتى ما تنكسر عملية الفصل.
    normalized_raw = raw.replace("،", ",")
    candidates = [c.strip().strip(".") for c in normalized_raw.split(",")]
    candidates = [c for c in candidates if c]  # شيل أي عنصر فاضي
    valid = [c for c in candidates if c in ALL_CATEGORIES]

    if not valid:
        logger.warning(f"Groq returned unrecognized category: {raw!r}")
        return ["لا_شي"], False  # Groq رد فعلياً، بس رده مو مفهوم — هذا مو فشل اتصال

    return valid, False


def get_reply_for_category(category: str) -> str | None:
    """يرجع نص الرد الجاهز المطابق لفئة FAQ، أو None اذا مو فئة FAQ (كود/لا_شي)."""
    for cat_name, _, reply in FAQ_RULES:
        if cat_name == category:
            return reply
    return None


# كلمات مفتاحية بسيطة لطلب الكود — تستخدم بس بالمسار الاحتياطي
# (fallback) لما Groq يفشل بالكامل، عشان طلب الكود الأساسي يضل يشتغل.
FALLBACK_CODE_KEYWORDS = ["كود", "رمز", "code", "otp"]


def classify_intent_fallback(text: str) -> list[str]:
    """
    مصنف احتياطي بسيط (بدون ذكاء اصطناعي) يعتمد على مطابقة كلمات
    مفتاحية مباشرة — يشتغل بس لما classify_intent (عن طريق Groq) يفشل
    بالكامل (429 مستمر، مشكلة شبكة، إلخ)، عشان البوت يضل يجاوب على
    أسئلة FAQ الأساسية بدل ما يسكت تماماً. أقل دقة من الذكاء الاصطناعي
    (ما يفهم السياق أو الفرق بين الشكوى والشراء)، بس أفضل من لا شي.
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

    for kw in FALLBACK_CODE_KEYWORDS:
        pos = normalized.find(kw.lower())
        if pos != -1:
            matches.append((pos, "طلب_كود"))
            break  # فئة وحدة كافية لطلب الكود

    matches.sort(key=lambda m: m[0])
    categories = [category for _, category in matches]
    return categories if categories else ["لا_شي"]


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


def was_code_recently_sent(chat_id: int) -> bool:
    """يتحقق اذا انبعث كود لهذا الزبون خلال آخر فترة قصيرة (نفس نافذة التصفير)."""
    state = _get_retry_state(chat_id)
    return state["attempt_count"] > 0


RESTART_MESSAGE = (
    "🔄 يبدو انه الكود ما يشتغل معك بشكل صحيح.\n"
    "جرب تسوي التالي: احذف الحساب من تطبيق المصادقة (Authenticator) "
    "وابدأ عملية التسجيل من جديد من الأول، وبعدها راسلني وبعطيك كود جديد."
)

STOPPED_MESSAGE = (
    "⚠️ يبدو انه فيه مشكلة مستمرة، حولت طلبك لصاحب المتجر مباشرة "
    "وراح يتواصل معك قريباً."
)


def process_code_request(chat_id: int, is_retry: bool) -> tuple[str | None, bool]:
    """
    يقرر شنو الرد المناسب لطلب كود (أول مرة أو مشكلة_كود)، حسب حالة العداد.

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

    # لو كنا ننتظر تأكيد الريستارت، وهذي رسالة جديدة (طلب_كود أو مشكلة_كود)
    # تعتبر تأكيد ضمني للريستارت → نبعث كود (محاولة 4) ونطفي علامة الانتظار
    if awaiting_restart:
        code = generate_totp_code(secret)
        _save_retry_state(chat_id, attempt_count=4, awaiting_restart=False)
        return f"🔐 الكود: {code}\n⏱️ صالح لمدة 30 ثانية تقريبا", False

    new_count = attempt_count + 1

    if new_count <= 2:
        code = generate_totp_code(secret)
        _save_retry_state(chat_id, attempt_count=new_count, awaiting_restart=False)
        return f"🔐 الكود: {code}\n⏱️ صالح لمدة 30 ثانية تقريبا", False

    if new_count == 3:
        _save_retry_state(chat_id, attempt_count=new_count, awaiting_restart=True)
        return RESTART_MESSAGE, False

    if new_count == 5:
        code = generate_totp_code(secret)
        _save_retry_state(chat_id, attempt_count=new_count, awaiting_restart=False)
        return f"🔐 الكود: {code}\n⏱️ صالح لمدة 30 ثانية تقريبا", False

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
        # اذا مو أمر، خلها تمر عادي (مثلاً حجي عادي وياك نفسك ما نتدخل فيه)
        return

    # 2) نبعث الرسالة للذكاء الاصطناعي (Groq) عشان يحدد القصد الحقيقي
    #    نمرر معلومة "هل انبعث كود قريباً" + "آخر رسالة سابقة" كسياق،
    #    عشان يفهم "ما صار" و اسئلة المتابعة القصيرة ("كم سعره؟") صح
    recent_code = was_code_recently_sent(chat_id)
    previous_message = LAST_MESSAGE_CACHE.get(chat_id)
    categories, ai_failed = await classify_intent(
        text, recent_code_sent=recent_code, previous_message=previous_message
    )

    if ai_failed:
        # Groq فشل بالكامل (429 مستمر أو مشكلة اتصال) — نستخدم المصنف
        # الاحتياطي البسيط بالكلمات المفتاحية بدل ما البوت يسكت تماماً
        categories = classify_intent_fallback(text)
        logger.warning(
            f"Groq unavailable — used keyword fallback for chat_id={chat_id}: {categories}"
        )

        global _LAST_AI_FAILURE_NOTIFICATION
        now = datetime.now(timezone.utc)
        should_notify_ai_down = (
            _LAST_AI_FAILURE_NOTIFICATION is None
            or (now - _LAST_AI_FAILURE_NOTIFICATION)
            > timedelta(minutes=AI_FAILURE_NOTIFICATION_COOLDOWN_MINUTES)
        )
        if should_notify_ai_down:
            _LAST_AI_FAILURE_NOTIFICATION = now
            try:
                await context.bot.send_message(
                    chat_id=OWNER_USER_ID,
                    text=(
                        "⚠️ تنبيه: الذكاء الاصطناعي (Groq) غير متوفر حالياً "
                        "(احتمال تجاوز الحد اليومي المجاني). البوت يستخدم "
                        "النظام الاحتياطي بالكلمات المفتاحية مؤقتاً — دقة "
                        "أقل من المعتاد."
                    ),
                )
            except Exception:
                logger.exception("Failed to notify owner about AI fallback")
    else:
        logger.info(f"Intent classification for chat_id={chat_id}: {categories}")

    # نحدّث الذاكرة المؤقتة بالرسالة الحالية عشان تصير "الرسالة السابقة"
    # لأي رسالة جاية بعدها من نفس الزبون
    LAST_MESSAGE_CACHE[chat_id] = text
    if len(LAST_MESSAGE_CACHE) > LAST_MESSAGE_CACHE_MAX_SIZE:
        # نشيل أقدم عنصر بشكل بسيط لمنع تضخم الذاكرة بلا حدود
        oldest_key = next(iter(LAST_MESSAGE_CACHE))
        del LAST_MESSAGE_CACHE[oldest_key]

    replies_to_send: list[str] = []
    should_notify_stopped = False

    for category in categories:
        if category == "لا_شي":
            continue

        if category in ("طلب_كود", "مشكلة_كود"):
            # طلب كود (أول مرة أو بعد مشكلة) — الشرط الأساسي يضل الربط
            # المسبق بـ /link، وبعده عداد المحاولات يقرر شنو الرد بالضبط
            reply_text, stopped = process_code_request(chat_id, is_retry=(category == "مشكلة_كود"))
            if reply_text:
                replies_to_send.append(reply_text)
            if stopped:
                should_notify_stopped = True
            continue

        # فئة FAQ عادية — نرسل نصها الجاهز فقط (الذكاء الاصطناعي لا يكتب رد حر)
        reply_text = get_reply_for_category(category)
        if reply_text:
            replies_to_send.append(reply_text)

    if should_notify_stopped:
        stopped_notification = (
            f"🚨 توقف الرد التلقائي على الكود!\n"
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
