import json,sys,tempfile,unittest,uuid,threading
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import App
from core import Problem,now
from platform_batch import PlatformBatch
from content_submit_batch import ContentSubmitBatch
class BatchTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.app=App(self.tmp.name);self.s=self.app.store;self.b=PlatformBatch(self.app)
  self.config=patch.object(self.app,'config',return_value={'noon_ready':True,'submit_enabled':False});self.config.start()
  self.dispatch=patch.object(self.app.executor,'submit');self.send=self.dispatch.start()
 def tearDown(self):
  self.dispatch.stop();self.config.stop();self.app.automation.close();self.app.media.close();self.app.executor.shutdown(wait=True);self.tmp.cleanup()
 def products(self,n=2):
  ids=self.s.import_rows([{'title_zh':'回查 '+uuid.uuid4().hex} for _ in range(n)])['created']
  for pid in ids:self.s.record_platform(pid,{'sku_parent':'Z'+pid})
  return ids
 def approved_submit_product(self,sku):
  pid=self.s.import_rows([{'title_zh':'可提交合成商品','source_sku':sku,'source_url':'https://example.com/item',
   'supplier':'QA','brand':'QA','facts':'黑色，1件','category':'test-category','title_en':'Black item','description_en':'One black item',
   'title_ar':'عنصر أسود','description_ar':'عنصر واحد','rights_evidence':'synthetic','mode':'NGS','cost_cny':1,'stock':10,
   'supply_checked_at':now()}])['created'][0]
  p=self.s.get(pid);p=self.s.update(pid,{'content_verified':True,'images_verified':True,'category_verified':True},p['revision'],
   {'images':[{'id':'qa','source':'qa.png','file':'qa.jpg','public_url':'https://example.com/qa.jpg'}]})
  return self.s.approve(pid,p['revision'])
 def request(self,ids):return {'product_ids':ids,'preview_token':self.b.preview({'product_ids':ids})['token'],'request_id':uuid.uuid4().hex,'confirmed':True}
 def test_isolated_preflight_and_replay(self):
  ids=self.products();self.s.record_platform(ids[1],{})
  b=self.request(ids);pre=self.b.preview(b);self.assertEqual(pre['ready'],1);self.assertEqual(pre['blocked'],1);self.send.assert_not_called()
  r=self.b.apply(b);again=self.b.apply(b);self.assertEqual(r['jobs'],again['jobs']);self.assertTrue(again['replayed']);self.assertEqual(self.send.call_count,1)
  self.assertEqual(PlatformBatch(self.app).latest()['request_id'],b['request_id'])
  with self.assertRaises(Problem):self.b.apply({**b,'product_ids':[ids[0]]})
 def test_changed_revision_parent_queue_and_config(self):
  ids=self.products(1);b=self.request(ids);p=self.s.get(ids[0]);self.s.update(p['id'],{'facts':'new'},p['revision'])
  with self.assertRaises(Problem):self.b.apply(b)
  b=self.request(ids);self.s.record_platform(ids[0],{'sku_parent':'other'})
  with self.assertRaises(Problem):self.b.apply(b)
  b=self.request(ids);self.s.add_job(ids[0],'refresh',self.s.get(ids[0])['revision'])
  with self.assertRaises(Problem):self.b.apply(b)
  self.app.config.return_value={'noon_ready':False};self.assertEqual(self.b.preview({'product_ids':ids})['ready'],0);self.send.assert_not_called()
 def test_atomic_rollback(self):
  b=self.request(self.products())
  with patch.object(self.s,'event',side_effect=Problem('injected rollback')):
   with self.assertRaises(Problem):self.b.apply(b)
  with self.s.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM jobs').fetchone()[0],0);self.assertEqual(c.execute("SELECT count(*) FROM ops_requests WHERE key LIKE 'platform-batch:%'").fetchone()[0],0)
  self.send.assert_not_called()
 def test_500_limit_and_capacity(self):
  ids=self.products(500);r=self.b.apply(self.request(ids));self.assertEqual(len(r['jobs']),500);self.assertEqual(self.send.call_count,500)
  progress=self.b.status(r['request_id']);self.assertEqual(progress['counts'],{'queued':500});self.assertEqual(len(progress['jobs']),500)
  self.s.job_result(r['jobs'][0]['job_id'],'failed','test');self.assertEqual(self.b.status(r['request_id'])['counts'],{'failed':1,'queued':499})
  with self.assertRaises(Problem):self.b.preview({'product_ids':ids+['extra']})
  with self.assertRaises(Problem):self.b.preview({'product_ids':[ids[0],ids[0]]})
  ids2=self.products(1)
  with self.s.connect() as c:
   template=c.execute("SELECT * FROM jobs WHERE status='queued' LIMIT 1").fetchone()
   for i in range(501):c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',(uuid.uuid4().hex,*tuple(template)[1:]))
  b=self.request(ids2);self.assertFalse(self.b.preview(b)['capacity_ok'])
  with self.assertRaises(Problem):self.b.apply(b)
 def test_concurrent_duplicate_receipt(self):
  b=self.request(self.products());results=[];errors=[]
  def run():
   try:results.append(self.b.apply(b))
   except Exception as e:errors.append(e)
  threads=[threading.Thread(target=run) for _ in range(2)]
  for t in threads:t.start()
  for t in threads:t.join()
  self.assertFalse(errors);self.assertEqual(results[0]['jobs'],results[1]['jobs']);self.assertEqual(self.send.call_count,2)
 def test_shutdown_dispatch_failure_visible(self):
  self.send.side_effect=RuntimeError('shutdown');r=self.b.apply(self.request(self.products(1)))
  with self.s.connect() as c:job=c.execute('SELECT * FROM jobs WHERE id=?',(r['jobs'][0]['job_id'],)).fetchone();self.assertEqual(job['status'],'failed');self.assertIn('尚未发送',job['message'])
 def test_queued_parent_change_prevents_network(self):
  pid=self.products(1)[0];p=self.s.get(pid);jid=self.s.add_job(pid,'refresh',p['revision']);self.s.record_platform(pid,{'sku_parent':'new'})
  with patch('server.Noon') as noon:self.app.run(jid,p,'refresh');noon.assert_not_called()

 def test_content_submit_preview_flags_unresolved_prior_revision(self):
  pid=self.products(1)[0];p=self.s.get(pid);jid=self.s.add_job(pid,'submit',p['revision'])
  self.s.job_result(jid,'interrupted','synthetic interruption')
  self.s.update(pid,{'facts':'后续编辑'},p['revision'])
  batch=ContentSubmitBatch(self.app).preview({'product_ids':[pid]})
  self.assertEqual(batch['blocked'],1)
  self.assertTrue(any('提交回执待核对' in reason for reason in batch['rows'][0]['reasons']))

 def test_content_submit_preview_allows_corrected_revision_after_known_receipt(self):
  pid=self.products(1)[0];p=self.s.get(pid);jid=self.s.add_job(pid,'submit',p['revision'])
  self.s.record_platform(pid,{'sku_parent':'SYNTHETIC-PARENT','submitted_revision':p['revision'],'live_verified':False})
  self.s.job_result(jid,'needs_attention','synthetic known receipt')
  self.s.update(pid,{'facts':'已修正的事实'},p['revision'])
  preview=ContentSubmitBatch(self.app).preview({'product_ids':[pid]})
  self.assertFalse(any('提交回执待核对' in reason for reason in preview['rows'][0]['reasons']))

 def test_manual_submit_reconciliation_is_idempotent_and_never_marks_live(self):
  pid=self.products(1)[0];p=self.s.get(pid);jid=self.s.add_job(pid,'submit',p['revision']);self.s.job_result(jid,'uncertain','synthetic lost response')
  body={'request_id':'reconcile-'+uuid.uuid4().hex,'job_id':jid,'outcome':'accepted','note':'合成回查：SKU命中且页面显示对应商品编号','sku_parent':'NOON-QA-1','confirmed':True}
  batch=ContentSubmitBatch(self.app);result=batch.reconcile(body);replay=batch.reconcile(body)
  self.assertEqual(result['job_status'],'needs_attention');self.assertTrue(replay['replayed']);self.assertFalse(result['reconciliation']['live_verified'])
  saved=self.s.get(pid);self.assertEqual(saved['platform']['submitted_revision'],p['revision']);self.assertEqual(saved['platform']['sku_parent'],'NOON-QA-1')
  self.assertEqual(saved['platform']['submit_reconciliation']['source'],'operator-entered-noon-readback')
  self.assertEqual(ContentSubmitBatch(self.app).preview({'product_ids':[pid]})['blocked'],1)
  with self.s.connect() as c:
   self.assertEqual(c.execute("SELECT count(*) FROM events WHERE product_id=? AND action='人工核对noon提交已接收'",(pid,)).fetchone()[0],1)
  with self.assertRaises(Problem):batch.reconcile({**body,'note':'different payload'})

 def test_not_found_reconciliation_allows_explicit_repreflight(self):
  pid=self.products(1)[0];p=self.s.get(pid);jid=self.s.add_job(pid,'submit',p['revision']);self.s.job_result(jid,'interrupted','synthetic interruption')
  body={'request_id':'notfound-'+uuid.uuid4().hex,'job_id':jid,'outcome':'not_found','note':'合成回查：按测试 SKU 未发现记录','sku_parent':'','confirmed':True}
  result=ContentSubmitBatch(self.app).reconcile(body)
  self.assertEqual(result['job_status'],'failed');self.assertNotIn('submitted_revision',self.s.get(pid)['platform'])
  preview=ContentSubmitBatch(self.app).preview({'product_ids':[pid]})
  self.assertFalse(any('回执待核对' in reason for reason in preview['rows'][0]['reasons']))
  self.assertTrue(self.s.add_job(pid,'submit',p['revision']),'recording not-found must clear the uncertain-write barrier')
  with self.s.connect() as c:self.assertEqual(c.execute('SELECT status FROM jobs WHERE id=?',(jid,)).fetchone()[0],'failed')

 def test_submit_reconciliation_rolls_back_atomically_on_audit_failure(self):
  pid=self.products(1)[0];p=self.s.get(pid);jid=self.s.add_job(pid,'submit',p['revision']);self.s.job_result(jid,'uncertain','synthetic lost response')
  body={'request_id':'rollback-'+uuid.uuid4().hex,'job_id':jid,'outcome':'not_found','note':'合成回查','sku_parent':'','confirmed':True}
  with patch.object(self.s,'event',side_effect=Problem('injected audit failure')):
   with self.assertRaises(Problem):ContentSubmitBatch(self.app).reconcile(body)
  self.assertEqual(self.s.get(pid)['platform']['sku_parent'],'Z'+pid)
  with self.s.connect() as c:
   self.assertEqual(c.execute('SELECT status FROM jobs WHERE id=?',(jid,)).fetchone()[0],'uncertain')
   self.assertEqual(c.execute("SELECT count(*) FROM ops_requests WHERE key LIKE 'submit-reconcile:%'").fetchone()[0],0)

 def test_noon_preflight_rate_limit_fails_before_content_write_and_can_be_rescheduled(self):
  p=self.approved_submit_product('SUBMIT-429');pid=p['id']
  with patch.object(self.app,'config',return_value={'noon_ready':True,'submit_enabled':True}):
   jid=self.s.add_job(pid,'submit',p['revision'])
   with patch('server.Noon') as noon:
    noon.return_value.attributes.side_effect=Problem('synthetic HTTP 429 during category preflight',502)
    self.app.run(jid,p,'submit')
    noon.return_value.attributes.assert_called_once_with('test-category');noon.return_value.submit.assert_not_called()
   with self.s.connect() as c:
    row=c.execute('SELECT status,message FROM jobs WHERE id=?',(jid,)).fetchone()
    self.assertEqual(row['status'],'failed');self.assertIn('429',row['message'])
   # The failed request was a read-only preflight; it did not create an unknown seller write.
   self.assertTrue(self.s.add_job(pid,'submit',p['revision']))

 def test_recovery_during_read_only_preflight_cannot_cross_submit_boundary(self):
  p=self.approved_submit_product('SUBMIT-RECOVERY-PREFLIGHT');pid=p['id'];jid=self.s.add_job(pid,'submit',p['revision'])
  contract={'attributes':[{'attribute_code':k,'is_mandatory':True,'is_localizable':True,'attribute_type':'ATTRIBUTE_TYPE_TEXT'}
   for k in ['product_title','long_description']]}
  def recover_while_reading(_category):
   with self.s.connect() as c:
    row=c.execute('SELECT status,result FROM jobs WHERE id=?',(jid,)).fetchone()
   self.assertEqual(row['status'],'running');self.assertEqual(json.loads(row['result']),{'phase':'preflight'})
   self.s.recover_jobs()
   return contract
  with patch.object(self.app,'config',return_value={'noon_ready':True,'submit_enabled':True}),patch('server.Noon') as noon:
   noon.return_value.attributes.side_effect=recover_while_reading
   self.app.run(jid,p,'submit')
   noon.return_value.attributes.assert_called_once_with('test-category')
   noon.return_value.submit.assert_not_called()
  with self.s.connect() as c:row=c.execute('SELECT status,message FROM jobs WHERE id=?',(jid,)).fetchone()
  self.assertEqual(row['status'],'failed');self.assertIn('未发送请求',row['message'])

 def test_lost_submit_response_after_remote_acceptance_reconciles_without_resend(self):
  p=self.approved_submit_product('SUBMIT-LOST-RESPONSE');pid=p['id'];jid=self.s.add_job(pid,'submit',p['revision'])
  remote={'offers':{},'submit_count':0,'dispatch_phase':None,'dispatch_raw':None}
  contract={'attributes':[{'attribute_code':k,'is_mandatory':True,'is_localizable':True,'attribute_type':'ATTRIBUTE_TYPE_TEXT'}
   for k in ['product_title','long_description']]}
  def accepted_then_lost(_payload):
   with self.s.connect() as c:
    remote['dispatch_raw']=c.execute('SELECT result FROM jobs WHERE id=?',(jid,)).fetchone()[0]
   state=json.loads(remote['dispatch_raw'])
   remote['dispatch_phase']=state
   remote['submit_count']+=1
   remote['offers']['SUBMIT-LOST-RESPONSE']={'sku_parent':'REMOTE-PARENT-1','accepted':True}
   raise Problem('synthetic connection closed after remote commit',502)
  with patch.object(self.app,'config',return_value={'noon_ready':True,'submit_enabled':True}),patch('server.Noon') as noon:
   noon.return_value.attributes.return_value=contract;noon.return_value.submit.side_effect=accepted_then_lost
   self.app.run(jid,p,'submit')
   self.assertEqual(self.s.history()['jobs'][0]['status'],'uncertain',self.s.history()['jobs'][0]['message'])
   self.assertEqual(remote['dispatch_phase'],{'phase':'submit_dispatching'},f"write boundary at Noon call: {remote['dispatch_raw']!r}")
   self.assertEqual(remote['submit_count'],1);self.assertEqual(remote['offers']['SUBMIT-LOST-RESPONSE']['accepted'],True)
   self.assertEqual(self.s.history()['jobs'][0]['status'],'uncertain')
   self.app.run(jid,p,'submit');noon.return_value.submit.assert_called_once()
   reconciliation=ContentSubmitBatch(self.app).reconcile({'request_id':'lost-readback-'+uuid.uuid4().hex,'job_id':jid,
    'outcome':'accepted','note':'合成卖家回查确认 SKU 对应远端商品','sku_parent':remote['offers']['SUBMIT-LOST-RESPONSE']['sku_parent'],'confirmed':True})
   self.assertEqual(reconciliation['job_status'],'needs_attention');self.assertEqual(remote['submit_count'],1)
   saved=self.s.get(pid);self.assertEqual(saved['platform']['sku_parent'],'REMOTE-PARENT-1')
   self.assertEqual(saved['platform']['submitted_revision'],p['revision']);self.assertFalse(saved['platform']['live_verified'])
   preview=ContentSubmitBatch(self.app).preview({'product_ids':[pid]})
   self.assertTrue(any('平台提交回执' in reason for reason in preview['rows'][0]['reasons']))

 def test_cancel_during_inflight_submit_keeps_uncertain_receipt_and_never_replays(self):
  pid=self.s.import_rows([{'title_zh':'可提交合成商品','source_sku':'SUBMIT-CANCEL','source_url':'https://example.com/item',
   'supplier':'QA','brand':'QA','facts':'黑色，1件','category':'test-category','title_en':'Black item','description_en':'One black item',
   'title_ar':'عنصر أسود','description_ar':'عنصر واحد','rights_evidence':'synthetic','mode':'NGS','cost_cny':1,'stock':10,
   'supply_checked_at':now()}])['created'][0]
  p=self.s.get(pid);p=self.s.update(pid,{'content_verified':True,'images_verified':True,'category_verified':True},p['revision'],
   {'images':[{'id':'qa','source':'qa.png','file':'qa.jpg','public_url':'https://example.com/qa.jpg'}]});p=self.s.approve(pid,p['revision'])
  with patch.object(self.app,'config',return_value={'noon_ready':True,'submit_enabled':True}):
   batch=ContentSubmitBatch(self.app);preview=batch.preview({'product_ids':[pid]})
   body={'product_ids':[pid],'preview_token':preview['token'],'request_id':'submit-cancel-inflight','confirmed':True}
   result=batch.apply(body);jid=result['jobs'][0]['job_id']
   entered=threading.Event();release=threading.Event()
   def uncertain_send(_payload):
    entered.set();release.wait(3);raise Problem('synthetic timeout after request dispatch',502)
   contract={'attributes':[{'attribute_code':k,'is_mandatory':True,'is_localizable':True,'attribute_type':'ATTRIBUTE_TYPE_TEXT'} for k in ['product_title','long_description']]}
   with patch('server.Noon') as noon:
    noon.return_value.attributes.return_value=contract;noon.return_value.submit.side_effect=uncertain_send
    worker=threading.Thread(target=self.app.run,args=(jid,p,'submit'));worker.start()
    try:
     self.assertTrue(entered.wait(2))
     cancelled=batch.cancel(result['request_id'])
     self.assertEqual(cancelled['cancelled_now'],0)
     self.assertEqual(batch.status(result['request_id'])['counts'],{'running':1})
    finally:release.set();worker.join(4)
    self.assertFalse(worker.is_alive());self.assertEqual(batch.status(result['request_id'])['counts'],{'uncertain':1})
    self.app.run(jid,p,'submit');self.assertEqual(noon.return_value.submit.call_count,1)

 def test_cancel_500_unsent_callbacks_never_call_noon(self):
  r=self.b.apply(self.request(self.products(500)));cancelled=self.b.cancel(r['request_id'])
  self.assertEqual(cancelled['cancelled_now'],500);self.assertEqual(cancelled['counts'],{'cancelled':500})
  with patch('server.Noon') as noon:
   for args in self.send.call_args_list:
    _,jid,p,kind=args.args;self.app.run(jid,p,kind)
   noon.assert_not_called()
  self.assertEqual(self.b.cancel(r['request_id'])['cancelled_now'],0)
 def test_cancel_keeps_inflight_result_and_other_batch(self):
  ids=self.products(2);r=self.b.apply(self.request(ids));other=self.b.apply(self.request(self.products(1)))
  jid=r['jobs'][0]['job_id'];pid=r['jobs'][0]['product_id'];p=self.s.get(pid);entered=threading.Event();release=threading.Event()
  def content(parent):entered.set();release.wait(5);return {'sku_parent':parent,'statuses':[],'images':[]}
  with patch('server.Noon') as noon:
   noon.return_value.content.side_effect=content;t=threading.Thread(target=self.app.run,args=(jid,p,'refresh'));t.start()
   try:
    self.assertTrue(entered.wait(3));stopped=self.b.cancel(r['request_id']);self.assertEqual(stopped['cancelled_now'],1);self.assertEqual(stopped['counts'],{'running':1,'cancelled':1})
    self.assertEqual(self.b.status(other['request_id'])['counts'],{'queued':1})
   finally:release.set();t.join(5)
   self.assertFalse(t.is_alive());self.assertEqual(self.b.status(r['request_id'])['counts'],{'done':1,'cancelled':1})
   self.app.run(jid,p,'refresh');self.assertEqual(noon.call_count,1)
  self.assertEqual(self.s.get(pid)['platform']['content_response']['sku_parent'],p['platform']['sku_parent'])
 def test_shutdown_does_not_replace_concurrent_cancellation(self):
  b=self.request(self.products(2))
  def shutdown(*args):self.b.cancel(b['request_id']);raise RuntimeError('shutdown')
  self.send.side_effect=shutdown;r=self.b.apply(b);self.assertEqual(self.b.status(r['request_id'])['counts'],{'cancelled':2})
 def test_cancel_unknown_and_invalid_batch(self):
  with self.assertRaises(Problem):self.b.cancel('missing')
  with self.assertRaises(Problem):self.b.cancel('')
