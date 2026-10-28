"""Owner-managed account delivery. No bot import, clients, or money on import."""
from dataclasses import dataclass
from datetime import datetime,timezone
from html import escape
from pathlib import Path
import asyncio,base64,re,uuid

from financial_ledger import BAGHDAD, FinanceError, source_id
from finance_ui import keyboard,services

GUIDE='https://t.me/+IcHCjNi8_ilkZjdi'
CHANNEL='https://t.me/+M8XsrznhCNJkNTA6'
TERMS='https://totp-code-bot.onrender.com/terms'
_services=None


@dataclass
class DeliveryServices:
    client:object
    owner_id:int
    products:object
    plans:object
    duration:object
    is_chatgpt:object
    schedule:object
    cancel_schedule:object
    invite:object
    identity:object=None


def configure_delivery(config):
    global _services
    _services=config


def compact(value):return base64.urlsafe_b64encode(uuid.UUID(str(value)).bytes).decode().rstrip('=')


def expanded(value):
    return str(uuid.UUID(bytes=base64.urlsafe_b64decode(value+'=='))) if len(value)==22 else str(uuid.UUID(value))


def funding_token(value):return source_id('F',value)[2:22]


def display_date(value):
    if not value:return '—'
    return datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(BAGHDAD).strftime('%Y-%m-%d %H:%M')+' بغداد'


def rpc(action,**payload):
    try:return _services.client.rpc('delivery_manage',{'p_owner':_services.owner_id,'p_action':action,'p':payload}).execute().data
    except Exception as exc:
        code=str(getattr(exc,'code',''))
        if code=='PGRST202':raise FinanceError('شغّل ملف supabase_account_delivery.sql أولاً. لم أنفّذ ربطاً جزئياً.') from None
        reasons={
            'capacity exceeded':'الحساب ممتلئ أو محجوز بعملية أخرى. لم أتجاوز السعة.',
            'allocation already used':'هذه الدفعة مستخدمة أو محجوزة؛ اختر دفعة أخرى أو راجع توزيعها.',
            'already activated':'هذه الدفعة مفعلة بالنظام السابق؛ راجع اشتراكها قبل استخدامها.',
            'Card changed':'البطاقة تغيّرت؛ أعد فتحها.',
            'Subscription changed':'الاشتراك تغير؛ راجع العملية قبل الإكمال.',
            'pending delivery':'لهذا الاشتراك عملية تسليم معلقة. أكملها أو ألغها أولاً.',
            'credentials differ':'بيانات الحساب الموجود مختلفة؛ لم أستبدلها بصمت.',
            'TOTP differs':'مفتاح الحساب القديم مختلف؛ يحتاج مراجعة قبل ربطه.',
            'Reservation expired':'انتهى الحجز أو بدأت محاولة إرسال؛ افتح حالة العملية.',
            'Possible credential':'قد تكون البيانات وصلت؛ تحقق من التسليم قبل الإلغاء.',
            'later subscription':'تراجع عن تعديلات الاشتراك اللاحقة أولاً.',
            'Account changed':'الحساب تغير أو تعطل؛ أعد مراجعته.',
        }
        reason=str(getattr(exc,'message',exc))
        for marker,message in reasons.items():
            if marker in reason:raise FinanceError(message) from None
        # No DB exception string: account registration contains credentials.
        raise FinanceError('تعذّر تأكيد عملية التسليم. افتحها من /delivery وراجع حالتها؛ لا تعتبرها مكتملة.') from None


def validate_account(state):
    email=state['email'].strip()
    if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',email):raise FinanceError('اكتب إيميل صحيحاً.')
    secret=re.sub(r'\s+','',state['secret']).upper().rstrip('=')
    if not re.fullmatch('[A-Z2-7]{16,128}',secret):raise FinanceError('المفتاح Base32 طويل، وليس كوداً مؤقتاً من ستة أرقام.')
    try:
        if len(base64.b32decode(secret+'='*((8-len(secret)%8)%8)))<10:raise ValueError()
    except Exception:raise FinanceError('مفتاح TOTP غير صالح.') from None
    if not 1<=int(state['capacity'])<=20:raise FinanceError('السعة بين 1 و20.')
    if not 6<=len(state['password'])<=1024:raise FinanceError('راجع طول كلمة المرور (6 إلى 1024 حرفاً).')
    if not re.fullmatch('[A-Za-z0-9_-]{4,40}',state['code']):raise FinanceError('رمز الربط من 4 إلى 40 حرفاً إنكليزياً أو رقماً أو _ أو -.')
    return {**state,'email':email,'secret':secret,'capacity':int(state['capacity'])}


