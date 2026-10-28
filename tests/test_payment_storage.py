import ast
import __future__
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import re
import unittest
import uuid

from payment_storage import PaymentWorksheet, locate_payment_row, payment_datetime
from debt_manager import is_debt_payment_row, is_debt_repayment_row
from payment_link_logic import find_current_paid_delivery_row


HEADER = ["التاريخ والوقت", "المبلغ الكلي", "طرق الدفع", "المنتج", "الزبون", "الحساب", "Chat ID", "رقم العملية"]
PAYMENT = ["2026-10-28 06:23", "50000", "زين كاش 50000", "تحويل ماستر 50 الف", ".", "", "8686963327"]


class FakeWorksheet:
    title = "الورقة1"
    row_count = 1000

    def __init__(self, rows):
        self.rows = deepcopy(rows)
        self.writes = []
        self.fail_after_write = False

    def get_all_values(self, **kwargs):
        return deepcopy(self.rows)

    def row_values(self, row, **kwargs):
        return deepcopy(self.rows[row - 1]) if row <= len(self.rows) else []

    def append_row(self, *args, **kwargs):
        raise AssertionError("Google's inferred append must never be called")

    def update(self, *, range_name, values, value_input_option):
        self.writes.append((range_name, deepcopy(values)))
        match = re.fullmatch(r"A(\d+):H\1", range_name)
        if not match:
            raise AssertionError(f"Unexpected new-payment destination {range_name}")
        row = int(match.group(1))
        self.rows.extend([[] for _ in range(max(0, row - len(self.rows)))])
        self.rows[row - 1] = list(values[0])
        if self.fail_after_write:
            self.fail_after_write = False
            raise TimeoutError("Google saved data but its response was lost")
        return {"updatedRange": f"'الورقة1'!{range_name}"}

    def update_cell(self, row, col, value):
        self.writes.append((row, col, value))
        self.rows[row - 1].extend([""] * max(0, col - len(self.rows[row - 1])))
        self.rows[row - 1][col - 1] = value

    def add_rows(self, count):
        self.row_count += count


def bot_functions(sheet):
    """Execute the real bot functions without contacting Telegram/Supabase."""
    wanted = {
        "append_payment_row", "parse_sheet_amount", "is_debt_payment_row",
        "parse_sheet_datetime", "parse_stats_datetime", "parse_stats_period",
        "get_payment_rows_in_period", "calculate_totals_summary",
        "calculate_income_report", "format_period_label", "payment_stats_date_warning",
        "calculate_product_breakdown",
        "get_current_chatgpt_delivery_payment", "find_completable_chatgpt_row",
    }
    root = Path(__file__).resolve().parents[1]
    selected = []
    for filename in ('bot.py','income_manager.py','reports_manager.py'):
        tree = ast.parse((root / filename).read_text())
        selected.extend(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted)
    ns = {
        "get_google_sheet": lambda: sheet,
        "active_report": lambda *args, **kwargs: None,
        "require_legacy_write": lambda: None,
        "finance_active": lambda: False,
        "FinanceError": RuntimeError,
        "is_debt_payment_row": is_debt_payment_row,
        "is_debt_repayment_row": is_debt_repayment_row,
        "find_current_paid_delivery_row": find_current_paid_delivery_row,
        "CHATGPT_ROW_MATCH_WINDOW_DAYS": 7,
        "datetime": datetime, "timedelta": timedelta, "timezone": timezone,
        "STATS_TIMEZONE": timezone(timedelta(hours=3)),
        "PERIOD_LABELS": {"all": "كل الوقت", "today": "اليوم"},
        "payment_datetime": payment_datetime, "re": re, "uuid": uuid,
        "logger": logging.getLogger("storage-tests"),
        "is_chatgpt_product_name": lambda product: False,
        "format_customer_line": lambda name, username=None: name,
        "get_expense_rows_in_period": lambda period: [],
        "get_instagram_sales_rows_in_period": lambda period: [],
        "get_catalog_products": lambda: [],
        "_stats_catalog_product_for_name": lambda name, products: None,
        "GOOGLE_SHEET_WORKSHEET_NAME": "الورقة1",
    }
    for i, column in enumerate(("DATE", "TOTAL", "PAYMENTS", "PRODUCT", "CUSTOMER", "CHATGPT_ACCOUNT", "CHAT_ID", "OPERATION_ID"), 1):
        ns[f"SHEET_COL_{column}"] = i
    exec(compile(ast.Module(body=selected, type_ignores=[]), "bot-storage-functions", "exec",
                 flags=__future__.annotations.compiler_flag), ns)
    return ns


