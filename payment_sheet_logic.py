"""Pure helpers for locating the payments worksheet safely."""

from __future__ import annotations


def normalize_header(value: object) -> str:
    return " ".join(str(value or "").strip().lower().replace("_", " ").split())


def payment_worksheet_score(title: str, headers: list[str], configured_title: str) -> int:
    """Score a worksheet without relying on an English default tab name."""
    normalized = [normalize_header(value) for value in headers[:8]]
    joined = " | ".join(normalized)
    score = 0
    if title == configured_title:
        score += 5
    if title in {"الورقة1", "Sheet1"}:
        score += 4
    signals = (
        ("تاريخ", "date"),
        ("مبلغ", "مجموع", "total", "amount"),
        ("دفع", "payments", "payment"),
        ("منتج", "product"),
        ("زبون", "عميل", "customer"),
        ("chat id", "chatid", "معرف"),
    )
    for alternatives in signals:
        if any(term in joined for term in alternatives):
            score += 3
    if len(headers) >= 7:
        score += 2
    return score


def choose_payment_worksheet_title(
    configured_title: str, worksheet_headers: list[tuple[str, list[str]]]
) -> str | None:
    """Choose only a high-confidence worksheet; never silently pick a random tab."""
    if not worksheet_headers:
        return None
    ranked = sorted(
        (
            (payment_worksheet_score(title, headers, configured_title), title)
            for title, headers in worksheet_headers
        ),
        reverse=True,
    )
    best_score, best_title = ranked[0]
    return best_title if best_score >= 12 else None
