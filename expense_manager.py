"""Owner expenses and linked financial refunds; never cancel other services."""
from __future__ import annotations

from copy import deepcopy
from financial_ledger import FinanceError, ledger, money, parse_wallets, transaction_date
from finance_ui import services, draft, navigate, display, customer_text, commit_card, suspend_finance_inputs

EXPENSE_KINDS = {'cost':'تكلفة منتج','operating':'تشغيل المتجر','fee':'رسوم وعمولات','general':'مصروف عام'}


async def start_expense(context, *, customer_id=None, customer_name=None, photo_message=None, refund=None, quick=None):
    ledger().require_active()
    suspend_finance_inputs()
    state=draft('expense',customer_id=customer_id,customer_name=customer_name,step='product',category='cost')
    if photo_message: state.update(photo=True,message_id=photo_message.message_id)
    if refund:
        state.update(kind='refund',reference_id=refund['id'],customer_id=refund.get('customer_id'),
                     product=refund.get('product'),description='استرجاع من '+refund['id'],amount=refund.get('sales',0),step='amount',return_to='finance_view_'+refund['id'])
    if quick: state.update(quick,step='summary')
    await render_expense(state,context)
    return state


def expense_payload(state):
    if not state.get('payments') or not state.get('description'):
        raise FinanceError('حدد المبلغ والمحفظة وسبب المصروف.')
    return {**{k:deepcopy(state[k]) for k in ('customer_id','customer_name','product','product_id','plan_id','plan_name','description','reference_id','category','proof_id') if k in state},
            'id':state['operation_id'],'kind':state['kind'],'occurred_at':state['occurred_at'],'payments':deepcopy(state['payments'])}


async def render_expense(state,context,query=None):
    text=('💸 استرجاع مرتبط بعملية' if state['kind']=='refund' else '💸 تسجيل مصروف')+'\n'+customer_text(state)
    step=state['step']; rows=[];state['awaiting']=False
    if step=='product':
        products=[p for p in services().products() if p.get('is_active')]
        state['product_options']=[str(p['id']) for p in products]
        rows=[[(p['name'],f'product{i}')] for i,p in enumerate(products)]
        rows += [[('تشغيل المتجر','categoryoperating'),('رسوم وعمولات','categoryfee')],[('✏️ سبب حر','free')]]
        text+='\nاختَر المنتج أو سبب المصروف:'
    elif step=='plan':
        plans=[p for p in services().plans(state['product_id']) if p.get('is_active')]
        state['plan_options']=[str(p['id']) for p in plans]
        rows=[[(p['name'],f'plan{i}')] for i,p in enumerate(plans)]
        rows += [[('تكلفة عامة لهذا المنتج','noplan')]]
        text+='\nاختَر الباقة لربط التكلفة بها، أو تكلفة عامة:'
    elif step=='wallet':
        state['wallet_options']=list(services().wallets())
        rows=[[(w,f'wallet{i}')] for i,w in enumerate(state['wallet_options'])]
        text+='\nاختَر المحفظة التي خرج منها المبلغ:'
    elif step in {'amount','free','date','split'}:
        text+='\n'+{'amount':'اكتب المبلغ المصروف فعلياً، مثل 15000 أو 15 ألف:',
                      'free':'اكتب سبب المصروف:', 'date':'اكتب تاريخ الصرف: 2026-10-28 14:30 بتوقيت بغداد:',
                      'split':'اكتب المحافظ والمبالغ: ماستر 5000 + زين كاش 3000'}[step]
        state['awaiting']=True
    else:
        state['step']='summary'
        text+=f"\nالنوع: {EXPENSE_KINDS.get(state.get('category'),'استرجاع')}\nالمنتج: {state.get('product') or 'عام'}\nالباقة: {state.get('plan_name') or '—'}\nالسبب: {state.get('description') or 'غير محدد'}\n"
        text+='\n'.join(f"{p['wallet']}: −{p['amount']:,}" for p in state.get('payments',[]))
        if state.get('reference_id'):text+=f"\nالعملية الأصلية: {state['reference_id']}\nهذا استرجاع مالي؛ لا يلغي حسابات الزبون الأخرى."
        rows=[[('✅ تثبيت العملية','commit')],[('تعديل المبلغ','editamount'),('تعديل السبب','free')],[('تاريخ الصرف','date'),('توزيع المحافظ','split')]]
    rows += [[('◀️ رجوع','back'),('إلغاء','cancel')]]
    await display(context,state,text[:4000],rows,query)


