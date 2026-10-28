import os,json,subprocess,unittest,uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime,timezone
from unittest.mock import Mock

from financial_ledger import FinanceError,Ledger,money,validate,build_legacy_snapshot,parse_wallets
from income_manager import income_payload
from reports_manager import totals


class AmountTests(unittest.TestCase):
    def test_integer_dinars_and_explicit_units(self):
        for text,expected in [('١٥٬٠٠٠',15000),('15 ألف',15000),('7.5 ألف',7500),('1 مليون',1000000),('15000.0',15000)]:
            self.assertEqual(money(text),expected)
        for text in ['7.5','دين 8000',True,15.2,'NaN','-8000']:
            with self.assertRaises(FinanceError):money(text)

    def test_wallet_split(self):
        self.assertEqual(parse_wallets('ماستر 5000 + زين كاش 3 ألف',['ماستر','زين كاش']),[{'wallet':'ماستر','amount':5000},{'wallet':'زين كاش','amount':3000}])

    def test_purchase_debt_and_balance_share_one_receipt(self):
        s={'operation_id':'PAY-TEST','kind':'sale','occurred_at':'2026-10-28T00:00:00Z','customer_id':7,'product':'ChatGPT','sales':8000,
           'payments':[{'wallet':'master','amount':15000}],'settlements':[{'debt_id':'D1','amount':3000}],'difference':'credit'}
        p=income_payload(s)
        self.assertEqual(p['credit_added'],4000)
        self.assertEqual(validate(p),p)

    def test_underpayment_requires_explicit_choice(self):
        s={'operation_id':'PAY-TEST','kind':'sale','occurred_at':'2026-10-28T00:00:00Z','customer_id':7,'product':'ChatGPT','sales':8000,'payments':[{'wallet':'master','amount':7800}]}
        with self.assertRaises(FinanceError):income_payload(s)
        for decision,field in [('debt','new_debt'),('fee','fee')]:
            s['difference']=decision;self.assertEqual(income_payload(s)[field],200);validate(income_payload(s))
        s['difference']='discount';self.assertEqual(income_payload(s)['sales'],7800)

    def test_network_error_never_falls_back_to_legacy(self):
        c=Mock();c.rpc.return_value.execute.side_effect=RuntimeError('offline')
        with self.assertRaises(FinanceError):Ledger(c,1).active()

    def test_absent_schema_is_explicit_legacy_mode(self):
        class Missing(Exception):code='PGRST202'
        c=Mock();c.rpc.return_value.execute.side_effect=Missing()
        self.assertEqual(Ledger(c,1).mode(),'missing')

    def test_old_active_schema_blocks_posts_before_money_rpc(self):
        c=Mock();c.rpc.return_value.execute.return_value.data={'mode':'active'}
        with self.assertRaisesRegex(FinanceError,'حماية تدقيق الخزن'):
            Ledger(c,1).post({'id':'PAY-NEW','kind':'capital','payments':[{'wallet':'master','amount':1000}]})
        self.assertEqual([call.args[0] for call in c.rpc.call_args_list],['finance_setup'])

    def test_old_schema_cannot_begin_or_activate_setup(self):
        for action in ('begin','activate'):
            c=Mock();c.rpc.return_value.execute.return_value.data={'mode':'legacy'}
            with self.assertRaisesRegex(FinanceError,'حماية تدقيق الخزن'):Ledger(c,1).setup(action)
            self.assertEqual(c.rpc.call_count,1)

    def test_current_schema_allows_atomic_post(self):
        c=Mock();c.rpc.return_value.execute.side_effect=[Mock(data={'mode':'active','schema_version':2}),Mock(data={'id':'PAY-NEW'})]
        self.assertEqual(Ledger(c,1).post({'id':'PAY-NEW','kind':'capital','payments':[{'wallet':'master','amount':1000}]}),{'id':'PAY-NEW'})
        self.assertEqual([call.args[0] for call in c.rpc.call_args_list],['finance_setup','finance_post'])

    def test_historical_import_does_not_add_cash_to_opening_wallets(self):
        p=[['date','total','wallet','product','name','account','chat'],['2026-10-27','15000','master 15000','ChatGPT','Ali','','123']]
        s=build_legacy_snapshot(p,[['date','amount','reason']],[],[],[],{'master':50000})
        self.assertEqual(s['accounts'],[{'key':'wallet:master','balance':50000}])
        self.assertEqual(s['operations'][0]['cash_in'],15000)
        self.assertNotIn('sales',s['operations'][0])

    def test_shifted_historical_expense_is_imported_once(self):
        e=[['date','amount','reason'],['']*80+['2026-10-27','15000','Scripta']]
        s=build_legacy_snapshot([],e,[],[],[],{})
        self.assertEqual(len(s['operations']),1);self.assertEqual(s['operations'][0]['expense'],15000)

    def test_unrecognized_expense_blocks_import_instead_of_disappearing(self):
        with self.assertRaises(FinanceError):build_legacy_snapshot([],[['date','amount','reason'],['']*80+['unknown','cash','Scripta']],[],[],[],{})

    def test_reports_keep_reversal_on_its_own_date(self):
        sale=dict(id='A',kind='sale',occurred_at='2026-10-26T12:00:00Z',cash_in=8000,cash_out=0,sales=8000,expense=0)
        rev=dict(id='B',kind='reversal',occurred_at='2026-10-27T12:00:00Z',reference_id='A',cash_in=0,cash_out=0,sales=0,expense=0)
        self.assertEqual(totals([sale,rev],'2026-10-26')['received'],8000)
        self.assertEqual(totals([sale,rev],'2026-10-27')['spent'],8000)
        self.assertEqual(totals([sale,rev])['sales'],0)


