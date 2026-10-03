"""Explicit current-version bulk review; never fills or confirms missing facts."""
import json
from core import Problem
from platform_batch import selection,digest

class ApprovalBatch:
    def __init__(self,store,validate_visuals=None):self.store=store;self.validate_visuals=validate_visuals
    def preview(self,b,connection=None):
        ids=selection(b)
        if connection is None:
            with self.store.connect() as c:
                c.execute('BEGIN');return self.preview(b,c)
        c=connection;rows=[]
        for pid in ids:
            saved=c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone()
            row={'id':pid,'title':'商品不存在','status':'blocked','reasons':['商品不存在'],'auto_submit':False}
            if saved:
                p=self.store.unpack(saved);reasons=list(p['issues']);flows=[]
                for i in c.execute("SELECT i.status,i.step,r.plan,r.status AS run_status FROM automation_items i JOIN automation_runs r ON r.id=i.run_id WHERE i.product_id=? AND i.status NOT IN ('done','cancelled')",(pid,)):
                    plan=json.loads(i['plan']);flows.append({'status':i['status'],'step':plan['steps'][i['step']],'run_status':i['run_status'],'submit':plan.get('submit',False)})
                if c.execute("SELECT 1 FROM jobs WHERE product_id=? AND status IN ('queued','running')",(pid,)).fetchone():reasons.append('商品有排队或执行中的后台任务')
                if any(f['step']!='approval' or f['status'] not in ('approval','attention') for f in flows):reasons.append('商品流程尚未停在最终审核步骤')
                from video_batch import gallery_files
                try:row['files']=gallery_files(self.store,p)
                except (Problem,OSError,KeyError):reasons.append('成图文件缺失或无法读取，请修复后重新预检')
                if self.validate_visuals:
                    try:self.validate_visuals(p)
                    except Problem as e:reasons.append(str(e))
                if any(f['submit'] for f in flows) and any(not im.get('public_url') for im in p['images']):reasons.append('自动提交流程仍缺成图公开地址')
                row.update(title=p['title_zh'],sku=p['partner_sku'],revision=p['revision'],reasons=reasons,status='blocked' if reasons else 'kept' if p['reviewed'] else 'ready',auto_submit=any(f['submit'] for f in flows),flows=flows,
                    facts=p['facts'],title_en=p['title_en'],title_ar=p['title_ar'],description_en=p['description_en'],description_ar=p['description_ar'],category=p['category'],rights=p['rights_evidence'],supply_checked_at=p['supply_checked_at'],images=[{'file':im['file'],'public_url':im.get('public_url','')} for im in p['images']])
            rows.append(row)
        ready=[r for r in rows if r['status']=='ready']
        return {'rows':rows,'ready':len(ready),'auto_submit':sum(r['auto_submit'] for r in ready),'token':digest(rows)}
    def apply(self,b):
        ids=selection(b);key=b.get('request_id')
        if not isinstance(key,str) or not 1<=len(key)<=100 or b.get('confirmed') is not True:raise Problem('请预检并确认本批当前版本资料')
        fingerprint=digest([ids,b.get('preview_token'),b.get('ack_auto_submit') is True])
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE');old=c.execute('SELECT * FROM ops_requests WHERE key=?',('approval-batch:'+key,)).fetchone()
            if old:
                if old['digest']!=fingerprint:raise Problem('此请求编号已用于其他审核内容',409)
                return json.loads(old['result'])
            pre=self.preview(b,c)
            if pre['token']!=b.get('preview_token'):raise Problem('商品、审核条件或流程已变化，请重新预检',409)
            if not pre['ready']:raise Problem('没有可审核的商品',409)
            if pre['auto_submit'] and b.get('ack_auto_submit') is not True:raise Problem('本批含自动提交流程，请确认审核后将继续原有自动提交安排')
            approved=[]
            for row in pre['rows']:
                if row['status']!='ready':continue
                self.store.approve(row['id'],row['revision'],connection=c);approved.append({'id':row['id'],'revision':row['revision']})
            result={'approved':approved,'skipped':[{'id':r['id'],'status':r['status'],'reasons':r['reasons']} for r in pre['rows'] if r['status']!='ready'],'auto_submit':pre['auto_submit']}
            c.execute('INSERT INTO ops_requests VALUES(?,?,?)',('approval-batch:'+key,fingerprint,json.dumps(result,ensure_ascii=False)))
        return result
