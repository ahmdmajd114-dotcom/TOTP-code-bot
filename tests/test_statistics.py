from copy import deepcopy
from datetime import datetime
import io,json,os,subprocess,unittest,uuid,zipfile,csv
from types import SimpleNamespace
from unittest.mock import Mock,AsyncMock,patch

from financial_ledger import FinanceError
from statistics_engine import build_report,make_events,period_bounds,stamp
from statistics_operations import operational_report
from statistics_export import export_report,safe_cell
from statistics_manager import summary_text

NOW=stamp('2026-10-28T13:38:00Z')


def snapshot():
    return {'as_of':NOW.isoformat(),'cutover_at':'2026-10-26T00:00:00Z','statistics_extension':True,
            'accounts':{'wallet:master':100000,'wallet:zain':50000},'debts':[], 'entries':[],
            'operations':[{'id':'OPEN','kind':'opening','occurred_at':'2026-10-26T00:00:00Z',
                           'payload':{'accounts':[{'key':'wallet:master','balance':100000},{'key':'wallet:zain','balance':50000}]}}],
            'operational':{'catalog_products':[{'id':'gpt','name':'ChatGPT','aliases':['جات']}]},'source_status':{}}


def record(s,oid,kind='sale',at='2026-10-28T08:00:00Z',**kw):
    r={'id':oid,'kind':kind,'occurred_at':at,'recorded_at':at,'customer_id':1,'product':'ChatGPT',
       'cash_in':0,'cash_out':0,'sales':0,'expense':0,'refunded':0,'historical':False,**kw}
    p=r.pop('data',{});r['payload']={'id':oid,'kind':kind,'product':r.get('product'),**p}
    s['operations'].append(r);return r


def delta(s,oid,key,value):
    s['entries'].append({'operation_id':oid,'account_key':key,'delta':value})
    s['accounts'][key]=s['accounts'].get(key,0)+value


