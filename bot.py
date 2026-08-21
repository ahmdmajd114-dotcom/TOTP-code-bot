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
- أنت تضيف حساباً خاصاً مباشرة داخل محادثة الزبون بأمر /addprivate
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
from chatgpt_sales_flow import (
    asks_payment_guidance,
    can_request_account_code,
    decide_code_retry,
    decide_private_code_retry,
    is_acknowledgement,
    is_ambiguous_followup,
    is_chatgpt_support_issue,
    is_payment_claim,
    is_private_chatgpt_plan,
    resolve_plan_choice,
    should_review_payment_photo,
    is_paid_amount_sufficient,
    classify_receipt_recency,
)
from modesty_guard import is_flirtatious_text, is_guarded_chat
from instagram_sales import commission_for, format_iqd, normalize_chat_type, parse_amount

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
INSTAGRAM_MANAGER_USER_ID = int(os.environ.get("INSTAGRAM_MANAGER_USER_ID", "0"))
INSTAGRAM_COMMISSION_PERCENT = int(os.environ.get("INSTAGRAM_COMMISSION_PERCENT", "25"))
NOTIFICATIONS_GROUP_ID = int(os.environ.get("NOTIFICATIONS_GROUP_ID", "-1003771659131"))  # قروب سجل الردود
SUBSCRIPTION_FEEDBACK_URL = os.environ.get("SUBSCRIPTION_FEEDBACK_URL", "").strip()
# التحية الأولى بالمحادثة الجديدة تنتظر حتى نعرف إن كان الزبون سيكمل طلبه.
# إذا بقيت تحية فقط، يرسلها البوت بعد هذه المدة؛ وإذا وصلت رسالة ثانية قبلها
# تُضم للتحية وتُصنّف كرسالة واحدة.
INITIAL_GREETING_WAIT_SECONDS = float(os.environ.get("INITIAL_GREETING_WAIT_SECONDS", "60"))
# التحية وحدها لا تُرسل خلال فترة الهدوء الليلية؛ الطلب الواضح داخل نفس
# الرسالة يبقى فورياً، وكذلك أي متابعة تصل قبل إرسال التحية المؤجلة.
QUIET_HOURS_START = (0, 30)
QUIET_HOURS_END = (9, 0)

# أرقام الفروع (Topics) داخل قروب الإشعارات — كل فرع مخصص لنوع إشعار
TOPIC_NOTIFICATIONS = 6   # الردود العامة، الشكاوى، مشاكل الكود
TOPIC_PAYMENTS = 8        # إشعار كل عملية دفع/إضافة منتج
TOPIC_EXPENSES = 10       # إشعار كل عملية مصروف
TOPIC_INTERACTIVE = 12    # محجوز لاحقاً — تفاعل مباشر مع البوت
TOPIC_CHATGPT_ACCOUNTS = 33  # إضافة حساب مشترك جديد + ربط زبون بحساب
TOPIC_DEBTS = int(os.environ.get("TOPIC_DEBTS", "0"))  # تسجيل دين جديد + تسديد دين — لازم تحدد رقمه الحقيقي
SHARED_CHATGPT_ACCOUNT_CAPACITY = 3  # الحد الثابت لكل حساب ChatGPT مشترك
# معرف المحادثة المسموح للفلتر أن يعمل عليها. يبقى في Render فقط، ولا يوضع
# في الكود أو Git. الصفر يعني أن الفلتر متوقف بالكامل.
MODESTY_GUARD_CHAT_ID = int(os.environ.get("MODESTY_GUARD_CHAT_ID", "0"))
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
INSTAGRAM_SALES_WORKSHEET_NAME = os.environ.get("INSTAGRAM_SALES_WORKSHEET_NAME", "مبيعات الإنستغرام")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ------------------------------------------------------------------
# إدارة مفاتيح Groq المتعددة — نحتفظ بمؤشر لآخر مفتاح ناجح، ونستمر
# نستخدمه لحد ما يفشل هو نفسه (مو نرجع للأول كل مرة). لو فشل، ننتقل
# للمفتاح الجاي بالقائمة تلقائياً، ونحدث المؤشر.
# ------------------------------------------------------------------
_current_groq_key_index = 0

# {chat_id: {"task": asyncio.Task, "text": str, "business_connection_id": str,
#            "message_id": int, "customer_name": str, "customer_username": str}}
_pending_initial_greetings: dict[int, dict] = {}


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


_instagram_sales_sheet = None


def get_instagram_sales_worksheet():
    """Return/create the protected Instagram sales tab with a stable schema."""
    global _instagram_sales_sheet
    if _instagram_sales_sheet is not None:
        return _instagram_sales_sheet
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
            _instagram_sales_sheet = spreadsheet.worksheet(INSTAGRAM_SALES_WORKSHEET_NAME)
        except gspread.exceptions.WorksheetNotFound:
            _instagram_sales_sheet = spreadsheet.add_worksheet(
                title=INSTAGRAM_SALES_WORKSHEET_NAME, rows=2000, cols=20
            )
            _instagram_sales_sheet.append_row(
                [
                    "رقم العملية", "التاريخ والوقت", "يوزر الإنستغرام", "المنتج",
                    "نوع الجات", "نوع المحفظة", "المبلغ", "النسبة %",
                    "العمولة", "الحالة", "تم دفع العمولة؟", "المسجل",
                    "Telegram User ID", "ملاحظات", "مصدر الحساب", "معرف الحساب", "الحساب المستخدم", "تاريخ دفع العمولة", "مدة الاشتراك",
                ],
                value_input_option="USER_ENTERED",
            )
        if getattr(_instagram_sales_sheet, "col_count", 0) < 20:
            _instagram_sales_sheet.resize(rows=max(getattr(_instagram_sales_sheet, "row_count", 1000), 2000), cols=20)
        header = _instagram_sales_sheet.row_values(1)
        required_tail = ["مصدر الحساب", "معرف الحساب", "الحساب المستخدم", "تاريخ دفع العمولة", "مدة الاشتراك"]
        if header and not all(value in header for value in required_tail):
            _instagram_sales_sheet.update("O1:S1", [required_tail])
        return _instagram_sales_sheet
    except Exception:
        logger.exception("Failed to connect to Instagram sales worksheet")
        return None


def append_instagram_sale(sale: dict) -> bool:
    sheet = get_instagram_sales_worksheet()
    if sheet is None:
        return False
    try:
        sheet.append_row(
            [
                sale["sale_id"], sale["created_at"], sale["instagram_account"],
                sale["product"], sale.get("chat_type") or "—", sale["wallet"],
                sale["amount"], sale["commission_percent"], sale["commission"],
                "مؤكدة", "لا", sale["recorded_by"], sale["recorded_by_id"],
                sale.get("notes") or "", sale.get("account_source") or "—",
                sale.get("account_id") or "—", sale.get("account_display") or "—", "",
                f"{sale.get('duration_months')} شهر" if sale.get("duration_months") else "—",
            ],
            value_input_option="USER_ENTERED",
        )
        return True
    except Exception:
        logger.exception("Failed to append Instagram sale")
        return False


def save_instagram_subscription_reminder(sale: dict, state: dict) -> bool:
    """Schedule an Instagram expiry reminder; Instagram has no Telegram chat id."""
    duration_months = state.get("duration_months")
    if sale.get("product") != CHATGPT_PRODUCT_NAME or duration_months not in {1, 2}:
        return True
    now = datetime.now(timezone.utc)
    try:
        supabase.table("subscription_reminders").insert({
            "customer_chat_id": None,
            "customer_name": sale["instagram_account"],
            "customer_username": sale["instagram_account"],
            "subscription_type": "instagram",
            "duration_months": duration_months,
            "duration_days": duration_months * 30,
            "product_name": sale["product"],
            "plan_name": f"{sale.get('chat_type') or ''} {duration_months} شهر".strip(),
            "plan_duration": f"{duration_months} شهر",
            "feedback_only": False,
            "feedback_status": "none",
            "started_at": now.isoformat(),
            "expires_at": (now + timedelta(days=duration_months * 30)).isoformat(),
            "source": "instagram",
            "instagram_sale_id": sale["sale_id"],
            "instagram_account": sale["instagram_account"],
        }).execute()
        return True
    except Exception:
        logger.exception("Failed to save Instagram subscription reminder")
        return False


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
# منع تكرار ردود الـ FAQ لنفس الزبون خلال ست ساعات. نتحقق من الأرشيف حتى
# يبقى المنع فعالاً بعد إعادة التشغيل أو إعادة النشر، ونستخدم الذاكرة أيضاً
# لمنع تكرارين متزامنين قبل اكتمال الأرشفة.
# ------------------------------------------------------------------
FAQ_REPEAT_COOLDOWN_SECONDS = 6 * 60 * 60
_faq_reply_log: dict[tuple[int, str], datetime] = {}


def should_send_faq_reply(chat_id: int, category: str, reply_text: str) -> bool:
    """
    يمنع تكرار رد فئة FAQ لنفس الزبون والرد نفسه خلال ست ساعات.
    الفحص من conversation_archive يبقى فعالاً بعد restart/deploy.
    """
    key = (chat_id, category)
    now = datetime.now(timezone.utc)
    last_memory_reply = _faq_reply_log.get(key)
    if last_memory_reply is not None:
        if (now - last_memory_reply).total_seconds() < FAQ_REPEAT_COOLDOWN_SECONDS:
            return False
        _faq_reply_log.pop(key, None)

    try:
        archived = (
            supabase.table("conversation_archive")
            .select("created_at")
            .eq("customer_chat_id", chat_id)
            .eq("sender_type", "bot")
            .ilike("message_text", f"%{reply_text}%")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if archived.data:
            latest_created_at = datetime.fromisoformat(
                archived.data[0]["created_at"].replace("Z", "+00:00")
            )
            if (now - latest_created_at).total_seconds() < FAQ_REPEAT_COOLDOWN_SECONDS:
                _faq_reply_log[key] = latest_created_at
                return False
    except Exception:
        # إذا كان الأرشيف غير متاح، تبقى حماية الذاكرة فعالة ولا نوقف الرد.
        logger.exception("Failed to check persistent FAQ reply history")

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
# حد أقصى صورة دفع وحدة لكل زبون خلال آخر 6 ساعات — الهدف منع إزعاج
# متكرر من زبون يرسل سكرين شوت كثير لنفس عملية الدفع. المفتاح هو
# customer_chat_id، والقيمة قائمة بأوقات وصول الصور المقبولة (تلقائياً
# فقط، مو صور accept اليدوي) خلال النافذة الحالية.
# ------------------------------------------------------------------
PHOTO_RATE_LIMIT_MAX = 1
PHOTO_RATE_LIMIT_WINDOW_HOURS = 6
_photo_timestamps: dict[int, list[datetime]] = {}
_processed_business_photo_keys: set[tuple[int, int, bool]] = set()


def has_recent_photo_marker(chat_id: int, sender_type: str) -> bool:
    """يفحص علامة الصورة الدائمة حتى لا يتصفر الحد بعد restart/deploy."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=PHOTO_RATE_LIMIT_WINDOW_HOURS)
    try:
        rows = (
            supabase.table("conversation_archive")
            .select("id")
            .eq("customer_chat_id", chat_id)
            .eq("sender_type", sender_type)
            .eq("image_description", "[photo_rate_limit_marker]")
            .gte("created_at", cutoff.isoformat())
            .limit(1)
            .execute()
            .data
            or []
        )
        return bool(rows)
    except Exception:
        logger.exception("Failed to check persistent photo rate limit")
        return False


def archive_photo_rate_limit_marker(chat_id: int, sender_type: str) -> None:
    """يسجل أن الصورة دخلت الفلو حتى يبقى حد الست ساعات محفوظاً."""
    archive_message(
        chat_id, None, None, sender_type=sender_type,
        image_description="[photo_rate_limit_marker]",
    )


def is_photo_within_rate_limit(customer_chat_id: int) -> bool:
    """
    يتحقق هل هذي الصورة ضمن حد صورة وحدة/6 ساعات لهذا الزبون. لو نعم،
    يسجل وقت وصولها ويرجع True (نحول الصورة عادي). لو تجاوز الحد،
    يرجع False بدون ما يسجل شي (الصورة تتجاهل بالكامل).
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=PHOTO_RATE_LIMIT_WINDOW_HOURS)

    if has_recent_photo_marker(customer_chat_id, "customer"):
        return False

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

    if has_recent_photo_marker(OWNER_USER_ID, "owner"):
        return False

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
        "تدلل، بالخدمة",
    ),
    (
        "chatgpt",
        [
            "chatgpt", "chat gpt", "chat", "جات", "چات", "جي بي تي",
            "شات جي بي تي", "شات", "چات جي بي تي", "شات جيبيتي",
            "جيبيتي", "gpt", "open ai", "openai", "اوبن اي اي",
            "چاتجيبيتي", "جاتي",
        ],
        "بلي موجود هاي الباقات المتوفرة Chat GPT\n"
        "اشتراك خاص شهر 25\n"
        "اشتراك خاص شهرين 35\n"
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
            "تليجرام", "التليجرام", "تلغرام", "التلغرام", "تليغرام",
            "التليغرام", "تليكرام", "التليكرام", "telegram", "تلي",
        ],
        "متوفر تلث اشهر ب 25 اما السة ب 35 والسنة ب55 الف",
    ),
]

SEEN_DELAY_SECONDS = 5       # فترة قبل ما البوت "يشوف" الرسالة (قبل علامة الصح الزرقاء)
PRE_TYPING_PAUSE_SECONDS = 3  # فترة صمت بعد علامة الصح، قبل ما يبدأ "يكتب..."
TYPING_DURATION_SECONDS = 6   # مدة ظهور "يكتب..." قبل إرسال الرد

LINK_PATTERN = re.compile(r"^/link\s+(\S+)$", re.IGNORECASE)
ADD_PATTERN = re.compile(r"^/addaccount\s+(\S+)\s+(\S+)(?:\s+(.+))?$", re.IGNORECASE)
PRIVATE_ADD_PATTERN = re.compile(r"^/addprivate\s+(\S+)(?:\s+(.+))?$", re.IGNORECASE)
PRIVATE_ADD_TARGET_PATTERN = re.compile(r"^/addprivate\s+(-?\d+)\s+(\S+)(?:\s+(.+))?$", re.IGNORECASE)
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

# مدد المنتجات الثابتة التي لا تحتاج سؤالاً إضافياً من الأونر.
# القيمة None تعني منتج دائم، وبالتالي لا ينشأ له تنبيه انتهاء.
FIXED_PRODUCT_DURATIONS = {
    "كانفا": 365,
    "انكي": None,
    "فرينوت": 365,
    "گودنوت": 365,
}
AMBOS_DURATION_OPTIONS = [("شهر", 30), ("3 أشهر", 90), ("6 أشهر", 180), ("سنة", 365)]

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
BTN_SUBSCRIPTION_REMINDER = "🔔 إضافة تنبيه اشتراك"
BTN_PERSONAL_REMINDER = "⏰ تذكير شخصي"
BTN_INSTAGRAM_SALE = "📲 تسجيل بيع إنستغرام"
BTN_INSTAGRAM_ADMIN = "📲 إدارة عمولات الإنستغرام"
BTN_BACK = "◀️ رجوع"
PAYMENT_METHOD_INPUT_TIMEOUT = timedelta(minutes=10)

MAIN_REPLY_KEYBOARD = ReplyKeyboardMarkup(
    [
        [KeyboardButton(BTN_EXPENSE), KeyboardButton(BTN_INCOME)],
        [KeyboardButton(BTN_ADD_ACCOUNT), KeyboardButton(BTN_STATS)],
        [KeyboardButton(BTN_DEBT), KeyboardButton(BTN_TEACH)],
        [KeyboardButton(BTN_CATALOG), KeyboardButton(BTN_PAYMENT_METHODS)],
        [KeyboardButton(BTN_CHATGPT_VAULT), KeyboardButton(BTN_SUBSCRIPTION_REMINDER)],
        [KeyboardButton(BTN_PERSONAL_REMINDER)],
        [KeyboardButton(BTN_INSTAGRAM_ADMIN)],
    ],
    resize_keyboard=True,
)

INSTAGRAM_MANAGER_KEYBOARD = ReplyKeyboardMarkup(
    [[KeyboardButton(BTN_INSTAGRAM_SALE)]], resize_keyboard=True
)


def is_instagram_manager(user_id: int | None) -> bool:
    return bool(INSTAGRAM_MANAGER_USER_ID and user_id == INSTAGRAM_MANAGER_USER_ID)


def instagram_product_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("جات", callback_data="ig_product_جات")],
        [InlineKeyboardButton("انكي", callback_data="ig_product_انكي"), InlineKeyboardButton("كانفا", callback_data="ig_product_كانفا")],
        [InlineKeyboardButton("تليجرام مميز", callback_data="ig_product_تليجرام")],
        [InlineKeyboardButton("✏️ منتج آخر", callback_data="ig_product_manual")],
    ])


def instagram_chat_type_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("خاص", callback_data="ig_type_خاص"), InlineKeyboardButton("مشترك", callback_data="ig_type_مشترك")],
    ])


def instagram_account_source_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("من عدنا", callback_data="ig_source_private_ours")],
        [InlineKeyboardButton("خاص من الزبون", callback_data="ig_source_private_customer")],
    ])


def instagram_duration_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("شهر", callback_data="ig_duration_1"), InlineKeyboardButton("شهرين", callback_data="ig_duration_2")],
    ])


def instagram_shared_accounts_keyboard() -> InlineKeyboardMarkup:
    accounts = get_chatgpt_shared_accounts()
    rows = []
    for account in accounts:
        used = get_instagram_shared_account_usage(account)
        capacity = min(int(account.get("capacity") or SHARED_CHATGPT_ACCOUNT_CAPACITY), SHARED_CHATGPT_ACCOUNT_CAPACITY)
        remaining = max(0, capacity - used)
        if remaining <= 0:
            continue
        label = account.get("email") or "حساب مشترك"
        account_id = account.get("id")
        rows.append([InlineKeyboardButton(f"{label} — متبقي {remaining}", callback_data=f"ig_shared_{account_id}")])
    if not rows:
        rows.append([InlineKeyboardButton("⚠️ لا توجد حسابات مشتركة", callback_data="ig_noop")])
    return InlineKeyboardMarkup(rows)


def instagram_private_accounts_keyboard() -> InlineKeyboardMarkup:
    accounts = get_instagram_private_accounts()
    rows = []
    for account in accounts:
        label = account.get("label") or "حساب خاص"
        rows.append([InlineKeyboardButton(label, callback_data=f"ig_private_{account['id']}")])
    if not rows:
        rows.append([InlineKeyboardButton("⚠️ لا توجد حسابات خاصة من عدنا", callback_data="ig_noop")])
    return InlineKeyboardMarkup(rows)


def instagram_wallet_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(method, callback_data=f"ig_wallet_{method}")] for method in PAYMENT_METHODS]
    return InlineKeyboardMarkup(rows)


def instagram_sale_prompt(state: dict) -> str:
    return (
        "📲 تسجيل بيع إنستغرام\n\n"
        f"المنتج: {state.get('product') or '—'}\n"
        f"نوع الجات: {state.get('chat_type') or '—'}\n"
        f"مصدر الحساب: {state.get('account_source_label') or '—'}\n"
        f"الحساب المستخدم: {state.get('account_display') or '—'}\n"
        f"المدة: {state.get('duration_months') or '—'} شهر\n"
        f"الحساب: {state.get('instagram_account') or '—'}\n"
        f"المحفظة: {state.get('wallet') or '—'}\n"
        f"المبلغ: {format_iqd(state['amount']) if state.get('amount') else '—'}"
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


def upsert_customer_contact(
    platform: str,
    external_id: str,
    display_name: str | None = None,
    username: str | None = None,
    chat_id: int | None = None,
    business_connection_id: str | None = None,
) -> bool:
    """يحفظ جهة اتصال بشكل دائم حتى لا يعتمد سجل العملاء على ذاكرة البوت."""
    if not platform or not external_id:
        return False
    now = datetime.now(timezone.utc).isoformat()
    payload = {
        "platform": platform,
        "external_id": str(external_id),
        "display_name": display_name,
        "username": username,
        "chat_id": chat_id,
        "business_connection_id": business_connection_id,
        "last_seen_at": now,
    }
    try:
        supabase.table("customer_contacts").upsert(
            payload,
            on_conflict="platform,external_id",
        ).execute()
        return True
    except Exception:
        logger.exception("Failed to upsert customer contact %s:%s", platform, external_id)
        return False


def payment_keyboard(methods: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"{'✅' if row['is_active'] else '⏸️'} {row['name']}", callback_data=f"pm_{row['id']}")] for row in methods]
    rows.append([InlineKeyboardButton("➕ إضافة طريقة دفع", callback_data="pm_add")])
    return InlineKeyboardMarkup(rows)


