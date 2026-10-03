import io,json,sys,tempfile,time,unittest,uuid,hashlib,subprocess
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image,ImageDraw
from core import Store,Problem
from media import Media,ffmpeg
from slideshow import duration

class MotionVideoTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.store=Store(self.root);self.media=Media(self.store);self.media.start()
 def tearDown(self):self.media.close();self.tmp.cleanup()
 def asset(self,color):
  path=self.root/(uuid.uuid4().hex+'.png');im=Image.new('RGB',(800,600),color);ImageDraw.Draw(im).rectangle((0,0,799,599),outline='lime',width=8);im.save(path);return self.media.ingest(path,color+'.png','Synthetic color fixture')
 def render(self,assets,**fields):
  b={'request_id':uuid.uuid4().hex,'recipes':[{'kind':'slideshow','asset_ids':[a['id'] for a in assets],'seconds':1,**fields}]};r=self.media.submit(b);jid=r['task_ids'][0];end=time.monotonic()+60
  while time.monotonic()<end:
   task=next(t for t in self.media.state()['tasks'] if t['id']==jid)
   if task['status'] in ('done','failed','interrupted','cancelled'):break
   time.sleep(.05)
  self.assertEqual(task['status'],'done',task['message']);return self.media.get(task['result'][0]),b,r
 def frame(self,a,t):
  r=subprocess.run([ffmpeg(),'-loglevel','error','-ss',str(t),'-i',str(self.media.root/a['file']),'-frames:v','1','-f','image2pipe','-vcodec','png','-'],capture_output=True,check=True);return Image.open(io.BytesIO(r.stdout)).convert('RGB')
 def bbox_color(self,im):
  mask=Image.eval(im.getchannel('R'),lambda v:255 if v>160 else 0);other=Image.eval(im.getchannel('G'),lambda v:255 if v<90 else 0)
  from PIL import ImageChops
  return ImageChops.multiply(mask,other).getbbox()
 def test_crossfade_has_expected_order_duration_and_blended_middle(self):
  red=self.asset('red');blue=self.asset('blue');a,_,_=self.render([red,blue],motion='none',transition='fade');self.assertAlmostEqual(a['duration'],1.6,delta=.08);self.assertEqual((a['width'],a['height']),(1080,1080))
  first=self.frame(a,.2).getpixel((540,540));middle=self.frame(a,.8).getpixel((540,540));last=self.frame(a,1.3).getpixel((540,540));self.assertGreater(first[0],220);self.assertGreater(last[2],220);self.assertTrue(80<middle[0]<180 and 80<middle[2]<180,middle)
 def test_push_motion_preserves_edges_source_bytes_and_replay(self):
  red=self.asset('red');original=(self.media.root/red['file']).read_bytes();a,b,result=self.render([red],motion='push',transition='cut')
  start=self.frame(a,.03);end=self.frame(a,.9);x=self.bbox_color(start);y=self.bbox_color(end);self.assertGreater(y[2]-y[0],(x[2]-x[0])*1.02);self.assertGreater(y[0],70);self.assertLess(y[2],1010)
  self.assertEqual((self.media.root/red['file']).read_bytes(),original);self.assertEqual(self.media.submit(b),result);self.assertEqual(a['parents'],[red['id']]);self.assertEqual(a['recipe']['motion'],'push')
 def test_alternate_three_shots_portrait(self):
  red=self.asset('red');blue=self.asset('blue');green=self.asset('green');a,_,_=self.render([red,blue,green],motion='alternate',transition='fade',aspect='portrait');self.assertEqual((a['width'],a['height']),(1080,1920));self.assertAlmostEqual(a['duration'],2.2,delta=.08);self.assertGreater(self.frame(a,1.0).getpixel((540,960))[2],200)
 def test_pull_with_hard_cut_landscape(self):
  red=self.asset('red');blue=self.asset('blue');a,_,_=self.render([red,blue],motion='pull',transition='cut',aspect='landscape');self.assertEqual((a['width'],a['height']),(1920,1080));self.assertAlmostEqual(a['duration'],2,delta=.1);self.assertGreater(self.frame(a,1.4).getpixel((960,540))[2],220)
 def test_validation_legacy_recipe_and_saved_options(self):
  a=self.asset('red');base={'kind':'slideshow','asset_ids':[a['id']],'seconds':2};r=self.media.recipe(base);self.assertNotIn('motion',r);self.assertEqual(r,self.media.recipe({**base,'motion':'none','transition':'cut'}))
  for bad in ({'motion':'rotate'},{'transition':'wipe'},{'seconds':.1}):
   with self.assertRaises(Problem):self.media.recipe({**base,**bad})
  tid=self.media.save_template({'name':'Motion preset','recipe':{**base,'motion':'pull','transition':'fade'}})['id'];preset=next(p for p in self.media.state()['templates'] if p['id']==tid);self.assertEqual(preset['recipe']['motion'],'pull');self.assertNotIn('asset_ids',preset['recipe']);self.assertEqual(duration(preset['recipe'],3),5.2)
if __name__=='__main__':unittest.main()
