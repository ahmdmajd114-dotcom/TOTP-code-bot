import unittest

from catalog_logic import (
    catalog_category,
    catalog_product_id,
    format_customer_catalog_reply,
    match_catalog_products,
)


PRODUCTS = [
    {"id": "p1", "name": "Canva", "aliases": ["كانفا", "كنفا"], "is_active": True},
    {"id": "p2", "name": "قديم", "aliases": ["قديم"], "is_active": False},
]


class CatalogMatchingTests(unittest.TestCase):
    def test_matches_live_alias_with_arabic_diacritics(self):
        matches = match_catalog_products("عندكم كَانْفَا؟", PRODUCTS)
        self.assertEqual([item["id"] for item in matches], ["p1"])

    def test_does_not_match_disabled_product_or_partial_word(self):
        self.assertEqual(match_catalog_products("قديم", PRODUCTS), [])
        self.assertEqual(match_catalog_products("مكانفات", PRODUCTS), [])

    def test_catalog_category_round_trip(self):
        self.assertEqual(catalog_product_id(catalog_category("abc")), "abc")


class CatalogReplyTests(unittest.TestCase):
    def test_reply_uses_only_current_active_plans(self):
        reply = format_customer_catalog_reply(
            PRODUCTS[0],
            [
                {"name": "سنة", "price": 25, "duration": "12 شهر", "description": "خاص", "is_active": True},
                {"name": "قديم", "price": 10, "is_active": False},
            ],
        )
        self.assertIn("سنة لمدة 12 شهر، سعره 25 ألف.", reply)
        self.assertIn("ملاحظة: خاص", reply)
        self.assertNotIn("قديم", reply)

    def test_chatgpt_plans_are_rendered_as_natural_customer_lines(self):
        reply = format_customer_catalog_reply(
            {"id": "chat", "name": "ChatGPT", "is_active": True},
            [
                {"name": "اشتراك شهر مشترك", "price": 8, "duration": "شهر", "is_active": True},
                {"name": "اشتراك شهر خاص", "price": 28000, "duration": "شهر", "description": "تفعيل على حسابك", "is_active": True},
                {"name": "اشتراك خاص شهرين", "price": 39, "duration": "شهرين", "is_active": True},
            ],
        )

        self.assertIn("- شهر مشترك، سعره 8 آلاف.", reply)
        self.assertIn("- شهر خاص، سعره 28 ألف.", reply)
        self.assertIn("- شهرين خاص، سعره 39 ألف.", reply)
        self.assertIn("ملاحظة: تفعيل على حسابك", reply)

    def test_no_active_plan_means_no_customer_offer(self):
        self.assertIsNone(format_customer_catalog_reply(PRODUCTS[0], []))


if __name__ == "__main__":
    unittest.main()