async def show_payment_methods(message) -> None:
    await message.reply_text("💳 طرق الدفع\nاختَر طريقة لتعديلها، أو أضف طريقة جديدة.", reply_markup=payment_keyboard(get_payment_methods()))


def get_chatgpt_shared_vault_summary() -> str:
    """يعرض إحصاء الخزينة بدون كشف الإيميلات أو كلمات المرور."""
    try:
        native_accounts = supabase.table("chatgpt_shared_accounts").select("id, capacity, is_active").execute().data or []
        active = [account for account in native_accounts if account.get("is_active")]
        legacy_accounts = supabase.table("totp_accounts").select("id, label, link_code").execute().data or []
        assignments = supabase.table("chatgpt_account_assignments").select("account_id").eq("status", "active").execute().data or []
        used_by_account: dict[str, int] = {}
        for assignment in assignments:
            account_id = assignment["account_id"]
            used_by_account[account_id] = used_by_account.get(account_id, 0) + 1
        seats = sum(
            max(0, min(int(account["capacity"]), SHARED_CHATGPT_ACCOUNT_CAPACITY) - used_by_account.get(account["id"], 0))
            for account in active
        )
        native_names = set()
        native_rows = supabase.table("chatgpt_shared_accounts").select("email").execute().data or []
        native_names = {(account.get("email") or "").strip().lower() for account in native_rows}
        legacy_only_count = sum(1 for account in legacy_accounts if (account.get("label") or account.get("link_code") or "").strip().lower() not in native_names)
        return f"🤖 خزينة ChatGPT المشتركة\nالحسابات المفعلة: {len(active) + legacy_only_count}\nالمقاعد المتاحة: {seats}"
    except Exception:
        logger.exception("Failed to load ChatGPT shared vault summary")
        return "⚠️ ما كدرت أقرأ خزينة حسابات ChatGPT."


def get_chatgpt_shared_accounts() -> list[dict]:
    """يرجع الحسابات الجديدة والقديمة بقائمة خزينة موحدة."""
    accounts: list[dict] = []
    try:
        accounts.extend(
            supabase.table("chatgpt_shared_accounts")
            .select("id, email, is_active, capacity")
            .eq("is_active", True)
            .order("created_at")
            .execute().data or []
        )
    except Exception:
        logger.exception("Failed to fetch shared ChatGPT accounts")
    try:
        legacy_accounts = (
            supabase.table("totp_accounts")
            .select("id, link_code, label")
            .order("created_at")
            .execute().data or []
        )
        existing_names = {str(account.get("email") or "").strip().lower() for account in accounts}
        for account in legacy_accounts:
            name = (account.get("label") or account.get("link_code") or "").strip()
            if name.lower() in existing_names:
                continue
            accounts.append({
                "id": f"legacy:{account['id']}",
                "legacy_id": account["id"],
                "email": name,
                "is_active": True,
                "capacity": SHARED_CHATGPT_ACCOUNT_CAPACITY,
                "source": "legacy",
            })
    except Exception:
        logger.exception("Failed to fetch legacy shared ChatGPT accounts")
    return accounts


def get_instagram_private_accounts() -> list[dict]:
    """Private accounts owned by us; customer-owned private accounts are never listed."""
    try:
        return (
            supabase.table("totp_accounts")
            .select("id, label, link_code")
            .like("link_code", "private_%")
            .order("created_at")
            .execute().data or []
        )
    except Exception:
        logger.exception("Failed to fetch private accounts for Instagram sales")
        return []


def get_instagram_account_secret(account_source: str, account_id: str) -> str | None:
    """Resolve only the selected account's TOTP secret for a code request."""
    try:
        if account_source == "shared":
            if str(account_id).startswith("legacy:"):
                rows = supabase.table("totp_accounts").select("secret").eq("id", str(account_id)[len("legacy:"):]).limit(1).execute().data or []
                return rows[0].get("secret") if rows else None
            rows = supabase.table("chatgpt_shared_accounts").select("totp_secret").eq("id", account_id).limit(1).execute().data or []
            return rows[0].get("totp_secret") if rows else None
        if account_source == "private_ours":
            rows = supabase.table("totp_accounts").select("secret").eq("id", account_id).like("link_code", "private_%").limit(1).execute().data or []
            return rows[0].get("secret") if rows else None
    except Exception:
        logger.exception("Failed to fetch selected Instagram account TOTP secret")
    return None


def build_shared_vault_accounts_keyboard(accounts: list[dict]) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        f"{account.get('email') or 'حساب بدون اسم'}",
        callback_data=(
            f"vault_legacy_account_{account['legacy_id']}"
            if account.get("source") == "legacy"
            else f"vault_account_{account['id']}"
        ),
    )] for account in accounts]
    rows.append([InlineKeyboardButton("➕ إضافة حساب مشترك", callback_data="vault_add_shared")])
    return InlineKeyboardMarkup(rows)


def build_shared_account_actions_keyboard(account_id: str, legacy: bool = False) -> InlineKeyboardMarkup:
    prefix = "vault_legacy_relogin_" if legacy else "vault_relogin_"
    cancel_prefix = "vault_cancel_customer_legacy_" if legacy else "vault_cancel_customer_native_"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📣 إرسال طلب إعادة تسجيل الدخول", callback_data=f"{prefix}{account_id}")],
        [InlineKeyboardButton("🗑️ إلغاء ارتباط زبون", callback_data=f"{cancel_prefix}{account_id}")],
        [InlineKeyboardButton("◀️ رجوع للحسابات", callback_data="vault_accounts")],
    ])


def get_latest_customer_payment(chat_id: int) -> tuple[int, list[str]] | None:
    sheet = get_google_sheet()
    if sheet is None:
        return None
    try:
        rows = sheet.get_all_values()
        for row_number in range(len(rows), 1, -1):
            row = rows[row_number - 1]
            if len(row) >= SHEET_COL_CHAT_ID and row[SHEET_COL_CHAT_ID - 1].strip() == str(chat_id):
                if row[SHEET_COL_TOTAL - 1].strip() and row[SHEET_COL_PAYMENTS - 1].strip() != "ملغاة":
                    return row_number, row
    except Exception:
        logger.exception("Failed to find customer payment for cancellation")
    return None


def parse_payment_vault_amounts(payments_text: str) -> dict[str, int]:
    amounts: dict[str, int] = {}
    for vault in VAULT_NAMES:
        match = re.search(rf"{re.escape(vault)}\s+([\d,]+)", payments_text or "")
        if match:
            amounts[vault] = int(match.group(1).replace(",", ""))
    return amounts


async def send_shared_account_relogin_notifications(context: ContextTypes.DEFAULT_TYPE, account_id: str) -> tuple[int, int]:
    """ينبه المشتركين الفعّالين المرتبطين بحساب واحد بعد تسجيل الخروج الجماعي."""
    try:
        accounts = supabase.table("chatgpt_shared_accounts").select("id, email").eq("id", account_id).limit(1).execute().data or []
        assignments = (
            supabase.table("chatgpt_account_assignments")
            .select("customer_chat_id")
            .eq("account_id", account_id).eq("status", "active")
            .execute().data or []
        )
    except Exception:
        logger.exception("Failed to find shared-account customers")
        return 0, 0

    if not accounts:
        return 0, 0
    account_name = accounts[0].get("email") or "حساب ChatGPT المشترك"
    customer_ids = sorted({row.get("customer_chat_id") for row in assignments if row.get("customer_chat_id") is not None})
    sent = failed = 0
    now = datetime.now(timezone.utc).isoformat()
    for customer_id in customer_ids:
        try:
            reminders = (
                supabase.table("subscription_reminders")
                .select("customer_chat_id, business_connection_id")
                .eq("customer_chat_id", customer_id).eq("status", "active")
                .eq("subscription_type", "shared").gt("expires_at", now)
                .limit(1).execute().data or []
            )
            if not reminders:
                continue
            reminder = reminders[0]
            text = (
                "السلام عليكم،\n\n"
                "صار أكو تسريب بالحساب ومدتكم محفوظة، بس أرجع سجّل لو سمحت واطلب كود حتى يندز مباشرة إن شاء الله."
            )
            send_kwargs = {"chat_id": customer_id, "text": text}
            if reminder.get("business_connection_id"):
                send_kwargs["business_connection_id"] = reminder["business_connection_id"]
            await context.bot.send_message(**send_kwargs)
            sent += 1
        except Exception:
            failed += 1
            logger.exception("Failed to send shared-account relogin notice to %s", customer_id)
    return sent, failed


async def send_legacy_shared_account_relogin_notifications(
    context: ContextTypes.DEFAULT_TYPE, account_id: str, account_name: str,
) -> tuple[int, int]:
    """نفس التنبيه للحسابات القديمة المرتبطة عبر totp_links."""
    customer_ids = get_customers_for_account(account_id)
    sent = failed = 0
    now = datetime.now(timezone.utc).isoformat()
    for customer_id in sorted(set(customer_ids)):
        try:
            reminders = (
                supabase.table("subscription_reminders")
                .select("customer_chat_id, business_connection_id")
                .eq("customer_chat_id", customer_id).eq("status", "active")
                .eq("subscription_type", "shared").gt("expires_at", now)
                .limit(1).execute().data or []
            )
            if not reminders:
                continue
            send_kwargs = {
                "chat_id": customer_id,
                "text": (
                    "السلام عليكم،\n\n"
                    "صار أكو تسريب بالحساب ومدتكم محفوظة، بس أرجع سجّل لو سمحت واطلب كود حتى يندز مباشرة إن شاء الله."
                ),
            }
            if reminders[0].get("business_connection_id"):
                send_kwargs["business_connection_id"] = reminders[0]["business_connection_id"]
            await context.bot.send_message(**send_kwargs)
            sent += 1
        except Exception:
            failed += 1
            logger.exception("Failed to send legacy shared-account notice to %s", customer_id)
    return sent, failed


async def show_chatgpt_shared_vault(message) -> None:
    accounts = get_chatgpt_shared_accounts()
    await message.reply_text(
        get_chatgpt_shared_vault_summary(),
        reply_markup=build_shared_vault_accounts_keyboard(accounts) if accounts else InlineKeyboardMarkup([[InlineKeyboardButton("➕ إضافة حساب مشترك", callback_data="vault_add_shared")]]),
    )


async def handle_chatgpt_vault_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.from_user.id != OWNER_USER_ID:
        return
    await query.answer()
    if query.data.startswith("vault_cancel_customer_"):
        remainder = query.data[len("vault_cancel_customer_"):]
        source, account_id = remainder.split("_", 1)
        context.user_data["pending_cancel_customer"] = {"source": source, "account_id": account_id}
        await query.edit_message_text(
            "أرسل chat_id للزبون المراد إلغاء ارتباطه كـ رد على هذه الرسالة.\n"
            "راح أعرض لك تفاصيل الدفع قبل أي إلغاء."
        )
        return
    if query.data.startswith("vault_cancel_confirm_"):
        parts = query.data[len("vault_cancel_confirm_"):].split("_", 3)
        if len(parts) != 4:
            await query.edit_message_text("⚠️ بيانات الإلغاء غير صحيحة.")
            return
        source, account_id, chat_id_text, row_text = parts
        try:
            chat_id, row_number = int(chat_id_text), int(row_text)
        except ValueError:
            await query.edit_message_text("⚠️ بيانات الزبون غير صحيحة.")
            return
        sheet = get_google_sheet()
        payment_row = None
        if sheet is not None:
            try:
                rows = sheet.get_all_values()
                if 1 < row_number <= len(rows):
                    payment_row = rows[row_number - 1]
            except Exception:
                logger.exception("Failed to read payment before cancellation")
        if payment_row is None or len(payment_row) < SHEET_COL_CHAT_ID or payment_row[SHEET_COL_CHAT_ID - 1].strip() != str(chat_id):
            await query.edit_message_text("⚠️ عملية الدفع غير موجودة أو تغيرت. أعد المحاولة.")
            return
        vault_amounts = parse_payment_vault_amounts(payment_row[SHEET_COL_PAYMENTS - 1])
        changed = []
        for vault, amount in vault_amounts.items():
            if not adjust_vault_balance(vault, -amount):
                for previous_vault, previous_amount in changed:
                    adjust_vault_balance(previous_vault, previous_amount)
                await query.edit_message_text("⚠️ تعذر تعديل الخزنة، لم يتم الإلغاء.")
                return
            changed.append((vault, amount))
        try:
            sheet.update_cell(row_number, SHEET_COL_TOTAL, "")
            sheet.update_cell(row_number, SHEET_COL_PAYMENTS, "ملغاة")
            sheet.update_cell(row_number, SHEET_COL_CHATGPT_ACCOUNT, "")
            if source == "native":
                supabase.table("chatgpt_account_assignments").update({"status": "cancelled"}).eq("account_id", account_id).eq("customer_chat_id", chat_id).eq("status", "active").execute()
            else:
                supabase.table("totp_links").delete().eq("account_id", account_id).eq("chat_id", chat_id).execute()
            supabase.table("subscription_reminders").update({"status": "cancelled"}).eq("customer_chat_id", chat_id).eq("subscription_type", "shared").eq("status", "active").execute()
            context.user_data.pop("pending_cancel_customer", None)
            await query.edit_message_text(
                f"✅ تم إلغاء ارتباط الزبون وإلغاء الدفع.\n"
                f"المبلغ المرتجع للخزنة: {', '.join(f'{v} {a}' for v, a in vault_amounts.items()) or '—'}"
            )
        except Exception:
            for vault, amount in changed:
                adjust_vault_balance(vault, amount)
            logger.exception("Failed to complete customer cancellation")
            await query.edit_message_text("⚠️ تعذر إكمال الإلغاء، وتمت محاولة إرجاع تعديل الخزنة.")
        return
    if query.data.startswith("vault_cancel_abort_"):
        context.user_data.pop("pending_cancel_customer", None)
        await query.edit_message_text("تم إلغاء العملية بدون أي تغيير.")
        return
    if query.data == "vault_accounts":
        accounts = get_chatgpt_shared_accounts()
        await query.edit_message_text(
            get_chatgpt_shared_vault_summary(),
            reply_markup=build_shared_vault_accounts_keyboard(accounts),
        )
        return
    if query.data.startswith("vault_account_"):
        account_id = query.data[len("vault_account_"):]
        accounts = [account for account in get_chatgpt_shared_accounts() if str(account.get("id")) == account_id]
        if not accounts:
            await query.edit_message_text("الحساب غير موجود أو متوقف.")
            return
        await query.edit_message_text(
            f"الحساب المشترك:\n{accounts[0].get('email') or '—'}\n\n"
            "بعد ما تسوي تسجيل خروج للكل من هذا الحساب، اضغط الزر حتى ننبه المشتركين الفعّالين.",
            reply_markup=build_shared_account_actions_keyboard(account_id),
        )
        return
    if query.data.startswith("vault_legacy_account_"):
        account_id = query.data[len("vault_legacy_account_"):]
        accounts = [account for account in get_chatgpt_shared_accounts() if str(account.get("legacy_id")) == account_id]
        if not accounts:
            await query.edit_message_text("الحساب غير موجود.")
            return
        await query.edit_message_text(
            f"الحساب المشترك:\n{accounts[0].get('email') or '—'}\n\n"
            "هذا حساب مضاف بالطريقة القديمة /addaccount، لكنه صار ضمن نفس خزينة الحسابات.",
            reply_markup=build_shared_account_actions_keyboard(account_id, legacy=True),
        )
        return
    if query.data.startswith("vault_relogin_"):
        account_id = query.data[len("vault_relogin_"):]
        sent, failed = await send_shared_account_relogin_notifications(context, account_id)
        await query.edit_message_text(
            f"✅ تم إرسال الرسالة إلى {sent} مشترك فعّال."
            + (f"\n⚠️ فشل الإرسال إلى {failed}." if failed else "")
            + "\n\nإذا تريد ترسلها مرة ثانية، افتح الحساب واضغط الزر من جديد.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ رجوع للحسابات", callback_data="vault_accounts")]]),
        )
        return
    if query.data.startswith("vault_legacy_relogin_"):
        account_id = query.data[len("vault_legacy_relogin_"):]
        accounts = [account for account in get_chatgpt_shared_accounts() if str(account.get("legacy_id")) == account_id]
        account_name = accounts[0].get("email") if accounts else "الحساب المشترك"
        sent, failed = await send_legacy_shared_account_relogin_notifications(context, account_id, account_name)
        await query.edit_message_text(
            f"✅ تم إرسال الرسالة إلى {sent} مشترك فعّال."
            + (f"\n⚠️ فشل الإرسال إلى {failed}." if failed else ""),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ رجوع للحسابات", callback_data="vault_accounts")]]),
        )
        return
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
                "totp_secret": secret, "capacity": SHARED_CHATGPT_ACCOUNT_CAPACITY,
            }).execute()
        except Exception:
            logger.exception("Failed to add shared ChatGPT account")
            await message.reply_text("⚠️ ما انحفظ الحساب. تأكد أن الإيميل مو مضاف سابقاً.")
            return True
        try:
            # نخلي الحساب الجديد معروفاً أيضاً لمسار /link القديم.
            legacy_exists = supabase.table("totp_accounts").select("id").eq("label", state["email"]).limit(1).execute().data or []
            if not legacy_exists:
                supabase.table("totp_accounts").insert({
                    "link_code": "vault_" + uuid.uuid4().hex[:12],
                    "secret": secret,
                    "label": state["email"],
                }).execute()
        except Exception:
            logger.exception("Failed to mirror new shared account into legacy accounts")
        context.user_data.pop("pending_shared_account", None)
        await message.reply_text("✅ تم حفظ الحساب المشترك. سعته 3 أشخاص.")
        await show_chatgpt_shared_vault(message)
        return True
    return False


async def handle_cancel_customer_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    state = context.user_data.get("pending_cancel_customer")
    message = update.message
    if not state or not message or not message.text or not message.reply_to_message:
        return False
    try:
        chat_id = int(message.text.strip())
    except ValueError:
        await message.reply_text("أرسل chat_id صحيح كرقم فقط.")
        return True
    payment = get_latest_customer_payment(chat_id)
    if payment is None:
        await message.reply_text("ما لكيت دفعة مسجلة لهذا الزبون حتى ألغيها.")
        return True
    row_number, row = payment
    context.user_data["pending_cancel_customer"]["chat_id"] = chat_id
    await message.reply_text(
        f"⚠️ تأكيد الإلغاء\n\nالزبون: {chat_id}\n"
        f"المنتج: {row[3] if len(row) > 3 else '—'}\n"
        f"المبلغ: {row[1]}\nطرق الدفع: {row[2]}\n\n"
        "سيُفك الربط، يُلغى الدفع، ويُخصم المبلغ من الخزنة. هل تؤكد؟",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ نعم، ألغِ الربط والدفع", callback_data=f"vault_cancel_confirm_{state['source']}_{state['account_id']}_{chat_id}_{row_number}")],
            [InlineKeyboardButton("❌ إلغاء", callback_data=f"vault_cancel_abort_{chat_id}")],
        ]),
    )
    return True


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


def build_ambos_duration_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=f"pay_ambos_duration_{days}")]
        for label, days in AMBOS_DURATION_OPTIONS
    ])


def duration_to_days(duration: str | None) -> int | None:
    """يحوّل مدة الكاتالوج إلى أيام تقريبية ثابتة للتنبيه."""
    text = normalize_style_text(duration or "")
    joined = " ".join(text)
    if any(word in text for word in {"سنه", "سنة", "عام", "سنه"}):
        return 365
    match = re.search(r"(\d+)\s*(?:شهر|اشهر|أشهر)", joined)
    if match:
        return int(match.group(1)) * 30
    if "شهرين" in text:
        return 60
    if "شهر" in text:
        return 30
    match = re.search(r"(\d+)\s*(?:يوم|ايام|أيام)", joined)
    return int(match.group(1)) if match else None


