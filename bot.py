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
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    CommandHandler,
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
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-20b")

# مسار ملف مفاتيح حساب خدمة Google (Service Account) — ملف JSON خارجي
# لا ينرفع لـ Git إطلاقاً (مضاف لـ .gitignore). المسار الافتراضي
# /etc/secrets/google_service_account.json يطابق طريقة Render لتخزين
# "Secret Files" — لو تستضيف بمكان ثاني، بدّل GOOGLE_SERVICE_ACCOUNT_FILE
GOOGLE_SERVICE_ACCOUNT_FILE = os.environ.get(
    "GOOGLE_SERVICE_ACCOUNT_FILE", "/etc/secrets/google_service_account.json"
)
GOOGLE_SHEET_ID = os.environ["GOOGLE_SHEET_ID"]  # الـ ID تبع الشيت (من رابطه)
GOOGLE_SHEET_WORKSHEET_NAME = os.environ.get("GOOGLE_SHEET_WORKSHEET_NAME", "Sheet1")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------------------------------------------------------------
# اتصال Google Sheets (gspread) — يُبنى مرة وحدة عند بدء تشغيل البوت.
# لو فشل الاتصال (ملف مفقود، صلاحيات ناقصة، إلخ)، البوت يضل يشتغل
# عادي بكل شي ثاني، بس ميزة تسجيل الدفع تتعطل وتنبهك بذلك بدل ما يكرش.
# ------------------------------------------------------------------
_google_sheet = None


def get_google_sheet():
    """يرجع كائن الشيت (worksheet) جاهز للكتابة، أو None لو فشل الاتصال."""
    global _google_sheet
    if _google_sheet is not None:
        return _google_sheet
    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_file(
            GOOGLE_SERVICE_ACCOUNT_FILE, scopes=scopes
        )
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
        _google_sheet = spreadsheet.worksheet(GOOGLE_SHEET_WORKSHEET_NAME)
        logger.info("Google Sheets connection established successfully")
        return _google_sheet
    except Exception:
        logger.exception("Failed to connect to Google Sheets")
        return None


_expenses_sheet = None


def get_expenses_worksheet():
    """
    يرجع كائن صفحة (Tab) المصروفات، وينشئها تلقائياً لو مو موجودة أصلاً
    (بعنوان: التاريخ والوقت — المبلغ — السبب). يرجع None لو فشل الاتصال.
    """
    global _expenses_sheet
    if _expenses_sheet is not None:
        return _expenses_sheet
    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_file(
            GOOGLE_SERVICE_ACCOUNT_FILE, scopes=scopes
        )
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
        try:
            _expenses_sheet = spreadsheet.worksheet(EXPENSES_WORKSHEET_NAME)
        except gspread.exceptions.WorksheetNotFound:
            _expenses_sheet = spreadsheet.add_worksheet(
                title=EXPENSES_WORKSHEET_NAME, rows=1000, cols=3
            )
            _expenses_sheet.append_row(
                ["التاريخ والوقت", "المبلغ", "السبب"], value_input_option="USER_ENTERED"
            )
            logger.info(f"Created new expenses worksheet: {EXPENSES_WORKSHEET_NAME}")
        return _expenses_sheet
    except Exception:
        logger.exception("Failed to connect to expenses worksheet")
        return None


_vaults_sheet = None


def get_vaults_worksheet():
    """
    يرجع كائن صفحة (Tab) خزائن الرصيد، وينشئها تلقائياً لو مو موجودة —
    صف عناوين (ماستر، زين كاش، رصيد اثير، رصيد اسيا) + صف واحد للأرصدة
    الحالية (يبدأ بأصفار، يتحدث لاحقاً). يرجع None لو فشل الاتصال.
    """
    global _vaults_sheet
    if _vaults_sheet is not None:
        return _vaults_sheet
    try:
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_file(
            GOOGLE_SERVICE_ACCOUNT_FILE, scopes=scopes
        )
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(GOOGLE_SHEET_ID)
        try:
            _vaults_sheet = spreadsheet.worksheet(VAULTS_WORKSHEET_NAME)
        except gspread.exceptions.WorksheetNotFound:
            _vaults_sheet = spreadsheet.add_worksheet(
                title=VAULTS_WORKSHEET_NAME, rows=10, cols=len(VAULT_NAMES)
            )
            _vaults_sheet.append_row(VAULT_NAMES, value_input_option="USER_ENTERED")
            _vaults_sheet.append_row([0] * len(VAULT_NAMES), value_input_option="USER_ENTERED")
            logger.info(f"Created new vaults worksheet: {VAULTS_WORKSHEET_NAME}")
        return _vaults_sheet
    except Exception:
        logger.exception("Failed to connect to vaults worksheet")
        return None


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
# حالة مؤقتة (بذاكرة البرنامج) لكل عملية تسجيل دفع جارية — المفتاح
# هو message_id تبع رسالة الصورة المحولة بمحادثتك مع البوت. تنمحي
# فور التثبيت أو الإلغاء، أو عند اعادة تشغيل البوت (مقبول).
#
# كل عنصر: {
#   "customer_name": str, "customer_username": str | None,
#   "customer_chat_id": int,       # chat_id تبع الزبون الأصلي (Business)
#   "product": str | None,
#   "payments": list[tuple[str, int]],  # [(طريقة الدفع، المبلغ), ...]
#   "pending_method": str | None,       # طريقة دفع اخترناها وننتظر مبلغها
#   "pending_amount": int,              # المبلغ المتراكم بالضغط قبل التثبيت
#   "awaiting_manual_amount": bool,     # ننتظر رقم يدوي مكتوب كرسالة نصية
# }
# ------------------------------------------------------------------
_pending_payments: dict[int, dict] = {}

# ------------------------------------------------------------------
# سجل خفيف لعمليات الدفع المكتملة (بعد pay_finalize) — يبقى موجود
# حتى بعد ما تنمحى الحالة الكاملة من _pending_payments، عشان لو رديت
# على رسالة "تم الحفظ بنجاح" بإيميل أو كلمة "خاص"، نعرف مين الزبون
# وشنو منتجه. المفتاح message_id لنفس رسالة التأكيد النهائي.
# ------------------------------------------------------------------
_completed_payments: dict[int, dict] = {}

# ------------------------------------------------------------------
# حالة مؤقتة لعملية تسجيل مصروف جارية — مفتاح وحيد (بس عملية وحدة
# بنفس الوقت للأونر، مو محتاج تعدد مثل الدفعات). تُخزن بذاكرة البرنامج.
# {
#   "message_id": int,          # رسالة الملخص الوحيدة اللي نعدلها
#   "amount": int,
#   "reason": str | None,
#   "awaiting_manual_amount": bool,
#   "awaiting_manual_reason": bool,
# }
# ------------------------------------------------------------------
_pending_expense: dict | None = None

# ------------------------------------------------------------------
# حالة مؤقتة لفلو إضافة حساب TOTP التفاعلي (بديل لكتابة /addaccount
# يدوياً) — بس عملية وحدة بنفس الوقت.
# {
#   "message_id": int,
#   "step": "link_code" | "secret" | "label",
#   "link_code": str | None,
#   "secret": str | None,
# }
# ------------------------------------------------------------------
_pending_add_account: dict | None = None

# ------------------------------------------------------------------
# حالة مؤقتة لانتظار إدخال تاريخ يدوي بشاشة الإحصائيات — {message_id, next_action}
# ------------------------------------------------------------------
_pending_stats_period: dict | None = None

# ------------------------------------------------------------------
# حالة مؤقتة لانتظار إدخال مبلغ يدوي أثناء تعديل رصيد خزنة —
# {message_id, vault_name, mode} — mode: "set" | "add" | "sub"
# ------------------------------------------------------------------
_pending_vault_edit: dict | None = None

# ------------------------------------------------------------------
# حد أقصى 3 صور دفع لكل زبون خلال آخر 6 ساعات — الهدف منع إزعاج
# متكرر من زبون يرسل سكرين شوت كثير لنفس عملية الدفع. المفتاح هو
# customer_chat_id، والقيمة قائمة بأوقات وصول الصور المقبولة (تلقائياً
# فقط، مو صور accept اليدوي) خلال النافذة الحالية.
# ------------------------------------------------------------------
PHOTO_RATE_LIMIT_MAX = 1
PHOTO_RATE_LIMIT_WINDOW_HOURS = 6
_photo_timestamps: dict[int, list[datetime]] = {}


