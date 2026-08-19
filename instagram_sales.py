"""Pure helpers for Instagram-attributed sales and commission calculations."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


COMMISSION_PERCENT = Decimal("25")


def parse_amount(text: str) -> int | None:
    """Parse an IQD amount written with optional commas/spaces."""
    value = re.sub(r"[^0-9]", "", text or "")
    if not value:
        return None
    amount = int(value)
    return amount if amount > 0 else None


def commission_for(amount: int, percent: Decimal = COMMISSION_PERCENT) -> int:
    """Return a rounded commission in the same currency unit as amount."""
    result = (Decimal(amount) * percent / Decimal("100")).quantize(
        Decimal("1"), rounding=ROUND_HALF_UP
    )
    return int(result)


def normalize_chat_type(value: str) -> str | None:
    """Only ChatGPT sales may have a private/shared subtype."""
    words = (value or "").strip().lower()
    if words in {"خاص", "private"}:
        return "خاص"
    if words in {"مشترك", "shared"}:
        return "مشترك"
    return None


def format_iqd(amount: int) -> str:
    return f"{amount:,}"
