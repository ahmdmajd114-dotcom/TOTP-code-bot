"""Financial reports and audit trail; unavailable history never becomes zero."""
from __future__ import annotations
from datetime import datetime,timedelta,timezone
from copy import deepcopy

from financial_ledger import FinanceError,ledger,BAGHDAD,KINDS,operation_id,now_iso,transaction_date
from finance_ui import keyboard,menu_keyboard,services


def period_bounds(period):
    now=datetime.now(BAGHDAD);today=now.replace(hour=0,minute=0,second=0,microsecond=0)
    if period=='all':return None
    if period=='today':return today,today+timedelta(days=1)
    if period=='week':return today-timedelta(days=today.weekday()),today+timedelta(days=1)
    if period=='month':return today.replace(day=1),today+timedelta(days=1)
    if len(period)==7:
        start=datetime.strptime(period,'%Y-%m').replace(tzinfo=BAGHDAD)
        end=(start.replace(day=28)+timedelta(days=4)).replace(day=1)
        return start,end
    if ' إلى ' in period:
        a,b=period.split(' إلى ',1);start=datetime.fromisoformat(transaction_date(a));end=datetime.fromisoformat(transaction_date(b))+(timedelta(days=1) if len(b.strip())==10 else timedelta(minutes=1))
        if start>=end:raise FinanceError('بداية الفترة لازم قبل نهايتها.')
        return start,end
    start=datetime.fromisoformat(transaction_date(period))
    return start,start+timedelta(days=1)


def totals(operations,period='all',product=None):
    bounds=period_bounds(period);result=dict(received=0,spent=0,sales=0,expenses=0,refunds=0,legacy=0,undated=0,sales_unknown=0)
    reversed_ids={r['reference_id'] for r in operations if r['kind']=='reversal'}
    by_id={r['id']:r for r in operations}
    for row in operations:
        if product is not None and row.get('product')!=product:continue
        stamp=row.get('occurred_at')
        if stamp:
            stamp=datetime.fromisoformat(stamp.replace('Z','+00:00'))
            if bounds and not bounds[0]<=stamp<bounds[1]:continue
        else:
            result['undated']+=1
            if bounds:continue
        original=by_id.get(row.get('reference_id')) if row['kind']=='reversal' else None
        if original:
            # A reversal is an inverse cash movement on its own date. Do not
            # erase the original from last month's closed report.
            result['received']+=int(original.get('cash_out',0))
            result['spent']+=int(original.get('cash_in',0))
            result['sales']-=int(original.get('sales') or 0)
            result['expenses']-=int(original.get('expense') or 0)
            result['refunds']-=int(original.get('refunded') or 0)
            continue
        result['received']+=int(row.get('cash_in',0));result['spent']+=int(row.get('cash_out',0))
        result['expenses']+=int(row.get('expense',0));result['refunds']+=int(row.get('refunded',0))
        if row.get('sales') is None and row['kind'] in {'legacy_receipt','legacy_instagram'}:result['sales_unknown']+=1
        else:result['sales']+=int(row.get('sales') or 0)
        if row.get('historical'):result['legacy']+=1
    return result


def financial_report(period='today',product=None):
    from statistics_data import read_snapshot
    from statistics_engine import build_report,product_key
    from statistics_manager import summary_text
    snapshot=read_snapshot();filters={'period':period}
    if product is not None:
        filters['product']=product_key({},product,snapshot.get('operational',{}).get('catalog_products',[]))
    return summary_text(build_report(snapshot,filters))


def _record_label(row):
    return f"{KINDS.get(row['kind'], 'سجل قديم')} | {row.get('product') or row.get('customer_id') or 'عام'} | {max(int(row.get('cash_in',0)),int(row.get('cash_out',0))):,}"


async def show_record_search(message,context,text):
    rows=[r for r in ledger().operations() if text.casefold() in str(r['payload']).casefold() or text in str(r['cash_in']) or text in str(r['cash_out'])]
    context.user_data['finance_search_text']=text
    context.user_data['finance_records_return']='finance_records_search_results'
    await message.reply_text(f'نتائج: {len(rows)}'+('؛ أول 20، ضيّق البحث للمزيد.' if len(rows)>20 else ''),reply_markup=menu_keyboard([[(_record_label(r)[:80],'finance_view_'+r['id'])] for r in rows[:20]],back='finance_records_search'))