class StatisticsTests(unittest.TestCase):
    def test_sale_overpayment_then_credit_funded_purchase(self):
        s=snapshot();record(s,'A',cash_in=15000,sales=8000,data={'credit_added':7000})
        delta(s,'A','wallet:master',15000);delta(s,'A','credit:1',7000)
        record(s,'B',cash_in=1000,sales=8000,data={'credit_used':7000});delta(s,'B','wallet:master',1000);delta(s,'B','credit:1',-7000)
        r=build_report(s);t=r['totals']
        self.assertEqual((t['received'],t['sales'],t['credit_added'],t['credit_used']),(16000,16000,7000,7000))
        self.assertEqual((r['buyers'],t['sales_count'],r['positions']['credit_current']),(1,2,0))

    def test_cross_product_debt_sale_credit_allocations_sum_exactly(self):
        s=snapshot();s['debts']=[{'id':'D1','product':'Scripta','product_id':'script','remaining':0}]
        record(s,'A',cash_in=15000,sales=8000,data={'product_id':'gpt','credit_added':4000,'settlements':[{'debt_id':'D1','amount':3000}]})
        delta(s,'A','wallet:master',15000);delta(s,'A','credit:1',4000)
        r=build_report(s);groups={g['key']:g['metrics'] for g in r['products']}
        self.assertEqual(groups['id:gpt']['received'],8000);self.assertEqual(groups['id:script']['received'],3000)
        self.assertEqual(groups['customer_credit']['received'],4000)
        self.assertEqual(sum(g['received'] for g in groups.values()),15000)
        self.assertEqual(groups['id:script']['debt_paid'],3000)

    def test_unknown_cash_priority_is_explicit_not_invented(self):
        s=snapshot();s['debts']=[{'id':'D','product':'Scripta','remaining':0}]
        record(s,'A',cash_in=1000,sales=8000,data={'credit_used':10000,'settlements':[{'debt_id':'D','amount':3000}]})
        delta(s,'A','wallet:master',1000)
        r=build_report(s);self.assertEqual(r['totals']['mixed_cash'],1000)
        self.assertEqual(sum(g['metrics']['received'] for g in r['products']),1000)

    def test_debt_sale_and_repayment_do_not_repeat_sale(self):
        s=snapshot();s['debts']=[{'id':'D','remaining':0,'product':'ChatGPT'}]
        record(s,'A','debt_issue',sales=8000,data={'new_debt':8000})
        record(s,'B','debt_repayment',cash_in=8000,data={'settlements':[{'debt_id':'D','amount':8000}]})
        delta(s,'B','wallet:master',8000)
        t=build_report(s)['totals'];self.assertEqual((t['sales'],t['received'],t['sales_count']),(8000,8000,1))

    def test_capital_withdrawal_transfer_are_not_sales_or_expenses(self):
        s=snapshot();record(s,'A','capital',product=None,cash_in=20000);delta(s,'A','wallet:master',20000)
        record(s,'B','withdrawal',product=None,cash_out=3000);delta(s,'B','wallet:master',-3000)
        record(s,'C','transfer',product=None);delta(s,'C','wallet:master',-5000);delta(s,'C','wallet:zain',5000)
        r=build_report(s);self.assertEqual((r['totals']['sales'],r['totals']['expenses']),(0,0))
        self.assertEqual(sum(w['movement'] for w in r['positions']['wallets']),17000)

    def test_correction_is_not_classified_as_customer_refund(self):
        s=snapshot();record(s,'A',cash_in=8000,sales=8000);delta(s,'A','wallet:master',8000)
        record(s,'R','reversal',reference_id='A');delta(s,'R','wallet:master',-8000)
        t=build_report(s)['totals'];self.assertEqual((t['received'],t['sales'],t['refunds']),(0,0,0))
        self.assertEqual((t['receipt_corrections'],t['sales_count']),(8000,0))

    def test_reversal_in_later_period_preserves_earlier_report(self):
        s=snapshot();record(s,'A',at='2026-10-27T08:00:00Z',cash_in=8000,sales=8000);delta(s,'A','wallet:master',8000)
        record(s,'R','reversal',reference_id='A');delta(s,'R','wallet:master',-8000)
        self.assertEqual(build_report(s,{'period':'2026-10-27'})['totals']['sales'],8000)
        self.assertEqual(build_report(s)['totals']['sales'],-8000)

    def test_refund_and_its_reversal_net_to_zero(self):
        s=snapshot();record(s,'A',cash_in=8000,sales=8000);delta(s,'A','wallet:master',8000)
        record(s,'F','refund',cash_out=3000,refunded=3000,reference_id='A');delta(s,'F','wallet:master',-3000)
        record(s,'R','reversal',reference_id='F');delta(s,'R','wallet:master',3000)
        t=build_report(s)['totals'];self.assertEqual((t['refunds'],t['spent']),(0,0))

    def test_stable_id_survives_rename_and_legacy_alias(self):
        s=snapshot();record(s,'A',product='Old GPT',sales=8000,data={'product_id':'gpt'})
        record(s,'B',product='جات',sales=8000)
        self.assertEqual(len(build_report(s)['products']),1)

    def test_missing_timestamp_only_in_all_time_and_no_mutation(self):
        s=snapshot();record(s,'A','legacy_expense',at=None,cash_out=150150,expense=150150,historical=True)
        before=deepcopy(s)
        self.assertEqual(build_report(s)['totals']['expenses'],0)
        self.assertEqual(build_report(s,{'period':'all'})['totals']['expenses'],150150)
        self.assertEqual(build_report(s)['quality']['undated'],1);self.assertEqual(s,before)

    def test_unknown_sales_never_presented_as_complete_zero(self):
        s=snapshot();record(s,'A','legacy_receipt',cash_in=8000,sales=None,historical=True)
        r=build_report(s);self.assertEqual(r['quality']['unknown_sales'],1)
        self.assertIn('المعروف فقط',summary_text(r));self.assertIsNone(r['profit'])

    def test_historical_opening_is_unavailable_before_cutover(self):
        s=snapshot();r=build_report(s,{'period':'2026-10-25'})
        self.assertIsNone(r['positions']['debt_opening']);self.assertIsNone(r['positions']['wallets'][0]['opening'])

    def test_wallet_equation_and_internal_difference_are_visible(self):
        s=snapshot();record(s,'A','expense',cash_out=5000,expense=5000);delta(s,'A','wallet:master',-5000)
        r=build_report(s);w=r['positions']['wallets'][0]
        self.assertEqual(w['opening']+w['incoming']-w['outgoing']+w['transfers']+w['adjustments'],w['closing'])
        s['accounts']['wallet:master']+=777
        self.assertEqual(build_report(s)['quality']['balance_mismatches'],1)

    def test_date_boundaries_and_comparison_use_baghdad(self):
        s=snapshot();record(s,'A',at='2026-10-27T20:59:59Z',sales=1)
        record(s,'B',at='2026-10-27T21:00:00Z',sales=2)
        r=build_report(s);self.assertEqual(r['totals']['sales'],2)
        start,end=period_bounds('2026-10-27 إلى 2026-10-28',NOW)
        self.assertEqual(start.hour,0);self.assertEqual(end,NOW)
        with self.assertRaises(FinanceError):period_bounds('2026-10-29',NOW)
        self.assertIsNone(stamp('2026-10-27 22:00'))

    def test_filters_combine_without_changing_global_stocks(self):
        s=snapshot();record(s,'A',sales=8,data={'product_id':'gpt','plan_id':'m','source':'telegram'})
        record(s,'B',sales=10,customer_id=2,data={'source':'instagram'})
        r=build_report(s,{'period':'today','product':'id:gpt','plan':'m','source':'telegram','customer':'1'})
        self.assertEqual((r['totals']['sales'],len(r['events'])),(8,1));self.assertEqual(r['positions']['cash_current'],150000)

    def test_new_and_returning_buyers_are_unique(self):
        s=snapshot();record(s,'A',at='2026-10-27T08:00:00Z',sales=1)
        record(s,'B',sales=1);record(s,'C',sales=1);record(s,'D',customer_id=2,sales=1)
        r=build_report(s);self.assertEqual((r['buyers'],r['new_buyers'],r['returning']),(2,1,1))

    def test_missing_original_or_debt_blocks_incomplete_totals(self):
        s=snapshot();record(s,'R','reversal',reference_id='missing')
        with self.assertRaises(FinanceError):build_report(s)
        s=snapshot();record(s,'A',data={'settlements':[{'debt_id':'missing','amount':1}]})
        with self.assertRaises(FinanceError):build_report(s)

    def test_export_all_records_is_formula_safe_and_totals_match(self):
        s=snapshot()
        for i in range(1201):record(s,str(i),product='=IMPORTXML("bad")',cash_in=1,sales=1)
        r=build_report(s);file=export_report(r)
        with zipfile.ZipFile(file) as z:
            rows=list(csv.reader(io.StringIO(z.read('operations.csv').decode('utf-8-sig'))))
            self.assertEqual(len(rows),1202)
            self.assertEqual(sum(int(row[6]) for row in rows[1:]),r['totals']['received'])
        self.assertTrue(safe_cell(' =bad').startswith("'"));self.assertEqual(safe_cell(-8),-8)

    def test_operational_expiry_does_not_trust_active_status_alone(self):
        s=snapshot();s['operational']['subscription_reminders']=[
            {'id':1,'status':'active','started_at':'2026-10-01T00:00:00Z','expires_at':'2026-10-27T00:00:00Z'},
            {'id':2,'status':'active','started_at':'2026-10-01T00:00:00Z','expires_at':'2026-11-27T00:00:00Z','customer_chat_id':1},
            {'id':3,'status':'pending_delivery'}]
        r=operational_report(s,NOW)['subscriptions'];self.assertEqual((r['active'],r['expired'],r['pending_delivery']),(1,1,1))

    def test_missing_operational_data_not_zero(self):
        r=operational_report(snapshot(),NOW)
        self.assertIsNone(r['accounts']);self.assertIsNone(r['rewards'])

    def test_rewards_distinguish_people_from_messages(self):
        s=snapshot();s['operational'].update(continuity_bot_contacts=[{'customer_chat_id':1}],continuity_bot_invites=[],
            customer_campaigns=[{'id':'a'},{'id':'b'}],customer_campaign_recipients=[{'customer_chat_id':1,'delivery_status':'sent'}]*2)
        r=operational_report(s,NOW)['rewards'];self.assertEqual((r['messages'],r['campaign_customers']),(2,1))

    def test_debt_stocks_before_and_after_repayment_are_not_current_balance(self):
        s=snapshot();s['debts']=[{'id':'D','product':'ChatGPT','remaining':3000}]
        record(s,'P','debt_repayment',cash_in=5000,data={'settlements':[{'debt_id':'D','amount':5000}]})
        delta(s,'P','wallet:master',5000)
        p=build_report(s)['positions'];self.assertEqual((p['debt_opening'],p['debt_closing'],p['debt_current']),(8000,3000,3000))
        p=build_report(s,{'period':'2026-10-27'})['positions'];self.assertEqual((p['debt_closing'],p['debt_current']),(8000,3000))

    def test_cost_categories_sum_to_total_without_inventing_profit(self):
        s=snapshot()
        for i,category in enumerate(['cost','operating','fee','general',None]):
            record(s,str(i),'expense',cash_out=100,expense=100,data={'category':category})
            delta(s,str(i),'wallet:master',-100)
        t=build_report(s)['totals'];self.assertEqual(t['expenses'],500)
        self.assertEqual(sum(t[k] for k in ['product_costs','operating_expenses','fee_expenses','general_expenses','unknown_expenses']),500)

    def test_private_accounts_count_accounts_not_unique_customers(self):
        s=snapshot();s['operational'].update(chatgpt_shared_accounts=[],chatgpt_account_assignments=[],
            totp_accounts=[{'id':'a','label':'a@example.com','is_private':True},{'id':'b','label':'b@example.com','is_private':True}],
            customer_totp_account_links=[{'account_id':'a','customer_chat_id':1,'status':'active'},{'account_id':'b','customer_chat_id':1,'status':'active'}])
        r=operational_report(s,NOW)['accounts'];self.assertEqual((r['private'],r['links']),(2,2))
        for account in s['operational']['totp_accounts']:account['label']='خاص'
        self.assertEqual(operational_report(s,NOW)['accounts']['private'],2)

    def test_prior_day_comparison_is_same_elapsed_time(self):
        s=snapshot();record(s,'A',at='2026-10-27T13:37:59Z',sales=100)
        record(s,'B',at='2026-10-27T13:38:01Z',sales=200)
        self.assertEqual(build_report(s)['previous']['sales'],100)

    def test_unknown_plan_can_be_selected(self):
        s=snapshot();record(s,'A',sales=100)
        self.assertEqual(build_report(s,{'period':'today','plan':'unknown'})['totals']['sales'],100)


