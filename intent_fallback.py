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


def infer_greeting_category(text: str) -> str | None:
    """Recognize common Iraqi greeting variants, including stretched spellings."""
    normalized = normalize_arabic_text(text)
    if re.match(r"^(?:السلام|سلام)(?:\s+عليكم)?(?:\b|$)", normalized):
        return "سلام"
    first_word = normalized.split(maxsplit=1)[0] if normalized else ""
    if (
        re.fullmatch(r"ه(?:لا|لو)و*", first_word)
        or first_word in {"مرحبا", "مرحبتين", "اهلا", "اهلين", "hi", "hello", "hey"}
    ):
        return "ترحيب"
    return None


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
    """Keep one greeting before a real request, while dropping incidental thanks."""
    ordered = list(dict.fromkeys(categories))
    if any(category not in CONVERSATIONAL_CATEGORIES for category in ordered):
        greeting = "سلام" if "سلام" in ordered else "ترحيب" if "ترحيب" in ordered else None
        actions = [category for category in ordered if category not in CONVERSATIONAL_CATEGORIES]
        return ([greeting] if greeting else []) + actions
    return ordered


def contextual_thanks_reply(service_fulfilled: bool) -> str:
    """A polite exit differs from thanks after actual fulfillment."""
    return "تدللون، بالخدمة" if service_fulfilled else "اهلاً وسهلاً"


def feedback_reply_is_positive(text: str) -> bool:
    """Return whether an Iraqi-Arabic follow-up clearly expresses satisfaction.

    This deliberately evaluates phrases rather than treating every occurrence of
    words such as "مشكلة" or "تقصير" as negative.  Customers often mention a
    past problem while thanking the team for resolving it.
    """
    normalized = normalize_arabic_text(text)
    if not normalized:
        return False

    # An unresolved problem or an explicit complaint must always reach the owner,
    # even if the customer begins politely with "شكراً".
    explicit_negative_phrases = (
        "مو زين", "مو حلو", "سيء", "زفت", "ما يشتغل", "مايفتح",
        "ما انحلت", "ما انحل", "ما انحلت المشكله", "ما ساعدتوني",
        "ما فادتني", "اريد تعويض", "اريد استرجاع", "استرجاع فلوسي",
        "تقصير منكم", "قصرتوا وياي",
    )
    if any(normalize_arabic_text(phrase) in normalized for phrase in explicit_negative_phrases):
        return False

    positive_phrases = (
        "ما كان اكو تقصير", "ماكو تقصير", "ما قصرتوا", "ماقصرتوا",
        "عاشت ايدكم", "عاشوا ايدكم", "شكرا جزيلا", "شكرا الكم",
        "اشكركم", "تجربتي ممتعه", "تجربه ممتعه", "تجربة مفيدة",
        "تجربه مفيده", "راضي عن الخدمه", "راضية عن الخدمة",
        "انحلت المشكله", "انحل الموضوع", "حليتوا المشكله",
        "ساعدتوني", "اخذتوا من وقتكم", "بارك الله بيكم",
    )
    if any(normalize_arabic_text(phrase) in normalized for phrase in positive_phrases):
        return True

    # Short, unambiguous approvals are safe too.  Do not use broad terms such as
    # "كلش" alone: they occur in both praise and complaints.
    positive_words = ("ممتاز", "ممتعه", "مفيده", "راضي", "راضية", "تمام")
    return any(normalize_arabic_text(word) in normalized for word in positive_words)


def is_owner_payment_shortcut(text: str) -> bool:
    """Recognize the owner's exact in-chat shortcut for sending payment details."""
    return normalize_arabic_text(text) in {"دفع", "طرق الدفع"}