async def handle_report_menu(data,query,context):
    ledger().require_active()
    if data=='finance_reports':
        from statistics_manager import open_statistics
        await open_statistics(query.message,context)
    elif data=='finance_period_custom':
        context.user_data['finance_report_input']='period'
        await query.message.reply_text('اكتب يوماً مثل 2026-10-28، أو فترة:\n2026-10-16 20:20 إلى 2026-10-23 20:20',reply_markup=menu_keyboard([],back='finance_reports'))
    elif data=='finance_period_product':
        context.user_data['finance_report_input']='product'
        await query.message.reply_text('اكتب اسم المنتج كما يظهر بالكتالوج:',reply_markup=menu_keyboard([],back='finance_reports'))
    elif data.startswith('finance_period_'):
        await query.message.reply_text(financial_report(data[len('finance_period_'):]),reply_markup=menu_keyboard([],back='finance_reports'))
    elif data.startswith('finance_records'):
        if data=='finance_records_search_results':
            await show_record_search(query.message,context,context.user_data.get('finance_search_text',''))
            return
        if data=='finance_records_search':
            context.user_data['finance_report_input']='search';await query.message.reply_text('اكتب اسم الزبون، chat_id، المبلغ أو رقم العملية:',reply_markup=menu_keyboard([],back='finance_records'));return
        page=int(data.split('_')[-1]) if data.split('_')[-1].isdigit() else 0
        rows=sorted(ledger().operations(),key=lambda r:r['recorded_at'],reverse=True)
        context.user_data['finance_records_return']=f'finance_records_{page}'
        # IDs passed in callbacks remain under Telegram's 64 byte limit.
        buttons=[[(_record_label(r)[:80],'finance_view_'+r['id'])] for r in rows[page*8:(page+1)*8] if len(('finance_view_'+r['id']).encode())<=64]
        nav=[]
        if page:nav.append(('السابق',f'finance_records_{page-1}'))
        if (page+1)*8<len(rows):nav.append(('التالي',f'finance_records_{page+1}'))
        if nav:buttons.append(nav)
        buttons.append([('بحث بالزبون أو المبلغ أو الرقم','finance_records_search')])
        await query.message.reply_text(f'سجل العمليات — {len(rows)}',reply_markup=menu_keyboard(buttons))
    elif data.startswith('finance_view_'):
        oid=data[len('finance_view_'):];r=next((r for r in ledger().operations() if r['id']==oid),None)
        if not r:raise FinanceError('لم أجد العملية.')
        p=r['payload'];text=f"{KINDS.get(r['kind'],'سجل قديم')}\nرقم: {oid}\nالزبون: {p.get('customer_name') or r.get('customer_id') or 'عام'}\nالمنتج: {r.get('product') or '—'}\nقبض: {r['cash_in']:,}\nصرف: {r['cash_out']:,}\nوقت العملية: {r.get('occurred_at') or 'غير معروف'}\nوقت التسجيل: {r['recorded_at']}\nالسبب: {p.get('description') or '—'}"
        buttons=[]
        if not r['historical'] and r['kind']!='reversal':
            buttons.append([('إلغاء مالي مرتبط','finance_reverse_'+oid)])
            if r['kind'] in {'sale','expense'}:buttons.append([('تصحيح بعملية بديلة','finance_correct_'+oid)])
        if r['kind']=='sale' and not r['historical']:buttons.append([('استرجاع جزئي أو كامل','finance_refund_'+oid)])
        await query.message.reply_text(text[:4000],reply_markup=menu_keyboard(buttons,back=context.user_data.get('finance_records_return','finance_records')))
    elif data.startswith('finance_reverse_'):
        oid=data[len('finance_reverse_'):]
        context.user_data['finance_report_input']='reverse';context.user_data['finance_reverse_id']=oid
        await query.message.reply_text('اكتب سبب التراجع. بعدها يعرض لك النظام أثره قبل التثبيت:',reply_markup=menu_keyboard([],back='finance_view_'+oid))
    elif data.startswith(('finance_refund_','finance_correct_')):
        correction=data.startswith('finance_correct_');oid=data.split('_',2)[2]
        r=next(r for r in ledger().operations() if r['id']==oid)
        from expense_manager import start_expense,render_expense
        from income_manager import start_income,render_income
        if not correction:
            await start_expense(context,refund=r)
        else:
            start,render=(start_income,render_income) if r['kind']=='sale' else (start_expense,render_expense)
            state=await start(context,customer_id=r.get('customer_id'),customer_name=r['payload'].get('customer_name'))
            state.update({k:v for k,v in r['payload'].items() if k not in {'id','kind','occurred_at','source'}})
            state.update(correction_of=oid,step='summary',kind=r['kind'],return_to='finance_view_'+oid);await render(state,context)
    elif data=='finance_pending':
        rows=(ledger().client.table('finance_outbox').select('*').eq('owner_id',services().owner_id).neq('status','done').order('next_at').limit(20).execute().data or [])
        text='⏳ تحتاج متابعة\n'+('\n'.join(f"{r['operation_id']} — محاولات: {r['attempts']} — مزامنة بالانتظار" for r in rows) if rows else 'لا توجد مزامنة معلقة.')
        drafts=ledger().client.table('finance_drafts').select('state').eq('owner_id',services().owner_id).order('updated_at',desc=True).limit(50).execute().data or []
        drafts=[r['state'] for r in drafts if not r['state'].get('closed') and not r['state'].get('committed')]
        await query.message.reply_text(text+'\n\nبطاقات غير مثبتة: '+str(len(drafts)),reply_markup=menu_keyboard([[(f"{s.get('customer_name') or 'عام'} — {s.get('product') or s['flow']}", 'finance_draft_'+s['draft_id'])] for s in drafts[:15]]))
    elif data.startswith('finance_customers'):
        context.user_data['finance_report_input']='customer';await query.message.reply_text('أرسل chat_id لعرض بطاقة الزبون المالية:',reply_markup=menu_keyboard([]))


