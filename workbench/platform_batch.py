"""Explicit, idempotent batch reads of previously submitted noon content."""
import hashlib,json
from core import Problem,ident,now

def digest(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def selection(b):
    ids=b.get('product_ids')
    if not isinstance(ids,list) or not 1<=len(ids)<=500 or any(not isinstance(i,str) or not i for i in ids) or len(ids)!=len(set(ids)):raise Problem('请选择1至500个不同商品')
    try:
        for value in ids:value.encode('utf-8')
    except UnicodeEncodeError:raise Problem('商品编号含有无效Unicode字符，请重新选择')
    return sorted(ids)

class PlatformBatch:
    def __init__(self,app,kind='refresh'):
        if kind not in ('refresh','offers','prices','transfer'):raise Problem('回查类型无效')
        self.app=app;self.store=app.store;self.kind=kind;self.prefix={'refresh':'platform-batch:','offers':'offer-batch:','prices':'price-batch:','transfer':'transfer-batch:'}[kind]
    def preview(self,b,connection=None):
        ids=selection(b)
        if connection is None:
            with self.store.connect() as c:
                c.execute('BEGIN');return self.preview(b,c)
        c=connection;ready=self.app.config()['noon_ready'];rows=[]
        queued=c.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
        for pid in ids:
            row={'id':pid,'title':'商品不存在','sku':'','status':'blocked','reason':'商品已不存在'}
            saved=c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone()
            if saved:
                p=self.store.unpack(saved);parent=(p.get('platform') or {}).get('sku_parent')
                row.update(title=p['title_zh'],sku=p['partner_sku'],revision=p['revision'],parent=parent)
                if p['demo']:reason='示例商品不能回查平台'
                elif self.kind=='prices' and p.get('mode')!='LOCAL':reason='仅适用于已确认的本地销售模式；NGS美元转移价不能当作沙特售价'
                elif self.kind=='transfer' and p.get('mode')!='NGS':reason='仅适用于已确认的NGS模式；本地SAR售价走另一条流程'
                elif not ready:reason='noon店铺尚未接入'
                elif self.kind=='refresh' and (not isinstance(parent,str) or not parent.strip()):reason='尚无平台商品编号'
                elif c.execute("SELECT 1 FROM jobs WHERE product_id=? AND status IN ('queued','running')",(pid,)).fetchone():reason='商品已有处理任务，请等待完成'
                else:reason=''
                row.update(status='blocked' if reason else 'ready',reason=reason or {'offers':'可读取报价、净库存与前台状态','prices':'可只读回查沙特本地售价（SAR）与启用意图','transfer':'可批量读取NGS美元转移价与启用意图','refresh':'可读取平台内容状态'}[self.kind])
            rows.append(row)
        count=sum(r['status']=='ready' for r in rows)
        return {'rows':rows,'ready':count,'blocked':len(rows)-count,'queued':queued,'capacity_ok':queued+count<=1000,'token':digest([rows,queued,ready,self.kind]),'max_content_reads':1 if self.kind=='transfer' and count else count}
    def apply(self,b):
        ids=selection(b);key=b.get('request_id')
        if not isinstance(key,str) or not key.strip() or len(key)>100:raise Problem('回查请求编号无效')
        if b.get('confirmed') is not True:raise Problem('请确认仅回查预览中可用商品')
        fingerprint=digest([ids,b.get('preview_token')]);key=self.prefix+key;dispatch=[]
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE');old=c.execute('SELECT * FROM ops_requests WHERE key=?',(key,)).fetchone()
            if old:
                if old['digest']!=fingerprint:raise Problem('同一请求编号不能用于不同回查批次',409)
                return {**json.loads(old['result']),'replayed':True}
            pre=self.preview(b,c)
            if pre['token']!=b.get('preview_token'):raise Problem('商品、店铺配置或任务队列已变化，请重新预检',409)
            if not pre['ready']:raise Problem('没有可以回查的商品',409)
            if not pre['capacity_ok']:raise Problem('待执行任务超过1000项，请等待队列完成后重新预检',409)
            for row in pre['rows']:
                if row['status']!='ready':continue
                p=self.store.unpack(c.execute('SELECT * FROM products WHERE id=?',(row['id'],)).fetchone());jid=ident();ts=now()
                c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',(jid,p['id'],self.kind,p['revision'],'queued','批量回查等待处理',None,ts,ts))
                self.store.event(c,p['id'],'批量回查排队',{'offers':'只读取报价与前台状态，不修改价格库存','prices':'只读取沙特本地售价与启用意图，不修改平台价格','transfer':'批量读取NGS美元转移价，不修改平台报价','refresh':'只读取noon内容状态，不提交或修改平台商品'}[self.kind])
                dispatch.append((jid,p))
            result={'kind':self.kind,'request_id':b['request_id'],'jobs':[{'job_id':jid,'product_id':p['id']} for jid,p in dispatch],'skipped':[r for r in pre['rows'] if r['status']!='ready'],'replayed':False}
            c.execute('INSERT INTO ops_requests VALUES(?,?,?)',(key,fingerprint,json.dumps(result,ensure_ascii=False)))
        if self.kind=='transfer':
            try:self.app.executor.submit(self.app.run_transfer_batch,dispatch)
            except RuntimeError:
                with self.store.connect() as c:
                    for jid,_ in dispatch:c.execute("UPDATE jobs SET status='failed',message='应用正在关闭，回查尚未发送，请重开后重新安排',updated_at=? WHERE id=? AND status='queued'",(now(),jid))
        else:
            for jid,p in dispatch:
                try:self.app.executor.submit(self.app.run,jid,p,self.kind)
                except RuntimeError:
                    with self.store.connect() as c:c.execute("UPDATE jobs SET status='failed',message='应用正在关闭，回查尚未发送，请重开后重新安排',updated_at=? WHERE id=? AND status='queued'",(now(),jid))
        return result
    def status(self,key):
        if not isinstance(key,str) or not key.strip() or len(key)>100:raise Problem('回查请求编号无效')
        with self.store.connect() as c:
            row=c.execute('SELECT result FROM ops_requests WHERE key=?',(self.prefix+key,)).fetchone()
            if not row:raise Problem('回查批次不存在',404)
            result=json.loads(row['result']);ids=[j['job_id'] for j in result['jobs']]
            jobs=[dict(r) for r in c.execute('SELECT id AS job_id,product_id,status,message FROM jobs WHERE id IN ('+','.join('?' for _ in ids)+') ORDER BY rowid',ids)]
        counts={}
        for j in jobs:counts[j['status']]=counts.get(j['status'],0)+1
        return {**result,'jobs':jobs,'counts':counts,'missing':len(ids)-len(jobs)}
    def latest(self):
        with self.store.connect() as c:row=c.execute("SELECT key FROM ops_requests WHERE key LIKE 'platform-batch:%' OR key LIKE 'offer-batch:%' OR key LIKE 'price-batch:%' OR key LIKE 'transfer-batch:%' ORDER BY rowid DESC LIMIT 1").fetchone()
        if not row:return None
        kind='offers' if row['key'].startswith('offer-batch:') else 'prices' if row['key'].startswith('price-batch:') else 'transfer' if row['key'].startswith('transfer-batch:') else 'refresh'
        return PlatformBatch(self.app,kind).status(row['key'].split(':',1)[1])
    def cancel(self,key):
        if not isinstance(key,str) or not key.strip() or len(key)>100:raise Problem('回查请求编号无效')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT result FROM ops_requests WHERE key=?',(self.prefix+key,)).fetchone()
            if not row:raise Problem('回查批次不存在',404)
            ids=[j['job_id'] for j in json.loads(row['result'])['jobs']]
            rows=c.execute("SELECT id,product_id FROM jobs WHERE kind IN ('refresh','offers','prices','transfer') AND status='queued' AND id IN ("+','.join('?' for _ in ids)+')',ids).fetchall()
            for job in rows:
                c.execute("UPDATE jobs SET status='cancelled',message='已停止本批尚未执行的回查；未向平台发送本任务',updated_at=? WHERE id=?",(now(),job['id']))
                self.store.event(c,job['product_id'],'取消回查','尚未执行的批量回查已取消')
        return {**self.status(key),'cancelled_now':len(rows)}
