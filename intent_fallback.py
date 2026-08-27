"""Safe helpers for Arabic normalization and AI FAQ-intent fallback."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Iterable


ARABIC_MARKS_RE = re.compile(
    r"[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed]"
)
CONVERSATIONAL_CATEGORIES = {"سلام", "ترحيب", "شكر"}


def normalize_arabic_text(text: str) -> str:
    """Normalize spelling noise without changing the meaning of the message."""
    normalized = (text or "").strip().lower()
    normalized = ARABIC_MARKS_RE.sub("", normalized)
    normalized = normalized.replace("ـ", "")
    normalized = re.sub(r"[أإآٱ]", "ا", normalized)
    normalized = normalized.replace("ى", "ي").replace("ة", "ه")
    normalized = re.sub(r"(.)\1{2,}", r"\1", normalized)
    return re.sub(r"\s+", " ", normalized)


@dataclass(frozen=True)
class FAQIntent:
    categories: tuple[str, ...] = ()
    confidence: float = 0.0


def parse_faq_intent(raw: str, allowed_categories: Iterable[str]) -> FAQIntent:
    """Parse a model classification and reject unknown or malformed actions."""
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not match:
        return FAQIntent()
    try:
        value = json.loads(match.group(0))
        confidence = float(value.get("confidence", 0))
    except (TypeError, ValueError, json.JSONDecodeError):
        return FAQIntent()
    if not 0 <= confidence <= 1:
        return FAQIntent()

    allowed = set(allowed_categories)
    raw_categories = value.get("categories")
    if not isinstance(raw_categories, list):
        return FAQIntent()
    categories: list[str] = []
    for category in raw_categories:
        if isinstance(category, str) and category in allowed and category not in categories:
            categories.append(category)
    return FAQIntent(tuple(categories[:3]), confidence)


def prioritize_action_categories(categories: Iterable[str]) -> list[str]:
    """Drop greetings/thanks when the same customer turn contains a real request."""
    ordered = list(dict.fromkeys(categories))
    if any(category not in CONVERSATIONAL_CATEGORIES for category in ordered):
        return [category for category in ordered if category not in CONVERSATIONAL_CATEGORIES]
    return ordered


def contextual_thanks_reply(service_fulfilled: bool) -> str:
    """A polite exit differs from thanks after actual fulfillment."""
    return "تدللون، بالخدمة" if service_fulfilled else "اهلاً وسهلاً"
