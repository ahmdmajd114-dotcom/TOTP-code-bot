"""Unlisted PMT beneficiary directory page.

The page is deliberately not linked from the public MedBox catalogue.  The
source data stays in the local PMT workbook builder so there is one canonical
copy for the spreadsheet and this page.
"""

from html import escape
from hashlib import sha256
from pathlib import Path
import re
from typing import Optional


_SOURCE_FILE = Path(__file__).with_name("build_beneficiaries_workbook.mjs")


def _normalise_area(value: str) -> str:
    value = " ".join(value.split())
    if not value:
        return "غير محدد"
    if "المعلمين" in value:
        return "حي المعلمين"
    if "طهمازي" in value:
        return "الطهمازية"
    if "اكرمين" in value:
        return "الأكرمين"
    return value


def _records() -> list[dict[str, str]]:
    source = _SOURCE_FILE.read_text(encoding="utf-8")
    match = re.search(r"const raw = String\.raw`(.*?)`;", source, re.DOTALL)
    if not match:
        raise RuntimeError("PMT source data could not be loaded")
    rows: list[dict[str, str]] = []
    for line in match.group(1).strip().splitlines():
        name, address = line.split("\t", 1)
        parts = [part.strip() for part in address.split("/")]
        beneficiary_key = sha256(f"{name}|{parts[-1] if parts else ''}".encode("utf-8")).hexdigest()
        rows.append({
            "key": beneficiary_key,
            "name": name,
            "district": parts[0] if parts else "غير محدد",
            "area": _normalise_area(parts[1] if len(parts) > 1 else ""),
            "mahalla": parts[2] if len(parts) > 2 else "",
            "alley": parts[3] if len(parts) > 3 else "",
            "house": "/".join(parts[4:-2]) if len(parts) > 6 else "",
            "landmark": parts[-2] if len(parts) > 1 and parts[-2] else "غير محدد",
            "phone": parts[-1] if parts else "",
        })
    return sorted(rows, key=lambda row: (row["area"], row["landmark"], row["name"]))


def beneficiary_keys() -> set[str]:
    return {row["key"] for row in _records()}


