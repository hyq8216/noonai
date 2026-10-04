"""Batch generation -> comparison handoff, all fixtures isolated and offline."""
import json,os,sys,unittest,uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import test_visual_checks as comparison
from core import Problem,ident,now
CHECK_FAKE=comparison.FAKE
from test_visuals import FAKE as IMAGE_FAKE,synthetic_output

PIPE_FAKE='import sys\nexec('+repr(IMAGE_FAKE)+" if 'features.image_generation=true' in sys.argv else "+repr(CHECK_FAKE)+')\n'

class VisualPipelineTests(unittest.TestCase):
 setUp=comparison.VisualCheckTests.setUp
 tearDown=comparison.VisualCheckTests.tearDown
 row=comparison.VisualCheckTests.row
 def prepare(self,count=1,**extra):
  products=[]
  for i in range(count):
   pid=self.app.store.import_rows([{'title_zh':'Pipeline '+str(i),'facts':'One red square'}])['created'][0];self.app.media.ingest(self.image,'QA reference','Synthetic QA',pid);products.append(pid)
  b={'product_ids':products,'shots':['hero'],'model':'gpt-6-sol','aspect':'square','style':'Plain QA','brief':'','auto_check':True,'check_profile_id':self.pid,'request_id':uuid.uuid4().hex,'confirmed':True,**extra}
  b['preview_token']=self.app.visual_batch.preview(b)['token'];return b
 def candidate(self,vid):
  pid=self.app.visuals.get(vid)['recipe']['product_id'];out=self.app.media.ingest(self.image,'QA output','Synthetic QA',pid);self.app.visuals.update(vid,'candidate','QA output',asset_id=out['id'])
 def pipeline(self,count=1,**extra):return self.app.visual_batch.apply(self.prepare(count,**extra))
 def test_preview_reservation_and_atomic_replay(self):
  b=self.prepare(shots=['hero','scene']);preview=self.app.visual_batch.preview(b);self.assertEqual((preview['new_tasks'],preview['check_tasks']),(2,2));self.assertEqual(self.v.state()['jobs'],[])
  with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(self.app.visual_batch.apply,[b,b]))
  self.assertEqual(results[0],results[1]);self.assertEqual(len(results[0]['check_job_ids']),2);self.assertTrue(all(j['status']=='waiting_image' for j in self.v.state()['jobs']));self.v.tick();self.assertEqual(self.app.models.state()['calls'],[])
 def test_actual_protocol_generation_then_automatic_comparison(self):
  self.binary.write_text('#!'+sys.executable+'\n'+PIPE_FAKE);out=self.pipeline(2)
  generated=synthetic_output(self.root/'generated.png')
  with patch.dict(os.environ,{'NOON_VISUAL_FILE':str(generated)}):self.app.visuals.drain()
  self.assertTrue(all(self.app.visuals.get(i)['status']=='candidate' for i in out['job_ids']))
  self.v.tick();self.v.tick();self.assertTrue(all(self.row(i)['status']=='done' for i in out['check_job_ids']));self.assertEqual(len(self.app.models.state()['calls']),2)
  self.v.tick();self.assertEqual(len(self.app.models.state()['calls']),2);self.assertTrue(all(self.app.visuals.get(i)['status']=='candidate' for i in out['job_ids']))
 def test_pause_before_generation_and_resume_preserves_handoff(self):
  out=self.pipeline();self.v.control({'action':'pause'});self.candidate(out['job_ids'][0]);self.v.tick();self.assertEqual(self.row(out['check_job_ids'][0])['status'],'paused');self.assertEqual(self.app.models.state()['calls'],[])
  self.v.control({'action':'resume'});self.assertEqual(self.row(out['check_job_ids'][0])['status'],'waiting_image');self.v.tick();self.assertEqual(self.row(out['check_job_ids'][0])['status'],'done')
 def test_cancel_before_image_never_calls_check(self):
  out=self.pipeline();jid=out['check_job_ids'][0];self.v.control({'action':'cancel','id':jid});self.candidate(out['job_ids'][0]);self.v.tick();self.assertEqual(self.row(jid)['status'],'cancelled');self.assertEqual(self.app.models.state()['calls'],[])
 def test_bad_output_isolated_while_other_candidate_completes(self):
  out=self.pipeline(2);self.app.visuals.update(out['job_ids'][0],'output_rejected','Too small');self.candidate(out['job_ids'][1]);self.v.tick()
  self.assertEqual([self.row(i)['status'] for i in out['check_job_ids']],['blocked','done']);self.assertEqual(len(self.app.models.state()['calls']),1);self.assertEqual(self.v.state(group='attention')['total'],2)
 def test_model_configuration_change_before_handoff_blocks(self):
  out=self.pipeline();self.app.models.save({**self.app.models.get(self.pid),'name':'Changed'});self.candidate(out['job_ids'][0]);self.v.tick();self.assertEqual(self.row(out['check_job_ids'][0])['status'],'blocked');self.assertEqual(self.app.models.state()['calls'],[])
 def test_changed_fact_blocks_only_affected_candidate(self):
  out=self.pipeline(2)
  for vid in out['job_ids']:self.candidate(vid)
  p=self.app.store.get(out['product_ids'][0]);self.app.store.update(p['id'],{'facts':'Changed'},p['revision']);self.v.tick()
  self.assertEqual([self.row(i)['status'] for i in out['check_job_ids']],['blocked','done']);self.assertEqual(len(self.app.models.state()['calls']),1)
 def test_reservation_failure_rolls_back_generation_too(self):
  b=self.prepare()
  with patch.object(self.v,'reserve',side_effect=Problem('Injected failure')):
   with self.assertRaises(Problem):self.app.visual_batch.apply(b)
  self.assertEqual(self.app.visuals.state()['jobs'],[]);self.assertEqual(self.v.state()['jobs'],[])
  self.assertEqual(len(self.app.visual_batch.apply(b)['check_job_ids']),1)
 def test_stale_check_capacity_preview_and_full_queue(self):
  b=self.prepare();p=self.app.store.get(b['product_ids'][0])
  with self.app.store.connect() as c:
   for _ in range(2000):c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',(ident(),p['id'],'visual-check',1,'waiting_image','QA reservation',json.dumps({'phase':'waiting_image'}),now(),now()))
  with self.assertRaises(Problem):self.app.visual_batch.apply(b)
  preview=self.app.visual_batch.preview(b);self.assertEqual(preview['new_tasks'],0);self.assertEqual(preview['rows'][0]['status'],'capacity');self.assertEqual(self.app.visuals.state()['jobs'],[])
 def test_reservations_survive_restart_and_backup_pauses(self):
  out=self.pipeline();self.app.store.recover_jobs();self.assertEqual(self.row(out['check_job_ids'][0])['status'],'waiting_image')
  archive=self.app.recovery.create();pre=self.app.recovery.inspect(self.app.recovery.archive_path(archive['id']));self.app.recovery.schedule({**pre,'confirmed':True});self.app.recovery.apply_pending();self.assertEqual(self.row(out['check_job_ids'][0])['status'],'paused')
 def test_manual_check_cannot_duplicate_reserved_handoff(self):
  out=self.pipeline();vid=out['job_ids'][0];self.candidate(vid);self.assertEqual(self.v.preview({'ids':[vid],'profile_id':self.pid})['calls'],0);self.v.tick();self.assertEqual(len(self.app.models.state()['calls']),1)
 def test_service_failure_pauses_images_not_yet_finished(self):
  out=self.pipeline(2);self.candidate(out['job_ids'][0])
  with patch.dict(os.environ,{'NOON_FAKE_MODE':'failed'}):self.v.tick()
  self.assertEqual([self.row(i)['status'] for i in out['check_job_ids']],['uncertain','paused']);self.assertEqual(self.row(out['check_job_ids'][1])['record']['phase'],'waiting_image')
 def test_generation_blocked_remains_resumable_and_visible(self):
  out=self.pipeline();vid=out['job_ids'][0];self.app.visuals.update(vid,'blocked','Login needed');self.v.tick();s=self.v.state(group='attention');self.assertEqual(s['total'],1);self.assertEqual(s['jobs'][0]['generation']['message'],'Login needed');self.candidate(vid);self.v.tick();self.assertEqual(self.row(out['check_job_ids'][0])['status'],'done')
 def test_pagination_reaches_older_reserved_jobs(self):
  out=self.pipeline(51);first=self.v.state();second=self.v.state(page=1);self.assertEqual((first['total'],first['pages'],len(first['jobs']),len(second['jobs'])),(51,2,50,1));self.assertFalse(set(j['id'] for j in first['jobs'])&set(j['id'] for j in second['jobs']));self.assertEqual(self.v.state(group='processing')['total'],51)
  with self.assertRaises(Problem):self.v.state(page=-1)
  with self.assertRaises(Problem):self.v.state(group='bad')
if __name__=='__main__':unittest.main()
