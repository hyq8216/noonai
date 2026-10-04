"""Real image/video pipeline tests; no external model or marketplace calls."""
import io
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import quote
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PIL import Image,ImageDraw
from core import Store, Problem
from media import Media, ffmpeg, probe_video
from server import App, Handler, ThreadingHTTPServer

class MediaTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.store=Store(self.root);self.media=Media(self.store);self.media.start()
    def tearDown(self):
        self.media.close();self.tmp.cleanup()
    def image(self,color='red'):
        p=self.root/(uuid.uuid4().hex+'.png');Image.new('RGB',(800,600),color).save(p)
        a=self.media.ingest(p,p.name,'synthetic test fixture')
        self.assertEqual(p.read_bytes(),(self.media.root/a['file']).read_bytes())
        return a
    def video(self):
        p=self.root/'input.mp4'
        subprocess.run([ffmpeg(),'-hide_banner','-loglevel','error','-f','lavfi','-i','color=c=blue:s=320x240:r=25:d=3','-f','lavfi','-i','sine=frequency=440:duration=3','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-shortest','-y',str(p)],check=True)
        return self.media.ingest(p,'test.mp4','synthetic test fixture')
    def submit(self,kind,assets,**fields):
        return self.media.submit({'request_id':uuid.uuid4().hex,'recipes':[{'kind':kind,'asset_ids':[a['id'] for a in assets],**fields}]})['task_ids'][0]
    def wait(self,jid,status='done'):
        deadline=time.monotonic()+35
        while time.monotonic()<deadline:
            t=next(t for t in self.media.state()['tasks'] if t['id']==jid)
            if t['status'] in ('done','failed','cancelled','interrupted'):
                self.assertEqual(t['status'],status,t['message']);return t
            time.sleep(.05)
        self.fail('Task did not finish: '+jid)
    def test_original_validation(self):
        self.image()
        p=self.root/'bad.png';p.write_bytes(b'not an image')
        with self.assertRaises(Problem):self.media.ingest(p,'bad.png','test')
        self.assertEqual(len(self.media.state()['assets']),1)
    def test_templates_and_exact_product_attachment(self):
        a=self.image()
        for kind,size in [('square',(1600,1600)),('portrait',(1600,2000)),('feature',(1600,1600))]:
            t=self.wait(self.submit(kind,[a],title='Test product',caption='Source facts only'))
            out=self.media.get(t['result'][0]);path=self.media.root/out['file']
            with Image.open(path) as im:
                self.assertEqual(im.size,size);self.assertEqual(im.mode,'RGB');self.assertTrue(im.info['icc_profile'])
            p=self.store.get(self.store.import_rows([{'title_zh':kind}])['created'][0])
            updated=self.media.attach({'asset_id':out['id'],'product_id':p['id'],'revision':p['revision']})
            image=updated['images'][0]
            self.assertEqual(image['size'],list(size));self.assertEqual(path.read_bytes(),(self.store.assets/image['file']).read_bytes())
            self.assertFalse(updated['images_verified'])
            before=set(self.store.assets.iterdir())
            with self.assertRaises(Problem):self.media.attach({'asset_id':a['id'],'product_id':p['id'],'revision':p['revision']})
            self.assertEqual(before,set(self.store.assets.iterdir()))
            with self.assertRaises(Problem):self.media.attach({'asset_id':out['id'],'product_id':p['id'],'revision':updated['revision']})

    def test_arabic_feature_template_uses_fallback_without_raqm(self):
        a=self.image()
        with patch('media.features.check',return_value=False):
            task=self.wait(self.submit('feature',[a],title='منتج عالي الجودة',caption='تفاصيل المنتج للاستخدام اليومي'))
        out=self.media.get(task['result'][0])
        with Image.open(self.media.root/out['file']) as rendered:
            title=rendered.crop((80,1160,1520,1310)).convert('L').point(lambda value:255 if value<180 else 0)
            caption=rendered.crop((80,1350,1520,1550)).convert('L').point(lambda value:255 if value<180 else 0)
            self.assertIsNotNone(title.getbbox());self.assertLess(title.getbbox()[3],75)
            self.assertIsNotNone(caption.getbbox());self.assertLess(caption.getbbox()[3],55)
            self.assertEqual(rendered.size,(1600,1600))
        from media import draw_text
        canvas=Image.new('RGB',(300,180),'white')
        with patch('media.features.check',return_value=False):
            draw_text(ImageDraw.Draw(canvas),'المنتج'*8,(0,0,300,180),24)
        ink=canvas.convert('L').point(lambda value:255 if value<180 else 0).getbbox()
        self.assertIsNotNone(ink);self.assertGreater(ink[0],0);self.assertLessEqual(ink[2],300);self.assertLessEqual(ink[3],180)
    def test_batch_atomic_idempotent_pause_cancel_retry(self):
        a=self.image();self.media.control({'action':'pause'})
        b={'request_id':'same','recipes':[{'kind':'square','asset_ids':[a['id']]}]}
        one=self.media.submit(b);self.assertEqual(one,self.media.submit(b))
        with self.assertRaises(Problem):self.media.submit({**b,'recipes':[{'kind':'portrait','asset_ids':[a['id']]}]})
        with self.assertRaises(Problem):self.media.submit({'request_id':'invalid','recipes':b['recipes']+[{'kind':'video','asset_ids':[a['id']]}]})
        self.assertEqual(len(self.media.state()['tasks']),1)
        jid=one['task_ids'][0];self.assertEqual(self.media.task_status(jid),'queued')
        self.media.control({'action':'cancel','id':jid});self.wait(jid,'cancelled')
        self.media.control({'action':'retry','id':jid});self.media.control({'action':'resume'});self.wait(jid)
    def test_collage_order_and_slideshow_frames(self):
        red=self.image('red');blue=self.image('blue')
        t=self.wait(self.submit('collage',[red,blue]));out=self.media.get(t['result'][0])
        with Image.open(self.media.root/out['file']) as im:
            self.assertGreater(im.getpixel((400,800))[0],240);self.assertGreater(im.getpixel((1200,800))[2],240)
        t=self.wait(self.submit('slideshow',[red,blue],seconds=1));out=self.media.get(t['result'][0]);p=self.media.root/out['file']
        self.assertEqual((out['width'],out['height']),(1080,1080));self.assertAlmostEqual(out['duration'],2,delta=.1)
        for second,channel in [(.3,0),(1.3,2)]:
            raw=subprocess.check_output([ffmpeg(),'-hide_banner','-loglevel','error','-ss',str(second),'-i',str(p),'-frames:v','1','-f','image2pipe','-vcodec','png','-'])
            with Image.open(io.BytesIO(raw)) as im:self.assertGreater(im.getpixel((540,540))[channel],240)
    def test_video_trim_audio_cover_and_empty_duration(self):
        a=self.video();self.assertTrue(a['audio'])
        self.assertEqual(self.media.recipe({'kind':'video','asset_ids':[a['id']],'start':1,'duration':''})['duration'],2)
        for mute in [False,True]:
            t=self.wait(self.submit('video',[a],start=.5,duration=1,aspect='portrait',mute=mute));out=self.media.get(t['result'][0])
            self.assertEqual((out['width'],out['height']),(1080,1920));self.assertEqual(out['audio'],not mute);self.assertAlmostEqual(out['duration'],1,delta=.1)
        t=self.wait(self.submit('cover',[a],start=2,duration=999));self.assertEqual(self.media.get(t['result'][0])['kind'],'image')
    def test_restart_interrupted_and_saved_recipe(self):
        a=self.image();self.media.control({'action':'pause'});jid=self.submit('square',[a])
        tid=self.media.save_template({'name':'Preset','recipe':{'kind':'square','asset_ids':[a['id']]}})['id']
        self.media.close()
        with self.store.connect() as c:c.execute("UPDATE media_tasks SET status='running' WHERE id=?",(jid,))
        self.media=Media(self.store);self.media.start()
        self.assertEqual(self.media.task_status(jid),'interrupted');self.assertTrue(self.media.state()['paused'])
        preset=self.media.state()['templates'][0];self.assertEqual(preset['id'],tid);self.assertNotIn('asset_ids',preset['recipe'])
        self.media.control({'action':'retry','id':jid});self.media.control({'action':'resume'});self.wait(jid)
    def test_running_cancel_leaves_no_output(self):
        a=self.image();entered=threading.Event()
        def waiting(jid,recipe):
            entered.set()
            while True:self.media.check_cancel(jid);time.sleep(.02)
        with patch.object(self.media,'run',waiting):
            jid=self.submit('square',[a]);self.assertTrue(entered.wait(3));self.media.control({'action':'cancel','id':jid});self.wait(jid,'cancelled')
        self.assertEqual(len(self.media.state()['assets']),1)
    def test_attach_concurrent_edit_removes_uncommitted_files(self):
        a=self.image();p=self.store.get(self.store.import_rows([{'title_zh':'Concurrent'}])['created'][0]);before=set(self.store.assets.iterdir())
        with patch.object(self.store,'update',side_effect=Problem('conflict',409)):
            with self.assertRaises(Problem):self.media.attach({'asset_id':a['id'],'product_id':p['id'],'revision':p['revision']})
        self.assertEqual(before,set(self.store.assets.iterdir()))
    def test_cancel_stops_live_encoder(self):
        a=self.image()
        def encode(jid,recipe):
            self.media.command(['-re','-f','lavfi','-i','color=c=red:s=320x240:d=30','-f','null','-'],jid)
        with patch.object(self.media,'run',encode):
            jid=self.submit('square',[a]);deadline=time.monotonic()+3;process=None
            while time.monotonic()<deadline:
                with self.media.process_lock:process=self.media.process
                if process and process.poll() is None:break
                time.sleep(.02)
            self.assertIsNotNone(process);self.assertIsNone(process.poll())
            self.media.control({'action':'cancel','id':jid});self.wait(jid,'cancelled')
            self.assertIsNotNone(process.poll());self.assertIsNone(self.media.process)

