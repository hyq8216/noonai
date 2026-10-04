import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import App, Handler, ThreadingHTTPServer


class StockPlanHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = App(self.tmp.name)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.app = self.app
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        product = self.app.store.import_rows([{'title_zh': '库存预览HTTP测试', 'mode': 'LOCAL', 'stock': 500}])['created'][0]
        self.product = self.app.store.get(product)
        self.warehouse = self.app.ops.transact('entity', {'request_id': 'stock-http-warehouse', 'kind': 'warehouse', 'name': 'HTTP候选仓'})
        self.app.store.record_platform(product, {'sku_parent': 'NOON-HTTP-QA'})
        self.app.ops.transact('adjust', {'request_id': 'stock-http-adjust', 'product_id': product,
                                         'warehouse_id': self.warehouse['id'], 'direction': 'in',
                                         'quantity': 10, 'reason': '合成HTTP期初库存'})
        shop = self.app.ops.transact('entity', {'request_id': 'stock-http-shop', 'kind': 'shop', 'name': 'HTTP测试店铺'})
        self.app.ops.transact('order', {'request_id': 'stock-http-order', 'shop_id': shop['id'],
                                        'warehouse_id': self.warehouse['id'], 'external_id': 'STOCK-HTTP-ORDER',
                                        'currency': 'SAR', 'lines': [{'product_id': product, 'quantity': 2, 'unit_price': '10'}]})

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.app.automation.close()
        self.app.media.close()
        self.app.executor.shutdown(wait=True)
        self.tmp.cleanup()

    def post(self, body, token=True):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['X-Workbench-Token'] = self.app.token
        request = Request(self.url + '/api/stock-plan/preview', data=json.dumps(body).encode(), headers=headers)
        return urlopen(request, timeout=10)

    def test_authenticated_preview_serializes_result_and_does_not_write_or_send(self):
        with self.app.store.connect() as c:
            before = (c.execute('SELECT count(*) FROM ops_movements').fetchone()[0],
                      c.execute('SELECT count(*) FROM ops_documents').fetchone()[0])
        with patch('server.Noon') as noon:
            with self.post({'warehouse_id': self.warehouse['id'], 'warehouse_code': 'FBPI_QA', 'buffer': 1}) as response:
                result = json.load(response)
            noon.assert_not_called()
        row = next(row for row in result['rows'] if row['id'] == self.product['id'])
        self.assertEqual((row['on_hand'], row['unreserved_order_demand'], row['qty']), (10, 2, 7))
        self.assertEqual(row['status'], 'candidate')
        self.assertIn('不会发送平台', result['notice'])
        with self.app.store.connect() as c:
            after = (c.execute('SELECT count(*) FROM ops_movements').fetchone()[0],
                     c.execute('SELECT count(*) FROM ops_documents').fetchone()[0])
        self.assertEqual(after, before)
        self.assertEqual(self.app.store.get(self.product['id'])['revision'], self.product['revision'])

    def test_preview_route_requires_session_token_and_rejects_invalid_warehouse(self):
        body = {'warehouse_id': self.warehouse['id'], 'buffer': 0}
        with self.assertRaises(HTTPError) as denied:
            self.post(body, token=False)
        self.assertEqual(denied.exception.code, 403)
        with self.post({**body, 'warehouse_code': '   '}) as response:
            blank = json.load(response)
        self.assertEqual(blank['warehouse_code'], '')
        self.assertEqual(blank['rows'][0]['status'], 'blocked')
        with self.assertRaises(HTTPError) as invalid:
            self.post({'warehouse_id': 'not-a-warehouse', 'buffer': 0})
        self.assertEqual(invalid.exception.code, 404)


if __name__ == '__main__':
    unittest.main(verbosity=2)