def catalog_product_for_payment_name(name: str) -> dict | None:
    normalized_name = " ".join(normalize_style_text(name))
    for product in get_catalog_products():
        candidates = [product.get("name") or "", *(product.get("aliases") or [])]
        if any(normalized_name == " ".join(normalize_style_text(str(candidate))) for candidate in candidates):
            return product
    return None


def prepare_generic_subscription(state: dict) -> list[dict]:
    """يملأ الباقة تلقائياً إن كانت وحيدة، ويرجع الباقات التي تحتاج اختياراً."""
    product_name = state.get("product") or ""
    if product_name in FIXED_PRODUCT_DURATIONS:
        state["duration_days"] = FIXED_PRODUCT_DURATIONS[product_name]
        state["reminder_disabled"] = state["duration_days"] is None
        return []
    if product_name == "امبوس":
        return [{"static_duration": True, "name": label, "duration": label, "days": days} for label, days in AMBOS_DURATION_OPTIONS]
    product = catalog_product_for_payment_name(state.get("product") or "")
    if not product or is_chatgpt_product(product):
        return []
    plans = [plan for plan in get_catalog_plans(product["id"]) if plan.get("is_active") and duration_to_days(plan.get("duration"))]
    if len(plans) == 1:
        plan = plans[0]
        state.update({
            "plan_id": plan["id"], "plan_name": plan["name"],
            "plan_duration": plan.get("duration"),
            "duration_days": duration_to_days(plan.get("duration")),
        })
        return []
    return plans


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


def build_subscription_type_keyboard() -> InlineKeyboardMarkup:
    """اختيار مؤكد من الأونر بعد دفع جات؛ لا نعتمد على تخمين نص الزبون."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("خاص شهر", callback_data="pay_subscription_private_1"),
            InlineKeyboardButton("مشترك شهر", callback_data="pay_subscription_shared_1"),
        ],
        [
            InlineKeyboardButton("خاص شهرين", callback_data="pay_subscription_private_2"),
            InlineKeyboardButton("مشترك شهرين", callback_data="pay_subscription_shared_2"),
        ],
    ])


def build_subscription_plan_keyboard(plans: list[dict]) -> InlineKeyboardMarkup:
    """يختار الأونر مدة/باقة المنتج العام من الكاتالوج قبل تثبيت الدفع."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"{plan['name']} — {plan['duration'] or 'بدون مدة'}",
            callback_data=f"pay_plan_{plan['id']}",
        )]
        for plan in plans
    ])


def build_link_debt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ نعم، دين", callback_data="linkdebt_yes"),
        InlineKeyboardButton("❌ لا، مو دين", callback_data="linkdebt_no"),
    ]])


def build_link_debt_plan_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("خاص شهر", callback_data="linkplan_private_1"),
            InlineKeyboardButton("مشترك شهر", callback_data="linkplan_shared_1"),
        ],
        [
            InlineKeyboardButton("خاص شهرين", callback_data="linkplan_private_2"),
            InlineKeyboardButton("مشترك شهرين", callback_data="linkplan_shared_2"),
        ],
    ])


def build_manual_subscription_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("خاص شهر", callback_data="subrem_private_1"),
            InlineKeyboardButton("مشترك شهر", callback_data="subrem_shared_1"),
        ],
        [
            InlineKeyboardButton("خاص شهرين", callback_data="subrem_private_2"),
            InlineKeyboardButton("مشترك شهرين", callback_data="subrem_shared_2"),
        ],
    ])


def parse_personal_reminder_input(text: str, now: datetime | None = None) -> tuple[datetime, str] | None:
    """Parse `YYYY-MM-DD HH:MM | purpose` (or the common DD/MM/YYYY form)."""
    parts = [part.strip() for part in (text or "").split("|", 1)]
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    raw_datetime, purpose = parts
    parsed = None
    for fmt in ("%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M"):
        try:
            parsed = datetime.strptime(raw_datetime, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        return None
    due_at = parsed.replace(tzinfo=timezone(timedelta(hours=3)))
    current = now or datetime.now(timezone.utc)
    if due_at.astimezone(timezone.utc) <= current.astimezone(timezone.utc):
        return None
    return due_at, purpose


def personal_reminder_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "✅ وصلني التذكير", callback_data=f"personal_reminder_ack_{reminder_id}"
    )]])


def format_payment_summary(state: dict) -> str:
    """يبني نص الملخص المعروض فوق الأزرار أثناء تسجيل الدفع."""
    lines = ["تسجيل عملية دفع"]
    customer_line = state["customer_name"]
    if state.get("customer_username"):
        customer_line += f" (@{state['customer_username']})"
    lines.append(f"الزبون: {customer_line}")

    product = state.get("product")
    lines.append(f"المنتج: {product if product else '— لم يُختر بعد —'}")

    if product == CHATGPT_PRODUCT_NAME:
        subscription_type = state.get("subscription_type")
        duration_months = state.get("duration_months")
        if subscription_type and duration_months:
            type_text = "خاص" if subscription_type == "private" else "مشترك"
            duration_text = "شهر" if duration_months == 1 else "شهرين"
            lines.append(f"الاشتراك: {type_text} {duration_text}")
        else:
            lines.append("الاشتراك: — اختره عند تثبيت العملية —")
    elif state.get("plan_name"):
        lines.append(f"الباقة/المدة: {state['plan_name']} — {state.get('plan_duration') or '—'}")

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

        if len(row) >= SHEET_COL_PAYMENTS and row[SHEET_COL_PAYMENTS - 1].strip() == "ملغاة":
            continue

        if total_empty or payments_empty or account_empty:
            return i + 1  # gspread صفوف 1-indexed

    return None


def append_payment_row(state: dict, force_new: bool = False) -> bool:
    """
    يضيف سطر جديد بـ Google Sheet لعملية دفع مكتملة، أو يكمل سطر ناقص
    موجود لنفس الزبون لو المنتج جات (خلال آخر أسبوع). يرجع True لو نجح
    الحفظ، False لو فشل. force_new مخصص لتسديد الدين حتى ينحسب كدخل
    جديد بتاريخ التسديد، ولا يكتب فوق سطر الدين/العملية الأصلية.
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
        if not force_new and product == CHATGPT_PRODUCT_NAME and chat_id is not None:
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


def save_subscription_reminder(state: dict) -> bool:
    """يحفظ تنبيه انتهاء أي منتج ذي مدة من الكاتالوج أو ChatGPT."""
    subscription_type = state.get("subscription_type")
    duration_months = state.get("duration_months")
    chat_id = state.get("customer_chat_id")
    duration_days = state.get("duration_days")
    feedback_only = bool(state.get("reminder_disabled"))
    if duration_days is None and duration_months in {1, 2}:
        duration_days = 30 * duration_months
    if feedback_only:
        duration_days = 1
    if not chat_id or not duration_days or duration_days <= 0:
        return False

    now = datetime.now(timezone.utc)
    try:
        supabase.table("subscription_reminders").insert({
            "customer_chat_id": chat_id,
            "business_connection_id": state.get("business_connection_id"),
            "customer_name": state["customer_name"],
            "customer_username": state.get("customer_username"),
            "product_name": state.get("product") or state.get("product_name") or "غير محدد",
            "plan_name": state.get("plan_name"),
            "plan_duration": state.get("plan_duration"),
            "subscription_type": subscription_type or "general",
            "duration_months": duration_months,
            "duration_days": duration_days,
            "feedback_only": feedback_only,
            "feedback_status": "scheduled" if feedback_only else "none",
            "is_debt": bool(state.get("is_debt", False)),
            "started_at": now.isoformat(),
            "expires_at": (now + timedelta(days=duration_days)).isoformat(),
        }).execute()
        return True
    except Exception:
        logger.exception("Failed to save subscription reminder")
        return False


def save_personal_reminder(due_at: datetime, purpose: str) -> bool:
    try:
        supabase.table("personal_reminders").insert({
            "owner_user_id": OWNER_USER_ID,
            "remind_at": due_at.astimezone(timezone.utc).isoformat(),
            "purpose": purpose,
        }).execute()
        return True
    except Exception:
        logger.exception("Failed to save personal reminder")
        return False


async def check_personal_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Notify due personal reminders and repeat them until acknowledged."""
    now = datetime.now(timezone.utc)
    repeat_after = now - timedelta(minutes=15)
    try:
        reminders = (supabase.table("personal_reminders")
            .select("id, remind_at, purpose, last_notified_at, notification_count")
            .eq("owner_user_id", OWNER_USER_ID).eq("status", "pending")
            .lte("remind_at", now.isoformat()).execute().data or [])
    except Exception:
        logger.exception("Failed to load personal reminders")
        return

    for reminder in reminders:
        last_notified = reminder.get("last_notified_at")
        if last_notified:
            try:
                if datetime.fromisoformat(last_notified.replace("Z", "+00:00")) > repeat_after:
                    continue
            except ValueError:
                pass
        try:
            await context.bot.send_message(
                chat_id=OWNER_USER_ID,
                text=f"⏰ تذكير\n\n{reminder['purpose']}\n\nيبقى هذا التذكير يتكرر كل 15 دقيقة إلى أن تأكد الاستلام.",
                reply_markup=personal_reminder_keyboard(reminder["id"]),
            )
            supabase.table("personal_reminders").update({
                "last_notified_at": now.isoformat(),
                "notification_count": (reminder.get("notification_count") or 0) + 1,
            }).eq("id", reminder["id"]).eq("status", "pending").execute()
        except Exception:
            logger.exception("Failed to send personal reminder %s", reminder.get("id"))


async def handle_personal_reminder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.from_user.id != OWNER_USER_ID:
        return
    try:
        reminder_id = int(query.data.rsplit("_", 1)[1])
    except (AttributeError, ValueError, TypeError):
        await query.answer("التذكير غير صحيح.", show_alert=True)
        return
    try:
        result = (supabase.table("personal_reminders")
            .update({"status": "acknowledged", "acknowledged_at": datetime.now(timezone.utc).isoformat()})
            .eq("id", reminder_id).eq("owner_user_id", OWNER_USER_ID).eq("status", "pending")
            .execute())
        if not result.data:
            await query.answer("هذا التذكير مؤكد مسبقاً.", show_alert=True)
            return
        await query.answer("تم تأكيد الاستلام.")
        await query.edit_message_text(query.message.text + "\n\n✅ تم تأكيد الاستلام.")
    except Exception:
        logger.exception("Failed to acknowledge personal reminder %s", reminder_id)
        await query.answer("تعذر تأكيد التذكير، حاول مرة ثانية.", show_alert=True)
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

    instagram_rows = get_instagram_sales_rows_in_period("today")
    if instagram_rows is not None:
        total_today += sum(parse_amount(row[6]) or 0 for row in instagram_rows if len(row) > 6)
    instagram_rows = get_instagram_sales_rows_in_period("week")
    if instagram_rows is not None:
        total_week += sum(parse_amount(row[6]) or 0 for row in instagram_rows if len(row) > 6)
    instagram_rows = get_instagram_sales_rows_in_period("month")
    if instagram_rows is not None:
        total_month += sum(parse_amount(row[6]) or 0 for row in instagram_rows if len(row) > 6)

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

STATS_TIMEZONE = timezone(timedelta(hours=3))


def parse_stats_datetime(value: str) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=STATS_TIMEZONE)
        except ValueError:
            continue
    raise ValueError(f"Unrecognized date/time: {value!r}")


def parse_stats_period(period_key: str) -> tuple[datetime, datetime] | None:
    """
    يحول مفتاح فترة (جاهز أو نص يدوي) إلى (start_date, end_date) شامل
    الطرفين. يرجع None لو الفترة "كل الوقت" (بدون حدود)، أو يرمي
    ValueError لو نص يدوي غير مفهوم.
    """
    now = datetime.now(STATS_TIMEZONE)
    today = now.date()
    day_start = datetime.combine(today, datetime.min.time(), tzinfo=STATS_TIMEZONE)
    day_end = datetime.combine(today, datetime.max.time(), tzinfo=STATS_TIMEZONE)

    if period_key == "today":
        return day_start, day_end
    if period_key == "week":
        week_start = today - timedelta(days=today.weekday())
        return datetime.combine(week_start, datetime.min.time(), tzinfo=STATS_TIMEZONE), day_end
    if period_key == "month":
        month_start = today.replace(day=1)
        return datetime.combine(month_start, datetime.min.time(), tzinfo=STATS_TIMEZONE), day_end
    if period_key == "all":
        return None

    # نطاق يدوي: YYYY-MM-DD HH:MM إلى YYYY-MM-DD HH:MM
    range_parts = re.split(r"\s+(?:إلى|الى|to)\s+", period_key, maxsplit=1, flags=re.IGNORECASE)
    if len(range_parts) == 2:
        start = parse_stats_datetime(range_parts[0])
        end = parse_stats_datetime(range_parts[1])
        if end < start:
            raise ValueError("نهاية الفترة أقدم من بدايتها")
        return start, end

    # نص يدوي: YYYY-MM-DD (يوم محدد) أو YYYY-MM (شهر محدد)
    if len(period_key) == 10:
        d = datetime.strptime(period_key, "%Y-%m-%d").date()
        return datetime.combine(d, datetime.min.time(), tzinfo=STATS_TIMEZONE), datetime.combine(d, datetime.max.time(), tzinfo=STATS_TIMEZONE)
    if len(period_key) == 7:
        d = datetime.strptime(period_key + "-01", "%Y-%m-%d").date()
        if d.month == 12:
            next_month = d.replace(year=d.year + 1, month=1)
        else:
            next_month = d.replace(month=d.month + 1)
        month_end = next_month - timedelta(days=1)
        return datetime.combine(d, datetime.min.time(), tzinfo=STATS_TIMEZONE), datetime.combine(month_end, datetime.max.time(), tzinfo=STATS_TIMEZONE)

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
            row_date = parse_stats_datetime(row[0])
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
            row_date = parse_stats_datetime(row[0])
        except (ValueError, IndexError):
            continue

        if date_range is not None:
            start, end = date_range
            if not (start <= row_date <= end):
                continue

        filtered.append(row)

    return filtered


def get_instagram_sales_rows_in_period(period_key: str) -> list[list[str]] | None:
    """يرجع مبيعات إنستغرام المؤكدة ضمن الفترة، حسب وقت البيع."""
    sheet = get_instagram_sales_worksheet()
    if sheet is None:
        return None
    try:
        rows = sheet.get_all_values()
    except Exception:
        logger.exception("Failed to read Instagram sales for stats")
        return None
    date_range = parse_stats_period(period_key)
    filtered = []
    for row in rows[1:]:
        if len(row) < 10 or row[9].strip() != "مؤكدة":
            continue
        try:
            row_datetime = parse_stats_datetime(row[1])
        except (ValueError, IndexError):
            continue
        if date_range is not None and not (date_range[0] <= row_datetime <= date_range[1]):
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
    instagram_rows = get_instagram_sales_rows_in_period(period_key)

    if payment_rows is None or expense_rows is None or instagram_rows is None:
        return "تعذر الاتصال بـ Google Sheet — تأكد من إعدادات الاتصال."

    total_income = 0
    for row in payment_rows:
        if len(row) >= 2 and row[1].strip():
            try:
                total_income += int(float(row[1]))
            except ValueError:
                continue

    instagram_income = sum(parse_amount(row[6]) or 0 for row in instagram_rows if len(row) > 6)
    total_income += instagram_income

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
        f"منه مبيعات إنستغرام: {instagram_income}\n"
        f"المصروف: {total_expense}\n"
        f"الصافي: {net}"
    )


def calculate_product_breakdown(period_key: str) -> str:
    """يحسب دخل + عدد عمليات لكل منتج بفترة معينة. يرجع نص جاهز للعرض."""
    payment_rows = get_payment_rows_in_period(period_key)
    instagram_rows = get_instagram_sales_rows_in_period(period_key)
    if payment_rows is None or instagram_rows is None:
        return "تعذر الاتصال بـ Google Sheet — تأكد من إعدادات الاتصال."

    label = format_period_label(period_key)
    if not payment_rows and not instagram_rows:
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
        if amount <= 0:
            continue

        product_totals[product] = product_totals.get(product, 0) + amount
        product_counts[product] = product_counts.get(product, 0) + 1

    for row in instagram_rows:
        if len(row) < 7:
            continue
        product = row[3].strip() or "غير محدد"
        amount = parse_amount(row[6]) or 0
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

    shared_count = len(get_shared_accounts_list())

    return private_count, shared_count


def get_shared_accounts_list() -> list[dict]:
    """Return native and legacy shared accounts for owner statistics."""
    accounts: list[dict] = []
    try:
        native = supabase.table("chatgpt_shared_accounts").select("id, email, capacity").eq("is_active", True).order("created_at").execute().data or []
        accounts.extend({"id": row["id"], "link_code": "", "label": row.get("email"), "capacity": row.get("capacity", SHARED_CHATGPT_ACCOUNT_CAPACITY), "source": "native"} for row in native)
    except Exception:
        logger.exception("Failed to fetch native shared accounts list")
    try:
        legacy = supabase.table("totp_accounts").select("id, link_code, label").not_.like("link_code", "private_%").execute().data or []
        accounts.extend({**row, "source": "legacy", "capacity": SHARED_CHATGPT_ACCOUNT_CAPACITY} for row in legacy)
    except Exception:
        logger.exception("Failed to fetch legacy shared accounts list")
    return accounts


def get_instagram_account_sales(account_id: str) -> list[list[str]]:
    """Return Instagram sales linked to a specific internal account."""
    sheet = get_instagram_sales_worksheet()
    if sheet is None:
        return []
    try:
        return [
            row for row in sheet.get_all_values()[1:]
            if len(row) >= 16 and row[15].strip() == str(account_id) and row[9].strip() == "مؤكدة"
        ]
    except Exception:
        logger.exception("Failed to fetch Instagram sales for account %s", account_id)
        return []


def get_instagram_shared_account_usage(account: dict) -> int:
    """Count active Telegram assignments plus confirmed Instagram seats."""
    account_id = str(account.get("id") or "")
    try:
        if str(account_id).startswith("legacy:"):
            telegram_used = len(get_customers_for_account(account_id[len("legacy:"):]))
        else:
            assignments = (
                supabase.table("chatgpt_account_assignments")
                .select("id")
                .eq("account_id", account_id)
                .eq("status", "active")
                .execute().data or []
            )
            telegram_used = len(assignments)
        return telegram_used + len(get_instagram_account_sales(account_id))
    except Exception:
        logger.exception("Failed to calculate available seats for account %s", account_id)
        return 0


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
        prefix = "stats_native_account_" if acc.get("source") == "native" else "stats_account_"
        rows.append([InlineKeyboardButton(label, callback_data=f"{prefix}{acc['id']}")])
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
    "recipient_match (true/false/null), receipt_datetime (صيغة YYYY-MM-DD HH:MM أو null), "
    "recency (recent/old/not_visible), confidence (0-100), "
    "reason (نص عربي قصير).\n"
    "تعتبر الصورة وصل دفع فقط إذا ظهر تطبيق/خدمة دفع مع مبلغ وعملية تحويل أو نجاح. "
    "صورة التسجيل، الموقع، المحادثة، أو أي شاشة غير دفع = false. لا تعتبر الوقت "
    "حديثاً إلا إذا ظهر تاريخ/وقت يتوافق مع اليوم أو آخر ساعتين بحسب وقت بغداد المعطى. "
    "انسخ التاريخ والوقت الظاهرين كما هما إلى receipt_datetime بعد تحويلهما للصيغة المطلوبة، ولا تكتب فرقاً زمنياً تقديرياً."
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
        # يقبل المبلغ الذي يساوي سعر الباقة أو يزيد عليه، ويرفض الناقص فقط.
        amount_matches = is_paid_amount_sufficient(expected_amount, amount)
        baghdad_now = datetime.now(timezone(timedelta(hours=3)))
        computed_recency = classify_receipt_recency(result.get("receipt_datetime"), baghdad_now)
        if computed_recency is not None:
            result["recency"] = computed_recency

        approved = (
            result.get("is_payment_receipt") is True
            and expected_amount is not None
            and amount_matches
            and result.get("recipient_match") is True
            and result.get("recency") == "recent"
            and int(result.get("confidence") or 0) >= 85
        )
        if approved:
            result["decision"] = "approved"
        elif (
            result.get("is_payment_receipt") is False
            or result.get("recency") in {"old", "future"}
            or (expected_amount is not None and amount is not None and not amount_matches)
            or result.get("recipient_match") is False
        ):
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
    "اختَر payment_methods عندما يطلب طرق الدفع بعد اختيار باقة، وpayment_next_step "
    "عندما يسأل شنو يسوي بعدها أو هل يدفع أولاً. اختَر "
    "request_plan_choice إذا يريد الشراء ولم يحدد باقة. اختَر "
    "clarify_plan_type إذا حدد المدة فقط، وclarify_plan_duration إذا حدد "
    "خاص أو مشترك فقط. اختَر code_request فقط بعد تسليم الحساب، واختَر "
    "workspace_guidance لسؤال Personal أو Workspace. اختَر "
    "selected_plan_price إذا سأل عن المبلغ بعد أن اختار باقة. اختَر "
    "request_payment_proof فقط إذا قال حوّلت/دفعت ولم يرسل صورة. اختَر "
    "payment_under_review عند إرسال صورة تحويل. اختَر request_support_screenshot "
    "لمشكلة تحتاج صورة. اختَر support_pending عندما يظهر من السياق أن عنده "
    "مشكلة تقنية أو اشتراك قائم—even إذا رسالته الحالية فقط مثل: «تگدر "
    "تساعدني؟» أو «هلو» أو تفصيل قصير للمشكلة. support_pending يعني لا "
    "نرسل أي رد حالياً ونحافظ على سياق الدعم. اختَر no_reply للكلام الذي "
    "لا يحتاج جواب ولا يغيّر الحالة. اختَر handoff للحالة الحساسة أو غير "
    "المؤكدة، وclarify إذا الكلام غير واضح."
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


