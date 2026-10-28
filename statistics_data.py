"""Read-only snapshot adapter; missing statistics SQL is explicit, never zero."""
from datetime import datetime, timezone
from financial_ledger import FinanceError, ledger


def read_snapshot():
    db=ledger(); db.require_active()
    try:
        result=db.client.rpc('finance_statistics_read',{'p_owner':db.owner_id}).execute().data
    except Exception as exc:
        if str(getattr(exc,'code',''))!='PGRST202':
            raise FinanceError('تعذرت قراءة الإحصائيات؛ لم أعرض تقريراً ناقصاً كأنه مكتمل.') from exc
        result=db.snapshot()
        result['statistics_extension']=False
        result['source_status']={'operational':'يلزم تشغيل supabase_statistics.sql لإحصائيات التشغيل المتزامنة.'}
    if not isinstance(result,dict) or not all(k in result for k in ('operations','accounts','debts','entries')):
        raise FinanceError('استجابة الإحصائيات ناقصة؛ حدّث جداول المالية.')
    # A fallback still uses one financial MVCC snapshot. Operational counters
    # remain unavailable until their single-snapshot RPC is installed.
    result.setdefault('as_of',datetime.now(timezone.utc).isoformat())
    return result
