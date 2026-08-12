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
import uuid
import base64
import asyncio
import logging
import json
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
NOTIFICATIONS_GROUP_ID = int(os.environ.get("NOTIFICATIONS_GROUP_ID", "-1003771659131"))  # قروب سجل الردود

# أرقام الفروع (Topics) داخل قروب الإشعارات — كل فرع مخصص لنوع إشعار
TOPIC_NOTIFICATIONS = 6   # الردود العامة، الشكاوى، مشاكل الكود
TOPIC_PAYMENTS = 8        # إشعار كل عملية دفع/إضافة منتج
TOPIC_EXPENSES = 10       # إشعار كل عملية مصروف
TOPIC_INTERACTIVE = 12    # محجوز لاحقاً — تفاعل مباشر مع البوت
TOPIC_CHATGPT_ACCOUNTS = 33  # إضافة حساب مشترك جديد + ربط زبون بحساب
TOPIC_DEBTS = int(os.environ.get("TOPIC_DEBTS", "0"))  # تسجيل دين جديد + تسديد دين — لازم تحدد رقمه الحقيقي
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
GROQ_API_KEYS = [
    k.strip() for k in os.environ.get("GROQ_API_KEYS", os.environ.get("GROQ_API_KEY", "")).split(",") if k.strip()
]
if not GROQ_API_KEYS:
    raise RuntimeError("لازم تحدد GROQ_API_KEYS (مفاتيح مفصولة بفاصلة) أو GROQ_API_KEY بمتغيرات البيئة")
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
# إدارة مفاتيح Groq المتعددة — نحتفظ بمؤشر لآخر مفتاح ناجح، ونستمر
# نستخدمه لحد ما يفشل هو نفسه (مو نرجع للأول كل مرة). لو فشل، ننتقل
# للمفتاح الجاي بالقائمة تلقائياً، ونحدث المؤشر.
# ------------------------------------------------------------------
_current_groq_key_index = 0


async def call_groq_api(payload: dict, timeout: float = 20.0) -> dict | None:
    """
    نقطة استدعاء موحدة لكل طلبات Groq API (نص، صور، صوت لاحقاً) —
    تدير تعدد المفاتيح تلقائياً: تبدأ من آخر مفتاح ناجح، ولو فشل
    (خطأ شبكة أو رد غير ناجح) تجرب باقي المفاتيح بالترتيب حتى تنجح
    وحدة أو تنتهي القائمة. يرجع الـ JSON response كامل، أو None لو
    فشلت كل المفاتيح.
    """
    global _current_groq_key_index

    num_keys = len(GROQ_API_KEYS)
    for attempt in range(num_keys):
        key_index = (_current_groq_key_index + attempt) % num_keys
        api_key = GROQ_API_KEYS[key_index]
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            resp.raise_for_status()
            _current_groq_key_index = key_index  # هذا المفتاح نجح، نثبته للمرة الجاية
            return resp.json()
        except Exception:
            logger.exception(f"Groq API call failed with key index {key_index} — trying next key if available")
            continue

    logger.error("All Groq API keys failed for this request")
    return None


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


_debts_sheet = None

# أعمدة صفحة الديون بالترتيب
DEBT_COL_DATE = 1
DEBT_COL_CHAT_ID = 2
DEBT_COL_CUSTOMER = 3
DEBT_COL_PRODUCT = 4
DEBT_COL_AMOUNT = 5  # المبلغ المتبقي من الدين (يتحدث لأقل عند تسديد جزئي)
DEBT_COL_STATUS = 6  # "غير مدفوع" أو "مدفوع"
DEBT_COL_PAID_DATE = 7  # يتحدث بتاريخ آخر تسديد (جزئي أو نهائي)


def get_debts_worksheet():
    """
    يرجع كائن صفحة (Tab) الديون، وينشئها تلقائياً لو مو موجودة —
    (التاريخ، Chat ID، الزبون، المنتج، المبلغ المتبقي، الحالة، تاريخ آخر تسديد).
    يرجع None لو فشل الاتصال.
    """
    global _debts_sheet
    if _debts_sheet is not None:
        return _debts_sheet
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
            _debts_sheet = spreadsheet.worksheet(DEBTS_WORKSHEET_NAME)
        except gspread.exceptions.WorksheetNotFound:
            _debts_sheet = spreadsheet.add_worksheet(
                title=DEBTS_WORKSHEET_NAME, rows=1000, cols=7
            )
            _debts_sheet.append_row(
                ["التاريخ", "Chat ID", "الزبون", "المنتج", "المبلغ المتبقي", "الحالة", "تاريخ آخر تسديد"],
                value_input_option="USER_ENTERED",
            )
            logger.info(f"Created new debts worksheet: {DEBTS_WORKSHEET_NAME}")
        return _debts_sheet
    except Exception:
        logger.exception("Failed to connect to debts worksheet")
        return None


def append_debt_row(chat_id: int, customer_line: str, product: str, amount: int) -> bool:
    """يضيف سطر دين جديد (غير مدفوع، المبلغ المتبقي = المبلغ الكامل). يرجع True لو نجح الحفظ."""
    sheet = get_debts_worksheet()
    if sheet is None:
        return False

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    row = [date_str, str(chat_id), customer_line, product, amount, "غير مدفوع", ""]

    try:
        sheet.append_row(row, value_input_option="USER_ENTERED")
        return True
    except Exception:
        logger.exception("Failed to append debt row to Google Sheet")
        return False


def find_unpaid_debt(chat_id: int, product: str) -> tuple[int, int] | None:
    """
    يدور عن أول دين غير مدفوع لنفس الزبون ونفس المنتج بصفحة الديون.
    يرجع (رقم الصف 1-indexed لـ gspread، المبلغ المتبقي الحالي) لو لقى، أو None.
    """
    sheet = get_debts_worksheet()
    if sheet is None:
        return None

    try:
        rows = sheet.get_all_values()
    except Exception:
        logger.exception("Failed to read debts rows while searching for unpaid debt")
        return None

    chat_id_str = str(chat_id)
    for i in range(1, len(rows)):  # نتجاوز صف العناوين
        row = rows[i]
        if len(row) < DEBT_COL_STATUS:
            continue
        row_chat_id = row[DEBT_COL_CHAT_ID - 1].strip()
        row_product = row[DEBT_COL_PRODUCT - 1].strip()
        row_status = row[DEBT_COL_STATUS - 1].strip()
        if row_chat_id == chat_id_str and row_product == product and row_status == "غير مدفوع":
            try:
                remaining = int(float(row[DEBT_COL_AMOUNT - 1]))
            except (ValueError, IndexError):
                remaining = 0
            return i + 1, remaining

    return None


def process_debt_repayment(row_number: int, remaining_before: int, paid_amount: int) -> tuple[bool, int]:
    """
    يعالج تسديد دين (كامل أو جزئي). يطرح paid_amount من remaining_before:
    - لو الناتج <= 0: الدين يقفل بالكامل (المبلغ المتبقي يصير 0، الحالة "مدفوع")
    - لو الناتج > 0: تسديد جزئي (المبلغ المتبقي يتحدث للفرق، الحالة تضل "غير مدفوع")
    يرجع (نجح الحفظ أو لا، المبلغ المتبقي الجديد بعد التسديد).
    """
    sheet = get_debts_worksheet()
    if sheet is None:
        return False, remaining_before

    new_remaining = max(0, remaining_before - paid_amount)
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    try:
        sheet.update_cell(row_number, DEBT_COL_AMOUNT, new_remaining)
        sheet.update_cell(row_number, DEBT_COL_PAID_DATE, date_str)
        if new_remaining <= 0:
            sheet.update_cell(row_number, DEBT_COL_STATUS, "مدفوع")
        return True, new_remaining
    except Exception:
        logger.exception("Failed to process debt repayment")
        return False, remaining_before


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
#   "vault": str | None,
#   "reason": str | None,
#   "photo_file_id": str | None,  # لو الفلو بدأ من صورة مصروف مؤكدة
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
# حالة مؤقتة لفلو تسجيل دين جديد — بس عملية وحدة بنفس الوقت.
# {
#   "message_id": int,
#   "step": "chat_id" | "product" | "amount",
#   "chat_id": int | None,
#   "customer_line": str | None,
#   "product": str | None,
#   "amount": int,
#   "awaiting_manual_amount": bool,
# }
# ------------------------------------------------------------------
_pending_debt: dict | None = None

# ------------------------------------------------------------------
# حالة جلسة تلقين نشطة (الطريقة 3 — تلقين يدوي مباشر) — بس جلسة وحدة
# بنفس الوقت. تفعل بزر BTN_TEACH، وتضل نشطة لين تضغط "إنهاء الجلسة".
# {
#   "session_id": str,                # معرف فريد لهذي الجلسة (يربط كل أمثلتها مع بعض)
#   "customer_messages": list[str],   # رسائل الزبون المجمّعة (قبل الرد)
#   "customer_chat_id": int | None,   # لو عرفناه من forward_origin
#   "awaiting_reply": bool,           # صار ضغط "هذا ردي"، ننتظر نص الرد الجاي
# }
# ------------------------------------------------------------------
_teaching_session: dict | None = None

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
# حد أقصى صورة وحدة لكل 6 ساعات لصور المصروف اللي ترسلها أنت (owner)
# — عداد مستقل تماماً عن عداد صور الزبائن أعلاه، بمفتاح ثابت واحد
# (OWNER_USER_ID) بما إنه مصدر وحيد (أنت بس).
# ------------------------------------------------------------------
_owner_photo_timestamps: list[datetime] = []


def is_owner_photo_within_rate_limit() -> bool:
    """نفس منطق is_photo_within_rate_limit، بس لصور المصروف اللي ترسلها أنت."""
    global _owner_photo_timestamps
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=PHOTO_RATE_LIMIT_WINDOW_HOURS)

    timestamps = [t for t in _owner_photo_timestamps if t > cutoff]

    if len(timestamps) >= PHOTO_RATE_LIMIT_MAX:
        _owner_photo_timestamps = timestamps
        return False

    timestamps.append(now)
    _owner_photo_timestamps = timestamps
    return True


# ------------------------------------------------------------------
# حالة مؤقتة لصورة مصروف محتملة بانتظار تأكيدك (✅ مصروف / ❌ إلغاء) —
# مفتاح وحيد (عملية وحدة بنفس الوقت). المفتاح message_id لرسالة الصورة
# المحولة بمحادثتك مع البوت.
# {"file_id": str}
# ------------------------------------------------------------------
_pending_expense_photo_confirm: dict[int, dict] = {}


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

# اسم صفحة (Tab) الديون بنفس الشيت — تُنشأ تلقائياً لو مو موجودة
DEBTS_WORKSHEET_NAME = "ديون"

# نصوص أزرار لوحة المفاتيح الثابتة (Reply Keyboard) تحت صندوق الكتابة
BTN_EXPENSE = "💸 تسجيل مصروف"
BTN_INCOME = "📊 تقرير الدخل"
BTN_ADD_ACCOUNT = "➕ إضافة حساب"
BTN_STATS = "📈 إحصائيات"
BTN_DEBT = "💳 تسجيل دين"
BTN_TEACH = "📝 بدء تلقين جديد"
BTN_CATALOG = "🗂️ المنتجات والباقات"
BTN_PAYMENT_METHODS = "💳 طرق الدفع"
BTN_CHATGPT_VAULT = "🤖 خزينة حسابات ChatGPT"
BTN_BACK = "◀️ رجوع"
PAYMENT_METHOD_INPUT_TIMEOUT = timedelta(minutes=10)

MAIN_REPLY_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton(BTN_EXPENSE), KeyboardButton(BTN_INCOME)],
        [KeyboardButton(BTN_ADD_ACCOUNT), KeyboardButton(BTN_STATS)],
        [KeyboardButton(BTN_DEBT), KeyboardButton(BTN_TEACH)],
        [KeyboardButton(BTN_CATALOG), KeyboardButton(BTN_PAYMENT_METHODS)],
        [KeyboardButton(BTN_CHATGPT_VAULT)],
    ],
    resize_keyboard=True,
)


def get_catalog_products() -> list[dict]:
    try:
        return (supabase.table("catalog_products").select("id, name, aliases, is_active").order("name").execute().data or [])
    except Exception:
        logger.exception("Failed to fetch catalog products")
        return []


def get_catalog_product(product_id: str) -> dict | None:
    try:
        result = supabase.table("catalog_products").select("id, name, aliases, is_active").eq("id", product_id).execute()
        return result.data[0] if result.data else None
    except Exception:
        logger.exception("Failed to fetch catalog product")
        return None


def get_catalog_plans(product_id: str) -> list[dict]:
    try:
        return (
            supabase.table("catalog_plans")
            .select("id, name, price, duration, description, is_active")
            .eq("product_id", product_id).order("price").execute().data or []
        )
    except Exception:
        logger.exception("Failed to fetch catalog plans")
        return []


def build_catalog_main_keyboard(products: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        f"{'✅' if product['is_active'] else '⏸️'} {product['name']}",
        callback_data=f"catalog_product_{product['id']}",
    )] for product in products]
    rows.append([InlineKeyboardButton("➕ إضافة منتج", callback_data="catalog_add_product")])
    return InlineKeyboardMarkup(rows)


def build_catalog_product_keyboard(product: dict, plans: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        f"{'✅' if plan['is_active'] else '⏸️'} {plan['name']} — {plan['price']}",
        callback_data=f"catalog_plan_{plan['id']}",
    )] for plan in plans]
    rows.append([InlineKeyboardButton("➕ إضافة باقة", callback_data=f"catalog_add_plan_{product['id']}")])
    rows.append([
        InlineKeyboardButton("✏️ تعديل الاسم والكلمات", callback_data=f"catalog_edit_product_{product['id']}"),
        InlineKeyboardButton("⏸️ إيقاف المنتج" if product["is_active"] else "✅ تفعيل المنتج", callback_data=f"catalog_toggle_product_{product['id']}"),
    ])
    rows.append([InlineKeyboardButton("🗑️ حذف المنتج", callback_data=f"catalog_pdelc_{product['id']}")])
    rows.append([InlineKeyboardButton(BTN_BACK, callback_data="catalog_main")])
    return InlineKeyboardMarkup(rows)


def format_catalog_product(product: dict, plans: list[dict]) -> str:
    status = "مفعّل" if product["is_active"] else "متوقف"
    lines = [f"{product['name']} — {status}", "", "الباقات:"]
    aliases = product.get("aliases") or []
    if aliases:
        lines.insert(2, f"كلمات التعرف: {', '.join(aliases)}")
    if not plans:
        lines.append("ماكو باقات بعد.")
    for plan in plans:
        plan_status = "مفعلة" if plan["is_active"] else "متوقفة"
        details = f" — {plan['duration']}" if plan.get("duration") else ""
        lines.append(f"• {plan['name']}: {plan['price']}{details} ({plan_status})")
    return "\n".join(lines)


async def show_catalog_main(message) -> None:
    products = get_catalog_products()
    await message.reply_text(
        "🗂️ المنتجات والباقات\nاختَر منتجًا لإدارة باقاته وأسعاره.",
        reply_markup=build_catalog_main_keyboard(products),
    )


