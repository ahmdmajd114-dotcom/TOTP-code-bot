import ast
import __future__
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import threading
import unittest

import account_delivery as delivery
from catalog_read_cache import catalog_read, customer_catalog_reads
import performance_diagnostics as diagnostics
from runtime_scheduler import run_io,background_job


class CatalogReadTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_customer_reply_reuses_reads_but_returns_independent_copies(self):
        load = Mock(return_value=[{'price':8000}])
        cached = catalog_read(load)
        with customer_catalog_reads():
            first = await run_io(cached,'p1')
            first[0]['price'] = 1
            second = await run_io(cached,'p1')
        self.assertEqual(second,[{'price':8000}])
        self.assertEqual(load.call_count,1)
        with customer_catalog_reads():
            await run_io(cached,'p1')
        self.assertEqual(load.call_count,2)

    async def test_owner_edits_always_read_current_price(self):
        load = Mock(side_effect=[8000,9000]);cached=catalog_read(load)
        with customer_catalog_reads(enabled=False):
            self.assertEqual(await run_io(cached),8000)
            self.assertEqual(await run_io(cached),9000)
        self.assertEqual(load.call_count,2)

    async def test_failure_is_never_cached_as_an_empty_catalog(self):
        load=Mock(side_effect=[RuntimeError('offline'),[{'price':8000}]])
        cached=catalog_read(load)
        with customer_catalog_reads():
            with self.assertRaises(RuntimeError):await run_io(cached)
            self.assertEqual(await run_io(cached),[{'price':8000}])

    async def test_two_customer_updates_never_share_cached_reads(self):
        load=Mock(side_effect=['customer A','customer B']);cached=catalog_read(load)
        async def reply():
            with customer_catalog_reads():
                a=await run_io(cached)
                await asyncio.sleep(0)
                self.assertEqual(await run_io(cached),a)
                return a
        self.assertEqual(set(await asyncio.gather(reply(),reply())),{'customer A','customer B'})
        self.assertEqual(load.call_count,2)

    async def test_real_catalog_functions_reduce_fifteen_requests_to_three(self):
        tree=ast.parse(Path(__file__).resolve().parents[1].joinpath('bot.py').read_text())
        names={'get_catalog_products','get_catalog_product','get_catalog_plans'}
        functions=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
        client=Mock();query=client.table.return_value;query.select.return_value=query
        query.order.return_value=query;query.eq.return_value=query
        query.execute.return_value=SimpleNamespace(data=[{'id':'p1','price':8000}])
        ns={'catalog_read':catalog_read,'supabase':client,'logger':Mock()}
        exec(compile(ast.Module(body=functions,type_ignores=[]),'catalog-read-tests','exec',flags=__future__.annotations.compiler_flag),ns)
        with customer_catalog_reads():
            for _ in range(5):
                await run_io(ns['get_catalog_products'])
                await run_io(ns['get_catalog_product'],'p1')
                await run_io(ns['get_catalog_plans'],'p1')
        self.assertEqual(query.execute.call_count,3)


class DeliveryPerformanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_plan_read_does_not_hold_telegram_loop(self):
        loop=asyncio.get_running_loop();started=asyncio.Event();release=threading.Event()
        def read_plans():
            loop.call_soon_threadsafe(started.set);release.wait(3);return []
        data={'request':{'id':'r','customer_name':'customer','customer_id':1,'state':{'step':'plan'},'status':'draft'},
              'account':{'email':'test@example.com','capacity':2},'occupancy':{'seats':0,'held':0,'people':0,'unreviewed':0}}
        with patch.object(delivery,'catalog_options',side_effect=read_plans),patch.object(delivery,'markups',return_value=None):
            message=SimpleNamespace(reply_text=AsyncMock());context=SimpleNamespace(user_data={})
            task=asyncio.create_task(delivery.show_card(message,context,data))
            try:
                await asyncio.wait_for(started.wait(),1)
                self.assertFalse(task.done())
                tick=asyncio.Event();loop.call_soon(tick.set)
                await asyncio.wait_for(tick.wait(),1)
            finally:
                release.set();await task
        message.reply_text.assert_awaited_once()


class CustomerIntakeTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_does_not_wait_for_slow_google_connection(self):
        tree=ast.parse(Path(__file__).resolve().parents[1].joinpath('bot.py').read_text())
        functions=[n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name in {'warm_store_connections','on_bot_initialized'}]
        loop=asyncio.get_running_loop();started=asyncio.Event();release=threading.Event()
        def google():loop.call_soon_threadsafe(started.set);release.wait(3);return object()
        ns={'run_io':run_io,'background_job':background_job,'get_google_sheet':google,
            '_payment_sheet_resolution_note':None,'OWNER_USER_ID':99,
            'backfill_linked_chatgpt_schedules':AsyncMock(),'logger':Mock()}
        exec(compile(ast.Module(body=functions,type_ignores=[]),'store-startup','exec',flags=__future__.annotations.compiler_flag),ns)
        tasks=[]
        def create(coroutine):
            task=asyncio.create_task(coroutine);tasks.append(task);return task
        app=SimpleNamespace(create_task=create,bot=SimpleNamespace(send_message=AsyncMock()))
        try:
            await asyncio.wait_for(ns['on_bot_initialized'](app),1)
            await asyncio.wait_for(started.wait(),1)
            self.assertFalse(tasks[0].done())
        finally:
            release.set();await asyncio.gather(*tasks)

    async def test_contact_is_saved_once_for_fast_code_and_debounced_text(self):
        tree=ast.parse(Path(__file__).resolve().parents[1].joinpath('bot.py').read_text())
        functions=[n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='on_business_message']
        for fast in (True,False):
            contacts=Mock();notify=AsyncMock()
            ns={'run_io':run_io,'asyncio':asyncio,'OWNER_USER_ID':99,'MODESTY_GUARD_CHAT_ID':None,
                '_owner_arrival_reply_targets':{},'_ready_customer_texts':{},'_delayed_greeting_delivery_keys':set(),
                '_latest_customer_message_ids':{},'upsert_customer_contact':contacts,
                'is_guarded_chat':lambda *a:False,'get_auto_reply_paused':lambda:False,
                'seconds_until_customer_replies_allowed':lambda:0,
                'keyword_match_categories':lambda text:['طلب_كود'] if fast else [],
                'cancel_pending_customer_text_batch':Mock(),'queue_customer_text_batch':Mock(),
                'get_customer_totp_accounts':lambda cid:[],'is_live_support_context_active':lambda cid:True,
                'archive_message':Mock(),'notify_owner':notify,'logger':Mock()}
            exec(compile(ast.Module(body=functions,type_ignores=[]),'customer-intake','exec',flags=__future__.annotations.compiler_flag),ns)
            bm=SimpleNamespace(chat=SimpleNamespace(id=1,full_name='Ali',first_name='Ali',username=None),
                from_user=SimpleNamespace(id=1),message_id=10,business_connection_id='business',photo=None,voice=None,
                text='كود' if fast else 'مرحبا سؤال',caption=None)
            update=SimpleNamespace(business_message=bm,edited_business_message=None)
            await ns['on_business_message'](update,SimpleNamespace())
            if not fast:
                ns['_ready_customer_texts'][(1,10)]=bm.text
                await ns['on_business_message'](update,SimpleNamespace())
            self.assertEqual(contacts.call_count,1)
            notify.assert_awaited_once()


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old=list(diagnostics._samples);self.owner=diagnostics._owner_id
        diagnostics._samples.clear();diagnostics.configure_performance(99)

    def tearDown(self):
        diagnostics._samples.clear();diagnostics._samples.extend(self.old)
        diagnostics._owner_id=self.owner

    async def test_only_owner_can_read_timings_and_report_never_changes_money(self):
        diagnostics.record_timing('io','code','bot.py:123',3,1)
        message=SimpleNamespace(reply_text=AsyncMock())
        update=SimpleNamespace(effective_user=SimpleNamespace(id=1),effective_message=message)
        await diagnostics.show_performance(update,None)
        message.reply_text.assert_not_awaited()
        update.effective_user.id=99
        await diagnostics.show_performance(update,None)
        text=message.reply_text.call_args.args[0]
        self.assertIn('bot.py:123',text);self.assertIn('3.00',text);self.assertIn('1.00',text)
        self.assertIn('لا تغيّر البيانات المالية',text)

    def test_old_timings_expire_and_memory_is_bounded(self):
        with patch.object(diagnostics,'monotonic',return_value=0):
            diagnostics.record_timing('io','owner','old',99)
        with patch.object(diagnostics,'monotonic',return_value=1000):
            self.assertNotIn('old',diagnostics.summary())
            for i in range(2100):diagnostics.record_timing('io','owner','recent',1)
        self.assertEqual(len(diagnostics._samples),2000)


if __name__=='__main__':unittest.main()