def is_photo_within_rate_limit(customer_chat_id: int) -> bool:
    """
    يتحقق هل هذي الصورة ضمن حد 3 صور/6 ساعات لهذا الزبون. لو نعم،
    يسجل وقت وصولها ويرجع True (نحول الصورة عادي). لو تجاوز الحد،
    يرجع False بدون ما يسجل شي (الصورة تتجاهل بالكامل).
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=PHOTO_RATE_LIMIT_WINDOW_HOURS)

    timestamps = _photo_timestamps.get(customer_chat_id, [])
    # نشيل أي وقت أقدم من النافذة الحالية (منتهي الصلاحية)
    timestamps = [t for t in timestamps if t > cutoff]

    if len(timestamps) >= PHOTO_RATE_LIMIT_MAX:
        _photo_timestamps[customer_chat_id] = timestamps  # نحدث القائمة المصفاة حتى لو رفضنا
        return False

    timestamps.append(now)
    _photo_timestamps[customer_chat_id] = timestamps
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
            "السلام عليكم", "سلام عليكم", "سلامو عليكم", "سلامة عليكم",
            "السلام عليكم ورحمة الله", "assalamu alaikum", "salam alaikum",
        ],
        "وعليكم السلام ورحمة الله وبركاته اهلا وسهلا",
    ),
    (
        "ترحيب",
        [
            "هلا", "مرحبا", "مرحبتين", "هلو", "hi", "hello", "hey",
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
            "شلون الدفع", "طرق التسديد", "كيفية الدفع", "شنو طرق الدفع",
            "زين كاش", "سوبر كي", "زد كاش", "اريد ادفع", "ادفع", "ماستر",
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
        "متوفر تلث اشهر ب 25 اما السة ب 35 والسنة ب55 الف",
    ),
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
# ميزة تسجيل الدفع بـ Google Sheet — قوائم المنتجات وطرق الدفع الثابتة
# اللي تظهر كأزرار وقت تأكيد عملية دفع. القائمة مبنية على منتجات
# FAQ_RULES + رصيد، والمنتج/الطريقة الأولى بكل قائمة هي "الاختيار
# السريع" (الأكثر استخداماً) اللي يطلع كزر مباشر بدون فتح قائمة.
# ------------------------------------------------------------------
PAYMENT_PRODUCTS = ["جات", "انكي", "كانفا", "فرينوت", "گودنوت", "تليجرام مميز", "امبوس", "Coursera"]
PAYMENT_METHODS = ["ماستر", "زين كاش", "رصيد اثير", "رصيد اسيا"]

# أسماء الخزائن — نفس أسماء طرق الدفع بالضبط، عشان كل طريقة دفع تربط
# مباشرة بخزنتها المطابقة بدون أي تحويل إضافي
VAULT_NAMES = PAYMENT_METHODS

PAYMENT_AMOUNT_STEP_SMALL = 1000
PAYMENT_AMOUNT_STEP_LARGE = 5000

# اسم صفحة (Tab) المصروفات بنفس الشيت — تُنشأ تلقائياً لو مو موجودة
EXPENSES_WORKSHEET_NAME = "ورقة المصروفات"

# اسم صفحة (Tab) خزائن الرصيد بنفس الشيت — تُنشأ تلقائياً لو مو موجودة
VAULTS_WORKSHEET_NAME = "خزائن الرصيد"

# نصوص أزرار لوحة المفاتيح الثابتة (Reply Keyboard) تحت صندوق الكتابة
BTN_EXPENSE = "💸 تسجيل مصروف"
BTN_INCOME = "📊 تقرير الدخل"
BTN_ADD_ACCOUNT = "➕ إضافة حساب"
BTN_STATS = "📈 إحصائيات"
BTN_BACK = "◀️ رجوع"

MAIN_REPLY_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton(BTN_EXPENSE), KeyboardButton(BTN_INCOME)],
        [KeyboardButton(BTN_ADD_ACCOUNT), KeyboardButton(BTN_STATS)],
    ],
    resize_keyboard=True,
)


def build_confirm_cancel_keyboard() -> InlineKeyboardMarkup:
    """أول زرين يطلعون تحت صورة دفع جديدة توصل من زبون."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ تأكيد", callback_data="pay_confirm"),
            InlineKeyboardButton("❌ إلغاء", callback_data="pay_cancel"),
        ]
    ])


def build_product_keyboard() -> InlineKeyboardMarkup:
    """أول شاشة بعد التأكيد — اختيار سريع لأكثر منتج مبيع + بقية المنتجات."""
    quick_pick = PAYMENT_PRODUCTS[0]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(quick_pick, callback_data=f"pay_product_{quick_pick}")],
        [InlineKeyboardButton("بقية المنتجات ▾", callback_data="pay_product_list")],
    ])


def build_product_list_keyboard() -> InlineKeyboardMarkup:
    """قائمة كل المنتجات ما عدا الاختيار السريع (اللي طلع بالشاشة السابقة) + زر رجوع."""
    rows = [
        [InlineKeyboardButton(p, callback_data=f"pay_product_{p}")]
        for p in PAYMENT_PRODUCTS[1:]
    ]
    rows.append([InlineKeyboardButton(BTN_BACK, callback_data="pay_back_to_product")])
    return InlineKeyboardMarkup(rows)


def build_method_list_keyboard() -> InlineKeyboardMarkup:
    """قائمة كل طرق الدفع ما عدا الاختيار السريع + زر رجوع."""
    rows = [
        [InlineKeyboardButton(m, callback_data=f"pay_method_{m}")]
        for m in PAYMENT_METHODS[1:]
    ]
    rows.append([InlineKeyboardButton(BTN_BACK, callback_data="pay_back_to_method")])
    return InlineKeyboardMarkup(rows)


def build_amount_keyboard() -> InlineKeyboardMarkup:
    """شاشة تحديد مبلغ طريقة الدفع المختارة — أزرار تراكمية + إدخال يدوي + تثبيت المبلغ."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"+{PAYMENT_AMOUNT_STEP_SMALL}", callback_data="pay_amount_add_small"),
            InlineKeyboardButton(f"+{PAYMENT_AMOUNT_STEP_LARGE}", callback_data="pay_amount_add_large"),
        ],
        [InlineKeyboardButton("✏️ إدخال يدوي", callback_data="pay_amount_manual")],
        [InlineKeyboardButton("✅ تثبيت المبلغ", callback_data="pay_amount_commit")],
    ])


def build_summary_keyboard(has_product: bool, has_payment: bool) -> InlineKeyboardMarkup:
    """
    الشاشة الرئيسية بعد ما فيه منتج أو طريقة دفع واحدة محفوظة على الأقل —
    زر المرحلة الحالية (منتج أو طريقة دفع جديدة) + زر التثبيت النهائي دايماً.
    """
    rows = []
    if not has_product:
        rows.append([InlineKeyboardButton(PAYMENT_PRODUCTS[0], callback_data=f"pay_product_{PAYMENT_PRODUCTS[0]}")])
        rows.append([InlineKeyboardButton("بقية المنتجات ▾", callback_data="pay_product_list")])
    else:
        rows.append([InlineKeyboardButton(PAYMENT_METHODS[0], callback_data=f"pay_method_{PAYMENT_METHODS[0]}")])
        rows.append([InlineKeyboardButton("بقية الطرق ▾", callback_data="pay_method_list")])
    rows.append([InlineKeyboardButton("✅ تثبيت العملية", callback_data="pay_finalize")])
    return InlineKeyboardMarkup(rows)


def format_payment_summary(state: dict) -> str:
    """يبني نص الملخص المعروض فوق الأزرار أثناء تسجيل الدفع."""
    lines = ["تسجيل عملية دفع"]
    customer_line = state["customer_name"]
    if state.get("customer_username"):
        customer_line += f" (@{state['customer_username']})"
    lines.append(f"الزبون: {customer_line}")

    product = state.get("product")
    lines.append(f"المنتج: {product if product else '— لم يُختر بعد —'}")

    payments = state.get("payments", [])
    if payments:
        payments_text = " + ".join(f"{method} {amount}" for method, amount in payments)
        total = sum(amount for _, amount in payments)
        lines.append(f"طرق الدفع: {payments_text}")
        lines.append(f"المجموع الكلي: {total}")
    else:
        lines.append("طرق الدفع: — لم تُضف بعد —")

    return "\n".join(lines)


SHEET_COL_DATE = 1
SHEET_COL_TOTAL = 2
SHEET_COL_PAYMENTS = 3
SHEET_COL_PRODUCT = 4
SHEET_COL_CUSTOMER = 5
SHEET_COL_CHATGPT_ACCOUNT = 6
SHEET_COL_CHAT_ID = 7

CHATGPT_PRODUCT_NAME = "جات"
CHATGPT_ROW_MATCH_WINDOW_DAYS = 7


def format_customer_line(customer_name: str, customer_username: str | None) -> str:
    line = customer_name
    if customer_username:
        line += f" (@{customer_username})"
    return line


def find_completable_chatgpt_row(sheet, chat_id: int) -> int | None:
    """
    يدور عن آخر سطر بالشيت لنفس chat_id، شرط المنتج = جات، خلال آخر
    أسبوع، وفيه خانة مهمة فاضية (مبلغ/طرق الدفع/حسابات جات). يرجع رقم
    الصف (1-indexed كما تتوقعه gspread) لو لقى، أو None لو لازم سطر جديد.
    """
    try:
        rows = sheet.get_all_values()
    except Exception:
        logger.exception("Failed to read rows while searching for completable ChatGPT row")
        return None

    if len(rows) <= 1:
        return None

    cutoff = datetime.now(timezone.utc) - timedelta(days=CHATGPT_ROW_MATCH_WINDOW_DAYS)
    chat_id_str = str(chat_id)

    # نفحص من الأسفل للأعلى (الأحدث أول) عشان نلقى أقرب سطر مطابق بسرعة
    for i in range(len(rows) - 1, 0, -1):  # نتجاوز صف العناوين (index 0)
        row = rows[i]
        if len(row) < SHEET_COL_CHAT_ID:
            continue

        row_chat_id = row[SHEET_COL_CHAT_ID - 1].strip()
        row_product = row[SHEET_COL_PRODUCT - 1].strip()

        if row_chat_id != chat_id_str or row_product != CHATGPT_PRODUCT_NAME:
            continue

        try:
            row_date = datetime.strptime(row[SHEET_COL_DATE - 1].split(" ")[0], "%Y-%m-%d")
            row_date = row_date.replace(tzinfo=timezone.utc)
        except (ValueError, IndexError):
            continue

        if row_date < cutoff:
            break  # وصلنا لصفوف أقدم من أسبوع، ما فيه فايدة نكمل الفحص

        total_empty = not row[SHEET_COL_TOTAL - 1].strip() if len(row) >= SHEET_COL_TOTAL else True
        payments_empty = not row[SHEET_COL_PAYMENTS - 1].strip() if len(row) >= SHEET_COL_PAYMENTS else True
        account_empty = not row[SHEET_COL_CHATGPT_ACCOUNT - 1].strip() if len(row) >= SHEET_COL_CHATGPT_ACCOUNT else True

        if total_empty or payments_empty or account_empty:
            return i + 1  # gspread صفوف 1-indexed

    return None


def append_payment_row(state: dict) -> bool:
    """
    يضيف سطر جديد بـ Google Sheet لعملية دفع مكتملة، أو يكمل سطر ناقص
    موجود لنفس الزبون لو المنتج جات (خلال آخر أسبوع). يرجع True لو نجح
    الحفظ، False لو فشل — الاستدعاء المسؤول يتعامل مع الفشل بتنبيه
    الأونر بدل ما يفترض النجاح.
    """
    sheet = get_google_sheet()
    if sheet is None:
        return False

    now = datetime.now(timezone.utc)
    date_time_str = now.strftime("%Y-%m-%d %H:%M")

    payments = state.get("payments", [])
    total = sum(amount for _, amount in payments)
    payments_text = " + ".join(f"{method} {amount}" for method, amount in payments)

    customer_line = format_customer_line(state["customer_name"], state.get("customer_username"))
    product = state.get("product") or ""
    chat_id = state.get("customer_chat_id")

    try:
        target_row = None
        if product == CHATGPT_PRODUCT_NAME and chat_id is not None:
            target_row = find_completable_chatgpt_row(sheet, chat_id)

        if target_row is not None:
            # نكمل السطر الناقص — نعبي بس الخانات الفاضية (مبلغ/طرق الدفع)
            sheet.update_cell(target_row, SHEET_COL_TOTAL, total)
            sheet.update_cell(target_row, SHEET_COL_PAYMENTS, payments_text)
        else:
            row = [
                date_time_str,
                total,
                payments_text,
                product,
                customer_line,
                "",  # حسابات جات — تُعبى لاحقاً عن طريق accept/link
                str(chat_id) if chat_id is not None else "",
            ]
            sheet.append_row(row, value_input_option="USER_ENTERED")
        return True
    except Exception:
        logger.exception("Failed to append/update payment row in Google Sheet")
        return False


def upsert_chatgpt_account(
    chat_id: int, customer_name: str, customer_username: str | None, account_text: str
) -> bool:
    """
    يسجل معلومة حساب ChatGPT (إيميل أو 'خاص') لزبون معين — يكمل سطر
    ناقص موجود (خلال آخر أسبوع، منتجه جات) لو لقى، وإلا يفتح سطر جديد
    مستقل (التاريخ + بيانات الزبون + عمود حسابات جات، الباقي فاضي).
    """
    sheet = get_google_sheet()
    if sheet is None:
        return False

    now = datetime.now(timezone.utc)
    date_time_str = now.strftime("%Y-%m-%d %H:%M")
    customer_line = format_customer_line(customer_name, customer_username)

    try:
        target_row = find_completable_chatgpt_row(sheet, chat_id)

        if target_row is not None:
            sheet.update_cell(target_row, SHEET_COL_CHATGPT_ACCOUNT, account_text)
        else:
            row = [
                date_time_str,
                "",  # المبلغ الكلي — يُعبى لاحقاً عند تسجيل الدفع
                "",  # طرق الدفع — نفس الشي
                CHATGPT_PRODUCT_NAME,
                customer_line,
                account_text,
                str(chat_id),
            ]
            sheet.append_row(row, value_input_option="USER_ENTERED")
        return True
    except Exception:
        logger.exception("Failed to upsert ChatGPT account info in Google Sheet")
        return False


def build_expense_amount_keyboard() -> InlineKeyboardMarkup:
    """شاشة تحديد مبلغ المصروف — نفس أزرار مبلغ الدفع، بس callback_data مختلف."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"+{PAYMENT_AMOUNT_STEP_SMALL}", callback_data="exp_amount_add_small"),
            InlineKeyboardButton(f"+{PAYMENT_AMOUNT_STEP_LARGE}", callback_data="exp_amount_add_large"),
        ],
        [InlineKeyboardButton("✏️ إدخال يدوي", callback_data="exp_amount_manual")],
        [InlineKeyboardButton("✅ تثبيت المبلغ", callback_data="exp_amount_commit")],
    ])


