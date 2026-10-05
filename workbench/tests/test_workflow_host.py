import unittest,json
from unittest.mock import patch,Mock
import test_image_host as fixtures
from core import Problem
class WorkflowHostTests(unittest.TestCase):
 setUp=fixtures.ImageHostTests.setUp
 tearDown=fixtures.ImageHostTests.tearDown
 def begin(self,confirm_images=True):
  p=self.s.get(self.pid);p=self.s.update(self.pid,{'source_url':'https://detail.1688.com/offer/1.html','supplier':'QA'},p['revision'])
  if confirm_images:p=self.s.update(p['id'],{'images_verified':True},p['revision'])
  self.a=self.app.automation
  b={'product_ids':[self.pid],'name':'自动托管','request_id':'host-flow','plan':{'image_host':True}}
  pre=self.a.preflight(b);self.assertEqual(pre['eligible_ids'],[self.pid]);self.r=self.a.create({**b,'preflight_token':pre['token']});self.a.tick();return b
 def item(self):return self.a.state()['items'][0]
 def dispatch(self):
  with patch.object(self.app.executor,'submit') as s:self.a.tick()
  return s.call_args.args[1:]
 def test_full_workflow_upload_then_product_approval(self):
  self.begin();jid,p,kind=self.dispatch();self.assertEqual(self.item()['data']['host_job'],jid)
  with patch.object(self.h,'client',return_value=Mock()),patch('image_host.verify_public'):self.app.run(jid,p,kind)
  self.a.tick();self.assertEqual(self.item()['step'],2);self.a.tick();self.a.tick();self.assertEqual(self.item()['status'],'approval');self.assertFalse(self.s.get(self.pid)['reviewed'])
 def test_waiting_keeps_same_job_and_cancel_stops_unsent(self):
  self.begin();jid,p,kind=self.dispatch();self.a.tick();self.assertEqual(self.item()['data']['host_job'],jid)
  self.a.control({'action':'cancel_run','run_id':self.r['id']})
  with patch.object(self.h,'publish') as pub:self.app.run(jid,p,kind);pub.assert_not_called()
 def test_failure_manual_retry_creates_new_job_only_on_retry(self):
  self.begin();jid,p,kind=self.dispatch()
  with patch.object(self.h,'publish',side_effect=Problem('失败')):self.app.run(jid,p,kind)
  self.a.tick();self.assertEqual(self.item()['status'],'attention');self.a.tick();self.assertEqual(self.item()['data']['host_job'],jid)
  self.a.control({'action':'retry','item_id':self.item()['id'],'revision':self.s.get(self.pid)['revision']});new,_,_=self.dispatch();self.assertNotEqual(new,jid)
 def test_unconfirmed_images_wait_then_resume(self):
  p=self.s.get(self.pid);self.s.update(self.pid,{},p['revision'],{'images_verified':False});self.begin(confirm_images=False);self.a.tick();self.assertEqual(self.item()['status'],'approval')
  p=self.s.get(self.pid);self.s.update(self.pid,{'images_verified':True},p['revision']);self.a.control({'action':'retry','item_id':self.item()['id'],'revision':self.s.get(self.pid)['revision']});self.assertTrue(self.dispatch())
 def test_changed_config_does_not_upload_to_new_destination(self):
  self.begin();self.h.save({**self.config,'revision':self.h.state()['revision']})
  with patch.object(self.app.executor,'submit') as s:self.a.tick();s.assert_not_called()
  self.assertEqual(self.item()['status'],'attention')
 def test_post_upload_edit_blocks_advancement(self):
  self.begin();jid,p,kind=self.dispatch()
  with patch.object(self.h,'client',return_value=Mock()),patch('image_host.verify_public'):self.app.run(jid,p,kind)
  p=self.s.get(self.pid);self.s.update(self.pid,{'title_zh':'编辑后'},p['revision']);self.a.tick();self.assertEqual(self.item()['status'],'attention')

import test_workflow_visual as visual_fixtures
class AIHostingTests(unittest.TestCase):
 setUp=visual_fixtures.WorkflowVisualTests.setUp
 tearDown=visual_fixtures.WorkflowVisualTests.tearDown
 item=visual_fixtures.WorkflowVisualTests.item
 candidate=visual_fixtures.WorkflowVisualTests.candidate
 def test_approved_ai_images_flow_to_host_without_extra_image_confirmation(self):
  self.app.image_host.save({'endpoint':'https://store.example.test','region':'test','bucket':'qa-bucket','prefix':'images','public_base':'https://cdn.example.test','addressing_style':'path','access_key':'QA','secret_key':'QA'})
  b={'product_ids':[self.pid],'request_id':'ai-host','name':'AI托管','plan':{'ai_visual':self.opt,'image_host':True}};self.a.create(b);self.a.tick();self.a.tick();link=self.item()['data']['ai_visual']
  for jid in link['job_ids']:self.candidate(jid,True)
  self.a.tick();self.assertFalse(self.s.get(self.pid)['images_verified'])
  with patch.object(self.app.executor,'submit') as submit:self.a.tick()
  _,jid,p,kind=submit.call_args.args
  with patch.object(self.app.image_host,'client',return_value=Mock()),patch('image_host.verify_public'):self.app.run(jid,p,kind)
  self.a.tick();self.assertEqual(self.item()['step'],3);self.assertEqual(len(self.s.get(self.pid)['images']),2);self.assertTrue(all(im['public_url'] for im in self.s.get(self.pid)['images']))
