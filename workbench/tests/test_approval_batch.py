import json,unittest,uuid
from unittest.mock import patch
import test_workflow_video as fixtures
from test_automation import CONTENT
from core import now,Problem
from approval_batch import ApprovalBatch
class ApprovalBatchTests(unittest.TestCase):
 setUp=fixtures.WorkflowVideoTests.setUp
 tearDown=fixtures.WorkflowVideoTests.tearDown
 product=fixtures.WorkflowVideoTests.product
 def ready(self):
  p=self.product();p=self.s.update(p['id'],{'source_url':'https://detail.1688.com/offer/123.html','source_sku':uuid.uuid4().hex,'supplier':'QA','brand':'QA','category':'test','mode':'NGS','cost_cny':1,'stock':10,'supply_checked_at':now(),**{k:v for k,v in CONTENT.items() if k!='warnings'}},p['revision']);return self.s.update(p['id'],{'content_verified':True,'images_verified':True,'category_verified':True},p['revision'])
 def body(self,*products):
  b={'product_ids':[p['id'] for p in products],'request_id':uuid.uuid4().hex,'confirmed':True};b['preview_token']=ApprovalBatch(self.s).preview(b)['token'];return b
 def test_partial_readiness_and_replay(self):
  a=self.ready();b=self.product(verified=False);batch=ApprovalBatch(self.s);body=self.body(a,b);self.assertFalse(self.s.get(a['id'])['reviewed']);r=batch.apply(body);self.assertEqual(len(r['approved']),1);self.assertEqual(batch.apply(body),r);self.assertFalse(self.s.get(b['id'])['reviewed'])
 def test_stale_revision_and_image_bytes(self):
  p=self.ready();batch=ApprovalBatch(self.s);b=self.body(p);self.s.update(p['id'],{'facts':'new'},p['revision'])
  with self.assertRaises(Problem):batch.apply(b)
  p=self.ready();b=self.body(p);(self.s.assets/p['images'][0]['file']).write_bytes(b'changed')
  with self.assertRaises(Problem):batch.apply(b)
 def test_atomic_failure_rolls_back_earlier_approval(self):
  a=self.ready();b=self.ready();batch=ApprovalBatch(self.s);body=self.body(a,b);orig=self.s.approve;calls=[]
  def fail(*args,**kw):
   calls.append(1)
   if len(calls)==2:raise Problem('fail')
   return orig(*args,**kw)
  with patch.object(self.s,'approve',side_effect=fail):
   with self.assertRaises(Problem):batch.apply(body)
  self.assertFalse(self.s.get(a['id'])['reviewed']);self.assertFalse(self.s.get(b['id'])['reviewed']);self.assertEqual(len(batch.apply(body)['approved']),2)
 def test_waiting_approval_flow_resumes(self):
  p=self.ready();a=self.app.automation;a.create({'product_ids':[p['id']],'name':'Review','request_id':'review'})
  for _ in range(3):a.tick()
  self.assertEqual(a.state()['items'][0]['status'],'approval');ApprovalBatch(self.s).apply(self.body(p));a.tick();a.tick();self.assertEqual(a.state()['items'][0]['status'],'done')
 def test_auto_submit_requires_ack_and_public_urls(self):
  p=self.ready();p=self.s.update(p['id'],{},p['revision'],{'images':[{**im,'public_url':'https://example.test/'+im['file']} for im in p['images']]});a=self.app.automation
  with patch.object(self.app,'config',return_value={**self.app.config(),'noon_ready':True,'submit_enabled':True}):rid=a.create({'product_ids':[p['id']],'name':'Submit','request_id':'submit','plan':{'submit':True}})['id']
  for _ in range(3):a.tick()
  batch=ApprovalBatch(self.s);b=self.body(p);self.assertEqual(batch.preview(b)['auto_submit'],1)
  with self.assertRaises(Problem):batch.apply(b)
  b['ack_auto_submit']=True;batch.apply(b);self.assertTrue(self.s.get(p['id'])['reviewed'])
 def test_inflight_jobs_and_missing_files_block(self):
  p=self.ready();self.s.add_job(p['id'],'translate',p['revision']);batch=ApprovalBatch(self.s);self.assertEqual(batch.preview({'product_ids':[p['id']]})['ready'],0)
  p=self.ready();(self.s.assets/p['images'][0]['file']).unlink();self.assertEqual(batch.preview({'product_ids':[p['id']]})['ready'],0)
 def test_500_products_all_counted_and_approved(self):
  base=self.ready();rows=[{**base,'source_sku':'batch-'+str(n)} for n in range(499)];ids=self.s.import_rows(rows)['created'];products=[base]
  for pid in ids:products.append(self.s.update(pid,{'content_verified':True,'images_verified':True,'category_verified':True},1,{'images':base['images']}))
  batch=ApprovalBatch(self.s);b=self.body(*products);self.assertEqual(batch.preview(b)['ready'],500);self.assertEqual(len(batch.apply(b)['approved']),500)
