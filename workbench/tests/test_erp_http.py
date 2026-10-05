"""Real local ERP endpoints with synthetic documents and an isolated database."""
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import Problem
from server import App, Handler, LocalHTTPServer


class ERPHTTPTests(unittest.TestCase):
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
        self.app.collection_schedules.close(); self.app.backup_schedules.close()
        self.app.collection_executor.shutdown(wait=True, cancel_futures=True)
        self.app.source_inbox.close(); self.app.automation.close()
        self.app.visual_checks.close(); self.app.visuals.close(); self.app.media.close()
        self.app.models.codex.close(); self.app.executor.shutdown(wait=True)
        self.tmp.cleanup()

    def request(self, path, body=None, authenticated=True, raw=False):
        headers = {'Content-Type': 'application/json'}
        if body is not None and authenticated:
            headers['X-Workbench-Token'] = self.app.token
        request = Request(self.url + path, headers=headers,
                          data=json.dumps(body).encode() if body is not None else None)
        with urlopen(request, timeout=10) as response:
            payload = response.read()
            return (payload, dict(response.headers)) if raw else json.loads(payload)

    def entities_and_product(self):
        shop = self.request('/api/ops/entity', {'request_id':'erp-http-shop', 'kind':'shop', 'name':'合成店铺'})
        warehouse = self.request('/api/ops/entity', {'request_id':'erp-http-warehouse', 'kind':'warehouse', 'name':'合成仓库'})
        imported = self.request('/api/import', {'products':[
            {'title_zh':'黑色合成夹子', 'source_sku':'BLACK', 'source_url':'https://example.com/clips',
             'supplier':'合成供应商', 'brand':'合成品牌', 'facts':'黑色；5件装', 'cost_cny':5, 'stock':10},
            {'title_zh':'蓝色合成夹子', 'source_sku':'BLUE', 'source_url':'https://example.com/clips',
             'supplier':'合成供应商', 'brand':'合成品牌', 'facts':'蓝色；5件装', 'cost_cny':5, 'stock':10}]})
        products = [self.app.store.get(pid) for pid in imported['created']]
        return shop, warehouse, products

    def test_new_read_endpoints_and_utf8_download_headers(self):
        for endpoint in ('order-intake', 'settlements', 'catalog-groups', 'analytics',
                         'collection-schedules', 'backup-schedules', 'bank-reconciliation',
                         'fulfillment', 'procurement', 'after-sales', 'alerts', 'batch-editor',
                         'inventory-counts','shipping-manifests','pricing-plans','ad-analytics',
                         'import-profiles','domestic-capture','supplier-quotes','fx-registry','replenishment'):
            self.assertIsInstance(self.request('/api/' + endpoint + '/state'), dict)
        for endpoint in ('order-intake', 'settlements', 'analytics', 'bank-reconciliation', 'after-sales'):
            action = 'export' if endpoint in ('analytics', 'after-sales') else 'template'
            data, headers = self.request('/api/' + endpoint + '/' + action, raw=True)
            self.assertTrue(data.startswith(b'\xef\xbb\xbf'))
            self.assertIn("filename*=UTF-8''", headers['Content-Disposition'])
            self.assertIn('text/csv', headers['Content-Type'])

    def test_domestic_capture_extension_is_a_fixed_source_only_download(self):
        import io, zipfile
        payload,headers=self.request('/api/domestic-capture/extension',raw=True)
        self.assertEqual(headers['Content-Type'],'application/zip')
        self.assertIn("filename*=UTF-8''",headers['Content-Disposition'])
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            self.assertEqual(set(archive.namelist()),{'noon-domestic-capture/'+n for n in
                ('manifest.json','parser.js','popup.html','popup.css','popup.js')})
            manifest=json.loads(archive.read('noon-domestic-capture/manifest.json'))
            self.assertEqual(manifest['manifest_version'],3)
            self.assertNotIn('cookies',manifest['permissions'])
            self.assertNotIn('<all_urls>',manifest.get('host_permissions',[]))
        providers=self.request('/api/channels/state')['providers']
        self.assertEqual({p['id'] for p in providers if p.get('preferred')},
                         {'1688','taobao','pinduoduo','supplier_file'})

    def test_frozen_backend_extension_uses_packaged_resource_directory(self):
        import io, zipfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as packed:
            base=Path(packed);folder=base/'domestic-extension';folder.mkdir()
            files=('manifest.json','parser.js','popup.html','popup.css','popup.js')
            for name in files:(folder/name).write_bytes(('packaged-'+name).encode())
            with patch('server.BASE',base),patch('server.sys.frozen',True,create=True):
                payload,_headers=self.request('/api/domestic-capture/extension',raw=True)
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                for name in files:
                    self.assertEqual(archive.read('noon-domestic-capture/'+name),('packaged-'+name).encode())

    def test_shared_order_and_settlement_ledger_with_durable_replay(self):
        shop, warehouse, products = self.entities_and_product()
        row = {'external_id':'SYNTHETIC-001', 'partner_sku':products[0]['partner_sku'],
               'quantity':2, 'unit_price':'12.50', 'currency':'SAR'}
        preview = self.request('/api/order-intake/preview', {
            'shop_id':shop['id'], 'warehouse_id':warehouse['id'], 'rows':[row]})
        self.assertEqual(preview['ready'], 1)
        self.assertEqual(self.request('/api/state?surface=orders')['ops']['documents'], [])
        apply = {'token':preview['token'], 'request_id':'erp-http-orders', 'confirmed':True}
        with self.assertRaises(HTTPError) as denied:
            self.request('/api/order-intake/apply', apply, authenticated=False)
        self.assertEqual(denied.exception.code, 403)
        receipt = self.request('/api/order-intake/apply', apply)
        self.assertEqual(receipt['created'], 1)
        self.assertEqual(self.request('/api/order-intake/apply', apply), receipt)
        orders = self.request('/api/state?surface=orders')['ops']['documents']
        self.assertEqual(len(orders), 1); self.assertEqual(orders[0]['status'], 'new')
        # Analytics filters orders by persisted creation day; follow this order's
        # recorded timestamp instead of a fixed date or a second wall-clock read.
        business_day = orders[0]['created_at'][:10]
        self.assertEqual(self.request('/api/state?surface=inventory')['ops']['stock'], [])
        batch = self.request('/api/settlements/preview', {'shop_id':shop['id'], 'format':'json',
            'content':json.dumps([{'evidence_key':'synthetic-sale-1','external_id':'SYNTHETIC-001',
                'category':'sale','currency':'SAR','amount':'25.00','date':business_day,
                'fx':'1.9','evidence':'合成结算第1行','kind':'income'}])})
        self.assertEqual(batch['summary']['ready'], 1)
        confirm = {'batch_id':batch['id'],'token':batch['token'],
                   'request_id':'erp-http-settlement','confirmed':True}
        saved = self.request('/api/settlements/apply', confirm)
        self.assertEqual(saved['summary']['imported'], 1)
        self.assertEqual(self.request('/api/settlements/apply', confirm), saved)
        with self.app.store.connect() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM finance_entries').fetchone()[0], 1)
            self.assertEqual(connection.execute('SELECT count(*) FROM finance_payments').fetchone()[0], 0)
        report = self.request(f'/api/analytics/state?from={business_day}&to={business_day}')
        self.assertEqual(report['orders']['total'], 1)
        self.assertEqual(report['order_currencies'][0]['currency'], 'SAR')

    def test_group_confirmation_is_local_and_revision_bound(self):
        _, _, products = self.entities_and_product()
        body = {'name':'合成夹子系列','axes':['颜色'], 'members':[
            {'product_id':p['id'], 'revision':p['revision'], 'values':{'颜色':color}}
            for p, color in zip(products, ['黑色','蓝色'])]}
        preview = self.request('/api/catalog-groups/preview', body)
        saved = self.request('/api/catalog-groups/save', {**body,
            'preview_digest':preview['preview_digest'], 'confirmed':True, 'request_id':'erp-http-group'})
        self.assertEqual(len(saved['members']), 2)
        self.assertTrue(all(not self.app.store.get(p['id'])['reviewed'] for p in products))
        self.assertEqual(len(self.request('/api/catalog-groups/state')['rows']), 1)

    def test_restore_lock_applies_to_every_new_write_route(self):
        self.app.recovery.pending.write_text('{}')
        endpoints = ('supplier-quotes/save','supplier-quotes/cancel','fx-registry/save','fx-registry/cancel',
                     'fx-registry/convert','replenishment/apply','domestic-capture/apply','inventory-counts/apply','shipping-manifests/apply',
                     'shipping-manifests/handoff','pricing-plans/save','ad-analytics/apply',
                     'import-profiles/save','import-profiles/remove','order-intake/apply','settlements/apply','catalog-groups/save',
                     'collection-schedules/save','backup-schedules/run', 'bank-reconciliation/match',
                     'fulfillment/apply', 'procurement/apply', 'after-sales/refund',
                     'alerts/action', 'batch-editor/apply')
        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint), self.assertRaises(HTTPError) as blocked:
                self.request('/api/' + endpoint, {})
            self.assertEqual(blocked.exception.code, 409)

    def test_catalog_diagnostics_are_read_only_and_exports_are_downloadable(self):
        _, _, products = self.entities_and_product()
        body = {'name':'合成规格诊断', 'axes':['颜色','尺寸'], 'members':[
            {'product_id':p['id'],'revision':p['revision'],
             'values':{'颜色':color,'尺寸':size}}
            for p,color,size in zip(products, ('黑色','蓝色'), ('小','大'))]}
        with self.app.store.connect() as c:
            before = [tuple(r) for r in c.execute('SELECT * FROM products ORDER BY id')]
        diagnosis = self.request('/api/catalog-groups/diagnose', body)
        self.assertIsInstance(diagnosis, dict)
        self.assertEqual(self.request('/api/catalog-groups/state')['rows'], [])
        preview = self.request('/api/catalog-groups/preview', body)
        self.request('/api/catalog-groups/save', {**body,'preview_digest':preview['preview_digest'],
            'confirmed':True,'request_id':'http-deep-group-save'})
        gid = self.request('/api/catalog-groups/state')['rows'][0]['id']
        data, headers = self.request('/api/catalog-groups/export?id='+gid, raw=True)
        self.assertTrue(data.startswith(b'\xef\xbb\xbf'))
        self.assertIn('text/csv',headers['Content-Type'])
        self.assertIn("filename*=UTF-8''",headers['Content-Disposition'])
        with self.app.store.connect() as c:
            self.assertEqual([tuple(r) for r in c.execute('SELECT * FROM products ORDER BY id')], before)
        with self.assertRaises(HTTPError) as unauthorized:
            self.request('/api/catalog-groups/diagnose', body, authenticated=False)
        self.assertEqual(unauthorized.exception.code,403)
        self.app.recovery.pending.write_text('{}')
        with self.assertRaises(HTTPError) as blocked:
            self.request('/api/catalog-groups/diagnose', body)
        self.assertEqual(blocked.exception.code,409)

    def test_alert_filter_validation_and_csv_download_use_server_routes(self):
        state = self.request('/api/alerts/state?severity=warning&sort=severity&mode=open')
        self.assertTrue(all(r['severity']=='warning' and r['mode']=='open' for r in state['rows']))
        data,headers = self.request('/api/alerts/export?severity=warning&mode=open',raw=True)
        self.assertTrue(data.startswith(b'\xef\xbb\xbf'))
        self.assertIn('text/csv',headers['Content-Type'])
        self.assertIn("filename*=UTF-8''",headers['Content-Disposition'])
        for query in ('severity=invalid','sort=invalid','mode=invalid'):
            for route in ('state','export'):
                with self.subTest(route=route,query=query),self.assertRaises(HTTPError) as invalid:
                    self.request('/api/alerts/'+route+'?'+query)
                self.assertEqual(invalid.exception.code,400)

    def manual_order(self, shop, warehouse, product, quantity=1):
        return self.request('/api/ops/order', {'shop_id':shop['id'], 'warehouse_id':warehouse['id'],
            'external_id':'SYNTHETIC-ERP-ORDER', 'currency':'SAR', 'request_id':'erp-http-manual-order',
            'lines':[{'product_id':product['id'], 'quantity':quantity, 'unit_price':'12.50'}]})

    def after_sales_action(self, action, body, key):
        preview = self.request('/api/after-sales/preview', {**body, 'action':action})
        request = {**body, 'preview_digest':preview['preview_digest'], 'confirmed':True, 'request_id':key}
        receipt = self.request('/api/after-sales/' + action, request)
        self.assertEqual(self.request('/api/after-sales/' + action, request), receipt)
        return receipt

    def test_fulfillment_return_quarantine_release_and_refund_conserve_ledger(self):
        shop, warehouse, products = self.entities_and_product(); product = products[0]
        order = self.manual_order(shop, warehouse, product)
        self.request('/api/ops/adjust', {'product_id':product['id'], 'warehouse_id':warehouse['id'],
            'quantity':10, 'direction':'in', 'reason':'合成盘点入库', 'request_id':'erp-http-stock'})
        waves = []
        for action in ('reserve','ship'):
            body = {'action':action,'order_ids':[order['id']]}
            if action == 'ship':
                body['shipments'] = [{'order_id':order['id'],'carrier':'合成承运商','tracking':'SYNTHETIC-TRACK-1'}]
            preview = self.request('/api/fulfillment/preview', body)
            self.assertTrue(preview['can_apply'])
            request = {'token':preview['token'],'request_id':'erp-http-' + action,'confirmed':True}
            receipt = self.request('/api/fulfillment/apply', request)
            self.assertEqual(self.request('/api/fulfillment/apply', request), receipt)
            waves.append(receipt)
        order = self.request('/api/state?surface=orders')['ops']['documents'][0]
        self.assertEqual(order['status'], 'shipped')
        case = self.after_sales_action('create', {'order_id':order['id'],'order_revision':order['revision'],
            'reason':'合成退货待复核','quantities':{product['id']:1},'return_disposition':'quarantine'}, 'erp-http-case')
        case = self.after_sales_action('receive', {'id':case['id'],'revision':case['revision'],
            'order_revision':case['order_revision'],'evidence':'合成收到一件'}, 'erp-http-return')
        stock = self.request('/api/state?surface=inventory')['ops']['stock'][0]
        self.assertEqual((stock['on_hand'],stock['reserved']), (9,0))
        case = self.after_sales_action('dispose', {'id':case['id'],'revision':case['revision'],
            'order_revision':case['order_revision'],'disposition':'release','quantities':{product['id']:1},
            'evidence':'合成实物复核通过'}, 'erp-http-release')
        stock = self.request('/api/state?surface=inventory')['ops']['stock'][0]
        self.assertEqual((stock['on_hand'],stock['reserved']), (10,0))
        case = self.after_sales_action('refund', {'id':case['id'],'revision':case['revision'],
            'order_revision':case['order_revision'],'category':'refund','amount':'12.50','currency':'SAR',
            'fx':'1.9','date':'2026-10-03','evidence':'合成退款费用第1行','evidence_key':'erp-http-refund-1',
            'reason':'合成全额退款费用'}, 'erp-http-refund')
        with self.app.store.connect() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM finance_entries').fetchone()[0],1)
            self.assertEqual(connection.execute('SELECT count(*) FROM finance_payments').fetchone()[0],0)
        order = self.request('/api/state?surface=orders')['ops']['documents'][0]
        self.assertEqual(order['lines'][0]['returned'], 1)
        self.assertEqual(len(order['returns']), 1)
        self.assertEqual(len(self.request('/api/fulfillment/export?id=' + waves[0]['id'], raw=True)[0]) > 0, True)

    def test_bank_matching_settles_once_without_second_expense_posting(self):
        entry = self.request('/api/finance/entry', {'kind':'expense','category':'other','currency':'SAR',
            'amount':'25.00','date':'2026-10-03','fx':'1.9','evidence':'合成费用第1行',
            'evidence_key':'erp-http-bank-expense','request_id':'erp-http-bank-entry'})
        batch = self.request('/api/bank-reconciliation/preview', {'format':'json', 'content':json.dumps([
            {'account_name':'合成SAR账户','bank_reference':'SYNTHETIC-BANK-1','currency':'SAR',
             'direction':'out','amount':'25.00','date':'2026-10-03','fx':'1.9','evidence':'合成银行第1行'}])})
        imported = self.request('/api/bank-reconciliation/import', {'batch_id':batch['id'],'token':batch['token'],
            'request_id':'erp-http-bank-import','confirmed':True})
        with self.app.store.connect() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM finance_payments').fetchone()[0],0)
        row = self.request('/api/bank-reconciliation/state')['batches'][0]['rows'][0]
        body = {'row_id':row['id'],'row_token':row['row_token'],'entry_id':entry['id'],
                'entry_revision':entry['revision'],'confirmed':True,'request_id':'erp-http-bank-match'}
        saved = self.request('/api/bank-reconciliation/match', body)
        self.request('/api/bank-reconciliation/match', body)
        self.assertTrue(saved)
        with self.app.store.connect() as connection:
            self.assertEqual(connection.execute('SELECT count(*) FROM finance_payments').fetchone()[0],1)
            self.assertEqual(connection.execute('SELECT count(*) FROM finance_entries').fetchone()[0],1)
        self.assertEqual(self.request('/api/bank-reconciliation/state')['batches'][0]['summary']['matched'],1)

    def test_procurement_and_batch_edit_revisions_are_visible_across_modules(self):
        shop, warehouse, products = self.entities_and_product(); product = products[0]
        order = self.manual_order(shop, warehouse, product, quantity=2)
        supplier = self.request('/api/ops/entity', {'kind':'supplier','name':'合成采购供应商','request_id':'erp-http-supplier'})
        purchase = self.request('/api/ops/purchase', {'supplier_id':supplier['id'],'warehouse_id':warehouse['id'],
            'lines':[{'product_id':product['id'],'quantity':3,'unit_price':'5.00'}],'request_id':'erp-http-purchase'})
        body = {'order_id':order['id'],'purchase_id':purchase['id'],'product_id':product['id'],
            'quantity':2,'order_revision':order['revision'],'purchase_revision':purchase['revision'],'link_revision':0}
        preview = self.request('/api/procurement/preview', body)
        linked = self.request('/api/procurement/apply', {**body,'preview_token':preview['preview_token'],
            'confirmed':True,'request_id':'erp-http-link'})
        self.assertEqual(linked['quantity'],2)
        self.assertEqual(self.request('/api/state?surface=inventory')['ops']['stock'],[])
        patch = {'product_ids':[product['id']],'patch':{'facts':'黑色；经核对5件装'},'mode':'replace'}
        preview = self.request('/api/batch-editor/preview', patch)
        self.request('/api/batch-editor/apply', {**patch,'preview_token':preview['token'],
            'confirmed':True,'request_id':'erp-http-batch-edit'})
        changed = self.app.store.get(product['id'])
        self.assertEqual(changed['facts'],'黑色；经核对5件装')
        self.assertEqual(changed['partner_sku'],product['partner_sku'])
        self.assertFalse(changed['reviewed'])

    def test_restore_disables_both_new_schedulers(self):
        account = self.request('/api/channels/save', {'provider':'custom_json','name':'合成授权账户',
            'base_url':'https://example.com/catalog','enabled':True,'config':{}})
        rule = self.request('/api/collection-schedules/save', {'name':'合成周期读取','account_id':account['id'],
            'account_revision':account['revision'],'query':'','page_limit':1,'interval_minutes':15,
            'enabled':True,'confirmed':True,'request_id':'erp-http-source-rule'})
        config = self.request('/api/backup-schedules/state')
        self.request('/api/backup-schedules/save', {'version':config['version'],'enabled':True,
            'interval_minutes':60,'retain_count':2,'confirmed':True,'request_id':'erp-http-backup-rule'})
        copy = Path(self.tmp.name) / 'paused.sqlite3'
        with self.app.store.connect() as source, sqlite3.connect(copy) as target:
            source.backup(target)
        self.app.recovery.paused_copy(copy)
        with sqlite3.connect(copy) as connection:
            self.assertEqual(connection.execute('SELECT enabled FROM collection_schedule_rules').fetchone()[0],0)
            self.assertEqual(connection.execute('SELECT enabled FROM backup_schedule_config').fetchone()[0],0)

    def test_opt_in_catalog_http_bootstrap_is_bounded_and_catches_concurrent_edit(self):
        ids=[]
        for start, count in ((0,500),(500,251)):
            result=self.request('/api/import', {'products':[
                {'title_zh':'合成分页商品 '+str(i),'source_url':'https://example.com/capacity',
                 'source_sku':'HTTP-'+str(i),'facts':'黑色；5件装'}
                for i in range(start,start+count)]})
            ids.extend(result['created'])
        first=self.request('/api/state?catalog_paged=1')
        self.assertEqual(len(first['products']),500)
        self.assertTrue(first['catalog_reset'])
        cache={p['id']:p for p in first['products']}
        changed=first['products'][0]
        self.request('/api/products/'+changed['id']+'/save', {'revision':changed['revision'],
            'data':{**changed,'facts':'黑色；经核对5件装'}})
        page=first; requests=0
        while page.get('catalog_has_more'):
            page=self.request('/api/catalog/snapshot?catalog_paged=1&catalog_token='+page['catalog_token'])
            requests+=1;self.assertLess(requests,10)
            if page.get('catalog_reset'):cache={}
            for product in page.get('products',page.get('product_changes',[])):cache[product['id']]=product
            for pid in page.get('removed_product_ids',[]):cache.pop(pid,None)
            self.assertLessEqual(len(page.get('products',page.get('product_changes',[]))),500)
        self.assertEqual(set(cache),set(ids))
        self.assertEqual(cache[changed['id']]['facts'],'黑色；经核对5件装')

    def test_submit_reconciliation_http_route_is_authenticated_and_idempotent(self):
        pid=self.app.store.import_rows([{'title_zh':'HTTP人工对账合成商品','source_sku':'HTTP-RECON'}])['created'][0]
        product=self.app.store.get(pid);job_id=self.app.store.add_job(pid,'submit',product['revision'])
        self.app.store.job_result(job_id,'uncertain','合成丢失响应')
        body={'request_id':'http-reconcile-once','job_id':job_id,'outcome':'accepted',
              'note':'合成HTTP测试：SKU回查命中','sku_parent':'HTTP-NOON-QA','confirmed':True}
        result=self.request('/api/content-submit-batch/reconcile',body)
        replay=self.request('/api/content-submit-batch/reconcile',body)
        self.assertEqual(result['job_status'],'needs_attention');self.assertTrue(replay['replayed'])
        self.assertFalse(result['reconciliation']['live_verified'])
        self.assertEqual(self.app.store.get(pid)['platform']['sku_parent'],'HTTP-NOON-QA')
        unauth=Request(self.url+'/api/content-submit-batch/reconcile',headers={'Content-Type':'application/json'},
                       data=json.dumps({**body,'request_id':'unauth-reconcile'}).encode())
        with self.assertRaises(HTTPError) as denied:urlopen(unauth,timeout=10)
        self.assertIn(denied.exception.code,(401,403))

    def test_uncertain_paid_automation_retry_requires_confirmation_over_http(self):
        profile=self.app.models.save({'name':'HTTP MiniMax plan','provider':'minimax-subscription','model':'MiniMax-M3',
            'api_key':'sk-cp-http-synthetic','enabled':True})['id']
        self.app.models.route({'role':'primary','profile_id':profile})
        pid=self.request('/api/import',{'products':[{'title_zh':'HTTP订阅超时商品','source_sku':'HTTP-MODEL-RETRY',
            'source_url':'https://example.com/http-model-retry','supplier':'HTTP synthetic supplier','facts':'黑色，1件'}]})['created'][0]
        run=self.app.automation.create({'request_id':'http-uncertain-model-run','name':'HTTP uncertain model retry',
            'product_ids':[pid],'plan':{'translate':True}})['id']
        self.app.automation.tick() # source step
        with patch('models.request_json',side_effect=Problem('synthetic timeout after dispatch',502)):
            self.app.automation.tick() # paid model attempt ends with an uncertain receipt
        item=next(i for i in self.app.automation.state()['items'] if i['run_id']==run)
        self.assertEqual((item['status'],item['attempt']),('attention',0))
        def post(body):
            req=Request(self.url+'/api/automation/control',headers={'Content-Type':'application/json','X-Workbench-Token':self.app.token},
                        data=json.dumps(body).encode())
            try:
                with urlopen(req,timeout=10) as response:return response.status,json.loads(response.read())
            except HTTPError as error:return error.code,json.loads(error.read())
        code,denied=post({'item_id':item['id'],'action':'retry','revision':self.app.store.get(pid)['revision']})
        self.assertEqual(code,409);self.assertIn('可能再次消耗订阅额度',denied['error'])
        self.assertEqual(next(i for i in self.app.automation.state()['items'] if i['id']==item['id'])['attempt'],0)
        code,accepted=post({'item_id':item['id'],'action':'retry','revision':self.app.store.get(pid)['revision'],
                            'confirm_model_retry_after_prior_call':True})
        self.assertEqual(code,200);self.assertEqual(accepted['id'],item['id'])
        self.assertEqual(next(i for i in self.app.automation.state()['items'] if i['id']==item['id'])['attempt'],1)

    def test_interrupted_product_translation_retry_requires_confirmation_over_http(self):
        profile=self.app.models.save({'name':'HTTP translation plan','provider':'minimax-subscription','model':'MiniMax-M3',
            'api_key':'sk-cp-http-translation','enabled':True})['id']
        self.app.models.route({'role':'primary','profile_id':profile})
        pid=self.request('/api/import',{'products':[{'title_zh':'HTTP翻译重试商品','source_sku':'HTTP-TRANSLATE-RETRY',
            'source_url':'https://example.com/http-translate-retry','supplier':'HTTP synthetic supplier','facts':'黑色，1件'}]})['created'][0]
        product=self.app.store.get(pid);old=self.app.store.add_job(pid,'translate',product['revision'])
        with self.app.store.connect() as c:
            c.execute("INSERT INTO model_calls(id,request_key,digest,profile_id,profile,product_id,status,reserved_micro,charged_micro,message,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                      ('http-synthetic-call','product-job:'+old,'digest',profile,'{}',pid,'uncertain',0,0,'synthetic timeout', '2026-10-04T00:00:00Z','2026-10-04T00:00:00Z'))
        self.app.store.job_result(old,'interrupted','synthetic restart during model call')
        path=f'/api/products/{pid}/translate';body={'revision':product['revision']}
        with patch.object(self.app.executor,'submit') as submit:
            with self.assertRaises(HTTPError) as rejected:
                self.request(path,body)
            self.assertEqual(rejected.exception.code,409)
            self.assertIn('模型步骤已有调用记录',rejected.exception.read().decode())
            submit.assert_not_called()
            accepted=self.request(path,{**body,'confirm_model_retry_after_prior_call':True})
            self.assertTrue(accepted['job_id']);submit.assert_called_once()
        with self.app.store.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM events WHERE product_id=? AND action='确认重试模型翻译'",(pid,)).fetchone()[0],1)


if __name__ == '__main__':
    unittest.main()