def delivery_text(email,password,template=None):
    template=template or {}
    # URLs supplied by the server template, validated on save. No secret in
    # callbacks, notifications, logs, or the customer-facing template.
    for k,default in [('guide_url',GUIDE),('channel_url',CHANNEL),('terms_url',TERMS)]:
        value=template.get(k) or default
        if not value.startswith('https://') or any(c.isspace() for c in value):raise FinanceError('رابط القالب غير صالح.')
    return ('<b>ChatGPT</b>\n\n'+escape(email)+'\n\n<blockquote>'+escape(password)+'</blockquote>\n\n'
        +f'<a href="{escape(template.get("guide_url") or GUIDE,quote=True)}">طريقة التسجيل</a>\n\n'
        +f'<a href="{escape(template.get("channel_url") or CHANNEL,quote=True)}">لو سمحت انضم للقناة</a>\n\n'
        +'شروط الاستخدام (وصول الرسالة يعني موافقتك على الشروط)\n\n'
        +escape(template.get('terms_url') or TERMS)+'\n\nهنيئاً 🎉')


def markups(request,rows):
    result=[[(label,f"dl:{compact(request['id'])}:{request['revision']}:{action}") for label,action in line] for line in rows]
    if any(len(callback.encode())>64 for line in result for _,callback in line):raise FinanceError('معرف زر التسليم أطول من حد تيليجرام؛ أعد فتح العملية.')
    return keyboard(result)


def catalog_options():
    result=[]
    for product in _services.products():
        if product.get('is_active') and _services.is_chatgpt(product.get('name')):
            for plan in _services.plans(str(product['id'])):
                days=_services.duration(plan.get('duration'))
                if plan.get('is_active') and days and int(plan.get('price') or 0)>0:
                    result.append({'product':product['name'],'product_id':str(product['id']),'plan_id':str(plan['id']),
                        'plan_name':plan['name'],'days':int(days),'allocated':int(plan['price'])})
    return result


async def show_card(message,context,data):
    r=data['request'];state=r['state'];status=r['status'];a=data['account'];occ=data['occupancy']
    text=f"🔗 تسليم ChatGPT\nالزبون: {r['customer_name']} ({r['customer_id']})\nالحساب: {a['email']}\nالسعة: {a['capacity']} | مشغول: {occ['seats']} | محجوز: {occ['held']}\nأشخاص مرتبطون: {occ['people']}"
    if occ['unreviewed']:text+=f"\n⚠️ {occ['unreviewed']} رابط قديم يحجز مقعداً حتى تُراجع مدته."
    rows=[]
    if status=='complete':
        result=r.get('result') or {};text+=f"\n✅ التسليم والربط مثبتان.\nمعرف العملية: {r['id']}\nينتهي: {display_date(result.get('ends_at'))}"
        active=[c for c in data.get('contracts',[]) if c['status']=='active' and datetime.fromisoformat(c['ends_at'].replace('Z','+00:00'))>datetime.now(timezone.utc)]
        if data.get('contracts') is not None:text+=f"\nلهذا الزبون: {len(active)} اشتراك فعال على {len({c['account_id'] for c in active})} حساب مختلف."
        if result.get('undone'):text+='\n↩️ تم التراجع عن تغيير الاشتراك، والأموال محفوظة كما هي.'
        elif state.get('mode')=='adopt':text+='\nاعتماد رابط قديم بمدته الموجودة؛ لم تُرسل رسالة حساب جديدة.'
        else:
            rows.append([('↩️ مراجعة التراجع','undo_review')])
            rows.append([('🔄 إعادة محاولة جدولة المتابعة','followup')])
    elif status in ('sending','uncertain'):
        text+='\n⚠️ نتيجة الإرسال غير مؤكدة. تحقق من محادثة الزبون؛ لا تعيد الإرسال تلقائياً.'
        rows=[[('وصلت البيانات: أكمل التسجيل','received')],[('لم تصل: اسمح بمحاولة جديدة','not_received')]]
    elif status=='sent':
        text+='\n✅ إرسال البيانات مؤكد؛ بقي إكمال التسجيل دون إرسال جديد.';rows=[[('إكمال التسجيل','finish')]]
    elif status=='reserved':
        text+=f"\nنوع العملية: {state.get('mode')}\nالتمويل: {state.get('funding_id') or 'لا تغيير مالي'}\nالأيام: {state.get('days','تُحفظ المدة')}\nالمقاعد: {state.get('seats')}\nالحجز صالح 15 دقيقة."
        expired=datetime.fromisoformat(r['hold_until'].replace('Z','+00:00'))<=datetime.now(timezone.utc)
        rows=[[('🔄 انتهى الحجز: راجع واحجز مجدداً','reserve')]] if expired else [[('✅ تسليم وتثبيت','send')]]
        rows += [[('◀️ رجوع وتحرير الحجز','release'),('إلغاء','cancel')]]
    elif status=='cancelled':text+='\nأُلغيت العملية قبل التسليم.'
    else:
        step=state.get('step','purpose')
        if step=='purpose':
            rows=[[('➕ اشتراك إضافي مستقل','mode_new')]]
            if data['contracts']:
                rows += [[('🔄 تجديد اشتراك محدد','mode_renew')],[('🔁 استبدال حساب اشتراك','mode_replace'),('💝 تعويض','mode_comp')],[('📨 إعادة إرسال بلا مدة جديدة','mode_resend')]]
            rows += [[('💳 المشتري/الدافع شخص آخر','payer')]]
            for old in data.get('legacy_reminders',[]):
                rows.append([(f"مراجعة اشتراك قديم {old['id']} — ينتهي {old['ends_at'][:10]}",'import_'+str(old['id']))])
            text+='\nاختر الغرض؛ الاشتراك المنتهي لا يُعد فعالاً، ويمكن تجديده بالاختيار.'
        elif step=='target':
            text+='\nاختر الاشتراك المقصود:'
            for c in data['contracts']:
                rows.append([(f"{str(c['id'])[:8]} — {display_date(c['ends_at'])} — {c['seats']} مقعد",'target_'+compact(c['id']))])
        elif step=='funding':
            text+='\nاختر الدفعة؛ كل تخصيص مستقل ولا نعيد استخدام المبلغ:'
            for f in data['funding']:
                if f['available']>0 and _services.is_chatgpt(f.get('product')):
                    rows.append([(f"{f['product']} — متاح {f['available']:,} — {f['id'][-8:]}",'fund_'+funding_token(f['id']))])
            rows.append([('💳 تسجيل دين صريح عند التثبيت','debt')])
        elif step=='plan':
            text+='\nاختر الباقة والمدة:'
            for p in catalog_options():rows.append([(f"{p['plan_name']} — {p['allocated']:,}",'plan_'+compact(p['plan_id']))])
        elif step in ('days','payer','seats'):
            text+='\n'+{'days':'اكتب مدة التعويض بالأيام (1 إلى 3650).','payer':'اكتب chat_id المشتري؛ المستفيد يبقى الزبون الحالي.','seats':'اكتب عدد المقاعد لهذا الاشتراك (1 إلى 20).'}[step]
            context.user_data['delivery_input']={'id':r['id'],'revision':r['revision'],'step':step}
        elif step=='summary':
            text+=f"\nنوع العملية: {state.get('mode')}\nالباقة: {state.get('plan_name','المدة الحالية')}\nالأيام: {state.get('days','تبقى كما هي')}\nالمبلغ المخصص: {state.get('allocated',0):,}\nالمقاعد: {state.get('seats',1)}\nالدافع: {state.get('payer_id',r['customer_id'])}"
            rows=[[('✅ مراجعة وحجز السعة','reserve')]]
            if state.get('mode')=='new':rows.append([('تعديل عدد المقاعد','seats')])
        rows += [[('◀️ رجوع','back'),('إلغاء','cancel')]]
    await message.reply_text(text,reply_markup=markups(r,rows) if rows else None)


