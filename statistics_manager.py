"""Owner statistics UI. Frozen snapshots, reusable filters and read-only drilldown."""
from runtime_scheduler import run_io, background_job
from pathlib import Path
import logging
import re
from financial_ledger import FinanceError, source_id
from finance_ui import services, keyboard
from statistics_data import read_snapshot
from statistics_engine import build_report, LABELS, contribution
from statistics_operations import operational_report

logger=logging.getLogger(__name__)
PAGE=5
NAMES={'today':'اليوم','yesterday':'أمس','week':'الأسبوع','month':'الشهر','all':'كل الوقت'}


def amount(value): return 'غير متاح' if value is None else f'{value:,}'


def summary_text(report):
    t=report['totals'];p=report['positions'];q=report['quality']
    sales=amount(t['sales'])+(' (المعروف فقط)' if q['unknown_sales'] else '')
    text=(f"📊 الإحصائيات — {NAMES.get(report['filters']['period'],report['filters']['period'])}\n"
          f"بتوقيت بغداد | قراءة: {report['as_of'].strftime('%Y-%m-%d %H:%M:%S')}\n\n"
          f"المقبوض بعد التصحيحات: {t['received']:,}\nالخارج بعد التصحيحات: {t['spent']:,}\n"
          f"صافي حركة الأموال: {t['received']-t['spent']:,}\nالمبيعات: {sales}\n"
          f"المصاريف: {t['expenses']:,}\nالمسترجعات الفعلية: {t['refunds']:,}\n"
          f"المبيعات بعد المسترجعات: {t['net_sales']:,}\n"
          f"صافي عدد المبيعات: {t['sales_count']} | زبائن المبيعات المعروفة: {report['buyers']}\n"
          f"أول بيع معروف: {report['new_buyers']} | سبق لهم شراء معروف: {report['returning']}\n\n"
          f"الديون الحالية: {p['debt_current']:,}\nأرصدة محفوظة للزبائن الآن: {p['credit_current']:,}")
    if t['reversal_count']:text+=f"\nتراجعات القيود: {t['reversal_count']} | قبض مصحح: {t['receipt_corrections']:,} | صرف مصحح: {t['spend_corrections']:,}"
    if q['unknown_sales']:text+=f"\n⚠️ {q['unknown_sales']} حركة قديمة بقيمة مقبوض {q['unknown_receipts']:,}؛ تصنيف البيع غير معروف."
    if q['undated']:text+=f"\n⚠️ {q['undated']} حركة بلا تاريخ؛ تدخل في كل الوقت فقط."
    if q['balance_mismatches']:text+=f"\n⚠️ {q['balance_mismatches']} رصيد لا يطابق قيوده؛ راجع تدقيق الخزن قبل الاعتماد."
    if q['unidentified_sales']:text+=f"\n⚠️ {q['unidentified_sales']} بيع بدون هوية زبون؛ لا يُضاف لعدد الأشخاص بالتخمين."
    if not report['snapshot'].get('statistics_extension'):text+='\n⚠️ تفاصيل التشغيل تحتاج ملف إضافة الإحصائيات.'
    active=[f'{k}: {v}' for k,v in report['filters'].items() if k!='period' and v]
    if active:text+='\nالفلاتر: '+' | '.join(active)
    return text


def buttons(rows,back='home'):
    return keyboard([*[[ (label,'stats2:'+action) for label,action in line] for line in rows],
                     [('◀️ رجوع','stats2:'+back)]])


def session(context):
    s=context.user_data.get('statistics_session')
    if not s:raise FinanceError('انتهت جلسة التقرير؛ افتح الإحصائيات للحصول على قراءة جديدة.')
    return s


