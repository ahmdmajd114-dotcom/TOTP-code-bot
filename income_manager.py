"""Receipts, purchases, debt allocation and customer deposits in one owner flow."""
from __future__ import annotations
from runtime_scheduler import run_io, background_job

from copy import deepcopy
from datetime import datetime

from financial_ledger import FinanceError, ledger, money, now_iso, parse_wallets, transaction_date
from financial_ledger import BAGHDAD
from finance_ui import services, draft, navigate, display, customer_text, commit_card, suspend_finance_inputs, selected_wallet


def allocation(state):
    cash = sum(p['amount'] for p in state.get('payments', []))
    settlements = sum(p['amount'] for p in state.get('settlements', []))
    sale = int(state.get('sales', 0))
    return cash + state.get('credit_used', 0) - sale - settlements


def income_payload(state):
    difference = allocation(state)
    decision = state.get('difference')
    if state.get('kind')!='credit_deposit' and difference and decision not in ({'credit', 'price'} if difference > 0 else {'debt', 'discount', 'fee'}):
        raise FinanceError('حدد مصير الفرق قبل التثبيت.')
    payload = {key: deepcopy(state[key]) for key in ('customer_id', 'customer_name', 'customer_username', 'product', 'product_id', 'plan_id', 'plan_name', 'plan_duration', 'business_connection_id', 'proof_id', 'source', 'reference_id') if key in state}
    kind = state.get('kind', 'sale')
    if kind in {'debt_repayment','credit_deposit'}:
        prefix='PAY-DEBT-' if kind=='debt_repayment' else 'PAY-CREDIT-'
        if not state['operation_id'].startswith(prefix):
            state['operation_id']=prefix+state['operation_id'].removeprefix('PAY-')
    sale = int(state.get('sales', 0))
    payload.update(id=state['operation_id'], kind=kind, occurred_at=state['occurred_at'], payments=deepcopy(state.get('payments', [])),
                   settlements=deepcopy(state.get('settlements', [])), sales=sale, quoted_sales=sale, credit_used=state.get('credit_used', 0))
    if kind == 'credit_deposit':
        payload['credit_added'] = sum(p['amount'] for p in payload['payments'])
        return payload
    if difference > 0:
        if decision == 'credit':
            payload['credit_added'] = difference
        else:
            if kind != 'sale':
                raise FinanceError('الزيادة بتسديد الدين تُحفظ رصيداً أو توزّع على شراء.')
            payload['sales'] += difference
    elif difference < 0:
        if decision == 'debt':
            payload['new_debt'] = -difference
        elif decision == 'fee':
            payload['fee'] = -difference
        else:
            payload['sales'] += difference
            payload['discount'] = -difference
    if state.get('subscription') and payload.get('sales', 0) > 0:
        payload['subscription'] = deepcopy(state['subscription'])
    return payload


async def start_income(context, *, customer_id=None, customer_name=None, photo_message=None, source=None, quick=None):
    (await run_io(lambda: ledger().require_active()))
    (await run_io(lambda: suspend_finance_inputs()))
    state = draft('income', customer_id=customer_id, customer_name=customer_name, source=source or 'telegram', sales=0)
    if photo_message is not None:
        state.update(photo=True, message_id=photo_message.message_id)
    if quick:
        state.update(quick)
    state['step'] = 'purpose' if customer_id is not None and (await run_io(lambda: ledger().debts(customer_id, open_only=True))) else 'product' if customer_id is not None else 'customer'
    if quick and quick.get('product'):
        state['step'] = 'summary'
    await render_income(state, context)
    return state


def _products():
    return [p for p in services().products() if p.get('is_active')]


def _choose_product(state, product):
    state.update(product=product['name'], product_id=str(product['id']), difference=None, sales=0,plan_id=None,plan_name=None,subscription=None)
    plans = [p for p in services().plans(str(product['id'])) if p.get('is_active')]
    if len(plans) == 1:
        _choose_plan(state, plans[0])
    else:
        navigate(state, 'plan' if plans else 'price')


def _choose_plan(state, plan):
    state.update(plan_id=str(plan['id']), plan_name=plan['name'], plan_duration=plan.get('duration'), sales=money(plan['price']), difference=None)
    state['subscription'] = {'plan_id': str(plan['id']), 'plan_name': plan['name'], 'plan_duration': plan.get('duration')}
    navigate(state, 'wallet' if not state.get('payments') else 'summary')


