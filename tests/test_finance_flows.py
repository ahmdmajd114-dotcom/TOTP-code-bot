from copy import deepcopy
from dataclasses import dataclass
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,AsyncMock

import financial_ledger as fl
import finance_ui as ui
import finance_integration as integration
from income_manager import start_income,handle_income_action,income_input,income_payload
from expense_manager import start_expense,handle_expense_action,expense_input,expense_payload


@dataclass
class Button:
    text:str
    callback_data:str

@dataclass
class Markup:
    inline_keyboard:list


class Query:
    def __init__(self,rows):self.rows=deepcopy(rows)
    def select(self,*args,**kwargs):return self
    def eq(self,key,value):
        self.rows=[r for r in self.rows if r.get(key)==value];return self
    def contains(self,key,fields):
        self.rows=[r for r in self.rows if all(r.get(key,{}).get(k)==v for k,v in fields.items())];return self
    def order(self,*args,**kwargs):return self
    def limit(self,n):self.rows=self.rows[:n];return self
    def execute(self):return SimpleNamespace(data=self.rows)


class FakeLedger:
    owner_id=99
    def __init__(self):
        self.states={};self.records={};self.debt_rows=[];self.balances={};self.posts=[]
        self.fail=False;self.lose_response=False;self.client=self
    def table(self,name):
        if name=='finance_drafts':return Query([{'owner_id':99,'state':s} for s in self.states.values()])
        return Query([{'owner_id':99,**r} for r in self.records.values()])
    def mode(self,**kwargs):return 'active'
    def active(self):return True
    def require_active(self):pass
    def save_draft(self,state):self.states[state['draft_id']]=deepcopy(state)
    def draft(self,key):return deepcopy(self.states[key])
    def operation(self,key):return self.records.get(key)
    def claim_outbox(self,oid=None):return {'id':oid,'token':1,'payload':self.records[oid]}
    def ack_outbox(self,*args,**kwargs):pass
    def accounts(self):return deepcopy(self.balances)
    def snapshot(self):return {'operations':[{'id':'OPEN','kind':'opening','sales':0,'occurred_at':fl.now_iso(),'recorded_at':fl.now_iso(),'posting_index':0,'payload':{'accounts':[{'key':k,'balance':v} for k,v in self.balances.items()]}}]+list(self.records.values()),'accounts':deepcopy(self.balances),'debts':deepcopy(self.debt_rows),'entries':[]}
    def debts(self,cid=None,open_only=False):return [deepcopy(d) for d in self.debt_rows if cid is None or d['customer_id']==cid]
    def operations(self,cid=None):return [r for r in self.records.values() if cid is None or r.get('customer_id')==cid]
    def post(self,payload,**kwargs):
        if self.fail:raise fl.FinanceError('offline')
        fl.validate(payload)
        self.posts.append(deepcopy(payload));self.records[payload['id']]=deepcopy(payload)
        if self.lose_response:
            self.lose_response=False;raise fl.FinanceError('response lost')
        return {'id':payload['id']}


class FinancialFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old=fl._ledger;self.old_ui=ui._services;self.old_config=integration._config
        self.db=FakeLedger();fl._ledger=self.db
        self.products=[{'id':'p1','name':'ChatGPT','is_active':True},{'id':'p2','name':'Scripta','is_active':True}]
        self.plans=[{'id':'m1','name':'شهر','price':8000,'duration':'شهر','is_active':True}]
        ui.configure_finance_ui(ui.FinanceServices(99,lambda:self.products,lambda pid:self.plans,
            lambda:['ماستر','زين كاش'],lambda cid:('زبون',None),Button,Markup,Mock(),AsyncMock(),AsyncMock(),AsyncMock()))
        self.context=SimpleNamespace(user_data={},bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=7)),
            edit_message_text=AsyncMock(),edit_message_caption=AsyncMock(),delete_business_messages=AsyncMock()))
    def tearDown(self):fl._ledger=self.old;ui._services=self.old_ui;integration._config=self.old_config
    def query(self,state,action,owner=99,revision=None):
        prefix={'income':'fi','expense':'fe','vault':'fv'}[state['flow']]
        q=SimpleNamespace(from_user=SimpleNamespace(id=owner),data=f"{prefix}:{state['draft_id']}:{revision if revision is not None else state['revision']}:{action}",
            message=SimpleNamespace(reply_text=AsyncMock()),answer=AsyncMock(),edit_message_text=AsyncMock(),edit_message_caption=AsyncMock())
        return SimpleNamespace(callback_query=q)

    async def test_single_plan_defaults_and_only_wallet_then_confirmation(self):
        state=await start_income(self.context,customer_id=123,customer_name='Ali')
        await handle_income_action(state,'product0',self.context,None)
        self.assertEqual(state['sales'],8000);self.assertEqual(state['step'],'wallet')
        state['wallet_options']=['ماستر'];await handle_income_action(state,'wallet0',self.context,None)
        self.assertEqual(state['step'],'summary');self.assertEqual(state['payments'],[{'wallet':'ماستر','amount':8000}])
        self.assertEqual(self.db.posts,[])

    async def test_multiple_plans_are_not_silently_selected(self):
        self.plans.append({'id':'y1','name':'سنة','price':50000,'is_active':True})
        state=await start_income(self.context,customer_id=123)
        await handle_income_action(state,'product0',self.context,None)
        self.assertEqual(state['step'],'plan');self.assertIsNone(state['plan_id'])

    async def test_expense_plan_price_is_never_used_as_purchase_cost(self):
        state=await start_expense(self.context,customer_id=123)
        await handle_expense_action(state,'product0',self.context,None)
        self.assertEqual(state['step'],'amount');self.assertNotIn('amount',state)
        expense_input(state,'15 ألف');self.assertEqual(state['amount'],15000)
        state['wallet_options']=['ماستر'];await handle_expense_action(state,'wallet0',self.context,None)
        self.assertEqual(expense_payload(state)['payments'][0]['amount'],15000)

    async def test_deposit_is_not_sale_and_uses_its_own_operation_prefix(self):
        s=await start_income(self.context,customer_id=123)
        await handle_income_action(s,'deposit',self.context,None)
        s['payments']=[{'wallet':'ماستر','amount':15000}]
        p=income_payload(s)
        self.assertEqual(p['sales'],0);self.assertEqual(p['credit_added'],15000)
        self.assertTrue(p['id'].startswith('PAY-CREDIT-'))

    async def test_partial_repayment_uses_actual_amount(self):
        self.db.debt_rows=[{'id':'D1','customer_id':123,'product':'ChatGPT','remaining':8000}]
        s=await start_income(self.context,customer_id=123)
        await handle_income_action(s,'repay',self.context,None)
        s['debt_options']=['D1'];await handle_income_action(s,'debt0',self.context,None)
        s.update(step='amount',payments=[{'wallet':'ماستر','amount':8000}]);income_input(s,'3000')
        p=income_payload(s)
        self.assertEqual(p['settlements'][0]['amount'],3000);self.assertTrue(p['id'].startswith('PAY-DEBT-'))

    async def test_fully_credit_funded_sale_needs_no_fake_cash(self):
        self.db.balances={'credit:123':8000}
        s=await start_income(self.context,customer_id=123)
        s['sales']=8000;s['product']='ChatGPT'
        await handle_income_action(s,'creditonly',self.context,None)
        p=income_payload(s);fl.validate(p)
        self.assertEqual(p['payments'],[]);self.assertEqual(p['credit_used'],8000)

    async def test_authorization_and_stale_buttons(self):
        s=await start_income(self.context,customer_id=123)
        await ui.handle_finance_callback(self.query(s,'purchase',owner=1),self.context)
        self.assertEqual(self.db.draft(s['draft_id'])['revision'],s['revision'])
        await ui.handle_finance_callback(self.query(s,'deposit',revision=0),self.context)
        self.assertEqual(self.db.draft(s['draft_id'])['kind'],'sale')

    async def test_failed_commit_preserves_draft_for_retry(self):
        s=await start_income(self.context,customer_id=123)
        s.update(step='summary',product='ChatGPT',sales=8000,payments=[{'wallet':'ماستر','amount':8000}]);self.db.save_draft(s)
        self.db.fail=True
        await ui.handle_finance_callback(self.query(s,'commit'),self.context)
        pending=self.db.draft(s['draft_id']);self.assertNotIn('committed',pending);self.assertIn('prepared_payload',pending)
        self.db.fail=False
        await ui.handle_finance_callback(self.query(pending,'commit'),self.context)
        self.assertTrue(self.db.draft(s['draft_id'])['committed']);self.assertEqual(len(self.db.posts),1)

    async def test_lost_success_response_is_detected_even_after_restart(self):
        s=await start_income(self.context,customer_id=123)
        s.update(step='summary',product='ChatGPT',sales=8000,payments=[{'wallet':'ماستر','amount':8000}]);self.db.save_draft(s)
        self.db.lose_response=True
        await ui.handle_finance_callback(self.query(s,'commit'),self.context)
        recovered=self.db.draft(s['draft_id'])
        # Simulate a new process: callback rereads persisted state and operation.
        await ui.handle_finance_callback(self.query(recovered,'commit'),self.context)
        self.assertEqual(len(self.db.posts),1)

    async def test_multiple_pending_inputs_require_reply(self):
        a=await start_expense(self.context);a.update(step='amount',awaiting=True);self.db.save_draft(a)
        b=await start_expense(self.context);b.update(step='amount',awaiting=True,message_id=8);self.db.save_draft(b)
        a['awaiting']=True;self.db.save_draft(a)
        message=SimpleNamespace(text='5000',reply_to_message=None,reply_text=AsyncMock())
        update=SimpleNamespace(message=message,effective_user=SimpleNamespace(id=99))
        self.assertTrue(await ui.handle_finance_input(update,self.context));self.assertNotIn('amount',self.db.draft(a['draft_id']))
        message.reply_to_message=SimpleNamespace(message_id=8)
        self.assertTrue(await ui.handle_finance_input(update,self.context));self.assertEqual(self.db.draft(b['draft_id'])['amount'],5000)

    async def test_sarf_deletes_before_looking_up_or_opening_draft(self):
        sequence=[]
        self.context.bot.delete_business_messages.side_effect=lambda **kwargs:sequence.append('delete')
        self.db.require_active=lambda:sequence.append('database')
        self.context.bot.send_message.side_effect=lambda **kwargs:(sequence.append('card') or SimpleNamespace(message_id=7))
        bm=SimpleNamespace(business_connection_id='b',message_id=12)
        await integration.financial_shortcut(self.context,bm,123,'صرف','Ali')
        self.assertEqual(sequence[:2],['delete','database'])
        self.assertIn('card',sequence);self.assertEqual(self.db.posts,[])

    async def test_qabd_uses_current_customer_and_deletes_first(self):
        sequence=[]
        self.context.bot.delete_business_messages.side_effect=lambda **kwargs:sequence.append('delete')
        self.db.require_active=lambda:sequence.append('database')
        bm=SimpleNamespace(business_connection_id='b',message_id=15)
        self.assertTrue(await integration.financial_shortcut(self.context,bm,123,'قبض','Ali'))
        self.assertEqual(sequence[:2],['delete','database'])
        state=next(iter(self.db.states.values()))
        self.assertEqual(state['flow'],'income');self.assertEqual(state['customer_id'],123)
        self.assertEqual(state['step'],'product');self.assertEqual(self.db.posts,[])

    async def test_back_at_first_step_returns_menu_without_writing_money(self):
        s=await start_income(self.context,customer_id=123)
        update=self.query(s,'back')
        await ui.handle_finance_callback(update,self.context)
        self.assertEqual(self.db.posts,[])
        self.assertEqual(self.db.draft(s['draft_id'])['step'],'product')
        self.assertIn('المالية',update.callback_query.message.reply_text.call_args.args[0])

    async def test_back_preserves_expense_amount_and_wallet(self):
        s=await start_expense(self.context,customer_id=123)
        await handle_expense_action(s,'product0',self.context,None)
        expense_input(s,'15000')
        from expense_manager import render_expense
        await render_expense(s,self.context)
        self.assertEqual(s['step'],'wallet')
        await ui.handle_finance_callback(self.query(s,'back'),self.context)
        after=self.db.draft(s['draft_id'])
        self.assertEqual(after['step'],'amount');self.assertEqual(after['amount'],15000)
        self.assertEqual(self.db.posts,[])

    async def test_back_from_manual_report_clears_waiting_input(self):
        s=await start_income(self.context,customer_id=123)
        update=self.query(s,'noop');update.callback_query.data='finance_period_custom'
        await ui.handle_finance_menu(update,self.context)
        self.assertEqual(self.context.user_data['finance_report_input'],'period')
        update.callback_query.data='finance_reports'
        await ui.handle_finance_menu(update,self.context)
        self.assertNotIn('finance_report_input',self.context.user_data)
        self.assertEqual(self.db.posts,[])

    async def test_reversal_card_has_back_and_returns_to_original_record(self):
        self.db.records['PAY-ORIGINAL']={'id':'PAY-ORIGINAL','kind':'expense','historical':False,'occurred_at':fl.now_iso(),'recorded_at':fl.now_iso(),'cash_in':0,'cash_out':3000,'payload':{'description':'server'}}
        s=ui.draft('vault',kind='reversal',reference_id='PAY-ORIGINAL',description='mistake',step='summary',return_to='finance_view_PAY-ORIGINAL')
        from vault_manager import render_vault
        await render_vault(s,self.context)
        callbacks=[b.callback_data.rsplit(':',1)[1] for line in self.context.bot.send_message.call_args.kwargs['reply_markup'].inline_keyboard for b in line]
        self.assertIn('back',callbacks);self.assertNotIn('amount',callbacks)
        update=self.query(s,'back');await ui.handle_finance_callback(update,self.context)
        self.assertIn('PAY-ORIGINAL',update.callback_query.message.reply_text.call_args.args[0])
        self.assertEqual(self.db.posts,[])

    async def test_transfer_opens_goes_back_and_commits_once(self):
        from vault_manager import start_vault_action
        await start_vault_action(self.context,'transfer')
        key=next(iter(self.db.states))
        for action in ('wallet0','wallet1'):
            s=self.db.draft(key)
            await ui.handle_finance_callback(self.query(s,action),self.context)
        message=SimpleNamespace(text='5000',reply_to_message=SimpleNamespace(message_id=7),reply_text=AsyncMock())
        update=SimpleNamespace(message=message,effective_user=SimpleNamespace(id=99))
        await ui.handle_finance_input(update,self.context)
        message.text='نقل رصيد';await ui.handle_finance_input(update,self.context)
        s=self.db.draft(key);self.assertEqual(s['step'],'summary')
        await ui.handle_finance_callback(self.query(s,'back'),self.context)
        s=self.db.draft(key)
        self.assertEqual(s['step'],'reason');self.assertEqual(s['amount'],5000)
        self.assertEqual(s['wallet'],'ماستر');self.assertEqual(s['to_wallet'],'زين كاش')
        self.assertEqual(self.db.posts,[])
        await ui.handle_finance_input(update,self.context)
        s=self.db.draft(key);await ui.handle_finance_callback(self.query(s,'commit'),self.context)
        self.assertEqual(len(self.db.posts),1)
        p=self.db.posts[0];self.assertEqual(p['kind'],'transfer')
        self.assertEqual(p['payments'],[{'wallet':'ماستر','amount':5000}])
        self.assertEqual(p['to_wallet'],'زين كاش')

    async def test_credit_adjustment_card_opens_and_cannot_create_cash(self):
        from customer_balance_manager import start_credit_adjustment
        self.db.balances['credit:123']=1000
        await start_credit_adjustment(self.context,123,500,'تصحيح سابق')
        s=next(iter(self.db.states.values()))
        await ui.handle_finance_callback(self.query(s,'commit'),self.context)
        self.assertEqual(self.db.posts[0]['kind'],'credit_adjustment')
        self.assertEqual(self.db.posts[0]['adjustment'],500)
        self.assertEqual(self.db.posts[0]['payments'],[])

    async def test_refund_stays_linked_to_one_sale_and_never_cancels_other_accounts(self):
        s=await start_expense(self.context,refund={'id':'PAY-OLD','customer_id':123,'product':'ChatGPT','sales':8000})
        expense_input(s,'3000');s['wallet_options']=['ماستر'];await handle_expense_action(s,'wallet0',self.context,None)
        p=expense_payload(s);self.assertEqual(p['reference_id'],'PAY-OLD');self.assertEqual(p['kind'],'refund')
        self.assertNotIn('cancel_all_subscriptions',p)

    def test_debt_card_identifier_survives_new_debts_and_order_changes(self):
        self.db.debt_rows=[{'id':'Z','customer_id':123,'customer_name':'Ali','product':'ChatGPT','remaining':8000,'original':8000,'created_at':'2026-10-28T10:00:00Z'}]
        first=integration.financial_debt_records()[0]['row_number']
        self.db.debt_rows.insert(0,{**self.db.debt_rows[0],'id':'A'})
        after=next(d for d in integration.financial_debt_records() if d['debt_id']=='Z')['row_number']
        self.assertEqual(first,after)


if __name__=='__main__':unittest.main()
