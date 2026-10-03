"""Durable product workflows. A failed item never silently skips a step.

The scheduler is local to the running application. External submit is delegated
only after a current product approval, then held for result inspection on errors.
"""
import hashlib
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from core import Problem, ident, now
from media import RECIPES
from models import text, CONTENT_FIELDS, ModelLimit, numeric_content_issues

STEPS={'source':'检查货源事实','translate':'生成英阿内容','review':'模型复核','ai_visual':'AI制作与成片验收','images':'批量图片模板','video':'制作商品展示视频','image_host':'上传并核对图片','check':'检查刊登资料','approval':'等待人工审核','submit':'提交noon内容','finish':'流程完成'}

def workflow_input(b):
    ids=b.get('product_ids')
    if not isinstance(ids,list) or not 1<=len(ids)<=500 or any(not isinstance(i,str) for i in ids) or len(ids)!=len(set(ids)):raise Problem('请选择1至500个不同商品')
    raw=b.get('plan',{})
    if not isinstance(raw,dict):raise Problem('流程配置无效')
    template=raw.get('image_template','')
    if template not in ('','square','portrait','feature'):raise Problem('此流程支持方形、竖版或卖点单图模板')
    plan={'translate':raw.get('translate') is True,'review':raw.get('review') is True,'image_template':template,'submit':raw.get('submit') is True}
    if raw.get('ai_visual') is not None:
        from workflow_visual import options
        if template:raise Problem('AI制作与普通图片模板请选择一种')
        plan['ai_visual']=options(raw['ai_visual'])
    if raw.get('image_host') is True:plan['image_host']=True
    if raw.get('video') is not None:
        from video_batch import VideoBatch
        plan['video']=VideoBatch.settings({'product_ids':['validation'],**raw['video']})[1] if isinstance(raw['video'],dict) else None
        if plan['video'] is None:raise Problem('视频配方无效')
    # Omit the new key for legacy requests so their persisted replay digests remain valid.
    if raw.get('missing_only') is True:plan['missing_only']=True
    plan['steps']=['source']+(['translate'] if plan['translate'] else [])+(['review'] if plan['review'] else [])+(['images'] if template else [])+(['ai_visual'] if plan.get('ai_visual') else [])+(['image_host'] if plan.get('image_host') else [])+['check','approval']+(['video'] if plan.get('video') else [])+(['submit'] if plan['submit'] else [])+['finish']
    return ids,plan

