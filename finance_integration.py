"""Small adapters between existing store flows and the financial feature modules."""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime,timezone
from typing import Callable
import logging,re,hashlib

from financial_ledger import (FinanceError,configure_ledger,ledger,build_legacy_snapshot,
    source_id,now_iso,money,operation_id,parse_wallets)
from finance_ui import (FinanceServices,configure_finance_ui,show_finance,services,
    keyboard,handle_finance_input,finance_worker)
from finance_projection import ProjectionServices,configure_projection,project_operation

logger=logging.getLogger(__name__)
_config=None


@dataclass
class StoreFinance:
    client: object
    owner_id: int
    products: Callable
    plans: Callable
    wallets: Callable
    identity: Callable
    button: Callable
    markup: Callable
    spreadsheet: Callable
    payment_sheet: Callable
    expense_sheet: Callable
    instagram_sheet: Callable
    credit_sheet: Callable
    wallet_sheet: Callable
    wallet_balances: Callable
    debt_sheet: Callable
    legacy_debts: Callable
    subscription: Callable
    notify: Callable
    instagram: Callable


def configure_store_finance(config):
    global _config
    _config=config
    configure_ledger(config.client,config.owner_id)
    configure_projection(ProjectionServices(config.spreadsheet,config.payment_sheet,config.expense_sheet,
        config.wallet_sheet,financial_wallet_names,config.debt_sheet,config.subscription,config.instagram))
    configure_finance_ui(FinanceServices(config.owner_id,config.products,config.plans,financial_wallet_names,
        config.identity,config.button,config.markup,snapshot,project_operation,config.subscription,config.notify))


def configured():return _config is not None


def financial_wallet_names():
    """Owner treasuries are accounting accounts, not customer payment labels."""
    try:
        names=list(_config.wallets())
        if not names or any(not isinstance(name,str) or not name.strip() for name in names):
            raise FinanceError('قائمة الخزائن المالية غير مكتملة؛ راجع إعداد الخزائن.')
        return list(dict.fromkeys(names))
    except FinanceError:raise
    except Exception as exc:raise FinanceError('تعذر قراءة الخزائن المعتمدة؛ لم أختر خزنة افتراضية.') from exc


def active():
    return configured() and ledger().active()


def require_legacy_write():
    if active():raise FinanceError('استخدم البطاقة المالية الجديدة؛ تحديث الشيت مباشرة لا يغير السجل المعتمد.')


def record_instagram_finance(sale,state):
    if not active():return None
    oid=source_id('PAY-IG',sale['sale_id'])
    payload={'id':oid,'kind':'sale','occurred_at':state.setdefault('finance_occurred_at',now_iso()),'source':'instagram',
       'customer_name':'@'+sale['instagram_account'],'product':sale['product'],'sales':sale['amount'],
       'payments':[{'wallet':sale['wallet'],'amount':sale['amount']}],
       'instagram_sale':sale,'instagram_state':{'duration_months':state.get('duration_months')}}
    ledger().post(payload)
    return True


def record_instagram_commission(sale_id,wallet,commission):
    if not active():return False
    oid=source_id('EXP-IG',sale_id)
    existing=next((r for r in ledger().operations() if r['id']==oid),None)
    if existing:return True
    ledger().post({'id':oid,'kind':'expense','occurred_at':now_iso(),'product':'عمولات إنستغرام',
      'description':'عمولة عملية '+sale_id,'instagram_commission':sale_id,'payments':[{'wallet':wallet,'amount':commission}]})
    return True


def snapshot():
    def read(getter):
        sheet=getter()
        if sheet is None:raise FinanceError('تعذر قراءة أحد الشيتات؛ لا يمكن اعتماد أرقام ناقصة.')
        return sheet.get_all_values()
    wallet_sheet=_config.wallet_sheet()
    if wallet_sheet is None:raise FinanceError('تعذر قراءة أرصدة الخزن.')
    values=wallet_sheet.row_values(2)
    names=wallet_sheet.row_values(1)
    if not names or any(not str(name).strip() for name in names):raise FinanceError('عناوين ورقة الخزن ناقصة؛ راجعها قبل التهيئة.')
    balances={name:money(values[i],signed=True) if i<len(values) and str(values[i]).strip() else 0 for i,name in enumerate(names)}
    return build_legacy_snapshot(read(_config.payment_sheet),read(_config.expense_sheet),read(_config.instagram_sheet),
        _config.legacy_debts(),read(_config.credit_sheet),balances)


