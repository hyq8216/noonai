"""No subscription calls: protocol fixtures verify file acceptance and release gates."""
import base64
import io
import json
import os
import sys
import tempfile
import time
from datetime import datetime,timedelta,timezone
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image,ImageDraw
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Store,Problem
from media import Media
from visuals import Visuals,CHECKS,image_bytes,generate_image
from codex_subscription import CodexSubscription

FAKE=r'''
import json,sys,time,os,base64
mode=os.environ.get('NOON_VISUAL_MODE','ok')
def send(v):print(json.dumps(v),flush=True)
for line in sys.stdin:
 d=json.loads(line);rid=d.get('id');method=d.get('method');p=d.get('params',{});r={}
 if rid is None:continue
 if method=='config/read':r={'config':{'mcp_servers':{}}}
 elif method=='account/read':r={'account':{'type':'chatgpt','planType':'plus'}}
 elif method=='modelProvider/capabilities/read':r={'imageGeneration':mode!='no_images'} if mode!='unknown_images' else {}
 elif method=='model/list':r={'data':[{'model':'gpt-6-sol'}]}
 elif method=='account/rateLimits/read':r={'rateLimitsByLimitId':{'codex':{'primary':{'usedPercent':100 if mode=='limit' else 1,'resetsAt':int(time.time())+500,'windowDurationMins':300}}}}
 elif method=='thread/start':
  assert 'features.image_generation=true' in sys.argv
  assert 'features.code_mode_host=true' in sys.argv
  assert 'features.shell_tool=false' in sys.argv
  assert 'features.code_mode=false' in sys.argv
  assert p['sandbox']=='read-only' and p['environments']==[]
  r={'thread':{'id':'qa-thread'},'model':p['model']}
 elif method=='turn/start':
  assert len([x for x in p['input'] if x['type']=='localImage'])==1
  assert 'OPENAI_API_KEY' not in os.environ
  send({'id':rid,'result':{'turn':{'id':'qa-turn'}}})
  if mode=='exit':sys.exit(0)
  item={'type':'agentMessage','text':'/private/fake-product.png','phase':'final_answer'} if mode=='text' else {'type':'imageGeneration','id':'qa-image','status':'completed','result':base64.b64encode(open(os.environ['NOON_VISUAL_FILE'],'rb').read()).decode()}
  if mode=='tool':item={'type':'commandExecution','id':'forbidden'}
  send({'method':'item/completed','params':{'threadId':'qa-thread','turnId':'qa-turn','item':item}})
  send({'method':'turn/completed','params':{'threadId':'qa-thread','turn':{'id':'qa-turn','status':'failed' if mode=='failed_turn' else 'completed','error':{'message':'service failed Bearer secret-bearer access_token=secret-token https://example.invalid/?secret=value'} if mode=='failed_turn' else None}}});continue
 send({'id':rid,'result':r})
'''

def synthetic_output(path,size=(1600,1600),color=(65,76,89)):
 """Distinct, high-resolution white-background fixture accepted by local QC."""
 image=Image.new('RGB',size,'white')
 draw=ImageDraw.Draw(image)
 w,h=size
 draw.polygon([(w*.28,h*.3),(w*.72,h*.3),(w*.8,h*.68),(w*.2,h*.68)],fill=color)
 image.save(path)
 return path

def synthetic_noise_output(path):
 image=Image.new('RGB',(1600,1600),'white')
 noise=Image.frombytes('RGB',(900,900),os.urandom(900*900*3))
 image.paste(noise,(350,350));noise.close();image.save(path)
 return path

class VisualTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.store=Store(self.root/'data');self.media=Media(self.store);self.codex=CodexSubscription();self.v=Visuals(self.store,self.media,self.codex)
  self.kick=patch.object(self.v,'kick');self.kick.start()
  self.pid=self.store.import_rows([{'title_zh':'测试商品','facts':'黑色；5件；8cm','brand':'无品牌'}])['created'][0]
  self.source=self.root/'source.png';Image.new('RGB',(1600,1600),'gray').save(self.source)
  self.output=synthetic_output(self.root/'output.png')
  self.a=self.media.ingest(self.source,'测试参考原图','测试自制素材',self.pid)
  self.binary=self.root/'codex';self.binary.write_text('#!'+sys.executable+'\n'+FAKE);self.binary.chmod(0o700)
  self.find=patch('codex_subscription.executable',return_value=str(self.binary));self.find.start()
  self.env=patch.dict(os.environ,{'NOON_VISUAL_FILE':str(self.output),'NOON_VISUAL_MODE':'ok','OPENAI_API_KEY':'must-not-be-used'});self.env.start()
 def tearDown(self):
  self.codex.close();self.v.close();self.media.close();self.env.stop();self.find.stop();self.kick.stop();self.tmp.cleanup()
 def request(self,**extra):return {'request_id':'batch-one','product_id':self.pid,'revision':self.store.get(self.pid)['revision'],'asset_ids':[self.a['id']],'shots':['hero'],'confirmed':True,'locked_features':'5件黑色商品，形状和尺寸不变','style':'柔和棚拍',**extra}
 def job(self,**extra):return self.v.submit(self.request(**extra))['job_ids'][0]
 def review(self,jid,**extra):return self.v.review({'id':jid,'expected_updated_at':self.v.get(jid)['updated_at'],'decision':'approved','checks':{k:True for k in CHECKS},**extra})
 def attach(self,aid,pid=None):
  pid=pid or self.pid;return self.media.attach({'asset_id':aid,'product_id':pid,'revision':self.store.get(pid)['revision']})
 def test_batch_dedupe_and_original_reference_validation(self):
  a=self.v.submit(self.request(shots=['hero','scene']));self.assertEqual(a,self.v.submit(self.request(shots=['hero','scene'])))
  with self.assertRaises(Problem):self.v.submit(self.request(shots=['model']))
  for extra in ({'confirmed':False},{'asset_ids':[]},{'shots':['video']},{'request_id':'%'}):
   with self.assertRaises(Problem):self.v.submit(self.request(**extra))
  p2=self.store.import_rows([{'title_zh':'另一商品'}])['created'][0]
  with self.assertRaises(Problem):self.v.submit(self.request(request_id='other',product_id=p2))
 def test_real_protocol_candidate_review_and_attach(self):
  jid=self.job();self.v.drain();j=self.v.get(jid);self.assertEqual(j['status'],'candidate');self.assertTrue(j['dispatched_at']);self.assertEqual(j['trace']['turn_id'],'qa-turn')
  with self.assertRaises(Problem):self.attach(j['asset_id'])
  with self.assertRaises(Problem):self.review(jid,checks={'identity':True})
  self.review(jid);p=self.attach(j['asset_id']);self.assertEqual(len(p['images']),1);self.assertFalse(p['images_verified'])
  self.assertEqual(self.media.get(j['asset_id'])['parents'],[self.a['id']]);self.assertEqual(self.source.read_bytes(),(self.media.root/self.a['file']).read_bytes())
 def test_image_capability_must_be_confirmed_before_dispatch(self):
  for mode in ('no_images','unknown_images'):
   jid=self.job(request_id=mode.replace('_','-'));self.v.control({'action':'resume'})
   with patch.dict(os.environ,{'NOON_VISUAL_MODE':mode}):self.v.drain()
   j=self.v.get(jid);self.assertEqual(j['status'],'blocked');self.assertIsNone(j['dispatched_at']);self.assertIn('未确认支持生图',j['message'])
 def test_text_response_diagnostics_persist_without_becoming_image(self):
  jid=self.job()
  with patch.dict(os.environ,{'NOON_VISUAL_MODE':'text'}):self.v.drain()
  j=self.v.get(jid);d=j['trace']['diagnostics'];self.assertEqual(d['turn_status'],'completed');self.assertEqual(d['image_items'],0);self.assertIn('/private/fake-product.png',d['response_note']);self.assertIn('只返回了文字',j['message']);self.assertIsNone(j['asset_id'])
  self.v.start();self.assertEqual(self.v.get(jid)['trace']['diagnostics'],d);self.assertTrue(self.v.state()['paused'])
 def test_failed_turn_diagnostics_redact_service_secrets(self):
  jid=self.job()
  with patch.dict(os.environ,{'NOON_VISUAL_MODE':'failed_turn'}):self.v.drain()
  j=self.v.get(jid);d=j['trace']['diagnostics'];self.assertEqual(d['turn_status'],'failed');self.assertEqual(j['status'],'uncertain');self.assertIsNone(j['asset_id'])
  for secret in ('secret-bearer','secret-token','example.invalid'):self.assertNotIn(secret,d['service_error'])
 def test_success_records_receipt_but_not_quality_approval(self):
  self.assertIsNone(self.v.state()['last_image_received_at']);jid=self.job();self.v.drain();j=self.v.get(jid)
  self.assertIsNotNone(self.v.state()['last_image_received_at']);self.assertEqual(j['trace']['diagnostics']['image_items'],1);self.assertEqual(j['status'],'candidate');self.assertFalse(self.store.get(self.pid)['images_verified'])
 def test_diagnostic_notes_are_bounded_and_redacted(self):
  from visuals import diagnostic_text
  for value in ('sk-exampleSecret','Bearer private-secret','access_token=anotherSecret','eyJheader.payload.signature'):
   self.assertNotIn(value,diagnostic_text(value))
  self.assertEqual(len(diagnostic_text('x'*3000)),1200)
 def test_low_resolution_output_preserved_and_cannot_be_approved(self):
  Image.new('RGB',(1254,1254),'red').save(self.output);jid=self.job();self.v.drain();j=self.v.get(jid)
  self.assertEqual(j['status'],'output_rejected');a=self.media.get(j['asset_id']);self.assertEqual((self.media.root/a['file']).read_bytes(),self.output.read_bytes());self.assertFalse(self.v.state()['paused'])
  self.assertEqual(j['trace']['diagnostics']['output_width'],1254);self.assertEqual(j['trace']['diagnostics']['output_check'],'rejected')
  with self.assertRaises(Problem):self.review(jid)
  with self.assertRaises(Problem):self.attach(j['asset_id'])
  self.v.control({'action':'discard-output','id':jid});self.assertEqual(self.v.get(jid)['status'],'cancelled');self.assertTrue((self.media.root/a['file']).exists())
  with self.assertRaises(Problem):self.attach(j['asset_id'])
 def test_wrong_aspect_preserved_stops_remaining_batch(self):
  Image.new('RGB',(2400,1600),'red').save(self.output);jobs=self.v.submit(self.request(shots=['hero','scene']))['job_ids'];self.v.drain()
  self.assertEqual(self.v.get(jobs[0])['status'],'output_rejected');self.assertEqual(self.v.get(jobs[1])['status'],'output_rejected');self.assertIn('画幅',self.v.get(jobs[0])['message'])
  with self.assertRaises(Problem):self.review(jobs[0])
 def test_large_image_protocol_not_truncated_at_two_megabytes(self):
  # Preserve a white hero perimeter while making the transport payload noisy.
  synthetic_noise_output(self.output)
  self.assertGreater(self.output.stat().st_size,2_000_000)
  jid=self.job();self.v.drain();self.assertEqual(self.v.get(jid)['status'],'candidate')
 def test_lost_response_no_automatic_retry_and_batch_paused(self):
  jobs=self.v.submit(self.request(shots=['hero','scene']))['job_ids']
  with patch.dict(os.environ,{'NOON_VISUAL_MODE':'exit'}):self.v.drain()
  self.assertEqual(self.v.get(jobs[0])['status'],'uncertain');self.assertEqual(self.v.get(jobs[1])['status'],'queued');self.assertTrue(self.v.state()['paused'])
  with self.assertRaises(Problem):self.v.control({'action':'retry','id':jobs[0]})
  self.v.start();self.assertEqual(self.v.get(jobs[0])['status'],'uncertain')
 def test_quota_before_dispatch_can_retry(self):
  jid=self.job()
  with patch.dict(os.environ,{'NOON_VISUAL_MODE':'limit'}):self.v.drain()
  self.assertEqual(self.v.get(jid)['status'],'waiting');self.assertIsNone(self.v.get(jid)['dispatched_at']);self.assertFalse(self.v.state()['paused'])
  self.v.drain();self.assertEqual(self.v.get(jid)['status'],'waiting')
  self.v.update(jid,'waiting','due',trace=json.dumps({'retry_at':(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()}));self.v.drain();self.assertEqual(self.v.get(jid)['status'],'candidate')
 def test_prose_file_and_extra_tools_are_not_images(self):
  for mode in ('text','tool'):
   jid=self.job(request_id=mode)
   self.v.control({'action':'resume'})
   with patch.dict(os.environ,{'NOON_VISUAL_MODE':mode}):self.v.drain()
   self.assertEqual(self.v.get(jid)['status'],'uncertain');self.assertIsNone(self.v.get(jid)['asset_id'])
 def test_source_changes_block_generation_and_later_use(self):
  jid=self.job();self.v.drain();self.review(jid)
  p=self.store.get(self.pid);self.store.update(self.pid,{'facts':'6件红色'},p['revision'])
  with self.assertRaises(Problem):self.attach(self.v.get(jid)['asset_id'])
  with self.assertRaises(Problem):self.review(jid)
  self.review(jid,decision='rejected',note='源商品已变更')
 def test_reference_copy_is_hash_checked_before_model_dispatch(self):
  jid=self.job();original=__import__('shutil').copyfile
  def corrupt_copy(source,destination):
   result=original(source,destination)
   Path(destination).write_bytes(b'concurrently replaced reference')
   return result
  with patch('visuals.shutil.copyfile',side_effect=corrupt_copy),patch('visuals.generate_image') as generate:
   self.v.drain();generate.assert_not_called()
  job=self.v.get(jid)
  self.assertEqual(job['status'],'blocked');self.assertIsNone(job['dispatched_at'])
  self.assertIn('参考原图与已核验版本不一致',job['message'])
 def test_candidate_not_usable_through_template_derivative(self):
  jid=self.job();self.v.drain();aid=self.v.get(jid)['asset_id']
  recipe=self.media.recipe({'kind':'square','asset_ids':[aid]})
  task=self.media.submit({'request_id':'local-edit','recipes':[recipe]})['task_ids'][0]
  import time
  for _ in range(100):
   if self.media.task_status(task)=='done':break
   time.sleep(.05)
  derivative=next(a for a in self.media.state()['assets'] if a.get('task_id')==task)
  with self.assertRaises(Problem):self.attach(derivative['id'])
  self.review(jid);self.assertEqual(len(self.attach(derivative['id'])['images']),1)
 def test_tamper_output_and_cross_product_attachment(self):
  jid=self.job();self.v.drain();self.review(jid);aid=self.v.get(jid)['asset_id']
  p2=self.store.import_rows([{'title_zh':'另一商品'}])['created'][0]
  with self.assertRaises(Problem):self.attach(aid,p2)
  (self.media.root/self.media.get(aid)['file']).write_bytes(b'tamper')
  with self.assertRaises(Problem):self.attach(aid)
 def test_output_path_scope_and_malformed_result(self):
  hidden=self.root/'outside.png';hidden.write_bytes(self.source.read_bytes());cwd=self.root/'cwd';cwd.mkdir()
  with self.assertRaises(Problem):image_bytes({'result':'not-a-real-image-url','savedPath':str(hidden)},str(cwd))
  local=cwd/'image.png';local.write_bytes(hidden.read_bytes());self.assertEqual(image_bytes({'savedPath':str(local)},str(cwd)),local.read_bytes())
  self.assertEqual(image_bytes({'result':'data:image/png;base64,'+base64.b64encode(local.read_bytes()).decode()},str(cwd)),local.read_bytes())
 def test_restart_marks_dispatched_uncertain_and_preflight_blocked(self):
  j1,j2=self.v.submit(self.request(shots=['hero','detail']))['job_ids']
  self.v.update(j1,'generating','started',dispatched_at='2026-10-01');self.v.update(j2,'preparing','checking')
  self.v.start();self.assertEqual(self.v.get(j1)['status'],'uncertain');self.assertEqual(self.v.get(j2)['status'],'blocked')
  self.assertTrue(self.v.state()['paused'])
 def test_daily_cap_does_not_dispatch_more_images(self):
  jid=self.job()
  with patch.object(self.v,'daily_limit',return_value=0),patch.object(self.codex,'session') as session:self.v.drain();session.assert_not_called()
  self.assertEqual(self.v.get(jid)['status'],'waiting');self.assertIsNone(self.v.get(jid)['dispatched_at']);self.assertIn('retry_at',self.v.get(jid)['trace'])

 def wait_until_due(self,jid):
  self.v.update(jid,'waiting','quota wait',trace=json.dumps({'retry_at':(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()}))
 def test_waiting_blocks_next_queued_job_until_due(self):
  ids=self.v.submit(self.request(shots=['hero','scene']))['job_ids']
  with patch.dict(os.environ,{'NOON_VISUAL_MODE':'limit'}):self.v.drain()
  self.assertEqual(self.v.get(ids[0])['status'],'waiting');self.assertEqual(self.v.get(ids[1])['status'],'queued')
  with patch.object(self.v,'run') as run:self.v.drain();run.assert_not_called()
  self.wait_until_due(ids[0]);self.v.drain();self.assertTrue(all(self.v.get(i)['status']=='candidate' for i in ids))
 def test_background_wake_runs_due_task_without_manual_resume(self):
  jid=self.job()
  with patch.dict(os.environ,{'NOON_VISUAL_MODE':'limit'}):self.v.drain()
  self.v.kick.side_effect=self.v.drain
  with patch('visuals.WAKE_INTERVAL_SECONDS',.02):
   self.v.start();self.wait_until_due(jid);deadline=time.monotonic()+5
   while time.monotonic()<deadline and self.v.get(jid)['status']!='candidate':time.sleep(.02)
   self.assertEqual(self.v.get(jid)['status'],'candidate');self.assertTrue(self.v.state()['auto_resume_running'])
 def test_manual_pause_is_respected_even_after_retry_time(self):
  jid=self.job();self.wait_until_due(jid);self.v.control({'action':'pause'});self.v.kick.reset_mock();self.v.wake_due();self.v.kick.assert_not_called();self.v.drain();self.assertEqual(self.v.get(jid)['status'],'waiting')
  self.v.control({'action':'resume'});self.v.drain();self.assertEqual(self.v.get(jid)['status'],'candidate')
 def test_cancel_waiting_does_not_send_and_releases_queue(self):
  ids=self.v.submit(self.request(shots=['hero','scene']))['job_ids'];self.wait_until_due(ids[0]);self.v.control({'action':'cancel','id':ids[0]});self.v.drain();self.assertEqual(self.v.get(ids[0])['status'],'cancelled');self.assertIsNone(self.v.get(ids[0])['dispatched_at']);self.assertEqual(self.v.get(ids[1])['status'],'candidate')
 def test_wait_persists_restart_and_sent_wait_is_never_retried(self):
  jid=self.job();self.wait_until_due(jid);self.v.start();self.assertEqual(self.v.get(jid)['status'],'waiting');self.v.drain();self.assertEqual(self.v.get(jid)['status'],'candidate')
  self.wait_until_due(jid);self.v.start();self.assertEqual(self.v.get(jid)['status'],'uncertain');self.assertTrue(self.v.state()['paused'])
 def test_subscription_wait_after_dispatch_is_uncertain(self):
  from codex_subscription import SubscriptionWait
  jid=self.job()
  def failed_after_send(id):
   self.v.update(id,'generating','sent',dispatched_at=datetime.now(timezone.utc).isoformat())
   raise SubscriptionWait('ambiguous failure',(datetime.now(timezone.utc)+timedelta(seconds=20)).isoformat())
  with patch.object(self.v,'run',side_effect=failed_after_send):self.v.drain()
  self.assertEqual(self.v.get(jid)['status'],'uncertain');self.assertTrue(self.v.state()['paused']);self.assertIsNone(self.v.state()['next_retry_at'])
 def test_invalid_retry_time_stops_instead_of_looping(self):
  from codex_subscription import SubscriptionWait
  jid=self.job()
  with patch.object(self.v,'run',side_effect=SubscriptionWait('bad','not-a-time')):self.v.drain()
  self.assertEqual(self.v.get(jid)['status'],'blocked');self.assertTrue(self.v.state()['paused'])

if __name__=='__main__':unittest.main()
