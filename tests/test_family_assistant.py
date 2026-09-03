from datetime import date
import unittest

from family_assistant import (
    ANKI_PRICE_IQD,
    AnkiTopic,
    anki_family_guidance,
    review_anki_receipt,
)


class AnkiFamilyGuidanceTests(unittest.TestCase):
    def test_offer_has_the_fixed_price_and_no_permanent_guarantee(self):
        guidance = anki_family_guidance(AnkiTopic.OFFER)
        self.assertIn("5 آلاف", guidance.customer_reply)
        self.assertIn("ما لم ينحذف", guidance.customer_reply)

    def test_installation_requires_owner_and_does_not_keep_codes(self):
        guidance = anki_family_guidance(AnkiTopic.INSTALLATION)
        self.assertTrue(guidance.requires_owner)
        self.assertTrue(any("لا تحفظوه" in item for item in guidance.family_checklist))


class AnkiReceiptReviewTests(unittest.TestCase):
    TODAY = date(2026, 9, 3)

    def test_requires_full_receipt_data(self):
        result = review_anki_receipt(
            method="ماستر", amount_iqd=None, recipient_matches_store=True,
            receipt_date=self.TODAY, today=self.TODAY,
        )
        self.assertEqual(result.status, "needs_receipt")

    def test_rejects_old_or_wrong_amount_receipt(self):
        result = review_anki_receipt(
            method="زين كاش", amount_iqd=ANKI_PRICE_IQD - 1,
            recipient_matches_store=True, receipt_date=self.TODAY, today=self.TODAY,
        )
        self.assertEqual(result.status, "mismatch")

    def test_credit_requires_line_confirmation(self):
        result = review_anki_receipt(
            method="رصيد اثير", amount_iqd=ANKI_PRICE_IQD,
            recipient_matches_store=True, receipt_date=self.TODAY, today=self.TODAY,
            credit_received_on_line=False,
        )
        self.assertEqual(result.status, "needs_manual_review")

    def test_valid_cash_transfer_is_eligible_not_auto_approved(self):
        result = review_anki_receipt(
            method="ماستر", amount_iqd=ANKI_PRICE_IQD,
            recipient_matches_store=True, receipt_date=self.TODAY, today=self.TODAY,
        )
        self.assertEqual(result.status, "eligible_for_delivery")


if __name__ == "__main__":
    unittest.main()
