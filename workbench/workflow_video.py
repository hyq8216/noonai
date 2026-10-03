"""Reuse the gallery-video queue with a durable request checkpoint."""
import json
from core import Problem
from video_batch import VideoBatch,gallery_signature,file_hash

def process(auto,i,p):
    data=i['data'];batch=VideoBatch(auto.app)
    if p['revision']!=i['revision']:raise Problem('商品在视频步骤前已变化，请审核后按当前版本重试',409)
    if not p['reviewed'] or p['issues']:raise Problem('请先完成当前商品版本审核，再制作流程视频',409)
    tid=data.get('video_task')
    if not tid:
        request=data.get('video_request')
        if not request:
            pre=batch.preview({'product_ids':[p['id']],**i['plan']['video']});row=pre['rows'][0]
            if row['status'] in ('kept','active'):
                data.update(video_task=row['task_id'],video_signature=row['signature'],video_owned=False);auto.checkpoint(i,data);tid=row['task_id']
            elif row['status']=='ready':
                request={'product_ids':[p['id']],**i['plan']['video'],'confirmed':True,'preview_token':pre['token'],'request_id':'workflow-video-'+i['id']+'-'+str(i['attempt'])}
                data.update(video_request=request,video_signature=row['signature'],video_owned=True);auto.checkpoint(i,data)
            else:raise Problem('视频预检未通过：'+'；'.join(row['reasons']),409)
        if not tid:
            # The exact persisted request is replayed after a crash, even if the queue changed.
            result=batch.apply(request,owner_item=i['id']);tid=result['task_ids'][0];data['video_task']=tid;auto.checkpoint(i,data)
    if gallery_signature(auto.store,p)!=data['video_signature']:raise Problem('视频对应的图片或验收信息已变化，请重新核对',409)
    with auto.store.connect() as c:task=c.execute('SELECT * FROM media_tasks WHERE id=?',(tid,)).fetchone()
    if not task or task['status'] in ('failed','interrupted','cancelled'):raise Problem('视频制作未完成，请在图片与视频查看原因，修复原任务后重试本步骤',409)
    if task['status']!='done':auto.save(i,'waiting','等待商品展示视频制作，可到图片与视频查看进度',data=data);return
    outputs=json.loads(task['result'])
    if not outputs:raise Problem('视频任务未返回成品',409)
    reviews=[]
    for aid in outputs:
        a=auto.app.media.get(aid);path=auto.app.media.root/a['file']
        if a['kind']!='video' or not path.is_file() or file_hash(path)!=a['sha256']:raise Problem('视频成品缺失或发生变化，请重新核对',409)
        reviews.append((a.get('video_review') or {},a['sha256']))
    data['video_outputs']=outputs
    if any(r.get('decision')=='rejected' for r,_ in reviews):raise Problem('商品展示视频未通过播放验收，请在自动化中心按当前版本重做视频步骤',409)
    if not all(r.get('decision')=='approved' and r.get('signature')==data['video_signature'] and r.get('sha256')==sha for r,sha in reviews):
        auto.save(i,'approval','商品展示视频已生成，请播放完整成品并逐项验收；未上传或刊登视频',data=data);return
    auto.save(i,'queued','商品展示视频已播放验收；未上传或刊登视频',True,data)

def cancel(c,items):
    for item in items:
        data=json.loads(item['data'])
        if data.get('video_owned') and data.get('video_task'):
            c.execute("UPDATE media_tasks SET status='cancelled',message='所属商品流程取消，停止未执行视频' WHERE id=? AND status='queued'",(data['video_task'],))