class Automation:
    def __init__(self,app):
        self.app=app;self.store=app.store;self.stop=threading.Event();self.thread=None;self.last_status_scan=0.0
        with self.store.connect() as c:c.executescript('''
        CREATE TABLE IF NOT EXISTS automation_runs(id TEXT PRIMARY KEY,request_key TEXT UNIQUE,digest TEXT NOT NULL,name TEXT NOT NULL,plan TEXT NOT NULL,status TEXT NOT NULL,run_at TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS automation_items(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,product_id TEXT NOT NULL,revision INTEGER NOT NULL,step INTEGER NOT NULL,status TEXT NOT NULL,attempt INTEGER NOT NULL DEFAULT 0,data TEXT NOT NULL,message TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS automation_events(id INTEGER PRIMARY KEY AUTOINCREMENT,item_id TEXT NOT NULL,step TEXT NOT NULL,status TEXT NOT NULL,message TEXT NOT NULL,created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_automation_items_run_id ON automation_items(run_id);
        CREATE INDEX IF NOT EXISTS idx_automation_items_product_id ON automation_items(product_id);
        CREATE INDEX IF NOT EXISTS idx_automation_items_status ON automation_items(status);
        CREATE INDEX IF NOT EXISTS idx_automation_items_runnable ON automation_items(updated_at) WHERE status IN ('queued','waiting','approval');
        CREATE INDEX IF NOT EXISTS idx_automation_runs_status_at ON automation_runs(status,run_at);
        ''')
    def start(self):
        with self.store.connect() as c:c.execute("UPDATE automation_items SET status='attention',message='应用关闭时此步被中断，请核对商品和调用记录后重试',updated_at=? WHERE status='processing'",(now(),))
        self.thread=threading.Thread(target=self.loop,name='automation',daemon=True);self.thread.start()
    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=65)
    def state(self,page=0,group='all',query='',item_pages='{}',include_detail=True,include_counts=False):
        if not include_detail:
            with self.store.connect() as c:
                active=c.execute("SELECT count(*) FROM automation_runs WHERE status NOT IN ('done','cancelled')").fetchone()[0]
                counts={r[0]:r[1] for r in c.execute("SELECT status,count(*) FROM automation_items WHERE status NOT IN ('done','cancelled') GROUP BY status")} if include_counts else {}
            return {'active_count':active,'item_counts':counts,'scheduler_running':bool(self.thread and self.thread.is_alive())}
        try:page=int(page)
        except (ValueError,TypeError):raise Problem('流程页码无效')
        if page<0 or page>1000000:raise Problem('流程页码无效')
        groups={'all':'1','active':"r.status NOT IN ('done','cancelled')",'attention':"EXISTS (SELECT 1 FROM automation_items i WHERE i.run_id=r.id AND i.status='attention')",'approval':"EXISTS (SELECT 1 FROM automation_items i WHERE i.run_id=r.id AND i.status='approval')",'paused':"r.status='paused'",'done':"r.status='done'",'cancelled':"r.status='cancelled'"}
        if group not in groups:raise Problem('流程筛选无效')
        if not isinstance(query,str) or len(query)>200:raise Problem('流程搜索最多200字')
        if not isinstance(item_pages,str) or len(item_pages)>10000:raise Problem('流程商品页码无效')
        try:item_pages=json.loads(item_pages)
        except (TypeError,ValueError):raise Problem('流程商品页码无效')
        if not isinstance(item_pages,dict) or any(not isinstance(k,str) or type(v) is not int or not 0<=v<=1000000 for k,v in item_pages.items()):raise Problem('流程商品页码无效')
        query=query.strip();where=groups[group];params=[]
        if query:
            # instr treats %, _ and quotes as literal user text, not SQL patterns.
            where+=" AND (instr(lower(r.name),lower(?))>0 OR r.id=? OR EXISTS (SELECT 1 FROM automation_items i JOIN products p ON p.id=i.product_id WHERE i.run_id=r.id AND (instr(lower(json_extract(p.data,'$.title_zh')),lower(?))>0 OR instr(lower(json_extract(p.data,'$.partner_sku')),lower(?))>0 OR instr(lower(json_extract(p.data,'$.source_sku')),lower(?))>0)))"
            params=[query]*5
        with self.store.connect() as c:
            c.execute('BEGIN')
            total=c.execute('SELECT count(*) FROM automation_runs r WHERE '+where,params).fetchone()[0]
            pages=max(1,(total+9)//10);page=min(page,pages-1)
            runs=[dict(r) for r in c.execute('SELECT r.* FROM automation_runs r WHERE '+where+' ORDER BY r.rowid DESC LIMIT 10 OFFSET ?',params+[page*10])]
            ids=[r['id'] for r in runs]
            run_item_counts={r['run_id']:r['n'] for r in c.execute('SELECT run_id,count(*) AS n FROM automation_items WHERE run_id IN ('+','.join('?' for _ in ids)+') GROUP BY run_id',ids)} if ids else {}
            visible_pages={};items=[]
            for rid in ids:
                count=run_item_counts.get(rid,0);selected=min(item_pages.get(rid,0),max(0,(count-1)//50));visible_pages[rid]=selected
                items.extend(dict(row) for row in c.execute('SELECT * FROM automation_items WHERE run_id=? ORDER BY rowid LIMIT 50 OFFSET ?',(rid,selected*50)))
            events=[dict(r) for r in c.execute('SELECT * FROM automation_events ORDER BY id DESC LIMIT 200')]
            active=c.execute("SELECT count(*) FROM automation_runs WHERE status NOT IN ('done','cancelled')").fetchone()[0]
            item_counts={r[0]:r[1] for r in c.execute('SELECT status,count(*) FROM automation_items GROUP BY status')}
        for r in runs:r['plan']=json.loads(r['plan'])
        for i in items:i['data']=json.loads(i['data'])
        return {'runs':runs,'items':items,'run_item_counts':run_item_counts,'item_pages':visible_pages,'events':events,'steps':STEPS,'scheduler_running':bool(self.thread and self.thread.is_alive()),'page':page,'pages':pages,'total':total,'page_size':10,'group':group,'query':query,'active_count':active,'item_counts':item_counts}
    def attention(self,page=0,group='all'):
        try:index=int(page)
        except (TypeError,ValueError):raise Problem('待办页码无效')
        if str(index)!=str(page) or not 0<=index<=1000000:raise Problem('待办页码无效')
        if group not in ('all','attention','approval'):raise Problem('待办筛选无效')
        where="i.status IN ('attention','approval')" if group=='all' else 'i.status=?'
        params=[] if group=='all' else [group]
        with self.store.connect() as c:
            total=c.execute('SELECT count(*) FROM automation_items i WHERE '+where,params).fetchone()[0]
            pages=max(1,(total+49)//50);index=min(index,pages-1)
            rows=[dict(r) for r in c.execute('''SELECT i.id,i.product_id,i.run_id,i.step,i.status,i.message,i.updated_at,
                CAST((SELECT count(*) FROM automation_items x WHERE x.run_id=i.run_id AND x.rowid<i.rowid)/50 AS INTEGER) AS item_page,
                r.name AS run_name,r.status AS run_status,r.plan,p.revision AS product_revision,
                json_extract(p.data,'$.title_zh') AS title,json_extract(p.data,'$.partner_sku') AS sku
                FROM automation_items i JOIN automation_runs r ON r.id=i.run_id LEFT JOIN products p ON p.id=i.product_id
                WHERE '''+where+" ORDER BY CASE i.status WHEN 'attention' THEN 0 ELSE 1 END,i.updated_at,i.rowid LIMIT 50 OFFSET ?",params+[index*50])]
        for row in rows:
            steps=json.loads(row.pop('plan'))['steps']
            row['step_name']=steps[row['step']] if 0<=row['step']<len(steps) else ''
        return {'rows':rows,'total':total,'page':index,'pages':pages,'group':group,'page_size':50}
    def active_product(self,c,pid):
        return bool(c.execute("SELECT 1 FROM automation_items WHERE product_id=? AND status NOT IN ('done','cancelled')",(pid,)).fetchone()
            or c.execute("SELECT 1 FROM jobs WHERE product_id=? AND status IN ('queued','running')",(pid,)).fetchone()
            or c.execute("SELECT 1 FROM visual_jobs WHERE json_extract(recipe,'$.product_id')=? AND status IN ('queued','waiting','preparing','generating')",(pid,)).fetchone())
    def preflight(self,b,c=None):
        ids,plan=workflow_input(b)
        if c is None:
            with self.store.connect() as connection:
                connection.execute('BEGIN')
                return self.preflight(b,connection)
        ready={role:self.app.models.ready(role) for role in ('primary','review')}
        config=self.app.config();rows=[]
        host_config=self.app.image_host.state() if plan.get('image_host') else None
        visual_preview=self.app.visual_batch.preview({'product_ids':ids,**plan['ai_visual']},c) if plan.get('ai_visual') else None
        visual_rows={r['id']:r for r in visual_preview['rows']} if visual_preview else {}
        for pid in ids:
            p=self.store.unpack(c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone())
            missing=[k for k in CONTENT_FIELDS if not p.get(k,'').strip()]
            translation=plan['translate'] and (not plan.get('missing_only') or bool(missing))
            review=plan['review'] and not (plan.get('missing_only') and p.get('content_verified') and not missing and not translation)
            images=bool(plan['image_template']) and not (plan.get('missing_only') and p.get('images_verified') and p['images'])
            reasons=[];status='ready'
            if host_config is not None and not host_config['configured']:reasons.append('图片托管未配置，请先连接已有存储空间')
            if plan.get('image_host') and not plan.get('ai_visual') and not p['images']:reasons.append('没有可托管成图，请先添加图片或启用AI制作')
            if p['demo']:reasons.append('示例商品不能进入运营流程')
            for k,label in [('source_url','货源链接'),('supplier','供应商'),('facts','规格事实')]:
                if not p.get(k,'').strip():reasons.append('缺少'+label)
            if translation and not ready['primary']:reasons.append('批量处理模型未配置或已停用')
            if review and not ready['review']:reasons.append('内容复核模型未配置或已停用')
            if review and missing and not translation:reasons.append('双语字段未齐全，请启用补齐双语')
            if images and not p['images']:reasons.append('请先添加真实商品原图')
            if images and not p.get('rights_evidence'):reasons.append('请先记录图片使用依据')
            if plan['submit'] and not (config['noon_ready'] and config['submit_enabled']):reasons.append('店铺内容提交尚未接通，请先关闭自动提交')
            if plan['submit'] and not plan.get('image_host'):
                if plan.get('ai_visual') or images:
                    reasons.append('本流程会替换商品图片；自动提交前请启用图片托管，或关闭自动提交并在成图验收后补齐公开地址')
                elif not p['images'] or any(not im.get('public_url') for im in p['images']):
                    reasons.append('自动提交需要每张商品图片的公开地址；请先补齐并核对，或在流程中启用图片托管')
            if visual_preview:
                vr=visual_rows[pid]
                if vr['status'] not in ('ready','kept'):reasons.extend(vr['reasons'])
            if reasons:status='blocked'
            if self.active_product(c,pid):status='active';reasons=['已有未结束任务，请到自动化中心或任务记录查看']
            rows.append({'id':pid,'revision':p['revision'],'title':p['title_zh'],'sku':p['partner_sku'],'status':status,'reasons':reasons,
                'missing_fields':missing,'translate':bool(translation),'review':bool(review),'images':images,'visual':visual_rows.get(pid),'issues':p['issues']})
        token=hashlib.sha256(json.dumps([plan,rows,visual_preview['token'] if visual_preview else None,host_config],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        return {'token':token,'rows':rows,'eligible_ids':[r['id'] for r in rows if r['status']=='ready'],
            'calls':sum(int(r['translate'])+int(r['review']) for r in rows if r['status']=='ready'),
            'image_calls':sum(len(r['visual']['new_shots']) for r in rows if r['status']=='ready' and r['visual']),
            'visual_profile_revision':visual_preview['check_profile']['revision'] if visual_preview and visual_preview['check_profile'] else None,
            'image_checks':sum(len(plan['ai_visual']['shots']) for r in rows if r['status']=='ready') if plan.get('ai_visual',{}).get('auto_check') else 0}
    def create(self,b,connection=None):
        if connection is None:
            with self.store.connect() as c:
                c.execute('BEGIN IMMEDIATE');return self.create(b,connection=c)
        name=text(b.get('name'),'流程名称',80);key=text(b.get('request_id'),'请求编号',100)
        ids,plan=workflow_input(b)
        stamp=b.get('run_at') or now()
        try:
            dt=datetime.fromisoformat(stamp.replace('Z','+00:00'))
            if dt.tzinfo is None:raise ValueError()
            stamp=dt.astimezone(timezone.utc).isoformat()
        except (ValueError,TypeError,AttributeError):raise Problem('运行时间需包含时区')
        digest_input=[name,ids,plan,b.get('run_at')]
        if b.get('preflight_token'):digest_input.append(b['preflight_token'])
        digest=hashlib.sha256(json.dumps(digest_input,sort_keys=True).encode()).hexdigest()
        c=connection
        old=c.execute('SELECT id,digest FROM automation_runs WHERE request_key=?',(key,)).fetchone()
        if old:
            if old['digest']!=digest:raise Problem('请求编号已用于其他流程',409)
            return {'id':old['id']}
        preview=self.preflight(b,c)
        if b.get('preflight_token'):
            if preview['token']!=b['preflight_token']:raise Problem('商品或处理条件已变化，请重新预检后开始',409)
            ids=preview['eligible_ids']
            if not ids:raise Problem('本批没有可处理商品，请先补充资料或调整步骤',409)
        elif len(preview['eligible_ids'])!=len(ids):
            raise Problem('部分商品未通过流程预检：'+next('；'.join(r['reasons']) for r in preview['rows'] if r['status']!='ready'),409)
        if plan.get('ai_visual',{}).get('auto_check'):
            plan['ai_visual']['check_profile_revision']=self.app.models.get(plan['ai_visual']['check_profile_id'],c)['revision']
        if plan.get('image_host'):
            host=self.app.image_host.state()
            if not host['configured']:raise Problem('图片托管尚未配置',409)
            plan['host_config_revision']=host['revision']
        products=[self.store.unpack(c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone()) for pid in ids]
        for p in products:
            if p['demo']:raise Problem('示例商品不能进入运营流程')
            if self.active_product(c,p['id']):raise Problem(p['title_zh']+'已有未结束任务',409)
        rid=ident();ts=now();c.execute('INSERT INTO automation_runs VALUES(?,?,?,?,?,?,?,?,?)',(rid,key,digest,name,json.dumps(plan),'queued',stamp,ts,ts))
        for p in products:c.execute('INSERT INTO automation_items VALUES(?,?,?,?,?,?,?,?,?,?)',(ident(),rid,p['id'],p['revision'],0,'queued',0,'{}','等待开始',ts))
        return {'id':rid}
    def control(self,b):
        action=b.get('action')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if action in ('pause','resume','cancel_run'):
                r=c.execute('SELECT * FROM automation_runs WHERE id=?',(b.get('run_id'),)).fetchone()
                if not r:raise Problem('流程不存在',404)
                if r['status'] in ('done','cancelled'):raise Problem('流程已结束',409)
                status='paused' if action=='pause' else 'cancelled' if action=='cancel_run' else 'running'
                c.execute('UPDATE automation_runs SET status=?,updated_at=? WHERE id=?',(status,now(),r['id']))
                if action=='cancel_run':
                    from workflow_visual import cancel_children
                    cancel_children(c,c.execute("SELECT data FROM automation_items WHERE run_id=? AND status NOT IN ('done','cancelled')",(r['id'],)).fetchall())
                    c.execute("UPDATE automation_items SET status='cancelled',message='已取消后续步骤；已发生的操作和在途请求不会回滚',updated_at=? WHERE run_id=? AND status NOT IN ('done','cancelled')",(now(),r['id']))
                return {'id':r['id']}
            i=c.execute('SELECT * FROM automation_items WHERE id=?',(b.get('item_id'),)).fetchone()
            if not i:raise Problem('商品流程不存在',404)
            plan=json.loads(c.execute('SELECT plan FROM automation_runs WHERE id=?',(i['run_id'],)).fetchone()[0]);step=plan['steps'][i['step']]
            if action=='cancel_item' and i['status'] not in ('done','cancelled'):
                from workflow_visual import cancel_children
                cancel_children(c,[i])
                c.execute("UPDATE automation_items SET status='cancelled',message='已取消后续步骤；在途请求不会回滚',updated_at=? WHERE id=?",(now(),i['id']))
            elif action in ('retry','retry_fallback') and i['status'] in ('attention','approval'):
                if step=='submit':raise Problem('提交结果可能已到达平台，禁止流程直接重发。请在商品页面回查平台后重新安排。',409)
                if action=='retry_fallback' and step!='translate':raise Problem('备用模型重试只适用于内容生成步骤')
                p=self.store.unpack(c.execute('SELECT * FROM products WHERE id=?',(i['product_id'],)).fetchone())
                if p['revision']!=b.get('revision'):raise Problem('商品已更新，请刷新后重试',409)
                data=json.loads(i['data'])
                if step=='video':
                    task=c.execute('SELECT status,result FROM media_tasks WHERE id=?',(data.get('video_task'),)).fetchone() if data.get('video_task') else None
                    rejected=bool(task and task['status']=='done' and any(
                        (self.app.media.get(aid,c).get('video_review') or {}).get('decision')=='rejected'
                        for aid in json.loads(task['result'] or '[]')))
                    if not task or task['status'] in ('failed','interrupted','cancelled') or rejected:
                        for field in ('video_task','video_request','video_signature','video_outputs','video_owned'):data.pop(field,None)
                if step=='image_host' and data.get('host_job'):
                    job=c.execute('SELECT status FROM jobs WHERE id=?',(data['host_job'],)).fetchone()
                    if job and job['status'] in ('failed','interrupted','cancelled'):data.pop('host_job')
                data.pop('children',None);data.pop('image_assets',None)
                if action=='retry_fallback':data['translation_role']='fallback'
                c.execute("UPDATE automation_items SET status='queued',revision=?,attempt=attempt+1,data=?,message='按当前版本重试本步',updated_at=? WHERE id=?",(p['revision'],json.dumps(data),now(),i['id']))
                c.execute("UPDATE automation_runs SET status='running',updated_at=? WHERE id=? AND status NOT IN ('paused','cancelled')",(now(),i['run_id']))
            else:raise Problem('此状态不能执行该操作',409)
        return {'id':i['id']}
    def rework_visual(self,b):
        if b.get('confirmed') is not True:raise Problem('请确认重做会再次使用订阅额度')
        key=b.get('request_id')
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9-]{1,80}',key):raise Problem('重做请求编号无效')
        jid=b.get('job_id');stamp=b.get('expected_updated_at')
        if not isinstance(jid,str) or not isinstance(stamp,str):raise Problem('请刷新原视觉任务后再重做')
        ledger='visual-workflow-rework:'+key
        fingerprint=hashlib.sha256(json.dumps([jid,stamp],ensure_ascii=False).encode()).hexdigest()
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            prior=c.execute('SELECT digest,result FROM ops_requests WHERE key=?',(ledger,)).fetchone()
            if prior:
                if prior['digest']!=fingerprint:raise Problem('重做请求编号已用于其他任务',409)
                return {**json.loads(prior['result']),'replayed':True}
            old=self.app.visuals.get(jid,c)
            if old['status'] not in ('rejected','output_rejected') or old['updated_at']!=stamp:
                raise Problem('原候选图状态已变化，请刷新后核对',409)
            recipe=old['recipe'];pid=recipe['product_id']
            owners=[]
            for row in c.execute("SELECT i.*,r.plan,r.status AS run_status FROM automation_items i JOIN automation_runs r ON r.id=i.run_id WHERE i.product_id=? AND i.status IN ('attention','approval','waiting')",(pid,)):
                data=json.loads(row['data']);plan=json.loads(row['plan']);link=data.get('ai_visual') or {}
                if jid in link.get('job_ids',[]) and plan['steps'][row['step']]=='ai_visual':owners.append((row,data,plan,link))
            if len(owners)!=1:raise Problem('无法唯一找到仍在视觉步骤的商品流程，请到自动化中心核对',409)
            item,data,plan,link=owners[0]
            if item['run_status'] in ('paused','cancelled','done'):raise Problem('所属流程已暂停或结束，请先核对流程状态',409)
            product=self.store.unpack(c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone())
            if product['revision']!=item['revision'] or link['source_signature']!=recipe['source_signature']:
                raise Problem('商品或原图依据已变化，请先核对当前商品版本',409)
            self.app.visuals.current(recipe)
            note=(old.get('review') or {}).get('note','').strip()
            extra='\n\n上一张未通过验收，请修正：'+note if note else ''
            brief=recipe['brief']+extra
            if len(brief)>3000:raise Problem('补充要求已超过长度限制，请在单件视觉制作中整理重做要求',409)
            shots=list(link['job_ids']);position=shots.index(jid)
            check_ids=list(link.get('check_ids') or [])
            profile=None
            if check_ids:
                if len(check_ids)!=len(shots):raise Problem('原流程的图片检查记录不完整，请先核对',409)
                profile=self.app.models.get(plan['ai_visual']['check_profile_id'],c)
                if profile['revision']!=plan['ai_visual'].get('check_profile_revision') or not profile['enabled'] or profile['provider']!='codex-subscription':
                    raise Problem('图片对照模型配置已变化，请先核对流程',409)
            submitted=self.app.visuals.submit({'request_id':'wf-rework-'+key,'product_id':pid,'revision':product['revision'],
                'asset_ids':[ref['id'] for ref in recipe['references']],'shots':[recipe['shot']],
                'model':recipe['model'],'aspect':recipe['aspect'],'style':recipe['style'],'brief':brief,
                'shot_briefs':recipe.get('shot_briefs',{}),
                'locked_features':recipe['identity']['locked_features'],'confirmed':True,'confirmed_unassigned':True},connection=c)
            replacement=submitted['job_ids'][0];shots[position]=replacement
            if old['status']=='output_rejected':
                c.execute("UPDATE visual_jobs SET status='cancelled',message='不合规格输出已由新任务替代；原文件保留',updated_at=? WHERE id=?",(now(),jid))
            if profile:
                old_check=check_ids[position]
                checked=c.execute('SELECT status FROM jobs WHERE id=? AND kind=?',(old_check,'visual-check')).fetchone()
                if not checked:raise Problem('原图片检查任务不存在，请先核对',409)
                if checked['status'] in ('waiting_image','queued','waiting','paused','blocked') and not c.execute('SELECT 1 FROM model_calls WHERE request_key=?',('visual-check-'+old_check,)).fetchone():
                    c.execute("UPDATE jobs SET status='cancelled',message='原图已重做，停止尚未发送的旧图片检查',updated_at=? WHERE id=?",(now(),old_check))
                check_ids[position]=self.app.visual_checks.reserve([replacement],profile,'workflow-rework:'+item['id'],c)[0]
            data['ai_visual']={**link,'job_ids':shots,'check_ids':check_ids}
            data.pop('retry_at',None)
            c.execute("UPDATE automation_items SET status='waiting',attempt=attempt+1,data=?,message='未通过图片已重新排队，等待新图与验收',updated_at=? WHERE id=?",(json.dumps(data,ensure_ascii=False),now(),item['id']))
            c.execute("UPDATE automation_runs SET status='running',updated_at=? WHERE id=? AND status NOT IN ('paused','cancelled')",(now(),item['run_id']))
            c.execute('INSERT INTO automation_events(item_id,step,status,message,created_at) VALUES(?,?,?,?,?)',(item['id'],'ai_visual','waiting','重做未通过图片；原任务与验收记录保留',now()))
            result={'job_id':replacement,'item_id':item['id'],'run_id':item['run_id'],'replayed':False}
            c.execute('INSERT INTO ops_requests VALUES(?,?,?)',(ledger,fingerprint,json.dumps(result,ensure_ascii=False)))
        self.app.visuals.kick()
        return result
    def save(self,i,status,message,advance=False,data=None,revision=None):
        saved_data=dict(data if data is not None else i['data'])
        if status in ('waiting','approval') and not saved_data.get('retry_at'):
            saved_data['retry_at']=(datetime.now(timezone.utc)+timedelta(seconds=5)).isoformat()
        elif status not in ('waiting','approval'):
            saved_data.pop('retry_at',None)
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            # A concurrent cancellation remains terminal, even if an in-flight call completes.
            row=c.execute('SELECT status FROM automation_items WHERE id=?',(i['id'],)).fetchone()
            if row['status']=='cancelled':return
            c.execute('UPDATE automation_items SET status=?,step=?,revision=?,data=?,message=?,updated_at=? WHERE id=?',(status,i['step']+int(advance),revision if revision is not None else i['revision'],json.dumps(saved_data,ensure_ascii=False),message,now(),i['id']))
            if advance or status!=i['status'] or message!=i['message']:
                c.execute('INSERT INTO automation_events(item_id,step,status,message,created_at) VALUES(?,?,?,?,?)',(i['id'],i['step_name'],status,message,now()))
    def checkpoint(self,i,data):
        with self.store.connect() as c:c.execute('UPDATE automation_items SET data=?,updated_at=? WHERE id=? AND status!=?',(json.dumps(data),now(),i['id'],'cancelled'))
        i['data']=data
    def loop(self):
        while not self.stop.is_set():
            try:self.tick()
            except Exception:
                # Unexpected failures are visible and isolated; never run the same step silently forever.
                with self.store.connect() as c:c.execute("UPDATE automation_items SET status='attention',message='流程执行异常，请检查后重试',updated_at=? WHERE status='processing'",(now(),))
            self.stop.wait(.4)
    def tick(self):
        # Bound each pass and rotate by last update. A waiting item cannot keep
        # thousands of untouched products behind it in a large catalog run.
        with self.store.connect() as c:
            cutoff=now()
            work=[row['id'] for row in c.execute("""SELECT i.id FROM automation_items i
                JOIN automation_runs r ON r.id=i.run_id
                WHERE r.status IN ('queued','running','attention') AND r.run_at<=?
                  AND i.status IN ('queued','waiting','approval')
                  AND coalesce(json_extract(i.data,'$.retry_at'),'')<=?
                ORDER BY i.updated_at,i.rowid LIMIT 100""",(cutoff,cutoff))]
        for item_id in work:
            if self.stop.is_set():break
            with self.store.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                row=c.execute('SELECT * FROM automation_items WHERE id=?',(item_id,)).fetchone()
                if not row or row['status'] not in ('queued','waiting','approval'):continue
                run=c.execute('SELECT * FROM automation_runs WHERE id=?',(row['run_id'],)).fetchone()
                if not run or run['status'] not in ('queued','running','attention') or run['run_at']>now():continue
                i=dict(row);i['data']=json.loads(i['data']);i['plan']=json.loads(run['plan']);i['step_name']=i['plan']['steps'][i['step']]
                if i['data'].get('retry_at','')>now():continue
                c.execute("UPDATE automation_items SET status='processing' WHERE id=?",(i['id'],))
                c.execute("UPDATE automation_runs SET status='running',updated_at=? WHERE id=?",(now(),run['id']))
            if self.stop.is_set():
                self.save(i,i['status'],i['message']);break
            try:self.process(i)
            except ModelLimit as e:self.save(i,'waiting',str(e),data={**i['data'],'retry_at':e.retry_at})
            except Problem as e:self.save(i,'attention',str(e))
            except Exception:self.save(i,'attention','此步执行异常，未继续后续步骤；请检查资料和服务配置')
        if time.monotonic()-self.last_status_scan<5:return
        self.last_status_scan=time.monotonic()
        with self.store.connect() as c:
            groups=c.execute("""SELECT r.id,r.status,count(i.id) AS total,
                sum(CASE WHEN i.status IN ('done','cancelled') THEN 1 ELSE 0 END) AS finished,
                sum(CASE WHEN i.status NOT IN ('done','cancelled','attention','approval') THEN 1 ELSE 0 END) AS active
                FROM automation_runs r JOIN automation_items i ON i.run_id=r.id
                WHERE r.status IN ('queued','running','attention') AND r.run_at<=?
                GROUP BY r.id""",(now(),)).fetchall()
            for group in groups:
                status='done' if group['finished']==group['total'] else 'attention' if not group['active'] else 'running'
                if status!=group['status']:
                    c.execute("UPDATE automation_runs SET status=? WHERE id=? AND status NOT IN ('paused','cancelled')",(status,group['id']))
    def process(self,i):
        p=self.store.get(i['product_id']);step=i['step_name'];data=i['data'];key=f"workflow:{i['id']}:{step}:{i['attempt']}"
        data.pop('retry_at',None)
        if step=='video':
            from workflow_video import process
            return process(self,i,p)
        if step=='image_host':
            from workflow_host import process
            return process(self,i,p)
        if step=='ai_visual':
            from workflow_visual import process
            return process(self,i,p)
        if p['revision']!=i['revision']:
            if step=='approval' and p['reviewed'] and not p['issues']:
                self.save(i,'queued','当前版本已人工审核',True,revision=p['revision']);return
            raise Problem('商品内容已被其他操作修改，请核对后按当前版本重试',409)
        if step=='source':
            missing=[label for k,label in [('source_url','货源链接'),('supplier','供应商'),('facts','规格事实')] if not p[k]]
            if missing:raise Problem('货源资料不完整：'+ '、'.join(missing))
            self.save(i,'queued','货源基础字段齐全',True)
        elif step=='translate':
            fields=[k for k in CONTENT_FIELDS if not p.get(k,'').strip()] if i['plan'].get('missing_only') else CONTENT_FIELDS
            if not fields:
                self.save(i,'queued','双语内容已齐全，保留原稿，未调用模型',True);return
            result=self.app.models.translate(p,key,data.get('translation_role','primary'))
            draft={k:result['content'][k] for k in fields}
            numeric=numeric_content_issues(p,{**p,**draft})
            model_warnings=[w for w in result['warnings'] if w not in result.get('numeric_issues',[])]
            notes=(model_warnings+numeric)[:20]
            data['translation']={'call_id':result['call_id'],'model':result['model'],'warnings':notes}
            self.checkpoint(i,data)
            updated=self.store.update(p['id'],draft,p['revision'],internal={'content_notes':notes})
            self.save(i,'queued',f'已保存 {len(fields)} 个双语字段，保留其他原稿，未自动审核' if i['plan'].get('missing_only') else '英阿草稿已保存，未自动审核',True,data,updated['revision'])
        elif step=='review':
            if i['plan'].get('missing_only') and p.get('content_verified') and all(p.get(k,'').strip() for k in CONTENT_FIELDS):
                self.save(i,'queued','内容已人工核对，保留确认，未调用复核模型',True);return
            if any(not p.get(k) for k in CONTENT_FIELDS):raise Problem('双语字段未齐全，不能复核')
            result=self.app.models.review(p,key);data['review']={k:result[k] for k in ('call_id','model','passed','warnings')}
            if not result['passed'] or result['warnings']:self.save(i,'attention','模型复核提出问题，请查看备注并修改商品',data=data)
            else:self.save(i,'queued','模型复核未发现问题，仍需人工审核',True,data)
        elif step=='images':
            if i['plan'].get('missing_only') and p.get('images_verified') and p['images']:
                self.save(i,'queued','图片已验收，保留现有图片与公开地址',True);return
            if not data.get('children'):
                if not p['images']:raise Problem('商品还没有图片，请先上传或从素材库加入')
                title=p['title_en'] if i['plan']['image_template']=='feature' else ''
                caption=p['description_en'] if i['plan']['image_template']=='feature' else ''
                if len(title)>120 or len(caption)>260:raise Problem('卖点辅图文字过长，请缩短英文标题或说明，或改用其他模板；未截断文字')
                originals=[]
                for im in p['images']:
                    if im.get('media_asset_id'):
                        asset=self.app.media.get(im['media_asset_id'])
                        while len(asset.get('parents',[]))==1:asset=self.app.media.get(asset['parents'][0])
                        originals.append(asset['id']);continue
                    path=self.store.assets/im['source']
                    if not path.is_file():raise Problem('商品原图文件缺失')
                    a=self.app.media.ingest(path,p['partner_sku']+' 原图',p['rights_evidence'],p['id']);originals.append(a['id'])
                data['image_assets']=originals;self.checkpoint(i,data)
                recipes=[{'kind':i['plan']['image_template'],'asset_ids':[aid],'title':title,'caption':caption} for aid in originals]
                data['children']=self.app.media.submit({'request_id':key,'recipes':recipes})['task_ids'];self.checkpoint(i,data)
                self.save(i,'waiting','图片已进入加工队列',data=data);return
            with self.store.connect() as c:
                children=[]
                for jid in data['children']:
                    row=c.execute('SELECT * FROM media_tasks WHERE id=?',(jid,)).fetchone()
                    children.append({**dict(row),'result':json.loads(row['result'])} if row else None)
            if any(t is None for t in children):raise Problem('关联媒体任务不在当前记录中，请核对')
            if any(t['status'] in ('failed','cancelled','interrupted') for t in children):raise Problem('图片加工未完成，请到图片与视频查看原因')
            if not all(t['status']=='done' for t in children):self.save(i,'waiting','等待图片加工完成',data=data);return
            outputs=[t['result'][0] for t in children]
            updated=self.app.media.attach({'asset_ids':outputs,'product_id':p['id'],'revision':p['revision'],'replace':True})
            data['outputs']=outputs;self.save(i,'queued','模板成图已替换商品图片，待重新核对',True,data,updated['revision'])
        elif step=='check':
            data['issues']=p['issues'];self.save(i,'queued','已整理'+str(len(p['issues']))+'项待核对内容',True,data)
        elif step=='approval':
            if p['reviewed'] and not p['issues']:self.save(i,'queued','当前版本已人工审核',True)
            elif i['status']!='approval':self.save(i,'approval','请打开商品补齐资料、核对内容和图片，然后审核当前版本')
            else:
                with self.store.connect() as c:c.execute("UPDATE automation_items SET status='approval' WHERE id=? AND status='processing'",(i['id'],))
        elif step=='submit':
            if not data.get('submit_job'):
                # Persist intent before a possible external write. A crash leaves attention, never auto-resubmission.
                self.checkpoint(i,{**data,'submit_intent_at':now()})
                result=self.app.queue(p['id'],'submit',p['revision']);data=i['data'];data['submit_job']=result['job_id'];self.checkpoint(i,data)
                self.save(i,'waiting','等待noon内容提交结果',data=data);return
            with self.store.connect() as c:job=c.execute('SELECT * FROM jobs WHERE id=?',(data['submit_job'],)).fetchone()
            if job is None or job['status'] in ('failed','interrupted','needs_attention','uncertain'):raise Problem('提交结果需要核对，请打开商品回查平台，流程不会自动重发')
            if job['status']!='done':self.save(i,'waiting','等待平台响应',data=data);return
            self.save(i,'queued','内容接口已返回；平台审核和实际可售尚待验证',True)
        elif step=='finish':self.save(i,'done','所选步骤已完成；不代表平台实际可售')