async def render_income(state, context, query=None):
    step = state['step']
    heading = '💵 تسجيل قبض\n' + customer_text(state)
    rows = []
    state['awaiting'] = False
    if step == 'customer':
        text = heading + '\nأرسل chat_id للزبون، أو اختَر قبضاً عاماً.'
        state['awaiting'] = True
        rows = [[('إيراد آخر بدون زبون', 'general')]]
    elif step == 'purpose':
        text = heading + '\nهذا الزبون عليه دين؛ هذه الدفعة مقابل شنو؟'
        rows = [[('تسديد دين', 'repay')], [('شراء جديد', 'purchase')], [('دين وشراء بنفس الدفعة', 'mixed')], [('رصيد مقدم للزبون', 'deposit')]]
    elif step == 'debt':
        debts = (await run_io(lambda: ledger().debts(state['customer_id'], open_only=True)))
        state['debt_options'] = [d['id'] for d in debts]
        if not debts:
            raise FinanceError('ماكو دين مفتوح؛ حدّث البطاقة.')
        text = heading + '\nاختَر الدين المقصود:'
        rows = [[(f"{d['product']} — {d.get('plan_name') or ''} — {int(d['remaining']):,}", f'debt{i}')] for i, d in enumerate(debts)]
    elif step in {'product', 'plan'}:
        if step == 'product':
            options = (await run_io(lambda: _products()))
            state['product_options'] = [str(p['id']) for p in options]
            rows = [[(p['name'], f'product{i}')] for i, p in enumerate(options)]
            rows += [[('✏️ منتج أو إيراد حر', 'free')]]
            if state.get('customer_id') is not None:
                rows += [[('رصيد مقدم للزبون', 'deposit')]]
            text = heading + '\nاختَر المنتج:'
        else:
            options = (await run_io(lambda: [p for p in services().plans(state['product_id']) if p.get('is_active')]))
            state['plan_options'] = [str(p['id']) for p in options]
            text = heading + f"\n{state['product']}\nاختَر الباقة:"
            rows = [[(f"{p['name']} — {money(p['price']):,}", f'plan{i}')] for i, p in enumerate(options)]
    elif step == 'wallet':
        state['wallet_options'] = (await run_io(lambda: list(services().wallets())))
        text = heading + '\nاختَر الخزنة:'
        rows = [[(name, f'wallet{i}')] for i, name in enumerate(state['wallet_options'])]
        if state.get('customer_id') is not None and state['kind']!='credit_deposit' and state.get('sales',0)+sum(s['amount'] for s in state.get('settlements',[]))>0:
            rows += [[('الدفع من رصيد الزبون فقط','creditonly')]]
    elif step=='debt_amount_select':
        text=heading+'\nاختَر التسديد الذي تريد تعديله:'
        rows=[[(f"{s['debt_id']} — {s['amount']:,}",f'editdebt{i}')] for i,s in enumerate(state['settlements'])]
    elif step in {'amount', 'price', 'free', 'split', 'date', 'credit', 'debt_amount'}:
        credit_prompt=''
        if step=='credit':
            available=await run_io(lambda: ledger().accounts().get('credit:'+str(state.get('customer_id')),0))
            credit_prompt=f"رصيد الزبون المتاح: {available:,}\nاكتب المبلغ الذي تريد استخدامه (0 للإلغاء):"
        text = heading + '\n' + {'amount': 'اكتب المبلغ المستلم فعلياً:', 'price': 'اكتب سعر البيع المتفق عليه:',
            'free': 'اكتب اسم المنتج أو سبب الإيراد:', 'split': 'اكتب المحافظ والمبالغ: ماستر 5000 + زين كاش 3000',
            'date': 'اكتب تاريخ الاستلام بتوقيت بغداد: 2026-10-28 14:30',
            'credit': credit_prompt,
            'debt_amount': 'اكتب المبلغ المخصص لتسديد هذا الدين:'}[step]
        state['awaiting'] = True
    else:
        state['step'] = 'summary'
        payments = '\n'.join(f"{p['wallet']}: {p['amount']:,}" for p in state.get('payments', [])) or 'لم تحدد'
        text = (heading + f"\nالمنتج: {state.get('product') or '—'}\nالباقة: {state.get('plan_name') or '—'}\n"
                f"سعر البيع: {state.get('sales',0):,}\nالمقبوض:\n{payments}\n"
                f"تسديد ديون: {sum(s['amount'] for s in state.get('settlements',[])):,}\n"
                f"استخدام رصيد سابق: {state.get('credit_used',0):,}\n"
                f"تاريخ الاستلام: {datetime.fromisoformat(state['occurred_at']).astimezone(BAGHDAD).strftime('%Y-%m-%d %H:%M')}")
        difference = allocation(state)
        if state['kind'] != 'credit_deposit' and difference:
            text += f"\nالفرق: {difference:+,}"
            if not state.get('difference'):
                if difference > 0:
                    rows += [[('حفظ الزيادة رصيداً', 'diffcredit')]]
                    if state['kind'] == 'sale': rows += [[('اعتماد كامل المبلغ سعر بيع', 'diffprice')]]
                elif state['kind'] == 'sale':
                    rows += [[('المتبقي دين', 'diffdebt')], [('خصم للزبون', 'diffdiscount')], [('رسوم تحويل تحملها المتجر', 'difffee')]]
            else:
                text += '\nمعالجة الفرق: ' + {'credit':'رصيد للزبون','price':'تعديل سعر البيع','debt':'دين متبقٍ','discount':'خصم','fee':'رسوم تحويل'}[state['difference']]
        rows += [[('✅ تثبيت العملية', 'commit')], [('✏️ تعديل المبلغ', 'editamount'), ('تعديل المنتج', 'editproduct')],
                 [('توزيع على محافظ', 'split'), ('تاريخ الاستلام', 'date')]]
        if state['kind']=='sale':rows += [[('تعديل سعر البيع المتفق عليه','editprice')]]
        if state.get('customer_id') is not None:
            rows += [[('استخدام رصيد الزبون', 'credit'), ('إضافة تسديد دين', 'adddebt')]]
        if state.get('settlements'):
            rows += [[('تعديل مبلغ التسديد', 'debtamount'), ('إزالة التسديد', 'cleardebt')]]
        if state.get('difference'): rows += [[('تغيير معالجة الفرق', 'resetdifference')]]
    rows += [[('◀️ رجوع', 'back'), ('إلغاء', 'cancel')]]
    await display(context, state, text[:4000], rows, query)