def build_expense_vault_keyboard() -> InlineKeyboardMarkup:
    """شاشة اختيار الخزنة اللي ينسحب منها مبلغ المصروف."""
    rows = [[InlineKeyboardButton(v, callback_data=f"exp_vault_{v}")] for v in VAULT_NAMES]
    rows.append([InlineKeyboardButton("بدون خزنة محددة", callback_data="exp_vault_none")])
    return InlineKeyboardMarkup(rows)


def build_expense_reason_keyboard() -> InlineKeyboardMarkup:
    """شاشة اختيار سبب المصروف — منتج سريع + بقية المنتجات + إدخال حر."""
    quick_pick = PAYMENT_PRODUCTS[0]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(quick_pick, callback_data=f"exp_reason_{quick_pick}")],
        [InlineKeyboardButton("بقية المنتجات ▾", callback_data="exp_reason_list")],
        [InlineKeyboardButton("✏️ إدخال حر", callback_data="exp_reason_manual")],
    ])


def build_expense_reason_list_keyboard() -> InlineKeyboardMarkup:
    """قائمة كل المنتجات ما عدا الاختيار السريع + زر رجوع، لسبب المصروف."""
    rows = [
        [InlineKeyboardButton(p, callback_data=f"exp_reason_{p}")]
        for p in PAYMENT_PRODUCTS[1:]
    ]
    rows.append([InlineKeyboardButton(BTN_BACK, callback_data="exp_back_to_reason")])
    return InlineKeyboardMarkup(rows)


def format_expense_summary(expense: dict) -> str:
    """يبني نص الملخص المعروض فوق أزرار تسجيل المصروف."""
    lines = ["تسجيل مصروف"]
    amount = expense.get("amount", 0)
    vault = expense.get("vault")
    reason = expense.get("reason")
    lines.append(f"المبلغ: {amount if amount else '— لم يُحدد بعد —'}")
    lines.append(f"الخزنة: {vault if vault else '— لم تُحدد بعد —'}")
    lines.append(f"السبب: {reason if reason else '— لم يُحدد بعد —'}")
    return "\n".join(lines)


def append_expense_row(amount: int, reason: str) -> bool:
    """يضيف سطر مصروف جديد لصفحة المصروفات. يرجع True لو نجح الحفظ."""
    sheet = get_expenses_worksheet()
    if sheet is None:
        return False

    date_time_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    row = [date_time_str, amount, reason]

    try:
        sheet.append_row(row, value_input_option="USER_ENTERED")
        return True
    except Exception:
        logger.exception("Failed to append expense row to Google Sheet")
        return False


def get_vault_balances() -> dict[str, int] | None:
    """يرجع الأرصدة الحالية لكل الخزائن كـ dict، أو None لو فشل الاتصال."""
    sheet = get_vaults_worksheet()
    if sheet is None:
        return None

    try:
        values = sheet.row_values(2)  # الصف الثاني (بعد صف العناوين)
    except Exception:
        logger.exception("Failed to read vault balances")
        return None

    balances = {}
    for i, name in enumerate(VAULT_NAMES):
        try:
            balances[name] = int(float(values[i])) if i < len(values) and values[i].strip() else 0
        except (ValueError, IndexError):
            balances[name] = 0
    return balances


def adjust_vault_balance(vault_name: str, delta: int) -> bool:
    """يضيف (أو يطرح لو delta سالب) مبلغ من رصيد خزنة معينة. يرجع True لو نجح."""
    sheet = get_vaults_worksheet()
    if sheet is None or vault_name not in VAULT_NAMES:
        return False

    try:
        balances = get_vault_balances()
        if balances is None:
            return False
        new_value = balances[vault_name] + delta
        col_index = VAULT_NAMES.index(vault_name) + 1  # gspread أعمدة 1-indexed
        sheet.update_cell(2, col_index, new_value)
        return True
    except Exception:
        logger.exception("Failed to adjust vault balance")
        return False


def set_vault_balance(vault_name: str, new_value: int) -> bool:
    """يحدد رصيد خزنة معينة برقم مطلق (يستبدل القديم بالكامل). يرجع True لو نجح."""
    sheet = get_vaults_worksheet()
    if sheet is None or vault_name not in VAULT_NAMES:
        return False

    try:
        col_index = VAULT_NAMES.index(vault_name) + 1
        sheet.update_cell(2, col_index, new_value)
        return True
    except Exception:
        logger.exception("Failed to set vault balance")
        return False


def format_vault_balances() -> str:
    """يبني نص عرض أرصدة كل الخزائن."""
    balances = get_vault_balances()
    if balances is None:
        return "تعذر الاتصال بـ Google Sheet — تأكد من إعدادات الاتصال."

    lines = ["أرصدة الخزائن\n"]
    for name in VAULT_NAMES:
        lines.append(f"{name}: {balances.get(name, 0)}")
    return "\n".join(lines)


def calculate_income_report() -> str:
    """
    يقرأ كل صفوف الشيت ويحسب دخل اليوم، الأسبوع الحالي، والشهر الحالي.
    يرجع نص جاهز للعرض، أو رسالة خطأ واضحة لو فشل الاتصال بالشيت.
    """
    sheet = get_google_sheet()
    if sheet is None:
        return "تعذر الاتصال بـ Google Sheet — تأكد من إعدادات الاتصال."

    try:
        rows = sheet.get_all_values()
    except Exception:
        logger.exception("Failed to read rows from Google Sheet for income report")
        return "صار خطأ أثناء قراءة الشيت — حاول مرة ثانية بعد شوي."

    if len(rows) <= 1:
        return "ماكو أي عمليات دفع مسجلة لحد الحين."

    now = datetime.now(timezone.utc)
    today = now.date()
    week_start = today - timedelta(days=today.weekday())  # الاثنين هو بداية الأسبوع
    month_start = today.replace(day=1)

    total_today = 0
    total_week = 0
    total_month = 0

    # نتجاوز صف العناوين (index 0)
    for row in rows[1:]:
        if len(row) < 2:
            continue
        date_str, amount_str = row[0], row[1]
        try:
            row_date = datetime.strptime(date_str.split(" ")[0], "%Y-%m-%d").date()
            amount = int(float(amount_str))
        except (ValueError, IndexError):
            continue  # صف فيه بيانات غير متوقعة — نتجاهله بدل ما نكرش

        if row_date == today:
            total_today += amount
        if row_date >= week_start:
            total_week += amount
        if row_date >= month_start:
            total_month += amount

    return (
        "تقرير الدخل\n\n"
        f"اليوم: {total_today}\n"
        f"هذا الأسبوع: {total_week}\n"
        f"هذا الشهر: {total_month}"
    )


# ------------------------------------------------------------------
# طبقة الإحصائيات الشاملة — تدعم فترات مرنة (اليوم/الأسبوع/الشهر/كل
# الوقت/تاريخ محدد يدوي)، تفصيل حسب المنتج، وإحصائيات حسابات ChatGPT.
# ------------------------------------------------------------------

