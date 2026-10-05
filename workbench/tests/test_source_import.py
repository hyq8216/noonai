import json,random,sys,tempfile,unittest
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
 def test_ambiguous_existing_identity_stays_blocked_for_every_matching_row(self):
  identity={'source_url':'https://supplier.example/ambiguous','source_sku':'SAME','supplier':'Factory','facts':'red plastic box'}
  existing=self.store.import_rows([{'title_zh':'Existing A',**identity}])['created'][0]
  with self.store.connect() as c:
   c.execute('INSERT INTO products(id,source_key,data,revision,approved_revision,created_at,updated_at) SELECT ?,?,data,revision,approved_revision,created_at,updated_at FROM products WHERE id=?',('legacy-copy',identity['source_url']+'|'+identity['source_sku'],existing))
  preview=self.imp.preview({'products':[{'title_zh':'Candidate',**identity},{'title_zh':'Candidate',**identity}]})
  self.assertEqual([row['status'] for row in preview['rows']],['blocked','blocked'])
  self.assertTrue(all('多份商品档案' in row['reason'] for row in preview['rows']))

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
 def test_unencodable_unicode_is_reported_as_input_error_for_csv_and_json(self):
  with self.assertRaisesRegex(Problem,'CSV含有无效Unicode'):
   self.imp.preview({'csv':'商品名称\n\ud800'})
  with self.assertRaisesRegex(Problem,'JSON商品资料含有无效Unicode'):
   self.imp.preview({'products':[{'title_zh':'\ud800'}]})
 def test_common_physical_spec_columns_are_collected_but_commercial_fields_are_not(self):
  b={'csv':'商品名称,货源链接,规格货号,供应商,规格事实,颜色,材质,采购成本（人民币元）,库存数量,起订量,营销词\n红色餐盒,https://supplier.example/box,R-1,工厂,食品接触级,深红,铝合金,12.5,0,50,热销包邮\n'}
  pre=self.imp.preview(b)
  self.assertEqual(pre['fact_columns'],[5,6])
  self.assertEqual((pre['ready'],pre['blocked']),(1,0))
  self.assertEqual(pre['rows'][0]['values']['facts'],'食品接触级\n颜色：深红\n材质：铝合金')
  self.assertEqual(pre['rows'][0]['values']['cost_cny'],12.5)
  self.assertEqual(pre['rows'][0]['values']['stock'],0)
  self.assertNotIn('起订量',pre['rows'][0]['values']['facts'])
  self.assertNotIn('营销词',pre['rows'][0]['values']['facts'])
  out=self.imp.apply({**b,'preview_token':pre['token'],'confirmed':True})
  product=self.store.get(out['created'][0])
  self.assertEqual(product['facts'],pre['rows'][0]['values']['facts'])
  replay=self.imp.apply({**b,'preview_token':pre['token'],'confirmed':True})
  self.assertTrue(replay['replayed'])
  self.assertEqual(len(self.store.list()),1)
 def test_manual_spec_columns_preserve_header_and_skip_blank_values(self):
  b={'csv':'商品名称,货源链接,供应商,材质,适配设备,宣传语\n支架,https://supplier.example/stand,工厂,铝合金,平板电脑,\n第二件,https://supplier.example/stand-2,工厂,钢材,,促销款\n','fact_columns':[3,4]}
  pre=self.imp.preview(b)
  self.assertEqual(pre['fact_columns'],[3,4])
  self.assertEqual(pre['rows'][0]['values']['facts'],'材质：铝合金\n适配设备：平板电脑')
  self.assertEqual(pre['rows'][1]['values']['facts'],'材质：钢材')
  with self.assertRaisesRegex(Problem,'规格列'):
   self.imp.preview({**b,'mapping':{'title_zh':0,'supplier':3},'fact_columns':[3]})
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
 def test_deterministic_malformed_csv_and_json_mutations_never_escape_or_write(self):
  rng=random.Random(20261005)
  values=[None,'',0,-1,1.25,True,False,[],{},['x'],{'x':1},'NaN','1-3','¥12','https://[','\ud800','\x00','  商品\n名称  ']
  json_unexpected=[]
  for n in range(256):
   row={'title_zh':f'Fuzz {n}'}
   for field in ('source_url','source_sku','supplier','facts','cost_cny','stock','mode','attribute_values'):
    if rng.random()<.65:row[field]=rng.choice(values)
   try:
    preview=self.imp.preview({'products':[row]})
    self.assertEqual(preview['ready']+preview['blocked']+preview['duplicates'],1)
   except Problem:
    pass
   except Exception as exc:
    json_unexpected.append((n,type(exc).__name__,str(exc)));break
  self.assertEqual(json_unexpected,[])

  csv_rng=random.Random(991205);chars='商品名称,\"\n\r\\;|\x00\u202e😀';csv_unexpected=[]
  for n in range(384):
   text='商品名称,规格事实\n'+''.join(csv_rng.choice(chars) for _ in range(csv_rng.randrange(0,300)))
   try:
    preview=self.imp.preview({'csv':text})
    self.assertEqual(preview['ready']+preview['blocked']+preview['duplicates'],len(preview['rows']))
   except Problem:
    pass
   except Exception as exc:
    csv_unexpected.append((n,type(exc).__name__,str(exc)));break
  self.assertEqual(csv_unexpected,[])
  self.assertEqual(self.store.list(),[],'preview mutation checks must never import products')
if __name__=='__main__':unittest.main()
