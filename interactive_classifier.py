"""Small, dependency-free helpers for archive-guided interactive classification."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


INTERACTIVE_INTENTS = {
    "acknowledgement",
    "ask_payment_methods",
    "ask_price",
    "closing",
    "code_request",
    "greeting",
    "next_step",
    "other",
    "payment_claim",
    "plan_selection",
    "purchase",
    "registration_help",
    "support",
    "workspace_help",
}


@dataclass(frozen=True)
class IntentFrame:
    """Semantic meaning extracted by the model; never a reply/action itself."""

    intent: str = "other"
    product: str | None = None
    plan_type: str | None = None
    duration: str | None = None
    confidence: float = 0.0


def normalize_classifier_text(text: str) -> str:
    """Normalize Arabic variants while preserving word order for prompt examples."""
    value = (text or "").lower().strip()
    value = re.sub(r"[أإآٱ]", "ا", value)
    value = value.replace("ى", "ي").replace("ة", "ه")
    value = re.sub(r"(.)\1{2,}", r"\1", value)
    value = re.sub(r"\s+", " ", value)
    return value


def mentions_chatgpt(text: str) -> bool:
    normalized = normalize_classifier_text(text)
    words = set(re.findall(r"[\w\u0600-\u06ff]+", normalized))
    return bool(words & {"chatgpt", "chat", "gpt", "جات", "تشات", "شات", "جيبيتي"}) or "جي بي تي" in normalized


def has_support_signal(text: str) -> bool:
    """Detect unmistakable problem language without deciding the product."""
    normalized = normalize_classifier_text(text)
    words = set(re.findall(r"[\w\u0600-\u06ff]+", normalized))
    direct = {
        "مشكله", "مشكلتي", "خربان", "خرب", "متوقف", "واقف", "رفض",
        "يرفض", "مايشتغل", "مايفتح", "مايدخل", "خطا", "error",
    }
    split_failure = "ما" in words and bool(words & {"يشتغل", "يفتح", "يدخل", "صار"})
    return bool(words & direct) or split_failure


def should_enter_support_mode(
    current_text: str,
    recent_customer_text: str,
    workflow_state: str,
    selected_product_is_chatgpt: bool,
) -> bool:
    """Support outranks sales when the problem belongs to the active product context."""
    if workflow_state in {"support_pending", "support_review"}:
        return True
    if not has_support_signal(current_text):
        return False
    has_product_context = (
        mentions_chatgpt(current_text)
        or mentions_chatgpt(recent_customer_text)
        or selected_product_is_chatgpt
        or workflow_state in {
            "awaiting_plan_choice", "awaiting_payment", "awaiting_payment_proof",
            "payment_review", "payment_verified", "account_delivered", "code_sent",
        }
    )
    return has_product_context


def parse_intent_frame(raw: str) -> IntentFrame:
    """Parse the model's JSON defensively; malformed output becomes `other`."""
    value = (raw or "").strip()
    if value.startswith("```"):
        value = re.sub(r"^```(?:json)?\s*", "", value, flags=re.IGNORECASE)
        value = re.sub(r"\s*```$", "", value)
    match = re.search(r"\{.*\}", value, flags=re.DOTALL)
    if not match:
        return IntentFrame()
    try:
        payload = json.loads(match.group(0))
    except (TypeError, ValueError):
        return IntentFrame()

    intent = str(payload.get("intent") or "other").strip().lower()
    if intent not in INTERACTIVE_INTENTS:
        intent = "other"
    product = str(payload.get("product") or "").strip().lower() or None
    if product not in {None, "chatgpt"}:
        product = None
    plan_type = str(payload.get("plan_type") or "").strip().lower() or None
    if plan_type not in {None, "private", "shared"}:
        plan_type = None
    duration = str(payload.get("duration") or "").strip().lower() or None
    if duration not in {None, "one_month", "two_months"}:
        duration = None
    try:
        confidence = max(0.0, min(1.0, float(payload.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0
    return IntentFrame(intent, product, plan_type, duration, confidence)


def guard_interactive_action(
    action_key: str,
    workflow_state: str,
    has_selected_plan: bool,
    account_was_delivered: bool,
) -> str:
    """Final invariant gate between any classifier and a customer-facing reply."""
    if workflow_state in {"support_pending", "support_review"}:
        if action_key in {
            "chatgpt_plans", "catalog_product_plans", "payment_methods",
            "payment_next_step", "selected_plan_price", "request_plan_choice",
            "clarify_plan_type", "clarify_plan_duration", "clarify_product",
        }:
            return "request_support_screenshot"

    if action_key in {
        "payment_methods", "payment_next_step", "selected_plan_price",
        "request_payment_proof",
    } and not has_selected_plan:
        return "request_plan_choice"

    if action_key == "code_request" and not account_was_delivered:
        return "handoff"
    return action_key


def infer_action_from_archive_reply(reply: str, templates: dict[str, str]) -> str | None:
    """Infer a safe action label from an archived fixed reply.

    Archive rows predate action labels, so this intentionally uses exact
    normalized template matches and a small set of unmistakable phrases.
    Unknown replies stay unlabeled instead of being assigned a guessed action.
    """
    normalized_reply = normalize_classifier_text(reply)
    if not normalized_reply:
        return None

    for action_key, response_text in templates.items():
        normalized_template = normalize_classifier_text(response_text)
        if normalized_template and normalized_reply == normalized_template:
            return action_key

    phrase_actions = (
        (("طرق الدفع", "طرق التحويل"), "payment_methods"),
        (("اختار الباقه", "اختار الباقة"), "request_plan_choice"),
        (("خاص لو مشترك",), "clarify_plan_type"),
        (("شهر لو شهرين",), "clarify_plan_duration"),
        (("صوره التحويل", "صورة التحويل", "دزلي صوره الوصل", "دزلي صورة الوصل"), "request_payment_proof"),
        (("workspace", "مساحه عمل", "مساحة عمل"), "workspace_guidance"),
        (("لحظه وادزلك الكود", "لحظة وأدزلك الكود"), "code_request"),
        (("مافهمت قصدك", "ما فهمت قصدك", "وضحلي"), "clarify"),
        (("اهلا وسهلا", "أهلا وسهلا"), "greeting"),
    )
    for phrases, action_key in phrase_actions:
        if any(normalize_classifier_text(phrase) in normalized_reply for phrase in phrases):
            return action_key
    return None


def infer_intent_from_archive_reply(reply: str, templates: dict[str, str]) -> str | None:
    """Translate legacy reply examples into semantic hints, never executable actions."""
    action = infer_action_from_archive_reply(reply, templates)
    return {
        "closing": "closing",
        "greeting": "greeting",
        "payment_methods": "ask_payment_methods",
        "request_payment_proof": "payment_claim",
        "request_plan_choice": "purchase",
        "clarify_plan_type": "plan_selection",
        "clarify_plan_duration": "plan_selection",
        "workspace_guidance": "workspace_help",
        "code_request": "code_request",
        "request_support_screenshot": "support",
    }.get(action)
