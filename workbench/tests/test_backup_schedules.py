import hashlib
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backup_schedules import BackupSchedules
from core import Problem, ident

class TestStore:
    def __init__(self,root):self.root=root;self.db=root/'workbench.sqlite3'
    def connect(self):
        c=sqlite3.connect(self.db);c.row_factory=sqlite3.Row;return c

class TestRecovery:
    def __init__(self,root):
        self.pending=root/'recovery'/'pending.json';self.archives=root/'recovery'/'archives';self.archives.mkdir(parents=True)
        self.lock=threading.RLock();self.calls=0
    def create(self,label='manual'):
        self.calls+=1;key=ident();data=('verified-test-'+key).encode();(self.archives/(key+'.zip')).write_bytes(data)
        result={'id':key,'label':label,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'created_at':'2026-10-03T00:00:00+00:00'}
        (self.archives/(key+'.json')).write_text(json.dumps(result));return result

class BackupSchedulesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.clock=[100000.0]
        self.app=SimpleNamespace(store=TestStore(self.root),write_lock=threading.RLock(),recovery=TestRecovery(self.root))
        self.s=BackupSchedules(self.app,clock=lambda:self.clock[0])
    def tearDown(self):self.s.close();self.tmp.cleanup()
    def save(self,enabled=True,retain=2,interval=60,**extra):
        return self.s.save({'version':self.s.state()['version'],'request_id':ident(),'confirmed':True,'enabled':enabled,'retain_count':retain,'interval_minutes':interval,**extra})
    def backup(self,**extra):return self.s.run({'version':self.s.state()['version'],'request_id':ident(),'confirmed':True,**extra})
    def path(self,key):return self.app.recovery.archives/(key+'.zip')
    def test_default_disabled_validation_confirmation_and_versions(self):
        self.assertFalse(self.s.state()['enabled']);self.assertIsNone(self.s.tick());self.assertEqual(self.app.recovery.calls,0)
        for fields in ({'interval_minutes':59},{'interval_minutes':True},{'retain_count':31},{'retain_count':0},{'confirmed':False},{'version':9}):
            with self.assertRaises(Problem):self.save(**fields)
        with self.assertRaises(Problem):self.backup(confirmed=False)
        self.save();self.assertEqual(self.s.state()['version'],1)
        self.save(enabled=False,confirmed=False);self.assertIsNone(self.s.state()['next_run_at'])
    def test_save_and_run_idempotent_replay_different_payload_rejected(self):
        body={'request_id':ident(),'version':0,'enabled':True,'interval_minutes':60,'retain_count':2,'confirmed':True}
        self.s.save(body);self.assertTrue(self.s.save(body)['replayed'])
        with self.assertRaises(Problem):self.s.save({**body,'retain_count':3})
        run={'request_id':ident(),'version':1,'confirmed':True};a=self.s.run(run);b=self.s.run(run)
        self.assertEqual(a['archive_id'],b['archive_id']);self.assertTrue(b['replayed']);self.assertEqual(self.app.recovery.calls,1)
        with self.assertRaises(Problem):self.s.run({**run,'confirmed':False})
    def test_tick_due_failure_waits_next_interval_and_never_deletes(self):
        self.save(retain=1);old=self.backup();self.clock[0]+=3600
        with patch.object(self.app.recovery,'create',side_effect=OSError('disk full')):
            failed=self.s.tick();self.assertEqual(failed['status'],'failed');self.assertIn('disk full',failed['error'])
            self.assertIsNone(self.s.tick());self.assertTrue(self.path(old['archive_id']).exists())
        self.clock[0]+=3600;success=self.s.tick();self.assertEqual(success['status'],'success')
        self.assertFalse(self.path(old['archive_id']).exists());self.assertEqual(len(self.s.state()['history']),3)
    def test_retention_preserves_manual_rollback_unregistered_json_and_history(self):
        manual=self.app.recovery.create();rollback=self.app.recovery.create('before-restore');unregistered=self.app.recovery.create('scheduled:unknown')
        arbitrary=self.app.recovery.archives/'user.zip';arbitrary.write_bytes(b'user file')
        self.save(retain=1);old=self.backup();new=self.backup()
        for a in (manual,rollback,unregistered,new):self.assertTrue(self.path(a.get('archive_id') or a['id']).exists())
        self.assertTrue(arbitrary.exists());self.assertFalse(self.path(old['archive_id']).exists())
        self.assertTrue((self.app.recovery.archives/(old['archive_id']+'.json')).exists())
        self.assertIsNotNone(self.s.state()['history'][-1]['deleted_at'])
    def test_symlink_archive_or_metadata_refuses_cleanup_and_preserves_target(self):
        for suffix in ('zip','json'):
            self.save(retain=1);old=self.backup();path=self.app.recovery.archives/(old['archive_id']+'.'+suffix)
            target=self.root/('outside-'+suffix);target.write_bytes(path.read_bytes());path.unlink();path.symlink_to(target)
            new=self.backup();self.assertEqual(new['status'],'success');self.assertTrue(new['cleanup_error']);self.assertTrue(path.is_symlink());self.assertTrue(target.exists())
    def test_directory_symlink_and_changed_hash_refuse_cleanup(self):
        self.save(retain=1);old=self.backup();self.path(old['archive_id']).write_bytes(b'tampered')
        new=self.backup();self.assertIn('副本文件已变化',new['cleanup_error']);self.assertTrue(self.path(old['archive_id']).exists())
        archives=self.app.recovery.archives;real=archives.with_name('elsewhere');archives.rename(real);archives.symlink_to(real,target_is_directory=True)
        new=self.backup();self.assertIn('目录存在链接',new['cleanup_error']);self.assertTrue((real/(old['archive_id']+'.zip')).exists())
    def test_clock_restart_no_catchup_and_pending_restore_wait(self):
        self.save();self.clock[0]+=72000
        self.s=BackupSchedules(self.app,clock=lambda:self.clock[0]);self.assertIsNone(self.s.tick())
        self.clock[0]+=3600;self.app.recovery.pending.write_text('{}')
        self.assertIsNone(self.s.tick())
        with self.assertRaises(Problem):self.backup()
        with self.assertRaises(Problem):self.save()
        self.app.recovery.pending.unlink();self.assertEqual(self.s.tick()['status'],'success');self.assertIsNone(self.s.tick())
    def test_interrupted_request_is_not_replayed_and_history_retained(self):
        body={'request_id':ident(),'version':0,'confirmed':True}
        with patch.object(self.app.recovery,'create',side_effect=KeyboardInterrupt('crash')):
            with self.assertRaises(KeyboardInterrupt):self.s.run(body)
        self.s=BackupSchedules(self.app,clock=lambda:self.clock[0]);result=self.s.run(body)
        self.assertEqual(result['status'],'interrupted');self.assertTrue(result['replayed']);self.assertEqual(self.app.recovery.calls,0)
        self.assertEqual(self.s.state()['latest']['status'],'interrupted')
    def test_single_execution_serializes_concurrent_run_and_close_wakes_thread(self):
        body={'request_id':ident(),'version':0,'confirmed':True};results=[];errors=[]
        def run():
            try:results.append(self.s.run(body))
            except Exception as e:errors.append(e)
        threads=[threading.Thread(target=run) for _ in range(2)]
        for t in threads:t.start()
        for t in threads:t.join(timeout=3)
        self.assertFalse(errors);self.assertEqual(self.app.recovery.calls,1);self.assertEqual(len(results),2)
        self.s.start();thread=self.s._thread;self.s.close();self.assertFalse(thread.is_alive())
    def test_real_recovery_creates_verified_archive_without_credentials(self):
        import zipfile
        from server import App
        from recovery import Recovery
        app=App(self.root)
        schedule=BackupSchedules(app,clock=lambda:self.clock[0])
        # Standalone module construction precedes Recovery in the integrated App.
        app.recovery=Recovery(app.store.root)
        try:
            pid=app.store.import_rows([{'title_zh':'周期完整备份商品','facts':'黑色5件'}])['created'][0]
            (self.root/'.env').write_text('secret=excluded')
            (self.root/'credentials'/'private.key').write_text('not portable')
            result=schedule.run({'request_id':ident(),'version':0,'confirmed':True})
            self.assertEqual(result['status'],'success',result['error'])
            archive=app.recovery.archive_path(result['archive_id'])
            self.assertEqual(app.recovery.validate(archive)['counts']['products'],1)
            with zipfile.ZipFile(archive) as z:
                self.assertIn('workbench.sqlite3',z.namelist())
                self.assertNotIn('.env',z.namelist())
                self.assertFalse(any('credentials' in n for n in z.namelist()))
            self.assertEqual(app.store.get(pid)['title_zh'],'周期完整备份商品')
        finally:
            schedule.close()
            for name in ('backup_schedules','collection_schedules','source_inbox','visual_checks','visuals','automation','media'):
                resource=getattr(app,name,None)
                if resource:resource.close()
            app.models.codex.close();app.collection_executor.shutdown(wait=True,cancel_futures=True);app.executor.shutdown(wait=True,cancel_futures=True)

    def test_constructor_needs_no_recovery_until_state_or_execution(self):
        del self.app.recovery
        other=BackupSchedules(self.app,clock=lambda:self.clock[0]);other.close()

if __name__=='__main__':unittest.main()
