import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import Problem
from server import App


class CatalogCampaignTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = App(Path(self.tmp.name))
        self.store = self.app.store
        self.campaign = self.app.catalog_campaign

    def tearDown(self):
        self.app.executor.shutdown(wait=True, cancel_futures=True)
        self.app.visual_checks.close()
        self.app.visuals.close()
        self.app.media.close()
        self.tmp.cleanup()

    def products(self, count, *, missing_last=False):
        rows = [{'title_zh': f'目录铺货样本 {i:04d}',
                 'source_url': f'https://supplier.example/catalog/{i}',
                 'source_sku': f'CAT-{i:04d}', 'supplier': '合成供应商',
                 'facts': '' if missing_last and i == count - 1 else '黑色塑料收纳夹，10件装'}
                for i in range(count)]
        created = []
        for start in range(0, len(rows), 500):
            created.extend(self.store.import_rows(rows[start:start + 500])['created'])
        return created

    def body(self, ids, *, plan=None, request_id=None):
        return {'product_ids': ids, 'plan': plan or {'translate': False},
                'name': '浏览器外批次 QA', 'request_id': request_id or uuid.uuid4().hex,
                'confirmed': True}

    def test_501_ready_and_one_blocked_are_chunked_audited_and_replay_safe(self):
        ids = self.products(502, missing_last=True)
        body = self.body(ids, plan={'translate': True, 'missing_only': True})
        with patch.object(self.app.models, 'ready', return_value=True):
            preview = self.campaign.preview(body)
            self.assertEqual([chunk['count'] for chunk in preview['chunks']], [500, 2])
            self.assertEqual([chunk['ready'] for chunk in preview['chunks']], [500, 1])
            self.assertEqual([chunk['blocked'] for chunk in preview['chunks']], [0, 1])
            self.assertEqual(preview['totals']['ready'], 501)
            self.assertEqual(preview['totals']['blocked'], 1)
            self.assertEqual(preview['totals']['calls'], 501)
            self.assertEqual(len(self.app.automation.state()['runs']), 0)

            queued = self.campaign.apply({**body, 'preview_token': preview['token']})
            self.assertEqual(queued['totals']['ready'], 501)
            self.assertEqual([run['ready'] for run in queued['runs']], [500, 1])
            self.assertEqual(len(queued['runs']), 2)
            counts = [self.app.automation.state()['run_item_counts'][run['id']]
                      for run in queued['runs']]
            self.assertEqual(counts, [500, 1])

            replay = self.campaign.apply({**body, 'preview_token': preview['token']})
        self.assertTrue(replay['replayed'])
        self.assertEqual(len(self.app.automation.state()['runs']), 2)
        history = self.campaign.history()
        self.assertEqual(history['total'], 1)
        self.assertTrue(history['rows'][0]['exceptions_saved'])
        exceptions = self.campaign.exceptions(body['request_id'])
        self.assertEqual(len(exceptions['rows']), 1)
        self.assertEqual(exceptions['rows'][0]['sku'], self.store.get(ids[-1])['partner_sku'])

    def test_stale_catalog_preview_has_no_campaign_or_receipt_write(self):
        ids = self.products(2)
        body = self.body(ids)
        preview = self.campaign.preview(body)
        self.assertEqual(preview['totals']['ready'], 2)
        self.store.update(ids[0], {'facts': '十个黑色塑料收纳夹'},
                          self.store.get(ids[0])['revision'])
        with self.assertRaises(Problem):
            self.campaign.apply({**body, 'preview_token': preview['token']})
        self.assertEqual(self.app.automation.state()['runs'], [])
        with self.store.connect() as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM ops_requests WHERE key LIKE 'catalog-campaign:%'"
            ).fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
