import json,os,subprocess,unittest,uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock,Mock,patch

import account_delivery as ad
from financial_ledger import FinanceError


class TemplateTests(unittest.TestCase):
    def test_postgres_fractional_timestamp_precision_is_supported(self):
        for fraction in ('1','12','123','1234','12345','123456'):
            value=ad.parse_time('2026-10-28T23:44:03.'+fraction+'+03:00')
            self.assertEqual(value.microsecond,int(fraction.ljust(6,'0')))
            self.assertIn('بغداد',ad.display_date('2026-10-28T23:44:03.'+fraction+'+03:00'))
    def test_compact_ids_keep_telegram_callback_under_64_bytes(self):
        value=str(uuid.uuid4());self.assertEqual(ad.expanded(ad.compact(value)),value)
        from finance_ui import configure_finance_ui
        import finance_ui
        old=finance_ui._services
        finance_ui._services=SimpleNamespace(button=lambda label,callback_data:callback_data,markup=lambda rows:rows)
        try:
            actions=['target_'+ad.compact(value),'plan_'+ad.compact(value),'fund_'+ad.funding_token('PAY-'+uuid.uuid4().hex)]
            rows=ad.markups({'id':value,'revision':999},[[('test',a)] for a in actions])
            self.assertTrue(all(len(row[0].encode())<=64 for row in rows))
        finally:finance_ui._services=old
    def test_exact_template_links_password_quote_and_html_escaping(self):
        text=ad.delivery_text('ali+tag@example.com','P<a&ss')
        self.assertIn('<blockquote>P&lt;a&amp;ss</blockquote>',text)
        self.assertIn('href="https://t.me/+IcHCjNi8_ilkZjdi">طريقة التسجيل',text)
        self.assertIn('href="https://t.me/+M8XsrznhCNJkNTA6">لو سمحت انضم للقناة',text)
        self.assertNotIn('secret',text)

    def test_totp_is_base32_not_six_digit_code(self):
        valid=dict(email='a@example.com',password='Password',secret='JBSWY3DPEHPK3PXP',capacity=3,code='test_code')
        self.assertEqual(ad.validate_account(valid)['capacity'],3)
        for change in ({'secret':'123456'},{'secret':'NOT!SECRET'},{'capacity':0},{'email':'bad'},{'code':'x'}):
            with self.assertRaises(FinanceError):ad.validate_account({**valid,**change})

    def test_rpc_errors_do_not_echo_credentials(self):
        old=ad._services;client=Mock();client.rpc.return_value.execute.side_effect=RuntimeError('secret_value password')
        ad._services=SimpleNamespace(client=client,owner_id=1)
        try:
            with self.assertRaises(FinanceError) as caught:ad.rpc('register',password='secret_value')
            self.assertNotIn('secret_value',str(caught.exception))
        finally:ad._services=old


