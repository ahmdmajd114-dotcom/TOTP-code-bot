"""Wallet movements and explicit reconciliation; transfers are not revenue."""
from __future__ import annotations

from financial_ledger import FinanceError,ledger,money,transaction_date
from finance_ui import services,draft,navigate,display,keyboard,commit_card,suspend_finance_inputs


async def show_vaults(message):
    ledger().require_active(); accounts=ledger().accounts()
    names=list(dict.fromkeys([*services().wallets(),*(k[7:] for k in accounts if k.startswith('wallet:'))]))
    text='🏦 أرصدة الخزن المحسوبة\n'+'\n'.join(f"{w}: {accounts.get('wallet:'+w,0):,}" for w in names)
    await message.reply_text(text,reply_markup=keyboard([[('تحويل بين الخزن','finance_wallet_transfer')],[('إضافة رأس مال','finance_wallet_capital'),('سحب شخصي','finance_wallet_withdrawal')],[('مطابقة رصيد فعلي','finance_wallet_vault_adjustment')]]))


async def start_vault_action(context,kind):
    ledger().require_active()
    suspend_finance_inputs()
    if kind not in {'transfer','capital','withdrawal','vault_adjustment'}:raise FinanceError('نوع حركة الخزنة غير صالح.')
    state=draft('vault',kind=kind,step='wallet')
    await render_vault(state,context)


async def render_vault(state,context,query=None):
    from financial_ledger import KINDS
    text='🏦 '+KINDS[state['kind']];rows=[];step=state['step'];state['awaiting']=False
    if step in {'wallet','to'}:
        state['wallet_options']=list(services().wallets())
        rows=[[(w,f'wallet{i}')] for i,w in enumerate(state['wallet_options']) if step!='to' or w!=state.get('wallet')]
        text+='\nاختَر المحفظة '+('المستلمة:' if step=='to' else 'المصدر:')
    elif step in {'amount','reason','date'}:
        state['awaiting']=True
        text+='\n'+('اكتب فرق تسوية رصيد الزبون، مثال 7000 أو -2000:' if step=='amount' and state['kind']=='credit_adjustment' else 'اكتب الرصيد الفعلي الكامل:' if step=='amount' and state['kind']=='vault_adjustment' else 'اكتب المبلغ:' if step=='amount' else 'اكتب سبب الحركة أو الفرق:' if step=='reason' else 'اكتب تاريخ الحركة بتوقيت بغداد:')
    else:
        text+=(f"\nالزبون: {state.get('customer_name')}\nالتغيير في الرصيد: {state.get('adjustment',0):+,}" if state['kind']=='credit_adjustment' else f"\nالمحفظة: {state.get('wallet')}\nالمبلغ: {state.get('amount',0):,}")
        if state.get('to_wallet'):text+=f"\nإلى: {state['to_wallet']}"
        if state['kind']=='vault_adjustment':text+=f"\nالفرق الذي سيسجل: {state['adjustment']:+,}"
        text+=f"\nالسبب: {state.get('description')}\nالحركة مستقلة عن المبيعات والمصاريف."
        rows=[[('✅ تثبيت','commit')],[('تعديل المبلغ','amount'),('تاريخ الحركة','date')]]
    rows += [[('◀️ رجوع','back'),('إلغاء','cancel')]]
    await display(context,state,text,rows,query)


async def handle_vault_action(state,action,context,query):
    if action.startswith('wallet'):
        wallet=state['wallet_options'][int(action[6:])]
        if state['step']=='to':state['to_wallet']=wallet;navigate(state,'amount')
        else:
            state['wallet']=wallet;navigate(state,'to' if state['kind']=='transfer' else 'amount')
    elif action in {'amount','date'}:navigate(state,action)
    elif action=='commit':
        payload={k:state[k] for k in ('kind','occurred_at','description','wallet','to_wallet','adjustment','reference_id','customer_id','customer_name') if k in state}
        payload['id']=state['operation_id']
        payload['payments']=[] if state['kind'] in {'vault_adjustment','credit_adjustment','reversal'} else [{'wallet':state['wallet'],'amount':state['amount']}]
        if state['kind']=='vault_adjustment':
            current=ledger().accounts().get('wallet:'+state['wallet'],0)
            if current!=state['expected_balance']:raise FinanceError('رصيد المحفظة تغير أثناء المراجعة؛ أعد المطابقة.')
            payload['expected_balance']=state['expected_balance']
        await commit_card(state,payload,context,query)


def vault_input(state,text):
    if state['step']=='amount':
        amount=money(text,signed=state['kind'] in {'vault_adjustment','credit_adjustment'})
        if amount<=0 and state['kind'] not in {'vault_adjustment','credit_adjustment'}:raise FinanceError('المبلغ لازم أكبر من صفر.')
        state['amount']=amount
        if state['kind']=='credit_adjustment':
            state['adjustment']=amount
            if state['expected_balance']+amount<0:raise FinanceError('رصيد الزبون ما يصير سالباً.')
        if state['kind']=='vault_adjustment':
            balance=ledger().accounts().get('wallet:'+state['wallet'],0)
            state.update(adjustment=amount-balance,expected_balance=balance)
        navigate(state,'reason')
    elif state['step']=='reason':
        if not text.strip():raise FinanceError('اكتب سبب الحركة.')
        state['description']=text.strip();navigate(state,'summary')
    elif state['step']=='date':state['occurred_at']=transaction_date(text);navigate(state,'summary')


# Existing Sheets path retained until explicit ledger activation.
LEGACY_DEPENDENCIES = ('Credentials', 'GOOGLE_SERVICE_ACCOUNT_FILE', 'GOOGLE_SHEET_ID', 'VAULTS_WORKSHEET_NAME', 'VAULT_NAMES', '_vaults_sheet', 'finance_active', 'gspread', 'ledger', 'logger', 'require_legacy_write')

def configure_legacy(values):
    """Inject store adapters; feature modules never import bot.py."""
    globals().update({name: values[name] for name in LEGACY_DEPENDENCIES})

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


def get_vault_balances() -> dict[str, int] | None:
    """يرجع الأرصدة الحالية لكل الخزائن كـ dict، أو None لو فشل الاتصال."""
    if finance_active():
        accounts = ledger().accounts()
        return {name: accounts.get('wallet:'+name, 0) for name in VAULT_NAMES}
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
    require_legacy_write()
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
    require_legacy_write()
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

