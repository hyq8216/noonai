import base64
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
import uuid
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image
from core import Store,Problem,now,normalize_image
from server import App
from models import Models

CONTENT={'title_en':'Black Clips, 5 Pieces','description_en':'Five black clips.','title_ar':'مشابك سوداء','description_ar':'خمسة مشابك سوداء','warnings':[]}
def response(data=CONTENT,usage=True):
    return {'choices':[{'message':{'content':json.dumps(data,ensure_ascii=False)},'finish_reason':'stop'}],**({'usage':{'prompt_tokens':100,'completion_tokens':50}} if usage else {})}

class ModelsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name);self.models=Models(self.store)
    def tearDown(self):self.tmp.cleanup()
    def profile(self,**extra):
        return self.models.save({'name':'QA model','provider':'openai','model':'gpt-6-luna','enabled':True,'api_key':'test-secret-only','input_price':'.1','output_price':'.5','daily_usd':'1',**extra})['id']
    def test_secret_permissions_no_readback_and_endpoint_change(self):
        pid=self.profile();p=self.models.get(pid)
        raw=json.dumps(self.models.state());self.assertNotIn('test-secret-only',raw)
        self.assertEqual((self.models.secrets/(pid+'.key')).stat().st_mode&0o777,0o600)
        with self.assertRaises(Problem):self.models.save({**p,'provider':'custom','base_url':'https://example.com/v1'})
        self.models.save({**p,'enabled':False});self.assertFalse(self.models.get(pid)['enabled'])
        with self.assertRaises(Problem):self.models.save({**p,'enabled':True})
    def test_replay_and_usage_budget(self):
        pid=self.profile(daily_calls=1)
        with patch('models.request_json',return_value=response()) as req:
            one=self.models.call(pid,{'facts':'5 pieces'},'same');two=self.models.call(pid,{'facts':'5 pieces'},'same')
            self.assertEqual(one,two);self.assertEqual(req.call_count,1)
            self.assertEqual(one['estimated_micro'],35)
            with self.assertRaises(Problem):self.models.call(pid,{'facts':'6 pieces'},'same')
            with self.assertRaises(Problem):self.models.call(pid,{'facts':'5 pieces'},'next')
        self.assertEqual(self.models.state()['today'][0]['calls'],1)
    def test_failed_response_keeps_usage_and_no_automatic_retry(self):
        pid=self.profile()
        with patch('models.request_json',return_value=response({'title_en':'broken'})) as req:
            with self.assertRaises(Problem):self.models.call(pid,{},'bad')
            with self.assertRaises(Problem):self.models.call(pid,{},'bad')
            self.assertEqual(req.call_count,1)
        c=self.models.state()['calls'][0];self.assertEqual(c['status'],'failed');self.assertEqual(c['charged_micro'],35)
    def test_unknown_usage_keeps_reservation_and_redacts_errors(self):
        pid=self.profile()
        with patch('models.request_json',side_effect=Problem('failed test-secret-only',502)):
            with self.assertRaises(Problem) as e:self.models.call(pid,{},'fail')
        self.assertNotIn('test-secret-only',str(e.exception));c=self.models.state()['calls'][0]
        self.assertEqual(c['charged_micro'],c['reserved_micro']);self.assertGreater(c['charged_micro'],0)
    def test_budget_and_rate_reject_before_network(self):
        pid=self.profile(daily_usd='.000001')
        with patch('models.request_json') as req:
            with self.assertRaises(Problem):self.models.call(pid,{},'overbudget')
            req.assert_not_called()
        p=self.models.get(pid);self.models.save({**p,'daily_usd':'1','rpm':1})
        with patch('models.request_json',return_value=response()) as req:
            self.models.call(pid,{},'first')
            with self.assertRaises(Problem):self.models.call(pid,{},'rate-limited')
            self.assertEqual(req.call_count,1)
    def test_concurrent_replay_and_recovery(self):
        pid=self.profile();entered=threading.Event();release=threading.Event();errors=[]
        def slow(*args):entered.set();release.wait(3);return response()
        def call():
            try:self.models.call(pid,{},'concurrent')
            except Exception as e:errors.append(e)
        with patch('models.request_json',side_effect=slow) as req:
            t=threading.Thread(target=call);t.start();self.assertTrue(entered.wait(2))
            with self.assertRaises(Problem):self.models.call(pid,{},'concurrent')
            release.set();t.join();self.assertFalse(errors);self.assertEqual(req.call_count,1)
        with self.store.connect() as c:c.execute("UPDATE model_calls SET status='calling'")
        self.models.recover();self.assertEqual(self.models.state()['calls'][0]['status'],'uncertain')
    def test_minimax_parameters_and_reasoning_separation(self):
        pid=self.profile(provider='minimax',model='MiniMax-M3')
        r=response();r['choices'][0]['message']['content']='<think>not listing text</think>'+r['choices'][0]['message']['content']
        with patch('models.request_json',return_value=r) as req:
            result=self.models.call(pid,{},'m3');body=req.call_args.args[1]
            self.assertEqual(body['thinking'],{'type':'disabled'});self.assertTrue(body['reasoning_split']);self.assertNotIn('reasoning_effort',body)
        self.assertEqual(result['content']['title_en'],CONTENT['title_en'])