def find_catalog_product_context(text: str) -> dict | None:
    """يلتقط المنتج من اسمه أو الكلمات التي أضافها الأونر في الكاتالوج."""
    words = normalize_style_text(text)
    for product in get_catalog_products():
        if not product.get("is_active"):
            continue
        product_terms = normalize_style_text(product.get("name") or "")
        for alias in product.get("aliases") or []:
            product_terms.update(normalize_style_text(str(alias)))
        if words & product_terms:
            return product
    return None


def find_selected_catalog_plan(product: dict, text: str) -> dict | None:
    """يقبل اختيار باقة غير ChatGPT فقط عندما يذكر اسمها أو سعرها بوضوح."""
    words = normalize_style_text(text)
    plans = [plan for plan in get_catalog_plans(product["id"]) if plan.get("is_active")]
    matches: list[dict] = []
    for plan in plans:
        plan_words = normalize_style_text(plan.get("name") or "")
        # كلمات مثل «اشتراك» و«باقة» لا تكفي لاختيار منتج بالنيابة عن الزبون.
        meaningful_words = plan_words - {"اشتراك", "باقة", "الباقه", "الاشتراك"}
        explicit_price = str(plan.get("price") or "") in words
        if explicit_price or (meaningful_words and meaningful_words <= words):
            matches.append(plan)
    return matches[0] if len(matches) == 1 else None


def get_selected_catalog_product(customer_chat_id: int) -> dict | None:
    state = get_interactive_sale_state(customer_chat_id)
    product_id = state.get("selected_product_id")
    return get_catalog_product(product_id) if product_id else None


def is_chatgpt_product(product: dict | None) -> bool:
    return bool(product and product.get("name", "").strip().lower() == "chatgpt")


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
    plans = [plan for plan in get_catalog_plans(product["id"]) if plan.get("is_active")]
    choice = resolve_plan_choice([text], plans)
    if choice.plan:
        return product, dict(choice.plan)
    return None


def get_chatgpt_plan_choice_gap(text: str) -> str | None:
    """يحدد شنو الناقص من اختيار الباقة، بدون أن نخمن بداله."""
    product = get_chatgpt_catalog_product()
    if not product:
        return None
    plans = [plan for plan in get_catalog_plans(product["id"]) if plan.get("is_active")]
    choice = resolve_plan_choice([text], plans)
    return choice.missing if choice.missing in {"clarify_plan_type", "clarify_plan_duration"} else None


