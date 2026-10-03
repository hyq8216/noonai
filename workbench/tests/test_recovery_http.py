import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

class RecoveryHTTPTests(unittest.TestCase):
 def test_actual_restart_restore_download_and_write_guard(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);p=None
   def start():
    ready=root/'ready-test.json';ready.unlink(missing_ok=True)
    proc=subprocess.Popen([sys.executable,str(Path(__file__).resolve().parents[1]/'server.py'),'--port','0','--data',str(root),'--ready-file',str(ready)],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    for _ in range(100):
     if ready.exists():return proc,json.loads(ready.read_text())['url']
     if proc.poll() is not None:raise AssertionError(proc.stderr.read().decode())
     time.sleep(.05)
    proc.terminate();raise AssertionError('startup timed out')
   def call(path,data=None,raw=None):
    headers={}
    body=None
    if data is not None:body=json.dumps(data).encode();headers={'Content-Type':'application/json','X-Workbench-Token':token}
    if raw is not None:body=raw;headers={'Content-Type':'application/octet-stream','X-Workbench-Token':token}
    with urllib.request.urlopen(urllib.request.Request(url+path,data=body,headers=headers),timeout=15) as r:
     b=r.read();return json.loads(b) if r.headers['Content-Type'].startswith('application/json') else b
   try:
    p,url=start();s=call('/api/state');token=s['token']
    pid=call('/api/import',{'products':[{'title_zh':'应恢复的商品'}]})['created'][0]
    a=call('/api/backup/create',{});archive=call('/api/backup/download/'+a['id']);self.assertTrue(archive.startswith(b'PK'))
    call('/api/import',{'products':[{'title_zh':'备份后新增的商品'}]})
    stage=call('/api/backup/inspect',raw=archive)
    call('/api/backup/schedule',{**stage,'confirmed':True})
    with self.assertRaises(urllib.error.HTTPError) as denied:call('/api/import',{'products':[{'title_zh':'不能写入'}]})
    self.assertEqual(denied.exception.code,409);self.assertEqual(len(call('/api/state')['products']),2)
    p.terminate();p.wait(timeout=15);p.stderr.close();p,url=start();s=call('/api/state');token=s['token']
    self.assertEqual([x['id'] for x in s['products']],[pid]);self.assertEqual(s['recovery']['last_restore']['status'],'restored');self.assertTrue(s['media']['paused']);self.assertTrue(s['visuals']['paused'])
    rollback=s['recovery']['last_restore']['rollback_archive_id'];stage=call('/api/backup/inspect',raw=call('/api/backup/download/'+rollback))
    call('/api/backup/schedule',{**stage,'confirmed':True});p.terminate();p.wait(timeout=15);p.stderr.close();p,url=start();s=call('/api/state');self.assertEqual(len(s['products']),2)
   finally:
    if p and p.poll() is None:p.terminate();p.wait(timeout=15)
    if p and p.stderr:p.stderr.close()

if __name__=='__main__':unittest.main()
