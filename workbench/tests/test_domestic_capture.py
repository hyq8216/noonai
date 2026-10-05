import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Problem,ident
from server import App
from domestic_capture import DomesticCapture,MAX_BYTES

class DomesticCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.app=App(Path(self.tmp.name));self.d=DomesticCapture(self.app)
        self.account=self.app.channel_accounts.save({'provider':'1688','name':'合成1688来源','enabled':True,'base_url':'','config':{},'revision':0})
    def tearDown(self):
        for name in ('backup_schedules','collection_schedules','source_inbox','visual_checks','visuals','automation','media'):
            obj=getattr(self.app,name,None)
            if obj:obj.close()
        self.app.models.codex.close();self.app.collection_executor.shutdown(wait=True,cancel_futures=True);self.app.executor.shutdown(wait=True,cancel_futures=True);self.tmp.cleanup()
    def body(self,**changes):
        item={'product_id':'123456','sku':'SKU-BLACK','title':'合成黑色夹子','source_url':'https://detail.1688.com/offer/123456.html','source_price':2.5,'source_currency':'CNY','stock':0,'images':['https://example.com/test.png'],'facts':'颜色：黑色','captured_at':'2026-10-03T12:00:00+00:00','method':'visible-dom',**changes}
        return {'account_id':self.account['id'],'account_revision':self.account['revision'],'package':{'format':'noon-domestic-capture-v1','provider':'1688','captured_at':'2026-10-03T12:00:00+00:00','items':[item]}}
    def apply(self,body):
        pre=self.d.preview(body)
        return self.d.apply({**body,'preview_token':pre['token'],'confirmed':True,'request_id':ident()})
    def candidates(self):return self.app.source_collection.state()['candidates']
    def test_explicit_two_confirmations_candidate_then_existing_product_workflow(self):
        b=self.body();pre=self.d.preview(b);self.assertEqual(pre['counts']['new'],1);self.assertEqual(self.candidates(),[]);self.assertEqual(self.app.store.list(),[])
        with self.assertRaises(Problem):self.d.apply({**b,'preview_token':pre['token'],'request_id':ident()})
        result=self.apply(b);self.assertEqual(len(result['created']),1);self.assertEqual(self.app.store.list(),[])
        candidate=self.candidates()[0];self.assertEqual(candidate['normalized']['stock'],0);self.assertIsNone(candidate['normalized']['cost_cny']);self.assertEqual(candidate['raw']['source_price'],2.5)
        self.assertEqual(candidate['snapshots'][0]['capture']['product_id'],'123456')
        preview=self.app.source_collection.preview({'candidate_ids':result['candidate_ids']});self.assertEqual(preview['ready'],1)
        imported=self.app.source_collection.apply({'request_id':ident(),'candidate_ids':result['candidate_ids'],'preview_token':preview['token'],'confirmed':True})
        product=self.app.store.get(imported['created'][0]);self.assertIsNone(product['cost_cny']);self.assertEqual(product['source_sku'],'SKU-BLACK');self.assertFalse(product['reviewed'])
    def test_missing_sku_not_invented_and_missing_price_stock_retained(self):
        b=self.body(sku='',source_price=None,stock=None);p=self.d.preview(b);self.assertEqual(p['rows'][0]['missing'],['规格货号','网页售价','可供数量'])
        result=self.apply(b);candidate=self.candidates()[0];self.assertEqual(candidate['status'],'blocked');self.assertEqual(candidate['normalized']['source_sku'],'');self.assertIsNone(candidate['normalized']['stock'])
        self.assertEqual(self.app.source_collection.preview({'candidate_ids':result['candidate_ids']})['ready'],0)
    def test_url_platform_id_and_credential_fields_are_rejected(self):
        for changes in ({'source_url':'https://detail.1688.com.evil.com/offer/123456.html'},{'source_url':'https://detail.1688.com/offer/999.html'},{'source_url':'https://detail.1688.com/offer/123456.html?token=secret'},{'source_url':'https://user:secret@detail.1688.com/offer/123456.html'},{'source_url':'https://detail.1688.com/search?keyword=123456'},{'images':['https://example.com/image?access_token=secret']},{'images':['https://example.com/image?session_id=secret']},{'images':['https://example.com/image?csrf_token=secret']},{'source_url':'https://detail.1688.com/offer/123456.html?api_key=secret'},{'cookie':'session-secret'},{'cost_cny':2}):
            with self.assertRaises(Problem):self.d.preview(self.body(**changes))
        b=self.body();b['package']['metadata']={'access-token':'secret'}
        with self.assertRaises(Problem):self.d.preview(b)
        self.assertEqual(self.candidates(),[])
    def test_domestic_platform_urls_and_account_binding(self):
        for provider,url in (('taobao','https://item.taobao.com/item.htm?id=123456&spm=test'),('taobao','https://detail.tmall.com/item.htm?id=123456'),('pinduoduo','https://mobile.yangkeduo.com/goods.html?goods_id=123456')):
            account=self.app.channel_accounts.save({'provider':provider,'name':'合成'+url,'enabled':True,'base_url':'','config':{},'revision':0})
            b=self.body(source_url=url);b['account_id']=account['id'];b['account_revision']=account['revision'];b['package']['provider']=provider
            self.assertEqual(self.d.preview(b)['provider'],provider)
        b=self.body();b['package']['provider']='taobao'
        with self.assertRaises(Problem):self.d.preview(b)
        b=self.body();b['account_revision']+=1
        with self.assertRaises(Problem):self.d.preview(b)
    def test_bounds_bad_structure_currency_numbers_and_timestamp(self):
        for changes in ({'stock':True},{'stock':1.5},{'source_price':'2-5'},{'source_price':float('nan')},{'source_currency':'USD'},{'source_currency':''},{'product_id':'123/456'},{'sku':'bad\nSKU'},{'captured_at':'2026-10-03'},{'title':''},{'images':['http://example.com/insecure']}):
            with self.assertRaises(Problem):self.d.preview(self.body(**changes))
        b=self.body();b['package']['items']=b['package']['items']*501
        with self.assertRaises(Problem):self.d.preview(b)
        b=self.body();b['package']['items'][0]['title']='x'*MAX_BYTES
        with self.assertRaises(Problem):self.d.preview(b)
        b=self.body();b['package']['items']={}
        with self.assertRaises(Problem):self.d.preview(b)
    def test_replays_duplicate_same_observation_and_different_fact_conflict_preserve_product(self):
        b=self.body();pre=self.d.preview(b);body={**b,'preview_token':pre['token'],'confirmed':True,'request_id':ident()}
        result=self.d.apply(body);candidate=self.candidates()[0];initial_revision=candidate['revision'];self.assertTrue(self.d.apply(body)['replayed'])
        with self.assertRaises(Problem):self.d.apply({**body,'package':{**b['package'],'captured_at':'2026-10-04T00:00:00+00:00'}})
        duplicate=self.apply(b);self.assertEqual(duplicate['created'],[]);self.assertEqual(duplicate['skipped'],[1]);self.assertEqual(self.candidates()[0]['revision'],initial_revision)
        p=self.app.source_collection.preview({'candidate_ids':result['candidate_ids']});imported=self.app.source_collection.apply({'candidate_ids':result['candidate_ids'],'preview_token':p['token'],'confirmed':True,'request_id':ident()});pid=imported['created'][0];before=self.app.store.get(pid)
        changed=self.body(source_price=9,images=['https://example.com/other.png']);conflict=self.apply(changed);self.assertEqual(len(conflict['conflicts']),1)
        saved=self.candidates()[0];self.assertEqual(saved['status'],'conflict');self.assertEqual(len(saved['snapshots']),2);self.assertEqual(saved['raw']['source_price'],9);self.assertEqual(saved['normalized'],candidate['normalized']);self.assertEqual(self.app.store.get(pid),before)
        self.assertEqual(self.apply(changed)['skipped'],[1]);self.assertEqual(len(self.candidates()[0]['snapshots']),2)
    def test_stale_preview_and_pending_recovery_rejected(self):
        b=self.body();p=self.d.preview(b);self.apply(b)
        with self.assertRaises(Problem):self.d.apply({**b,'preview_token':p['token'],'confirmed':True,'request_id':ident()})
        self.app.recovery.pending.write_text('{}')
        with self.assertRaises(Problem):self.apply(self.body(sku='ANOTHER'))
    def test_manual_sku_correction_keeps_original_observation_and_audits_evidence(self):
        b=self.body(sku='',source_price=None,stock=None);unknown=self.apply(b);original_package=copy.deepcopy(b['package'])
        corrected={**b,'corrections':[{'row':1,'sku':'SUPPLIER-VERIFIED-BLACK','supplier':'人工核对供应商','facts':'人工核对颜色：黑色','evidence':'合成供应商规格单第2行，仅测试依据'}]}
        pre=self.d.preview(corrected);self.assertEqual(pre['counts']['new'],1);self.assertEqual(corrected['package'],original_package)
        result=self.apply(corrected);candidates=self.candidates();self.assertEqual(len(candidates),2)
        old=next(c for c in candidates if c['id']==unknown['created'][0]);self.assertEqual(old['status'],'blocked');self.assertEqual(old['normalized']['source_sku'],'')
        candidate=next(c for c in candidates if c['id']==result['created'][0]);capture=candidate['snapshots'][0]['capture']
        self.assertEqual(capture['original_raw']['source_sku'],'');self.assertEqual(capture['sku'],'');self.assertTrue(capture['correction']['manual'])
        self.assertEqual(capture['correction']['fields']['sku'],{'before':'','after':'SUPPLIER-VERIFIED-BLACK'});self.assertIn('规格单',capture['correction']['evidence'])
        self.assertIsNone(candidate['raw']['source_price']);self.assertIsNone(candidate['normalized']['stock']);self.assertIsNone(candidate['normalized']['cost_cny'])
        with self.app.store.connect() as c:
            event=json.loads(c.execute("SELECT detail FROM events WHERE action='国内网页采集录入' ORDER BY id DESC LIMIT 1").fetchone()[0]);self.assertEqual(event['manual_corrections'][0]['capture']['original_raw']['source_sku'],'')
        self.assertEqual(self.app.store.list(),[])
    def test_manual_correction_requires_evidence_no_secrets_no_prices_or_stock(self):
        b=self.body(sku='')
        invalid=[{'row':1,'sku':'REAL'},{'row':1,'sku':'REAL','evidence':' '},{'row':1,'sku':'','evidence':'规格单'},
                 {'row':1,'sku':'REAL','evidence':'Cookie: secret-session'}, {'row':1,'sku':'REAL','facts':'access_token=secret','evidence':'规格单'}, {'row':1,'sku':'REAL','evidence':'password=secret'}, {'row':1,'sku':'REAL','evidence':'refresh_token=secret'},
                 {'row':1,'source_price':0,'evidence':'售价'}, {'row':1,'stock':0,'evidence':'数量'}, {'row':1,'cost_cny':0,'evidence':'成本'},
                 {'row':2,'sku':'REAL','evidence':'规格单'}, {'row':True,'sku':'REAL','evidence':'规格单'}]
        for correction in invalid:
            with self.assertRaises(Problem):self.d.preview({**b,'corrections':[correction]})
        with self.assertRaises(Problem):self.d.preview({**b,'corrections':[{'row':1,'sku':'A','evidence':'依据'},{'row':1,'sku':'B','evidence':'依据'}]})
        self.assertEqual(self.candidates(),[])
    def test_correction_token_invalidates_on_values_or_evidence_and_existing_product_unchanged(self):
        b=self.body();result=self.apply(b);p=self.app.source_collection.preview({'candidate_ids':result['candidate_ids']});imported=self.app.source_collection.apply({'candidate_ids':result['candidate_ids'],'preview_token':p['token'],'confirmed':True,'request_id':ident()});before=self.app.store.get(imported['created'][0])
        corrected={**b,'corrections':[{'row':1,'supplier':'核对后供应商','evidence':'合成规格文件第1行'}]};pre=self.d.preview(corrected)
        for change in ({'supplier':'另一供应商'},{'evidence':'改为另一份依据'}):
            altered={**corrected,'corrections':[{**corrected['corrections'][0],**change}]}
            self.assertNotEqual(self.d.preview(altered)['token'],pre['token'])
            with self.assertRaises(Problem):self.d.apply({**altered,'preview_token':pre['token'],'confirmed':True,'request_id':ident()})
        changed=self.apply(corrected);self.assertEqual(len(changed['conflicts']),1);self.assertEqual(self.app.store.get(imported['created'][0]),before)
        snapshot=self.candidates()[0]['snapshots'][-1];self.assertEqual(snapshot['capture']['original_raw']['supplier'],'');self.assertEqual(snapshot['capture']['correction']['fields']['supplier']['after'],'核对后供应商')
        self.assertEqual(snapshot['raw']['source_price'],2.5);self.assertEqual(snapshot['normalized']['stock'],0)

    def test_conflicting_same_identity_within_package_rejected_and_exact_duplicates_skipped(self):
        b=self.body();b['package']['items'].append(copy.deepcopy(b['package']['items'][0]));p=self.d.preview(b);self.assertEqual(p['counts']['duplicate'],1)
        result=self.apply(b);self.assertEqual(len(result['created']),1)
        b=self.body();b['package']['items'].append({**b['package']['items'][0],'source_price':99})
        with self.assertRaises(Problem):self.d.preview(b)

if __name__=='__main__':unittest.main()
