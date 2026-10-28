"""Shared owner-only financial cards, persisted drafts and setup review."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable
import logging

from financial_ledger import FinanceError, KINDS, ledger, now_iso, operation_id

logger = logging.getLogger(__name__)
_services = None


@dataclass
class FinanceServices:
    owner_id: int
    products: Callable
    plans: Callable
    wallets: Callable
    identity: Callable
    button: Callable
    markup: Callable
    legacy_snapshot: Callable
    project: Callable
    subscription: Callable
    notify: Callable


def configure_finance_ui(services):
    global _services
    _services = services


def services():
    return _services


def keyboard(rows):
    return _services.markup([[_services.button(label, callback_data=callback) for label, callback in line] for line in rows])


def draft(kind, **fields):
    return {'draft_id': operation_id('D')[2:12], 'operation_id': operation_id('PAY' if kind == 'income' else 'EXP'),
            'flow': kind, 'kind': 'sale' if kind == 'income' else 'expense', 'occurred_at': now_iso(),
            'step': 'product', 'history': [], 'revision': 0, 'awaiting': False, 'payments': [], **fields}


def navigate(state, step):
    if state.get('step') != step:
        state.setdefault('history', []).append(state.get('step', 'summary'))
    state['step'], state['awaiting'] = step, False


def back(state):
    state['step'] = state['history'].pop() if state.get('history') else 'summary'
    state['awaiting'] = False


def card_rows(state, rows):
    prefix = 'fi' if state['flow'] == 'income' else 'fe' if state['flow'] == 'expense' else 'fv'
    return keyboard([[(label, f"{prefix}:{state['draft_id']}:{state['revision']}:{action}") for label, action in line] for line in rows])


async def display(context, state, text, rows, query=None):
    state['revision'] += 1
    ledger().save_draft(state)
    markup = card_rows(state, rows)
    if state.get('photo') and len(text)>1000:
        # Telegram photo captions have a lower limit than normal messages.
        # Keep the proof photo and move the complete review to a text card.
        if query:
            await query.edit_message_caption(caption='صورة الإثبات محفوظة؛ تفاصيل العملية بالبطاقة النصية أدناه.',reply_markup=None)
        state['photo']=False
        sent=await context.bot.send_message(chat_id=_services.owner_id,text=text[:4000],reply_markup=markup)
        state['message_id']=sent.message_id
        ledger().save_draft(state)
        return
    if query and getattr(query.message,'message_id',state.get('message_id'))!=state.get('message_id'):
        query=None
    if query:
        if state.get('photo'):
            await query.edit_message_caption(caption=text, reply_markup=markup)
        else:
            await query.edit_message_text(text=text, reply_markup=markup)
    elif state.get('message_id'):
        method = context.bot.edit_message_caption if state.get('photo') else context.bot.edit_message_text
        field = 'caption' if state.get('photo') else 'text'
        await method(chat_id=_services.owner_id, message_id=state['message_id'], reply_markup=markup, **{field: text})
    else:
        sent = await context.bot.send_message(chat_id=_services.owner_id, text=text, reply_markup=markup)
        state['message_id'] = sent.message_id
        ledger().save_draft(state)


def customer_text(state):
    return f"الزبون: {state.get('customer_name') or state.get('customer_id') or 'عام'}"


def suspend_finance_inputs(except_id=None):
    """Keep unfinished cards, but never consume another feature's next text."""
    if ledger().mode()!='active':return
    rows=ledger().client.table('finance_drafts').select('state').eq('owner_id',_services.owner_id).contains('state',{'awaiting':True}).limit(200).execute().data or []
    for row in rows:
        state=row['state']
        if state['draft_id']!=except_id:
            state['awaiting']=False
            ledger().save_draft(state)


async def handle_finance_callback(update, context):
    query = update.callback_query
    if query.from_user.id != _services.owner_id:
        await query.answer('هذا الزر للأونر فقط.', show_alert=True)
        return
    try:
        prefix, key, revision, action = query.data.split(':', 3)
        state = ledger().draft(key)
        await query.answer()
        saved = ledger().operation(state['operation_id'])
        if saved:
            state['committed'] = True
        if state.get('committed'):
            await query.message.reply_text(f"✅ العملية محفوظة برقم {state['operation_id']}؛ لم أكررها.")
            return
        if action == 'cancel':
            state['closed'], state['awaiting'] = True, False
            ledger().save_draft(state)
            await query.edit_message_caption(caption='تم الإلغاء قبل التثبيت.', reply_markup=None) if state.get('photo') else await query.edit_message_text('تم الإلغاء قبل التثبيت.', reply_markup=None)
            return
        if state.get('closed'):
            raise FinanceError('هذه البطاقة ملغاة. افتح عملية جديدة.')
        suspend_finance_inputs(state['draft_id'])
        from income_manager import handle_income_action, render_income
        from expense_manager import handle_expense_action, render_expense
        from vault_manager import handle_vault_action, render_vault
        handler, render = {'fi': (handle_income_action, render_income), 'fe': (handle_expense_action, render_expense), 'fv': (handle_vault_action, render_vault)}[prefix]
        if int(revision) == state['revision']:
            if action == 'back':
                back(state)
            else:
                await handler(state, action, context, query)
                if state.get('committed'):
                    return
        await render(state, context, query)
    except FinanceError as exc:
        await query.message.reply_text(f'⚠️ {exc}')
    except Exception:
        logger.exception('Financial card failed')
        await query.message.reply_text('⚠️ تعذر إكمال البطاقة. أعد فتحها من المالية؛ لا تعتبرها مثبتة بدون رقم عملية.')


