"""Export exactly the frozen report; formula-safe CSV, no authentication data."""
import csv
import io
import zipfile
from statistics_engine import LABELS, MONEY, COUNTS, contribution


def safe_cell(value):
    if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@')):
        return "'"+value
    return value


def csv_bytes(rows):
    stream=io.StringIO(newline='');writer=csv.writer(stream)
    for row in rows:writer.writerow([safe_cell(v) for v in row])
    return stream.getvalue().encode('utf-8-sig')


def export_report(report):
    fields=(*MONEY,*COUNTS)
    tables={
        'filters.csv':[['الفلتر','القيمة'],*[[k,v] for k,v in report['filters'].items()]],
        'summary.csv':[['المؤشر','القيمة'],['وقت القراءة',report['as_of'].isoformat()],['الفترة',report['filters']['period']],
                       *[[LABELS[k],v] for k,v in report['totals'].items()]],
        'products.csv':[['معرف المنتج','الاسم',*[LABELS[k] for k in fields]],
                        *[[g['key'],g['name'],*[g['metrics'][k] for k in fields]] for g in report['products']]],
        'operations.csv':[['رقم العملية','تاريخ العملية','تاريخ التسجيل','النوع','معرف الزبون','المصدر',*[LABELS[k] for k in fields]],
                          *[[e['id'],e['record'].get('occurred_at') or '',e['record'].get('recorded_at') or '',e['record']['kind'],e['customer'],e['source'],
                             *[contribution(e,report['filters'])[k] for k in fields]] for e in report['events']]],
        'wallets.csv':[['الخزنة','البداية','الداخل','الخارج','صافي التحويلات','التسويات','نهاية الفترة','الآن'],
                       *[[r['key'],r['opening'] if r['opening'] is not None else 'غير متاح',r['incoming'],r['outgoing'],r['transfers'],r['adjustments'],
                          r['closing'] if r['closing'] is not None else 'غير متاح',r['current']] for r in report['positions']['wallets']]],
        'quality.csv':[['البند','القيمة'],*[[k,v] for k,v in report['quality'].items()],
                       *[[k,v] for k,v in report['snapshot'].get('source_status',{}).items() if v!='ok']],
    }
    from statistics_operations import operational_report
    operational=operational_report(report['snapshot'],report['as_of'])
    tables['operational.csv']=[['القسم','المؤشر','القيمة']]
    for name,section in operational.items():
        if isinstance(section,dict):
            tables['operational.csv'] += [[name,k,v] for k,v in section.items() if isinstance(v,(str,int))]
    tables['customer_credits.csv']=[['حساب الرصيد','بداية الفترة','نهايتها','الآن'],
        *[[r['key'],r['opening'] if r['opening'] is not None else 'غير متاح',r['closing'] if r['closing'] is not None else 'غير متاح',r['current']] for r in report['positions']['credits']]]
    result=io.BytesIO()
    with zipfile.ZipFile(result,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,rows in tables.items():archive.writestr(name,csv_bytes(rows))
    result.seek(0);result.name='MedBox-statistics.zip'
    return result