async def intercept_payment(update,context,state):
    """Route receipt/AC to the new persistent card once cutover is active."""
    if not configured():return False
    query=update.callback_query
    try:
        if not active():return False
        from debt_manager import cancel_debt_input
        cancel_debt_input(context)
        if query.data=='pay_cancel':
            await query.answer();await query.edit_message_caption(caption='أُلغيت البطاقة قبل التثبيت.',reply_markup=None);return True
        from income_manager import start_income,render_income
        # A legacy card still open at cutover is carried into a fresh draft,
        # never committed via the old wallet-increment path.
        await query.answer()
        if state.get('finance_draft_id'):
            s=ledger().draft(state['finance_draft_id'])
            await render_income(s,context,query)
            return True
        s=await start_income(context,customer_id=state.get('customer_chat_id'),customer_name=state.get('customer_name'),photo_message=query.message)
        s.update(business_connection_id=state.get('business_connection_id'),customer_username=state.get('customer_username'),proof_id=state.get('proof_id'))
        if state.get('product'):
            s.update(product=state['product'],sales=state.get('plan_price') or sum(a for _,a in state.get('payments',[])),
                payments=[{'wallet':w,'amount':a} for w,a in state.get('payments',[])],step='summary')
        ledger().save_draft(s)
        state['finance_draft_id']=s['draft_id']
        await render_income(s,context,query)
        return True
    except FinanceError as exc:
        await query.message.reply_text(f'⚠️ {exc}');return True


async def intercept_expense_photo(update,context,pending):
    if not configured():return False
    query=update.callback_query
    try:
        if not active():return False
        from expense_manager import start_expense
        await query.answer()
        if query.data=='expphoto_no':await query.message.delete()
        elif query.data=='expphoto_refund':
            cid=pending.get('customer_chat_id');rows=[r for r in ledger().operations(cid) if r['kind']=='sale' and not r['historical']]
            await query.edit_message_caption(caption='اختَر عملية البيع الأصلية حتى يرتبط الاسترجاع بها:',reply_markup=keyboard([[((r.get('product') or 'بيع')+' — '+str(r['sales']),'finance_refund_'+r['id'])] for r in rows[-15:]]))
        else:
            s=await start_expense(context,customer_id=pending.get('customer_chat_id'),photo_message=query.message)
            pending['finance_draft_id']=s['draft_id']
        return True
    except FinanceError as exc:
        await query.message.reply_text(f'⚠️ {exc}');return True


async def financial_shortcut(context,bm,chat_id,text,customer_name=None):
    command=text.strip()
    if command not in {'صرف','قبض'}:return False
    # Deletion has priority even when the ledger is unavailable/not configured.
    try:
        await context.bot.delete_business_messages(business_connection_id=bm.business_connection_id,message_ids=[bm.message_id])
    except Exception:logger.warning('Cannot delete financial shortcut for %s',chat_id)
    try:
        ledger().require_active()
        from debt_manager import cancel_debt_input
        cancel_debt_input(context)
        if command=='قبض':
            from income_manager import start_income
            await start_income(context,customer_id=chat_id,customer_name=customer_name)
        else:
            from expense_manager import start_expense
            await start_expense(context,customer_id=chat_id,customer_name=customer_name)
    except FinanceError as exc:
        await context.bot.send_message(chat_id=_config.owner_id,text=f'⚠️ {exc}')
    return True


async def finance_owner_input(update,context):
    if not configured():return False
    message=update.message
    if not message or not message.text:return False
    try:
        if any(context.user_data.get(key) for key in ('pending_catalog_input','pending_payment_input','pending_shared_account','campaign_draft','pending_manual_subscription','pending_personal_reminder')):
            return False
        text=message.text.strip()
        if ledger().mode()!='active':
            if not text.startswith(('قبض ','مصروف ')):return False
            ledger().require_active()
        from reports_manager import report_input
        if await report_input(update,context):return True
        if await handle_finance_input(update,context):return True
        if not text.startswith(('قبض ','مصروف ')):return False
        ledger().require_active()
        # Deterministic parsing only. A nonunique product or wallet asks for
        # explicit selection instead of silently guessing a financial amount.
        prefix,body=text.split(' ',1)
        wallets=[w for w in financial_wallet_names() if body.endswith(' '+w)]
        if len(wallets)!=1:raise FinanceError('اكتب اسم المحفظة بنهاية النص، مثل: مصروف سكريبت 15 ألف ماستر.')
        wallet=wallets[0];body=body[:-len(wallet)].strip()
        match=re.fullmatch(r'(.+?)\s+([\d٠-٩۰-۹][\d٠-٩۰-۹,.٬٫]*(?:\s+(?:ألف|الف|آلاف|مليون))?)',body)
        if not match:raise FinanceError('اكتب المنتج ثم المبلغ ثم المحفظة.')
        product,raw=match.groups();amount=money(raw)
        quick={'product':product,'payments':[{'wallet':wallet,'amount':amount}]}
        if prefix=='مصروف':
            from expense_manager import start_expense
            quick.update(amount=amount,description=product);await start_expense(context,quick=quick)
        else:
            from income_manager import start_income,render_income
            from intent_fallback import normalize_arabic_text
            matches=[p for p in _config.products() if p.get('is_active') and normalize_arabic_text(product) in {normalize_arabic_text(str(n)) for n in [p['name'],*(p.get('aliases') or [])]}]
            quick.update(sales=amount)
            if len(matches)==1:
                p=matches[0];plans=[x for x in _config.plans(str(p['id'])) if x.get('is_active')]
                quick.update(product=p['name'],product_id=str(p['id']))
                if len(plans)==1:
                    plan=plans[0];quick.update(sales=money(plan['price']),plan_id=str(plan['id']),plan_name=plan['name'],plan_duration=plan.get('duration'),
                        subscription={'plan_id':str(plan['id']),'plan_name':plan['name'],'plan_duration':plan.get('duration')})
                if plans:quick['needs_customer']=True
            state=await start_income(context,customer_name='عام',quick=quick)
            if quick.get('needs_customer'):
                state['step']='customer'
                await render_income(state,context)
        return True
    except FinanceError as exc:
        await message.reply_text(f'⚠️ {exc}');return True


