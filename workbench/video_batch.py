"""One photo-video per verified product gallery, using immutable input snapshots."""
import hashlib,json,re
from pathlib import Path
from core import Problem,now
from media import string,bounded,SIZES
from slideshow import options,duration
from source_import import digest

LIMIT=50
QUEUE_LIMIT=500

def file_hash(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def gallery_files(store,p):
    files=[]
    for im in p['images']:
        path=store.assets/im['file']
        if path.is_symlink() or not path.resolve().is_relative_to(store.assets.resolve()) or not path.is_file():raise Problem('商品图片文件缺失或路径无效')
        files.append({'image_id':im['id'],'file':im['file'],'sha256':file_hash(path)})
    return files

def gallery_signature(store,p):return digest([p['id'],p['images_verified'],p['rights_evidence'],p['facts'],p['source_sku'],gallery_files(store,p)])

class VideoBatch:
    def __init__(self,app):self.app=app;self.store=app.store;self.media=app.media
    @staticmethod
    def settings(b):
        ids=b.get('product_ids')
        if not isinstance(ids,list) or not 1<=len(ids)<=LIMIT or any(not isinstance(i,str) for i in ids) or len(set(ids))!=len(ids):raise Problem('每批请选择1至50件不同商品制作视频')
        r={'kind':'slideshow','seconds':bounded(b.get('seconds',3),1,10,'每张停留秒数'),'aspect':b.get('aspect','square'),'background':b.get('background','#ffffff')}
        if r['aspect'] not in SIZES or not isinstance(r['background'],str) or not re.fullmatch('#[0-9a-fA-F]{6}',r['background']):raise Problem('画幅或背景无效')
        options(b,r);return ids,r
    def preview(self,b,c=None):
        if c is None:
            with self.store.connect() as conn:
                conn.execute('BEGIN');return self.preview(b,conn)
        ids,opts=self.settings(b);rows=[];queued=c.execute("SELECT count(*) FROM media_tasks WHERE status IN ('queued','running','cancelling')").fetchone()[0];slots=QUEUE_LIMIT-queued
        jobs={}
        marks=','.join('?' for _ in ids)
        for rec in c.execute("SELECT id,status,recipe,result FROM media_tasks WHERE json_extract(recipe,'$.product_video.product_id') IN ("+marks+") ORDER BY rowid DESC",ids):
            r=json.loads(rec['recipe']);binding=r.get('product_video')
            if binding and binding['product_id'] in ids:jobs.setdefault(binding['product_id'],[]).append({**dict(rec),'recipe':r,'binding':binding})
        for pid in ids:
            p=self.store.unpack(c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone());row={'id':pid,'title':p['title_zh'],'sku':p['partner_sku'],'status':'blocked','reasons':[],'images':len(p['images']),'seconds':round(duration(opts,len(p['images'])),2) if p['images'] else 0};why=row['reasons']
            if p['demo']:why.append('示例商品不能制作批量商品视频')
            if not p['images'] or len(p['images'])>8:why.append('请先加入1至8张商品图片')
            if not p['images_verified']:why.append('商品图片尚未验收')
            if not p['rights_evidence']:why.append('缺少图片使用依据')
            try:signature=gallery_signature(self.store,p);row.update(signature=signature,fingerprint=digest([pid,signature,opts]))
            except Problem as e:why.append(str(e))
            if not why:
                previous=next((j for j in jobs.get(pid,[]) if j['binding']['fingerprint']==row['fingerprint'] and j['status'] in ('queued','running','cancelling','done')),None)
                reusable=False
                if previous and previous['status']=='done':
                    try:
                        outputs=[self.media.get(i,c) for i in json.loads(previous['result'])]
                        reusable=bool(outputs) and all(a.get('video_review',{}).get('decision')!='rejected' and (self.media.root/a['file']).is_file() and file_hash(self.media.root/a['file'])==a['sha256'] for a in outputs)
                    except (Problem,OSError):pass
                if reusable:row.update(status='kept',task_id=previous['id']);why.append('已有相同图片与配方的成品，保留现有视频')
                elif previous and previous['status']!='done':row.update(status='active',task_id=previous['id']);why.append('相同商品视频已在队列中')
                elif slots<1:row['status']='capacity';why.append('视频处理队列已满，请先完成或取消任务')
                else:row['status']='ready';slots-=1
            rows.append(row)
        return {'rows':rows,'options':opts,'ready':sum(r['status']=='ready' for r in rows),'queued':queued,'queue_limit':QUEUE_LIMIT,'token':digest([opts,rows,queued])}
    def apply(self,b,owner_item=None):
        if b.get('confirmed') is not True:raise Problem('请确认按预览中的已验收图片制作商品视频')
        key=string(b.get('request_id'),100,'操作编号',True);request_key='video-batch:'+key;h=digest(b);created=[]
        try:
            with self.store.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                if owner_item:
                    owner=c.execute('SELECT i.status,r.status AS run_status,i.data FROM automation_items i JOIN automation_runs r ON r.id=i.run_id WHERE i.id=?',(owner_item,)).fetchone()
                    if not owner or owner['status']=='cancelled' or owner['run_status'] in ('paused','cancelled'):raise Problem('所属流程已暂停或取消，未继续派发视频',409)
                old=c.execute('SELECT digest,result FROM ops_requests WHERE key=?',(request_key,)).fetchone()
                if old:
                    if old['digest']!=h:raise Problem('操作编号已用于不同内容',409)
                    return json.loads(old['result'])
                pre=self.preview(b,c)
                if pre['token']!=b.get('preview_token'):raise Problem('商品图片、验收或任务已变化，请重新预检',409)
                if not pre['ready']:raise Problem('没有可新建的视频任务')
                tasks=[];products=[]
                for row in pre['rows']:
                    if row['status']!='ready':continue
                    p=self.store.unpack(c.execute('SELECT * FROM products WHERE id=?',(row['id'],)).fetchone());assets=[]
                    existing_assets=[json.loads(rec['data']) for rec in c.execute("SELECT data FROM media_assets WHERE json_extract(data,'$.product_id')=?",(p['id'],))]
                    for index,im in enumerate(gallery_files(self.store,p)):
                        marker={**im,'product_id':p['id']};found=None
                        for a in existing_assets:
                            if a.get('product_image_snapshot')==marker and (self.media.root/a['file']).is_file() and file_hash(self.media.root/a['file'])==a['sha256']:found=a;break
                        if not found:
                            found=self.media.ingest(self.store.assets/im['file'],f'视频输入快照 · {p["partner_sku"]} · {index+1}',p['rights_evidence'],p['id'],connection=c,snapshot=marker);created.append(found)
                        if found['sha256']!=im['sha256']:raise Problem('图片在复制期间变化，请重新预检',409)
                        assets.append(found['id'])
                    if gallery_signature(self.store,p)!=row['signature']:raise Problem('商品图片在准备期间变化，请重新预检',409)
                    result=self.media.submit({'request_id':'vb-'+digest([key,p['id']]),'recipes':[{**pre['options'],'asset_ids':assets}]},connection=c);tid=result['task_ids'][0]
                    recipe=json.loads(c.execute('SELECT recipe FROM media_tasks WHERE id=?',(tid,)).fetchone()[0]);recipe['product_video']={'product_id':p['id'],'signature':row['signature'],'fingerprint':row['fingerprint']}
                    c.execute('UPDATE media_tasks SET recipe=? WHERE id=?',(json.dumps(recipe),tid));self.store.event(c,p['id'],'批量视频排队',tid);tasks.append(tid);products.append(p['id'])
                if owner_item:
                    data=json.loads(owner['data']);data['video_task']=tasks[0]
                    c.execute('UPDATE automation_items SET data=? WHERE id=?',(json.dumps(data),owner_item))
                out={'task_ids':tasks,'product_ids':products,'skipped':[r for r in pre['rows'] if r['status']!='ready']};c.execute('INSERT INTO ops_requests VALUES(?,?,?)',(request_key,h,json.dumps(out)))
        except Exception:
            for a in created:
                for key in ('file','preview'):(self.media.root/a[key]).unlink(missing_ok=True)
            raise
        self.media.kick();return out

    def review(self,b):
        aid=b.get('asset_id');decision=b.get('decision');checks=b.get('checks')
        if not isinstance(aid,str) or decision not in ('approved','rejected') or not isinstance(checks,dict):raise Problem('视频验收资料无效')
        if decision=='approved' and any(checks.get(k) is not True for k in ('identity','motion','quality')):
            raise Problem('通过前请播放完整视频并逐项核对商品、画面与质量')
        note=string(b.get('note',''),2000,'视频验收说明',decision=='rejected')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            a=self.media.get(aid,c);binding=(a.get('recipe') or {}).get('product_video')
            if a['kind']!='video' or not binding or not a.get('task_id') or a.get('product_id')!=binding.get('product_id'):
                raise Problem('只有按商品图组制作的视频可在此验收',409)
            if a['sha256']!=b.get('expected_sha256'):raise Problem('视频文件记录已变化，请刷新后核对',409)
            task=c.execute('SELECT status,recipe,result FROM media_tasks WHERE id=?',(a['task_id'],)).fetchone()
            if not task or task['status']!='done' or aid not in json.loads(task['result'] or '[]') or (json.loads(task['recipe']).get('product_video')!=binding):
                raise Problem('视频任务与成品记录不一致，请先核对',409)
            previous=a.get('video_review') or {}
            if previous.get('at','')!=b.get('expected_review_at',''):raise Problem('视频验收记录已变化，请刷新后核对',409)
            p=self.store.unpack(c.execute('SELECT * FROM products WHERE id=?',(binding['product_id'],)).fetchone())
            if gallery_signature(self.store,p)!=binding['signature']:
                raise Problem('商品图片或验收状态已变化，请重新制作当前图组的视频',409)
            path=self.media.root/a['file']
            if not path.is_file() or file_hash(path)!=a['sha256']:raise Problem('视频文件缺失或发生变化，不能验收',409)
            a['video_review']={'decision':decision,'checks':{k:checks.get(k) is True for k in ('identity','motion','quality')},
                               'note':note,'at':now(),'signature':binding['signature'],'sha256':a['sha256']}
            c.execute('UPDATE media_assets SET data=? WHERE id=?',(json.dumps(a,ensure_ascii=False),aid))
            self.store.event(c,p['id'],'商品视频验收','通过' if decision=='approved' else '不通过：'+note[:120])
        return {'asset':a}