async def report_input(update,context):
    key=context.user_data.get('finance_report_input');message=update.message
    if not key or not message or not message.text:return False
    text=message.text.strip()
    try:
        if key=='period':await message.reply_text(financial_report(text),reply_markup=menu_keyboard([],back='finance_reports'))
        elif key=='product':await message.reply_text(financial_report('all',text),reply_markup=menu_keyboard([],back='finance_reports'))
        elif key=='customer':
            cid=int(text);snapshot=ledger().snapshot();rows=[r for r in snapshot['operations'] if r.get('customer_id')==cid];debts=[d for d in snapshot['debts'] if d['customer_id']==cid and int(d['remaining'])>0];a=snapshot['accounts']
            name,_=services().identity(cid)
            await message.reply_text(f"بطاقة {name} ({cid})\nعليه: {sum(int(d['remaining']) for d in debts):,}\nإله رصيد: {a.get('credit:'+str(cid),0):,}\nالعمليات: {len(rows)}",reply_markup=menu_keyboard([[('تسجيل قبض','finance_income')],[('تسوية رصيد الزبون',f'finance_credit_{cid}')]],back='finance_customers'))
        elif key=='search':
            await show_record_search(message,context,text)
        elif key=='reverse':
            oid=context.user_data['finance_reverse_id'];r=next(r for r in ledger().operations() if r['id']==oid)
            # Persist a normal vault-flow card with an explicit inverse summary.
            from finance_ui import draft,display
            s=draft('vault',kind='reversal',reference_id=oid,description=text,step='summary',return_to='finance_view_'+oid,
                    reversal_preview=f"سيعكس القبض {r['cash_in']:,} والصرف {r['cash_out']:,} وتأثير الديون والأرصدة للعملية نفسها.")
            from vault_manager import render_vault
            await render_vault(s,context)
        context.user_data.pop('finance_report_input',None)
    except (FinanceError,ValueError) as exc:await message.reply_text(f'⚠️ {exc}')
    return True


