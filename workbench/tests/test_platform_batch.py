import sys,tempfile,unittest,uuid,threading
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import App
from core import Problem
from platform_batch import PlatformBatch
class BatchTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.app=App(self.tmp.name);self.s=self.app.store;self.b=PlatformBatch(self.app)
  self.config=patch.object(self.app,'config',return_value={'noon_ready':True});self.config.start()
  self.dispatch=patch.object(self.app.executor,'submit');self.send=self.dispatch.start()
 def tearDown(self):
  self.dispatch.stop();self.config.stop();self.app.automation.close();self.app.media.close();self.app.executor.shutdown(wait=True);self.tmp.cleanup()
 def products(self,n=2):
  ids=self.s.import_rows([{'title_zh':'回查 '+uuid.uuid4().hex} for _ in range(n)])['created']
  for pid in ids:self.s.record_platform(pid,{'sku_parent':'Z'+pid})
  return ids
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
