import sys,tempfile,unittest,json
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import App
from source_import import SourceImport
from core import Problem
CONTENT={'title_en':'Red box','description_en':'Red plastic box','title_ar':'صندوق أحمر','description_ar':'صندوق بلاستيكي أحمر'}
class IntakeAutomationTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.app=App(Path(self.tmp.name));self.imp=SourceImport(self.app.store,self.app.automation);self.ready=patch.object(self.app.models,'ready',return_value=True);self.ready.start()
 def tearDown(self):
  self.ready.stop();self.app.models.codex.close();self.app.visuals.close();self.app.visual_checks.close();self.app.media.close();self.app.executor.shutdown();self.tmp.cleanup()
 def body(self):return {'products':[{'title_zh':'红盒','source_url':'https://detail.1688.com/offer/1.html','source_sku':'R','supplier':'工厂','facts':'红色塑料盒'}],'processing':{'translate':True,'review':True}}
 def prepared(self,b):return {**b,'preview_token':self.imp.preview(b)['token'],'confirmed':True}
 def test_preview_no_write_then_atomic_queue(self):
  b=self.body();p=self.imp.preview(b);self.assertEqual(p['processing']['max_calls'],2);self.assertEqual(self.app.store.list(),[]);self.assertEqual(self.app.automation.state()['runs'],[])
  r=self.imp.apply(self.prepared(b));self.assertEqual(r['processing']['queued'],1);run=self.app.automation.state()['runs'][0];self.assertEqual(run['id'],r['processing']['run_id']);self.assertTrue(run['plan']['missing_only']);self.assertFalse(run['plan']['submit']);self.assertEqual(run['plan']['image_template'],'')
 def test_real_workflow_executes_once_and_keeps_approval_gate(self):
  b=self.body();r=self.imp.apply(self.prepared(b));self.app.automation.tick()
  with patch.object(self.app.models,'translate',return_value={'content':CONTENT,'call_id':'fixture','model':'fixture','warnings':[]}) as tr,patch.object(self.app.models,'review',return_value={'call_id':'review','model':'fixture','passed':True,'warnings':[]}) as rev:
   for _ in range(4):self.app.automation.tick()
   self.assertEqual(tr.call_count,1);self.assertEqual(rev.call_count,1)
  p=self.app.store.get(r['created'][0]);self.assertEqual(p['title_en'],'Red box');self.assertFalse(p['reviewed']);self.assertFalse(p['content_verified']);self.assertNotEqual(self.app.automation.state()['items'][0]['status'],'done')
 def test_missing_facts_are_imported_but_not_queued(self):
  b=self.body();b['products'].append({'title_zh':'缺资料'});p=self.imp.preview(b);self.assertEqual(p['processing']['waiting_rows'],[2]);r=self.imp.apply(self.prepared(b));self.assertEqual(len(r['created']),2);self.assertEqual(r['processing']['queued'],1);self.assertEqual(len(r['processing']['waiting']),1)
 def test_no_models_or_no_options_do_not_dispatch(self):
  b=self.body()
  with patch.object(self.app.models,'ready',return_value=False):
   p=self.imp.preview(b);self.assertEqual(p['processing']['max_calls'],0);r=self.imp.apply(self.prepared(b));self.assertIsNone(r['processing']['run_id'])
  self.assertEqual(self.app.automation.state()['runs'],[])
 def test_queue_failure_rolls_back_import_and_receipt(self):
  b=self.prepared(self.body())
  with patch.object(self.app.automation,'create',side_effect=Problem('Injected queue failure')):
   with self.assertRaises(Problem):self.imp.apply(b)
  self.assertEqual(self.app.store.list(),[]);self.assertFalse(self.imp.preview(self.body())['already_imported']);self.assertEqual(self.imp.apply(b)['processing']['queued'],1)
 def test_parallel_replay_no_duplicate_workflow(self):
  b=self.prepared(self.body())
  with ThreadPoolExecutor(max_workers=2) as pool:r=list(pool.map(self.imp.apply,[b,b]))
  self.assertEqual(len(self.app.automation.state()['runs']),1);self.assertEqual(r[0]['processing']['run_id'],r[1]['processing']['run_id'])
 def test_changed_plan_requires_preview_and_cannot_smuggle_submit(self):
  b=self.prepared(self.body());b['processing']['review']=False
  with self.assertRaises(Problem):self.imp.apply(b)
  b=self.body();b['processing']['submit']=True
  with self.assertRaises(Problem):self.imp.preview(b)
 def test_preexisting_file_does_not_retroactively_dispatch(self):
  b=self.body();b.pop('processing');self.imp.apply(self.prepared(b));r=self.imp.apply(self.prepared(self.body()));self.assertTrue(r['replayed']);self.assertNotIn('processing',r);self.assertEqual(self.app.automation.state()['runs'],[])
 def test_completed_bilingual_content_is_preserved(self):
  b=self.body();b['products'][0].update(CONTENT);b['processing']['review']=False;p=self.imp.preview(b);self.assertEqual(p['processing']['max_calls'],0);self.imp.apply(self.prepared(b))
  with patch.object(self.app.models,'translate') as call:
   self.app.automation.tick();self.app.automation.tick();self.assertEqual(call.call_count,0)
 def test_model_availability_changed_requires_new_preview(self):
  b=self.prepared(self.body())
  with patch.object(self.app.models,'ready',return_value=False):
   with self.assertRaises(Problem) as e:self.imp.apply(b)
   self.assertEqual(e.exception.status,409)
  self.assertEqual(self.app.store.list(),[])
if __name__=='__main__':unittest.main()