async def handle_catalog_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.from_user.id != OWNER_USER_ID:
        return
    data = query.data
    await query.answer()

    if data == "catalog_main":
        products = get_catalog_products()
        await query.message.reply_text(
            "🗂️ المنتجات والباقات\nاختَر منتجًا لإدارة باقاته وأسعاره.",
            reply_markup=build_catalog_main_keyboard(products),
        )
        return

    if data == "catalog_add_product":
        context.user_data["pending_catalog_input"] = {"message_id": query.message.message_id, "step": "product_data"}
        await query.edit_message_text(
            "اكتب المنتج بهذا الشكل كـرد على هذي الرسالة:\nاسم المنتج | كلمات يتعرف عليها البوت مفصولة بفاصلة\n\nمثال: ChatGPT | تشات، جات، chat",
            reply_markup=None,
        )
        return

    if data.startswith("catalog_edit_product_"):
        product_id = data[len("catalog_edit_product_"):]
        if get_catalog_product(product_id) is None:
            await query.edit_message_text("⚠️ المنتج ما عاد موجود.")
            return
        context.user_data["pending_catalog_input"] = {
            "message_id": query.message.message_id,
            "step": "product_data",
            "product_id": product_id,
        }
        await query.edit_message_text(
            "اكتب الاسم والكلمات الجديدة بهذا الشكل كـرد على هذي الرسالة:\nاسم المنتج | كلمات يتعرف عليها البوت مفصولة بفاصلة\n\nمثال: ChatGPT | تشات، جات، chat",
            reply_markup=None,
        )
        return

    if data.startswith("catalog_toggle_product_"):
        product_id = data[len("catalog_toggle_product_"):]
        product = get_catalog_product(product_id)
        if product is None:
            await query.edit_message_text("⚠️ المنتج ما عاد موجود.")
            return
        try:
            supabase.table("catalog_products").update({"is_active": not product["is_active"]}).eq("id", product_id).execute()
            product = get_catalog_product(product_id)
            if product:
                plans = get_catalog_plans(product_id)
                await query.edit_message_text(format_catalog_product(product, plans), reply_markup=build_catalog_product_keyboard(product, plans))
        except Exception:
            logger.exception("Failed to toggle catalog product")
            await query.edit_message_text("⚠️ فشل تحديث حالة المنتج.")
        return

    if data.startswith("catalog_pdelc_"):
        product_id = data[len("catalog_pdelc_"):]
        product = get_catalog_product(product_id)
        if product is None:
            await query.edit_message_text("⚠️ المنتج ما عاد موجود.")
            return
        await query.edit_message_text(
            f"تأكيد حذف المنتج «{product['name']}» وكل باقاته؟\nهذا الإجراء ما يرجع.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑️ نعم، احذف", callback_data=f"catalog_pdel_{product_id}")],
                [InlineKeyboardButton("◀️ إلغاء", callback_data=f"catalog_product_{product_id}")],
            ]),
        )
        return

    if data.startswith("catalog_pdel_"):
        product_id = data[len("catalog_pdel_"):]
        try:
            supabase.table("catalog_products").delete().eq("id", product_id).execute()
            products = get_catalog_products()
            await query.edit_message_text("✅ تم حذف المنتج وباقاته.", reply_markup=build_catalog_main_keyboard(products))
        except Exception:
            logger.exception("Failed to delete catalog product")
            await query.edit_message_text("⚠️ فشل حذف المنتج.")
        return

    if data.startswith("catalog_product_"):
        try:
            product = get_catalog_product(data[len("catalog_product_"):])
            if product is None:
                await query.message.reply_text("⚠️ المنتج ما عاد موجود.")
                return
            plans = get_catalog_plans(product["id"])
            # نرسل رسالة جديدة: هذا أضمن من تعديل رسالة زر قديمة، خصوصاً
            # إذا ضغطت عليها بعد إعادة نشر البوت.
            await query.message.reply_text(
                format_catalog_product(product, plans),
                reply_markup=build_catalog_product_keyboard(product, plans),
            )
        except Exception:
            logger.exception("Failed to open catalog product")
            await query.message.reply_text("⚠️ ما انفتح المنتج. جرّب مرة ثانية بعد دقيقة.")
        return

    if data.startswith("catalog_add_plan_"):
        product_id = data[len("catalog_add_plan_"):]
        context.user_data["pending_catalog_input"] = {"message_id": query.message.message_id, "step": "plan_data", "product_id": product_id}
        await query.edit_message_text("اكتب الباقة بهذا الشكل كـرد على هذي الرسالة:\nاسم الباقة | السعر | المدة | وصف اختياري", reply_markup=None)
        return

    if data.startswith("catalog_plan_"):
        plan_id = data[len("catalog_plan_"):]
        try:
            result = supabase.table("catalog_plans").select("id, product_id, name, price, duration, description, is_active").eq("id", plan_id).execute()
            plan = result.data[0] if result.data else None
        except Exception:
            logger.exception("Failed to fetch catalog plan")
            plan = None
        if plan is None:
            await query.edit_message_text("⚠️ الباقة ما عادت موجودة.")
            return
        text = f"{plan['name']}\nالسعر: {plan['price']}\nالمدة: {plan.get('duration') or '—'}\nالوصف: {plan.get('description') or '—'}\nالحالة: {'مفعلة' if plan['is_active'] else 'متوقفة'}"
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✏️ تعديل السعر", callback_data=f"catalog_price_{plan_id}")],
            [InlineKeyboardButton("✏️ تعديل تفاصيل الباقة", callback_data=f"catalog_edit_plan_{plan_id}")],
            [InlineKeyboardButton("⏸️ إيقاف" if plan["is_active"] else "✅ تفعيل", callback_data=f"catalog_toggle_{plan_id}")],
            [InlineKeyboardButton("🗑️ حذف الباقة", callback_data=f"catalog_xdelc_{plan_id}")],
            [InlineKeyboardButton(BTN_BACK, callback_data=f"catalog_product_{plan['product_id']}")],
        ])
        await query.edit_message_text(text, reply_markup=keyboard)
        return

    if data.startswith("catalog_price_"):
        context.user_data["pending_catalog_input"] = {
            "message_id": query.message.message_id,
            "step": "plan_price",
            "plan_id": data[len("catalog_price_"):],
        }
        await query.edit_message_text("اكتب السعر الجديد رقم فقط كـرد على هذي الرسالة:", reply_markup=None)
        return

    if data.startswith("catalog_edit_plan_"):
        plan_id = data[len("catalog_edit_plan_"):]
        try:
            result = supabase.table("catalog_plans").select("id, product_id").eq("id", plan_id).execute()
            plan = result.data[0] if result.data else None
        except Exception:
            logger.exception("Failed to fetch catalog plan for editing")
            plan = None
        if plan is None:
            await query.edit_message_text("⚠️ الباقة ما عادت موجودة.")
            return
        context.user_data["pending_catalog_input"] = {
            "message_id": query.message.message_id,
            "step": "plan_data",
            "product_id": plan["product_id"],
            "plan_id": plan_id,
        }
        await query.edit_message_text("اكتب البيانات الجديدة بهذا الشكل كـرد على هذي الرسالة:\nاسم الباقة | السعر | المدة | وصف اختياري", reply_markup=None)
        return

    if data.startswith("catalog_xdelc_"):
        plan_id = data[len("catalog_xdelc_"):]
        try:
            result = supabase.table("catalog_plans").select("id, product_id, name").eq("id", plan_id).execute()
            plan = result.data[0] if result.data else None
        except Exception:
            logger.exception("Failed to fetch catalog plan for deletion")
            plan = None
        if plan is None:
            await query.edit_message_text("⚠️ الباقة ما عادت موجودة.")
            return
        await query.edit_message_text(
            f"تأكيد حذف باقة «{plan['name']}»؟\nهذا الإجراء ما يرجع.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🗑️ نعم، احذف", callback_data=f"catalog_xdel_{plan_id}")],
                [InlineKeyboardButton("◀️ إلغاء", callback_data=f"catalog_plan_{plan_id}")],
            ]),
        )
        return

    if data.startswith("catalog_xdel_"):
        plan_id = data[len("catalog_xdel_"):]
        try:
            result = supabase.table("catalog_plans").select("id, product_id").eq("id", plan_id).execute()
            plan = result.data[0] if result.data else None
            if plan is None:
                await query.edit_message_text("⚠️ الباقة ما عادت موجودة.")
                return
            supabase.table("catalog_plans").delete().eq("id", plan_id).execute()
            product = get_catalog_product(plan["product_id"])
            if product:
                plans = get_catalog_plans(product["id"])
                await query.edit_message_text("✅ تم حذف الباقة.\n\n" + format_catalog_product(product, plans), reply_markup=build_catalog_product_keyboard(product, plans))
        except Exception:
            logger.exception("Failed to delete catalog plan")
            await query.edit_message_text("⚠️ فشل حذف الباقة.")
        return

    if data.startswith("catalog_toggle_"):
        plan_id = data[len("catalog_toggle_"):]
        try:
            result = supabase.table("catalog_plans").select("id, product_id, is_active").eq("id", plan_id).execute()
            plan = result.data[0] if result.data else None
            if plan:
                supabase.table("catalog_plans").update({"is_active": not plan["is_active"]}).eq("id", plan_id).execute()
                product = get_catalog_product(plan["product_id"])
                if product:
                    plans = get_catalog_plans(product["id"])
                    await query.edit_message_text(format_catalog_product(product, plans), reply_markup=build_catalog_product_keyboard(product, plans))
        except Exception:
            logger.exception("Failed to toggle catalog plan")
            await query.edit_message_text("⚠️ فشل تحديث حالة الباقة.")


async def handle_catalog_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    message = update.message
    state = context.user_data.get("pending_catalog_input")
    if not message or not message.text or state is None or not message.reply_to_message:
        return False
    if message.reply_to_message.message_id != state["message_id"]:
        return False

    text = message.text.strip()
    try:
        if state["step"] == "product_data":
            name, _, aliases_text = text.partition("|")
            name = name.strip()
            aliases = [item.strip() for item in aliases_text.replace("،", ",").split(",") if item.strip()]
            if not name:
                await message.reply_text("اكتب اسم المنتج أولاً.")
                return True
            payload = {"name": name, "aliases": aliases}
            if state.get("product_id"):
                supabase.table("catalog_products").update(payload).eq("id", state["product_id"]).execute()
                await message.reply_text("✅ تم تعديل المنتج والكلمات.")
            else:
                supabase.table("catalog_products").insert(payload).execute()
                await message.reply_text("✅ تم إضافة المنتج.")
        elif state["step"] == "plan_data":
            parts = [part.strip() for part in text.split("|", 3)]
            if len(parts) < 2 or not parts[0] or not parts[1].isdigit():
                await message.reply_text("الصيغة غير صحيحة. استخدم: اسم الباقة | السعر | المدة | وصف")
                return True
            payload = {"product_id": state["product_id"], "name": parts[0], "price": int(parts[1]), "duration": parts[2] if len(parts) > 2 else None, "description": parts[3] if len(parts) > 3 else None}
            if state.get("plan_id"):
                supabase.table("catalog_plans").update(payload).eq("id", state["plan_id"]).execute()
                await message.reply_text("✅ تم تعديل الباقة.")
            else:
                supabase.table("catalog_plans").insert(payload).execute()
                await message.reply_text("✅ تم إضافة الباقة.")
        elif state["step"] == "plan_price":
            if not text.isdigit():
                await message.reply_text("اكتب السعر رقم فقط.")
                return True
            supabase.table("catalog_plans").update({"price": int(text)}).eq("id", state["plan_id"]).execute()
            await message.reply_text("✅ تم تعديل السعر.")
        else:
            return False
    except Exception:
        logger.exception("Catalog input failed")
        await message.reply_text("⚠️ فشل الحفظ. تأكد أن جداول الكتالوج موجودة في Supabase.")
        return True

    context.user_data.pop("pending_catalog_input", None)
    # نرجع شاشة الكاتالوك مباشرة حتى تقدر تفتح المنتج وتضيف باقاته
    # بدون ما تحتاج تضغط زر لوحة المفاتيح مرة ثانية.
    await show_catalog_main(message)
    return True


def get_payment_methods() -> list[dict]:
    try:
        return supabase.table("payment_methods").select("id, name, instructions, is_active").order("display_order").order("name").execute().data or []
    except Exception:
        logger.exception("Failed to fetch payment methods")
        return []


def payment_keyboard(methods: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"{'✅' if row['is_active'] else '⏸️'} {row['name']}", callback_data=f"pm_{row['id']}")] for row in methods]
    rows.append([InlineKeyboardButton("➕ إضافة طريقة دفع", callback_data="pm_add")])
    return InlineKeyboardMarkup(rows)


async def show_payment_methods(message) -> None:
    await message.reply_text("💳 طرق الدفع\nاختَر طريقة لتعديلها، أو أضف طريقة جديدة.", reply_markup=payment_keyboard(get_payment_methods()))


def get_chatgpt_shared_vault_summary() -> str:
    """يعرض إحصاء الخزينة بدون كشف الإيميلات أو كلمات المرور."""
    try:
        accounts = supabase.table("chatgpt_shared_accounts").select("id, capacity, is_active").execute().data or []
        active = [account for account in accounts if account.get("is_active")]
        assignments = supabase.table("chatgpt_account_assignments").select("account_id").eq("status", "active").execute().data or []
        used_by_account: dict[str, int] = {}
        for assignment in assignments:
            account_id = assignment["account_id"]
            used_by_account[account_id] = used_by_account.get(account_id, 0) + 1
        seats = sum(max(0, int(account["capacity"]) - used_by_account.get(account["id"], 0)) for account in active)
        return f"🤖 خزينة ChatGPT المشتركة\nالحسابات المفعلة: {len(active)}\nالمقاعد المتاحة: {seats}"
    except Exception:
        logger.exception("Failed to load ChatGPT shared vault summary")
        return "⚠️ ما كدرت أقرأ خزينة حسابات ChatGPT."


async def show_chatgpt_shared_vault(message) -> None:
    await message.reply_text(
        get_chatgpt_shared_vault_summary(),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ إضافة حساب مشترك", callback_data="vault_add_shared")]]),
    )


async def handle_chatgpt_vault_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.from_user.id != OWNER_USER_ID:
        return
    await query.answer()
    if query.data == "vault_add_shared":
        context.user_data["pending_shared_account"] = {"step": "email"}
        await query.edit_message_text("أرسل إيميل حساب ChatGPT المشترك:")


async def handle_shared_account_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    message = update.message
    state = context.user_data.get("pending_shared_account")
    if not message or not message.text or not state:
        return False
    text = message.text.strip()
    step = state.get("step")
    if step == "email":
        if "@" not in text:
            await message.reply_text("اكتب إيميل صحيح.")
            return True
        state["email"] = text
        state["step"] = "password"
        await message.reply_text("تمام. أرسل كلمة المرور:")
        return True
    if step == "password":
        if len(text) < 6:
            await message.reply_text("كلمة المرور قصيرة جداً، أرسلها كاملة.")
            return True
        state["password"] = text
        state["step"] = "totp_secret"
        await message.reply_text("أرسل مفتاح TOTP الطويل للحساب (Base32، مو الكود ذي 6 أرقام):")
        return True
    if step == "totp_secret":
        secret = re.sub(r"\s+", "", text).upper()
        try:
            pyotp.TOTP(secret).now()
        except Exception:
            await message.reply_text("المفتاح مو صحيح. أرسل مفتاح TOTP الطويل مرة ثانية.")
            return True
        try:
            supabase.table("chatgpt_shared_accounts").insert({
                "email": state["email"], "password": state["password"],
                "totp_secret": secret, "capacity": 9,
            }).execute()
        except Exception:
            logger.exception("Failed to add shared ChatGPT account")
            await message.reply_text("⚠️ ما انحفظ الحساب. تأكد أن الإيميل مو مضاف سابقاً.")
            return True
        context.user_data.pop("pending_shared_account", None)
        await message.reply_text("✅ تم حفظ الحساب المشترك. سعته 9 أشخاص.")
        await show_chatgpt_shared_vault(message)
        return True
    return False