async def handle_income_action(state, action, context, query):
    if action == 'commit':
        await commit_card(state, income_payload(state), context, query)
    elif action == 'general':
        state.update(customer_id=None, customer_name='عام', kind='sale'); navigate(state, 'free')
    elif action in {'purchase', 'repay', 'mixed', 'deposit'}:
        state.update(kind='debt_repayment' if action=='repay' else 'credit_deposit' if action=='deposit' else 'sale',
                     sales=0, settlements=[], difference=None, mixed=action=='mixed')
        if action == 'deposit':
            state['product'] = 'رصيد مقدم'; navigate(state, 'wallet')
        else:
            navigate(state, 'debt' if action in {'repay','mixed'} else 'product')
    elif action.startswith('debt') and action[4:].isdigit():
        debt_id = state['debt_options'][int(action[4:])]
        debt = (await run_io(lambda: next((d for d in ledger().debts(state['customer_id'], open_only=True) if d['id']==debt_id), None)))
        if not debt: raise FinanceError('هذا الدين تغير؛ حدّث القائمة.')
        item={'debt_id': debt_id,'amount': int(debt['remaining']),'expected_remaining':int(debt['remaining'])}
        if state.pop('add_settlement',False):
            previous=[s for s in state.get('settlements',[]) if s['debt_id']!=debt_id]
            state.update(settlements=previous+[item],difference=None);navigate(state,'summary');return
        state.update(settlements=[item], difference=None)
        if state.get('mixed'):
            navigate(state, 'product')
        else:
            state.update(product=debt['product'], plan_name=debt.get('plan_name'), sales=0, kind='debt_repayment')
            navigate(state, 'wallet')
    elif action.startswith('product') and action[7:].isdigit():
        product = (await run_io(lambda: next((p for p in _products() if str(p['id']) == state['product_options'][int(action[7:])]), None)))
        if not product: raise FinanceError('المنتج لم يعد متاحاً.')
        (await run_io(lambda: _choose_product(state, product)))
    elif action.startswith('plan') and action[4:].isdigit():
        plan_id = state['plan_options'][int(action[4:])]
        plan = (await run_io(lambda: next((p for p in services().plans(state['product_id']) if str(p['id'])==plan_id and p.get('is_active')),None)))
        if not plan: raise FinanceError('الباقة تغيرت؛ أعد اختيارها.')
        (await run_io(lambda: _choose_plan(state,plan)))
    elif action.startswith('wallet') and action[6:].isdigit():
        wallet = (await run_io(lambda: selected_wallet(state,action)))
        expected = state.get('sales',0) + sum(p['amount'] for p in state.get('settlements',[])) - state.get('credit_used',0)
        state['payments'] = [{'wallet':wallet,'amount': max(0,expected)}]
        navigate(state,'summary' if expected>0 else 'amount')
    elif action=='creditonly':
        needed=state.get('sales',0)+sum(s['amount'] for s in state.get('settlements',[]))
        available=(await run_io(lambda: ledger().accounts())).get('credit:'+str(state['customer_id']),0)
        if needed<=0 or needed>available:raise FinanceError('الرصيد لا يغطي العملية كاملة؛ استخدم جزءاً منه من الملخص.')
        state.update(payments=[],credit_used=needed,difference=None);navigate(state,'summary')
    elif action.startswith('diff'):
        state['difference'] = action[4:]
    elif action == 'resetdifference': state.pop('difference',None)
    elif action == 'editamount': navigate(state,'amount')
    elif action == 'editprice': navigate(state,'price')
    elif action == 'editproduct':
        state['kind']='sale'; navigate(state,'product')
    elif action == 'adddebt':
        state['add_settlement']=True; navigate(state,'debt')
    elif action == 'cleardebt': state.update(settlements=[],difference=None)
    elif action == 'debtamount':
        state['edit_debt_index']=0
        navigate(state,'debt_amount_select' if len(state['settlements'])>1 else 'debt_amount')
    elif action.startswith('editdebt'):
        state['edit_debt_index']=int(action[8:]);navigate(state,'debt_amount')
    elif action in {'free','split','date','credit'}: navigate(state,action)


