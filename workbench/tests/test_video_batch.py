import json,sys,tempfile,time,unittest,uuid
from datetime import datetime,timezone,timedelta
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image
from server import App
from video_batch import VideoBatch
from core import Problem
class VideoBatchTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.app=App(Path(self.tmp.name));self.s=self.app.store;self.media=self.app.media;self.batch=VideoBatch(self.app);self.media.control({'action':'pause'})
  # Workflow tests advance the retry clock instead of sleeping for each durable wait.
  self.scheduler_time=[datetime.now(timezone.utc)];auto=self.app.automation;auto.clock=lambda:self.scheduler_time[0];tick=auto.tick
  def advance_tick():self.scheduler_time[0]+=timedelta(seconds=6);return tick()
  auto.tick=advance_tick
 def tearDown(self):self.app.models.codex.close();self.app.visuals.close();self.app.visual_checks.close();self.media.close();self.app.executor.shutdown();self.tmp.cleanup()
 def product(self,title='Product',color='red',verified=True):
  pid=self.s.import_rows([{'title_zh':title,'facts':'test fixture','rights_evidence':'synthetic fixture'}])['created'][0];images=[]
  for i in range(2):
   key=uuid.uuid4().hex;path=self.s.assets/(key+'.jpg');Image.new('RGB',(400,400),color).save(path);images.append({'id':key,'file':path.name,'source':path.name,'size':[400,400],'public_url':''})
  return self.s.update(pid,{},1,{'images':images,'images_verified':verified})
 def body(self,*products):return {'product_ids':[p['id'] for p in products],'seconds':1,'motion':'push','transition':'fade','aspect':'square'}
 def prepared(self,b):return {**b,'preview_token':self.batch.preview(b)['token'],'request_id':uuid.uuid4().hex,'confirmed':True}
 def test_per_product_isolation_missing_approval_and_replay(self):
  red=self.product('Red');blue=self.product('Blue','blue');bad=self.product('Unreviewed',verified=False);b=self.body(red,blue,bad);pre=self.batch.preview(b);self.assertEqual(pre['ready'],2);a=self.prepared(b);r=self.batch.apply(a);self.assertEqual(len(r['task_ids']),2);self.assertEqual(self.batch.apply(a),r)
  for t in self.media.state()['tasks']:
   self.assertEqual({self.media.get(x)['product_id'] for x in t['recipe']['asset_ids']},{t['recipe']['product_video']['product_id']})
  self.assertEqual({r['status'] for r in self.batch.preview(b)['rows']},{'active','blocked'})
 def test_stale_preview_and_mutated_file_rejected(self):
  p=self.product();b=self.prepared(self.body(p));Image.new('RGB',(400,400),'blue').save(self.s.assets/p['images'][0]['file'])
  with self.assertRaises(Problem):self.batch.apply(b)
  self.assertFalse(self.media.state()['tasks'])
 def test_transaction_failure_removes_snapshots_and_tasks(self):
  p=self.product();b=self.prepared(self.body(p));before=set(self.media.root.iterdir())
  with patch.object(self.media,'submit',side_effect=Problem('Injected failure')):
   with self.assertRaises(Problem):self.batch.apply(b)
  self.assertEqual(set(self.media.root.iterdir()),before);self.assertFalse(self.media.state()['assets']);self.assertFalse(self.media.state()['tasks']);self.assertEqual(len(self.batch.apply(b)['task_ids']),1)
 def test_queued_gallery_change_fails_without_output(self):
  p=self.product();r=self.batch.apply(self.prepared(self.body(p)));self.s.update(p['id'],{'images_verified':False},p['revision']);self.media.control({'action':'resume'});task=self.wait(r['task_ids'][0]);self.assertEqual(task['status'],'failed');self.assertIn('图片或验收',task['message']);self.assertFalse(task['result'])
 def wait(self,jid):
  deadline=time.monotonic()+25
  while time.monotonic()<deadline:
   t=next(t for t in self.media.state()['tasks'] if t['id']==jid)
   if t['status'] in ('done','failed','cancelled'):return t
   time.sleep(.05)
  self.fail('render timeout')
 def test_real_encoding_then_reuses_output_and_snapshots_for_new_recipe(self):
  p=self.product();b=self.body(p);r=self.batch.apply(self.prepared(b));self.media.control({'action':'resume'});t=self.wait(r['task_ids'][0]);self.assertEqual(t['status'],'done',t['message']);pre=self.batch.preview(b);self.assertEqual(pre['rows'][0]['status'],'kept')
  output=self.media.get(t['result'][0]);self.assertEqual(output['product_id'],p['id']);self.assertAlmostEqual(output['duration'],1.6,delta=.1)
  self.media.control({'action':'pause'});b['motion']='pull';self.batch.apply(self.prepared(b));snapshots=[a for a in self.media.state()['assets'] if a.get('product_image_snapshot')];self.assertEqual(len(snapshots),2)
 def test_snapshot_corruption_blocks_render(self):
  p=self.product();r=self.batch.apply(self.prepared(self.body(p)));a=self.media.state()['assets'][0];(self.media.root/a['file']).write_bytes(b'changed');self.media.control({'action':'resume'});t=self.wait(r['task_ids'][0]);self.assertEqual(t['status'],'failed');self.assertIn('快照',t['message'])
 def test_limits_options_and_confirmation(self):
  p=self.product();b=self.body(p)
  for bad in ({'product_ids':[]},{'product_ids':[p['id']]*51},{'aspect':'wrong'}):
   with self.assertRaises(Problem):self.batch.preview({**b,**bad})
  with self.assertRaises(Problem):self.batch.apply({**self.prepared(b),'confirmed':False})
 def test_snapshot_excluded_from_visual_original_selection(self):
  p=self.product();self.batch.apply(self.prepared(self.body(p)));pre=self.app.visual_batch.preview({'product_ids':[p['id']],'shots':['hero'],'model':'gpt-6-sol','aspect':'square','style':'white','brief':''});self.assertEqual(pre['rows'][0]['references'],[])
if __name__=='__main__':unittest.main()