@background_job
async def open_statistics(message, context, *, page='home', period=None):
    from finance_ui import suspend_finance_inputs
    from debt_manager import cancel_debt_input
    cancel_debt_input(context)
    for key in ('pending_catalog_input','pending_payment_input','pending_shared_account','campaign_draft','pending_manual_subscription','pending_personal_reminder'):
        context.user_data.pop(key,None)
    (await run_io(lambda: suspend_finance_inputs()))
    context.user_data.pop('finance_report_input',None)
    context.user_data.pop('statistics_input',None)
    try:
        snapshot=(await run_io(lambda: read_snapshot()))  # Replace the session only after a complete read.
    except FinanceError as exc:
        await message.reply_text(f'⚠️ {exc}',reply_markup=buttons([[('إعادة القراءة','open'),('ملف إضافة الإحصائيات','sql')]]))
        return False
    filters=dict(context.user_data.get('statistics_filters',{'period':'today'}))
    if period:filters['period']=period
    context.user_data['statistics_session']={'snapshot':snapshot,'filters':filters}
    await render(message,context,page)
    return True


def pages(rows,page):
    page=min(max(0,int(page)),max(0,(len(rows)-1)//PAGE))
    return page,rows[page*PAGE:(page+1)*PAGE]


def pager(rows,page,action):
    nav=[]
    if page:nav.append(('السابق',f'{action}:{page-1}'))
    if (page+1)*PAGE<len(rows):nav.append(('التالي',f'{action}:{page+1}'))
    return [nav] if nav else []


async def render(message,context,view='home'):
    s=session(context);r=build_report(s['snapshot'],s['filters']);context.user_data['statistics_filters']=dict(s['filters'])
    s['view']=view
    op=operational_report(s['snapshot'],r['as_of']);t=r['totals'];rows=[];back='home'
    parts=view.split(':');action=parts[0];page=int(parts[1]) if len(parts)>1 and parts[1].isdigit() else 0
    if action=='home':
        text=summary_text(r)
        rows=[[('📅 الفترة والفلاتر','filters'),('🔎 تفاصيل الأرقام','metrics')],
              [('📦 المنتجات','products:0'),('🧾 الباقات','plans:0')],
              [('💳 الديون والأرصدة','debts'),('🏦 الخزن','wallets:0')],
              [('📨 مصادر المبيعات','sources:0'),('📊 المقارنة','compare')],
              [('👥 الاشتراكات والحسابات','subscriptions'),('🎁 الدعوات والحملات','rewards')],
              [('⚠️ تحتاج انتباهك','attention:0'),('💸 تفصيل المصاريف','expenses')],
              [('📥 تصدير نفس القراءة','export'),('🔄 قراءة جديدة','refresh')]]
        await message.reply_text(text,reply_markup=keyboard([[ (label,'stats2:'+a) for label,a in line] for line in rows]));return
    if action=='filters':
        text='الفترة الحالية: '+NAMES.get(s['filters']['period'],s['filters']['period'])+'\nاختياراتك تبقى محفوظة أثناء التنقل. أرصدة الخزن والالتزامات تعرض إجمالياً مع توضيح ذلك.'
        rows=[[('اليوم','period:today'),('أمس','period:yesterday')],[('الأسبوع','period:week'),('الشهر','period:month')],
              [('كل الوقت','period:all'),('فترة مخصصة','custom')],[('اختيار المنتج','filterproducts:0'),('اختيار الباقة','filterplans:0')],
              [('تيليجرام','source:telegram'),('إنستغرام','source:instagram'),('مصدر غير محدد','source:unknown')],
              [('حسب الزبون','customer'),('مسح الفلاتر الإضافية','clear')]]
    elif action in {'products','plans','sources','filterproducts','filterplans'}:
        dimension='products' if 'product' in action else 'plans' if 'plan' in action else 'sources'
        if action.startswith('filter'):
            f=dict(s['filters']);f.pop('product' if dimension=='products' else 'plan',None)
            groups=build_report(s['snapshot'],f)[dimension];back='filters'
        else:groups=r[dimension]
        page,shown=pages(groups,page);text=f"📦 {dimension} — {len(groups)} مجموعة\nالفترة: {NAMES.get(s['filters']['period'],s['filters']['period'])}"
        for g in shown:
            m=g['metrics'];text+=f"\n\n{g['name'][:100]}\nمبيعات: {m['sales']:,} | عدد: {m['sales_count']}\nمقبوض: {m['received']:,} | مصاريف: {m['expenses']:,}\nتسديد دين: {m['debt_paid']:,} | مسترجع: {m['refunds']:,}"
            if action.startswith('filter'):
                prefix='product' if dimension=='products' else 'plan'
                rows.append([('اختيار '+g['name'][:55],prefix+':'+source_id('K',g['key']))])
        rows+=pager(groups,page,action)
        if not groups:text+='\nلا توجد حركات مطابقة.'
    elif action=='metrics':
        text='اختَر رقماً لعرض العمليات التي كوّنته. القيم بعد التصحيحات؛ التراجع يظهر بقيمة عكسية.'
        rows=[[(f'{LABELS[k]}: {amount(t[k])}','detail:'+k+':0')] for k in LABELS]
        # Keep Telegram message keyboards manageable; values are compact.
    elif action=='detail':
        metric=parts[1];page=int(parts[2]);back='metrics'
        if metric not in LABELS:raise FinanceError('مؤشر غير معروف.')
        events=[e for e in r['events'] if contribution(e,s['filters'])[metric]]
        page,shown=pages(events,page);text=f"{LABELS[metric]}: {t[metric]:,}\nعمليات مكوِّنة: {len(events)}"
        for e in shown:
            text+=f"\n\n{e['id']}\n{e['record'].get('occurred_at') or 'تاريخ غير معروف'}\nالقيمة: {contribution(e,s['filters'])[metric]:+,}"
            rows.append([('تفاصيل '+e['id'][-12:],'event:'+source_id('E',e['id']))])
        rows+=pager(events,page,'detail:'+metric)
    elif action=='event':
        e=next((e for e in r['all_events'] if source_id('E',e['id'])==parts[1]),None)
        if e is None:raise FinanceError('العملية خارج فلاتر هذه القراءة.')
        text=f"🧾 {e['id']}\nالنوع: {e['record']['kind']}\nتاريخ العملية: {e['record'].get('occurred_at') or 'غير معروف'}\nتاريخ التسجيل: {e['record'].get('recorded_at') or 'غير معروف'}\nالزبون: {e['customer'] or 'غير محدد'}\nالمصدر: {e['source']}"
        for k,v in e['metrics'].items():
            if v:text+=f'\n{LABELS[k]}: {v:+,}'
        text+='\nتفاصيل العملية كاملة، مستقلة عن فلاتر الملخص. هذه شاشة قراءة؛ التعديل من سجل المالية.';back=s.get('event_back','metrics')
    elif action=='wallets':
        p=r['positions'];page,shown=pages(p['wallets'],page);text='🏦 الخزن — إجمالي المتجر، مستقلة عن فلتر المنتج والمصدر'
        for w in shown:
            text+=f"\n\n{w['key'][7:]}\nبداية الفترة: {amount(w['opening'])}\nداخل: {w['incoming']:,} | خارج: {w['outgoing']:,}\nصافي تحويلات: {w['transfers']:+,} | تسويات: {w['adjustments']:+,}\nنهاية الفترة: {amount(w['closing'])}\nالآن: {w['current']:,}"
        text+='\n\nبداية الفترات السابقة لاعتماد الأرصدة غير متاحة، ولا يُعاد بناؤها بالتخمين.'
        rows+=pager(p['wallets'],page,'wallets')
    elif action=='debts':
        p=r['positions'];text=(f"💳 حركة الديون ضمن الفلاتر\nجديد: {t['debt_new']:,}\nمسدد: {t['debt_paid']:,}\nرصيد مضاف: {t['credit_added']:,}\nرصيد مستخدم: {t['credit_used']:,}\n\n"
            f"أرصدة إجمالية لكل المتجر\nديون بداية الفترة: {amount(p['debt_opening'])}\nديون نهايتها: {amount(p['debt_closing'])}\nالمتبقي الآن: {p['debt_current']:,}\nأرصدة الزبائن الآن: {p['credit_current']:,}\nالخزن الآن: {p['cash_current']:,}\nالأرصدة المحفوظة للزبائن التزام، وليست ربحاً.\nالفترات السابقة لاعتماد البداية لا تملك رصيداً تاريخياً موثوقاً.")
    elif action=='compare':
        text='📊 مقارنة بفترة سابقة مماثلة حتى نفس الوقت، بتوقيت بغداد'
        if not r['previous']:text+='\nاختَر فترة محددة أولاً.'
        else:
            for k in ('received','sales','expenses','refunds','sales_count'):
                text+=f"\n{LABELS[k]}: {t[k]:,} | السابقة: {r['previous'][k]:,} | الفرق: {t[k]-r['previous'][k]:+,}"
        text+='\nالبيانات التاريخية الناقصة تؤثر على المقارنة؛ راجع جودة البيانات.'
    elif action in {'expenses','costs'}:
        # Old inline buttons still open the expense breakdown after renaming.
        text=(f"💸 تفصيل المصاريف\nالمصاريف المسجلة: {t['expenses']:,}\n"
              f"شراء منتجات: {t['product_costs']:,}\nتشغيل: {t['operating_expenses']:,}\nرسوم: {t['fee_expenses']:,}\nعام: {t['general_expenses']:,}\nغير مصنف: {t['unknown_expenses']:,}")
    elif action=='subscriptions':
        text='👥 الاشتراكات والحسابات — حالتها وقت القراءة، لكل المتجر'
        sub=op['subscriptions'];acc=op['accounts']
        if sub is None:text+='\nالاشتراكات: المصدر غير متاح.'
        else:
            for k,label in [('active','فعالة حسب المدة'),('expired','منتهية'),('pending_delivery','بانتظار التسليم'),('expiring_3d','تنتهي خلال 3 أيام'),('customers','زبائن الاشتراكات الفعالة'),('unknown','تحتاج مراجعة التاريخ')]:text+=f"\n{label}: {sub.get(k,0)}"
        if acc is None:text+='\nالحسابات: المصدر غير متاح.'
        else:text+=f"\nحسابات خاصة فعلية: {acc['private']}\nحسابات مشتركة فعلية: {acc['shared']}\nروابط زبائن مسجلة: {acc['links']}\nالروابط المسجلة لا تثبت وحدها صلاحية الاشتراك أو شغور المقعد."
        text+='\nلم نغيّر حالة الاشتراكات أو صلاحيات طلب الأكواد.'
        if acc is not None:rows.append([('تفاصيل الحسابات والسعة','accounts:0')])
    elif action=='accounts':
        back='subscriptions';acc=op['accounts'];text='📋 الحسابات الفعلية وروابطها المسجلة'
        if acc is None:text+='\nالمصادر غير مكتملة.'
        else:
            page,shown=pages(acc['rows'],page)
            for a in shown:
                text+=f"\n\n{a['key'][:120]}\nالنوع: {'خاص' if a['private'] else 'مشترك'}\nالسعة المسجلة: {amount(a['capacity'])}\nروابط زبائن مسجلة: {a['customers']}"
            text+='\nالمقاعد المتاحة فعلياً والتكلفة لكل مقعد تحتاج ربط الاشتراك بالحساب؛ لا نستنتجها من عدد الروابط وحده.'
            rows+=pager(acc['rows'],page,'accounts')
    elif action=='rewards':
        text='🎁 الدعوات والحملات — الإجمالي حتى وقت القراءة'
        rewards=op['rewards']
        if rewards is None:text+='\nأحد مصادر الدعوات غير متاح؛ لا يمكن تأكيد الإجمالي.'
        else:
            text+=f"\nضغطوا Start: {rewards['started']}\nعدد الحملات: {rewards['campaigns']}\nعدد رسائل الحملات: {rewards['messages']}\nأشخاص مميزون استُهدفوا: {rewards['campaign_customers']}"
            for group,title in [('invites','دعوات فردية'),('delivery','تسليم الحملات')]:text+='\n'+title+': '+', '.join(f'{k}: {v}' for k,v in rewards[group].items())
    elif action=='attention':
        items=[{'id':e['id'],'reason':'تاريخ غير معروف'} for e in r['undated_events']]+op['attention']
        page,shown=pages(items,page);q=r['quality']
        text=f"⚠️ تحتاج انتباهك\nأرصدة غير مطابقة: {q['balance_mismatches']}\nحركات بلا تاريخ: {q['undated']}\nمبيعات قديمة غير معروفة: {q['unknown_sales']}\nحركات فيها جزء غير مصنف: {q['unclassified']}\nعناصر متابعة: {len(items)}"
        for x in shown:
            text+=f"\n\n{x['id']}\n{x['reason']}"
            if any(e['id']==x['id'] for e in r['all_events']):
                rows.append([('عرض '+x['id'][-12:],'event:'+source_id('E',x['id']))])
        for name,state in op['unavailable'].items():text+=f'\nمصدر {name}: {state}'
        rows+=pager(items,page,'attention');rows.append([('ملف إضافة الإحصائيات','sql')])
    else:raise FinanceError('صفحة إحصائية غير معروفة.')
    await message.reply_text(text,reply_markup=buttons(rows,back))


@background_job
async def handle_statistics_callback(update,context):
    q=update.callback_query
    if q.from_user.id!=services().owner_id:
        await q.answer('للأونر فقط.',show_alert=True);return
    await q.answer();data=q.data[len('stats2:'):]
    try:
        context.user_data.pop('statistics_input',None)
        if data in {'refresh','open'}:await open_statistics(q.message,context);return
        if data=='sql':
            with Path(__file__).with_name('supabase_statistics.sql').open('rb') as document:
                await context.bot.send_document(chat_id=services().owner_id,document=document,caption='إضافة قراءة فقط: شغّل الملف بنفس Supabase ثم اضغط قراءة جديدة. لا تعِد تهيئة المالية.')
            return
        s=session(context)
        if data.startswith('event:'):s['event_back']=s.get('view','metrics')
        if data=='export':
            from statistics_export import export_report
            document=await run_io(lambda: export_report(build_report(s['snapshot'],s['filters'])))
            await context.bot.send_document(chat_id=services().owner_id,document=document,caption='نفس القراءة والفلاتر. بيانات CSV تشمل جميع النتائج دون قص؛ البداية التاريخية المجهولة موضحة.')
            return
        if data in {'custom','customer'}:
            context.user_data['statistics_input']=data
            await q.message.reply_text('اكتب اليوم أو الفترة: 2026-10-27 إلى 2026-10-28' if data=='custom' else 'أرسل chat_id أو instagram:username كما يظهر بالسجل.',reply_markup=buttons([],back='filters'));return
        if data.startswith('period:'):s['filters']['period']=data.split(':',1)[1];data='home'
        if data.startswith('source:'):s['filters']['source']=data.split(':',1)[1];data='home'
        if data=='clear':s['filters']={'period':s['filters']['period']};data='home'
        if data.startswith(('product:','plan:')):
            dimension,token=data.split(':',1);f=dict(s['filters']);f.pop(dimension,None)
            groups=build_report(s['snapshot'],f)['products' if dimension=='product' else 'plans']
            key=next((g['key'] for g in groups if source_id('K',g['key'])==token),None)
            if key is None:raise FinanceError('الخيار غير موجود في هذه القراءة.')
            s['filters'][dimension]=key;data='home'
        await render(q.message,context,data)
    except (FinanceError,ValueError) as exc:
        await q.message.reply_text(f'⚠️ {exc}',reply_markup=buttons([[('قراءة جديدة','open')]]))
    except Exception:
        logger.exception('Statistics unavailable; no financial writes attempted')
        await q.message.reply_text('⚠️ تعذر بناء التقرير؛ لم أعتبر البيانات الناقصة أصفاراً.',reply_markup=buttons([[('قراءة جديدة','open')]]))


async def statistics_input(update,context):
    mode=context.user_data.get('statistics_input')
    if not mode:return False
    if not update.effective_user or update.effective_user.id!=services().owner_id:return False
    text=(update.message.text or '').strip()
    try:
        s=session(context);filters=dict(s['filters']);filters['period' if mode=='custom' else 'customer']=text
        if mode=='customer' and not re.fullmatch(r'-?\d+|instagram:[\w.]+',text):
            raise FinanceError('أرسل معرف الزبون الرقمي أو instagram:username، وليس الاسم الظاهر.')
        build_report(s['snapshot'],filters)  # Preserve previous selection on error.
        s['filters']=filters;context.user_data.pop('statistics_input',None)
        await render(update.message,context)
    except (FinanceError,ValueError) as exc:await update.message.reply_text(f'⚠️ {exc}',reply_markup=buttons([],back='filters'))
    return True