async def handle_expense_action(state,action,context,query):
    if action=='commit': await commit_card(state,expense_payload(state),context,query)
    elif action.startswith('product') and action[7:].isdigit():
        pid=state['product_options'][int(action[7:])]
        product=next((p for p in services().products() if str(p['id'])==pid and p.get('is_active')),None)
        if not product:raise FinanceError('المنتج لم يعد متاحاً.')
        state.update(product_id=pid,product=product['name'],description=product['name'],category='cost')
        plans=[p for p in services().plans(pid) if p.get('is_active')]
        if len(plans)==1:
            state.update(plan_id=str(plans[0]['id']),plan_name=plans[0]['name']);navigate(state,'amount')
        else:navigate(state,'plan' if plans else 'amount')
    elif action.startswith('plan') and action[4:].isdigit():
        pid=state['plan_options'][int(action[4:])]
        plan=next((p for p in services().plans(state['product_id']) if str(p['id'])==pid and p.get('is_active')),None)
        if not plan:raise FinanceError('الباقة تغيرت.')
        state.update(plan_id=pid,plan_name=plan['name']);navigate(state,'amount')
    elif action=='noplan':state.update(plan_id=None,plan_name=None);navigate(state,'amount')
    elif action.startswith('wallet') and action[6:].isdigit():
        state['payments']=[{'wallet':state['wallet_options'][int(action[6:])],'amount':state['amount']}];navigate(state,'summary')
    elif action.startswith('category'):
        state.update(category=action[8:],product=None,product_id=None);navigate(state,'free')
    elif action=='editamount':navigate(state,'amount')
    elif action in {'free','date','split'}:
        if action=='free' and not state.get('product') and state.get('category')=='cost':state['category']='general'
        navigate(state,action)


def expense_input(state,text):
    step=state['step']
    if step=='amount':
        amount=money(text)
        if amount<=0:raise FinanceError('المبلغ لازم أكبر من صفر.')
        state['amount']=amount
        if len(state.get('payments',[]))==1:
            state['payments'][0]['amount']=amount;navigate(state,'summary')
        else:navigate(state,'wallet')
    elif step=='free':
        if not text.strip():raise FinanceError('اكتب سبب المصروف.')
        state['description']=text.strip()
        if state['kind']=='refund':navigate(state,'amount' if not state.get('payments') else 'summary')
        elif state.get('payments'):navigate(state,'summary')
        else:navigate(state,'amount')
    elif step=='date':state['occurred_at']=transaction_date(text);navigate(state,'summary')
    elif step=='split':state['payments']=parse_wallets(text,services().wallets());navigate(state,'summary')


async def expense_shortcut(context,bm,customer_id,customer_name=None):
    # Delete before every lookup, database access or owner-card operation.
    try:
        await context.bot.delete_business_messages(business_connection_id=bm.business_connection_id,message_ids=[bm.message_id])
    except Exception:
        await context.bot.send_message(chat_id=services().owner_id,text='⚠️ تعذر حذف كلمة صرف؛ تحقق من صلاحية حذف الرسائل.')
    await start_expense(context,customer_id=customer_id,customer_name=customer_name)


