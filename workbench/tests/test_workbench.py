import base64
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request,urlopen
from urllib.error import HTTPError
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image
from core import Store,Problem,now,normalize_image,economics,payload,normalize_source_url
from server import App,Handler,ThreadingHTTPServer,export_package
from connectors import preflight_attributes,translate
from source_import import SourceImport
from source_updates import SourceUpdates

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=Store(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()
    def make(self,**extra):
        raw={'title_zh':'测试商品','source_url':'https://detail.1688.com/offer/123.html?tracking=abc',
             'source_sku':'BLACK-5','supplier':'测试供应商','facts':'黑色，5件装。',**extra}
        return self.store.get(self.store.import_rows([raw])['created'][0])
    def complete(self):
        p=self.make(brand='Test',category='test-category',title_en='Black Cable Clips, Set of 5',description_en='Five black clips.',
                    title_ar='مشابك سوداء',description_ar='خمسة مشابك سوداء',rights_evidence='自摄测试素材',mode='NGS',stock=10,cost_cny=8,supply_checked_at=now())
        return self.store.update(p['id'],{'content_verified':True,'images_verified':True,'category_verified':True},p['revision'],{'images':[{'id':'a','source':'a.jpg','file':'b.jpg','public_url':'https://example.com/a.jpg'}]})
    def test_duplicate_tracking_and_variant(self):
        p=self.make(); result=self.store.import_rows([{'title_zh':'重复','source_url':'https://detail.1688.com/offer/123.html?tracking=other','source_sku':'BLACK-5'}])
        self.assertEqual(result['duplicates'],[p['id']]); self.assertEqual(len(self.store.list()),1)
        result=self.store.import_rows([{'title_zh':'不同规格','source_url':p['source_url'],'source_sku':'WHITE-5'}]); self.assertEqual(len(result['created']),1)
    def test_source_identity_key_does_not_alias_pipe_characters_and_reads_legacy_key(self):
        rows=[
            {'title_zh':'链接甲','source_url':'https://supplier.example/a|b','source_sku':'c','supplier':'工厂','facts':'红色盒'},
            {'title_zh':'链接乙','source_url':'https://supplier.example/a','source_sku':'b|c','supplier':'工厂','facts':'蓝色盒'},
        ]
        result=self.store.import_rows(rows)
        self.assertEqual(len(result['created']),2)
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT count(DISTINCT source_key) FROM products').fetchone()[0],2)
        # Existing databases contain delimiter-joined identities. They remain matchable.
        legacy=self.make(source_url='https://supplier.example/legacy|path',source_sku='legacy-sku')
        with self.store.connect() as c:
            c.execute('UPDATE products SET source_key=? WHERE id=?',(legacy['source_url']+'|'+legacy['source_sku'],legacy['id']))
        replay=self.store.import_rows([{'title_zh':'旧商品重复','source_url':legacy['source_url'],'source_sku':legacy['source_sku']}])
        self.assertEqual(replay['duplicates'],[legacy['id']])
    def test_source_import_and_update_keep_pipe_delimited_identities_separate(self):
        with self.store.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS ops_requests(key TEXT PRIMARY KEY,digest TEXT NOT NULL,result TEXT NOT NULL)')
        rows=[
            {'title_zh':'链接甲','source_url':'https://supplier.example/a|b','source_sku':'c','supplier':'工厂','facts':'红色盒'},
            {'title_zh':'链接乙','source_url':'https://supplier.example/a','source_sku':'b|c','supplier':'工厂','facts':'蓝色盒'},
        ]
        importer=SourceImport(self.store);preview=importer.preview({'products':rows})
        self.assertEqual([row['status'] for row in preview['rows']],['ready','ready'])
        result=importer.apply({'products':rows,'preview_token':preview['token'],'confirmed':True})
        self.assertEqual(len(result['created']),2)
        updates=SourceUpdates(self.store);body={'products':[
            {'source_url':rows[0]['source_url'],'source_sku':rows[0]['source_sku'],'stock':2},
            {'source_url':rows[1]['source_url'],'source_sku':rows[1]['source_sku'],'stock':7},
        ]}
        update_preview=updates.preview(body)
        self.assertEqual([row['status'] for row in update_preview['rows']],['ready','ready'])
        updates.apply({**body,'preview_token':update_preview['token'],'confirmed':True})
        products=[self.store.get(pid) for pid in result['created']]
        self.assertEqual({(p['source_url'],p['source_sku']):p['stock'] for p in products},
                         {(rows[0]['source_url'],rows[0]['source_sku']):2,(rows[1]['source_url'],rows[1]['source_sku']):7})
        legacy=self.make(source_url='https://supplier.example/legacy|path',source_sku='legacy-sku',stock=3)
        with self.store.connect() as c:
            c.execute('UPDATE products SET source_key=? WHERE id=?',(legacy['source_url']+'|'+legacy['source_sku'],legacy['id']))
        old_file={'products':[{'title_zh':legacy['title_zh'],'source_url':legacy['source_url'],'source_sku':legacy['source_sku'],
                               'supplier':legacy['supplier'],'facts':legacy['facts']}]}
        old_preview=importer.preview(old_file)
        self.assertEqual(old_preview['rows'][0]['status'],'duplicate')
        self.assertEqual(old_preview['rows'][0]['existing_id'],legacy['id'])
        old_update={'products':[{'source_url':legacy['source_url'],'source_sku':legacy['source_sku'],'stock':9}]}
        old_update_preview=updates.preview(old_update)
        self.assertEqual(old_update_preview['rows'][0]['status'],'ready')
        updates.apply({**old_update,'preview_token':old_update_preview['token'],'confirmed':True})
        self.assertEqual(self.store.get(legacy['id'])['stock'],9)
    def test_source_url_normalization_and_credential_rejection(self):
        self.assertEqual(normalize_source_url('http://m.1688.com/offer/123.html?spm=share#detail'),
                         'https://detail.1688.com/offer/123.html')
        self.assertEqual(normalize_source_url('https://shop.example/product?id=red#gallery'),
                         'https://shop.example/product?id=red')
        for url in (
            'https://seller:secret@example.com/product/1',
            'https://:secret@example.com/product/1',
            'https://:@example.com/product/1',
        ):
            with self.subTest(url=url), self.assertRaises(Problem):
                self.store.import_rows([{'title_zh':'凭据不得入库','source_url':url}])
        self.assertEqual(self.store.list(),[])
        product=self.make()
        with self.assertRaises(Problem):
            self.store.update(product['id'],{'source_url':'https://:synthetic-password@example.org/product/1'},product['revision'])
        saved=self.store.get(product['id'])
        self.assertEqual(saved['revision'],product['revision'])
        self.assertEqual(saved['source_url'],product['source_url'])
    def test_atomic_invalid_batch(self):
        with self.assertRaises(Problem): self.store.import_rows([{'title_zh':'有效'},{'title_zh':'无效','stock':-1}])
        self.assertEqual(self.store.list(),[])
    def test_invalid_numbers(self):
        for value in ['nan','Infinity',-1,True]:
            with self.assertRaises(Problem): self.make(cost_cny=value)
        with self.assertRaises(Problem): self.make(stock=1.5)
    def test_no_imported_approval(self):
        p=self.make(content_verified=True,images_verified=True,category_verified=True)
        self.assertFalse(p['content_verified']); self.assertFalse(p['reviewed'])
    def test_review_gate_and_invalidation(self):
        p=self.make()
        with self.assertRaises(Problem): self.store.approve(p['id'],p['revision'])
        self.tmp.cleanup(); self.tmp=tempfile.TemporaryDirectory(); self.store=Store(self.tmp.name)
        p=self.complete(); self.assertEqual(p['issues'],[])
        approved=self.store.approve(p['id'],p['revision']); self.assertTrue(approved['reviewed'])
        changed=self.store.update(p['id'],{'facts':'黑色，6件装。'},p['revision'])
        self.assertFalse(changed['reviewed']); self.assertFalse(changed['content_verified'])
        with self.assertRaises(Problem): self.store.approve(p['id'],p['revision'])
    def test_stale_revision(self):
        p=self.make(); self.store.update(p['id'],{'note':'a'},p['revision'])
        with self.assertRaises(Problem): self.store.update(p['id'],{'note':'b'},p['revision'])
        self.assertEqual(self.store.get(p['id'])['note'],'a')
    def test_expired_supply_blocks(self):
        p=self.complete(); p=self.store.update(p['id'],{'supply_checked_at':'2020-01-01T00:00:00+00:00'},p['revision'])
        self.assertTrue(any('过期' in x for x in p['issues']))
    def test_demo_never_approved(self):
        pid=self.store.import_rows([{'title_zh':'示例'}],demo=True)['created'][0]
        self.assertTrue(any('示例' in x for x in self.store.get(pid)['issues']))
    def test_persistence_restart_jobs(self):
        p=self.make(); jid=self.store.add_job(p['id'],'translate',p['revision'])
        other=Store(self.tmp.name); other.recover_jobs()
        self.assertEqual(other.get(p['id'])['source_sku'],'BLACK-5')
        queued=other.history()['jobs'][0]
        self.assertEqual((queued['id'],queued['status']),(jid,'failed'))
        self.assertIn('尚未开始外部调用',queued['message'])
        # Once a worker atomically claims a job, restart cannot know whether an
        # external request left the process, so it must require reconciliation.
        running=other.add_job(p['id'],'translate',p['revision'])
        with other.connect() as c:c.execute("UPDATE jobs SET status='running' WHERE id=?",(running,))
        other.recover_jobs();result=other.history()['jobs'][0]
        self.assertEqual((result['id'],result['status']),(running,'interrupted'))
        self.assertIn('结果可能不确定',result['message'])
    def test_one_job_per_product(self):
        p=self.make(); self.store.add_job(p['id'],'translate',p['revision'])
        with self.assertRaises(Problem): self.store.add_job(p['id'],'translate',p['revision'])
    def test_image_and_zip(self):
        # Synthetic geometry only: tests exercise image handling, not a real product edit.
        image=Image.new('RGBA',(800,700),(255,0,0,128)); buf=io.BytesIO(); image.save(buf,'PNG')
        im=normalize_image(self.store,base64.b64encode(buf.getvalue()).decode(),'square')
        with Image.open(self.store.assets/im['file']) as result:
            self.assertEqual(result.size,(1600,1600)); self.assertEqual(result.mode,'RGB'); self.assertEqual(result.format,'JPEG'); self.assertTrue(result.info.get('icc_profile'))
            self.assertEqual(result.getpixel((0,0)),(255,255,255))
        self.assertEqual((self.store.assets/im['source']).read_bytes(),buf.getvalue())
        p=self.make(); p=self.store.update(p['id'],{},p['revision'],{'images':[im]})
        with zipfile.ZipFile(io.BytesIO(export_package(self.store,[p['id']]))) as z:
            self.assertIsNone(z.testzip()); self.assertEqual(len(z.namelist()),6)
            manifest=json.loads(z.read('商品档案.json')); self.assertEqual(manifest['kind'],'local-content-draft')
    def test_image_errors(self):
        with self.assertRaises(Problem): normalize_image(self.store,'bad','square')
        with self.assertRaises(Problem): normalize_image(self.store,'','invalid')
    def test_ngs_model(self):
        e=economics(dict(mode='NGS',cost_cny=28,domestic_shipping_cny=5,packing_cny=2,other_cny=3.5,acquisition_cny=6,transfer_usd=9,fx=7.2,loss_rate=.08,collection_rate=.015))
        self.assertEqual(e['contribution_cny'],14.22); self.assertEqual(e['retained_cny'],59.62)
    def test_missing_services_fail_honestly(self):
        with patch.dict(os.environ,{'TEXT_API_KEY':'','TEXT_MODEL':''}):
            with self.assertRaises(Problem): translate(self.make())
    def test_unmapped_category_blocks(self):
        p=self.complete()
        with self.assertRaises(Problem): preflight_attributes({'attributes':[{'attribute_code':'material','is_mandatory':True}]},payload(p))

    def test_image_url_shared_save(self):
        p=self.complete()
        q=self.store.update(p['id'],{'image_urls':{'a':'https://example.com/new.jpg'}},p['revision'])
        self.assertEqual(q['images'][0]['public_url'],'https://example.com/new.jpg')
        self.assertFalse(q['images_verified'])
        for url in ('javascript:bad','https://:synthetic-password@images.example/item.jpg','https://[malformed/item.jpg'):
            with self.subTest(url=url), self.assertRaises(Problem):
                self.store.update(q['id'],{'image_urls':{'a':url}},q['revision'])
        self.assertEqual(self.store.get(p['id'])['images'][0]['public_url'],'https://example.com/new.jpg')
    def test_source_snapshot_and_versions(self):
        p=self.make(); q=self.store.update(p['id'],{'facts':'更新事实'},p['revision'])
        self.assertEqual(q['source_snapshot']['facts'],'黑色，5件装。')
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM revisions WHERE product_id=?',(p['id'],)).fetchone()[0],2)
    def test_translation_limit(self):
        p=self.make()
        with patch.dict(os.environ,{'TEXT_DAILY_LIMIT':'0'}):
            with self.assertRaises(Problem): self.store.add_job(p['id'],'translate',p['revision'])
    def test_approval_expires_with_supply(self):
        p=self.complete(); self.store.approve(p['id'],p['revision'])
        with patch('core.datetime') as clock:
            from datetime import datetime,timezone,timedelta
            clock.now.return_value=datetime.now(timezone.utc)+timedelta(days=2)
            clock.fromisoformat=datetime.fromisoformat
            self.assertFalse(self.store.get(p['id'])['reviewed'])
    def test_worker_preserves_newer_edits(self):
        app=App(self.tmp.name); p=self.make()
        jid=self.store.add_job(p['id'],'translate',p['revision'])
        self.store.update(p['id'],{'note':'newer'},p['revision'])
        with patch('server.translate',return_value=({'title_en':'stale','description_en':'old','title_ar':'قديم','description_ar':'قديم'},[],{})):
            app.run(jid,p,'translate')
        self.assertEqual(self.store.get(p['id'])['note'],'newer')
        self.assertEqual(self.store.get(p['id'])['title_en'],'')
        self.assertEqual(self.store.history()['jobs'][0]['status'],'failed')
        app.executor.shutdown()
    def test_submit_200_with_content_problem_is_not_live(self):
        app=App(self.tmp.name); p=self.complete(); p=self.store.approve(p['id'],p['revision'])
        jid=self.store.add_job(p['id'],'submit',p['revision'])
        contract={'attributes':[{'attribute_code':k,'is_mandatory':True,'is_localizable':True,'attribute_type':'ATTRIBUTE_TYPE_TEXT'} for k in ['product_title','long_description']]}
        with patch.object(app,'config',return_value={'noon_ready':True,'submit_enabled':True}),patch('server.Noon') as client:
            client.return_value.attributes.return_value=contract
            client.return_value.submit.return_value={'sku_parent':'TEST-PARENT','status':{'status_id':3,'message':'invalid'}}
            app.run(jid,p,'submit')
        result=self.store.get(p['id'])
        self.assertEqual(result['platform']['sku_parent'],'TEST-PARENT')
        self.assertFalse(result['platform']['live_verified'])
        self.assertEqual(self.store.history()['jobs'][0]['status'],'needs_attention')
        app.executor.shutdown()

class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp=tempfile.TemporaryDirectory(); cls.app=App(cls.tmp.name)
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler); cls.server.app=cls.app
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True); cls.thread.start()
        cls.url=f'http://127.0.0.1:{cls.server.server_port}'
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.app.executor.shutdown(); cls.tmp.cleanup()
    def call(self,path,body=None,headers=None):
        req=Request(self.url+path,data=json.dumps(body).encode() if body is not None else None,headers=headers or {})
        return urlopen(req)
    def test_csrf_and_origin(self):
        with self.assertRaises(HTTPError) as cm: self.call('/api/import',{'products':[{'title_zh':'x'}]})
        self.assertEqual(cm.exception.code,403)
        with self.assertRaises(HTTPError) as cm: self.call('/api/import',{}, {'X-Workbench-Token':self.app.token,'Origin':'https://evil.example'})
        self.assertEqual(cm.exception.code,403)
    def test_supplier_import_rejects_unencodable_unicode_as_client_error(self):
        initial=len(self.app.store.list())
        headers={'X-Workbench-Token':self.app.token}
        for body in ({'csv':'商品名称\n\ud800'},{'products':[{'title_zh':'\ud800'}]}):
            with self.assertRaises(HTTPError) as failed:self.call('/api/import/preview',body,headers)
            self.assertEqual(failed.exception.code,400)
        self.assertEqual(len(self.app.store.list()),initial)
    def test_source_lead_preview_rejects_unencodable_unicode_as_client_error(self):
        headers={'X-Workbench-Token':self.app.token}
        with self.assertRaises(HTTPError) as failed:
            self.call('/api/source-leads/preview',{'links':'https://supplier.example/item/1\n\ud800'},headers)
        self.assertEqual(failed.exception.code,400)
        self.assertEqual(self.app.source_leads.list()['total'],0)
    def test_source_lead_add_rejects_unencodable_unicode_before_fingerprinting(self):
        headers={'X-Workbench-Token':self.app.token}
        with self.assertRaises(HTTPError) as failed:
            self.call('/api/source-leads/add',{'request_id':'bad-unicode','confirmed':True,
                      'preview_token':'stale','links':'https://supplier.example/item/1\n\ud800'},headers)
        self.assertEqual(failed.exception.code,400)
        self.assertEqual(self.app.source_leads.list()['total'],0)
    def test_operations_rejects_unencodable_unicode_before_fingerprinting(self):
        before=self.app.ops.state()['entities']
        with self.assertRaises(HTTPError) as failed:
            self.call('/api/ops/entity',{'request_id':'bad-unicode','kind':'supplier','name':'供应商\ud800'},
                      {'X-Workbench-Token':self.app.token})
        self.assertEqual(failed.exception.code,400)
        self.assertEqual(self.app.ops.state()['entities'],before)
    def test_platform_batch_preview_rejects_unencodable_product_id(self):
        headers={'X-Workbench-Token':self.app.token}
        with self.assertRaises(HTTPError) as failed:
            self.call('/api/platform-batch/preview',{'kind':'offers','product_ids':['bad-\ud800']},headers)
        self.assertEqual(failed.exception.code,400)
        self.assertEqual(self.app.store.history()['jobs'],[])
    def test_finance_rejects_unencodable_request_before_entry_or_receipt(self):
        before=self.app.finance.state()
        with self.assertRaises(HTTPError) as failed:
            self.call('/api/finance/entry',{'request_id':'bad-\ud800','kind':'income'},
                      {'X-Workbench-Token':self.app.token})
        self.assertEqual(failed.exception.code,400)
        self.assertEqual(self.app.finance.state()['entries'],before['entries'])
        with self.app.store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM finance_requests').fetchone()[0],0)
    def test_visual_generation_rejects_unencodable_product_before_task_creation(self):
        headers={'X-Workbench-Token':self.app.token}
        with self.assertRaises(HTTPError) as failed:
            self.call('/api/visuals/tasks',{'request_id':'valid-request','product_id':'bad-\ud800','revision':1,
                      'asset_ids':['synthetic-asset'],'shots':['hero'],'confirmed':True},headers)
        self.assertEqual(failed.exception.code,400)
        with self.app.store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM visual_jobs').fetchone()[0],0)
    def test_catalog_campaign_preview_rejects_unencodable_product_before_query(self):
        headers={'X-Workbench-Token':self.app.token}
        with self.assertRaises(HTTPError) as failed:
            self.call('/api/catalog-campaign/preview',{'product_ids':['bad-\ud800'],'plan':{'translate':False}},headers)
        self.assertEqual(failed.exception.code,400)
        self.assertEqual(self.app.automation.state()['runs'],[])
    def test_direct_product_import_rejects_unencodable_unicode_before_database_write(self):
        before=len(self.app.store.list())
        headers={'X-Workbench-Token':self.app.token}
        for product in ({'title_zh':'名称\ud800'},
                        {'title_zh':'合法名称','attribute_values':{'Material':{'value':'Steel\ud800'}}}):
            with self.assertRaises(HTTPError) as failed:
                self.call('/api/import',{'products':[product]},headers)
            self.assertEqual(failed.exception.code,400)
        self.assertEqual(len(self.app.store.list()),before)
    def test_state_and_persistence(self):
        data=json.loads(self.call('/api/state').read()); self.assertNotIn('TEXT_API_KEY',json.dumps(data))
        r=json.loads(self.call('/api/import',{'products':[{'title_zh':'HTTP商品'}]},{'X-Workbench-Token':self.app.token}).read())
        self.assertEqual(len(r['created']),1)
        self.assertTrue(any(p['title_zh']=='HTTP商品' for p in json.loads(self.call('/api/state').read())['products']))
    def test_public_image_url_endpoint_rejects_embedded_credentials(self):
        pid=self.app.store.import_rows([{'title_zh':'HTTP成图凭据测试'}])['created'][0]
        product=self.app.store.get(pid)
        product=self.app.store.update(pid,{},product['revision'],{'images':[{
            'id':'synthetic-image','source':'local.png','file':'synthetic.jpg',
            'public_url':'https://images.example/original.jpg'}]})
        for url in ('https://:synthetic-password@images.example/item.jpg','https://[malformed/item.jpg'):
            with self.subTest(url=url), self.assertRaises(HTTPError) as failed:
                self.call(f'/api/products/{pid}/images',{
                    'revision':product['revision'],'image_id':'synthetic-image','public_url':url},
                    {'X-Workbench-Token':self.app.token})
            self.assertEqual(failed.exception.code,400)
            saved=self.app.store.get(pid)
            self.assertEqual(saved['revision'],product['revision'])
            self.assertEqual(saved['images'][0]['public_url'],'https://images.example/original.jpg')
    def test_health_and_readiness_probes_are_small_and_database_aware(self):
        health=json.loads(self.call('/healthz').read())
        self.assertEqual(health,{'status':'ok','service':'noon-studio'})
        ready=json.loads(self.call('/readyz').read())
        self.assertEqual(ready,{'status':'ready','checks':{'database':'ok'}})
        self.assertNotIn(self.app.token,json.dumps(health)+json.dumps(ready))
        with patch.object(self.app.store,'connect',side_effect=OSError('private database path')):
            with self.assertRaises(HTTPError) as failed:self.call('/readyz')
            self.assertEqual(failed.exception.code,503)
            self.assertEqual(json.loads(failed.exception.read()),{'status':'not_ready','checks':{'database':'error'}})
    def test_traversal_and_unknown_host(self):
        with self.assertRaises(HTTPError): self.call('/assets/../../.env')
        with self.assertRaises(HTTPError) as cm: self.call('/api/state',headers={'Host':'evil.example'})
        self.assertEqual(cm.exception.code,403)

if __name__=='__main__': unittest.main(verbosity=2)
