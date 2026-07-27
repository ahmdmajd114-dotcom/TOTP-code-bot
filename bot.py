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
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------------------------------------------------------------
# نظام الردود التلقائية (FAQ) — لكل الزبائن بدون شرط ربط
# كل عنصر: (اسم الفئة، قائمة كلمات مفتاحية للتوضيح فقط، نص الرد)
# الكلمات المفتاحية هنا مو مستخدمة للمطابقة المباشرة بعد الآن —
# القصد يتحدد عن طريق تصنيف الذكاء الاصطناعي (classify_intent)،
# وبعدها يُختار نص الرد المطابق من هذي القائمة عن طريق اسم الفئة.
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

# كل الفئات الممكنة اللي الذكاء الاصطناعي يختار منها — تُبنى تلقائياً
# من أسماء الفئات بـ FAQ_RULES، بالإضافة لفئتين خاصتين:
# "طلب_كود" (الزبون يطلب الكود فعلاً الحين) و "لا_شي" (ما فيه قصد واضح).
FAQ_CATEGORY_NAMES = [category for category, _, _ in FAQ_RULES]
ALL_CATEGORIES = FAQ_CATEGORY_NAMES + ["طلب_كود", "لا_شي"]

CLASSIFIER_SYSTEM_PROMPT = (
    "أنت مصنف نية (intent classifier) لبوت رد تلقائي على تيليجرام بمتجر عراقي "
    "يبيع اشتراكات واشياء رقمية. تجيك رسالة زبون بالعربي (لهجة عراقية) أو الإنكليزي، "
    "ومهمتك الوحيدة إنك تحدد شنو القصد الحقيقي من الرسالة، وترجع اسم فئة واحدة "
    "بالضبط من هذي القائمة (بدون أي شرح أو نص إضافي):\n\n"
    + "\n".join(f"- {c}" for c in ALL_CATEGORIES)
    + "\n\n"
    "توضيح الفئات:\n"
    "- سلام: تحية اسلامية (السلام عليكم) فقط، وليس مجرد ذكر كلمة سلام بسياق آخر.\n"
    "- ترحيب: تحية عادية (هلا، مرحبا) بدون طلب او سؤال.\n"
    "- شكر: عبارة شكر او دعاء خير (شكرا، تسلم، عاشت ايدك، الله يعطيك العافية) "
    "بأي صياغة، حتى لو مختلفة عن الكلمات المذكورة بالمثال.\n"
    "- chatgpt: الزبون يسأل عن شراء / سعر / تفاصيل اشتراك ChatGPT جديد فعلا "
    "وبشكل حالي. لا تصنفها chatgpt اذا الرسالة فيها اي كلمة تدل على شكوى او "
    "مشكلة تقنية (مثل: مشكلة، ما يشتغل، خربان، وقف، ما يفتح، طلعت رسالة خطأ) "
    "حتى لو ذكرت اسم المنتج (ChatGPT / جات / چات) بنفس الرسالة — لأن هذا "
    "يعني الزبون عنده اشتراك موجود اصلا وعنده مشكلة فيه، مو انه يريد يشتري "
    "اشتراك جديد. صنف هذي الحالة لا_شي دائما (الأونر يرد عليها بنفسه).\n"
    "- طرق_الدفع: سؤال عن طريقة الدفع بشكل عام.\n"
    "- دفع_رصيد: الزبون يريد يدفع برصيد / كارت رصيد.\n"
    "- anki / freenote / goodnote / canva / تليجرام_مميز: سؤال شراء فعلي عن "
    "هذا المنتج بالتحديد (ونفس قاعدة الشكوى اعلاه تنطبق عليهم: ذكر مشكلة "
    "بمنتج موجود اصلا يعني لا_شي، مو طلب شراء).\n"
    "- طلب_كود: الزبون يطلب كود الدخول / رمز التحقق الحين بشكل فعلي، واضح، وحالي "
    "(مثل: ابعثلي الكود، ودني اسجل هسه، ارسل الكود).\n"
    "- لا_شي: أي رسالة ما تنطبق بوضوح وبشكل مباشر وحالي على فئة اعلاه، "
    "بما فيها اي شكوى او مشكلة تقنية بمنتج مشترك فيه الزبون مسبقا.\n\n"
    "قاعدة مهمة جداً حول الوقت والتردد: صنّف طلب_كود فقط اذا كان الطلب "
    "حاضر وفوري وصريح. اذا الرسالة فيها اي اشارة زمنية مستقبلية او شرطية "
    "(مثل: باجر، بعدين، بوقتها، لما اسجل، اذا احتجته، ساعتها) فهذا يعني "
    "الزبون يتكلم عن نية مستقبلية وليس طلب حالي — صنفها لا_شي حتى لو "
    "ذكرت كلمة كود او رمز بنفس الرسالة. وبنفس الشكل، اذا الرسالة هي مجرد "
    "سؤال عن آلية العمل او طريقة الاستخدام (مثل: شلون افعل الرمز؟ من وين "
    "اجيب الكود؟ شنو يعني هالرمز؟) بدون طلب صريح انه يريده الحين، صنفها "
    "لا_شي ايضاً — فرق بين 'اشرحلي شلون اسوي الكود' و'ابعثلي الكود الحين'.\n\n"
    "أمثلة (هذي فقط توضيح للتصنيف، مو ردود جاهزة):\n"
    "رسالة: 'هل قد باجر اسجل وبوكتها اريد الكود' → لا_شي "
    "(اشارة زمنية مستقبلية صريحة: باجر، بوكتها)\n"
    "رسالة: 'بس أراسلكم ع الرمز من افعله واحتاج كود الرمز فقط؟' → لا_شي "
    "(سؤال عن آلية العمل، مو طلب فوري)\n"
    "رسالة: 'ابعثلي الكود الحين لو سمحت' → طلب_كود (طلب فوري وصريح)\n"
    "رسالة: 'اريد جات' أو 'اريد چات' → chatgpt (طلب شراء/استفسار فوري)\n"
    "رسالة: 'عندي مشكلة، جي بي تي زين' → لا_شي (يذكر ChatGPT بسياق شكوى، مو طلب شراء)\n"
    "رسالة: 'عندي مشكلة باشتراك جات' → لا_شي (كلمة 'مشكلة' + منتج موجود اصلا = شكوى، مو طلب شراء جديد)\n\n"
    "اذا الرسالة فيها اكثر من قصد واحد حقيقي وحالي بنفس الوقت (مثلا تحية + "
    "سؤال شراء)، ارجع الفئتين مفصولتين بفاصلة إنكليزية عادية \",\" فقط "
    "(وليس الفاصلة العربية \"،\")، بترتيب ظهورهن بالرسالة. "
    "رجاءا استخدم فقط أسماء الفئات المذكورة اعلاه بالضبط كما هي، بدون أي "
    "كلام او علامات ترقيم اضافية."
)


