import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
import os
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import AsyncMock, Mock, patch

import work_hours_manager as wh


def moment(hour, minute=0, day=23):
    return datetime(2026, 11, day, hour, minute, tzinfo=wh.BAGHDAD)


class Query:
    def __init__(self, db):
        self.db=db; self.filters=[]; self.payload=None
    def select(self, *args): return self
    def limit(self, *args): return self
    def eq(self, key, value):
        self.filters.append(lambda row:row.get(key)==value); return self
    def lte(self, key, value):
        self.filters.append(lambda row:datetime.fromisoformat(row[key]) <= datetime.fromisoformat(value)); return self
    def update(self, payload): self.payload=payload; return self
    def execute(self):
        if self.db.failure: raise RuntimeError('unavailable')
        rows=[row for row in self.db.rows if all(f(row) for f in self.filters)]
        if self.payload:
            for row in rows: row.update(self.payload)
        return NS(data=deepcopy(rows))


class Store:
    def __init__(self): self.rows=[]; self.failure=False
    def table(self, name):
        assert name=='customer_work_hours'; return Query(self)
    def rpc(self, name, p):
        assert name=='work_hours_event'
        def execute():
            if self.failure: raise RuntimeError('unavailable')
            now=datetime.fromisoformat(p['p_at'])
            if not wh.in_work_hours(now):return NS(data=None)
            day=wh.day_key(p['p_chat'],now)[1]
            row=next((r for r in self.rows if r['chat_id']==p['p_chat'] and r['work_day']==day),None)
            state={'customer':'waiting','owner':'answered','code':'code'}[p['p_kind']]
            if not row:
                row=dict(owner_id=p['p_owner'],chat_id=p['p_chat'],work_day=day,
                    first_message_id=p['p_message'],first_at=now.isoformat(),due_at=(now+timedelta(minutes=15)).isoformat(),
                    status=state,revision=1)
                self.rows.append(row)
            else:
                if p['p_kind']!='customer':row['status']=state
                row['revision']+=1
            row['business_id']=p['p_business']
            return NS(data=deepcopy(row))
        return NS(execute=execute)


class PolicyTests(unittest.TestCase):
    def test_exact_baghdad_boundaries_and_utc_conversion(self):
        for hour, minute, expected in [(7,59,False),(8,0,True),(15,59,True),(16,0,False)]:
            now=moment(hour,minute)
            self.assertEqual(wh.in_work_hours(now),expected)
            self.assertEqual(wh.in_work_hours(now.astimezone(timezone.utc)),expected)

    def test_direct_owner_reply_excludes_commands_and_automated_bot_messages(self):
        bm=NS(from_user=NS(id=99),text='تدلل',photo=None)
        self.assertTrue(wh.is_direct_reply(bm,99))
        for text in ('/id','/link 123','دين','قبض','ac','س','كود'):
            bm.text=text
            self.assertFalse(wh.is_direct_reply(bm,99),text)
        bm.text='تدلل';bm.sender_business_bot=NS(id=123)
        self.assertFalse(wh.is_direct_reply(bm,99))


class WorkHoursTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.previous=wh._services
        self.db=Store();self.now=moment(9)
        self.paused=False;self.archive=Mock();self.owner_replied=Mock(return_value=False)
        wh._cancelled.clear();wh._timers.clear();wh.failure.reported=False
        wh.configure_work_hours(wh.WorkHoursServices(self.db,99,lambda:self.paused,self.archive,
            self.owner_replied,lambda:self.now))
        self.context=NS(bot=NS(send_message=AsyncMock(return_value=NS(message_id=100))),
            application=NS(create_task=Mock(side_effect=self.fake_task)))
        self.bm=NS(chat=NS(id=1,full_name='Customer',first_name='Customer',username=None),
            business_connection_id='business',message_id=10,date=self.now)
        self.io=patch.object(wh,'run_io',self.io_call);self.io.start()
        self.timer=patch.object(wh,'create_timer',self.fake_task);self.timer.start()
    async def io_call(self,fn,*args,**kwargs):
        await asyncio.sleep(0)
        return fn(*args,**kwargs)
    def fake_task(self,coroutine):
        coroutine.close()
        return Mock()
    def tearDown(self):
        self.io.stop();self.timer.stop();wh._services=self.previous;wh._cancelled.clear();wh._timers.clear()
    async def route(self,text):
        return await wh.route(self.context,self.bm,text=text)
    async def advance(self,minutes):
        self.now+=timedelta(minutes=minutes)
        await wh.work_hours_worker(self.context)

    async def test_first_message_fifteen_minutes_and_additional_messages_do_not_reset(self):
        self.assertEqual(await self.route('السلام عليكم'),'hold')
        due=self.db.rows[0]['due_at']
        await self.advance(10)
        self.context.bot.send_message.assert_not_awaited()
        self.bm.date=self.now;self.bm.message_id+=1
        await self.route('اريد اشتراك')
        self.assertEqual(self.db.rows[0]['due_at'],due)
        await self.advance(5)
        self.context.bot.send_message.assert_awaited_once_with(chat_id=1,business_connection_id='business',text=wh.WAIT_TEXT)
        self.assertEqual(self.db.rows[0]['status'],'sent')
        await self.route('بعدكم؟')
        await self.advance(20)
        self.assertEqual(self.context.bot.send_message.await_count,1)

    async def test_owner_reply_cancels_across_process_memory_reset(self):
        await self.route('مرحبا')
        await wh.owner_answered(self.context,1,'business',15)
        wh._cancelled.clear();wh._timers.clear()
        await self.advance(15)
        self.context.bot.send_message.assert_not_awaited()
        self.assertEqual(self.db.rows[0]['status'],'answered')

    async def test_code_request_cancels_wait_and_only_login_continuations_are_allowed(self):
        await self.route('مرحبا')
        self.assertEqual(await self.route('اريد الكود'),'login')
        self.assertEqual(await self.route('المصادقة'),'login')
        self.assertEqual(await self.route('دخلت'),'login')
        self.assertEqual(await self.route('سعر اشتراك جات؟'),'hold')
        wh._cancelled.clear();wh._timers.clear()
        await self.advance(15)
        self.context.bot.send_message.assert_not_awaited()

    async def test_login_screens_cancel_wait_without_authorizing_totp_here(self):
        for intent in ('email_code','authenticator_code','passkey','login_unknown'):
            self.assertEqual(await wh.route(self.context,self.bm,photo_intent=intent),'login')
        self.assertEqual(self.db.rows[0]['status'],'code')
        await self.advance(15)
        self.context.bot.send_message.assert_not_awaited()

    async def test_late_afternoon_wait_expires_at_four_and_old_messages_are_not_flushed(self):
        self.now=moment(15,55);self.bm.date=self.now
        await self.route('اريد اشتراك')
        await self.advance(15)
        self.context.bot.send_message.assert_not_awaited()
        self.assertEqual(await self.route('اريد اشتراك'),'hold')
        self.bm.date=self.now;self.bm.message_id+=1
        self.assertEqual(await self.route('اريد اشتراك'),'normal')

    async def test_same_customer_gets_independent_day(self):
        await self.route('مرحبا');await self.advance(15)
        self.now=moment(9,day=24);self.bm.date=self.now;self.bm.message_id+=1
        await self.route('مرحبا');await self.advance(15)
        self.assertEqual(self.context.bot.send_message.await_count,2)

    async def test_recovery_worker_and_timer_race_send_only_once(self):
        await self.route('مرحبا')
        self.now+=timedelta(minutes=15)
        row=deepcopy(self.db.rows[0])
        await asyncio.gather(wh.send_waiting(self.context,row),wh.work_hours_worker(self.context))
        self.assertEqual(self.context.bot.send_message.await_count,1)

    async def test_owner_arrival_while_claim_is_in_progress_suppresses_send(self):
        await self.route('مرحبا')
        self.owner_replied.side_effect=lambda *args:wh.cancel_immediately(1,self.now) or False
        await self.advance(15)
        self.context.bot.send_message.assert_not_awaited()

    async def test_archived_owner_response_prevents_send_after_restart(self):
        await self.route('مرحبا');self.owner_replied.return_value=True
        await self.advance(15)
        self.context.bot.send_message.assert_not_awaited()
        self.assertEqual(self.db.rows[0]['status'],'answered')

    async def test_pause_and_failed_storage_never_allow_general_reply(self):
        self.paused=True
        self.assertEqual(await self.route('الكود'),'hold')
        self.assertFalse(self.db.rows)
        self.paused=False;self.db.failure=True
        self.assertEqual(await self.route('سعر جات'),'hold')
        self.assertEqual(self.context.bot.send_message.call_args.kwargs['chat_id'],99)

    async def test_unknown_send_outcome_is_not_retried(self):
        await self.route('مرحبا')
        self.context.bot.send_message.side_effect=TimeoutError()
        await self.advance(15)
        self.assertEqual(self.db.rows[0]['status'],'sending')
        calls=self.context.bot.send_message.await_count
        await self.advance(1)
        self.assertEqual(self.context.bot.send_message.await_count,calls)

    async def test_successful_manual_bot_reply_cancels_but_automatic_reply_does_not(self):
        class Base:
            def __init__(self,token):pass
            async def send_message(self,*args,**kwargs):return NS(message_id=11)
        bot=wh.build_work_hours_bot('fake',Base)
        await self.route('مرحبا')
        async def reply(update,context):
            return await bot.send_message(chat_id=1,business_connection_id='business',text='تدلل')
        tracked=wh.track_handler(reply)
        await tracked(NS(effective_user=NS(id=1)),self.context)
        self.assertEqual(self.db.rows[0]['status'],'waiting')
        await tracked(NS(effective_user=NS(id=99)),self.context)
        self.assertEqual(self.db.rows[0]['status'],'answered')

    async def test_sending_boundary_blocks_prepared_general_reply_and_allows_confirmed_login(self):
        sent=AsyncMock(return_value=NS(message_id=11))
        class Base:
            def __init__(self,token):pass
            async def send_message(self,*args,**kwargs):return await sent(*args,**kwargs)
        bot=wh.build_work_hours_bot('fake',Base)
        self.now=moment(7,59);self.bm.date=self.now;self.bm.text='مرحبا';self.bm.from_user=NS(id=1)
        async def reply(update,context):
            # Intent was classified before 08:00, but the typing delay crosses
            # into the protected period.
            self.now=moment(8)
            return await bot.send_message(chat_id=1,business_connection_id='business',text='سعر الباقة...')
        update=NS(effective_user=NS(id=1),business_message=self.bm)
        with self.assertRaises(wh.WorkHoursReplyBlocked):
            await wh.track_handler(reply)(update,self.context)
        sent.assert_not_awaited()
        async def code_reply(update,context):
            wh.login_reply_authorized.set(True)
            return await bot.send_message(chat_id=1,business_connection_id='business',text='123456')
        await wh.track_handler(code_reply)(update,self.context)
        sent.assert_awaited_once()

    async def test_waiting_message_bypasses_customer_boundary_guard(self):
        sent=AsyncMock(return_value=NS(message_id=11))
        class Base:
            def __init__(self,token):pass
            async def send_message(self,*args,**kwargs):return await sent(*args,**kwargs)
        self.context.bot=wh.build_work_hours_bot('fake',Base)
        self.bm.from_user=NS(id=1);self.bm.text='مرحبا'
        await self.route('مرحبا');self.now+=timedelta(minutes=15)
        async def deliver(update,context):await wh.send_waiting(context,self.db.rows[0])
        await wh.track_handler(deliver)(NS(business_message=self.bm,effective_user=NS(id=1)),self.context)
        sent.assert_awaited_once_with(chat_id=1,business_connection_id='business',text=wh.WAIT_TEXT)

    async def test_failed_manual_send_does_not_cancel_customer_wait(self):
        class Base:
            def __init__(self,token):pass
            async def send_message(self,*args,**kwargs):raise TimeoutError()
        bot=wh.build_work_hours_bot('fake',Base)
        await self.route('مرحبا')
        async def reply(update,context):await bot.send_message(chat_id=1,business_connection_id='business',text='تدلل')
        with self.assertRaises(TimeoutError):
            await wh.track_handler(reply)(NS(effective_user=NS(id=99)),self.context)
        self.assertEqual(self.db.rows[0]['status'],'waiting')

    async def test_shutdown_cancels_timer_without_losing_durable_wait(self):
        await self.route('مرحبا')
        wh._timers.clear()
        task=asyncio.create_task(wh.wait_for_reply(self.context,self.db.rows[0],900))
        wh._timers[(1,'2026-11-23')]=task
        drain=AsyncMock()
        await asyncio.wait_for(wh.stop_work_hours(drain)(NS()),1)
        self.assertTrue(task.cancelled())
        self.assertEqual(self.db.rows[0]['status'],'waiting')


