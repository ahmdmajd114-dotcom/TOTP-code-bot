import unittest
from datetime import datetime, timedelta, timezone

from conversation_policy import is_within_context_gap


class ConversationContextTimingTests(unittest.TestCase):
    def test_support_context_remains_active_before_thirty_minutes(self):
        now = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)
        self.assertTrue(is_within_context_gap(now - timedelta(minutes=29), now=now))

    def test_thirty_minutes_of_silence_starts_a_new_context(self):
        now = datetime(2026, 8, 27, 15, 0, tzinfo=timezone.utc)
        self.assertFalse(is_within_context_gap(now - timedelta(minutes=30), now=now))

    def test_invalid_timestamp_is_not_an_active_context(self):
        self.assertFalse(is_within_context_gap("not-a-date"))


if __name__ == "__main__":
    unittest.main()
