"""قواعد ثابتة لمساعد العائلة الداخلي.

هذا الملف لا يتصل بواتساب أو قاعدة بيانات ولا يخزّن كلمات مرور أو رموز تحقق.
وظيفته تحويل سؤال الأهل إلى خطوة عمل واضحة وقابلة للمراجعة.  الذكاء الاصطناعي
يمكنه اختيار ``topic`` فقط؛ القرار المالي أو الفني يبقى محكوماً بهذه القواعد.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
import re


ANKI_PRICE_IQD = 5_000
ANKI_PAYMENT_METHODS = ("ماستر", "زين كاش", "رصيد اثير")


class AnkiTopic(str, Enum):
    OFFER = "offer"
    PAYMENT = "payment"
    RECEIPT_REVIEW = "receipt_review"
    INSTALLATION = "installation"
    SUPPORT = "support"
    ESCALATE = "escalate"


@dataclass(frozen=True)
class FamilyGuidance:
    """رد داخلي للأهل، وليس رسالة تلقائية للزبون."""

    topic: AnkiTopic
    customer_reply: str
    family_checklist: tuple[str, ...]
    requires_owner: bool = False


@dataclass(frozen=True)
class ReceiptReview:
    """نتيجة فحص أولي؛ لا تعتبر التحويل مقبولاً تلقائياً."""

    status: str  # needs_receipt | needs_manual_review | mismatch | eligible_for_delivery
    checklist: tuple[str, ...]


def classify_anki_family_topic(text: str) -> AnkiTopic:
    """يصنف سؤال الأهل إلى خطوة أنكي، ولا يفسر النص كتعليمات برمجية.

    هذا تصنيف ضيق ومقصود للحالات المتكررة. إذا ما كانت الحالة واضحة نعيدها
    لصاحب المتجر بدلاً من تخمين قرار حساس.
    """
    value = (text or "").lower()
    value = re.sub(r"[أإآٱ]", "ا", value).replace("ى", "ي").replace("ة", "ه")
    words = set(re.findall(r"[\w\u0600-\u06ff]+", value))
    if words & {"حذف", "محذوف", "مشكل", "مشكله", "مايشتغل", "مايفتح", "دعم", "خربان"}:
        return AnkiTopic.SUPPORT
    if words & {"وصل", "تحويل", "ماستر", "زين", "كاش", "اثير", "رصيد"}:
        return AnkiTopic.RECEIPT_REVIEW if words & {"وصل", "حول", "حولت", "دافعل", "دفعت"} else AnkiTopic.PAYMENT
    if words & {"تثبيت", "تنزيل", "ابستور", "ابل", "ايفون", "ايباد", "تسجيل", "كود", "رمز"} or any(
        fragment in value for fragment in ("ننز", "تنزل", "ينزل", "نزله", "اب ستور")
    ):
        return AnkiTopic.INSTALLATION
    if words & {"ادفع", "دفع", "اشترك", "اشتراك", "سعر", "بكم"}:
        return AnkiTopic.PAYMENT
    if words & {"انكي", "anki", "انچي"}:
        return AnkiTopic.OFFER
    return AnkiTopic.ESCALATE


def anki_family_guidance(topic: AnkiTopic) -> FamilyGuidance:
    """يعيد نصاً ثابتاً مبنياً على سياسة المتجر، من دون تخمين من النموذج."""
    if topic is AnkiTopic.OFFER:
        return FamilyGuidance(
            topic,
            "بلي متوفر أنكي، خدمة التهيئة والتنزيل سعرها 5 آلاف. "
            "يبقى التطبيق على الجهاز ما لم ينحذف أو تتغير حالة الشراء من Apple.",
            (
                "أكدوا أن جهاز الزبون iPhone أو iPad.",
                "لا تعطون وعداً بإعادة تنزيل غير محدودة إذا انحذف التطبيق.",
                "إذا وافق، انتقلوا فقط إلى طرق الدفع.",
            ),
        )
    if topic is AnkiTopic.PAYMENT:
        return FamilyGuidance(
            topic,
            "تمام، الدفع متاح بماستر أو زين كاش أو رصيد اثير. بعد الدفع دزلي صورة الوصل الواضحة.",
            (
                "المبلغ المطلوب: 5000 دينار.",
                "لا تبدؤون التهيئة قبل صورة وصل أو تأكيد أهل البيت المسؤولين.",
                "بطاقة الرصيد تحتاج فحص وصول الرصيد إلى الخط قبل اعتبارها مكتملة.",
            ),
        )
    if topic is AnkiTopic.RECEIPT_REVIEW:
        return FamilyGuidance(
            topic,
            "دزلي صورة أوضح للوصل إذا ما مبين بيها المبلغ والوقت والجهة المستلمة.",
            (
                "طابقوا المبلغ مع 5000 دينار.",
                "طابقوا التاريخ والوقت: لازم يكون التحويل حديثاً، مو صورة قديمة.",
                "طابقوا اسم أو رقم الجهة المستلمة مع بيانات المتجر.",
                "لا تقولون للزبون تم التأكيد إلا بعد الفحص البشري.",
            ),
        )
    if topic is AnkiTopic.INSTALLATION:
        return FamilyGuidance(
            topic,
            "بعد ما يتم التأكيد، راح نكمل وياك خطوات التنزيل على جهازك.",
            (
                "استعملوا الفيديو المثبّت كمرجع للخطوات، لا تعتمدون على الذاكرة.",
                "أي رمز تحقق يُدخل فقط من صاحب الحساب على جهازه أثناء الجلسة.",
                "لا تنسخون كلمة مرور أو رمز تحقق داخل المحادثة ولا تحفظوه.",
                "قبل الختام تأكدوا أن التطبيق ظهر ويشتغل على جهاز الزبون.",
            ),
            requires_owner=True,
        )
    if topic is AnkiTopic.SUPPORT:
        return FamilyGuidance(
            topic,
            "بلا زحمة دزلي سكرين للمشكلة واذكر شنو مكتوب عندك بالضبط.",
            (
                "حددوا: مشكلة تنزيل، شراء، تسجيل دخول، أو التطبيق لا يفتح.",
                "إذا التطبيق محذوف، لا تعطون وعداً بإعادة التنزيل؛ حولوا الحالة لصاحب المتجر.",
                "إذا المشكلة تخص Apple أو الحساب، لا تطلبوا بيانات اعتماد عبر الشات.",
            ),
            requires_owner=True,
        )
    return FamilyGuidance(
        topic,
        "خليني أتأكد من صاحب المتجر حتى نعطيك جواب دقيق.",
        ("لا تخمّنون جواباً أو سعراً أو استثناءً.",),
        requires_owner=True,
    )


def review_anki_receipt(
    *,
    method: str | None,
    amount_iqd: int | None,
    recipient_matches_store: bool | None,
    receipt_date: date | None,
    today: date,
    credit_received_on_line: bool | None = None,
) -> ReceiptReview:
    """يفرض أدنى معلومات لازمة قبل تسليم الخدمة.

    ``eligible_for_delivery`` معناها أن الوصل يستحق موافقة شخص من العائلة،
    وليس قبولاً أوتوماتيكياً من البوت.
    """
    if method not in ANKI_PAYMENT_METHODS or amount_iqd is None or receipt_date is None:
        return ReceiptReview(
            "needs_receipt",
            ("اطلبوا طريقة الدفع وصورة وصل يظهر فيها المبلغ والتاريخ والجهة المستلمة.",),
        )
    if amount_iqd != ANKI_PRICE_IQD:
        return ReceiptReview(
            "mismatch",
            (f"المبلغ غير مطابق: المطلوب {ANKI_PRICE_IQD} دينار.", "لا تسلّمون الخدمة قبل حل الفرق."),
        )
    if receipt_date != today:
        return ReceiptReview(
            "mismatch",
            ("تاريخ الوصل ليس اليوم؛ اطلبوا وصلاً حديثاً أو حولوه لصاحب المتجر.",),
        )
    if recipient_matches_store is not True:
        return ReceiptReview(
            "mismatch",
            ("الجهة المستلمة غير مؤكدة أو لا تطابق بيانات المتجر.",),
        )
    if method == "رصيد اثير" and credit_received_on_line is not True:
        return ReceiptReview(
            "needs_manual_review",
            ("الرصيد يحتاج فحص وصوله فعلياً إلى خط المتجر قبل تسليم الخدمة.",),
        )
    return ReceiptReview(
        "eligible_for_delivery",
        ("الوصل مطابق مبدئياً.", "فرد من العائلة يوافق يدوياً ثم يبدأ خطوات التهيئة."),
    )
