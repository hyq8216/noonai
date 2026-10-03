import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import App
from source_inbox import SourceInbox,MAX_CATALOG_FILE
from recovery import Recovery
from core import Problem

CSV='商品名称,货源链接,规格货号,供应商,规格事实\n红盒,https://detail.1688.com/offer/1.html,R1,工厂,红色塑料盒\n'


class SourceInboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=App(Path(self.tmp.name))
        self.box=self.app.source_inbox
    def tearDown(self):
        self.app.models.codex.close();self.app.visual_checks.close();self.app.visuals.close();self.app.media.close();self.app.executor.shutdown(wait=False,cancel_futures=True);self.tmp.cleanup()
    def put(self,name,content):
        path=self.box.folder/name
        path.write_text(content)
        old=path.stat().st_mtime-10
        os.utime(path,(old,old))
        return path
    def enable(self,translate=False,review=False):
        self.box.configure({'enabled':True,'translate':translate,'review':review})
    def scan(self):self.box.tick();self.box.tick()

    def test_disabled_stable_file_then_exactly_once_import_and_restart(self):
        self.put('supplier.csv',CSV)
        self.scan();self.assertEqual(self.app.store.list(),[])
        self.enable();self.box.tick();self.assertEqual(self.app.store.list(),[])
        self.box.tick();self.assertEqual(len(self.app.store.list()),1)
        self.assertEqual(self.box.state()['files'][0]['status'],'done')
        self.scan();self.assertEqual(len(self.app.store.list()),1)
        restarted=SourceInbox(self.app);restarted.tick();restarted.tick()
        self.assertEqual(len(self.app.store.list()),1)
        self.assertEqual(len(restarted.state()['files']),1)

    def test_bad_file_recorded_and_corrected_file_imported(self):
        self.enable();path=self.put('supplier.json','{broken')
        self.scan();self.assertEqual(self.box.state()['files'][0]['status'],'attention')
        self.assertEqual(self.app.store.list(),[])
        self.put('supplier.json','[{"title_zh":"红盒","source_url":"https://detail.1688.com/offer/1.html","source_sku":"R1","supplier":"工厂","facts":"红色塑料盒"}]')
        self.scan();self.assertEqual(len(self.app.store.list()),1)
        self.assertEqual([x['status'] for x in self.box.state()['files']],['done','attention'])
        self.assertTrue(path.exists())

    def test_partial_file_reports_skipped_rows_and_never_repeats(self):
        self.enable();self.put('mixed.csv',CSV+',https://detail.1688.com/offer/2.html,R2,工厂,红盒\n')
        self.scan();s=self.box.state()['files'][0]
        self.assertEqual(s['status'],'done')
        self.assertEqual(s['result']['created'],1)
        self.assertEqual(s['result']['skipped'],1)
        self.scan();self.assertEqual(len(self.app.store.list()),1)

    def test_processing_option_creates_one_existing_workflow(self):
        self.enable(translate=True,review=True);self.put('supplier.csv',CSV)
        with patch.object(self.app.models,'ready',return_value=True):self.scan()
        s=self.box.state()['files'][0]
        self.assertEqual(s['result']['queued'],1)
        self.assertTrue(s['result']['run_id'])
        self.assertEqual(len(self.app.automation.state()['runs']),1)

    def test_symlink_ignored_oversize_recorded_and_settings_validated(self):
        self.enable();self.put('large.csv','x'*(MAX_CATALOG_FILE+1))
        target=self.put('real.txt','ignored')
        (self.box.folder/'alias.csv').symlink_to(target)
        self.scan();files=self.box.state()['files']
        self.assertEqual(len(files),1)
        self.assertEqual(files[0]['status'],'attention')
        self.assertIn('20MB',files[0]['message'])
        with self.assertRaises(Problem):self.box.configure({'enabled':1,'translate':False,'review':False})

    def test_backup_keeps_supplier_file_and_restored_copy_disables_scan(self):
        self.enable();self.put('supplier.csv',CSV);self.scan()
        archive=self.app.recovery.create()
        with tempfile.TemporaryDirectory() as dest:
            self.app.recovery.validate(self.app.recovery.archive_path(archive['id']),dest)
            self.assertEqual((Path(dest)/'source-inbox'/'supplier.csv').read_text(),CSV)
            database=Path(dest)/'workbench.sqlite3'
            self.app.recovery.paused_copy(database)
            with sqlite3.connect(database) as c:
                self.assertEqual(c.execute('SELECT enabled FROM source_inbox_config').fetchone()[0],0)

    def test_supply_update_folder_changes_only_local_supplier_fields_once(self):
        pid=self.app.store.import_rows([{'title_zh':'红盒','source_url':'https://detail.1688.com/offer/1.html','source_sku':'R1','cost_cny':10,'stock':8}])['created'][0]
        p=self.app.store.get(pid)
        self.enable()
        path=self.box.updates/'stock.csv'
        def put(value):
            path.write_text('工作台SKU,采购成本（人民币元）,库存数量\n'+p['partner_sku']+','+value+'\n')
            old=path.stat().st_mtime-10;os.utime(path,(old,old))
        put('12,0')
        self.scan()
        saved=self.app.store.get(pid)
        self.assertEqual((saved['cost_cny'],saved['stock']),(12,0))
        self.assertEqual(saved['revision'],p['revision']+1)
        self.assertEqual(saved['supply_checked_at'],'')
        record=self.box.state()['files'][0]
        self.assertEqual((record['name'],record['result']['updated']),('updates/stock.csv',1))
        self.assertEqual(record['result']['rows'][0]['changes']['stock']['after'],0)
        self.assertEqual(saved['platform'],p['platform'])
        self.scan();self.assertEqual(self.app.store.get(pid)['revision'],saved['revision'])
        put('13,6');self.scan()
        self.assertEqual((self.app.store.get(pid)['cost_cny'],self.app.store.get(pid)['stock']),(13,6))
        self.assertEqual(len(self.box.state()['files']),2)

    def test_supply_update_invalid_identity_and_unchanged_are_visible(self):
        pid=self.app.store.import_rows([{'title_zh':'红盒','cost_cny':10,'stock':8}])['created'][0]
        sku=self.app.store.get(pid)['partner_sku']
        self.enable()
        path=self.box.updates/'unchanged.csv'
        path.write_text('工作台SKU,库存数量\n'+sku+',8\n')
        old=path.stat().st_mtime-10;os.utime(path,(old,old));self.scan()
        result=self.box.state()['files'][0]
        self.assertEqual(result['status'],'done');self.assertEqual(result['result']['unchanged'],1)
        self.assertEqual(result['result']['rows'][0]['status'],'unchanged')
        bad=self.box.updates/'bad.csv';bad.write_text('工作台SKU,库存数量\nNOT-A-SKU,3\n')
        old=bad.stat().st_mtime-10;os.utime(bad,(old,old));self.scan()
        self.assertEqual(next(r for r in self.box.state()['files'] if r['name']=='updates/bad.csv')['status'],'attention')
        self.assertEqual(self.app.store.get(pid)['revision'],1)

    def test_backup_includes_supply_update_files_and_rejects_symlink(self):
        self.enable();path=self.box.updates/'supplier.csv';path.write_text('工作台SKU,库存数量\nUNKNOWN,1\n')
        archive=self.app.recovery.create()
        with tempfile.TemporaryDirectory() as dest:
            self.app.recovery.validate(self.app.recovery.archive_path(archive['id']),dest)
            self.assertEqual((Path(dest)/'source-inbox'/'updates'/'supplier.csv').read_text(),path.read_text())
        (self.box.updates/'shortcut.csv').symlink_to(path)
        with self.assertRaises(Problem):self.app.recovery.create()

    def test_previous_schema_backup_migrates_without_losing_products(self):
        self.enable();self.put('supplier.csv',CSV);self.scan()
        with tempfile.TemporaryDirectory() as old,tempfile.TemporaryDirectory() as dest:
            old=Path(old);old_db=old/'workbench.sqlite3'
            with sqlite3.connect(self.app.store.db) as source,sqlite3.connect(old_db) as target:source.backup(target)
            with sqlite3.connect(old_db) as c:
                c.execute('DROP TABLE source_inbox_config');c.execute('DROP TABLE source_inbox_files')
            legacy=Recovery(old);archive=legacy.create()
            self.app.recovery.validate(legacy.archive_path(archive['id']),dest)
            with sqlite3.connect(Path(dest)/'workbench.sqlite3') as c:
                self.assertEqual(c.execute('SELECT count(*) FROM products').fetchone()[0],1)
                self.assertEqual(c.execute('SELECT enabled FROM source_inbox_config').fetchone()[0],0)


if __name__=='__main__':unittest.main()
