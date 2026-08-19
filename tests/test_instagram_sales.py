import unittest
from decimal import Decimal

from instagram_sales import commission_for, normalize_chat_type, parse_amount


class InstagramSalesTests(unittest.TestCase):
    def test_parse_amount_accepts_commas_and_rejects_empty(self):
        self.assertEqual(parse_amount("25,000 دينار"), 25000)
        self.assertIsNone(parse_amount(""))

    def test_commission_is_twenty_five_percent(self):
        self.assertEqual(commission_for(25000), 6250)
        self.assertEqual(commission_for(1000, Decimal("12.5")), 125)

    def test_chat_type_is_normalized(self):
        self.assertEqual(normalize_chat_type("خاص"), "خاص")
        self.assertEqual(normalize_chat_type("shared"), "مشترك")
        self.assertIsNone(normalize_chat_type("كانفا"))


if __name__ == "__main__":
    unittest.main()
