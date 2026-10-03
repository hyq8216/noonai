import json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Problem
from server import App
from source_import import SourceImport

class SourceImportTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.app=App(Path(self.tmp.name));self.store=self.app.store;self.imp=SourceImport(self.store)
 def tearDown(self):self.app.models.codex.close();self.app.visuals.close();self.app.visual_checks.close();self.app.media.close();self.app.executor.shutdown();self.tmp.cleanup()
 def body(self,**extra):return {'csv':'商品名称,1688链接,规格SKU,采购价（元）,库存数量\n红色盒,https://detail.1688.com/offer/123.html?track=a,RED,12.5,20\n蓝色盒,https://detail.1688.com/offer/123.html,BLUE,13,10\n坏记录,https://detail.1688.com/offer/9.html,X,价格区间,-1\n',**extra}
 def prepared(self,b):return {**b,'preview_token':self.imp.preview(b)['token'],'confirmed':True}
 def test_chinese_columns_partial_import_and_no_trusted_flags(self):
  b=self.body();p=self.imp.preview(b);self.assertEqual((p['ready'],p['blocked']),(2,1));self.assertEqual(self.store.list(),[]);out=self.imp.apply(self.prepared(b));self.assertEqual(len(out['created']),2);self.assertEqual(out['skipped'][0]['row'],3)
  for product in self.store.list():self.assertFalse(product['reviewed']);self.assertFalse(product['content_verified']);self.assertEqual(product['images'],[])
  self.assertEqual({p['source_sku'] for p in self.store.list()},{'RED','BLUE'})
 def test_unknown_headers_require_mapping_and_ambiguous_price_not_guessed(self):
  b={'csv':'名称,价格\n测试,18\n'};p=self.imp.preview(b);self.assertEqual(p['ready'],0);self.assertEqual(p['mapping'],{})
  b['mapping']={'title_zh':0,'cost_cny':1};p=self.imp.preview(b);self.assertEqual(p['ready'],1);self.assertEqual(p['rows'][0]['values']['cost_cny'],18)
 def test_duplicate_headers_not_automatically_chosen(self):
  b={'csv':'商品名称,商品名称\n甲,乙\n'};self.assertNotIn('title_zh',self.imp.preview(b)['mapping']);b['mapping']={'title_zh':1};self.assertEqual(self.imp.preview(b)['rows'][0]['title'],'乙')
 def test_conflicting_variants_hold_every_row_in_group(self):
  b={'products':[{'title_zh':'红','source_url':'https://detail.1688.com/offer/1.html','cost_cny':1},{'title_zh':'蓝','source_url':'https://detail.1688.com/offer/1.html?x=y','cost_cny':2}]};p=self.imp.preview(b);self.assertEqual((p['ready'],p['blocked']),(0,2))
 def test_identical_rows_and_existing_catalog_not_overwritten(self):
  row={'title_zh':'Box','source_url':'https://detail.1688.com/offer/1.html','source_sku':'A','stock':3};b={'products':[row,row]};pre=self.imp.preview(b);self.assertEqual((pre['ready'],pre['duplicates']),(1,1));out=self.imp.apply(self.prepared(b));pid=out['created'][0]
  b={'products':[{**row,'stock':99}]};pre=self.imp.preview(b);self.assertEqual(pre['ready'],0);self.assertEqual(pre['duplicates'],1);self.assertEqual(self.store.get(pid)['stock'],3)
 def test_identical_file_without_source_url_is_replay_safe(self):
  b=self.prepared({'products':[{'title_zh':'No source'}]})
  with ThreadPoolExecutor(max_workers=2) as pool:out=list(pool.map(self.imp.apply,[b,b]))
  self.assertEqual(len(self.store.list()),1);self.assertEqual(out[0]['created'],out[1]['created']);self.assertEqual({r['replayed'] for r in out},{True,False});pre=self.imp.preview({'products':[{'title_zh':'No source'}]});self.assertTrue(pre['already_imported']);self.assertEqual(pre['ready'],0)
 def test_stale_catalog_or_mapping_requires_new_preview(self):
  b=self.prepared(self.body());self.store.import_rows([{'title_zh':'Already here','source_url':'https://detail.1688.com/offer/123.html','source_sku':'RED'}])
  with self.assertRaises(Problem):self.imp.apply(b)
  pre=self.imp.preview(self.body());self.assertEqual((pre['ready'],pre['duplicates']),(1,1))
  b=self.prepared(self.body());b['mapping']={'title_zh':0}
  with self.assertRaises(Problem):self.imp.apply(b)
 def test_apply_failure_rolls_back_all_rows_and_receipt(self):
  b=self.prepared(self.body());original=self.store.event;calls=[]
  def fail(*args):
   calls.append(1)
   if len(calls)==2:raise Problem('Injected write error')
   return original(*args)
  with patch.object(self.store,'event',side_effect=fail):
   with self.assertRaises(Problem):self.imp.apply(b)
  self.assertEqual(self.store.list(),[]);self.assertEqual(len(self.imp.apply(b)['created']),2)
 def test_malformed_rows_quoted_commas_and_multiline(self):
  b={'csv':'商品名称,规格说明\n"盒,红色","两行\n规格"\n列数错,说明,多余\n'};p=self.imp.preview(b);self.assertEqual((p['ready'],p['blocked']),(1,1));self.assertEqual(p['rows'][0]['values']['facts'],'两行\n规格')
  with self.assertRaises(Problem):self.imp.preview({'csv':'名称\n"unclosed'})
 def test_mapping_limits_confirmation_and_invalid_values(self):
  for mapping in ({'title_zh':True},{'title_zh':99},{'evil':0},{'title_zh':0,'facts':0}):
   with self.assertRaises(Problem):self.imp.preview(self.body(mapping=mapping))
  with self.assertRaises(Problem):self.imp.apply({**self.prepared(self.body()),'confirmed':False})
  for value in ('1-3','¥12','NaN',True,-1):
   p=self.imp.preview({'products':[{'title_zh':'Bad price','cost_cny':value}]});self.assertEqual(p['blocked'],1)
  with self.assertRaises(Problem):self.imp.preview({'products':[{'title_zh':'X'}]*501})
 def test_json_approval_and_platform_fields_ignored(self):
  b={'products':[{'title_zh':'Safe','content_verified':True,'images_verified':True,'platform':{'approved':True},'images':[{'id':'bad'}]}]};out=self.imp.apply(self.prepared(b));p=self.store.get(out['created'][0]);self.assertFalse(p['content_verified']);self.assertFalse(p['images_verified']);self.assertIsNone(p['platform']);self.assertEqual(p['images'],[])
 def test_1688_suffix_lookalike_query_is_not_discarded(self):
  b={'products':[{'title_zh':'A','source_url':'https://not1688.com/p?variant=red'},{'title_zh':'B','source_url':'https://not1688.com/p?variant=blue'}]};self.assertEqual(self.imp.preview(b)['ready'],2);self.assertEqual(len(self.imp.apply(self.prepared(b))['created']),2)
 def test_malformed_url_is_isolated_to_one_row(self):
  b={'products':[{'title_zh':'Good'},{'title_zh':'Bad URL','source_url':'https://[broken'}]};pre=self.imp.preview(b);self.assertEqual((pre['ready'],pre['blocked']),(1,1));self.assertEqual(len(self.imp.apply(self.prepared(b))['created']),1)
 def test_exact_import_receipt_survives_backup_restore(self):
  b=self.prepared({'products':[{'title_zh':'No URL stable replay'}]});first=self.imp.apply(b);archive=self.app.recovery.create();pre=self.app.recovery.inspect(self.app.recovery.archive_path(archive['id']));self.app.recovery.schedule({**pre,'confirmed':True});self.app.recovery.apply_pending();again=self.imp.apply(b);self.assertTrue(again['replayed']);self.assertEqual(first['created'],again['created']);self.assertEqual(len(self.store.list()),1)
if __name__=='__main__':unittest.main()
