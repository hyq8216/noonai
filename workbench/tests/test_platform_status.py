import copy,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from platform_status import summarize
from core import Problem
from server import App

def response():
 return {'sku_parent':'Z1','images':[{'review_status':'REVIEW_STATUS_VALID','visibility':'VISIBILITY_STATUS_VISIBLE','issues':[]}], 'statuses':[{'language':lang,'content':{'completeness':'100%','missing_attributes':[],'invalid_attributes':[]},'qc':{'status':'QC_STATUS_APPROVED','rejection_reasons':[],'comment':''},'overall_status':'OVERALL_STATUS_ACTIVE','errors':[]} for lang in ['LANGUAGE_EN','LANGUAGE_AR']]}
def summary(raw=None):return summarize({'sku_parent':'Z1','submitted_revision':1,'content_response':response() if raw is None else raw},1)
class StatusTests(unittest.TestCase):
 def test_success_is_content_only(self):
  s=summary();self.assertEqual(s['group'],'active');self.assertFalse(s['live_verified']);self.assertEqual(len(s['languages']),2)
 def test_rejections_and_attributes_are_preserved(self):
  r=response();r['statuses'][1]['content']['missing_attributes']=['colour'];r['statuses'][1]['qc']={'status':'QC_STATUS_REJECTED','rejection_reasons':['wrong size'],'comment':'check source'}
  r['images'][0]['issues']=[{'code':'IMAGE_BLUR'}];s=summary(r);self.assertEqual(s['group'],'attention');self.assertIn('缺少属性：colour',s['languages'][1]['reasons']);self.assertIn('IMAGE_BLUR',s['images'][0]['reasons'][0])
 def test_pending_and_hidden_image(self):
  r=response()
  for x in r['statuses']:x['qc']['status']='QC_STATUS_PENDING';x['overall_status']='OVERALL_STATUS_INACTIVE'
  self.assertEqual(summary(r)['group'],'pending');r['images'][0]['visibility']='VISIBILITY_STATUS_HIDDEN';self.assertEqual(summary(r)['group'],'attention')
 def test_partial_and_unknown_cannot_be_passed(self):
  for change in [lambda r:r['statuses'].pop(),lambda r:r['statuses'][0]['qc'].update(status='NEW'),lambda r:r['statuses'][0].update(language='LANGUAGE_NEW'),lambda r:r['statuses'][0].update(errors={}),lambda r:r['images'][0].update(review_status={}),lambda r:r['statuses'][0].update(overall_status=[]),lambda r:r['statuses'].append(copy.deepcopy(r['statuses'][0]))]:
   r=response();change(r);self.assertNotEqual(summary(r)['group'],'active')
  self.assertEqual(summary({'sku_parent':'WRONG'})['group'],'unknown');self.assertEqual(summary({})['group'],'unknown')
 def test_revision_and_legacy_record(self):
  s=summarize({'sku_parent':'Z1','submitted_revision':1,'content_response':response()},2);self.assertIn('本地商品已修改',s['notes'][0]);self.assertFalse(s['live_verified'])
  self.assertEqual(summarize(None,1)['group'],'unsubmitted');self.assertEqual(summarize({'sku_parent':'Z1'},1)['group'],'unchecked')
class RefreshTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.app=App(self.tmp.name);self.store=self.app.store
  self.pid=self.store.import_rows([{'title_zh':'测试'}])['created'][0];self.store.record_platform(self.pid,{'sku_parent':'Z1','submitted_revision':1,'checked_at':'old'})
 def tearDown(self):self.app.automation.close();self.app.media.close();self.app.executor.shutdown(wait=True);self.tmp.cleanup()
 def run_refresh(self,raw):
  p=self.store.get(self.pid);jid=self.store.add_job(self.pid,'refresh',p['revision'])
  with patch('server.Noon') as client:client.return_value.content.return_value=raw;self.app.run(jid,p,'refresh')
  with self.store.connect() as c:return dict(c.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone())
 def test_valid_refresh_persists_readable_summary(self):
  self.assertEqual(self.run_refresh(response())['status'],'done');p=self.store.get(self.pid);self.assertEqual(p['platform_summary']['group'],'active');self.assertFalse(p['reviewed']);self.assertEqual(p['revision'],1)
 def test_bad_response_keeps_last_record(self):
  for raw in [{},{'sku_parent':'Z2','statuses':[]},[],{'sku_parent':'Z1','statuses':{}}]:
   self.assertEqual(self.run_refresh(raw)['status'],'failed');self.assertEqual(self.store.get(self.pid)['platform']['checked_at'],'old')
 def test_parent_changed_during_read_not_overwritten(self):
  p=self.store.get(self.pid);jid=self.store.add_job(self.pid,'refresh',1)
  def changed(parent):self.store.record_platform(self.pid,{'sku_parent':'Z2'});return response()
  with patch('server.Noon') as client:client.return_value.content.side_effect=changed;self.app.run(jid,p,'refresh')
  self.assertEqual(self.store.get(self.pid)['platform'],{'sku_parent':'Z2'})
  with self.store.connect() as c:self.assertEqual(c.execute('SELECT status FROM jobs WHERE id=?',(jid,)).fetchone()[0],'failed')
