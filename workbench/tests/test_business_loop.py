"""Cross-module regression for the local purchase-to-settlement business loop.

All documents are synthetic fixtures. This proves local ledger integration only;
it does not contact a supplier, seller account, payment provider, or noon.
"""
import tempfile
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import Store, Problem, ident, now  # noqa: E402
from operations import Operations  # noqa: E402
from finance import Finance  # noqa: E402


class BusinessLoopTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.ops = Operations(self.store)
        self.finance = Finance(self.store)
        self.product = self.store.import_rows([{
            'title_zh': '合成收发货测试商品',
            'source_sku': 'QA-LOOP-01',
            'facts': '仅用于跨模块自动化测试',
        }])['created'][0]
        self.supplier = self.entity('supplier', '合成供应商')
        self.warehouse = self.entity('warehouse', '合成沙特仓')
        self.shop = self.entity('shop', '合成 noon 店铺')

    def tearDown(self):
        self.tmp.cleanup()

    def op(self, action, **body):
        return self.ops.transact(action, {'request_id': ident(), **body})

    def entity(self, kind, name):
        return self.op('entity', kind=kind, name=name)['id']

    def op_revision(self, action, document, **body):
        return self.op(action, id=document['id'], revision=document['revision'], **body)

    def finance_call(self, action, **body):
        return self.finance.transact(action, {'request_id': ident(), **body})

    def finance_entry(self, entry):
        return next(row for row in self.finance.state()['entries'] if row['id'] == entry['id'])

    def finance_revision(self, action, entry, **body):
        current = self.finance_entry(entry)
        return self.finance_call(action, id=current['id'], revision=current['revision'], **body)

    def order_summary(self, order_id):
        return next(row for row in self.finance.state()['orders'] if row['id'] == order_id)

    def reconcile(self, order_id):
        order = self.order_summary(order_id)
        return self.finance_call('review', order_id=order_id, fingerprint=order['fingerprint'],
            income_checked=True, costs_checked=True, refunds_checked=True,
            note='核对合成订单、结算、成本及退货凭证')

    def test_purchase_to_delivered_order_settlement_cost_and_return_stay_consistent(self):
        # Receive against a local purchase document; no adjustment shortcut and no supplier call.
        purchase = self.op('purchase', supplier_id=self.supplier, warehouse_id=self.warehouse,
            lines=[{'product_id': self.product, 'quantity': 5, 'unit_price': '3.50'}])
        purchase = self.op_revision('receive', purchase, quantities={self.product: 5})
        self.assertEqual((purchase['status'], self.ops.state('inventory')['stock'][0]['on_hand']), ('received', 5))

        order = self.op('order', shop_id=self.shop, warehouse_id=self.warehouse,
            external_id='SYNTHETIC-ORDER-1', currency='SAR',
            lines=[{'product_id': self.product, 'quantity': 2, 'unit_price': '25'}])
        order = self.op_revision('reserve', order)
        self.assertEqual(order['status'], 'reserved')
        inventory = self.ops.state('inventory')['stock'][0]
        self.assertEqual((inventory['on_hand'], inventory['reserved'], inventory['available']), (5, 2, 3))
        order = self.op_revision('ship', order, carrier='合成承运商', tracking='QA-TRACK-1')
        order = self.op_revision('deliver', order, evidence='合成签收回执')
        self.assertEqual(order['status'], 'delivered')
        self.assertEqual(self.ops.state('inventory')['stock'][0]['on_hand'], 3)

        # Operational source documents do not silently create recognized finance entries.
        self.assertEqual(self.finance.state()['entries'], [])
        with self.assertRaises(Problem):
            self.reconcile(order['id'])  # delivered status alone is not financial reconciliation
        sale = self.finance_call('entry', kind='income', category='sale', document_id=order['id'],
            currency='SAR', amount='50', fx='1.9', date=now()[:10],
            evidence='合成卖家结算行', evidence_key='QA-SETTLEMENT-1')
        cost = self.finance_call('entry', kind='expense', category='goods', document_id=purchase['id'],
            currency='CNY', amount='7', fx='1', date=now()[:10],
            evidence='合成采购成本行', evidence_key='QA-COST-1')
        self.finance_revision('allocate', cost, allocations=[{'order_id': order['id'], 'amount': '7'}])
        payment_body = {'id': sale['id'], 'revision': sale['revision'], 'amount': '20', 'fx': '1.85',
            'date': now()[:10], 'evidence_key': 'QA-BANK-1', 'evidence': '合成部分到账', 'account': '合成账户'}
        payment_request = {'request_id': 'qa-settlement-payment', **payment_body}
        payment = self.finance.transact('payment', payment_request)
        self.assertEqual(self.finance.transact('payment', payment_request), payment)

        state = self.finance.state()
        summary = state['summary']
        order_state = self.order_summary(order['id'])
        self.assertEqual((summary['income_cents'], summary['expense_cents']), (9500, 700))
        self.assertEqual((summary['cash_in_cents'], summary['cash_out_cents']), (3700, 0))
        self.assertEqual(order_state['contribution_cents'], 8800)
        self.assertEqual(self.finance_entry(sale)['remaining_cents'], 3000)
        self.reconcile(order['id'])
        self.assertTrue(self.order_summary(order['id'])['reconciled'])

        # A refund and physical return are separate facts. Each invalidates the previous reconciliation.
        refund = self.finance_call('entry', kind='expense', category='refund', document_id=order['id'],
            currency='SAR', amount='5', fx='1.9', date=now()[:10],
            evidence='合成退款行', evidence_key='QA-REFUND-1')
        self.assertEqual(self.order_summary(order['id'])['contribution_cents'], 7850)
        self.assertFalse(self.order_summary(order['id'])['reconciled'])
        order = self.op_revision('return', order, quantities={self.product: 1}, restock=True,
            reason='合成退货验收')
        self.assertEqual(order['status'], 'delivered')
        self.assertEqual(self.ops.state('inventory')['stock'][0]['on_hand'], 4)
        self.assertFalse(self.order_summary(order['id'])['reconciled'])
        self.reconcile(order['id'])
        self.assertTrue(self.order_summary(order['id'])['reconciled'])
        self.assertEqual(self.finance_entry(refund)['remaining_cents'], 500)


if __name__ == '__main__':
    unittest.main()
