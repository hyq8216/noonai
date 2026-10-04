import base64,io,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch,Mock
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import App
from core import Problem,normalize_image
from image_host import verify_public,PublicObjectMissing

class ImageHostTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.app=App(Path(self.tmp.name));self.h=self.app.image_host;self.s=self.app.store;__import__('workflow_clock').install_clock(self.app.automation)
  self.config={'endpoint':'https://s3.example.test','region':'test-1','bucket':'test-bucket','prefix':'noon-images','public_base':'https://cdn.example.test','addressing_style':'path','access_key':'TESTACCESS','secret_key':'TESTSECRET'}
  self.pid=self.s.import_rows([{'title_zh':'测试商品','facts':'red card'}])['created'][0]
  buf=io.BytesIO();Image.new('RGB',(1600,1600),'red').save(buf,format='PNG');im=normalize_image(self.s,base64.b64encode(buf.getvalue()).decode(),'square')
  self.s.update(self.pid,{},1,{'images':[im],'images_verified':True});self.h.save(self.config)
 def tearDown(self):
  self.app.models.codex.close();self.app.automation.close();self.app.visual_checks.close();self.app.visuals.close();self.app.media.close();self.app.executor.shutdown();self.tmp.cleanup()
 def queued(self):
  b={'product_ids':[self.pid],'request_id':'test','confirmed':True};pre=self.h.preview(b);b['preview_token']=pre['token']
  with patch.object(self.app.executor,'submit') as submit:r=self.h.apply(b)
  return b,r,submit.call_args.args[2]
 def test_config_private_and_target_change(self):
  self.assertNotIn('TESTSECRET',json.dumps(self.h.state()));self.assertEqual(self.h.path.stat().st_mode&0o777,0o600)
  c={**self.config,'revision':self.h.state()['revision'],'secret_key':'','access_key':''};self.h.save(c)
  c.update(revision=self.h.state()['revision'],endpoint='https://different.test')
  with self.assertRaises(Problem):self.h.save(c)
 def test_preflight_missing_confirmation_and_hash_changes(self):
  p=self.s.get(self.pid);self.s.update(self.pid,{},p['revision'],{'images_verified':False});self.assertEqual(self.h.preview({'product_ids':[self.pid]})['ready'],0)
  self.s.update(self.pid,{},self.s.get(self.pid)['revision'],{'images_verified':True});b={'product_ids':[self.pid],'request_id':'x','confirmed':True};b['preview_token']=self.h.preview(b)['token'];p=self.s.get(self.pid);(self.s.assets/p['images'][0]['file']).write_bytes(b'changed')
  with self.assertRaises(Problem):self.h.apply(b)
 def test_idempotent_cancel_and_frozen_config(self):
  b,r,p=self.queued()
  self.assertEqual(self.h.apply(b),r)
  with self.assertRaises(Problem):self.h.save({**self.config,'revision':self.h.state()['revision']})
  self.h.cancel({'request_id':'test'})
  with patch.object(self.h,'publish') as pub:self.app.run(r['jobs'][0]['job_id'],p,'image-host');pub.assert_not_called()
  self.assertEqual(self.h.status('test')['jobs'][0]['status'],'cancelled')
 def test_upload_verify_and_reapproval(self):
  b,r,p=self.queued();client=Mock()
  with patch.object(self.h,'client',return_value=client),patch('image_host.verify_public',side_effect=[PublicObjectMissing('missing',404),None]) as verify:out=self.h.publish(p)
  client.put_object.assert_called_once();self.assertEqual(verify.call_count,2);kw=client.put_object.call_args.kwargs;self.assertNotIn('ACL',kw);self.assertIn('ContentMD5',kw)
  saved=self.s.get(self.pid);self.assertTrue(saved['images'][0]['public_url'].startswith('https://cdn.example.test/noon-images/'));self.assertFalse(saved['images_verified']);self.assertFalse(saved['reviewed']);self.assertEqual(len(out['images']),1)
 def test_existing_identical_object_skips_upload(self):
  _,_,p=self.queued();client=Mock()
  with patch.object(self.h,'client',return_value=client),patch('image_host.verify_public'):self.h.publish(p)
  client.put_object.assert_not_called()
 def test_bad_public_result_preserves_product(self):
  _,_,p=self.queued();old=self.s.get(self.pid);client=Mock()
  with patch.object(self.h,'client',return_value=client),patch('image_host.verify_public',side_effect=Problem('mismatch')):
   with self.assertRaises(Problem):self.h.publish(p)
  client.put_object.assert_not_called()
  self.assertEqual(self.s.get(self.pid)['revision'],old['revision']);self.assertFalse(self.s.get(self.pid)['images'][0]['public_url'])
 def test_unknown_or_conflicting_public_object_never_triggers_put(self):
  _,_,p=self.queued();client=Mock()
  for error in (Problem('public read timeout',502),Problem('HTTP 403',502),Problem('public bytes differ',502)):
   with self.subTest(error=str(error)),patch.object(self.h,'client',return_value=client),patch('image_host.verify_public',side_effect=error):
    with self.assertRaises(Problem):self.h.publish(p)
  client.put_object.assert_not_called()
 def test_inflight_edit_not_overwritten(self):
  _,_,p=self.queued()
  def edit(*args):self.s.update(self.pid,{'title_zh':'已修改'},p['revision'])
  with patch.object(self.h,'client',return_value=Mock()),patch('image_host.verify_public',side_effect=edit):
   with self.assertRaises(Problem):self.h.publish(p)
  self.assertEqual(self.s.get(self.pid)['title_zh'],'已修改');self.assertFalse(self.s.get(self.pid)['images'][0]['public_url'])
 def test_public_requires_exact_bytes_image_and_200(self):
  response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock();response.status=200;response.headers={'Content-Type':'image/jpeg'};response.read.return_value=b'photo'
  with patch('image_host.build_opener') as opener:
   opener.return_value.open.return_value=response;verify_public('https://example.test/a',b'photo')
   response.read.return_value=b'wrong'
   with self.assertRaises(Problem):verify_public('https://example.test/a',b'photo')
   response.read.return_value=b'photo';response.headers={'Content-Type':'text/html'}
   with self.assertRaises(Problem):verify_public('https://example.test/a',b'photo')
 def test_public_http_404_is_the_only_upload_permitting_readback_result(self):
  from urllib.error import HTTPError
  with patch('image_host.build_opener') as opener:
   opener.return_value.open.side_effect=HTTPError('https://example.test/a',404,'Not Found',{},None)
   with self.assertRaises(PublicObjectMissing):verify_public('https://example.test/a',b'photo')
   opener.return_value.open.side_effect=HTTPError('https://example.test/a',403,'Forbidden',{},None)
   with self.assertRaisesRegex(Problem,'HTTP 403') as forbidden:verify_public('https://example.test/a',b'photo')
   self.assertNotIsInstance(forbidden.exception,PublicObjectMissing)
 def test_cancel_requires_explicit_batch(self):
  self.queued()
  with self.assertRaises(Problem):self.h.cancel({})
  self.assertEqual(self.h.status('test')['jobs'][0]['status'],'queued')
 def test_real_sdk_constructed_without_network(self):
  client=self.h.client(self.h.config());self.assertEqual(client.meta.endpoint_url,self.config['endpoint']);client.close()
