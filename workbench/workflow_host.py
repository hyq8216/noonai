"""Persist the workflow-to-hosting-job link before any external upload."""
import json
from core import Problem,ident,now

def process(auto,i,p):
    app=auto.app;data=i['data'];jid=data.get('host_job')
    if jid:
        with auto.store.connect() as c:job=c.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
        if not job or job['status'] in ('failed','interrupted','cancelled'):raise Problem('图片托管未完成，请查看任务原因后按当前版本重试；将先核对已有公网图片',409)
        if job['status']!='done':auto.save(i,'waiting','等待图片上传和公网内容核对');return
        result=json.loads(job['result']);expected={r['image_id']:r['public_url'] for r in result['images']}
        if p['revision']!=result['revision'] or {im['id']:im.get('public_url') for im in p['images']}!=expected:raise Problem('托管完成后商品已修改，请核对后重新安排流程',409)
        auto.save(i,'queued','图片已通过公网内容核对，继续刊登资料检查；仍需商品审核',True,data,p['revision']);return
    if p['revision']!=i['revision']:raise Problem('商品已修改，请核对后按当前版本重试',409)
    snapshot={**p,'_host_visual_outputs':data.get('visual_outputs',[])}
    if not p.get('images_verified') and not snapshot['_host_visual_outputs']:
        auto.save(i,'approval','请打开商品确认成图，再按当前版本重试托管步骤');return
    host=app.image_host
    with host.lock,auto.store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        item=c.execute('SELECT status,data FROM automation_items WHERE id=?',(i['id'],)).fetchone();run=c.execute('SELECT status FROM automation_runs WHERE id=?',(i['run_id'],)).fetchone()
        if item['status']=='cancelled':return
        if run['status']=='paused':c.execute('UPDATE automation_items SET status=? WHERE id=?',(i['status'],i['id']));return
        persisted=json.loads(item['data'])
        if persisted.get('host_job'):c.execute("UPDATE automation_items SET status='waiting' WHERE id=?",(i['id'],));return
        config=host.config()
        if not config or config['revision']!=i['plan']['host_config_revision']:raise Problem('托管设置已变化，请重新建立流程以确认上传目标',409)
        current=auto.store.unpack(c.execute('SELECT * FROM products WHERE id=?',(p['id'],)).fetchone())
        if current['revision']!=i['revision']:raise Problem('商品排队前已变化，请重新核对',409)
        if c.execute("SELECT 1 FROM jobs WHERE product_id=? AND status IN ('queued','running')",(p['id'],)).fetchone():raise Problem('商品还有其他后台任务，请稍后重试',409)
        if c.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]>=1000:raise Problem('后台队列已满，请稍后重试',409)
        files=host.validated_files(snapshot);snapshot.update(_host_config=config['revision'],_host_hashes=[h for _,_,h in files]);jid=ident();ts=auto.now()
        c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',(jid,p['id'],'image-host',p['revision'],'queued','流程图片等待上传与公网核对',None,ts,ts));data={**data,'host_job':jid}
        c.execute('UPDATE automation_items SET data=?,updated_at=? WHERE id=?',(json.dumps(data),ts,i['id']))
    try:app.executor.submit(app.run,jid,snapshot,'image-host')
    except RuntimeError:auto.store.job_result(jid,'failed','应用关闭，尚未发送托管')
    auto.save(i,'waiting','图片已进入托管队列；暂停流程不会撤回在途上传',data=data)

def cancel(c,items):
    for item in items:
        jid=json.loads(item['data']).get('host_job')
        if jid:c.execute("UPDATE jobs SET status='cancelled',message='所属流程取消，停止未执行托管',updated_at=? WHERE id=? AND kind='image-host' AND status='queued'",(now(),jid))
