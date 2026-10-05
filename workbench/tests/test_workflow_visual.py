import json,sys,tempfile,unittest,uuid
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from visual_fixtures import output_image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import App
from core import Problem,now
from visuals import CHECKS
class WorkflowVisualTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.app=App(self.root/'data');self.s=self.app.store;self.a=self.app.automation;__import__('workflow_clock').install_clock(self.app.automation)
  self.kick=patch.object(self.app.visuals,'kick');self.kick.start();self.image=self.root/'reference.png';Image.new('RGB',(1600,1600),'gray').save(self.image)
  self.pid=self.s.import_rows([{'title_zh':'灰盒','facts':'One gray box','supplier':'QA','source_url':'https://detail.1688.com/offer/123.html'}])['created'][0]
  self.app.media.ingest(self.image,'原图','Synthetic QA',self.pid)
  self.opt={'shots':['hero','scene'],'model':'gpt-6-sol','aspect':'square','style':'Soft studio light','auto_check':False}
 def tearDown(self):
  self.kick.stop();self.a.close();self.app.visual_checks.close();self.app.visuals.close();self.app.media.close();self.app.executor.shutdown(wait=True);self.tmp.cleanup()
 def body(self):return {'product_ids':[self.pid],'name':'AI workflow','request_id':uuid.uuid4().hex,'plan':{'ai_visual':self.opt}}
 def start(self):
  b=self.body();pre=self.a.preflight(b);self.assertEqual(pre['eligible_ids'],[self.pid]);self.a.create({**b,'preflight_token':pre['token']});self.a.tick();self.a.tick();return self.item()['data']['ai_visual']
 def item(self):return self.a.state()['items'][0]
 def candidate(self,jid,approve=False):
  generated=self.root/(jid+'.png');output_image(generated,color='black' if self.app.visuals.get(jid)['recipe']['shot']=='hero' else 'blue')
  a=self.app.media.ingest(generated,'成片','Synthetic QA',self.pid)
  with self.s.connect() as c:
   asset=json.loads(c.execute('SELECT data FROM media_assets WHERE id=?',(a['id'],)).fetchone()[0]);asset.update(visual_job_id=jid,parents=[]);c.execute('UPDATE media_assets SET data=? WHERE id=?',(json.dumps(asset),a['id']))
  self.app.visuals.update(jid,'candidate','Synthetic candidate',asset_id=a['id']);j=self.app.visuals.get(jid)
  if approve:self.app.visuals.review({'id':jid,'expected_updated_at':j['updated_at'],'decision':'approved','checks':{k:True for k in CHECKS}})
  return a['id']
 def test_preflight_photos_and_no_calls(self):
  pre=self.a.preflight(self.body());self.assertEqual(pre['image_calls'],2);self.assertEqual(pre['image_checks'],0);self.assertEqual(len(pre['rows'][0]['visual']['references']),1)
  with self.s.connect() as c:c.execute('DELETE FROM media_assets')
  self.assertEqual(self.a.preflight(self.body())['eligible_ids'],[])
  self.assertEqual(self.app.visuals.state()['jobs'],[])
 def test_generate_wait_approve_attach_and_no_duplicate(self):
  link=self.start();self.a.tick();self.a.tick();self.assertEqual(len(self.app.visuals.state()['jobs']),2)
  for jid in link['job_ids']:self.candidate(jid)
  self.a.tick();self.assertEqual(self.item()['status'],'approval');self.assertEqual(self.s.get(self.pid)['images'],[])
  for jid in link['job_ids']:
   j=self.app.visuals.get(jid);self.app.visuals.review({'id':jid,'expected_updated_at':j['updated_at'],'decision':'approved','checks':{k:True for k in CHECKS}})
  self.a.tick();p=self.s.get(self.pid);self.assertEqual(len(p['images']),2);self.assertFalse(p['images_verified']);self.assertFalse(p['reviewed']);self.assertEqual(self.item()['step'],2)
  self.a.tick();self.a.tick();self.assertEqual(self.item()['status'],'approval');self.assertEqual(len(self.app.visuals.state()['jobs']),2)
 def test_checks_reserved_and_mismatch_requires_reapproval(self):
  profile=self.app.models.save({'name':'Compare','provider':'codex-subscription','model':'gpt-6-sol','enabled':True})['id'];self.opt.update(auto_check=True,check_profile_id=profile)
  self.assertEqual(self.a.preflight(self.body())['image_checks'],2);link=self.start();self.assertEqual(len(link['check_ids']),2)
  for jid in link['job_ids']:self.candidate(jid,True)
  self.a.tick();self.assertEqual(self.item()['status'],'waiting')
  with self.s.connect() as c:
   for jid in link['check_ids']:c.execute("UPDATE jobs SET status='done',result=? WHERE id=?",(json.dumps({'report':{'verdict':'mismatch'}}),jid))
  self.a.tick();self.assertEqual(self.item()['status'],'approval');self.assertIn('采用原因',self.item()['message']);self.assertEqual(self.s.get(self.pid)['images'],[])
  self.a.tick();self.assertEqual(self.item()['status'],'approval');self.assertEqual(self.app.models.state()['calls'],[])
 def test_identity_change_stops_and_retry_keeps_child_ids(self):
  link=self.start();p=self.s.get(self.pid);p=self.s.update(self.pid,{'facts':'Now two red boxes'},p['revision']);self.a.tick();self.assertEqual(self.item()['status'],'attention')
  self.a.control({'action':'retry','item_id':self.item()['id'],'revision':p['revision']});self.a.tick();self.assertEqual(self.item()['status'],'attention');self.assertEqual(self.item()['data']['ai_visual'],link);self.assertEqual(len(self.app.visuals.state()['jobs']),2)
 def test_child_and_link_transaction_rollback(self):
  self.a.create(self.body());self.a.tick()
  with patch.object(self.s,'event',side_effect=Problem('rollback')):self.a.tick()
  self.assertEqual(self.app.visuals.state()['jobs'],[]);self.assertNotIn('ai_visual',self.item()['data'])
 def test_cancel_stops_unsent_children_not_inflight(self):
  link=self.start();self.app.visuals.update(link['job_ids'][0],'generating','in flight',dispatched_at=now())
  self.a.control({'action':'cancel_run','run_id':self.item()['run_id']})
  self.assertEqual([self.app.visuals.get(jid)['status'] for jid in link['job_ids']],['generating','cancelled'])
 def test_mutually_exclusive_plans(self):
  with self.assertRaises(Problem):self.a.preflight({**self.body(),'plan':{'ai_visual':self.opt,'image_template':'square'}})

 def test_match_report_allows_human_approval_then_continuation(self):
  profile=self.app.models.save({'name':'Compare','provider':'codex-subscription','model':'gpt-6-sol','enabled':True})['id'];self.opt.update(auto_check=True,check_profile_id=profile);link=self.start()
  for jid in link['job_ids']:self.candidate(jid,True)
  with self.s.connect() as c:
   for jid in link['check_ids']:c.execute("UPDATE jobs SET status='done',result=? WHERE id=?",(json.dumps({'report':{'verdict':'match'}}),jid))
  self.a.tick();self.assertEqual(len(self.s.get(self.pid)['images']),2);self.assertEqual(self.item()['step'],2)
 def test_recovery_after_attachment_does_not_attach_twice(self):
  link=self.start()
  for jid in link['job_ids']:self.candidate(jid,True)
  with patch.object(self.a,'save',side_effect=RuntimeError('crash after attachment')):
   with self.assertRaises(RuntimeError):self.a.tick()
  p=self.s.get(self.pid);self.assertEqual(len(p['images']),2)
  with self.s.connect() as c:c.execute("UPDATE automation_items SET status='queued'")
  self.a.tick();self.assertEqual(self.s.get(self.pid)['revision'],p['revision']);self.assertEqual(self.item()['step'],2)
 def test_existing_candidates_reused_without_new_generation(self):
  body={'product_ids':[self.pid],**self.opt,'request_id':uuid.uuid4().hex,'confirmed':True};pre=self.app.visual_batch.preview(body);out=self.app.visual_batch.apply({**body,'preview_token':pre['token']})
  for jid in out['job_ids']:self.candidate(jid)
  self.assertEqual(self.a.preflight(self.body())['image_calls'],0);link=self.start();self.assertEqual(set(link['job_ids']),set(out['job_ids']));self.assertEqual(len(self.app.visuals.state()['jobs']),2)
