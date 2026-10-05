"""A possibly delivered Noon write stays blocked across edits and restart."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from content_submit_batch import ContentSubmitBatch
from connectors import RateLimited
from core import Problem, now
from server import App, Handler, ThreadingHTTPServer


class UncertainSubmitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = App(self.tmp.name)
        self.store = self.app.store

    def tearDown(self):
        self.app.executor.shutdown(wait=True)
        self.app.visuals.close()
        self.tmp.cleanup()

    def approved_product(self):
        source = {'title_zh': '五件黑色夹子', 'source_url': 'https://supplier.example/item/' + uuid.uuid4().hex,
                  'supplier': 'QA supplier', 'facts': '5 black plastic clips'}
        pid = self.store.import_rows([source])['created'][0]
        p = self.store.get(pid)
        p = self.store.update(pid, {
            'brand': 'QA', 'category': 'qa-category', 'title_en': 'Black Plastic Clips, 5 Pieces',
            'description_en': 'Five black plastic clips.', 'title_ar': 'مشابك بلاستيكية سوداء',
            'description_ar': 'خمس مشابك بلاستيكية سوداء', 'rights_evidence': 'QA synthetic asset',
            'mode': 'NGS', 'cost_cny': 1, 'stock': 10, 'supply_checked_at': now(),
        }, p['revision'], {'images': [{'id': 'qa-image', 'source': 'source.png', 'file': 'clean.jpg',
                                       'public_url': 'https://images.example/qa.jpg'}]})
        p = self.store.update(pid, {'content_verified': True, 'images_verified': True, 'category_verified': True}, p['revision'])
        return self.store.approve(pid, p['revision'])

    def submit_contract(self):
        return {'attributes': [{'attribute_code': key, 'is_mandatory': True, 'is_localizable': True,
                                'attribute_type': 'ATTRIBUTE_TYPE_TEXT'}
                               for key in ('product_title', 'long_description')]}

    def queue_batch_without_worker(self,p,request_id):
        batch=ContentSubmitBatch(self.app);body={'product_ids':[p['id']]}
        with patch.object(self.app,'config',return_value={'noon_ready':True,'submit_enabled':True}):
            preview=batch.preview(body)
            self.assertEqual(preview['ready'],1)
            body.update(request_id=request_id,preview_token=preview['token'],confirmed=True)
            with patch.object(self.app.executor,'submit') as submit:
                result=batch.apply(body)
                submit.assert_called_once()
        return batch,result

    def test_content_submit_batch_isolates_mixed_results_and_replay_never_resends(self):
        products=[self.approved_product() for _ in range(3)]
        batch=ContentSubmitBatch(self.app)
        body={'product_ids':[p['id'] for p in products]}
        dispatched=[]
        with patch.object(self.app,'config',return_value={'noon_ready':True,'submit_enabled':True}):
            preview=batch.preview(body)
            self.assertEqual(preview['ready'],3)
            body.update(request_id='mixed-content-submit',preview_token=preview['token'],confirmed=True)
            with patch.object(self.app.executor,'submit',side_effect=lambda fn,*args,**kwargs:dispatched.append((fn,args,kwargs))) as submit:
                queued=batch.apply(body)
                self.assertEqual(submit.call_count,3)
        self.assertEqual(len(dispatched),3)
        with patch.object(self.app,'config',return_value={'noon_ready':True,'submit_enabled':True}),patch('server.Noon') as noon:
            noon.return_value.attributes.return_value=self.submit_contract()
            noon.return_value.submit.side_effect=[
                {'sku_parent':'PARENT-SUCCESS','status':{'status_id':0}},
                TimeoutError('synthetic lost response'),
                {'sku_parent':'PARENT-ATTENTION','status':{'status_id':17}},
            ]
            for fn,args,kwargs in dispatched:fn(*args,**kwargs)
            self.assertEqual(noon.return_value.submit.call_count,3)
            snapshot=batch.status(queued['request_id'])
            self.assertEqual(snapshot['counts'],{'done':1,'uncertain':1,'needs_attention':1})
            by_product={job['product_id']:job['status'] for job in snapshot['jobs']}
            expected={args[1]['id']:status for (_,args,_) ,status in zip(dispatched,('done','uncertain','needs_attention'))}
            self.assertEqual(by_product,expected)
            with patch.object(self.app.executor,'submit') as resend:
                replay=batch.apply(body)
                resend.assert_not_called()
            self.assertTrue(replay['replayed'])
            self.assertEqual([item['job_id'] for item in replay['jobs']],[item['job_id'] for item in queued['jobs']])
            self.assertEqual(noon.return_value.submit.call_count,3)
            after= batch.status(queued['request_id'])
            self.assertEqual(after['counts'],snapshot['counts'])
        with patch.object(self.app,'config',return_value={'noon_ready':True,'submit_enabled':True}):
            blocked=batch.preview({'product_ids':[p['id'] for p in products]})
        self.assertEqual(blocked['ready'],0)
        reasons={row['id']:row['reasons'] for row in blocked['rows']}
        uncertain_id=next(pid for pid,status in expected.items() if status=='uncertain')
        attention_id=next(pid for pid,status in expected.items() if status=='needs_attention')
        done_id=next(pid for pid,status in expected.items() if status=='done')
        self.assertTrue(any('提交结果待核对' in reason for reason in reasons[uncertain_id]))
        self.assertTrue(any('此前提交结果待核对' in reason for reason in reasons[attention_id]))
        self.assertTrue(any('已获得平台提交回执' in reason for reason in reasons[done_id]))

    def test_timeout_marks_uncertain_and_edit_cannot_bypass_single_or_batch_guard(self):
        p = self.approved_product()
        jid = self.store.add_job(p['id'], 'submit', p['revision'])
        with patch.object(self.app, 'config', return_value={'noon_ready': True, 'submit_enabled': True}), \
             patch('server.Noon') as noon:
            noon.return_value.attributes.return_value = self.submit_contract()
            noon.return_value.submit.side_effect = TimeoutError('synthetic lost response')
            self.app.run(jid, p, 'submit')

        job = self.store.history()['jobs'][0]
        self.assertEqual((job['id'], job['status']), (jid, 'uncertain'))
        with self.assertRaisesRegex(Problem, '提交结果待核对'):
            self.store.add_job(p['id'], 'submit', p['revision'])
        edited = self.store.update(p['id'], {'note': 'new content revision'}, p['revision'])
        edited = self.store.approve(p['id'], edited['revision'])
        with self.assertRaisesRegex(Problem, '修改商品版本也不能解除拦截'):
            self.store.add_job(p['id'], 'submit', edited['revision'])

        batch = ContentSubmitBatch(self.app)
        with patch.object(self.app, 'config', return_value={'noon_ready': True, 'submit_enabled': True}):
            preview = batch.preview({'product_ids': [p['id']]})
        self.assertEqual(preview['rows'][0]['status'], 'blocked')
        self.assertTrue(any('修改版本也不能解除拦截' in reason for reason in preview['rows'][0]['reasons']))

    def test_explicit_noon_rate_limit_is_not_misreported_as_unknown_or_retried(self):
        p=self.approved_product();jid=self.store.add_job(p['id'],'submit',p['revision'])
        with patch.object(self.app,'config',return_value={'noon_ready':True,'submit_enabled':True}),patch('server.Noon') as noon:
            noon.return_value.attributes.return_value=self.submit_contract()
            noon.return_value.submit.side_effect=RateLimited(42,'request_qa_123')
            self.app.run(jid,p,'submit')
            noon.return_value.submit.assert_called_once()
        job=next(item for item in self.store.history()['jobs'] if item['id']==jid)
        self.assertEqual(job['status'],'failed')
        self.assertIn('HTTP 429',job['message']);self.assertIn('42 秒',job['message']);self.assertIn('request_qa_123',job['message'])
        self.assertIsNone(self.store.get(p['id']).get('platform'))
        next_product=self.approved_product();next_job=self.store.add_job(next_product['id'],'submit',next_product['revision'])
        with patch.object(self.app,'config',return_value={'noon_ready':True,'submit_enabled':True}),patch('server.Noon') as noon:
            self.app.run(next_job,next_product,'submit');noon.assert_not_called()
        next_result=next(item for item in self.store.history()['jobs'] if item['id']==next_job)
        self.assertEqual(next_result['status'],'failed');self.assertIn('本次请求未发送',next_result['message'])
        with patch('server.Noon') as noon:
            with self.assertRaisesRegex(Problem,'本次请求未发送') as blocked:self.app.noon_client()
            self.assertEqual(blocked.exception.status,429);noon.assert_not_called()
        reopened=App(self.tmp.name)
        try:
            with patch('server.Noon') as noon:
                with self.assertRaisesRegex(Problem,'限流冷却中') as blocked:reopened.noon_client()
                self.assertEqual(blocked.exception.status,429);noon.assert_not_called()
        finally:
            reopened.executor.shutdown(wait=True);reopened.visuals.close()
        with self.store.connect() as c:c.execute("UPDATE runtime_state SET value='2000-01-01T00:00:00+00:00' WHERE key='noon_rate_limited_until'")
        with patch('server.Noon') as noon:
            self.app.noon_client();noon.assert_called_once()
        with self.store.connect() as c:self.assertIsNone(c.execute("SELECT value FROM runtime_state WHERE key='noon_rate_limited_until'").fetchone())
        # A later attempt requires an explicit new local queue action.
        retry=self.store.add_job(p['id'],'submit',p['revision'])
        self.assertNotEqual(retry,jid)

    def test_rate_limit_cooldown_survives_real_server_process_restart(self):
        self.app.record_noon_rate_limit(RateLimited(75,'restart-qa-request'))
        ready=Path(self.tmp.name)/'cooldown-ready.json'
        runner='''
import sys
sys.path.insert(0,sys.argv[2])
import server
class ForbiddenNoon:
 def __init__(self,*args,**kwargs):raise server.Problem('test forbids external Noon access',418)
server.Noon=ForbiddenNoon
server.start(sys.argv[1],0,sys.argv[3])
'''
        process=subprocess.Popen([sys.executable,'-c',runner,self.tmp.name,
            str(Path(__file__).resolve().parents[1]),str(ready)],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,
            env={k:v for k,v in os.environ.items() if not k.startswith(('NOON_','OPENAI_','TEXT_','IMAGE_HOST_'))})
        try:
            deadline=time.monotonic()+10
            while not ready.exists() and time.monotonic()<deadline:
                if process.poll() is not None:raise AssertionError(process.stderr.read().decode())
                time.sleep(.05)
            self.assertTrue(ready.exists(),'service did not start with the persisted cooldown database')
            url=json.loads(ready.read_text())['url']
            with urlopen(url+'/api/state',timeout=10) as response:token=json.load(response)['token']
            for _ in range(2):
                request=Request(url+'/api/noon/categories',data=b'{}',headers={'Content-Type':'application/json','X-Workbench-Token':token})
                with self.assertRaises(HTTPError) as limited:urlopen(request,timeout=10)
                self.assertEqual(limited.exception.code,429)
                self.assertIn('冷却中',json.loads(limited.exception.read())['error'])
                limited.exception.close()
        finally:
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=10)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=10)
            if process.stderr:process.stderr.close()

    def test_valid_noon_receipt_survives_both_local_persistence_crash_windows(self):
        # Exercise failure before record_platform commits and failure after it
        # commits but before job_result can mark the job done.
        for persisted_before_error in (False,True):
            with self.subTest(persisted_before_error=persisted_before_error):
                p=self.approved_product();jid=self.store.add_job(p['id'],'submit',p['revision'])
                parent='PARENT-'+uuid.uuid4().hex[:12]
                with patch.object(self.app,'config',return_value={'noon_ready':True,'submit_enabled':True}), patch('server.Noon') as noon:
                    noon.return_value.attributes.return_value=self.submit_contract()
                    noon.return_value.submit.return_value={'sku_parent':parent,'status':{'status_id':0}}
                    original=self.store.record_platform
                    def fail_at_boundary(*args,**kwargs):
                        if persisted_before_error:original(*args,**kwargs)
                        raise OSError('synthetic local receipt persistence interruption')
                    with patch.object(self.store,'record_platform',side_effect=fail_at_boundary):
                        self.app.run(jid,p,'submit')
                    self.assertEqual(noon.return_value.submit.call_count,1)
                job=next(j for j in self.store.history()['jobs'] if j['id']==jid)
                self.assertEqual(job['status'],'uncertain')
                with self.assertRaisesRegex(Problem,'提交结果待核对'):
                    self.store.add_job(p['id'],'submit',p['revision'])
                platform=self.store.get(p['id']).get('platform') or {}
                self.assertEqual(platform.get('sku_parent'),parent if persisted_before_error else None)
                self.store.reconcile_submit(jid,'found',p['partner_sku'],'Seller Lab 按 SKU 找到对应刊登',parent)
                recovered=self.store.get(p['id'])['platform']
                self.assertEqual(recovered['sku_parent'],parent)
                self.assertFalse(recovered['live_verified'])
                with patch.object(self.app,'config',return_value={'noon_ready':True,'submit_enabled':True}), patch.object(self.app.executor,'submit') as dispatch:
                    with self.assertRaisesRegex(Problem,'当前版本已有平台提交回执'):
                        self.app.queue(p['id'],'submit',p['revision'])
                    dispatch.assert_not_called()

    def test_interrupted_submit_remains_blocked_after_restart_and_revision_change(self):
        p = self.approved_product()
        jid = self.store.add_job(p['id'], 'submit', p['revision'])
        with self.store.connect() as c:
            c.execute("UPDATE jobs SET status='running' WHERE id=?", (jid,))
        self.store.recover_jobs()
        edited = self.store.update(p['id'], {'note': 'edited after restart'}, p['revision'])
        with self.assertRaisesRegex(Problem, '修改商品版本也不能解除拦截'):
            self.store.add_job(p['id'], 'submit', edited['revision'])
        self.assertEqual(self.store.history()['jobs'][0]['status'], 'interrupted')

    def test_real_process_kill_after_remote_acceptance_does_not_replay_on_restart(self):
        """Kill the worker after a local fake Noon accepts the request but before its reply."""
        accepted=threading.Event();release=threading.Event();received=[]
        class Receiver(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                received.append(self.rfile.read(int(self.headers.get('Content-Length','0'))))
                accepted.set();release.wait(15)
                try:self.send_response(200);self.end_headers();self.wfile.write(b'{"accepted":true}')
                except (BrokenPipeError,ConnectionResetError):pass
        receiver=ThreadingHTTPServer(('127.0.0.1',0),Receiver)
        receiver_thread=threading.Thread(target=receiver.serve_forever,daemon=True);receiver_thread.start()
        ready=Path(self.tmp.name)/'restart-ready.json';identity=Path(self.tmp.name)/'worker-identity.json'
        runner_code='''
import json,sys,urllib.request
from pathlib import Path
sys.path.insert(0,sys.argv[3])
import server
from core import now
app=server.App(sys.argv[1]);app.config=lambda:{'noon_ready':True,'submit_enabled':True}
class FakeNoon:
 def __init__(self,on_rate_limited=None):pass
 def attributes(self,category):return {'attributes':[{'attribute_code':k,'is_mandatory':True,'is_localizable':True,'attribute_type':'ATTRIBUTE_TYPE_TEXT'} for k in ('product_title','long_description')]}
 def submit(self,data):
  request=urllib.request.Request(sys.argv[2],data=json.dumps(data).encode(),headers={'Content-Type':'application/json'})
  with urllib.request.urlopen(request,timeout=30) as response:response.read()
  return {'sku_parent':'SHOULD-NOT-BE-SAVED','status':{'status_id':0}}
server.Noon=FakeNoon
pid=app.store.import_rows([{'title_zh':'断电边界测试商品','source_url':'https://supplier.example/crash-test','supplier':'Synthetic QA','facts':'5 pieces'}])['created'][0]
product=app.store.get(pid)
product=app.store.update(pid,{'brand':'QA','category':'qa-category','title_en':'QA Product','description_en':'Synthetic item','title_ar':'منتج','description_ar':'عنصر اختباري','rights_evidence':'Synthetic QA','mode':'NGS','cost_cny':1,'stock':10,'supply_checked_at':now()},product['revision'],{'images':[{'id':'crash-image','source':'source.png','file':'clean.jpg','public_url':'https://images.example/qa.jpg'}]})
product=app.store.update(pid,{'content_verified':True,'images_verified':True,'category_verified':True},product['revision'])
product=app.store.approve(pid,product['revision']);job_id=app.store.add_job(pid,'submit',product['revision'])
Path(sys.argv[4]).write_text(json.dumps({'product_id':pid,'job_id':job_id,'revision':product['revision']}))
app.run(job_id,product,'submit')
'''
        worker=subprocess.Popen([sys.executable,'-c',runner_code,self.tmp.name,
                                 f'http://127.0.0.1:{receiver.server_port}/accept',
                                 str(Path(__file__).resolve().parents[1]),str(identity)],
                                stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,
                                env={k:v for k,v in os.environ.items() if not k.startswith(('NOON_','OPENAI_','TEXT_','IMAGE_HOST_'))})
        server_process=None
        try:
            deadline=time.monotonic()+10
            while not accepted.wait(.05) and time.monotonic()<deadline:
                if worker.poll() is not None:raise AssertionError(f'worker exited before fake Noon acceptance (rc={worker.returncode}) '+worker.stderr.read().decode())
            self.assertTrue(accepted.is_set(),'fake Noon did not receive the submission')
            identity_data=json.loads(identity.read_text());pid=identity_data['product_id'];jid=identity_data['job_id'];revision=identity_data['revision']
            worker.kill();worker.wait(timeout=10)
            release.set()
            ready.unlink(missing_ok=True)
            server_process=subprocess.Popen([sys.executable,str(Path(__file__).resolve().parents[1]/'server.py'),
                '--port','0','--data',self.tmp.name,'--ready-file',str(ready)],
                stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,
                env={k:v for k,v in os.environ.items() if not k.startswith(('NOON_','OPENAI_','TEXT_','IMAGE_HOST_'))})
            deadline=time.monotonic()+10
            while not ready.exists() and time.monotonic()<deadline:
                if server_process.poll() is not None:raise AssertionError(server_process.stderr.read().decode())
                time.sleep(.05)
            self.assertTrue(ready.exists(),'server did not recover after worker termination')
            url=json.loads(ready.read_text())['url']
            with urlopen(url+'/api/state',timeout=10) as response:state=json.load(response)
            with urlopen(url+'/api/history/list?stream=jobs',timeout=10) as response:history=json.load(response)
            job=next(item for item in history['jobs'] if item['id']==jid)
            self.assertEqual(job['status'],'interrupted')
            self.assertEqual(len(received),1,'external endpoint must receive only the original request')
            with self.assertRaises(HTTPError) as blocked:
                urlopen(Request(url+f"/api/products/{pid}/submit",
                    data=json.dumps({'revision':revision}).encode(),
                    headers={'Content-Type':'application/json','X-Workbench-Token':state['token']}),timeout=10)
            self.assertEqual(blocked.exception.code,409)
            time.sleep(.2)
            self.assertEqual(len(received),1,'restart or blocked retry must not automatically replay the write')
        finally:
            release.set();receiver.shutdown();receiver.server_close();receiver_thread.join(timeout=2)
            if worker.poll() is None:worker.kill();worker.wait(timeout=10)
            if worker.stderr:worker.stderr.close()
            if server_process and server_process.poll() is None:
                server_process.terminate()
                try:server_process.wait(timeout=10)
                except subprocess.TimeoutExpired:server_process.kill();server_process.wait(timeout=10)
            if server_process and server_process.stderr:server_process.stderr.close()

    def test_restart_marks_unclaimed_submit_unsent_and_allows_manual_requeue(self):
        p=self.approved_product()
        with patch.object(self.app,'config',return_value={'noon_ready':True,'submit_enabled':True}), \
             patch.object(self.app.executor,'submit') as dispatch:
            first=self.app.queue(p['id'],'submit',p['revision'])['job_id']
            dispatch.assert_called_once()
            self.store.recover_jobs()
            job=next(j for j in self.store.history()['jobs'] if j['id']==first)
            self.assertEqual(job['status'],'failed')
            self.assertIn('尚未开始外部调用',job['message'])
            second=self.app.queue(p['id'],'submit',p['revision'])['job_id']
        self.assertNotEqual(first,second)
        self.assertEqual(self.store.history()['jobs'][0]['status'],'queued')

    def test_executor_shutdown_failure_is_audited_as_not_sent_and_retryable(self):
        p=self.approved_product()
        with patch.object(self.app,'config',return_value={'noon_ready':True,'submit_enabled':True}), \
             patch.object(self.app.executor,'submit',side_effect=RuntimeError('executor stopped')):
            with self.assertRaises(Problem) as error:self.app.queue(p['id'],'submit',p['revision'])
        self.assertEqual(error.exception.status,503)
        job=self.store.history()['jobs'][0]
        self.assertEqual(job['status'],'failed');self.assertIn('尚未开始外部调用',job['message'])
        # A known pre-dispatch failure is safe to arrange again without Seller Lab reconciliation.
        self.store.add_job(p['id'],'submit',p['revision'])

    def test_batch_cancel_before_worker_claim_prevents_noon_dispatch(self):
        p=self.approved_product();batch,result=self.queue_batch_without_worker(p,'cancel-before-claim')
        jid=result['jobs'][0]['job_id']
        cancelled=batch.cancel('cancel-before-claim')
        self.assertEqual(cancelled['cancelled_now'],1)
        self.assertEqual(cancelled['jobs'][0]['status'],'cancelled')
        with patch('server.Noon') as noon:
            self.app.run(jid,p,'submit')
            noon.assert_not_called()
        self.assertEqual(self.store.history()['jobs'][0]['status'],'cancelled')

    def test_batch_cancel_after_worker_claim_does_not_claim_inflight_request_stopped(self):
        p=self.approved_product();batch,result=self.queue_batch_without_worker(p,'cancel-after-claim')
        jid=result['jobs'][0]['job_id']
        with self.store.connect() as c:c.execute("UPDATE jobs SET status='running' WHERE id=?",(jid,))
        cancelled=batch.cancel('cancel-after-claim')
        self.assertEqual(cancelled['cancelled_now'],0)
        self.assertEqual(cancelled['jobs'][0]['status'],'running')
        self.assertEqual(self.store.history()['jobs'][0]['status'],'running')

    def test_manual_absent_reconciliation_is_audited_and_only_unblocks_future_manual_queue(self):
        p = self.approved_product()
        jid = self.store.add_job(p['id'], 'submit', p['revision'])
        self.store.job_result(jid, 'uncertain', 'synthetic timeout after request dispatch')
        with self.assertRaisesRegex(Problem, '店铺 SKU 不匹配'):
            self.store.reconcile_submit(jid, 'absent', 'WRONG-SKU', 'Seller Lab 核对后没有对应刊登')
        with self.assertRaisesRegex(Problem, '至少8个字'):
            self.store.reconcile_submit(jid, 'absent', p['partner_sku'], '未找到')

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        server.app = self.app
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}/api/jobs/reconcile-submit'
            base = {'job_id': jid, 'outcome': 'absent', 'partner_sku': p['partner_sku'],
                    'evidence': 'Seller Lab 按此 SKU 搜索，确认没有对应刊登'}
            denied = Request(url, data=json.dumps(base).encode(), headers={
                'Content-Type': 'application/json', 'X-Workbench-Token': self.app.token})
            with self.assertRaises(HTTPError) as error:urlopen(denied)
            self.assertEqual(error.exception.code, 400)
            error.exception.close()
            accepted = Request(url, data=json.dumps({**base, 'confirmed': True}).encode(), headers={
                'Content-Type': 'application/json', 'X-Workbench-Token': self.app.token})
            response = urlopen(accepted)
            self.assertEqual(response.status, 200)
        finally:
            server.shutdown();server.server_close();thread.join(timeout=2)

        with self.store.connect() as c:
            rec=c.execute('SELECT outcome,evidence FROM submit_reconciliations WHERE job_id=?',(jid,)).fetchone()
            event_count=c.execute("SELECT count(*) FROM events WHERE product_id=? AND action='提交结果人工对账'",(p['id'],)).fetchone()[0]
        self.assertEqual(tuple(rec),('absent',base['evidence']))
        replay=self.store.reconcile_submit(jid,'absent',p['partner_sku'],base['evidence'])
        self.assertTrue(replay['replayed']);self.assertEqual(event_count,1)
        with self.assertRaisesRegex(Problem,'不允许覆盖'):
            self.store.reconcile_submit(jid,'absent',p['partner_sku'],'不同的核对结论证据')
        self.assertIsNone((self.store.get(p['id']).get('platform') or {}).get('live_verified'))
        with patch.object(self.app, 'config', return_value={'noon_ready': True, 'submit_enabled': True}):
            preview=ContentSubmitBatch(self.app).preview({'product_ids':[p['id']]})
        self.assertEqual(preview['rows'][0]['status'],'ready')
        # Resolving a receipt never dispatches a new seller write on its own.
        queued=self.store.add_job(p['id'],'submit',p['revision'])
        self.assertNotEqual(queued,jid)

    def test_found_reconciliation_records_parent_without_claiming_live_status(self):
        p=self.approved_product();jid=self.store.add_job(p['id'],'submit',p['revision'])
        self.store.job_result(jid,'interrupted','interrupted during possible submit')
        out=self.store.reconcile_submit(jid,'found',p['partner_sku'],'Seller Lab 按 SKU 查到对应商品记录','QA-PARENT-1')
        self.assertEqual(out['outcome'],'found')
        saved=self.store.get(p['id'])['platform']
        self.assertEqual(saved['sku_parent'],'QA-PARENT-1')
        self.assertEqual(saved['submitted_revision'],p['revision'])
        self.assertFalse(saved['live_verified'])
        with self.assertRaisesRegex(Problem,'Noon 提交回执'):
            self.store.add_job(p['id'],'submit',p['revision'])
        with self.assertRaisesRegex(Problem,'不允许覆盖'):
            self.store.reconcile_submit(jid,'absent',p['partner_sku'],'又确认没有该商品')


if __name__ == '__main__':
    unittest.main(verbosity=2)
