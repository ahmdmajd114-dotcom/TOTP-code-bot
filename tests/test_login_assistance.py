import asyncio
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
import uuid
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

import login_assistance as la
from photo_intent import PhotoIntent, is_confident_code_verification


class RulesTests(unittest.TestCase):
    def test_only_authenticator_screen_authorizes_totp(self):
        for intent in la.LOGIN_PHOTOS:
            with self.subTest(intent=intent):
                self.assertEqual(la.decide(intent).code, intent == 'authenticator_code')
                self.assertEqual(is_confident_code_verification(PhotoIntent(intent, .99)), intent == 'authenticator_code')

    def test_negative_and_help_requests_never_issue_codes(self):
        for text in ('ما يطلب رمز المصادقة', 'ما اشتغل رمز المصادقة', 'شنو تطبيق المصادقة', 'كود الايميل', 'باسكي'):
            self.assertFalse(la.decide(la.text_signal(text), 'clarify').code, text)

    def test_cash_and_unrelated_success_are_not_login(self):
        self.assertIsNone(la.text_signal('اريد گودنوت'))
        self.assertIsNone(la.text_signal('اريد كودنوت'))
        self.assertFalse(la.decide('success').text)
        self.assertFalse(la.decide('problem').text)

    def test_bilingual_instructions_and_no_totp_on_email_repeat(self):
        self.assertIn(la.PASSWORD, la.INTRO)
        self.assertIn(la.PASSWORD, la.EMAIL)
        self.assertIn('لوحة المفاتيح', la.EMAIL)
        self.assertIn(la.ALTERNATIVE, la.PASSKEY)
        self.assertEqual(la.decide('email_code', 'email').text, la.SCREENSHOT)
        self.assertEqual(la.decide('success', 'authenticator').text, la.DONE)
        self.assertFalse(la.decide('success', 'complete').text)


class Query:
    def __init__(self, db):
        self.db = db
        self.filters = []
        self.op = 'select'
        self.payload = {}

    def select(self, *args): return self
    def limit(self, *args): return self
    def eq(self, key, value):
        self.filters.append(lambda row: row.get(key) == value)
        return self
    def lte(self, key, value):
        self.filters.append(lambda row: row.get(key, '') <= value)
        return self
    def gte(self, key, value):
        self.filters.append(lambda row: row.get(key, '') >= value)
        return self
    def lt(self, key, value):
        self.filters.append(lambda row: row.get(key, '') < value)
        return self
    def update(self, payload):
        self.op = 'update'; self.payload = payload
        return self
    def insert(self, payload):
        self.op = 'insert'; self.payload = payload
        return self
    def execute(self):
        if self.db.failure:
            raise RuntimeError('Database unavailable')
        if self.op == 'insert':
            row = dict(self.payload, id=str(len(self.db.rows)+1))
            self.db.rows.append(row)
            return NS(data=[deepcopy(row)])
        rows = [row for row in self.db.rows if all(f(row) for f in self.filters)]
        if self.op == 'update':
            for row in rows: row.update(self.payload)
        return NS(data=deepcopy(rows))


class DB:
    def __init__(self): self.rows = []; self.failure = False
    def table(self, name):
        assert name == 'chatgpt_login_sessions'
        return Query(self)


class FlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = DB()
        self.selection = 'a'
        self.accounts = [{'id': 'a'}, {'id': 'b'}]
        self.old = la._services
        la.configure_login(la.LoginServices(self.db, 99, lambda chat: self.accounts,
            lambda accounts: next(a for a in accounts if a['id'] == self.selection),
            lambda chat, account: True, lambda: False, lambda chat: True))
        self.context = NS(bot=NS(send_message=AsyncMock()))
        self.bm = NS(chat=NS(id=1), business_connection_id='business', message_id=10)
        self.io = patch.object(la, 'run_io', self.run_io)
        self.io.start()

    async def run_io(self, fn, *args): return fn(*args)
    def tearDown(self): self.io.stop(); la._services = self.old

    async def test_ineligible_customer_cannot_start_login_or_use_old_picker(self):
        la._services.eligible = Mock(return_value=False)
        la._services.accounts = Mock(return_value=self.accounts)
        self.assertIs(await la.handle_login(self.context, self.bm, text='كود', code_request=True), True)
        self.assertIs(await la.handle_login(self.context, self.bm, photo=PhotoIntent(intent='email_code', confidence=.99)), True)
        await la.account_chosen(self.context, self.bm, 1, 'a', 'business')
        la._services.accounts.assert_not_called()
        self.context.bot.send_message.assert_not_awaited()
        self.assertEqual(self.db.rows, [])
    async def send(self, text=None, photo=None):
        result = await la.handle_login(self.context, self.bm, text=text, photo=photo)
        self.bm.message_id += 1
        return result

    async def test_email_password_authenticator_success_and_duplicate(self):
        self.assertTrue(await self.send(photo=PhotoIntent('email_code', .99)))
        self.assertEqual(self.context.bot.send_message.call_args.kwargs['text'], la.EMAIL)
        self.assertTrue(await self.send('اريد الكود'))
        self.assertEqual(self.context.bot.send_message.call_args.kwargs['text'], la.CLARIFY)
        self.assertEqual(await self.send('تطبيق المصادقة'), 'code')
        # The store adapter will generate and send the numeric-only code.
        self.assertEqual(self.context.bot.send_message.await_count, 2)
        self.assertEqual(await self.send('اريد الكود'), 'code')
        self.assertTrue(await self.send('دخلت'))
        self.assertEqual(self.context.bot.send_message.call_args.kwargs['text'], la.DONE)
        self.bm.message_id -= 1
        await self.send('دخلت')
        self.assertEqual(self.context.bot.send_message.await_count, 3)

    async def test_single_word_answer_and_restart_load_existing_state(self):
        await self.send('الكود')
        services = la._services
        la.configure_login(la.LoginServices(**vars(services)))
        self.assertEqual(await self.send('المصادقة'), 'code')
        self.assertEqual(await self.send('كود'), 'code')

    async def test_passkey_or_new_passkey_interrupts_code_stage(self):
        await self.send('تطبيق المصادقة')
        for text in ('طلع باسكي', 'انشاء باسكي'):
            self.assertTrue(await self.send(text))
            self.assertTrue(await self.send('الكود'))
            self.assertEqual(self.context.bot.send_message.call_args.kwargs['text'], la.CLARIFY)

    async def test_uncertain_photo_after_authenticator_does_not_reuse_stage(self):
        await self.send('تطبيق المصادقة')
        self.assertTrue(await self.send(photo=PhotoIntent('authenticator_code', .4)))
        self.assertEqual(self.context.bot.send_message.call_args.kwargs['text'], la.CLARIFY)

    async def test_multiple_accounts_do_not_share_challenge_state(self):
        await self.send('تطبيق المصادقة')
        self.selection = 'b'
        self.assertTrue(await self.send('الكود'))
        self.assertEqual(self.context.bot.send_message.call_args.kwargs['text'], la.CLARIFY)
        self.selection = 'a'
        self.assertEqual(await self.send('الكود'), 'code')

    async def test_failed_read_or_write_blocks_code_and_notifies_owner(self):
        self.db.failure = True
        self.assertTrue(await self.send('تطبيق المصادقة'))
        self.assertEqual(self.context.bot.send_message.call_args.kwargs['chat_id'], 99)
        self.db.failure = False
        with patch.object(Query, 'insert', side_effect=RuntimeError('failed write')):
            self.assertTrue(await self.send('تطبيق المصادقة'))
        self.assertEqual(self.context.bot.send_message.call_args.kwargs['chat_id'], 99)

    async def test_replayed_message_never_issues_second_code(self):
        self.assertEqual(await self.send('تطبيق المصادقة'), 'code')
        self.bm.message_id -= 1
        self.assertTrue(await self.send('تطبيق المصادقة'))

    async def test_expired_stage_requires_clarification_again(self):
        await self.send('تطبيق المصادقة')
        self.db.rows[0]['updated_at'] = (datetime.now(timezone.utc)-timedelta(days=2)).isoformat()
        self.assertTrue(await self.send('الكود'))

    def queue_note(self):
        self.db.rows.append(dict(id='note', owner_id=99, chat_id=1, account_id='a', business_id='business',
            stage='delivered', note_status='queued', due_at=datetime.now(timezone.utc).isoformat()))

    async def test_delayed_note_is_separate_claimed_once_and_suppressed_on_reply(self):
        self.queue_note()
        await la.send_note(self.context, 1, 'a')
        await la.send_note(self.context, 1, 'a')
        self.assertEqual(self.context.bot.send_message.await_count, 1)
        self.assertEqual(self.context.bot.send_message.call_args.kwargs['text'], la.INTRO)
        self.db.rows[0]['note_status'] = 'queued'
        await la.owner_takeover(1)
        await la.send_note(self.context, 1, 'a')
        self.assertEqual(self.context.bot.send_message.await_count, 1)

    async def test_note_timeout_never_retries_unknown_send(self):
        self.queue_note()
        self.context.bot.send_message.side_effect = TimeoutError()
        await la.send_note(self.context, 1, 'a')
        await la.send_note(self.context, 1, 'a')
        self.assertEqual(self.db.rows[0]['note_status'], 'sending')
        # One delivery attempt plus one owner notification attempt.
        self.assertEqual(self.context.bot.send_message.await_count, 2)

    async def test_timer_waits_five_seconds(self):
        with patch.object(la.asyncio, 'sleep', new=AsyncMock()) as sleep, patch.object(la, 'send_note', new=AsyncMock()) as note:
            await la.delayed_note(self.context, 1, 'a')
        sleep.assert_awaited_once_with(5)
        note.assert_awaited_once_with(self.context, 1, 'a')

    async def test_account_picker_clarifies_without_consuming_code(self):
        self.assertEqual(await self.send('تطبيق المصادقة'), 'code')
        await la.account_chosen(self.context, self.bm, 1, 'b', 'business')
        self.assertEqual(self.context.bot.send_message.call_args.kwargs['text'], la.CLARIFY)
        self.selection = 'b'
        self.bm.message_id += 1
        self.assertEqual(await self.send('المصادقة'), 'code')

    async def test_note_not_sent_after_customer_has_advanced(self):
        self.queue_note()
        self.db.rows[0].update(updated_at=datetime.now(timezone.utc).isoformat(), last_message_id=1)
        await self.send('تطبيق المصادقة')
        await la.send_note(self.context, 1, 'a')
        self.context.bot.send_message.assert_not_awaited()

    async def test_recovery_skips_stale_notes_without_sending(self):
        self.queue_note()
        self.db.rows[0]['due_at'] = (datetime.now(timezone.utc)-timedelta(minutes=20)).isoformat()
        await la.login_worker(self.context)
        self.assertEqual(self.db.rows[0]['note_status'], 'skipped')
        self.context.bot.send_message.assert_not_awaited()

    async def test_committed_delivery_schedules_only_a_new_message(self):
        client = Mock()
        client.table.return_value.select.return_value.eq.return_value.eq.return_value.single.return_value.execute.return_value.data = {'legacy_id': 'a'}
        client.rpc.return_value.execute.return_value.data = True
        la._services.client = client
        la._services.select = Mock(return_value=True)
        self.context.application = NS(create_task=Mock())
        request = dict(account_id='managed', customer_id=1, business_id='business',
                       sent_at=datetime.now(timezone.utc).isoformat())
        # Avoid creating a pending coroutine: only inspect scheduling here.
        with patch.object(la, 'delayed_note', new=Mock(return_value='scheduled')) as delayed:
            await la.start_delivery(self.context, request, 50)
            self.context.application.create_task.assert_called_once_with('scheduled')
            delay = delayed.call_args.args[-1]
            self.assertGreater(delay, 4)
            self.assertLessEqual(delay, 5)
            la._services.select.assert_called_once_with(1, 'a')
            self.assertEqual(client.rpc.call_args.args[1]['p_message'], 50)
            # A database-confirmed duplicate must not reset account selection
            # or schedule another initial note.
            client.rpc.return_value.execute.return_value.data = False
            await la.start_delivery(self.context, request, 50)
            self.assertEqual(delayed.call_count, 1)
            self.assertEqual(la._services.select.call_count, 1)

    async def test_start_failure_notifies_owner_without_scheduling(self):
        self.context.application = NS(create_task=Mock())
        with patch.object(la, 'run_io', side_effect=RuntimeError('unavailable')):
            await la.start_delivery(self.context, dict(customer_id=1, account_id='a'), 10)
        self.context.application.create_task.assert_not_called()
        self.assertEqual(self.context.bot.send_message.call_args.kwargs['chat_id'], 99)


