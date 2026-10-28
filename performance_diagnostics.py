"""Bounded, read-only runtime measurements; never store arguments or customers."""
from collections import deque
from threading import Lock
from time import monotonic

_samples = deque(maxlen=2000)
_lock = Lock()
_owner_id = None


def configure_performance(owner_id):
    global _owner_id
    _owner_id = int(owner_id)


def record_timing(category, lane, site, elapsed, waiting=0):
    with _lock:
        _samples.append((monotonic(), category, lane, site, elapsed, waiting))


def summary():
    now = monotonic()
    with _lock:
        samples = [r for r in _samples if now-r[0] <= 15*60]
    lines = ['⏱ تشخيص الأداء — آخر 15 دقيقة منذ التشغيل']
    if not samples:
        return '\n'.join(lines+['لا توجد قياسات حديثة بعد.'])
    groups = {}
    for _, category, lane, site, elapsed, waiting in samples:
        row = groups.setdefault((category, lane, site), [0, 0, 0, 0])
        row[0] += 1
        row[1] += elapsed
        row[2] = max(row[2], elapsed)
        row[3] = max(row[3], waiting)
    names = {'code':'الأكواد', 'customer':'الزبائن', 'owner':'الأونر', 'background':'الخلفية'}
    lines.append('أبطأ المراحل المقاسة؛ المدد بالثواني:')
    for (category, lane, site), (count, total, worst, queued) in sorted(groups.items(), key=lambda pair: max(pair[1][2], pair[1][3]), reverse=True)[:8]:
        label = 'معالجة رسالة' if category == 'update' else 'انتظار شبكة/خدمة'
        lines.append(f"{names.get(lane,lane)} — {label}\n{site}\nمرات: {count} | متوسط: {total/count:.2f} | أعلى: {worst:.2f} | أعلى انتظار بالطابور: {queued:.2f}")
    lines.append('يشمل الانتظار المقصود للتحية وتجميع الرسائل ودورة الكود. القياسات لا تغيّر البيانات المالية.')
    return '\n\n'.join(lines)


async def show_performance(update, context):
    if not update.effective_user or update.effective_user.id != _owner_id:
        return
    await update.effective_message.reply_text(summary())