@unittest.skipUnless(os.environ.get('ACCOUNT_DELIVERY_TEST_DSN'),'Local delivery database required')
class DeliverySQLTests(unittest.TestCase):
    def sql(self,sql,ok=True):
        r=subprocess.run(['/opt/homebrew/bin/psql',os.environ['ACCOUNT_DELIVERY_TEST_DSN'],'-XAt','-v','ON_ERROR_STOP=1','-c',sql],capture_output=True,text=True)
        if ok and r.returncode:self.fail(r.stderr)
        if not ok:self.assertNotEqual(r.returncode,0)
        return r.stdout.strip()
    def quoted(self,value):return "'"+json.dumps(value).replace("'","''")+"'::jsonb"
    def call(self,action,ok=True,**p):
        out=self.sql(f"select delivery_manage({self.owner},'{action}',{self.quoted(p)})",ok)
        return json.loads(out) if ok else out
    def setUp(self):
        self.owner=uuid.uuid4().int%10**12;self.customer=self.owner+100
        self.sql(f"select finance_setup({self.owner},'begin');select finance_setup({self.owner},'stage','{{\"accounts\":[],\"debts\":[],\"operations\":[]}}');select finance_setup({self.owner},'activate')")
        self.product=str(uuid.uuid4());self.plan=str(uuid.uuid4())
        self.sql(f"insert into catalog_products(id,name) values('{self.product}','ChatGPT');insert into catalog_plans(id,product_id,name,price,duration) values('{self.plan}','{self.product}','Month',8000,'30 days')")
        self.code='dl_'+uuid.uuid4().hex[:12]
        self.account=self.call('register',email=self.code+'@example.com',password='Password',secret='JBSWY3DPEHPK3PXP',capacity=3,code=self.code)['id']
    def sale(self,amount=8000,customer=None):
        oid='PAY-'+uuid.uuid4().hex
        p=dict(id=oid,kind='sale',occurred_at='2026-10-28T00:00:00Z',customer_id=customer or self.customer,product='ChatGPT',product_id=self.product,sales=amount,payments=[{'wallet':'master','amount':amount}])
        self.sql(f"select finance_post({self.owner},{self.quoted(p)})");return oid
    def start(self,customer=None,code=None,message_id=None):
        return self.call('start',id=str(uuid.uuid4()),customer_id=customer or self.customer,customer_name='Customer',code=code or self.code,business_id='test',message_id=message_id or uuid.uuid4().int%10**10)
    def select(self,data,**state):
        return self.call('edit',id=data['request']['id'],revision=data['request']['revision'],state=state)
    def prepare(self,mode='new',target=None,customer=None,funding=None,code=None,seats=1,days=30):
        d=self.start(customer=customer,code=code)
        state=dict(mode=mode,days=days,seats=seats,product='ChatGPT',product_id=self.product,plan_id=self.plan,plan_name='Month',allocated=8000)
        if mode in ('new','renew'):state['funding_id']=funding or self.sale(customer=customer)
        if target:state.update(target=target['id'],target_revision=target['revision'])
        return self.select(d,**state)
    def finish(self,d):
        rid=d['request']['id'];self.call('reserve',id=rid);self.call('claim',id=rid);self.call('sent',id=rid,message_id=123)
        return self.call('finish',id=rid)
    def test_register_retry_and_secret_not_in_normal_cards(self):
        data=self.call('list');self.assertNotIn('Password',json.dumps(data));self.assertNotIn('JBSWY',json.dumps(data))
        saved=self.call('register',email=self.code+'@example.com',password='Password',secret='JBSWY3DPEHPK3PXP',capacity=3,code=self.code)
        self.assertTrue(saved['duplicate']);self.assertEqual(saved['id'],self.account)
    def test_duplicate_command_uses_same_persisted_request(self):
        a=self.start(message_id=99);b=self.start(message_id=99)
        self.assertEqual(a['request']['id'],b['request']['id'])
    def test_new_delivery_has_one_contract_and_idempotent_finish(self):
        d=self.prepare();r=self.finish(d);again=self.call('finish',id=d['request']['id'])
        self.assertTrue(again['duplicate']);self.assertEqual(len(r['contracts']),1)
        self.assertEqual(r['occupancy']['people'],1);self.assertEqual(r['occupancy']['seats'],1)
        self.assertEqual(self.sql(f"select count(*) from delivery_periods where owner_id={self.owner}"),'1')
    def test_same_customer_two_subscriptions_count_one_person_two_seats(self):
        self.finish(self.prepare());r=self.finish(self.prepare())
        self.assertEqual(len(r['contracts']),2);self.assertEqual((r['occupancy']['people'],r['occupancy']['seats']),(1,2))
    def test_capacity_reservations_serialize_concurrent_owners_actions(self):
        requests=[self.prepare(customer=self.customer+i,seats=2) for i in range(2)]
        def reserve(d):
            result=subprocess.run(['/opt/homebrew/bin/psql',os.environ['ACCOUNT_DELIVERY_TEST_DSN'],'-XAt','-v','ON_ERROR_STOP=1','-c',f"select delivery_manage({self.owner},'reserve',{self.quoted({'id':d['request']['id']})})"],capture_output=True,text=True)
            return result.returncode==0
        with ThreadPoolExecutor(2) as pool:results=list(pool.map(reserve,requests))
        self.assertEqual(sum(results),1)
    def test_repeated_claim_cannot_send_twice(self):
        d=self.prepare();rid=d['request']['id'];self.call('reserve',id=rid);self.call('claim',id=rid)
        self.call('claim',id=rid,ok=False)
        self.call('cancel',id=rid,ok=False)
        self.assertEqual(self.call('get',id=rid)['request']['status'],'sending')
    def test_reserved_seat_released_on_cancel_and_no_contract_created(self):
        d=self.prepare(seats=3);rid=d['request']['id'];self.call('reserve',id=rid)
        self.call('cancel',id=rid)
        result=self.call('reserve',id=self.prepare(customer=self.customer+1,seats=3)['request']['id'])
        self.assertEqual(result['request']['status'],'reserved')
    def test_payment_allocation_cannot_be_reused(self):
        funding=self.sale();self.finish(self.prepare(funding=funding))
        d=self.prepare(funding=funding);self.call('reserve',id=d['request']['id'],ok=False)
    def test_split_payment_supports_two_explicit_subscriptions(self):
        funding=self.sale(16000);self.finish(self.prepare(funding=funding));r=self.finish(self.prepare(funding=funding))
        self.assertEqual(len(r['contracts']),2)
        self.call('reserve',id=self.prepare(funding=funding)['request']['id'],ok=False)
    def test_renewal_extends_selected_contract_without_extra_seat(self):
        first=self.finish(self.prepare());c=first['contracts'][0]
        r=self.finish(self.prepare(mode='renew',target=c))
        self.assertEqual(len(r['contracts']),1);self.assertEqual(r['occupancy']['seats'],1)
        self.assertGreater(r['contracts'][0]['ends_at'],c['ends_at'])
    def test_compensation_changes_exact_contract_not_siblings(self):
        self.finish(self.prepare());r=self.finish(self.prepare());target=r['contracts'][0];sibling=r['contracts'][1]
        result=self.finish(self.prepare(mode='comp',target=target,days=7))
        unchanged=next(c for c in result['contracts'] if c['id']==sibling['id'])
        self.assertEqual(unchanged['ends_at'],sibling['ends_at'])
    def test_debt_is_created_only_on_finish_without_cash(self):
        d=self.prepare(funding='debt');self.call('reserve',id=d['request']['id'])
        self.assertEqual(self.sql(f'select count(*) from finance_debts where owner_id={self.owner}'),'0')
        self.call('claim',id=d['request']['id']);self.call('sent',id=d['request']['id']);self.call('finish',id=d['request']['id'])
        self.assertEqual(self.sql(f'select sum(remaining) from finance_debts where owner_id={self.owner}'),'8000')
        self.assertEqual(self.sql(f'select count(*) from finance_entries where owner_id={self.owner}'),'0')
    def test_owner_cannot_read_or_modify_other_owner_request(self):
        d=self.start();old=self.owner;self.owner+=1
        self.call('get',id=d['request']['id'],ok=False);self.owner=old
    def test_changed_credentials_block_sending_and_secrets_require_claim(self):
        d=self.prepare();rid=d['request']['id'];self.call('reserve',id=rid)
        self.assertEqual(self.sql(f"select delivery_credentials({self.owner},'{rid}')"),'')
        self.sql(f"update chatgpt_shared_accounts set password='OtherPassword' where id=(select native_id from delivery_accounts where id='{self.account}')")
        self.call('claim',id=rid,ok=False)
    def test_uncertain_owner_confirmation_finishes_without_reclaim(self):
        d=self.prepare();rid=d['request']['id'];self.call('reserve',id=rid);self.call('claim',id=rid)
        self.call('resolve_sent',id=rid);r=self.call('finish',id=rid)
        self.assertEqual(r['request']['status'],'complete')
    def test_undo_changes_subscription_not_money_and_is_not_repeatable(self):
        d=self.prepare();r=self.finish(d)
        before=self.sql(f'select finance_read({self.owner})')
        result=self.call('undo',id=d['request']['id'])
        self.assertTrue(result['request']['result']['undone']);self.assertEqual(result['occupancy']['unreviewed'],1)
        self.call('release_access',id=self.account,customer_id=self.customer)
        self.assertEqual(self.call('get',id=d['request']['id'])['occupancy']['seats'],0)
        self.assertEqual(self.sql(f'select finance_read({self.owner})'),before)
        self.call('undo',id=d['request']['id'],ok=False)
    def test_code_permission_exact_account_expiry_and_disabled(self):
        r=self.finish(self.prepare());legacy=self.sql(f"select legacy_id from delivery_accounts where id='{self.account}'")
        p=json.loads(self.sql(f"select delivery_code_permission({self.customer},'{legacy}')"));self.assertTrue(p['allowed'])
        self.sql(f"update delivery_contracts set ends_at=now()-interval '1 day',starts_at=now()-interval '30 days' where owner_id={self.owner}")
        p=json.loads(self.sql(f"select delivery_code_permission({self.customer},'{legacy}')"));self.assertFalse(p['allowed'])

    def test_replace_one_contract_preserves_other_old_account_entitlement(self):
        first=self.finish(self.prepare());both=self.finish(self.prepare());target=both['contracts'][0]
        code2='dl_'+uuid.uuid4().hex[:12]
        second=self.call('register',email=code2+'@example.com',password='Password',secret='JBSWY3DPEHPK3PXP',capacity=3,code=code2)
        result=self.finish(self.prepare(mode='replace',target=target,code=code2))
        old_legacy=self.sql(f"select legacy_id from delivery_accounts where id='{self.account}'")
        permission=json.loads(self.sql(f"select delivery_code_permission({self.customer},'{old_legacy}')"))
        self.assertTrue(permission['allowed']);self.assertEqual(len(result['contracts']),2)
        self.assertEqual(next(c for c in result['contracts'] if c['id']==target['id'])['ends_at'],target['ends_at'])

    def test_guard_blocks_financial_reversal_until_delivery_is_reversed(self):
        funding=self.sale();d=self.prepare(funding=funding);self.finish(d)
        reverse={'id':'REV-'+uuid.uuid4().hex,'kind':'reversal','reference_id':funding,'occurred_at':'2026-10-28T00:00:00Z'}
        self.sql(f'select finance_post({self.owner},{self.quoted(reverse)})',False)
        self.call('undo',id=d['request']['id'])
        self.sql(f'select finance_post({self.owner},{self.quoted(reverse)})')

    def test_payer_is_distinct_from_beneficiary(self):
        payer=self.customer+9;funding=self.sale(customer=payer)
        data=self.start();data=self.select(data,mode='new',payer_id=payer,funding_id=funding,days=30,seats=1,allocated=8000,product='ChatGPT',product_id=self.product,plan_id=self.plan)
        result=self.finish(data);c=result['contracts'][0]
        self.assertEqual((c['customer_id'],c['payer_id']),(self.customer,payer))

    def test_reviewed_legacy_reminder_preserves_duration_and_does_not_post_money(self):
        legacy=self.sql(f"select legacy_id from delivery_accounts where id='{self.account}'")
        self.sql(f"insert into totp_links(chat_id,account_id) values({self.customer},{legacy});insert into delivery_legacy_holds(account_id,customer_id) values('{self.account}',{self.customer})")
        rem=self.sql(f"insert into subscription_reminders(customer_chat_id,customer_name,subscription_type,product_name,started_at,expires_at,status) values({self.customer},'Old','shared','ChatGPT',now()-interval '23 days',now()+interval '7 days','active') returning id")
        before=self.sql(f'select finance_read({self.owner})');d=self.start()
        self.assertEqual(d['occupancy']['unreviewed'],1)
        r=self.call('import_reminder',id=d['request']['id'],reminder_id=int(rem.splitlines()[0]))
        self.assertEqual(r['occupancy']['seats'],1);self.assertEqual(r['occupancy']['unreviewed'],0)
        self.assertEqual(self.sql(f'select finance_read({self.owner})'),before)

    def test_failed_reservation_has_no_debt_or_subscription_side_effect(self):
        occupied=self.prepare(customer=self.customer+1,seats=3);self.call('reserve',id=occupied['request']['id'])
        d=self.prepare(funding='debt');self.call('reserve',id=d['request']['id'],ok=False)
        self.assertEqual(self.sql(f'select count(*) from finance_debts where owner_id={self.owner}'),'0')
        self.assertEqual(self.sql(f'select count(*) from delivery_contracts where owner_id={self.owner}'),'0')

    def test_funding_claim_is_shared_by_followup_projection_and_legacy_link(self):
        funding=self.sale();d=self.prepare(funding=funding);self.call('reserve',id=d['request']['id'])
        self.assertEqual(self.sql(f"select delivery_funding_managed({self.owner},'{funding}')"),'t')

    def test_credentials_update_is_atomic_preserves_contracts_and_blocks_stale_send(self):
        result=self.finish(self.prepare());before=deepcopy(result['contracts'])
        reserved=self.prepare();self.call('reserve',id=reserved['request']['id'])
        account=self.call('list')['accounts'][0]
        self.call('credentials',id=self.account,revision=account['revision'],password='NewPassword',secret='KRSXG5DSNFXGOIDB')
        self.call('claim',id=reserved['request']['id'],ok=False)
        self.assertEqual(self.call('get',id=reserved['request']['id'])['contracts'],before)
        legacy=self.sql(f"select secret from totp_accounts where id::text=(select legacy_id from delivery_accounts where id='{self.account}')")
        self.assertEqual(legacy,'KRSXG5DSNFXGOIDB')
        self.assertNotIn('NewPassword',self.sql(f'select details from delivery_events where owner_id={self.owner}'))

    def test_undo_renewal_restores_original_end_and_keeps_original_subscription(self):
        first=self.finish(self.prepare());c=first['contracts'][0]
        renewal=self.prepare(mode='renew',target=c);self.finish(renewal)
        r=self.call('undo',id=renewal['request']['id'])
        self.assertEqual(r['contracts'][0]['ends_at'],c['ends_at']);self.assertEqual(r['occupancy']['seats'],1)

    def test_old_unmanaged_customer_keeps_legacy_permission_until_review(self):
        legacy=self.sql(f"select legacy_id from delivery_accounts where id='{self.account}'")
        self.sql(f"insert into totp_links(chat_id,account_id) values({self.customer},{legacy})")
        r=json.loads(self.sql(f"select delivery_code_permission({self.customer},'{legacy}')"))
        self.assertFalse(r['managed'])

    def test_capacity_change_cannot_evict_seated_or_reserved_customers(self):
        self.finish(self.prepare(seats=2));a=self.call('list')['accounts'][0]
        self.call('account',id=self.account,revision=a['revision'],capacity=1,ok=False)
        self.assertEqual(self.call('list')['accounts'][0]['capacity'],3)

    def test_send_and_duplicate_callback_through_real_sql_and_mock_telegram(self):
        import asyncio,finance_ui
        async def exercise():
            old_ad=ad._services;old_ui=finance_ui._services
            def invoke(action,**p):return self.call(action,**p)
            def credentials(name,p):
                return SimpleNamespace(execute=lambda:SimpleNamespace(data=json.loads(self.sql(f"select delivery_credentials({self.owner},'{p['p_id']}')"))))
            client=Mock();client.rpc.side_effect=credentials;client.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value.data=[]
            ad._services=ad.DeliveryServices(client,self.owner,Mock(),Mock(),Mock(),Mock(),AsyncMock(return_value=True),AsyncMock(),Mock())
            finance_ui._services=SimpleNamespace(button=lambda label,callback_data:SimpleNamespace(text=label,callback_data=callback_data),markup=lambda rows:SimpleNamespace(inline_keyboard=rows))
            context=SimpleNamespace(user_data={},bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=9))))
            msg=SimpleNamespace(reply_text=AsyncMock())
            try:
                d=self.prepare();reserved=self.call('reserve',id=d['request']['id'])
                with patch('account_delivery.rpc',side_effect=invoke):
                    await ad.show_card(msg,context,reserved)
                    q=SimpleNamespace(from_user=SimpleNamespace(id=self.owner),data=f"dl:{ad.compact(d['request']['id'])}:{reserved['request']['revision']}:send",message=msg,answer=AsyncMock())
                    await ad.handle_delivery_callback(SimpleNamespace(callback_query=q),context)
                    await ad.handle_delivery_callback(SimpleNamespace(callback_query=q),context)
                self.assertEqual(context.bot.send_message.call_count,1)
                self.assertIn('طريقة التسجيل',context.bot.send_message.call_args.kwargs['text'])
                self.assertEqual(self.call('get',id=d['request']['id'])['request']['status'],'complete')
            finally:ad._services=old_ad;finance_ui._services=old_ui
        asyncio.run(exercise())

    def test_anon_cannot_access_rpc_or_credentials(self):
        for function in ('delivery_manage(bigint,text,jsonb)','delivery_credentials(bigint,uuid)','delivery_code_permission(bigint,text)'):
            self.assertEqual(self.sql(f"select has_function_privilege('anon','{function}','execute')"),'f')

    def test_legacy_adoption_consumes_already_activated_sale_without_new_money(self):
        funding=self.sale();legacy=self.sql(f"select legacy_id from delivery_accounts where id='{self.account}'")
        self.sql(f"insert into totp_links(chat_id,account_id) values({self.customer},{legacy})")
        rem=self.sql(f"insert into subscription_reminders(customer_chat_id,customer_name,subscription_type,product_name,started_at,expires_at,status,financial_operation_id) values({self.customer},'Old','shared','ChatGPT',now()-interval '23 days',now()+interval '7 days','active','{funding}') returning id").splitlines()[0]
        d=self.start();self.call('import_reminder',id=d['request']['id'],reminder_id=int(rem))
        d=self.prepare(funding=funding);self.call('reserve',id=d['request']['id'],ok=False)

    def test_expired_hold_can_be_reviewed_again_without_duplicate_money(self):
        d=self.prepare();rid=d['request']['id'];self.call('reserve',id=rid)
        self.sql(f"update delivery_requests set hold_until=now()-interval '1 minute' where id='{rid}'")
        self.call('claim',id=rid,ok=False);result=self.call('reserve',id=rid)
        self.assertEqual(result['request']['status'],'reserved')
        self.assertEqual(self.sql(f'select count(*) from delivery_periods where owner_id={self.owner}'),'0')

    def test_old_assignment_path_cannot_overbook_registered_account(self):
        native=self.sql(f"select native_id from delivery_accounts where id='{self.account}'")
        self.sql(f"insert into chatgpt_account_assignments(account_id,customer_chat_id,status) values('{native}',{self.customer},'active')",False)

    def test_delivery_resource_returns_capacity_and_current_holds(self):
        r=self.prepare(seats=2);self.call('reserve',id=r['request']['id'])
        legacy=self.sql(f"select legacy_id from delivery_accounts where id='{self.account}'")
        data=json.loads(self.sql(f"select delivery_account_customers('{legacy}')"))
        self.assertEqual(data['resource']['occupancy']['held'],2)
        self.assertEqual(data['resource']['capacity'],3)


