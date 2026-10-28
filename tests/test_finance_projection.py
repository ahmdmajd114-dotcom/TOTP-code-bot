from copy import deepcopy
from dataclasses import fields
from types import SimpleNamespace
import re,unittest
import asyncio,threading
from unittest.mock import AsyncMock,Mock

import finance_projection as projection
import financial_ledger as fl


class Sheet:
    col_count=20;row_count=1000
    def __init__(self,title,rows=None):self.title=title;self.rows=deepcopy(rows or [[]]);self.lost=False
    def get_all_values(self):return deepcopy(self.rows)
    def row_values(self,n):return deepcopy(self.rows[n-1])
    def resize(self,**kwargs):self.col_count=kwargs['cols']
    def add_rows(self,n):self.row_count+=n
    def append_row(self,row,**kwargs):self.rows.append([str(v) for v in row]);self._respond()
    def _respond(self):
        if self.lost:self.lost=False;raise TimeoutError('saved response lost')
    def update(self,*,range_name,values,value_input_option):
        m=re.fullmatch(r'([A-Z]+)(\d+):([A-Z]+)(\d+)',range_name);col,start,_,end=m.groups()
        start,end=int(start),int(end);offset=ord(col)-65
        for n,value in enumerate(values,start):
            while len(self.rows)<n:self.rows.append([])
            self.rows[n-1].extend(['']*max(0,offset+len(value)-len(self.rows[n-1])))
            self.rows[n-1][offset:offset+len(value)]=[str(v) for v in value]
        self._respond()


class ProjectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old=projection._services;self.oldledger=fl._ledger
        self.pay=Sheet('payments',[['date','amount','method','product','name','account','chat','id']])
        self.exp=Sheet('expenses',[['date','amount','reason']]);self.wallet=Sheet('wallet',[['master'],['100000']]);self.debt=Sheet('debts')
        self.sheets={}
        def add(**kwargs):
            sheet=Sheet(kwargs['title']);self.sheets[sheet.title]=sheet;return sheet
        book=SimpleNamespace(worksheets=lambda:list(self.sheets.values()),add_worksheet=add)
        self.p={'id':'PAY-12345678','kind':'sale','occurred_at':'2026-10-28T10:00:00Z','customer_id':123,'customer_name':'Ali','product':'ChatGPT','sales':8000,'payments':[{'wallet':'master','amount':8000}]}
        self.balances={'wallet:master':108000,'credit:123':0}
        self.record={'id':self.p['id'],'recorded_at':'2026-10-28T10:00:02Z','cash_in':8000,'cash_out':0,'sales':8000,'expense':0,'refunded':0,'customer_id':123}
        fl._ledger=SimpleNamespace(operations=lambda: [self.record],operation=lambda oid:self.record,accounts=lambda:deepcopy(self.balances),debts=lambda cid=None:[])
        self.subscription=AsyncMock();self.ig=Mock()
        projection.configure_projection(projection.ProjectionServices(lambda:book,lambda:self.pay,lambda:self.exp,lambda:self.wallet,lambda:['master'],lambda:self.debt,self.subscription,self.ig))
    def tearDown(self):projection._services=self.old;fl._ledger=self.oldledger

    async def test_retry_never_increments_wallet_or_appends_receipt_twice(self):
        await projection.project_operation(self.p);await projection.project_operation(self.p)
        self.assertEqual(len(self.pay.rows),2);self.assertEqual(self.wallet.rows[1],['108000'])
        self.assertEqual(len(self.sheets['السجل المالي'].rows),2)

    async def test_lost_sheet_response_is_recognized_on_retry(self):
        self.pay.lost=True
        with self.assertRaises(TimeoutError):await projection.project_operation(self.p)
        await projection.project_operation(self.p)
        self.assertEqual(len(self.pay.rows),2);self.assertEqual(self.wallet.rows[1],['108000'])

    async def test_refund_is_one_expense_and_original_receipt_is_preserved(self):
        await projection.project_operation(self.p);original=deepcopy(self.pay.rows)
        refund={**self.p,'id':'EXP-REFUND','kind':'refund','reference_id':self.p['id'],'description':'refund','payments':[{'wallet':'master','amount':3000}]}
        self.record.update(id='EXP-REFUND',cash_in=0,cash_out=3000,sales=0,refunded=3000)
        self.balances['wallet:master']=105000
        await projection.project_operation(refund);await projection.project_operation(refund)
        self.assertEqual(self.pay.rows,original);self.assertEqual(len(self.exp.rows),2)
        self.assertEqual(self.wallet.rows[1],['105000'])

    async def test_subscription_failure_does_not_repeat_money_projections(self):
        self.p['subscription']={'plan_name':'month'};self.subscription.side_effect=RuntimeError('offline')
        with self.assertRaises(RuntimeError):await projection.project_operation(self.p)
        self.subscription.side_effect=None
        await projection.project_operation(self.p)
        self.assertEqual(len(self.pay.rows),2);self.assertEqual(self.wallet.rows[1],['108000'])

    async def test_blocked_google_does_not_block_telegram_event_loop(self):
        loop=asyncio.get_running_loop();started=asyncio.Event();release=threading.Event()
        original=self.pay.get_all_values
        def slow_read():
            loop.call_soon_threadsafe(started.set);release.wait(3);return original()
        self.pay.get_all_values=slow_read
        task=asyncio.create_task(projection.project_operation(self.p))
        try:
            await asyncio.wait_for(started.wait(),1)
            self.assertFalse(task.done())
            # This scheduling point cannot run while Sheets executes on-loop.
            tick=asyncio.Event();loop.call_soon(tick.set)
            await asyncio.wait_for(tick.wait(),1)
        finally:
            release.set();await task
        self.assertEqual(len(self.pay.rows),2)

    async def test_concurrent_projection_attempts_still_have_one_receipt(self):
        await asyncio.gather(projection.project_operation(self.p),projection.project_operation(self.p))
        self.assertEqual(len(self.pay.rows),2)
        self.assertEqual(self.wallet.rows[1],['108000'])


if __name__=='__main__':unittest.main()
