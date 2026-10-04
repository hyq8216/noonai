import csv
import hashlib
import io
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

    def test_unreadable_new_catalog_does_not_abort_updates_scan(self):
        pid=self.app.store.import_rows([{'title_zh':'可更新商品','source_url':'https://supplier.example/update-1',
                                         'supplier':'工厂','facts':'红色盒','stock':3}])['created'][0]
        self.enable()
        update=self.box.updates/'stock.csv'
        update.write_text('工作台SKU,库存数量\n'+self.app.store.get(pid)['partner_sku']+',9\n')
        old=update.stat().st_mtime-10;os.utime(update,(old,old))
        original_iterdir=Path.iterdir
        def fail_new_catalog(path):
            if path==self.box.folder:raise PermissionError('synthetic unreadable folder')
            return original_iterdir(path)
        with patch.object(Path,'iterdir',fail_new_catalog):
            self.box.tick();self.box.tick()
        self.assertEqual(self.app.store.get(pid)['stock'],9)
        self.assertIn('new',self.box.last_error)

    def test_unreadable_photo_sku_folder_does_not_abort_other_sku(self):
        self.enable()
        broken=self.box.photos/'BROKEN-SKU';good=self.box.photos/'GOOD-SKU'
        broken.mkdir();good.mkdir()
        (broken/'01.jpg').write_bytes(b'broken-folder fixture')
        (good/'01.jpg').write_bytes(b'good-folder fixture')
        original_iterdir=Path.iterdir
        def fail_broken_photo_folder(path):
            if path==broken:raise PermissionError('synthetic unreadable SKU folder')
            return original_iterdir(path)
        with patch.object(Path,'iterdir',fail_broken_photo_folder),patch.object(self.box,'check_photo') as check:
            self.box.tick()
        self.assertEqual([call.args[0] for call in check.call_args_list],[good/'01.jpg'])
        self.assertEqual(self.box.scan_progress['photos'],{'checked':1,'total':1})
        self.assertIn('原图 BROKEN-SKU',self.box.last_error)

    def test_partial_file_reports_skipped_rows_and_never_repeats(self):
        self.enable();self.put('mixed.csv',CSV+',https://detail.1688.com/offer/2.html,R2,工厂,红盒\n')
        self.scan();s=self.box.state()['files'][0]
        self.assertEqual(s['status'],'done')
        self.assertEqual(s['result']['created'],1)
        self.assertEqual(s['result']['skipped'],1)
        self.scan();self.assertEqual(len(self.app.store.list()),1)

    def test_cross_batch_identity_conflicts_and_all_issue_rows_survive_restart(self):
        self.enable()
        output=io.StringIO(newline='');writer=csv.writer(output)
        writer.writerow(['商品名称','货源链接','规格货号','供应商','规格事实','采购成本（人民币元）'])
        rows=[]
        for number in range(1,5001):
            rows.append([f'商品{number}',f'https://detail.1688.com/offer/{number}.html',f'S{number}','工厂','款式A','12'])
        rows[0]=['重复商品','https://detail.1688.com/offer/1.html','S1','工厂','款式A','12']
        rows[499]=list(rows[0])
        rows[500]=['冲突商品甲','https://detail.1688.com/offer/9000.html','CONFLICT','工厂','款式A','12']
        rows[501]=['冲突商品乙','https://detail.1688.com/offer/9000.html','CONFLICT','工厂','款式B','12']
        for index in (9,19,29,39,49,59,69,79):rows[index][5]='不是价格'
        writer.writerows(rows);content=output.getvalue()
        source=Path(self.tmp.name)/'supplier.csv';source.write_text(content)
        old=source.stat().st_mtime-10;os.utime(source,(old,old))
        deposited=self.box.deposit(source,'supplier.csv','new')
        digest=deposited['digest']
        catalog_path=self.box.folder/deposited['name']
        old=catalog_path.stat().st_mtime-10;os.utime(catalog_path,(old,old))
        self.box.tick();self.box.tick()
        self.assertEqual(len(self.app.store.list()),4989)
        receipt=self.box.file_detail(deposited['name'],digest)['result']
        issues=receipt['issue_rows']
        self.assertEqual([item['row'] for item in issues],[10,20,30,40,50,60,70,80,500,501,502])
        self.assertEqual([item['status'] for item in issues],['blocked']*8+['duplicate','blocked','blocked'])
        self.assertIn('采购成本',issues[0]['reason'])
        self.assertEqual(len(receipt['issue_examples']),5)
        self.assertTrue(receipt['mapping_complete'])
        restarted=SourceInbox(self.app)
        reread=restarted.file_detail(deposited['name'],digest)['result']
        self.assertEqual(reread['issue_rows'],issues)

    def test_large_catalog_restart_reuses_committed_batch_receipt(self):
        self.enable()
        output=io.StringIO(newline='');writer=csv.writer(output)
        writer.writerow(['商品名称','货源链接','规格货号','供应商','规格事实'])
        writer.writerows([[f'商品{n}',f'https://detail.1688.com/offer/{n}.html',f'S{n}','工厂','款式A'] for n in range(1,502)])
        content=output.getvalue();path=self.put('resume.csv',content)
        config={'enabled':1,'translate':0,'review':0}
        original_record=self.box.record
        def interrupt_after_first_batch(name,digest,status,message,result=None):
            original_record(name,digest,status,message,result)
            if status=='processing' and message.startswith('已处理 1/2 批'):
                raise RuntimeError('simulated process termination after batch receipt')
        with patch.object(self.box,'record',side_effect=interrupt_after_first_batch):
            with self.assertRaisesRegex(RuntimeError,'simulated process termination'):
                self.box.process_large_catalog('resume.csv',hashlib.sha256(content.encode()).hexdigest(),'.csv',content,config)
        self.assertEqual(len(self.app.store.list()),500)
        with self.app.store.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM ops_requests WHERE key LIKE 'source-import:%'").fetchone()[0],1)
        restarted=SourceInbox(self.app)
        restarted.check_file(path,config)
        restarted.check_file(path,config)
        self.assertEqual(len(self.app.store.list()),501)
        record=restarted.file_detail('resume.csv',hashlib.sha256(content.encode()).hexdigest())
        self.assertEqual(record['status'],'done')
        self.assertEqual(record['result']['cataloged'],501)
        with self.app.store.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM ops_requests WHERE key LIKE 'source-import:%'").fetchone()[0],2)

    def test_large_supply_updates_keep_zero_blank_and_cross_batch_duplicate_contract(self):
        products=self.app.store.import_rows([{'title_zh':f'商品{n}','cost_cny':10,'stock':7} for n in range(1,501)])['created']
        saved=[self.app.store.get(pid) for pid in products]
        output=io.StringIO(newline='');writer=csv.writer(output)
        writer.writerow(['工作台SKU','采购成本（人民币元）','库存数量'])
        rows=[[p['partner_sku'],'12','5'] for p in saved]
        rows[0]=[saved[0]['partner_sku'],'',0]
        rows[1]=[saved[1]['partner_sku'],0,'']
        rows[2]=[saved[2]['partner_sku'],'','']
        rows[3]=[saved[3]['partner_sku'],'13','4']
        rows.append([saved[3]['partner_sku'],'14','9'])
        writer.writerows(rows);content=output.getvalue();digest=hashlib.sha256(content.encode()).hexdigest()
        self.assertTrue(self.box.process_large_updates('updates/bulk.csv',digest,'.csv',content))
        self.assertEqual(self.app.store.get(products[0])['cost_cny'],10)
        self.assertEqual(self.app.store.get(products[0])['stock'],0)
        self.assertEqual(self.app.store.get(products[1])['cost_cny'],0)
        self.assertEqual(self.app.store.get(products[1])['stock'],7)
        self.assertEqual(self.app.store.get(products[2])['revision'],1)
        self.assertEqual(self.app.store.get(products[3])['revision'],1)
        receipt=self.box.file_detail('updates/bulk.csv',digest)['result']
        self.assertEqual((receipt['updated'],receipt['skipped']),(498,3))
        self.assertEqual([item['row'] for item in receipt['issue_rows']],[3,4,501])
        self.assertEqual([item['status'] for item in receipt['issue_rows']],['blocked','blocked','blocked'])

    def test_large_supply_update_restart_does_not_apply_committed_batch_twice(self):
        products=self.app.store.import_rows([{'title_zh':f'商品{n}','stock':1} for n in range(1,501)])['created']
        saved=[self.app.store.get(pid) for pid in products]
        output=io.StringIO(newline='');writer=csv.writer(output)
        writer.writerow(['工作台SKU','库存数量'])
        rows=[[p['partner_sku'],8] for p in saved]
        rows.append([saved[0]['partner_sku'],9])
        writer.writerows(rows);content=output.getvalue()
        path=self.box.updates/'resume.csv';path.write_text(content)
        old=path.stat().st_mtime-10;os.utime(path,(old,old))
        digest=hashlib.sha256(content.encode()).hexdigest()
        original_record=self.box.record
        def interrupt_after_first_batch(name,file_digest,status,message,result=None):
            original_record(name,file_digest,status,message,result)
            if status=='processing' and message.startswith('已处理 1/2 批'):
                raise RuntimeError('simulated process termination after update commit')
        with patch.object(self.box,'record',side_effect=interrupt_after_first_batch):
            with self.assertRaisesRegex(RuntimeError,'simulated process termination'):
                self.box.process_large_updates('updates/resume.csv',digest,'.csv',content)
        changed=[p for p in products if self.app.store.get(p)['revision']==2]
        self.assertEqual(len(changed),499)
        restarted=SourceInbox(self.app)
        config={'enabled':1,'translate':0,'review':0}
        restarted.check_file(path,config,'updates')
        restarted.check_file(path,config,'updates')
        self.assertEqual([self.app.store.get(pid)['revision'] for pid in products],[1]+[2]*499)
        receipt=restarted.file_detail('updates/resume.csv',digest)
        self.assertEqual(receipt['status'],'done')
        self.assertEqual((receipt['result']['updated'],receipt['result']['unchanged'],receipt['result']['skipped']),(0,499,2))

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
                c.execute('DROP TABLE runtime_state')
            legacy=Recovery(old);archive=legacy.create()
            self.app.recovery.validate(legacy.archive_path(archive['id']),dest)
            with sqlite3.connect(Path(dest)/'workbench.sqlite3') as c:
                self.assertEqual(c.execute('SELECT count(*) FROM products').fetchone()[0],1)
                self.assertEqual(c.execute('SELECT enabled FROM source_inbox_config').fetchone()[0],0)
                self.assertEqual(c.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='runtime_state'").fetchone()[0],1)


if __name__=='__main__':unittest.main()
