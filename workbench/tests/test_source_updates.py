import json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import App
from core import Problem
from source_updates import SourceUpdates

class SupplyUpdatesTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.app=App(Path(self.tmp.name));self.store=self.app.store;self.imp=SourceUpdates(self.store)
  self.pid=self.store.import_rows([{'title_zh':'红盒','source_url':'https://detail.1688.com/offer/88.html','source_sku':'R','cost_cny':10,'stock':5,'title_en':'Red Box','supply_checked_at':'2026-10-02T01:00:00Z'}])['created'][0];self.p=self.store.get(self.pid)
 def tearDown(self):
  self.app.models.codex.close();self.app.visuals.close();self.app.visual_checks.close();self.app.media.close();self.app.executor.shutdown();self.tmp.cleanup()
 def body(self,**extra):return {'products':[{'partner_sku':self.p['partner_sku'],'cost_cny':12,'stock':4,**extra}]}
 def prepared(self,b):return {**b,'preview_token':self.imp.preview(b)['token'],'confirmed':True}
 def test_updates_clear_supply_and_approval_keep_content_images_platform_and_source(self):
  with self.store.connect() as c:
   data=json.loads(c.execute('SELECT data FROM products WHERE id=?',(self.pid,)).fetchone()[0]);data.update(content_verified=True,images_verified=True,platform={'state':'pending'});c.execute('UPDATE products SET data=?,approved_revision=revision WHERE id=?',(json.dumps(data),self.pid))
  before=self.store.get(self.pid);b=self.body();pre=self.imp.preview(b);self.assertEqual(pre['ready'],1);self.assertEqual(self.store.get(self.pid)['revision'],1)
  self.imp.apply(self.prepared(b));p=self.store.get(self.pid);self.assertEqual((p['stock'],p['cost_cny'],p['supply_checked_at']),(4,12,''));self.assertFalse(p['reviewed']);self.assertTrue(p['content_verified']);self.assertTrue(p['images_verified']);self.assertEqual(p['title_en'],'Red Box');self.assertEqual(p['platform'],before['platform']);self.assertEqual(p['source_snapshot'],before['source_snapshot'])
 def test_exact_source_match_with_tracking_parameters(self):
  b={'csv':'货源链接,规格货号,库存,采购价（元）\nhttps://detail.1688.com/offer/88.html?track=1,R,8,11\n'};p=self.imp.preview(b);self.assertEqual(p['ready'],1);self.imp.apply(self.prepared(b));self.assertEqual(self.store.get(self.pid)['stock'],8)
 def test_blank_preserves_zero_clears_and_unknown_no_create(self):
  b=self.body(cost_cny='',stock=0);p=self.imp.preview(b);self.assertEqual(p['rows'][0]['values'],{'stock':0});self.imp.apply(self.prepared(b));p=self.store.get(self.pid);self.assertEqual((p['stock'],p['cost_cny']),(0,10));self.assertIn('可供数量不足或未确认',p['issues'])
  b=self.body(partner_sku='unknown');self.assertEqual(self.imp.preview(b)['blocked'],1);self.assertEqual(len(self.store.list()),1)
 def test_sku_and_source_disagreement_is_blocked(self):
  for fields in ({'source_sku':'B'},{'source_url':'https://detail.1688.com/offer/99.html'}):self.assertEqual(self.imp.preview(self.body(**fields))['blocked'],1)
 def test_conflicts_block_all_identical_duplicates_once(self):
  row=self.body()['products'][0];p=self.imp.preview({'products':[row,{**row,'stock':9}]});self.assertEqual((p['ready'],p['blocked']),(0,2))
  b={'products':[row,row]};pre=self.imp.preview(b);self.assertEqual((pre['ready'],pre['duplicates']),(1,1));self.imp.apply(self.prepared(b));self.assertEqual(self.store.get(self.pid)['revision'],2)
 def test_unchanged_does_not_clear_confirmations(self):
  b=self.body(cost_cny=10,stock=5);pre=self.imp.preview(b);self.assertEqual((pre['ready'],pre['unchanged']),(0,1));self.assertEqual(self.store.get(self.pid)['supply_checked_at'],self.p['supply_checked_at'])
 def test_stale_preview_after_product_change(self):
  b=self.prepared(self.body());self.store.update(self.pid,{'note':'Changed'},1)
  with self.assertRaises(Problem) as e:self.imp.apply(b)
  self.assertEqual(e.exception.status,409);self.assertEqual(self.store.get(self.pid)['cost_cny'],10)
 def test_submit_in_flight_blocks_preview_and_apply(self):
  b=self.prepared(self.body())
  with self.store.connect() as c:c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',('job',self.pid,'submit',1,'queued','pending',None,'2026','2026'))
  self.assertEqual(self.imp.preview(self.body())['blocked'],1)
  with self.assertRaises(Problem):self.imp.apply(b)
 def test_parallel_replay_once_and_file_can_be_used_later(self):
  b=self.prepared(self.body())
  with ThreadPoolExecutor(max_workers=2) as pool:out=list(pool.map(self.imp.apply,[b,b]))
  self.assertEqual({r['replayed'] for r in out},{True,False});self.assertEqual(self.store.get(self.pid)['revision'],2)
  self.store.update(self.pid,{'stock':1},2);p=self.imp.preview(self.body());self.assertEqual(p['ready'],1);self.imp.apply(self.prepared(self.body()));self.assertEqual(self.store.get(self.pid)['revision'],4)
 def test_transaction_rollback_including_audit(self):
  second=self.store.import_rows([{'title_zh':'蓝盒','stock':9}])['created'][0];b=self.body();b['products'].append({'partner_sku':self.store.get(second)['partner_sku'],'stock':4});a=self.prepared(b);orig=self.store.update
  def fail(pid,*args,**kwargs):
   if pid==second:raise Problem('Injected failure')
   return orig(pid,*args,**kwargs)
  with patch.object(self.store,'update',side_effect=fail):
   with self.assertRaises(Problem):self.imp.apply(a)
  self.assertEqual(self.store.get(self.pid)['revision'],1);self.assertEqual(len(self.imp.apply(a)['updated']),2)
 def test_bad_rows_isolated_and_untrusted_fields_ignored(self):
  b=self.body(content_verified=True,title_en='bad',transfer_usd=99);b['products'].append({'partner_sku':self.p['partner_sku'],'stock':-1});p=self.imp.preview(b);self.assertEqual((p['ready'],p['blocked']),(1,1));self.imp.apply(self.prepared(b));p=self.store.get(self.pid);self.assertEqual(p['title_en'],'Red Box');self.assertIsNone(p['transfer_usd'])
 def test_manual_mapping_confirmation_and_missing_identifiers(self):
  b={'csv':'货号别名,数量别名\n'+self.p['partner_sku']+',3\n'};self.assertEqual(self.imp.preview(b)['blocked'],1);b['mapping']={'partner_sku':0,'stock':1};self.assertEqual(self.imp.preview(b)['ready'],1)
  with self.assertRaises(Problem):self.imp.apply({**self.prepared(b),'confirmed':False})
  self.assertEqual(self.imp.preview({'products':[{'stock':3}]})['blocked'],1)
if __name__=='__main__':unittest.main()
