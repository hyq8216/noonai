"""Isolated image comparison tests: local protocol fixture, never a real account."""
import copy,json,os,sys,tempfile,unittest,uuid
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Problem
from server import App
from visual_check_schema import FIELDS,validate
from visual_checks import VisualChecks
from test_subscription import FAKE as TEXT_FAKE

FAKE=TEXT_FAKE.replace("result={'ok':True}","""if 'identity' in props:
   images=[x for x in p['input'] if x['type']=='localImage']
   assert len(images)==2 and all(os.path.isfile(x['path']) for x in images)
   assert 'features.image_generation=false' in sys.argv and 'features.shell_tool=false' in sys.argv
   result={k:{'status':'mismatch' if k=='color' else 'uncertain','evidence':'Synthetic QA comparison','reference_indices':[1]} for k in props}
  else:result={'ok':True}""")

class VisualCheckTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.app=App(self.root/'data');self.v=self.app.visual_checks
  self.kick=patch.object(self.app.visuals,'kick');self.kick.start()
  self.binary=self.root/'codex';self.binary.write_text('#!'+sys.executable+'\n'+FAKE);self.binary.chmod(0o700)
  self.find=patch('codex_subscription.executable',return_value=str(self.binary));self.find.start()
  self.pid=self.app.models.save({'name':'QA Sol','provider':'codex-subscription','model':'gpt-6-sol','enabled':True,'rpm':120})['id']
  self.image=self.root/'source.png';Image.new('RGB',(1600,1600),'red').save(self.image)
 def tearDown(self):
  self.app.models.codex.close();self.v.close();self.app.visuals.close();self.app.media.close();self.app.executor.shutdown();self.find.stop();self.kick.stop();self.tmp.cleanup()
 def candidate(self):
  pid=self.app.store.import_rows([{'title_zh':'Synthetic QA','facts':'One red square'}])['created'][0]
  ref=self.app.media.ingest(self.image,'Reference','Synthetic QA',pid)
  b={'request_id':uuid.uuid4().hex,'product_id':pid,'revision':1,'asset_ids':[ref['id']],'shots':['hero'],'confirmed':True,'locked_features':'One red square','style':'Plain'}
  jid=self.app.visuals.submit(b)['job_ids'][0]
  out=self.app.media.ingest(self.image,'Synthetic output','QA fixture',pid)
  self.app.visuals.update(jid,'candidate','Synthetic candidate',asset_id=out['id'])
  return jid
 def body(self,*ids):
  b={'ids':list(ids),'profile_id':self.pid,'request_id':uuid.uuid4().hex,'confirmed':True};b['preview_token']=self.v.preview(b)['token'];return b
 def row(self,jid):return next(j for j in self.v.state()['jobs'] if j['id']==jid)
 def test_schema_rejects_invalid_and_aggregates_conservatively(self):
  data={k:{'status':'match','evidence':'Visible','reference_indices':[1]} for k in FIELDS}
  self.assertEqual(validate(data,1)['verdict'],'match');data['color']['status']='uncertain';self.assertEqual(validate(data,1)['verdict'],'uncertain');data['identity']['status']='mismatch';self.assertEqual(validate(data,1)['verdict'],'mismatch')
  for refs in ([True],[2],[],[1,1]):
   bad=copy.deepcopy(data);bad['identity']['reference_indices']=refs
   with self.assertRaises(Problem):validate(bad,1)
  with self.assertRaises(Problem):validate({},1)
 def test_protocol_ledger_replay_and_no_automatic_approval(self):
  vid=self.candidate();b=self.body(vid)
  with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(self.v.submit,[b,b]))
  self.assertEqual(results[0],results[1]);jid=results[0]['job_ids'][0]
  with patch.dict(os.environ,{'OPENAI_API_KEY':'must-not-send'}):self.v.tick()
  j=self.row(jid);self.assertEqual(j['status'],'done');self.assertEqual(j['record']['report']['verdict'],'mismatch');self.assertEqual(self.app.visuals.get(vid)['status'],'candidate')
  self.assertEqual(self.v.preview({'ids':[vid],'profile_id':self.pid})['calls'],0)
  self.assertEqual(self.v.submit(b),results[0]);self.v.tick();self.assertEqual(len(self.app.models.state()['calls']),1)
  r=j['record'];paths=self.v.input(vid)['paths']
  with patch.object(self.app.models.codex,'session',side_effect=AssertionError('No reconnect')):
   out=self.app.models.call(self.pid,r['source'],'visual-check-'+jid,'visual-check',r['product_id'],image_paths=paths)
  self.assertEqual(out,r['report'])
 def test_preview_rejects_unencodable_image_job_id_before_database_query(self):
  with self.assertRaises(Problem):self.v.preview({'ids':['bad-\ud800'],'profile_id':self.pid})
  self.assertEqual(self.app.store.history()['jobs'],[])
 def test_submit_rejects_unencodable_request_before_ledger_or_job_write(self):
  before=self.app.store.history()['jobs']
  with self.assertRaises(Problem):self.v.submit({'ids':['synthetic'],'profile_id':self.pid,'request_id':'bad-\ud800','confirmed':True})
  self.assertEqual(self.app.store.history()['jobs'],before)
 def test_stale_preview_and_stale_result(self):
  vid=self.candidate();b=self.body(vid);path=self.v.input(vid)['paths'][0];os.utime(path,ns=(1,1))
  with self.assertRaises(Problem):self.v.submit(b)
  jid=self.v.submit(self.body(vid))['job_ids'][0];self.v.tick();self.assertFalse(self.row(jid)['stale']);os.utime(path,ns=(2,2));self.assertTrue(self.row(jid)['stale'])
 def test_changed_product_before_dispatch_blocks_without_call(self):
  vid=self.candidate();jid=self.v.submit(self.body(vid))['job_ids'][0];p=self.app.store.get(self.v.input(vid)['product_id']);self.app.store.update(p['id'],{'facts':'Changed'},p['revision']);self.v.tick()
  self.assertEqual(self.row(jid)['status'],'blocked');self.assertEqual(self.app.models.state()['calls'],[])
 def test_daily_wait_pause_resume_and_cancel(self):
  self.app.models.save({**self.app.models.get(self.pid),'daily_calls':0});vid=self.candidate();jid=self.v.submit(self.body(vid))['job_ids'][0];self.v.tick()
  j=self.row(jid);self.assertEqual(j['status'],'waiting');deadline=j['record']['retry_at'];self.assertEqual(self.app.models.state()['calls'],[])
  self.v.control({'action':'pause'});self.assertEqual(self.row(jid)['status'],'paused');self.v.control({'action':'resume'});self.assertEqual(self.row(jid)['record']['retry_at'],deadline);self.v.tick();self.assertEqual(self.row(jid)['status'],'waiting')
  self.v.control({'action':'cancel','id':jid});self.assertEqual(self.row(jid)['status'],'cancelled');self.assertEqual(self.v.preview({'ids':[vid],'profile_id':self.pid})['calls'],1)
 def test_sent_failure_is_not_replayed_and_pauses_other_jobs(self):
  a,b=self.candidate(),self.candidate();ids=self.v.submit(self.body(a,b))['job_ids']
  with patch.dict(os.environ,{'NOON_FAKE_MODE':'failed'}):self.v.tick()
  self.assertEqual([self.row(i)['status'] for i in ids],['uncertain','paused']);self.v.control({'action':'cancel','id':ids[0]});self.assertEqual(self.row(ids[0])['status'],'uncertain');self.v.tick();self.assertEqual(len(self.app.models.state()['calls']),1)
 def test_restart_resumes_only_known_unsent(self):
  a,b=self.candidate(),self.candidate();ids=self.v.submit(self.body(a,b))['job_ids'];r=self.row(ids[1])['record'];r['phase']='running';self.v.update(ids[1],'running','Fixture interruption',r)
  self.app.store.recover_jobs();other=VisualChecks(self.app)
  self.assertEqual(self.row(ids[0])['status'],'queued');self.assertEqual(self.row(ids[1])['status'],'interrupted');other.close()
 def test_profile_change_and_api_profile_rejected(self):
  vid=self.candidate();b=self.body(vid);self.app.models.save({**self.app.models.get(self.pid),'name':'Changed'})
  with self.assertRaises(Problem):self.v.submit(b)
  api=self.app.models.save({'name':'API','provider':'openai','model':'test','enabled':False,'input_price':'1','output_price':'1'})['id']
  with self.assertRaises(Problem):self.v.preview({'ids':[vid],'profile_id':api})
 def test_submission_does_not_wait_for_inflight_model(self):
  vid=self.candidate();self.v.lock.acquire()
  try:self.assertEqual(len(self.v.submit(self.body(vid))['job_ids']),1)
  finally:self.v.lock.release()
 def test_backup_restore_pauses_waiting_checks(self):
  vid=self.candidate();jid=self.v.submit(self.body(vid))['job_ids'][0];r=self.row(jid)['record'];r.update(phase='waiting',retry_at='2099-01-01T00:00:00+00:00');self.v.update(jid,'waiting','QA quota wait',r)
  archive=self.app.recovery.create();preview=self.app.recovery.inspect(self.app.recovery.archive_path(archive['id']));self.app.recovery.schedule({**preview,'confirmed':True});self.app.recovery.apply_pending()
  self.assertEqual(self.row(jid)['status'],'paused')
 def test_change_during_comparison_invalidates_result(self):
  vid=self.candidate();jid=self.v.submit(self.body(vid))['job_ids'][0];original=self.app.models.call
  def mutate(*args,**kwargs):
   result=original(*args,**kwargs);p=self.app.store.get(args[4]);self.app.store.update(p['id'],{'facts':'Changed during call'},p['revision']);return result
  with patch.object(self.app.models,'call',side_effect=mutate):self.v.tick()
  self.assertEqual(self.row(jid)['status'],'uncertain');self.assertNotIn('report',self.row(jid)['record']);self.assertEqual(len(self.app.models.state()['calls']),1)
if __name__=='__main__':unittest.main()
