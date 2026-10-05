import json,time,unittest
from unittest.mock import patch
import test_video_batch as fixtures
from test_automation import CONTENT
from core import now,Problem
class WorkflowVideoTests(unittest.TestCase):
 setUp=fixtures.VideoBatchTests.setUp
 tearDown=fixtures.VideoBatchTests.tearDown
 product=fixtures.VideoBatchTests.product
 wait=fixtures.VideoBatchTests.wait
 def begin(self):
  p=self.product();p=self.s.update(p['id'],{'source_url':'https://detail.1688.com/offer/123.html','supplier':'QA','brand':'QA','category':'test','mode':'NGS','cost_cny':1,'stock':10,'supply_checked_at':now(),**{k:v for k,v in CONTENT.items() if k!='warnings'}},p['revision']);p=self.s.update(p['id'],{'content_verified':True,'images_verified':True,'category_verified':True},p['revision']);self.s.approve(p['id'],p['revision']);self.pid=p['id'];self.a=self.app.automation
  self.rid=self.a.create({'request_id':'video-flow','name':'视频流程','product_ids':[p['id']],'plan':{'video':{'seconds':1,'aspect':'square'}}})['id']
  for _ in range(3):self.a.tick()
 def item(self):return self.a.state()['items'][0]
 def test_real_video_returns_to_flow_and_no_repeat(self):
  self.begin();self.a.tick();tid=self.item()['data']['video_task'];self.a.tick();self.assertEqual(self.item()['data']['video_task'],tid);self.media.control({'action':'resume'});self.assertEqual(self.wait(tid)['status'],'done');self.a.tick();self.assertEqual(len(self.item()['data']['video_outputs']),1);self.assertEqual(self.item()['status'],'approval')
  # Encoding completion does not replace playback approval.
  self.a.tick();self.assertEqual(self.item()['status'],'approval');self.assertEqual(len(self.media.state()['tasks']),1)
  from video_batch import VideoBatch
  aid=self.item()['data']['video_outputs'][0];asset=self.media.get(aid)
  with self.assertRaises(Problem):VideoBatch(self.app).review({'asset_id':aid,'decision':'approved','expected_sha256':asset['sha256'],'checks':{'identity':True}})
  VideoBatch(self.app).review({'asset_id':aid,'decision':'approved','expected_sha256':asset['sha256'],'checks':{k:True for k in ('identity','motion','quality')}})
  self.a.tick();self.a.tick();self.assertEqual(self.item()['status'],'done');self.assertEqual(len(self.media.state()['tasks']),1)
 def test_checkpoint_crash_replays_same_media_task(self):
  self.begin();original=self.a.checkpoint
  def crash(i,data):
   if data.get('video_task'):raise KeyboardInterrupt()
   return original(i,data)
  with patch.object(self.a,'checkpoint',side_effect=crash):
   with self.assertRaises(KeyboardInterrupt):self.a.tick()
  self.assertEqual(len(self.media.state()['tasks']),1)
  with self.s.connect() as c:c.execute("UPDATE automation_items SET status='queued'")
  self.a.tick();self.assertEqual(len(self.media.state()['tasks']),1);self.assertTrue(self.item()['data']['video_task'])
 def test_cancellation_stops_owned_queued_task(self):
  self.begin();self.a.tick();tid=self.item()['data']['video_task'];self.a.control({'action':'cancel_run','run_id':self.rid});self.assertEqual(self.media.task_status(tid),'cancelled')
 def test_modified_gallery_blocks_workflow(self):
  self.begin();self.a.tick();p=self.s.get(self.pid);self.s.update(self.pid,{'images_verified':False},p['revision']);self.a.tick();self.assertEqual(self.item()['status'],'attention')
 def test_failed_child_not_automatically_recreated(self):
  self.begin();self.a.tick();tid=self.item()['data']['video_task']
  with self.s.connect() as c:c.execute("UPDATE media_tasks SET status='failed' WHERE id=?",(tid,))
  self.a.tick();self.assertEqual(self.item()['status'],'attention');self.a.tick();self.assertEqual(len(self.media.state()['tasks']),1)

 def test_failed_preparation_can_be_explicitly_replanned(self):
  self.begin()
  with patch.object(self.media,'submit',side_effect=Problem('准备失败')):self.a.tick()
  self.assertEqual(self.item()['status'],'attention');self.assertFalse(self.media.state()['tasks'])
  self.a.control({'action':'retry','item_id':self.item()['id'],'revision':self.s.get(self.pid)['revision']});self.assertNotIn('video_request',self.item()['data']);self.a.tick();self.assertTrue(self.item()['data']['video_task'])
