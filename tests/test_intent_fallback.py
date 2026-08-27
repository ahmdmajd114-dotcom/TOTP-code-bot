import unittest

from intent_fallback import (
    contextual_thanks_reply,
    normalize_arabic_text,
    parse_faq_intent,
    prioritize_action_categories,
)


class ArabicNormalizationTests(unittest.TestCase):
    def test_tanween_and_diacritics_do_not_break_thanks(self):
        self.assertEqual(normalize_arabic_text("شُكْرًا جَزِيلًا"), "شكرا جزيلا")

    def test_tatweel_and_exaggerated_letters_are_normalized(self):
        self.assertEqual(normalize_arabic_text("شــــكرااا"), "شكرا")


class FAQIntentParserTests(unittest.TestCase):
    def test_accepts_only_allowlisted_categories(self):
        result = parse_faq_intent(
            '{"categories":["شكر","طلب_كود"],"confidence":0.96}',
            {"شكر", "ترحيب"},
        )
        self.assertEqual(result.categories, ("شكر",))
        self.assertEqual(result.confidence, 0.96)

    def test_rejects_plain_text_and_invalid_confidence(self):
        self.assertEqual(parse_faq_intent("شكر", {"شكر"}).categories, ())
        self.assertEqual(
            parse_faq_intent('{"categories":["شكر"],"confidence":4}', {"شكر"}).categories,
            (),
        )


class LiveContextPolicyTests(unittest.TestCase):
    def test_product_request_outranks_greeting_and_thanks(self):
        self.assertEqual(
            prioritize_action_categories(["سلام", "catalog:p1", "شكر"]),
            ["catalog:p1"],
        )

    def test_conversational_only_categories_are_preserved(self):
        self.assertEqual(
            prioritize_action_categories(["سلام", "شكر"]),
            ["سلام", "شكر"],
        )

    def test_thanks_reply_depends_on_fulfilled_service(self):
        self.assertEqual(contextual_thanks_reply(False), "اهلاً وسهلاً")
        self.assertEqual(contextual_thanks_reply(True), "تدللون، بالخدمة")


if __name__ == "__main__":
    unittest.main()
