"""Idempotent Sheets projections of committed financial operations.

No wallet increments happen here: balances are mirrored from the ledger.
Every appended projection has an operation key and is read back before success.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime,timezone
from threading import RLock
import asyncio
from typing import Callable
from financial_ledger import ledger,FinanceError,KINDS

_services=None
_lock=RLock()
_async_lock=asyncio.Lock()


@dataclass
class ProjectionServices:
    spreadsheet: Callable
    payments: Callable
    expenses: Callable
    wallets: Callable
    wallet_names: Callable
    debts: Callable
    subscription: Callable
    instagram: Callable


def configure_projection(services):
    global _services
    _services=services


def ensure_sheet(title,headers):
    book=_services.spreadsheet()
    sheet=next((w for w in book.worksheets() if w.title==title),None)
    if sheet is None:
        sheet=book.add_worksheet(title=title,rows=1000,cols=len(headers))
        sheet.update(range_name=f'A1:{column(len(headers))}1',values=[headers],value_input_option='RAW')
    elif sheet.col_count<len(headers):sheet.resize(rows=sheet.row_count,cols=len(headers))
    return sheet


def column(number):
    result=''
    while number:
        number,rem=divmod(number-1,26);result=chr(65+rem)+result
    return result


def upsert_row(sheet,row,key_index,*,replace=False):
    with _lock:
        rows=sheet.get_all_values()
        matches=[i for i,r in enumerate(rows[1:],2) if len(r)>key_index and str(r[key_index])==str(row[key_index])]
        if len(matches)>1:raise FinanceError('تكرر رقم عملية في الشيت؛ يلزم مراجعة الصفوف.')
        target=matches[0] if matches else max(2,len(rows)+1)
        if matches and not replace:
            existing=sheet.row_values(target)
            if len(existing)<=key_index or str(existing[key_index])!=str(row[key_index]):raise FinanceError('فشل التحقق من نسخة العملية.')
            return target
        if target>sheet.row_count:sheet.add_rows(100)
        sheet.update(range_name=f'A{target}:{column(len(row))}{target}',values=[row],value_input_option='RAW')
        saved=sheet.row_values(target)
        if len(saved)<=key_index or str(saved[key_index])!=str(row[key_index]):raise FinanceError('لم تتأكد مزامنة الشيت.')
        return target


def mirror_debts(payload):
    sheet=_services.debts()
    affected={s['debt_id'] for s in payload.get('settlements',[])}
    source=payload['id']
    if payload['kind']=='reversal':
        original=ledger().operation(payload['reference_id'])
        affected.update(s['debt_id'] for s in original['payload'].get('settlements',[]))
        source=payload['reference_id']
    debts=ledger().debts(payload.get('customer_id')) if payload.get('customer_id') is not None else []
    if payload['kind']=='reversal':debts=ledger().debts(original.get('customer_id'))
    for d in debts:
        if d['id'] not in affected and d.get('source_operation_id')!=source:continue
        remaining=int(d['remaining']);original=int(d['original'])
        values=[d['created_at'],str(d['customer_id']),d['customer_name'],d['product'],remaining,
                'مدفوع' if remaining==0 else 'جزئي' if remaining<original else 'غير مدفوع',d.get('last_paid_at') or '',
                d['id'],d.get('product_id') or '',d.get('plan_id') or '',d.get('plan_name') or '',original,original-remaining,
                d.get('last_paid_at') or d['created_at'],d.get('last_operation_id') or '']
        if d.get('legacy_row'):
            sheet.update(range_name=f"A{d['legacy_row']}:O{d['legacy_row']}",values=[values],value_input_option='RAW')
        else:upsert_row(sheet,values,7,replace=True)


async def project_operation(payload):
    async with _async_lock:
        await _project_operation(payload)


async def _project_operation(payload):
    oid=payload['id'];kind=payload['kind'];payments=payload.get('payments',[])
    stamp=payload['occurred_at'];total=sum(p['amount'] for p in payments)
    payment_text=' + '.join(f"{p['wallet']} {p['amount']}" for p in payments)
    if kind in {'sale','debt_repayment','credit_deposit'}:
        sheet=_services.payments()
        if sheet is None:raise FinanceError('الشيت غير متاح.')
        rows=sheet.get_all_values()
        if not any(len(r)>7 and r[7]==oid for r in rows[1:]):
            # Keep exact A:H compatibility for /link and account delivery.
            sheet.append_row([stamp,total,payment_text,payload.get('product') or KINDS[kind],payload.get('customer_name') or 'عام','',str(payload.get('customer_id') or ''),oid],value_input_option='RAW')
        rows=sheet.get_all_values()
        if not any(len(r)>7 and r[7]==oid and int(r[1] or 0)==total for r in rows[1:]):raise FinanceError('دفعة الشيت بانتظار التحقق.')
    if kind in {'expense','refund'} or payload.get('fee'):
        sheet=_services.expenses()
        if sheet is None:raise FinanceError('ورقة المصروفات غير متاحة.')
        if sheet.col_count<9:sheet.resize(rows=sheet.row_count,cols=9)
        sheet.update(range_name='D1:I1',values=[['رقم العملية','النوع','المنتج','Chat ID','المحافظ','العملية الأصلية']],value_input_option='RAW')
        reason=payload.get('description') or (KINDS[kind]+' '+str(payload.get('product') or ''))
        upsert_row(sheet,[stamp,total if kind in {'expense','refund'} else payload['fee'],reason,oid,kind,payload.get('product') or '',str(payload.get('customer_id') or ''),payment_text,payload.get('reference_id') or ''],3)
    journal=ensure_sheet('السجل المالي',['رقم العملية','تاريخ الحركة','تاريخ التسجيل','النوع','الزبون','المنتج','المقبوض','الخارج','المبيعات','المصروف','المسترجع','الدين الجديد','رصيد مضاف','رصيد مستخدم','مرجع'])
    record=ledger().operation(oid)
    upsert_row(journal,[oid,stamp,record['recorded_at'],KINDS.get(kind,kind),payload.get('customer_name') or str(payload.get('customer_id') or ''),payload.get('product') or '',record['cash_in'],record['cash_out'],record['sales'] or 0,record['expense'],record['refunded'],payload.get('new_debt',0),payload.get('credit_added',0),payload.get('credit_used',0),payload.get('reference_id') or ''],0)
    accounts=ledger().accounts();wallet_sheet=_services.wallets()
    if wallet_sheet is None:raise FinanceError('ورقة الخزن غير متاحة.')
    names=list(dict.fromkeys([*wallet_sheet.row_values(1),*_services.wallet_names(),*(k[7:] for k in accounts if k.startswith('wallet:'))]))
    names=[n for n in names if n]
    if wallet_sheet.col_count<len(names):wallet_sheet.resize(rows=wallet_sheet.row_count,cols=len(names))
    wallet_sheet.update(range_name=f'A1:{column(len(names))}2',values=[names,[accounts.get('wallet:'+name,0) for name in names]],value_input_option='RAW')
    credit_sheet=ensure_sheet('أرصدة مالية معتمدة',['Chat ID','الرصيد'])
    cid=payload.get('customer_id') or record.get('customer_id')
    if cid is not None:upsert_row(credit_sheet,[str(cid),accounts.get('credit:'+str(cid),0)],0,replace=True)
    mirror_debts(payload)
    if payload.get('subscription') and not payload.get('correction_of'):
        await _services.subscription(payload)
    if payload.get('instagram_sale') or payload.get('instagram_commission'):
        _services.instagram(payload)
