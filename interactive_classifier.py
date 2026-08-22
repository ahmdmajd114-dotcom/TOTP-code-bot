"""Small, dependency-free helpers for archive-guided interactive classification."""

from __future__ import annotations

import re


def normalize_classifier_text(text: str) -> str:
    """Normalize Arabic variants while preserving word order for prompt examples."""
    value = (text or "").lower().strip()
    value = re.sub(r"[أإآٱ]", "ا", value)
    value = value.replace("ى", "ي").replace("ة", "ه")
    value = re.sub(r"(.)\1{2,}", r"\1", value)
    value = re.sub(r"\s+", " ", value)
    return value


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

