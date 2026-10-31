from copy import deepcopy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock, patch

from financial_ledger import FinanceError
from vault_manager import audit_balances, wallet_statement
from reports_manager import totals, operation_wallet_text
import reports_manager


def snapshot():
    return {
        'accounts': {'wallet:ماستر': 105000, 'wallet:زين كاش': 53000},
        'operations': [
            {'id':'OPEN','kind':'opening','payload':{'accounts':[{'key':'wallet:ماستر','balance':100000},{'key':'wallet:زين كاش','balance':50000}]}},
            {'id':'PAY-Z','kind':'sale','posting_index':2,'recorded_at':'2026-10-28T10:00:00Z','payload':{'customer_name':'Ali'},'product':'ChatGPT'},
            {'id':'FIN-A','kind':'transfer','posting_index':3,'recorded_at':'2026-10-28T09:59:59Z','payload':{'description':'move'}},
        ],
        'entries': [
            {'operation_id':'PAY-Z','account_key':'wallet:ماستر','delta':8000},
            {'operation_id':'FIN-A','account_key':'wallet:ماستر','delta':-3000},
            {'operation_id':'FIN-A','account_key':'wallet:زين كاش','delta':3000},
        ],
    }


class OperationDetailsTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_entry_read_does_not_show_misleading_no_cash(self):
        db=Mock()
        db.operation.return_value={'id':'PAY-1'}
        db.operation_entries.side_effect=FinanceError('read failed')
        query=SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
        with patch.object(reports_manager,'ledger',return_value=db):
            with self.assertRaises(FinanceError):
                await reports_manager.handle_report_menu('finance_view_PAY-1',query,SimpleNamespace(user_data={}))
        query.message.reply_text.assert_not_called()

    async def test_detail_card_includes_committed_wallet_and_amount(self):
        db=Mock()
        db.operation.return_value=dict(id='PAY-1',kind='sale',payload={},cash_in=10000,cash_out=0,recorded_at='2026-10-31',historical=False)
        db.operation_entries.return_value=[{'account_key':'wallet:رصيد اثير','delta':10000}]
        query=SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
        with patch.object(reports_manager,'ledger',return_value=db),patch.object(reports_manager,'menu_keyboard',return_value=None):
            await reports_manager.handle_report_menu('finance_view_PAY-1',query,SimpleNamespace(user_data={}))
        self.assertIn('رصيد اثير: دخل 10,000',query.message.reply_text.call_args.args[0])
        db.operation_entries.assert_called_once_with('PAY-1')


class TreasuryAuditTests(unittest.TestCase):
    def test_operation_shows_posted_wallets_instead_of_payload_guess(self):
        row={'payload':{'payments':[{'wallet':'wrong','amount':10000}]}}
        text=operation_wallet_text(row,[{'account_key':'wallet:ماستر','delta':10000},{'account_key':'wallet:زين كاش','delta':-5000},{'account_key':'credit:1','delta':8000}])
        self.assertIn('ماستر: دخل 10,000',text)
        self.assertIn('زين كاش: خرج 5,000',text)
        self.assertNotIn('wrong',text)
        self.assertNotIn('credit:',text)

    def test_historical_wallet_is_not_presented_as_new_cash(self):
        text=operation_wallet_text({'historical':True,'payload':{'payments':[{'wallet':'ماستر','amount':10000}]}},[])
        self.assertIn('لا يغيّر أرصدة',text)
        self.assertIn('ماستر — 10,000',text)
        self.assertIn('غير محفوظة',operation_wallet_text({'historical':True,'payload':{}},[]))

    def test_credit_only_operation_has_no_invented_wallet(self):
        self.assertIn('لا توجد حركة نقدية',operation_wallet_text({'payload':{}},[{'account_key':'credit:1','delta':8000}]))

    def test_balances_are_reconstructed_from_opening_and_signed_movements(self):
        result=audit_balances(snapshot())
        self.assertTrue(all(r['matched'] for r in result.values()))
        self.assertEqual(result['wallet:ماستر']['expected'],105000)

    def test_unexplained_increase_is_visible_and_never_auto_fixed(self):
        s=snapshot();s['accounts']['wallet:ماستر']+=999
        before=deepcopy(s);result=audit_balances(s)
        self.assertFalse(result['wallet:ماستر']['matched'])
        self.assertEqual(result['wallet:ماستر']['difference'],999)
        self.assertEqual(s,before)

    def test_account_with_no_source_is_not_reported_as_matched(self):
        s=snapshot();s['accounts']['wallet:Unknown']=1000
        self.assertFalse(audit_balances(s)['wallet:Unknown']['matched'])

    def test_missing_account_or_missing_entries_cannot_be_silently_zero(self):
        s=snapshot();del s['accounts']['wallet:ماستر']
        self.assertFalse(audit_balances(s)['wallet:ماستر']['matched'])
        del s['entries']
        with self.assertRaises(FinanceError):audit_balances(s)

    def test_entry_must_link_to_a_real_operation(self):
        s=snapshot();s['entries'][0]['operation_id']='missing'
        with self.assertRaises(FinanceError):audit_balances(s)

    def test_running_balances_follow_posting_order_not_transaction_start_time(self):
        balance,rows=wallet_statement(snapshot(),'ماستر')
        self.assertEqual([(r['id'],r['balance']) for r in rows],[('FIN-A',105000),('PAY-Z',108000)])
        self.assertEqual(balance['actual'],105000)

    def test_undoing_refund_does_not_leave_refund_counted(self):
        sale={'id':'S','kind':'sale','occurred_at':'2026-10-27T12:00:00Z','cash_in':8000,'cash_out':0,'sales':8000}
        refund={'id':'R','kind':'refund','occurred_at':'2026-10-27T12:01:00Z','cash_in':0,'cash_out':3000,'refunded':3000,'sales':0,'reference_id':'S'}
        reverse={'id':'V','kind':'reversal','occurred_at':'2026-10-27T12:02:00Z','cash_in':0,'cash_out':0,'sales':0,'reference_id':'R'}
        result=totals([sale,refund,reverse])
        self.assertEqual(result['refunds'],0)
        self.assertEqual(result['received']-result['spent'],8000)


if __name__=='__main__':unittest.main()
