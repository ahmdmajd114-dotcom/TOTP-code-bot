import unittest

from chatgpt_sales_flow import (
    asks_payment_guidance,
    is_acknowledgement,
    is_ambiguous_followup,
    is_payment_claim,
    is_private_chatgpt_plan,
    resolve_plan_choice,
)


PLANS = [
    {"id": "shared-1", "name": "اشتراك شهر مشترك", "price": 8, "is_active": True},
    {"id": "shared-2", "name": "اشتراك شهرين مشترك", "price": 15, "is_active": True},
    {"id": "private-1", "name": "اشتراك شهر خاص", "price": 25, "is_active": True},
    {"id": "private-2", "name": "اشتراك شهرين خاص", "price": 35, "is_active": True},
]


class ChatGPTPlanChoiceTests(unittest.TestCase):
    def assert_plan(self, messages, expected_id):
        choice = resolve_plan_choice(messages, PLANS)
        self.assertIsNotNone(choice.plan)
        self.assertEqual(choice.plan["id"], expected_id)

    def test_month_without_type_never_guesses_shared(self):
        choice = resolve_plan_choice(["اريد جات شهر"], PLANS)
        self.assertIsNone(choice.plan)
        self.assertEqual(choice.missing, "clarify_plan_type")

    def test_type_without_duration_never_guesses_month(self):
        choice = resolve_plan_choice(["اريد جات خاص"], PLANS)
        self.assertIsNone(choice.plan)
        self.assertEqual(choice.missing, "clarify_plan_duration")

    def test_choice_is_merged_across_messages(self):
        self.assert_plan(["اريد جات شهر", "خاص"], "private-1")

    def test_shared_two_months_is_resolved(self):
        self.assert_plan(["اريد جات", "مشترك", "شهرين"], "shared-2")

    def test_direct_complete_choice_is_resolved(self):
        self.assert_plan(["اريد جات مشترك شهر"], "shared-1")

    def test_unique_price_is_a_valid_choice(self):
        self.assert_plan(["اريد باقة 25"], "private-1")

    def test_ambiguous_or_empty_choice_requests_clarification(self):
        choice = resolve_plan_choice(["اختارته"], PLANS)
        self.assertIsNone(choice.plan)
        self.assertEqual(choice.missing, "request_plan_choice")

    def test_ambiguous_followup_is_not_guessed(self):
        self.assertTrue(is_ambiguous_followup("شنو عدكم غيره"))
        self.assertFalse(is_ambiguous_followup("شنو عدكم غير الشات؟"))
        self.assertFalse(is_ambiguous_followup("شنو اسوي حتى ادفع"))

    def test_acknowledgement_needs_no_reply(self):
        self.assertTrue(is_acknowledgement("تمام"))
        self.assertTrue(is_acknowledgement("اوكي"))
        self.assertFalse(is_acknowledgement("زين شكد ادفع"))

    def test_payment_guidance_needs_a_real_question(self):
        self.assertTrue(asks_payment_guidance("شنو اسوي حتى ادفع"))
        self.assertTrue(asks_payment_guidance("شلون ادفع"))
        self.assertFalse(asks_payment_guidance("شنو"))
        self.assertFalse(asks_payment_guidance("تمام"))

    def test_payment_claim_is_distinct_from_payment_question(self):
        self.assertTrue(is_payment_claim("تمام حولت"))
        self.assertTrue(is_payment_claim("دفعت"))
        self.assertFalse(is_payment_claim("شلون ادفع"))

    def test_private_plan_never_uses_shared_delivery(self):
        self.assertTrue(is_private_chatgpt_plan("اشتراك شهر خاص"))
        self.assertFalse(is_private_chatgpt_plan("اشتراك شهر مشترك"))


if __name__ == "__main__":
    unittest.main()