async def start_link(context,bm,chat_id,text):
    match=re.fullmatch(r'/link\s+(\S+)',text.strip(),re.I)
    if not match or not _services:return False
    # All /link commands disappear before reads, including old account paths.
    if bm is not None:
        try:await context.bot.delete_business_messages(business_connection_id=bm.business_connection_id,message_ids=[bm.message_id])
        except Exception:pass
    if bm is None:raise FinanceError('استخدم /link داخل محادثة المستفيد.')
    try:
        data=await asyncio.to_thread(rpc,'start',id=str(uuid.uuid4()),code=match[1],customer_id=chat_id,
            customer_name=bm.chat.full_name or bm.chat.first_name or str(chat_id),business_id=bm.business_connection_id,message_id=bm.message_id)
    except FinanceError as exc:
        # Explicitly missing extension leaves untouched legacy behavior. Native
        # credentials are not auto-sent through an uninstalled workflow.
        if 'supabase_account_delivery.sql' in str(exc):return False
        await context.bot.send_message(chat_id=_services.owner_id,text='⚠️ '+str(exc));return True
    if not data.get('managed'):return False
    if data['request']['status']=='draft' and not data['request']['state'] and not data['contracts'] and not data.get('legacy_reminders'):
        state={'mode':'new','step':'funding','seats':1,'payer_id':chat_id}
        eligible=[f for f in data['funding'] if f['available']>0 and _services.is_chatgpt(f.get('product'))]
        if len(eligible)==1:
            f=eligible[0];plans=catalog_options()
            matching=[p for p in plans if p['plan_id']==f['payload'].get('plan_id') and p['allocated']<=f['available']]
            if not matching:matching=[p for p in plans if p['allocated']==f['available']]
            if len(matching)==1:state.update(matching[0],funding_id=f['id'],available=f['available'],step='summary')
        data=await asyncio.to_thread(rpc,'edit',id=data['request']['id'],revision=data['request']['revision'],state=state)
    await show_card(SimpleMessage(context),context,data);return True