# Existing Sheets path retained until explicit ledger activation.
LEGACY_DEPENDENCIES = ('FinanceError', 'PERIOD_LABELS', 'STATS_PRODUCT_EXPENSE_ALIASES', 'STATS_TIMEZONE', 'active_report', 'datetime', 'get_catalog_products', 'get_expenses_worksheet', 'get_google_sheet', 'get_instagram_sales_worksheet', 'is_debt_payment_row', 'logger', 'match_catalog_products', 'normalize_arabic_text', 'parse_amount', 'parse_sheet_amount', 'payment_datetime', 'public_catalog_product_key', 're', 'timedelta')

def configure_legacy(values):
    """Inject store adapters; feature modules never import bot.py."""
    globals().update({name: values[name] for name in LEGACY_DEPENDENCIES})

def calculate_income_report() -> str:
    """
    يقرأ كل صفوف الشيت ويحسب دخل اليوم، الأسبوع الحالي، والشهر الحالي.
    يرجع نص جاهز للعرض، أو رسالة خطأ واضحة لو فشل الاتصال بالشيت.
    """
    try:
        report = active_report('today')
        if report is not None:
            return report
    except FinanceError as exc:
        return f'⚠️ {exc}'
    sheet = get_google_sheet()
    if sheet is None:
        return "تعذر الاتصال بـ Google Sheet — تأكد من إعدادات الاتصال."

    try:
        rows = sheet.get_all_values()
    except Exception:
        logger.exception("Failed to read rows from Google Sheet for income report")
        return "صار خطأ أثناء قراءة الشيت — حاول مرة ثانية بعد شوي."

    if len(rows) <= 1:
        return "ماكو أي عمليات دفع مسجلة لحد الحين."

    now = datetime.now(STATS_TIMEZONE)
    today = now.date()
    week_start = today - timedelta(days=today.weekday())  # الاثنين هو بداية الأسبوع
    month_start = today.replace(day=1)

    total_today = 0
    total_week = 0
    total_month = 0

    # نتجاوز صف العناوين (index 0)
    for row in rows[1:]:
        if len(row) < 2:
            continue
        if is_debt_payment_row(row):
            continue
        date_str, amount_str = row[0], row[1]
        try:
            row_date = parse_sheet_datetime(date_str).date()
        except (ValueError, IndexError):
            continue  # صف فيه بيانات غير متوقعة — نتجاهله بدل ما نكرش
        amount = parse_sheet_amount(amount_str)
        if amount is None:
            continue

        if row_date == today:
            total_today += amount
        if row_date >= week_start:
            total_week += amount
        if row_date >= month_start:
            total_month += amount

    instagram_rows = get_instagram_sales_rows_in_period("today")
    if instagram_rows is not None:
        total_today += sum(parse_amount(row[6]) or 0 for row in instagram_rows if len(row) > 6)
    instagram_rows = get_instagram_sales_rows_in_period("week")
    if instagram_rows is not None:
        total_week += sum(parse_amount(row[6]) or 0 for row in instagram_rows if len(row) > 6)
    instagram_rows = get_instagram_sales_rows_in_period("month")
    if instagram_rows is not None:
        total_month += sum(parse_amount(row[6]) or 0 for row in instagram_rows if len(row) > 6)

    date_warning = payment_stats_date_warning("today")
    return (
        "تقرير الدخل\n\n"
        f"اليوم: {total_today}\n"
        f"هذا الأسبوع: {total_week}\n"
        f"هذا الشهر: {total_month}"
        + date_warning
    )


def parse_stats_datetime(value: str) -> datetime:
    for fmt in (
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
        "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
        "%d-%m-%Y %H:%M:%S", "%d-%m-%Y %H:%M", "%d-%m-%Y",
    ):
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=STATS_TIMEZONE)
        except ValueError:
            continue
    raise ValueError(f"Unrecognized date/time: {value!r}")


