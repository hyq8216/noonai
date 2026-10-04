"""Explicit S3-compatible image hosting; public bytes must match local bytes."""
import base64,hashlib,json,re,threading
from pathlib import Path
from urllib.parse import urlsplit,quote
from urllib.error import HTTPError
from urllib.request import Request,build_opener
from core import Problem,ident,now
from connectors import NoRedirect
from platform_batch import digest,selection
from recovery import write_json

LIMIT=25*1024*1024
PUBLIC_FIELDS=('endpoint','region','bucket','prefix','public_base','addressing_style')

class PublicObjectMissing(Problem):
    """The origin explicitly confirmed that this deterministic object is absent."""

def https(value,label):
    if not isinstance(value,str) or len(value)>1000:raise Problem(label+'无效')
    p=urlsplit(value)
    if p.scheme!='https' or not p.hostname or p.username or p.password or p.query or p.fragment or any(x.isspace() for x in value):raise Problem(label+'须为不带账号或查询参数的HTTPS地址')
    return value.rstrip('/')

def verify_public(url,raw):
    try:
        with build_opener(NoRedirect()).open(Request(url,headers={'Accept-Encoding':'identity'}),timeout=30) as r:
            if r.status!=200 or not r.headers.get('Content-Type','').lower().startswith('image/'):raise Problem('公网地址未返回图片，保留原商品地址',502)
            body=r.read(len(raw)+1)
        if hashlib.sha256(body).digest()!=hashlib.sha256(raw).digest():raise Problem('公网图片与本地成图不一致，保留原商品地址；请关闭托管端图片改写',502)
    except HTTPError as e:
        if e.code==404:raise PublicObjectMissing('公网图片对象明确不存在，可以尝试上传',404)
        raise Problem(f'公网图片读取返回HTTP {e.code}，对象状态未确认，未再次上传',502)
    except Problem:raise
    except Exception:raise Problem('公网图片读取失败，对象状态未确认，未再次上传；请稍后重新核对',502)