def active_report(period,*,products=False):
    if not active():return None
    from reports_manager import financial_report
    if products:
        names=sorted({r.get('product') for r in ledger().operations() if r.get('product')})
        return '\n\n'.join(financial_report(period,name) for name in names)[:4000] or 'لا توجد حركات منتجات.'
    return financial_report(period)


def financial_debt_records(chat_id=None,open_only=False):
    result=[]
    # Stable synthetic row indexes for new ledger debts keep old dashboard
    # callbacks usable. Money updates always target debt_id, never this index.
    all_debts=sorted(ledger().debts(),key=lambda d:d['id'])
    used_rows=set()
    for index,d in enumerate(all_debts,1):
        row_number=d.get('legacy_row') or 1000000+int(hashlib.sha256(d['id'].encode()).hexdigest()[:14],16)
        if row_number in used_rows:raise FinanceError('تعارض في معرف بطاقة الدين؛ لم أختر ديناً آخر.')
        used_rows.add(row_number)
        if chat_id is not None and d['customer_id']!=int(chat_id):continue
        remaining=int(d['remaining']);original=int(d['original'])
        if open_only and remaining<=0:continue
        result.append({'row_number':row_number,'debt_id':d['id'],
          'chat_id':d['customer_id'],'customer_line':d['customer_name'],'product':d['product'],
          'product_id':d.get('product_id'),'plan_id':d.get('plan_id'),'plan_name':d.get('plan_name'),
          'created_at':d['created_at'],'remaining':remaining,'original_amount':original,'paid_total':original-remaining,
          'status':'مدفوع' if remaining==0 else 'جزئي' if remaining<original else 'غير مدفوع',
          'last_paid_at':d.get('last_paid_at') or '', 'last_operation_id':d.get('last_operation_id') or ''})
    return result


def post_debt(chat_id,name,product,amount,*,debt_id,product_id=None,plan_id=None,plan_name=None):
    oid=source_id('FIN-DEBT',debt_id)
    existing=next((r for r in ledger().operations(chat_id) if r['id']==oid),None)
    if existing:
        p=existing['payload']
        if p.get('product')!=product or p.get('new_debt')!=amount:raise FinanceError('تغيّر الدين بعد حفظه؛ راجع العملية الأصلية.')
        return True
    ledger().post({'id':oid,'kind':'debt_issue','occurred_at':now_iso(),'customer_id':int(chat_id),'customer_name':name,
        'product':product,'product_id':product_id,'plan_id':plan_id,'plan_name':plan_name,'sales':amount,'new_debt':amount,'payments':[]})
    return True


async def financial_repay(query,context,state):
    from income_manager import start_income,render_income
    s=await start_income(context,customer_id=state['customer_chat_id'],customer_name=state['customer_name'])
    s.update(kind='debt_repayment',sales=0,product=state['product'],step='summary',
        payments=[{'wallet':w,'amount':a} for w,a in state['payments']],
        settlements=[{'debt_id':state['selected_debt_id'],'amount':min(state['selected_debt_remaining'],sum(a for _,a in state['payments'])),'expected_remaining':state['selected_debt_remaining']}])
    await render_income(s,context)
    await query.message.reply_text('أكمل التثبيت من البطاقة المالية الجديدة؛ لم يتغير الدين بعد.')
    return False
