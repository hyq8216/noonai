"""Durable local media pipeline. Recipes never overwrite original source assets."""
import hashlib
import io
import json
import math
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from PIL import Image, ImageOps, ImageCms, ImageEnhance, ImageDraw, ImageFont, features
from core import Problem, ident, now

Image.MAX_IMAGE_PIXELS=24_000_000
MAX_UPLOAD=100*1024*1024
RECIPES={'square':'方形白底画布','portrait':'竖版白底画布','feature':'卖点辅图','collage':'多图拼版','slideshow':'图组视频','video':'视频剪裁与转码','cover':'视频封面'}
SIZES={'square':(1080,1080),'portrait':(1080,1920),'landscape':(1920,1080)}


def ffmpeg():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError,RuntimeError):raise Problem('视频引擎未安装，请使用包含视频引擎的桌面版',409)


def bounded(value,low,high,label):
    if isinstance(value,bool):raise Problem(label+'格式无效')
    try:n=float(value)
    except (ValueError,TypeError):raise Problem(label+'格式无效')
    if not math.isfinite(n) or not low<=n<=high:raise Problem(f'{label}需在{low}至{high}之间')
    return n


def string(value,maximum,label,required=False):
    if not isinstance(value,str) or len(value)>maximum or (required and not value.strip()):raise Problem(label+'为空或格式无效')
    return value.strip()


def probe_video(path):
    # Protocols are limited to local files/pipes. Uploaded playlist/URL inputs are not accepted.
    try:
        p=subprocess.run([ffmpeg(),'-hide_banner','-nostdin','-threads','2','-protocol_whitelist','file,pipe','-i',str(path),'-map','0:v:0','-frames:v','1','-an','-f','null','-'],capture_output=True,timeout=25)
    except subprocess.TimeoutExpired:raise Problem('视频读取超时，请提供较短的普通MP4文件')
    log=p.stderr.decode(errors='replace')
    if p.returncode:raise Problem('视频无法解码，请使用正常的MP4、MOV或WebM视频')
    duration=re.search(r'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)',log)
    lines=[x for x in log.splitlines() if 'Video:' in x]
    dims=re.search(r'(?:,|\s)(\d{2,5})x(\d{2,5})(?:[,\s\[])',lines[-1]) if lines else None
    if not duration or not dims:raise Problem('无法读取视频时长或分辨率')
    seconds=int(duration[1])*3600+int(duration[2])*60+float(duration[3]);w,h=map(int,dims.groups())
    if not 0<seconds<=600 or w*h>16_777_216 or min(w,h)<32:raise Problem('视频应在10分钟以内、分辨率不超过约1600万像素')
    return {'width':w,'height':h,'duration':round(seconds,3),'audio':bool(re.search(r'Stream .*Audio:',log))}


def image_info(path):
    try:
        with Image.open(path) as im:
            if im.format not in ('PNG','JPEG','WEBP'):raise Problem('仅支持JPG、PNG和WebP图片')
            if im.width*im.height>24_000_000 or min(im.size)<100:raise Problem('图片最短边至少100像素，最多2400万像素')
            if getattr(im,'n_frames',1)>1:raise Problem('暂不接受动画图片，请使用单帧图片或视频')
            im.load();oriented=ImageOps.exif_transpose(im)
            return {'width':oriented.width,'height':oriented.height,'duration':0,'format':im.format}
    except (OSError,ValueError,Image.DecompressionBombError):raise Problem('图片文件无法读取或尺寸过大')


def rgb_image(path):
    with Image.open(path) as src:
        im=ImageOps.exif_transpose(src)
        srgb=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB'))
        alpha=im.convert('RGBA').getchannel('A')
        if src.info.get('icc_profile'):
            try:im=ImageCms.profileToProfile(im,ImageCms.ImageCmsProfile(io.BytesIO(src.info['icc_profile'])),srgb,outputMode='RGB')
            except Exception:raise Problem('图片颜色配置无效，请提供标准sRGB图片')
        out=Image.new('RGB',im.size,'white');out.paste(im.convert('RGB'),mask=alpha)
        return out


