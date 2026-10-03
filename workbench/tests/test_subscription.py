"""Subscription transport tests use a local fake Codex process, never the account."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Store,Problem
from models import Models,ModelLimit
from codex_subscription import CodexSubscription,RPC,SubscriptionWait,check_limits,limits_view

FAKE=r'''
import json,sys,time,os
mode=os.environ.get('NOON_FAKE_MODE','ok')
content={'title_en':'Black clips, 5 pieces','description_en':'Plastic, 8 cm.','title_ar':'مشابك سوداء','description_ar':'بلاستيك، 5 قطع، 8 سم','warnings':[]}
def send(d):print(json.dumps(d),flush=True)
for line in sys.stdin:
 d=json.loads(line);method=d.get('method');p=d.get('params',{});rid=d.get('id')
 if rid is None:continue
 r={}
 if method=='config/read':r={'config':{'mcp_servers':{'outside_tool':{'command':'do-not-run'}},'model_provider':'openai'}}
 elif method=='account/read':r={'account':{'type':'apiKey' if mode=='apikey' else 'chatgpt','email':'private@example.com','planType':'plus'}}
 elif method=='model/list':r={'data':[{'model':'gpt-6-luna','displayName':'Luna'},{'model':'gpt-6-sol','displayName':'Sol'}]}
 elif method=='account/rateLimits/read':
  if mode=='quota_unknown':r={}
  else:r={'rateLimitsByLimitId':{'codex':{'primary':{'usedPercent':100 if mode=='limited' else 20,'resetsAt':int(time.time())+600,'windowDurationMins':300},'credits':{'balance':'private'}}}}
 elif method=='thread/start':
  assert p['config']['mcp_servers.outside_tool.enabled'] is False
  assert p['ephemeral'] is True and p['sandbox']=='read-only' and p['environments']==[]
  assert 'OPENAI_API_KEY' not in os.environ
  r={'thread':{'id':'thread-qa'},'model':'unexpected' if mode=='model_changed' else p['model']}
 elif method=='turn/start':
  send({'id':rid,'result':{'turn':{'id':'turn-qa','status':'inProgress'}}})
  if mode=='timeout':time.sleep(10);continue
  if mode=='approval':send({'id':987,'method':'item/commandExecution/requestApproval','params':{}});continue
  if mode=='tool':send({'method':'item/started','params':{'threadId':'thread-qa','turnId':'turn-qa','item':{'type':'commandExecution'}}});continue
  props=p['outputSchema']['properties']
  result={'ok':True} if 'ok' in props else {'passed':True,'warnings':[]} if 'passed' in props else content
  send({'method':'thread/tokenUsage/updated','params':{'threadId':'thread-qa','turnId':'turn-qa','tokenUsage':{'last':{'inputTokens':200,'outputTokens':100}}}})
  send({'method':'item/completed','params':{'threadId':'thread-qa','turnId':'turn-qa','item':{'type':'agentMessage','phase':'final_answer','text':'invalid' if mode=='malformed' else json.dumps(result)}}})
  send({'method':'turn/completed','params':{'threadId':'thread-qa','turn':{'id':'turn-qa','status':'failed' if mode=='failed' else 'completed'}}});continue
 send({'id':rid,'result':r})
'''

class SubscriptionTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
  self.binary=self.root/'codex';self.binary.write_text('#!'+sys.executable+'\n'+FAKE);self.binary.chmod(0o700)
  self.find=patch('codex_subscription.executable',return_value=str(self.binary));self.find.start()
  self.m=Models(Store(self.root/'data'))
  self.pid=self.m.save({'name':'Luna subscription','provider':'codex-subscription','model':'gpt-6-luna','enabled':True})['id']
 def tearDown(self):self.m.codex.close();self.find.stop();self.temp.cleanup()
 def test_profile_no_key_or_dollar_and_role_ready(self):
  self.assertFalse(list(self.m.secrets.glob('*.key')))
  self.m.route({'role':'primary','profile_id':self.pid});self.assertTrue(self.m.ready())
  with self.assertRaises(Problem):self.m.save({'name':'Bad','provider':'codex-subscription','model':'gpt-6-luna','api_key':'do-not-save'})
  with self.assertRaises(Problem):self.m.save({**self.m.get(self.pid),'provider':'openai','input_price':'1','output_price':'1','daily_usd':'1','api_key':'key'})
 def test_account_readback_has_no_email_credentials_or_balance(self):
  state=self.m.codex.refresh();encoded=json.dumps(state)
  self.assertTrue(state['logged_in']);self.assertEqual(state['plan'],'plus')
  self.assertNotIn('private',encoded);self.assertNotIn('balance',encoded);self.assertEqual(len(state['models']),2)
 def test_generate_parse_usage_and_replay_without_connection(self):
  with patch.dict(os.environ,{'OPENAI_API_KEY':'never-use-this'}):r=self.m.call(self.pid,{'facts':'5 pieces'},'one')
  self.assertEqual(r['content']['title_en'],'Black clips, 5 pieces');self.assertIsNone(r['estimated_micro'])
  self.assertEqual(r['billing'],'subscription');self.assertEqual(r['usage']['prompt_tokens'],200)
  with patch.object(self.m.codex,'session',side_effect=AssertionError('must not connect')):
   self.assertEqual(r,self.m.call(self.pid,{'facts':'5 pieces'},'one'))
   with self.assertRaises(Problem):self.m.call(self.pid,{'facts':'6 pieces'},'one')
  call=self.m.state()['calls'][0];self.assertEqual(call['billing'],'subscription');self.assertEqual(call['charged_micro'],0)
 def test_quota_wait_before_ledger_and_resume(self):
  with patch.dict(os.environ,{'NOON_FAKE_MODE':'limited'}):
   with self.assertRaises(ModelLimit) as e:self.m.call(self.pid,{},'quota')
  self.assertIsNotNone(e.exception.retry_at);self.assertEqual(self.m.state()['calls'],[])
  self.assertEqual(self.m.call(self.pid,{},'quota')['billing'],'subscription')
 def test_unknown_quota_and_api_auth_do_not_dispatch(self):
  for mode in ('quota_unknown','apikey'):
   with patch.dict(os.environ,{'NOON_FAKE_MODE':mode}):
    with self.assertRaises(Problem):self.m.call(self.pid,{},mode)
  self.assertEqual(self.m.state()['calls'],[])
 def test_interrupted_malformed_changed_model_no_silent_replay(self):
  for mode in ('failed','malformed','model_changed','tool','approval'):
   with patch.dict(os.environ,{'NOON_FAKE_MODE':mode}):
    with self.assertRaises(Problem):self.m.call(self.pid,{},mode)
   self.assertEqual(self.m.state()['calls'][0]['status'],'uncertain')
   with self.assertRaises(Problem):self.m.call(self.pid,{},mode)
 def test_single_connection_rejects_overlap(self):
  with self.m.codex.session():
   with self.assertRaises(SubscriptionWait):self.m.codex.refresh()
   with self.assertRaises(ModelLimit):self.m.call(self.pid,{},'busy')
  self.assertEqual(self.m.state()['calls'],[])
 def test_timeout_closes_child(self):
  with patch.dict(os.environ,{'NOON_FAKE_MODE':'timeout'}):
   with self.m.codex.session() as (rpc,cwd):
    with self.assertRaises(Problem):self.m.codex.generate(rpc,cwd,'gpt-6-luna','JSON','{}','probe',timeout=.1)
   self.assertIsNotNone(rpc.proc.poll())
 def test_daily_limit_is_additional_not_dollars(self):
  self.m.save({**self.m.get(self.pid),'daily_calls':1});self.m.call(self.pid,{},'first')
  with self.assertRaises(ModelLimit):self.m.call(self.pid,{},'second')
  self.assertEqual(len(self.m.state()['calls']),1)
 def test_quota_mapping_unknown_reset_and_multiwindow(self):
  t=int(time.time());pools=limits_view({'rateLimitsByLimitId':{'gpt-6-sol':{'primary':{'usedPercent':100,'resetsAt':t+60}},'codex':{'primary':{'usedPercent':2,'resetsAt':t+60}}}})
  check_limits(pools,'gpt-6-luna')
  with self.assertRaises(SubscriptionWait):check_limits(pools,'gpt-6-sol')
  pools=limits_view({'rateLimits':{'primary':{'usedPercent':100,'resetsAt':None}}})
  with self.assertRaises(Problem):check_limits(pools,'gpt-6-luna')
  with self.assertRaises(Problem):check_limits([{'id':'unmapped','windows':[]}],'gpt-6-luna')
 def test_shutdown_interrupts_inflight_connection(self):
  entered=threading.Event();errors=[];children=[]
  def work():
   try:
    with self.m.codex.session() as (rpc,cwd):
     children.append(rpc.proc);entered.set()
     self.m.codex.generate(rpc,cwd,'gpt-6-luna','JSON','{}','probe',timeout=10)
   except Problem as e:errors.append(e)
  with patch.dict(os.environ,{'NOON_FAKE_MODE':'timeout'}):
   worker=threading.Thread(target=work);worker.start();self.assertTrue(entered.wait(3));time.sleep(.1)
   self.m.codex.close();worker.join(5)
  self.assertFalse(worker.is_alive());self.assertTrue(errors);self.assertIsNotNone(children[0].poll())
  with self.assertRaises(Problem):self.m.codex.refresh()
 def test_review_and_probe_schema(self):
  self.assertTrue(self.m.call(self.pid,{},'review','review')['passed'])
  self.assertTrue(self.m.call(self.pid,{},'probe','probe')['ok'])

if __name__=='__main__':unittest.main()