async def handle_payment_method_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.from_user.id != OWNER_USER_ID:
        return
    data = query.data
    await query.answer()
    if data == "pm_back":
        context.user_data.pop("pending_payment_input", None)
        await show_payment_methods(query.message)
        return
    if data == "pm_add":
        context.user_data["pending_payment_input"] = {
            "message_id": query.message.message_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        await query.edit_message_text(
            "اكتب طريقة الدفع بهذا الشكل:\nاسم الطريقة | التفاصيل التي تصل للزبون\n\nمثال: زين كاش | الرقم: 07xxxxxxxxx باسم أحمد",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ إلغاء", callback_data="pm_back")]]),
        )
        return
    if data.startswith("pm_edit_"):
        context.user_data["pending_payment_input"] = {
            "message_id": query.message.message_id,
            "id": data[len("pm_edit_"):],
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        await query.edit_message_text(
            "اكتب الاسم والتفاصيل الجديدة بهذا الشكل:\nاسم الطريقة | التفاصيل التي تصل للزبون",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ إلغاء", callback_data="pm_back")]]),
        )
        return
    if data.startswith("pm_toggle_"):
        method_id = data[len("pm_toggle_"):]
        rows = supabase.table("payment_methods").select("is_active").eq("id", method_id).execute().data or []
        if rows:
            supabase.table("payment_methods").update({"is_active": not rows[0]["is_active"]}).eq("id", method_id).execute()
        await show_payment_methods(query.message)
        return
    if data.startswith("pm_delc_"):
        method_id = data[len("pm_delc_"):]
        await query.edit_message_text("تأكيد حذف طريقة الدفع؟", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🗑️ نعم، احذف", callback_data=f"pm_del_{method_id}")], [InlineKeyboardButton("◀️ إلغاء", callback_data=f"pm_{method_id}")]]))
        return
    if data.startswith("pm_del_"):
        supabase.table("payment_methods").delete().eq("id", data[len("pm_del_"):]).execute()
        await show_payment_methods(query.message)
        return
    if data.startswith("pm_"):
        method_id = data[len("pm_"):]
        rows = supabase.table("payment_methods").select("id, name, instructions, is_active").eq("id", method_id).execute().data or []
        if not rows:
            await query.message.reply_text("⚠️ طريقة الدفع ما عادت موجودة.")
            return
        method = rows[0]
        await query.message.reply_text(f"{method['name']}\n\n{method['instructions']}\n\nالحالة: {'مفعلة' if method['is_active'] else 'متوقفة'}", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✏️ تعديل", callback_data=f"pm_edit_{method_id}")], [InlineKeyboardButton("⏸️ إيقاف" if method['is_active'] else "✅ تفعيل", callback_data=f"pm_toggle_{method_id}")], [InlineKeyboardButton("🗑️ حذف", callback_data=f"pm_delc_{method_id}")], [InlineKeyboardButton(BTN_BACK, callback_data="pm_back")]]))


async def handle_payment_method_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    message = update.message
    state = context.user_data.get("pending_payment_input")
    if not message or not message.text or not state:
        return False
    started_at = state.get("started_at")
    if started_at:
        try:
            if datetime.now(timezone.utc) - datetime.fromisoformat(started_at) > PAYMENT_METHOD_INPUT_TIMEOUT:
                context.user_data.pop("pending_payment_input", None)
                return False
        except (TypeError, ValueError):
            context.user_data.pop("pending_payment_input", None)
            return False
    name, separator, instructions = message.text.strip().partition("|")
    if not separator or not name.strip() or not instructions.strip():
        await message.reply_text("الصيغة غير صحيحة. استخدم: اسم الطريقة | التفاصيل")
        return True
    payload = {"name": name.strip(), "instructions": instructions.strip()}
    try:
        if state.get("id"):
            supabase.table("payment_methods").update(payload).eq("id", state["id"]).execute()
        else:
            supabase.table("payment_methods").insert(payload).execute()
    except Exception:
        logger.exception("Failed to save payment method")
        await message.reply_text("⚠️ فشل حفظ طريقة الدفع.")
        return True
    context.user_data.pop("pending_payment_input", None)
    await message.reply_text("✅ تم الحفظ.")
    await show_payment_methods(message)
    return True


def build_confirm_cancel_keyboard() -> InlineKeyboardMarkup:
    """أول زرين يطلعون تحت صورة دفع جديدة توصل من زبون."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ تأكيد", callback_data="pay_confirm"),
            InlineKeyboardButton("❌ إلغاء", callback_data="pay_cancel"),
        ]
    ])


def build_expense_photo_confirm_keyboard() -> InlineKeyboardMarkup:
    """زرين يطلعون تحت صورة أرسلتها أنت (owner) — هل هذي إثبات مصروف؟"""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ مصروف", callback_data="expphoto_yes"),
            InlineKeyboardButton("❌ ليس مصروف", callback_data="expphoto_no"),
        ]
    ])


def build_product_keyboard() -> InlineKeyboardMarkup:
    """أول شاشة بعد التأكيد — اختيار سريع لأكثر منتج مبيع + بقية المنتجات + إدخال حر."""
    quick_pick = PAYMENT_PRODUCTS[0]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(quick_pick, callback_data=f"pay_product_{quick_pick}")],
        [InlineKeyboardButton("بقية المنتجات ▾", callback_data="pay_product_list")],
        [InlineKeyboardButton("✏️ إدخال حر", callback_data="pay_product_manual")],
    ])


def build_product_list_keyboard() -> InlineKeyboardMarkup:
    """قائمة كل المنتجات ما عدا الاختيار السريع + إدخال حر + زر رجوع."""
    rows = [
        [InlineKeyboardButton(p, callback_data=f"pay_product_{p}")]
        for p in PAYMENT_PRODUCTS[1:]
    ]
    rows.append([InlineKeyboardButton("✏️ إدخال حر", callback_data="pay_product_manual")])
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


def build_summary_keyboard(has_product: bool, has_payment: bool, show_debt_repayment: bool = False) -> InlineKeyboardMarkup:
    """
    الشاشة الرئيسية بعد ما فيه منتج أو طريقة دفع واحدة محفوظة على الأقل —
    زر المرحلة الحالية (منتج أو طريقة دفع جديدة) + زر التثبيت النهائي دايماً.
    show_debt_repayment=True يضيف زر "تسديد دين" جنب زر التثبيت، لما
    الزبون عليه دين غير مدفوع يطابق المنتج المختار حالياً.
    """
    rows = []
    if not has_product:
        rows.append([InlineKeyboardButton(PAYMENT_PRODUCTS[0], callback_data=f"pay_product_{PAYMENT_PRODUCTS[0]}")])
        rows.append([InlineKeyboardButton("بقية المنتجات ▾", callback_data="pay_product_list")])
        rows.append([InlineKeyboardButton("✏️ إدخال حر", callback_data="pay_product_manual")])
    else:
        rows.append([InlineKeyboardButton(PAYMENT_METHODS[0], callback_data=f"pay_method_{PAYMENT_METHODS[0]}")])
        rows.append([InlineKeyboardButton("بقية الطرق ▾", callback_data="pay_method_list")])
    rows.append([InlineKeyboardButton("✅ تثبيت العملية", callback_data="pay_finalize")])
    if show_debt_repayment:
        rows.append([InlineKeyboardButton("💳 تسديد دين", callback_data="pay_debt_repay")])
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


def build_debt_product_keyboard() -> InlineKeyboardMarkup:
    """شاشة اختيار منتج الدين — منتج سريع + بقية المنتجات + إدخال حر."""
    quick_pick = PAYMENT_PRODUCTS[0]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(quick_pick, callback_data=f"debt_product_{quick_pick}")],
        [InlineKeyboardButton("بقية المنتجات ▾", callback_data="debt_product_list")],
        [InlineKeyboardButton("✏️ إدخال حر", callback_data="debt_product_manual")],
    ])


def build_debt_product_list_keyboard() -> InlineKeyboardMarkup:
    """قائمة كل المنتجات ما عدا الاختيار السريع + زر رجوع، لمنتج الدين."""
    rows = [
        [InlineKeyboardButton(p, callback_data=f"debt_product_{p}")]
        for p in PAYMENT_PRODUCTS[1:]
    ]
    rows.append([InlineKeyboardButton(BTN_BACK, callback_data="debt_back_to_product")])
    return InlineKeyboardMarkup(rows)


def build_debt_amount_keyboard() -> InlineKeyboardMarkup:
    """شاشة تحديد مبلغ الدين — نفس أزرار مبلغ الدفع، بس callback_data مختلف."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"+{PAYMENT_AMOUNT_STEP_SMALL}", callback_data="debt_amount_add_small"),
            InlineKeyboardButton(f"+{PAYMENT_AMOUNT_STEP_LARGE}", callback_data="debt_amount_add_large"),
        ],
        [InlineKeyboardButton("✏️ إدخال يدوي", callback_data="debt_amount_manual")],
        [InlineKeyboardButton("✅ تثبيت الدين", callback_data="debt_amount_commit")],
    ])


def format_debt_summary(debt: dict) -> str:
    """يبني نص الملخص المعروض فوق أزرار تسجيل الدين."""
    lines = ["تسجيل دين"]
    customer_line = debt.get("customer_line")
    product = debt.get("product")
    amount = debt.get("amount", 0)
    lines.append(f"الزبون: {customer_line if customer_line else '— لم يُحدد بعد —'}")
    lines.append(f"المنتج: {product if product else '— لم يُحدد بعد —'}")
    lines.append(f"المبلغ: {amount if amount else '— لم يُحدد بعد —'}")
    return "\n".join(lines)


async def send_debt_notification(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """يبعث إشعار دين (تسجيل جديد أو تسديد) لفرع 'ديون' بقروب الإشعارات."""
    try:
        await context.bot.send_message(
            chat_id=NOTIFICATIONS_GROUP_ID, message_thread_id=TOPIC_DEBTS, text=text
        )
    except Exception:
        logger.exception("Failed to send debt notification to topic")


def build_teaching_keyboard(has_customer_message: bool) -> InlineKeyboardMarkup:
    """
    شاشة جلسة التلقين — زر "هذا ردي" يطلع بس لو فيه رسالة زبون واحدة
    على الأقل متجمعة، بالإضافة لزر "إنهاء الجلسة" دايماً.
    """
    rows = []
    if has_customer_message:
        rows.append([InlineKeyboardButton("✅ هذا ردي", callback_data="teach_mark_reply")])
    rows.append([InlineKeyboardButton("⏹ إنهاء الجلسة", callback_data="teach_end_session")])
    return InlineKeyboardMarkup(rows)


def format_teaching_status(session: dict) -> str:
    """يبني نص حالة جلسة التلقين الحالية."""
    count = len(session["customer_messages"])
    if session.get("awaiting_reply"):
        return (
            f"جلسة تلقين نشطة\n"
            f"رسائل الزبون المجمّعة: {count}\n\n"
            f"الحين حوّل أو اكتب ردك — راح يُحفظ كمثال ويبدأ مثال جديد تلقائياً."
        )
    return (
        f"جلسة تلقين نشطة\n"
        f"رسائل الزبون المجمّعة: {count}\n\n"
        f"حوّل رسائل الزبون (وحدة أو أكثر)، وبعدها اضغط ✅ هذا ردي."
    )


def save_style_example(session_id: str, customer_chat_id, customer_message: str, owner_reply: str, source: str) -> bool:
    """يحفظ مثال تعلم جديد بجدول style_examples، مربوط بجلسة تلقين معينة. يرجع True لو نجح."""
    try:
        supabase.table("style_examples").insert({
            "session_id": session_id,
            "customer_chat_id": customer_chat_id,
            "customer_message": customer_message,
            "owner_reply": owner_reply,
            "source": source,
        }).execute()
        return True
    except Exception:
        logger.exception("Failed to save style example")
        return False


ARCHIVE_IMPORT_PAGE_SIZE = 500
ARCHIVE_STYLE_CONTEXT_MAX_MESSAGES = 10
ARCHIVE_STYLE_CONTEXT_MAX_CHARS = 4_000


def fetch_all_table_rows(table_name: str, columns: str) -> list[dict]:
    """يجلب الصفوف على دفعات حتى لا يقتصر الاستيراد على أول 1000 سجل."""
    rows: list[dict] = []
    start = 0
    while True:
        try:
            res = (
                supabase.table(table_name)
                .select(columns)
                .order("created_at")
                .range(start, start + ARCHIVE_IMPORT_PAGE_SIZE - 1)
                .execute()
            )
        except Exception:
            logger.exception("Failed to fetch %s while importing archive", table_name)
            raise

        batch = res.data or []
        rows.extend(batch)
        if len(batch) < ARCHIVE_IMPORT_PAGE_SIZE:
            return rows
        start += ARCHIVE_IMPORT_PAGE_SIZE


def format_archive_style_context(messages: list[str]) -> str:
    """يبقي آخر جزء مفيد من الحوار ضمن حد آمن لطول المثال والـprompt."""
    selected = messages[-ARCHIVE_STYLE_CONTEXT_MAX_MESSAGES:]
    context = "\n".join(selected)
    if len(context) <= ARCHIVE_STYLE_CONTEXT_MAX_CHARS:
        return context
    return "…\n" + context[-ARCHIVE_STYLE_CONTEXT_MAX_CHARS:]


def build_style_examples_from_archive() -> tuple[list[dict], int]:
    """
    يحوّل الأرشيف إلى أمثلة تلقين نظيفة:
    - يحافظ على تسلسل الحوار داخل نفس conversation_session_id (زبون/بوت/أونر).
    - ينشئ مثالاً عند أي رد نصي للمتجر: من الأونر أو من البوت.
    - يحتفظ بمصدر الرد، حتى نعرف هل المثال من أسلوب الأونر أو رد FAQ للبوت.
    - يستبعد الصور الفارغة فقط لأنها لا تحتوي نصاً صالحاً كمثال.
    - لا يعيد إدخال زوج (رسالة الزبون، رد الأونر) الموجود مسبقاً.

    يرجع (الأمثلة الجديدة، عدد الأزواج التي تم تخطيها لأنها مكررة).
    """
    archive_rows = fetch_all_table_rows(
        "conversation_archive",
        "conversation_session_id, customer_chat_id, sender_type, message_text, created_at",
    )
    existing_examples = fetch_all_table_rows(
        "style_examples", "customer_message, owner_reply"
    )
    existing_pairs = {
        ((row.get("customer_message") or "").strip(), (row.get("owner_reply") or "").strip())
        for row in existing_examples
    }

    histories: dict[str, list[str]] = {}
    sessions_with_customer: set[str] = set()
    candidates: list[dict] = []
    skipped_duplicates = 0

    for row in archive_rows:
        session_id = row.get("conversation_session_id")
        sender_type = row.get("sender_type")
        message_text = (row.get("message_text") or "").strip()
        if not session_id or not message_text:
            continue

        if sender_type == "customer":
            histories.setdefault(session_id, []).append(f"الزبون: {message_text}")
            sessions_with_customer.add(session_id)
            continue

        if sender_type not in {"owner", "bot"}:
            continue

        # لا قيمة لمثال ما لم يبدأ الحوار برسالة زبون واحدة على الأقل.
        if session_id not in sessions_with_customer:
            continue

        conversation_context = format_archive_style_context(histories.get(session_id, []))
        if not conversation_context:
            continue

        pair = (conversation_context, message_text)
        if pair in existing_pairs:
            skipped_duplicates += 1
        else:
            candidates.append({
                "customer_chat_id": row.get("customer_chat_id"),
                "customer_message": conversation_context,
                "owner_reply": message_text,
                "source": f"conversation_archive:{sender_type}",
            })
            existing_pairs.add(pair)

        responder = "البوت" if sender_type == "bot" else "صاحب المتجر"
        histories.setdefault(session_id, []).append(f"{responder}: {message_text}")

    return candidates, skipped_duplicates


def import_archive_as_style_examples() -> tuple[int, int, int]:
    """يحفظ أمثلة الأرشيف الجديدة دفعات، ويرجع (المضاف، المكرر، الإجمالي المرشح)."""
    candidates, skipped_duplicates = build_style_examples_from_archive()
    if not candidates:
        return 0, skipped_duplicates, 0

    import_session_id = str(uuid.uuid4())
    records = [
        {
            "session_id": import_session_id,
            "customer_chat_id": item["customer_chat_id"],
            "customer_message": item["customer_message"],
            "owner_reply": item["owner_reply"],
            "source": item["source"],
        }
        for item in candidates
    ]
    for start in range(0, len(records), ARCHIVE_IMPORT_PAGE_SIZE):
        supabase.table("style_examples").insert(
            records[start : start + ARCHIVE_IMPORT_PAGE_SIZE]
        ).execute()
    return len(records), skipped_duplicates, len(candidates)


CONVERSATION_SESSION_GAP_MINUTES = int(os.environ.get("CONVERSATION_SESSION_GAP_MINUTES", "30"))
# هذه المرحلة للمراقبة فقط: تقسيم المحادثات وتسجيل سياقها، وليس اتخاذ قرار
# أو إرسال رد جديد للزبون على أساس هذا السياق.


def get_context_event_types(
    sender_type: str,
    message_text: str | None,
    image_description: str | None,
) -> list[str]:
    """يلتقط مراحل عامة من النص كي نفهم تسلسل المحادثة لاحقاً.

    النتائج علامات داخلية فقط، ولا تدخل بمنطق ردود FAQ أو الدفع حالياً.
    """
    text = f"{message_text or ''}\n{image_description or ''}".lower()
    if not text:
        return ["image_received"] if image_description else []

    events: list[str] = []
    def has(*terms: str) -> bool:
        return any(term in text for term in terms)

    if has("السلام عليكم", "هلا", "اهلا", "أهلا"):
        events.append("greeting")
    if has("chatgpt", "chat gpt", "تشات", "جات"):
        events.append("chatgpt_interest")
    if has("باقة", "باقات", "سعر", "اسعار", "أسعار", "شكد", "مشترك", "خاص", "شهر"):
        events.append("plan_or_price_discussion")
    if has("طرق الدفع", "طريقة الدفع", "اريد ادفع", "أريد أدفع", "ماستر", "زين كاش", "رصيد"):
        events.append("payment_method_discussion")
    if has("حولت", "حوّلت", "تم التحويل", "دفعت"):
        events.append("payment_claimed")
    if has("صورة التحويل", "وصل التحويل", "سكرين", "سكرين شوت", "لقطة شاشة") or image_description:
        events.append("payment_or_support_image")
    if has("كود", "رمز", "code"):
        events.append("code_request_or_help")
    if has("ما صار", "ما قبل", "ماقبل", "ما يشتغل", "مايشتغل", "مشكلة", "خطأ"):
        events.append("support_issue")
    if has("شكراً", "شكرا", "تعبتكم", "عاشت ايدكم", "عاشت إيدكم"):
        events.append("thanks")
    if sender_type in ("owner", "bot") and has("الايميل", "الإيميل", "password", "كلمة المرور", "بيانات الحساب"):
        events.append("account_or_registration_guidance")
    return events


def stage_from_context_events(events: list[str], previous_stage: str = "observing") -> str:
    """يحدث المرحلة للعرض والتحليل فقط؛ لا يحرّك أي إجراء تلقائي."""
    if "thanks" in events:
        return "completed"
    if "support_issue" in events:
        return "support_needed"
    if "code_request_or_help" in events:
        return "code_or_registration"
    if "payment_or_support_image" in events:
        return "payment_or_support_review"
    if "payment_claimed" in events:
        return "payment_claimed"
    if "payment_method_discussion" in events:
        return "payment_discussion"
    if "plan_or_price_discussion" in events:
        return "plan_discussion"
    if "chatgpt_interest" in events:
        return "product_interest"
    if "greeting" in events:
        return "greeting"
    return previous_stage


def get_or_create_conversation_session_id(customer_chat_id: int) -> str:
    """
    يرجع conversation_session_id المناسب لرسالة جديدة من هذا الزبون —
    لو آخر رسالة مؤرشفة لنفس الزبون أقل من مدة السكوت المضبوطة، يرجع نفس رقمها
    (نفس السياق يستمر). لو أقدم من هذا الفاصل أو ماكو رسائل سابقة،
    يولّد رقم جديد (سياق محادثة جديد).
    """
    try:
        res = (
            supabase.table("conversation_archive")
            .select("conversation_session_id, created_at")
            .eq("customer_chat_id", customer_chat_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if res.data:
            last_created_at = datetime.fromisoformat(res.data[0]["created_at"].replace("Z", "+00:00"))
            if datetime.now(timezone.utc) - last_created_at < timedelta(minutes=CONVERSATION_SESSION_GAP_MINUTES):
                return res.data[0]["conversation_session_id"]
            supabase.table("conversation_sessions").update({
                "status": "closed",
                "closed_at": last_created_at.isoformat(),
            }).eq("id", res.data[0]["conversation_session_id"]).eq("status", "open").execute()
    except Exception:
        logger.exception("Failed to check last archived message for session continuity")

    return str(uuid.uuid4())


def archive_message(
    customer_chat_id: int,
    customer_name: str | None,
    customer_username: str | None,
    sender_type: str,
    message_text: str | None = None,
    image_description: str | None = None,
) -> None:
    """
    يؤرشف رسالة وحدة (من زبون، أونر، أو بوت) بجدول conversation_archive
    — يحدد تلقائياً السياق (conversation_session_id) المناسب حسب منطق
    فترة السكوت. لا يوقف تنفيذ الرسالة الأساسي لو فشل.
    """
    session_id = get_or_create_conversation_session_id(customer_chat_id)
    now = datetime.now(timezone.utc).isoformat()
    source = "interactive" if customer_chat_id < 0 else "live"
    try:
        # ننشئ صف الجلسة قبل الأرشفة حتى كل حدث يتبع سياقاً معروفاً.
        session_rows = (
            supabase.table("conversation_sessions")
            .select("message_count, latest_stage")
            .eq("id", session_id)
            .limit(1)
            .execute().data or []
        )
        if not session_rows:
            supabase.table("conversation_sessions").insert({
                "id": session_id,
                "customer_chat_id": customer_chat_id,
                "customer_name": customer_name,
                "customer_username": customer_username,
                "source": source,
                "started_at": now,
                "last_activity_at": now,
            }).execute()
            session_rows = [{"message_count": 0, "latest_stage": "observing"}]

        archive_result = supabase.table("conversation_archive").insert({
            "conversation_session_id": session_id,
            "customer_chat_id": customer_chat_id,
            "customer_name": customer_name,
            "customer_username": customer_username,
            "sender_type": sender_type,
            "message_text": message_text,
            "image_description": image_description,
        }).execute()

        events = get_context_event_types(sender_type, message_text, image_description)
        archive_rows = archive_result.data or []
        archive_message_id = archive_rows[0].get("id") if archive_rows else None
        if archive_message_id and events:
            supabase.table("conversation_context_events").upsert([
                {
                    "conversation_session_id": session_id,
                    "archive_message_id": archive_message_id,
                    "sender_type": sender_type,
                    "event_type": event_type,
                }
                for event_type in events
            ], on_conflict="archive_message_id,event_type").execute()

        current = session_rows[0]
        supabase.table("conversation_sessions").update({
            "customer_name": customer_name,
            "customer_username": customer_username,
            "last_activity_at": now,
            "message_count": int(current.get("message_count") or 0) + 1,
            "latest_stage": stage_from_context_events(events, current.get("latest_stage") or "observing"),
        }).eq("id", session_id).execute()
    except Exception:
        logger.exception("Failed to archive conversation message")


CONVERSATION_SUMMARY_GAP_MINUTES = 30  # فترة صمت تستدعي دمج الرسائل الجديدة بالملخص التراكمي

SUMMARY_MERGE_PROMPT = (
    "انت تلخص محادثة بين بوت ومستخدم بشكل تراكمي. عندك ملخص سابق "
    "(ممكن يكون فاضي لو أول مرة) + رسائل جديدة صارت بعده. ادمجهم بملخص "
    "واحد جديد شامل، بحدود 150 كلمة، باللهجة العراقية، يحافظ على أهم "
    "المعلومات والسياق من الملخص القديم والرسائل الجديدة مع بعض. "
    "رد بالملخص النهائي بس، بدون مقدمات."
)


def get_conversation_summary(customer_chat_id: int) -> dict | None:
    """يرجع صف الملخص التراكمي الحالي لزبون معين، أو None لو ماكو."""
    try:
        res = (
            supabase.table("conversation_summaries")
            .select("summary_text, last_message_at")
            .eq("customer_chat_id", customer_chat_id)
            .execute()
        )
        return res.data[0] if res.data else None
    except Exception:
        logger.exception("Failed to fetch conversation summary")
        return None


async def maybe_update_conversation_summary(customer_chat_id: int) -> None:
    """
    يفحص هل مرت 30 دقيقة بدون رسائل جديدة منذ آخر تحديث للملخص —
    لو إي، يجمع الرسائل الجديدة من conversation_archive منذ آخر
    ملخص، يدمجها مع الملخص القديم (لو موجود) بطلب AI واحد، ويحدث
    conversation_summaries. يُستدعى "بالكسل" (lazy) وقت وصول رسالة
    جديدة، بدل مؤقت خارجي.
    """
    existing = get_conversation_summary(customer_chat_id)
    now = datetime.now(timezone.utc)

    if existing and existing.get("last_message_at"):
        last_at = datetime.fromisoformat(existing["last_message_at"].replace("Z", "+00:00"))
        if now - last_at < timedelta(minutes=CONVERSATION_SUMMARY_GAP_MINUTES):
            return  # لسا داخل نفس الجلسة، ما نلخص

    # نجمع الرسائل الجديدة منذ آخر تحديث ملخص (أو كل التاريخ لو أول مرة)
    try:
        query = (
            supabase.table("conversation_archive")
            .select("sender_type, message_text, created_at")
            .eq("customer_chat_id", customer_chat_id)
            .order("created_at")
        )
        if existing and existing.get("last_message_at"):
            query = query.gt("created_at", existing["last_message_at"])
        res = query.execute()
        new_messages = res.data or []
    except Exception:
        logger.exception("Failed to fetch new messages for summary update")
        return

    if not new_messages:
        return

    transcript = "\n".join(
        f"{'المستخدم' if m['sender_type'] == 'customer' else 'البوت'}: {m['message_text']}"
        for m in new_messages if m.get("message_text")
    )
    old_summary = existing["summary_text"] if existing and existing.get("summary_text") else "لا يوجد ملخص سابق."

    try:
        data = await call_groq_api({
            "model": CHATGPT_CONTEXT_MODEL,
            "temperature": 0.3,
            "max_completion_tokens": 500,
            "reasoning_effort": "low",
            "messages": [
                {"role": "system", "content": SUMMARY_MERGE_PROMPT},
                {"role": "user", "content": f"الملخص السابق:\n{old_summary}\n\nالرسائل الجديدة:\n{transcript}"},
            ],
        }, timeout=15.0)
        if data is None:
            return
        new_summary = data["choices"][0]["message"]["content"].strip()
    except Exception:
        logger.exception("Failed to merge conversation summary")
        return

    latest_message_at = new_messages[-1]["created_at"]
    try:
        supabase.table("conversation_summaries").upsert({
            "customer_chat_id": customer_chat_id,
            "summary_text": new_summary,
            "last_message_at": latest_message_at,
        }).execute()
    except Exception:
        logger.exception("Failed to save updated conversation summary")


async def send_expense_notification(context: ContextTypes.DEFAULT_TYPE, expense: dict) -> None:
    """يبعث إشعار مصروف مكتمل لفرع 'مصروفات' بقروب الإشعارات."""
    vault_line = expense.get("vault") or "بدون خزنة محددة"
    notification = (
        f"💸 مصروف جديد\n"
        f"السبب: {expense.get('reason')}\n"
        f"المبلغ: {expense.get('amount')}\n"
        f"الخزنة: {vault_line}"
    )
    try:
        await context.bot.send_message(
            chat_id=NOTIFICATIONS_GROUP_ID, message_thread_id=TOPIC_EXPENSES, text=notification
        )
    except Exception:
        logger.exception("Failed to send expense notification to topic")


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


def get_all_accounts_with_secrets() -> list[dict]:
    """يرجع كل الحسابات (id, link_code, label, secret) — تستخدم لتوليد أكواد TOTP بفرع التفاعل."""
    try:
        res = supabase.table("totp_accounts").select("id, link_code, label, secret").execute()
        return res.data or []
    except Exception:
        logger.exception("Failed to fetch accounts with secrets")
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

# موديل مخصص لقراءة ووصف الصور (Vision) — الموديل الوحيد المدعوم
# رسمياً لقراءة الصور بـ Groq حالياً (يدعم صور + نص بنفس الوقت)
IMAGE_VISION_MODEL = "qwen/qwen3.6-27b"

# موديل مخصص لتحويل الصوت لنص (Speech-to-Text) — أسرع نسخة من Whisper
# بحفاظ على دقة عالية، مناسب لرسائل صوتية قصيرة/متوسطة
WHISPER_MODEL = "whisper-large-v3-turbo"

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
        data = await call_groq_api({
            "model": CHATGPT_CONTEXT_MODEL,
            "temperature": 0,
            "max_completion_tokens": 500,
            "reasoning_effort": "low",
            "messages": [
                {"role": "system", "content": CHATGPT_CONTEXT_PROMPT},
                {"role": "user", "content": text},
            ],
        }, timeout=15.0)
        if data is None:
            return "شراء"
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


IMAGE_DESCRIPTION_PROMPT = (
    "صف هذي الصورة بالتفصيل باللغة العربية — وضح شنو محتواها الأساسي، "
    "وأهم المعلومات الظاهرة فيها (نصوص، أرقام، مبالغ، أسماء تطبيقات، "
    "أو أي تفاصيل مهمة تساعد فهم سياق الرسالة). رد بوصف واحد شامل "
    "ومباشر، بدون مقدمات."
)

PAYMENT_PROOF_ANALYSIS_PROMPT = (
    "أنت تدقق صورة إثبات دفع لمتجر عراقي. حلل ما يظهر فعلياً فقط، ولا تفترض "
    "معلومات غير موجودة. أخرج JSON فقط بهذه الحقول: "
    "is_payment_receipt (true/false), amount (رقم أو null), payment_method (نص أو null), "
    "recipient_match (true/false/null), recency (recent/old/not_visible), confidence (0-100), "
    "reason (نص عربي قصير).\n"
    "تعتبر الصورة وصل دفع فقط إذا ظهر تطبيق/خدمة دفع مع مبلغ وعملية تحويل أو نجاح. "
    "صورة التسجيل، الموقع، المحادثة، أو أي شاشة غير دفع = false. لا تعتبر الوقت "
    "حديثاً إلا إذا ظهر تاريخ/وقت يتوافق مع اليوم أو آخر ساعتين بحسب وقت بغداد المعطى."
)


async def describe_image(file_bytes: bytes) -> str | None:
    """
    يستخدم Groq Vision (موديل IMAGE_VISION_MODEL) لوصف محتوى صورة.
    يرجع الوصف النصي، أو None لو فشل الاتصال أو التحليل.

    ملاحظة: qwen3.6 موديل "hybrid thinking" — نوقف وضع التفكير الداخلي
    (reasoning_effort="none") و نخفي أي تفكير لو صار رغم ذلك
    (reasoning_format="hidden")، عشان توفير التوكنز — وصف صورة مهمة
    بسيطة ما تحتاج تفكير عميق مثل رياضيات أو برمجة.
    """
    try:
        base64_image = base64.b64encode(file_bytes).decode("utf-8")
        data = await call_groq_api({
            "model": IMAGE_VISION_MODEL,
            "temperature": 0.7,
            "top_p": 0.8,
            "max_completion_tokens": 500,
            "reasoning_effort": "none",
            "reasoning_format": "hidden",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": IMAGE_DESCRIPTION_PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"},
                        },
                    ],
                }
            ],
        }, timeout=20.0)
        if data is None:
            return None
        return data["choices"][0]["message"]["content"].strip()
    except Exception:
        logger.exception("Groq image description failed")
        return None