def parse_stats_period(period_key: str) -> tuple[object, object] | None:
    """
    يحول مفتاح فترة (جاهز أو نص يدوي) إلى (start_date, end_date) شامل
    الطرفين. يرجع None لو الفترة "كل الوقت" (بدون حدود)، أو يرمي
    ValueError لو نص يدوي غير مفهوم.
    """
    now = datetime.now(timezone.utc)
    today = now.date()

    if period_key == "today":
        return today, today
    if period_key == "week":
        week_start = today - timedelta(days=today.weekday())
        return week_start, today
    if period_key == "month":
        month_start = today.replace(day=1)
        return month_start, today
    if period_key == "all":
        return None

    # نص يدوي: YYYY-MM-DD (يوم محدد) أو YYYY-MM (شهر محدد)
    if len(period_key) == 10:
        d = datetime.strptime(period_key, "%Y-%m-%d").date()
        return d, d
    if len(period_key) == 7:
        d = datetime.strptime(period_key + "-01", "%Y-%m-%d").date()
        if d.month == 12:
            next_month = d.replace(year=d.year + 1, month=1)
        else:
            next_month = d.replace(month=d.month + 1)
        month_end = next_month - timedelta(days=1)
        return d, month_end

    raise ValueError(f"Unrecognized period format: {period_key!r}")


def get_payment_rows_in_period(period_key: str) -> list[list[str]] | None:
    """
    يرجع كل صفوف الدفعات (بدون صف العناوين) اللي تقع بالفترة المحددة.
    يرجع None لو فشل الاتصال بالشيت.
    """
    sheet = get_google_sheet()
    if sheet is None:
        return None

    try:
        rows = sheet.get_all_values()
    except Exception:
        logger.exception("Failed to read payment rows for stats")
        return None

    if len(rows) <= 1:
        return []

    date_range = parse_stats_period(period_key)
    filtered = []

    for row in rows[1:]:
        if len(row) < 1 or not row[0].strip():
            continue
        try:
            row_date = datetime.strptime(row[0].split(" ")[0], "%Y-%m-%d").date()
        except (ValueError, IndexError):
            continue

        if date_range is not None:
            start, end = date_range
            if not (start <= row_date <= end):
                continue

        filtered.append(row)

    return filtered


def get_expense_rows_in_period(period_key: str) -> list[list[str]] | None:
    """نفس get_payment_rows_in_period بس لصفحة المصروفات."""
    sheet = get_expenses_worksheet()
    if sheet is None:
        return None

    try:
        rows = sheet.get_all_values()
    except Exception:
        logger.exception("Failed to read expense rows for stats")
        return None

    if len(rows) <= 1:
        return []

    date_range = parse_stats_period(period_key)
    filtered = []

    for row in rows[1:]:
        if len(row) < 1 or not row[0].strip():
            continue
        try:
            row_date = datetime.strptime(row[0].split(" ")[0], "%Y-%m-%d").date()
        except (ValueError, IndexError):
            continue

        if date_range is not None:
            start, end = date_range
            if not (start <= row_date <= end):
                continue

        filtered.append(row)

    return filtered


PERIOD_LABELS = {"today": "اليوم", "week": "هذا الأسبوع", "month": "هذا الشهر", "all": "كل الوقت"}


def format_period_label(period_key: str) -> str:
    if period_key in PERIOD_LABELS:
        return PERIOD_LABELS[period_key]
    return period_key  # نص تاريخ يدوي، نعرضه كما هو


def calculate_totals_summary(period_key: str) -> str:
    """يحسب دخل/مصروف/صافي إجمالي لفترة معينة. يرجع نص جاهز للعرض."""
    payment_rows = get_payment_rows_in_period(period_key)
    expense_rows = get_expense_rows_in_period(period_key)

    if payment_rows is None or expense_rows is None:
        return "تعذر الاتصال بـ Google Sheet — تأكد من إعدادات الاتصال."

    total_income = 0
    for row in payment_rows:
        if len(row) >= 2 and row[1].strip():
            try:
                total_income += int(float(row[1]))
            except ValueError:
                continue

    total_expense = 0
    for row in expense_rows:
        if len(row) >= 2 and row[1].strip():
            try:
                total_expense += int(float(row[1]))
            except ValueError:
                continue

    net = total_income - total_expense
    label = format_period_label(period_key)

    return (
        f"إحصائيات {label}\n\n"
        f"الدخل: {total_income}\n"
        f"المصروف: {total_expense}\n"
        f"الصافي: {net}"
    )


def calculate_product_breakdown(period_key: str) -> str:
    """يحسب دخل + عدد عمليات لكل منتج بفترة معينة. يرجع نص جاهز للعرض."""
    payment_rows = get_payment_rows_in_period(period_key)
    if payment_rows is None:
        return "تعذر الاتصال بـ Google Sheet — تأكد من إعدادات الاتصال."

    label = format_period_label(period_key)
    if not payment_rows:
        return f"ماكو أي عمليات دفع مسجلة لفترة {label}."

    product_totals: dict[str, int] = {}
    product_counts: dict[str, int] = {}

    for row in payment_rows:
        if len(row) < 4:
            continue
        product = row[3].strip()
        if not product:
            continue
        amount_str = row[1].strip() if len(row) >= 2 else ""
        try:
            amount = int(float(amount_str)) if amount_str else 0
        except ValueError:
            amount = 0

        product_totals[product] = product_totals.get(product, 0) + amount
        product_counts[product] = product_counts.get(product, 0) + 1

    lines = [f"تفصيل المنتجات — {label}\n"]
    for product in sorted(product_totals.keys(), key=lambda p: product_totals[p], reverse=True):
        lines.append(f"{product}: {product_totals[product]} ({product_counts[product]} عملية)")

    return "\n".join(lines)


def calculate_chatgpt_account_stats() -> tuple[int, int]:
    """
    يرجع (عدد الحسابات الخاصة، عدد الحسابات المشتركة) — الخاصة تُحسب من
    عمود حسابات جات بالشيت (نص يبدأ بـ"خاص")، والمشتركة من عدد الحسابات
    المسجلة بـ Supabase (totp_accounts) اللي إلها زبون واحد أو أكثر
    مرتبط فيها (عن طريق totp_links).
    """
    private_count = 0
    sheet = get_google_sheet()
    if sheet is not None:
        try:
            rows = sheet.get_all_values()
            seen_chat_ids = set()
            for row in rows[1:]:
                if len(row) < SHEET_COL_CHATGPT_ACCOUNT:
                    continue
                account_text = row[SHEET_COL_CHATGPT_ACCOUNT - 1].strip()
                chat_id = row[SHEET_COL_CHAT_ID - 1].strip() if len(row) >= SHEET_COL_CHAT_ID else ""
                if account_text.startswith("خاص") and chat_id and chat_id not in seen_chat_ids:
                    seen_chat_ids.add(chat_id)
                    private_count += 1
        except Exception:
            logger.exception("Failed to count private ChatGPT accounts from sheet")

    shared_count = 0
    try:
        res = supabase.table("totp_accounts").select("id").execute()
        shared_count = len(res.data) if res.data else 0
    except Exception:
        logger.exception("Failed to count shared accounts from Supabase")

    return private_count, shared_count


def get_shared_accounts_list() -> list[dict]:
    """يرجع قائمة الحسابات المشتركة (id, link_code, label) من Supabase."""
    try:
        res = supabase.table("totp_accounts").select("id, link_code, label").execute()
        return res.data or []
    except Exception:
        logger.exception("Failed to fetch shared accounts list")
        return []


def get_customers_for_account(account_id) -> list[int]:
    """يرجع قائمة chat_id تبع كل الزباين المرتبطين بحساب مشترك معين."""
    try:
        res = supabase.table("totp_links").select("chat_id").eq("account_id", account_id).execute()
        return [r["chat_id"] for r in (res.data or [])]
    except Exception:
        logger.exception("Failed to fetch customers for account")
        return []


def build_stats_main_keyboard() -> InlineKeyboardMarkup:
    """الشاشة الرئيسية للإحصائيات — ستة أزرار."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 إجمالي الدخل/المصروف/الصافي", callback_data="stats_totals_default")],
        [InlineKeyboardButton("📦 تفصيل حسب المنتج", callback_data="stats_products_period")],
        [InlineKeyboardButton("🤖 حسابات ChatGPT", callback_data="stats_chatgpt_main")],
        [InlineKeyboardButton("📅 اختيار فترة زمنية", callback_data="stats_totals_period")],
        [InlineKeyboardButton("💵 أرصدة الخزائن", callback_data="stats_vaults_view")],
        [InlineKeyboardButton("✏️ تعديل رصيد", callback_data="stats_vaults_edit")],
    ])


def build_period_selection_keyboard(next_action: str) -> InlineKeyboardMarkup:
    """
    شاشة اختيار فترة (تُستخدم لأكثر من غرض — next_action يحدد شنو يصير
    بعد الاختيار: "totals" لإجمالي بس، أو "products" لتفصيل المنتجات).
    """
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("اليوم", callback_data=f"stats_period_{next_action}_today"),
            InlineKeyboardButton("الأسبوع", callback_data=f"stats_period_{next_action}_week"),
        ],
        [
            InlineKeyboardButton("الشهر", callback_data=f"stats_period_{next_action}_month"),
            InlineKeyboardButton("كل الوقت", callback_data=f"stats_period_{next_action}_all"),
        ],
        [InlineKeyboardButton("✏️ تاريخ محدد", callback_data=f"stats_period_manual_{next_action}")],
        [InlineKeyboardButton(BTN_BACK, callback_data="stats_back_to_main")],
    ])


def build_chatgpt_main_keyboard(private_count: int, shared_count: int) -> InlineKeyboardMarkup:
    total = private_count + shared_count
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"الإجمالي: {total}", callback_data="stats_noop")],
        [InlineKeyboardButton(f"الحسابات الخاصة ({private_count})", callback_data="stats_chatgpt_private")],
        [InlineKeyboardButton(f"الحسابات المشتركة ({shared_count})", callback_data="stats_chatgpt_shared")],
        [InlineKeyboardButton(BTN_BACK, callback_data="stats_back_to_main")],
    ])


def build_shared_accounts_keyboard(accounts: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for acc in accounts:
        label = acc.get("label") or acc.get("link_code", "بدون اسم")
        rows.append([InlineKeyboardButton(label, callback_data=f"stats_account_{acc['id']}")])
    rows.append([InlineKeyboardButton(BTN_BACK, callback_data="stats_chatgpt_main")])
    return InlineKeyboardMarkup(rows)


def build_vault_edit_select_keyboard() -> InlineKeyboardMarkup:
    """شاشة اختيار الخزنة اللي تريد تعدل رصيدها."""
    rows = [[InlineKeyboardButton(v, callback_data=f"stats_vaultedit_select_{v}")] for v in VAULT_NAMES]
    rows.append([InlineKeyboardButton(BTN_BACK, callback_data="stats_back_to_main")])
    return InlineKeyboardMarkup(rows)


def build_vault_edit_mode_keyboard(vault_name: str) -> InlineKeyboardMarkup:
    """شاشة اختيار طريقة التعديل: تحديد رقم كامل، أو زيادة/نقصان."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 تحديد رقم كامل", callback_data=f"stats_vaultedit_mode_set_{vault_name}")],
        [InlineKeyboardButton("➕ زيادة", callback_data=f"stats_vaultedit_mode_add_{vault_name}")],
        [InlineKeyboardButton("➖ نقصان", callback_data=f"stats_vaultedit_mode_sub_{vault_name}")],
        [InlineKeyboardButton(BTN_BACK, callback_data="stats_vaults_edit")],
    ])


