"""Explicit batch queue for already approved noon content.

Preview and queue creation are local. Each SKU is submitted by the existing
single-product worker, so a partial batch can be inspected without replaying
successful or uncertain writes.
"""
import json

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
            if c.execute("SELECT 1 FROM jobs WHERE product_id=? AND kind='submit' AND revision=? AND status IN ('uncertain','needs_attention','interrupted')", (pid,p['revision'])).fetchone():
                reasons.append('此前提交结果待核对，不能直接重发当前版本')
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
