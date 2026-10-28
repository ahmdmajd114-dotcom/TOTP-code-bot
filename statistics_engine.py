"""Pure, exact-dinar statistics. Never writes balances or guesses old facts."""
from collections import defaultdict
from datetime import datetime, timedelta
import re

from financial_ledger import BAGHDAD, FinanceError, money

MONEY = ('received', 'spent', 'sales', 'expenses', 'refunds', 'debt_new',
         'debt_paid', 'credit_added', 'credit_used', 'capital', 'withdrawals',
         'discounts', 'fees', 'purchase_cash', 'repayment_cash', 'deposit_cash',
         'mixed_cash', 'legacy_cash', 'receipt_corrections', 'spend_corrections',
         'net_sales', 'product_costs', 'operating_expenses', 'fee_expenses', 'general_expenses', 'unknown_expenses')
COUNTS = ('sales_count', 'receipts_count', 'expense_count', 'reversal_count')
LABELS = {'received':'المقبوض المسجل بعد التصحيحات', 'spent':'الخارج المسجل بعد التصحيحات',
          'sales':'المبيعات المسجلة', 'expenses':'المصاريف', 'refunds':'المسترجعات',
          'debt_new':'ديون جديدة', 'debt_paid':'ديون مسددة', 'credit_added':'رصيد مضاف للزبائن',
          'credit_used':'رصيد مستخدم', 'capital':'رأس مال مضاف', 'withdrawals':'سحب شخصي',
          'discounts':'الخصومات', 'fees':'رسوم محتسبة', 'purchase_cash':'قبض مخصص للشراء',
          'repayment_cash':'قبض مخصص لتسديد الدين', 'deposit_cash':'قبض محفوظ رصيداً',
          'mixed_cash':'قبض مختلط غير قابل للتوزيع النقدي', 'legacy_cash':'مقبوض قديم غير مصنف',
          'receipt_corrections':'تصحيحات قيود القبض', 'spend_corrections':'تصحيحات قيود الصرف',
          'sales_count':'صافي عدد المبيعات', 'receipts_count':'صافي عدد الدفعات',
          'expense_count':'صافي عدد المصاريف', 'reversal_count':'عمليات التراجع'}
LABELS.update(net_sales='المبيعات بعد المسترجعات', product_costs='مصاريف تكلفة المنتجات',
              operating_expenses='مصاريف التشغيل', fee_expenses='مصاريف الرسوم',
              general_expenses='مصاريف عامة', unknown_expenses='مصاريف غير مصنفة')


def stamp(value):
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        # New ledger timestamps must carry their timezone. Do not shift a
        # manually edited, ambiguous legacy timestamp by guessing UTC/Baghdad.
        if result.tzinfo is None:
            return None
        return result.astimezone(BAGHDAD)
    except (ValueError, TypeError):
        return None


def period_bounds(period, now):
    now = now.astimezone(BAGHDAD)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == 'all': return None, now
    if period == 'today': return today, now
    if period == 'yesterday': return today-timedelta(days=1), today
    if period == 'week': return today-timedelta(days=today.weekday()), now
    if period == 'month': return today.replace(day=1), now
    def parse(text):
        text = text.strip().translate(str.maketrans('٠١٢٣٤٥٦٧٨٩', '0123456789'))
        for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d', '%d/%m/%Y %H:%M', '%d/%m/%Y'):
            try: return datetime.strptime(text, fmt).replace(tzinfo=BAGHDAD)
            except ValueError: pass
        raise FinanceError('اكتب يوماً YYYY-MM-DD أو فترة: YYYY-MM-DD إلى YYYY-MM-DD.')
    if ' إلى ' in period:
        a, b = period.split(' إلى ', 1)
        start, end = parse(a), parse(b)
        end += timedelta(minutes=1) if ':' in b else timedelta(days=1)
    elif re.fullmatch(r'\d{4}-\d{2}', period):
        start = parse(period+'-01')
        end = (start.replace(day=28)+timedelta(days=4)).replace(day=1)
    else:
        start = parse(period)
        end = start + (timedelta(minutes=1) if ':' in period else timedelta(days=1))
    if start >= end or start > now:
        raise FinanceError('راجع حدود الفترة؛ البداية لازم قبل النهاية وألا تكون بالمستقبل.')
    return start, min(end, now)