def has_any_normalized_term(normalized: str, terms: set[str]) -> bool:
    return any(term in normalized for term in terms)


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
    allowed_actions = [
        "static_faq", "payment_next_step", "support_pending", "no_reply", *templates.keys(),
    ]
    recent_messages, session_id = get_recent_interactive_context(customer_chat_id)
    style_examples_text = format_style_examples(get_relevant_style_examples(new_message))
    context_lines = []
    for item in recent_messages:
        text = (item.get("message_text") or "").strip()
        if text:
            speaker = "الزبون" if item.get("sender_type") == "customer" else "المتجر"
            # بيانات الحساب لا تفيد المصنف بعد التسليم، ولا يجوز إرسالها إلى
            # موديل خارجي ضمن سياق المحادثة.
            if speaker == "المتجر" and re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text):
                text = "[تم تسليم بيانات الحساب]"
            context_lines.append(f"{speaker}: {redact_context_text(text)}")
    context_text = "\n".join(context_lines) or "لا يوجد سياق سابق."
    sale_state = get_interactive_sale_state(customer_chat_id)
    workflow_state = sale_state.get("workflow_state", "observing")

    # الحالات الواضحة لا تحتاج تخمين من الموديل. هذا يمنع الخطأ الظاهر
    # بالتجربة: "رايد جات" يجب أن يعرض الباقات، لا أن يطلب اختيارها.
    normalized = " ".join(normalize_style_text(new_message))
    # تبدأ الشكوى حالة سياقية، لا مجرد تجاهل لرسالة واحدة. لذلك الرسائل
    # التالية مثل «تگدر تساعدني؟» أو «هلو» تبقى ضمن نفس الشكوى ولا تعود
    # إلى المصنف العام أو لمسار عرض الباقات.
    if workflow_state == "support_pending":
        return "no_reply"
    # «عندي مشكلة تشات» ليست طلب شراء. لا نسمح بمرورها لمسار الكاتالوج
    # الذي يعرض الأسعار بمجرد رؤية كلمة «تشات». الدعم الذكي لم يفعّل بعد
    # في فرع التفاعل، لذلك نثبت حالة دعم صامتة إلى أن تبدأ جلسة جديدة.
    if is_chatgpt_support_issue(new_message):
        set_interactive_sale_state(customer_chat_id, "support_pending")
        return "no_reply"
    # بعد إرسال الوصل لا نسمح لأي كلمة لاحقة (مثل "هسة" أو "جات") أن ترجع
    # المحادثة للباقات أو للردود العامة. النتيجة الوحيدة تكون فحص الصورة ثم
    # التسليم تلقائياً عند القبول، أو طلب وصل صحيح عند الرفض.
    if workflow_state == "payment_review":
        return "payment_under_review"
    # التحيات دقيقة وحساسة للأسلوب: لا نتركها للموديل. هلو/هلا ليست
    # سلاماً شرعياً، وبالتالي ردها المعتمد "اهلا وسهلا" فقط.
    greeting_categories = set(keyword_match_categories(new_message))
    if "سلام" in greeting_categories:
        return "static_faq"
    if "ترحيب" in greeting_categories:
        return "static_faq"
    if "شكر" in greeting_categories:
        return "closing"
    # هذه الحالات تأتي بعد تسليم الحساب فقط؛ ما نسمح لكلمة "كود" أن
    # تخرج من مسار البيع أو تعطي كوداً لشخص لم يستلم حساباً.
    if can_request_account_code(workflow_state):
        if has_any_normalized_term(normalized, {"workspace", "personal", "مساحه", "مساحة"}):
            return "workspace_guidance"
        if has_any_normalized_term(normalized, {"شلون", "اسجل", "سجل", "تسجيل", "وين"}):
            return "registration_guidance"
        if has_any_normalized_term(normalized, {"ماصار", "مايشتغل", "خطا", "خطأ", "مشكله", "مشكلة"}) or (
            "ما" in normalized and has_any_normalized_term(normalized, {"صار", "يشتغل"})
        ):
            set_interactive_sale_state(customer_chat_id, "support_review")
            return "request_support_screenshot"
        if has_any_normalized_term(normalized, {"كود", "الرمز", "رمز", "code", "otp"}):
            return "code_request"
    # «تمام» أو «أوكي» تأكيد مفهوم، لكنه لا يطلب خطوة من المتجر. هذا ينطبق
    # على جميع مراحل البيع المفتوحة، لا على الدفع فقط.
    if workflow_state in {
        "awaiting_plan_choice", "awaiting_catalog_plan_choice", "awaiting_payment", "awaiting_payment_proof",
        "account_delivered", "code_sent", "support_review",
    } and is_acknowledgement(new_message):
        return "no_reply"
    if workflow_state == "awaiting_plan_choice":
        # اختيار الباقة قد يصل على رسالتين: «أريد جات شهر» ثم «خاص». نجمع
        # رسائل الزبون ضمن الجلسة الحالية فقط حتى لا ننسى الجزء الأول ولا
        # نخلط أي محادثة قديمة.
        customer_choice_messages = [
            (item.get("message_text") or "").strip()
            for item in recent_messages
            if item.get("sender_type") == "customer" and (item.get("message_text") or "").strip()
        ]
        plan_choice_text = " ".join(customer_choice_messages[-4:])
        selected = find_selected_chatgpt_plan(plan_choice_text)
        if selected:
            product, plan = selected
            set_interactive_sale_state(customer_chat_id, "awaiting_payment", product["id"], plan["id"])
            return "payment_methods"
        missing_choice = get_chatgpt_plan_choice_gap(plan_choice_text)
        if missing_choice:
            return missing_choice
        return "request_plan_choice"
    if workflow_state == "awaiting_catalog_plan_choice":
        product = get_selected_catalog_product(customer_chat_id)
        if not product:
            return "clarify"
        selected_plan = find_selected_catalog_plan(product, new_message)
        if selected_plan:
            set_interactive_sale_state(customer_chat_id, "awaiting_payment", product["id"], selected_plan["id"])
            return "payment_methods"
        # إعادة سؤال «شنو متوفر؟» أو اسم المنتج تعرض نفس باقاته، ولا ترجع
        # إلى فلو ChatGPT ولا تطلب اختياراً أعمى.
        return "catalog_product_plans"
    # «شكد لازم أدفع؟» بعد اختيار الباقة سؤال عن مبلغ الباقة نفسها، مو
    # طلب لإعادة أرقام الدفع. نعتمد السعر المخزّن للباقة المختارة.
    if workflow_state in {"awaiting_payment", "awaiting_payment_proof"} and is_plan_question(new_message):
        expected_amount, _ = get_expected_payment_for_interactive_session(customer_chat_id)
        if expected_amount is not None:
            return "selected_plan_price"
    # «شنو عدكم غيره؟» لا تحدد هل يقصد باقات أو منتجات أو طريقة دفع أخرى؛
    # نسأل بدلاً من تخمين المقصود وإرباكه.
    if is_ambiguous_followup(new_message):
        return "clarify"
    if is_payment_claim(new_message):
        if workflow_state in {"awaiting_payment", "awaiting_payment_proof"}:
            set_interactive_sale_state(customer_chat_id, "awaiting_payment_proof")
            return "request_payment_proof"
        return "request_plan_choice"
    if workflow_state in {"awaiting_payment", "awaiting_payment_proof"} and asks_payment_guidance(new_message):
        return "payment_next_step"
    if has_any_normalized_term(normalized, {"ادفع", "الدفع", "ماستر", "زين", "رصيد", "تحويل"}):
        if workflow_state in {"awaiting_payment", "awaiting_payment_proof"}:
            return "payment_methods"
        return "request_plan_choice"
    if any(term in normalized for term in {"شكرا", "شكراً", "تعبتكم", "عاشت"}):
        return "closing"
    # الزبون قد يحدد المنتج والباقـة من أول رسالة مثل: «أريد جات مشترك
    # شهر». لا نعيد له القائمة في هذه الحالة؛ نثبت اختياره وننتقل مباشرة
    # لخطوة الدفع.
    if is_chatgpt_catalog_context(new_message):
        selected = find_selected_chatgpt_plan(new_message)
        if selected:
            product, plan = selected
            set_interactive_sale_state(customer_chat_id, "awaiting_payment", product["id"], plan["id"])
            return "payment_methods"
        missing_choice = get_chatgpt_plan_choice_gap(new_message)
        if missing_choice:
            set_interactive_sale_state(customer_chat_id, "awaiting_plan_choice", None, None)
            return missing_choice
    if is_chatgpt_catalog_context(new_message) or (
        is_plan_question(new_message) and is_chatgpt_catalog_context(context_text)
    ):
        set_interactive_sale_state(customer_chat_id, "awaiting_plan_choice", None, None)
        return "chatgpt_plans"

    # منتجات الكاتالوج الأخرى لها نفس منطق العرض والاختيار، لكن لا تدخل
    # أبداً في تسليم حسابات ChatGPT.
    catalog_product = find_catalog_product_context(new_message)
    if catalog_product and not is_chatgpt_product(catalog_product):
        set_interactive_sale_state(customer_chat_id, "awaiting_catalog_plan_choice", catalog_product["id"], None)
        return "catalog_product_plans"

    # داخل عملية بيع قائمة، أي كلام ما فهمناه لا يرجع لآخر رد ولا يخرج عن
    # السياق. نسأل الزبون يوضح قصده.
    if workflow_state in {
        "awaiting_plan_choice", "awaiting_payment", "awaiting_payment_proof",
        "account_delivered", "code_sent", "support_review",
    }:
        return "clarify"

    messages = [{"role": "system", "content": TEST_ACTION_SELECTOR_PROMPT}]
    if style_examples_text:
        messages.append({"role": "system", "content": f"أمثلة مؤرشفة ومعتمدة لفهم المسار فقط:\n\n{style_examples_text}"})
    messages.append({
        "role": "user",
        "content": (
            f"حالة الجلسة الحالية: {workflow_state}\n\n"
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
        if raw_action == "support_pending":
            set_interactive_sale_state(customer_chat_id, "support_pending")
            return "no_reply"
        for action_key in allowed_actions:
            if raw_action == action_key or action_key in raw_action:
                logger.info("Interactive selector chose %s for session %s", action_key, session_id)
                return action_key
        logger.warning("Interactive selector returned unsupported value: %r", raw_action)
        return "handoff"
    except Exception:
        logger.exception("Failed to choose interactive response action")
        return None


def render_test_response(
    action_key: str, customer_text: str, customer_chat_id: int | None = None
) -> str:
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
    if action_key == "selected_plan_price":
        if customer_chat_id is not None:
            amount, plan_name = get_expected_payment_for_interactive_session(customer_chat_id)
            if amount is not None:
                return f"سعره {amount} آلاف."
        return "تدلل، اختار الباقة اللي تناسبك حتى أگلك سعرها بالضبط."
    if action_key == "payment_next_step":
        amount, plan_name = (None, "")
        if customer_chat_id is not None:
            amount, plan_name = get_expected_payment_for_interactive_session(customer_chat_id)
        amount_text = f" {amount} آلاف" if amount is not None else ""
        product = get_selected_catalog_product(customer_chat_id) if customer_chat_id is not None else None
        if product and not is_chatgpt_product(product):
            return f"إي تدفع أول{amount_text} على وحدة من الطرق، وبعدها دزلي صورة التحويل حتى أتأكد وأجهز طلبك."
        if is_private_chatgpt_plan(plan_name):
            return f"إي تدفع أول{amount_text} على وحدة من الطرق، وبعدها دزلي صورة التحويل حتى أتأكد وأفعّل اشتراكك."
        return f"إي تدفع أول{amount_text} على وحدة من الطرق، وبعدها دزلي صورة التحويل حتى أتأكد وأدزلك الحساب."
    if action_key == "clarify":
        return "عفواً ما فهمت قصدك، تكدر توضحلي؟"
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
    if action_key == "catalog_product_plans":
        product = get_selected_catalog_product(customer_chat_id) if customer_chat_id is not None else None
        if product:
            plans = [plan for plan in get_catalog_plans(product["id"]) if plan.get("is_active")]
            if plans:
                lines = [f"بلي موجود هاي الباقات المتوفرة {product['name']}:", ""]
                lines.extend(f"- {plan['name']} {plan['price']}" for plan in plans)
                return "\n".join(lines)
            return f"تدلل، {product['name']} حالياً ما بي باقات مضافة."
        return "عفواً ما فهمت قصدك، تكدر توضحلي؟"
    templates = get_interactive_response_templates()
    return templates.get(action_key) or templates.get("handoff") or "تدلل، خليني أتأكد من الموضوع وأرجعلك."


def keyword_match_categories(text: str) -> list[str]:
    """
    المصنف الأساسي — مطابقة كلمات مفتاحية مباشرة (بدون ذكاء اصطناعي).
    يرجع قائمة فئات مرتبة حسب موقع ظهور الكلمة المفتاحية بالرسالة.
    """
    # وحّد اختلافات الكتابة العربية والتكرار قبل البحث؛ الرسائل الواردة من
    # الزبائن كثيراً ما تكون مثل «تلغرام»، «تليغرام» أو «شاتتت».
    normalized = text.strip().lower()
    normalized = re.sub(r"[أإآٱ]", "ا", normalized)
    normalized = normalized.replace("ى", "ي").replace("ة", "ه")
    normalized = re.sub(r"(.)\1{2,}", r"\1", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    matches: list[tuple[int, str]] = []  # (موقع الظهور، اسم الفئة)

    for category, keywords, _ in FAQ_RULES:
        best_position = None
        for kw in keywords:
            keyword = kw.lower()
            keyword = re.sub(r"[أإآٱ]", "ا", keyword)
            keyword = keyword.replace("ى", "ي").replace("ة", "ه")
            keyword = re.sub(r"(.)\1{2,}", r"\1", keyword)
            # طابق الكلمة/العبارة كاملة، لا جزءاً داخل كلمة ثانية؛ هذا يمنع
            # كلمة قصيرة مثل «تلي» من تشغيل رد تليجرام داخل نص غير متعلق.
            pattern = rf"(?<![\w\u0600-\u06ff]){re.escape(keyword)}(?![\w\u0600-\u06ff])"
            match = re.search(pattern, normalized)
            pos = match.start() if match else -1
            if pos != -1 and (best_position is None or pos < best_position):
                best_position = pos
        if best_position is not None:
            matches.append((best_position, category))

    for kw in CODE_REQUEST_KEYWORDS:
        keyword = kw.lower()
        keyword = re.sub(r"[أإآٱ]", "ا", keyword)
        keyword = keyword.replace("ى", "ي").replace("ة", "ه")
        pattern = rf"(?<![\w\u0600-\u06ff]){re.escape(keyword)}(?![\w\u0600-\u06ff])"
        match = re.search(pattern, normalized)
        pos = match.start() if match else -1
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
        # ذكر المنتج وحده («ChatGPT»، «شات») هو طلب شائع لفتح الباقات. لا
        # نرسله للمصنف العام حتى لا يصنّفه أحياناً كموضوع غير متعلق ويسكت.
        chatgpt_words = {"chatgpt", "chat", "gpt", "جات", "چات", "تشات", "شات", "جيبيتي"}
        normalized_words = set(re.findall(r"[a-z0-9]+|[\u0600-\u06ff]+", text.lower()))
        if normalized_words and normalized_words <= chatgpt_words:
            return categories, False

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


async def infer_contextual_payment_request(chat_id: int, text: str) -> bool:
    """يفهم طلب الدفع غير المباشر بعد أن يرسل البوت عرض منتج أو باقة."""
    normalized = _normalize_greeting_text(text)
    if not normalized or any(term in normalized for term in ("شكرا", "شكراً", "تمام", "اوكي", "اوك")):
        return False
    try:
        rows = (supabase.table("conversation_archive")
            .select("sender_type, message_text, created_at")
            .eq("customer_chat_id", chat_id)
            .order("created_at", desc=True).limit(12).execute().data or [])
    except Exception:
        logger.exception("Failed to load context for payment intent")
        return False
    latest_bot = next((row for row in rows if row.get("sender_type") == "bot"), None)
    if not latest_bot:
        return False
    offer_text = latest_bot.get("message_text") or ""
    offer_markers = (
        "الباقات", "الباقة", "اشتراك", "اختَر", "اختار", "المنتج", "الأسعار",
        "السعر", "متوفر", "chatgpt", "كانفا", "انكي", "فرينوت", "گودنوت",
    )
    if not any(marker.lower() in offer_text.lower() for marker in offer_markers):
        return False
    if "طرق الدفع" in offer_text or any(term in offer_text for term in ("ماستر", "زين كاش", "رصيد اثير", "رصيد اسيا")):
        return False

    context_lines = []
    for row in reversed(rows):
        message_text = (row.get("message_text") or "").strip()
        if message_text:
            speaker = "الزبون" if row.get("sender_type") == "customer" else "البوت"
            context_lines.append(f"{speaker}: {redact_context_text(message_text)}")
    prompt = (
        "أنت مصنف نوايا لمتجر عراقي. لا تكتب رداً.\n"
        "قرر هل رسالة الزبون الحالية تطلب معرفة طرق الدفع أو الخطوة التالية لإتمام الشراء.\n"
        "إذا كان المقصود مثل: هسه شسوي؟ شلون أكمل؟ بعدين شنو؟ بعد عرض منتج، أجب payment_methods.\n"
        "إذا كانت شكراً أو تمام أو إقراراً بلا سؤال، أجب no_reply.\n"
        "إذا كانت عن شيء آخر، أجب no_reply. أجب بكلمة واحدة فقط.\n\n"
        f"آخر سياق:\n{chr(10).join(context_lines[-8:])}\n\n"
        f"رسالة الزبون الحالية: {redact_context_text(text)}"
    )
    try:
        data = await call_groq_api({
            "model": CHATGPT_CONTEXT_MODEL,
            "temperature": 0,
            "max_completion_tokens": 20,
            "reasoning_effort": "low",
            "messages": [{"role": "user", "content": prompt}],
        }, timeout=8.0)
        raw = ((data or {}).get("choices") or [{}])[0].get("message", {}).get("content", "")
        if raw.strip().lower() == "payment_methods":
            return True
    except Exception:
        logger.exception("Failed to classify contextual payment request")

    # احتياط سريع إذا تعذر الـAI، مع بقاء شرط وجود عرض منتج سابق.
    return any(term in normalized for term in ("هسه شسوي", "شنو اسوي", "شلون اكمل", "كيف اكمل", "بعدها شنو", "الخطوه الجايه"))


async def infer_contextual_code_request(chat_id: int, text: str) -> str | None:
    """يصنف ردود ما بعد الكود: retry أو restart_done أو لا علاقة لها بالكود."""
    state = _get_retry_state(chat_id)
    if is_private_totp_account(chat_id):
        return None
    if state["attempt_count"] not in {1, 2, 3, 4} and not state["awaiting_restart_confirmation"]:
        return None
    normalized = _normalize_greeting_text(text)
    if any(term in normalized for term in ("شكرا", "شكراً", "تمام", "اوكي", "اوك")):
        return None
    try:
        rows = (supabase.table("conversation_archive")
            .select("sender_type, message_text")
            .eq("customer_chat_id", chat_id)
            .order("created_at", desc=True).limit(10).execute().data or [])
    except Exception:
        logger.exception("Failed to load context for code retry intent")
        rows = []
    latest_bot = next((row for row in rows if row.get("sender_type") == "bot"), None)
    if not latest_bot or not ("الكود:" in (latest_bot.get("message_text") or "") or state["awaiting_restart_confirmation"]):
        return None
    context_lines = []
    for row in reversed(rows):
        value = (row.get("message_text") or "").strip()
        if value:
            context_lines.append(("الزبون" if row.get("sender_type") == "customer" else "البوت") + f": {redact_context_text(value)}")
    prompt = (
        "أنت مصنف سياق لطلب كود دخول لحساب ChatGPT مشترك. لا تكتب رداً.\n"
        "أجب retry إذا كان كلام الزبون يعني أن الكود لم يعمل أو يريد كوداً آخر، حتى لو لم يذكر كلمة كود.\n"
        "أجب restart_done إذا قال إنه عمل رست/حذف الحساب/بدأ من جديد ويريد الكود بعد الريست.\n"
        "أجب no_reply إذا كانت الرسالة شكراً أو تمام أو لا علاقة لها. كلمة واحدة فقط.\n\n"
        f"السياق:\n{chr(10).join(context_lines[-8:])}\n\n"
        f"الرسالة الحالية: {redact_context_text(text)}"
    )
    try:
        data = await call_groq_api({
            "model": CHATGPT_CONTEXT_MODEL,
            "temperature": 0,
            "max_completion_tokens": 20,
            "reasoning_effort": "low",
            "messages": [{"role": "user", "content": prompt}],
        }, timeout=8.0)
        raw = ((data or {}).get("choices") or [{}])[0].get("message", {}).get("content", "").strip().lower()
        if raw in {"retry", "restart_done"}:
            return raw
    except Exception:
        logger.exception("Failed to classify contextual code retry request")

    if state["awaiting_restart_confirmation"] and any(term in normalized for term in ("سويت رست", "سويت ريست", "حذفت", "من البدايه", "من البداية")):
        return "restart_done"
    if any(term in normalized for term in ("ما اشتغل", "مايشتغل", "ما يشتغل", "ماصار", "ما صار", "مافتح", "ما فتح", "نزلي", "دزلي", "ثاني", "مره ثانيه", "مرة ثانية")):
        return "retry"
    return None


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


def is_private_totp_account(chat_id: int) -> bool:
    """يتحقق هل الحساب المربوط أضيف عبر مسار الحساب الخاص."""
    try:
        links = supabase.table("totp_links").select("account_id").eq("chat_id", chat_id).limit(1).execute().data or []
        if not links:
            return False
        accounts = supabase.table("totp_accounts").select("link_code").eq("id", links[0]["account_id"]).limit(1).execute().data or []
        return bool(accounts and str(accounts[0].get("link_code") or "").startswith("private_"))
    except Exception:
        logger.exception("Failed to identify private TOTP account for chat %s", chat_id)
        return False


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
    """يحجز حساباً مشتركاً فيه مقعد من أصل 3 لهذه الجلسة التجريبية."""
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
            effective_capacity = min(int(account["capacity"]), SHARED_CHATGPT_ACCOUNT_CAPACITY)
            if (used.count or 0) >= effective_capacity:
                continue
            supabase.table("chatgpt_account_assignments").insert({
                "account_id": account["id"], "customer_chat_id": customer_chat_id,
                "conversation_session_id": state["id"],
            }).execute()
            return account
    except Exception:
        logger.exception("Failed to assign shared ChatGPT account")
    return None


async def check_expired_subscription_reminders(context: ContextTypes.DEFAULT_TYPE) -> None:
    """يرسل رسالة Feedback للزبون مرة واحدة عند انتهاء أي اشتراك."""
    now = datetime.now(timezone.utc)
    try:
        reminders = (
            supabase.table("subscription_reminders")
            .select("id, customer_chat_id, customer_name, customer_username, business_connection_id, product_name, plan_name, plan_duration, subscription_type, duration_months, feedback_only, source, instagram_sale_id, instagram_account")
            .eq("status", "active").lte("expires_at", now.isoformat()).execute().data or []
        )
    except Exception:
        logger.exception("Failed to check subscription reminders")
        return

    for reminder in reminders:
        product_text = reminder.get("product_name") or "اشتراكك"
        plan_text = reminder.get("plan_name") or reminder.get("plan_duration") or ""
        customer = reminder["customer_name"]
        if reminder.get("customer_username"):
            customer += f" (@{reminder['customer_username']})"
        try:
            supabase.table("subscription_reminders").update({
                "status": "expired", "expiry_notified_at": now.isoformat(),
            }).eq("id", reminder["id"]).eq("status", "active").execute()

            # إذا لم يبقَ للزبون اشتراك فعّال آخر (مثلاً جدد مقدماً)، نفك
            # /link أيضاً. لذلك عداد زبائن الحساب بالإحصائيات ينقص فوراً.
            unlinked = False
            chat_id = reminder.get("customer_chat_id")
            if not reminder.get("feedback_only") and chat_id is not None and can_unlink_expired_customer(chat_id):
                supabase.table("totp_links").delete().eq("chat_id", chat_id).execute()
                unlinked = True

            feedback_text = (
                "السلام عليكم.\n\n"
                "إن شاء الله كانت تجربتك ويانا ممتعة ومفيدة.\n\n"
                "حابين نعرف شلون كانت تجربتك؟ وإذا واجهتك أي مشكلة أو قصّرنا وياك بشي، خبرنا."
            )

            chat_id = reminder.get("customer_chat_id")
            customer_send_error = False
            reply_markup = None
            if chat_id is not None:
                try:
                    send_kwargs = {"chat_id": chat_id, "text": feedback_text, "reply_markup": reply_markup}
                    if reminder.get("business_connection_id"):
                        send_kwargs["business_connection_id"] = reminder["business_connection_id"]
                    await context.bot.send_message(**send_kwargs)
                except Exception:
                    customer_send_error = True
                    logger.exception("Failed to send expiry feedback to customer %s", chat_id)
            supabase.table("subscription_reminders").update({
                "feedback_status": "awaiting_reply",
                "feedback_requested_at": now.isoformat(),
            }).eq("id", reminder["id"]).execute()
            source_text = "\n📲 المصدر: Instagram" if reminder.get("source") == "instagram" else ""
            sale_text = f"\nرقم العملية: {reminder.get('instagram_sale_id')}" if reminder.get("instagram_sale_id") else ""
            await context.bot.send_message(
                chat_id=OWNER_USER_ID,
                text=(f"🔔 انتهى اشتراك {product_text} للزبون: {customer}\n"
                      + source_text + sale_text + "\n"
                      + ("✅ تم فك ربطه من الحساب.\n" if unlinked else "")
                      + ("⚠️ فشل إرسال رسالة المتابعة للزبون." if customer_send_error
                         else "✅ أُرسلت رسالة المتابعة وننتظر رده." if chat_id is not None
                         else "⚠️ ماكو chat_id لإرسال رسالة المتابعة.")),
            )
        except Exception:
            logger.exception("Failed to notify expired subscription %s", reminder.get("id"))


def feedback_reply_is_positive(text: str) -> bool:
    normalized = (text or "").lower()
    negative = ("مو زين", "مو حلو", "سيئ", "سيء", "مشكله", "مشكلة", "ما يشتغل", "مايفتح", "تعويض", "استرجاع", "زفت")
    if any(term in normalized for term in negative):
        return False
    positive = ("حلو", "زين", "ممتاز", "ممتعه", "ممتعة", "شكرا", "شكراً", "عاشت", "كلش", "راضي", "تمام")
    return any(term in normalized for term in positive)


async def handle_feedback_followup(context: ContextTypes.DEFAULT_TYPE, bm, text: str) -> bool:
    """يعالج رد الزبون على رسالة المتابعة قبل دخوله إلى الردود العامة."""
    try:
        rows = (
            supabase.table("subscription_reminders")
            .select("id, business_connection_id")
            .eq("customer_chat_id", bm.chat.id).eq("feedback_status", "awaiting_reply")
            .order("feedback_requested_at", desc=True).limit(1).execute().data or []
        )
    except Exception:
        logger.exception("Failed to find pending feedback reply")
        return False
    if not rows:
        return False

    reminder_id = rows[0]["id"]
    if not feedback_reply_is_positive(text):
        supabase.table("subscription_reminders").update({
            "feedback_status": "needs_owner",
            "feedback_responded_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", reminder_id).execute()
        try:
            await context.bot.send_message(
                chat_id=OWNER_USER_ID,
                text=(
                    "⚠️ رد غير إيجابي على متابعة التقييم\n"
                    f"الزبون: {bm.chat.full_name or bm.chat.first_name or 'غير معروف'}\n"
                    f"chat_id: {bm.chat.id}\n\n"
                    f"كتب: {text}\n\n"
                    "تم إيقاف الإرسال التلقائي لهذا الرد ويحتاج تدخلك."
                ),
            )
        except Exception:
            logger.exception("Failed to notify owner about negative feedback")
        return True

    supabase.table("subscription_reminders").update({
        "feedback_status": "positive",
        "feedback_responded_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", reminder_id).execute()
    if SUBSCRIPTION_FEEDBACK_URL:
        send_kwargs = {
            "business_connection_id": rows[0].get("business_connection_id") or bm.business_connection_id,
            "chat_id": bm.chat.id,
            "text": "كلش خوش، عاشت إيدك 🌷\nإذا تحب، هذا رابط تقييم تجربتك ويانا:",
            "reply_markup": InlineKeyboardMarkup([[InlineKeyboardButton("📝 قيّم تجربتك", url=SUBSCRIPTION_FEEDBACK_URL)]]),
        }
        try:
            await context.bot.send_message(**send_kwargs)
        except Exception:
            logger.exception("Failed to send feedback link")
    return True


def generate_totp_code(secret: str) -> str:
    totp = pyotp.TOTP(secret)
    return totp.now()


# ------------------------------------------------------------------
# نظام تتبع محاولات الكود الفاشلة (code_retry_tracker بقاعدة Supabase)
# التسلسل المتفق عليه لما الزبون يقول "ما صار" بشكل متكرر:
#   محاولة 1، 2  → كود جديد تلقائياً
#   محاولة 3     → رسالة "سوي ريستارت" بدون كود
#   بعد تأكيد الريستارت → كود 3
#   المحاولة 4        → كود 4
#   بعد فشل الرابع     → توقف، تنبيه للأونر مع أزرار التحكم
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


def has_active_subscription(chat_id: int) -> bool:
    """الكود لا يصدر إلا بوجود اشتراك مثبت ولم يصل تاريخ انتهائه بعد."""
    try:
        rows = (
            supabase.table("subscription_reminders")
            .select("id").eq("customer_chat_id", chat_id).eq("status", "active")
            .gt("expires_at", datetime.now(timezone.utc).isoformat()).limit(1).execute().data or []
        )
        return bool(rows)
    except Exception:
        # عند تعذر قراءة حالة الاشتراك نختار عدم إرسال الكود؛ هذا يمنع
        # استمرار الوصول بالخطأ بعد الانتهاء.
        logger.exception("Failed to check active subscription for chat %s", chat_id)
        return False


def can_unlink_expired_customer(chat_id: int) -> bool:
    """لا نفك الربط إذا كان للزبون تجديد فعّال آخر؛ عند خطأ قاعدة البيانات نبقيه."""
    try:
        rows = (
            supabase.table("subscription_reminders")
            .select("id").eq("customer_chat_id", chat_id).eq("status", "active")
            .gt("expires_at", datetime.now(timezone.utc).isoformat()).limit(1).execute().data or []
        )
        return not rows
    except Exception:
        logger.exception("Failed to determine whether expired customer can be unlinked: %s", chat_id)
        return False


RESTART_MESSAGE = (
    "يبدو انه الكود ما يشتغل معك بشكل صحيح.\n"
    "جرب تسوي التالي: احذف الحساب من تطبيق المصادقة (Authenticator) "
    "وابدأ عملية التسجيل من جديد من الأول، وبعدها راسلني وبعطيك كود جديد."
)

RESTART_CONFIRMATION_MESSAGE = (
    "تمام. سوّي الريست واحذف الحساب من تطبيق Authenticator، وبعدها اكتبلي "
    "«سويت رست» حتى أنزلك كود جديد."
)

STOPPED_MESSAGE = (
    "يبدو انه فيه مشكلة مستمرة، حولت طلبك لصاحب المتجر مباشرة "
    "وراح يتواصل معك قريباً."
)


def process_code_request(chat_id: int, restart_confirmed: bool = False) -> tuple[str | None, bool]:
    """
    يقرر شنو الرد المناسب لطلب كود، حسب حالة عداد المحاولات.

    يرجع (نص الرد أو None، هل نبعث تنبيه خاص "توقف" للأونر).
    نص الرد يكون: كود فعلي، أو رسالة ريستارت، أو None لو نوقف كلياً.
    """
    state = _get_retry_state(chat_id)
    attempt_count = state["attempt_count"]
    awaiting_restart = state["awaiting_restart_confirmation"]

    is_private_account = is_private_totp_account(chat_id)

    # الحسابات المشتركة تحتاج اشتراكاً فعالاً؛ الحساب الخاص يعتمد على
    # الربط الذي أنشأه /addprivate ولا يتوقف بانتهاء تذكير الاشتراك.
    if not is_private_account and not has_active_subscription(chat_id):
        return None, False

    result = get_secret_for_chat(chat_id)
    if result is None:
        # مو مربوط اصلاً — نفس السلوك القديم، تجاهل صامت
        return None, False

    secret, label = result

    if awaiting_restart:
        if not restart_confirmed:
            return RESTART_CONFIRMATION_MESSAGE, False
        # بعد تأكيد الريست نرسل الكود الثالث؛ إذا فشل يليه الكود الرابع،
        # وبعد فشل الرابع تنتقل الحالة إلى موافقة الأونر.
        code = generate_totp_code(secret)
        _save_retry_state(chat_id, 3, False)
        return f"الكود: {code}\nصالح لمدة 30 ثانية تقريبا", False

    if not is_private_account and attempt_count >= 4:
        if attempt_count == 4:
            _save_retry_state(chat_id, 5, False)
            return STOPPED_MESSAGE, True
        return None, True

    decision = (
        decide_private_code_retry(attempt_count, awaiting_restart)
        if is_private_account
        else decide_code_retry(attempt_count, awaiting_restart)
    )
    if decision.action == "send_code":
        code = generate_totp_code(secret)
        _save_retry_state(chat_id, decision.attempt_count, decision.awaiting_restart)
        return f"الكود: {code}\nصالح لمدة 30 ثانية تقريبا", False
    if decision.action == "ask_restart":
        _save_retry_state(chat_id, decision.attempt_count, decision.awaiting_restart)
        return RESTART_MESSAGE, False
    # المحاولة السادسة وما بعدها: نوقف ونبلغ الأونر.
    _save_retry_state(chat_id, decision.attempt_count, decision.awaiting_restart)
    return None, True


async def handle_manual_extra_code_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يتيح للأونر إرسال كود إضافي يدوياً بعد توقف المحاولات التلقائية."""
    query = update.callback_query
    if query is None or query.from_user.id != OWNER_USER_ID:
        return
    data = query.data or ""
    try:
        chat_id = int(data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await query.answer("معرف الزبون غير صحيح.", show_alert=True)
        return
    if data.startswith("code_manual_stop_"):
        await query.answer("تم إيقاف الأكواد الإضافية.")
        await query.edit_message_text(query.message.text + "\n\n❌ تم إيقاف الأكواد الإضافية.")
        return
    if not data.startswith("code_manual_send_"):
        return
    result = get_secret_for_chat(chat_id)
    if result is None:
        await query.answer("ما لكيت حساب مرتبط.", show_alert=True)
        return
    secret, _ = result
    code = generate_totp_code(secret)
    send_kwargs = {"chat_id": chat_id, "text": f"الكود: {code}\nصالح لمدة 30 ثانية تقريبا"}
    try:
        reminder = (supabase.table("subscription_reminders")
            .select("business_connection_id")
            .eq("customer_chat_id", chat_id).eq("status", "active")
            .order("started_at", desc=True).limit(1).execute().data or [])
        if reminder and reminder[0].get("business_connection_id"):
            send_kwargs["business_connection_id"] = reminder[0]["business_connection_id"]
        await context.bot.send_message(**send_kwargs)
        await query.answer("تم إرسال كود إضافي.")
        await query.edit_message_text(query.message.text + "\n\n➕ تم إرسال كود إضافي للزبون.")
    except Exception:
        logger.exception("Failed to send manual extra code to %s", chat_id)
        await query.answer("تعذر إرسال الكود.", show_alert=True)


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


async def add_private_account(
    context: ContextTypes.DEFAULT_TYPE,
    target_chat_id: int,
    secret: str,
    label: str | None = None,
) -> None:
    """ينشئ حساباً خاصاً ويربطه بزبون محدد."""
    try:
        # Secret الـTOTP عادة Base32 (حروف وأرقام)، وليس كوداً من 6 أرقام.
        pyotp.TOTP(secret.strip().replace(" ", "")).now()
        link_code = "private_" + uuid.uuid4().hex[:12]
        account = supabase.table("totp_accounts").insert({
            "link_code": link_code,
            "secret": secret.strip().replace(" ", ""),
            "label": label or "حساب خاص",
        }).execute().data
        if not account:
            raise RuntimeError("لم تُرجع قاعدة البيانات الحساب المضاف")
        supabase.table("totp_links").upsert({
            "chat_id": target_chat_id,
            "account_id": account[0]["id"],
        }).execute()

        state = get_interactive_sale_state(target_chat_id)
        if state.get("workflow_state") == "private_activation_pending":
            set_interactive_sale_state(target_chat_id, "account_delivered")

        await context.bot.send_message(
            chat_id=OWNER_USER_ID,
            text=(f"✅ تمت إضافة الحساب الخاص وربطه بالزبون.\n"
                  f"chat_id: {target_chat_id}\n"
                  f"ملاحظة: {label or 'حساب خاص'}\n"
                  "من هسه إذا يطلب كود، ينرسل له تلقائياً."),
        )
        try:
            await context.bot.send_message(
                chat_id=NOTIFICATIONS_GROUP_ID,
                message_thread_id=TOPIC_CHATGPT_ACCOUNTS,
                text=(f"➕ حساب خاص جديد مربوط تلقائياً\n"
                      f"الزبون: {target_chat_id}\n"
                      f"ملاحظة: {label or 'حساب خاص'}"),
            )
        except Exception:
            logger.exception("Failed to notify private account link")
    except Exception as e:
        logger.exception("addprivate failed")
        await context.bot.send_message(
            chat_id=OWNER_USER_ID,
            text=("⚠️ ما انضاف الحساب الخاص. تأكد أن الـ Secret هو مفتاح TOTP "
                  "Base32 وليس كوداً مؤقتاً من 6 أرقام.\n" + str(e)),
        )


async def handle_owner_command(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, bm=None) -> bool:
    """يعالج أوامر الأونر: /addaccount، /addprivate، /link، /resetcode، وaccept."""

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

    # /addprivate <secret> [label]
    # يُستخدم داخل محادثة الزبون نفسها. ينشئ حساب TOTP ويربطه فوراً بهذا
    # الزبون، ثم يفتح له مسار طلب الكود التلقائي.
    private_match = PRIVATE_ADD_PATTERN.match(text.strip())
    if private_match:
        secret, label = private_match.groups()
        await add_private_account(context, chat_id, secret, label)
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

        # إذا كان هذا ربط حساب خاص بعد تأكيد الدفع، يصير الزبون مخوّلاً
        # بطلب الكود تلقائياً. الحسابات المشتركة تبقى على مسارها المعتاد.
        state = get_interactive_sale_state(chat_id)
        if state.get("workflow_state") == "private_activation_pending":
            set_interactive_sale_state(chat_id, "account_delivered")

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
            text=(f"✅ تم ربط هذا الزبون بالحساب ({label or link_code}).{sheet_note}\n"
                  f"chat_id للتنبيه اليدوي: {chat_id}"),
        )
        # إذا كان دافع مسبقاً وباقته مسجلة، الربط يكفي ولا نسألك عن الدين.
        # سؤال الدين مخصص فقط للزبون الذي لا يملك اشتراكاً فعّالاً مسجلاً.
        if bm is not None and not has_active_subscription(chat_id):
            context.user_data["pending_link_debt"] = {
                "customer_chat_id": chat_id,
                "customer_name": bm.chat.full_name or bm.chat.first_name or "غير معروف",
                "customer_username": bm.chat.username,
                "account_label": label or link_code,
            }
            await context.bot.send_message(
                chat_id=OWNER_USER_ID,
                text="هل هذا الزبون دين؟",
                reply_markup=build_link_debt_keyboard(),
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

    محدودة بصورة وحدة/6 ساعات لكل زبون — أي صورة تتجاوز الحد تُتجاهل
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
        "business_connection_id": bm.business_connection_id,
        "product": None,
        "payments": [],
        "pending_method": None,
        "pending_amount": 0,
        "awaiting_manual_amount": False,
        "awaiting_manual_product": False,
        "subscription_type": None,
        "duration_months": None,
        "plan_id": None,
        "plan_name": None,
        "plan_duration": None,
        "duration_days": None,
    }
    archive_photo_rate_limit_marker(customer_chat_id, "customer")


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
    archive_photo_rate_limit_marker(OWNER_USER_ID, "owner")


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

    # بعد دفع منتج «جات» الأونر هو من يثبت النوع والمدة، لذلك لا يخلط
    # البوت بين خاص/مشترك أو شهر/شهرين من كلام الزبون وحده.
    if data.startswith("pay_subscription_"):
        try:
            _, _, subscription_type, duration_text = data.split("_", 3)
            duration_months = int(duration_text)
        except (ValueError, TypeError):
            await query.answer("اختيار الاشتراك غير صحيح.", show_alert=True)
            return
        if subscription_type not in {"private", "shared"} or duration_months not in {1, 2}:
            await query.answer("اختيار الاشتراك غير صحيح.", show_alert=True)
            return
        state["subscription_type"] = subscription_type
        state["duration_months"] = duration_months
        data = "pay_finalize"

    if data.startswith("pay_plan_"):
        plan_id = data[len("pay_plan_"):]
        try:
            rows = supabase.table("catalog_plans").select("id, product_id, name, duration, is_active").eq("id", plan_id).limit(1).execute().data or []
        except Exception:
            rows = []
        selected_product = catalog_product_for_payment_name(state.get("product") or "")
        if (not rows or not rows[0].get("is_active") or not duration_to_days(rows[0].get("duration"))
                or (selected_product and rows[0].get("product_id") != selected_product.get("id"))):
            await query.answer("الباقة غير متاحة أو بلا مدة.", show_alert=True)
            return
        plan = rows[0]
        state.update({
            "plan_id": plan["id"], "plan_name": plan["name"],
            "plan_duration": plan.get("duration"),
            "duration_days": duration_to_days(plan.get("duration")),
        })
        data = "pay_finalize"

    if data.startswith("pay_ambos_duration_"):
        try:
            duration_days = int(data[len("pay_ambos_duration_"):])
        except ValueError:
            await query.answer("المدة غير صحيحة.", show_alert=True)
            return
        if duration_days not in {days for _, days in AMBOS_DURATION_OPTIONS}:
            await query.answer("المدة غير متاحة.", show_alert=True)
            return
        label = next(label for label, days in AMBOS_DURATION_OPTIONS if days == duration_days)
        state.update({"plan_name": label, "plan_duration": label, "duration_days": duration_days})
        data = "pay_finalize"

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
        if product == "امبوس":
            await query.edit_message_caption(
                caption=format_payment_summary(state) + "\n\nاختَر مدة Ambos:",
                reply_markup=build_ambos_duration_keyboard(),
            )
            return
        generic_plans = prepare_generic_subscription(state)
        if generic_plans:
            await query.edit_message_caption(
                caption=format_payment_summary(state) + "\n\nاختَر الباقة/المدة:",
                reply_markup=build_subscription_plan_keyboard(generic_plans),
            )
            return
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

        if state["product"] == CHATGPT_PRODUCT_NAME and not state.get("subscription_type"):
            await query.edit_message_caption(
                caption=format_payment_summary(state) + "\n\nاختَر نوع ومدة الاشتراك حتى ينحفظ تنبيه نهايته:",
                reply_markup=build_subscription_type_keyboard(),
            )
            return

        if (state["product"] != CHATGPT_PRODUCT_NAME
                and not state.get("duration_days")
                and not state.get("reminder_disabled")):
            generic_plans = prepare_generic_subscription(state)
            if generic_plans:
                await query.edit_message_caption(
                    caption=format_payment_summary(state) + "\n\nاختَر الباقة/المدة حتى ينحفظ تنبيه النهاية:",
                    reply_markup=build_subscription_plan_keyboard(generic_plans),
                )
                return
            await query.answer("ما لقيت مدة مفعلة لهذا المنتج بالكاتالوج.", show_alert=True)
            return

        saved = append_payment_row(state)
        subscription_saved = False
        if saved and (state["product"] == CHATGPT_PRODUCT_NAME or state.get("duration_days") or state.get("reminder_disabled")):
            subscription_saved = save_subscription_reminder(state)

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
            if state.get("reminder_disabled"):
                reminder_note = "\nℹ️ هذا المنتج دائم، ما يحتاج تنبيه انتهاء."
            else:
                reminder_note = "\n🔔 تم تسجيل تنبيه انتهاء الاشتراك." if subscription_saved else "\n⚠️ تم حفظ الدفعة، بس فشل حفظ تنبيه الاشتراك."
            final_text = format_payment_summary(state) + "\n\n✅ تم الحفظ بنجاح." + reminder_note
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
        payment_stats_saved = False

        # نزيد رصيد كل خزنة مطابقة لطرق الدفع المستخدمة بالتسديد
        if saved:
            for method, amount in state["payments"]:
                if method in VAULT_NAMES:
                    adjust_vault_balance(method, amount)
            # تسديد الدين دخل جديد اليوم، حتى لو كان أصل الدين قديماً.
            # force_new يمنع دمجه مع سطر ChatGPT قديم أو ناقص.
            payment_stats_saved = append_payment_row(state, force_new=True)

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
            if not payment_stats_saved:
                final_text += "\n⚠️ تم تحديث الدين، لكن تعذر إضافة التسديد لإحصائيات الدخل."
        else:
            final_text = format_payment_summary(state) + "\n\n⚠️ فشل تحديث حالة الدين بـ Google Sheet — تحقق من الاتصال يدوياً."

        del _pending_payments[message_id]

        try:
            await query.edit_message_caption(caption=final_text, reply_markup=None)
        except Exception:
            logger.exception("Failed to update final debt-repayment confirmation message")
        return


async def handle_link_debt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يكمل سؤال الدين الذي يظهر مباشرة بعد /link."""
    query = update.callback_query
    if query is None or query.from_user.id != OWNER_USER_ID:
        return
    state = context.user_data.get("pending_link_debt")
    if state is None:
        await query.answer("انتهت صلاحية هذا السؤال.", show_alert=True)
        return
    await query.answer()
    if query.data == "linkdebt_no":
        context.user_data.pop("pending_link_debt", None)
        await query.edit_message_text("تمام، تم الربط بدون تسجيل دين.")
        return
    if query.data == "linkdebt_yes":
        await query.edit_message_text(
            "تمام، هذا دين. اختَر نوع ومدة الاشتراك؛ من الآن يبدأ الحساب ويسمح له بطلب الكود.",
            reply_markup=build_link_debt_plan_keyboard(),
        )
        return
    if not query.data.startswith("linkplan_"):
        return
    try:
        _, subscription_type, duration_text = query.data.split("_", 2)
        duration_months = int(duration_text)
    except (ValueError, TypeError):
        await query.edit_message_text("⚠️ اختيار الباقة غير صحيح.")
        return
    if subscription_type not in {"private", "shared"} or duration_months not in {1, 2}:
        await query.edit_message_text("⚠️ اختيار الباقة غير صحيح.")
        return

    saved = save_subscription_reminder({
        **state,
        "subscription_type": subscription_type,
        "duration_months": duration_months,
        "is_debt": True,
    })
    if not saved:
        await query.edit_message_text("⚠️ تم الربط، بس فشل تسجيل اشتراك الدين. تأكد من تشغيل SQL الجديد.")
        return
    context.user_data.pop("pending_link_debt", None)
    type_text = "خاص" if subscription_type == "private" else "مشترك"
    duration_text = "شهر" if duration_months == 1 else "شهرين"
    end = datetime.now(timezone(timedelta(hours=3))) + timedelta(days=30 * duration_months)
    await query.edit_message_text(
        f"✅ تم تسجيل دين: {type_text} {duration_text}\n"
        f"الكود متاح للزبون من هسه.\n"
        f"ينتهي الاشتراك: {end.strftime('%Y-%m-%d %H:%M')}"
    )


async def handle_manual_subscription_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يبدأ إضافة تنبيه يدوي مستقل، حتى لو لم يمرّ عبر صورة دفع."""
    query = update.callback_query
    if query is None or query.from_user.id != OWNER_USER_ID:
        return
    try:
        _, subscription_type, duration_text = query.data.split("_", 2)
        duration_months = int(duration_text)
    except (AttributeError, ValueError, TypeError):
        await query.answer("اختيار غير صحيح.", show_alert=True)
        return
    if subscription_type not in {"private", "shared"} or duration_months not in {1, 2}:
        await query.answer("اختيار غير صحيح.", show_alert=True)
        return
    context.user_data["pending_manual_subscription"] = {
        "subscription_type": subscription_type,
        "duration_months": duration_months,
    }
    await query.answer()
    type_text = "خاص" if subscription_type == "private" else "مشترك"
    duration_text = "شهر" if duration_months == 1 else "شهرين"
    await query.edit_message_text(
        f"اختيارك: {type_text} {duration_text}\n\n"
        "اكتب هكذا حتى ينقطع الكود عند الانتهاء:\n"
        "chat_id | اسم الزبون | @username (اختياري)"
    )


async def handle_manual_subscription_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """يكمل التسجيل اليدوي باسم الزبون بعد أن يختار الأونر الباقة."""
    state = context.user_data.get("pending_manual_subscription")
    message = update.message
    if state is None or message is None or not message.text:
        return False
    parts = [part.strip() for part in message.text.split("|", 2)]
    if len(parts) < 2 or not parts[0].lstrip("-").isdigit() or not parts[1]:
        await message.reply_text("الصيغة الصحيحة: chat_id | اسم الزبون | @username (اختياري)")
        return True
    chat_id = int(parts[0])
    name = parts[1]
    username = parts[2] if len(parts) == 3 else ""
    saved = save_subscription_reminder({
        "customer_chat_id": chat_id,
        "customer_name": name,
        "customer_username": username or None,
        "subscription_type": state["subscription_type"],
        "duration_months": state["duration_months"],
    })
    if saved:
        end = datetime.now(timezone(timedelta(hours=3))) + timedelta(days=30 * state["duration_months"])
        await message.reply_text(f"✅ تم تسجيل التنبيه. ينتهي: {end.strftime('%Y-%m-%d %H:%M')}")
        context.user_data.pop("pending_manual_subscription", None)
    else:
        await message.reply_text("⚠️ فشل الحفظ. تأكد من تشغيل ملف Supabase الجديد.")
    return True


async def handle_personal_reminder_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not context.user_data.get("pending_personal_reminder"):
        return False
    message = update.message
    if not message or not message.text:
        return False
    parsed = parse_personal_reminder_input(message.text.strip())
    if parsed is None:
        await message.reply_text(
            "الصيغة أو الوقت غير صحيح. اكتب مثلاً:\n"
            "2026-08-22 15:30 | أتصل بالمورّد\n"
            "والوقت لازم يكون بالمستقبل."
        )
        return True
    due_at, purpose = parsed
    if save_personal_reminder(due_at, purpose):
        context.user_data.pop("pending_personal_reminder", None)
        await message.reply_text(
            f"✅ تم حفظ التذكير.\nوقت التنبيه: {due_at.strftime('%Y-%m-%d %H:%M')} بتوقيت بغداد\n"
            f"الغرض: {purpose}\n\nسأكرره كل 15 دقيقة إلى أن تضغط «وصلني التذكير»."
        )
    else:
        await message.reply_text("⚠️ فشل حفظ التذكير. تأكد من تشغيل ملف Supabase الجديد.")
    return True


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

    if product == "امبوس":
        try:
            await context.bot.edit_message_caption(
                chat_id=OWNER_USER_ID,
                message_id=replied_id,
                caption=format_payment_summary(state) + "\n\nاختَر مدة Ambos:",
                reply_markup=build_ambos_duration_keyboard(),
            )
        except Exception:
            logger.exception("Failed to show Ambos duration options")
        return True

    generic_plans = prepare_generic_subscription(state)
    if generic_plans:
        try:
            await context.bot.edit_message_caption(
                chat_id=OWNER_USER_ID,
                message_id=replied_id,
                caption=format_payment_summary(state) + "\n\nاختَر الباقة/المدة:",
                reply_markup=build_subscription_plan_keyboard(generic_plans),
            )
        except Exception:
            logger.exception("Failed to show generic subscription plans")
        return True

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
            text=("اكتب الفترة كـ رد على هذي الرسالة.\n"
                  "يوم: 2026-08-21\n"
                  "أو نطاق بالوقت: 2026-08-21 09:00 إلى 2026-08-21 18:00"),
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

    if data.startswith("stats_account_relogin_"):
        account_id = data[len("stats_account_relogin_"):]
        accounts = supabase.table("totp_accounts").select("label, link_code").eq("id", account_id).limit(1).execute().data or []
        account_name = (accounts[0].get("label") if accounts else None) or (accounts[0].get("link_code") if accounts else "الحساب المشترك")
        sent, failed = await send_legacy_shared_account_relogin_notifications(context, account_id, account_name)
        await query.edit_message_text(
            f"✅ تم إرسال الرسالة إلى {sent} مشترك فعّال."
            + (f"\n⚠️ فشل الإرسال إلى {failed}." if failed else ""),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ رجوع", callback_data=f"stats_account_{account_id}")]]),
        )
        return

    if data.startswith("stats_native_account_"):
        account_id = data[len("stats_native_account_"):]
        accounts = supabase.table("chatgpt_shared_accounts").select("email, capacity").eq("id", account_id).limit(1).execute().data or []
        if not accounts:
            await query.edit_message_text("الحساب غير موجود.")
            return
        assignments = supabase.table("chatgpt_account_assignments").select("customer_chat_id").eq("account_id", account_id).eq("status", "active").execute().data or []
        instagram_sales = get_instagram_account_sales(account_id)
        capacity = min(int(accounts[0].get("capacity") or SHARED_CHATGPT_ACCOUNT_CAPACITY), SHARED_CHATGPT_ACCOUNT_CAPACITY)
        total_used = len(assignments) + len(instagram_sales)
        instagram_lines = "\n".join(f"@{row[2]} — {row[19] if len(row) > 19 else '—'}" for row in instagram_sales) or "لا توجد مبيعات Instagram"
        await query.edit_message_text(
            f"الحساب المشترك: {accounts[0].get('email') or '—'}\n\n"
            f"المستخدم: {total_used}/{capacity}\n"
            f"المتبقي: {max(0, capacity - total_used)}\n"
            f"مشتركو Telegram: {len(assignments)}\n"
            f"مشتركو Instagram: {len(instagram_sales)}\n\n"
            f"مبيعات Instagram:\n{instagram_lines}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(BTN_BACK, callback_data="stats_chatgpt_shared")]]),
        )
        return

    if data.startswith("stats_account_"):
        account_id = data[len("stats_account_"):]
        customer_chat_ids = get_customers_for_account(account_id)
        instagram_sales = get_instagram_account_sales(f"legacy:{account_id}")
        total_used = len(customer_chat_ids) + len(instagram_sales)
        instagram_lines = "\n".join(f"@{row[2]} — {row[19] if len(row) > 19 else '—'}" for row in instagram_sales) or "لا توجد مبيعات Instagram"
        text = (
            f"المستخدم: {total_used}/{SHARED_CHATGPT_ACCOUNT_CAPACITY}\n"
            f"المتبقي: {max(0, SHARED_CHATGPT_ACCOUNT_CAPACITY - total_used)}\n"
            f"مشتركو Telegram: {len(customer_chat_ids)}\n"
            f"مشتركو Instagram: {len(instagram_sales)}\n\n"
            f"مبيعات Instagram:\n{instagram_lines}"
        )
        await query.edit_message_text(
            text=text,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📣 إعادة تسجيل الدخول للفعّالين", callback_data=f"stats_account_relogin_{account_id}")],
                [InlineKeyboardButton(BTN_BACK, callback_data="stats_chatgpt_shared")],
            ]),
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
        BTN_SUBSCRIPTION_REMINDER, BTN_PERSONAL_REMINDER, BTN_INSTAGRAM_ADMIN,
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

    if text == BTN_SUBSCRIPTION_REMINDER:
        context.user_data.pop("pending_manual_subscription", None)
        await message.reply_text(
            "اختَر نوع ومدة الاشتراك اللي تريد تضيفه يدوياً:",
            reply_markup=build_manual_subscription_keyboard(),
        )
        return True

    if text == BTN_PERSONAL_REMINDER:
        context.user_data["pending_personal_reminder"] = True
        await message.reply_text(
            "اكتب التذكير بهذا الشكل:\n"
            "YYYY-MM-DD HH:MM | الغرض من التذكير\n\n"
            "مثال: 2026-08-22 15:30 | أتصل بالمورّد\n"
            "وتكدر تستخدم أيضاً DD/MM/YYYY HH:MM. الوقت حسب توقيت بغداد."
        )
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

    if text == BTN_INSTAGRAM_ADMIN:
        await show_instagram_admin(message)
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
        await message.reply_text(
            "صيغة غير صحيحة. استخدم مثلاً:\n"
            "2026-08-21\nأو\n2026-08-21 09:00 إلى 2026-08-21 18:00"
        )
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

    # لكل /newtest رقم سياق اصطناعي مستقل، حتى ما تختلط سيناريوهات الاختبار.
    # إذا ما بدأ الأونر جلسة يظل السلوك القديم متاحًا للتوافق.
    customer_chat_id = context.user_data.get("interactive_test_chat_id", OWNER_USER_ID)
    image_description = None
    payment_analysis = None
    photo_outside_payment_flow = False
    if message.photo:
        user_text = message.caption or "[صورة مرفقة]"
        state = get_interactive_sale_state(customer_chat_id)
        if not should_review_payment_photo(state.get("workflow_state", "observing")):
            # ما نصرف فحص رؤية على أي صورة خارج مسار الدفع، ولا نفسرها
            # تلقائياً على أنها وصل تحويل.
            photo_outside_payment_flow = True
        else:
            try:
                file = await context.bot.get_file(message.photo[-1].file_id)
                image_bytes = bytes(await file.download_as_bytearray())
                # فحص وصل الدفع يغني عن طلب وصف ثانٍ لنفس الصورة. سابقاً كان
                # يستدعي قارئ الصورة مرتين متتاليتين (قد يصل التأخير لـ45 ثانية).
                payment_analysis = await analyze_payment_proof(image_bytes, customer_chat_id)
            except Exception:
                logger.exception("Failed to analyze image in interactive topic")
            if payment_analysis is None:
                await message.reply_text("⚠️ ما كدرت أدقق الصورة هسه. جرّب دزها مرة ثانية بعد دقيقة.")
                return
            image_description = payment_analysis.get("reason") or "صورة تم إرسالها للفحص"
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

    # نؤرشف رسالتك أول (كـ "customer" بالمعنى الوظيفي — طرف المحادثة)
    archive_message(
        customer_chat_id, "تجربة", None, sender_type="customer",
        message_text=user_text, image_description=image_description,
    )

    # الذكاء الاصطناعي يختار الإجراء فقط؛ الرد النهائي دائماً ثابت ومخزن.
    if photo_outside_payment_flow:
        action_key = "clarify"
    elif image_description:
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
    # التدقيق الطبيعي لا يحتاج رسالة وسيطة: الزبون يشوف فقط النتيجة
    # النهائية (تسليم الحساب أو طلب وصل أوضح)، بينما إشعار المراجعة
    # يبقى للأونر داخل فرع الإشعارات.
    if action_key == "payment_under_review":
        return
    if action_key == "no_reply":
        return
    if action_key == "payment_proof_approved":
        _, plan_name = get_expected_payment_for_interactive_session(customer_chat_id)
        selected_product = get_selected_catalog_product(customer_chat_id)
        if selected_product and not is_chatgpt_product(selected_product):
            # بقية المنتجات تستخدم نفس التحقق والدفع، لكن لا يجوز أن تقع
            # في مسار تسليم حساب ChatGPT. تجهيزها له فلو خاص لاحقاً.
            set_interactive_sale_state(customer_chat_id, "fulfillment_pending")
            reply = "تم تأكيد التحويل، راح أجهز طلبك وأرجعلك."
        elif is_private_chatgpt_plan(plan_name):
            # تفعيل الخاص يحتاج صاحب المتجر؛ لا نشارك بيانات حساب مشترك ولا
            # نسمح بمسار الكود قبل أن يتم التفعيل فعلياً.
            set_interactive_sale_state(customer_chat_id, "private_activation_pending")
            reply = "تم تأكيد التحويل، راح أفعّله إلك وأرجعلك."
        else:
            account = assign_shared_chatgpt_account(customer_chat_id)
            if account:
                set_interactive_sale_state(customer_chat_id, "account_delivered")
                reply = CHATGPT_DELIVERY_TEMPLATE.format(email=account["email"], password=account["password"])
            else:
                # لا يوجد مقعد متاح؛ لا نرسل أي بيانات حساب.
                reply = "تم تأكيد التحويل مبدئياً، بس حالياً ماكو مقعد مشترك متاح. دا أرتبلك واحد وأرجعلك."
    elif action_key == "code_request":
        # الكود لا ينرسل إلا للحساب الذي سُلّم داخل هذه الجلسة. العداد نفسه
        # المستخدم بالبوت الحقيقي يطبق قواعد إعادة المحاولة أيضاً.
        reply, stopped = process_code_request(customer_chat_id)
        if reply:
            set_interactive_sale_state(customer_chat_id, "code_sent")
        else:
            reply = render_test_response("handoff", user_text, customer_chat_id)
            if stopped:
                logger.warning("Interactive test code retries stopped for session %s", customer_chat_id)
    else:
        reply = render_test_response(action_key, user_text, customer_chat_id)

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
    if await handle_cancel_customer_input(update, context):
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
    message = update.message
    if message and message.text:
        # من محادثة الأونر الخاصة استخدم: /addprivate <chat_id> <secret> [اسم]
        target_match = PRIVATE_ADD_TARGET_PATTERN.match(message.text.strip())
        if target_match:
            target_chat_id, secret, label = target_match.groups()
            await add_private_account(context, int(target_chat_id), secret, label)
            return
        if message.text.strip().lower().startswith("/addprivate"):
            await message.reply_text(
                "استخدم الأمر داخل محادثة الزبون هكذا:\n"
                "/addprivate SECRET_KEY اسم اختياري\n\n"
                "أو من محادثتك ويا البوت:\n"
                "/addprivate CHAT_ID SECRET_KEY اسم اختياري"
            )
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
    if await handle_manual_subscription_input(update, context):
        return
    if await handle_personal_reminder_input(update, context):
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


async def handle_instagram_manager_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Limited data-entry flow for the Instagram sales operator."""
    message = update.message
    if not message or not is_instagram_manager(update.effective_user.id if update.effective_user else None):
        return False
    state = context.user_data.get("instagram_sale")
    if not state or not message.text:
        return False
    text = message.text.strip()

    if state["step"] == "product":
        state["product"] = text
        state["step"] = "account"
        await message.reply_text("اكتب اسم أو يوزر حساب الإنستغرام:")
        return True
    if state["step"] == "account":
        if len(text) < 2:
            await message.reply_text("اكتب اسم أو يوزر صحيح للحساب:")
            return True
        state["instagram_account"] = text.lstrip("@")
        state["step"] = "wallet"
        await message.reply_text("اختَر نوع المحفظة:", reply_markup=instagram_wallet_keyboard())
        return True
    if state["step"] == "amount":
        amount = parse_amount(text)
        if amount is None:
            await message.reply_text("أرسل المبلغ كرقم، مثال: 25000")
            return True
        state["amount"] = amount
        commission = commission_for(amount, percent=INSTAGRAM_COMMISSION_PERCENT)
        state["step"] = "confirm"
        await message.reply_text(
            instagram_sale_prompt(state) +
            f"\n\nعمولته ({INSTAGRAM_COMMISSION_PERCENT}%): {format_iqd(commission)}",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("✅ حفظ العملية", callback_data="ig_confirm")],
                [InlineKeyboardButton("❌ إلغاء", callback_data="ig_cancel")],
            ]),
        )
        return True
    return False


async def handle_instagram_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not is_instagram_manager(query.from_user.id):
        return
    await query.answer()
    data = query.data or ""
    state = context.user_data.get("instagram_sale")

    if data == "ig_cancel":
        context.user_data.pop("instagram_sale", None)
        await query.edit_message_text("تم إلغاء تسجيل العملية.")
        return
    if data == "ig_confirm":
        if not state or state.get("step") != "confirm":
            await query.edit_message_text("انتهت جلسة التسجيل. ابدأ عملية جديدة.")
            return
        sale = {
            "sale_id": "IG-" + uuid.uuid4().hex[:10].upper(),
            "created_at": datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S"),
            "instagram_account": state["instagram_account"],
            "product": state["product"],
            "chat_type": state.get("chat_type"),
            "duration_months": state.get("duration_months"),
            "wallet": state["wallet"],
            "amount": state["amount"],
            "commission_percent": INSTAGRAM_COMMISSION_PERCENT,
            "commission": commission_for(state["amount"], percent=INSTAGRAM_COMMISSION_PERCENT),
            "recorded_by": query.from_user.full_name,
            "recorded_by_id": query.from_user.id,
            "account_source": state.get("account_source"),
            "account_id": state.get("account_id"),
            "account_display": state.get("account_display"),
        }
        if sale.get("account_source") == "shared":
            selected_accounts = [account for account in get_chatgpt_shared_accounts() if str(account.get("id")) == str(sale.get("account_id"))]
            if not selected_accounts:
                await query.edit_message_text("⚠️ الحساب المشترك لم يعد متاحاً.")
                return
            capacity = min(int(selected_accounts[0].get("capacity") or SHARED_CHATGPT_ACCOUNT_CAPACITY), SHARED_CHATGPT_ACCOUNT_CAPACITY)
            if get_instagram_shared_account_usage(selected_accounts[0]) >= capacity:
                await query.edit_message_text("⚠️ هذا الحساب امتلأ أثناء التسجيل. ابدأ العملية واختر حساباً آخر.")
                return
        # مبيعات إنستغرام تدخل مباشرة إلى الخزنة التي اختارها المدير.
        wallet_added = adjust_vault_balance(sale["wallet"], sale["amount"])
        if not wallet_added:
            await query.edit_message_text("⚠️ تعذر تحديث الخزنة. لم يتم حفظ عملية البيع.")
            return
        saved = append_instagram_sale(sale)
        if not saved:
            # إذا فشل سجل المبيعات، نرجع الزيادة حتى لا يبقى الرصيد خاطئاً.
            adjust_vault_balance(sale["wallet"], -sale["amount"])
        reminder_saved = save_instagram_subscription_reminder(sale, state) if saved else False
        if saved and sale.get("instagram_account"):
            # هذا الحقل هو المعرف الذي يدخله مدير مبيعات الإنستغرام.
            # إذا كان يوزر الزبون، يبقى محفوظاً ضمن سجل العملاء للرسائل المستقبلية.
            upsert_customer_contact(
                platform="instagram",
                external_id=sale["instagram_account"].lstrip("@"),
                display_name=sale["instagram_account"],
                username=sale["instagram_account"].lstrip("@"),
            )
        context.user_data.pop("instagram_sale", None)
        if saved:
            if sale.get("account_source") in {"shared", "private_ours"}:
                context.user_data.setdefault("instagram_code_refs", {})[sale["sale_id"]] = {
                    "account_source": sale["account_source"], "account_id": sale["account_id"],
                }
            try:
                await context.bot.send_message(
                    chat_id=OWNER_USER_ID,
                    text=(
                        "📲 تسجيل بيع جديد من الإنستغرام\n"
                        f"رقم العملية: {sale['sale_id']}\n"
                        f"الحساب: @{sale['instagram_account']}\n"
                        f"المنتج: {sale['product']}"
                        + (f" ({sale['chat_type']})" if sale.get("chat_type") else "")
                        + f"\nالمبلغ: {format_iqd(sale['amount'])}"
                        + f"\nالمدة: {sale.get('duration_months') or state.get('duration_months') or '—'} شهر"
                        + f"\nالحساب المستخدم: {sale.get('account_display') or '—'}"
                        + f"\nالخزنة: {sale['wallet']} (تمت إضافة مبلغ البيع)"
                        + f"\nالعمولة: {format_iqd(sale['commission'])}"
                    ),
                )
            except Exception:
                logger.exception("Failed to notify owner about Instagram sale")
            await query.edit_message_text(
                "✅ تم حفظ عملية بيع الإنستغرام\n"
                f"رقم العملية: {sale['sale_id']}\n"
                f"الحساب: {sale.get('account_display') or '—'}\n"
                f"المدة: {sale.get('duration_months') or '—'} شهر\n"
                f"الخزنة: {sale['wallet']} (تمت إضافة مبلغ البيع)\n"
                f"العمولة: {format_iqd(sale['commission'])}\n"
                + ("🔔 تم تسجيل تنبيه الانتهاء." if reminder_saved else "⚠️ لم يتم تسجيل تنبيه الانتهاء.")
                , reply_markup=InlineKeyboardMarkup(
                    ([[InlineKeyboardButton("🔐 جلب كود الحساب", callback_data=f"ig_code_{sale['sale_id']}")]]
                     if sale.get("account_source") in {"shared", "private_ours"} else [])
                    + [[InlineKeyboardButton("✅ إنهاء", callback_data="ig_done")]]
                )
            )
        else:
            await query.edit_message_text("⚠️ تعذر الحفظ في Google Sheets. أعد المحاولة أو بلغ المالك.")
        return
    if not state:
        if data == "ig_done":
            await query.edit_message_text("تم إنهاء العملية.")
            return
        if data.startswith("ig_code_"):
            sale_id = data[len("ig_code_"):]
            code_ref = context.user_data.get("instagram_code_refs", {}).get(sale_id)
            if not code_ref:
                await query.answer("انتهت صلاحية زر الكود.", show_alert=True)
                return
            secret = get_instagram_account_secret(code_ref["account_source"], code_ref["account_id"])
            if not secret:
                await query.answer("تعذر جلب كود الحساب.", show_alert=True)
                return
            await query.message.reply_text(f"🔐 الكود الحالي: {pyotp.TOTP(secret).now()}\nيتغير تلقائياً.")
            return
        return
    if data == "ig_done":
        await query.edit_message_text("تم إنهاء العملية.")
        return
    if data.startswith("ig_code_"):
        sale_id = data[len("ig_code_"):]
        refs = context.user_data.get("instagram_code_refs", {})
        code_ref = refs.get(sale_id)
        if not code_ref:
            # Keep the authorization bound to the exact sale message/session.
            await query.answer("انتهت صلاحية زر الكود.", show_alert=True)
            return
        secret = get_instagram_account_secret(code_ref["account_source"], code_ref["account_id"])
        if not secret:
            await query.answer("تعذر جلب كود الحساب.", show_alert=True)
            return
        await query.message.reply_text(f"🔐 الكود الحالي: {pyotp.TOTP(secret).now()}\nيتغير تلقائياً.")
        return
    if data == "ig_product_manual":
        state["step"] = "product"
        await query.edit_message_text("اكتب اسم المنتج:")
        return
    if data.startswith("ig_product_"):
        product = data[len("ig_product_"):]
        state["product"] = product
        if product == "جات":
            state["step"] = "chat_type"
            await query.edit_message_text("اختَر نوع الجات:", reply_markup=instagram_chat_type_keyboard())
        else:
            state["step"] = "account"
            await query.edit_message_text("اكتب اسم أو يوزر حساب الإنستغرام:")
        return
    if data.startswith("ig_type_"):
        state["chat_type"] = normalize_chat_type(data[len("ig_type_"):])
        if state["chat_type"] == "مشترك":
            state.update({"account_source": "shared", "account_source_label": "مشترك من عدنا", "step": "shared_account"})
            await query.edit_message_text("اختَر الحساب المشترك:", reply_markup=instagram_shared_accounts_keyboard())
        else:
            state["step"] = "private_source"
            await query.edit_message_text("الخاص من عدنا لو من الزبون؟", reply_markup=instagram_account_source_keyboard())
        return
    if data.startswith("ig_shared_"):
        account_id = data[len("ig_shared_"):]
        accounts = [account for account in get_chatgpt_shared_accounts() if str(account.get("id")) == account_id]
        if not accounts:
            await query.answer("الحساب غير موجود أو متوقف.", show_alert=True)
            return
        account = accounts[0]
        capacity = min(int(account.get("capacity") or SHARED_CHATGPT_ACCOUNT_CAPACITY), SHARED_CHATGPT_ACCOUNT_CAPACITY)
        if get_instagram_shared_account_usage(account) >= capacity:
            await query.answer("هذا الحساب امتلأ، اختَر حساباً متوفراً.", show_alert=True)
            return
        state.update({"account_source": "shared", "account_source_label": "مشترك من عدنا", "account_id": account_id, "account_display": accounts[0].get("email") or "حساب مشترك", "step": "duration"})
        await query.edit_message_text("اختَر مدة الاشتراك:", reply_markup=instagram_duration_keyboard())
        return
    if data.startswith("ig_source_private_"):
        source = data[len("ig_source_private_"):]
        if source == "ours":
            state.update({"account_source": "private_ours", "account_source_label": "خاص من عدنا", "step": "private_account"})
            await query.edit_message_text("اختَر الإيميل الخاص من عدنا:", reply_markup=instagram_private_accounts_keyboard())
        else:
            state.update({"account_source": "private_customer", "account_source_label": "خاص من الزبون", "account_id": None, "account_display": "خاص من الزبون", "step": "duration"})
            await query.edit_message_text("اختَر مدة الاشتراك:", reply_markup=instagram_duration_keyboard())
        return
    if data.startswith("ig_private_"):
        account_id = data[len("ig_private_"):]
        accounts = [account for account in get_instagram_private_accounts() if str(account.get("id")) == account_id]
        if not accounts:
            await query.answer("الحساب غير موجود.", show_alert=True)
            return
        state.update({"account_source": "private_ours", "account_source_label": "خاص من عدنا", "account_id": account_id, "account_display": accounts[0].get("label") or "حساب خاص", "step": "duration"})
        await query.edit_message_text("اختَر مدة الاشتراك:", reply_markup=instagram_duration_keyboard())
        return
    if data.startswith("ig_duration_"):
        state["duration_months"] = int(data[len("ig_duration_"):])
        state["step"] = "account"
        await query.edit_message_text("اكتب اسم أو يوزر حساب الإنستغرام:")
        return
    if data.startswith("ig_wallet_"):
        state["wallet"] = data[len("ig_wallet_"):]
        state["step"] = "amount"
        await query.edit_message_text("اكتب مبلغ الدفع بالأرقام، مثال: 25000")


async def on_instagram_manager_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_instagram_manager(update.effective_user.id if update.effective_user else None):
        return
    if update.message and update.message.text == BTN_INSTAGRAM_SALE:
        context.user_data["instagram_sale"] = {
            "step": "product", "product": None, "chat_type": None,
            "instagram_account": None, "wallet": None, "amount": None,
            "duration_months": None, "account_source": None,
            "account_source_label": None, "account_id": None,
            "account_display": None,
        }
        await update.message.reply_text("اختَر المنتج:", reply_markup=instagram_product_keyboard())
        return
    await handle_instagram_manager_message(update, context)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """أمر /start — يرسل لوحة المفاتيح الثابتة (مصروف/دخل/إضافة حساب) بمحادثتك مع البوت."""
    if update.effective_user is None:
        return
    if is_instagram_manager(update.effective_user.id):
        await update.message.reply_text("جاهز. من هنا تسجل مبيعات الإنستغرام فقط:", reply_markup=INSTAGRAM_MANAGER_KEYBOARD)
        return
    if update.effective_user.id != OWNER_USER_ID:
        return
    await update.message.reply_text("جاهز. استخدم الأزرار بالأسفل:", reply_markup=MAIN_REPLY_KEYBOARD)


async def cmd_instagram_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only quick totals for confirmed Instagram sales."""
    if update.effective_user is None or update.effective_user.id != OWNER_USER_ID:
        return
    sheet = get_instagram_sales_worksheet()
    if sheet is None:
        await update.message.reply_text("⚠️ تعذر الاتصال بشيت مبيعات الإنستغرام.")
        return
    try:
        rows = sheet.get_all_values()[1:]
        total = sum(int(re.sub(r"[^0-9]", "", row[6])) for row in rows if len(row) > 8 and row[6])
        commission = sum(int(re.sub(r"[^0-9]", "", row[8])) for row in rows if len(row) > 8 and row[8])
        await update.message.reply_text(
            "📊 تقرير مبيعات الإنستغرام\n\n"
            f"عدد العمليات: {len(rows)}\n"
            f"إجمالي المبيعات: {format_iqd(total)}\n"
            f"إجمالي العمولة ({INSTAGRAM_COMMISSION_PERCENT}%): {format_iqd(commission)}\n"
            f"الصافي لك: {format_iqd(total - commission)}"
        )
    except Exception:
        logger.exception("Failed to build Instagram report")
        await update.message.reply_text("⚠️ تعذر قراءة تقرير مبيعات الإنستغرام.")


def get_instagram_commission_rows(unpaid_only: bool = False) -> list[tuple[int, list[str]]]:
    sheet = get_instagram_sales_worksheet()
    if sheet is None:
        return []
    rows = sheet.get_all_values()
    result = []
    for row_number, row in enumerate(rows[1:], start=2):
        if len(row) < 11 or not row[0].strip():
            continue
        if unpaid_only and row[10].strip() == "نعم":
            continue
        result.append((row_number, row))
    return result


async def show_instagram_admin(message) -> None:
    """Owner-only commission control panel."""
    try:
        rows = get_instagram_commission_rows(unpaid_only=True)
        total = sum(int(re.sub(r"[^0-9]", "", row[8])) for _, row in rows if len(row) > 8 and row[8])
        text = (
            "📲 إدارة عمولات الإنستغرام\n\n"
            f"العمليات غير المسددة: {len(rows)}\n"
            f"إجمالي العمولات غير المسددة: {format_iqd(total)}"
        )
        buttons = []
        for _, row in rows[-20:]:
            buttons.append([InlineKeyboardButton(
                f"{row[0]} — {row[8]} — {row[2]}", callback_data=f"igadmin_sale_{row[0]}"
            )])
        buttons.append([InlineKeyboardButton("🔄 تحديث", callback_data="igadmin_list")])
        await message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    except Exception:
        logger.exception("Failed to show Instagram commission admin")
        await message.reply_text("⚠️ تعذر قراءة عمولات الإنستغرام.")


async def handle_instagram_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.from_user.id != OWNER_USER_ID:
        return
    await query.answer()
    if query.data == "igadmin_list":
        await show_instagram_admin(query.message)
        return
    if not query.data.startswith("igadmin_sale_"):
        return
    sale_id = query.data[len("igadmin_sale_"):]
    sheet = get_instagram_sales_worksheet()
    if sheet is None:
        await query.edit_message_text("⚠️ تعذر الاتصال بالشيت.")
        return
    try:
        rows = get_instagram_commission_rows(unpaid_only=False)
        match = next(((row_number, row) for row_number, row in rows if row[0] == sale_id), None)
        if match is None:
            await query.edit_message_text("العملية غير موجودة.")
            return
        row_number, row = match
        paid = len(row) > 10 and row[10].strip() == "نعم"
        if not paid:
            await query.edit_message_text(
                f"رقم العملية: {row[0]}\n"
                f"الحساب: @{row[2]}\n"
                f"المبلغ: {row[6]}\n"
                f"العمولة: {row[8]}\n\n"
                "حالة العمولة: غير مسددة",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ تم دفع العمولة", callback_data=f"igadmin_paid_{sale_id}")],
                    [InlineKeyboardButton("◀️ رجوع", callback_data="igadmin_list")],
                ]),
            )
        else:
            await query.edit_message_text(f"✅ العمولة مسددة مسبقاً للعملية {sale_id}.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ رجوع", callback_data="igadmin_list")]]))
    except Exception:
        logger.exception("Failed to show Instagram commission %s", sale_id)
        await query.edit_message_text("⚠️ تعذر قراءة العملية.")


async def handle_instagram_commission_paid_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.from_user.id != OWNER_USER_ID:
        return
    await query.answer()
    sale_id = (query.data or "")[len("igadmin_paid_"):]
    sheet = get_instagram_sales_worksheet()
    if sheet is None:
        await query.edit_message_text("⚠️ تعذر الاتصال بالشيت.")
        return
    try:
        match = next(((row_number, row) for row_number, row in get_instagram_commission_rows(False) if row[0] == sale_id), None)
        if match is None:
            await query.edit_message_text("العملية غير موجودة.")
            return
        row_number, row = match
        if len(row) > 10 and row[10].strip() == "نعم":
            await query.edit_message_text(f"✅ العمولة مسددة مسبقاً للعملية {sale_id}.")
            return
        wallet = row[5].strip() if len(row) > 5 else ""
        commission = parse_amount(row[8]) if len(row) > 8 else None
        if wallet not in VAULT_NAMES or commission is None:
            await query.edit_message_text("⚠️ بيانات الخزنة أو العمولة غير صالحة لهذه العملية.")
            return
        if not adjust_vault_balance(wallet, -commission):
            await query.edit_message_text("⚠️ تعذر خصم العمولة من الخزنة. لم يتم تأكيد الدفع.")
            return
        expense_reason = f"عمولة إنستغرام — العملية {sale_id} — @{row[2]}"
        if not append_expense_row(commission, expense_reason):
            adjust_vault_balance(wallet, commission)
            await query.edit_message_text("⚠️ تعذر تسجيل العمولة كمصروف. لم يتم تأكيد الدفع.")
            return
        sheet.update_cell(row_number, 11, "نعم")
        sheet.update_cell(row_number, 19, datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S"))
        await send_expense_notification(context, {
            "amount": commission, "reason": expense_reason, "vault": wallet,
        })
        await query.edit_message_text(
            f"✅ تم تسجيل دفع العمولة\nرقم العملية: {sale_id}\nالعمولة: {row[8]}\n"
            f"الخزنة: {wallet} (-{format_iqd(commission)})\nتم تسجيلها ضمن المصروفات.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ رجوع للعمولات", callback_data="igadmin_list")]]),
        )
    except Exception:
        logger.exception("Failed to mark Instagram commission paid")
        await query.edit_message_text("⚠️ تعذر تحديث حالة العمولة في الشيت.")


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


def _normalize_greeting_text(text: str) -> str:
    """توحيد نص قصير فقط لغرض معرفة ما إذا كان تحية بلا طلب آخر."""
    normalized = text.strip().lower()
    normalized = re.sub(r"[أإآٱ]", "ا", normalized)
    normalized = normalized.replace("ى", "ي").replace("ة", "ه")
    normalized = re.sub(r"[،,.!؟?؛;:\-ـ_]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def is_greeting_only_message(text: str) -> bool:
    """True للتحيات المعروفة فقط، وليس لتحية مرفقة بطلب أو مشكلة."""
    normalized = _normalize_greeting_text(text)
    greeting_phrases = {
        _normalize_greeting_text(keyword)
        for category, keywords, _ in FAQ_RULES
        if category in {"سلام", "ترحيب"}
        for keyword in keywords
    }
    return normalized in greeting_phrases


def seconds_until_customer_replies_allowed(now: datetime | None = None) -> float:
    """يرجع ثواني الانتظار إذا كانت الساعة ضمن فترة الهدوء، وإلا صفر."""
    baghdad = timezone(timedelta(hours=3))
    current = now.astimezone(baghdad) if now else datetime.now(baghdad)
    start_hour, start_minute = QUIET_HOURS_START
    end_hour, end_minute = QUIET_HOURS_END
    quiet_start = current.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
    quiet_end = current.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
    if current < quiet_start:
        return 0
    if current < quiet_end:
        return max(0, (quiet_end - current).total_seconds())
    return 0


def is_new_customer_conversation(chat_id: int) -> bool:
    """يؤخر التحية فقط إذا ماكو رسالة حديثة ضمن نفس سياق المحادثة."""
    try:
        result = (
            supabase.table("conversation_archive")
            .select("created_at")
            .eq("customer_chat_id", chat_id)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if not result.data:
            return True
        created_at = datetime.fromisoformat(result.data[0]["created_at"].replace("Z", "+00:00"))
        return datetime.now(timezone.utc) - created_at >= timedelta(minutes=CONVERSATION_SESSION_GAP_MINUTES)
    except Exception:
        # عند تعذر قراءة الأرشيف، نختار الانتظار الآمن للتحية بدل إرسالها وحدها.
        logger.exception("Failed to determine whether customer conversation is new")
        return True


async def _send_delayed_initial_greeting(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    business_connection_id: str,
    message_id: int,
    customer_name: str,
    customer_username: str | None,
    text: str,
) -> None:
    """يرسل تحية منفردة فقط بعد انتهاء نافذة الانتظار."""
    try:
        await asyncio.sleep(INITIAL_GREETING_WAIT_SECONDS)
        quiet_wait = seconds_until_customer_replies_allowed()
        if quiet_wait:
            logger.info("Holding greeting until 09:00 Baghdad for chat_id=%s", chat_id)
            await asyncio.sleep(quiet_wait)
        reply = get_exact_test_faq_reply(text)
        if not reply:
            return
        if not should_send_faq_reply(chat_id, "سلام" if "وعليكم" in reply else "ترحيب", reply):
            archive_message(chat_id, customer_name, customer_username, sender_type="customer", message_text=text)
            return
        await human_like_reply_sequence(context, chat_id, business_connection_id, message_id)
        await context.bot.send_message(
            business_connection_id=business_connection_id,
            chat_id=chat_id,
            text=reply,
        )
        archive_message(chat_id, customer_name, customer_username, sender_type="customer", message_text=text)
        archive_message(chat_id, customer_name, customer_username, sender_type="bot", message_text=reply)
        await notify_owner(context, chat_id, customer_name, customer_username, text, reply)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Failed to send delayed initial greeting")
    finally:
        pending = _pending_initial_greetings.get(chat_id)
        if pending and pending.get("task") is asyncio.current_task():
            _pending_initial_greetings.pop(chat_id, None)


async def on_business_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعالج رسائل Telegram Business الجديدة والمعدلة (محادثتك الشخصية)."""
    bm = update.business_message or update.edited_business_message
    if not bm:
        return
    is_edited_message = update.edited_business_message is not None

    sender_id = bm.from_user.id if bm.from_user else None
    is_from_owner = sender_id == OWNER_USER_ID
    chat_id = bm.chat.id

    # سجل دائم للمستخدم من أول تعامل، مستقل عن أرشيف الرسائل.
    if not is_from_owner:
        upsert_customer_contact(
            platform="telegram",
            external_id=str(chat_id),
            display_name=bm.chat.full_name or bm.chat.first_name or "غير معروف",
            username=bm.chat.username,
            chat_id=chat_id,
            business_connection_id=bm.business_connection_id,
        )

    # حماية محادثة متفق عليها: لا تمرر النص للذكاء الاصطناعي ولا تحفظه في
    # الأرشيف. نحذف فقط من المحادثة المحددة في Render، سواء كانت الرسالة
    # مرسلة من الأونر أو من الطرف الآخر.
    guard_text = bm.text or bm.caption
    if is_guarded_chat(chat_id, MODESTY_GUARD_CHAT_ID):
        if guard_text and is_flirtatious_text(guard_text):
            try:
                await context.bot.delete_business_messages(
                    business_connection_id=bm.business_connection_id,
                    message_ids=[bm.message_id],
                )
                logger.info("Modesty guard deleted a matched message")
            except Exception:
                logger.exception("Modesty guard could not delete a matched message")
                try:
                    await context.bot.send_message(
                        chat_id=OWNER_USER_ID,
                        text="⚠️ فلتر الحماية ما كدر يحذف رسالة. تأكد من صلاحية حذف كل الرسائل واتصال البوت.",
                    )
                except Exception:
                    logger.exception("Could not notify owner about modesty guard deletion failure")
        # هذه المحادثة مخصصة للحماية فقط: لا FAQ ولا رد تلقائي ولا أرشفة
        # ولا فحص صور أو تحويلات أو إرسال إشعارات المتجر.
        return

    # صورة دفع جاية من الزبون (مو منك) — نحولها لمحادثتك الخاصة مع
    # البوت مع أزرار تأكيد/إلغاء عشان تبدأ تسجيل عملية الدفع
    if bm.photo and not is_from_owner:
        photo_key = (chat_id, bm.message_id, False)
        if photo_key in _processed_business_photo_keys:
            logger.info("Ignoring duplicate business photo update: %s", photo_key)
            return
        _processed_business_photo_keys.add(photo_key)
        # نشغل وصف الصورة بالأرشيف كـ task موازي منفصل — ما يبطئ ولا
        # يأثر على فلو تأكيد الدفع الأساسي (كل وحدة تشتغل لحالها)
        asyncio.create_task(describe_and_archive_customer_photo(context, bm))
        await handle_incoming_payment_photo(update, context, bm)
        return

    # صورة أرسلتها أنت (owner) بمحادثتك مع زبون معين — محتملة إثبات
    # مصروف، نحولها لمحادثتك مع البوت ونسألك تأكيد. بالتوازي، نؤرشفها
    # (وصف بالذكاء الاصطناعي) بدون ما يأثر على فلو المصروف
    if bm.photo and is_from_owner:
        photo_key = (chat_id, bm.message_id, True)
        if photo_key in _processed_business_photo_keys:
            logger.info("Ignoring duplicate owner business photo update: %s", photo_key)
            return
        _processed_business_photo_keys.add(photo_key)
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

    # اسم الزبون واسم المستخدم (لو موجود) — نستخدمهن بالتنبيه للأونر
    customer_name = bm.chat.full_name or bm.chat.first_name or "غير معروف"
    customer_username = bm.chat.username

    if not is_from_owner and await handle_feedback_followup(context, bm, text):
        archive_message(chat_id, customer_name, customer_username, sender_type="customer", message_text=text)
        return

    # لا نرسل تحية البداية وحدها فوراً. ننتظر قليلاً حتى تصل بقية رسالة
    # الزبون؛ عندها تُصنّف التحية والطلب معاً، فيكون الرد إمّا كاملاً أو صامتاً
    # إذا كان الطلب غير مدعوم/غير واضح.
    if not is_from_owner:
        pending_greeting = _pending_initial_greetings.pop(chat_id, None)
        if pending_greeting:
            pending_task = pending_greeting.get("task")
            if pending_task and not pending_task.done():
                pending_task.cancel()
            text = f"{pending_greeting['text']}\n{text}"
            logger.info("Merged follow-up into pending initial greeting for chat_id=%s", chat_id)
        elif not is_edited_message and is_greeting_only_message(text) and is_new_customer_conversation(chat_id):
            pending = {
                "text": text,
                "customer_name": customer_name,
                "customer_username": customer_username,
                "task": None,
            }
            pending["task"] = asyncio.create_task(
                _send_delayed_initial_greeting(
                    context,
                    chat_id,
                    bm.business_connection_id,
                    bm.message_id,
                    customer_name,
                    customer_username,
                    text,
                )
            )
            _pending_initial_greetings[chat_id] = pending
            logger.info(
                "Delaying initial greeting for chat_id=%s by %ss",
                chat_id,
                INITIAL_GREETING_WAIT_SECONDS,
            )
            return

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
    if (
        not is_chatgpt_complaint
        and "طرق الدفع" not in categories
        and (not categories or set(categories).issubset({"سلام", "ترحيب"}))
        and await infer_contextual_payment_request(chat_id, text)
    ):
        categories.append("طرق الدفع")
        logger.info("Inferred contextual payment request for chat_id=%s", chat_id)
    contextual_code_action = await infer_contextual_code_request(chat_id, text)
    restart_confirmed = contextual_code_action == "restart_done"
    if contextual_code_action in {"retry", "restart_done"} and "طلب_كود" not in categories:
        categories.append("طلب_كود")
        logger.info("Inferred contextual code action=%s for chat_id=%s", contextual_code_action, chat_id)
    # إذا كانت الرسالة تحية مرفقة بكلام آخر ولم ينتج عنها أي فئة قابلة
    # للإجابة، لا نرسل التحية وحدها. هذا هو الفرق بين «السلام عليكم» فقط
    # وبين «السلام عليكم، أريد منتجاً غير موجود».
    if (
        not is_greeting_only_message(text)
        and categories
        and set(categories).issubset({"سلام", "ترحيب"})
    ):
        categories = []
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
            reply_text, stopped = process_code_request(chat_id, restart_confirmed=restart_confirmed)
            if reply_text:
                replies_to_send.append(reply_text)
            if stopped:
                should_notify_stopped = True
            continue

        # فئة FAQ عادية — لا نكرر نفس الرد لنفس الزبون حتى بعد restart/deploy.
        # التعديل يُعامل كرسالة جديدة: مثلاً «كانفات» ثم تعديلها إلى «كانفا»
        # يجب أن يشغّل رد Canva حتى لو كانت المحاولة الأولى قبل دقائق.
        reply_text = get_reply_for_category(category)
        if not reply_text:
            continue
        if not is_edited_message and not should_send_faq_reply(chat_id, category, reply_text):
            continue
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
                chat_id=NOTIFICATIONS_GROUP_ID,
                message_thread_id=TOPIC_NOTIFICATIONS,
                text=stopped_notification,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ إرسال كود إضافي", callback_data=f"code_manual_send_{chat_id}")],
                    [InlineKeyboardButton("❌ إيقاف", callback_data=f"code_manual_stop_{chat_id}")],
                ]),
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

    # يفحص كل 15 دقيقة؛ لذلك التنبيه يصل خلال ربع ساعة كحد أقصى من النهاية.
    if app.job_queue is None:
        logger.error("JobQueue غير متوفر: ثبّت python-telegram-bot[job-queue] لتفعيل تنبيهات الاشتراكات.")
    else:
        app.job_queue.run_repeating(check_expired_subscription_reminders, interval=15 * 60, first=10)
        app.job_queue.run_repeating(check_personal_reminders, interval=60, first=15)

    # تحديثات business_message — رسائل الزبائن (نص وصور) عن طريق
    # Telegram Business، وهي أساس عمل البوت
    app.add_handler(MessageHandler(filters.UpdateType.BUSINESS_MESSAGE, on_business_message))
    # تعديل الرسالة قد يضيف كلاماً مخالفاً بعد إرسال رسالة عادية؛ نعيد تمريرها
    # لنفس الحارس قبل أن تصل إلى أي من وظائف المتجر.
    app.add_handler(MessageHandler(filters.UpdateType.EDITED_BUSINESS_MESSAGE, on_business_message))

    # أزرار تسجيل الدفع — تشتغل بمحادثتك الخاصة مع البوت نفسه
    app.add_handler(CallbackQueryHandler(handle_payment_callback, pattern=r"^pay_"))

    # تسجيل مبيعات الإنستغرام — متاح فقط لمعرف المدير المحدد بالبيئة.
    app.add_handler(CallbackQueryHandler(handle_instagram_callback, pattern=r"^ig_"))
    app.add_handler(CallbackQueryHandler(handle_instagram_admin_callback, pattern=r"^igadmin_(?:list|sale_)"))
    app.add_handler(CallbackQueryHandler(handle_instagram_commission_paid_callback, pattern=r"^igadmin_paid_"))

    # زر إضافة تنبيه اشتراك يدوي من لوحة الأونر.
    app.add_handler(CallbackQueryHandler(handle_manual_subscription_callback, pattern=r"^subrem_"))
    app.add_handler(CallbackQueryHandler(handle_personal_reminder_callback, pattern=r"^personal_reminder_ack_"))

    # سؤال الدين والباقته بعد ربط زبون بحساب /link.
    app.add_handler(CallbackQueryHandler(handle_link_debt_callback, pattern=r"^link(?:debt|plan)_"))

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

    # أزرار الأونر لإرسال كود إضافي أو إيقافه بعد توقف المحاولات التلقائية.
    app.add_handler(CallbackQueryHandler(handle_manual_extra_code_callback, pattern=r"^code_manual_(?:send|stop)_"))

    # زرين تبديل عرض/إخفاء كود TOTP بفرع التفاعل
    app.add_handler(CallbackQueryHandler(handle_getcode_callback, pattern=r"^getcode_"))

    # أمر /start — يرسل لوحة المفاتيح الثابتة (مصروف/دخل/إضافة حساب)
    app.add_handler(CommandHandler("start", cmd_start))

    # أمر /income لعرض تقرير الدخل — بمحادثتك الخاصة مع البوت
    # (تيليجرام يشترط أوامر بحروف إنكليزية بس، ما يقبل حروف عربية بأسماء الأوامر)
    app.add_handler(CommandHandler("income", cmd_income_report))

    # تقرير مبيعات الإنستغرام — للأونر فقط.
    app.add_handler(CommandHandler("instagram_report", cmd_instagram_report))

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
            filters.ChatType.PRIVATE & filters.TEXT & filters.User(INSTAGRAM_MANAGER_USER_ID),
            on_instagram_manager_private_message,
        )
    )

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