class DeliveryUITests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old=ad._services
        ad._services=SimpleNamespace(owner_id=99,client=Mock())
        self.context=SimpleNamespace(user_data={},bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=7)),delete_business_messages=AsyncMock()))
        self.message=SimpleNamespace(reply_text=AsyncMock())
    def tearDown(self):ad._services=self.old
    async def test_delete_command_precedes_lookup_and_duplicate_command_is_persisted(self):
        order=[];self.context.bot.delete_business_messages.side_effect=lambda **kw:order.append('delete')
        bm=SimpleNamespace(business_connection_id='b',message_id=1,chat=SimpleNamespace(full_name='User',first_name='User'))
        def lookup(*args,**kw):order.append('read');return {'managed':False}
        with patch('account_delivery.rpc',side_effect=lookup):self.assertFalse(await ad.start_link(self.context,bm,1,'/link code'))
        self.assertEqual(order,['delete','read'])
    async def test_send_timeout_does_not_finish_or_resend(self):
        r={'id':str(uuid.uuid4()),'state':{'mode':'new'},'customer_id':1,'business_id':'b'};data={'request':r}
        calls=[]
        def call(action,**kw):calls.append(action);return data
        ad._services.client.rpc.return_value.execute.return_value.data={'email':'a@example.com','password':'Password'}
        self.context.bot.send_message.side_effect=TimeoutError()
        with patch('account_delivery.rpc',side_effect=call),patch('account_delivery.show_card',new=AsyncMock()):await ad.send_delivery(self.context,self.message,data)
        self.assertEqual(calls,['claim','get']);self.assertEqual(self.context.bot.send_message.call_count,1)