def get_expected_payment_for_interactive_session(customer_chat_id: int) -> tuple[int | None, str]:
    """يجلب المبلغ المتوقع من الباقة التي اختارها الزبون في جلسة الاختبار."""
    state = get_interactive_sale_state(customer_chat_id)
    plan_id = state.get("selected_plan_id")
    if not plan_id:
        return None, ""
    try:
        rows = supabase.table("catalog_plans").select("name, price").eq("id", plan_id).limit(1).execute().data or []
        if rows:
            return int(rows[0]["price"]), rows[0]["name"]
    except Exception:
        logger.exception("Failed to get expected payment amount")
    return None, ""


async def analyze_payment_proof(file_bytes: bytes, customer_chat_id: int) -> dict | None:
    """يفحص وصل الدفع بالصورة مقابل الباقة وطرق الدفع المعتمدة."""
    expected_amount, plan_name = get_expected_payment_for_interactive_session(customer_chat_id)
    active_methods = [method for method in get_payment_methods() if method.get("is_active")]
    destinations = "\n".join(f"- {method['name']}: {method['instructions']}" for method in active_methods)
    prompt = (
        f"{PAYMENT_PROOF_ANALYSIS_PROMPT}\n\n"
        f"وقت بغداد الحالي: {datetime.now(timezone(timedelta(hours=3))).strftime('%Y-%m-%d %H:%M')}\n"
        f"الباقة المختارة: {plan_name or 'غير معروفة'}\n"
        f"المبلغ المطلوب: {expected_amount if expected_amount is not None else 'غير معروف'}\n"
        f"وجهات الدفع المعتمدة (طابق الاسم/الرقم الظاهر فقط):\n{destinations or 'لا توجد وجهات مضبوطة'}"
    )
    try:
        base64_image = base64.b64encode(file_bytes).decode("utf-8")
        data = await call_groq_api({
            "model": IMAGE_VISION_MODEL,
            "temperature": 0,
            "max_completion_tokens": 350,
            "reasoning_effort": "none",
            "reasoning_format": "hidden",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}},
            ]}],
        }, timeout=25.0)
        if data is None:
            return None
        raw = data["choices"][0]["message"]["content"].strip()
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return None
        result = json.loads(match.group(0))
        amount = result.get("amount")
        try:
            amount = int(str(amount).replace(",", "")) if amount is not None else None
        except (TypeError, ValueError):
            amount = None
        result["amount"] = amount
        result["expected_amount"] = expected_amount
        result["plan_name"] = plan_name
        approved = (
            result.get("is_payment_receipt") is True
            and expected_amount is not None
            and amount == expected_amount
            and result.get("recipient_match") is True
            and result.get("recency") == "recent"
            and int(result.get("confidence") or 0) >= 85
        )
        if approved:
            result["decision"] = "approved"
        elif result.get("is_payment_receipt") is False:
            result["decision"] = "rejected"
        else:
            result["decision"] = "needs_review"
        return result
    except Exception:
        logger.exception("Payment proof analysis failed")
        return None


def save_interactive_payment_proof(customer_chat_id: int, analysis: dict) -> None:
    """يسجل نتيجة الفحص بدون الاحتفاظ بالصورة أو بيانات دخول."""
    state = get_interactive_sale_state(customer_chat_id)
    if not state.get("id"):
        return
    try:
        supabase.table("payment_proof_reviews").insert({
            "conversation_session_id": state["id"],
            "customer_chat_id": customer_chat_id,
            "selected_plan_id": state.get("selected_plan_id"),
            "expected_amount": analysis.get("expected_amount"),
            "detected_amount": analysis.get("amount"),
            "decision": analysis.get("decision", "needs_review"),
            "analysis": analysis,
        }).execute()
    except Exception:
        logger.exception("Failed to save payment proof review")


async def notify_interactive_payment_review(context: ContextTypes.DEFAULT_TYPE, analysis: dict) -> None:
    """ينبه المالك بنتيجة التدقيق، حتى الحالات المقبولة تظل ظاهرة له."""
    decision_label = {"approved": "✅ قبول مبدئي", "needs_review": "⚠️ مراجعة", "rejected": "❌ مرفوض"}.get(
        analysis.get("decision"), "⚠️ مراجعة"
    )
    message = (
        f"🧪 فحص وصل دفع — فرع التفاعل\n{decision_label}\n"
        f"الباقة: {analysis.get('plan_name') or 'غير معروفة'}\n"
        f"المطلوب: {analysis.get('expected_amount') or 'غير معروف'}\n"
        f"المقروء: {analysis.get('amount') or 'غير واضح'}\n"
        f"السبب: {analysis.get('reason') or 'لا يوجد'}"
    )
    try:
        await context.bot.send_message(chat_id=NOTIFICATIONS_GROUP_ID, message_thread_id=TOPIC_NOTIFICATIONS, text=message)
    except Exception:
        logger.exception("Failed to notify owner about interactive payment proof")


async def transcribe_audio(file_bytes: bytes, filename: str = "audio.ogg") -> str | None:
    """
    يستخدم Groq Whisper (موديل WHISPER_MODEL) لتحويل رسالة صوتية لنص.
    يدير تعدد المفاتيح بنفسه (endpoint مختلف عن call_groq_api — طلب
    multipart/form-data، مو JSON). يرجع النص المكتوب، أو None لو فشلت
    كل المفاتيح.
    """
    global _current_groq_key_index

    num_keys = len(GROQ_API_KEYS)
    for attempt in range(num_keys):
        key_index = (_current_groq_key_index + attempt) % num_keys
        api_key = GROQ_API_KEYS[key_index]
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    files={"file": (filename, file_bytes)},
                    data={"model": WHISPER_MODEL, "language": "ar"},
                )
            resp.raise_for_status()
            _current_groq_key_index = key_index
            return resp.json().get("text", "").strip()
        except Exception:
            logger.exception(f"Groq Whisper call failed with key index {key_index} — trying next key if available")
            continue

    logger.error("All Groq API keys failed for audio transcription")
    return None


TEST_ACTION_SELECTOR_PROMPT = (
    "انت مصنف سياق فقط لمتجر عراقي يبيع اشتراكات رقمية. لا تكتب ردًا للزبون "
    "ولا تذكر معلومة أو سعر أو رقم. مهمتك اختيار action_key واحد فقط من القائمة "
    "المسموح بها. اعتمد على تسلسل المحادثة والأمثلة المؤرشفة لفهم المعنى. رد "
    "بـ action_key فقط بلا شرح. اختَر static_faq للسؤال المباشر الذي يغطيه رد "
    "ثابت. اختَر chatgpt_plans عندما يطلب جات/ChatGPT أو يسأل عن باقاته، "
    "حتى لو سأل فقط \"شنو الباقات\" وكان الطلب السابق داخل السياق عن جات. "
    "اختَر payment_methods عندما يطلب الدفع أو طرق الدفع. اختَر "
    "request_plan_choice إذا يريد الشراء ولم يحدد باقة. اختَر "
    "request_payment_proof فقط إذا قال حوّلت/دفعت ولم يرسل صورة. اختَر "
    "payment_under_review عند إرسال صورة تحويل. اختَر request_support_screenshot "
    "لمشكلة تحتاج صورة، وhandoff للحالة الحساسة أو غير المؤكدة، وclarify إذا "
    "الكلام غير واضح."
)


STYLE_EXAMPLE_CANDIDATE_LIMIT = 500


