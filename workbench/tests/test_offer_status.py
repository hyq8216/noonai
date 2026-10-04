import copy,json,unittest
from io import BytesIO
from unittest.mock import patch,Mock
from urllib.error import HTTPError
import test_video_batch as fixtures
from core import Problem
from connectors import Noon,request_json,RateLimited
from offer_status import validate,summarize
from platform_batch import PlatformBatch

def response(sku='SKU'):
 return {'partner_sku':sku,'sku':'NOON-123','title':'Test','brand':'QA','offers':[{'offer_code':'O1','country_code':'sa','business_model':'noon','price':{'amount':10,'currency':'SAR'},'is_active':True,'active_net_stock':3,'live_status':True,'offer_issues':[]}]}
class OfferTests(unittest.TestCase):
 setUp=fixtures.VideoBatchTests.setUp
 tearDown=fixtures.VideoBatchTests.tearDown
 def product(self):return self.s.get(self.s.import_rows([{'title_zh':'报价测试'}])['created'][0])
 def test_visible_is_separate_from_activation(self):
  raw=response();r={'offer_readback':{'response':raw,'local_revision':1,'checked_at':'test'}};self.assertEqual(summarize(r,'SKU',1)['group'],'visible');raw['offers'][0]['live_status']=False;self.assertEqual(summarize(r,'SKU',1)['group'],'not_live')
 def test_ambiguous_unknown_and_missing_market(self):
  raw=response();raw['offers'][0]['active_net_stock']=0;r={'offer_readback':{'response':raw,'local_revision':1}};self.assertEqual(summarize(r,'SKU',1)['group'],'unknown');raw['offers'][0]['country_code']='ae';self.assertEqual(summarize(r,'SKU',1)['group'],'missing')
 def test_stale_offer_readback_is_not_counted_as_visible(self):
  r={'offer_readback':{'response':response(),'local_revision':1}}
  result=summarize(r,'SKU',2)
  self.assertEqual(result['group'],'unknown')
  self.assertEqual(result['offers'][0]['group'],'unknown')
  self.assertEqual(result['offers'][0]['label'],'报价回读已过期')
  self.assertIn('本地资料已有变化',result['notes'][0])
 def test_live_status_with_zero_price_is_not_effectively_sellable(self):
  raw=response();raw['offers'][0]['price']['amount']=0;r={'offer_readback':{'response':raw,'local_revision':1}}
  self.assertEqual(summarize(r,'SKU',1)['group'],'unknown')
 def test_bad_values_and_duplicate_offers_rejected(self):
  for k,v in [('live_status','true'),('is_active',1),('active_net_stock',True),('offer_issues',{}),('price',{'amount':float('nan'),'currency':'SAR'})]:
   raw=response();raw['offers'][0][k]=v
   with self.assertRaises(Problem):validate(raw,'SKU')
  raw=response();raw['offers']*=2
  with self.assertRaises(Problem):validate(raw,'SKU')
  with self.assertRaises(Problem):validate(response(),'OTHER')
 def test_bad_readback_preserves_previous_and_content(self):
  p=self.product();self.s.record_platform(p['id'],{'sku_parent':'P','content_response':{'test':True}});self.s.record_offer(p['id'],p['partner_sku'],response(p['partner_sku']),p['revision']);previous=self.s.get(p['id'])['platform']
  with self.assertRaises(Problem):self.s.record_offer(p['id'],p['partner_sku'],response('wrong'),p['revision'])
  self.assertEqual(self.s.get(p['id'])['platform'],previous);self.assertEqual(self.s.get(p['id'])['revision'],p['revision'])
 def test_get_method_and_encoded_sku(self):
  n=Noon.__new__(Noon);n.creds={'project_code':'qa'};n.opener=Mock()
  with patch('connectors.request_json',return_value={}) as req:n.offers('A/B ?');self.assertTrue(req.call_args.args[0].endswith('A%2FB%20%3F'));self.assertEqual(req.call_args.kwargs['method'],'GET')
  opener=Mock();result=Mock();result.__enter__=Mock(return_value=result);result.__exit__=Mock();result.read.return_value=b'{}';opener.open.return_value=result;request_json('https://example.test',None,opener=opener,method='GET');req=opener.open.call_args.args[0];self.assertEqual(req.get_method(),'GET');self.assertIsNone(req.data)
 def test_429_exposes_bounded_retry_metadata_without_retry_or_body(self):
  opener=Mock();opener.open.side_effect=HTTPError('https://example.test',429,'Too Many Requests',{'X-Ratelimit-Retry-After':'37','X-Request-Id':'req_123-abc'},BytesIO(b'secret response body'))
  with self.assertRaises(RateLimited) as caught:request_json('https://example.test',{},opener=opener)
  self.assertEqual(caught.exception.status,429);self.assertEqual(caught.exception.retry_after_seconds,37);self.assertEqual(caught.exception.request_id,'req_123-abc')
  self.assertIn('37 秒',str(caught.exception));self.assertNotIn('secret response body',str(caught.exception));self.assertEqual(opener.open.call_count,1)
  opener.open.side_effect=HTTPError('https://example.test',429,'Too Many Requests',{'X-Ratelimit-Retry-After':'9999999','X-Request-Id':'secret token'},BytesIO(b''))
  with self.assertRaises(RateLimited) as invalid:request_json('https://example.test',{},opener=opener)
  self.assertIsNone(invalid.exception.retry_after_seconds);self.assertIsNone(invalid.exception.request_id);self.assertEqual(opener.open.call_count,2)
 def test_noon_client_notifies_owner_on_429(self):
  n=Noon.__new__(Noon);n.creds={'project_code':'qa'};n.opener=Mock();n.on_rate_limited=Mock();limited=RateLimited(12,'req-qa')
  with patch('connectors.request_json',side_effect=limited) as req:
   with self.assertRaises(RateLimited):n.offers('SKU')
  self.assertEqual(req.call_count,1);n.on_rate_limited.assert_called_once_with(limited)
 def test_batch_offer_mode_no_parent_replay_and_cancel(self):
  p=self.product();batch=PlatformBatch(self.app,'offers');b={'product_ids':[p['id']],'request_id':'offer','confirmed':True}
  with patch.object(self.app,'config',return_value={'noon_ready':True}),patch.object(self.app.executor,'submit') as submit:
   pre=batch.preview(b);self.assertEqual(pre['ready'],1);b['preview_token']=pre['token'];r=batch.apply(b);self.assertEqual(batch.apply(b)['jobs'],r['jobs']);self.assertEqual(submit.call_count,1)
  self.assertEqual(PlatformBatch(self.app).latest()['kind'],'offers');batch.cancel('offer');self.assertEqual(batch.status('offer')['counts']['cancelled'],1)
 def test_actual_job_saves_offer_only(self):
  p=self.product();jid=self.s.add_job(p['id'],'offers',p['revision'])
  with patch('server.Noon') as n:n.return_value.offers.return_value=response(p['partner_sku']);self.app.run(jid,p,'offers');n.return_value.offers.assert_called_once_with(p['partner_sku']);n.return_value.submit.assert_not_called()
  saved=self.s.get(p['id']);self.assertEqual(saved['offer_summary']['group'],'visible');self.assertEqual(saved['stock'],p['stock'])