def normalized(value):
    return re.sub(r'\s+', ' ', str(value or '').strip().casefold())


def product_key(payload, label, catalog):
    if payload.get('product_id'):
        return 'id:'+str(payload['product_id'])
    match = {str(p['id']) for p in catalog if normalized(label) in
             {normalized(n) for n in [p.get('name'), *(p.get('aliases') or [])]} and normalized(label)}
    if len(match) == 1:
        return 'id:'+match.pop()
    return 'name:'+normalized(label) if normalized(label) else 'unassigned'


def empty(): return {k:0 for k in (*MONEY, *COUNTS)}


def _amount(value): return money(0 if value is None else value, signed=True)


def make_events(snapshot):
    """Each contribution belongs to exactly one product; sums are conserved."""
    operations = snapshot['operations']
    by_id = {r['id']:r for r in operations}
    if len(by_id) != len(operations): raise FinanceError('تكرر رقم عملية؛ لا يمكن اعتماد التقرير.')
    debts = {str(d['id']):d for d in snapshot['debts']}
    catalog = snapshot.get('operational', {}).get('catalog_products', [])
    events = []
    for row in operations:
        if row['kind'] == 'opening': continue
        reversal = row['kind'] == 'reversal'
        original = by_id.get(row.get('reference_id')) if reversal else row
        if not original or (reversal and original['kind'] == 'reversal'):
            raise FinanceError('تراجع بلا عملية أصلية واضحة؛ التقرير غير مكتمل.')
        p = original['payload']; sign = -1 if reversal else 1
        label = original.get('product') or p.get('product') or 'غير مصنف'
        key = product_key(p, original.get('product'), catalog)
        parts = {}
        def add(metric, amount, target=key, name=label):
            value = sign*_amount(amount)
            if not value:return
            if target not in parts: parts[target] = {'key':target, 'name':name, 'metrics':empty()}
            parts[target]['metrics'][metric] += value
        kind = original['kind']; cin = _amount(original.get('cash_in')); cout = _amount(original.get('cash_out'))
        for metric, field in [('sales','sales'), ('expenses','expense'), ('refunds','refunded')]:
            add(metric, original.get(field))
        expense_category = 'fee' if kind=='sale' and p.get('fee') else p.get('category')
        category_metric={'cost':'product_costs','operating':'operating_expenses','fee':'fee_expenses','general':'general_expenses'}.get(expense_category,'unknown_expenses')
        add(category_metric,original.get('expense'))
        add('sales_count', int(kind in {'sale','debt_issue'} and _amount(original.get('sales'))>0))
        add('expense_count', int(kind in {'expense','legacy_expense'}))
        add('receipts_count', int(cin>0))
        for metric, field in [('debt_new','new_debt'), ('credit_added','credit_added'),
                              ('credit_used','credit_used'), ('discounts','discount'), ('fees','fee')]:
            add(metric, p.get(field))
        settlements = p.get('settlements') or []
        for s in settlements:
            debt = debts.get(str(s['debt_id']))
            if debt is None: raise FinanceError('تسديد مرتبط بدين مفقود؛ لم أنسبه لمنتج افتراضي.')
            dk = product_key(debt, debt.get('product'), catalog)
            add('debt_paid', s['amount'], dk, debt.get('product') or 'غير مصنف')
        # Cash attribution is derived only when the stored allocation proves
        # it. A mixture funded by old credit has no recorded payment priority.
        repaid = sum(_amount(s['amount']) for s in settlements)
        added = _amount(p.get('credit_added'))
        purchase = cin-repaid-added
        if kind in {'sale','debt_repayment','credit_deposit','debt_issue'}:
            if purchase >= 0:
                add('received', purchase); add('purchase_cash', purchase)
                for s in settlements:
                    debt=debts[str(s['debt_id'])]; dk=product_key(debt,debt.get('product'),catalog)
                    add('received',s['amount'],dk,debt.get('product') or 'غير مصنف')
                    add('repayment_cash',s['amount'],dk,debt.get('product') or 'غير مصنف')
                add('received',added,'customer_credit','أرصدة الزبائن')
                add('deposit_cash',added,'customer_credit','أرصدة الزبائن')
            else:
                add('received',cin,'mixed','قبض مختلط غير موزع'); add('mixed_cash',cin,'mixed','قبض مختلط غير موزع')
        else:
            add('received',cin)
            if kind=='capital': add('capital',cin)
            elif kind.startswith('legacy_'): add('legacy_cash',cin)
        add('spent',cout)
        if kind=='withdrawal': add('withdrawals',cout)
        if reversal:
            # Corrections are disclosed separately, not described as a bank
            # payment or an actual refund to the customer.
            parts.setdefault(key,{'key':key,'name':label,'metrics':empty()})
            parts[key]['metrics']['receipt_corrections'] += cin
            parts[key]['metrics']['spend_corrections'] += cout
            parts[key]['metrics']['reversal_count'] += 1
        customer = str(original.get('customer_id') or '')
        if not customer and p.get('instagram_sale'):
            customer='instagram:'+normalized(p['instagram_sale'].get('instagram_account'))
        source = p.get('source') or ('instagram' if p.get('instagram_sale') or kind=='legacy_instagram' else 'telegram' if original.get('customer_id') else 'unknown')
        metrics=empty()
        for part in parts.values():
            part['metrics']['net_sales']=part['metrics']['sales']-part['metrics']['refunds']
            for k,v in part['metrics'].items(): metrics[k]+=v
        events.append({'id':row['id'], 'record':row, 'original':original, 'parts':list(parts.values()),
                       'metrics':metrics, 'at':stamp(row.get('occurred_at')), 'source':source,
                       'customer':customer, 'plan_id':str(p.get('plan_id') or ''),
                       'plan_name':p.get('plan_name') or 'باقة غير محددة',
                       'unknown_sales':original.get('sales') is None and kind in {'legacy_receipt','legacy_instagram'}})
    return events


