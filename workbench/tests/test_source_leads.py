import sys
import tempfile
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import Problem, Store
from operations import Operations
from source_leads import SourceLeads


class SourceLeadsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        Operations(self.store)  # creates the shared idempotency receipt table
        self.leads = SourceLeads(self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def previewed(self, links, request_id='candidate-batch-1'):
        preview = self.leads.preview({'links': links})
        return {'links': links, 'preview_token': preview['token'],
                'request_id': request_id, 'confirmed': True}

    def test_preview_canonicalizes_and_isolates_candidate_states(self):
        self.store.import_rows([{'title_zh': '已有商品',
                                 'source_url': 'https://detail.1688.com/offer/3.html'}])
        self.leads.add(self.previewed('https://detail.1688.com/offer/2.html'))
        links = '\n'.join([
            'https://m.1688.com/offer/3.html?spm=x',
            'https://detail.1688.com/offer/2.html?track=y',
            'http://detail.1688.com/offer/4.html?track=z',
            'https://detail.1688.com/offer/4.html',
            'https://not1688.com/item?a=1',
            'not a URL',
        ])
        preview = self.leads.preview({'links': links})
        self.assertEqual(preview['counts'], {
            'ready': 2, 'cataloged': 1, 'queued': 1, 'duplicate': 1, 'invalid': 1})
        self.assertEqual(preview['rows'][2]['url'], 'https://detail.1688.com/offer/4.html')
        self.assertEqual(preview['rows'][3]['status'], 'duplicate')
        # Previewing candidates does not create products; only the seeded catalog row exists.
        self.assertEqual(len(self.store.list()), 1)

    def test_add_is_idempotent_and_rejects_stale_preview(self):
        body = self.previewed('https://detail.1688.com/offer/1.html')
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(self.leads.add, [body, body]))
        self.assertEqual({result['added'] for result in results}, {1})
        self.assertEqual({result['replayed'] for result in results}, {False, True})
        self.assertEqual(self.leads.list()['total'], 1)
        conflict = {**body, 'links': 'https://detail.1688.com/offer/2.html'}
        with self.assertRaises(Problem):
            self.leads.add(conflict)
        stale = self.previewed('https://detail.1688.com/offer/3.html', 'stale-preview')
        self.store.import_rows([{'title_zh': '并发加入商品',
                                 'source_url': 'https://detail.1688.com/offer/3.html'}])
        with self.assertRaises(Problem):
            self.leads.add(stale)

    def test_export_has_more_uses_remaining_source_pages_not_filtered_rows(self):
        urls = [f'https://supplier.example/items/{n}' for n in range(5001)]
        with self.store.connect() as connection:
            connection.executemany('INSERT INTO source_leads(url,created_at) VALUES(?,?)',
                                    [(url, '2026-10-04T00:00:00+00:00') for url in urls])
        self.store.import_rows([{'title_zh': f'已采集{n}', 'source_url': urls[n]}
                                for n in range(5)])

        first = self.leads.export(0)
        self.assertEqual(first['source_count'], 5000)
        self.assertEqual(len(first['urls']), 4995)
        self.assertTrue(first['has_more'])
        second = self.leads.export(1)
        self.assertEqual(second['source_count'], 1)
        self.assertEqual(second['urls'], [urls[5000]])
        self.assertFalse(second['has_more'])

        # Exactly one complete page is terminal; filtered catalog rows do not create
        # a fictitious extra file for the UI to offer.
        with self.store.connect() as connection:
            connection.execute('DELETE FROM source_leads WHERE url=?', (urls[5000],))
        exact = self.leads.export(0)
        self.assertEqual(exact['source_count'], 5000)
        self.assertEqual(len(exact['urls']), 4995)
        self.assertFalse(exact['has_more'])


if __name__ == '__main__':
    unittest.main()