@unittest.skipUnless(os.environ.get('WORK_HOURS_TEST_DSN'),'Requires isolated local PostgreSQL')
class WorkHoursDatabaseTests(unittest.TestCase):
    def setUp(self):self.owner=uuid.uuid4().int % 10**12
    def tearDown(self):self.sql(f'delete from customer_work_hours where owner_id={self.owner}')
    def sql(self,query,ok=True):
        result=subprocess.run(['/opt/homebrew/bin/psql',os.environ['WORK_HOURS_TEST_DSN'],'-XAt','-v','ON_ERROR_STOP=1','-c',query],text=True,capture_output=True)
        self.assertEqual(result.returncode==0,ok,result.stderr)
        return result.stdout.strip()
    def event(self,kind,minute=0):
        return self.sql(f"select work_hours_event({self.owner},1,'b',10,'{kind}','2026-11-23 09:{minute:02}:00+03')")
    def test_repeat_and_concurrent_first_messages_keep_one_deadline(self):
        with ThreadPoolExecutor(2) as pool:list(pool.map(self.event,['customer','customer']))
        self.event('customer',10)
        self.assertEqual(self.sql(f"select count(*)||':'||extract(epoch from(due_at-first_at))::int from customer_work_hours where owner_id={self.owner} group by due_at,first_at"),'1:900')
    def test_owner_and_code_cancellation_persist(self):
        self.event('customer');self.event('owner',2);self.event('customer',3)
        self.assertEqual(self.sql(f'select status from customer_work_hours where owner_id={self.owner}'),'answered')
        self.event('code',4);self.event('customer',5)
        self.assertEqual(self.sql(f'select status from customer_work_hours where owner_id={self.owner}'),'code')
    def test_time_limits_and_service_only_access(self):
        for hour in ('07:59','16:00'):
            self.sql(f"select work_hours_event({self.owner},1,'b',1,'customer','2026-11-23 {hour}:00+03')")
        self.assertEqual(self.sql(f'select count(*) from customer_work_hours where owner_id={self.owner}'),'0')
        for role in ('anon','authenticated'):
            self.sql(f'set role {role}; select * from customer_work_hours',False)
            self.sql(f"set role {role}; select work_hours_event({self.owner},1,'b',1,'customer')",False)
