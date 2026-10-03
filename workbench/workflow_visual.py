"""Bridge the product workflow to existing visual and comparison queues."""
import json
from datetime import datetime,timedelta,timezone
from core import Problem,now
from models import ModelLimit,text
from visuals import SHOTS,ASPECTS,source_signature,shot_briefs

def options(raw):
    if not isinstance(raw,dict):raise Problem('AI视觉方案无效')
    shots=raw.get('shots')
    if not isinstance(shots,list) or not 1<=len(shots)<=4 or any(not isinstance(s,str) or s not in SHOTS for s in shots) or len(set(shots))!=len(shots):raise Problem('请选择1至4种AI拍摄方案')
    if 'hero' not in shots:raise Problem('自动替换商品图组必须包含白底主图；单独制作辅图请使用电商视觉制作')
    shots=[shot for shot in SHOTS if shot in shots]
    if raw.get('model') not in ('gpt-6-sol','gpt-6-luna') or raw.get('aspect') not in ASPECTS:raise Problem('AI模型或画幅无效')
    result={'shots':shots,'model':raw['model'],'aspect':raw['aspect'],'style':text(raw.get('style'),'统一视觉风格',2000),'brief':text(raw.get('brief',''),'补充要求',3000,False),'auto_check':raw.get('auto_check') is True}
    per_shot=shot_briefs(raw.get('shot_briefs'))
    if per_shot:result['shot_briefs']=per_shot
    if result['auto_check']:result['check_profile_id']=text(raw.get('check_profile_id'),'对照检查模型',100)
    if raw.get('reference_asset_ids') is not None:
        refs=raw['reference_asset_ids']
        if not isinstance(refs,list) or not 1<=len(refs)<=6 or any(not isinstance(a,str) or not a for a in refs) or len(set(refs))!=len(refs):
            raise Problem('请指定1至6张不同的商品原图')
        result['reference_asset_ids']=refs
    return result