def parse_sheet_datetime(value: str) -> datetime:
    """يقرأ أوقات Google Sheet المحفوظة كـ UTC ويحوّلها إلى توقيت بغداد."""
    return payment_datetime(value).astimezone(STATS_TIMEZONE)


def parse_stats_period(period_key: str) -> tuple[datetime, datetime] | None:
    """
    يحول مفتاح فترة (جاهز أو نص يدوي) إلى (start_date, end_date) شامل
    الطرفين. يرجع None لو الفترة "كل الوقت" (بدون حدود)، أو يرمي
    ValueError لو نص يدوي غير مفهوم.
    """
    now = datetime.now(STATS_TIMEZONE)
    today = now.date()
    day_start = datetime.combine(today, datetime.min.time(), tzinfo=STATS_TIMEZONE)
    day_end = datetime.combine(today, datetime.max.time(), tzinfo=STATS_TIMEZONE)

    if period_key == "today":
        return day_start, day_end
    if period_key == "week":
        week_start = today - timedelta(days=today.weekday())
        return datetime.combine(week_start, datetime.min.time(), tzinfo=STATS_TIMEZONE), day_end
    if period_key == "month":
        month_start = today.replace(day=1)
        return datetime.combine(month_start, datetime.min.time(), tzinfo=STATS_TIMEZONE), day_end
    if period_key == "all":
        return None

    # نطاق يدوي: YYYY-MM-DD HH:MM إلى YYYY-MM-DD HH:MM
    range_parts = re.split(r"\s+(?:إلى|الى|to)\s+", period_key, maxsplit=1, flags=re.IGNORECASE)
    if len(range_parts) == 2:
        start = parse_stats_datetime(range_parts[0])
        end = parse_stats_datetime(range_parts[1])
        if end < start:
            raise ValueError("نهاية الفترة أقدم من بدايتها")
        return start, end

    # نص يدوي: YYYY-MM-DD (يوم محدد) أو YYYY-MM (شهر محدد)
    if len(period_key) == 10:
        d = datetime.strptime(period_key, "%Y-%m-%d").date()
        return datetime.combine(d, datetime.min.time(), tzinfo=STATS_TIMEZONE), datetime.combine(d, datetime.max.time(), tzinfo=STATS_TIMEZONE)
    if len(period_key) == 7:
        d = datetime.strptime(period_key + "-01", "%Y-%m-%d").date()
        if d.month == 12:
            next_month = d.replace(year=d.year + 1, month=1)
        else:
            next_month = d.replace(month=d.month + 1)
        month_end = next_month - timedelta(days=1)
        return datetime.combine(d, datetime.min.time(), tzinfo=STATS_TIMEZONE), datetime.combine(month_end, datetime.max.time(), tzinfo=STATS_TIMEZONE)

    raise ValueError(f"Unrecognized period format: {period_key!r}")


def get_payment_rows_in_period(period_key: str) -> list[list[str]] | None:
    """
    يرجع كل صفوف الدفعات (بدون صف العناوين) اللي تقع بالفترة المحددة.
    يرجع None لو فشل الاتصال بالشيت.
    """
    sheet = get_google_sheet()
    if sheet is None:
        return None

    try:
        rows = sheet.get_all_values()
    except Exception:
        logger.exception("Failed to read payment rows for stats")
        return None

    if len(rows) <= 1:
        return []

    date_range = parse_stats_period(period_key)
    filtered = []

    for row in rows[1:]:
        if not any(row):
            continue
        if date_range is None:
            filtered.append(row)
            continue
        try:
            row_date = parse_sheet_datetime(row[0])
        except (ValueError, IndexError):
            continue

        if date_range is not None:
            start, end = date_range
            if not (start <= row_date <= end):
                continue

        filtered.append(row)

    return filtered