@unittest.skipUnless(os.environ.get('FINANCE_TEST_DSN'),'Set FINANCE_TEST_DSN to an isolated PostgreSQL database')
class PostgresTransactionTests(unittest.TestCase):
    def sql(self,sql,ok=True):
        r=subprocess.run(['/opt/homebrew/bin/psql',os.environ['FINANCE_TEST_DSN'],'-XAt','-v','ON_ERROR_STOP=1','-c',sql],text=True,capture_output=True)
        if ok and r.returncode:self.fail(r.stderr)
        if not ok:self.assertNotEqual(r.returncode,0,r.stdout)
        return r.stdout.strip()

    def json(self,value):return "'"+json.dumps(value,ensure_ascii=False).replace("'","''")+"'::jsonb"

    def rpc(self,p,ok=True):return self.sql(f'select finance_post({self.owner},{self.json(p)})',ok)

    def setUp(self):
        self.owner=uuid.uuid4().int%10**12
        self.sql(f"select finance_setup({self.owner},'begin')")
        snap={'accounts':[{'key':'wallet:master','balance':100000},{'key':'wallet:zain','balance':50000}],'debts':[],'operations':[]}
        self.sql(f"select finance_setup({self.owner},'stage',{self.json(snap)});select finance_setup({self.owner},'activate')")

    def op(self,kind='sale',**fields):
        p={'id':'PAY-'+uuid.uuid4().hex[:16],'kind':kind,'occurred_at':'2026-10-27T12:00:00Z',**fields}
        if kind=='sale':p={'customer_id':123,'product':'ChatGPT','sales':8000,'payments':[{'wallet':'master','amount':8000}],**p}
        return p

    def balances(self):
        return json.loads(self.sql(f"select coalesce(jsonb_object_agg(key,balance),'{{}}') from finance_accounts where owner_id={self.owner}"))

    def debt(self,amount=8000):
        p=self.op('debt_issue',customer_id=123,customer_name='Ali',product='ChatGPT',sales=amount,new_debt=amount,payments=[]);self.rpc(p);return 'DEBT-'+p['id']

    def remaining(self,did):return int(self.sql(f"select remaining from finance_debts where owner_id={self.owner} and id='{did}'"))

    def test_sale_and_credit_are_one_atomic_commit(self):
        p=self.op(payments=[{'wallet':'master','amount':15000}],credit_added=7000);self.rpc(p)
        self.assertEqual(self.balances()['wallet:master'],115000);self.assertEqual(self.balances()['credit:123'],7000)

    def test_duplicate_and_lost_response_do_not_repeat_money(self):
        p=self.op();self.rpc(p);self.assertIn('true',self.rpc(p));self.assertEqual(self.balances()['wallet:master'],108000)
        q=deepcopy(p);q['sales']=9000;self.rpc(q,False);self.assertEqual(self.balances()['wallet:master'],108000)

    def test_incorrect_allocation_rolls_back_every_table(self):
        self.rpc(self.op(sales=9000),False)
        self.assertEqual(self.balances()['wallet:master'],100000)
        self.assertEqual(self.sql(f'select count(*) from finance_outbox where owner_id={self.owner}'),'0')

    def test_partial_and_full_repayment(self):
        did=self.debt()
        self.rpc(self.op('debt_repayment',customer_id=123,settlements=[{'debt_id':did,'amount':3000,'expected_remaining':8000}],payments=[{'wallet':'master','amount':3000}]))
        self.assertEqual(self.remaining(did),5000)
        self.rpc(self.op('debt_repayment',customer_id=123,settlements=[{'debt_id':did,'amount':5000}],payments=[{'wallet':'master','amount':5000}]))
        self.assertEqual(self.remaining(did),0)

    def test_wrong_customer_or_stale_debt_cannot_change_money(self):
        did=self.debt()
        p=self.op('debt_repayment',customer_id=321,settlements=[{'debt_id':did,'amount':3000}],payments=[{'wallet':'master','amount':3000}])
        self.rpc(p,False);p['customer_id']=123;p['settlements'][0]['expected_remaining']=5000;self.rpc(p,False)
        self.assertEqual(self.remaining(did),8000);self.assertEqual(self.balances()['wallet:master'],100000)

    def test_mixed_receipt_sale_debt_and_credit(self):
        did=self.debt(3000)
        self.rpc(self.op(payments=[{'wallet':'master','amount':15000}],settlements=[{'debt_id':did,'amount':3000}],credit_added=4000))
        self.assertEqual(self.remaining(did),0);self.assertEqual(self.balances()['credit:123'],4000)

    def test_credit_cannot_be_overspent_and_is_atomic(self):
        self.rpc(self.op(payments=[{'wallet':'master','amount':5000}],credit_used=3000),False)
        self.assertEqual(self.balances()['wallet:master'],100000)
        self.rpc(self.op('credit_deposit',customer_id=123,credit_added=3000,payments=[{'wallet':'master','amount':3000}]))
        self.rpc(self.op(payments=[{'wallet':'master','amount':5000}],credit_used=3000))
        self.assertEqual(self.balances()['credit:123'],0)

    def test_debt_paid_from_credit_changes_no_treasury_and_retries_once(self):
        did=self.debt()
        p=self.op('debt_repayment',customer_id=123,settlements=[{'debt_id':did,'amount':8000}],credit_used=8000,payments=[])
        self.rpc(p,False)
        self.assertEqual(self.remaining(did),8000)
        self.rpc(self.op('credit_deposit',customer_id=123,credit_added=8000,payments=[{'wallet':'master','amount':8000}]))
        before=self.balances();self.rpc(p);self.assertIn('true',self.rpc(p))
        after=self.balances()
        self.assertEqual(after['wallet:master'],before['wallet:master'])
        self.assertEqual(after['wallet:zain'],before['wallet:zain'])
        self.assertEqual(after['credit:123'],0);self.assertEqual(self.remaining(did),0)

    def test_transfer_expense_and_capital(self):
        self.rpc(self.op('transfer',to_wallet='zain',payments=[{'wallet':'master','amount':5000}]))
        self.assertEqual(self.balances()['wallet:master'],95000);self.assertEqual(self.balances()['wallet:zain'],55000)
        self.assertEqual(self.sql(f'select sum(sales) from finance_operations where owner_id={self.owner}'),'0')
        self.rpc(self.op('expense',payments=[{'wallet':'master','amount':3000}],description='server'))
        self.assertEqual(self.balances()['wallet:master'],92000)

    def test_refund_link_and_limit(self):
        sale=self.op();self.rpc(sale)
        self.rpc(self.op('refund',customer_id=123,reference_id=sale['id'],payments=[{'wallet':'master','amount':3000}]))
        self.rpc(self.op('refund',customer_id=123,reference_id=sale['id'],payments=[{'wallet':'master','amount':6000}]),False)
        self.assertEqual(self.balances()['wallet:master'],105000)

    def test_refund_cannot_return_unpaid_debt(self):
        sale=self.op(payments=[{'wallet':'master','amount':5000}],new_debt=3000);self.rpc(sale)
        self.rpc(self.op('refund',customer_id=123,reference_id=sale['id'],payments=[{'wallet':'master','amount':8000}]),False)

    def test_reversal_and_duplicate_reversal(self):
        sale=self.op();self.rpc(sale)
        rev=self.op('reversal',reference_id=sale['id']);self.rpc(rev);self.rpc(rev)
        self.assertEqual(self.balances()['wallet:master'],100000)
        self.rpc(self.op('reversal',reference_id=sale['id']),False)

    def test_correction_failure_does_not_leave_original_reversed(self):
        sale=self.op();self.rpc(sale)
        bad=self.op(sales=9000)
        self.sql(f"select finance_correct({self.owner},'{sale['id']}',{self.json(bad)})",False)
        self.assertEqual(self.balances()['wallet:master'],108000)
        good=self.op(sales=9000,payments=[{'wallet':'master','amount':9000}])
        self.sql(f"select finance_correct({self.owner},'{sale['id']}',{self.json(good)})")
        self.assertEqual(self.balances()['wallet:master'],109000)

    def test_proof_reuse_is_rejected(self):
        self.rpc(self.op(proof_id='receipt-1'));self.rpc(self.op(proof_id='receipt-1'),False)
        self.assertEqual(self.balances()['wallet:master'],108000)

    def test_database_rejects_fractional_dinars(self):
        self.rpc(self.op(sales=8000.5),False)
        self.rpc(self.op(payments=[{'wallet':'master','amount':8000.5}]),False)

    def test_concurrent_same_operation_is_once(self):
        p=self.op()
        with ThreadPoolExecutor(4) as pool:list(pool.map(lambda _:self.rpc(p),range(4)))
        self.assertEqual(self.balances()['wallet:master'],108000)

    def test_concurrent_repayments_cannot_overdraw_debt(self):
        did=self.debt()
        def post(_):
            p=self.op('debt_repayment',customer_id=123,settlements=[{'debt_id':did,'amount':6000}],payments=[{'wallet':'master','amount':6000}])
            return subprocess.run(['/opt/homebrew/bin/psql',os.environ['FINANCE_TEST_DSN'],'-XAt','-v','ON_ERROR_STOP=1','-c',f'select finance_post({self.owner},{self.json(p)})'],capture_output=True).returncode
        with ThreadPoolExecutor(2) as pool:codes=list(pool.map(post,range(2)))
        self.assertEqual(codes.count(0),1);self.assertEqual(self.remaining(did),2000)

    def test_outbox_lease_retry_and_stale_ack(self):
        p=self.op();self.rpc(p)
        job=json.loads(self.sql(f'select finance_claim_outbox({self.owner})'))
        self.assertEqual(self.sql(f'select finance_claim_outbox({self.owner})'),'')
        self.sql(f"select finance_ack_outbox({self.owner},'{job['id']}',{job['token']+1},null)")
        self.assertEqual(self.sql(f"select status from finance_outbox where owner_id={self.owner}"),'working')
        self.sql(f"select finance_ack_outbox({self.owner},'{job['id']}',{job['token']},'offline')")
        self.assertEqual(self.sql(f"select status from finance_outbox where owner_id={self.owner}"),'pending')

    def test_wallet_reconciliation_rejects_stale_balance(self):
        self.rpc(self.op('vault_adjustment',wallet='master',adjustment=-5000,description='count',expected_balance=99999),False)
        self.rpc(self.op('vault_adjustment',wallet='master',adjustment=1000,description='count',payments=[{'wallet':'master','amount':1000}]),False)
        self.assertEqual(self.balances()['wallet:master'],100000)

    def test_service_role_only(self):
        self.sql(f"set role anon;select finance_post({self.owner},{self.json(self.op())})",False)

    def test_review_freezes_new_money_and_can_be_cancelled(self):
        owner=self.owner+10**12
        self.sql(f"select finance_setup({owner},'begin')")
        self.sql(f'select finance_post({owner},{self.json(self.op())})',False)
        self.sql(f"select finance_setup({owner},'cancel')")
        self.assertEqual(self.sql(f'select mode from finance_settings where owner_id={owner}'),'legacy')

    def test_activation_imports_history_without_reapplying_wallet_effects(self):
        owner=self.owner+10**12
        snap={'accounts':[{'key':'wallet:master','balance':50000}], 'debts':[],
            'operations':[{'id':'HIST-OLD','kind':'legacy_receipt','occurred_at':'2026-10-26T12:00:00Z','cash_in':8000}]}
        self.sql(f"select finance_setup({owner},'begin');select finance_setup({owner},'stage',{self.json(snap)});select finance_setup({owner},'activate')")
        data=json.loads(self.sql(f'select finance_read({owner})'))
        self.assertEqual(data['accounts']['wallet:master'],50000)
        self.assertEqual(len(data['operations']),2)
        self.assertTrue(any(r['kind']=='opening' for r in data['operations']))
        self.sql(f"select finance_setup({owner},'activate')",False)

    def test_reversal_rejects_credit_already_used(self):
        sale=self.op(payments=[{'wallet':'master','amount':15000}],credit_added=7000);self.rpc(sale)
        self.rpc(self.op(payments=[{'wallet':'master','amount':1000}],credit_used=7000))
        self.rpc(self.op('reversal',reference_id=sale['id']),False)
        self.assertEqual(self.balances()['credit:123'],0)

    def test_unexplained_balance_edit_blocks_new_financial_writes(self):
        self.sql(f"update finance_accounts set balance=balance+777 where owner_id={self.owner} and key='wallet:master'")
        p=self.op();self.rpc(p,False)
        self.assertEqual(self.balances()['wallet:master'],100777)
        self.assertEqual(self.sql(f"select count(*) from finance_operations where owner_id={self.owner} and id='{p['id']}'"),'0')
        from vault_manager import audit_balances
        audit=audit_balances(json.loads(self.sql(f'select finance_read({self.owner})')))
        self.assertEqual(audit['wallet:master']['difference'],777)

    def test_refund_can_be_undone_once_and_refund_limit_is_restored(self):
        sale=self.op();self.rpc(sale)
        refund=self.op('refund',customer_id=123,reference_id=sale['id'],payments=[{'wallet':'master','amount':3000}]);self.rpc(refund)
        reverse=self.op('reversal',reference_id=refund['id']);self.rpc(reverse);self.rpc(reverse)
        self.assertEqual(self.balances()['wallet:master'],108000)
        self.rpc(self.op('reversal',reference_id=refund['id']),False)
        self.rpc(self.op('refund',customer_id=123,reference_id=sale['id'],payments=[{'wallet':'master','amount':8000}]))
        self.assertEqual(self.balances()['wallet:master'],100000)

    def test_new_debt_can_be_undone_before_repayment_without_changing_wallets(self):
        debt=self.op('debt_issue',customer_id=123,customer_name='Ali',product='ChatGPT',sales=8000,new_debt=8000,payments=[])
        self.rpc(debt)
        self.rpc(self.op('reversal',reference_id=debt['id']))
        self.assertEqual(self.remaining('DEBT-'+debt['id']),0)
        self.assertEqual(self.balances()['wallet:master'],100000)

    def test_transfer_undo_restores_both_wallets_once(self):
        transfer=self.op('transfer',to_wallet='zain',payments=[{'wallet':'master','amount':5000}]);self.rpc(transfer)
        reverse=self.op('reversal',reference_id=transfer['id']);self.rpc(reverse);self.rpc(reverse)
        self.assertEqual(self.balances()['wallet:master'],100000)
        self.assertEqual(self.balances()['wallet:zain'],50000)

    def test_corrected_sale_keeps_receipt_proof_reserved(self):
        sale=self.op(proof_id='receipt-original');self.rpc(sale)
        replacement=self.op(sales=9000,payments=[{'wallet':'master','amount':9000}],proof_id='receipt-original')
        self.sql(f"select finance_correct({self.owner},'{sale['id']}',{self.json(replacement)})")
        self.assertEqual(self.balances()['wallet:master'],109000)
        self.rpc(self.op(proof_id='receipt-original'),False)

    def test_statement_has_exact_posting_sequence_for_simultaneous_timestamps(self):
        from vault_manager import wallet_statement
        first=self.op(id='PAY-ZZZ-LAST-LEX');second=self.op(id='PAY-AAA-FIRST-LEX')
        self.sql(f'select finance_post({self.owner},{self.json(first)}); select finance_post({self.owner},{self.json(second)})')
        data=json.loads(self.sql(f'select finance_read({self.owner})'))
        audit,rows=wallet_statement(data,'master')
        self.assertEqual([(r['id'],r['balance']) for r in rows],[(second['id'],116000),(first['id'],108000)])
        self.assertTrue(audit['matched'])


if __name__=='__main__':unittest.main()
