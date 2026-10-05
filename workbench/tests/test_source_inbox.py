import csv
import hashlib
import io
import os
import sqlite3
import sys
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import App
from source_inbox import SourceInbox,MAX_CATALOG_FILE
from recovery import Recovery
from core import Problem
from visuals import CHECKS
from media import ffmpeg

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

    def test_supplier_file_replaced_during_read_is_not_imported_from_stale_snapshot(self):
        self.enable()
        old_csv='商品名称,货源链接,规格货号,供应商,规格事实\n旧款盒,https://supplier.example/item/1.html,R1,工厂,红色塑料盒\n'
        new_csv='商品名称,货源链接,规格货号,供应商,规格事实\n新款盒,https://supplier.example/item/2.html,R1,工厂,红色塑料盒\n'
        self.assertEqual(len(old_csv.encode()),len(new_csv.encode()))
        path=self.put('changing.csv',old_csv)
        original_stat=path.stat()
        config={'enabled':1,'translate':0,'review':0}
        self.box.check_file(path,config)
        original_read_bytes=Path.read_bytes
        replaced=False
        def replace_after_snapshot(candidate):
            nonlocal replaced
            raw=original_read_bytes(candidate)
            if candidate==path and not replaced:
                path.write_text(new_csv)
                os.utime(path,ns=(original_stat.st_atime_ns,original_stat.st_mtime_ns))
                replaced=True
            return raw
        with patch.object(Path,'read_bytes',replace_after_snapshot):
            self.box.check_file(path,config)
        self.assertTrue(replaced)
        self.assertEqual(self.app.store.list(),[])
        self.box.check_file(path,config)
        products=self.app.store.list()
        self.assertEqual(len(products),1)
        self.assertEqual(products[0]['title_zh'],'新款盒')
        with self.app.store.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM source_inbox_files WHERE name='changing.csv'").fetchone()[0],1)

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

    def test_catalog_scan_rotates_independently_across_more_than_one_hundred_files(self):
        self.enable()
        for folder in (self.box.folder,self.box.updates):
            for index in range(205):
                (folder/f'supplier-{index:03d}.csv').write_text(CSV)
        checked={'new':[],'updates':[]}
        with patch.object(self.box,'check_file',side_effect=lambda path,config,kind:checked[kind].append(path.name)):
            self.box.tick()
            self.assertEqual(self.box.scan_progress['new'],{'checked':100,'total':205})
            self.assertEqual(self.box.scan_progress['updates'],{'checked':100,'total':205})
            self.assertEqual(checked['new'],[f'supplier-{i:03d}.csv' for i in range(100)])
            self.assertEqual(checked['updates'],[f'supplier-{i:03d}.csv' for i in range(100)])
        restarted=SourceInbox(self.app)
        self.assertEqual(restarted.catalog_cursors,{'new':'supplier-099.csv','updates':'supplier-099.csv'})
        with patch.object(restarted,'check_file',side_effect=lambda path,config,kind:checked[kind].append(path.name)):
            restarted.tick()
            self.assertEqual(restarted.scan_progress['new'],{'checked':100,'total':205})
            self.assertEqual(restarted.scan_progress['updates'],{'checked':100,'total':205})
            self.assertEqual(checked['new'][-100:],[f'supplier-{i:03d}.csv' for i in range(100,200)])
            self.assertEqual(checked['updates'][-100:],[f'supplier-{i:03d}.csv' for i in range(100,200)])
            restarted.tick()
        for kind in ('new','updates'):
            self.assertEqual(len(checked[kind]),300)
            self.assertEqual(set(checked[kind][:205]),{f'supplier-{i:03d}.csv' for i in range(205)})
            self.assertEqual(checked[kind][200:205],[f'supplier-{i:03d}.csv' for i in range(200,205)])
            self.assertEqual(checked[kind][205:],[f'supplier-{i:03d}.csv' for i in range(95)])
        self.assertEqual(restarted.scan_progress['new'],{'checked':100,'total':205})
        self.assertEqual(restarted.scan_progress['updates'],{'checked':100,'total':205})

    def test_catalog_scan_crash_before_checkpoint_rechecks_batch_after_restart(self):
        self.enable()
        for index in range(105):
            (self.box.folder/f'supplier-{index:03d}.csv').write_text(CSV)
        checked=[]
        def interrupt_after_first(path,config,kind):
            checked.append(path.name)
            raise SystemExit('synthetic interruption')
        with patch.object(self.box,'check_file',side_effect=interrupt_after_first):
            with self.assertRaises(SystemExit):self.box.tick()
        with self.app.store.connect() as c:
            self.assertIsNone(c.execute("SELECT cursor FROM source_inbox_cursor WHERE kind='new'").fetchone())
        restarted=SourceInbox(self.app)
        replayed=[]
        with patch.object(restarted,'check_file',side_effect=lambda path,config,kind:replayed.append(path.name)):
            restarted.tick()
        self.assertEqual(checked,['supplier-000.csv'])
        self.assertEqual(replayed,[f'supplier-{i:03d}.csv' for i in range(100)])
        self.assertEqual(restarted.catalog_cursors['new'],'supplier-099.csv')

    def test_photo_scan_resumes_after_restart(self):
        self.enable()
        folder=self.box.photos/'PHOTO-SKU';folder.mkdir()
        for index in range(2001):(folder/f'{index:05d}.jpg').write_bytes(b'x')
        checked=[]
        with patch.object(self.box,'check_photo',side_effect=lambda path,event:checked.append(path.name)), \
             patch.object(self.box,'schedule_visual'):
            self.box.tick()
        self.assertEqual(len(checked),2000)
        self.assertEqual(checked[-1],'01999.jpg')
        restarted=SourceInbox(self.app)
        self.assertEqual(restarted.photo_cursor,folder/'01999.jpg')
        resumed=[]
        with patch.object(restarted,'check_photo',side_effect=lambda path,event:resumed.append(path.name)), \
             patch.object(restarted,'schedule_visual'):
            restarted.tick()
        self.assertEqual(resumed[0],'02000.jpg')
        self.assertEqual(len(resumed),2000)
        self.assertEqual(restarted.scan_progress['photos'],{'checked':2000,'total':2001})

    def test_photo_import_receipt_is_reused_after_crash_before_cursor_checkpoint(self):
        pid=self.app.store.import_rows([{'title_zh':'中断恢复图片样本','source_url':'https://supplier.example/photo-replay',
            'source_sku':'PHOTO-REPLAY-Z','supplier':'合成工厂','facts':'灰色塑料收纳盒'}])['created'][0]
        self.enable(translate=True,review=True)
        self.box.configure_visual({'enabled':True,'recipe':{'shots':['detail','hero','scene'],'model':'gpt-6-sol',
            'aspect':'square','style':'统一柔和棚拍','brief':'保持商品事实','auto_check':False}})
        other=self.box.photos/'PHOTO-REPLAY-A';other.mkdir()
        for index in range(2000):(other/f'photo-{index:04d}.jpg').touch()
        folder=self.box.photos/'PHOTO-REPLAY-Z';folder.mkdir()
        photo=folder/'front.png';Image.new('RGB',(800,800),'gray').save(photo)
        rights=folder/'rights.txt';rights.write_text('合成测试图片，授权用于此商品')
        old=time.time()-120;os.utime(photo,(old,old));os.utime(rights,(old,old))
        original=self.box.check_photo
        interrupted=False
        def import_then_crash(path,event):
            nonlocal interrupted
            if path!=photo:return
            original(path,event)
            if interrupted:return
            with self.app.store.connect() as c:
                receipt=c.execute("SELECT status FROM source_inbox_files WHERE name='photos/PHOTO-REPLAY-Z/front.png'").fetchone()
            if receipt and receipt['status']=='done':
                interrupted=True
                raise SystemExit('synthetic crash after durable photo receipt')
        with patch.object(self.box,'check_photo',side_effect=import_then_crash):
            self.box.tick()
            self.assertEqual(self.box.photo_cursor.name,'photo-1999.jpg')
            self.box.tick()
            self.assertEqual(self.box.photo_cursor.name,'photo-1998.jpg')
            with self.assertRaisesRegex(SystemExit,'durable photo receipt'):self.box.tick()
        self.assertTrue(interrupted)
        with self.app.store.connect() as c:
            self.assertEqual(c.execute("SELECT cursor FROM source_inbox_cursor WHERE kind='photos'").fetchone()['cursor'],'PHOTO-REPLAY-A/photo-1998.jpg')
            self.assertEqual(c.execute('SELECT count(*) FROM media_assets WHERE json_extract(data,\'$.product_id\')=?',(pid,)).fetchone()[0],1)

        restarted=SourceInbox(self.app)
        def replay_only_target(path,event):
            if path==photo:original(path,event)
        with patch.object(restarted,'check_photo',side_effect=replay_only_target), \
             patch.object(self.app.models,'ready',return_value=True), \
             patch.object(self.app.visuals,'kick'):
            restarted.tick();restarted.tick()
        self.assertEqual(self.app.automation.state()['total'],1)
        restarted_again=SourceInbox(self.app)
        with patch.object(restarted_again,'check_photo',side_effect=replay_only_target), \
             patch.object(self.app.models,'ready',return_value=True), \
             patch.object(self.app.visuals,'kick'):
            restarted_again.tick();restarted_again.tick()
        self.assertEqual(self.app.automation.state()['total'],1)
        with self.app.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM media_assets WHERE json_extract(data,\'$.product_id\')=?',(pid,)).fetchone()[0],1)
            self.assertEqual(c.execute("SELECT count(*) FROM source_inbox_files WHERE name='photos/PHOTO-REPLAY-Z/front.png' AND status='done'").fetchone()[0],1)
            self.assertEqual(c.execute("SELECT count(*) FROM automation_runs WHERE request_key LIKE 'inbox-%'").fetchone()[0],1)
        record=restarted_again.file_detail('photos/PHOTO-REPLAY-Z/front.png',hashlib.sha256(photo.read_bytes()+b'\0'+rights.read_bytes()).hexdigest())
        self.assertEqual(record['result']['product_id'],pid)

    def test_video_scan_resumes_after_restart(self):
        self.enable()
        folder=self.box.videos/'VIDEO-SKU';folder.mkdir()
        for index in range(501):(folder/f'{index:05d}.mp4').write_bytes(b'x')
        checked=[]
        with patch.object(self.box,'check_video',side_effect=lambda path,event:checked.append(path.name) or None):
            self.box.tick()
        self.assertEqual(len(checked),500)
        self.assertEqual(checked[-1],'00499.mp4')
        restarted=SourceInbox(self.app)
        self.assertEqual(restarted.video_cursor,folder/'00499.mp4')
        resumed=[]
        with patch.object(restarted,'check_video',side_effect=lambda path,event:resumed.append(path.name) or None):
            restarted.tick()
        self.assertEqual(resumed[0],'00500.mp4')
        self.assertEqual(len(resumed),500)
        self.assertEqual(restarted.scan_progress['videos'],{'checked':500,'total':501})

    def test_original_video_inbox_imports_optimizes_and_reconciles_exactly_once(self):
        pid=self.app.store.import_rows([{'title_zh':'原视频端到端样本','source_url':'https://supplier.example/video-e2e',
            'source_sku':'VIDEO-E2E-1','supplier':'合成工厂','facts':'合成视频测试商品'}])['created'][0]
        self.enable()
        self.box.configure_video({'enabled':True,'recipe':{'aspect':'square','mute':True,'max_seconds':3,'cover':True}})
        self.app.media.start()
        self.app.media.control({'action':'pause'})
        folder=self.box.videos/'VIDEO-E2E-1';folder.mkdir()
        video=folder/'source.mp4'
        subprocess.run([ffmpeg(),'-hide_banner','-loglevel','error','-f','lavfi','-i',
            'color=c=blue:s=320x240:r=25:d=3','-f','lavfi','-i','sine=frequency=440:duration=3',
            '-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-shortest','-y',str(video)],check=True)
        rights=folder/'rights.txt';rights.write_text('合成测试视频，授权用于此商品')
        old=time.time()-120
        os.utime(video,(old,old));os.utime(rights,(old,old))
        source_bytes=video.read_bytes();source_sha=hashlib.sha256(source_bytes).hexdigest()

        self.box.tick()  # first observation establishes the stability snapshot
        self.box.tick()  # stable exact-SKU import and idempotent optimization request
        with self.app.store.connect() as c:
            original=c.execute("SELECT id,data FROM media_assets WHERE json_extract(data,'$.sha256')=?",(source_sha,)).fetchone()
            self.assertIsNotNone(original)
            original_data=__import__('json').loads(original['data'])
            self.assertEqual(original_data['product_id'],pid)
            self.assertEqual(original_data['rights'],'合成测试视频，授权用于此商品')
            self.assertEqual((self.app.media.root/original_data['file']).read_bytes(),source_bytes)
            tasks=[dict(row) for row in c.execute('SELECT id,status,request_key FROM media_tasks ORDER BY request_key')]
            self.assertEqual(len(tasks),2)
            self.assertTrue(all(row['request_key'].startswith('inbox-video-') for row in tasks))
            self.assertEqual([task['status'] for task in tasks],['queued','queued'])
        self.box.configure_video({'enabled':True,'recipe':{'aspect':'portrait','mute':True,'max_seconds':4,'cover':False}})
        self.box.tick()  # changed recipe waits for the original queued work
        with self.app.store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM media_tasks').fetchone()[0],2)
        self.box.configure_video({'enabled':False,'recipe':{'aspect':'landscape','mute':True,'max_seconds':5,'cover':False}})
        self.box.tick()  # disabled automation reconciles existing work, but adds none
        with self.app.store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM media_tasks').fetchone()[0],2)
        self.app.media.control({'action':'resume'})

        deadline=time.monotonic()+35
        while time.monotonic()<deadline:
            with self.app.store.connect() as c:
                statuses=[row['status'] for row in c.execute('SELECT status FROM media_tasks')]
            if statuses and all(status=='done' for status in statuses):break
            if any(status in ('failed','cancelled','interrupted') for status in statuses):
                self.fail(f'自动优化任务异常结束：{statuses}')
            time.sleep(.05)
        self.assertEqual(statuses,['done','done'])

        self.assertIn('videos/VIDEO-E2E-1/source.mp4',self.box.video_ready)
        with self.app.store.connect() as c:self.assertFalse(self.box.video_config(c)['enabled'])
        with patch.object(self.box,'schedule_video',wraps=self.box.schedule_video) as reconcile, \
             patch.object(self.box,'check_video',wraps=self.box.check_video) as check_video:
            self.box.tick()
            self.box.tick()
            self.assertGreater(reconcile.call_count,0,{'video_ready':self.box.video_ready,'progress':self.box.scan_progress,
                'last_error':self.box.last_error,'cursor':str(self.box.video_cursor),'check_calls':check_video.call_count})
        digest=hashlib.sha256(source_bytes+b'\0'+rights.read_bytes()).hexdigest()
        receipt=self.box.file_detail('videos/VIDEO-E2E-1/source.mp4',digest)
        self.assertEqual(receipt['status'],'done')
        self.assertEqual(receipt['result']['product_id'],pid)
        self.assertEqual(receipt['result']['asset_id'],original['id'])
        marker=hashlib.sha256(__import__('json').dumps([original['id'],source_sha,
            {'aspect':'square','mute':True,'max_seconds':3,'cover':True}],sort_keys=True).encode()).hexdigest()
        optimized=self.box.file_detail('videos/VIDEO-E2E-1/source.mp4/自动优化',marker)
        self.assertEqual(optimized['status'],'done')
        self.assertEqual(optimized['result']['task_ids'],[row['id'] for row in tasks])
        with self.app.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM media_tasks').fetchone()[0],2)
            assets=[__import__('json').loads(row['data']) for row in c.execute(
                "SELECT data FROM media_assets WHERE json_extract(data,'$.product_id')=?",(pid,))]
        self.assertEqual(sum(asset['sha256']==source_sha for asset in assets),1)
        self.assertTrue(any(asset['kind']=='video' and asset['sha256']!=source_sha for asset in assets))
        self.assertTrue(any(asset['kind']=='image' for asset in assets))

    def test_unchanged_or_empty_scan_does_not_write_cursor_rows(self):
        self.enable()
        with self.app.store.connect() as c:
            c.executescript('''CREATE TABLE cursor_write_log(kind TEXT NOT NULL);
                CREATE TRIGGER log_cursor_insert AFTER INSERT ON source_inbox_cursor BEGIN
                    INSERT INTO cursor_write_log VALUES(NEW.kind);
                END;
                CREATE TRIGGER log_cursor_update AFTER UPDATE ON source_inbox_cursor BEGIN
                    INSERT INTO cursor_write_log VALUES(NEW.kind);
                END;''')
        self.box.tick();self.box.tick()
        with self.app.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM cursor_write_log').fetchone()[0],0)
        self.put('supplier.csv',CSV)
        with patch.object(self.box,'check_file') as check_file:
            self.box.tick()
            with self.app.store.connect() as c:
                self.assertEqual(c.execute('SELECT kind FROM cursor_write_log').fetchall()[0]['kind'],'new')
                self.assertEqual(c.execute('SELECT count(*) FROM cursor_write_log').fetchone()[0],1)
            self.box.tick()
        check_file.assert_called()
        with self.app.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM cursor_write_log').fetchone()[0],1)

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

    def test_photo_rights_replaced_during_read_is_not_imported_with_stale_basis(self):
        self.enable()
        self.app.store.import_rows([{'title_zh':'授权竞态图片','source_sku':'RIGHTS-PHOTO'}])
        folder=self.box.photos/'RIGHTS-PHOTO';folder.mkdir()
        photo=folder/'front.png';Image.new('RGB',(800,800),'gray').save(photo)
        rights=folder/'rights.txt';rights.write_text('合成授权依据版本甲',encoding='utf-8')
        self.assertEqual(len(rights.read_bytes()),len('合成授权依据版本乙'.encode()))
        old=time.time()-120;os.utime(photo,(old,old));os.utime(rights,(old,old))
        self.box.check_photo(photo,0)
        original_stat=rights.stat();original_read_bytes=Path.read_bytes;replaced=False
        def replace_after_snapshot(path):
            nonlocal replaced
            raw=original_read_bytes(path)
            if path==rights and not replaced:
                rights.write_text('合成授权依据版本乙',encoding='utf-8')
                os.utime(rights,ns=(original_stat.st_atime_ns,original_stat.st_mtime_ns))
                replaced=True
            return raw
        with patch.object(Path,'read_bytes',replace_after_snapshot):
            self.box.check_photo(photo,0)
        self.assertTrue(replaced)
        self.assertEqual(self.app.media.state()['assets'],[])
        self.box.check_photo(photo,0)  # observe the changed marker
        self.box.check_photo(photo,0)  # process only the stable new authorization
        assets=self.app.media.state()['assets']
        self.assertEqual(len(assets),1)
        self.assertEqual(assets[0]['rights'],'合成授权依据版本乙')

    def test_invalid_photo_rights_is_recorded_once_and_recovers_when_fixed(self):
        self.enable()
        self.app.store.import_rows([{'title_zh':'无效授权编码图片','source_sku':'INVALID-RIGHTS'}])
        folder=self.box.photos/'INVALID-RIGHTS';folder.mkdir()
        photo=folder/'front.png';Image.new('RGB',(800,800),'gray').save(photo)
        rights=folder/'rights.txt';rights.write_bytes(b'\xff\xfeinvalid utf8')
        old=time.time()-120;os.utime(photo,(old,old));os.utime(rights,(old,old))
        self.box.check_photo(photo,0)  # stable marker observation
        self.box.check_photo(photo,0)  # persist a visible encoding issue
        rows=self.box.list_files(group='all',kind='photos')['files']
        issue=next(row for row in rows if row['name']=='photos/INVALID-RIGHTS/front.png')
        self.assertEqual(issue['status'],'attention')
        self.assertIn('UTF-8',issue['message'])
        self.assertEqual(self.app.media.state()['assets'],[])
        with self.app.store.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM source_inbox_files WHERE name='photos/INVALID-RIGHTS/front.png'").fetchone()[0],1)
        self.box.check_photo(photo,0)
        with self.app.store.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM source_inbox_files WHERE name='photos/INVALID-RIGHTS/front.png'").fetchone()[0],1)
        rights.write_text('合成测试授权依据，UTF-8 修复版',encoding='utf-8')
        os.utime(rights,(old,old))
        self.box.check_photo(photo,0)  # new marker observation
        self.box.check_photo(photo,0)  # import only with corrected text
        asset=self.app.media.state()['assets'][0]
        self.assertEqual(asset['rights'],'合成测试授权依据，UTF-8 修复版')
        self.assertEqual(self.box.state()['files'][0]['status'],'done')

    def test_video_rights_replaced_during_read_stops_ingest(self):
        self.enable()
        folder=self.box.videos/'RIGHTS-VIDEO';folder.mkdir()
        video=folder/'clip.mp4';video.write_bytes(b'synthetic video placeholder')
        rights=folder/'rights.txt';rights.write_text('视频授权依据版本甲',encoding='utf-8')
        old=time.time()-120;os.utime(video,(old,old));os.utime(rights,(old,old))
        self.box.check_video(video,0)
        original_stat=rights.stat();original_read_bytes=Path.read_bytes;replaced=False
        def replace_after_snapshot(path):
            nonlocal replaced
            raw=original_read_bytes(path)
            if path==rights and not replaced:
                rights.write_text('视频授权依据版本乙',encoding='utf-8')
                os.utime(rights,ns=(original_stat.st_atime_ns,original_stat.st_mtime_ns))
                replaced=True
            return raw
        with patch.object(Path,'read_bytes',replace_after_snapshot), \
             patch.object(self.box.video_import,'ingest',side_effect=Problem('synthetic ingest should not run')) as ingest:
            self.box.check_video(video,0)
        self.assertTrue(replaced)
        ingest.assert_not_called()
        self.assertEqual(self.app.media.state()['assets'],[])

    def test_references_manifest_replaced_during_read_is_rejected(self):
        folder=self.box.photos/'MANIFEST-RACE';folder.mkdir()
        photos=[folder/'front-A.png',folder/'front-B.png']
        for photo in photos:photo.touch()
        manifest=folder/'references.txt';manifest.write_text('front-A.png\n',encoding='utf-8')
        self.assertEqual(manifest.stat().st_size,len('front-B.png\n'.encode()))
        original_stat=manifest.stat()
        original_read_bytes=Path.read_bytes
        replaced=False
        def replace_after_snapshot(path):
            nonlocal replaced
            raw=original_read_bytes(path)
            if path==manifest and not replaced:
                manifest.write_text('front-B.png\n',encoding='utf-8')
                os.utime(manifest,ns=(original_stat.st_atime_ns,original_stat.st_mtime_ns))
                replaced=True
            return raw
        with patch.object(Path,'read_bytes',replace_after_snapshot):
            with self.assertRaisesRegex(Problem,'读取期间发生变化'):
                self.box.visual_reference_photos(folder,photos)
        self.assertTrue(replaced)
        self.assertEqual(manifest.read_text(encoding='utf-8'),'front-B.png\n')

    def test_arrived_rights_documented_photo_runs_bilingual_draft_to_visual_human_gate(self):
        pid=self.app.store.import_rows([{'title_zh':'视觉投递样本','source_url':'https://supplier.example/visual',
            'source_sku':'VISUAL-SKU','supplier':'合成工厂','facts':'灰色塑料收纳盒'}])['created'][0]
        self.enable(translate=True,review=True)
        self.box.configure_visual({'enabled':True,'recipe':{'shots':['detail','hero','scene'],'model':'gpt-6-sol',
            'aspect':'square','style':'统一柔和棚拍','brief':'保持商品事实','auto_check':False}})
        folder=self.box.photos/'VISUAL-SKU';folder.mkdir()
        photo=folder/'front.png';Image.new('RGB',(800,800),'gray').save(photo)
        rights=folder/'rights.txt';rights.write_text('合成浏览器测试素材，允许用于本商品重新制作')
        old=time.time()-120
        os.utime(photo,(old,old));os.utime(rights,(old,old))

        with patch.object(self.app.models,'ready',return_value=True):
            self.box.tick();self.assertEqual(self.app.automation.state()['total'],0)
            self.box.tick()
        state=self.app.automation.state()
        self.assertEqual(state['total'],1)
        self.assertEqual(len(state['items']),1)
        item=state['items'][0];plan=state['runs'][0]['plan']
        self.assertEqual(item['product_id'],pid);self.assertEqual(item['status'],'queued')
        self.assertEqual(plan['ai_visual']['shots'],['hero','scene','detail'])
        self.assertEqual(len(plan['ai_visual']['reference_asset_ids']),1)
        self.assertIn('approval',plan['steps'])
        photo_records=self.box.list_files(group='all',kind='photos')['files']
        self.assertTrue(any(record['name']=='photos/VISUAL-SKU/front.png' for record in photo_records),self.box.state())
        photo_record=next(record for record in photo_records if record['name']=='photos/VISUAL-SKU/front.png')
        source_record=self.box.file_detail(photo_record['name'],photo_record['digest'])
        self.assertEqual(source_record['status'],'done')
        self.assertEqual(source_record['result']['product_id'],pid)
        asset=self.app.media.get(source_record['result']['asset_id'])
        self.assertEqual(asset['rights'],'合成浏览器测试素材，允许用于本商品重新制作')
        kick=patch.object(self.app.visuals,'kick');kick.start();self.addCleanup(kick.stop)
        translated={'content':{'title_en':'Gray Storage Box','description_en':'Gray plastic storage box.',
            'title_ar':'صندوق تخزين رمادي','description_ar':'صندوق تخزين بلاستيكي رمادي.'},
            'warnings':[],'numeric_issues':[],'call_id':'synthetic-translate','model':'fixture'}
        reviewed={'passed':True,'warnings':[],'call_id':'synthetic-review','model':'fixture'}
        with patch.object(self.app.models,'translate',return_value=translated) as translate, \
             patch.object(self.app.models,'review',return_value=reviewed) as review:
            for _ in range(4):self.app.automation.tick()
        self.assertEqual(translate.call_count,1);self.assertEqual(review.call_count,1)
        current=self.app.store.get(pid)
        self.assertEqual((current['title_en'],current['title_ar']),('Gray Storage Box','صندوق تخزين رمادي'))
        self.assertFalse(current['content_verified']);self.assertFalse(current['reviewed'])
        state=self.app.automation.state();item=next(row for row in state['items'] if row['product_id']==pid)
        self.assertEqual((item['step'],item['status']),(3,'waiting'))
        self.assertEqual(state['runs'][0]['plan']['steps'][item['step']],'ai_visual')
        self.assertIn('approval',state['runs'][0]['plan']['steps'])
        link=item['data']['ai_visual'];jobs=[self.app.visuals.get(jid) for jid in link['job_ids']]
        self.assertEqual([job['recipe']['shot'] for job in jobs],['hero','scene','detail'])
        self.assertTrue(all(job['status']=='queued' and job['recipe']['references'][0]['id']==asset['id'] for job in jobs))
        for job in jobs:
            shot=job['recipe']['shot'];output=Path(self.tmp.name)/(shot+'.png')
            image=Image.new('RGB',(1600,1600),'white');image.paste({'hero':'#333333','scene':'#666666','detail':'#999999'}[shot],(400,400,1200,1200));image.save(output);image.close()
            generated=self.app.media.ingest(output,'合成成片 '+shot,'合成 QA',pid)
            self.app.visuals.update(job['id'],'candidate','合成候选成片',asset_id=generated['id'])
        self.app.automation.tick(force_waiting=True)
        held=next(row for row in self.app.automation.state()['items'] if row['product_id']==pid)
        self.assertEqual(held['status'],'approval');self.assertEqual(self.app.store.get(pid)['images'],[])
        for job in jobs:
            current_job=self.app.visuals.get(job['id'])
            self.app.visuals.review({'id':job['id'],'expected_updated_at':current_job['updated_at'],
                'decision':'approved','checks':{check:True for check in CHECKS}})
        for _ in range(3):self.app.automation.tick(force_waiting=True)
        current=self.app.store.get(pid)
        held=next(row for row in self.app.automation.state()['items'] if row['product_id']==pid)
        self.assertEqual((held['step'],held['status']),(5,'approval'))
        self.assertEqual([image['media_asset_id'] for image in current['images']],
                         [self.app.visuals.get(jid)['asset_id'] for jid in link['job_ids']])
        self.assertFalse(current['images_verified']);self.assertFalse(current['reviewed'])
        self.box.tick()
        self.assertEqual(self.app.automation.state()['total'],1)
        self.assertEqual({job['status'] for job in self.app.visuals.state()['jobs']},{'approved'})
        self.assertEqual(self.app.models.state()['calls'],[])
        with self.app.store.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM jobs WHERE product_id=? AND kind='submit'",(pid,)).fetchone()[0],0)

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

    def test_ambiguous_existing_source_identity_remains_blocked_across_batches(self):
        self.enable()
        url='https://supplier.example/ambiguous-item'
        identity={'source_url':url,'source_sku':'SAME','supplier':'工厂','facts':'红色塑料盒'}
        existing=self.app.store.import_rows([{'title_zh':'既有商品甲',**identity}])['created'][0]
        with self.app.store.connect() as c:
            c.execute('INSERT INTO products(id,source_key,data,revision,approved_revision,created_at,updated_at) SELECT ?,?,data,revision,approved_revision,created_at,updated_at FROM products WHERE id=?',('legacy-copy',url+'|SAME',existing))
        output=io.StringIO(newline='');writer=csv.writer(output)
        writer.writerow(['商品名称','货源链接','规格货号','供应商','规格事实'])
        rows=[['待核对商品',url,'SAME','工厂','红色塑料盒'],
              ['待核对商品',url,'SAME','工厂','红色塑料盒']]
        rows.extend([[f'新商品{n}',f'https://supplier.example/new/{n}',f'N{n}','工厂','蓝色塑料盒'] for n in range(499)])
        writer.writerows(rows);content=output.getvalue()
        digest=hashlib.sha256(content.encode()).hexdigest()
        self.box.process_large_catalog('ambiguous.csv',digest,'.csv',content,{'enabled':1,'translate':0,'review':0})
        receipt=self.box.file_detail('ambiguous.csv',digest)['result']
        issues={item['row']:item for item in receipt['issue_rows']}
        self.assertEqual(issues[1]['status'],'blocked')
        self.assertEqual(issues[2]['status'],'blocked')
        self.assertIn('多份商品档案',issues[1]['reason'])
        self.assertIn('多份商品档案',issues[2]['reason'])
        self.assertEqual(len(self.app.store.list()),501)

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

    def test_large_catalog_pipe_characters_do_not_alias_across_batch_boundary(self):
        self.enable()
        output=io.StringIO(newline='');writer=csv.writer(output)
        writer.writerow(['商品名称','货源链接','规格货号','供应商','规格事实'])
        rows=[['普通商品'+str(n),f'https://supplier.example/item/{n}','SKU-'+str(n),'工厂','蓝色塑料盒'] for n in range(2,501)]
        rows.insert(0,['链接甲','https://supplier.example/a|b','c','工厂','红色盒'])
        rows.append(['链接乙','https://supplier.example/a','b|c','工厂','绿色盒'])
        writer.writerows(rows);content=output.getvalue();digest=hashlib.sha256(content.encode()).hexdigest()
        self.assertTrue(self.box.process_large_catalog('pipe-collision.csv',digest,'.csv',content,{'enabled':1,'translate':0,'review':0}))
        self.assertEqual(len(self.app.store.list()),501)
        receipt=self.box.file_detail('pipe-collision.csv',digest)['result']
        self.assertEqual((receipt['cataloged'],receipt['skipped']),(501,0))
        self.assertEqual([row['row'] for row in receipt['created_rows']],[*range(1,502)])

    def test_identical_large_catalog_batches_report_each_deduplicated_row(self):
        self.enable()
        output=io.StringIO(newline='');writer=csv.writer(output)
        writer.writerow(['商品名称','供应商','规格事实'])
        writer.writerows([['同款商品','工厂','红色塑料盒'] for _ in range(1000)])
        content=output.getvalue();path=self.put('identical.csv',content)
        self.box.tick();self.box.tick()
        self.assertEqual(len(self.app.store.list()),1)
        digest=hashlib.sha256(content.encode()).hexdigest()
        receipt=self.box.file_detail('identical.csv',digest)['result']
        self.assertEqual((receipt['cataloged'],receipt['skipped']),(1,999))
        self.assertEqual([item['row'] for item in receipt['issue_rows']],[*range(2,1001)])
        self.assertTrue(all(item['status']=='duplicate' for item in receipt['issue_rows']))
        self.assertTrue(all('缺少货源链接' in item['reason'] for item in receipt['issue_rows']))

    def test_distinct_unlinked_large_catalog_rows_are_not_merged(self):
        """Without a source link, only fully identical rows may be deduplicated."""
        self.enable()
        output=io.StringIO(newline='');writer=csv.writer(output)
        writer.writerow(['商品名称','供应商','规格事实'])
        writer.writerows([[f'商品{i % 2}','工厂','红色塑料盒'] for i in range(1000)])
        content=output.getvalue()
        config={'enabled':1,'translate':0,'review':0}
        digest=hashlib.sha256(content.encode()).hexdigest()
        self.box.process_large_catalog('alternating.csv',digest,'.csv',content,config)
        receipt=self.box.file_detail('alternating.csv',digest)['result']
        self.assertEqual(len(self.app.store.list()),2,receipt)
        self.assertEqual((receipt['cataloged'],receipt['skipped']),(2,998))
        self.assertEqual([item['row'] for item in receipt['created_rows']],[1,2])
        self.assertEqual([item['row'] for item in receipt['issue_rows']],[*range(3,1001)])
        self.assertTrue(all(item['status']=='duplicate' for item in receipt['issue_rows']))

    def test_identical_large_catalog_batches_stay_deduplicated_after_restart(self):
        self.enable()
        output=io.StringIO(newline='');writer=csv.writer(output)
        writer.writerow(['商品名称','供应商','规格事实'])
        writer.writerows([['同款商品','工厂','红色塑料盒'] for _ in range(1000)])
        content=output.getvalue();path=self.put('identical-restart.csv',content)
        digest=hashlib.sha256(content.encode()).hexdigest()
        config={'enabled':1,'translate':0,'review':0}
        original_record=self.box.record
        def interrupt_after_first_batch(name,file_digest,status,message,result=None):
            original_record(name,file_digest,status,message,result)
            if status=='processing' and message.startswith('已处理 1/2 批'):
                raise RuntimeError('simulated termination after identical batch commit')
        with patch.object(self.box,'record',side_effect=interrupt_after_first_batch):
            with self.assertRaisesRegex(RuntimeError,'identical batch commit'):
                self.box.process_large_catalog('identical-restart.csv',digest,'.csv',content,config)
        self.assertEqual(len(self.app.store.list()),1)
        restarted=SourceInbox(self.app)
        restarted.check_file(path,config);restarted.check_file(path,config)
        self.assertEqual(len(self.app.store.list()),1)
        receipt=restarted.file_detail('identical-restart.csv',digest)['result']
        self.assertEqual((receipt['cataloged'],receipt['skipped']),(1,999))
        self.assertEqual([item['row'] for item in receipt['issue_rows']],[*range(2,1001)])
        self.assertEqual([item['row'] for item in receipt['created_rows']],[1])
        self.assertTrue(receipt['mapping_complete'])
        with self.app.store.connect() as c:
            self.assertEqual(c.execute("SELECT count(*) FROM ops_requests WHERE key LIKE 'source-import:%'").fetchone()[0],1)

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