def selected(event, filters, start, end):
    if event['at'] is None:
        if start is not None: return False
    elif (start and event['at']<start) or event['at']>=end: return False
    if filters.get('source') and event['source']!=filters['source']: return False
    if filters.get('customer') and event['customer']!=str(filters['customer']): return False
    if filters.get('plan') and (event['plan_id'] or 'unknown')!=filters['plan']: return False
    return not filters.get('product') or any(p['key']==filters['product'] for p in event['parts'])


def contribution(event, filters):
    result=empty()
    for part in event['parts']:
        if filters.get('product') and part['key']!=filters['product']: continue
        for k,v in part['metrics'].items(): result[k]+=v
    return result


def positions(snapshot, start, end, events, filters):
    """Stocks are global; product/source filters cannot invent wallet balances."""
    opening=next((r for r in snapshot['operations'] if r['kind']=='opening'),None)
    cutoff=stamp(snapshot.get('cutover_at')) or stamp((opening or {}).get('occurred_at'))
    by_id={e['id']:e for e in events}; entries=snapshot.get('entries')
    if entries is None: raise FinanceError('قيود الخزن غير متاحة؛ حدّث ملف المالية قبل اعتماد التقرير.')
    rows=[]
    for key,current in sorted(snapshot['accounts'].items()):
        if not key.startswith(('wallet:','credit:')): continue
        future=0; interval=0; incoming=outgoing=transfers=adjustments=0
        for entry in entries:
            if entry['account_key']!=key: continue
            e=by_id.get(entry['operation_id'])
            if e is None or e['at'] is None: raise FinanceError('قيد مالي بلا عملية مؤرخة؛ كشف الرصيد غير مكتمل.')
            delta=_amount(entry['delta'])
            if e['at']>=end: future+=delta
            if (start is None or e['at']>=start) and e['at']<end:
                interval+=delta
                kind=e['original']['kind']
                if kind=='transfer': transfers+=delta
                elif kind in {'vault_adjustment','credit_adjustment'}: adjustments+=delta
                elif delta>0: incoming+=delta
                else: outgoing-=delta
        closing=_amount(current)-future
        covered=start is not None and cutoff is not None and start>=cutoff
        rows.append({'key':key,'current':_amount(current),'opening':closing-interval if covered else None,
                     'closing':closing if cutoff and end>=cutoff else None,'movement':interval,
                     'incoming':incoming,'outgoing':outgoing,'transfers':transfers,'adjustments':adjustments,
                     'covered':covered})
    debt_now=sum(_amount(d['remaining']) for d in snapshot['debts'])
    def debt_at(point):
        if not cutoff or point<cutoff: return None
        return debt_now-sum(e['metrics']['debt_new']-e['metrics']['debt_paid'] for e in events if e['at'] and e['at']>=point)
    return {'wallets':[r for r in rows if r['key'].startswith('wallet:')],
            'credits':[r for r in rows if r['key'].startswith('credit:')], 'debt_current':debt_now,
            'debt_opening':debt_at(start) if start else None,'debt_closing':debt_at(end),
            'credit_current':sum(_amount(v) for k,v in snapshot['accounts'].items() if k.startswith('credit:')),
            'cash_current':sum(_amount(v) for k,v in snapshot['accounts'].items() if k.startswith('wallet:')),
            'global':True,'cutoff':cutoff}


