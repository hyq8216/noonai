import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from connectors import Noon
from core import Problem
from platform_batch import PlatformBatch
from pricing_status import summarize, validate
from server import App


def response(sku, code='OK'):
    return {'items': [{'partner_sku': sku, 'country_code': 'sa',
                       'status': {'status_id': 0 if code == 'OK' else 5, 'status_code': code, 'message': ''},
                       'price': 149.5 if code == 'OK' else None,
                       'msrp': None, 'is_active': True if code == 'OK' else None}]}


class PricingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = App(self.tmp.name)
        self.store = self.app.store

    def tearDown(self):
        self.app.automation.close()
        self.app.media.close()
        self.app.executor.shutdown(wait=True)
        self.tmp.cleanup()

    def product(self, mode='LOCAL'):
        pid = self.store.import_rows([{'title_zh': '售价测试', 'mode': mode}])['created'][0]
        return self.store.get(pid)

    def test_sar_endpoint_payload(self):
        client = Noon.__new__(Noon)
        with patch.object(client, 'post', return_value={}) as post:
            client.pricing_get_sa('A/B')
        post.assert_called_once_with('/pricing/v1/pricing/get', {'items': [{'partner_sku': 'A/B', 'country_code': 'sa'}]})

    def test_strict_per_item_result_and_preservation(self):
        p = self.product()
        self.store.record_pricing(p['id'], p['partner_sku'], response(p['partner_sku']), p['revision'])
        saved = self.store.get(p['id'])
        self.assertEqual(saved['pricing_summary']['price'], 149.5)
        self.assertEqual(saved['pricing_summary']['group'], 'priced')
        self.assertEqual(saved['revision'], p['revision'])
        before = saved['platform']
        for invalid in ({'items': []}, response('wrong'), {'items': [response(p['partner_sku'])['items'][0]] * 2},
                        {'items': [{**response(p['partner_sku'])['items'][0], 'price': True}]},
                        {'items': [{**response(p['partner_sku'])['items'][0], 'is_active': 'true'}]}):
            with self.assertRaises(Problem):
                self.store.record_pricing(p['id'], p['partner_sku'], invalid, p['revision'])
        self.assertEqual(self.store.get(p['id'])['platform'], before)

    def test_not_found_is_not_a_price_and_content_refresh_preserves_readback(self):
        p = self.product()
        self.store.record_pricing(p['id'], p['partner_sku'], response(p['partner_sku'], 'NOT_FOUND'), p['revision'])
        self.assertEqual(self.store.get(p['id'])['pricing_summary']['group'], 'missing')
        self.store.record_platform(p['id'], {'sku_parent': 'P'})
        self.assertEqual(self.store.get(p['id'])['pricing_summary']['group'], 'missing')
        with self.assertRaises(Problem):
            validate({'items': [{**response(p['partner_sku'], 'NOT_FOUND')['items'][0], 'price': 100}]}, p['partner_sku'])

    def test_batch_blocks_unknown_and_ngs_and_rechecks_before_network(self):
        local = self.product()
        ngs = self.product('NGS')
        batch = PlatformBatch(self.app, 'prices')
        with patch.object(self.app, 'config', return_value={'noon_ready': True}), patch.object(self.app.executor, 'submit') as submit:
            pre = batch.preview({'product_ids': [local['id'], ngs['id']]})
            self.assertEqual((pre['ready'], pre['blocked']), (1, 1))
            req = {'product_ids': [local['id'], ngs['id']], 'preview_token': pre['token'], 'request_id': 'prices', 'confirmed': True}
            result = batch.apply(req)
            self.assertEqual(len(result['jobs']), 1)
            self.assertEqual(batch.apply(req)['jobs'], result['jobs'])
            self.assertEqual(submit.call_count, 1)
        self.assertEqual(PlatformBatch(self.app).latest()['kind'], 'prices')
        self.store.update(local['id'], {'mode': 'NGS'}, local['revision'])
        with patch('server.Noon') as noon:
            self.app.run(result['jobs'][0]['job_id'], local, 'prices')
            noon.assert_not_called()
        self.assertEqual(batch.status('prices')['counts'], {'failed': 1})

    def test_job_records_per_item_failure_without_false_success(self):
        p = self.product()
        jid = self.store.add_job(p['id'], 'prices', p['revision'])
        with patch('server.Noon') as noon:
            noon.return_value.pricing_get_sa.return_value = response(p['partner_sku'], 'NOT_FOUND')
            self.app.run(jid, p, 'prices')
            noon.return_value.pricing_get_sa.assert_called_once_with(p['partner_sku'])
        self.assertEqual(self.store.get(p['id'])['pricing_summary']['group'], 'missing')
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT status FROM jobs WHERE id=?', (jid,)).fetchone()[0], 'needs_attention')


if __name__ == '__main__':
    unittest.main()