class SimpleMessage:
    def __init__(self,context):self.context=context
    async def reply_text(self,text,**kwargs):
        return await self.context.bot.send_message(chat_id=_services.owner_id,text=text,**kwargs)


async def finish_delivery(context,message,data):
    r=data['request']
    result=await asyncio.to_thread(rpc,'finish',id=r['id'])
    await show_card(message,context,result)
    if r['state'].get('mode')=='resend':return
    await retry_followup(context,message,result)


async def retry_followup(context,message,result):
    r=result['request']
    contract_id=(result['request'].get('result') or {}).get('contract_id')
    # Follow-up is targeted to this reminder, never the customer's latest one.
    reminder=(result['request'].get('result') or {}).get('reminder_id')
    if reminder:
        try:
            state={'customer_chat_id':r['customer_id'],'reminder_id':reminder,'duration_days':r['state'].get('days') or 1}
            existing=await asyncio.to_thread(lambda:_services.client.table('subscription_reminders').select('scheduled_message_id,status').eq('id',reminder).eq('customer_chat_id',r['customer_id']).execute().data)
            if existing and existing[0].get('scheduled_message_id'):
                await _services.cancel_schedule(r['customer_id'],existing[0]['scheduled_message_id'])
                await asyncio.to_thread(lambda:_services.client.table('subscription_reminders').update({'scheduled_message_id':None,'scheduled_message_status':'none'}).eq('id',reminder).execute())
            if existing and existing[0]['status']=='cancelled':return
            ok=await _services.schedule(state)
            if not ok:await message.reply_text('⚠️ المدة والربط محفوظان؛ جدولة المتابعة تحتاج إعادة محاولة من /delivery.')
            await asyncio.to_thread(_services.invite,r['customer_id'],r['business_id'])
        except Exception:await message.reply_text('⚠️ التسليم مثبت؛ متابعة الاشتراك تحتاج مراجعة. لا تعد إرسال البيانات.')


async def send_delivery(context,message,data):
    r=data['request']
    claimed=await asyncio.to_thread(rpc,'claim',id=r['id'])
    try:
        creds=await asyncio.to_thread(lambda:_services.client.rpc('delivery_credentials',{'p_owner':_services.owner_id,'p_id':r['id']}).execute().data)
        if not creds:raise FinanceError('تعذرت قراءة بيانات الحساب المحجوز.')
        text=delivery_text(creds['email'],creds['password'],claimed['request']['state'].get('template'))
    except Exception:
        # Before issuing Telegram request, delivery is known not to have happened.
        await asyncio.to_thread(rpc,'resolve_unsent',id=r['id'])
        raise FinanceError('لم أرسل بيانات الدخول؛ افتح العملية وأعد مراجعتها.') from None
    if r['state'].get('mode')=='renew':
        # Renewal changes the period, not credential disclosure.
        sent_data=await asyncio.to_thread(rpc,'sent',id=r['id'])
    else:
        try:
            sent=await context.bot.send_message(chat_id=r['customer_id'],business_connection_id=r['business_id'],text=text,parse_mode='HTML',disable_web_page_preview=True)
        except Exception:
            await message.reply_text('⚠️ نتيجة الإرسال تحتاج تحقق. لم أكررها تلقائياً.')
            await show_card(message,context,await asyncio.to_thread(rpc,'get',id=r['id']));return
        # If this write fails, request stays sending; owner can confirm receipt
        # and finish without sending the password again.
        sent_data=await asyncio.to_thread(rpc,'sent',id=r['id'],message_id=sent.message_id)
    await finish_delivery(context,message,sent_data)