class StatisticsDataTests(unittest.TestCase):
    def test_only_missing_rpc_falls_back_to_financial_snapshot(self):
        from statistics_data import read_snapshot
        class Missing(Exception):code='PGRST202'
        db=Mock();db.owner_id=1;db.client.rpc.return_value.execute.side_effect=Missing();db.snapshot.return_value=snapshot()
        with patch('statistics_data.ledger',return_value=db):
            r=read_snapshot();self.assertFalse(r['statistics_extension'])
        db.client.rpc.return_value.execute.side_effect=RuntimeError('offline')
        with patch('statistics_data.ledger',return_value=db):
            with self.assertRaises(FinanceError):read_snapshot()


class StatisticsUITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import finance_ui as ui
        self.ui=ui;self.old_services=ui._services
        button=lambda text,callback_data:SimpleNamespace(text=text,callback_data=callback_data)
        ui._services=ui.FinanceServices(99,Mock(),Mock(),Mock(),Mock(),button,lambda rows:SimpleNamespace(inline_keyboard=rows),Mock(),Mock(),Mock(),Mock())
        self.context=SimpleNamespace(user_data={},bot=SimpleNamespace(send_document=AsyncMock()))
        self.message=SimpleNamespace(reply_text=AsyncMock())
        self.data=snapshot();record(self.data,'A',cash_in=8000,sales=8000,data={'product_id':'gpt','plan_id':'monthly'})
        delta(self.data,'A','wallet:master',8000)
        self.reader=patch('statistics_manager.read_snapshot',return_value=self.data).start()
        patch('finance_ui.suspend_finance_inputs').start()

    def tearDown(self):
        patch.stopall();self.ui._services=self.old_services

    async def click(self,action,owner=99):
        from statistics_manager import handle_statistics_callback
        q=SimpleNamespace(from_user=SimpleNamespace(id=owner),data='stats2:'+action,message=self.message,answer=AsyncMock())
        await handle_statistics_callback(SimpleNamespace(callback_query=q),self.context)
        return q

    async def test_all_sections_and_drilldown_share_one_read_only_snapshot(self):
        from statistics_manager import open_statistics
        from financial_ledger import source_id
        before=deepcopy(self.data);await open_statistics(self.message,self.context)
        for view in ['filters','metrics','products:0','plans:0','sources:0','wallets:0','debts','compare','costs','subscriptions','rewards','attention:0','detail:received:0','event:'+source_id('E','A')]:
            await self.click(view)
            text=self.message.reply_text.call_args.args[0]
            self.assertNotIn('تعذر بناء',text,view);self.assertLessEqual(len(text),4096)
        self.reader.assert_called_once();self.assertEqual(self.data,before)

    async def test_combined_filters_back_and_export_keep_same_snapshot(self):
        from statistics_manager import open_statistics,statistics_input
        await open_statistics(self.message,self.context)
        await self.click('period:yesterday');await self.click('home')
        self.assertEqual(self.context.user_data['statistics_filters']['period'],'yesterday')
        await self.click('custom')
        update=SimpleNamespace(effective_user=SimpleNamespace(id=99),message=SimpleNamespace(text='2026-10-28',reply_text=AsyncMock()))
        await statistics_input(update,self.context);await self.click('export')
        with zipfile.ZipFile(self.context.bot.send_document.call_args.kwargs['document']) as z:
            rows=list(csv.reader(io.StringIO(z.read('operations.csv').decode('utf-8-sig'))))
        self.assertEqual(len(rows),2);self.reader.assert_called_once()

    async def test_back_clears_pending_input_and_unauthorized_cannot_read(self):
        from statistics_manager import open_statistics
        await self.click('open',owner=1);self.reader.assert_not_called()
        await open_statistics(self.message,self.context);await self.click('custom')
        self.assertEqual(self.context.user_data['statistics_input'],'custom')
        await self.click('filters');self.assertNotIn('statistics_input',self.context.user_data)

    async def test_failed_refresh_does_not_replace_good_snapshot_or_show_zeros(self):
        from statistics_manager import open_statistics
        await open_statistics(self.message,self.context)
        previous=self.context.user_data['statistics_session']
        self.reader.side_effect=FinanceError('تعذرت القراءة')
        await self.click('refresh')
        self.assertIs(self.context.user_data['statistics_session'],previous)
        self.assertIn('تعذرت القراءة',self.message.reply_text.call_args.args[0])

    async def test_invalid_period_preserves_last_filters_for_retry(self):
        from statistics_manager import open_statistics,statistics_input
        await open_statistics(self.message,self.context);await self.click('custom')
        update=SimpleNamespace(effective_user=SimpleNamespace(id=99),message=SimpleNamespace(text='invalid date',reply_text=AsyncMock()))
        await statistics_input(update,self.context)
        self.assertEqual(self.context.user_data['statistics_filters']['period'],'today')
        self.assertEqual(self.context.user_data['statistics_input'],'custom')