# ------------------------------------------------------------------
# طبقة الذكاء الاصطناعي — محدودة جداً ومستخدمة بس بحالة وحدة:
# لما الرسالة فيها كلمة مفتاحية لـ chatgpt، نحتاج نميز هل هذا طلب
# شراء/سؤال سعر فعلي، أو شكوى بمشكلة باشتراك موجود اصلا (يوقف الرد،
# وينبه الأونر). كل باقي التصنيف يعتمد على الكلمات المفتاحية مباشرة.
# ------------------------------------------------------------------
# موديل مخصص لفحص قصد الزبون وقت ذكر chatgpt — أدق بفهم اللهجة
# العراقية من الموديل الافتراضي، يستخدم بس لهذا الفحص المحدد
CHATGPT_CONTEXT_MODEL = "openai/gpt-oss-120b"

CHATGPT_CONTEXT_PROMPT = (
    "انت تحلل رسائل زبائن عراقيين بمتجر يبيع اشتراكات ChatGPT، باللهجة "
    "العراقية العامية. الرسالة الجاية فيها ذكر لـ ChatGPT (جات/چات/جي بي تي). "
    "صنف قصد الزبون الحقيقي لفئة وحدة بالضبط:\n\n"
    "شراء: يريد يشتري اشتراك جديد، يسأل عن السعر، يسأل هل متوفر، يريد يجدد.\n"
    "شكوى: عنده اشتراك جات موجود اصلا وفيه مشكلة (ما يشتغل، توقف، خطأ، رفض).\n"
    "غير_متعلق: يذكر كلمة جات بس مب بخصوص الشراء أو الاشتراك إطلاقاً — "
    "مثل سؤال عام، رأي، دردشة، أو حتى لو يسأل عن جات كموضوع عام مب متعلق "
    "بالمتجر (مثل \"شكو جات زين؟\" أو \"جات يفهم عربي؟\").\n\n"
    "امثلة:\n"
    "'عندي مشكلة بجات' → شكوى\n"
    "'جات ما يشتغل' → شكوى\n"
    "'اريد جات' → شراء\n"
    "'اشتراك جات موجود؟' → شراء\n"
    "'كم سعر جات' → شراء\n"
    "'شكو جات زين وياكم؟' → غير_متعلق\n"
    "'جات افضل لو جيميناي؟' → غير_متعلق\n"
    "'شنو رايكم بجات الجديد؟' → غير_متعلق\n\n"
    "رد بكلمة وحدة بالضبط: شراء، او شكوى، او غير_متعلق."
)


async def classify_chatgpt_context(text: str) -> str:
    """
    يستخدم الذكاء الاصطناعي (Groq، موديل CHATGPT_CONTEXT_MODEL) لتمييز
    قصد الزبون الحقيقي وقت أي ذكر لـ chatgpt بالرسالة. يرجع 'شراء' أو
    'شكوى' أو 'غير_متعلق' (افتراضي 'شراء' لو فشل الاتصال، عشان البوت
    يرد بأسعار chatgpt بدل ما يسكت أو يتجاهل رسالة قد تكون طلب حقيقي).

    ملاحظة: موديلات gpt-oss هي "reasoning models" — تحتاج reasoning_effort
    منخفض (عشان السرعة وتقليل التكلفة لمهمة تصنيف بسيطة) و max_completion_tokens
    كافي لاستيعاب أي تفكير داخلي قبل الجواب النهائي، وإلا ينقطع الرد.
    """
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {GROQ_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": CHATGPT_CONTEXT_MODEL,
                    "temperature": 0,
                    "max_completion_tokens": 500,
                    "reasoning_effort": "low",
                    "messages": [
                        {"role": "system", "content": CHATGPT_CONTEXT_PROMPT},
                        {"role": "user", "content": text},
                    ],
                },
            )
        resp.raise_for_status()
        data = resp.json()
        raw = data["choices"][0]["message"]["content"].strip()
        logger.info(f"classify_chatgpt_context raw response: {raw!r} (input: {text!r})")
        if "شكوى" in raw:
            return "شكوى"
        if "غير" in raw:
            return "غير_متعلق"
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


async def classify_intent(text: str) -> tuple[list[str], bool]:
    """
    المصنف الرئيسي المستخدم بكل رسالة. الأساس مطابقة كلمات مفتاحية
    مباشرة (سريع وموثوق، بدون ذكاء اصطناعي). الذكاء الاصطناعي يتفعل
    بس لو الرسالة تحتوي كلمة مفتاحية لـ chatgpt، عشان يميز قصد الزبون
    الحقيقي (شراء/شكوى/غير متعلق) — شي ما تقدر الكلمات المفتاحية وحدها
    تميزه بدقة، خصوصاً لما زبون يذكر "جات" بسياق عام مب متعلق بالشراء.

    يرجع (categories, is_chatgpt_complaint):
    - categories: قائمة فئات (ممكن فيها "شكوى_منتج" اذا كانت شكوى
      chatgpt — هذي فئة خاصة تخلي البوت يسكت وينبه الأونر).
    - is_chatgpt_complaint: True لو الذكاء الاصطناعي أكد انها شكوى.
    """
    categories = keyword_match_categories(text)

    if "chatgpt" in categories:
        # أي ذكر لـ chatgpt يحتاج فحص AI يحدد القصد الحقيقي — مب بس
        # حالة الشكوى، عشان نمنع رد أسعار خاطئ على رسائل مب متعلقة
        verdict = await classify_chatgpt_context(text)

        if verdict == "شكوى":
            categories = [c for c in categories if c != "chatgpt"]
            categories.append("شكوى_منتج")
            return categories, True

        if verdict == "غير_متعلق":
            # الزبون ذكر جات بس مب طالب شراء ولا عنده شكوى — نشيل فئة
            # chatgpt بالكامل، ما نرد عليها إطلاقاً (نتجاهل هذا الجزء)
            categories = [c for c in categories if c != "chatgpt"]

        # verdict == "شراء" → نخلي فئة chatgpt كما هي، يطلع رد الأسعار

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


