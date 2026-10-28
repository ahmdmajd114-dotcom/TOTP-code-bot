import unittest

from payment_sheet_logic import choose_payment_worksheet_title


PAYMENT_HEADERS = [
    "التاريخ والوقت", "المبلغ الكلي", "طرق الدفع", "المنتج",
    "الزبون", "حساب ChatGPT", "Chat ID",
]


class PaymentWorksheetSelectionTests(unittest.TestCase):
    def test_finds_arabic_tab_when_default_is_english(self):
        selected = choose_payment_worksheet_title("Sheet1", [
            ("ديون", ["التاريخ", "Chat ID", "الزبون", "المنتج", "المتبقي"]),
            ("الورقة1", PAYMENT_HEADERS),
        ])
        self.assertEqual(selected, "الورقة1")

    def test_prefers_configured_valid_tab(self):
        selected = choose_payment_worksheet_title("دفعات المتجر", [
            ("الورقة1", PAYMENT_HEADERS),
            ("دفعات المتجر", PAYMENT_HEADERS),
        ])
        self.assertEqual(selected, "دفعات المتجر")

    def test_rejects_unrelated_tabs(self):
        selected = choose_payment_worksheet_title("Sheet1", [
            ("ديون", ["رقم الدين", "الحالة"]),
            ("المصروفات", ["السبب", "المصروف"]),
        ])
        self.assertIsNone(selected)


if __name__ == "__main__":
    unittest.main()
