"""Explicit customer credit corrections; never invent a receipt or a sale."""
from runtime_scheduler import run_io, background_job
from financial_ledger import ledger,FinanceError
from finance_ui import draft,display,services


async def start_credit_adjustment(context,customer_id,amount=None,reason=None):
    (await run_io(lambda: ledger().require_active()))
    name,_=(await run_io(lambda: services().identity(customer_id)))
    current=(await run_io(lambda: ledger().accounts())).get('credit:'+str(customer_id),0)
    state=draft('vault',kind='credit_adjustment',customer_id=customer_id,customer_name=name,
                expected_balance=current,step='amount')
    if amount is not None:
        if current+amount<0:raise FinanceError('التسوية تجعل رصيد الزبون سالباً.')
        state.update(adjustment=amount,amount=amount,description=reason or 'تصحيح رصيد سابق',step='summary')
    from vault_manager import render_vault
    await render_vault(state,context)