async def handle_delivery_callback(update,context):
    q=update.callback_query
    if q.from_user.id!=_services.owner_id:await q.answer('للأونر فقط.',show_alert=True);return
    await q.answer();context.user_data.pop('delivery_input',None)
    try:
        _,key,revision,action=q.data.split(':',3);rid=expanded(key)
        data=await asyncio.to_thread(rpc,'get',id=rid);r=data['request'];state=dict(r['state'])
        if int(revision)!=r['revision']:
            await show_card(q.message,context,data);return
        if action=='send':await send_delivery(context,q.message,data);return
        if action=='finish':await finish_delivery(context,q.message,data);return
        if action=='followup':await retry_followup(context,q.message,data);return
        if action=='received':
            data=await asyncio.to_thread(rpc,'resolve_sent',id=rid)
            await finish_delivery(context,q.message,data);return
        if action=='not_received':data=await asyncio.to_thread(rpc,'resolve_unsent',id=rid)
        elif action=='cancel':data=await asyncio.to_thread(rpc,'cancel',id=rid)
        elif action=='reserve':data=await asyncio.to_thread(rpc,'reserve',id=rid)
        elif action=='undo_review':
            await q.message.reply_text('التراجع يعكس تغيير الاشتراك والحجز فقط؛ لا يرجع الأموال تلقائياً. بيانات الدخول وصلت وتحتاج تغييرها إذا كان التسليم خاطئاً.',reply_markup=markups(r,[[('تأكيد التراجع','undo')],[('رجوع','reload')]]));return
        elif action=='undo':
            data=await asyncio.to_thread(rpc,'undo',id=rid)
            await retry_followup(context,q.message,data)
            await q.message.reply_text('⚠️ بيانات الدخول سبق إرسالها. حرر الجلسة فعلياً أو غيّر بيانات الدخول قبل إعادة بيع المقعد. لم أغيّر الدفعة أو الدين تلقائياً.')
        elif action=='reload':pass
        else:
            if action=='release':
                data=await asyncio.to_thread(rpc,'release',id=rid)
                await show_card(q.message,context,data);return
            if action.startswith('import_'):
                await q.message.reply_text('هذا يعتمد الاشتراك القديم على الحساب المختار مع الحفاظ على مدته ودفعه. يجب أن يكون هذا هو الحساب المرتبط فعلاً.',
                    reply_markup=markups(r,[[('اعتماد الربط القديم','adopt_'+action[7:])],[('رجوع','reload')]]));return
            if action.startswith('adopt_'):
                data=await asyncio.to_thread(rpc,'import_reminder',id=rid,reminder_id=int(action[6:]))
                await show_card(q.message,context,data);return
            previous=state.get('step','purpose')
            if action.startswith('mode_'):
                state={'mode':action[5:],'step':'funding' if action=='mode_new' else 'target','payer_id':state.get('payer_id',r['customer_id']),'payer_name':state.get('payer_name'),'seats':1}
            elif action.startswith('target_'):
                target=next(c for c in data['contracts'] if compact(c['id'])==action[7:])
                state.update(target=target['id'],target_revision=target['revision'],seats=target['seats'],step='funding' if state['mode']=='renew' else 'days' if state['mode']=='comp' else 'summary')
            elif action.startswith('fund_'):
                f=next(f for f in data['funding'] if funding_token(f['id'])==action[5:])
                if not _services.is_chatgpt(f.get('product')):raise FinanceError('الدفعة لا تخص ChatGPT.')
                state.update(funding_id=f['id'],available=f['available'],step='plan')
            elif action=='debt':state.update(funding_id='debt',step='plan')
            elif action.startswith('plan_'):
                plan=next(p for p in catalog_options() if compact(p['plan_id'])==action[5:]);state.update(plan,step='summary')
                if state['funding_id']!='debt' and state['allocated']>state['available']:raise FinanceError('قيمة الباقة أعلى من المتاح بالدفعة؛ راجع المالية أو اختر تسجيل دين صريح.')
            elif action in ('days','payer','seats'):state['step']=action
            elif action=='back':state['step']=state.get('history',['purpose'])[-1] if state.get('history') else 'purpose';state['history']=state.get('history',[])[:-1]
            else:raise FinanceError('خيار غير معروف.')
            if action!='back' and state.get('step')!=previous:state['history']=[*state.get('history',[]),previous]
            data=await asyncio.to_thread(rpc,'edit',id=rid,revision=r['revision'],state=state)
        await show_card(q.message,context,data)
    except (FinanceError,ValueError,StopIteration) as exc:
        await q.message.reply_text('⚠️ '+(str(exc) if isinstance(exc,FinanceError) else 'الخيار تغير؛ افتح العملية مجدداً من /delivery.'))
    except Exception:
        await q.message.reply_text('⚠️ تعذر إكمال الخطوة. راجع حالة العملية من /delivery؛ لم أكرر الإرسال.')


async def handle_delivery_input(update,context):
    pending=context.user_data.get('delivery_input')
    if not pending:return False
    if not update.effective_user or update.effective_user.id!=_services.owner_id:return False
    text=(update.message.text or '').strip()
    try:
        value=int(text.translate(str.maketrans('٠١٢٣٤٥٦٧٨٩','0123456789')))
        data=await asyncio.to_thread(rpc,'get',id=pending['id']);r=data['request'];state=dict(r['state'])
        if r['revision']!=pending['revision']:raise FinanceError('البطاقة تغيرت؛ أعد فتح العملية.')
        key=pending['step'];limit=3650 if key=='days' else 20 if key=='seats' else 10**16
        if not 1<=value<=limit:raise FinanceError('القيمة خارج الحدود.')
        state['payer_id' if key=='payer' else key]=value;state['step']='purpose' if key=='payer' else 'summary'
        if key=='payer' and _services.identity:
            name,_=await asyncio.to_thread(_services.identity,value)
            state['payer_name']=name or str(value)
        data=await asyncio.to_thread(rpc,'edit',id=r['id'],revision=r['revision'],state=state)
        context.user_data.pop('delivery_input',None);await show_card(update.message,context,data)
    except (ValueError,FinanceError) as exc:await update.message.reply_text('⚠️ '+(str(exc) if isinstance(exc,FinanceError) else 'اكتب رقماً صحيحاً.'))
    return True