def process(auto,i,p):
    app=auto.app;data=i['data'];link=data.get('ai_visual')
    if 'hero' not in i['plan']['ai_visual']['shots']:
        raise Problem('自动替换图组缺少白底主图，请按包含主图的新方案重新建立流程',409)
    if not link:
        if p['revision']!=i['revision']:raise Problem('商品已修改，请按当前版本重试',409)
        with auto.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            item=c.execute('SELECT * FROM automation_items WHERE id=?',(i['id'],)).fetchone()
            run=c.execute('SELECT status FROM automation_runs WHERE id=?',(i['run_id'],)).fetchone()
            if item['status']=='cancelled':return
            if run['status']=='paused':auto_status=i['status'];c.execute('UPDATE automation_items SET status=? WHERE id=?',(auto_status,i['id']));return
            persisted=json.loads(item['data'])
            if persisted.get('ai_visual'):data=persisted;link=data['ai_visual']
            else:
                b={'product_ids':[p['id']],**i['plan']['ai_visual']}
                pre=app.visual_batch.preview(b,c,owner_item=i['id']);row=pre['rows'][0]
                if pre['check_profile'] and pre['check_profile']['revision']!=i['plan']['ai_visual'].get('check_profile_revision'):raise Problem('对照检查模型配置已变化，请重新安排流程',409)
                if row['revision']!=i['revision']:raise Problem('商品在排队前已变化，请重新核对',409)
                if row['status']=='capacity':
                    retry=(datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat()
                    raise ModelLimit('AI制作或对照检查队列已满，一分钟后自动重试',retry)
                if row['status'] not in ('ready','kept'):raise Problem('AI制作预检未通过：'+'；'.join(row['reasons']),409)
                job_ids=[j['id'] for j in row['kept']]
                if row['status']=='ready':job_ids+=app.visuals.submit({**pre['plans'][p['id']],'request_id':'wf-'+i['id']},connection=c)['job_ids']
                # Preserve requested shot order even when some candidates were reused.
                by_shot={app.visuals.get(jid,c)['recipe']['shot']:jid for jid in job_ids};job_ids=[by_shot[shot] for shot in b['shots']]
                checks=app.visual_checks.reserve(job_ids,pre['check_profile'],'workflow:'+i['id'],c) if pre['check_profile'] else []
                link={'job_ids':job_ids,'check_ids':checks,'source_signature':source_signature(p)};data={**data,'ai_visual':link}
                c.execute('UPDATE automation_items SET data=?,updated_at=? WHERE id=?',(json.dumps(data,ensure_ascii=False),now(),i['id']))
                auto.store.event(c,p['id'],'流程AI视觉排队',f'关联 {len(job_ids)} 张成图与 {len(checks)} 项对照检查；未自动验收')
        i['data']=data;app.visuals.kick()
        auto.save(i,'waiting','AI制作已排队，成图后继续对照检查与成片验收',data=data);return
    if source_signature(p)!=link['source_signature']:raise Problem('商品身份资料已变化，原AI任务不得继续用于此商品；请核对后重新建立流程',409)
    jobs=[app.visuals.get(jid) for jid in link['job_ids']]
    if any(j['status'] in ('blocked','uncertain','output_rejected','rejected','cancelled') for j in jobs):raise Problem('AI制作有待核对或未通过任务，请到电商视觉制作处理；本流程不会自动重做',409)
    if not all(j['status'] in ('candidate','approved') for j in jobs):auto.save(i,'waiting','等待AI制作；额度等待和制作进度见电商视觉制作',data=data,revision=p['revision']);return
    if link['check_ids']:
        with auto.store.connect() as c:checks=[c.execute('SELECT id,status,result FROM jobs WHERE id=?',(jid,)).fetchone() for jid in link['check_ids']]
        if any(j is None or j['status'] in ('blocked','failed','uncertain','cancelled','interrupted') for j in checks):raise Problem('图片对照检查需要处理，请查看原检查任务；未自动重新调用',409)
        if not all(j['status']=='done' for j in checks):auto.save(i,'waiting','等待图片对照检查完成',data=data,revision=p['revision']);return
    if not all(j['status']=='approved' for j in jobs):auto.save(i,'approval','请在电商视觉制作逐张验收本流程成图；通过后自动加入商品',data=data,revision=p['revision']);return
    if link['check_ids']:
        approved_overrides={jid for j in jobs for jid in (j.get('review') or {}).get('overridden_check_ids',[])}
        if any(json.loads(check['result'] or '{}').get('report',{}).get('verdict')!='match' and check['id'] not in approved_overrides for check in checks):
            auto.save(i,'approval','图片对照提示差异或无法判断；请在电商视觉制作重新验收并记录采用原因',data=data,revision=p['revision']);return
    with auto.store.connect() as c:
        unresolved=any(app.visuals.unresolved_comparisons(j,c) for j in jobs)
    if unresolved:
        auto.save(i,'approval','新增图片对照结论待复核；请在电商视觉制作重新验收',data=data,revision=p['revision']);return
    by_shot={j['recipe']['shot']:j for j in jobs}
    if 'hero' not in by_shot or len(by_shot)!=len(jobs):raise Problem('自动图组缺少白底主图或有重复拍摄类型，请重新核对后建立流程',409)
    outputs=[by_shot[shot]['asset_id'] for shot in SHOTS if shot in by_shot]
    assets=[app.media.get(aid) for aid in outputs]
    if len({asset['sha256'] for asset in assets})!=len(assets):
        raise Problem('自动图组中不同拍摄类型返回了完全相同的图片文件，请重做重复图片后继续',409)
    for asset in assets:app.visuals.validate_asset(asset,p['id'])
    present={im.get('media_asset_id') for im in p['images']}
    if not set(outputs)<=present:
        if set(outputs)&present:raise Problem('本流程只有部分成图已加入商品，请先整理图片，避免重复替换',409)
        p=app.media.attach({'asset_ids':outputs,'product_id':p['id'],'revision':p['revision'],'replace':True})
    data['visual_outputs']=outputs
    auto.save(i,'queued','AI成图已验收并加入商品，继续刊登资料检查；仍需审核商品当前版本',True,data,p['revision'])

def cancel_children(c,items):
    from workflow_host import cancel
    cancel(c,items)
    from workflow_video import cancel as cancel_video
    cancel_video(c,items)
    for item in items:
        link=json.loads(item['data']).get('ai_visual',{})
        for jid in link.get('job_ids',[]):
            c.execute("UPDATE visual_jobs SET status='cancelled',message='所属商品流程取消，停止未发送制作',updated_at=? WHERE id=? AND dispatched_at IS NULL AND status IN ('queued','waiting')",(now(),jid))
        for jid in link.get('check_ids',[]):
            c.execute("UPDATE jobs SET status='cancelled',message='所属商品流程取消，停止未发送对照检查',updated_at=? WHERE id=? AND status IN ('waiting_image','queued','waiting','paused')",(now(),jid))