def income_input(state, text):
    step = state['step']
    if step == 'customer':
        cid = money(text,signed=True)
        name, username = services().identity(cid)
        state.update(customer_id=cid, customer_name=name, customer_username=username)
        if ledger().debts(cid,open_only=True):navigate(state,'purpose')
        elif state.pop('needs_customer',False):navigate(state,'summary' if state.get('subscription') else 'plan')
        else:navigate(state,'product')
    elif step == 'free':
        if not text.strip(): raise FinanceError('اكتب اسم المنتج أو سبب الإيراد.')
        state.update(product=text.strip(),product_id=None,plan_id=None,plan_name=None,subscription=None,kind='sale')
        navigate(state,'price')
    elif step == 'price':
        price=money(text)
        if price<=0:raise FinanceError('سعر البيع لازم أكبر من صفر.')
        state.update(sales=price,difference=None); navigate(state,'summary' if state.get('payments') else 'wallet')
    elif step == 'amount':
        amount=money(text)
        if amount==0 and state.get('credit_used',0)>0:
            state['payments']=[];state.pop('difference',None);navigate(state,'summary');return
        if amount<=0 or not state.get('payments'): raise FinanceError('حدد المحفظة وأدخل مبلغاً أكبر من صفر.')
        if len(state['payments'])!=1: raise FinanceError('استخدم توزيع المحافظ لتعديل دفعة متعددة المحافظ.')
        state['payments'][0]['amount']=amount
        if state['kind']=='debt_repayment' and len(state.get('settlements',[]))==1:
            item=state['settlements'][0]
            debt=next(d for d in ledger().debts(state['customer_id']) if d['id']==item['debt_id'])
            item['amount']=min(amount,int(debt['remaining']))
        state.pop('difference',None); navigate(state,'summary')
    elif step == 'split':
        state['payments']=parse_wallets(text,services().wallets()); state.pop('difference',None); navigate(state,'summary')
    elif step == 'date': state['occurred_at']=transaction_date(text); navigate(state,'summary')
    elif step == 'credit':
        amount=money(text)
        if amount>ledger().accounts().get('credit:'+str(state['customer_id']),0): raise FinanceError('المبلغ أكثر من رصيد الزبون المتاح.')
        state.update(credit_used=amount,difference=None); navigate(state,'summary')
    elif step == 'debt_amount':
        amount=money(text)
        current=state['settlements'][state.get('edit_debt_index',0)]
        debt=next(d for d in ledger().debts(state['customer_id']) if d['id']==current['debt_id'])
        if amount<=0 or amount>int(debt['remaining']): raise FinanceError('التسديد لازم أكبر من صفر ولا يتجاوز المتبقي.')
        current['amount']=amount; state.pop('difference',None); navigate(state,'summary')


