"""Bulk reference-matched image queues; candidate approval remains per product."""
import hashlib
import json
import re
from core import Problem,now
from media import string
from visuals import SHOTS,ASPECTS,QUEUE_LIMIT,source_signature,digest,shot_briefs

class VisualBatch:
    def __init__(self,app):self.app=app;self.store=app.store
    def options(self,b,connection=None):
        ids=b.get('product_ids');shots=b.get('shots')
        if not isinstance(ids,list) or not 1<=len(ids)<=500 or any(not isinstance(i,str) for i in ids) or len(set(ids))!=len(ids):raise Problem('请选择 1 至 500 件不同商品')
        if not isinstance(shots,list) or not 1<=len(shots)<=4 or any(not isinstance(s,str) or s not in SHOTS for s in shots) or len(set(shots))!=len(shots):raise Problem('请选择 1 至 4 种拍摄方案')
        shots=[shot for shot in SHOTS if shot in shots]
        if b.get('model') not in ('gpt-6-sol','gpt-6-luna') or b.get('aspect') not in ASPECTS:raise Problem('模型或画幅无效')
        reference_ids=b.get('reference_asset_ids')
        if reference_ids is not None and (len(ids)!=1 or not isinstance(reference_ids,list) or not 1<=len(reference_ids)<=6 or
                any(not isinstance(a,str) or not a for a in reference_ids) or len(set(reference_ids))!=len(reference_ids)):
            raise Problem('指定参考图仅支持单件商品的1至6张不同原图')
        check_profile=None
        if b.get('auto_check') is True:
            check_profile=self.app.models.get(b.get('check_profile_id'),connection)
            if not check_profile['enabled'] or check_profile['provider']!='codex-subscription':raise Problem('自动检查请选择已启用的Codex订阅模型',409)
        per_shot=shot_briefs(b.get('shot_briefs'))
        return ids,{'shots':shots,'model':b['model'],'aspect':b['aspect'],'style':string(b.get('style'),2000,'整套视觉风格',True),'brief':string(b.get('brief',''),3000,'补充要求'),'check_profile':check_profile,
                    **({'shot_briefs':per_shot} if per_shot else {}),
                    **({'reference_asset_ids':reference_ids} if reference_ids is not None else {})}
    def preview(self,b,connection=None,owner_item=None):
        ids,options=self.options(b,connection)
        if connection is None:
            with self.store.connect() as c:
                c.execute('BEGIN');return self.preview(b,c,owner_item)
        c=connection;assets={};jobs={};rows=[];plans={};hashes={}
        def intact(a):
            path=self.app.media.root/a['file']
            if a['file'] not in hashes:
                if not path.is_file():hashes[a['file']]=None
                else:
                    h=hashlib.sha256()
                    with path.open('rb') as f:
                        for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
                    hashes[a['file']]=h.hexdigest()
            return hashes[a['file']]==a['sha256']
        marks=','.join('?' for _ in ids)
        for row in c.execute("SELECT data FROM media_assets WHERE json_extract(data,'$.product_id') IN ("+marks+") AND json_extract(data,'$.kind')='image' AND coalesce(json_array_length(json_extract(data,'$.parents')),0)=0 AND coalesce(json_extract(data,'$.visual_job_id'),'')='' AND coalesce(json_extract(data,'$.product_image_snapshot'),'')='' ORDER BY created_at,id",ids):
            a=json.loads(row['data'])
            assets.setdefault(a['product_id'],[]).append(a)
        queued=c.execute("SELECT count(*) FROM visual_jobs WHERE status IN ('queued','waiting','preparing','generating')").fetchone()[0];slots=QUEUE_LIMIT-queued
        check_queued=c.execute("SELECT count(*) FROM jobs WHERE kind='visual-check' AND status IN ('waiting_image','queued','running','waiting','paused')").fetchone()[0]
        check_slots=2000-check_queued
        for row in c.execute("SELECT id,status,recipe,updated_at FROM visual_jobs WHERE status NOT IN ('cancelled','rejected') AND json_extract(recipe,'$.product_id') IN ("+marks+") ORDER BY created_at DESC,rowid DESC",ids):
            j=dict(row);j['recipe']=json.loads(j['recipe'])
            jobs.setdefault(j['recipe']['product_id'],[]).append(j)
        for pid in ids:
            p=self.store.unpack(c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone());refs=assets.get(pid,[]);existing=jobs.get(pid,[]);reasons=[]
            if options.get('reference_asset_ids') is not None:
                by_id={a['id']:a for a in refs}
                missing=[aid for aid in options['reference_asset_ids'] if aid not in by_id]
                if missing:reasons.append('指定原图已不属于当前商品或不再是独立原图，请重新核对参考图清单')
                refs=[by_id[aid] for aid in options['reference_asset_ids'] if aid in by_id]
            signature=source_signature(p);kept=[];needed=[];current_refs={a['id']:a for a in refs}
            for shot in options['shots']:
                found=next((j for j in existing if j['recipe']['source_signature']==signature and j['recipe']['shot']==shot and j['status'] in ('candidate','approved') and all(j['recipe'][k]==options[k] for k in ('style','brief','model','aspect')) and j['recipe'].get('shot_brief','')==options.get('shot_briefs',{}).get(shot,'') and {a['id'] for a in j['recipe']['references']}==set(current_refs) and all(all(a[k]==current_refs[a['id']][k] for k in ('file','sha256','rights')) and intact(a) for a in j['recipe']['references'])),None)
                if found:kept.append({'id':found['id'],'shot':shot,'status':found['status']})
                else:needed.append(shot)
            if p['demo']:reasons.append('示例商品不能进入批量生图')
            if needed:
                if not p['facts'].strip():reasons.append('请先填写商品规格事实，作为必须保留的产品特征')
                elif len(p['facts'])>3000:reasons.append('规格事实超过3000字，请先在单件视觉制作中整理锁定特征')
                if not refs:reasons.append('没有已关联此商品的原图，请在图片与视频中导入并关联原图')
                elif len(refs)>6:reasons.append('关联原图超过6张，请在单件视觉制作中明确选择')
                for a in refs:
                    path=self.app.media.root/a['file']
                    if not a.get('rights'):reasons.append('原图缺少使用依据：'+a['name'])
                    elif not intact(a):reasons.append('原图缺失或文件发生变化：'+a['name'])
                if any(j['status'] in ('queued','waiting','preparing','generating','blocked','uncertain','output_rejected') for j in existing):reasons.append('已有未结束或待核对的生图任务，请先处理原任务')
                if c.execute("SELECT 1 FROM automation_items WHERE product_id=? AND id!=? AND status NOT IN ('done','cancelled')",(pid,owner_item or '')).fetchone() or c.execute("SELECT 1 FROM jobs WHERE product_id=? AND status IN ('queued','running')",(pid,)).fetchone():reasons.append('已有其他未结束商品任务，请先完成或取消')
            status='blocked' if reasons else 'ready' if needed else 'kept'
            if status=='ready' and len(needed)>slots:status='capacity';reasons.append('待制作清单容量不足，请完成或取消部分任务后再加入')
            if status=='ready' and options['check_profile'] and len(needed)>check_slots:status='capacity';reasons.append('待检查容量不足，请先处理已有检查任务')
            if status=='ready':
                slots-=len(needed)
                if options['check_profile']:check_slots-=len(needed)
                plans[pid]={**options,'product_id':pid,'revision':p['revision'],'asset_ids':[a['id'] for a in refs],'shots':needed,'locked_features':p['facts'],'confirmed':True}
            rows.append({'id':pid,'title':p['title_zh'],'revision':p['revision'],'status':status,'reasons':list(dict.fromkeys(reasons)),
                'references':[{k:a[k] for k in ('id','name','preview','sha256','rights')} for a in refs], 'locked_features':p['facts'],'attribute_values':p.get('attribute_values',{}),'new_shots':needed,'kept':kept,'tasks':[{'id':j['id'],'shot':j['recipe']['shot'],'status':j['status']} for j in existing[:8]]})
        count=sum(len(p['shots']) for p in plans.values())
        return {'token':digest([options,rows,queued,check_queued if options['check_profile'] else None]),'rows':rows,'plans':plans,'new_tasks':count,'check_tasks':count if options['check_profile'] else 0,'check_profile':options['check_profile'],'queued':queued,'queue_limit':QUEUE_LIMIT,'daily_limit':self.app.visuals.daily_limit(c)}
    def apply(self,b):
        key=string(b.get('request_id'),100,'操作编号',True)
        if not re.fullmatch(r'[A-Za-z0-9-]+',key):raise Problem('操作编号无效')
        if b.get('confirmed') is not True:raise Problem('请确认已检查每件商品的原图归属、使用依据和规格事实')
        h=digest(['visual_batch',b]);request_key='visual-batch:'+key
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE');old=c.execute('SELECT digest,result FROM ops_requests WHERE key=?',(request_key,)).fetchone()
            if old:
                if old['digest']!=h:raise Problem('操作编号已用于不同内容',409)
                return json.loads(old['result'])
            preview=self.preview(b,c)
            if preview['token']!=b.get('preview_token'):raise Problem('商品、原图、任务或队列容量已变化，请重新预检',409)
            if not preview['plans']:raise Problem('没有可加入的制作任务，请先处理预检问题',409)
            ids=[];check_ids=[]
            for pid,plan in preview['plans'].items():
                result=self.app.visuals.submit({**plan,'request_id':'vb-'+hashlib.sha256((key+pid).encode()).hexdigest()},connection=c);ids.extend(result['job_ids'])
                self.store.event(c,pid,'批量视觉排队','；'.join(SHOTS[s]['name'] for s in plan['shots']))
            if preview['check_profile']:check_ids=self.app.visual_checks.reserve(ids,preview['check_profile'],request_key,c)
            out={'job_ids':ids,'check_job_ids':check_ids,'product_ids':list(preview['plans']),'skipped':[r['id'] for r in preview['rows'] if r['status']!='ready']}
            c.execute('INSERT INTO ops_requests VALUES(?,?,?)',(request_key,h,json.dumps(out)))
        self.app.visuals.kick();return out