def build_report(snapshot, filters=None, now=None):
    filters=dict(filters or {}); filters.setdefault('period','today')
    now=stamp(snapshot.get('as_of')) or now or datetime.now(BAGHDAD)
    from vault_manager import audit_balances
    audit=audit_balances(snapshot)
    events=make_events(snapshot); start,end=period_bounds(filters['period'],now)
    current=[e for e in events if selected(e,filters,start,end)]
    totals=empty(); groups={}; sources={}; plans={}; customers=defaultdict(int)
    for e in current:
        m=contribution(e,filters)
        for k,v in m.items(): totals[k]+=v
        if e['customer']:customers[e['customer']]+=m['sales_count']
        for part in e['parts']:
            if filters.get('product') and part['key']!=filters['product']:continue
            group=groups.setdefault(part['key'],{'key':part['key'],'name':part['name'],'metrics':empty()})
            for k,v in part['metrics'].items():group['metrics'][k]+=v
        for collection,key,name in ((sources,e['source'],e['source']), (plans,e['plan_id'] or 'unknown',e['plan_name'])):
            group=collection.setdefault(key,{'key':key,'name':name,'metrics':empty()})
            for k,v in m.items(): group['metrics'][k]+=v
    prior={}
    if start:
        # Compare the same elapsed duration, not a full previous day against
        # a partial current day. Calendar months/weeks retain their boundaries.
        if filters['period']=='month': prior_start=(start-timedelta(days=1)).replace(day=1); prior_end=min(prior_start+(end-start),start)
        elif filters['period']=='week': prior_start=start-timedelta(days=7); prior_end=prior_start+(end-start)
        else: prior_start=start-(timedelta(days=1) if filters['period']=='today' else end-start); prior_end=prior_start+(end-start)
        prior=empty()
        for e in events:
            if selected(e,filters,prior_start,prior_end):
                for k,v in contribution(e,filters).items():prior[k]+=v
    undated=[e for e in events if e['at'] is None]
    unknown=[e for e in current if e['unknown_sales']]
    previous_buyers={e['customer'] for e in events if start and e['at'] and e['at']<start and e['metrics']['sales_count']>0 and e['customer']}
    buyers={k for k,v in customers.items() if v>0}
    quality={'balance_mismatches':sum(not a['matched'] for a in audit.values()),
             'undated':len(undated),'undated_cash':sum(e['metrics']['received']-e['metrics']['spent'] for e in undated),
             'unknown_sales':len(unknown),'unknown_receipts':sum(contribution(e,filters)['received'] for e in unknown),
             'unidentified_sales':sum(1 for e in current if contribution(e,filters)['sales_count']>0 and not e['customer']),
             'unclassified':len([e for e in current if e['original']['kind'] in {'sale','debt_issue','expense','legacy_receipt','legacy_expense','legacy_instagram'} and any(p['key']=='unassigned' for p in e['parts'])])}
    if any(sum(g['metrics'][k] for g in groups.values())!=totals[k] for k in totals):
        raise FinanceError('تفاصيل المنتجات لا تطابق الإجمالي؛ لم أعرض رقماً غير متوازن.')
    return {'filters':filters,'as_of':now,'start':start,'end':end,'totals':totals,'events':current,'all_events':events,
            'products':sorted(groups.values(),key=lambda g:(-g['metrics']['sales'],g['name'])),
            'sources':list(sources.values()),'plans':list(plans.values()),'quality':quality,
            'buyers':len(buyers),'returning':len(buyers & previous_buyers),'new_buyers':len(buyers-previous_buyers),
            'positions':positions(snapshot,start,end,events,filters),'previous':prior,
            'profit':None,'undated_events':undated,'snapshot':snapshot}
