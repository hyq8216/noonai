"""Real local HTTP integration, with isolated supplier protocol fixtures."""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import App, Handler, LocalHTTPServer


class ChannelHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = App(self.tmp.name)
        self.server = LocalHTTPServer(('127.0.0.1', 0), Handler)
        self.server.app = self.app
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.app.collection_executor.shutdown(wait=True, cancel_futures=True)
        self.app.source_inbox.close(); self.app.automation.close()
        self.app.visual_checks.close(); self.app.visuals.close(); self.app.media.close()
        self.app.models.codex.close(); self.app.executor.shutdown(wait=True)
        self.tmp.cleanup()

    def call(self, path, body=None, authenticated=True):
        headers = {'Content-Type': 'application/json'}
        if body is not None and authenticated:
            headers['X-Workbench-Token'] = self.app.token
        request = Request(self.url+path, headers=headers,
                          data=json.dumps(body).encode() if body is not None else None)
        with urlopen(request, timeout=10) as response:
            return json.load(response)

    def account(self):
        return self.call('/api/channels/save', {
            'provider': 'custom_json', 'name': '隔离供应商', 'base_url': 'https://example.com/catalog',
            'enabled': True, 'token': 'http-fixture-secret', 'config': {}})

    def test_registry_and_saved_credentials_never_read_back(self):
        providers = self.call('/api/channels/state')['providers']
        self.assertTrue({'1688','shopify','ebay','amazon','custom_json'} <= {p['id'] for p in providers})
        account = self.account()
        state = self.call('/api/state?surface=channels')
        self.assertEqual(state['channels']['accounts'][0]['id'], account['id'])
        self.assertNotIn('http-fixture-secret', json.dumps(state))
        self.assertNotIn('http-fixture-secret', json.dumps(self.call('/api/channels/state')))

    def test_write_guard_and_stale_account_revision(self):
        with self.assertRaises(HTTPError) as denied:
            self.call('/api/channels/save', {'provider':'custom_json','name':'拒绝'}, authenticated=False)
        self.assertEqual(denied.exception.code, 403)
        account = self.account()
        edited = {**account, 'name': '编辑后'}
        self.call('/api/channels/save', edited)
        with self.assertRaises(HTTPError) as stale:
            self.call('/api/channels/save', edited)
        self.assertEqual(stale.exception.code, 409)

    def test_async_collection_preview_and_confirmed_import(self):
        account = self.account()
        items = [{'external_id':'variant-1','external_product_id':'product-1',
                  'title_zh':'隔离采集商品','source_url':'https://example.com/products/1',
                  'source_sku':'BLACK','supplier':'隔离供应商','facts':'黑色；5件装',
                  'stock':0,'source_currency':'USD','source_price':'9.50','images':[]}]
        body = {'account_id':account['id'],'request_id':'http-collection',
                'query':'clips','page_limit':1,'confirmed':True}
        with patch('channel_adapters.fetch_page', return_value={'items':items,'next_cursor':None,'warnings':[]}) as fetch:
            run = self.call('/api/collection/create', body)
            deadline = time.monotonic()+5
            while time.monotonic() < deadline:
                state = self.call('/api/collection/state')
                if state['candidates'] and all(r['status'] not in ('queued','running') for r in state['runs']):
                    break
                time.sleep(.02)
            else:
                self.fail('collection did not finish')
            replay = self.call('/api/collection/create', body)
            self.assertEqual(replay['id'], run['id'])
            self.assertEqual(fetch.call_count, 1)
        self.assertEqual(self.call('/api/state')['products'], [])
        selected = {'candidate_ids':[state['candidates'][0]['id']]}
        preview = self.call('/api/collection/preview', selected)
        apply = {**selected,'preview_token':preview['token'],'request_id':'http-import','confirmed':True}
        self.call('/api/collection/apply', apply)
        self.call('/api/collection/apply', apply)
        products = self.call('/api/state')['products']
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]['stock'], 0)
        self.assertIsNone(products[0]['cost_cny'])
        self.assertFalse(products[0]['reviewed'])
        self.assertEqual(products[0]['images'], [])
        original = products[0]['source_collection']
        edited = self.app.store.update(products[0]['id'], {'note':'本地补充'}, products[0]['revision'])
        self.assertEqual(edited['source_collection'], original)

    def test_restore_pending_rejects_channel_and_collection_writes(self):
        self.app.recovery.pending.write_text('{}')
        for path in ('/api/channels/save','/api/collection/create','/api/collection/apply'):
            with self.assertRaises(HTTPError) as denied:
                self.call(path, {})
            self.assertEqual(denied.exception.code, 409)

    def test_cancelled_network_read_finishes_before_restore_can_be_scheduled(self):
        account = self.account()
        entered = threading.Event(); release = threading.Event()
        def fetch(*args, **kwargs):
            entered.set(); release.wait(5)
            return {'items': [], 'next_cursor': None, 'warnings': []}
        with patch('channel_adapters.fetch_page', side_effect=fetch):
            try:
                run = self.call('/api/collection/create', {'account_id': account['id'],
                    'request_id': 'in-flight', 'page_limit': 1, 'confirmed': True})
                self.assertTrue(entered.wait(2))
                self.call('/api/collection/control', {'run_id':run['id'],'action':'cancel'})
                archive = self.app.recovery.create()
                stage = self.app.recovery.inspect(self.app.recovery.archive_path(archive['id']))
                with self.assertRaises(HTTPError) as denied:
                    self.call('/api/backup/schedule', {**stage,'confirmed':True})
                self.assertEqual(denied.exception.code, 409)
            finally:
                release.set()
            deadline = time.monotonic()+5
            while self.app.collection_futures and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertFalse(self.app.collection_futures)
            self.assertTrue(self.call('/api/backup/schedule', {**stage,'confirmed':True})['scheduled'])

    def test_executor_delivery_failure_is_visible_and_retryable(self):
        account = self.account()
        self.app.collection_executor.shutdown(wait=True)
        with self.assertRaises(HTTPError) as unavailable:
            self.call('/api/collection/create', {'account_id': account['id'],
                'request_id': 'executor-closed', 'page_limit':1, 'confirmed':True})
        self.assertEqual(unavailable.exception.code, 409)
        run = self.call('/api/collection/state')['runs'][0]
        self.assertEqual(run['status'], 'attention')
        self.assertEqual(self.app.store.list(), [])
