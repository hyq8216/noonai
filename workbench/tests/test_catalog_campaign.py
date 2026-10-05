import sys
import sqlite3
import subprocess
import tempfile
import time
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

    def test_model_route_change_after_preview_requires_fresh_confirmation(self):
        ids = self.products(1)
        body = self.body(ids, plan={'translate': True, 'review': True})
        profile_ids = []
        for name, model in (('Primary A', 'fixture-a'), ('Primary B', 'fixture-b'),
                            ('Review', 'fixture-review')):
            profile_ids.append(self.app.models.save({
                'name': name, 'provider': 'custom', 'base_url': 'https://model.example/v1',
                'model': model, 'enabled': False, 'daily_calls': 50, 'rpm': 10,
                'max_output_tokens': 4000, 'input_price': '1', 'output_price': '1',
                'daily_usd': '1'
            })['id'])
        def reset_routes():
            self.app.models.route({'role': 'primary', 'profile_id': profile_ids[0]})
            self.app.models.route({'role': 'review', 'profile_id': profile_ids[2]})

        def update_profile(profile_id):
            profile = self.app.models.get(profile_id)
            self.app.models.save({**profile, 'revision': profile['revision'],
                                  'name': profile['name'] + ' edited'})

        route_changes = (
            ('primary route', lambda: self.app.models.route(
                {'role': 'primary', 'profile_id': profile_ids[1]})),
            ('review route', lambda: self.app.models.route(
                {'role': 'review', 'profile_id': profile_ids[1]})),
            ('primary profile revision', lambda: update_profile(profile_ids[0])),
            ('review profile revision', lambda: update_profile(profile_ids[2])),
        )
        with patch.object(self.app.models, 'ready', return_value=True):
            for label, change in route_changes:
                reset_routes()
                with self.subTest(change=label):
                    preview = self.campaign.preview(body)
                    change()
                    with self.assertRaises(Problem) as error:
                        self.campaign.apply({**body, 'preview_token': preview['token']})
                    self.assertEqual(error.exception.status, 409)
                    self.assertEqual(self.app.automation.state()['runs'], [])
                    with self.store.connect() as connection:
                        self.assertEqual(connection.execute(
                            "SELECT count(*) FROM ops_requests WHERE key LIKE 'catalog-campaign:%'"
                        ).fetchone()[0], 0)

            reset_routes()
            preview = self.campaign.preview(body)
            self.app.models.route({'role': 'fallback', 'profile_id': profile_ids[1]})
            queued = self.campaign.apply({**body, 'preview_token': preview['token']})
        self.assertEqual(queued['totals']['ready'], 1,
                         'changing an unused fallback route should not stale the preview')
        self.assertEqual(len(self.app.automation.state()['runs']), 1)

    def test_catalog_campaign_rejects_unencodable_product_id_without_run_or_receipt(self):
        body = self.body(['bad-\ud800'])
        with self.assertRaises(Problem):
            self.campaign.preview(body)
        with self.assertRaises(Problem):
            self.campaign.apply({**body, 'preview_token': '0' * 64})
        self.assertEqual(self.app.automation.state()['runs'], [])
        with self.store.connect() as connection:
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM ops_requests WHERE key LIKE 'catalog-campaign:%'"
            ).fetchone()[0], 0)

    def test_full_5000_catalog_campaign_creates_ten_exactly_sized_runs_and_replays(self):
        ids = self.products(5000)
        body = self.body(ids, plan={'translate': False}, request_id='full-catalog-5000')
        preview = self.campaign.preview(body)
        self.assertEqual([chunk['count'] for chunk in preview['chunks']], [500] * 10)
        self.assertEqual([chunk['ready'] for chunk in preview['chunks']], [500] * 10)
        self.assertEqual(preview['totals']['ready'], 5000)
        self.assertEqual(preview['totals']['blocked'], 0)
        self.assertEqual(preview['totals']['calls'], 0)
        self.assertEqual(len(self.app.automation.state()['runs']), 0)

        queued = self.campaign.apply({**body, 'preview_token': preview['token']})
        self.assertEqual([run['ready'] for run in queued['runs']], [500] * 10)
        self.assertEqual(len(queued['runs']), 10)
        self.assertEqual(queued['totals']['ready'], 5000)
        state = self.app.automation.state()
        self.assertEqual(len(state['runs']), 10)
        self.assertEqual(sum(state['run_item_counts'][run['id']] for run in queued['runs']), 5000)
        replay = self.campaign.apply({**body, 'preview_token': preview['token']})
        self.assertTrue(replay['replayed'])
        self.assertEqual(len(self.app.automation.state()['runs']), 10)

    def test_5000_catalog_campaign_preserves_exceptions_at_every_chunk_boundary(self):
        ids = self.products(5000)
        # Put one blocked product at the end of every 500-item chunk. This
        # exercises exception-to-chunk mapping across both full and final
        # boundaries while keeping each chunk eligible for creation.
        blocked_ids = [ids[end - 1] for end in range(500, 5001, 500)]
        for product_id in blocked_ids:
            product = self.store.get(product_id)
            self.store.update(product_id, {'facts': ''}, product['revision'])

        body = self.body(ids, plan={'translate': False}, request_id='mixed-catalog-5000')
        preview = self.campaign.preview(body)
        self.assertEqual([chunk['count'] for chunk in preview['chunks']], [500] * 10)
        self.assertEqual([chunk['ready'] for chunk in preview['chunks']], [499] * 10)
        self.assertEqual([chunk['blocked'] for chunk in preview['chunks']], [1] * 10)
        self.assertEqual(preview['totals']['ready'], 4990)
        self.assertEqual(preview['totals']['blocked'], 10)
        self.assertEqual([row['id'] for row in preview['exception_rows']], blocked_ids)

        queued = self.campaign.apply({**body, 'preview_token': preview['token']})
        self.assertEqual([run['index'] for run in queued['runs']], list(range(1, 11)))
        self.assertEqual([run['ready'] for run in queued['runs']], [499] * 10)
        self.assertEqual(queued['skipped'], 10)
        self.assertEqual(sum(self.app.automation.state()['run_item_counts'][run['id']]
                             for run in queued['runs']), 4990)
        exceptions = self.campaign.exceptions(body['request_id'])
        self.assertEqual([row['sku'] for row in exceptions['rows']],
                         [self.store.get(product_id)['partner_sku'] for product_id in blocked_ids])

        replay = self.campaign.apply({**body, 'preview_token': preview['token']})
        self.assertTrue(replay['replayed'])
        self.assertEqual(len(self.app.automation.state()['runs']), 10)

    def test_5000_campaign_separates_active_and_blocked_products(self):
        ids = self.products(5000)
        active_id, blocked_id = ids[498], ids[499]
        active_body = {'product_ids': [active_id], 'plan': {'translate': False},
                       'name': '已存在的合成流程', 'request_id': 'existing-active-catalog-item'}
        active_preview = self.app.automation.preflight(active_body)
        active_run = self.app.automation.create({**active_body, 'preflight_token': active_preview['token']})
        product = self.store.get(blocked_id)
        self.store.update(blocked_id, {'facts': ''}, product['revision'])

        body = self.body(ids, plan={'translate': False}, request_id='active-and-blocked-5000')
        preview = self.campaign.preview(body)
        self.assertEqual(preview['totals']['ready'], 4998)
        self.assertEqual(preview['totals']['blocked'], 1)
        self.assertEqual(preview['totals']['active'], 1)
        self.assertEqual(preview['chunks'][0]['ready'], 498)
        self.assertEqual(preview['chunks'][0]['active'], 1)
        self.assertEqual(preview['chunks'][0]['blocked'], 1)
        self.assertEqual([(row['id'], row['status']) for row in preview['exception_rows']],
                         [(active_id, 'active'), (blocked_id, 'blocked')])

        queued = self.campaign.apply({**body, 'preview_token': preview['token']})
        self.assertEqual(queued['skipped'], 2)
        self.assertEqual([run['ready'] for run in queued['runs']], [498] + [500] * 9)
        counts = self.app.automation.state()['run_item_counts']
        self.assertEqual(sum(counts[run['id']] for run in queued['runs']), 4998)
        with self.store.connect() as connection:
            self.assertEqual(connection.execute(
                'SELECT count(*) FROM automation_items WHERE run_id=?',
                (active_run['id'],)).fetchone()[0], 1)
        exceptions = self.campaign.exceptions(body['request_id'])['rows']
        self.assertEqual([row['status'] for row in exceptions], ['active', 'blocked'])

    def test_failure_after_second_chunk_creation_rolls_back_and_same_request_retries(self):
        ids = self.products(501)
        body = self.body(ids, plan={'translate': False}, request_id='rollback-after-chunk-two')
        preview = self.campaign.preview(body)
        apply_body = {**body, 'preview_token': preview['token']}
        create = self.app.automation.create
        calls = 0

        def fail_after_second_chunk(*args, **kwargs):
            nonlocal calls
            result = create(*args, **kwargs)
            calls += 1
            if calls == 2:
                raise RuntimeError('synthetic failure after second chunk insert')
            return result

        with patch.object(self.app.automation, 'create', side_effect=fail_after_second_chunk):
            with self.assertRaisesRegex(RuntimeError, 'after second chunk'):
                self.campaign.apply(apply_body)
        self.assertEqual(self.app.automation.state()['runs'], [])
        with self.store.connect() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM automation_items').fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT count(*) FROM ops_requests WHERE key='catalog-campaign:rollback-after-chunk-two'"
            ).fetchone()[0], 0)

        queued = self.campaign.apply(apply_body)
        self.assertEqual([run['ready'] for run in queued['runs']], [500, 1])
        self.assertEqual(len(self.app.automation.state()['runs']), 2)

    def test_process_kill_after_second_chunk_insert_rolls_back_whole_campaign(self):
        with tempfile.TemporaryDirectory(prefix='noon-campaign-kill-') as temp:
            root = Path(temp) / 'data'
            ready = Path(temp) / 'second-chunk-inserted'
            script = r'''
import sys,time
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'workbench'))
from server import App
root=Path(sys.argv[1]);ready=Path(sys.argv[2]);app=App(root)
ids=[]
for start in range(0,501,500):
 rows=[{'title_zh':f'强杀恢复商品 {n:04d}','source_url':f'https://supplier.example/kill/{n}',
        'source_sku':f'KILL-{n:04d}','supplier':'合成供应商','facts':'合成规格事实'}
       for n in range(start,min(start+500,501))]
 ids.extend(app.store.import_rows(rows)['created'])
body={'product_ids':ids,'plan':{'translate':False},'name':'强杀回滚测试',
      'request_id':'process-kill-after-chunk-two','confirmed':True}
preview=app.catalog_campaign.preview(body);body['preview_token']=preview['token']
create=app.automation.create;calls=0
def pause_after_insert(*args,**kwargs):
 global calls
 result=create(*args,**kwargs);calls+=1
 if calls==2:
  ready.write_text('second chunk inserted')
  while True:time.sleep(.02)
 return result
app.automation.create=pause_after_insert
app.catalog_campaign.apply(body)
'''
            proc = subprocess.Popen(
                [sys.executable, '-c', script, str(root), str(ready)],
                cwd=Path(__file__).resolve().parents[2],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            deadline = time.monotonic() + 30
            while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            if not ready.exists():
                proc.kill()
                stdout, stderr = proc.communicate(timeout=10)
                self.fail(f'child did not reach second chunk insert; stdout={stdout!r}, stderr={stderr!r}')
            proc.kill()
            stdout, stderr = proc.communicate(timeout=10)
            self.assertNotEqual(proc.returncode, 0, stdout)

            with sqlite3.connect(root / 'workbench.sqlite3') as connection:
                self.assertEqual(connection.execute('SELECT count(*) FROM products').fetchone()[0], 501)
                self.assertEqual(connection.execute('SELECT count(*) FROM automation_runs').fetchone()[0], 0)
                self.assertEqual(connection.execute('SELECT count(*) FROM automation_items').fetchone()[0], 0)
                self.assertEqual(connection.execute(
                    "SELECT count(*) FROM ops_requests WHERE key='catalog-campaign:process-kill-after-chunk-two'"
                ).fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