@unittest.skipUnless(os.environ.get('FINANCE_TEST_DSN'),'Local isolated PostgreSQL required')
class StatisticsSQLTests(unittest.TestCase):
    def sql(self,text):
        r=subprocess.run(['/opt/homebrew/bin/psql',os.environ['FINANCE_TEST_DSN'],'-XAt','-v','ON_ERROR_STOP=1','-c',text],capture_output=True,text=True)
        if r.returncode:self.fail(r.stderr)
        return r.stdout.strip()

    def test_statistics_rpc_preserves_money_and_returns_every_operation(self):
        owner=uuid.uuid4().int%10**12
        self.sql(f"select finance_setup({owner},'begin');select finance_setup({owner},'stage','{{\"accounts\":[],\"debts\":[],\"operations\":[]}}');select finance_setup({owner},'activate')")
        self.sql(f"insert into finance_operations(owner_id,id,payload,kind,occurred_at,cash_in,sales,historical) select {owner},'H-'||i,'{{}}','legacy_receipt',now(),1,null,true from generate_series(1,1201) i")
        before=self.sql(f'select finance_read({owner})')
        r=json.loads(self.sql(f'select finance_statistics_read({owner})'))
        self.assertEqual(len(r['operations']),1202);self.assertIn('as_of',r)
        self.assertEqual(self.sql(f'select finance_read({owner})'),before)
        self.assertEqual(len(r['source_status']),10)

    def test_rpc_is_not_public_and_has_no_secret_columns(self):
        self.assertEqual(self.sql("select has_function_privilege('anon','finance_statistics_read(bigint)','execute')"),'f')
        self.assertEqual(self.sql("select has_function_privilege('service_role','finance_statistics_read(bigint)','execute')"),'t')
        definition=self.sql("select pg_get_functiondef('finance_statistics_read(bigint)'::regprocedure)")
        self.assertNotIn("'totp_secret'",definition);self.assertNotIn("'password'",definition)

    def test_real_sources_use_allowlisted_columns_and_campaign_owner(self):
        owner=uuid.uuid4().int%10**12
        self.sql(f"select finance_setup({owner},'begin');select finance_setup({owner},'stage','{{\"accounts\":[],\"debts\":[],\"operations\":[]}}');select finance_setup({owner},'activate')")
        output=self.sql(f"""begin; alter role service_role bypassrls;
          create table catalog_products(id text,name text,aliases jsonb,is_active boolean);
          insert into catalog_products values('p','Product','[]',true);
          create table totp_accounts(id text,label text,link_code text,secret text);
          insert into totp_accounts values('1','test','private_token','HIDDEN_SECRET');
          create table customer_campaigns(id text,owner_user_id bigint,status text,created_at timestamptz);
          insert into customer_campaigns values('mine',{owner},'sent',now()),('other',{owner+1},'sent',now());
          create table customer_campaign_recipients(id bigint,campaign_id text,customer_chat_id bigint,delivery_status text,delivered_at timestamptz);
          insert into customer_campaign_recipients values(1,'mine',1,'sent',now()),(2,'other',2,'sent',now());
          grant select on catalog_products,totp_accounts,customer_campaigns,customer_campaign_recipients to service_role;
          set local role service_role;
          select finance_statistics_read({owner}); rollback;""")
        data=json.loads(next(line for line in output.splitlines() if line.startswith('{')))
        sources=data['operational'];self.assertEqual(sources['catalog_products'][0]['id'],'p')
        self.assertTrue(sources['totp_accounts'][0]['is_private']);self.assertNotIn('HIDDEN_SECRET',json.dumps(data))
        self.assertNotIn('private_token',json.dumps(data));self.assertEqual(len(sources['customer_campaign_recipients']),1)
        self.assertEqual(sources['customer_campaigns'][0]['id'],'mine')