class ImageHost:
    def __init__(self,app):
        self.app=app;self.store=app.store;self.path=self.store.root/'.image-host.json';self.lock=threading.RLock()
    def config(self):
        if not self.path.exists():return {}
        try:return json.loads(self.path.read_text())
        except Exception:raise Problem('图片托管配置无法读取，请重新保存',409)
    def state(self):
        c=self.config();return {**{k:c.get(k,'') for k in PUBLIC_FIELDS},'configured':bool(c),'has_credentials':bool(c.get('secret_key')),'revision':c.get('revision',''),'live_verified':False}
    def save(self,b):
        with self.lock:
            old=self.config()
            if b.get('revision','')!=old.get('revision',''):raise Problem('托管设置已变化，请刷新',409)
            with self.store.connect() as db:
                if db.execute("SELECT 1 FROM jobs WHERE kind='image-host' AND status IN ('queued','running')").fetchone():raise Problem('请等待或停止托管队列后修改设置',409)
            c={k:str(b.get(k,'')).strip() for k in PUBLIC_FIELDS}
            c['endpoint']=https(c['endpoint'],'存储接口');c['public_base']=https(c['public_base'],'公开图片根地址')
            if urlsplit(c['endpoint']).path not in ('','/'):raise Problem('存储接口仅填写域名，不包含桶名或路径')
            if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]',c['bucket']):raise Problem('存储桶名称无效')
            if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}',c['region']):raise Problem('区域代码无效')
            c['prefix']=c['prefix'].strip('/')
            if not re.fullmatch(r'[a-zA-Z0-9_/-]{1,150}',c['prefix']) or '..' in c['prefix']:raise Problem('请填写由字母、数字、短横线或斜线组成的专用图片目录')
            if c['addressing_style'] not in ('path','virtual'):raise Problem('地址方式无效')
            # Reuse credentials only for the same destination; never forward old keys to a new endpoint.
            same=all(c[k]==old.get(k) for k in ('endpoint','region','bucket'))
            for k in ('access_key','secret_key'):
                c[k]=str(b.get(k,'')).strip() or (old.get(k,'') if same else '')
                if not c[k] or len(c[k])>500:raise Problem('请填写完整存储密钥；修改存储目标时需重新填写')
            c['revision']=ident();write_json(self.path,c)
            return self.state()
    def files(self,p,require_verified=True):
        if p['demo']:raise Problem('示例商品不能发布图片')
        if not p['images']:raise Problem('商品还没有成图')
        if require_verified and not p.get('images_verified'):raise Problem('请先确认商品图片符合真实商品')
        out=[]
        for im in p['images']:
            name=im.get('file','');path=(self.store.assets/name).resolve()
            if path.parent!=self.store.assets.resolve() or not re.fullmatch(r'[a-f0-9]{32}\.(jpg|png|webp)',name) or not path.is_file():raise Problem('成图文件缺失或路径无效')
            if not 0<path.stat().st_size<=LIMIT:raise Problem('成图需在25MB以内')
            raw=path.read_bytes();out.append((im,raw,hashlib.sha256(raw).hexdigest()))
        return out
    def validated_files(self,p):
        outputs=p.get('_host_visual_outputs',[])
        if outputs:
            if set(outputs)!={im.get('media_asset_id') for im in p['images']} or len(outputs)!=len(p['images']):raise Problem('流程成图与当前商品图片不一致，请重新核对',409)
            for aid in outputs:self.app.visuals.validate_asset(self.app.media.get(aid),p['id'])
        return self.files(p,require_verified=not bool(outputs))
    def preview(self,b,connection=None):
        ids=selection(b);config=self.config()
        if connection is None:
            with self.store.connect() as c:
                c.execute('BEGIN');return self.preview(b,c)
        c=connection;rows=[]
        for pid in ids:
            row={'id':pid,'title':'商品不存在','status':'blocked','reason':'商品已不存在','images':0}
            saved=c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone()
            if saved:
                p=self.store.unpack(saved);row.update(title=p['title_zh'],revision=p['revision'])
                try:
                    if not config:raise Problem('请先在连接设置中配置图片托管')
                    if c.execute("SELECT 1 FROM jobs WHERE product_id=? AND status IN ('queued','running')",(pid,)).fetchone():raise Problem('商品已有后台任务')
                    if c.execute("SELECT 1 FROM automation_items WHERE product_id=? AND status NOT IN ('done','cancelled')",(pid,)).fetchone():raise Problem('商品仍在自动流程中，请先结束或取消该商品流程')
                    files=self.files(p);row.update(status='ready',reason='上传并核对全部成图后更新地址，商品需重新审核',images=len(files),hashes=[h for _,_,h in files])
                except Problem as e:row['reason']=str(e)
            rows.append(row)
        queued=c.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
        return {'rows':rows,'ready':sum(r['status']=='ready' for r in rows),'image_count':sum(r['images'] for r in rows),'token':digest([rows,config.get('revision'),queued]),'capacity_ok':queued+sum(r['status']=='ready' for r in rows)<=1000}
    def apply(self,b):
        ids=selection(b);key=b.get('request_id');dispatch=[]
        if not isinstance(key,str) or not 1<=len(key)<=100 or b.get('confirmed') is not True:raise Problem('请先预检并确认本批托管')
        fingerprint=digest([ids,b.get('preview_token')])
        with self.lock,self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE');old=c.execute('SELECT * FROM ops_requests WHERE key=?',('image-host:'+key,)).fetchone()
            if old:
                if old['digest']!=fingerprint:raise Problem('请求编号已用于其他批次',409)
                return json.loads(old['result'])
            pre=self.preview(b,c)
            if pre['token']!=b.get('preview_token'):raise Problem('商品、图片或配置已变化，请重新预检',409)
            if not pre['ready'] or not pre['capacity_ok']:raise Problem('没有可处理商品或队列已满',409)
            config_revision=self.config()['revision']
            for row in pre['rows']:
                if row['status']!='ready':continue
                p=self.store.unpack(c.execute('SELECT * FROM products WHERE id=?',(row['id'],)).fetchone());jid=ident();ts=now();p['_host_config']=config_revision;p['_host_hashes']=row['hashes']
                c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',(jid,p['id'],'image-host',p['revision'],'queued','图片等待上传与公网核对',None,ts,ts));dispatch.append((jid,p))
            result={'request_id':key,'jobs':[{'job_id':j,'product_id':p['id']} for j,p in dispatch],'skipped':[r for r in pre['rows'] if r['status']!='ready']}
            c.execute('INSERT INTO ops_requests VALUES(?,?,?)',('image-host:'+key,fingerprint,json.dumps(result,ensure_ascii=False)))
        for jid,p in dispatch:
            try:self.app.executor.submit(self.app.run,jid,p,'image-host')
            except RuntimeError:
                with self.store.connect() as c:c.execute("UPDATE jobs SET status='failed',message='应用关闭，尚未执行',updated_at=? WHERE id=? AND status='queued'",(now(),jid))
        return result
    def status(self,key=None):
        with self.store.connect() as c:
            row=c.execute('SELECT result FROM ops_requests WHERE key=?',('image-host:'+key,)).fetchone() if key else c.execute("SELECT result FROM ops_requests WHERE key LIKE 'image-host:%' ORDER BY rowid DESC LIMIT 1").fetchone()
            if not row:return None
            r=json.loads(row['result']);ids=[j['job_id'] for j in r['jobs']]
            r['jobs']=[dict(j) for j in c.execute('SELECT id AS job_id,product_id,status,message FROM jobs WHERE id IN ('+','.join('?' for _ in ids)+') ORDER BY rowid',ids)];return r
    def cancel(self,b):
        key=b.get('request_id')
        if not isinstance(key,str) or not 1<=len(key)<=100:raise Problem('托管批次编号无效')
        r=self.status(key)
        if not r:raise Problem('托管批次不存在',404)
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for j in r['jobs']:c.execute("UPDATE jobs SET status='cancelled',message='已停止尚未执行的图片托管',updated_at=? WHERE id=? AND kind='image-host' AND status='queued'",(now(),j['job_id']))
        return self.status(r['request_id'])
    def client(self,c):
        import boto3
        from botocore.config import Config
        return boto3.client('s3',endpoint_url=c['endpoint'],region_name=c['region'],aws_access_key_id=c['access_key'],aws_secret_access_key=c['secret_key'],config=Config(signature_version='s3v4',s3={'addressing_style':c['addressing_style']},connect_timeout=15,read_timeout=30,retries={'total_max_attempts':1},request_checksum_calculation='when_required',response_checksum_validation='when_required'))
    def publish(self,p):
        c=self.config()
        if not c or c['revision']!=p.get('_host_config'):raise Problem('托管配置已变化，尚未上传',409)
        current=self.store.get(p['id'])
        if current['revision']!=p['revision']:raise Problem('商品版本已变化，尚未上传',409)
        current['_host_visual_outputs']=p.get('_host_visual_outputs',[])
        files=self.validated_files(current)
        if [h for _,_,h in files]!=p.get('_host_hashes'):raise Problem('成图文件已变化，尚未上传',409)
        urls={};receipts=[];client=self.client(c)
        for im,raw,h in files:
            key=c['prefix']+'/'+h+'.'+Path(im['file']).suffix.lstrip('.');url=c['public_base']+'/'+quote(key,safe='/')
            # Retry after an uncertain upload verifies the deterministic public object before another PUT.
            try:verify_public(url,raw)
            except PublicObjectMissing:
                try:client.put_object(Bucket=c['bucket'],Key=key,Body=raw,ContentType={'jpg':'image/jpeg','png':'image/png','webp':'image/webp'}[Path(im['file']).suffix[1:]],ContentMD5=base64.b64encode(hashlib.md5(raw).digest()).decode())
                except Exception:raise Problem('图片上传失败或结果未明；原地址保留。重试会先核对同一内容地址',502)
                verify_public(url,raw)
            urls[im['id']]=url;receipts.append({'image_id':im['id'],'key':key,'sha256':h,'public_url':url,'checked_at':now()})
        # Optimistic version check prevents a slow external request overwriting edits.
        if [h for _,_,h in self.validated_files({**self.store.get(p['id']),'_host_visual_outputs':p.get('_host_visual_outputs',[])})]!=p['_host_hashes']:raise Problem('上传期间本地图片已变化，公开地址未回填',409)
        updated=self.store.update(p['id'],{'image_urls':urls},p['revision'])
        return {'revision':updated['revision'],'images':receipts,'notice':'仅证明本次匿名读取内容一致；不代表noon已读取或商品可售'}
