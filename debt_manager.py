"""MedBox debt storage, owner UI and repayment workflow.

bot.py supplies explicit services in configure_debts(); this module never
imports bot.py or starts Telegram, Google or Supabase clients at import time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import re
from threading import RLock
from typing import Callable, TYPE_CHECKING
import uuid

from payment_storage import integer_text

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import ContextTypes


logger = logging.getLogger(__name__)
BTN_BACK = "◀️ رجوع"
BTN_DEBT = "💳 الديون"
DEBTS_WORKSHEET_NAME = "ديون"
HEADERS = [
    "التاريخ", "Chat ID", "الزبون", "المنتج", "المبلغ المتبقي", "الحالة",
    "تاريخ آخر تسديد", "رقم الدين", "معرف المنتج", "معرف الباقة", "الباقة",
    "المبلغ الأصلي", "المسدد الكلي", "آخر تحديث", "آخر عملية تسديد",
]
OPEN_STATUSES = {"غير مدفوع", "جزئي"}
SHEET_COL_TOTAL, SHEET_COL_PRODUCT = 2, 4
_sheet = None
_services = None
_pending_debt = None
_pending_repayments = {}
_busy_payments = set()
_storage_lock = RLock()


@dataclass
class DebtServices:
    owner_id: int
    open_spreadsheet: Callable
    products: Callable
    product: Callable
    plans: Callable
    catalog_product: Callable
    format_customer: Callable
    format_money: Callable
    identity: Callable
    record_income: Callable
    adjust_vault: Callable
    add_credit: Callable
    latest_payment: Callable
    payment_summary: Callable
    product_keyboard: Callable
    button: Callable
    markup: Callable
    payment_methods: Callable
    vault_names: tuple
    notification_chat_id: int
    notification_topic_id: int
    is_chatgpt_product: Callable
    duration_days: Callable
    permanent_duration: Callable
    save_subscription: Callable
    schedule_feedback: Callable


def configure_debts(services: DebtServices) -> None:
    global _services, _sheet
    _services = services
    _sheet = None


def parse_sheet_amount(value) -> int | None:
    raw = integer_text(value)
    return int(raw) if raw.isdigit() else None


def get_debts_worksheet():
    global _sheet
    if _sheet is None:
        spreadsheet = _services.open_spreadsheet()
        worksheets = spreadsheet.worksheets()
        sheet = next((ws for ws in worksheets if ws.title == DEBTS_WORKSHEET_NAME), None)
        if sheet is None:
            sheet = spreadsheet.add_worksheet(title=DEBTS_WORKSHEET_NAME, rows=1000, cols=len(HEADERS))
            sheet.update(range_name="A1:O1", values=[HEADERS], value_input_option="RAW")
        elif sheet.col_count < len(HEADERS):
            sheet.resize(rows=sheet.row_count, cols=len(HEADERS))
        if len(sheet.row_values(1)) < len(HEADERS):
            sheet.update(range_name="H1:O1", values=[HEADERS[7:]], value_input_option="RAW")
        _sheet = sheet
    return _sheet


def _debt_record(row: list, row_number: int) -> dict | None:
    values = [str(value if value is not None else "").strip() for value in row]
    values += [""] * max(0, len(HEADERS) - len(values))
    chat_id = integer_text(values[1])
    if not re.fullmatch(r"-?\d+", chat_id) or not values[3] or not values[5]:
        return None
    amount = parse_sheet_amount(values[4]) or 0
    original = parse_sheet_amount(values[11])
    paid = parse_sheet_amount(values[12])
    if values[5] == "مدفوع":
        # Old rows sometimes kept the original amount in E after closure.
        original = original if original is not None else (amount or None)
        remaining = 0
        paid = paid if paid is not None else original
    else:
        remaining = amount
        original = original if original is not None else (remaining + (paid or 0))
        paid = paid if paid is not None else max(0, original - remaining)
    return {
        "row_number": row_number, "chat_id": int(chat_id),
        "debt_id": values[7] or f"LEGACY-{row_number}",
        "created_at": values[0], "customer_line": values[2], "product": values[3],
        "remaining": remaining, "status": values[5], "last_paid_at": values[6],
        "product_id": values[8], "plan_id": values[9], "plan_name": values[10],
        "original_amount": original, "paid_total": paid,
        "last_operation_id": values[14],
    }


def list_debts(chat_id: int | None = None, *, open_only=False) -> list[dict]:
    rows = get_debts_worksheet().get_all_values()
    debts = []
    for index, row in enumerate(rows[1:], 2):
        debt = _debt_record(row, index)
        if not debt or (chat_id is not None and debt["chat_id"] != int(chat_id)):
            continue
        if open_only and (debt["status"] not in OPEN_STATUSES or debt["remaining"] <= 0):
            continue
        debts.append(debt)
    return list(reversed(debts))


def list_open_debts(chat_id: int | None = None) -> list[dict]:
    # Storage errors propagate: a read failure must never mean "no debts".
    return list_debts(chat_id, open_only=True)


def get_open_debt_by_row(chat_id: int, row_number: int) -> dict | None:
    return next((debt for debt in list_open_debts(chat_id) if debt["row_number"] == row_number), None)


def _same_product(left: str, right: str) -> bool:
    if left.casefold() == right.casefold():
        return True
    a, b = _services.catalog_product(left), _services.catalog_product(right)
    return bool(a and b and a.get("id") == b.get("id"))


def find_unpaid_debt(chat_id: int, product: str) -> tuple[int, int] | None:
    for debt in reversed(list_open_debts(chat_id)):
        if _same_product(debt["product"], product):
            return debt["row_number"], debt["remaining"]
    return None


def append_debt_row(chat_id, customer_line, product, amount, *, product_id=None, plan_id=None, plan_name=None, debt_id=None) -> bool:
    if not isinstance(amount, int) or amount <= 0:
        return False
    debt_id = debt_id or f"DEBT-{uuid.uuid4().hex[:12].upper()}"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    row = [now, str(chat_id), customer_line, product, amount, "غير مدفوع", "",
           debt_id, product_id or "", plan_id or "", plan_name or "", amount, 0, now, ""]
    try:
        with _storage_lock:
            sheet = get_debts_worksheet()
            rows = sheet.get_all_values()
            for existing in rows[1:]:
                if len(existing) > 7 and existing[7] == debt_id:
                    return integer_text(existing[1]) == str(chat_id) and existing[3] == product
            target = max(2, len(rows) + 1)
            if target > sheet.row_count:
                sheet.add_rows(100)
            sheet.update(range_name=f"A{target}:O{target}", values=[row], value_input_option="USER_ENTERED")
            saved = sheet.row_values(target)
            return len(saved) > 7 and saved[7] == debt_id and parse_sheet_amount(saved[4]) == amount
    except Exception:
        logger.exception("Failed to save debt %s", debt_id)
        return False


def process_debt_repayment(row_number, remaining_before, paid_amount, *, operation_id=None) -> tuple[bool, int]:
    if paid_amount <= 0:
        return False, remaining_before
    try:
        with _storage_lock:
            sheet = get_debts_worksheet()
            values = sheet.row_values(row_number)
            values += [""] * max(0, len(HEADERS) - len(values))
            debt = _debt_record(values, row_number)
            if not debt:
                return False, remaining_before
            if operation_id and debt["last_operation_id"] == operation_id:
                return True, debt["remaining"]
            if debt["remaining"] != remaining_before or debt["status"] not in OPEN_STATUSES:
                return False, debt["remaining"]
            remaining = max(0, remaining_before - paid_amount)
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            values[4:7] = [remaining, "مدفوع" if remaining == 0 else "جزئي", now]
            values[11] = debt["original_amount"] or remaining_before
            values[12] = (debt["paid_total"] or 0) + min(remaining_before, paid_amount)
            values[13:15] = [now, operation_id or ""]
            sheet.update(range_name=f"E{row_number}:O{row_number}", values=[values[4:15]], value_input_option="USER_ENTERED")
            saved = _debt_record(sheet.row_values(row_number), row_number)
            return bool(saved and saved["remaining"] == remaining and saved["last_operation_id"] == (operation_id or "")), remaining
    except Exception:
        logger.exception("Failed to update debt repayment row=%s", row_number)
        return False, remaining_before


def _active_products():
    return [p for p in _services.products() if p.get("is_active")]


def _legacy_product_names():
    return [p["name"] for p in _active_products()]


async def cmd_fix_existing_debt_payment(
    update: Update, context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """يحوّل آخر دفعة مسجلة بالخطأ إلى تسديد دين، بلا إضافة دخل ثانٍ.

    الاستخدام: /fixdebt CHAT_ID PRODUCT
    """
    message = update.effective_message
    if message is None or update.effective_user is None or update.effective_user.id != _services.owner_id:
        return
    if len(context.args) < 2:
        await message.reply_text("الاستخدام: /fixdebt CHAT_ID PRODUCT")
        return
    try:
        customer_chat_id = int(context.args[0])
    except ValueError:
        await message.reply_text("⚠️ chat_id غير صحيح.")
        return
    product = " ".join(context.args[1:]).strip()
    latest_payment = _services.latest_payment(customer_chat_id)
    if latest_payment is None:
        await message.reply_text("⚠️ ما لقيت دفعة مسجلة لهذا الزبون.")
        return
    payment_row_number, payment_row = latest_payment
    if is_debt_repayment_row(payment_row):
        await message.reply_text("هذه الدفعة مسجلة أصلاً كتسديد؛ لم أغير الدين.")
        return
    operation_id = f"FIX-{customer_chat_id}-{payment_row_number}"
    debts = list_debts(customer_chat_id)
    if any(d["last_operation_id"] == operation_id for d in debts):
        await message.reply_text("هذه الدفعة مرتبطة بتسديد سابق؛ لم أكرر التسديد.")
        return
    matching = [d for d in debts if d["status"] in OPEN_STATUSES and d["remaining"] > 0 and _same_product(d["product"], product)]
    if len(matching) != 1:
        await message.reply_text("⚠️ يجب تحديد دين واحد مطابق. راجع منظومة الديون؛ ما غيرت أي سجل.")
        return
    debt_info = matching[0]["row_number"], matching[0]["remaining"]
    row_product = payment_row[SHEET_COL_PRODUCT - 1].strip()
    requested_catalog = _services.catalog_product(product)
    row_catalog = _services.catalog_product(row_product)
    same_product = row_product.casefold() == product.casefold() or bool(
        requested_catalog and row_catalog
        and requested_catalog.get("id") == row_catalog.get("id")
    )
    if not same_product:
        await message.reply_text(
            f"⚠️ آخر دفعة لهذا الزبون منتجها {row_product}، مو {product}.\n"
            "ما غيرت أي سجل."
        )
        return
    try:
        paid_amount = int(float(payment_row[SHEET_COL_TOTAL - 1].replace(",", "")))
    except (ValueError, IndexError):
        await message.reply_text("⚠️ مبلغ آخر دفعة غير صحيح؛ ما غيرت أي سجل.")
        return
    debt_row, remaining_before = debt_info
    credit_overage = max(0, paid_amount - remaining_before)
    if credit_overage:
        await message.reply_text("⚠️ الدفعة أكبر من الدين. راجع التسديد من منظومة الديون لتحديد مصير الزيادة؛ ما غيرت أي سجل.")
        return
    saved, new_remaining = process_debt_repayment(debt_row, remaining_before, paid_amount, operation_id=operation_id)
    if not saved:
        await message.reply_text("⚠️ تعذر تحديث الدين؛ ما تم إضافة أي دفعة.")
        return
    status = "تم تسديده بالكامل" if new_remaining <= 0 else f"بقي {new_remaining}"
    await message.reply_text(
        f"✅ تم ربط الدفعة المسجلة مسبقاً بالدين.\n"
        f"الزبون: {customer_chat_id}\nالمنتج: {product}\n"
        f"المبلغ: {paid_amount}\nالنتيجة: {status}\n"
        f"سطر الدفعة #{payment_row_number} بقي كما هو، وما انضاف دخل ثانٍ."
    )



def build_payment_purpose_keyboard() -> _services.markup:
    return _services.markup([
        [_services.button("💳 تسديد دين", callback_data="pay_purpose_debt")],
        [_services.button("🛍️ شراء جديد", callback_data="pay_purpose_purchase")],
        [_services.button("❌ إلغاء", callback_data="pay_cancel")],
    ])



def build_open_debts_keyboard(debts: list[dict]) -> _services.markup:
    rows = []
    for debt in debts:
        label = debt["product"]
        if debt.get("plan_name"):
            label += f" — {debt['plan_name']}"
        label += f" — المتبقي {_services.format_money(debt['remaining'])} د.ع"
        rows.append([_services.button(label[:60], callback_data=f"pay_debt_select_{debt['row_number']}")])
    rows.append([_services.button(BTN_BACK, callback_data="pay_debt_select_back")])
    return _services.markup(rows)



def build_debt_payment_ready_keyboard() -> _services.markup:
    return _services.markup([
        [_services.button("✏️ تعديل المبلغ", callback_data="pay_amount_manual")],
        [_services.button("✅ تثبيت تسديد الدين", callback_data="pay_debt_repay")],
        [_services.button(BTN_BACK, callback_data="pay_back_from_catalog_payment")],
    ])



def payment_state_has_matching_debt(state: dict) -> bool:
    """هل للزبون دين مفتوح يطابق المنتج المحدد في عملية الدفع؟"""
    if state.get("payment_purpose") == "purchase":
        return False
    customer_chat_id = state.get("customer_chat_id")
    product = state.get("product")
    return bool(
        product
        and customer_chat_id is not None
        and find_unpaid_debt(customer_chat_id, product) is not None
    )



def build_debt_overpayment_keyboard(overage: int) -> _services.markup:
    """يترك للأونر قرار مصير الزيادة بعد تسديد الدين."""
    return _services.markup([
        [_services.button(
            f"💰 حفظ {overage} رصيداً للزبون",
            callback_data="pay_debt_overage_credit",
        )],
        [_services.button(
            "✅ احتساب كامل المبلغ بدون رصيد",
            callback_data="pay_debt_overage_full",
        )],
        [_services.button(BTN_BACK, callback_data="pay_back_from_amount_edit")],
    ])



def build_link_debt_keyboard(customer_chat_id: int) -> _services.markup:
    """سؤال الدين يحمل chat_id داخل الزر حتى لا يضيع بعد إعادة تشغيل البوت."""
    return _services.markup([[
        _services.button("✅ نعم، دين", callback_data=f"linkdebt_yes_{customer_chat_id}"),
        _services.button("❌ لا، مو دين", callback_data=f"linkdebt_no_{customer_chat_id}"),
    ]])



def get_active_link_debt_catalog_plans() -> list[tuple[dict, dict]]:
    """باقات ChatGPT الفعالة ذات المدة، من كاتالوج الأونر فقط."""
    options = []
    for product in _services.products():
        if not product.get("is_active") or not _services.is_chatgpt_product(product):
            continue
        for plan in _services.plans(str(product["id"])):
            duration = plan.get("duration")
            if plan.get("is_active") and (
                _services.duration_days(duration) or _services.permanent_duration(duration)
            ):
                options.append((product, plan))
    return options



def build_link_debt_plan_keyboard(customer_chat_id: int) -> _services.markup:
    """لا يعرض إلا الباقات الفعالة التي أضافها الأونر للـChatGPT."""
    return _services.markup([
        [_services.button(
            f"{product['name']} — {plan['name']} ({plan.get('duration') or 'دائم'})",
            callback_data=f"linkplan_{plan['id']}_{customer_chat_id}",
        )]
        for product, plan in get_active_link_debt_catalog_plans()
    ])



def build_debt_product_keyboard() -> _services.markup:
    """شاشة اختيار منتج الدين من الكاتالوج مع إدخال حر."""
    products = _active_products()
    rows = [
        [_services.button(product["name"], callback_data=f"debt_catalog_{product['id']}")]
        for product in products
    ]
    if not rows:
        rows.append([_services.button("⚠️ ماكو منتجات مفعلة بالكاتالوج", callback_data="debt_noop")])
    rows.append([_services.button("✏️ كتابة حرة", callback_data="debt_product_manual")])
    rows.append([_services.button(BTN_BACK, callback_data="debt_back_to_main")])
    return _services.markup(rows)



def build_debt_plan_keyboard(product_id: str) -> _services.markup:
    plans = [plan for plan in _services.plans(product_id) if plan.get("is_active")]
    rows = [[_services.button(
        f"{plan['name']} — {_services.format_money(int(plan.get('price') or 0))} د.ع",
        callback_data=f"debt_plan_{plan['id']}",
    )] for plan in plans]
    rows.append([_services.button(BTN_BACK, callback_data="debt_back_to_product")])
    return _services.markup(rows)



def build_debt_main_keyboard() -> _services.markup:
    """القائمة الأولى للدين من زر لوحة الأونر."""
    return _services.markup([
        [_services.button("📋 المديونون والمبالغ المتبقية", callback_data="debt_list_open_0")],
        [_services.button("🔎 بحث عن زبون", callback_data="debt_search")],
        [_services.button("➕ تسجيل دين", callback_data="debt_menu_new")],
        [_services.button("💵 تسديد دين", callback_data="debt_list_repay_0")],
        [_services.button("🧾 سجل الديون المسددة", callback_data="debt_list_paid_0")],
        [_services.button("🔄 تحديث", callback_data="debt_main")],
    ])



def build_debt_product_list_keyboard() -> _services.markup:
    """قائمة كل المنتجات ما عدا الاختيار السريع + زر رجوع، لمنتج الدين."""
    rows = [
        [_services.button(p, callback_data=f"debt_product_{p}")]
        for p in _legacy_product_names()[1:]
    ]
    rows.append([_services.button(BTN_BACK, callback_data="debt_back_to_product")])
    return _services.markup(rows)



def build_debt_amount_keyboard() -> _services.markup:
    """الملخص الأخير: السعر يأتي من الباقة ولا يحتاج ضغطات زائدة."""
    return _services.markup([
        [_services.button("✏️ تعديل المبلغ", callback_data="debt_amount_manual")],
        [_services.button("✅ تثبيت الدين", callback_data="debt_amount_commit")],
        [_services.button(BTN_BACK, callback_data="debt_back_to_plan")],
        [_services.button("❌ إلغاء", callback_data="debt_cancel")],
    ])



async def start_debt_for_customer(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    customer_name: str,
    customer_username: str | None,
) -> None:
    """يفتح فلو الدين للأونر، مع تعبئة زبون المحادثة الحالية تلقائياً."""
    global _pending_debt
    debt = {
        "debt_id": f"DEBT-{uuid.uuid4().hex[:12].upper()}",
        "message_id": None,
        "step": "product",
        "chat_id": chat_id,
        "customer_line": _services.format_customer(customer_name, customer_username),
        "product": None,
        "product_id": None,
        "plan_id": None,
        "plan_name": None,
        "amount": 0,
        "awaiting_manual_product": False,
        "awaiting_manual_amount": False,
    }
    sent = await context.bot.send_message(
        chat_id=_services.owner_id,
        text=format_debt_summary(debt),
        reply_markup=build_debt_product_keyboard(),
    )
    debt["message_id"] = sent.message_id
    _pending_debt = debt



def format_debt_summary(debt: dict) -> str:
    """يبني نص الملخص المعروض فوق أزرار تسجيل الدين."""
    lines = ["تسجيل دين"]
    customer_line = debt.get("customer_line")
    product = debt.get("product")
    plan_name = debt.get("plan_name")
    amount = debt.get("amount", 0)
    lines.append(f"الزبون: {customer_line if customer_line else '— لم يُحدد بعد —'}")
    lines.append(f"المنتج: {product if product else '— لم يُحدد بعد —'}")
    lines.append(f"الباقة: {plan_name if plan_name else '—'}")
    lines.append(f"المبلغ: {amount if amount else '— لم يُحدد بعد —'}")
    return "\n".join(lines)



async def send_debt_notification(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """يبعث إشعار دين (تسجيل جديد أو تسديد) لفرع 'ديون' بقروب الإشعارات."""
    try:
        await context.bot.send_message(
            chat_id=_services.notification_chat_id, message_thread_id=_services.notification_topic_id, text=text
        )
    except Exception:
        logger.exception("Failed to send debt notification to topic")



def is_debt_payment_row(row: list[str]) -> bool:
    """أسطر الدين غير المحصل والدفعات الملغاة ليست دخلاً."""
    return len(row) > 2 and (row[2].strip().startswith("دين") or row[2].strip() == "ملغاة")


def is_debt_repayment_row(row: list[str]) -> bool:
    """Collected repayments count as income, but cannot fund another delivery."""
    return len(row) > 7 and str(row[7]).startswith("PAY-DEBT-")



async def _handle_debt_creation_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """يعالج كل ضغطات الأزرار الخاصة بفلو تسجيل دين جديد."""
    global _pending_debt

    query = update.callback_query
    if query.from_user.id != _services.owner_id:
        await query.answer("هذا الزر مخصص للأونر بس.", show_alert=True)
        return

    data = query.data
    await query.answer()

    if data == "debt_menu_new":
        sent = await query.edit_message_text("أرسل chat_id تبع الزبون (رقم فقط):", reply_markup=_keyboard([[(BTN_BACK, "debt_main")]]))
        _pending_debt = {
            "debt_id": f"DEBT-{uuid.uuid4().hex[:12].upper()}",
            "message_id": sent.message_id,
            "step": "chat_id",
            "chat_id": None,
            "customer_line": None,
            "product": None,
            "product_id": None,
            "plan_id": None,
            "plan_name": None,
            "amount": 0,
            "awaiting_manual_product": False,
            "awaiting_manual_amount": False,
        }
        return

    if data == "debt_back_to_main":
        _pending_debt = None
        await query.edit_message_text("💳 الدين", reply_markup=build_debt_main_keyboard())
        return

    if data == "debt_cancel":
        if _pending_debt and _pending_debt.get("message_id") != query.message.message_id:
            await query.answer("هذا الزر يخص عملية أقدم.", show_alert=True)
            return
        _pending_debt = None
        await query.edit_message_text("❌ تم إلغاء تسجيل الدين.")
        return

    if _pending_debt is None or _pending_debt.get("message_id") != query.message.message_id:
        await query.answer("انتهت صلاحية هذي العملية أو تم التعامل معها.", show_alert=True)
        return

    debt = _pending_debt

    if data.startswith("debt_catalog_"):
        product_id = data[len("debt_catalog_"):]
        product = _services.product(product_id)
        if not product or not product.get("is_active"):
            await query.answer("هذا المنتج غير متاح حالياً في الكاتالوج.", show_alert=True)
            return
        debt.update({
            "product": product["name"], "product_id": str(product["id"]),
            "plan_id": None, "plan_name": None, "amount": 0,
        })
        plans = [plan for plan in _services.plans(str(product["id"])) if plan.get("is_active")]
        if len(plans) == 1:
            plan = plans[0]
            debt.update({
                "plan_id": str(plan["id"]), "plan_name": plan["name"],
                "amount": int(plan.get("price") or 0), "step": "amount",
            })
            await query.edit_message_text(
                text=format_debt_summary(debt), reply_markup=build_debt_amount_keyboard()
            )
        elif plans:
            debt["step"] = "plan"
            await query.edit_message_text(
                text=format_debt_summary(debt) + "\n\nاختَر الباقة:",
                reply_markup=build_debt_plan_keyboard(str(product["id"])),
            )
        else:
            debt["step"] = "amount"
            await query.edit_message_text(
                text=format_debt_summary(debt), reply_markup=build_debt_amount_keyboard()
            )
        return

    if data.startswith("debt_plan_"):
        plan_id = data[len("debt_plan_"):]
        plans = [
            plan for plan in _services.plans(str(debt.get("product_id") or ""))
            if str(plan.get("id")) == plan_id and plan.get("is_active")
        ]
        if not plans:
            await query.answer("هذه الباقة لم تعد مفعلة.", show_alert=True)
            return
        plan = plans[0]
        debt.update({
            "plan_id": str(plan["id"]), "plan_name": plan["name"],
            "amount": int(plan.get("price") or 0), "step": "amount",
        })
        await query.edit_message_text(
            text=format_debt_summary(debt), reply_markup=build_debt_amount_keyboard()
        )
        return

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
        debt.update({
            "product_id": None, "plan_id": None, "plan_name": None, "amount": 0,
            "step": "product", "awaiting_manual_amount": False,
        })
        debt["awaiting_manual_product"] = True
        await query.edit_message_text(
            text=format_debt_summary(debt) + "\n\nاكتب اسم المنتج بالرسالة الجاية (كـ رد على هذي الرسالة):",
            reply_markup=_services.markup([[
                _services.button(BTN_BACK, callback_data="debt_back_to_product")
            ]]),
        )
        return

    if data == "debt_back_to_product":
        debt.update({
            "step": "product", "product": None, "product_id": None,
            "plan_id": None, "plan_name": None, "amount": 0,
            "awaiting_manual_product": False, "awaiting_manual_amount": False,
        })
        await query.edit_message_text(text=format_debt_summary(debt), reply_markup=build_debt_product_keyboard())
        return

    if data == "debt_back_to_plan":
        plans = [plan for plan in _services.plans(str(debt.get("product_id") or "")) if plan.get("is_active")] if debt.get("product_id") else []
        if len(plans) > 1:
            debt["step"] = "plan"
            await query.edit_message_text(text=format_debt_summary(debt) + "\n\nاختَر الباقة:", reply_markup=build_debt_plan_keyboard(str(debt["product_id"])))
        else:
            debt["step"] = "product"
            await query.edit_message_text(text=format_debt_summary(debt), reply_markup=build_debt_product_keyboard())
        return

    if data == "debt_amount_edit_back":
        debt["awaiting_manual_amount"] = False
        await query.edit_message_text(text=format_debt_summary(debt), reply_markup=build_debt_amount_keyboard())
        return

    if data == "debt_amount_add_small":
        debt["amount"] = debt.get("amount", 0) + 1000
        await query.edit_message_text(text=format_debt_summary(debt), reply_markup=build_debt_amount_keyboard())
        return

    if data == "debt_amount_add_large":
        debt["amount"] = debt.get("amount", 0) + 5000
        await query.edit_message_text(text=format_debt_summary(debt), reply_markup=build_debt_amount_keyboard())
        return

    if data == "debt_amount_manual":
        debt["awaiting_manual_amount"] = True
        await query.edit_message_text(
            text=format_debt_summary(debt) + "\n\nاكتب المبلغ رقم بس بالرسالة الجاية (كـ رد على هذي الرسالة):",
            reply_markup=_services.markup([[
                _services.button(BTN_BACK, callback_data="debt_amount_edit_back")
            ]]),
        )
        return

    if data == "debt_amount_commit":
        if not debt.get("amount") or not debt.get("product") or debt.get("chat_id") is None:
            await query.answer("لازم تحدد الزبون والمنتج والمبلغ قبل التثبيت.", show_alert=True)
            return

        saved_debt = append_debt_row(
            debt["chat_id"], debt["customer_line"], debt["product"], debt["amount"],
            product_id=debt.get("product_id"), plan_id=debt.get("plan_id"),
            plan_name=debt.get("plan_name"), debt_id=debt.get("debt_id"),
        )

        if saved_debt:
            final_text = format_debt_summary(debt) + "\n\n✅ تم تسجيل الدين بنجاح."
            await send_debt_notification(
                context,
                f"💳 دين جديد\nالزبون: {debt['customer_line']}\nالمنتج: {debt['product']}\nالمبلغ: {debt['amount']}",
            )
        else:
            await query.edit_message_text(
                text=format_debt_summary(debt) + "\n\n⚠️ تعذر تأكيد الحفظ. أعد التثبيت؛ لن يتكرر الدين.",
                reply_markup=build_debt_amount_keyboard(),
            )
            return

        try:
            await query.edit_message_text(text=final_text, reply_markup=build_debt_main_keyboard())
        except Exception:
            logger.exception("Failed to update final debt confirmation message")
        _pending_debt = None
        return



async def handle_debt_chat_id_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    يلتقط ردك الأول بفلو تسجيل الدين (chat_id الزبون كرد على الرسالة
    اللي طلبته). يتحقق من صحة الرقم، يجيب اسم/يوزر الزبون من تيليجرام
    لو ممكن، وينتقل لخطوة اختيار المنتج.
    """
    global _pending_debt

    message = update.message
    if not message or not message.text:
        return False
    if _pending_debt is None:
        return False
    if message.reply_to_message and _pending_debt.get("message_id") != message.reply_to_message.message_id:
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
        customer_line = _services.format_customer(customer_name, customer_username)
    except Exception:
        customer_line = f"chat_id: {chat_id}"

    _pending_debt["chat_id"] = chat_id
    _pending_debt["customer_line"] = customer_line
    _pending_debt["step"] = "product"

    try:
        await context.bot.edit_message_text(
            chat_id=_services.owner_id,
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
    if not message or not message.text:
        return False
    if _pending_debt is None:
        return False
    if message.reply_to_message and _pending_debt.get("message_id") != message.reply_to_message.message_id:
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
                chat_id=_services.owner_id,
                message_id=debt["message_id"],
                text=format_debt_summary(debt),
                reply_markup=build_debt_amount_keyboard(),
            )
        except Exception:
            logger.exception("Failed to update debt message after manual product entry")
        return True

    if debt.get("awaiting_manual_amount"):
        amount = parse_sheet_amount(message.text)
        if amount is None:
            await message.reply_text("الرجاء إدخال رقم صحيح فقط.")
            return True
        if amount <= 0:
            await message.reply_text("الرجاء إدخال مبلغ أكبر من صفر.")
            return True

        debt["amount"] = amount
        debt["awaiting_manual_amount"] = False
        try:
            await context.bot.edit_message_text(
                chat_id=_services.owner_id,
                message_id=debt["message_id"],
                text=format_debt_summary(debt),
                reply_markup=build_debt_amount_keyboard(),
            )
        except Exception:
            logger.exception("Failed to update debt message after manual amount entry")
        return True

    return False