async def handle_finance_input(update, context):
    message = update.message
    if not message or not message.text or not update.effective_user or update.effective_user.id != _services.owner_id:
        return False
    try:
        if not ledger().active():
            return False
        # A reply selects that card exactly. Otherwise only one waiting draft
        # may consume input, including after a process restart.
        rows = ledger().client.table('finance_drafts').select('state').eq('owner_id', _services.owner_id).contains('state', {'awaiting': True}).order('updated_at', desc=True).limit(30).execute().data or []
        candidates = [r['state'] for r in rows if not r['state'].get('closed') and not r['state'].get('committed')]
        if message.reply_to_message:
            candidates = [s for s in candidates if s.get('message_id') == message.reply_to_message.message_id]
        if not candidates:
            return False
        if len(candidates) != 1:
            await message.reply_text('عندك أكثر من بطاقة تنتظر إدخالاً؛ رد على البطاقة المطلوبة حتى ما يختلط المبلغ.')
            return True
        state = candidates[0]
        from income_manager import income_input, render_income
        from expense_manager import expense_input, render_expense
        from vault_manager import vault_input, render_vault
        handler, render = {'income': (income_input, render_income), 'expense': (expense_input, render_expense), 'vault': (vault_input, render_vault)}[state['flow']]
        handler(state, message.text)
        state['awaiting'] = False
        await render(state, context)
        return True
    except FinanceError as exc:
        await message.reply_text(f'⚠️ {exc}')
        return True
    except Exception:
        logger.exception('Failed to load persisted financial input')
        await message.reply_text('⚠️ تعذر قراءة البطاقة المالية؛ لم أسجل المبلغ بعملية أخرى.')
        return True


async def commit_card(state, payload, context, query):
    if state.get('correction_of'):
        payload['correction_of'] = state['correction_of']
    # Save the final immutable request before submitting it. Lost responses and
    # restarts then retry the exact same ID/date/amount, never a new operation.
    state['operation_id']=payload['id']
    state['prepared_payload']=payload
    ledger().save_draft(state)
    ledger().post(payload, correction=state.get('correction_of'))
    state['committed'], state['awaiting'] = True, False
    ledger().save_draft(state)
    pending = False
    job=None
    try:
        job=ledger().claim_outbox(payload['id'])
        if job:
            await _services.project(job['payload'])
            ledger().ack_outbox(job)
        else:
            pending=True
    except Exception:
        logger.exception('Immediate financial projection pending')
        if job:
            try:ledger().ack_outbox(job,'projection unavailable')
            except FinanceError:pass
        pending = True
    message = f"✅ تم حفظ العملية كاملة.\nرقم العملية: {payload['id']}"
    if pending:
        message += '\nمزامنة الشيت والمتابعة بالانتظار؛ النظام يعيدها تلقائياً.'
    if state.get('photo'):
        await query.edit_message_caption(caption=message, reply_markup=None)
    else:
        await query.edit_message_text(message, reply_markup=None)
    try:
        await _services.notify(context, payload)
    except Exception:
        logger.exception('Financial notification failed after commit')


async def finance_worker(context):
    try:
        if not ledger().active():
            return
        job = ledger().claim_outbox()
        if not job:
            return
        try:
            await _services.project(job['payload'])
        except Exception as exc:
            logger.exception('Financial projection retry queued: %s', job['id'])
            ledger().ack_outbox(job, str(type(exc).__name__))
        else:
            ledger().ack_outbox(job)
    except FinanceError:
        logger.warning('Financial worker unavailable; no money fallback performed')


async def show_finance(message):
    try:
        mode = ledger().mode(refresh=True)
        if mode=='active':suspend_finance_inputs()
        if mode != 'active':
            text = ('المالية الجديدة جاهزة للتهيئة. بعد قراءة البيانات يعرض البوت أرقام البداية قبل اعتمادها.\n'
                    'يلزم تشغيل ملف supabase_financial_ledger.sql مرة واحدة إذا لم تُثبّت الجداول.')
            rows = [[('📄 ملف التهيئة', 'finance_sql')], [('🔎 تهيئة ومراجعة البداية', 'finance_setup')]]
            if mode == 'review':
                text += '\nالمالية تنتظر اعتماد المراجعة أو إلغاءها.'
                rows += [[('✅ اعتماد أرقام البداية', 'finance_activate')], [('إلغاء التهيئة', 'finance_setup_cancel')]]
        else:
            from reports_manager import financial_report
            text = financial_report('today')
            rows = [[('💵 قبض', 'finance_income'), ('💸 مصروف', 'finance_expense')],
                    [('💳 الديون والأرصدة', 'finance_customers'), ('📊 التقارير', 'finance_reports')],
                    [('🏦 الخزن والتحويل', 'finance_vaults'), ('📋 سجل العمليات', 'finance_records')],
                    [('⏳ تحتاج متابعة', 'finance_pending')]]
        await message.reply_text(text, reply_markup=keyboard(rows))
    except FinanceError as exc:
        await message.reply_text(f'⚠️ {exc}')


