"""Project committed Instagram sales/commissions without adjusting cash twice."""
from financial_ledger import FinanceError

_config=None


def configure_instagram_projection(*,sheet,append,reminder):
    global _config
    _config=dict(sheet=sheet,append=append,reminder=reminder)


def project_financial_instagram(payload):
    sheet=_config['sheet']()
    if sheet is None:raise FinanceError('شيت إنستغرام غير متاح.')
    sale=payload.get('instagram_sale')
    if sale:
        if not any(r and r[0]==sale['sale_id'] for r in sheet.get_all_values()[1:]):
            if not _config['append'](sale):raise FinanceError('لم تتأكد مزامنة بيع إنستغرام.')
        if not _config['reminder']({**sale,'financial_operation_id':payload['id']},payload.get('instagram_state',{})):
            raise FinanceError('تنبيه بيع إنستغرام بانتظار المزامنة.')
    commission=payload.get('instagram_commission')
    if commission:
        found=next((i for i,r in enumerate(sheet.get_all_values()[1:],2) if r and r[0]==commission),None)
        if found is None:raise FinanceError('بيع إنستغرام لم يصل للشيت بعد.')
        sheet.update_cell(found,11,'نعم')
