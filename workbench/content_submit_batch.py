"""Explicit batch queue for already approved noon content.

Preview and queue creation are local. Each SKU is submitted by the existing
single-product worker, so a partial batch can be inspected without replaying
successful or uncertain writes.
"""
import json
import re

from core import Problem, ident, now
from platform_batch import digest, selection


class ContentSubmitBatch:
    prefix = 'content-submit-batch:'

    def __init__(self, app):
        self.app = app
        self.store = app.store

    def preview(self, body, connection=None):
        ids = selection(body)
        if connection is None:
            with self.store.connect() as c:
                c.execute('BEGIN')
                return self.preview(body, c)
        c = connection
        config = self.app.config()
        queued = c.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
        rows = []
        for pid in ids:
            saved = c.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone()
            if saved is None:
                rows.append({'id': pid, 'title': '商品不存在', 'sku': '', 'status': 'blocked', 'reasons': ['商品已不存在']})
                continue
            p = self.store.unpack(saved)
            reasons = []
            if p['demo']: reasons.append('示例商品不能提交')
            if not config['noon_ready']: reasons.append('noon店铺尚未接入')
            if not config['submit_enabled']: reasons.append('真实内容提交开关尚未启用')
            if not p['reviewed']: reasons.append('当前版本尚未完成审核')
            if p['issues']: reasons.extend(p['issues'][:5])
            if not p['images'] or any(not im.get('public_url') for im in p['images']):
                reasons.append('成图缺少可公开访问的HTTPS地址')
            try:self.app.validate_product_visuals(p)
            except Problem as e:reasons.append(str(e))
            if (p.get('platform') or {}).get('submitted_revision') == p['revision']:
                reasons.append('当前版本已获得平台提交回执，先回查或修改商品')
            if c.execute("SELECT 1 FROM jobs WHERE product_id=? AND status IN ('queued','running')", (pid,)).fetchone():
                reasons.append('商品已有处理任务')
            if c.execute("SELECT 1 FROM jobs WHERE product_id=? AND kind='submit' AND (status IN ('uncertain','interrupted') OR (status='needs_attention' AND revision=?))", (pid,p['revision'])).fetchone():
                reasons.append('该商品此前提交回执待核对；请按 SKU 回查并完成人工核对，系统不会自动重发')
            rows.append({'id': pid, 'title': p['title_zh'], 'sku': p['partner_sku'], 'revision': p['revision'],
                         'status': 'blocked' if reasons else 'ready', 'reasons': reasons})
        ready = sum(r['status'] == 'ready' for r in rows)
        return {'rows': rows, 'ready': ready, 'blocked': len(rows)-ready, 'queued': queued,
                'capacity_ok': queued+ready <= 1000,
                'token': digest([rows, queued, config['noon_ready'], config['submit_enabled']])}

    def apply(self, body):
        ids = selection(body)
        request_id = body.get('request_id')
        if not isinstance(request_id,str) or not request_id.strip() or len(request_id)>100:
            raise Problem('批量提交请求编号无效')
        if body.get('confirmed') is not True:
            raise Problem('请确认提交预检中可用的商品内容')
        key = self.prefix+request_id
        fingerprint = digest([ids,body.get('preview_token')])
        dispatch = []
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old = c.execute('SELECT * FROM ops_requests WHERE key=?',(key,)).fetchone()
            if old:
                if old['digest'] != fingerprint: raise Problem('同一请求编号不能用于不同批次',409)
                return {**json.loads(old['result']), 'replayed': True}
            pre = self.preview(body,c)
            if pre['token'] != body.get('preview_token'):
                raise Problem('商品、店铺配置或任务队列已变化，请重新预检',409)
            if not pre['ready']: raise Problem('没有可提交的商品',409)
            if not pre['capacity_ok']: raise Problem('待执行任务超过1000项，请稍后重新预检',409)
            for row in pre['rows']:
                if row['status'] != 'ready': continue
                p = self.store.unpack(c.execute('SELECT * FROM products WHERE id=?',(row['id'],)).fetchone())
                jid = ident(); ts = now()
                c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',
                          (jid,p['id'],'submit',p['revision'],'queued','批量内容提交等待处理',None,ts,ts))
                self.store.event(c,p['id'],'批量刊登排队',f"当前审核版本 {p['revision']} 已排队提交 noon 内容")
                dispatch.append((jid,p))
            result = {'request_id': request_id,
                      'jobs': [{'job_id': jid,'product_id': p['id']} for jid,p in dispatch],
                      'skipped': [r for r in pre['rows'] if r['status']!='ready'], 'replayed': False}
            c.execute('INSERT INTO ops_requests VALUES(?,?,?)',(key,fingerprint,json.dumps(result,ensure_ascii=False)))
        for jid,p in dispatch:
            try: self.app.executor.submit(self.app.run,jid,p,'submit')
            except RuntimeError:
                self.store.job_result(jid,'failed','应用正在关闭，本任务尚未发送；重新打开后再安排')
        return result

    def status(self, request_id):
        if not isinstance(request_id,str) or not request_id.strip() or len(request_id)>100:
            raise Problem('批量提交请求编号无效')
        with self.store.connect() as c:
            row = c.execute('SELECT result FROM ops_requests WHERE key=?',(self.prefix+request_id,)).fetchone()
            if not row: raise Problem('批量提交记录不存在',404)
            result = json.loads(row['result'])
            ids = [j['job_id'] for j in result['jobs']]
            jobs = [dict(r) for r in c.execute('SELECT id AS job_id,product_id,status,message FROM jobs WHERE id IN ('+
                                              ','.join('?' for _ in ids)+') ORDER BY rowid',ids)]
        counts = {}
        for job in jobs: counts[job['status']] = counts.get(job['status'],0)+1
        return {**result,'jobs':jobs,'counts':counts,'missing':len(ids)-len(jobs)}

    def latest(self):
        with self.store.connect() as c:
            row = c.execute('SELECT key FROM ops_requests WHERE key LIKE ? ORDER BY rowid DESC LIMIT 1',(self.prefix+'%',)).fetchone()
        return self.status(row['key'][len(self.prefix):]) if row else None

    def cancel(self, request_id):
        result = self.status(request_id)
        ids = [j['job_id'] for j in result['jobs']]
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            rows = c.execute("SELECT id,product_id FROM jobs WHERE kind='submit' AND status='queued' AND id IN ("+
                             ','.join('?' for _ in ids)+')',ids).fetchall()
            for row in rows:
                c.execute("UPDATE jobs SET status='cancelled',message='已取消，未向平台提交本任务',updated_at=? WHERE id=?",(now(),row['id']))
                self.store.event(c,row['product_id'],'取消批量刊登','尚未执行的内容提交已取消')
        return {**self.status(request_id),'cancelled_now':len(rows)}

    def reconcile(self, body):
        """Record an operator's explicit Noon readback for an uncertain content write."""
        if not isinstance(body,dict) or set(body)!={'request_id','job_id','outcome','note','sku_parent','confirmed'}:
            raise Problem('提交回执核对资料无效')
        request_id=body['request_id'];job_id=body['job_id'];outcome=body['outcome']
        note=body['note'];parent=body['sku_parent']
        if not isinstance(request_id,str) or not request_id.strip() or len(request_id)>100:raise Problem('核对请求编号无效')
        if not isinstance(job_id,str) or not re.fullmatch(r'[a-f0-9]{32}',job_id):raise Problem('待核对提交任务编号无效')
        if outcome not in ('accepted','not_found'):raise Problem('请选择平台已接收或未找到')
        if body['confirmed'] is not True:raise Problem('请确认已按商品SKU回查noon记录')
        if not isinstance(note,str) or not note.strip() or len(note)>1000 or any(ord(ch)<32 and ch not in '\t\n\r' for ch in note):
            raise Problem('请填写1000字以内的回查依据')
        if outcome=='accepted':
            if not isinstance(parent,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',parent):raise Problem('平台已接收时请填写有效的平台商品编号')
        elif parent!='':raise Problem('平台未找到时不应填写平台商品编号')
        fingerprint=digest([job_id,outcome,note.strip(),parent])
        key='submit-reconcile:'+request_id
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT digest,result FROM ops_requests WHERE key=?',(key,)).fetchone()
            if old:
                if old['digest']!=fingerprint:raise Problem('核对请求编号已用于其他资料',409)
                return {**json.loads(old['result']),'replayed':True}
            row=c.execute('SELECT id,product_id,revision,status FROM jobs WHERE id=? AND kind=\'submit\'',(job_id,)).fetchone()
            if not row:raise Problem('noon提交任务不存在',404)
            if row['status'] not in ('uncertain','interrupted'):raise Problem('只有结果不明或中断的提交任务可以人工核对',409)
            product=c.execute('SELECT data FROM products WHERE id=?',(row['product_id'],)).fetchone()
            if not product:raise Problem('提交商品已不存在',409)
            data=json.loads(product['data']);platform=dict(data.get('platform') or {})
            previous_revision=platform.get('submitted_revision');previous_parent=platform.get('sku_parent')
            if type(previous_revision) is int and previous_revision>row['revision']:
                raise Problem('商品已有更新版本的提交回执，不能用旧任务覆盖',409)
            if type(previous_revision) is int and previous_revision==row['revision']:
                if outcome!='accepted' or previous_parent!=parent:
                    raise Problem('本地已有该版本的平台接收回执，请核对平台商品编号后选择一致的结果',409)
            checked_at=now()
            reconciliation={'outcome':outcome,'source':'operator-entered-noon-readback','job_id':job_id,
                            'job_revision':row['revision'],'sku_parent':parent or None,
                            'evidence':note.strip(),'checked_at':checked_at}
            platform['submit_reconciliation']=reconciliation
            if outcome=='accepted':
                platform.update(sku_parent=parent,submitted_revision=row['revision'],checked_at=checked_at,live_verified=False)
                status='needs_attention';message='人工回查确认平台已接收此版本；内容审核、价格、库存与可售状态仍需分别核对'
                action='人工核对noon提交已接收'
            else:
                status='failed';message='人工回查未找到此SKU的提交商品；已记录核对，可重新预检和安排提交'
                action='人工核对noon未找到提交商品'
            data['platform']=platform
            c.execute('UPDATE products SET data=?,updated_at=? WHERE id=?',(json.dumps(data,ensure_ascii=False),checked_at,row['product_id']))
            receipt={'job_id':job_id,'product_id':row['product_id'],'revision':row['revision'],'outcome':outcome,
                     'sku_parent':parent or None,'evidence':note.strip(),'checked_at':checked_at,'live_verified':False}
            c.execute('UPDATE jobs SET status=?,message=?,result=?,updated_at=? WHERE id=? AND status IN (\'uncertain\',\'interrupted\')',
                      (status,message,json.dumps({'reconciliation':receipt},ensure_ascii=False),checked_at,job_id))
            self.store.event(c,row['product_id'],action,
                             f"提交任务 {job_id} · 版本 {row['revision']} · {note.strip()[:800]}")
            result={'reconciliation':receipt,'job_status':status,'message':message,'replayed':False}
            c.execute('INSERT INTO ops_requests VALUES(?,?,?)',(key,fingerprint,json.dumps(result,ensure_ascii=False)))
        return result
