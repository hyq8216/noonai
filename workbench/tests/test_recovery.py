import base64
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Problem,normalize_image,ident
from server import App
from recovery import Recovery,COMPONENTS,digest,write_json

class RecoveryTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.app=App(self.root);self.r=self.app.recovery
  self.pid=self.app.store.import_rows([{'title_zh':'备份测试商品','facts':'黑色5件装'}])['created'][0]
 def tearDown(self):
  self.app.models.codex.close();self.app.visuals.close();self.app.automation.close();self.app.media.close();self.app.executor.shutdown(wait=True,cancel_futures=True);self.tmp.cleanup()
 def photo(self):
  buf=io.BytesIO();Image.new('RGB',(700,700),'blue').save(buf,format='PNG')
  im=normalize_image(self.app.store,base64.b64encode(buf.getvalue()).decode(),'square')
  p=self.app.store.get(self.pid);self.app.store.update(self.pid,{},p['revision'],{'images':[im]})
  return im
 def backup(self):return self.r.create()
 def stage(self,a):return self.r.inspect(self.r.archive_path(a['id']))
 def schedule(self,a):
  p=self.stage(a);self.r.schedule({**p,'confirmed':True});return p
 def mutate_zip(self,a,mutate):
  path=self.r.archive_path(a['id']);out=self.root/'tampered.zip'
  with zipfile.ZipFile(path) as z:data={n:z.read(n) for n in z.namelist()}
  mutate(data)
  with zipfile.ZipFile(out,'w') as z:
   for n,b in data.items():z.writestr(n,b)
  return out
 def test_roundtrip_history_media_operations_and_secrets_exclusion(self):
  im=self.photo();p=self.app.store.get(self.pid);self.app.store.update(self.pid,{'title_zh':'最新名称'},p['revision'],{'images':[]})
  source=self.root/'input.png';Image.new('RGB',(160,160),'red').save(source)
  asset=self.app.media.ingest(source,'测试原图','自制测试',self.pid)
  self.app.ops.transact('entity',{'request_id':ident(),'kind':'warehouse','name':'备份仓'})
  (self.root/'.env').write_text('SECRET=excluded');(self.root/'credentials'/'private.key').write_text('secret')
  a=self.backup();self.assertEqual(a['counts']['products'],1)
  with zipfile.ZipFile(self.r.archive_path(a['id'])) as z:
   self.assertIn('assets/'+im['source'],z.namelist());self.assertIn('media/'+asset['file'],z.namelist());self.assertNotIn('.env',z.namelist());self.assertFalse(any('credentials' in n for n in z.namelist()))
  self.app.store.import_rows([{'title_zh':'稍后新增'}]);self.schedule(a);self.r.apply_pending()
  self.assertEqual(len(self.app.store.list()),1);self.assertEqual(self.app.store.get(self.pid)['title_zh'],'最新名称')
  self.assertEqual((self.root/'.env').read_text(),'SECRET=excluded');self.assertEqual((self.root/'credentials'/'private.key').read_text(),'secret')
  last=self.r.state()['last_restore'];self.assertEqual(last['status'],'restored')
  rollback=self.r.archive_path(last['rollback_archive_id']);self.assertEqual(self.r.validate(rollback)['counts']['products'],2)
  self.assertEqual(self.app.media.get(asset['id'])['sha256'],digest(self.root/'media'/asset['file']))
 def test_missing_original_aborts_archive_not_silently_partial(self):
  im=self.photo();(self.root/'assets'/im['source']).unlink()
  with self.assertRaises(Problem):self.backup()
  self.assertEqual(self.r.state()['archives'],[])
 def test_corrupted_member_rejected_and_no_data_changed(self):
  a=self.backup();path=self.mutate_zip(a,lambda d:d.update({'workbench.sqlite3':b'bad database'}))
  with self.assertRaises(Problem):self.r.inspect(path)
  self.assertEqual(len(self.app.store.list()),1);self.assertFalse(self.r.pending.exists())
 def test_path_traversal_and_symlinks_and_duplicates_rejected(self):
  a=self.backup()
  for name in ('../escape','/tmp/escape','credentials/a.key','assets/../../escape'):
   path=self.mutate_zip(a,lambda d:d.update({name:b'bad'}))
   with self.assertRaises(Problem):self.r.validate(path)
  path=self.mutate_zip(a,lambda d:None)
  with zipfile.ZipFile(path,'a') as z:
   info=zipfile.ZipInfo('assets/'+'a'*32+'.png');info.external_attr=(0o120777<<16);z.writestr(info,'/tmp/escape')
  with self.assertRaises(Problem):self.r.validate(path)
  path=self.mutate_zip(a,lambda d:None)
  with zipfile.ZipFile(path,'a') as z:z.writestr('manifest.json','{}')
  with self.assertRaises(Problem):self.r.validate(path)
 def test_incompatible_schema_and_database_missing_media_rejected(self):
  self.photo();a=self.backup()
  def schema_change(d):
   m=json.loads(d['manifest.json']);m['schema']=[];d['manifest.json']=json.dumps(m).encode()
  with self.assertRaises(Problem):self.r.validate(self.mutate_zip(a,schema_change))
  def missing(d):
   name=next(n for n in d if n.startswith('assets/'));del d[name];m=json.loads(d['manifest.json']);del m['files'][name];d['manifest.json']=json.dumps(m).encode()
  with self.assertRaises(Problem):self.r.validate(self.mutate_zip(a,missing))
 def test_confirmation_stale_digest_and_active_work_rejected(self):
  a=self.backup();p=self.stage(a)
  with self.assertRaises(Problem):self.r.schedule(p)
  with self.assertRaises(Problem):self.r.schedule({**p,'confirmed':True,'sha256':'wrong'})
  self.app.store.add_job(self.pid,'translate',self.app.store.get(self.pid)['revision'])
  with self.assertRaises(Problem):self.r.schedule({**p,'confirmed':True})
  self.assertFalse(self.r.pending.exists())
 def test_cancel_keeps_queues_paused_and_no_restore(self):
  a=self.backup();self.schedule(a);self.r.cancel();self.r.apply_pending()
  self.assertFalse(self.r.pending.exists());self.assertIsNone(self.r.state()['last_restore'])
  with self.app.store.connect() as c:self.assertEqual(c.execute('SELECT paused FROM media_control').fetchone()[0],1)
 def test_restore_pauses_old_work_disables_models_clears_approval(self):
  model=self.app.models.save({'name':'恢复测试模型','provider':'codex-subscription','model':'gpt-6-luna','enabled':True,'daily_call_limit':5,'revision':0})
  with self.app.store.connect() as c:
   c.execute('UPDATE products SET approved_revision=revision WHERE id=?',(self.pid,))
   c.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)",(ident(),self.pid,'submit',1,'queued','测试待发请求',None,'2026-01-01','2026-01-01'))
  a=self.backup()
  with self.app.store.connect() as c:c.execute("UPDATE jobs SET status='failed'")
  self.schedule(a);self.r.apply_pending()
  with self.app.store.connect() as c:
   self.assertIsNone(c.execute('SELECT approved_revision FROM products').fetchone()[0]);self.assertEqual(c.execute('SELECT status FROM jobs').fetchone()[0],'failed')
   self.assertFalse(json.loads(c.execute('SELECT data FROM model_profiles').fetchone()[0])['enabled'])
   self.assertEqual(c.execute('SELECT paused FROM visual_control').fetchone()[0],1)
 def test_tampered_pending_archive_does_not_replace_current_data(self):
  a=self.backup();p=self.schedule(a);self.app.store.import_rows([{'title_zh':'保留当前资料'}]);(self.r.imports/(p['id']+'.zip')).write_bytes(b'corrupt')
  self.r.apply_pending();self.assertEqual(len(self.app.store.list()),2);self.assertEqual(self.r.state()['last_restore']['status'],'failed')
 def test_interruption_each_component_rolls_back_on_next_start(self):
  self.photo();a=self.backup();self.app.store.import_rows([{'title_zh':'恢复前资料'}])
  real_replace=os.replace
  for stop_name in ('workbench.sqlite3','assets','media'):
   self.schedule(a)
   def crash(src,dst):
    real_replace(src,dst)
    if Path(src).parent==self.root and Path(src).name==stop_name:raise KeyboardInterrupt('simulated poweroff')
   with patch('recovery.os.replace',side_effect=crash):
    with self.assertRaises(KeyboardInterrupt):self.r.apply_pending()
   self.assertEqual(json.loads(self.r.pending.read_text())['phase'],'switching')
   Recovery(self.root,expected=[]).apply_pending()
   self.assertEqual(len(self.app.store.list()),2);self.assertEqual(self.r.state()['last_restore']['status'],'rolled_back')
   self.assertTrue(self.app.store.assets.is_dir())
 def test_committed_restore_finishes_after_interrupted_success_record(self):
  a=self.backup();self.app.store.import_rows([{'title_zh':'后来新增'}]);self.schedule(a)
  with patch.object(self.r,'finish',side_effect=KeyboardInterrupt()):
   with self.assertRaises(KeyboardInterrupt):self.r.apply_pending()
  self.assertEqual(json.loads(self.r.pending.read_text())['phase'],'committed')
  Recovery(self.root,expected=[]).apply_pending()
  self.assertEqual(len(self.app.store.list()),1);self.assertEqual(self.r.state()['last_restore']['status'],'restored')
 def test_failed_success_record_retains_committed_journal(self):
  a=self.backup();self.schedule(a)
  with patch.object(self.r,'finish',side_effect=OSError('simulated write error')):
   with self.assertRaises(OSError):self.r.apply_pending()
  self.assertEqual(json.loads(self.r.pending.read_text())['phase'],'committed')
  Recovery(self.root,expected=[]).apply_pending();self.assertEqual(self.r.state()['last_restore']['status'],'restored')
 def test_low_space_does_not_switch(self):
  a=self.backup();self.schedule(a)
  with patch('recovery.shutil.disk_usage',return_value=type('Space',(),{'free':0})()):self.r.apply_pending()
  self.assertEqual(self.r.state()['last_restore']['status'],'failed');self.assertEqual(len(self.app.store.list()),1)

if __name__=='__main__':unittest.main()
