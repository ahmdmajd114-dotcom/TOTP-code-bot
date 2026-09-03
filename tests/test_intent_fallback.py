import unittest

from intent_fallback import (
    contextual_thanks_reply,
    feedback_reply_is_positive,
    has_thanks_signal,
    infer_greeting_category,
    is_owner_payment_shortcut,
    normalize_arabic_text,
    parse_faq_intent,
    prioritize_action_categories,
)


class ArabicNormalizationTests(unittest.TestCase):
    def test_tanween_and_diacritics_do_not_break_thanks(self):
        self.assertEqual(normalize_arabic_text("شُكْرًا جَزِيلًا"), "شكرا جزيلا")

    def test_tatweel_and_exaggerated_letters_are_normalized(self):
        self.assertEqual(normalize_arabic_text("شــــكرااا"), "شكرا")

    def test_understands_iraqi_greeting_before_a_product_request(self):
        self.assertEqual(infer_greeting_category("هلاو رايد جات"), "ترحيب")
        self.assertEqual(infer_greeting_category("هلوو أريد كانفا"), "ترحيب")

    def test_distinguishes_salam_from_generic_greeting(self):
        self.assertEqual(infer_greeting_category("سلام أريد أمبوس"), "سلام")
        self.assertIsNone(infer_greeting_category("أريد أمبوس"))


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
    def test_greeting_is_kept_before_product_and_incidental_thanks_is_dropped(self):
        self.assertEqual(
            prioritize_action_categories(["سلام", "catalog:p1", "شكر"]),
            ["سلام", "catalog:p1"],
        )

    def test_religious_greeting_outranks_generic_greeting(self):
        self.assertEqual(
            prioritize_action_categories(["ترحيب", "سلام", "catalog:p1"]),
            ["سلام", "catalog:p1"],
        )

    def test_conversational_only_categories_are_preserved(self):
        self.assertEqual(
            prioritize_action_categories(["سلام", "شكر"]),
            ["سلام", "شكر"],
        )

    def test_thanks_reply_depends_on_fulfilled_service(self):
        self.assertEqual(contextual_thanks_reply(False), "أهلاً وسهلاً")
        self.assertEqual(contextual_thanks_reply(True), "تدللون، بالخدمة")

    def test_thanks_variants_survive_diacritics_spelling_and_emoji(self):
        for message in ("شُكْرًا 🌷", "أشكرك", "أشكركم 🌷", "عاشت إيدكم", "عاشت ايديكم 🙏", "ما قصرتوا"):
            with self.subTest(message=message):
                self.assertTrue(has_thanks_signal(message))


class FeedbackSatisfactionTests(unittest.TestCase):
    def test_customer_message_in_screenshot_is_positive_despite_mentioning_problem(self):
        text = (
            "عاشت ايدكم وشكرا جزيلا ما كان اكو اي تقصير من ناحيتكم "
            "وساعدتوني هواي وحاولتوا حل المشكلة واخدت من وقتكم "
            "الله يوفقكم يارب"
        )
        self.assertTrue(feedback_reply_is_positive(text))

    def test_past_problem_that_was_resolved_is_positive(self):
        self.assertTrue(feedback_reply_is_positive("شكراً، ساعدتوني وانحلت المشكلة"))

    def test_unresolved_problem_is_not_eligible_for_rating_link(self):
        self.assertFalse(feedback_reply_is_positive("شكراً بس ما انحلت المشكلة واريد تعويض"))

    def test_empty_or_vague_reply_is_not_eligible_for_rating_link(self):
        self.assertFalse(feedback_reply_is_positive("المشكلة موجودة"))


class OwnerPaymentShortcutTests(unittest.TestCase):
    def test_exact_payment_shortcuts_are_recognized(self):
        self.assertTrue(is_owner_payment_shortcut("دفع"))
        self.assertTrue(is_owner_payment_shortcut("طُرُق الدفع"))

    def test_regular_message_is_not_mistaken_for_a_shortcut(self):
        self.assertFalse(is_owner_payment_shortcut("تقدر تدفع على زين كاش"))


if __name__ == "__main__":
    unittest.main()