def get_expense_rows_in_period(period_key: str) -> list[list[str]] | None:
    """نفس get_payment_rows_in_period بس لصفحة المصروفات."""
    sheet = get_expenses_worksheet()
    if sheet is None:
        return None

    try:
        rows = sheet.get_all_values()
    except Exception:
        logger.exception("Failed to read expense rows for stats")
        return None

    if len(rows) <= 1:
        return []

    date_range = parse_stats_period(period_key)
    filtered = []

    for row in rows[1:]:
        if len(row) < 1 or not row[0].strip():
            continue
        try:
            row_date = parse_sheet_datetime(row[0])
        except (ValueError, IndexError):
            continue

        if date_range is not None:
            start, end = date_range
            if not (start <= row_date <= end):
                continue

        filtered.append(row)

    return filtered


def get_instagram_sales_rows_in_period(period_key: str) -> list[list[str]] | None:
    """يرجع مبيعات إنستغرام المؤكدة ضمن الفترة، حسب وقت البيع."""
    sheet = get_instagram_sales_worksheet()
    if sheet is None:
        return None
    try:
        rows = sheet.get_all_values()
    except Exception:
        logger.exception("Failed to read Instagram sales for stats")
        return None
    date_range = parse_stats_period(period_key)
    filtered = []
    for row in rows[1:]:
        if len(row) < 10 or row[9].strip() != "مؤكدة":
            continue
        try:
            row_datetime = parse_stats_datetime(row[1])
        except (ValueError, IndexError):
            continue
        if date_range is not None and not (date_range[0] <= row_datetime <= date_range[1]):
            continue
        filtered.append(row)
    return filtered


def payment_stats_date_warning(period_key: str) -> str:
    sheet = get_google_sheet()
    count = getattr(sheet, "last_undated_paid_rows", 0)
    if not count:
        return ""
    treatment = "محسوبة بالإجمالي" if period_key == "all" else "غير داخلة بهذه الفترة"
    return (f"\n\n⚠️ توجد {count} دفعة بلا تاريخ صالح؛ {treatment}. "
            "يلزم تصحيح تاريخها حتى تُنسب للفترة الصحيحة.")


def format_period_label(period_key: str) -> str:
    if period_key in PERIOD_LABELS:
        return PERIOD_LABELS[period_key]
    return period_key  # نص تاريخ يدوي، نعرضه كما هو


def calculate_totals_summary(period_key: str) -> str:
    """يحسب دخل/مصروف/صافي إجمالي لفترة معينة. يرجع نص جاهز للعرض."""
    try:
        report = active_report(period_key)
        if report is not None:
            return report
    except FinanceError as exc:
        return f'⚠️ {exc}'
    payment_rows = get_payment_rows_in_period(period_key)
    expense_rows = get_expense_rows_in_period(period_key)
    instagram_rows = get_instagram_sales_rows_in_period(period_key)

    if payment_rows is None or expense_rows is None or instagram_rows is None:
        return "تعذر الاتصال بـ Google Sheet — تأكد من إعدادات الاتصال."

    total_income = 0
    for row in payment_rows:
        if is_debt_payment_row(row):
            continue
        if len(row) >= 2 and row[1].strip():
            amount = parse_sheet_amount(row[1])
            if amount is not None:
                total_income += amount

    instagram_income = sum(parse_amount(row[6]) or 0 for row in instagram_rows if len(row) > 6)
    total_income += instagram_income

    total_expense = 0
    for row in expense_rows:
        if len(row) >= 2 and row[1].strip():
            amount = parse_sheet_amount(row[1])
            if amount is not None:
                total_expense += amount

    net = total_income - total_expense
    label = format_period_label(period_key)

    return (
        f"إحصائيات {label}\n\n"
        f"الدخل: {total_income}\n"
        f"منه مبيعات إنستغرام: {instagram_income}\n"
        f"المصروف: {total_expense}\n"
        f"الصافي: {net}"
        + payment_stats_date_warning(period_key)
    )


def _stats_catalog_product_for_name(name: str, products: list[dict]) -> dict | None:
    normalized_name = normalize_arabic_text(name)
    for product in products:
        candidates = [str(product.get("name") or "")]
        candidates.extend(str(alias) for alias in (product.get("aliases") or []))
        if normalized_name in {normalize_arabic_text(candidate) for candidate in candidates}:
            return product
    return None


