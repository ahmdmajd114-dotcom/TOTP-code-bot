from copy import deepcopy
import unittest

from financial_ledger import FinanceError
from vault_manager import audit_balances, wallet_statement
from reports_manager import totals


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


class TreasuryAuditTests(unittest.TestCase):
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