def normalize_style_text(text: str) -> set[str]:
    """تطبيع خفيف للهجة العربية حتى نختار أمثلة قريبة من رسالة الاختبار."""
    normalized = text.lower()
    normalized = re.sub(r"[أإآٱ]", "ا", normalized)
    normalized = normalized.replace("ى", "ي").replace("ة", "ه")
    normalized = re.sub(r"[^\w\u0600-\u06ff]+", " ", normalized)
    return {word for word in normalized.split() if len(word) >= 2}


def get_relevant_style_examples(query_text: str, limit: int = 8) -> list[dict]:
    """
    يرجّع أمثلة تلقين قريبة من رسالة الاختبار. هذا بحث معجمي بسيط ومقصود
    لمرحلة التجربة؛ يحافظ على عدد أمثلة صغير وواضح داخل طلب الـAI.
    """
    try:
        res = (
            supabase.table("style_examples")
            .select("customer_message, owner_reply, source, created_at")
            .eq("approval_status", "approved")
            .eq("is_active", True)
            .order("created_at", desc=True)
            .limit(STYLE_EXAMPLE_CANDIDATE_LIMIT)
            .execute()
        )
        examples = res.data or []
    except Exception:
        # جدول التلقين قد لا يكون منشأ بعد في بيئة جديدة؛ لا نوقف التجربة بسببه.
        logger.exception("Failed to fetch style examples for interactive test")
        return []

    query_words = normalize_style_text(query_text)
    if not query_words:
        return examples[:limit]

    ranked_examples = []
    for index, example in enumerate(examples):
        customer_words = normalize_style_text(example.get("customer_message") or "")
        reply_words = normalize_style_text(example.get("owner_reply") or "")
        # نعطي كلام الزبون وزن أعلى لأنه هو الذي يحدد نوع الطلب عادةً.
        score = (2 * len(query_words & customer_words)) + len(query_words & reply_words)
        ranked_examples.append((score, -index, example))

    ranked_examples.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [example for _, _, example in ranked_examples[:limit]]


def format_style_examples(examples: list[dict]) -> str:
    """يحوّل أمثلة التلقين إلى نص واضح للموديل، مع استبعاد الصفوف الناقصة."""
    formatted = []
    for index, example in enumerate(examples, start=1):
        customer_message = (example.get("customer_message") or "").strip()
        owner_reply = (example.get("owner_reply") or "").strip()
        if customer_message and owner_reply:
            source = example.get("source") or ""
            responder = "رد البوت" if source.endswith(":bot") else "رد صاحب المتجر"
            formatted.append(
                f"مثال {index}:\nسياق المحادثة:\n{redact_context_text(customer_message)}\n"
                f"{responder}: {redact_context_text(owner_reply)}"
            )
    return "\n\n".join(formatted)


def redact_context_text(text: str) -> str:
    """يمنع إرسال بيانات دخول أو أرقام حساسة إلى موديل اختيار الإجراء."""
    text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[بيانات مخفية]", text)
    return re.sub(r"\b\d{7,}\b", "[رقم مخفي]", text)


def get_interactive_response_templates() -> dict[str, str]:
    """الردود الثابتة المفعلة لفرع التفاعل فقط."""
    try:
        rows = (
            supabase.table("interactive_response_templates")
            .select("action_key, response_text")
            .eq("is_active", True)
            .execute().data or []
        )
        return {row["action_key"]: row["response_text"] for row in rows}
    except Exception:
        logger.exception("Failed to fetch interactive response templates")
        return {}