async def classify_intent(text: str) -> list[str]:
    """
    يبعث نص الزبون لـ Groq (نموذج مجاني وسريع) عشان يحدد القصد،
    ويرجع قائمة أسماء فئات من ALL_CATEGORIES (ممكن تكون فئة واحدة أو أكثر).
    اذا فشل الاتصال أو الرد غير مفهوم، يرجع ["لا_شي"] احتياطيا (البوت ما يرد
    بدل ما يرد غلط).
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": GROQ_MODEL,
                    "temperature": 0,
                    "max_tokens": 60,
                    "messages": [
                        {"role": "system", "content": CLASSIFIER_SYSTEM_PROMPT},
                        {"role": "user", "content": text},
                    ],
                },
            )
        resp.raise_for_status()
        data = resp.json()
        raw = data["choices"][0]["message"]["content"].strip()
    except Exception:
        logger.exception("Groq classification failed")
        return ["لا_شي"]

    # Groq أحياناً يرجع الفئات مفصولة بفاصلة عربية "،" بدل الفاصلة
    # الإنكليزية العادية "," — نطبّع النص أول (نحول الفاصلة العربية
    # لإنكليزية) قبل التقسيم، حتى ما تنكسر عملية الفصل.
    normalized_raw = raw.replace("،", ",")
    candidates = [c.strip().strip(".") for c in normalized_raw.split(",")]
    candidates = [c for c in candidates if c]  # شيل أي عنصر فاضي
    valid = [c for c in candidates if c in ALL_CATEGORIES]

    if not valid:
        logger.warning(f"Groq returned unrecognized category: {raw!r}")
        return ["لا_شي"]

    return valid


def get_reply_for_category(category: str) -> str | None:
    """يرجع نص الرد الجاهز المطابق لفئة FAQ، أو None اذا مو فئة FAQ (كود/لا_شي)."""
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
    categories = await classify_intent(text)
    logger.info(f"Intent classification for chat_id={chat_id}: {categories}")

    replies_to_send: list[str] = []

    for category in categories:
        if category == "لا_شي":
            continue

        if category == "طلب_كود":
            # طلب كود فعلي — الشرط الأساسي يضل الربط المسبق بـ /link
            result = get_secret_for_chat(chat_id)
            if result is not None:
                secret, label = result
                code = generate_totp_code(secret)
                replies_to_send.append(f"🔐 الكود: {code}\n⏱️ صالح لمدة 30 ثانية تقريبا")
                logger.info(f"Sent TOTP code to chat_id={chat_id} (account={label})")
            else:
                logger.info(f"Code requested by unlinked chat_id={chat_id} — ignored")
            continue

        # فئة FAQ عادية — نرسل نصها الجاهز فقط (الذكاء الاصطناعي لا يكتب رد حر)
        reply_text = get_reply_for_category(category)
        if reply_text:
            replies_to_send.append(reply_text)

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
