from copy import deepcopy
from dataclasses import dataclass, fields
from types import SimpleNamespace
import re
import unittest
from unittest.mock import AsyncMock, Mock

import debt_manager as dm


@dataclass
class Button:
    text: str
    callback_data: str


@dataclass
class Markup:
    inline_keyboard: list


class Sheet:
    title = "ديون"
    col_count = 15
    row_count = 1000

    def __init__(self, rows=None):
        self.rows = deepcopy(rows or [dm.HEADERS])
        self.writes = []
        self.fail_read = False
        self.fail_write = False
        self.lose_response = False

    def get_all_values(self):
        if self.fail_read:
            raise RuntimeError("unavailable")
        return deepcopy(self.rows)

    def row_values(self, number):
        return deepcopy(self.rows[number - 1]) if number <= len(self.rows) else []

    def update(self, *, range_name, values, value_input_option):
        if self.fail_write:
            raise RuntimeError("write failed")
        self.writes.append((range_name, deepcopy(values)))
        match = re.fullmatch(r"([A-Z]+)(\d+):([A-Z]+)(\d+)", range_name)
        a, row, b, _ = match.groups()
        assert len(a) == len(b) == 1
        row, start = int(row), ord(a) - 65
        self.rows.extend([[] for _ in range(max(0, row - len(self.rows)))])
        target = self.rows[row - 1]
        target.extend([""] * max(0, start + len(values[0]) - len(target)))
        target[start:start + len(values[0])] = [str(value) for value in values[0]]
        if self.lose_response:
            self.lose_response = False
            raise TimeoutError("write saved, response lost")

    def add_rows(self, count):
        self.row_count += count


def row(chat_id=123456789, amount=8000, *, name="زبون", product="ChatGPT", debt_id="DEBT-A"):
    return ["2026-10-28 06:23", str(chat_id), name, product, str(amount), "غير مدفوع", "",
            debt_id, "p1", "plan1", "شهر", str(amount), "0", "", ""]


class DebtTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sheet = Sheet([dm.HEADERS, row()])
        self.recorded = {}
        self.wallet_total = 0
        self.credit_total = 0

        def income(state, force_new=False):
            operation = state["payment_operation_id"]
            self.recorded.setdefault(operation, sum(amount for _, amount in state["payments"]))
            return True

        def wallet(method, amount):
            self.wallet_total += amount
            return True

        def credit(chat, customer, amount, product, reason):
            self.credit_total += amount
            return True, self.credit_total

        callbacks = {f.name: Mock() for f in fields(dm.DebtServices)}
        callbacks.update(
            owner_id=99, open_spreadsheet=Mock(),
            products=lambda: [{"id": "p1", "name": "ChatGPT", "is_active": True}],
            product=lambda key: {"id": key, "name": "ChatGPT", "is_active": True},
            plans=lambda key: [{"id": "plan1", "name": "شهر", "price": 8000, "duration": "30", "is_active": True}],
            catalog_product=lambda name: {"id": "p1"} if name in {"ChatGPT", "جات"} else None,
            format_customer=lambda name, username=None: name or "غير معروف", format_money=lambda value: str(value),
            identity=lambda chat: ("زبون", None), record_income=income,
            adjust_vault=wallet, add_credit=credit,
            payment_summary=lambda state: f"المنتج: {state.get('product')}\nالمجموع: {sum(a for _, a in state.get('payments', []))}",
            product_keyboard=lambda: Markup([]), button=Button, markup=Markup,
            payment_methods=lambda: ["ماستر", "زين كاش"], vault_names=("ماستر", "زين كاش"),
            notification_chat_id=10, notification_topic_id=20,
            is_chatgpt_product=lambda product: True, duration_days=lambda value: 30,
            permanent_duration=lambda value: False, save_subscription=lambda state: True,
            schedule_feedback=AsyncMock(return_value=True),
        )
        dm.configure_debts(dm.DebtServices(**callbacks))
        dm._sheet = self.sheet
        dm._pending_debt = None
        dm._pending_repayments.clear()
        dm._busy_payments.clear()
        self.context = SimpleNamespace(user_data={}, args=[], bot=SimpleNamespace(
            send_message=AsyncMock(return_value=SimpleNamespace(message_id=7)),
            edit_message_text=AsyncMock(), delete_business_messages=AsyncMock(),
            get_chat=AsyncMock(return_value=SimpleNamespace(full_name="زبون", first_name="زبون", username=None)),
        ))

    def update(self, callback, *, owner=99, photo=False):
        message = SimpleNamespace(message_id=7, photo=[1] if photo else [], caption="صورة" if photo else None,
                                  reply_text=AsyncMock(), edit_text=AsyncMock())
        query = SimpleNamespace(data=callback, from_user=SimpleNamespace(id=owner), message=message,
                                answer=AsyncMock(), edit_message_text=AsyncMock(return_value=message), edit_message_caption=AsyncMock())
        return SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=owner))

    def payment(self, amount=8000):
        state = {"customer_chat_id": 123456789, "customer_name": "زبون", "payments": [("ماستر", amount)]}
        dm._select_debt(state, dm.list_open_debts(123456789)[0])
        return state

    def test_partial_then_full_settlement_preserves_original(self):
        self.assertEqual(dm.process_debt_repayment(2, 8000, 3000, operation_id="PAY-1"), (True, 5000))
        debt = dm.list_open_debts(123456789)[0]
        self.assertEqual((debt["original_amount"], debt["paid_total"], debt["status"]), (8000, 3000, "جزئي"))
        self.assertEqual(dm.process_debt_repayment(2, 5000, 5000, operation_id="PAY-2"), (True, 0))
        self.assertEqual(dm.list_open_debts(123456789), [])
        self.assertEqual(dm.list_debts()[0]["paid_total"], 8000)

    def test_legacy_paid_amount_is_not_an_open_debt(self):
        legacy = row()[:7]
        legacy[5] = "مدفوع"
        self.sheet.rows = [dm.HEADERS, legacy]
        self.assertEqual(dm.list_open_debts(), [])
        self.assertEqual(dm.list_debts()[0]["remaining"], 0)
        self.assertEqual(dm.list_debts()[0]["paid_total"], 8000)

    def test_read_failure_does_not_mean_no_debts(self):
        self.sheet.fail_read = True
        with self.assertRaises(RuntimeError):
            dm.list_open_debts(123456789)

    def test_debt_creation_retry_does_not_duplicate(self):
        self.assertTrue(dm.append_debt_row(123456789, "زبون", "ChatGPT", 8000, debt_id="DEBT-NEW"))
        self.assertTrue(dm.append_debt_row(123456789, "زبون", "ChatGPT", 8000, debt_id="DEBT-NEW"))
        self.assertEqual(len(self.sheet.rows), 3)

    def test_stale_remaining_cannot_overwrite_newer_settlement(self):
        dm.process_debt_repayment(2, 8000, 3000, operation_id="PAY-1")
        self.assertFalse(dm.process_debt_repayment(2, 8000, 2000, operation_id="PAY-2")[0])
        self.assertEqual(dm.list_open_debts()[0]["remaining"], 5000)

    async def test_shortcut_is_deleted_before_opening_owner_card(self):
        sequence = []
        self.context.bot.delete_business_messages.side_effect = lambda **kwargs: sequence.append("delete")
        self.context.bot.send_message.side_effect = lambda **kwargs: (sequence.append("card") or SimpleNamespace(message_id=7))
        await dm.handle_debt_shortcut(self.context, SimpleNamespace(business_connection_id="b", message_id=5), 123456789, "زبون")
        self.assertEqual(sequence, ["delete", "card"])

    async def test_dashboard_lists_totals_and_customer_details(self):
        update = self.update("debt_main")
        await dm.handle_debt_callback(update, self.context)
        text = update.callback_query.message.edit_text.call_args.args[0]
        self.assertIn("إجمالي المتبقي: 8000", text)
        await dm.handle_debt_callback(self.update("debt_list_open_0"), self.context)
        detail = self.update("debt_view_123456789_2")
        await dm.handle_debt_callback(detail, self.context)
        self.assertIn("المسدد: 0", detail.callback_query.edit_message_text.call_args.args[0])

    async def test_owner_permission_is_required(self):
        update = self.update("debt_main", owner=1)
        await dm.handle_debt_callback(update, self.context)
        update.callback_query.message.edit_text.assert_not_called()
        update.callback_query.answer.assert_awaited()

    async def test_single_plan_is_automatic(self):
        await dm.start_debt_for_customer(self.context, 123456789, "زبون", None)
        update = self.update("debt_catalog_p1")
        await dm.handle_debt_callback(update, self.context)
        self.assertEqual(dm._pending_debt["amount"], 8000)
        self.assertEqual(dm._pending_debt["plan_id"], "plan1")

    async def test_multiple_plans_require_choice(self):
        dm._services.plans = lambda key: [
            {"id": "plan1", "name": "شهر", "price": 8000, "is_active": True},
            {"id": "plan2", "name": "سنة", "price": 45000, "is_active": True},
        ]
        await dm.start_debt_for_customer(self.context, 123456789, "زبون", None)
        await dm.handle_debt_callback(self.update("debt_catalog_p1"), self.context)
        self.assertEqual(dm._pending_debt["step"], "plan")
        await dm.handle_debt_callback(self.update("debt_plan_plan2"), self.context)
        self.assertEqual(dm._pending_debt["amount"], 45000)

    async def test_receipt_with_debt_asks_purpose_and_one_debt_auto_selects(self):
        state = {"customer_chat_id": 123456789, "customer_name": "زبون", "payments": []}
        update = self.update("pay_confirm", photo=True)
        self.assertTrue(await dm.handle_payment_debt_callback(update, self.context, state, {}))
        self.assertIn("تسديد لو شراء", update.callback_query.edit_message_caption.call_args.kwargs["caption"])
        await dm.handle_payment_debt_callback(self.update("pay_purpose_debt", photo=True), self.context, state, {})
        self.assertEqual(state["selected_debt_id"], "DEBT-A")

    async def test_new_purchase_leaves_debt_untouched(self):
        state = self.payment()
        await dm.handle_payment_debt_callback(self.update("pay_purpose_purchase", photo=True), self.context, state, {})
        self.assertEqual(state["payment_purpose"], "purchase")
        self.assertEqual(dm.list_open_debts()[0]["remaining"], 8000)
        self.assertFalse(dm.payment_state_has_matching_debt(state))

    async def test_income_failure_keeps_debt_and_wallet_unchanged(self):
        dm._services.record_income = lambda *args, **kwargs: False
        state = self.payment()
        self.assertFalse(await dm.settle_debt_payment(self.update("pay_debt_repay", photo=True).callback_query, self.context, state))
        self.assertEqual(dm.list_open_debts()[0]["remaining"], 8000)
        self.assertEqual(self.wallet_total, 0)

    async def test_debt_write_retry_preserves_one_income(self):
        state = self.payment()
        query = self.update("pay_debt_repay", photo=True).callback_query
        self.sheet.fail_write = True
        with self.assertLogs(dm.logger, level="ERROR"):
            self.assertFalse(await dm.settle_debt_payment(query, self.context, state))
        self.sheet.fail_write = False
        self.assertTrue(await dm.settle_debt_payment(query, self.context, state))
        self.assertEqual(len(self.recorded), 1)
        self.assertEqual(self.wallet_total, 8000)

    async def test_lost_debt_response_does_not_deduct_remaining_twice(self):
        state = self.payment(3000)
        query = self.update("pay_debt_repay", photo=True).callback_query
        self.sheet.lose_response = True
        with self.assertLogs(dm.logger, level="ERROR"):
            self.assertFalse(await dm.settle_debt_payment(query, self.context, state))
        self.assertTrue(await dm.settle_debt_payment(query, self.context, state))
        self.assertEqual(dm.list_open_debts()[0]["remaining"], 5000)
        self.assertEqual(self.wallet_total, 3000)

    async def test_overpayment_requires_explicit_decision(self):
        state = self.payment(15000)
        query = self.update("pay_debt_repay", photo=True).callback_query
        self.assertFalse(await dm.settle_debt_payment(query, self.context, state))
        self.assertEqual(self.recorded, {})
        state["debt_overage_decision"] = "credit"
        self.assertTrue(await dm.settle_debt_payment(query, self.context, state))
        self.assertEqual(self.credit_total, 7000)
        self.assertEqual(self.wallet_total, 15000)

    async def test_credit_failure_retry_does_not_repeat_wallet_or_income(self):
        state = self.payment(15000)
        state["debt_overage_decision"] = "credit"
        real_credit = dm._services.add_credit
        dm._services.add_credit = lambda *args: (False, 0)
        query = self.update("pay_debt_repay", photo=True).callback_query
        self.assertFalse(await dm.settle_debt_payment(query, self.context, state))
        dm._services.add_credit = real_credit
        self.assertTrue(await dm.settle_debt_payment(query, self.context, state))
        self.assertEqual(self.wallet_total, 15000)
        self.assertEqual(self.credit_total, 7000)
        self.assertEqual(len(self.recorded), 1)

    async def test_payment_targets_selected_debt_not_another_same_product(self):
        self.sheet.rows.append(row(amount=9000, debt_id="DEBT-B"))
        state = {"customer_chat_id": 123456789, "customer_name": "زبون", "payments": [("ماستر", 8000)]}
        dm._select_debt(state, dm._get_debt(123456789, 2))
        self.assertTrue(await dm.settle_debt_payment(self.update("pay_debt_repay", photo=True).callback_query, self.context, state))
        self.assertEqual([(d["debt_id"], d["remaining"]) for d in dm.list_open_debts()], [("DEBT-B", 9000)])

    async def test_dashboard_repayment_wallet_edit_and_partial_confirmation(self):
        await dm.handle_debt_callback(self.update("debt_repay_123456789_2"), self.context)
        await dm.handle_debt_callback(self.update("debt_wallet_1"), self.context)
        self.assertEqual(dm._pending_repayments[7]["payments"], [("زين كاش", 8000)])
        await dm.handle_debt_callback(self.update("debt_payment_edit"), self.context)
        message = SimpleNamespace(text="٣٬٠٠٠", reply_to_message=None, reply_text=AsyncMock())
        update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=99))
        self.assertTrue(await dm.handle_debt_search_or_repayment_input(update, self.context))
        await dm.handle_debt_callback(self.update("debt_payment_confirm"), self.context)
        self.assertNotIn(7, dm._pending_repayments)
        self.assertEqual(dm.list_open_debts()[0]["remaining"], 5000)
        self.assertEqual(list(self.recorded.values()), [3000])
        self.assertTrue(next(iter(self.recorded)).startswith("PAY-DEBT-"))
        self.assertEqual(self.wallet_total, 3000)

    async def test_customer_list_pages_stay_with_same_customer(self):
        self.sheet.rows += [row(debt_id=f"DEBT-{i}") for i in range(12)]
        self.sheet.rows.append(row(chat_id=987654321, name="آخر", debt_id="OTHER"))
        update = self.update("debt_customer_123456789_1")
        await dm.handle_debt_callback(update, self.context)
        buttons = update.callback_query.edit_message_text.call_args.kwargs["reply_markup"].inline_keyboard
        callbacks = [button.callback_data for line in buttons for button in line]
        self.assertIn("debt_customer_123456789_0", callbacks)
        self.assertFalse(any("987654321" in callback for callback in callbacks))
        self.assertEqual(sum(callback.startswith("debt_view_") for callback in callbacks), 5)

    async def test_free_product_amount_and_lost_creation_response_retry(self):
        await dm.start_debt_for_customer(self.context, 123456789, "زبون", None)
        await dm.handle_debt_callback(self.update("debt_product_manual"), self.context)
        message = SimpleNamespace(text="تحويل ماستر", reply_to_message=None, reply_text=AsyncMock())
        update = SimpleNamespace(message=message)
        self.assertTrue(await dm.handle_debt_manual_entry(update, self.context))
        await dm.handle_debt_callback(self.update("debt_amount_manual"), self.context)
        message.text = "50,000"
        self.assertTrue(await dm.handle_debt_manual_entry(update, self.context))
        self.sheet.lose_response = True
        with self.assertLogs(dm.logger, level="ERROR"):
            await dm.handle_debt_callback(self.update("debt_amount_commit"), self.context)
        self.assertIsNotNone(dm._pending_debt)
        await dm.handle_debt_callback(self.update("debt_amount_commit"), self.context)
        self.assertIsNone(dm._pending_debt)
        created = [d for d in dm.list_debts() if d["product"] == "تحويل ماستر"]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["remaining"], 50000)

    async def test_income_saved_blocks_stale_receipt_choices(self):
        state = self.payment()
        state["debt_income_saved"] = True
        before = deepcopy(state)
        for callback in ("pay_purpose_debt", "pay_debt_select_2", "pay_method_زين كاش", "pay_amount_manual", "pay_purpose_purchase"):
            update = self.update(callback, photo=True)
            self.assertTrue(await dm.handle_payment_debt_callback(update, self.context, state, {}))
            update.callback_query.answer.assert_awaited()
            self.assertEqual(state, before)

    async def test_changed_balance_requires_review_before_income_write(self):
        state = self.payment()
        dm.process_debt_repayment(2, 8000, 3000, operation_id="OTHER")
        self.assertFalse(await dm.settle_debt_payment(self.update("pay_debt_repay").callback_query, self.context, state))
        self.assertEqual(self.recorded, {})
        self.assertEqual(self.wallet_total, 0)

    async def test_receipt_with_multiple_debts_requires_exact_choice(self):
        self.sheet.rows.append(row(amount=9000, debt_id="DEBT-B"))
        state = {"customer_chat_id": 123456789, "customer_name": "زبون", "payments": []}
        await dm.handle_payment_debt_callback(self.update("pay_purpose_debt", photo=True), self.context, state, {})
        self.assertNotIn("selected_debt_row", state)
        await dm.handle_payment_debt_callback(self.update("pay_debt_select_2", photo=True), self.context, state, {})
        await dm.handle_payment_debt_callback(self.update("pay_method_ماستر", photo=True), self.context, state, {})
        pending = {7: state}
        await dm.handle_payment_debt_callback(self.update("pay_debt_repay", photo=True), self.context, state, pending)
        self.assertEqual(pending, {})
        self.assertEqual([(d["debt_id"], d["remaining"]) for d in dm.list_open_debts()], [("DEBT-B", 9000)])

    async def test_link_subscription_failure_retry_does_not_duplicate_debt(self):
        state = {"customer_chat_id": 123456789, "customer_name": "زبون", "is_debt": True}
        dm._services.save_subscription = lambda reminder: False
        query = self.update("linkplan_plan1_123456789").callback_query
        await dm.complete_link_debt_choice(query, self.context, state, 123456789, plan_id="plan1")
        self.assertIn("pending_link_debt", self.context.user_data)
        dm._services.save_subscription = lambda reminder: True
        await dm.complete_link_debt_choice(query, self.context, state, 123456789, plan_id="plan1")
        self.assertNotIn("pending_link_debt", self.context.user_data)
        self.assertEqual(len(self.sheet.rows), 3)
        dm._services.schedule_feedback.assert_awaited_once()

    async def test_legacy_fix_cannot_apply_same_payment_to_another_debt(self):
        dm._services.latest_payment = lambda chat: (402, ["2026-10-28", "3000", "ماستر 3000", "ChatGPT", "زبون", "", "123456789", "PAY-OLD"])
        self.context.args = ["123456789", "ChatGPT"]
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=99))
        await dm.cmd_fix_existing_debt_payment(update, self.context)
        self.assertEqual(dm.list_open_debts()[0]["remaining"], 5000)
        await dm.cmd_fix_existing_debt_payment(update, self.context)
        self.assertEqual(dm.list_open_debts()[0]["remaining"], 5000)
        self.assertEqual(self.recorded, {})


if __name__ == "__main__":
    unittest.main()