async def show_admin(message,context,page=0):
    from finance_ui import suspend_finance_inputs
    from debt_manager import cancel_debt_input
    cancel_debt_input(context)
    await asyncio.to_thread(suspend_finance_inputs)
    for key in ('statistics_input','finance_report_input','pending_catalog_input','pending_payment_input','pending_shared_account','campaign_draft','pending_manual_subscription','pending_personal_reminder'):
        context.user_data.pop(key,None)
    data=await asyncio.to_thread(rpc,'list')
    text='🔗 تسليم الحسابات\nاستخدم /link رمز_الحساب داخل محادثة المستفيد.\n'
    rows=[[('➕ تخزين/تجهيز حساب للتسليم','deliveryadmin:add')],[('📝 معاينة وتعديل روابط القالب','deliveryadmin:template')],[('📄 ملف تهيئة التسليم','deliveryadmin:sql')]]
    for a in data['accounts']:
        text+=f"\n{a['email']}\n/link {a['code']}\n{'فعال' if a['enabled'] else 'متوقف'} | السعة {a['capacity']} | المشغول {a['occupancy']['seats']} | المحجوز {a['occupancy']['held']}\n"
        rows.append([('إدارة '+a['email'][:45],'deliveryadmin:account:'+compact(a['id']))])
    # Telegram length is bounded per page; every account is kept accessible.
    for i in range(0,len(text),3500):await message.reply_text(text[i:i+3500])
    for r in data['pending']:
        rows.append([(f"{r['customer_id']} — {r['status']}",f"deliveryadmin:open:{compact(r['id'])}")])
    history=data.get('history',[]);page=max(0,min(page,max(0,(len(history)-1)//10)))
    for r in history[page*10:(page+1)*10]:
        if r['status']=='complete':rows.append([(f"سجل {r['customer_id']} — {r['created_at'][:10]}",f"deliveryadmin:open:{compact(r['id'])}")])
    nav=[]
    if page:nav.append(('السابق','deliveryadmin:history:'+str(page-1)))
    if (page+1)*10<len(history):nav.append(('التالي','deliveryadmin:history:'+str(page+1)))
    if nav:rows.append(nav)
    for offset in range(0,len(rows),40):
        await message.reply_text('العمليات المعلقة محفوظة بعد إعادة التشغيل. الحسابات القديمة لا تُنقل مددها بالتخمين.',reply_markup=keyboard(rows[offset:offset+40]))


async def handle_admin_callback(update,context):
    q=update.callback_query
    if q.from_user.id!=_services.owner_id:await q.answer('للأونر فقط.',show_alert=True);return
    await q.answer();action=q.data[len('deliveryadmin:'):]
    try:
        context.user_data.pop('delivery_input',None)
        context.user_data.pop('delivery_admin_input',None)
        if action=='sql':
            with Path(__file__).with_name('supabase_account_delivery.sql').open('rb') as document:
                await context.bot.send_document(chat_id=_services.owner_id,document=document,caption='شغّل ملف تسليم الحسابات بعد ملفات المالية والاشتراكات وخزينة ChatGPT. لا تعِد تهيئة المالية.')
        elif action in ('add','template'):
            context.user_data['delivery_admin_input']=action
            await q.message.reply_text('أرسل خمسة أسطر:\nالإيميل\nكلمة المرور\nمفتاح TOTP الطويل\nعدد المقاعد\nرمز الربط الإداري' if action=='add' else 'أرسل سطرين: رابط طريقة التسجيل ثم رابط القناة.',reply_markup=keyboard([[('إلغاء ورجوع','deliveryadmin:home')]]))
        elif action=='home':
            context.user_data.pop('delivery_admin_input',None);context.user_data.pop('delivery_admin_preview',None)
            await show_admin(q.message,context)
        elif action.startswith('history:'):
            await show_admin(q.message,context,page=int(action[8:]))
        elif action.startswith(('account:','toggle:','capacity:','credentials:')):
            task,key=action.split(':',1);aid=expanded(key)
            accounts=(await asyncio.to_thread(rpc,'list'))['accounts'];a=next(x for x in accounts if x['id']==aid)
            if task=='toggle':
                await asyncio.to_thread(rpc,'account',id=aid,revision=a['revision'],enabled=not a['enabled'])
                await show_admin(q.message,context);return
            if task=='capacity':
                context.user_data['delivery_admin_input']='capacity'
                context.user_data['delivery_admin_account']={'id':aid,'revision':a['revision']}
                await q.message.reply_text('أرسل السعة الجديدة (1 إلى 20). لا يمكن جعلها أقل من المقاعد المشغولة والمحجوزة.',reply_markup=keyboard([[('رجوع','deliveryadmin:account:'+key)]]));return
            if task=='credentials':
                context.user_data['delivery_admin_input']='credentials'
                context.user_data['delivery_admin_account']={'id':aid,'revision':a['revision'],'email':a['email'],'capacity':a['capacity'],'code':a['code']}
                await q.message.reply_text(f"هذا الحساب مرتبط بـ{a['occupancy']['people']} شخص. التغيير لا يرسل بيانات جديدة لهم تلقائياً ولا يعيد المدد.\nأرسل سطرين: كلمة المرور الجديدة ثم مفتاح TOTP.\nأو ثلاثة أسطر لتعديل الإيميل أيضاً: الإيميل ثم كلمة المرور ثم المفتاح.\nإذا كلمة المرور ما تغيرت اكتب الحالية.",reply_markup=keyboard([[('رجوع','deliveryadmin:account:'+key)]]));return
            rows=[[('إيقاف/تفعيل التسليم والأكواد','deliveryadmin:toggle:'+key)],[('تعديل السعة','deliveryadmin:capacity:'+key)],[('تحديث كلمة المرور وTOTP','deliveryadmin:credentials:'+key)]]
            holds=(await asyncio.to_thread(rpc,'list')).get('legacy_holds',[])
            for hold in holds:
                if hold['account_id']==aid:rows.append([('مراجعة تحرير وصول '+str(hold['customer_id']),f"deliveryadmin:logout:{key}:{hold['customer_id']}")])
            await q.message.reply_text(f"{a['email']}\nالسعة {a['capacity']}\nوصول قديم/مكشوف يحتاج مراجعة {a['occupancy']['unreviewed']}",
                reply_markup=keyboard([*rows,[('رجوع','deliveryadmin:home')]]))
        elif action.startswith(('logout:','free:')):
            task,key,customer=action.split(':',2);aid=expanded(key)
            if task=='logout':
                await q.message.reply_text('هل تأكدت من خروج هذا الزبون فعلياً أو تغيير بيانات الدخول؟ هذا يحرر الحجز غير المراجع فقط، ولا يلغي اشتراكاً فعالاً أو يغير الأموال.',
                    reply_markup=keyboard([[('نعم، تأكدت من إنهاء الوصول',f'deliveryadmin:free:{key}:{customer}')],[('رجوع','deliveryadmin:account:'+key)]]));return
            await asyncio.to_thread(rpc,'release_access',id=aid,customer_id=int(customer))
            await show_admin(q.message,context)
        elif action.startswith('save:'):
            preview=context.user_data.get('delivery_admin_preview')
            if not preview or preview.get('token')!=action[5:]:raise FinanceError('المعاينة انتهت أو تغيرت؛ افتح آخر معاينة.')
            saved=await asyncio.to_thread(rpc,{'add':'register','template':'save_template','capacity':'account','credentials':'credentials'}[preview['kind']],**preview['data'])
            context.user_data.pop('delivery_admin_preview',None)
            await q.message.reply_text('✅ تم الحفظ.'+(f"\n/link {saved['code']}" if saved.get('code') else ''))
            await show_admin(q.message,context)
        elif action.startswith('open:'):
            await show_card(q.message,context,await asyncio.to_thread(rpc,'get',id=expanded(action[5:])))
        else:raise FinanceError('خيار غير معروف.')
    except (FinanceError,ValueError,StopIteration) as exc:
        await q.message.reply_text('⚠️ '+(str(exc) if isinstance(exc,FinanceError) else 'الخيار تغير؛ أعد فتح القائمة.'),reply_markup=keyboard([[('📄 ملف التهيئة','deliveryadmin:sql')],[('رجوع','deliveryadmin:home')]]))


async def handle_admin_input(update,context):
    if not _services or not update.effective_user or update.effective_user.id!=_services.owner_id:return False
    msg=update.message;text=(msg.text or '').strip() if msg else ''
    if text.lower()=='/delivery':
        context.user_data.pop('delivery_input',None);context.user_data.pop('delivery_admin_input',None)
        try:await show_admin(msg,context)
        except FinanceError as exc:await msg.reply_text('⚠️ '+str(exc),reply_markup=keyboard([[('ملف تهيئة التسليم','deliveryadmin:sql')]]))
        return True
    mode=context.user_data.get('delivery_admin_input')
    if not mode:return await handle_delivery_input(update,context)
    try:
        lines=text.splitlines()
        if mode=='add':
            if len(lines)!=5:raise FinanceError('أرسل خمسة أسطر فقط، كما بالمثال.')
            data=validate_account(dict(zip(('email','password','secret','capacity','code'),lines)))
            preview=f"تجهيز الحساب: {data['email']}\nالسعة: {data['capacity']}\nالرمز: {data['code']}\nكلمة المرور ومفتاح TOTP محفوظان ولا يظهران بالإشعارات.\nإذا كان الحساب موجوداً ببيانات مختلفة، لن أستبدلها تلقائياً."
            try:await msg.delete()
            except Exception:pass
        elif mode=='credentials':
            if len(lines) not in (2,3):raise FinanceError('أرسل كلمة المرور ثم مفتاح TOTP؛ ويمكن إضافة الإيميل كسطر أول.')
            account=context.user_data['delivery_admin_account']
            checked=validate_account({**account,'email':lines[0] if len(lines)==3 else account['email'],'password':lines[-2],'secret':lines[-1]})
            data={'id':account['id'],'revision':account['revision'],'email':checked['email'],'password':checked['password'],'secret':checked['secret']}
            preview=f"تحديث بيانات {account['email']} إلى {checked['email']} مع الحفاظ على الربط والمدد. لا تُرسل بيانات للزبائن تلقائياً."
            try:await msg.delete()
            except Exception:pass
        elif mode=='capacity':
            value=int(text)
            if not 1<=value<=20:raise FinanceError('السعة بين 1 و20.')
            data={**context.user_data['delivery_admin_account'],'capacity':value}
            preview=f'تغيير السعة إلى {value}، دون تعديل اشتراكات الزبائن.'
        else:
            if len(lines)!=2 or any(not re.fullmatch(r'https://t\.me/\S+',line) for line in lines):raise FinanceError('أرسل رابطين صالحين من تليجرام، كل واحد بسطر.')
            data={'guide_url':lines[0],'channel_url':lines[1]}
            preview=delivery_text('example@example.com','كلمة مرور تجريبية',data)
        token=compact(uuid.uuid4())
        context.user_data['delivery_admin_preview']={'kind':mode,'data':data,'token':token}
        context.user_data.pop('delivery_admin_input',None)
        await msg.reply_text(preview,parse_mode='HTML' if mode=='template' else None,disable_web_page_preview=True,
             reply_markup=keyboard([[('✅ اعتماد','deliveryadmin:save:'+token)],[('رجوع','deliveryadmin:home'),('إلغاء','deliveryadmin:home')]]))
    except (FinanceError,ValueError) as exc:await msg.reply_text('⚠️ '+(str(exc) if isinstance(exc,FinanceError) else 'السعة يجب أن تكون رقماً.'))
    return True


def code_permission(customer_id,account_id):
    """None = unmanaged legacy. Missing extension preserves existing service;
    connectivity/auth failures cannot grant an account permission."""
    if not _services:return None
    try:
        result=_services.client.rpc('delivery_code_permission',{'p_customer':customer_id,'p_legacy':str(account_id)}).execute().data
        return bool(result['allowed']) if result['managed'] else None
    except Exception as exc:
        if str(getattr(exc,'code',''))=='PGRST202':return None
        return False


def funding_managed(oid):
    if not _services:return False
    try:return bool(_services.client.rpc('delivery_funding_managed',{'p_owner':_services.owner_id,'p_funding':oid}).execute().data)
    except Exception as exc:
        if str(getattr(exc,'code',''))=='PGRST202':return False
        raise FinanceError('تعذر فحص تخصيص الدفعة للتسليم؛ لم أفعّل اشتراكاً آخر.') from None


def reminder_managed(reminder_id):
    if not _services:return False
    try:return bool(_services.client.rpc('delivery_reminder_managed',{'p_reminder':reminder_id}).execute().data)
    except Exception as exc:
        if str(getattr(exc,'code',''))=='PGRST202':return False
        raise FinanceError('تعذر فحص ارتباط المتابعة؛ لم أغيّر مدة الاشتراك.') from None


def account_customers(account_id):
    if not _services:return None
    try:
        data=_services.client.rpc('delivery_account_customers',{'p_account':str(account_id)}).execute().data
        return [int(c) for c in data['customers']] if data['managed'] else None
    except Exception as exc:
        if str(getattr(exc,'code',''))=='PGRST202':return None
        raise FinanceError('تعذر قراءة المشتركين الفعالين للحساب؛ لم أعتبر القائمة فارغة.') from None


def account_resource(account_id):
    if not _services:return None
    raw=str(account_id).removeprefix('legacy:').removeprefix('native:')
    try:
        data=_services.client.rpc('delivery_account_customers',{'p_account':raw}).execute().data
        return data['resource'] if data['managed'] else None
    except Exception as exc:
        if str(getattr(exc,'code',''))=='PGRST202':return None
        raise FinanceError('تعذر قراءة حجز الحساب؛ لم أعتبره فارغاً.') from None
