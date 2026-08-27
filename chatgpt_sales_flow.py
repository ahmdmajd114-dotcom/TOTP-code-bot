"""منطق نقي وقابل للاختبار لاختيار باقات ChatGPT باللهجة العراقية."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Mapping


def normalized_words(text: str) -> set[str]:
    """تطبيع بسيط يكفي لفهم كلمات الاختيار، من دون تخمين المعنى."""
    normalized = (text or "").lower()
    normalized = re.sub(r"[أإآٱ]", "ا", normalized)
    normalized = normalized.replace("ى", "ي").replace("ة", "ه")
    normalized = normalized.replace("؟", " ").replace("،", " ").replace("؛", " ")
    normalized = re.sub(r"[^\w\u0600-\u06ff]+", " ", normalized)
    return {word for word in normalized.split() if len(word) >= 1}


@dataclass(frozen=True)
class PlanChoice:
    """نتيجة فهم الاختيار؛ لا تحتوي أي رد جاهز للزبون."""

    plan: Mapping[str, object] | None = None
    missing: str | None = None


@dataclass(frozen=True)
class CodeRetryDecision:
    action: str
    attempt_count: int
    awaiting_restart: bool


def _plan_words(plan: Mapping[str, object]) -> set[str]:
    return normalized_words(
        " ".join(
            str(plan.get(key) or "")
            for key in ("name", "duration", "description", "price")
        )
    )


def _duration_from_words(words: set[str]) -> str | None:
    if "شهرين" in words:
        return "two_months"
    if "شهر" in words:
        return "one_month"
    return None


def _type_from_words(words: set[str]) -> str | None:
    if "خاص" in words:
        return "private"
    if "مشترك" in words:
        return "shared"
    return None


def _plan_matches_type(plan_words: set[str], selected_type: str) -> bool:
    return (selected_type == "private" and "خاص" in plan_words) or (
        selected_type == "shared" and "مشترك" in plan_words
    )


def _plan_matches_duration(plan_words: set[str], selected_duration: str) -> bool:
    if selected_duration == "two_months":
        return "شهرين" in plan_words
    return "شهر" in plan_words and "شهرين" not in plan_words


def resolve_plan_choice(
    message_parts: Iterable[str], plans: Iterable[Mapping[str, object]]
) -> PlanChoice:
    """
    يدمج رسائل الزبون داخل الجلسة ويعيد باقة مؤكدة أو نوع السؤال الناقص.

    لا توجد هنا قاعدة اختيار افتراضي: لا تُرجَع الباقة إلا إذا عرفنا نوعها
    ومدتها معاً، أو اختار الزبون سعراً فريداً وصريحاً.
    """
    text = " ".join(part for part in message_parts if part)
    words = normalized_words(text)
    active_plans = [plan for plan in plans if plan.get("is_active", True)]

    explicit_price_matches = [
        plan for plan in active_plans
        if str(plan.get("price") or "") in words
    ]
    if len(explicit_price_matches) == 1:
        return PlanChoice(plan=explicit_price_matches[0])

    selected_type = _type_from_words(words)
    selected_duration = _duration_from_words(words)
    if selected_type and selected_duration:
        matches = [
            plan for plan in active_plans
            if _plan_matches_type(_plan_words(plan), selected_type)
            and _plan_matches_duration(_plan_words(plan), selected_duration)
        ]
        if len(matches) == 1:
            return PlanChoice(plan=matches[0])
        # إذا الكاتالوج نفسه ملتبس، ما نختار نيابة عن الزبون.
        return PlanChoice(missing="request_plan_choice")

    if selected_type:
        return PlanChoice(missing="clarify_plan_duration")
    if selected_duration:
        return PlanChoice(missing="clarify_plan_type")
    return PlanChoice(missing="request_plan_choice")


def is_ambiguous_followup(text: str) -> bool:
    """يكشف سؤالاً ناقصاً لا يجوز تفسيره أو تخمين المقصود منه."""
    words = normalized_words(text)
    asks_question = bool(words & {"شنو", "عدكم", "اكو"})
    refers_to_unspecified_alternative = bool(words & {"غيره", "غير", "بقيه", "باقي"})
    names_scope = bool(words & {"منتجات", "المنتجات", "منتج", "شات", "الشات", "chatgpt", "جات", "كانفا", "canva"})
    return asks_question and refers_to_unspecified_alternative and not names_scope


def is_acknowledgement(text: str) -> bool:
    """رسالة تأكيد لا تحتاج جواباً من المتجر."""
    words = normalized_words(text)
    acknowledgement_words = {"تمام", "اوكي", "اوكيه", "ok", "اوك"}
    # «زين» وحدها قد تكون اسم طريقة دفع، لكن «زين تمام» تعبير محادثة واضح.
    if "زين" in words and words & acknowledgement_words:
        words = words - {"زين"}
    return bool(words) and words <= acknowledgement_words


def asks_payment_guidance(text: str) -> bool:
    """يفرق سؤال الخطوة القادمة عن كلمة عامة مثل «شنو» أو «تمام»."""
    words = normalized_words(text)
    has_question = bool(words & {"شنو", "شلون", "كيف", "لو"})
    has_next_step = bool(words & {"اسوي", "سوي", "ادفع", "الدفع", "تحويل", "حساب", "الحساب"})
    return has_question and has_next_step


def asks_shared_private_difference(text: str) -> bool:
    """يفهم سؤال الفرق أو المفاضلة بين اشتراك ChatGPT الخاص والمشترك."""
    words = normalized_words(text)
    private_words = {"خاص", "الخاص", "والخاص"}
    shared_words = {"مشترك", "المشترك", "والمشترك"}
    mentions_both = bool(words & private_words) and bool(words & shared_words)
    comparison_words = {
        "شنو", "فرق", "الفرق", "يختلف", "اختلاف", "الاختلاف",
        "احسن", "افضل", "اختار", "انسب",
    }
    return mentions_both and bool(words & comparison_words)


def is_payment_claim(text: str) -> bool:
    """يميز تصريح الدفع عن سؤال الأسعار أو الاستفسار العام."""
    return bool(normalized_words(text) & {"حولت", "دفعت", "دافعل", "محول"})


def is_chatgpt_support_issue(text: str) -> bool:
    """يكشف شكوى اشتراك ChatGPT، ولا يخلطها بطلب شراء أو أسعار.

    الدالة نقية حتى تكون قاعدة الحماية نفسها قابلة للاختبار قبل أن تدخل
    في وكيل المحادثة أو أي واجهة أخرى.
    """
    words = normalized_words(text)
    chatgpt_terms = {"chatgpt", "chat", "gpt", "جات", "تشات", "شات", "جيبيتي"}
    issue_terms = {
        "مشكله", "مشكلتي", "خربان", "خرب", "متوقف", "وقف", "واقف",
        "يرفض", "رفض", "مايشتغل", "مايفتح", "مايدخل",
    }
    split_failure = "ما" in words and bool(words & {"يشتغل", "يفتح", "يدخل", "صار"})
    return bool(words & chatgpt_terms) and (bool(words & issue_terms) or split_failure)


def is_private_chatgpt_plan(plan_name: str) -> bool:
    """الباقات الخاصة لا يجوز أن تستلم حساباً مشتركاً تلقائياً."""
    return "خاص" in normalized_words(plan_name)


def decide_code_retry(attempt_count: int, awaiting_restart: bool) -> CodeRetryDecision:
    """قرار الكود بعد كل محاولة، مستقل عن قاعدة البيانات وTOTP."""
    if awaiting_restart:
        return CodeRetryDecision("send_code", 5, False)
    next_count = attempt_count + 1
    if next_count <= 3:
        return CodeRetryDecision("send_code", next_count, False)
    if next_count == 4:
        return CodeRetryDecision("ask_restart", next_count, True)
    if 5 <= next_count <= 7:
        return CodeRetryDecision("send_code", next_count, False)
    return CodeRetryDecision("stop", next_count, False)


def decide_private_code_retry(attempt_count: int, awaiting_restart: bool) -> CodeRetryDecision:
    """سلوك كود الحساب الخاص: تنبيه بعد 5 محاولات، بدون إيقاف نهائي."""
    if awaiting_restart:
        return CodeRetryDecision("send_code", attempt_count + 1, False)
    next_count = attempt_count + 1
    if next_count <= 5:
        return CodeRetryDecision("send_code", next_count, False)
    if next_count == 6:
        return CodeRetryDecision("ask_restart", next_count, True)
    return CodeRetryDecision("send_code", next_count, False)


def should_review_payment_photo(workflow_state: str) -> bool:
    """فحص الصورة مكلف وحساس؛ لا يتم إلا بعد اختيار الباقة/طلب الوصل."""
    return workflow_state in {"awaiting_payment", "awaiting_payment_proof"}


def is_paid_amount_sufficient(expected_catalog_price: int | None, detected_amount: int | None) -> bool:
    """أسعار الكاتالوج بالآلاف، والوصل بالدينار الكامل.

    لا نرفض زيادة المبلغ: قد يكون الزبون اختار أن يدفع أكثر أو أضاف رسماً.
    لكن لا يصح أبداً اعتبار مبلغ أقل من سعر الباقة مدفوعاً بالكامل.
    """
    if expected_catalog_price is None or detected_amount is None:
        return False
    required_iqd = expected_catalog_price * 1000 if expected_catalog_price < 1000 else expected_catalog_price
    return detected_amount >= required_iqd


def classify_receipt_recency(receipt_datetime: str | None, now: datetime) -> str | None:
    """يعيد recent/old/future حين يكون تاريخ الوصل قابلاً للقراءة، وإلا None.

    لا نعتمد فرقاً يصفه نموذج الرؤية بالكلام؛ الحساب هنا يتم من التاريخ نفسه.
    """
    if not receipt_datetime:
        return None
    value = str(receipt_datetime).strip()
    parsed = None
    for fmt in ("%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M", "%d-%m-%Y %H:%M"):
        try:
            parsed = datetime.strptime(value, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        return None
    # صورة التحويل التي تظهر بعد وقت بغداد الحالي بصورة ملموسة ليست وصلاً حديثاً صالحاً.
    if parsed > now.replace(tzinfo=None) + timedelta(minutes=5):
        return "future"
    return "recent" if now.replace(tzinfo=None) - parsed <= timedelta(hours=2) else "old"


def can_request_account_code(workflow_state: str) -> bool:
    """الكود لا يصدر إلا بعد تسليم حساب مشترك فعلياً."""
    return workflow_state in {"account_delivered", "code_sent", "support_review"}