class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.app=App(self.tmp.name);self.store=self.app.store;self.auto=self.app.automation;self.app.media.start();__import__('workflow_clock').install_clock(self.app.automation)
    def tearDown(self):
        self.auto.close();self.app.media.close();self.app.executor.shutdown(wait=True);self.tmp.cleanup()
    def product(self,**extra):
        p=self.store.get(self.store.import_rows([{'title_zh':'黑色夹子','source_url':'https://detail.1688.com/offer/'+str(uuid.uuid4().int)+'.html','supplier':'测试供应商','facts':'黑色，5件装。',**extra}])['created'][0]);return p
    def model(self,role='primary'):
        pid=self.app.models.save({'name':'QA '+role,'provider':'openai','model':'gpt-6-luna','api_key':'test-only','enabled':True,'input_price':'.1','output_price':'.5','daily_usd':'1'})['id'];self.app.models.route({'role':role,'profile_id':pid});return pid
    def create(self,p,plan=None,**extra):
        return self.auto.create({'request_id':uuid.uuid4().hex,'name':'QA flow','product_ids':[p['id']],'plan':plan or {},**extra})['id']
    def item(self,rid):return next(i for i in self.auto.state()['items'] if i['run_id']==rid)
    def tick(self,n=1):
        for _ in range(n):self.auto.tick()
    def batch(self,count=3):
        return self.auto.create({'request_id':uuid.uuid4().hex,'name':'Claim QA','product_ids':[self.product()['id'] for _ in range(count)]})['id']
    def test_only_current_item_claimed_and_stop_preserves_queue(self):
        rid=self.batch();seen=[];original=self.auto.process
        def process(item):
            statuses=[i['status'] for i in self.auto.state()['items'] if i['run_id']==rid]
            seen.append(statuses);original(item);self.auto.stop.set()
        with patch.object(self.auto,'process',side_effect=process):self.tick()
        self.assertEqual(len(seen),1);self.assertEqual(seen[0].count('processing'),1);self.assertEqual(seen[0].count('queued'),2)
        items=[i for i in self.auto.state()['items'] if i['run_id']==rid]
        self.assertEqual(sorted(i['step'] for i in items),[0,0,1]);self.assertTrue(all(i['status']=='queued' for i in items))
    def test_pause_during_execution_leaves_later_items_untouched(self):
        rid=self.batch();original=self.auto.process
        def process(item):
            self.auto.control({'action':'pause','run_id':rid});original(item)
        with patch.object(self.auto,'process',side_effect=process) as run:self.tick();self.assertEqual(run.call_count,1)
        items=[i for i in self.auto.state()['items'] if i['run_id']==rid]
        self.assertEqual(sorted(i['step'] for i in items),[0,0,1]);self.assertTrue(all(i['status']=='queued' for i in items))
        self.auto.control({'action':'resume','run_id':rid});self.tick()
        self.assertEqual(sorted(i['step'] for i in self.auto.state()['items'] if i['run_id']==rid),[1,1,2])
    def test_cancellation_during_execution_never_claims_remainder(self):
        rid=self.batch();original=self.auto.process
        def process(item):
            self.auto.control({'action':'cancel_run','run_id':rid});original(item)
        with patch.object(self.auto,'process',side_effect=process) as run:self.tick();self.assertEqual(run.call_count,1)
        self.assertTrue(all(i['status']=='cancelled' and i['step']==0 for i in self.auto.state()['items'] if i['run_id']==rid))
    def test_restart_marks_only_actual_inflight_item(self):
        rid=self.batch()
        def crash(item):raise KeyboardInterrupt('simulated process loss')
        with patch.object(self.auto,'process',side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):self.tick()
        with patch.object(self.auto,'loop',return_value=None):self.auto.start();self.auto.thread.join()
        items=[i for i in self.auto.state()['items'] if i['run_id']==rid]
        self.assertEqual(sum(i['status']=='attention' for i in items),1);self.assertEqual(sum(i['status']=='queued' for i in items),2)
    def test_create_atomic_dedupe_and_schedule_pause(self):
        p=self.product();future=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat();b={'name':'QA','request_id':'same','product_ids':[p['id']],'run_at':future}
        rid=self.auto.create(b)['id'];self.assertEqual(rid,self.auto.create(b)['id']);self.tick();self.assertEqual(self.item(rid)['status'],'queued')
        with self.assertRaises(Problem):self.auto.create({**b,'name':'changed'})
        with self.assertRaises(Problem):self.create(p)
        with self.store.connect() as c:c.execute('UPDATE automation_runs SET run_at=?',(now(),))
        self.auto.control({'action':'pause','run_id':rid});self.tick();self.assertEqual(self.item(rid)['step'],0)
        self.auto.control({'action':'resume','run_id':rid});self.tick();self.assertEqual(self.item(rid)['step'],1)
    def test_missing_source_isolated_and_current_revision_retry(self):
        bad=self.product();good=self.product();rid=self.auto.create({'request_id':'batch','name':'Batch','product_ids':[bad['id'],good['id']]})['id'];bad=self.store.update(bad['id'],{'facts':''},bad['revision']);self.tick()
        items=self.auto.state()['items'];b=next(i for i in items if i['product_id']==bad['id']);g=next(i for i in items if i['product_id']==good['id'])
        self.assertEqual(b['status'],'attention');self.assertEqual(g['step'],1)
        fixed=self.store.update(bad['id'],{'facts':'5 pieces'},bad['revision'])
        with self.assertRaises(Problem):self.auto.control({'action':'retry','item_id':b['id'],'revision':bad['revision']})
        self.auto.control({'action':'retry','item_id':b['id'],'revision':fixed['revision']});self.tick();self.assertEqual(next(i for i in self.auto.state()['items'] if i['id']==b['id'])['step'],1)
    def test_translation_review_and_changed_product_do_not_overwrite(self):
        self.model();self.model('review');p=self.product();rid=self.create(p,{'translate':True,'review':True});self.tick()
        with patch('models.request_json',return_value=response()):self.tick()
        self.assertEqual(self.store.get(p['id'])['title_en'],CONTENT['title_en'])
        with patch('models.request_json',return_value=response({'passed':False,'warnings':['需要核对数量']})):self.tick()
        i=self.item(rid);self.assertEqual(i['status'],'attention');self.assertEqual(i['data']['review']['warnings'],['需要核对数量']);self.assertFalse(self.store.get(p['id'])['reviewed'])
        p2=self.product();r2=self.create(p2,{'translate':True});self.tick();new=self.store.update(p2['id'],{'facts':'6 pieces'},p2['revision'])
        with patch('models.request_json') as req:self.tick();req.assert_not_called()
        self.assertEqual(self.item(r2)['status'],'attention');self.assertEqual(self.store.get(p2['id'])['facts'],'6 pieces')
    def test_inflight_model_result_does_not_override_concurrent_edit(self):
        self.model();p=self.product();rid=self.create(p,{'translate':True});self.tick()
        def changed(*args):self.store.update(p['id'],{'facts':'6 pieces'},p['revision']);return response()
        with patch('models.request_json',side_effect=changed):self.tick()
        self.assertEqual(self.item(rid)['status'],'attention');current=self.store.get(p['id']);self.assertEqual(current['facts'],'6 pieces');self.assertEqual(current['title_en'],'')
    def test_fallback_is_explicit_and_audited(self):
        self.model('primary');fallback=self.model('fallback');p=self.product();rid=self.create(p,{'translate':True});self.tick()
        with patch('models.request_json',side_effect=Problem('service unavailable',502)):self.tick()
        i=self.item(rid);self.assertEqual(i['status'],'attention');self.auto.control({'action':'retry_fallback','item_id':i['id'],'revision':p['revision']})
        with patch('models.request_json',return_value=response()):self.tick()
        self.assertEqual(self.item(rid)['step'],2);self.assertEqual(self.app.models.state()['calls'][0]['profile_id'],fallback)
    def test_rate_wait_resumes_without_duplicate_call(self):
        pid=self.model();profile=self.app.models.get(pid);self.app.models.save({**profile,'rpm':1})
        with patch('models.request_json',return_value=response()):self.app.models.call(pid,{},'prior')
        p=self.product();rid=self.create(p,{'translate':True});self.tick()
        with patch('models.request_json') as req:self.tick();req.assert_not_called()
        i=self.item(rid);self.assertEqual(i['status'],'waiting');self.assertIn('retry_at',i['data'])
        self.tick();self.assertEqual(len(self.app.models.state()['calls']),1)
        with self.store.connect() as c:
            c.execute("UPDATE model_calls SET created_at=?",((datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat(),))
            c.execute("UPDATE automation_items SET data=json_remove(data,'$.retry_at') WHERE id=?",(i['id'],))
        with patch('models.request_json',return_value=response()) as req:self.tick();self.assertEqual(req.call_count,1)
        self.assertEqual(self.item(rid)['step'],2)
    def test_disabled_route_never_falls_back_to_legacy_credentials(self):
        pid=self.model();p=self.app.models.get(pid);self.app.models.save({**p,'enabled':False})
        with patch.dict(os.environ,{'TEXT_API_KEY':'legacy-test','TEXT_MODEL':'legacy'}):
            self.assertFalse(self.app.config()['text_ready'])
            product=self.product()
            with patch('server.translate') as legacy:
                with self.assertRaises(Problem):self.app.queue(product['id'],'translate',product['revision'])
                legacy.assert_not_called()
    def test_real_image_workflow_and_approval_boundary(self):
        p=self.product(rights_evidence='synthetic fixture');b=io.BytesIO();Image.new('RGB',(800,800),'blue').save(b,'PNG');im=normalize_image(self.store,base64.b64encode(b.getvalue()).decode(),'square');p=self.store.update(p['id'],{},p['revision'],{'images':[im]})
        rid=self.create(p,{'image_template':'portrait'});self.tick(2)
        deadline=time.monotonic()+8
        while time.monotonic()<deadline and self.item(rid)['step']<2:self.tick();time.sleep(.05)
        current=self.store.get(p['id']);self.assertEqual(current['images'][0]['size'],[1600,2000]);self.assertFalse(current['images_verified']);self.tick(2);self.assertEqual(self.item(rid)['status'],'approval')
        before=len(self.auto.state()['events']);self.tick(3);self.assertEqual(len(self.auto.state()['events']),before)
    def test_current_approval_advances_but_never_auto_approves(self):
        p=self.product(brand='QA',category='test',rights_evidence='QA',mode='NGS',cost_cny=1,stock=10,supply_checked_at=now(),**{k:v for k,v in CONTENT.items() if k!='warnings'})
        p=self.store.update(p['id'],{'content_verified':True,'images_verified':True,'category_verified':True},p['revision'],{'images':[{'id':'qa','source':'qa.png','file':'qa.jpg','public_url':'https://example.com/image.jpg'}]})
        rid=self.create(p);self.tick(3);self.assertEqual(self.item(rid)['status'],'approval');self.assertFalse(self.store.get(p['id'])['reviewed'])
        self.store.approve(p['id'],p['revision']);self.tick(2);self.assertEqual(self.item(rid)['status'],'done')
    def test_cancel_inflight_does_not_reactivate_and_submit_never_retries(self):
        self.model();p=self.product();rid=self.create(p,{'translate':True});self.tick()
        def cancelled(*args):self.auto.control({'action':'cancel_run','run_id':rid});return response()
        with patch('models.request_json',side_effect=cancelled):self.tick()
        self.assertEqual(self.item(rid)['status'],'cancelled')
        with self.store.connect() as c:
            plan=json.loads(c.execute('SELECT plan FROM automation_runs WHERE id=?',(rid,)).fetchone()[0]);plan['steps'].insert(-1,'submit');plan['submit']=True
            c.execute('UPDATE automation_runs SET plan=? WHERE id=?',(json.dumps(plan),rid))
            c.execute("UPDATE automation_items SET status='attention',step=4 WHERE run_id=?",(rid,))
        i=self.item(rid)
        with self.assertRaises(Problem):self.auto.control({'action':'retry','item_id':i['id'],'revision':self.store.get(p['id'])['revision']})

if __name__=='__main__':unittest.main()
