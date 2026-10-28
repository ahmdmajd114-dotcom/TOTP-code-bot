"""Operational counters from the same snapshot; no secrets or side effects."""
from collections import Counter
from datetime import timedelta
import re
from statistics_engine import stamp, normalized


def operational_report(snapshot, now):
    data=snapshot.get('operational',{}); status=snapshot.get('source_status',{})
    unavailable={k:v for k,v in status.items() if v!='ok'}
    result={'unavailable':unavailable,'subscriptions':None,'accounts':None,'rewards':None,'attention':[]}
    operations={r['id']:r for r in snapshot['operations']}
    reversed_ids={r.get('reference_id') for r in operations.values() if r['kind']=='reversal'}
    reminders=data.get('subscription_reminders')
    if reminders is not None:
        counts=Counter(); customers=set(); pending=[]
        for r in reminders:
            start,end=stamp(r.get('started_at')),stamp(r.get('expires_at'))
            oid=r.get('financial_operation_id')
            if r.get('status')=='cancelled' or oid in reversed_ids:
                counts['cancelled']+=1;continue
            if r.get('status')=='pending_delivery' or r.get('feedback_status')=='pending_delivery':
                counts['pending_delivery']+=1
                if oid in operations:pending.append(oid)
            elif end is None or start is None:counts['unknown']+=1
            elif start>now:counts['future']+=1
            elif end<=now:counts['expired']+=1
            elif r.get('status')=='active':
                counts['active']+=1
                if r.get('customer_chat_id') is not None:customers.add('tg:'+str(r['customer_chat_id']))
                elif r.get('instagram_account'):customers.add('ig:'+normalized(r['instagram_account']))
                else:counts['unidentified_customers']+=1
                if end<=now+timedelta(days=3):counts['expiring_3d']+=1
            else:counts['other']+=1
        counts['customers']=len(customers);counts['total']=len(reminders)
        counts['pending_ids']=pending;result['subscriptions']=dict(counts)
        known={r.get('financial_operation_id') for r in reminders}
        for r in operations.values():
            if r['kind']=='sale' and not r.get('historical') and r['id'] not in reversed_ids and r['payload'].get('subscription') and r['id'] not in known:
                result['attention'].append({'id':r['id'],'reason':'بيع يحتاج التحقق من تسجيل متابعة الاشتراك'})
        result['attention'] += [{'id':oid,'reason':'مدفوع بانتظار التسليم'} for oid in pending]
    accounts=data.get('chatgpt_shared_accounts'); legacy=data.get('totp_accounts')
    links=data.get('customer_totp_account_links'); assignments=data.get('chatgpt_account_assignments')
    if all(rows is not None for rows in (accounts,legacy,links,assignments)):
        # Same email can occur in both native and legacy registries.
        canonical={}; account_map={}
        for source,rows in (('native',accounts),('legacy',legacy)):
            for r in rows:
                if source=='native' and not r.get('is_active'):continue
                label=normalized(r.get('email') or r.get('label'))
                # Repeated display labels such as "خاص" are not identities.
                key=label if re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',label) else source+':'+str(r['id'])
                private=source=='legacy' and bool(r.get('is_private'))
                canonical.setdefault(key,{'key':key,'private':private,'capacity':None if source=='legacy' else r.get('capacity'),'customers':set()})
                account_map[(source,str(r['id']))]=key
        for rows,source,field in ((assignments,'native','account_id'),(links,'legacy','account_id')):
            for r in rows:
                if r.get('status')!='active':continue
                raw=str(r[field]);actual_source=source
                if raw.startswith('native:'):actual_source='native';raw=raw[7:]
                if raw.startswith('legacy:'):actual_source='legacy';raw=raw[7:]
                key=account_map.get((actual_source,raw))
                if key:canonical[key]['customers'].add(str(r['customer_chat_id']))
        result['accounts']={'private':sum(r['private'] for r in canonical.values()),
                            'shared':sum(not r['private'] for r in canonical.values()),
                            'links':sum(len(r['customers']) for r in canonical.values()),
                            'rows':[{**r,'customers':len(r['customers'])} for r in canonical.values()]}
    contacts=data.get('continuity_bot_contacts'); invites=data.get('continuity_bot_invites')
    recipients=data.get('customer_campaign_recipients'); campaigns=data.get('customer_campaigns')
    if all(rows is not None for rows in (contacts,invites,recipients,campaigns)):
        result['rewards']={'started':len({r['customer_chat_id'] for r in contacts}),
                           'invites':dict(Counter(r['status'] for r in invites)),
                           'campaigns':len(campaigns),'messages':len(recipients),
                           'campaign_customers':len({r['customer_chat_id'] for r in recipients}),
                           'delivery':dict(Counter(r['delivery_status'] for r in recipients))}
    for r in snapshot.get('outbox',[]):
        if r['status']!='done':result['attention'].append({'id':r['operation_id'],'reason':'مزامنة معلقة'+(' بعد فشل' if r.get('last_error') else '')})
    return result
