"""Pure helpers for matching a recorded payment to a later account delivery."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable


def _possible_sheet_datetimes(value: str) -> list[datetime]:
    """Parse common raw and locale-formatted Google Sheets timestamps."""
    raw = str(value or "").strip()
    if not raw:
        return []
    parsed: list[datetime] = []
    for fmt in (
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
        "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y",
        "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%m/%d/%Y",
    ):
        try:
            candidate = datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if candidate not in parsed:
            parsed.append(candidate)
    try:
        iso_candidate = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if iso_candidate.tzinfo is None:
            iso_candidate = iso_candidate.replace(tzinfo=timezone.utc)
        else:
            iso_candidate = iso_candidate.astimezone(timezone.utc)
        if iso_candidate not in parsed:
            parsed.append(iso_candidate)
    except ValueError:
        pass
    return parsed


def find_current_paid_delivery_row(
    rows: list[list[str]],
    chat_id: int,
    is_target_product: Callable[[str], bool],
    *,
    now: datetime | None = None,
    max_age_days: int = 7,
) -> tuple[int, list[str]] | None:
    """Return the newest paid delivery row, tolerant of Sheets date formats.

    Columns follow the bot's payments sheet: date, total, payment method,
    product, customer, delivered account, chat_id. An unparseable date is
    accepted only while the account cell is still empty, which is the strong
    marker for the payment-before-/link workflow.
    """
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    cutoff = current - timedelta(days=max_age_days)
    wanted_chat_id = str(chat_id)

    for index in range(len(rows) - 1, 0, -1):
        row = rows[index]
        if len(row) < 7 or str(row[6]).strip() != wanted_chat_id:
            continue
        if not is_target_product(str(row[3])):
            continue
        total = str(row[1]).strip()
        payment = str(row[2]).strip()
        if not total or not payment or payment == "ملغاة":
            continue

        dates = _possible_sheet_datetimes(str(row[0]))
        if dates:
            if any(cutoff <= candidate <= current + timedelta(days=1) for candidate in dates):
                return index + 1, row
            continue

        account = str(row[5]).strip()
        if not account:
            return index + 1, row
    return None