def _stats_expense_product(reason: str, products: list[dict]) -> dict | None:
    """ينسب المصروف فقط إذا دل السبب على منتج واحد بوضوح."""
    matches = list(match_catalog_products(reason, products))
    if len(matches) == 1:
        return matches[0]
    if matches:
        return None
    normalized_reason = normalize_arabic_text(reason)
    fallback_matches = []
    for product in products:
        product_key = public_catalog_product_key(product.get("name"))
        aliases = STATS_PRODUCT_EXPENSE_ALIASES.get(product_key, ())
        if any(re.search(rf"(?<!\w){re.escape(normalize_arabic_text(alias))}(?!\w)", normalized_reason)
               for alias in aliases):
            fallback_matches.append(product)
    return fallback_matches[0] if len(fallback_matches) == 1 else None


def calculate_product_breakdown(period_key: str) -> str:
    """يحسب مبيعات ومصروفات وصافي وعدد عمليات كل منتج."""
    try:
        report = active_report(period_key, products=True)
        if report is not None:
            return report
    except FinanceError as exc:
        return f'⚠️ {exc}'
    payment_rows = get_payment_rows_in_period(period_key)
    expense_rows = get_expense_rows_in_period(period_key)
    instagram_rows = get_instagram_sales_rows_in_period(period_key)
    if payment_rows is None or expense_rows is None or instagram_rows is None:
        return "تعذر الاتصال بـ Google Sheet — تأكد من إعدادات الاتصال."

    label = format_period_label(period_key)
    if not payment_rows and not expense_rows and not instagram_rows:
        return f"ماكو أي عمليات دفع أو مصروفات مسجلة لفترة {label}."

    products = get_catalog_products()
    product_totals: dict[str, int] = {}
    product_expenses: dict[str, int] = {}
    product_counts: dict[str, int] = {}

    def product_label(raw_name: str) -> str:
        product = _stats_catalog_product_for_name(raw_name, products)
        return str(product.get("name")) if product else (raw_name.strip() or "غير محدد")

    for row in payment_rows:
        if is_debt_payment_row(row):
            continue
        if len(row) < 4:
            continue
        product = product_label(row[3])
        if not product:
            continue
        amount_str = row[1].strip() if len(row) >= 2 else ""
        amount = parse_sheet_amount(amount_str) or 0
        if amount <= 0:
            continue

        product_totals[product] = product_totals.get(product, 0) + amount
        product_counts[product] = product_counts.get(product, 0) + 1

    for row in instagram_rows:
        if len(row) < 7:
            continue
        product = product_label(row[3])
        amount = parse_amount(row[6]) or 0
        product_totals[product] = product_totals.get(product, 0) + amount
        product_counts[product] = product_counts.get(product, 0) + 1

    unmatched_expenses = 0
    for row in expense_rows:
        if len(row) < 3:
            continue
        amount = parse_sheet_amount(row[1]) or 0
        if amount <= 0:
            continue
        matched_product = _stats_expense_product(row[2], products)
        if matched_product is None:
            unmatched_expenses += amount
            continue
        product = str(matched_product.get("name") or "غير محدد")
        product_expenses[product] = product_expenses.get(product, 0) + amount

    lines = [f"تفصيل المنتجات — {label}\n"]
    product_names = set(product_totals) | set(product_expenses)
    for product in sorted(product_names, key=lambda p: product_totals.get(p, 0), reverse=True):
        income = product_totals.get(product, 0)
        expense = product_expenses.get(product, 0)
        lines.append(
            f"{product}\n"
            f"المبيعات: {income} ({product_counts.get(product, 0)} عملية)\n"
            f"المصروف: {expense}\n"
            f"الصافي: {income - expense}"
        )
    if unmatched_expenses:
        lines.append(
            f"مصروفات غير مرتبطة بمنتج واضح: {unmatched_expenses}\n"
            "لتدخل بحساب منتج، اكتب اسم المنتج ضمن سبب المصروف."
        )

    return "\n\n".join(lines) + payment_stats_date_warning(period_key)

