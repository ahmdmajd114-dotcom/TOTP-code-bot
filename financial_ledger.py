"""Durable financial transactions; no Telegram clients or imports from bot.py.

The Supabase RPC commits all balances, debt allocations and an outbox entry in
one PostgreSQL transaction. Sheets are projections, never a second money write.
Legacy mode is explicit; connectivity errors never silently select it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import logging
import re
import uuid

from payment_storage import integer_text, payment_datetime

logger = logging.getLogger(__name__)
BAGHDAD = timezone(timedelta(hours=3))
KINDS = {
    'sale': 'شراء جديد', 'debt_repayment': 'تسديد دين',
    'credit_deposit': 'رصيد للزبون', 'expense': 'مصروف', 'refund': 'استرجاع',
    'transfer': 'تحويل بين الخزن', 'capital': 'إضافة رأس مال',
    'withdrawal': 'سحب شخصي', 'debt_issue': 'دين جديد',
    'credit_adjustment': 'تسوية رصيد زبون', 'vault_adjustment': 'تسوية خزنة',
    'reversal': 'إلغاء مرتبط بعملية',
}
_ledger = None


class FinanceError(RuntimeError):
    pass


def money(value, *, signed=False) -> int:
    """Exact dinars. Accept Arabic digits/separators and explicit ألف, never floats."""
    if isinstance(value, bool) or isinstance(value, float):
        raise FinanceError('المبلغ لازم يكون عدداً صحيحاً بالدينار.')
    raw = str(value).strip().translate(str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹', '01234567890123456789'))
    raw = raw.replace(',', '').replace('٬', '').replace('٫', '.')
    match = re.fullmatch(r'([+-]?\d+(?:\.\d+)?)\s*(ألف|الف|آلاف|الاف|مليون|ملايين)?(?:\s*(?:د\.ع|دينار))?', raw)
    if not match:
        raise FinanceError('اكتب مبلغاً واضحاً، مثلاً 15000 أو 15 ألف.')
    factor = 1_000_000 if match[2] in {'مليون', 'ملايين'} else 1000 if match[2] else 1
    try:
        number = Decimal(match[1]) * factor
        if number != number.to_integral_value() or abs(number) > 10**15 or (number < 0 and not signed):
            raise FinanceError('المبلغ غير صالح أو يحتوي كسور دينار.')
        return int(number)
    except InvalidOperation as exc:
        raise FinanceError('المبلغ غير صالح.') from exc


def operation_id(prefix='FIN'):
    return f'{prefix}-{uuid.uuid4().hex.upper()}'


def source_id(prefix, *parts):
    return prefix + '-' + hashlib.sha256('|'.join(map(str, parts)).encode()).hexdigest()[:28].upper()


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def transaction_date(text):
    for fmt in ('%Y-%m-%d %H:%M', '%d-%m-%Y %H:%M', '%Y-%m-%d', '%d-%m-%Y'):
        try:
            stamp = datetime.strptime(text.strip(), fmt).replace(tzinfo=BAGHDAD)
            if stamp > datetime.now(BAGHDAD) + timedelta(minutes=1):
                raise FinanceError('تاريخ العملية ما يصير بالمستقبل.')
            return stamp.astimezone(timezone.utc).isoformat()
        except ValueError:
            pass
    raise FinanceError('اكتب التاريخ هكذا: 2026-10-28 14:30، بتوقيت بغداد.')


def parse_wallets(text, names):
    result = []
    for piece in text.split('+'):
        matches = [name for name in names if piece.strip().startswith(name + ' ')]
        if len(matches) != 1:
            raise FinanceError('اكتب اسم المحفظة والمبلغ، مثلاً: ماستر 5000 + زين كاش 3000.')
        wallet = matches[0]
        amount = money(piece.strip()[len(wallet):])
        if amount <= 0:
            raise FinanceError('مبلغ كل محفظة لازم أكبر من صفر.')
        result.append({'wallet': wallet, 'amount': amount})
    return result


def validate(payload):
    if payload.get('kind') not in KINDS or not payload.get('id'):
        raise FinanceError('نوع العملية أو رقمها غير صالح.')
    payments = payload.get('payments', [])
    for item in payments:
        if not item.get('wallet') or money(item['amount']) <= 0:
            raise FinanceError('راجع المحفظة والمبلغ.')
    for key in ('sales', 'credit_used', 'credit_added', 'new_debt', 'fee'):
        if key in payload:
            money(payload[key])
    kind = payload['kind']
    if kind in {'sale', 'debt_repayment', 'credit_deposit', 'debt_issue'}:
        cash = sum(item['amount'] for item in payments)
        settlements = payload.get('settlements', [])
        if len({s['debt_id'] for s in settlements}) != len(settlements):
            raise FinanceError('نفس الدين مكرر بالتوزيع.')
        for item in settlements:
            if money(item['amount']) <= 0:
                raise FinanceError('مبلغ التسديد غير صالح.')
        incoming = cash + payload.get('credit_used', 0) + payload.get('new_debt', 0) + payload.get('fee', 0)
        outgoing = payload.get('sales', 0) + payload.get('credit_added', 0) + sum(s['amount'] for s in settlements)
        if incoming <= 0 or incoming != outgoing:
            raise FinanceError('توزيع المبلغ غير متوازن. راجع الفرق قبل التثبيت.')
    return payload


class Ledger:
    def __init__(self, client, owner_id):
        self.client, self.owner_id = client, int(owner_id)
        self._mode = None

    @staticmethod
    def require_current_schema(result):
        if int(result.get('schema_version',0))<2:
            raise FinanceError('حدّث ملف supabase_financial_ledger.sql في Supabase قبل تشغيل الحفظ؛ نسخة الجداول الحالية تفتقد حماية تدقيق الخزن.')

    def rpc(self, name, **params):
        try:
            return self.client.rpc(name, {'p_owner': self.owner_id, **params}).execute().data
        except Exception as exc:
            logger.exception('Financial RPC failed: %s', name)
            reason=str(getattr(exc,'message',exc))
            messages={
                'Delivery allocation exists':'الدفعة مرتبطة باشتراك مُسلَّم أو محجوز. راجع عملية التسليم وتراجع عن أثرها المطلوب أولاً؛ لم أغيّر الأموال أو ألْغِ الخدمة تلقائياً.',
                'Financial balance mismatch':'وجدت فرقاً بين الأرصدة والقيود. أوقفت الحفظ؛ راجع تدقيق الخزن، ولا تعالج الفرق بزيادة مجهولة.',
                'Financial opening record missing':'سجل أرصدة البداية مفقود؛ لا أستطيع تأكيد الخزن قبل مراجعته.',
                'Customer credit has been used':'جزء من رصيد هذه العملية استُخدم لاحقاً. تراجع عن العملية التي استخدمته أولاً حتى ما يختل الرصيد.',
                'Debt has repayments; reverse them first':'هذا الدين عليه تسديدات لاحقة. تراجع عن التسديدات المرتبطة أولاً، ثم عن الدين.',
                'Operation already has a refund or reversal':'هذه العملية مرتبطة باسترجاع أو تراجع سابق. افتح الحركة التابعة وراجعها أولاً.',
                'Debt balance changed':'متبقي الدين تغيّر؛ أعد اختياره وراجع مبلغ التسديد.',
                'Insufficient customer credit':'رصيد الزبون لا يغطي المبلغ المحدد؛ راجع التوزيع.',
            }
            for marker,message in messages.items():
                if marker in reason:raise FinanceError(message) from exc
            raise FinanceError('تعذر تأكيد العملية المالية. أعد المحاولة بنفس البطاقة.') from exc

    def mode(self, *, refresh=False):
        if self._mode == 'active' and not refresh:
            return 'active'
        try:
            result = self.client.rpc('finance_setup', {'p_owner': self.owner_id, 'p_action': 'status'}).execute().data
        except Exception as exc:
            # Only an explicitly absent RPC means the old schema. Auth/network
            # failures are not evidence that it is safe to write legacy money.
            if str(getattr(exc, 'code', '')) == 'PGRST202':
                return 'missing'
            raise FinanceError('تعذر قراءة حالة السجل المالي؛ لم أغيّر طريقة التخزين.') from exc
        if result['mode']=='active':self.require_current_schema(result)
        self._mode = result['mode']
        return self._mode

    def active(self):
        mode = self.mode()
        if mode == 'review':
            raise FinanceError('المالية بانتظار اعتماد أرصدة البداية أو إلغاء التهيئة.')
        return mode == 'active'

    def require_active(self):
        if not self.active():
            raise FinanceError('هيّئ السجل المالي من المالية ← تهيئة ومراجعة، أولاً.')

    def setup(self, action, snapshot=None):
        self.require_current_schema(self.rpc('finance_setup',p_action='status'))
        result = self.rpc('finance_setup', p_action=action, p_snapshot=snapshot)
        self._mode = result['mode']
        return result

    def post(self, payload, *, correction=None):
        self.require_active()
        validate(payload)
        if correction:
            return self.rpc('finance_correct', p_reference=correction, p_replacement=payload)
        return self.rpc('finance_post', p=payload)

    def rows(self, table, *, filters=None, order=None):
        result, offset = [], 0
        try:
            while True:
                query = self.client.table(table).select('*').eq('owner_id', self.owner_id)
                for key, value in (filters or {}).items():
                    query = query.eq(key, value)
                query = query.order(order or ('id' if table in {'finance_operations', 'finance_debts'} else 'key'))
                batch = query.range(offset, offset + 499).execute().data or []
                result.extend(batch)
                if len(batch) < 500:
                    return result
                offset += 500
        except Exception as exc:
            raise FinanceError('تعذر قراءة البيانات المالية؛ التقرير غير مكتمل.') from exc

    def accounts(self):
        return {row['key']: int(row['balance']) for row in self.rows('finance_accounts')}

    def snapshot(self):
        return self.rpc('finance_read')

    def debts(self, customer_id=None, *, open_only=False):
        filters = {'customer_id': int(customer_id)} if customer_id is not None else {}
        result = self.rows('finance_debts', filters=filters)
        return [d for d in result if int(d['remaining']) > 0] if open_only else result

    def operations(self, customer_id=None):
        return self.rows('finance_operations', filters={'customer_id': int(customer_id)} if customer_id is not None else {})

    def operation(self, oid):
        try:
            rows=self.client.table('finance_operations').select('*').eq('owner_id',self.owner_id).eq('id',oid).limit(1).execute().data or []
            return rows[0] if rows else None
        except Exception as exc:
            raise FinanceError('تعذر قراءة العملية المالية.') from exc

    def draft(self, draft_id):
        try:
            rows = self.client.table('finance_drafts').select('state').eq('owner_id', self.owner_id).eq('id', draft_id).limit(1).execute().data
            if not rows:
                raise FinanceError('انتهت صلاحية البطاقة. افتح عملية جديدة.')
            return rows[0]['state']
        except FinanceError:
            raise
        except Exception as exc:
            raise FinanceError('تعذر قراءة البطاقة. أعد المحاولة.') from exc

    def save_draft(self, state):
        try:
            self.client.table('finance_drafts').upsert({'owner_id': self.owner_id, 'id': state['draft_id'], 'state': state, 'updated_at': now_iso()}, on_conflict='owner_id,id').execute()
        except Exception as exc:
            raise FinanceError('تعذر حفظ تفاصيل البطاقة. أعد المحاولة.') from exc

    def claim_outbox(self, oid=None):
        return self.rpc('finance_claim_outbox',p_id=oid)

    def ack_outbox(self, job, error=None):
        return self.rpc('finance_ack_outbox', p_id=job['id'], p_token=job['token'], p_error=error)


def configure_ledger(client, owner_id):
    global _ledger
    _ledger = Ledger(client, owner_id)
    return _ledger


def ledger():
    if _ledger is None:
        raise FinanceError('السجل المالي غير مهيأ.')
    return _ledger


def build_legacy_snapshot(payments, expenses, instagram, debts, credits, wallets):
    """Non-destructive import. Historical money has zero new wallet effects.

    Old rows do not identify discounts/refunds/cost bases reliably: preserve
    received/spent amounts and explicitly leave historical sales unknown.
    """
    operations, warnings, seen = [], [], set()

    def dated(value):
        try:
            return payment_datetime(value).isoformat()
        except ValueError:
            warnings.append('توجد حركة قديمة بلا تاريخ صالح؛ تدخل بالإجمالي فقط.')
            return None

    for kind, rows in (('receipt', payments), ('expense', expenses), ('instagram', instagram)):
        for number, row in enumerate(rows[1:], 2):
            if not any(row):
                continue
            if kind == 'receipt':
                if len(row) < 3 or str(row[2]).strip().startswith('دين') or str(row[2]).strip() == 'ملغاة' or not str(row[1]).strip():
                    continue
                amount = money(row[1]); timestamp = dated(row[0])
                if amount == 0:
                    continue
                op = {'id': row[7] if len(row) > 7 and row[7] else source_id('HIST-PAY', number, row), 'kind': 'legacy_receipt', 'occurred_at': timestamp,
                      'customer_id': int(integer_text(row[6])) if len(row) > 6 and re.fullmatch(r'-?\d+', integer_text(row[6])) else None,
                      'product': row[3] if len(row) > 3 else '', 'cash_in': amount, 'legacy_row': number}
            elif kind == 'expense':
                candidates=[]
                for offset in range(max(1,len(row)-2)):
                    if offset+2>=len(row):continue
                    try:
                        payment_datetime(row[offset]);money(row[offset+1])
                    except (ValueError,FinanceError):continue
                    if str(row[offset+2]).strip():candidates.append(tuple(row[offset:offset+3]))
                if len(set(candidates))>1:raise FinanceError('صف مصروف يحتوي أكثر من حركة محتملة؛ راجعه قبل التهيئة.')
                if candidates:row=list(candidates[0])
                if len(row) < 3 or not str(row[1]).strip():
                    if any(row):raise FinanceError('صف مصروف غير واضح؛ لم أتجاهله أثناء التهيئة.')
                    continue
                amount = money(row[1]); reason = str(row[2])
                if 'تعويض' in reason or 'استرجاع' in reason:
                    warnings.append('استرجاعات قديمة تحتاج مطابقة؛ لم أغيّر مبالغها تلقائياً.')
                op = {'id': source_id('HIST-EXP', number, row), 'kind': 'legacy_expense', 'occurred_at': dated(row[0]), 'cash_out': amount, 'expense': amount, 'description': reason, 'legacy_row': number}
            else:
                if len(row) < 10 or row[9] != 'مؤكدة':
                    continue
                op = {'id': source_id('HIST-IG', row[0]), 'kind': 'legacy_instagram', 'occurred_at': dated(row[1]), 'product': row[3], 'cash_in': money(row[6]), 'legacy_row': number}
            if op['id'] in seen:
                raise FinanceError('رقم عملية قديم مكرر؛ تحتاج مطابقة قبل التهيئة.')
            seen.add(op['id']); operations.append(op)
    accounts = [{'key': 'wallet:' + name, 'balance': money(value, signed=True)} for name, value in wallets.items()]
    credit_balances = {}
    for row in credits[1:]:
        if len(row) >= 6 and re.fullmatch(r'-?\d+', integer_text(row[1])):
            credit_balances[integer_text(row[1])] = money(row[5])
    accounts += [{'key': 'credit:' + chat, 'balance': value} for chat, value in credit_balances.items()]
    imported_debts = []
    for debt in debts:
        if not debt.get('original_amount') and debt['remaining']<=0:
            warnings.append('بعض الديون المغلقة القديمة بلا مبلغ أصلي؛ بقيت محفوظة بالشيت للمراجعة.')
            continue
        imported_debts.append({'id': debt['debt_id'], 'customer_id': debt['chat_id'], 'customer_name': debt['customer_line'],
            'product': debt['product'], 'product_id': debt.get('product_id'), 'plan_id': debt.get('plan_id'), 'plan_name': debt.get('plan_name'),
            'original': debt['original_amount'] or debt['remaining'], 'remaining': debt['remaining'],
            'created_at': dated(debt['created_at']) if debt['created_at'] else None,
            'last_paid_at': dated(debt['last_paid_at']) if debt['last_paid_at'] else None, 'legacy_row': debt['row_number']})
    return {'operations': operations, 'accounts': accounts, 'debts': imported_debts, 'warnings': sorted(set(warnings))}
