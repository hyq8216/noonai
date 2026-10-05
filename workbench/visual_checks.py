"""Durable subscription-only photo comparisons on the existing product job ledger."""
import hashlib
import json
import threading
from pathlib import Path
from core import Problem,ident,now
from models import ModelLimit
from visuals import source_signature
from visual_check_schema import FIELDS

def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

class VisualChecks:
    def __init__(self,app):
        self.app=app;self.store=app.store;self.stop=threading.Event();self.thread=None;self.lock=threading.Lock();self.submit_lock=threading.Lock();self.scheduler_error=None
        # Store.recover_jobs runs first. Only records known never sent can resume.
        with self.store.connect() as c:
            c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_visual_check_status_created ON jobs(kind,status,created_at)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_visual_check_source ON jobs(json_extract(result,'$.visual_job_id')) WHERE kind='visual-check'")
            c.execute("UPDATE jobs SET status='queued' WHERE kind='visual-check' AND status='interrupted' AND json_extract(result,'$.phase')='queued'")
    def start(self):
        if not self.thread:
            self.thread=threading.Thread(target=self.loop,name='visual-checks',daemon=True);self.thread.start()
    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=5)
    def loop(self):
        while not self.stop.wait(2):
            try:self.tick();self.scheduler_error=None
            except Exception:self.scheduler_error='检查队列暂时无法读取，请重启软件并核对任务记录'
    def reserve(self,visual_ids,profile,batch_id,c):
        """Reserve downstream work in the same transaction as image generation."""
        pending=c.execute("SELECT count(*) FROM jobs WHERE kind='visual-check' AND status IN ('waiting_image','queued','running','waiting','paused')").fetchone()[0]
        if pending+len(visual_ids)>2000:raise Problem('待检查容量不足，请处理已有任务后重新预检',409)
        ids=[]
        for vid in visual_ids:
            v=self.app.visuals.get(vid,c);recipe=v['recipe'];pid=recipe['product_id'];p=self.store.unpack(c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone())
            record={'visual_job_id':vid,'product_id':pid,'revision':p['revision'],'title':p['title_zh'],
                'profile_id':profile['id'],'profile_revision':profile['revision'],'reserved_profile':profile,
                'batch_id':batch_id,'phase':'waiting_image','automatic':True}
            jid=ident();ts=now();c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',(jid,pid,'visual-check',p['revision'],'waiting_image','制作完成后自动检查',json.dumps(record,ensure_ascii=False),ts,ts));ids.append(jid)
        return ids
    def reconcile(self):
        # Only finished/problem outputs need inspecting; queued images do not need file reads.
        with self.store.connect() as c:
            ready=[dict(row) for row in c.execute("SELECT j.id,j.result,v.status AS image_status FROM jobs j JOIN visual_jobs v ON v.id=json_extract(j.result,'$.visual_job_id') WHERE j.kind='visual-check' AND j.status='waiting_image' AND v.status IN ('candidate','approved','rejected','output_rejected','cancelled','uncertain') ORDER BY j.created_at,j.rowid")]
        for job in ready:
            r=json.loads(job['result']);status='queued';message='图片已完成，等待自动对照检查'
            try:
                if job['image_status'] in ('output_rejected','cancelled','uncertain'):raise Problem('制作未得到可检查成图，请先处理原制作任务；尚未调用检查模型',409)
                profile=self.app.models.get(r['profile_id'])
                if profile!=r['reserved_profile']:raise Problem('检查模型配置已变化，请取消未发送检查后重新安排',409)
                data=self.input(r['visual_job_id']);r.update({k:v for k,v in data.items() if k!='paths'});r.update(fingerprint=digest([data['source'],profile]),phase='queued')
            except Problem as e:status='blocked';message=str(e);r['phase']='blocked'
            with self.store.connect() as c:
                # A simultaneous pause/cancel takes precedence over this handoff.
                c.execute("UPDATE jobs SET status=?,message=?,result=?,updated_at=? WHERE id=? AND status='waiting_image'",(status,message,json.dumps(r,ensure_ascii=False),now(),job['id']))
    def input(self,vid,files=True):
        j=self.app.visuals.get(vid)
        if j['status'] not in ('candidate','approved','rejected') or not j['asset_id']:raise Problem('此任务没有通过输出规格检查的图片',409)
        recipe=j['recipe'];p=self.store.get(recipe['product_id'])
        if p['demo'] or source_signature(p)!=recipe['source_signature']:raise Problem('商品身份资料已变化或为示例，请重新制作后检查',409)
        assets=[self.app.media.get(a['id']) for a in recipe['references']]+[self.app.media.get(j['asset_id'])]
        for expected,a in zip(recipe['references'],assets):
            if a['sha256']!=expected['sha256']:raise Problem('参考图记录已变化',409)
        paths=[]
        for a in assets:
            path=(self.app.media.root/a['file']).resolve()
            if not path.is_relative_to(self.app.media.root.resolve()):raise Problem('素材路径无效',409)
            if not path.is_file():raise Problem('图片文件缺失',409)
            if files and (not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=a['sha256']):raise Problem('图片缺失或已变化，请重新检查素材',409)
            paths.append(path)
        if files:self.app.visuals.check_output(assets[-1])
        source={'identity':recipe['identity'],'source_signature':recipe['source_signature'],'shot':recipe['shot'],
                'images':[{'number':i+1,'role':'output' if i==len(assets)-1 else 'reference','id':a['id'],'sha256':a['sha256'],'file_stamp':[paths[i].stat().st_size,paths[i].stat().st_mtime_ns]} for i,a in enumerate(assets)]}
        return {'visual_job_id':vid,'product_id':p['id'],'revision':p['revision'],'title':p['title_zh'],'source':source,'paths':paths}
    def preview(self,b):
        ids=b.get('ids');profile=self.app.models.get(b.get('profile_id'))
        if profile['provider']!='codex-subscription' or not profile['enabled']:raise Problem('请选择已启用的Codex订阅模型，不会自动使用付费API',409)
        if not isinstance(ids,list) or not 1<=len(ids)<=500 or any(not isinstance(x,str) for x in ids) or len(set(ids))!=len(ids):raise Problem('请选择1至500个不同的生成图片任务')
        try:
            for value in ids:value.encode('utf-8')
        except UnicodeEncodeError:raise Problem('图片任务编号含有无效Unicode字符，请重新选择')
        with self.store.connect() as c:
            old=[{**dict(r),'record':json.loads(r['result'] or '{}')} for r in c.execute("SELECT * FROM jobs WHERE kind='visual-check' AND json_extract(result,'$.visual_job_id') IN ("+','.join('?' for _ in ids)+") AND status!='cancelled' ORDER BY created_at DESC,rowid DESC",ids)]
            queued=c.execute("SELECT count(*) FROM jobs WHERE kind='visual-check' AND status IN ('waiting_image','queued','running','waiting','paused')").fetchone()[0]
        rows=[];plans=[]
        for vid in ids:
            row={'id':vid,'status':'blocked','reason':'','title':''}
            try:
                data=self.input(vid);row['title']=data['title'];fp=digest([data['source'],profile]);existing=next((j for j in old if (j['record'].get('fingerprint')==fp or j['record'].get('phase')=='waiting_image') and j['record'].get('visual_job_id')==vid and j['status']!='cancelled'),None)
                if existing:
                    row.update(status='kept' if existing['status']=='done' else 'blocked',reason='已有相同图片与资料的检查结果' if existing['status']=='done' else '已有检查任务或待核对调用，未重复安排')
                elif queued+len(plans)>=2000:row['reason']='待检查容量已满，请先处理已有任务'
                else:
                    row.update(status='ready',reason='可安排一次订阅对照检查')
                    plans.append({k:v for k,v in {**data,'fingerprint':fp,'profile_id':profile['id'],'profile_revision':profile['revision'],'phase':'queued'}.items() if k!='paths'})
            except Problem as e:row['reason']=str(e)
            rows.append(row)
        token=digest([rows,plans,profile])
        return {'rows':rows,'plans':plans,'token':token,'calls':len(plans),'profile_name':profile['name']}
    def submit(self,b):
        key=b.get('request_id')
        if not isinstance(key,str) or not 1<=len(key)<=100 or not b.get('confirmed') is True:raise Problem('请确认本次检查会额外使用订阅额度')
        try:request_digest=digest(b)
        except UnicodeEncodeError:raise Problem('检查请求含有无效Unicode字符，请重新预览')
        ledger='visual-check:'+key
        with self.submit_lock,self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE');old=c.execute('SELECT * FROM ops_requests WHERE key=?',(ledger,)).fetchone()
            if old:
                if old['digest']!=request_digest:raise Problem('此请求编号已用于其他检查',409)
                return json.loads(old['result'])
            preview=self.preview(b)
            if b.get('preview_token')!=preview['token']:raise Problem('预览已变化，请重新预览后安排检查',409)
            if not preview['plans']:raise Problem('没有需要新建的检查任务',409)
            ids=[]
            for plan in preview['plans']:
                jid=ident();ts=now();c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',(jid,plan['product_id'],'visual-check',plan['revision'],'queued','等待图片对照检查',json.dumps(plan,ensure_ascii=False),ts,ts));ids.append(jid)
            result={'job_ids':ids};c.execute('INSERT INTO ops_requests VALUES(?,?,?)',(ledger,request_digest,json.dumps(result)))
            return result
    def update(self,jid,status,message,record):self.store.job_result(jid,status,message,record)
    def tick(self):
        if not self.lock.acquire(blocking=False):return
        try:
            self.reconcile()
            with self.store.connect() as c:
                row=c.execute("""SELECT * FROM jobs WHERE kind='visual-check'
                    AND (status='queued' OR (status='waiting' AND json_extract(result,'$.retry_at')<=?))
                    ORDER BY created_at,rowid LIMIT 1""",(now(),)).fetchone()
                if not row:return
                job=dict(row);r=json.loads(job['result'] or '{}');r['phase']='running'
                claimed=c.execute("UPDATE jobs SET status='running',message='正在对照原图与生成图片',result=?,updated_at=? WHERE id=? AND status=?",(json.dumps(r,ensure_ascii=False),now(),job['id'],job['status'])).rowcount
                if not claimed:return
            try:
                data=self.input(r['visual_job_id']);profile=self.app.models.get(r['profile_id'])
                if digest([data['source'],profile])!=r['fingerprint']:raise Problem('图片、商品资料或模型配置已变化，请重新预览',409)
                result=self.app.models.call(profile['id'],data['source'],'visual-check-'+job['id'],'visual-check',r['product_id'],image_paths=data['paths'])
                fresh=self.input(r['visual_job_id'])
                if fresh['source']!=data['source']:raise Problem('检查期间图片或商品资料发生变化，结果未采用',409)
                r.update(phase='done',report=result);self.update(job['id'],'done',{'match':'可见内容未发现差异，仍需成片验收','mismatch':'发现差异，请查看逐项依据','uncertain':'有无法判断的项目，请补充参考或人工核对'}[result['verdict']],r)
            except ModelLimit as e:
                r.update(phase='waiting',retry_at=e.retry_at);self.update(job['id'],'waiting',str(e),r)
            except Exception as e:
                with self.store.connect() as c:sent=c.execute('SELECT 1 FROM model_calls WHERE request_key=?',('visual-check-'+job['id'],)).fetchone()
                r['phase']='uncertain' if sent else 'blocked';self.update(job['id'],r['phase'],str(e) if isinstance(e,Problem) else '检查未完成，请核对模型调用记录',r)
                # Stop pending comparisons after a service fault; unrelated image generation is untouched.
                with self.store.connect() as c:c.execute("UPDATE jobs SET status='paused',message='前一项检查未完成，待核对后恢复' WHERE kind='visual-check' AND status IN ('waiting_image','queued','waiting')")
        finally:self.lock.release()
    def control(self,b):
        action=b.get('action')
        with self.store.connect() as c:
            if action=='pause':c.execute("UPDATE jobs SET status='paused',message='已暂停待检查任务' WHERE kind='visual-check' AND status IN ('waiting_image','queued','waiting')")
            elif action=='resume':c.execute("UPDATE jobs SET status=CASE WHEN json_extract(result,'$.phase') IN ('waiting','waiting_image') THEN json_extract(result,'$.phase') ELSE 'queued' END,message='已恢复待检查任务' WHERE kind='visual-check' AND status='paused'")
            elif action=='cancel':c.execute("UPDATE jobs SET status='cancelled',message='已取消尚未发送的检查' WHERE id=? AND kind='visual-check' AND status IN ('waiting_image','queued','waiting','paused','blocked')",(b.get('id'),))
            else:raise Problem('检查队列操作无效')
        return {'ok':True}
    def state(self,page=0,group='all',include_detail=True,candidate_page=0):
        if type(page) is not int or not 0<=page<=100000 or type(candidate_page) is not int or not 0<=candidate_page<=100000 or group not in ('all','processing','attention','done'):raise Problem('检查列表页码或筛选无效')
        if not include_detail:
            with self.store.connect() as c:
                counts={r['status']:r['n'] for r in c.execute("SELECT status,count(*) AS n FROM jobs WHERE kind='visual-check' GROUP BY status")}
            return {'jobs':[],'candidates':[],'pending':sum(counts.get(k,0) for k in ('waiting_image','queued','running','waiting')),
                'paused':counts.get('paused',0),'scheduler_error':self.scheduler_error,'fields':FIELDS}
        attention_where="""(j.status IN ('blocked','uncertain','interrupted','failed')
            OR (j.status='waiting_image' AND v.status IN ('blocked','output_rejected','uncertain'))
            OR (j.status='done' AND coalesce(json_extract(j.result,'$.report.verdict'),'')!='match'))"""
        group_where={'all':'1','processing':"j.status IN ('waiting_image','queued','running','waiting','paused')",
            'attention':attention_where,'done':"j.status='done'"}[group]
        source="FROM jobs j LEFT JOIN visual_jobs v ON v.id=json_extract(j.result,'$.visual_job_id') WHERE j.kind='visual-check'"
        with self.store.connect() as c:
            c.execute('BEGIN')
            counts={r['status']:r['n'] for r in c.execute("SELECT status,count(*) AS n FROM jobs WHERE kind='visual-check' GROUP BY status")}
            total=c.execute('SELECT count(*) '+source+' AND '+group_where).fetchone()[0]
            attention_count=c.execute('SELECT count(*) '+source+' AND '+attention_where).fetchone()[0]
            pages=max(1,(total+49)//50);page=min(page,pages-1)
            selected=[dict(r) for r in c.execute('SELECT j.*,v.status AS generation_status,v.message AS generation_message '+source+' AND '+group_where+' ORDER BY j.created_at DESC,j.rowid DESC LIMIT 50 OFFSET ?',(page*50,))]
            candidate_total=c.execute("SELECT count(*) FROM visual_jobs WHERE status IN ('candidate','approved','rejected')").fetchone()[0]
            candidate_pages=max(1,(candidate_total+49)//50);candidate_page=min(candidate_page,candidate_pages-1)
            candidates=[dict(r) for r in c.execute("SELECT id,recipe,status,asset_id FROM visual_jobs WHERE status IN ('candidate','approved','rejected') ORDER BY created_at DESC,rowid DESC LIMIT 50 OFFSET ?",(candidate_page*50,))]
        for j in selected:
            r=json.loads(j.pop('result') or '{}');j['record']=r;j['stale']=False
            generation_status=j.pop('generation_status');generation_message=j.pop('generation_message')
            j['generation']={'status':generation_status,'message':generation_message} if generation_status is not None else None
            if j['status']=='done':
                try:j['stale']=self.input(r['visual_job_id'],files=False)['source']!=r['source']
                except Problem:j['stale']=True
        return {'jobs':selected,
            'candidates':[{'id':j['id'],'product_id':json.loads(j['recipe'])['product_id'],'title':json.loads(j['recipe'])['identity']['name'],'shot':json.loads(j['recipe'])['shot'],'asset_id':j['asset_id']} for j in candidates],
            'candidate_page':candidate_page,'candidate_pages':candidate_pages,'candidate_total':candidate_total,
            'page':page,'pages':pages,'total':total,'group':group,'attention':attention_count,'fields':FIELDS,'scheduler_error':self.scheduler_error,
            'pending':sum(counts.get(k,0) for k in ('waiting_image','queued','running','waiting')),'paused':counts.get('paused',0)}