def build_pmt_page_html(
    comments: Optional[dict[str, str]] = None,
    hidden_keys: Optional[set[str]] = None,
) -> str:
    rows = _records()
    comments = comments or {}
    hidden_keys = hidden_keys or set()
    rows = [row for row in rows if row["key"] not in hidden_keys]
    cells = []
    for index, row in enumerate(rows, start=1):
        cells.append(
            "<tr>"
            f"<td>{index}</td><td>{escape(row['name'])}</td>"
            f"<td>{escape(row['area'])}</td><td>{escape(row['mahalla'])}</td>"
            f"<td>{escape(row['alley'])}</td><td>{escape(row['house'])}</td>"
            f"<td>{escape(row['landmark'])}</td>"
            f"<td dir=\"ltr\">{escape(row['phone'])}</td>"
            "<td class=\"comment-cell\">"
            f"<textarea data-comment-key=\"{row['key']}\" maxlength=\"1000\" placeholder=\"أضف تعليقاً…\">{escape(comments.get(row['key'], ''))}</textarea>"
            f"<button class=\"save-comment\" data-comment-key=\"{row['key']}\" type=\"button\">حفظ</button>"
            "<span class=\"saved\" aria-live=\"polite\"></span></td>"
            f"<td><button class=\"delete-person\" data-beneficiary-key=\"{row['key']}\" type=\"button\">حذف</button></td>"
            "</tr>"
        )
    return f"""<!doctype html>
<html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive">
<meta name="referrer" content="no-referrer">
<title>PMT | قائمة داخلية</title>
<style>
:root{{--navy:#16354b;--teal:#126f73;--line:#dce8e7;--paper:#fbfcfc}}
*{{box-sizing:border-box}}body{{margin:0;background:#edf4f2;color:#162e3d;font-family:Tahoma,Arial,sans-serif}}
header{{background:linear-gradient(120deg,var(--navy),var(--teal));color:#fff;padding:26px max(18px,calc((100% - 1300px)/2))}}
h1{{margin:0;font-size:clamp(23px,4vw,34px)}}header p{{margin:8px 0 0;opacity:.88}}
main{{width:min(1300px,calc(100% - 24px));margin:24px auto 50px}}.tools{{display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap;margin-bottom:16px}}
.count{{font-weight:bold;color:var(--teal)}}input{{width:min(450px,100%);padding:13px 15px;border:1px solid #aac5c2;border-radius:12px;font:inherit;background:#fff;outline:none}}input:focus{{border-color:var(--teal);box-shadow:0 0 0 3px #126f7322}}
.table-wrap{{overflow:auto;border:1px solid var(--line);border-radius:16px;background:#fff;box-shadow:0 12px 30px #163c4112}}table{{border-collapse:collapse;width:100%;min-width:1260px}}th{{position:sticky;top:0;background:var(--navy);color:#fff;white-space:nowrap}}th,td{{padding:12px 10px;border-bottom:1px solid var(--line);text-align:right;vertical-align:top}}tbody tr:nth-child(even){{background:var(--paper)}}tbody tr:hover{{background:#e7f4f2}}td:first-child{{text-align:center;color:#667982;font-weight:bold}}.comment-cell{{min-width:245px}}textarea{{display:block;width:100%;min-height:58px;resize:vertical;padding:9px;border:1px solid #b7d0cc;border-radius:9px;font:inherit;line-height:1.5;background:#fff}}.save-comment{{margin-top:7px;border:0;border-radius:8px;padding:7px 13px;background:var(--teal);color:#fff;font:inherit;font-weight:bold;cursor:pointer}}.save-comment:hover{{background:var(--navy)}}.save-comment:disabled{{opacity:.65;cursor:wait}}.saved{{margin-right:8px;font-size:12px;color:#237654}}.delete-person{{border:1px solid #dc2626;border-radius:8px;padding:7px 13px;background:#fff;color:#b91c1c;font:inherit;font-weight:bold;cursor:pointer}}.delete-person:hover{{background:#fef2f2}}.delete-person:disabled{{opacity:.6;cursor:wait}}
.privacy{{font-size:13px;color:#617478;margin:15px 0 0}}@media(max-width:600px){{header{{padding:22px 16px}}main{{width:min(100% - 16px,1300px)}}}}
</style></head><body>
<header><h1>قائمة PMT الداخلية</h1><p>بابل — مركز قضاء الحلة</p></header>
<main><div class="tools"><div class="count" id="count">{len(rows)} سجلًا</div><input id="search" type="search" placeholder="ابحث بالاسم أو الهاتف أو المنطقة أو النقطة الدالة" autofocus></div>
<div class="table-wrap"><table id="directory"><thead><tr><th>ت</th><th>الاسم</th><th>المنطقة/الحي</th><th>المحلة</th><th>الزقاق</th><th>الدار</th><th>النقطة الدالة</th><th>الهاتف</th><th>تعليق خاص</th><th>إجراء</th></tr></thead><tbody>{''.join(cells)}</tbody></table></div>
<p class="privacy">هذه صفحة داخلية غير مرتبطة من الموقع العام. لا تشارك رابطها أو محتواها.</p></main>
<script>
const rows=[...document.querySelectorAll('#directory tbody tr')], count=document.getElementById('count');
document.getElementById('search').addEventListener('input', event=>{{const query=event.target.value.trim().toLocaleLowerCase('ar');let visible=0;rows.forEach(row=>{{const show=!query||row.innerText.toLocaleLowerCase('ar').includes(query);row.hidden=!show;if(show)visible++}});count.textContent=`${{visible}} من {len(rows)} سجلًا`;}});
document.querySelectorAll('.save-comment').forEach(button=>button.addEventListener('click',async()=>{{const key=button.dataset.commentKey,textarea=document.querySelector(`textarea[data-comment-key="${{key}}"]`),status=button.nextElementSibling,comment=textarea.value.trim();button.disabled=true;status.textContent='جارٍ الحفظ…';try{{const response=await fetch('/pmt/comments',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{key,comment}})}});if(!response.ok)throw new Error();status.textContent='تم الحفظ ✓';setTimeout(()=>status.textContent='',2200)}}catch{{status.textContent='تعذر الحفظ'}}finally{{button.disabled=false}}}}));
document.querySelectorAll('.delete-person').forEach(button=>button.addEventListener('click',async()=>{{if(!confirm('حذف هذا الاسم من قائمة PMT؟ يمكن استرجاعه لاحقاً.'))return;button.disabled=true;try{{const response=await fetch('/pmt/beneficiary/delete',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{key:button.dataset.beneficiaryKey}})}});if(!response.ok)throw new Error();button.closest('tr').remove();count.textContent=`${{document.querySelectorAll('#directory tbody tr:not([hidden])').length}} سجلًا`}}catch{{button.disabled=false;alert('تعذر الحذف، حاول مرة أخرى.')}}}}));
</script></body></html>"""
