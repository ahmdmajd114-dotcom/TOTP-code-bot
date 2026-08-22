import unittest

from interactive_classifier import (
    GroundedAnswer,
    IntentFrame,
    guard_interactive_action,
    infer_action_from_archive_reply,
    infer_intent_from_archive_reply,
    is_product_availability_followup,
    is_support_cancellation,
    parse_intent_frame,
    parse_grounded_answer,
    should_enter_support_mode,
    should_switch_from_support,
    support_action_for_turn,
)


class ArchiveActionInferenceTests(unittest.TestCase):
    TEMPLATES = {
        "closing": "أهلين وسهلين، تدلل.",
        "clarify_plan_type": "تدلل، تريده خاص لو مشترك؟",
    }

    def test_exact_template_gets_action_label(self):
        self.assertEqual(
            infer_action_from_archive_reply("أهلين وسهلين، تدلل.", self.TEMPLATES),
            "closing",
        )

    def test_known_phrase_gets_action_label(self):
        self.assertEqual(
            infer_action_from_archive_reply("بلا زحمة دزلي صورة الوصل", {}),
            "request_payment_proof",
        )

    def test_unknown_owner_reply_is_not_guessed(self):
        self.assertIsNone(infer_action_from_archive_reply("أكيد حاضر", {}))

    def test_archive_is_exposed_as_intent_not_executable_action(self):
        self.assertEqual(
            infer_intent_from_archive_reply("بلا زحمة دزلي صورة الوصل", {}),
            "payment_claim",
        )


class StructuredIntentTests(unittest.TestCase):
    def test_model_output_is_parsed_as_meaning_not_action(self):
        frame = parse_intent_frame(
            '{"intent":"support","product":"chatgpt","plan_type":null,'
            '"duration":null,"confidence":0.96}'
        )
        self.assertEqual(
            frame,
            IntentFrame("support", "chatgpt", None, None, 0.96),
        )

    def test_malformed_or_invented_intent_is_safe(self):
        self.assertEqual(parse_intent_frame("payment_methods"), IntentFrame())
        self.assertEqual(
            parse_intent_frame('{"intent":"sell_everything","confidence":1}').intent,
            "other",
        )

    def test_any_known_product_can_survive_structured_parsing(self):
        frame = parse_intent_frame(
            '{"intent":"purchase","product":"canva","plan_type":null,'
            '"duration":null,"confidence":0.91}'
        )
        self.assertEqual(frame.product, "canva")

    def test_grounded_answer_requires_explicit_support_and_confidence(self):
        answer = parse_grounded_answer(
            '{"can_answer":true,"answer":"نعم متوفر اشتراك سنة.","confidence":0.94}'
        )
        self.assertEqual(
            answer,
            GroundedAnswer(True, "نعم متوفر اشتراك سنة.", 0.94),
        )

    def test_grounded_answer_rejects_plain_text_and_abstention(self):
        self.assertEqual(parse_grounded_answer("أكيد موجود"), GroundedAnswer())
        self.assertEqual(
            parse_grounded_answer(
                '{"can_answer":false,"answer":"تخمين","confidence":0.99}'
            ),
            GroundedAnswer(),
        )


class ConversationInvariantTests(unittest.TestCase):
    def test_problem_outranks_purchase_in_same_message(self):
        self.assertTrue(
            should_enter_support_mode(
                "اريد اشترك بس هسه عندي مشكلة",
                "الزبون: اريد جات خاص شهرين",
                "awaiting_payment",
                True,
            )
        )

    def test_generic_problem_uses_recent_chatgpt_context(self):
        self.assertTrue(
            should_enter_support_mode(
                "هسه عندي مشكلة",
                "اريد اشتراك جات خاص",
                "observing",
                False,
            )
        )

    def test_unrelated_generic_problem_does_not_assume_chatgpt(self):
        self.assertFalse(
            should_enter_support_mode(
                "عندي مشكلة",
                "هلو",
                "observing",
                False,
            )
        )

    def test_support_can_start_for_any_known_product(self):
        self.assertTrue(
            should_enter_support_mode(
                "كانفا ما يشتغل",
                "",
                "observing",
                False,
                current_mentions_known_product=True,
            )
        )

    def test_customer_can_cancel_support_topic(self):
        self.assertTrue(is_support_cancellation("زين عوف المشكلة"))
        self.assertTrue(
            should_switch_from_support(
                "support_review", "زين عوف المشكلة", False
            )
        )

    def test_explicit_new_product_interrupts_support(self):
        self.assertTrue(
            should_switch_from_support(
                "support_review", "اريد كانفا عدكم؟", True
            )
        )

    def test_short_availability_question_uses_product_context(self):
        self.assertTrue(is_product_availability_followup("عدكم لو لا؟"))
        self.assertTrue(is_product_availability_followup("زين شكد سعره"))
        self.assertFalse(is_product_availability_followup("زين تمام"))
        self.assertFalse(
            should_switch_from_support(
                "support_review", "انكي ما يشتغل", True
            )
        )

    def test_support_mode_can_never_fall_back_to_prices(self):
        self.assertEqual(
            guard_interactive_action(
                "chatgpt_plans", "support_review", False, False
            ),
            "request_support_screenshot",
        )

    def test_support_screenshot_is_requested_once(self):
        self.assertEqual(
            support_action_for_turn("awaiting_payment", "عندي مشكلة"),
            "request_support_screenshot",
        )
        self.assertEqual(
            support_action_for_turn("support_review", "اقصد عندي مشكلة جات"),
            "no_reply",
        )

    def test_price_and_payment_require_a_confirmed_plan(self):
        for action in ("selected_plan_price", "payment_methods", "payment_next_step"):
            with self.subTest(action=action):
                self.assertEqual(
                    guard_interactive_action(action, "observing", False, False),
                    "request_plan_choice",
                )

    def test_code_requires_delivered_account(self):
        self.assertEqual(
            guard_interactive_action("code_request", "awaiting_payment", True, False),
            "handoff",
        )


if __name__ == "__main__":
    unittest.main()