class PaymentLocationTests(unittest.TestCase):
    def test_reads_original_and_arbitrary_shifted_columns(self):
        for offset in (0, 17, 29, 77, 80, 150):
            with self.subTest(offset=offset):
                row, found = locate_payment_row([""] * offset + PAYMENT)
                self.assertEqual(found, offset)
                self.assertEqual(row[1], "50000")
                self.assertEqual(row[6], "8686963327")

    def test_serial_date_scientific_identifier_and_float_amount(self):
        raw = ["46323.26597222222", 50000.0, "زين كاش 50000", "تحويل", ".", "", "8.686963327E9"]
        row, offset = locate_payment_row([""] * 80 + raw)
        self.assertEqual(offset, 80)
        self.assertEqual(row[:2], ["2026-10-28 06:23:00", "50000"])
        self.assertEqual(row[6], "8686963327")

    def test_keeps_physical_row_numbers_and_legacy_data_untouched(self):
        raw = FakeWorksheet([HEADER, [], [""] * 80 + PAYMENT])
        sheet = PaymentWorksheet(raw)
        before = deepcopy(raw.rows)
        records = sheet.get_all_values()
        self.assertEqual(len(records), 3)
        self.assertEqual(records[2][6], "8686963327")
        self.assertEqual(raw.rows, before)
        self.assertEqual(sheet.last_shifted_rows, 1)

    def test_same_record_in_two_column_blocks_counted_once(self):
        raw = PAYMENT + [""] * (80 - len(PAYMENT)) + PAYMENT
        sheet = PaymentWorksheet(FakeWorksheet([HEADER, raw]))
        self.assertEqual(sum(int(row[1] or 0) for row in sheet.get_all_values()[1:]), 50000)

    def test_distinct_legitimate_payments_same_time_remain_separate(self):
        sheet = PaymentWorksheet(FakeWorksheet([HEADER, PAYMENT, [""] * 80 + PAYMENT]))
        self.assertEqual(sum(int(row[1] or 0) for row in sheet.get_all_values()[1:]), 100000)

    def test_duplicate_operation_id_counted_once_across_rows(self):
        row = PAYMENT + ["PAY-123"]
        sheet = PaymentWorksheet(FakeWorksheet([HEADER, row, [""] * 80 + row]))
        self.assertEqual(sum(int(row[1] or 0) for row in sheet.get_all_values()[1:]), 50000)

    def test_conflicting_operation_ids_fail_instead_of_hiding_money(self):
        first = PAYMENT + ["PAY-123"]
        second = list(first)
        second[1] = "60000"
        sheet = PaymentWorksheet(FakeWorksheet([HEADER, first, second]))
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            sheet.get_all_values()

    def test_multiple_distinct_records_in_same_physical_row_need_review(self):
        other = list(PAYMENT)
        other[6] = "123456789"
        with self.assertRaisesRegex(ValueError, "Multiple"):
            locate_payment_row(PAYMENT + [""] * 73 + other)

    def test_edit_and_cancel_legacy_row_uses_original_columns(self):
        raw = FakeWorksheet([HEADER, [""] * 80 + PAYMENT])
        sheet = PaymentWorksheet(raw)
        sheet.update_cell(2, 2, "")
        sheet.update_cell(2, 3, "ملغاة")
        sheet.update_cell(2, 6, "")
        self.assertEqual([entry[1] for entry in raw.writes], [82, 83, 86])
        self.assertTrue(all(value == "" for value in raw.rows[1][:80]))

    def test_new_payment_is_written_to_fixed_A_H_after_legacy_rows(self):
        raw = FakeWorksheet([HEADER, [""] * 80 + PAYMENT])
        sheet = PaymentWorksheet(raw)
        sheet.append_row(PAYMENT + ["PAY-NEW"])
        self.assertEqual(raw.writes[0][0], "A3:H3")
        self.assertEqual(sheet.row_values(3)[7], "PAY-NEW")
        self.assertEqual(raw.rows[1], [""] * 80 + PAYMENT)

    def test_appending_retries_same_operation_without_duplicate(self):
        raw = FakeWorksheet([HEADER])
        sheet = PaymentWorksheet(raw)
        sheet.append_row(PAYMENT + ["PAY-NEW"])
        sheet.append_row(PAYMENT + ["PAY-NEW"])
        self.assertEqual(len(raw.writes), 1)

    def test_bot_retry_after_lost_response_does_not_duplicate_income(self):
        raw = FakeWorksheet([HEADER])
        sheet = PaymentWorksheet(raw)
        ns = bot_functions(sheet)
        state = {"customer_name": ".", "customer_chat_id": 8686963327,
                 "product": PAYMENT[3], "payments": [("زين كاش", 50000)]}
        raw.fail_after_write = True
        with self.assertLogs("storage-tests", level="ERROR"):
            self.assertFalse(ns["append_payment_row"](state, force_new=True))
        self.assertTrue(ns["append_payment_row"](state, force_new=True))
        self.assertEqual(len(raw.rows), 2)
        self.assertEqual(len(raw.writes), 1)

    def test_new_payment_for_legacy_delivered_account_still_uses_A_H(self):
        old = ["2026-10-28 05:00", "", "", "ChatGPT", ".", "account@example.com", "8686963327"]
        raw = FakeWorksheet([HEADER, [""] * 80 + old])
        sheet = PaymentWorksheet(raw)
        ns = bot_functions(sheet)
        ns["is_chatgpt_product_name"] = lambda product: True
        ns["find_completable_chatgpt_row"] = lambda *args, **kwargs: 2
        state = {"customer_name": ".", "customer_chat_id": 8686963327,
                 "product": "ChatGPT", "payments": [("زين كاش", 50000)]}
        self.assertTrue(ns["append_payment_row"](state))
        self.assertEqual(raw.writes[0][0], "A3:H3")
        self.assertEqual(raw.rows[1], [""] * 80 + old)
        self.assertEqual(sheet.row_values(3)[5], "account@example.com")

    def test_bot_statistics_include_shifted_income_and_exclude_unpaid_cancelled(self):
        debt = list(PAYMENT)
        debt[2] = "دين زين كاش 50000"
        cancelled = list(PAYMENT)
        cancelled[2] = "ملغاة"
        sheet = PaymentWorksheet(FakeWorksheet([HEADER, [""] * 80 + PAYMENT, debt, cancelled]))
        ns = bot_functions(sheet)
        self.assertIn("الدخل: 50000", ns["calculate_totals_summary"]("all"))
        self.assertIn("الدخل: 50000", ns["calculate_totals_summary"]("2026-10-28"))
        self.assertIn("الدخل: 0", ns["calculate_totals_summary"]("2026-10-27"))

    def test_repayment_counts_as_income_without_becoming_new_subscription_payment(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        purchase = [today, "8000", "ماستر 8000", "ChatGPT", ".", "", "8686963327", "PAY-NEW"]
        repayment = [today, "50000", "زين كاش 50000", "ChatGPT", ".", "", "8686963327", "PAY-DEBT-SETTLEMENT"]
        sheet = PaymentWorksheet(FakeWorksheet([HEADER, purchase, repayment]))
        ns = bot_functions(sheet)
        ns["is_chatgpt_product_name"] = lambda product: product == "ChatGPT"
        self.assertIn("الدخل: 58000", ns["calculate_totals_summary"]("all"))
        self.assertEqual(ns["get_current_chatgpt_delivery_payment"](8686963327), (2, sheet.row_values(2)))
        self.assertEqual(ns["find_completable_chatgpt_row"](sheet, 8686963327, missing="account"), 2)

    def test_bad_date_is_counted_in_all_time_and_flagged_for_periods(self):
        row = list(PAYMENT)
        row[0] = "invalid date"
        sheet = PaymentWorksheet(FakeWorksheet([HEADER, [""] * 80 + row]))
        ns = bot_functions(sheet)
        self.assertIn("الدخل: 50000", ns["calculate_totals_summary"]("all"))
        report = ns["calculate_totals_summary"]("2026-10-28")
        self.assertIn("الدخل: 0", report)
        self.assertIn("بلا تاريخ صالح", report)

    def test_daily_weekly_monthly_report_reads_displaced_payments(self):
        row = list(PAYMENT)
        row[0] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        sheet = PaymentWorksheet(FakeWorksheet([HEADER, [""] * 80 + row]))
        report = bot_functions(sheet)["calculate_income_report"]()
        self.assertIn("اليوم: 50000", report)
        self.assertIn("هذا الأسبوع: 50000", report)
        self.assertIn("هذا الشهر: 50000", report)

    def test_product_breakdown_uses_same_displaced_payment_amount(self):
        sheet = PaymentWorksheet(FakeWorksheet([HEADER, [""] * 29 + PAYMENT]))
        report = bot_functions(sheet)["calculate_product_breakdown"]("2026-10-28")
        self.assertIn("تحويل ماستر 50 الف", report)
        self.assertIn("المبيعات: 50000 (1 عملية)", report)

    def test_account_only_legacy_rows_are_available_for_link_and_edits(self):
        row = list(PAYMENT)
        row[1:3] = ["", ""]
        row[5] = "account@example.com"
        sheet = PaymentWorksheet(FakeWorksheet([HEADER, [""] * 80 + row]))
        normalized = sheet.get_all_values()[1]
        self.assertEqual(normalized[5], "account@example.com")
        sheet.update_cell(2, 2, 8000)
        self.assertEqual(sheet.worksheet.rows[1][81], 8000)


if __name__ == "__main__":
    unittest.main()
