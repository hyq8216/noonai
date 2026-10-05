import base64,hashlib,io,ipaddress,json,os,sys,tempfile,threading,unittest
from datetime import datetime,timezone,timedelta
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch,Mock
from PIL import Image
from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import App
from core import Problem,normalize_image
from image_host import verify_public

class ImageHostTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.app=App(Path(self.tmp.name));self.h=self.app.image_host;self.s=self.app.store
  self.scheduler_time=[datetime.now(timezone.utc)];auto=self.app.automation;auto.clock=lambda:self.scheduler_time[0];tick=auto.tick
  def advance_tick():self.scheduler_time[0]+=timedelta(seconds=6);return tick()
  auto.tick=advance_tick
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
  with patch.object(self.h,'client',return_value=client),patch('image_host.verify_public',side_effect=[Problem('missing'),None]) as verify:out=self.h.publish(p)
  client.put_object.assert_called_once();self.assertEqual(verify.call_count,2);kw=client.put_object.call_args.kwargs;self.assertNotIn('ACL',kw);self.assertIn('ContentMD5',kw)
  saved=self.s.get(self.pid);self.assertTrue(saved['images'][0]['public_url'].startswith('https://cdn.example.test/noon-images/'));self.assertFalse(saved['images_verified']);self.assertFalse(saved['reviewed']);self.assertEqual(len(out['images']),1)
 def test_product_urls_and_host_receipt_commit_together(self):
  b,r,p=self.queued();jid=r['jobs'][0]['job_id']
  with patch.object(self.h,'client',return_value=Mock()),patch('image_host.verify_public'):
   self.app.run(jid,p,'image-host')
  job=self.h.status('test')['jobs'][0];saved=self.s.get(self.pid)
  self.assertEqual(job['status'],'done');self.assertTrue(saved['images'][0]['public_url'])
  with self.s.connect() as c:receipt=json.loads(c.execute('SELECT result FROM jobs WHERE id=?',(jid,)).fetchone()[0])
  self.assertEqual(receipt['revision'],saved['revision']);self.assertEqual(receipt['images'][0]['public_url'],saved['images'][0]['public_url'])
 def test_host_completion_transaction_rolls_back_product_on_failure(self):
  _,r,p=self.queued();jid=r['jobs'][0]['job_id'];original=self.s.update
  def fail_after_update(*args,**kwargs):
   original(*args,**kwargs);raise RuntimeError('injected process failure before host receipt commit')
  with patch.object(self.h,'client',return_value=Mock()),patch('image_host.verify_public'),patch.object(self.s,'update',side_effect=fail_after_update):
   self.app.run(jid,p,'image-host')
  job=self.h.status('test')['jobs'][0];saved=self.s.get(self.pid)
  self.assertEqual(job['status'],'failed');self.assertFalse(saved['images'][0]['public_url'])
 def test_existing_identical_object_skips_upload(self):
  _,_,p=self.queued();client=Mock()
  with patch.object(self.h,'client',return_value=client),patch('image_host.verify_public'):self.h.publish(p)
  client.put_object.assert_not_called()
 def test_bad_public_result_preserves_product(self):
  _,_,p=self.queued();old=self.s.get(self.pid)
  with patch.object(self.h,'client',return_value=Mock()),patch('image_host.verify_public',side_effect=Problem('mismatch')):
   with self.assertRaises(Problem):self.h.publish(p)
  self.assertEqual(self.s.get(self.pid)['revision'],old['revision']);self.assertFalse(self.s.get(self.pid)['images'][0]['public_url'])
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
 def test_signed_upload_https_readback_and_workflow_human_gate(self):
  objects={};uploads=[];public_reads=[];protocol_errors=[]
  class StoreHandler(BaseHTTPRequestHandler):
   protocol_version='HTTP/1.1'
   def log_message(self,*args):pass
   def do_PUT(self):
    parts=self.path.split('/',2)
    body=self.rfile.read(int(self.headers.get('Content-Length','0')))
    if len(parts)!=3 or parts[1]!='test-bucket':protocol_errors.append(('path',self.path));self.send_error(404);return
    if not self.headers.get('Authorization','').startswith('AWS4-HMAC-SHA256 Credential=TESTACCESS/'):
     protocol_errors.append(('authorization',self.headers.get('Authorization')));self.send_error(403);return
    key=parts[2];objects[key]={'body':body,'content_type':self.headers.get('Content-Type')};uploads.append({'key':key,'body':body,'content_type':self.headers.get('Content-Type'),'content_md5':self.headers.get('Content-MD5'),'content_length':self.headers.get('Content-Length'),'transfer_encoding':self.headers.get('Transfer-Encoding'),'expect':self.headers.get('Expect'),'version':self.request_version})
    self.send_response(200);self.send_header('ETag','"'+hashlib.md5(body).hexdigest()+'"');self.send_header('Content-Length','0');self.end_headers()
   def do_GET(self):
    public_reads.append(self.path)
    key=self.path.removeprefix('/public/')
    stored=objects.get(key)
    if stored is None:self.send_error(404);return
    body=stored['body'];self.send_response(200);self.send_header('Content-Type',stored['content_type']);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
  key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
  subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'localhost')])
  certificate=(x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
   .serial_number(x509.random_serial_number()).not_valid_before(datetime.now(timezone.utc)-timedelta(minutes=1))
   .not_valid_after(datetime.now(timezone.utc)+timedelta(days=1))
   .add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True)
   .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False)
   .sign(key,hashes.SHA256()))
  cert=self.tmp.name+'/fixture-ca.pem';private_key=self.tmp.name+'/fixture-key.pem'
  Path(cert).write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
  Path(private_key).write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
  server=ThreadingHTTPServer(('127.0.0.1',0),StoreHandler);server.daemon_threads=True
  import ssl
  context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(cert,private_key);server.socket=context.wrap_socket(server.socket,server_side=True)
  worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
  try:
   base=f'https://127.0.0.1:{server.server_port}'
   self.h.save({**self.config,'endpoint':base,'public_base':base+'/public','revision':self.h.state()['revision']})
   p=self.s.get(self.pid);p=self.s.update(self.pid,{'source_url':'https://detail.1688.com/offer/qa-host.html','supplier':'本地HTTPS夹具'},p['revision']);self.s.update(self.pid,{},p['revision'],{'images_verified':True})
   auto=self.app.automation;body={'product_ids':[self.pid],'request_id':'local-https-workflow','name':'本地 HTTPS 托管闭环','plan':{'image_host':True}}
   preview=auto.preflight(body);self.assertEqual(preview['eligible_ids'],[self.pid]);run=auto.create({**body,'preflight_token':preview['token']})
   auto.tick()
   with patch.object(self.app.executor,'submit') as submit:auto.tick()
   self.assertIsNotNone(submit.call_args,auto.state());_,jid,p,kind=submit.call_args.args;self.assertEqual(kind,'image-host')
   real_client=self.h.client
   class RecordingClient:
    def __init__(self,client):self.client=client
    def put_object(self,**kwargs):
     try:return self.client.put_object(**kwargs)
     except Exception as error:protocol_errors.append(repr(error));raise
   with patch.object(self.h,'client',side_effect=lambda config:RecordingClient(real_client(config))),patch.dict(os.environ,{'SSL_CERT_FILE':cert,'AWS_CA_BUNDLE':cert}):self.app.run(jid,p,'image-host')
  finally:
   server.shutdown();server.server_close();worker.join(timeout=5)
  with self.s.connect() as c:
   job=dict(c.execute('SELECT status,message,result FROM jobs WHERE id=?',(jid,)).fetchone())
  saved=self.s.get(self.pid)
  self.assertEqual(job['status'],'done',(job,protocol_errors,uploads,public_reads))
  self.assertFalse(protocol_errors,protocol_errors)
  self.assertEqual(len(uploads),1)
  self.assertTrue(uploads[0]['key'].startswith('noon-images/'))
  self.assertEqual(uploads[0]['body'],(self.s.assets/saved['images'][0]['file']).read_bytes())
  self.assertEqual(uploads[0]['content_type'],'image/jpeg');self.assertTrue(uploads[0]['content_md5'])
  self.assertEqual(uploads[0]['content_md5'],base64.b64encode(hashlib.md5(uploads[0]['body']).digest()).decode())
  self.assertEqual(Path(uploads[0]['key']).stem,hashlib.sha256(uploads[0]['body']).hexdigest())
  self.assertGreaterEqual(len(public_reads),2,'must check the deterministic public URL before and after uploading')
  self.assertEqual(saved['images'][0]['public_url'],base+'/public/'+uploads[0]['key'])
  for _ in range(3):auto.tick()
  workflow=auto.state()['items'][0]
  self.assertEqual(workflow['run_id'],run['id']);self.assertEqual(workflow['step'],3);self.assertEqual(workflow['status'],'approval')
  self.assertIn('审核当前版本',workflow['message'])
  self.assertFalse(saved['images_verified']);self.assertFalse(saved['reviewed']);self.assertIsNone(saved['approved_revision'])
  with self.s.connect() as c:
   receipt=json.loads(job['result'])
   self.assertEqual(receipt['images'][0]['sha256'],hashlib.sha256(uploads[0]['body']).hexdigest())
   self.assertEqual(c.execute("SELECT count(*) FROM jobs WHERE product_id=? AND kind='submit'",(self.pid,)).fetchone()[0],0)
 def test_cancel_requires_explicit_batch(self):
  self.queued()
  with self.assertRaises(Problem):self.h.cancel({})
  self.assertEqual(self.h.status('test')['jobs'][0]['status'],'queued')
 def test_real_sdk_constructed_without_network(self):
  client=self.h.client(self.h.config());self.assertEqual(client.meta.endpoint_url,self.config['endpoint']);client.close()
