import json,sys,tempfile,unittest,uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Problem
from server import App
from category_batch import CategoryBatch
from test_category_rules import SPEC

class CategoryBatchTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.app=App(self.tmp.name);self.store=self.app.store;self.batch=CategoryBatch(self.store)
  p=self.product();p=self.app.category_rules(p['id'],{'revision':p['revision'],'source':'file','contract':SPEC})
  self.source=self.store.update(p['id'],{'attribute_values':{'material':{'value':'Plastic'},'length':{'value':'20','unit':'centimeter'}}},p['revision'])
 def tearDown(self):self.app.executor.shutdown();self.app.visuals.close();self.tmp.cleanup()
 def product(self,**extra):return self.store.get(self.store.import_rows([{'title_zh':'QA '+uuid.uuid4().hex[:5],'category':'test-category',**extra}])['created'][0])
 def request(self,products,codes=None,**extra):
  b={'source_id':self.source['id'],'product_ids':[p['id'] for p in products],'attribute_codes':codes or [],'copy_rules':True,'request_id':uuid.uuid4().hex,**extra};b['preview_token']=self.batch.preview(b)['token'];return b
 def apply(self,b):return self.app.ops.transact('category_batch',b)
 def test_only_selected_values_copied_same_category_and_preserved_existing(self):
  a=self.product();b=self.product(attribute_values={'material':{'value':'Steel'}});other=self.product(category='other');body=self.request([a,b,other],['material']);out=self.apply(body)
  self.assertEqual(len(out['updated']),2);self.assertEqual(out['skipped'],[other['id']]);self.assertEqual(self.store.get(a['id'])['attribute_values'],{'material':{'value':'Plastic'}});self.assertEqual(self.store.get(b['id'])['attribute_values']['material']['value'],'Steel');self.assertNotIn('length',self.store.get(a['id'])['attribute_values']);self.assertFalse(self.store.get(a['id'])['category_verified'])
 def test_partial_metric_never_mixes_number_and_unit(self):
  p=self.product(attribute_values={'length':{'value':'30','unit':''}});out=self.apply(self.request([p],['length']));self.assertEqual(self.store.get(p['id'])['attribute_values']['length'],{'value':'30','unit':''})
 def test_preview_has_no_writes_and_rule_only_leaves_attributes_empty(self):
  p=self.product();body=self.request([p]);self.assertEqual(self.store.get(p['id'])['revision'],1);out=self.apply(body);saved=self.store.get(p['id']);self.assertEqual(saved['attribute_values'],{});self.assertEqual(saved['category_contract']['copied_from']['id'],self.source['id']);self.assertFalse(saved['reviewed'])
 def test_duplicate_click_and_restart_replays_result_without_second_revision(self):
  p=self.product();body=self.request([p],['material'])
  with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(self.apply,[body,body]))
  self.assertEqual(results[0],results[1]);self.assertEqual(self.store.get(p['id'])['revision'],2)
  from operations import Operations
  self.assertEqual(Operations(self.store).transact('category_batch',body),results[0])
  with self.assertRaises(Problem):self.apply({**body,'attribute_codes':['length']})
 def test_stale_target_and_source_refuse_all_changes(self):
  a=self.product();b=self.product();body=self.request([a,b],['material']);self.store.update(b['id'],{'note':'changed'},b['revision'])
  with self.assertRaises(Problem):self.apply(body)
  self.assertEqual(self.store.get(a['id'])['revision'],1)
  body=self.request([a,b]);self.store.update(self.source['id'],{'note':'changed'},self.source['revision'])
  with self.assertRaises(Problem):self.apply(body)
  self.assertEqual(self.store.get(a['id'])['revision'],1)
 def test_active_tasks_excluded_and_new_task_invalidates_preview(self):
  a=self.product();b=self.product();self.store.add_job(a['id'],'translate',a['revision']);body=self.request([a,b]);self.assertEqual(self.batch.preview(body)['rows'][0]['status'],'blocked');self.store.add_job(b['id'],'translate',b['revision'])
  with self.assertRaises(Problem):self.apply(body)
 def test_transaction_failure_rolls_back_products_history_and_request(self):
  a=self.product();b=self.product();body=self.request([a,b],['material']);original=self.store.update;counter=[0]
  def fail(*args,**kwargs):
   counter[0]+=1
   if counter[0]==2:raise Problem('injected failure')
   return original(*args,**kwargs)
  with patch.object(self.store,'update',side_effect=fail):
   with self.assertRaises(Problem):self.apply(body)
  self.assertEqual(self.store.get(a['id'])['revision'],1);self.assertEqual(self.store.get(b['id'])['revision'],1)
  with self.store.connect() as c:self.assertIsNone(c.execute('SELECT * FROM ops_requests WHERE key=?',(body['request_id'],)).fetchone());self.assertEqual(c.execute('SELECT count(*) FROM revisions WHERE product_id=?',(a['id'],)).fetchone()[0],1)
  self.assertEqual(len(self.apply(body)['updated']),2)
 def test_invalid_source_value_and_builtin_copy_refused(self):
  p=self.product()
  for code in ('product_title','missing','length_unit'):
   with self.assertRaises(Problem):self.request([p],[code])
  self.store.update(self.source['id'],{'attribute_values':{'material':{'value':'invalid'}}},self.source['revision'])
  with self.assertRaises(Problem):self.request([p],['material'])
 def test_noop_keeps_approval_revision_and_changed_target_rule_checked(self):
  p=self.product();self.apply(self.request([p],['material']));saved=self.store.get(p['id']);body=self.request([saved],['material']);self.assertEqual(self.batch.preview(body)['rows'][0]['status'],'unchanged')
  with self.assertRaises(Problem):self.apply(body)
  self.assertEqual(self.store.get(p['id'])['revision'],saved['revision'])
 def test_target_rule_rejects_source_option_without_rule_reuse(self):
  p=self.product();spec=json.loads(json.dumps(SPEC));next(a for a in spec['attributes'] if a['attribute_code']=='material')['attribute_options']=['Steel'];p=self.app.category_rules(p['id'],{'revision':p['revision'],'source':'file','contract':spec})
  body=self.request([p],['material'],copy_rules=False);self.assertEqual(self.batch.preview(body)['rows'][0]['status'],'blocked')
 def test_500_products_applied_with_durable_versions(self):
  ids=self.store.import_rows([{'title_zh':'QA '+str(i),'category':'test-category'} for i in range(500)])['created'];body=self.request([{'id':x} for x in ids],['material']);out=self.apply(body);self.assertEqual(len(out['updated']),500);self.assertTrue(all(r['revision']==2 for r in out['updated']))
if __name__=='__main__':unittest.main()