def _keyboard(rows):
    return _services.markup([
        [_services.button(label, callback_data=callback) for label, callback in row]
        for row in rows
    ])


def _money(amount):
    return "غير محفوظ بالسجل القديم" if amount is None else f"{_services.format_money(amount)} د.ع"


async def show_debt_main(message, *, edit=False):
    try:
        debts = list_debts()
    except Exception:
        logger.exception("Cannot load debt dashboard")
        await message.reply_text("⚠️ تعذر تحميل الديون. أعد المحاولة؛ لم أعتبر السجلات فارغة.", reply_markup=build_debt_main_keyboard())
        return
    opened = [d for d in debts if d["status"] in OPEN_STATUSES and d["remaining"] > 0]
    content = ("💳 منظومة الديون\n\n"
               f"إجمالي المتبقي: {_money(sum(d['remaining'] for d in opened))}\n"
               f"المديونون: {len({d['chat_id'] for d in opened})}\n"
               f"الديون المفتوحة: {len(opened)}\n"
               f"الديون المسددة: {sum(d['status'] == 'مدفوع' for d in debts)}")
    if edit:
        await message.edit_text(content, reply_markup=build_debt_main_keyboard())
    else:
        await message.reply_text(content, reply_markup=build_debt_main_keyboard())


async def _show_list(query, mode="open", page=0, *, debts=None, title=None, page_prefix=None):
    if debts is None:
        debts = list_debts()
        debts = [d for d in debts if (d["status"] == "مدفوع" if mode == "paid"
                 else d["status"] in OPEN_STATUSES and d["remaining"] > 0)]
    page = max(0, min(page, max(0, (len(debts) - 1) // 8)))
    buttons = []
    for debt in debts[page * 8:(page + 1) * 8]:
        callback = "debt_repay" if mode == "repay" else "debt_view"
        label = f"{debt['customer_line'] or debt['chat_id']} | {debt['product']} | {_money(debt['remaining'])}"
        buttons.append([(label[:90], f"{callback}_{debt['chat_id']}_{debt['row_number']}")])
    navigation = []
    page_prefix = page_prefix or f"debt_list_{mode}_"
    if page:
        navigation.append(("السابق", f"{page_prefix}{page - 1}"))
    if (page + 1) * 8 < len(debts):
        navigation.append(("التالي", f"{page_prefix}{page + 1}"))
    if navigation:
        buttons.append(navigation)
    buttons.append([(BTN_BACK, "debt_main")])
    heading = title or ("اختَر الدين لتسديده" if mode == "repay" else "الديون المسددة" if mode == "paid" else "الديون المفتوحة")
    await query.edit_message_text(
        heading + f"\nعدد السجلات: {len(debts)}\n" + ("اختَر السجل لعرض التفاصيل." if debts else "ماكو سجلات."),
        reply_markup=_keyboard(buttons),
    )


def _get_debt(chat_id, row_number):
    return next((d for d in list_debts(chat_id) if d["row_number"] == row_number), None)


async def _show_detail(query, debt):
    buttons = []
    if debt["remaining"] > 0 and debt["status"] in OPEN_STATUSES:
        buttons.append([("💵 تسديد هذا الدين", f"debt_repay_{debt['chat_id']}_{debt['row_number']}")])
    buttons += [[("➕ دين جديد لنفس الزبون", f"debt_new_customer_{debt['chat_id']}")],
                [("📋 كل ديون الزبون", f"debt_customer_{debt['chat_id']}")], [(BTN_BACK, "debt_main")]]
    await query.edit_message_text(
        f"💳 تفاصيل الدين\nالزبون: {debt['customer_line']}\nالمعرف: {debt['chat_id']}\n"
        f"المنتج: {debt['product']}\nالباقة: {debt['plan_name'] or '—'}\n"
        f"المبلغ الأصلي: {_money(debt['original_amount'])}\nالمسدد: {_money(debt['paid_total'])}\n"
        f"المتبقي: {_money(debt['remaining'])}\nالحالة: {debt['status']}\n"
        f"تاريخ الدين: {debt['created_at']}\nآخر تسديد: {debt['last_paid_at'] or '—'}\n"
        f"رقم الدين: {debt['debt_id']}", reply_markup=_keyboard(buttons),
    )


def _select_debt(state, debt):
    state.update({
        "payment_purpose": "debt", "selected_debt_row": debt["row_number"],
        "selected_debt_id": debt["debt_id"], "product": debt["product"],
        "catalog_product_id": debt["product_id"] or None, "plan_id": debt["plan_id"] or None,
        "plan_name": debt["plan_name"] or None, "plan_price": debt["remaining"],
        "selected_debt_remaining": debt["remaining"], "manual_product": False,
    })


def _wallet_keyboard(state):
    state["wallet_options"] = list(_services.payment_methods())
    rows = [[(method, f"debt_wallet_{index}")] for index, method in enumerate(state["wallet_options"])]
    rows.append([(BTN_BACK, "debt_main")])
    return _keyboard(rows)


def _repayment_ready_keyboard(state):
    if state.get("debt_income_saved"):
        return _keyboard([[("🔄 إكمال التسديد", "debt_payment_confirm")]])
    return _keyboard([
        [("✏️ تعديل المبلغ", "debt_payment_edit")],
        [("✅ تثبيت العملية", "debt_payment_confirm")],
        [(BTN_BACK, "debt_wallet_back")],
    ])


async def _edit_payment(query, content, markup=None):
    if getattr(query.message, "photo", None) or getattr(query.message, "caption", None) is not None:
        await query.edit_message_caption(caption=content, reply_markup=markup)
    else:
        await query.edit_message_text(text=content, reply_markup=markup)


async def settle_debt_payment(query, context, state, *, direct=False):
    """Shared settlement for receipts/AC and the debt dashboard."""
    confirm = "debt_payment_confirm" if direct else "pay_debt_repay"
    if not state.get("payments"):
        await query.answer("اختَر طريقة الدفع والمبلغ أولاً.", show_alert=True)
        return False
    chat_id = int(state["customer_chat_id"])
    selected_row = state.get("selected_debt_row")
    if selected_row is None:
        debts = [d for d in list_open_debts(chat_id) if _same_product(d["product"], state["product"])]
        if len(debts) != 1:
            await _edit_payment(query, "اختَر الدين بالتحديد قبل التسديد.", build_open_debts_keyboard(debts))
            return False
        _select_debt(state, debts[0])
        selected_row = debts[0]["row_number"]
    debt = _get_debt(chat_id, int(selected_row))
    if not debt or debt["debt_id"] != state.get("selected_debt_id"):
        await query.answer("سجل الدين تغيّر. أعد اختياره.", show_alert=True)
        return False
    operation = state.setdefault("payment_operation_id", f"PAY-DEBT-{uuid.uuid4().hex.upper()}")
    already_applied = debt["last_operation_id"] == operation
    remaining = int(state.get("settlement_remaining", state.get("selected_debt_remaining", debt["remaining"])))
    if not already_applied and (debt["status"] not in OPEN_STATUSES or debt["remaining"] != remaining):
        await query.answer("المتبقي تغيّر أو الدين مسدد. أعد فتحه لمراجعة المبلغ.", show_alert=True)
        return False
    received = sum(amount for _, amount in state["payments"])
    overage = max(0, received - remaining)
    if overage and not state.get("debt_overage_decision"):
        keyboard = _keyboard([
            [(f"💰 حفظ {_money(overage)} رصيداً", "debt_over_credit")],
            [("✅ احتساب كامل المبلغ بدون رصيد", "debt_over_full")],
            [(BTN_BACK, "debt_payment_ready")],
        ]) if direct else build_debt_overpayment_keyboard(overage)
        await _edit_payment(query, _services.payment_summary(state) + f"\n\nالمتبقي: {_money(remaining)}\nالزيادة: {_money(overage)}\nشنو تريد تسوي بالزيادة؟", keyboard)
        return False
    if not state.get("debt_income_saved"):
        if not _services.record_income(state, force_new=True):
            await _edit_payment(query, _services.payment_summary(state) + "\n\n⚠️ فشل تأكيد الدخل؛ لم يتغير الدين.", _keyboard([[("🔄 إعادة المحاولة", confirm)]]))
            return False
        state["debt_income_saved"] = True
    state["settlement_remaining"] = remaining
    saved, new_remaining = process_debt_repayment(selected_row, remaining, received, operation_id=operation)
    if not saved:
        await _edit_payment(query, "⚠️ الدخل محفوظ، وتعذر إكمال تحديث الدين. أعد المحاولة؛ لن يتكرر الدخل.", _keyboard([[("🔄 إكمال العملية", confirm)]]))
        return False
    applied_wallets = state.setdefault("debt_wallets_applied", [])
    for index, (method, amount) in enumerate(state["payments"]):
        if method in _services.vault_names and index not in applied_wallets:
            if not _services.adjust_vault(method, amount):
                await _edit_payment(query, "⚠️ الدخل والدين محفوظان؛ تعذر تحديث الخزنة. أكمل المحاولة.", _keyboard([[("🔄 إكمال العملية", confirm)]]))
                return False
            applied_wallets.append(index)
    if overage and state.get("debt_overage_decision") == "credit" and not state.get("debt_credit_saved"):
        success, balance = _services.add_credit(chat_id, _services.format_customer(state["customer_name"], state.get("customer_username")), overage, state["product"], f"فرق تسديد الدين {debt['debt_id']} — {operation}")
        if not success:
            await _edit_payment(query, "⚠️ التسديد محفوظ؛ تعذر حفظ الرصيد الزائد. أكمل المحاولة.", _keyboard([[("🔄 إكمال العملية", confirm)]]))
            return False
        state["debt_credit_saved"] = True
        state["debt_credit_balance"] = balance
    result = "✅ تم تسديد الدين بالكامل." if new_remaining == 0 else f"✅ تم تسديد جزء من الدين. المتبقي: {_money(new_remaining)}"
    if overage:
        result += f"\nالرصيد الزائد المحفوظ: {_money(overage)}" if state.get("debt_overage_decision") == "credit" else f"\nتم احتساب الزيادة {_money(overage)} ضمن كامل الدفعة."
    await send_debt_notification(context, f"{result}\nالزبون: {state['customer_name']} ({chat_id})\nالمنتج: {state['product']}\nالمستلم: {_money(received)}")
    await _edit_payment(query, _services.payment_summary(state) + "\n\n" + result, build_debt_main_keyboard() if direct else None)
    return True


async def handle_debt_callback(update, context):
    global _pending_debt
    query = update.callback_query
    if query.from_user.id != _services.owner_id:
        await query.answer("هذا الزر مخصص للأونر بس.", show_alert=True)
        return
    data = query.data or ""
    try:
        if data in {"debt_main", "debt_back_to_main"}:
            _pending_debt = None
            context.user_data.pop("debt_search", None)
            await query.answer()
            await show_debt_main(query.message, edit=True)
        elif data.startswith("debt_list_") or data == "debt_menu_repay":
            _pending_debt = None
            context.user_data.pop("debt_search", None)
            await query.answer()
            _, _, mode, page = data.split("_", 3) if data.startswith("debt_list_") else ("", "", "repay", "0")
            await _show_list(query, mode, int(page))
        elif data == "debt_search":
            _pending_debt = None
            context.user_data["debt_search"] = query.message.message_id
            await query.answer()
            await query.edit_message_text("اكتب اسم الزبون أو chat_id للبحث:", reply_markup=_keyboard([[(BTN_BACK, "debt_main")]]))
        elif data.startswith("debt_customer_"):
            await query.answer()
            parts = data[len("debt_customer_"):].split("_")
            chat_id, page = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
            await _show_list(query, page=page, debts=list_debts(chat_id), title=f"ديون الزبون {chat_id}", page_prefix=f"debt_customer_{chat_id}_")
        elif data.startswith("debt_new_customer_"):
            chat_id = int(data[len("debt_new_customer_"):])
            name, username = _services.identity(chat_id)
            await query.answer()
            await start_debt_for_customer(context, chat_id, name, username)
        elif data.startswith(("debt_view_", "debt_repay_")):
            _, action, chat, row = data.split("_", 3)
            debt = _get_debt(int(chat), int(row))
            if not debt:
                await query.answer("الدين لم يعد موجوداً؛ حدّث القائمة.", show_alert=True)
                return
            await query.answer()
            if action == "view":
                await _show_detail(query, debt)
            elif debt["remaining"] > 0 and debt["status"] in OPEN_STATUSES:
                state = {"customer_chat_id": debt["chat_id"], "customer_name": debt["customer_line"], "payments": []}
                _select_debt(state, debt)
                _pending_repayments[query.message.message_id] = state
                await query.edit_message_text(_services.payment_summary(state) + "\n\nاختَر طريقة الدفع:", reply_markup=_wallet_keyboard(state))
        elif data.startswith("debt_wallet_") or data.startswith("debt_payment_") or data in {"debt_over_credit", "debt_over_full"}:
            state = _pending_repayments.get(query.message.message_id)
            if not state:
                await query.answer("انتهت صلاحية العملية. أعد فتح الدين.", show_alert=True)
                return
            if state.get("debt_income_saved") and data != "debt_payment_confirm":
                await query.answer("الدخل محفوظ؛ أكمل التسديد الحالي دون تغيير المبلغ.", show_alert=True)
                return
            await query.answer()
            if data == "debt_wallet_back":
                state["payments"] = []
                state.pop("debt_overage_decision", None)
                await query.edit_message_text(_services.payment_summary(state), reply_markup=_wallet_keyboard(state))
            elif data.startswith("debt_wallet_"):
                method = state["wallet_options"][int(data[len("debt_wallet_"):])]
                state["payments"] = [(method, state["selected_debt_remaining"])]
                await query.edit_message_text(_services.payment_summary(state), reply_markup=_repayment_ready_keyboard(state))
            elif data == "debt_payment_edit":
                state["awaiting_amount"] = True
                await query.edit_message_text(_services.payment_summary(state) + "\n\nاكتب المبلغ المستلم فعلياً:", reply_markup=_keyboard([[(BTN_BACK, "debt_payment_ready")]]))
            elif data == "debt_payment_ready":
                state["awaiting_amount"] = False
                await query.edit_message_text(_services.payment_summary(state), reply_markup=_repayment_ready_keyboard(state))
            else:
                if data.startswith("debt_over_"):
                    state["debt_overage_decision"] = "credit" if data == "debt_over_credit" else "full"
                message_id = query.message.message_id
                if message_id in _busy_payments:
                    return
                _busy_payments.add(message_id)
                try:
                    if await settle_debt_payment(query, context, state, direct=True):
                        _pending_repayments.pop(message_id, None)
                finally:
                    _busy_payments.discard(message_id)
        else:
            context.user_data.pop("debt_search", None)
            await _handle_debt_creation_callback(update, context)
    except Exception:
        logger.exception("Debt UI failed for %s", data)
        await query.message.reply_text("⚠️ تعذر قراءة أو تحديث الديون. أعد المحاولة؛ لا تعتبر العملية مثبتة قبل ظهور التأكيد.", reply_markup=build_debt_main_keyboard())


async def handle_debt_search_or_repayment_input(update, context):
    message = update.message
    if not message or not message.text or not update.effective_user or update.effective_user.id != _services.owner_id:
        return False
    if context.user_data.get("debt_search"):
        context.user_data.pop("debt_search", None)
        term = message.text.strip().casefold()
        debts = [d for d in list_debts() if term in str(d["chat_id"]) or term in d["customer_line"].casefold()]
        if not debts:
            await message.reply_text("ما لقيت زبون مطابق.", reply_markup=build_debt_main_keyboard())
        else:
            rows = [[(f"{d['customer_line']} — {d['product']} — {_money(d['remaining'])}"[:90], f"debt_view_{d['chat_id']}_{d['row_number']}")] for d in debts[:20]]
            rows.append([(BTN_BACK, "debt_main")])
            await message.reply_text(f"نتائج البحث: {len(debts)}" + (" (أول 20 سجل؛ اكتب المعرف لبحث أدق)" if len(debts) > 20 else ""), reply_markup=_keyboard(rows))
        return True
    candidates = [(key, state) for key, state in _pending_repayments.items() if state.get("awaiting_amount")]
    if not candidates:
        return False
    if message.reply_to_message:
        candidates = [(key, state) for key, state in candidates if key == message.reply_to_message.message_id]
    if len(candidates) != 1:
        return False
    key, state = candidates[0]
    amount = parse_sheet_amount(message.text)
    if not amount or not state.get("payments"):
        await message.reply_text("اكتب مبلغاً صحيحاً أكبر من صفر.")
        return True
    method = state["payments"][0][0]
    state["payments"] = [(method, amount)]
    state["awaiting_amount"] = False
    state.pop("debt_overage_decision", None)
    await context.bot.edit_message_text(chat_id=_services.owner_id, message_id=key, text=_services.payment_summary(state), reply_markup=_repayment_ready_keyboard(state))
    return True


async def handle_debt_shortcut(context, bm, chat_id, customer_name=None, customer_username=None):
    if bm is not None:
        try:
            await context.bot.delete_business_messages(business_connection_id=bm.business_connection_id, message_ids=[bm.message_id])
        except Exception:
            logger.warning("Could not immediately delete debt shortcut for %s", chat_id)
    if not customer_name:
        customer_name, customer_username = _services.identity(chat_id)
    await start_debt_for_customer(context, chat_id, customer_name, customer_username)


def cancel_debt_input(context):
    global _pending_debt
    _pending_debt = None
    context.user_data.pop("debt_search", None)
    for state in _pending_repayments.values():
        state["awaiting_amount"] = False


def _receipt_methods_keyboard():
    rows = [[(method, f"pay_method_{method}")] for method in _services.payment_methods()]
    rows.append([(BTN_BACK, "pay_debt_select_back")])
    return _keyboard(rows)


async def handle_payment_debt_callback(update, context, state, pending_payments):
    """Return True only when the callback belongs to the debt integration."""
    query = update.callback_query
    data = query.data or ""
    try:
        if state.get("debt_income_saved") and data not in {"pay_debt_repay", "pay_finalize"}:
            await query.answer("الدخل محفوظ؛ أكمل التسديد الحالي دون تغيير المبلغ أو الدين.", show_alert=True)
            return True
        if data == "pay_confirm":
            debts = list_open_debts(state.get("customer_chat_id")) if state.get("customer_chat_id") is not None else []
            if not debts:
                return False
            await _edit_payment(query, _services.payment_summary(state) + "\n\nهذا الزبون عليه دين. هذه الدفعة تسديد لو شراء جديد؟", build_payment_purpose_keyboard())
            return True
        if data == "pay_purpose_purchase":
            if state.get("debt_income_saved"):
                await query.answer("الدخل مسجل للتسديد؛ أكمل العملية الحالية.", show_alert=True)
                return True
            for key in list(state):
                if key.startswith(("debt_", "selected_debt", "settlement_")):
                    state.pop(key, None)
            state.update({"payment_purpose": "purchase", "product": None, "payments": [], "plan_price": None, "plan_id": None, "plan_name": None})
            await _edit_payment(query, _services.payment_summary(state) + "\n\nشراء جديد؛ الديون السابقة محفوظة.", _services.product_keyboard())
            return True
        if data == "pay_purpose_debt":
            debts = list_open_debts(state.get("customer_chat_id"))
            if not debts:
                await query.answer("ماكو دين مفتوح حالياً.", show_alert=True)
                return True
            if len(debts) > 1:
                await _edit_payment(query, "اختَر الدين الذي تريد تسديده:", build_open_debts_keyboard(debts))
                return True
            data = f"pay_debt_select_{debts[0]['row_number']}"
        if data == "pay_debt_select_back":
            if state.get("debt_income_saved"):
                await query.answer("أكمل التسديد المسجل أولاً.", show_alert=True)
                return True
            await _edit_payment(query, "هذه الدفعة تسديد دين لو شراء جديد؟", build_payment_purpose_keyboard())
            return True
        if data.startswith("pay_debt_select_"):
            debt = get_open_debt_by_row(state["customer_chat_id"], int(data[len("pay_debt_select_"):]))
            if not debt:
                await query.answer("هذا الدين مسدد أو تغيّر.", show_alert=True)
                return True
            _select_debt(state, debt)
            await _edit_payment(query, _services.payment_summary(state) + "\n\nاختَر طريقة الدفع:", _receipt_methods_keyboard())
            return True
        if state.get("payment_purpose") == "debt":
            if state.get("debt_income_saved") and data in {"pay_amount_manual", "pay_back_from_catalog_payment", "pay_back_to_method", "pay_cancel"}:
                await query.answer("الدخل محفوظ؛ أكمل التسديد دون تغيير المبلغ.", show_alert=True)
                return True
            if data.startswith("pay_method_") and data != "pay_method_list":
                method = data[len("pay_method_"):]
                if method not in _services.payment_methods():
                    await query.answer("طريقة الدفع غير متاحة.", show_alert=True)
                    return True
                state["payments"] = [(method, state["selected_debt_remaining"])]
                state["editing_payment_index"] = 0
                await _edit_payment(query, _services.payment_summary(state), build_debt_payment_ready_keyboard())
                return True
            if data in {"pay_back_from_catalog_payment", "pay_back_to_method", "pay_method_list"}:
                state["payments"] = []
                state.pop("editing_payment_index", None)
                state["awaiting_manual_amount"] = False
                await _edit_payment(query, _services.payment_summary(state), _receipt_methods_keyboard())
                return True
            if data == "pay_back_from_amount_edit":
                state["awaiting_manual_amount"] = False
                await _edit_payment(query, _services.payment_summary(state), build_debt_payment_ready_keyboard())
                return True
        if data in {"pay_debt_repay", "pay_debt_overage_credit", "pay_debt_overage_full"} or (data == "pay_finalize" and state.get("payment_purpose") == "debt"):
            if data.startswith("pay_debt_overage_"):
                state["debt_overage_decision"] = "credit" if data.endswith("credit") else "full"
            key = query.message.message_id
            if key in _busy_payments:
                await query.answer("العملية قيد الحفظ.")
                return True
            _busy_payments.add(key)
            try:
                if await settle_debt_payment(query, context, state):
                    pending_payments.pop(key, None)
            finally:
                _busy_payments.discard(key)
            return True
        return False
    except Exception:
        logger.exception("Receipt/debt integration failed")
        await _edit_payment(query, "⚠️ تعذر قراءة أو تحديث الديون؛ لم أعتمدها كشراء جديد. أعد المحاولة.", _keyboard([[("🔄 إعادة المحاولة", query.data)]]))
        return True


async def complete_link_debt_choice(query, context, state, chat_id, *, choice=None, plan_id=None):
    """Delivery integration, shared by /link and /addprivate."""
    if choice == "no":
        context.user_data.pop("pending_link_debt", None)
        await query.edit_message_text("تم الربط بدون تسجيل دين.")
        return
    if choice == "yes":
        state["is_debt"] = True
        state.setdefault("debt_id", f"DEBT-{uuid.uuid4().hex[:12].upper()}")
        context.user_data["pending_link_debt"] = state
        options = get_active_link_debt_catalog_plans()
        await query.edit_message_text("اختَر باقة الدين:" if options else "⚠️ ماكو باقات ChatGPT مفعلة ذات مدة. أضف باقة ثم أعد الربط.", reply_markup=build_link_debt_plan_keyboard(chat_id) if options else None)
        return
    options = get_active_link_debt_catalog_plans()
    selected = next(((product, plan) for product, plan in options if str(plan["id"]) == str(plan_id)), None)
    if not selected:
        await query.edit_message_text("⚠️ الباقة لم تعد متاحة. أعد الربط واختر باقة مفعلة.")
        return
    product, plan = selected
    permanent = _services.permanent_duration(plan.get("duration"))
    duration = _services.duration_days(plan.get("duration"))
    reminder = {**state, "product": product["name"], "plan_id": plan["id"], "plan_name": plan["name"],
                "plan_duration": plan.get("duration"), "duration_days": duration,
                "reminder_disabled": permanent, "is_debt": bool(state.get("is_debt", True))}
    if reminder["is_debt"]:
        debt_id = state.setdefault("debt_id", f"DEBT-{uuid.uuid4().hex[:12].upper()}")
        context.user_data["pending_link_debt"] = state
        if not append_debt_row(chat_id, _services.format_customer(state.get("customer_name"), state.get("customer_username")), product["name"], int(plan.get("price") or 0), product_id=str(product["id"]), plan_id=str(plan["id"]), plan_name=plan["name"], debt_id=debt_id):
            await query.edit_message_text("⚠️ تعذر حفظ الدين؛ لم أسجل الاشتراك. أعد المحاولة.", reply_markup=build_link_debt_plan_keyboard(chat_id))
            return
    if not _services.save_subscription(reminder):
        await query.edit_message_text("⚠️ الدين محفوظ، وتعذر تسجيل مدة الاشتراك. أعد اختيار الباقة؛ لن يتكرر الدين.", reply_markup=build_link_debt_plan_keyboard(chat_id))
        return
    scheduled = await _services.schedule_feedback(reminder)
    context.user_data.pop("pending_link_debt", None)
    await query.edit_message_text(f"✅ تم تفعيل {product['name']} — {plan['name']}.\n" + ("الدين محفوظ ضمن منظومة الديون.\n" if reminder["is_debt"] else "") + ("المتابعة محجوزة." if scheduled else "⚠️ المدة محفوظة، لكن تعذر حجز المتابعة."))

