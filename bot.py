"""
بوت TOTP — يرد على رسائل حسابك الشخصي (Telegram Business) بكود Google Authenticator
تلقائياً لما الزبون يطلب "كود" / "رمز" / "code".

الفكرة:
- أنت (owner) تضيف حساب جديد بأمر /addaccount
- أنت تربط زبون معين بحساب معين بأمر /link داخل محادثته
- أي زبون يرسل كلمة مفتاحية (كود/رمز/code) يرجعله البوت TOTP الحالي تبع حسابه المربوط
"""

import os
import re
import asyncio
import logging
import pyotp
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

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# كلمات مفتاحية يفهمها البوت كطلب كود (عربي + انكليزي، بأي شكل كتابة)
CODE_KEYWORDS = [
    "اريد كود", "أريد كود", "اريد الكود", "أريد الكود",
    "اريد رمز", "أريد رمز", "اريد الرمز", "أريد الرمز",
    "كود", "رمز", "code", "otp",
]

# ------------------------------------------------------------------
# نظام الردود التلقائية (FAQ) — لكل الزبائن بدون شرط ربط
# كل عنصر: (اسم الفئة، قائمة كلمات مفتاحية، نص الرد)
# الترتيب هنا يحدد أولوية الفحص، وأيضاً ترتيب الرد اذا انطبقت اكثر
# من فئة بنفس الرسالة — الفحص يصير بترتيب ظهور الكلمة داخل النص
# نفسه، مو بترتيب هذي القائمة (شوف find_faq_matches بالأسفل).
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

REPLY_DELAY_SECONDS = 8
REPEAT_COOLDOWN_SECONDS = 60 * 60  # ساعة كاملة — نفس الفئة ما تتكرر لنفس الزبون خلالها

LINK_PATTERN = re.compile(r"^/link\s+(\S+)$", re.IGNORECASE)
ADD_PATTERN = re.compile(r"^/addaccount\s+(\S+)\s+(\S+)(?:\s+(.+))?$", re.IGNORECASE)


def is_code_request(text: str) -> bool:
    """يتحقق اذا الرسالة تحتوي كلمة مفتاحية لطلب كود."""
    normalized = text.strip().lower()
    return any(keyword in normalized for keyword in CODE_KEYWORDS)


def find_faq_matches(text: str) -> list[tuple[str, str]]:
    """
    يفحص النص عن كل الفئات المطابقة، ويرجع (اسم الفئة، الرد) بترتيب
    ظهور الكلمة المفتاحية داخل الرسالة نفسها (مو بترتيب القائمة).
    كل فئة تنطبق مرة وحدة بس حتى لو تكررت كلماتها بالرسالة.
    """
    normalized = text.strip().lower()
    matches = []  # (موقع الظهور بالنص، اسم الفئة، الرد)

    for category, keywords, reply in FAQ_RULES:
        best_position = None
        for kw in keywords:
            pos = normalized.find(kw.lower())
            if pos != -1 and (best_position is None or pos < best_position):
                best_position = pos
        if best_position is not None:
            matches.append((best_position, category, reply))

    matches.sort(key=lambda m: m[0])
    return [(category, reply) for _, category, reply in matches]


def filter_recent_repeats(chat_id: int, matches: list[tuple[str, str]]) -> list[str]:
    """
    يشيل من قائمة الردود أي فئة تم الرد عليها لنفس الزبون خلال آخر ساعة
    (محفوظ بقاعدة Supabase، يضل ثابت حتى لو انعاد تشغيل البوت)،
    ويحدّث توقيت الفئات الجديدة اللي راح نرد عليها الحين.
    """
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=REPEAT_COOLDOWN_SECONDS)
    replies_to_send = []

    for category, reply in matches:
        existing = (
            supabase.table("faq_reply_log")
            .select("last_sent_at")
            .eq("chat_id", chat_id)
            .eq("category", category)
            .execute()
        )

        should_send = True
        if existing.data:
            last_sent_at = datetime.fromisoformat(existing.data[0]["last_sent_at"])
            if last_sent_at > cutoff:
                should_send = False

        if not should_send:
            continue  # اترك هذي الفئة، رد عليها قريباً

        replies_to_send.append(reply)
        supabase.table("faq_reply_log").upsert(
            {"chat_id": chat_id, "category": category, "last_sent_at": now.isoformat()}
        ).execute()

    return replies_to_send


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


async def show_typing_then_wait(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    business_connection_id: str,
    seconds: float,
) -> None:
    """
    يعرض مؤشر 'يكتب...' طول فترة الانتظار قبل الرد، عشان يبين طبيعي.
    مؤشر الكتابة بتليجرام يختفي تلقائياً بعد 5 ثواني، فنجدده كل 4
    ثواني لحد ما تخلص فترة الانتظار كاملة.
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


async def on_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعالج كل الرسائل الجاية عن طريق Telegram Business (محادثتك الشخصية)."""
    bm = update.business_message
    if not bm or not bm.text:
        return

    chat_id = bm.chat.id
    text = bm.text
    sender_id = bm.from_user.id if bm.from_user else None

    is_from_owner = sender_id == OWNER_USER_ID

    # 1) اذا الرسالة منك انت (owner) — تحقق اذا هي أمر ربط/اضافة
    if is_from_owner:
        handled = await handle_owner_command(update, context, chat_id, text)
        if handled:
            return
        # اذا مو أمر، خلها تمر عادي (مثلاً حجي عادي وياك نفسك ما نتدخل فيه)
        return

    # 2) اذا الرسالة من الزبون — تحقق اذا يطلب كود (حصري للمرتبطين بـ /link)
    if is_code_request(text):
        result = get_secret_for_chat(chat_id)
        if result is not None:
            secret, label = result
            code = generate_totp_code(secret)
            reply = f"🔐 الكود: {code}\n⏱️ صالح لمدة 30 ثانية تقريباً"

            await show_typing_then_wait(
                context, chat_id, bm.business_connection_id, REPLY_DELAY_SECONDS
            )
            await context.bot.send_message(
                business_connection_id=bm.business_connection_id,
                chat_id=chat_id,
                text=reply,
            )
            logger.info(f"Sent TOTP code to chat_id={chat_id} (account={label})")
            return
        else:
            # ما عنده حساب مربوط — ما نرد بكود، نكمل نفحص FAQ تحسباً
            logger.info(f"Code requested by unlinked chat_id={chat_id}")

    # 3) الردود التلقائية (FAQ) — مفتوحة لأي زبون، بدون شرط ربط
    faq_matches = find_faq_matches(text)
    faq_replies = filter_recent_repeats(chat_id, faq_matches)
    if faq_replies:
        await show_typing_then_wait(
            context, chat_id, bm.business_connection_id, REPLY_DELAY_SECONDS
        )
        for reply_text in faq_replies:
            await context.bot.send_message(
                business_connection_id=bm.business_connection_id,
                chat_id=chat_id,
                text=reply_text,
            )
        logger.info(f"Sent {len(faq_replies)} FAQ reply(ies) to chat_id={chat_id}")


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
