import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import Problem, Store
from operations import Operations
from stock_plan import StockPlan


class StockPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.ops = Operations(self.store)
        self.local_id, self.ngs_id = self.store.import_rows([
            {'title_zh': '库存候选本地商品', 'mode': 'LOCAL', 'stock': 900},
            {'title_zh': '库存候选NGS商品', 'mode': 'NGS', 'stock': 800},
        ])['created']
        self.warehouse = self.entity('warehouse', 'FBPI候选仓')
        self.source = self.entity('warehouse', '调拨来源仓')
        self.shop = self.entity('shop', '候选测试店铺')
        self.plan = StockPlan(type('AppStub', (), {'store': self.store})())

    def tearDown(self):
        self.tmp.cleanup()

    def entity(self, kind, name):
        return self.ops.transact('entity', {'request_id': f'{kind}-{name}', 'kind': kind, 'name': name})['id']

    def transact(self, action, request_id, **body):
        return self.ops.transact(action, {'request_id': request_id, **body})

    def order(self, request_id, external_id, quantity):
        return self.transact('order', request_id, shop_id=self.shop, warehouse_id=self.warehouse,
                             external_id=external_id, currency='SAR',
                             lines=[{'product_id': self.local_id, 'quantity': quantity, 'unit_price': '10'}])

    def test_preview_uses_local_physical_stock_once_and_is_read_only(self):
        product = self.store.get(self.local_id)
        self.store.record_platform(self.local_id, {'sku_parent': 'NOON-QA-LOCAL'})
        self.transact('adjust', 'target-stock', product_id=self.local_id, warehouse_id=self.warehouse,
                      direction='in', quantity=20, reason='合成期初盘点')
        self.transact('adjust', 'source-stock', product_id=self.local_id, warehouse_id=self.source,
                      direction='in', quantity=10, reason='合成调拨来源')
        reserved_order = self.order('reserved-order', 'ORDER-RESERVED', 3)
        self.transact('reserve', 'reserve-order', id=reserved_order['id'], revision=reserved_order['revision'])
        self.order('new-order', 'ORDER-NEW', 4)
        self.transact('purchase', 'incoming-purchase', supplier_id=self.entity('supplier', '候选供应商'),
                      warehouse_id=self.warehouse, lines=[{'product_id': self.local_id, 'quantity': 50, 'unit_price': '1'}])
        transfer = self.transact('transfer', 'incoming-transfer', source_warehouse_id=self.source,
                                 warehouse_id=self.warehouse,
                                 lines=[{'product_id': self.local_id, 'quantity': 5}], note='测试在途库存')
        self.transact('transfer_dispatch', 'dispatch-transfer', id=transfer['id'], revision=transfer['revision'], evidence='合成出库记录')
        before_revision = self.store.get(self.local_id)['revision']
        with self.store.connect() as c:
            before = (c.execute('SELECT count(*) FROM ops_movements').fetchone()[0],
                      c.execute('SELECT count(*) FROM ops_documents').fetchone()[0])
        result = self.plan.preview({'warehouse_id': self.warehouse, 'warehouse_code': 'FBPI_QA', 'buffer': 2, 'cap': 8})
        row = next(r for r in result['rows'] if r['id'] == self.local_id)
        self.assertEqual(result['candidates'], 1)
        self.assertEqual(result['blocked'], 0)
        self.assertEqual((row['on_hand'], row['reserved'], row['unreserved_order_demand']), (20, 3, 4))
        self.assertEqual((row['supplier_available'], row['ceiling'], row['qty']), (900, 11, 8))
        self.assertNotIn(self.ngs_id, {r['id'] for r in result['rows']})
        self.assertIn('不会发送平台', result['notice'])
        self.assertEqual(self.store.get(self.local_id)['revision'], before_revision)
        with self.store.connect() as c:
            after = (c.execute('SELECT count(*) FROM ops_movements').fetchone()[0],
                     c.execute('SELECT count(*) FROM ops_documents').fetchone()[0])
        self.assertEqual(after, before)

    def test_missing_warehouse_code_and_platform_identity_stay_blocked(self):
        self.transact('adjust', 'physical-stock', product_id=self.local_id, warehouse_id=self.warehouse,
                      direction='in', quantity=12, reason='合成期初盘点')
        result = self.plan.preview({'warehouse_id': self.warehouse, 'warehouse_code': '', 'buffer': 1})
        row = next(r for r in result['rows'] if r['id'] == self.local_id)
        self.assertEqual(row['qty'], 11)
        self.assertEqual(row['status'], 'blocked')
        self.assertIn('尚未填入经 Seller Lab 确认的集成仓库编码', row['reasons'])
        self.assertIn('商品尚无 noon 平台编号', row['reasons'])

    def test_whitespace_warehouse_code_is_treated_as_unconfigured(self):
        result = self.plan.preview({'warehouse_id': self.warehouse, 'warehouse_code': '   ', 'buffer': 0})
        row = next(r for r in result['rows'] if r['id'] == self.local_id)
        self.assertEqual(result['warehouse_code'], '')
        self.assertEqual(row['status'], 'blocked')
        self.assertIn('尚未填入经 Seller Lab 确认的集成仓库编码', row['reasons'])

    def test_preview_rejects_bad_warehouse_quantity_and_code(self):
        valid = {'warehouse_id': self.warehouse, 'warehouse_code': 'WH-1'}
        for patch in ({'warehouse_id': 'missing'}, {'buffer': -1}, {'buffer': 1.5}, {'buffer': True},
                      {'cap': -1}, {'cap': True}, {'warehouse_code': 'WH 1'}):
            with self.subTest(patch=patch), self.assertRaises(Problem):
                self.plan.preview({**valid, **patch})


if __name__ == '__main__':
    unittest.main()