class MediaHTTPTests(unittest.TestCase):
    def test_upload_range_and_write_protection(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=App(tmp);http=ThreadingHTTPServer(('127.0.0.1',0),Handler);http.app=app
            thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start();url='http://127.0.0.1:'+str(http.server_port)
            try:
                buf=io.BytesIO();Image.new('RGB',(100,100),'red').save(buf,'PNG');raw=buf.getvalue()
                headers={'Content-Type':'application/octet-stream','X-Media-Name':quote('测试.png'),'X-Media-Rights':'test'}
                with self.assertRaises(HTTPError) as e:urlopen(Request(url+'/api/media/upload',raw,headers))
                self.assertEqual(e.exception.code,403);headers['X-Workbench-Token']=app.token
                with urlopen(Request(url+'/api/media/upload',raw,headers)) as r:a=json.load(r)
                with urlopen(Request(url+'/media/'+a['file'],headers={'Range':'bytes=2-15'})) as r:
                    self.assertEqual(r.status,206);self.assertEqual(r.read(),raw[2:16]);self.assertEqual(r.headers['Content-Range'],f'bytes 2-15/{len(raw)}')
                with urlopen(Request(url+'/media/'+a['file'],headers={'Range':'bytes=-5'})) as r:self.assertEqual(r.read(),raw[-5:])
                with urlopen(Request(url+'/media/'+a['file'],method='HEAD')) as r:
                    self.assertEqual(int(r.headers['Content-Length']),len(raw));self.assertEqual(r.read(),b'')
                with self.assertRaises(HTTPError) as e:urlopen(Request(url+'/media/'+a['file'],headers={'Range':'bytes=999999-'}))
                self.assertEqual(e.exception.code,416)
                with self.assertRaises(HTTPError):urlopen(url+'/media/../workbench.sqlite3')
            finally:http.shutdown();http.server_close();thread.join();app.media.close();app.executor.shutdown()

if __name__=='__main__':unittest.main()