def get_recent_interactive_context(customer_chat_id: int, limit: int = 16) -> tuple[list[dict], str | None]:
    """يجلب الرسائل التابعة لجلسة الاختبار الحالية فقط."""
    try:
        latest = (
            supabase.table("conversation_archive")
            .select("conversation_session_id")
            .eq("customer_chat_id", customer_chat_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute().data or []
        )
        if not latest:
            return [], None
        session_id = latest[0]["conversation_session_id"]
        rows = (
            supabase.table("conversation_archive")
            .select("sender_type, message_text")
            .eq("conversation_session_id", session_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute().data or []
        )
        return list(reversed(rows)), session_id
    except Exception:
        logger.exception("Failed to fetch current interactive context")
        return [], None


def is_chatgpt_catalog_context(text: str) -> bool:
    """يتعرف على طلب باقات ChatGPT من الكلمات المباشرة أو من استمرار السياق."""
    normalized = " ".join(normalize_style_text(text))
    direct_terms = {"chatgpt", "chat", "gpt", "جات", "تشات", "شات", "جيبيتي"}
    return any(term in normalized for term in direct_terms)


def is_plan_question(text: str) -> bool:
    normalized = " ".join(normalize_style_text(text))
    return any(term in normalized for term in {"باقه", "باقات", "سعر", "اسعار", "شكد", "خاص", "مشترك", "شهر"})


def get_chatgpt_catalog_product() -> dict | None:
    return next(
        (row for row in get_catalog_products()
         if row.get("is_active") and row.get("name", "").strip().lower() == "chatgpt"),
        None,
    )


def find_selected_chatgpt_plan(text: str) -> tuple[dict, dict] | None:
    """يحوّل اختيار الزبون باللهجة إلى باقة فعلية من الكاتالوج."""
    product = get_chatgpt_catalog_product()
    if not product:
        return None
    words = normalize_style_text(text)
    if not words:
        return None
    plans = [plan for plan in get_catalog_plans(product["id"]) if plan.get("is_active")]
    ranked: list[tuple[int, dict]] = []
    for plan in plans:
        plan_words = normalize_style_text(f"{plan.get('name') or ''} {plan.get('duration') or ''}")
        score = len(words & plan_words)
        # "شهر" وحدها يقصد بها غالباً الباقة ذات الشهر الواحد، وليس شهرين.
        if "شهر" in words and "شهرين" not in words and "شهرين" not in plan_words and "شهر" in plan_words:
            score += 3
        if "شهرين" in words and "شهرين" in plan_words:
            score += 3
        if "خاص" in words and "خاص" in plan_words:
            score += 3
        if "مشترك" in words and "مشترك" in plan_words:
            score += 3
        ranked.append((score, plan))
    ranked.sort(key=lambda item: item[0], reverse=True)
    if ranked and ranked[0][0] >= 2:
        return product, ranked[0][1]
    return None


def get_interactive_sale_state(customer_chat_id: int) -> dict:
    """حالة البيع للجلسة الحالية، حتى نفهم سؤال الخطوة التالية كتكملة."""
    try:
        rows = (
            supabase.table("conversation_sessions")
            .select("id, workflow_state, selected_product_id, selected_plan_id")
            .eq("customer_chat_id", customer_chat_id)
            .eq("status", "open")
            .order("last_activity_at", desc=True)
            .limit(1)
            .execute().data or []
        )
        return rows[0] if rows else {}
    except Exception:
        logger.exception("Failed to get interactive sale state")
        return {}


def set_interactive_sale_state(customer_chat_id: int, workflow_state: str, product_id: str | None = None, plan_id: str | None = None) -> None:
    """يحفظ انتقال الحالة في فرع التفاعل فقط."""
    state = get_interactive_sale_state(customer_chat_id)
    if not state.get("id"):
        return
    payload: dict[str, str | None] = {"workflow_state": workflow_state}
    if product_id is not None:
        payload["selected_product_id"] = product_id
    if plan_id is not None:
        payload["selected_plan_id"] = plan_id
    try:
        supabase.table("conversation_sessions").update(payload).eq("id", state["id"]).execute()
    except Exception:
        logger.exception("Failed to save interactive sale state")


async def choose_test_response_action(customer_chat_id: int, new_message: str) -> str | None:
    """الـAI يختار إجراءً فقط؛ النص النهائي لا يولّده الذكاء الاصطناعي."""
    templates = get_interactive_response_templates()
    allowed_actions = ["static_faq", *templates.keys()]
    recent_messages, session_id = get_recent_interactive_context(customer_chat_id)
    style_examples_text = format_style_examples(get_relevant_style_examples(new_message))
    context_lines = []
    for item in recent_messages:
        text = (item.get("message_text") or "").strip()
        if text:
            speaker = "الزبون" if item.get("sender_type") == "customer" else "المتجر"
            context_lines.append(f"{speaker}: {redact_context_text(text)}")
    context_text = "\n".join(context_lines) or "لا يوجد سياق سابق."
    sale_state = get_interactive_sale_state(customer_chat_id)
    workflow_state = sale_state.get("workflow_state", "observing")

    # الحالات الواضحة لا تحتاج تخمين من الموديل. هذا يمنع الخطأ الظاهر
    # بالتجربة: "رايد جات" يجب أن يعرض الباقات، لا أن يطلب اختيارها.
    normalized = " ".join(normalize_style_text(new_message))
    # التحيات دقيقة وحساسة للأسلوب: لا نتركها للموديل. هلو/هلا ليست
    # سلاماً شرعياً، وبالتالي ردها المعتمد "اهلا وسهلا" فقط.
    greeting_categories = set(keyword_match_categories(new_message))
    if "سلام" in greeting_categories:
        return "static_faq"
    if "ترحيب" in greeting_categories:
        return "static_faq"
    if "شكر" in greeting_categories:
        return "closing"
    if workflow_state == "awaiting_plan_choice":
        selected = find_selected_chatgpt_plan(new_message)
        if selected:
            product, plan = selected
            set_interactive_sale_state(customer_chat_id, "awaiting_payment", product["id"], plan["id"])
            return "payment_methods"
    if any(term in normalized for term in {"حولت", "حولت", "دفعت"}):
        set_interactive_sale_state(customer_chat_id, "awaiting_payment_proof")
        return "request_payment_proof"
    if workflow_state in {"awaiting_payment", "awaiting_payment_proof"} and any(
        term in normalized for term in {"هسه", "اسوي", "شنو", "شلون", "اوكي", "تمام"}
    ):
        return "payment_methods"
    if any(term in normalized for term in {"ادفع", "الدفع", "ماستر", "زين", "رصيد"}):
        return "payment_methods"
    if any(term in normalized for term in {"شكرا", "شكراً", "تعبتكم", "عاشت"}):
        return "closing"
    if is_chatgpt_catalog_context(new_message) or (
        is_plan_question(new_message) and is_chatgpt_catalog_context(context_text)
    ):
        set_interactive_sale_state(customer_chat_id, "awaiting_plan_choice", None, None)
        return "chatgpt_plans"

    messages = [{"role": "system", "content": TEST_ACTION_SELECTOR_PROMPT}]
    if style_examples_text:
        messages.append({"role": "system", "content": f"أمثلة مؤرشفة ومعتمدة لفهم المسار فقط:\n\n{style_examples_text}"})
    messages.append({
        "role": "user",
        "content": (
            f"سياق الجلسة الحالية:\n{context_text}\n\n"
            f"الإجراءات المسموح بها فقط: {', '.join(allowed_actions)}\n"
            "اختر action_key واحدًا فقط."
        ),
    })
    try:
        data = await call_groq_api({
            "model": CHATGPT_CONTEXT_MODEL,
            "temperature": 0,
            "max_completion_tokens": 200,
            "reasoning_effort": "low",
            "messages": messages,
        }, timeout=20.0)
        if data is None:
            return None
        raw_action = data["choices"][0]["message"]["content"].strip().lower()
        for action_key in allowed_actions:
            if raw_action == action_key or action_key in raw_action:
                logger.info("Interactive selector chose %s for session %s", action_key, session_id)
                return action_key
        logger.warning("Interactive selector returned unsupported value: %r", raw_action)
        return "handoff"
    except Exception:
        logger.exception("Failed to choose interactive response action")
        return None


def render_test_response(action_key: str, customer_text: str) -> str:
    """يحوّل الإجراء إلى رد ثابت، بدون صياغة من الذكاء الاصطناعي."""
    if action_key == "static_faq":
        return get_exact_test_faq_reply(customer_text) or "تدلل، وضحلي شنو تريد بالضبط حتى أساعدك."
    if action_key == "payment_methods":
        methods = [method for method in get_payment_methods() if method.get("is_active")]
        if methods:
            details = "\n\n".join(
                f"{method['name']}\n{method['instructions']}" for method in methods
            )
            return f"طرق الدفع\n\n{details}"
        return "تدلل، خليني أتأكد من طرق الدفع وأرجعلك."
    if action_key == "chatgpt_plans":
        product = next(
            (row for row in get_catalog_products()
             if row.get("is_active") and row.get("name", "").strip().lower() == "chatgpt"),
            None,
        )
        if product:
            plans = [plan for plan in get_catalog_plans(product["id"]) if plan.get("is_active")]
            if plans:
                lines = ["بلي موجود هاي الباقات المتوفرة ChatGPT:", ""]
                for plan in plans:
                    lines.append(f"- {plan['name']} {plan['price']}")
                return "\n".join(lines)
        return get_reply_for_category("chatgpt") or "تدلل، خليني أتأكد من باقات الشات وأرجعلك."
    templates = get_interactive_response_templates()
    return templates.get(action_key) or templates.get("handoff") or "تدلل، خليني أتأكد من الموضوع وأرجعلك."


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


# كل فئات الـFAQ الحالية هي معرفة معتمدة؛ فرع التفاعل يعرضها كما هي
# بدل أن يترك للـAI إعادة صياغتها أو إضافة تسويق غير مطلوب.
TEST_EXACT_FAQ_CATEGORIES = {category for category, _, _ in FAQ_RULES}


def get_exact_test_faq_reply(text: str) -> str | None:
    """
    في التفاعل، أي سؤال يغطيه الـFAQ ليس مكانًا للارتجال بالـAI. نرجع
    صيغة الـFAQ المعتمدة حرفيًا، ونترك الحالات الجديدة أو المركبة للـAI.
    """
    categories = keyword_match_categories(text)
    if not categories or not set(categories).issubset(TEST_EXACT_FAQ_CATEGORIES):
        return None

    replies = [get_reply_for_category(category) for category in categories]
    return "\n".join(reply for reply in replies if reply) or None


def get_secret_for_chat(chat_id: int) -> tuple[str, str] | None:
    """يرجع (secret, label) للحساب المربوط بهذا الزبون، أو None اذا مو مربوط."""
    link_res = (
        supabase.table("totp_links")
        .select("account_id")
        .eq("chat_id", chat_id)
        .execute()
    )
    if not link_res.data:
        # حسابات ChatGPT المشتركة التي سُلّمت عبر خزينة الحسابات.
        try:
            assigned = (
                supabase.table("chatgpt_account_assignments")
                .select("account_id")
                .eq("customer_chat_id", chat_id).eq("status", "active")
                .order("assigned_at", desc=True).limit(1).execute().data or []
            )
            if assigned:
                account = (
                    supabase.table("chatgpt_shared_accounts")
                    .select("totp_secret, email")
                    .eq("id", assigned[0]["account_id"]).limit(1).execute().data or []
                )
                if account:
                    return account[0]["totp_secret"], account[0]["email"]
        except Exception:
            logger.exception("Failed to get shared-account TOTP secret")
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


CHATGPT_DELIVERY_TEMPLATE = """ChatGPT

{email}

{password}

طريقة التسجيل
https://t.me/+IcHCjNi8_ilkZjdi

لو سمحت انضم للقناة
https://t.me/+M8XsrznhCNJkNTA6

شروط الاستخدام (وصول الرسالة يعني موافقتك على الشروط)
https://t.me/policy_use/2

هنيئاً 🎉"""


def assign_shared_chatgpt_account(customer_chat_id: int) -> dict | None:
    """يحجز حساباً مشتركاً فيه مقعد من أصل 9 لهذه الجلسة التجريبية."""
    state = get_interactive_sale_state(customer_chat_id)
    if not state.get("id"):
        return None
    try:
        existing = (
            supabase.table("chatgpt_account_assignments")
            .select("account_id, chatgpt_shared_accounts(email, password)")
            .eq("customer_chat_id", customer_chat_id)
            .eq("conversation_session_id", state["id"]).eq("status", "active")
            .limit(1).execute().data or []
        )
        if existing:
            return existing[0].get("chatgpt_shared_accounts")
        accounts = supabase.table("chatgpt_shared_accounts").select("id, email, password, capacity").eq("is_active", True).order("created_at").execute().data or []
        for account in accounts:
            used = supabase.table("chatgpt_account_assignments").select("id", count="exact").eq("account_id", account["id"]).eq("status", "active").execute()
            if (used.count or 0) >= int(account["capacity"]):
                continue
            supabase.table("chatgpt_account_assignments").insert({
                "account_id": account["id"], "customer_chat_id": customer_chat_id,
                "conversation_session_id": state["id"],
            }).execute()
            return account
    except Exception:
        logger.exception("Failed to assign shared ChatGPT account")
    return None


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
            try:
                await context.bot.send_message(
                    chat_id=NOTIFICATIONS_GROUP_ID,
                    message_thread_id=TOPIC_CHATGPT_ACCOUNTS,
                    text=f"➕ حساب مشترك جديد\nرمز الربط: {link_code}\nملاحظة: {label or '—'}",
                )
            except Exception:
                logger.exception("Failed to send new-account notification to topic")
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
        try:
            customer_name_for_topic = bm.chat.full_name or bm.chat.first_name or "غير معروف" if bm is not None else "غير معروف"
            customer_username_for_topic = bm.chat.username if bm is not None else None
            customer_line_for_topic = customer_name_for_topic
            if customer_username_for_topic:
                customer_line_for_topic += f" (@{customer_username_for_topic})"
            await context.bot.send_message(
                chat_id=NOTIFICATIONS_GROUP_ID,
                message_thread_id=TOPIC_CHATGPT_ACCOUNTS,
                text=f"🔗 ربط زبون بحساب مشترك\nالزبون: {customer_line_for_topic}\nالحساب: {label or link_code}",
            )
        except Exception:
            logger.exception("Failed to send account-link notification to topic")
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
    يرسل تنبيه (اسم الزبون، chat_id، شنو كتب، وشنو رد البوت) لقروب
    الإشعارات المخصص — لأن ردود البوت نفسها ما توصل إشعار (لأنها
    تنرسل بحساب الأونر نفسه عبر business_connection_id).
    """
    username_part = f" (@{customer_username})" if customer_username else ""
    notification = (
        f"📨 رسالة من: {customer_name}{username_part}\n"
        f"chat_id: {chat_id}\n\n"
        f"💬 كتب:\n{customer_message}\n\n"
        f"🤖 رد البوت:\n{bot_reply}"
    )
    try:
        await context.bot.send_message(
            chat_id=NOTIFICATIONS_GROUP_ID, message_thread_id=TOPIC_NOTIFICATIONS, text=notification
        )
    except Exception:
        logger.exception("Failed to notify owner")


async def describe_and_archive_customer_photo(context: ContextTypes.DEFAULT_TYPE, bm) -> None:
    """
    يحمّل صورة زبون، يوصفها بالذكاء الاصطناعي (Groq Vision)، ويؤرشف
    الوصف بجدول conversation_archive — بشكل مستقل تماماً عن فلو تأكيد
    الدفع الموجود، ما يأثر عليه ولا يبطئه (يُستدعى كـ task موازي).
    """
    customer_chat_id = bm.chat.id
    customer_name = bm.chat.full_name or bm.chat.first_name or "غير معروف"
    customer_username = bm.chat.username

    try:
        file = await context.bot.get_file(bm.photo[-1].file_id)
        file_bytes = bytes(await file.download_as_bytearray())
    except Exception:
        logger.exception("Failed to download customer photo for description")
        return

    description = await describe_image(file_bytes)
    if description is None:
        return

    archive_message(
        customer_chat_id, customer_name, customer_username,
        sender_type="customer", message_text=None, image_description=description,
    )


async def describe_and_archive_owner_photo(context: ContextTypes.DEFAULT_TYPE, bm) -> None:
    """
    يحمّل صورة أرسلتها أنت (owner) بمحادثة زبون معين، يوصفها بالذكاء
    الاصطناعي، ويؤرشفها بجدول conversation_archive بنفس سياق ذلك
    الزبون — متوازي تماماً مع فلو "هل هذا مصروف؟" الموجود، بدون ما
    يأثر عليه (يُستدعى كـ task موازي).
    """
    customer_chat_id = bm.chat.id
    customer_name = bm.chat.full_name or bm.chat.first_name or "غير معروف"
    customer_username = bm.chat.username

    try:
        file = await context.bot.get_file(bm.photo[-1].file_id)
        file_bytes = bytes(await file.download_as_bytearray())
    except Exception:
        logger.exception("Failed to download owner photo for description")
        return

    description = await describe_image(file_bytes)
    if description is None:
        return

    archive_message(
        customer_chat_id, customer_name, customer_username,
        sender_type="owner", message_text=None, image_description=description,
    )


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
        "awaiting_manual_product": False,
    }


async def handle_owner_expense_photo(update: Update, context: ContextTypes.DEFAULT_TYPE, photo) -> None:
    """
    توصل صورة منك (owner) — إما بمحادثتك مع زبون معين (Business) أو
    مباشرة بمحادثتك مع البوت نفسه. نحولها (أو نستخدمها مباشرة لو
    وصلت أصلاً بمحادثتك مع البوت) مع زرين: هل هذي إثبات مصروف؟
    محدودة بصورة وحدة/6 ساعات (عداد مستقل عن صور الزبائن).
    """
    if not is_owner_photo_within_rate_limit():
        logger.info("Owner expense photo ignored — exceeded rate limit")
        return

    file_id = photo[-1].file_id  # أعلى دقة متوفرة

    try:
        sent = await context.bot.send_photo(
            chat_id=OWNER_USER_ID,
            photo=file_id,
            caption="هل هذي إثبات مصروف؟",
            reply_markup=build_expense_photo_confirm_keyboard(),
        )
    except Exception:
        logger.exception("Failed to forward owner expense photo")
        return

    _pending_expense_photo_confirm[sent.message_id] = {"file_id": file_id}


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
    if data.startswith("pay_product_") and data not in ("pay_product_list", "pay_product_manual"):
        product = data[len("pay_product_"):]
        state["product"] = product
        customer_chat_id = state.get("customer_chat_id")
        has_debt = (
            customer_chat_id is not None and find_unpaid_debt(customer_chat_id, product) is not None
        )
        await query.edit_message_caption(
            caption=format_payment_summary(state),
            reply_markup=build_summary_keyboard(
                has_product=True, has_payment=bool(state["payments"]), show_debt_repayment=has_debt
            ),
        )
        return

    # -------------------- فتح قائمة بقية المنتجات --------------------
    if data == "pay_product_list":
        await query.edit_message_caption(
            caption=format_payment_summary(state),
            reply_markup=build_product_list_keyboard(),
        )
        return

    # -------------------- طلب إدخال اسم منتج حر (ينتظر رسالة نصية جاية كرد) --------------------
    if data == "pay_product_manual":
        state["awaiting_manual_product"] = True
        await query.edit_message_caption(
            caption=format_payment_summary(state) + "\n\nاكتب اسم المنتج بالرسالة الجاية (كـ رد على هذي الرسالة):",
            reply_markup=None,
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
        customer_chat_id = state.get("customer_chat_id")
        has_debt = (
            state["product"] and customer_chat_id is not None
            and find_unpaid_debt(customer_chat_id, state["product"]) is not None
        )
        await query.edit_message_caption(
            caption=format_payment_summary(state),
            reply_markup=build_summary_keyboard(
                has_product=bool(state["product"]), has_payment=bool(state["payments"]), show_debt_repayment=has_debt
            ),
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
        customer_chat_id = state.get("customer_chat_id")
        has_debt = (
            state["product"] and customer_chat_id is not None
            and find_unpaid_debt(customer_chat_id, state["product"]) is not None
        )
        await query.edit_message_caption(
            caption=format_payment_summary(state),
            reply_markup=build_summary_keyboard(
                has_product=bool(state["product"]), has_payment=True, show_debt_repayment=has_debt
            ),
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

        # نبعث إشعار لفرع "مدفوعات" بكل تفاصيل العملية — سطر منفصل لكل
        # طريقة دفع لو دفع الزبون بأكثر من طريقة بنفس العملية
        if saved:
            payment_lines = "\n".join(f"{method}: {amount}" for method, amount in state["payments"])
            payment_notification = (
                f"✅ عملية دفع جديدة\n"
                f"المنتج: {state['product']}\n"
                f"{payment_lines}"
            )
            try:
                await context.bot.send_message(
                    chat_id=NOTIFICATIONS_GROUP_ID, message_thread_id=TOPIC_PAYMENTS, text=payment_notification
                )
            except Exception:
                logger.exception("Failed to send payment notification to topic")

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

    # -------------------- تسديد دين — تستخدم نفس بيانات العملية الحالية --------------------
    if data == "pay_debt_repay":
        if not state["product"] or not state["payments"]:
            await query.answer("لازم تختار منتج وطريقة دفع وحدة على الأقل قبل التسديد.", show_alert=True)
            return

        customer_chat_id = state.get("customer_chat_id")
        if customer_chat_id is None:
            await query.answer("تعذر تحديد الزبون لهذي العملية.", show_alert=True)
            return

        debt_info = find_unpaid_debt(customer_chat_id, state["product"])
        if debt_info is None:
            await query.answer("ماكو دين غير مدفوع مطابق لهذا الزبون والمنتج حالياً.", show_alert=True)
            return

        debt_row, remaining_before = debt_info
        repay_amount = sum(amount for _, amount in state["payments"])
        saved, new_remaining = process_debt_repayment(debt_row, remaining_before, repay_amount)

        # نزيد رصيد كل خزنة مطابقة لطرق الدفع المستخدمة بالتسديد
        if saved:
            for method, amount in state["payments"]:
                if method in VAULT_NAMES:
                    adjust_vault_balance(method, amount)

        if saved:
            customer_line = format_customer_line(state["customer_name"], state.get("customer_username"))
            if new_remaining <= 0:
                await send_debt_notification(
                    context,
                    f"✅ تسديد دين بالكامل\nالزبون: {customer_line}\nالمنتج: {state['product']}\nالمبلغ المسدد: {repay_amount}",
                )
                final_text = format_payment_summary(state) + "\n\n✅ تم تسديد الدين بالكامل."
            else:
                await send_debt_notification(
                    context,
                    f"💳 تسديد جزئي لدين\nالزبون: {customer_line}\nالمنتج: {state['product']}\n"
                    f"المبلغ المسدد: {repay_amount}\nالمتبقي: {new_remaining}",
                )
                final_text = format_payment_summary(state) + f"\n\n✅ تم تسديد جزء من الدين. المتبقي: {new_remaining}"
        else:
            final_text = format_payment_summary(state) + "\n\n⚠️ فشل تحديث حالة الدين بـ Google Sheet — تحقق من الاتصال يدوياً."

        del _pending_payments[message_id]

        try:
            await query.edit_message_caption(caption=final_text, reply_markup=None)
        except Exception:
            logger.exception("Failed to update final debt-repayment confirmation message")
        return


async def handle_manual_product_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    يلتقط رسالة نصية جاية منك (owner) بمحادثتك الخاصة مع البوت وقت ما
    البوت ينتظر إدخال اسم منتج حر لعملية دفع جارية (رد على رسالة الصورة).
    يرجع True لو عالج الرسالة، False لو ما فيه عملية منتظرة إدخال يدوي.
    """
    message = update.message
    if not message or not message.text or not message.reply_to_message:
        return False

    replied_id = message.reply_to_message.message_id
    state = _pending_payments.get(replied_id)
    if state is None or not state.get("awaiting_manual_product"):
        return False

    product = message.text.strip()
    if not product:
        await message.reply_text("الرجاء كتابة اسم منتج غير فارغ.")
        return True

    state["product"] = product
    state["awaiting_manual_product"] = False

    customer_chat_id = state.get("customer_chat_id")
    has_debt = (
        customer_chat_id is not None and find_unpaid_debt(customer_chat_id, product) is not None
    )

    try:
        await context.bot.edit_message_caption(
            chat_id=OWNER_USER_ID,
            message_id=replied_id,
            caption=format_payment_summary(state),
            reply_markup=build_summary_keyboard(
                has_product=True, has_payment=bool(state["payments"]), show_debt_repayment=has_debt
            ),
        )
    except Exception:
        logger.exception("Failed to update caption after manual product entry")

    return True


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


async def handle_teaching_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعالج ضغطات أزرار جلسة التلقين: ✅ هذا ردي، ⏹ إنهاء الجلسة."""
    global _teaching_session

    query = update.callback_query
    if query.from_user.id != OWNER_USER_ID:
        await query.answer("هذا الزر مخصص للأونر بس.", show_alert=True)
        return

    if _teaching_session is None:
        await query.answer("ماكو جلسة تلقين نشطة حالياً.", show_alert=True)
        return

    data = query.data
    session = _teaching_session

    if data == "teach_mark_reply":
        if not session["customer_messages"]:
            await query.answer("لازم تحوّل رسالة زبون وحدة على الأقل أول.", show_alert=True)
            return
        session["awaiting_reply"] = True
        await query.answer()
        try:
            await query.edit_message_text(
                text=format_teaching_status(session),
                reply_markup=build_teaching_keyboard(has_customer_message=True),
            )
        except Exception:
            logger.exception("Failed to update teaching status after mark_reply")
        return

    if data == "teach_end_session":
        _teaching_session = None
        await query.answer()
        try:
            await query.edit_message_text(text="⏹ تم إنهاء جلسة التلقين.", reply_markup=None)
        except Exception:
            logger.exception("Failed to update message after ending teaching session")
        return



async def handle_debt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعالج كل ضغطات الأزرار الخاصة بفلو تسجيل دين جديد."""
    global _pending_debt

    query = update.callback_query
    if query.from_user.id != OWNER_USER_ID:
        await query.answer("هذا الزر مخصص للأونر بس.", show_alert=True)
        return

    if _pending_debt is None or _pending_debt.get("message_id") != query.message.message_id:
        await query.answer("انتهت صلاحية هذي العملية أو تم التعامل معها.", show_alert=True)
        return

    data = query.data
    await query.answer()
    debt = _pending_debt

    if data.startswith("debt_product_") and data not in ("debt_product_list", "debt_product_manual"):
        product = data[len("debt_product_"):]
        debt["product"] = product
        debt["step"] = "amount"
        await query.edit_message_text(text=format_debt_summary(debt), reply_markup=build_debt_amount_keyboard())
        return

    if data == "debt_product_list":
        await query.edit_message_text(text=format_debt_summary(debt), reply_markup=build_debt_product_list_keyboard())
        return

    if data == "debt_product_manual":
        debt["awaiting_manual_product"] = True
        await query.edit_message_text(
            text=format_debt_summary(debt) + "\n\nاكتب اسم المنتج بالرسالة الجاية (كـ رد على هذي الرسالة):",
            reply_markup=None,
        )
        return

    if data == "debt_back_to_product":
        await query.edit_message_text(text=format_debt_summary(debt), reply_markup=build_debt_product_keyboard())
        return

    if data == "debt_amount_add_small":
        debt["amount"] = debt.get("amount", 0) + PAYMENT_AMOUNT_STEP_SMALL
        await query.edit_message_text(text=format_debt_summary(debt), reply_markup=build_debt_amount_keyboard())
        return

    if data == "debt_amount_add_large":
        debt["amount"] = debt.get("amount", 0) + PAYMENT_AMOUNT_STEP_LARGE
        await query.edit_message_text(text=format_debt_summary(debt), reply_markup=build_debt_amount_keyboard())
        return

    if data == "debt_amount_manual":
        debt["awaiting_manual_amount"] = True
        await query.edit_message_text(
            text=format_debt_summary(debt) + "\n\nاكتب المبلغ رقم بس بالرسالة الجاية (كـ رد على هذي الرسالة):",
            reply_markup=None,
        )
        return

    if data == "debt_amount_commit":
        if not debt.get("amount") or not debt.get("product") or debt.get("chat_id") is None:
            await query.answer("لازم تحدد الزبون والمنتج والمبلغ قبل التثبيت.", show_alert=True)
            return

        saved_main = append_payment_row({
            "customer_name": debt["customer_line"],
            "customer_username": None,
            "customer_chat_id": debt["chat_id"],
            "product": debt["product"],
            "payments": [("دين", debt["amount"])],
        })
        saved_debt = append_debt_row(debt["chat_id"], debt["customer_line"], debt["product"], debt["amount"])

        if saved_main and saved_debt:
            final_text = format_debt_summary(debt) + "\n\n✅ تم تسجيل الدين بنجاح."
            await send_debt_notification(
                context,
                f"💳 دين جديد\nالزبون: {debt['customer_line']}\nالمنتج: {debt['product']}\nالمبلغ: {debt['amount']}",
            )
        else:
            final_text = format_debt_summary(debt) + "\n\n⚠️ فشل جزء من الحفظ بـ Google Sheet — تحقق من الاتصال يدوياً."

        try:
            await query.edit_message_text(text=final_text, reply_markup=None)
        except Exception:
            logger.exception("Failed to update final debt confirmation message")
        _pending_debt = None
        return



async def handle_expense_photo_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    يعالج ضغطة ✅ مصروف / ❌ ليس مصروف تحت صورة أرسلتها أنت. لو أكدت،
    يبدأ فلو تسجيل المصروف العادي (مبلغ → خزنة → سبب) بنفس رسالة
    الصورة (نعدل الـ caption تبعها بدل ما نرسل رسالة جديدة).
    """
    global _pending_expense

    query = update.callback_query
    if query.from_user.id != OWNER_USER_ID:
        await query.answer("هذا الزر مخصص للأونر بس.", show_alert=True)
        return

    message_id = query.message.message_id
    pending = _pending_expense_photo_confirm.get(message_id)
    if pending is None:
        await query.answer("انتهت صلاحية هذي الصورة أو تم التعامل معها.", show_alert=True)
        return

    data = query.data
    await query.answer()
    del _pending_expense_photo_confirm[message_id]

    if data == "expphoto_no":
        try:
            await query.message.delete()
        except Exception:
            logger.exception("Failed to delete non-expense owner photo")
        return

    # expphoto_yes — نبدأ فلو تسجيل مصروف عادي، بس نعدل caption الصورة
    # نفسها بدل ما نرسل رسالة نصية منفصلة
    _pending_expense = {
        "message_id": message_id,
        "amount": 0,
        "vault": None,
        "reason": None,
        "photo_file_id": pending["file_id"],
        "awaiting_manual_amount": False,
        "awaiting_manual_reason": False,
    }
    try:
        await query.edit_message_caption(
            caption=format_expense_summary(_pending_expense),
            reply_markup=build_expense_amount_keyboard(),
        )
    except Exception:
        logger.exception("Failed to start expense flow from photo")


async def edit_expense_message(query, expense: dict, text: str, reply_markup) -> None:
    """
    يعدل رسالة تسجيل المصروف — نص عادي (edit_message_text) لو الفلو
    بدأ من زر اللوحة الثابتة، أو caption لو الفلو بدأ من صورة مصروف
    مؤكدة (expense["photo_file_id"] موجود).
    """
    if expense.get("photo_file_id"):
        await query.edit_message_caption(caption=text, reply_markup=reply_markup)
    else:
        await query.edit_message_text(text=text, reply_markup=reply_markup)


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
        await edit_expense_message(query, expense, format_expense_summary(expense), build_expense_amount_keyboard())
        return

    if data == "exp_amount_add_large":
        expense["amount"] = expense.get("amount", 0) + PAYMENT_AMOUNT_STEP_LARGE
        await edit_expense_message(query, expense, format_expense_summary(expense), build_expense_amount_keyboard())
        return

    if data == "exp_amount_manual":
        expense["awaiting_manual_amount"] = True
        await edit_expense_message(
            query,
            expense,
            format_expense_summary(expense) + "\n\nاكتب المبلغ رقم بس بالرسالة الجاية (كـ رد على هذي الرسالة):",
            None,
        )
        return

    if data == "exp_amount_commit":
        if not expense.get("amount"):
            await query.answer("لازم تحدد مبلغ أكبر من صفر أول.", show_alert=True)
            return
        await edit_expense_message(query, expense, format_expense_summary(expense), build_expense_vault_keyboard())
        return

    if data.startswith("exp_vault_"):
        vault_key = data[len("exp_vault_"):]
        expense["vault"] = None if vault_key == "none" else vault_key
        await edit_expense_message(query, expense, format_expense_summary(expense), build_expense_reason_keyboard())
        return

    if data.startswith("exp_reason_") and data not in ("exp_reason_list", "exp_reason_manual"):
        reason = data[len("exp_reason_"):]
        expense["reason"] = reason
        saved = append_expense_row(expense["amount"], reason)
        if saved and expense.get("vault"):
            adjust_vault_balance(expense["vault"], -expense["amount"])
        if saved:
            await send_expense_notification(context, expense)
        final_text = format_expense_summary(expense) + (
            "\n\n✅ تم الحفظ بنجاح." if saved else "\n\n⚠️ فشل الحفظ بـ Google Sheet — تحقق من الاتصال يدوياً."
        )
        try:
            await edit_expense_message(query, expense, final_text, None)
        except Exception:
            logger.exception("Failed to update final expense confirmation message")
        _pending_expense = None
        return

    if data == "exp_reason_list":
        await edit_expense_message(
            query, expense, format_expense_summary(expense), build_expense_reason_list_keyboard()
        )
        return

    if data == "exp_back_to_reason":
        await edit_expense_message(query, expense, format_expense_summary(expense), build_expense_reason_keyboard())
        return

    if data == "exp_reason_manual":
        expense["awaiting_manual_reason"] = True
        await edit_expense_message(
            query,
            expense,
            format_expense_summary(expense) + "\n\nاكتب سبب المصروف بالرسالة الجاية (كـ رد على هذي الرسالة):",
            None,
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


async def edit_expense_message_by_bot(context: ContextTypes.DEFAULT_TYPE, expense: dict, text: str, reply_markup) -> None:
    """
    نسخة من edit_expense_message تستخدم context.bot مباشرة (بدل query)
    — للاستدعاء من مكان ما فيه callback_query (مثل رد نصي على رسالة).
    """
    if expense.get("photo_file_id"):
        await context.bot.edit_message_caption(
            chat_id=OWNER_USER_ID, message_id=expense["message_id"], caption=text, reply_markup=reply_markup
        )
    else:
        await context.bot.edit_message_text(
            chat_id=OWNER_USER_ID, message_id=expense["message_id"], text=text, reply_markup=reply_markup
        )


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
            await edit_expense_message_by_bot(
                context, expense, format_expense_summary(expense), build_expense_amount_keyboard()
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
        if saved:
            await send_expense_notification(context, expense)
        final_text = format_expense_summary(expense) + (
            "\n\n✅ تم الحفظ بنجاح." if saved else "\n\n⚠️ فشل الحفظ بـ Google Sheet — تحقق من الاتصال يدوياً."
        )
        try:
            await edit_expense_message_by_bot(context, expense, final_text, None)
        except Exception:
            logger.exception("Failed to update final expense confirmation after manual reason entry")
        _pending_expense = None
        return True

    return False


async def handle_reply_keyboard_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    يعالج ضغطات أزرار لوحة المفاتيح الثابتة (Reply Keyboard) تحت صندوق
    الكتابة: تسجيل مصروف، تقرير الدخل، إضافة حساب، تسجيل دين، بدء تلقين. يرجع
    True لو عالج.
    """
    global _pending_expense, _pending_add_account, _pending_debt, _teaching_session

    message = update.message
    if not message or not message.text:
        return False

    text = message.text.strip()

    # هذا الزر يعني أن المستخدم بدأ مهمة جديدة، لذلك نلغي أوضاع الإدخال
    # القديمة حتى ما تعترض المصروف أو التقرير أو أي وظيفة ثانية.
    if text in {
        BTN_CATALOG, BTN_PAYMENT_METHODS, BTN_EXPENSE, BTN_INCOME,
        BTN_ADD_ACCOUNT, BTN_STATS, BTN_DEBT, BTN_TEACH, BTN_CHATGPT_VAULT,
    }:
        context.user_data.pop("pending_payment_input", None)
        context.user_data.pop("pending_catalog_input", None)

    if text == BTN_CATALOG:
        await show_catalog_main(message)
        return True

    if text == BTN_PAYMENT_METHODS:
        await show_payment_methods(message)
        return True

    if text == BTN_CHATGPT_VAULT:
        await show_chatgpt_shared_vault(message)
        return True

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

    if text == BTN_DEBT:
        sent = await message.reply_text("أرسل chat_id تبع الزبون (رقم فقط):")
        _pending_debt = {
            "message_id": sent.message_id,
            "step": "chat_id",
            "chat_id": None,
            "customer_line": None,
            "product": None,
            "amount": 0,
            "awaiting_manual_product": False,
            "awaiting_manual_amount": False,
        }
        return True

    if text == BTN_TEACH:
        if _teaching_session is not None:
            await message.reply_text("فيه جلسة تلقين شغالة أصلاً — أنهيها أول قبل ما تبدأ وحدة جديدة.")
            return True
        _teaching_session = {
            "session_id": str(uuid.uuid4()),
            "customer_messages": [],
            "customer_chat_id": None,
            "awaiting_reply": False,
        }
        await message.reply_text(
            format_teaching_status(_teaching_session),
            reply_markup=build_teaching_keyboard(has_customer_message=False),
        )
        return True

    return False


async def handle_debt_chat_id_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    يلتقط ردك الأول بفلو تسجيل الدين (chat_id الزبون كرد على الرسالة
    اللي طلبته). يتحقق من صحة الرقم، يجيب اسم/يوزر الزبون من تيليجرام
    لو ممكن، وينتقل لخطوة اختيار المنتج.
    """
    global _pending_debt

    message = update.message
    if not message or not message.text or not message.reply_to_message:
        return False
    if _pending_debt is None or _pending_debt.get("message_id") != message.reply_to_message.message_id:
        return False
    if _pending_debt["step"] != "chat_id":
        return False

    text = message.text.strip()
    try:
        chat_id = int(text)
    except ValueError:
        await message.reply_text("الرجاء إدخال رقم chat_id صحيح فقط.")
        return True

    # نحاول نجيب اسم الزبون من تيليجرام (لو البوت إله وصول سابق لهذي المحادثة)
    try:
        chat = await context.bot.get_chat(chat_id)
        customer_name = chat.full_name or chat.first_name or "غير معروف"
        customer_username = chat.username
        customer_line = format_customer_line(customer_name, customer_username)
    except Exception:
        customer_line = f"chat_id: {chat_id}"

    _pending_debt["chat_id"] = chat_id
    _pending_debt["customer_line"] = customer_line
    _pending_debt["step"] = "product"

    try:
        await context.bot.edit_message_text(
            chat_id=OWNER_USER_ID,
            message_id=_pending_debt["message_id"],
            text=format_debt_summary(_pending_debt),
            reply_markup=build_debt_product_keyboard(),
        )
    except Exception:
        logger.exception("Failed to update debt message after chat_id entry")

    return True


async def handle_debt_manual_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    يلتقط رد نصي منك أثناء تسجيل دين — إما اسم منتج حر أو مبلغ يدوي،
    حسب أي خطوة بانتظار إدخال. يرجع True لو عالج الرسالة.
    """
    global _pending_debt

    message = update.message
    if not message or not message.text or not message.reply_to_message:
        return False
    if _pending_debt is None or _pending_debt.get("message_id") != message.reply_to_message.message_id:
        return False

    debt = _pending_debt

    if debt.get("step") == "product" and debt.get("awaiting_manual_product"):
        product = message.text.strip()
        if not product:
            await message.reply_text("الرجاء كتابة اسم منتج غير فارغ.")
            return True
        debt["product"] = product
        debt["awaiting_manual_product"] = False
        debt["step"] = "amount"
        try:
            await context.bot.edit_message_text(
                chat_id=OWNER_USER_ID,
                message_id=debt["message_id"],
                text=format_debt_summary(debt),
                reply_markup=build_debt_amount_keyboard(),
            )
        except Exception:
            logger.exception("Failed to update debt message after manual product entry")
        return True

    if debt.get("awaiting_manual_amount"):
        try:
            amount = int(re.sub(r"[^\d]", "", message.text))
        except ValueError:
            await message.reply_text("الرجاء إدخال رقم صحيح فقط.")
            return True
        if amount <= 0:
            await message.reply_text("الرجاء إدخال مبلغ أكبر من صفر.")
            return True

        debt["amount"] = amount
        debt["awaiting_manual_amount"] = False
        try:
            await context.bot.edit_message_text(
                chat_id=OWNER_USER_ID,
                message_id=debt["message_id"],
                text=format_debt_summary(debt),
                reply_markup=build_debt_amount_keyboard(),
            )
        except Exception:
            logger.exception("Failed to update debt message after manual amount entry")
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
            try:
                await context.bot.send_message(
                    chat_id=NOTIFICATIONS_GROUP_ID,
                    message_thread_id=TOPIC_CHATGPT_ACCOUNTS,
                    text=f"➕ حساب مشترك جديد\nرمز الربط: {link_code}\nملاحظة: {label or '—'}",
                )
            except Exception:
                logger.exception("Failed to send new-account notification to topic")
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


async def on_interactive_topic_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    يعالج رسائل نصية أو صوتية (مو أوامر) بفرع 'تفاعل' بالقروب — تجربة
    شات حر مباشر مع gpt-oss-120b، يستخدم ملخص متراكم (30 دقيقة صمت
    يستدعي دمج جديد) كذاكرة سياق بدل الاحتفاظ بكل التاريخ الخام.
    """
    message = update.message
    if not message or (not message.text and not message.voice and not message.photo):
        return
    if message.text and message.text.startswith("/"):
        return  # الأوامر (زي /getcode) تُعالج بـ handlers منفصلة
    if update.effective_user is None or update.effective_user.id != OWNER_USER_ID:
        return
    if message.message_thread_id != TOPIC_INTERACTIVE:
        return

    image_description = None
    payment_analysis = None
    if message.photo:
        try:
            file = await context.bot.get_file(message.photo[-1].file_id)
            image_bytes = bytes(await file.download_as_bytearray())
            # فحص وصل الدفع يغني عن طلب وصف ثانٍ لنفس الصورة. سابقاً كان
            # يستدعي قارئ الصورة مرتين متتاليتين (قد يصل التأخير لـ45 ثانية).
            payment_analysis = await analyze_payment_proof(image_bytes, context.user_data.get("interactive_test_chat_id", OWNER_USER_ID))
        except Exception:
            logger.exception("Failed to analyze image in interactive topic")
        if payment_analysis is None:
            await message.reply_text("⚠️ ما كدرت أدقق الصورة هسه. جرّب دزها مرة ثانية بعد دقيقة.")
            return
        image_description = payment_analysis.get("reason") or "صورة تم إرسالها للفحص"
        user_text = message.caption or "[صورة مرفقة]"
    elif message.voice:
        try:
            file = await context.bot.get_file(message.voice.file_id)
            file_bytes = bytes(await file.download_as_bytearray())
            user_text = await transcribe_audio(file_bytes)
        except Exception:
            logger.exception("Failed to download/transcribe voice message in interactive topic")
            user_text = None
        if not user_text:
            await message.reply_text("⚠️ فشل تحويل الصوت لنص.")
            return
    else:
        user_text = message.text

    # لكل /newtest رقم سياق اصطناعي مستقل، حتى ما تختلط سيناريوهات الاختبار.
    # إذا ما بدأ الأونر جلسة يظل السلوك القديم متاحًا للتوافق.
    customer_chat_id = context.user_data.get("interactive_test_chat_id", OWNER_USER_ID)

    # نؤرشف رسالتك أول (كـ "customer" بالمعنى الوظيفي — طرف المحادثة)
    archive_message(
        customer_chat_id, "تجربة", None, sender_type="customer",
        message_text=user_text, image_description=image_description,
    )

    # الذكاء الاصطناعي يختار الإجراء فقط؛ الرد النهائي دائماً ثابت ومخزن.
    if image_description:
        if payment_analysis is None:
            set_interactive_sale_state(customer_chat_id, "payment_review")
            action_key = "payment_under_review"
        else:
            save_interactive_payment_proof(customer_chat_id, payment_analysis)
            await notify_interactive_payment_review(context, payment_analysis)
            decision = payment_analysis.get("decision")
            if decision == "approved":
                set_interactive_sale_state(customer_chat_id, "payment_verified")
                action_key = "payment_proof_approved"
            elif decision == "rejected":
                set_interactive_sale_state(customer_chat_id, "awaiting_payment_proof")
                action_key = "payment_proof_rejected"
            else:
                set_interactive_sale_state(customer_chat_id, "payment_review")
                action_key = "payment_under_review"
    else:
        action_key = await choose_test_response_action(customer_chat_id, user_text)
    if action_key is None:
        await message.reply_text("⚠️ صار خطأ أثناء اختيار الإجراء — تحقق من اتصال Groq.")
        return
    if action_key == "payment_proof_approved":
        account = assign_shared_chatgpt_account(customer_chat_id)
        if account:
            set_interactive_sale_state(customer_chat_id, "account_delivered")
            reply = CHATGPT_DELIVERY_TEMPLATE.format(email=account["email"], password=account["password"])
        else:
            # لا يوجد مقعد متاح؛ لا نرسل أي بيانات حساب.
            reply = "تم تأكيد التحويل مبدئياً، بس حالياً ماكو مقعد مشترك متاح. دا أرتبلك واحد وأرجعلك."
    else:
        reply = render_test_response(action_key, user_text)

    archive_message(customer_chat_id, "تجربة", None, sender_type="bot", message_text=reply)

    try:
        await context.bot.send_message(
            chat_id=NOTIFICATIONS_GROUP_ID, message_thread_id=TOPIC_INTERACTIVE, text=reply
        )
    except Exception:
        logger.exception("Failed to send test chat reply to interactive topic")


async def on_owner_private_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    يعالج صور مرسلة مباشرة لمحادثتك مع البوت (مو Business) — لو فيه
    جلسة تلقين نشطة، الصورة تروح لها أولاً. غير هيك، محتملة إثبات
    مصروف، نفس معاملة الصور اللي ترسلها بمحادثة زبون.
    """
    if await handle_teaching_message(update, context):
        return
    if await handle_payment_method_input(update, context):
        return
    if await handle_shared_account_input(update, context):
        return
    if await handle_catalog_input(update, context):
        return

    message = update.message
    if not message or not message.photo:
        return
    await handle_owner_expense_photo(update, context, message.photo)


async def handle_teaching_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    يلتقط رسائل نصية، صور، أو صوتية (محولة أو مباشرة) بمحادثتك الخاصة
    مع البوت وقت ما فيه جلسة تلقين نشطة. يرجع True لو عالج الرسالة
    (يوقف أي معالجة ثانية للرسالة)، False لو ما فيه جلسة نشطة.

    - لو الرسالة صورة: تُوصف بالذكاء الاصطناعي (Groq Vision).
    - لو الرسالة صوتية: تُحول لنص (Groq Whisper).
    - النص/الوصف الناتج يُعامل بنفس منطق النص العادي (يضاف لرسائل
      الزبون، أو يُحفظ كرد).
    - كل رسالة (نص/صورة/صوت) تُؤرشف بالتوازي بجدول conversation_archive
      (لو customer_chat_id معروف لهذي الجلسة)، بنفس آلية أرشفة رسائل
      Business العادية — عشان جلسات التلقين ما تضل غايبة عن الأرشيف.
      رسائل الزبون المجمّعة تُؤرشف كـ sender_type="customer"، والرد
      المكتوب/المحوّل بعد "هذا ردي" يُؤرشف كـ sender_type="owner".
      لو ماكو customer_chat_id معروف لهذي الجلسة، نتجاهل الأرشفة
      بهدوء (الجدول محتاج قيمة صحيحة له) بدون ما يوقف باقي الفلو.
    - لو الجلسة بانتظار رسائل الزبون (awaiting_reply=False): يضيف
      الرسالة لقائمة customer_messages المتجمعة، ويحاول يستخرج
      customer_chat_id من forward_origin لو متوفر.
    - لو الجلسة بانتظار الرد (awaiting_reply=True): يحفظ المثال
      الكامل بـ style_examples، ويصفر الجلسة لمثال جديد تلقائياً.
    """
    global _teaching_session

    message = update.message
    if not message or _teaching_session is None:
        return False
    if not message.text and not message.photo and not message.voice:
        return False

    session = _teaching_session

    # نحاول نجيب معلومة الزبون الأصلي من forward_origin لو الرسالة محولة
    # ومو معروف عندنا chat_id لهذي الجلسة أصلاً
    if session["customer_chat_id"] is None and message.forward_origin is not None:
        origin = message.forward_origin
        origin_chat = getattr(origin, "sender_user", None) or getattr(origin, "chat", None)
        if origin_chat is not None:
            session["customer_chat_id"] = origin_chat.id

    is_image = False
    if message.photo:
        try:
            file = await context.bot.get_file(message.photo[-1].file_id)
            file_bytes = bytes(await file.download_as_bytearray())
        except Exception:
            logger.exception("Failed to download teaching session photo")
            await message.reply_text("⚠️ فشل تحميل الصورة.")
            return True

        description = await describe_image(file_bytes)
        if description is None:
            await message.reply_text("⚠️ فشل تحليل الصورة — تحقق من الاتصال.")
            return True
        text = f"[صورة: {description}]"
        is_image = True
    elif message.voice:
        try:
            file = await context.bot.get_file(message.voice.file_id)
            file_bytes = bytes(await file.download_as_bytearray())
        except Exception:
            logger.exception("Failed to download teaching session voice message")
            await message.reply_text("⚠️ فشل تحميل الرسالة الصوتية.")
            return True

        transcript = await transcribe_audio(file_bytes)
        if not transcript:
            await message.reply_text("⚠️ فشل تحويل الصوت لنص — تحقق من الاتصال.")
            return True
        text = transcript
    else:
        text = message.text.strip()
        if not text:
            return True  # رسالة فاضية بجلسة نشطة — نتجاهلها بصمت بدل ما نمررها لمعالج ثاني

    # ------------------------------------------------------------------
    # أرشفة موازية (الإضافة الجديدة) — تسجل كل رسالة بجلسة التلقين
    # (نص/صورة/صوت) بجدول conversation_archive، بنفس sender_type
    # المستخدم بباقي البوت ("customer" أو "owner"). لا تعطل ولا تؤخر
    # فلو التلقين نفسه لو فشلت (archive_message تبتلع أخطاءها داخلياً)،
    # وتُتجاهل كلياً لو customer_chat_id مو معروف بعد لهذي الجلسة.
    # ------------------------------------------------------------------
    if session["customer_chat_id"] is not None:
        archive_sender_type = "owner" if session["awaiting_reply"] else "customer"
        archive_message(
            session["customer_chat_id"],
            None,
            None,
            sender_type=archive_sender_type,
            message_text=None if is_image else text,
            image_description=text if is_image else None,
        )

    if not session["awaiting_reply"]:
        session["customer_messages"].append(text)
        await message.reply_text(
            format_teaching_status(session),
            reply_markup=build_teaching_keyboard(has_customer_message=True),
        )
        return True

    # awaiting_reply=True — هذا النص/الوصف هو الرد، نحفظ المثال ونبدأ مثال جديد
    combined_customer_message = "\n".join(session["customer_messages"])
    saved = save_style_example(session["session_id"], session["customer_chat_id"], combined_customer_message, text, source="manual")

    session["customer_messages"] = []
    session["awaiting_reply"] = False
    # نبقي customer_chat_id كما هو (غالباً نفس الزبون للمثال الجاي بنفس الجلسة)

    status_line = "✅ تم حفظ المثال." if saved else "⚠️ فشل حفظ المثال — تحقق من الاتصال."
    await message.reply_text(
        status_line + "\n\n" + format_teaching_status(session),
        reply_markup=build_teaching_keyboard(has_customer_message=False),
    )
    return True



async def on_owner_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    يعالج رسائل نصية عادية (مو Business) جاية منك بمحادثتك المباشرة
    مع البوت — جلسة تلقين نشطة (أولوية قصوى، تلتقط أي رسالة أثناءها)،
    إدخال اسم منتج حر أو مبلغ يدوي أثناء تسجيل دفع، تسجيل حساب ChatGPT
    خاص، إدخال مصروف يدوي، تاريخ يدوي بالإحصائيات، تعديل رصيد خزنة
    يدوياً، فلو تسجيل دين، أزرار لوحة المفاتيح الثابتة، أو فلو إضافة
    حساب تفاعلي.
    """
    if await handle_teaching_message(update, context):
        return
    # أزرار الإدارة الرئيسية تأخذ الأولوية على أي وضع إدخال قديم.
    if await handle_reply_keyboard_button(update, context):
        return
    if await handle_payment_method_input(update, context):
        return
    if await handle_shared_account_input(update, context):
        return
    if await handle_catalog_input(update, context):
        return
    if await handle_manual_product_entry(update, context):
        return
    if await handle_manual_amount_entry(update, context):
        return
    if await handle_expense_manual_entry(update, context):
        return
    if await handle_stats_manual_period_entry(update, context):
        return
    if await handle_vault_edit_manual_entry(update, context):
        return
    if await handle_debt_chat_id_entry(update, context):
        return
    if await handle_debt_manual_entry(update, context):
        return
    if await handle_chatgpt_account_reply(update, context):
        return
    await handle_add_account_flow(update, context)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر /start — يرسل لوحة المفاتيح الثابتة (مصروف/دخل/إضافة حساب) بمحادثتك مع البوت."""
    if update.effective_user is None or update.effective_user.id != OWNER_USER_ID:
        return
    await update.message.reply_text("جاهز. استخدم الأزرار بالأسفل:", reply_markup=MAIN_REPLY_KEYBOARD)


async def cmd_import_archive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    استيراد آمن لمرة أولى من conversation_archive إلى style_examples.
    يستورد التسلسل: رسائل زبون ثم رد نصي للمتجر (أونر أو بوت).
    """
    if update.effective_user is None or update.effective_user.id != OWNER_USER_ID:
        return
    if update.effective_chat is None or update.effective_chat.type != "private":
        return

    await update.message.reply_text("⏳ دا أرتب الأرشيف وأحوّل ردودك وردود البوت إلى أمثلة تلقين...")
    try:
        added, skipped_duplicates, total_candidates = import_archive_as_style_examples()
    except Exception:
        logger.exception("Archive-to-style import failed")
        await update.message.reply_text("⚠️ فشل الاستيراد. تحقق من اتصال Supabase والـ logs.")
        return

    await update.message.reply_text(
        "✅ اكتمل ترتيب الأرشيف.\n"
        f"أمثلة جديدة محفوظة: {added}\n"
        f"أمثلة مكررة تم تجاوزها: {skipped_duplicates}\n"
        f"إجمالي الأمثلة المرشحة: {total_candidates}\n\n"
        "الآن ردود فرع التفاعل تقدر تستخدم هذه الأمثلة كمرجع لأسلوبك."
    )


async def cmd_income_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر /دخل — يعرض تقرير الدخل (اليوم/الأسبوع/الشهر) من Google Sheet."""
    if update.effective_user is None or update.effective_user.id != OWNER_USER_ID:
        return
    report = calculate_income_report()
    await update.message.reply_text(report)


def build_getcode_show_keyboard(account_id) -> InlineKeyboardMarkup:
    """زر 'عرض الكود' — يطلع لما الرسالة تعرض اسم الحساب بس."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("👁 عرض الكود", callback_data=f"getcode_show_{account_id}")]])


def build_getcode_back_keyboard(account_id) -> InlineKeyboardMarkup:
    """زر 'رجوع' — يطلع لما الرسالة تعرض الكود نفسه."""
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ رجوع", callback_data=f"getcode_back_{account_id}")]])


async def cmd_getcode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    أمر /getcode — يشتغل بس بفرع 'تفاعل' بالقروب، من الأونر. يرسل
    رسالة منفصلة لكل حساب TOTP موجود، فيها اسم الحساب وزر 'عرض الكود'.
    """
    if update.effective_user is None or update.effective_user.id != OWNER_USER_ID:
        return
    if update.message is None or update.message.message_thread_id != TOPIC_INTERACTIVE:
        return  # يشتغل بس بفرع التفاعل، نتجاهل أي استخدام بمكان ثاني

    accounts = get_all_accounts_with_secrets()
    if not accounts:
        await update.message.reply_text("ماكو حسابات مسجلة حالياً.")
        return

    for acc in accounts:
        display_name = acc.get("label") or acc.get("link_code", "بدون اسم")
        try:
            await context.bot.send_message(
                chat_id=NOTIFICATIONS_GROUP_ID,
                message_thread_id=TOPIC_INTERACTIVE,
                text=display_name,
                reply_markup=build_getcode_show_keyboard(acc["id"]),
            )
        except Exception:
            logger.exception(f"Failed to send getcode message for account {acc.get('id')}")


async def cmd_newtest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يبدأ سياقًا مستقلاً لتجربة جديدة داخل فرع التفاعل فقط."""
    if update.effective_user is None or update.effective_user.id != OWNER_USER_ID:
        return
    if update.message is None or update.message.message_thread_id != TOPIC_INTERACTIVE:
        return

    # رقم اصطناعي سالب حتى لا يختلط تاريخ اختبار جديد مع chat_id زبون حقيقي.
    test_chat_id = -((OWNER_USER_ID * 1_000_000) + update.message.message_id)
    context.user_data["interactive_test_chat_id"] = test_chat_id
    await update.message.reply_text(
        "🧪 بدأت جلسة اختبار جديدة ومعزولة. اكتب الآن رسائل الزبون بالتسلسل؛ "
        "ولبدء سيناريو مختلف اكتب /newtest مرة ثانية."
    )


async def handle_getcode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعالج ضغطات زر 'عرض الكود' / 'رجوع' بفرع التفاعل."""
    query = update.callback_query
    if query.from_user.id != OWNER_USER_ID:
        await query.answer("هذا الزر مخصص للأونر بس.", show_alert=True)
        return

    data = query.data

    if data.startswith("getcode_show_"):
        account_id = data[len("getcode_show_"):]
        try:
            res = supabase.table("totp_accounts").select("secret, label, link_code").eq("id", account_id).execute()
        except Exception:
            logger.exception("Failed to fetch account secret for getcode")
            await query.answer("صار خطأ أثناء جلب الكود.", show_alert=True)
            return

        if not res.data:
            await query.answer("هذا الحساب ما عاد موجود.", show_alert=True)
            return

        secret = res.data[0]["secret"]
        code = generate_totp_code(secret)
        await query.answer()
        try:
            await query.edit_message_text(text=code, reply_markup=build_getcode_back_keyboard(account_id))
        except Exception:
            logger.exception("Failed to update message with TOTP code")
        return

    if data.startswith("getcode_back_"):
        account_id = data[len("getcode_back_"):]
        try:
            res = supabase.table("totp_accounts").select("label, link_code").eq("id", account_id).execute()
        except Exception:
            logger.exception("Failed to fetch account label for getcode back")
            await query.answer("صار خطأ.", show_alert=True)
            return

        display_name = "بدون اسم"
        if res.data:
            display_name = res.data[0].get("label") or res.data[0].get("link_code", "بدون اسم")

        await query.answer()
        try:
            await query.edit_message_text(text=display_name, reply_markup=build_getcode_show_keyboard(account_id))
        except Exception:
            logger.exception("Failed to revert message to account name")
        return


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
        # نشغل وصف الصورة بالأرشيف كـ task موازي منفصل — ما يبطئ ولا
        # يأثر على فلو تأكيد الدفع الأساسي (كل وحدة تشتغل لحالها)
        asyncio.create_task(describe_and_archive_customer_photo(context, bm))
        await handle_incoming_payment_photo(update, context, bm)
        return

    # صورة أرسلتها أنت (owner) بمحادثتك مع زبون معين — محتملة إثبات
    # مصروف، نحولها لمحادثتك مع البوت ونسألك تأكيد. بالتوازي، نؤرشفها
    # (وصف بالذكاء الاصطناعي) بدون ما يأثر على فلو المصروف
    if bm.photo and is_from_owner:
        asyncio.create_task(describe_and_archive_owner_photo(context, bm))
        await handle_owner_expense_photo(update, context, bm.photo)
        return

    # رسالة صوتية (من الزبون أو منك) — نحولها لنص عبر Whisper، ونعاملها
    # بعدها بنفس آلية الرسالة النصية العادية (تصنيف، رد، أرشفة)
    text = bm.text
    if not text and bm.voice:
        try:
            file = await context.bot.get_file(bm.voice.file_id)
            file_bytes = bytes(await file.download_as_bytearray())
            text = await transcribe_audio(file_bytes)
        except Exception:
            logger.exception("Failed to download/transcribe voice message")
            text = None
        if not text:
            return  # فشل تحويل الصوت — نتجاهل بصمت بدل ما نكرش

    if not text:
        return

    chat_id = bm.chat.id

    # اسم الزبون واسم المستخدم (لو موجود) — نستخدمهن بالتنبيه للأونر
    customer_name = bm.chat.full_name or bm.chat.first_name or "غير معروف"
    customer_username = bm.chat.username

    # 1) اذا الرسالة منك انت (owner) — تحقق اذا هي أمر ربط/اضافة/accept
    if is_from_owner:
        handled = await handle_owner_command(update, context, chat_id, text, bm=bm)
        if not handled:
            # ردود الأونر الحقيقية هي أهم مصدر للتعلم. الأوامر لا نؤرشفها
            # حتى لا تتحول إلى أمثلة أسلوب أو تدخل بسياق الزبون.
            archive_message(
                chat_id, customer_name, customer_username,
                sender_type="owner", message_text=text,
            )
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
            await context.bot.send_message(
                chat_id=NOTIFICATIONS_GROUP_ID, message_thread_id=TOPIC_NOTIFICATIONS, text=complaint_notification
            )
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
            await context.bot.send_message(
                chat_id=NOTIFICATIONS_GROUP_ID, message_thread_id=TOPIC_NOTIFICATIONS, text=stopped_notification
            )
        except Exception:
            logger.exception("Failed to send stopped-retry notification to owner")

    if not replies_to_send:
        archive_message(chat_id, customer_name, customer_username, sender_type="customer", message_text=text)
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
    archive_message(chat_id, customer_name, customer_username, sender_type="customer", message_text=text)
    archive_message(chat_id, customer_name, customer_username, sender_type="bot", message_text=combined_reply)
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

    # زرين تأكيد صورة مصروف محتملة (✅ مصروف / ❌ ليس مصروف)
    app.add_handler(CallbackQueryHandler(handle_expense_photo_callback, pattern=r"^expphoto_"))

    # أزرار الإحصائيات — تشتغل بمحادثتك الخاصة مع البوت نفسه
    app.add_handler(CallbackQueryHandler(handle_stats_callback, pattern=r"^stats_"))

    # أزرار فلو تسجيل الدين
    app.add_handler(CallbackQueryHandler(handle_debt_callback, pattern=r"^debt_"))

    # أزرار جلسة التلقين اليدوي
    app.add_handler(CallbackQueryHandler(handle_teaching_callback, pattern=r"^teach_"))

    # إدارة المنتجات والباقات — للأونر فقط داخل محادثته الخاصة مع البوت.
    app.add_handler(CallbackQueryHandler(handle_catalog_callback, pattern=r"^catalog_"))

    # إدارة تفاصيل الدفع المعتمدة — للأونر فقط.
    app.add_handler(CallbackQueryHandler(handle_payment_method_callback, pattern=r"^pm_"))

    # إدارة خزينة حسابات ChatGPT المشتركة — للأونر فقط.
    app.add_handler(CallbackQueryHandler(handle_chatgpt_vault_callback, pattern=r"^vault_"))

    # زرين تبديل عرض/إخفاء كود TOTP بفرع التفاعل
    app.add_handler(CallbackQueryHandler(handle_getcode_callback, pattern=r"^getcode_"))

    # أمر /start — يرسل لوحة المفاتيح الثابتة (مصروف/دخل/إضافة حساب)
    app.add_handler(CommandHandler("start", cmd_start))

    # أمر /income لعرض تقرير الدخل — بمحادثتك الخاصة مع البوت
    # (تيليجرام يشترط أوامر بحروف إنكليزية بس، ما يقبل حروف عربية بأسماء الأوامر)
    app.add_handler(CommandHandler("income", cmd_income_report))

    # أمر /importarchive — يحوّل محادثات الأرشيف القديمة إلى أمثلة أسلوب
    # نظيفة، ويُستخدم من الأونر داخل محادثته الخاصة مع البوت فقط.
    app.add_handler(CommandHandler("importarchive", cmd_import_archive))

    # أمر /getcode — يشتغل بس بفرع "تفاعل" بالقروب، يرسل رسالة منفصلة
    # لكل حساب TOTP مع زر لعرض الكود
    app.add_handler(CommandHandler("getcode", cmd_getcode))

    # أمر /newtest — يبدأ سياق اختبار مستقل بفرع التفاعل.
    app.add_handler(CommandHandler("newtest", cmd_newtest))

    # رسائل نصية عادية منك بمحادثتك الخاصة مع البوت — تستخدم حالياً
    # بس لالتقاط إدخال مبلغ يدوي أثناء تسجيل دفع (رد على رسالة الصورة)
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & filters.User(OWNER_USER_ID),
            on_owner_private_message,
        )
    )

    # صور مرسلة مباشرة لمحادثتك مع البوت (مو رد، مو Business) — محتملة
    # إثبات مصروف
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.PHOTO & filters.User(OWNER_USER_ID),
            on_owner_private_photo,
        )
    )

    # رسائل صوتية مرسلة مباشرة لمحادثتك مع البوت (مو Business) — محتملة
    # جزء من جلسة تلقين نشطة
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.VOICE & filters.User(OWNER_USER_ID),
            handle_teaching_message,
        )
    )

    # رسائل نصية أو صوتية أو صور بفرع "تفاعل" بالقروب — تجربة شات حر.
    app.add_handler(
        MessageHandler(
            filters.ChatType.SUPERGROUP & (filters.TEXT | filters.VOICE | filters.PHOTO) & filters.User(OWNER_USER_ID),
            on_interactive_topic_message,
        )
    )

    app.run_polling(
        allowed_updates=["business_message", "business_connection", "edited_business_message", "callback_query", "message"]
    )


if __name__ == "__main__":
    main()
