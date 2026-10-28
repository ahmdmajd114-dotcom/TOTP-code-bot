"""Idempotent post-commit subscription projection into existing delivery flow."""
from runtime_scheduler import run_io, background_job
from financial_ledger import FinanceError,ledger
from datetime import datetime,timedelta,timezone

_config=None


def configure_subscription_projection(*,client,save,schedule,duration,permanent,is_chatgpt):
    global _config
    _config=dict(client=client,save=save,schedule=schedule,duration=duration,permanent=permanent,is_chatgpt=is_chatgpt)


def financial_delivery_payment(chat_id):
    rows=ledger().operations(chat_id)
    cancelled={r['reference_id'] for r in rows if r['kind'] in {'refund','reversal'}}
    for record in sorted(rows,key=lambda r:r['recorded_at'],reverse=True):
        p=record['payload']
        if record['kind']!='sale' or record['historical'] or record['id'] in cancelled or not _config['is_chatgpt'](record.get('product')):continue
        from account_delivery import funding_managed
        if funding_managed(record['id']):continue
        if datetime.fromisoformat(record['occurred_at'].replace('Z','+00:00'))<datetime.now(timezone.utc)-timedelta(days=7):continue
        reminders=_config['client'].table('subscription_reminders').select('status,feedback_status').eq('financial_operation_id',record['id']).limit(1).execute().data or []
        if reminders and reminders[0]['status']!='pending_delivery' and reminders[0].get('feedback_status')!='pending_delivery':continue
        return 0,[record['occurred_at'],str(record['sales']), 'مدفوع من السجل المالي',record['product'],p.get('customer_name') or 'زبون','',str(chat_id),record['id']]
    return None


def ensure_pending_financial_delivery(chat_id):
    payment=financial_delivery_payment(chat_id)
    if payment is None:return
    oid=payment[1][7]
    existing=_config['client'].table('subscription_reminders').select('id').eq('financial_operation_id',oid).limit(1).execute().data or []
    if existing:return
    record=next(r for r in ledger().operations(chat_id) if r['id']==oid)
    p=record['payload'];sub=p.get('subscription')
    if not sub:return
    state={'customer_chat_id':chat_id,'customer_name':p.get('customer_name') or 'زبون','customer_username':p.get('customer_username'),
        'business_connection_id':p.get('business_connection_id'),'product':p['product'],'financial_operation_id':oid,**sub,
        'duration_days':_config['duration'](sub.get('plan_duration')),'reminder_disabled':_config['permanent'](sub.get('plan_duration'))}
    if not _config['save'](state,status='pending_delivery'):
        raise FinanceError('الدفعة محفوظة؛ تعذر تجهيز مدة الاشتراك. أعد الربط، لا تسجل دفعة ثانية.')


async def save_financial_subscription(payload):
    if not payload.get('customer_id') or not payload.get('subscription'):return
    from account_delivery import funding_managed
    if funding_managed(payload['id']):return
    client=_config['client'];oid=payload['id'];sub=payload['subscription']
    rows=(await run_io(lambda: client.table('subscription_reminders').select('*').eq('financial_operation_id',oid).limit(1).execute())).data or []
    state={'customer_chat_id':payload['customer_id'],'customer_name':payload.get('customer_name') or 'زبون',
        'customer_username':payload.get('customer_username'),'business_connection_id':payload.get('business_connection_id'),
        'product':payload['product'],'financial_operation_id':oid,**sub,
        'duration_days':_config['duration'](sub.get('plan_duration')),'reminder_disabled':_config['permanent'](sub.get('plan_duration'))}
    pending=_config['is_chatgpt'](payload['product'])
    if not rows:
        if not (await run_io(lambda: _config['save'](state,status='pending_delivery' if pending else 'active'))):
            raise FinanceError('متابعة الاشتراك بانتظار المزامنة.')
        rows=(await run_io(lambda: client.table('subscription_reminders').select('*').eq('financial_operation_id',oid).limit(1).execute())).data or []
    if not rows:raise FinanceError('لم تتأكد متابعة الاشتراك.')
    row=rows[0]
    if not pending and row.get('scheduled_message_status')!='scheduled':
        state['reminder_id']=row['id']
        if not await _config['schedule'](state):raise FinanceError('تعذر حجز المتابعة؛ يعاد تلقائياً.')


async def notify_financial_operation(context,payload):
    from finance_ui import services
    from financial_ledger import KINDS
    await context.bot.send_message(chat_id=services().owner_id,text=f"🧾 {KINDS[payload['kind']]}\nرقم العملية: {payload['id']}\nالزبون: {payload.get('customer_name') or 'عام'}\nالمنتج: {payload.get('product') or '—'}")
