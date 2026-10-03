import json,sys,tempfile,unittest,uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Problem,now
from server import App
from visuals import source_signature

class VisualBatchTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.app=App(self.root/'data');self.store=self.app.store;self.batch=self.app.visual_batch
  self.kick=patch.object(self.app.visuals,'kick');self.mockkick=self.kick.start();self.image=self.root/'reference.png';Image.new('RGB',(800,800),'gray').save(self.image)
 def tearDown(self):self.kick.stop();self.app.visuals.close();self.app.executor.shutdown();self.tmp.cleanup()
 def product(self,photo=True,**extra):
  p=self.store.get(self.store.import_rows([{'title_zh':'QA '+uuid.uuid4().hex[:5],'facts':'One gray plastic box',**extra}])['created'][0])
  if photo:self.app.media.ingest(self.image,'QA original','Synthetic QA reference',p['id'])
  return p
 def body(self,products,**extra):return {'request_id':uuid.uuid4().hex,'product_ids':[p['id'] for p in products],'shots':['hero'],'model':'gpt-6-sol','aspect':'square','style':'Soft studio lighting','brief':'','confirmed':True,**extra}
 def prepared(self,b):return {**b,'preview_token':self.batch.preview(b)['token']}
 def test_only_linked_originals_and_missing_facts_isolated(self):
  good=self.product();bad=self.product(photo=False);facts=self.product(facts='');self.app.media.ingest(self.image,'Unlinked photo','Synthetic QA')
  b=self.body([good,bad,facts]);preview=self.batch.preview(b);self.assertEqual([r['status'] for r in preview['rows']],['ready','blocked','blocked']);out=self.batch.apply({**b,'preview_token':preview['token']});self.assertEqual(out['product_ids'],[good['id']]);self.assertEqual(len(out['job_ids']),1)
  recipe=self.app.visuals.get(out['job_ids'][0])['recipe'];self.assertEqual(recipe['product_id'],good['id']);self.assertEqual(len(recipe['references']),1);self.assertEqual(recipe['identity']['locked_features'],good['facts'])
 def test_preview_readonly_and_requires_explicit_confirmation(self):
  p=self.product();b=self.prepared(self.body([p]));self.assertEqual(self.app.visuals.state()['jobs'],[]);self.mockkick.assert_not_called()
  with self.assertRaises(Problem):self.batch.apply({**b,'confirmed':False})
 def test_concurrent_replay_one_queue_and_response_survives_source_edit(self):
  p=self.product();b=self.prepared(self.body([p]))
  with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(self.batch.apply,[b,b]))
  self.assertEqual(results[0],results[1]);self.assertEqual(len(self.app.visuals.state()['jobs']),1)
  self.store.update(p['id'],{'facts':'changed'},p['revision']);self.assertEqual(self.batch.apply(b),results[0])
  with self.assertRaises(Problem):self.batch.apply({**b,'style':'changed'})
 def test_stale_product_photo_and_capacity_rechecked(self):
  p=self.product();b=self.prepared(self.body([p]));self.store.update(p['id'],{'facts':'changed'},p['revision'])
  with self.assertRaises(Problem):self.batch.apply(b)
  b=self.prepared(self.body([self.store.get(p['id'])]));a=self.app.media.state()['assets'][0];(self.app.media.root/a['file']).write_bytes(b'changed')
  with self.assertRaises(Problem):self.batch.apply(b)
  self.assertEqual(self.app.visuals.state()['jobs'],[])
 def test_existing_candidate_kept_and_missing_shot_only_queued(self):
  p=self.product();out=self.batch.apply(self.prepared(self.body([p])));self.app.visuals.update(out['job_ids'][0],'candidate','Synthetic candidate state')
  b=self.body([p],shots=['hero','scene']);pre=self.batch.preview(b);self.assertEqual(pre['rows'][0]['kept'][0]['shot'],'hero');self.assertEqual(pre['plans'][p['id']]['shots'],['scene']);out=self.batch.apply({**b,'preview_token':pre['token']});self.assertEqual(len(out['job_ids']),1)
 def test_uncertain_task_never_auto_repeated(self):
  p=self.product();out=self.batch.apply(self.prepared(self.body([p])));self.app.visuals.update(out['job_ids'][0],'uncertain','unknown',dispatched_at=now());b=self.body([p]);self.assertEqual(self.batch.preview(b)['rows'][0]['status'],'blocked')
 def test_output_rejected_blocks_bulk_until_explicitly_discarded(self):
  p=self.product();out=self.batch.apply(self.prepared(self.body([p])));jid=out['job_ids'][0];self.app.visuals.update(jid,'output_rejected','Wrong output dimensions',dispatched_at=now())
  self.assertEqual(self.batch.preview(self.body([p]))['rows'][0]['status'],'blocked');self.app.visuals.control({'action':'discard-output','id':jid})
  self.assertEqual(self.batch.preview(self.body([p]))['rows'][0]['status'],'ready')
 def test_more_than_six_references_require_manual_choice(self):
  p=self.product()
  for i in range(6):self.app.media.ingest(self.image,'Extra '+str(i),'Synthetic QA',p['id'])
  self.assertEqual(self.batch.preview(self.body([p]))['rows'][0]['status'],'blocked')
 def test_transaction_failure_leaves_no_jobs_or_ledger(self):
  a=self.product();b=self.product();body=self.prepared(self.body([a,b]));original=self.app.visuals.submit;count=[0]
  def fail(*args,**kwargs):
   count[0]+=1
   if count[0]==2:raise Problem('injected')
   return original(*args,**kwargs)
  with patch.object(self.app.visuals,'submit',side_effect=fail):
   with self.assertRaises(Problem):self.batch.apply(body)
  self.assertEqual(self.app.visuals.state()['jobs'],[]);self.mockkick.assert_not_called();self.assertEqual(len(self.batch.apply(body)['job_ids']),2)
 def test_attributes_in_identity_and_changes_invalidate_candidate(self):
  p=self.product(attribute_values={'material':{'value':'Plastic'}});out=self.batch.apply(self.prepared(self.body([p])));r=self.app.visuals.get(out['job_ids'][0])['recipe'];self.assertEqual(r['identity']['category_attributes'],p['attribute_values'])
  changed=self.store.update(p['id'],{'attribute_values':{'material':{'value':'Steel'}}},p['revision']);self.assertNotEqual(source_signature(changed),r['source_signature'])
  with self.assertRaises(Problem):self.app.visuals.current(r)
 def test_different_shooting_settings_do_not_reuse_old_candidate(self):
  p=self.product();out=self.batch.apply(self.prepared(self.body([p])));self.app.visuals.update(out['job_ids'][0],'candidate','Synthetic candidate state')
  preview=self.batch.preview(self.body([p],aspect='portrait'));self.assertEqual(preview['rows'][0]['kept'],[]);self.assertEqual(preview['new_tasks'],1)
 def test_500_products_2000_jobs_keep_daily_dispatch_limit_separate(self):
  first=self.product();asset=self.app.media.state()['assets'][0];products=[first]
  ids=self.store.import_rows([{'title_zh':'QA '+str(i),'facts':'One gray box'} for i in range(499)])['created']
  with self.store.connect() as c:
   for pid in ids:
    a={**asset,'id':uuid.uuid4().hex,'product_id':pid};c.execute('INSERT INTO media_assets VALUES(?,?,?)',(a['id'],json.dumps(a),now()));products.append({'id':pid})
  body=self.body(products,shots=['hero','scene','model','detail']);preview=self.batch.preview(body);self.assertEqual(preview['new_tasks'],2000);out=self.batch.apply({**body,'preview_token':preview['token']});self.assertEqual(len(out['job_ids']),2000);state=self.app.visuals.state();self.assertEqual(state['counts']['queued'],2000);self.assertEqual(state['daily_limit'],10);self.assertEqual(state['used_today'],0)
  extra=self.product();self.assertEqual(self.batch.preview(self.body([extra]))['rows'][0]['status'],'capacity')
if __name__=='__main__':unittest.main()