async def handle_owner_command(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, bm=None) -> bool:
    """يعالج أوامر الأونر: /addaccount، /link، /resetcode، وaccept. يرجع True اذا كانت الرسالة أمر تم التعامل معه."""

    # accept — رد على صورة دفع معينة من الزبون (بمحادثتك Business وياه)
    # عشان تحولها لمحادثتك مع البوت، حتى لو تجاوزت حد 3 صور/6 ساعات
    if text.strip().lower() == "accept":
        if bm is not None and bm.reply_to_message and bm.reply_to_message.photo:
            await handle_incoming_payment_photo(update, context, bm.reply_to_message, bypass_rate_limit=True)
            await context.bot.send_message(chat_id=OWNER_USER_ID, text="✅ تم تحويل الصورة.")
        else:
            await context.bot.send_message(
                chat_id=OWNER_USER_ID,
                text="⚠️ لازم ترد على رسالة الصورة نفسها وتكتب accept.",
            )
        return True

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

        # لو اللابل يشبه إيميل، نعتبره حساب ChatGPT مشترك ونسجله بالشيت
        # (يكمل سطر ناقص لنفس الزبون لو موجود، وإلا يفتح سطر جديد)
        sheet_note = ""
        if bm is not None and "@" in label and "." in label:
            customer_name = bm.chat.full_name or bm.chat.first_name or "غير معروف"
            customer_username = bm.chat.username
            saved = upsert_chatgpt_account(chat_id, customer_name, customer_username, label)
            sheet_note = "\n✅ تم تسجيل الحساب بالشيت." if saved else "\n⚠️ فشل تسجيل الحساب بالشيت."

        await context.bot.send_message(
            chat_id=OWNER_USER_ID,
            text=f"✅ تم ربط هذا الزبون بالحساب ({label or link_code}).{sheet_note}",
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


async def handle_incoming_payment_photo(
    update: Update, context: ContextTypes.DEFAULT_TYPE, bm, bypass_rate_limit: bool = False
) -> None:
    """
    توصل صورة دفع من زبون بمحادثة Business — نحولها لمحادثتك الخاصة
    مع البوت (مو Business) مع زرين: تأكيد/إلغاء، ونحفظ بيانات الزبون
    بحالة مؤقتة عشان نربطها لاحقاً بعملية التسجيل.

    محدودة بـ 3 صور/6 ساعات لكل زبون — أي صورة تتجاوز الحد تُتجاهل
    بالكامل (بدون تحويل وبدون حفظ)، وتقدر تحول أي وحدة منهن يدوياً
    بالرد عليها بكلمة accept بمحادثتك مع الزبون (Business) —
    bypass_rate_limit=True تُستخدم بالضبط بهذي الحالة لتخطي الحد.
    """
    customer_chat_id = bm.chat.id

    if not bypass_rate_limit and not is_photo_within_rate_limit(customer_chat_id):
        logger.info(f"Photo from chat_id={customer_chat_id} ignored — exceeded rate limit")
        return

    customer_name = bm.chat.full_name or bm.chat.first_name or "غير معروف"
    customer_username = bm.chat.username

    try:
        sent = await context.bot.send_photo(
            chat_id=OWNER_USER_ID,
            photo=bm.photo[-1].file_id,  # أعلى دقة متوفرة
            caption=f"صورة دفع من: {customer_name}" + (f" (@{customer_username})" if customer_username else ""),
            reply_markup=build_confirm_cancel_keyboard(),
        )
    except Exception:
        logger.exception("Failed to forward payment photo to owner")
        return

    _pending_payments[sent.message_id] = {
        "customer_name": customer_name,
        "customer_username": customer_username,
        "customer_chat_id": customer_chat_id,
        "product": None,
        "payments": [],
        "pending_method": None,
        "pending_amount": 0,
        "awaiting_manual_amount": False,
    }


async def handle_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    يعالج كل ضغطات الأزرار بمحادثتك الخاصة مع البوت لتسجيل عملية دفع —
    من التأكيد الأولي، اختيار المنتج وطريقة الدفع، تحديد المبلغ،
    ولين التثبيت النهائي وحفظ السطر بالشيت.
    """
    query = update.callback_query
    if query.from_user.id != OWNER_USER_ID:
        await query.answer("هذا الزر مخصص للأونر بس.", show_alert=True)
        return

    message_id = query.message.message_id
    state = _pending_payments.get(message_id)

    if state is None:
        await query.answer("انتهت صلاحية هذي العملية أو تم التعامل معها.", show_alert=True)
        return

    data = query.data
    await query.answer()

    # -------------------- إلغاء --------------------
    if data == "pay_cancel":
        del _pending_payments[message_id]
        try:
            await query.message.delete()
        except Exception:
            logger.exception("Failed to delete cancelled payment photo message")
        return

    # -------------------- تأكيد أولي: يفتح شاشة اختيار المنتج --------------------
    if data == "pay_confirm":
        await query.edit_message_caption(
            caption=format_payment_summary(state),
            reply_markup=build_product_keyboard(),
        )
        return

    # -------------------- اختيار منتج مباشر (سريع أو من القائمة) --------------------
    if data.startswith("pay_product_") and data != "pay_product_list":
        product = data[len("pay_product_"):]
        state["product"] = product
        await query.edit_message_caption(
            caption=format_payment_summary(state),
            reply_markup=build_summary_keyboard(has_product=True, has_payment=bool(state["payments"])),
        )
        return

    # -------------------- فتح قائمة بقية المنتجات --------------------
    if data == "pay_product_list":
        await query.edit_message_caption(
            caption=format_payment_summary(state),
            reply_markup=build_product_list_keyboard(),
        )
        return

    # -------------------- رجوع من قائمة المنتجات لشاشة اختيار المنتج الأولى --------------------
    if data == "pay_back_to_product":
        await query.edit_message_caption(
            caption=format_payment_summary(state),
            reply_markup=build_product_keyboard(),
        )
        return

    # -------------------- اختيار طريقة دفع مباشرة (سريعة أو من القائمة) --------------------
    if data.startswith("pay_method_") and data != "pay_method_list":
        method = data[len("pay_method_"):]
        state["pending_method"] = method
        state["pending_amount"] = 0
        await query.edit_message_caption(
            caption=format_payment_summary(state) + f"\n\nطريقة الدفع المختارة: {method}\nحدد المبلغ:",
            reply_markup=build_amount_keyboard(),
        )
        return

    # -------------------- فتح قائمة بقية طرق الدفع --------------------
    if data == "pay_method_list":
        await query.edit_message_caption(
            caption=format_payment_summary(state),
            reply_markup=build_method_list_keyboard(),
        )
        return

    # -------------------- رجوع من قائمة طرق الدفع لشاشة الملخص (اختيار سريع للطريقة) --------------------
    if data == "pay_back_to_method":
        await query.edit_message_caption(
            caption=format_payment_summary(state),
            reply_markup=build_summary_keyboard(has_product=bool(state["product"]), has_payment=bool(state["payments"])),
        )
        return

    # -------------------- زيادة المبلغ المتراكم --------------------
    if data == "pay_amount_add_small":
        state["pending_amount"] += PAYMENT_AMOUNT_STEP_SMALL
        await query.edit_message_caption(
            caption=format_payment_summary(state)
            + f"\n\nطريقة الدفع المختارة: {state['pending_method']}\nالمبلغ الحالي: {state['pending_amount']}",
            reply_markup=build_amount_keyboard(),
        )
        return

    if data == "pay_amount_add_large":
        state["pending_amount"] += PAYMENT_AMOUNT_STEP_LARGE
        await query.edit_message_caption(
            caption=format_payment_summary(state)
            + f"\n\nطريقة الدفع المختارة: {state['pending_method']}\nالمبلغ الحالي: {state['pending_amount']}",
            reply_markup=build_amount_keyboard(),
        )
        return

    # -------------------- طلب إدخال يدوي (ينتظر رسالة نصية جاية) --------------------
    if data == "pay_amount_manual":
        state["awaiting_manual_amount"] = True
        await query.edit_message_caption(
            caption=format_payment_summary(state)
            + f"\n\nطريقة الدفع المختارة: {state['pending_method']}\nاكتب المبلغ رقم بس بالرسالة الجاية (كـ رد على هذي الرسالة):",
            reply_markup=None,
        )
        return

    # -------------------- تثبيت المبلغ لهذي الطريقة --------------------
    if data == "pay_amount_commit":
        method = state.get("pending_method")
        amount = state.get("pending_amount", 0)
        if method and amount > 0:
            state["payments"].append((method, amount))
        state["pending_method"] = None
        state["pending_amount"] = 0
        await query.edit_message_caption(
            caption=format_payment_summary(state),
            reply_markup=build_summary_keyboard(has_product=bool(state["product"]), has_payment=True),
        )
        return

    # -------------------- تثبيت العملية بالكامل وحفظها بالشيت --------------------
    if data == "pay_finalize":
        if not state["product"] or not state["payments"]:
            await query.answer("لازم تختار منتج وطريقة دفع وحدة على الأقل قبل التثبيت.", show_alert=True)
            return

        saved = append_payment_row(state)

        # نزيد رصيد كل خزنة مطابقة لطرق الدفع المستخدمة بهذي العملية
        if saved:
            for method, amount in state["payments"]:
                if method in VAULT_NAMES:
                    adjust_vault_balance(method, amount)

        # نحفظ نسخة خفيفة من بيانات الزبون بعد التثبيت — عشان نقدر نربطها
        # لاحقاً لو رديت على رسالة التأكيد بإيميل/خاص (حساب ChatGPT خاص)
        _completed_payments[message_id] = {
            "customer_name": state["customer_name"],
            "customer_username": state.get("customer_username"),
            "customer_chat_id": state.get("customer_chat_id"),
            "product": state["product"],
        }
        del _pending_payments[message_id]

        if saved:
            final_text = format_payment_summary(state) + "\n\n✅ تم الحفظ بنجاح."
        else:
            final_text = format_payment_summary(state) + "\n\n⚠️ فشل الحفظ بـ Google Sheet — تحقق من الاتصال يدوياً."

        try:
            await query.edit_message_caption(caption=final_text, reply_markup=None)
        except Exception:
            logger.exception("Failed to update final payment confirmation message")
        return


async def handle_manual_amount_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    يلتقط رسالة نصية جاية منك (owner) بمحادثتك الخاصة مع البوت وقت ما
    البوت ينتظر إدخال مبلغ يدوي لعملية دفع جارية (رد على رسالة الصورة).
    يرجع True لو عالج الرسالة، False لو ما فيه عملية منتظرة إدخال يدوي.
    """
    message = update.message
    if not message or not message.text or not message.reply_to_message:
        return False

    replied_id = message.reply_to_message.message_id
    state = _pending_payments.get(replied_id)
    if state is None or not state.get("awaiting_manual_amount"):
        return False

    try:
        amount = int(re.sub(r"[^\d]", "", message.text))
    except ValueError:
        await message.reply_text("الرجاء إدخال رقم صحيح فقط.")
        return True

    if amount <= 0:
        await message.reply_text("الرجاء إدخال مبلغ أكبر من صفر.")
        return True

    state["pending_amount"] = amount
    state["awaiting_manual_amount"] = False

    try:
        await context.bot.edit_message_caption(
            chat_id=OWNER_USER_ID,
            message_id=replied_id,
            caption=format_payment_summary(state)
            + f"\n\nطريقة الدفع المختارة: {state['pending_method']}\nالمبلغ الحالي: {amount}",
            reply_markup=build_amount_keyboard(),
        )
    except Exception:
        logger.exception("Failed to update caption after manual amount entry")

    return True


async def handle_expense_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعالج كل ضغطات الأزرار الخاصة بفلو تسجيل مصروف."""
    global _pending_expense

    query = update.callback_query
    if query.from_user.id != OWNER_USER_ID:
        await query.answer("هذا الزر مخصص للأونر بس.", show_alert=True)
        return

    if _pending_expense is None or _pending_expense.get("message_id") != query.message.message_id:
        await query.answer("انتهت صلاحية هذي العملية أو تم التعامل معها.", show_alert=True)
        return

    data = query.data
    await query.answer()
    expense = _pending_expense

    if data == "exp_amount_add_small":
        expense["amount"] = expense.get("amount", 0) + PAYMENT_AMOUNT_STEP_SMALL
        await query.edit_message_text(
            text=format_expense_summary(expense), reply_markup=build_expense_amount_keyboard()
        )
        return

    if data == "exp_amount_add_large":
        expense["amount"] = expense.get("amount", 0) + PAYMENT_AMOUNT_STEP_LARGE
        await query.edit_message_text(
            text=format_expense_summary(expense), reply_markup=build_expense_amount_keyboard()
        )
        return

    if data == "exp_amount_manual":
        expense["awaiting_manual_amount"] = True
        await query.edit_message_text(
            text=format_expense_summary(expense) + "\n\nاكتب المبلغ رقم بس بالرسالة الجاية (كـ رد على هذي الرسالة):",
            reply_markup=None,
        )
        return

    if data == "exp_amount_commit":
        if not expense.get("amount"):
            await query.answer("لازم تحدد مبلغ أكبر من صفر أول.", show_alert=True)
            return
        await query.edit_message_text(
            text=format_expense_summary(expense), reply_markup=build_expense_vault_keyboard()
        )
        return

    if data.startswith("exp_vault_"):
        vault_key = data[len("exp_vault_"):]
        expense["vault"] = None if vault_key == "none" else vault_key
        await query.edit_message_text(
            text=format_expense_summary(expense), reply_markup=build_expense_reason_keyboard()
        )
        return

    if data.startswith("exp_reason_") and data not in ("exp_reason_list", "exp_reason_manual"):
        reason = data[len("exp_reason_"):]
        expense["reason"] = reason
        saved = append_expense_row(expense["amount"], reason)
        if saved and expense.get("vault"):
            adjust_vault_balance(expense["vault"], -expense["amount"])
        final_text = format_expense_summary(expense) + (
            "\n\n✅ تم الحفظ بنجاح." if saved else "\n\n⚠️ فشل الحفظ بـ Google Sheet — تحقق من الاتصال يدوياً."
        )
        try:
            await query.edit_message_text(text=final_text, reply_markup=None)
        except Exception:
            logger.exception("Failed to update final expense confirmation message")
        _pending_expense = None
        return

    if data == "exp_reason_list":
        await query.edit_message_text(
            text=format_expense_summary(expense), reply_markup=build_expense_reason_list_keyboard()
        )
        return

    if data == "exp_back_to_reason":
        await query.edit_message_text(
            text=format_expense_summary(expense), reply_markup=build_expense_reason_keyboard()
        )
        return

    if data == "exp_reason_manual":
        expense["awaiting_manual_reason"] = True
        await query.edit_message_text(
            text=format_expense_summary(expense) + "\n\nاكتب سبب المصروف بالرسالة الجاية (كـ رد على هذي الرسالة):",
            reply_markup=None,
        )
        return



async def handle_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعالج كل ضغطات الأزرار الخاصة بشاشات الإحصائيات."""
    global _pending_stats_period, _pending_vault_edit

    query = update.callback_query
    if query.from_user.id != OWNER_USER_ID:
        await query.answer("هذا الزر مخصص للأونر بس.", show_alert=True)
        return

    data = query.data
    await query.answer()

    if data == "stats_noop":
        return

    if data == "stats_back_to_main":
        await query.edit_message_text(text="الإحصائيات", reply_markup=build_stats_main_keyboard())
        return

    if data == "stats_totals_default":
        text = calculate_totals_summary("month")
        await query.edit_message_text(
            text=text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(BTN_BACK, callback_data="stats_back_to_main")]])
        )
        return

    if data == "stats_totals_period":
        await query.edit_message_text(text="اختر الفترة:", reply_markup=build_period_selection_keyboard("totals"))
        return

    if data == "stats_products_period":
        await query.edit_message_text(text="اختر الفترة:", reply_markup=build_period_selection_keyboard("products"))
        return

    if data.startswith("stats_period_manual_"):
        next_action = data[len("stats_period_manual_"):]
        _pending_stats_period = {"message_id": query.message.message_id, "next_action": next_action}
        await query.edit_message_text(
            text="اكتب التاريخ كـ رد على هذي الرسالة (مثال: 2026-07 لشهر، أو 2026-07-15 ليوم):",
            reply_markup=None,
        )
        return

    if data.startswith("stats_period_"):
        # الصيغة: stats_period_<next_action>_<period_key>
        remainder = data[len("stats_period_"):]
        next_action, period_key = remainder.rsplit("_", 1)
        back_target = "stats_totals_period" if next_action == "totals" else "stats_products_period"
        back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(BTN_BACK, callback_data=back_target)]])

        if next_action == "totals":
            text = calculate_totals_summary(period_key)
        else:
            text = calculate_product_breakdown(period_key)

        await query.edit_message_text(text=text, reply_markup=back_keyboard)
        return

    if data == "stats_chatgpt_main":
        private_count, shared_count = calculate_chatgpt_account_stats()
        await query.edit_message_text(
            text="حسابات ChatGPT",
            reply_markup=build_chatgpt_main_keyboard(private_count, shared_count),
        )
        return

    if data == "stats_chatgpt_private":
        private_count, _ = calculate_chatgpt_account_stats()
        await query.edit_message_text(
            text=f"عدد الحسابات الخاصة: {private_count}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(BTN_BACK, callback_data="stats_chatgpt_main")]]),
        )
        return

    if data == "stats_chatgpt_shared":
        accounts = get_shared_accounts_list()
        if not accounts:
            await query.edit_message_text(
                text="ماكو حسابات مشتركة مسجلة.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(BTN_BACK, callback_data="stats_chatgpt_main")]]),
            )
            return
        await query.edit_message_text(text="اختر حساب:", reply_markup=build_shared_accounts_keyboard(accounts))
        return

    if data.startswith("stats_account_"):
        account_id = data[len("stats_account_"):]
        customer_chat_ids = get_customers_for_account(account_id)
        if not customer_chat_ids:
            text = "ماكو زباين مرتبطين بهذا الحساب."
        else:
            text = f"عدد الزباين: {len(customer_chat_ids)}\n\n" + "\n".join(str(cid) for cid in customer_chat_ids)
        await query.edit_message_text(
            text=text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(BTN_BACK, callback_data="stats_chatgpt_shared")]]),
        )
        return

    if data == "stats_vaults_view":
        text = format_vault_balances()
        await query.edit_message_text(
            text=text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(BTN_BACK, callback_data="stats_back_to_main")]])
        )
        return

    if data == "stats_vaults_edit":
        await query.edit_message_text(text="اختر الخزنة:", reply_markup=build_vault_edit_select_keyboard())
        return

    if data.startswith("stats_vaultedit_select_"):
        vault_name = data[len("stats_vaultedit_select_"):]
        await query.edit_message_text(
            text=f"الخزنة: {vault_name}\nاختر طريقة التعديل:",
            reply_markup=build_vault_edit_mode_keyboard(vault_name),
        )
        return

    if data.startswith("stats_vaultedit_mode_"):
        # الصيغة: stats_vaultedit_mode_<set|add|sub>_<vault_name>
        remainder = data[len("stats_vaultedit_mode_"):]
        mode, vault_name = remainder.split("_", 1)
        _pending_vault_edit = {"message_id": query.message.message_id, "vault_name": vault_name, "mode": mode}
        mode_label = {"set": "الرقم الجديد الكامل", "add": "مبلغ الزيادة", "sub": "مبلغ النقصان"}[mode]
        await query.edit_message_text(
            text=f"الخزنة: {vault_name}\nاكتب {mode_label} كـ رد على هذي الرسالة:",
            reply_markup=None,
        )
        return



