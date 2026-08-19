import unittest
from datetime import datetime

from chatgpt_sales_flow import (
    asks_payment_guidance,
    can_request_account_code,
    decide_code_retry,
    is_acknowledgement,
    is_ambiguous_followup,
    is_chatgpt_support_issue,
    is_payment_claim,
    is_private_chatgpt_plan,
    resolve_plan_choice,
    should_review_payment_photo,
    is_paid_amount_sufficient,
    classify_receipt_recency,
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
        self.assertTrue(asks_payment_guidance("ادفع اول لو تدزلي الحساب"))
        self.assertFalse(asks_payment_guidance("شنو"))
        self.assertFalse(asks_payment_guidance("تمام"))

    def test_payment_claim_is_distinct_from_payment_question(self):
        self.assertTrue(is_payment_claim("تمام حولت"))
        self.assertTrue(is_payment_claim("دفعت"))
        self.assertFalse(is_payment_claim("شلون ادفع"))

    def test_chatgpt_support_is_never_treated_as_catalog_interest(self):
        self.assertTrue(is_chatgpt_support_issue("عندي مشكلة تشات"))
        self.assertTrue(is_chatgpt_support_issue("جات ما يفتح"))
        self.assertTrue(is_chatgpt_support_issue("حساب ChatGPT ما يشتغل"))
        self.assertFalse(is_chatgpt_support_issue("اريد اشتراك تشات"))
        self.assertFalse(is_chatgpt_support_issue("كم سعر ChatGPT"))

    def test_private_plan_never_uses_shared_delivery(self):
        self.assertTrue(is_private_chatgpt_plan("اشتراك شهر خاص"))
        self.assertFalse(is_private_chatgpt_plan("اشتراك شهر مشترك"))

    def test_code_retry_sequence(self):
        self.assertEqual(decide_code_retry(0, False).action, "send_code")
        self.assertEqual(decide_code_retry(1, False).attempt_count, 2)
        restart = decide_code_retry(2, False)
        self.assertEqual((restart.action, restart.attempt_count, restart.awaiting_restart), ("ask_restart", 3, True))
        self.assertEqual(decide_code_retry(3, True).attempt_count, 4)
        self.assertEqual(decide_code_retry(4, False).attempt_count, 5)
        self.assertEqual(decide_code_retry(5, False).action, "stop")

    def test_payment_photo_is_only_reviewed_inside_payment_flow(self):
        self.assertFalse(should_review_payment_photo("observing"))
        self.assertFalse(should_review_payment_photo("awaiting_plan_choice"))
        self.assertTrue(should_review_payment_photo("awaiting_payment"))
        self.assertTrue(should_review_payment_photo("awaiting_payment_proof"))

    def test_payment_amount_accepts_equal_or_higher_only(self):
        self.assertFalse(is_paid_amount_sufficient(8, 7_999))
        self.assertTrue(is_paid_amount_sufficient(8, 8_000))
        self.assertTrue(is_paid_amount_sufficient(8, 15_000))

    def test_receipt_recency_uses_the_receipt_date_not_a_model_estimate(self):
        now = datetime(2026, 8, 16, 19, 3)
        self.assertEqual(classify_receipt_recency("2026-08-16 18:49", now), "recent")
        self.assertEqual(classify_receipt_recency("16/07/2026 15:06", now), "old")
        self.assertEqual(classify_receipt_recency("2026-08-16 21:30", now), "future")

    def test_code_is_not_available_before_account_delivery(self):
        self.assertFalse(can_request_account_code("payment_verified"))
        self.assertFalse(can_request_account_code("private_activation_pending"))
        self.assertTrue(can_request_account_code("account_delivered"))
        self.assertTrue(can_request_account_code("code_sent"))


if __name__ == "__main__":
    unittest.main()
