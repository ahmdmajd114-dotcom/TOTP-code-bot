import unittest

from interactive_classifier import infer_action_from_archive_reply


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


if __name__ == "__main__":
    unittest.main()