async def handle_chatgpt_account_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    يلتقط رد منك (owner) على رسالة "تم الحفظ بنجاح" لعملية دفع منتجها
    جات — لو رديت بإيميل يسجله بعمود حسابات جات، لو رديت بكلمة "خاص"
    يسجل "خاص" بنفس العمود. يشتغل بس لو المنتج جات، يرجع True لو عالج
    الرسالة (حتى لو رفضها لسبب ما)، False لو ما تنطبق الشروط إطلاقاً.
    """
    message = update.message
    if not message or not message.text or not message.reply_to_message:
        return False

    replied_id = message.reply_to_message.message_id
    completed = _completed_payments.get(replied_id)
    if completed is None:
        return False

    if completed.get("product") != CHATGPT_PRODUCT_NAME:
        return False  # هذا الرد على عملية دفع منتج ثاني، مو جات — نتجاهل بصمت

    text = message.text.strip()
    is_private_only = text.lower() in ("خاص", "private")

    if is_private_only:
        account_text = "خاص"
    elif "@" in text and "." in text:
        account_text = f"خاص - {text}"
    else:
        await message.reply_text("الرجاء الرد بإيميل صحيح، أو كتابة كلمة 'خاص' فقط.")
        return True

    chat_id = completed.get("customer_chat_id")
    if chat_id is None:
        await message.reply_text("⚠️ ما لكيت بيانات الزبون لهذي العملية.")
        return True

    saved = upsert_chatgpt_account(
        chat_id, completed["customer_name"], completed.get("customer_username"), account_text
    )

    if saved:
        await message.reply_text("✅ تم تسجيل معلومة الحساب بالشيت.")
    else:
        await message.reply_text("⚠️ فشل الحفظ بـ Google Sheet — تحقق من الاتصال يدوياً.")

    return True


async def handle_expense_manual_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    يلتقط رد نصي منك أثناء تسجيل مصروف — إما مبلغ يدوي أو سبب حر،
    حسب أي خطوة بانتظار إدخال. يرجع True لو عالج الرسالة.
    """
    global _pending_expense

    message = update.message
    if not message or not message.text or not message.reply_to_message:
        return False
    if _pending_expense is None or _pending_expense.get("message_id") != message.reply_to_message.message_id:
        return False

    expense = _pending_expense

    if expense.get("awaiting_manual_amount"):
        try:
            amount = int(re.sub(r"[^\d]", "", message.text))
        except ValueError:
            await message.reply_text("الرجاء إدخال رقم صحيح فقط.")
            return True
        if amount <= 0:
            await message.reply_text("الرجاء إدخال مبلغ أكبر من صفر.")
            return True

        expense["amount"] = amount
        expense["awaiting_manual_amount"] = False
        try:
            await context.bot.edit_message_text(
                chat_id=OWNER_USER_ID,
                message_id=expense["message_id"],
                text=format_expense_summary(expense),
                reply_markup=build_expense_amount_keyboard(),
            )
        except Exception:
            logger.exception("Failed to update expense caption after manual amount entry")
        return True

    if expense.get("awaiting_manual_reason"):
        reason = message.text.strip()
        if not reason:
            await message.reply_text("الرجاء كتابة سبب غير فارغ.")
            return True

        expense["reason"] = reason
        expense["awaiting_manual_reason"] = False
        saved = append_expense_row(expense["amount"], reason)
        if saved and expense.get("vault"):
            adjust_vault_balance(expense["vault"], -expense["amount"])
        final_text = format_expense_summary(expense) + (
            "\n\n✅ تم الحفظ بنجاح." if saved else "\n\n⚠️ فشل الحفظ بـ Google Sheet — تحقق من الاتصال يدوياً."
        )
        try:
            await context.bot.edit_message_text(
                chat_id=OWNER_USER_ID, message_id=expense["message_id"], text=final_text, reply_markup=None
            )
        except Exception:
            logger.exception("Failed to update final expense confirmation after manual reason entry")
        _pending_expense = None
        return True

    return False