# Existing Sheets path retained until explicit ledger activation.
LEGACY_DEPENDENCIES = ('CHATGPT_ROW_MATCH_WINDOW_DAYS', 'GOOGLE_SHEET_WORKSHEET_NAME', 'SHEET_COL_CHATGPT_ACCOUNT', 'SHEET_COL_CHAT_ID', 'SHEET_COL_DATE', 'SHEET_COL_OPERATION_ID', 'SHEET_COL_PAYMENTS', 'SHEET_COL_PRODUCT', 'SHEET_COL_TOTAL', 'datetime', 'format_customer_line', 'get_google_sheet', 'is_chatgpt_product_name', 'is_debt_repayment_row', 'logger', 'parse_sheet_amount', 're', 'require_legacy_write', 'timedelta', 'timezone', 'uuid')

def configure_legacy(values):
    """Inject store adapters; feature modules never import bot.py."""
    globals().update({name: values[name] for name in LEGACY_DEPENDENCIES})

def find_completable_chatgpt_row(
    sheet, chat_id: int, *, missing: str = "any",
) -> int | None:
    """
    يدور عن آخر سطر ChatGPT بالشيت لنفس chat_id، خلال آخر
    أسبوع، وفيه الخانة المطلوبة فاضية. ``missing=payment`` لا يطابق
    سطراً مدفوعاً لمجرد أن الحساب فارغ، حتى لا تكتب دفعة جديدة فوق
    دفعة قديمة. يرجع رقم
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
        if is_debt_repayment_row(row):
            continue

        row_chat_id = row[SHEET_COL_CHAT_ID - 1].strip()
        row_product = row[SHEET_COL_PRODUCT - 1].strip()

        if row_chat_id != chat_id_str or not is_chatgpt_product_name(row_product):
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

        if missing == "payment":
            is_completable = total_empty or payments_empty
        elif missing == "account":
            is_completable = account_empty
        else:
            is_completable = total_empty or payments_empty or account_empty

        if is_completable:
            return i + 1  # gspread صفوف 1-indexed

    return None


def append_payment_row(state: dict, force_new: bool = False) -> bool:
    """
    يضيف سطر جديد بـ Google Sheet لعملية دفع مكتملة، أو يكمل سطر ناقص
    موجود لنفس الزبون لو المنتج جات (خلال آخر أسبوع). يرجع True لو نجح
    الحفظ، False لو فشل. force_new مخصص لتسديد الدين حتى ينحسب كدخل
    جديد بتاريخ التسديد، ولا يكتب فوق سطر الدين/العملية الأصلية.
    """
    require_legacy_write()
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
    operation_id = state.setdefault("payment_operation_id", f"PAY-{uuid.uuid4().hex.upper()}")

    try:
        # مفتاح العملية يجعل إعادة المحاولة آمنة: إذا نجح الإرسال للشيت
        # وانقطع الرد قبل التحقق، لن نضيف الدخل مرة ثانية.
        existing_rows = sheet.get_all_values()
        for existing in existing_rows[1:]:
            if (len(existing) >= SHEET_COL_OPERATION_ID
                    and existing[SHEET_COL_OPERATION_ID - 1].strip() == operation_id):
                return True
        target_row = None
        previously_delivered_account = ""
        if not force_new and is_chatgpt_product_name(product) and chat_id is not None:
            target_row = find_completable_chatgpt_row(sheet, chat_id, missing="payment")
        if target_row is not None and sheet.payment_column_offset(target_row):
            # الدفعة الجديدة تُكتب في A:H حتى لو سُلّم حسابها سابقاً
            # بسطر ذي أعمدة مزاحة. نحفظ الحساب معها ولا نغيّر السجل القديم.
            previously_delivered_account = sheet.row_values(target_row)[SHEET_COL_CHATGPT_ACCOUNT - 1]
            target_row = None

        if target_row is not None:
            # نسجل وقت استلام الدفع الفعلي، مو وقت ربط الحساب السابق.
            sheet.update_cell(target_row, SHEET_COL_DATE, date_time_str)
            sheet.update_cell(target_row, SHEET_COL_TOTAL, total)
            sheet.update_cell(target_row, SHEET_COL_PAYMENTS, payments_text)
            sheet.update_cell(target_row, SHEET_COL_OPERATION_ID, operation_id)
        else:
            row = [
                date_time_str,
                total,
                payments_text,
                product,
                customer_line,
                previously_delivered_account,
                str(chat_id) if chat_id is not None else "",
                operation_id,
            ]
            response = sheet.append_row(row, value_input_option="USER_ENTERED")
            response_data = response if isinstance(response, dict) else {}
            updated_range = str(response_data.get("updates", {}).get("updatedRange", ""))
            match = re.search(r"![^!]*?\$?[A-Z]+\$?(\d+):", updated_range)
            if match:
                target_row = int(match.group(1))

        # لا نعلن النجاح قبل أن نقرأ السطر ونتأكد أن Google Sheets حفظه.
        if target_row is None:
            all_rows = sheet.get_all_values()
            for index in range(len(all_rows) - 1, 0, -1):
                candidate = all_rows[index]
                if len(candidate) < SHEET_COL_PAYMENTS:
                    continue
                candidate_total = parse_sheet_amount(candidate[SHEET_COL_TOTAL - 1])
                candidate_payments = candidate[SHEET_COL_PAYMENTS - 1].strip()
                candidate_chat_id = (
                    candidate[SHEET_COL_CHAT_ID - 1].strip()
                    if len(candidate) >= SHEET_COL_CHAT_ID else ""
                )
                candidate_operation_id = (
                    candidate[SHEET_COL_OPERATION_ID - 1].strip()
                    if len(candidate) >= SHEET_COL_OPERATION_ID else ""
                )
                if (candidate_total == total and candidate_payments == payments_text
                        and (chat_id is None or candidate_chat_id == str(chat_id))
                        and candidate_operation_id == operation_id):
                    target_row = index + 1
                    break
        if target_row is None:
            logger.error("Payment was appended but its row could not be resolved for verification")
            return False
        saved_row = sheet.row_values(target_row)
        saved_total = parse_sheet_amount(
            saved_row[SHEET_COL_TOTAL - 1] if len(saved_row) >= SHEET_COL_TOTAL else None
        )
        saved_payments = (
            saved_row[SHEET_COL_PAYMENTS - 1].strip()
            if len(saved_row) >= SHEET_COL_PAYMENTS else ""
        )
        saved_chat_id = (
            saved_row[SHEET_COL_CHAT_ID - 1].strip()
            if len(saved_row) >= SHEET_COL_CHAT_ID else ""
        )
        saved_operation_id = (
            saved_row[SHEET_COL_OPERATION_ID - 1].strip()
            if len(saved_row) >= SHEET_COL_OPERATION_ID else ""
        )
        if saved_total != total or saved_payments != payments_text:
            logger.error(
                "Payment sheet verification failed row=%s expected_total=%s actual_total=%s",
                target_row, total, saved_total,
            )
            return False
        if chat_id is not None and saved_chat_id != str(chat_id):
            logger.error(
                "Payment sheet chat-id verification failed row=%s expected=%s actual=%s",
                target_row, chat_id, saved_chat_id,
            )
            return False
        if saved_operation_id != operation_id:
            logger.error(
                "Payment sheet operation-id verification failed row=%s expected=%s actual=%s",
                target_row, operation_id, saved_operation_id,
            )
            return False
        logger.info(
            "Payment saved and verified worksheet=%s row=%s chat_id=%s total=%s",
            getattr(sheet, "title", GOOGLE_SHEET_WORKSHEET_NAME),
            target_row, chat_id, total,
        )
        return True
    except Exception:
        logger.exception("Failed to append/update payment row in Google Sheet")
        return False

