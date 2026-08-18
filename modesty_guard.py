"""فلتر محلي لكلام الغزل في محادثة محددة وموافق عليها."""

from __future__ import annotations

import re


# كلمات واضحة فقط؛ التعمد هنا تقليل الحذف الخاطئ، ولا نرسل النص إلى AI.
FLIRTING_TERMS = frozenset({
    "حبيبي", "حبيبتي", "حبي", "حبيب", "عمري", "روحي", "قلبي", "حياتي",
    "عيوني", "عيني", "احبك", "احبج", "احبچ", "مشتاقلك", "مشتاقلج",
    "مشتاقلچ", "اشتاقلك", "اشتاقلج", "اشتاقلچ", "غرام", "عشق", "بوسه",
    "بوسة", "قبله", "قبلة", "قبلات",
})


def normalized_words(text: str | None) -> set[str]:
    value = (text or "").lower()
    value = re.sub(r"[أإآٱ]", "ا", value)
    value = value.replace("ى", "ي").replace("ة", "ه")
    value = re.sub(r"[^\w\u0600-\u06ff]+", " ", value)
    return {word for word in value.split() if word}


def is_flirtatious_text(text: str | None) -> bool:
    """يعيد True فقط لعبارات غزل صريحة متفق عليها."""
    return bool(normalized_words(text) & FLIRTING_TERMS)


def is_guarded_chat(chat_id: int, configured_chat_id: int) -> bool:
    """لا يعمل الفلتر من دون معرف محادثة صريح."""
    return bool(configured_chat_id and chat_id == configured_chat_id)