# Existing Sheets path retained until explicit ledger activation.
LEGACY_DEPENDENCIES = ('BTN_BACK', 'Credentials', 'EXPENSES_WORKSHEET_NAME', 'GOOGLE_SERVICE_ACCOUNT_FILE', 'GOOGLE_SHEET_ID', 'InlineKeyboardButton', 'InlineKeyboardMarkup', 'NOTIFICATIONS_GROUP_ID', 'PAYMENT_AMOUNT_STEP_LARGE', 'PAYMENT_AMOUNT_STEP_SMALL', 'TOPIC_EXPENSES', 'VAULT_NAMES', '_expenses_sheet', 'datetime', 'get_active_catalog_payment_products', 'gspread', 'logger', 'require_legacy_write', 'timezone')

def configure_legacy(values):
    """Inject store adapters; feature modules never import bot.py."""
    globals().update({name: values[name] for name in LEGACY_DEPENDENCIES})

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


def build_compensation_confirmation_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تأكيد التعويض وإنهاء الخدمة", callback_data="exp_comp_confirm")],
        [InlineKeyboardButton("❌ إلغاء", callback_data="exp_cancel")],
    ])


def build_expense_photo_confirm_keyboard(can_compensate_customer: bool = False) -> InlineKeyboardMarkup:
    """زرين يطلعون تحت صورة أرسلتها أنت (owner) — هل هذي إثبات مصروف؟"""
    rows = [
        [
            InlineKeyboardButton("✅ مصروف", callback_data="expphoto_yes"),
            InlineKeyboardButton("❌ ليس مصروف", callback_data="expphoto_no"),
        ],
    ]
    if can_compensate_customer:
        rows.insert(0, [InlineKeyboardButton("💸 تعويض هذا الزبون", callback_data="expphoto_refund")])
    return InlineKeyboardMarkup(rows)


def build_expense_amount_keyboard() -> InlineKeyboardMarkup:
    """شاشة تحديد مبلغ المصروف — نفس أزرار مبلغ الدفع، بس callback_data مختلف."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"+{PAYMENT_AMOUNT_STEP_SMALL}", callback_data="exp_amount_add_small"),
            InlineKeyboardButton(f"+{PAYMENT_AMOUNT_STEP_LARGE}", callback_data="exp_amount_add_large"),
        ],
        [InlineKeyboardButton("✏️ إدخال يدوي", callback_data="exp_amount_manual")],
        [InlineKeyboardButton("✅ تثبيت المبلغ", callback_data="exp_amount_commit")],
        [InlineKeyboardButton("❌ إلغاء العملية", callback_data="exp_cancel")],
    ])


def build_expense_vault_keyboard() -> InlineKeyboardMarkup:
    """شاشة اختيار الخزنة اللي ينسحب منها مبلغ المصروف."""
    rows = [[InlineKeyboardButton(v, callback_data=f"exp_vault_{v}")] for v in VAULT_NAMES]
    rows.append([InlineKeyboardButton("بدون خزنة محددة", callback_data="exp_vault_none")])
    rows.append([InlineKeyboardButton(BTN_BACK, callback_data="exp_back_to_amount")])
    return InlineKeyboardMarkup(rows)


def build_expense_reason_keyboard() -> InlineKeyboardMarkup:
    """يعرض كل منتجات الكاتالوج المفعلة كأسباب مصروف، مع إدخال حر."""
    products = get_active_catalog_payment_products()
    rows = [
        [InlineKeyboardButton(
            product["name"], callback_data=f"exp_reason_catalog_{product['id']}"
        )]
        for product in products
    ]
    if not rows:
        rows.append([InlineKeyboardButton(
            "⚠️ ماكو منتجات مفعلة بالكاتالوج", callback_data="exp_reason_noop"
        )])
    rows.append([InlineKeyboardButton("✏️ إدخال حر", callback_data="exp_reason_manual")])
    rows.append([InlineKeyboardButton(BTN_BACK, callback_data="exp_back_to_vault")])
    return InlineKeyboardMarkup(rows)


def build_expense_reason_list_keyboard() -> InlineKeyboardMarkup:
    """توافق مع رسائل قديمة كانت تفتح شاشة «بقية المنتجات»."""
    return build_expense_reason_keyboard()


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
    require_legacy_write()
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

