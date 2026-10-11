import unittest
from datetime import datetime, timezone

from payment_link_logic import find_current_paid_delivery_row


def is_chatgpt(value):
    return value.strip().casefold() in {"chatgpt", "جات"}


class PaymentLinkMatchingTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
        self.header = ["date", "total", "payment", "product", "customer", "account", "chat_id"]

    def test_matches_iso_payment_before_link(self):
        row = ["2026-10-11 11:55", "8000", "ماستر 8000", "ChatGPT", "Ali", "", "123"]
        self.assertEqual(
            find_current_paid_delivery_row([self.header, row], 123, is_chatgpt, now=self.now),
            (2, row),
        )

    def test_matches_locale_formatted_sheet_date(self):
        row = ["11/10/2026 11:55", "8000", "ماستر 8000", "ChatGPT", "Ali", "", "123"]
        self.assertEqual(
            find_current_paid_delivery_row([self.header, row], 123, is_chatgpt, now=self.now),
            (2, row),
        )

    def test_unparseable_date_is_safe_only_before_account_is_attached(self):
        pending = ["١١ تشرين الأول", "8000", "ماستر 8000", "جات", "Ali", "", "123"]
        linked = ["١١ تشرين الأول", "8000", "ماستر 8000", "جات", "Ali", "a@b.com", "123"]
        self.assertEqual(
            find_current_paid_delivery_row([self.header, pending], 123, is_chatgpt, now=self.now),
            (2, pending),
        )
        self.assertIsNone(
            find_current_paid_delivery_row([self.header, linked], 123, is_chatgpt, now=self.now)
        )

    def test_does_not_reuse_old_payment(self):
        row = ["2026-09-01 10:00", "8000", "ماستر 8000", "ChatGPT", "Ali", "", "123"]
        self.assertIsNone(
            find_current_paid_delivery_row([self.header, row], 123, is_chatgpt, now=self.now)
        )


if __name__ == "__main__":
    unittest.main()
