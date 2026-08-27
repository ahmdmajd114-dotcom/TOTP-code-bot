"""Pure timing rules for live customer-conversation context."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def is_within_context_gap(
    last_message_at: str | datetime | None,
    *,
    now: datetime | None = None,
    gap_minutes: int = 30,
) -> bool:
    """A context remains active only until the configured silence gap elapses."""
    if not last_message_at:
        return False
    try:
        last = (
            last_message_at
            if isinstance(last_message_at, datetime)
            else datetime.fromisoformat(str(last_message_at).replace("Z", "+00:00"))
        )
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    elapsed = current - last
    return timedelta(0) <= elapsed < timedelta(minutes=gap_minutes)