async def handle_finance_menu(update, context):
    query = update.callback_query
    if query.from_user.id != _services.owner_id:
        await query.answer('للأونر فقط.', show_alert=True)
        return
    await query.answer()
    data = query.data
    try:
        if data == 'finance_sql':
            with Path(__file__).with_name('supabase_financial_ledger.sql').open('rb') as document:
                await context.bot.send_document(chat_id=_services.owner_id, document=document, caption='شغّل الملف مرة واحدة في SQL Editor بمشروع Supabase نفسه، ثم اضغط تهيئة ومراجعة.')
        elif data == 'finance_setup':
            ledger().setup('begin')
            try:
                snapshot = _services.legacy_snapshot()
                ledger().setup('stage', snapshot)
            except Exception:
                ledger().setup('cancel')
                raise
            accounts = '\n'.join(f"{a['key'].replace('wallet:', '')}: {a['balance']:,}" for a in snapshot['accounts'] if a['key'].startswith('wallet:'))
            text = (f"مراجعة البداية\nحركات قديمة: {len(snapshot['operations'])}\nديون مفتوحة: {sum(d['remaining']>0 for d in snapshot['debts'])}\n{accounts}\n\n"
                    'هذه أرصدة البداية فقط؛ استيراد الماضي لن يضيفها مرة ثانية للخزن.\n'
                    'إجمالي المبيعات والربح للبيانات القديمة غير مكتمل لغياب تصنيفها التفصيلي.')
            if snapshot['warnings']:
                text += '\n\n' + '\n'.join(snapshot['warnings'])
            text += '\n\nالمالية متوقفة حتى تعتمد هذه الأرقام أو تلغي التهيئة.'
            await query.edit_message_text(text[:4000], reply_markup=keyboard([[('✅ اعتماد أرقام البداية', 'finance_activate')], [('إلغاء والعودة للنظام السابق', 'finance_setup_cancel')]]))
        elif data == 'finance_activate':
            ledger().setup('activate')
            await show_finance(query.message)
        elif data == 'finance_setup_cancel':
            ledger().setup('cancel')
            await query.edit_message_text('أُلغيت التهيئة؛ البيانات القديمة بقيت كما هي.')
        elif data == 'finance_income':
            from debt_manager import cancel_debt_input
            cancel_debt_input(context)
            for key in ('pending_catalog_input','pending_payment_input','pending_shared_account','campaign_draft','pending_manual_subscription','pending_personal_reminder'):
                context.user_data.pop(key,None)
            from income_manager import start_income
            await start_income(context)
        elif data == 'finance_expense':
            from debt_manager import cancel_debt_input
            cancel_debt_input(context)
            for key in ('pending_catalog_input','pending_payment_input','pending_shared_account','campaign_draft','pending_manual_subscription','pending_personal_reminder'):
                context.user_data.pop(key,None)
            from expense_manager import start_expense
            await start_expense(context)
        elif data == 'finance_vaults':
            from vault_manager import show_vaults
            await show_vaults(query.message)
        elif data.startswith('finance_credit_'):
            from customer_balance_manager import start_credit_adjustment
            await start_credit_adjustment(context,int(data[len('finance_credit_'):]))
        elif data.startswith('finance_draft_'):
            state=ledger().draft(data[len('finance_draft_'):])
            from income_manager import render_income
            from expense_manager import render_expense
            from vault_manager import render_vault
            if state.get('committed') or state.get('closed'):raise FinanceError('هذه البطاقة مثبتة أو ملغاة.')
            await {'income':render_income,'expense':render_expense,'vault':render_vault}[state['flow']](state,context)
        elif data.startswith(('finance_reports', 'finance_period_', 'finance_records', 'finance_view_', 'finance_pending', 'finance_reverse_', 'finance_refund_', 'finance_correct_', 'finance_customers')):
            from reports_manager import handle_report_menu
            await handle_report_menu(data, query, context)
        elif data.startswith('finance_wallet_'):
            from vault_manager import start_vault_action
            await start_vault_action(context, data[len('finance_wallet_'):])
    except FinanceError as exc:
        await query.message.reply_text(f'⚠️ {exc}')
    except Exception:
        logger.exception('Financial menu failed')
        await query.message.reply_text('⚠️ تعذر إكمال الطلب. لم أعتبر البيانات المفقودة أصفاراً.')