async def handle_reply_keyboard_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    يعالج ضغطات أزرار لوحة المفاتيح الثابتة (Reply Keyboard) تحت صندوق
    الكتابة: تسجيل مصروف، تقرير الدخل، إضافة حساب. يرجع True لو عالج.
    """
    global _pending_expense, _pending_add_account

    message = update.message
    if not message or not message.text:
        return False

    text = message.text.strip()

    if text == BTN_EXPENSE:
        sent = await message.reply_text(
            format_expense_summary({"amount": 0, "vault": None, "reason": None}),
            reply_markup=build_expense_amount_keyboard(),
        )
        _pending_expense = {
            "message_id": sent.message_id,
            "amount": 0,
            "vault": None,
            "reason": None,
            "awaiting_manual_amount": False,
            "awaiting_manual_reason": False,
        }
        return True

    if text == BTN_INCOME:
        report = calculate_income_report()
        await message.reply_text(report)
        return True

    if text == BTN_ADD_ACCOUNT:
        sent = await message.reply_text("أرسل رمز الربط (link code) للحساب الجديد:")
        _pending_add_account = {"message_id": sent.message_id, "step": "link_code", "link_code": None, "secret": None}
        return True

    if text == BTN_STATS:
        await message.reply_text("الإحصائيات", reply_markup=build_stats_main_keyboard())
        return True

    return False


async def handle_add_account_flow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    يلتقط ردودك المتتالية أثناء فلو إضافة حساب TOTP التفاعلي (رمز
    ربط → مفتاح سري → لابل)، وينفذ /addaccount بالخلفية عند الاكتمال.
    """
    global _pending_add_account

    message = update.message
    if not message or not message.text or _pending_add_account is None:
        return False

    text = message.text.strip()
    step = _pending_add_account["step"]

    if step == "link_code":
        _pending_add_account["link_code"] = text
        _pending_add_account["step"] = "secret"
        await message.reply_text("تمام. الحين أرسل المفتاح السري (Secret Key):")
        return True

    if step == "secret":
        _pending_add_account["secret"] = text
        _pending_add_account["step"] = "label"
        await message.reply_text("تمام. الحين أرسل اللابل/الملاحظة (أو أرسل - لتركها فاضية):")
        return True

    if step == "label":
        label = "" if text == "-" else text
        link_code = _pending_add_account["link_code"]
        secret = _pending_add_account["secret"]
        _pending_add_account = None

        try:
            supabase.table("totp_accounts").insert(
                {"link_code": link_code, "secret": secret, "label": label}
            ).execute()
            await message.reply_text(f"✅ تمت اضافة الحساب.\nرمز الربط: {link_code}\nملاحظة: {label or '—'}")
        except Exception as e:
            logger.exception("Failed to add account via interactive flow")
            await message.reply_text(f"⚠️ فشلت الاضافة — تأكد ان رمز الربط '{link_code}' غير مستخدم سابقاً.\n{e}")
        return True

    return False


async def handle_stats_manual_period_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    يلتقط رد نصي منك أثناء انتظار تاريخ يدوي بشاشة الإحصائيات (YYYY-MM
    أو YYYY-MM-DD)، ويعرض النتيجة المطلوبة (إجمالي أو تفصيل منتجات).
    """
    global _pending_stats_period

    message = update.message
    if not message or not message.text or not message.reply_to_message:
        return False
    if _pending_stats_period is None or _pending_stats_period.get("message_id") != message.reply_to_message.message_id:
        return False

    period_key = message.text.strip()
    next_action = _pending_stats_period["next_action"]
    _pending_stats_period = None

    try:
        parse_stats_period(period_key)
    except ValueError:
        await message.reply_text("صيغة غير صحيحة. استخدم YYYY-MM أو YYYY-MM-DD (مثال: 2026-07 أو 2026-07-15).")
        return True

    back_target = "stats_totals_period" if next_action == "totals" else "stats_products_period"
    back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(BTN_BACK, callback_data=back_target)]])

    if next_action == "totals":
        text = calculate_totals_summary(period_key)
    else:
        text = calculate_product_breakdown(period_key)

    await message.reply_text(text, reply_markup=back_keyboard)
    return True


async def handle_vault_edit_manual_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    يلتقط رد نصي منك أثناء انتظار مبلغ يدوي لتعديل رصيد خزنة معينة
    (تحديد رقم كامل / زيادة / نقصان)، وينفذ التعديل.
    """
    global _pending_vault_edit

    message = update.message
    if not message or not message.text or not message.reply_to_message:
        return False
    if _pending_vault_edit is None or _pending_vault_edit.get("message_id") != message.reply_to_message.message_id:
        return False

    vault_name = _pending_vault_edit["vault_name"]
    mode = _pending_vault_edit["mode"]
    _pending_vault_edit = None

    try:
        amount = int(re.sub(r"[^\d]", "", message.text))
    except ValueError:
        await message.reply_text("الرجاء إدخال رقم صحيح فقط.")
        return True

    if mode == "set":
        saved = set_vault_balance(vault_name, amount)
    elif mode == "add":
        saved = adjust_vault_balance(vault_name, amount)
    else:  # sub
        saved = adjust_vault_balance(vault_name, -amount)

    back_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton(BTN_BACK, callback_data="stats_back_to_main")]])
    if saved:
        text = format_vault_balances()
    else:
        text = "⚠️ فشل تحديث الرصيد بـ Google Sheet — تحقق من الاتصال يدوياً."

    await message.reply_text(text, reply_markup=back_keyboard)
    return True


async def on_owner_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    يعالج رسائل نصية عادية (مو Business) جاية منك بمحادثتك المباشرة
    مع البوت — إدخال مبلغ يدوي أثناء تسجيل دفع، تسجيل حساب ChatGPT
    خاص، إدخال مصروف يدوي، تاريخ يدوي بالإحصائيات، تعديل رصيد خزنة
    يدوياً، أزرار لوحة المفاتيح الثابتة، أو فلو إضافة حساب تفاعلي.
    """
    if await handle_manual_amount_entry(update, context):
        return
    if await handle_expense_manual_entry(update, context):
        return
    if await handle_stats_manual_period_entry(update, context):
        return
    if await handle_vault_edit_manual_entry(update, context):
        return
    if await handle_chatgpt_account_reply(update, context):
        return
    if await handle_reply_keyboard_button(update, context):
        return
    await handle_add_account_flow(update, context)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر /start — يرسل لوحة المفاتيح الثابتة (مصروف/دخل/إضافة حساب) بمحادثتك مع البوت."""
    if update.effective_user is None or update.effective_user.id != OWNER_USER_ID:
        return
    await update.message.reply_text("جاهز. استخدم الأزرار بالأسفل:", reply_markup=MAIN_REPLY_KEYBOARD)


async def cmd_income_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر /دخل — يعرض تقرير الدخل (اليوم/الأسبوع/الشهر) من Google Sheet."""
    if update.effective_user is None or update.effective_user.id != OWNER_USER_ID:
        return
    report = calculate_income_report()
    await update.message.reply_text(report)


async def on_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعالج كل الرسائل الجاية عن طريق Telegram Business (محادثتك الشخصية)."""
    bm = update.business_message
    if not bm:
        return

    sender_id = bm.from_user.id if bm.from_user else None
    is_from_owner = sender_id == OWNER_USER_ID

    # صورة دفع جاية من الزبون (مو منك) — نحولها لمحادثتك الخاصة مع
    # البوت مع أزرار تأكيد/إلغاء عشان تبدأ تسجيل عملية الدفع
    if bm.photo and not is_from_owner:
        await handle_incoming_payment_photo(update, context, bm)
        return

    if not bm.text:
        return

    chat_id = bm.chat.id
    text = bm.text

    # اسم الزبون واسم المستخدم (لو موجود) — نستخدمهن بالتنبيه للأونر
    customer_name = bm.chat.full_name or bm.chat.first_name or "غير معروف"
    customer_username = bm.chat.username

    # 1) اذا الرسالة منك انت (owner) — تحقق اذا هي أمر ربط/اضافة/accept
    if is_from_owner:
        handled = await handle_owner_command(update, context, chat_id, text, bm=bm)
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

    # تحديثات business_message — رسائل الزبائن (نص وصور) عن طريق
    # Telegram Business، وهي أساس عمل البوت
    app.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, on_business_message))

    # أزرار تسجيل الدفع — تشتغل بمحادثتك الخاصة مع البوت نفسه
    app.add_handler(CallbackQueryHandler(handle_payment_callback, pattern=r"^pay_"))

    # أزرار تسجيل المصروف — تشتغل بمحادثتك الخاصة مع البوت نفسه
    app.add_handler(CallbackQueryHandler(handle_expense_callback, pattern=r"^exp_"))

    # أزرار الإحصائيات — تشتغل بمحادثتك الخاصة مع البوت نفسه
    app.add_handler(CallbackQueryHandler(handle_stats_callback, pattern=r"^stats_"))

    # أمر /start — يرسل لوحة المفاتيح الثابتة (مصروف/دخل/إضافة حساب)
    app.add_handler(CommandHandler("start", cmd_start))

    # أمر /income لعرض تقرير الدخل — بمحادثتك الخاصة مع البوت
    # (تيليجرام يشترط أوامر بحروف إنكليزية بس، ما يقبل حروف عربية بأسماء الأوامر)
    app.add_handler(CommandHandler("income", cmd_income_report))

    # رسائل نصية عادية منك بمحادثتك الخاصة مع البوت — تستخدم حالياً
    # بس لالتقاط إدخال مبلغ يدوي أثناء تسجيل دفع (رد على رسالة الصورة)
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & filters.User(OWNER_USER_ID),
            on_owner_private_message,
        )
    )

    app.run_polling(
        allowed_updates=["business_message", "business_connection", "edited_business_message", "callback_query", "message"]
    )


if __name__ == "__main__":
    main()
