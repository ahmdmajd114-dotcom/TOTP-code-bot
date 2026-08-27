import unittest

from photo_intent import PhotoIntent, is_confident_code_verification, parse_photo_intent


class PhotoIntentTests(unittest.TestCase):
    def test_parses_code_verification_json_from_fenced_response(self):
        result = parse_photo_intent(
            '```json\n{"intent":"code_verification","confidence":0.94,'
            '"description":"شاشة ChatGPT تطلب رمز تحقق"}\n```'
        )

        self.assertEqual(result.intent, "code_verification")
        self.assertEqual(result.confidence, 0.94)
        self.assertIn("ChatGPT", result.description)
        self.assertTrue(is_confident_code_verification(result))

    def test_rejects_unknown_intent_as_safe_other(self):
        self.assertEqual(
            parse_photo_intent('{"intent":"send_code","confidence":1,"description":""}'),
            PhotoIntent(),
        )

    def test_low_confidence_code_does_not_authorize_code_path(self):
        result = parse_photo_intent(
            '{"intent":"code_verification","confidence":0.6,"description":"رقم ظاهر"}'
        )

        self.assertFalse(is_confident_code_verification(result))

    def test_payment_receipt_never_authorizes_code_path(self):
        result = parse_photo_intent(
            '{"intent":"payment_receipt","confidence":0.99,"description":"تحويل ناجح"}'
        )

        self.assertFalse(is_confident_code_verification(result))


if __name__ == "__main__":
    unittest.main()