def fit(im,size,background='#ffffff',upscale=False):
    out=Image.new('RGB',size,background);im=im.copy()
    if upscale:im=ImageOps.contain(im,size,Image.Resampling.LANCZOS)
    else:im.thumbnail(size,Image.Resampling.LANCZOS)
    out.paste(im,((size[0]-im.width)//2,(size[1]-im.height)//2));return out


def jpeg(im,path):
    im.convert('RGB').save(path,'JPEG',quality=93,dpi=(72,72),optimize=True,icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes())


def draw_text(draw,value,box,size,color='#19242c'):
    if not value:return
    is_ar=bool(re.search('[\u0600-\u06ff]',value))
    if is_ar and not features.check('raqm'):raise Problem('当前图片引擎不支持阿文排版，请更换环境或使用英文模板')
    paths=['/System/Library/Fonts/Supplemental/Arial Unicode.ttf','/System/Library/Fonts/PingFang.ttc','/System/Library/Fonts/Helvetica.ttc']
    # Linux cloud runners do not have macOS fonts. Select a font that covers
    # the requested script rather than silently rendering Chinese as boxes.
    if re.search('[\u3400-\u9fff]',value):
        paths+=['/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc']
    else:
        paths+=['/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
                '/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf']
    font_path=next((p for p in paths if Path(p).is_file()),None)
    if not font_path:raise Problem('模板字体不可用')
    font=ImageFont.truetype(font_path,size)
    opts={'direction':'rtl'} if is_ar else {}
    width=box[2]-box[0];lines=[];line=''
    for ch in value:
        if ch=='\n' or (line and draw.textlength(line+ch,font=font,**opts)>width):lines.append(line);line='' if ch=='\n' else ch
        else:line+=ch
    if line:lines.append(line)
    lineheight=int(size*1.45)
    if len(lines)*lineheight>box[3]-box[1]:raise Problem('模板文字太长，请缩短标题或说明，避免成图截字')
    for i,line in enumerate(lines):
        x=box[2]-draw.textlength(line,font=font,**opts) if is_ar else box[0]
        draw.text((x,box[1]+i*lineheight),line,font=font,fill=color,**opts)


class Media:
    def __init__(self,store):
        self.store=store;self.root=store.root/'media';self.root.mkdir(exist_ok=True)
        self.temp=self.root/'work';self.temp.mkdir(exist_ok=True)
        self.pool=ThreadPoolExecutor(max_workers=1,thread_name_prefix='media')
        self.stopping=threading.Event();self.process_lock=threading.Lock();self.process=None
        with store.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS media_assets(id TEXT PRIMARY KEY,data TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_media_assets_product_kind ON media_assets(json_extract(data,'$.product_id'),json_extract(data,'$.kind'));
            CREATE TABLE IF NOT EXISTS media_tasks(id TEXT PRIMARY KEY,request_key TEXT UNIQUE,digest TEXT NOT NULL,recipe TEXT NOT NULL,status TEXT NOT NULL,progress INTEGER NOT NULL DEFAULT 0,message TEXT NOT NULL,result TEXT NOT NULL DEFAULT '[]',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_media_tasks_product_video ON media_tasks(json_extract(recipe,'$.product_video.product_id'));
            CREATE INDEX IF NOT EXISTS idx_media_tasks_active ON media_tasks(status) WHERE status IN ('queued','running','cancelling');
            CREATE INDEX IF NOT EXISTS idx_media_tasks_status_updated ON media_tasks(status,updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_media_tasks_updated ON media_tasks(updated_at DESC);
            CREATE TABLE IF NOT EXISTS media_templates(id TEXT PRIMARY KEY,name TEXT NOT NULL,recipe TEXT NOT NULL,created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS media_control(id INTEGER PRIMARY KEY CHECK(id=1),paused INTEGER NOT NULL);
            INSERT OR IGNORE INTO media_control VALUES(1,0);
            ''')
    def start(self):
        with self.store.connect() as c:c.execute("UPDATE media_tasks SET status='interrupted',message='软件关闭时处理被中断，可手动重试',updated_at=? WHERE status IN ('running','cancelling')",(now(),))
        self.kick()
    def close(self):
        self.stopping.set()
        with self.process_lock:
            if self.process and self.process.poll() is None:self.process.terminate()
        self.pool.shutdown(wait=True,cancel_futures=True)
    def kick(self):
        if not self.stopping.is_set():self.pool.submit(self.drain)
    def get(self,aid,c=None):
        if c is None:
            with self.store.connect() as db:return self.get(aid,db)
        row=c.execute('SELECT data FROM media_assets WHERE id=?',(aid,)).fetchone()
        if not row:raise Problem('素材不存在',404)
        return json.loads(row['data'])
    def library(self,page=0,kind='all',search=''):
        if type(page) is not int or not 0<=page<=1000000:raise Problem('素材页码无效')
        if kind not in ('all','original','derived','image','video'):raise Problem('素材筛选无效')
        if not isinstance(search,str) or len(search)>200:raise Problem('素材搜索最多200字')
        search=search.strip()
        where=[];params=[]
        if kind=='original':where.append("coalesce(json_array_length(json_extract(a.data,'$.parents')),0)=0")
        elif kind=='derived':where.append("coalesce(json_array_length(json_extract(a.data,'$.parents')),0)>0")
        elif kind in ('image','video'):where.append("json_extract(a.data,'$.kind')=?");params.append(kind)
        if search:
            where.append("(instr(lower(json_extract(a.data,'$.name')),lower(?))>0 OR instr(lower(json_extract(a.data,'$.product_id')),lower(?))>0 OR instr(lower(json_extract(p.data,'$.partner_sku')),lower(?))>0)")
            params.extend([search]*3)
        condition=' WHERE '+' AND '.join(where) if where else ''
        join=" FROM media_assets a LEFT JOIN products p ON p.id=json_extract(a.data,'$.product_id')"
        with self.store.connect() as c:
            total=c.execute('SELECT count(*)'+join+condition,params).fetchone()[0]
            pages=max(1,(total+49)//50);page=min(page,pages-1)
            assets=[json.loads(r['data']) for r in c.execute('SELECT a.data'+join+condition+' ORDER BY a.created_at DESC,a.rowid DESC LIMIT 50 OFFSET ?',params+[page*50])]
        return {'assets':assets,'total':total,'page':page,'pages':pages,'page_size':50,'kind':kind,'search':search}
    def references(self,product_id,page=0,search='',include_unassigned=False):
        if type(page) is not int or not 0<=page<=1000000:raise Problem('原图页码无效')
        if not isinstance(product_id,str) or not product_id:raise Problem('请先选择商品')
        if not isinstance(search,str) or len(search)>200:raise Problem('原图搜索最多200字')
        if type(include_unassigned) is not bool:raise Problem('原图范围无效')
        search=search.strip()
        where=["json_extract(data,'$.kind')='image'","coalesce(json_array_length(json_extract(data,'$.parents')),0)=0","coalesce(json_extract(data,'$.visual_job_id'),'')=''",
               "(json_extract(data,'$.product_id')=?"+(" OR coalesce(json_extract(data,'$.product_id'),'')=''" if include_unassigned else '')+")"]
        params=[product_id]
        if search:where.append("instr(lower(json_extract(data,'$.name')),lower(?))>0");params.append(search)
        condition=' WHERE '+' AND '.join(where)
        with self.store.connect() as c:
            total=c.execute('SELECT count(*) FROM media_assets'+condition,params).fetchone()[0]
            pages=max(1,(total+49)//50);page=min(page,pages-1)
            assets=[json.loads(r['data']) for r in c.execute('SELECT data FROM media_assets'+condition+' ORDER BY created_at DESC,rowid DESC LIMIT 50 OFFSET ?',params+[page*50])]
        return {'assets':assets,'total':total,'page':page,'pages':pages,'page_size':50,'product_id':product_id,'search':search,'include_unassigned':include_unassigned}
    def state(self,include_assets=True,include_original_counts=True,include_tasks=True):
        with self.store.connect() as c:
            c.execute('BEGIN')
            assets=[json.loads(r['data']) for r in c.execute('SELECT data FROM media_assets ORDER BY created_at DESC,id DESC')] if include_assets else []
            asset_count=c.execute('SELECT count(*) FROM media_assets').fetchone()[0]
            original_counts={r['product_id']:r['n'] for r in c.execute("SELECT json_extract(data,'$.product_id') AS product_id,count(*) AS n FROM media_assets WHERE json_extract(data,'$.kind')='image' AND coalesce(json_array_length(json_extract(data,'$.parents')),0)=0 AND (json_extract(data,'$.visual_job_id') IS NULL OR json_extract(data,'$.visual_job_id')='') AND (json_extract(data,'$.product_image_snapshot') IS NULL OR json_extract(data,'$.product_image_snapshot') IN (0,'')) AND coalesce(json_extract(data,'$.product_id'),'')!='' GROUP BY json_extract(data,'$.product_id')")} if include_original_counts else {}
            has_active_tasks=bool(c.execute("SELECT 1 FROM media_tasks WHERE status IN ('queued','running','cancelling') LIMIT 1").fetchone())
            tasks=[dict(r) for r in c.execute('SELECT * FROM media_tasks ORDER BY created_at DESC,rowid DESC LIMIT 200')] if include_tasks else []
            for t in tasks:t['recipe']=json.loads(t['recipe']);t['result']=json.loads(t['result'])
            templates=[{**dict(r),'recipe':json.loads(r['recipe'])} for r in c.execute('SELECT * FROM media_templates ORDER BY created_at DESC')]
            paused=bool(c.execute('SELECT paused FROM media_control WHERE id=1').fetchone()[0])
        return {'assets':assets,'asset_count':asset_count,'original_counts':original_counts,'tasks':tasks,'has_active_tasks':has_active_tasks,'templates':templates,'paused':paused,'recipes':RECIPES}
    def list_tasks(self,page=0,group='all'):
        if type(page) is not int or not 0<=page<=100000:raise Problem('加工任务页码无效')
        groups={'all':'1','processing':"status IN ('queued','running','cancelling')",
                'attention':"status IN ('failed','interrupted')",'done':"status='done'",'cancelled':"status='cancelled'"}
        if group not in groups:raise Problem('加工任务筛选无效')
        where=groups[group]
        with self.store.connect() as c:
            c.execute('BEGIN')
            total=c.execute('SELECT count(*) FROM media_tasks WHERE '+where).fetchone()[0]
            pages=max(1,(total+49)//50);page=min(page,pages-1)
            tasks=[dict(r) for r in c.execute('SELECT id,recipe,status,progress,message,result,created_at,updated_at FROM media_tasks WHERE '+where+
                                             ' ORDER BY updated_at DESC,rowid DESC LIMIT 50 OFFSET ?',(page*50,))]
        for task in tasks:
            task['recipe']=json.loads(task['recipe']);task['result']=json.loads(task['result'])
        return {'tasks':tasks,'total':total,'page':page,'pages':pages,'group':group,'page_size':50}
    def ingest(self,path,name,rights,product_id='',connection=None,snapshot=None):
        name=string(name,250,'文件名',True);rights=string(rights,2000,'素材使用依据',True)
        if product_id:
            if connection is None:self.store.get(product_id)
            else:self.store.unpack(connection.execute('SELECT * FROM products WHERE id=?',(product_id,)).fetchone())
        path=Path(path)
        if not 0<path.stat().st_size<=MAX_UPLOAD:raise Problem('素材文件需在100MB以内')
        with path.open('rb') as f:magic=f.read(16)
        video=magic[4:8]==b'ftyp' or magic[:4]==b'\x1aE\xdf\xa3'
        if video:
            info=probe_video(path);kind='video';ext='webm' if magic[:4]==b'\x1aE\xdf\xa3' else 'mp4'
        else:
            info=image_info(path);kind='image';ext={'PNG':'png','JPEG':'jpg','WEBP':'webp'}[info['format']]
        aid=ident();dest=self.root/(aid+'.'+ext);shutil.copyfile(path,dest)
        preview=self.root/(aid+'-preview.jpg')
        try:
            if video:self.command(['-ss','0','-i',str(dest),'-frames:v','1','-vf','scale=480:480:force_original_aspect_ratio=decrease','-y',str(preview)],timeout=30)
            else:jpeg(fit(rgb_image(dest),(480,480)),preview)
            asset={'id':aid,'kind':kind,'name':name,'file':dest.name,'preview':preview.name,'rights':rights,'product_id':product_id,'parents':[],'task_id':None,'created_at':now(),'bytes':dest.stat().st_size,'sha256':hashlib.sha256(dest.read_bytes()).hexdigest(),**info}
            if snapshot:asset['product_image_snapshot']=snapshot
            def register(c):
                c.execute('INSERT INTO media_assets VALUES(?,?,?)',(aid,json.dumps(asset,ensure_ascii=False),asset['created_at']))
                self.store.event(c,product_id or None,'素材导入',name)
            if connection is None:
                with self.store.connect() as c:register(c)
            else:register(connection)
            return asset
        except Exception:dest.unlink(missing_ok=True);preview.unlink(missing_ok=True);raise
    def recipe(self,b,connection=None):
        kind=b.get('kind')
        if kind not in RECIPES:raise Problem('处理类型不存在')
        ids=b.get('asset_ids')
        if not isinstance(ids,list) or not 1<=len(ids)<=12 or any(not isinstance(x,str) for x in ids) or len(set(ids))!=len(ids):raise Problem('请选择1至12个不同素材')
        assets=[self.get(a,connection) for a in ids]
        image_kind=kind in ('square','portrait','feature','collage','slideshow')
        if any(a['kind']!=('image' if image_kind else 'video') for a in assets):raise Problem('所选素材类型不符合处理要求')
        if kind not in ('collage','slideshow') and len(ids)!=1:raise Problem('该处理每个任务只能使用一个素材，请使用批量生成')
        if kind=='collage' and len(ids)<2:raise Problem('拼版至少需要两张图片')
        if any(not a['rights'] for a in assets):raise Problem('素材使用依据未填写')
        background=b.get('background','#ffffff')
        if not isinstance(background,str) or not re.fullmatch('#[0-9a-fA-F]{6}',background):raise Problem('背景颜色无效')
        aspect=b.get('aspect','square')
        if aspect not in SIZES:raise Problem('画幅无效')
        r={'kind':kind,'asset_ids':ids,'background':background,'aspect':aspect,'enhance':b.get('enhance') is True,'title':string(b.get('title',''),120,'标题'),'caption':string(b.get('caption',''),260,'说明')}
        if kind in ('video','cover'):
            duration=assets[0]['duration'];r['start']=bounded(b.get('start',0),0,max(0,duration-.05),'开始秒数')
            if kind=='video':
                requested=b.get('duration')
                if requested in (None,''):requested=min(180,duration-r['start'])
                r['duration']=bounded(requested,.05,min(180,duration-r['start']),'片段时长')
            r['mute']=b.get('mute') is True
        if kind=='slideshow':
            r['seconds']=bounded(b.get('seconds',3),1,10,'每张停留秒数')
            from slideshow import options
            options(b,r)
        return r
    def submit(self,b,connection=None):
        if connection is None:
            with self.store.connect() as c:
                c.execute('BEGIN IMMEDIATE');result=self.submit(b,connection=c)
            self.kick();return result
        key=string(b.get('request_id'),100,'请求编号',True)
        batches=b.get('recipes')
        if not isinstance(batches,list) or not 1<=len(batches)<=50:raise Problem('每批需要1至50个处理任务')
        recipes=[self.recipe(r,connection) if isinstance(r,dict) else self.recipe({},connection) for r in batches]
        digest=hashlib.sha256(json.dumps(recipes,sort_keys=True).encode()).hexdigest();out=[]
        c=connection
        for index,r in enumerate(recipes):
            req=f'{key}:{index}';old=c.execute('SELECT id,digest FROM media_tasks WHERE request_key=?',(req,)).fetchone()
            if old:
                if old['digest']!=digest:raise Problem('该请求编号已用于不同内容',409)
                out.append(old['id']);continue
            jid=ident();ts=now();c.execute('INSERT INTO media_tasks(id,request_key,digest,recipe,status,message,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',(jid,req,digest,json.dumps(r,ensure_ascii=False),'queued','等待处理',ts,ts));out.append(jid)
        return {'task_ids':out}
    def control(self,b):
        action=b.get('action');jid=b.get('id')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if action in ('pause','resume'):
                c.execute('UPDATE media_control SET paused=? WHERE id=1',(action=='pause',));out={'paused':action=='pause'}
            else:
                row=c.execute('SELECT * FROM media_tasks WHERE id=?',(jid,)).fetchone()
                if not row:raise Problem('任务不存在',404)
                status=row['status']
                if action=='cancel' and status in ('queued','running'):
                    c.execute('UPDATE media_tasks SET status=?,message=?,updated_at=? WHERE id=?',('cancelled' if status=='queued' else 'cancelling','已取消' if status=='queued' else '正在停止处理',now(),jid))
                elif action=='retry' and status in ('failed','interrupted','cancelled'):
                    c.execute("UPDATE media_tasks SET status='queued',progress=0,message='重新排队',updated_at=? WHERE id=?",(now(),jid))
                else:raise Problem('当前任务状态不支持此操作',409)
                out={'id':jid}
        self.kick();return out
    def save_template(self,b):
        name=string(b.get('name'),80,'模板名称',True);r=self.recipe(b.get('recipe',{}));r.pop('asset_ids')
        tid=ident()
        with self.store.connect() as c:c.execute('INSERT INTO media_templates VALUES(?,?,?,?)',(tid,name,json.dumps(r,ensure_ascii=False),now()))
        return {'id':tid}
    def task_status(self,jid):
        with self.store.connect() as c:return c.execute('SELECT status FROM media_tasks WHERE id=?',(jid,)).fetchone()[0]
    def check_cancel(self,jid):
        if self.stopping.is_set():raise Problem('软件正在关闭，处理已中断')
        if jid and self.task_status(jid)=='cancelling':raise Problem('任务已取消')
    def command(self,args,jid=None,timeout=600):
        # No shell evaluation. Every path is created by the library; text is rendered into images.
        cmd=[ffmpeg(),'-hide_banner','-nostdin','-loglevel','error','-threads','2','-filter_threads','2','-protocol_whitelist','file,pipe',*args]
        with tempfile.TemporaryFile() as log:
            p=subprocess.Popen(cmd,stdout=subprocess.DEVNULL,stderr=log)
            if jid:
                with self.process_lock:self.process=p
            deadline=time.monotonic()+timeout
            try:
                while p.poll() is None:
                    self.check_cancel(jid)
                    if time.monotonic()>deadline:raise Problem('处理超时，请缩短视频或减少素材后重试')
                    time.sleep(.1)
                if p.returncode:raise Problem('视频处理未完成，请核对输入文件和时间范围')
            finally:
                if p.poll() is None:p.terminate()
                try:p.wait(timeout=5)
                except subprocess.TimeoutExpired:p.kill();p.wait()
                if jid:
                    with self.process_lock:self.process=None
    def drain(self):
        while not self.stopping.is_set():
            with self.store.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                if c.execute('SELECT paused FROM media_control WHERE id=1').fetchone()[0]:return
                row=c.execute("SELECT * FROM media_tasks WHERE status='queued' ORDER BY created_at,rowid LIMIT 1").fetchone()
                if not row:return
                jid=row['id'];c.execute("UPDATE media_tasks SET status='running',progress=5,message='正在加工素材',updated_at=? WHERE id=?",(now(),jid))
            try:self.run(jid,json.loads(row['recipe']))
            except Exception as e:
                with self.store.connect() as c:
                    status='interrupted' if self.stopping.is_set() else ('cancelled' if self.task_status(jid)=='cancelling' else 'failed')
                    message=str(e) if isinstance(e,Problem) else '素材处理失败，原件已保留，请检查输入后重试'
                    c.execute('UPDATE media_tasks SET status=?,message=?,updated_at=? WHERE id=?',(status,message,now(),jid))
    def render_image(self,r,assets,path):
        kind=r['kind'];size=(1600,2000) if kind=='portrait' else (1600,1600)
        canvas=Image.new('RGB',size,r['background']);images=[rgb_image(self.root/a['file']) for a in assets]
        if r['enhance']:images=[ImageEnhance.Sharpness(ImageEnhance.Contrast(i).enhance(1.04)).enhance(1.15) for i in images]
        if kind=='collage':
            cols=2 if len(images)<=4 else 3;rows=math.ceil(len(images)/cols);cell=(int(1480/cols),int(1480/rows))
            for i,im in enumerate(images):canvas.paste(fit(im,(cell[0]-24,cell[1]-24),r['background']),(60+(i%cols)*cell[0],60+(i//cols)*cell[1]))
        elif kind=='feature':
            canvas.paste(fit(images[0],(1440,1020),r['background']),(80,60));draw=ImageDraw.Draw(canvas)
            draw.line((80,1120,1520,1120),fill='#dce2e6',width=2)
            draw_text(draw,r['title'],(80,1160,1520,1310),52)
            draw_text(draw,r['caption'],(80,1350,1520,1550),32,'#566470')
        else:
            box=(int(size[0]*.88),int(size[1]*.84));canvas.paste(fit(images[0],box,r['background']),((size[0]-box[0])//2,(size[1]-box[1])//2))
        jpeg(canvas,path)
    def run(self,jid,r):
        if r.get('product_video'):
            from video_batch import gallery_signature
            p=self.store.get(r['product_video']['product_id'])
            if gallery_signature(self.store,p)!=r['product_video']['signature']:raise Problem('商品图片或验收状态已变化，请重新预检批量视频')
        assets=[self.get(a) for a in r['asset_ids']];kind=r['kind']
        if r.get('product_video'):
            from video_batch import file_hash
            if any(file_hash(self.root/a['file'])!=a['sha256'] for a in assets):raise Problem('视频输入快照发生变化，未继续制作')
        with tempfile.TemporaryDirectory(dir=self.temp,prefix=jid+'-') as tmp:
            tmp=Path(tmp);self.check_cancel(jid)
            if kind in ('square','portrait','feature','collage'):
                out=tmp/'result.jpg';self.render_image(r,assets,out);info=image_info(out);media_kind='image'
            elif kind=='cover':
                out=tmp/'result.jpg';self.command(['-ss',str(r['start']),'-i',str(self.root/assets[0]['file']),'-frames:v','1','-vf','scale=1600:1600:force_original_aspect_ratio=decrease','-y',str(out)],jid)
                info=image_info(out);media_kind='image'
            else:
                out=tmp/'result.mp4';w,h=SIZES[r['aspect']]
                animated=kind=='slideshow' and ('motion' in r or 'transition' in r)
                if animated:
                    from slideshow import arguments
                    args=arguments(self,r,assets,tmp,jid)
                elif kind=='slideshow':
                    for i,a in enumerate(assets):jpeg(fit(rgb_image(self.root/a['file']),(w,h),r['background']),tmp/f'frame-{i:03}.jpg')
                    # Fixed frame cadence preserves selection order without a user-controlled concat script.
                    args=['-framerate',str(1/r['seconds']),'-i',str(tmp/'frame-%03d.jpg'),'-t',str(len(assets)*r['seconds']),'-r','25','-an']
                else:
                    vf=f'scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color={r["background"].replace("#","0x")},setsar=1'
                    args=['-ss',str(r['start']),'-i',str(self.root/assets[0]['file']),'-t',str(r['duration']),'-vf',vf,'-map','0:v:0']
                    args+=['-an'] if r['mute'] else ['-map','0:a:0?','-c:a','aac','-b:a','128k']
                    args+=['-r','25']
                args+=['-c:v','libx264','-preset','veryfast','-crf','18' if animated else '22','-pix_fmt','yuv420p','-movflags','+faststart','-map_metadata','-1','-y',str(out)]
                self.command(args,jid);info=probe_video(out);media_kind='video'
                from slideshow import duration
                expected=duration(r,len(assets)) if kind=='slideshow' else r['duration']
                if abs(info['duration']-expected)>.3:raise Problem('输出视频时长校验失败，未登记为完成')
            self.check_cancel(jid)
            if r.get('product_video') and gallery_signature(self.store,self.store.get(r['product_video']['product_id']))!=r['product_video']['signature']:raise Problem('商品图片在制作期间变化，未登记视频成品')
            aid=ident();dest=self.root/(aid+out.suffix);preview=self.root/(aid+'-preview.jpg');shutil.copyfile(out,dest)
            try:
                if media_kind=='image':jpeg(fit(rgb_image(dest),(480,480)),preview)
                else:self.command(['-i',str(dest),'-frames:v','1','-vf','scale=480:480:force_original_aspect_ratio=decrease','-y',str(preview)],jid,30)
                same_products={a['product_id'] for a in assets}
                asset={'id':aid,'kind':media_kind,'name':RECIPES[kind]+' · '+assets[0]['name'],'file':dest.name,'preview':preview.name,'product_id':next(iter(same_products)) if len(same_products)==1 else '',
                       'rights':'沿用源素材使用依据，见来源记录','parents':r['asset_ids'],'task_id':jid,'recipe':r,'created_at':now(),'bytes':dest.stat().st_size,'sha256':hashlib.sha256(dest.read_bytes()).hexdigest(),**info}
                with self.store.connect() as c:
                    c.execute('BEGIN IMMEDIATE');self.check_cancel(jid)
                    c.execute('INSERT INTO media_assets VALUES(?,?,?)',(aid,json.dumps(asset,ensure_ascii=False),asset['created_at']))
                    c.execute("UPDATE media_tasks SET status='done',progress=100,message='成品已生成，待核对后使用',result=?,updated_at=? WHERE id=?",(json.dumps([aid]),now(),jid))
                    self.store.event(c,asset['product_id'] or None,'媒体加工完成',RECIPES[kind])
            except Exception:dest.unlink(missing_ok=True);preview.unlink(missing_ok=True);raise
    def attach(self,b):
        ids=b.get('asset_ids') if 'asset_ids' in b else [b.get('asset_id')]
        if not isinstance(ids,list) or not 1<=len(ids)<=8 or any(not isinstance(x,str) for x in ids) or len(set(ids))!=len(ids):raise Problem('请选择1至8张不同图片')
        assets=[self.get(aid) for aid in ids];pid=b.get('product_id');revision=b.get('revision');p=self.store.get(pid)
        if p['revision']!=revision:raise Problem('商品已更新，请刷新后再添加图片',409)
        if any(a['kind']!='image' for a in assets):raise Problem('当前商品图片栏只能接收图片')
        for a in assets:
            if hasattr(self,'validate_visual'):self.validate_visual(a,pid)
        existing=[] if b.get('replace') is True else p['images']
        if len(existing)+len(assets)>8:raise Problem('商品图片最多8张，请先整理')
        if any(x.get('media_asset_id') in ids for x in existing):raise Problem('该素材已经加入该商品',409)
        created=[];results=[]
        try:
            for a in assets:
                source=self.root/a['file'];aid=ident()
                original=self.store.assets/(aid+'-source'+source.suffix);dest=self.store.assets/(aid+'.jpg');created.extend([original,dest])
                shutil.copyfile(source,original)
                # Finished templates retain exact bytes and dimensions.
                if a.get('task_id') and source.suffix=='.jpg':shutil.copyfile(source,dest)
                else:jpeg(rgb_image(source),dest)
                results.append({'id':aid,'source':original.name,'file':dest.name,'source_size':[a['width'],a['height']],
                    'size':[a['width'],a['height']],'template':'media','public_url':'','sha256':a['sha256'],
                    'warning':'保留媒体成图尺寸与版式，请核对商品内容及目标类目图片要求',
                    'media_asset_id':a['id'],'media_parents':a['parents'],**({'visual_shot':a['visual_shot']} if a.get('visual_shot') else {})})
            return self.store.update(pid,{},revision,{'images':existing+results,'images_verified':False})
        except Exception:
            for path in created:path.unlink(missing_ok=True)
            raise