@unittest.skipUnless(os.environ.get('LOGIN_TEST_DSN'), 'Requires isolated local PostgreSQL')
class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.owner = uuid.uuid4().int % 10**12
    def tearDown(self):
        self.sql(f'delete from chatgpt_login_sessions where owner_id={self.owner}')
    def sql(self, sql, ok=True):
        result = subprocess.run(['/opt/homebrew/bin/psql', os.environ['LOGIN_TEST_DSN'], '-X', '-At',
                                 '-v', 'ON_ERROR_STOP=1', '-c', sql], capture_output=True, text=True)
        self.assertEqual(result.returncode == 0, ok, result.stderr)
        return result.stdout.strip()
    def start(self, message):
        self.sql(f"set role service_role; select start_chatgpt_login({self.owner},1,'a','business',{message})")
    def test_duplicate_start_does_not_reset_completed_login(self):
        self.start(10)
        self.sql(f"update chatgpt_login_sessions set stage='complete',last_message_id=15,note_status='skipped' where owner_id={self.owner}")
        self.start(10)
        self.start(12)
        self.assertEqual(self.sql(f"select stage from chatgpt_login_sessions where owner_id={self.owner}"), 'complete')
        self.start(20)
        self.assertEqual(self.sql(f"select stage||':'||note_status from chatgpt_login_sessions where owner_id={self.owner}"), 'delivered:queued')
        seconds = float(self.sql(f"select extract(epoch from (due_at-updated_at)) from chatgpt_login_sessions where owner_id={self.owner}"))
        self.assertAlmostEqual(seconds, 5, places=2)
    def test_only_one_worker_claims_note(self):
        self.start(10)
        query = f"update chatgpt_login_sessions set note_status='sending' where owner_id={self.owner} and note_status='queued' returning id"
        with ThreadPoolExecutor(2) as executor:
            results = list(executor.map(self.sql, [query, query]))
        self.assertEqual(sum('UPDATE 1' in result for result in results), 1)
    def test_only_one_customer_event_claims_transition(self):
        self.start(10)
        query = f"update chatgpt_login_sessions set stage='authenticator',last_message_id=11 where owner_id={self.owner} and last_message_id=10 returning id"
        with ThreadPoolExecutor(2) as executor:
            results = list(executor.map(self.sql, [query, query]))
        self.assertEqual(sum('UPDATE 1' in result for result in results), 1)
    def test_service_only_permissions(self):
        for role in ('anon', 'authenticated'):
            self.sql(f'set role {role}; select * from chatgpt_login_sessions', ok=False)
            self.sql(f"set role {role}; select start_chatgpt_login({self.owner},1,'a','b',1)", ok=False)
