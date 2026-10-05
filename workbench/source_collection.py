"""Durable read-only channel collection, followed by explicit candidate import."""
import json
import re
from core import Problem, clean, ident, now
from source_import import digest

COLLECTION_SCHEMA_SQL = '''
CREATE TABLE IF NOT EXISTS source_collection_runs(
 id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL, digest TEXT NOT NULL,
 account_id TEXT NOT NULL, account_revision INTEGER NOT NULL, provider TEXT NOT NULL,
 query TEXT NOT NULL, page_limit INTEGER NOT NULL, cursor TEXT, status TEXT NOT NULL,
 pages INTEGER NOT NULL DEFAULT 0, collected INTEGER NOT NULL DEFAULT 0,
 seen_cursors TEXT NOT NULL DEFAULT '[]', warnings TEXT NOT NULL DEFAULT '[]',
 message TEXT NOT NULL DEFAULT '', intent TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source_collection_candidates(
 id TEXT PRIMARY KEY, identity TEXT UNIQUE NOT NULL, provider TEXT NOT NULL,
 account_id TEXT NOT NULL, external_id TEXT NOT NULL, run_id TEXT NOT NULL,
 raw TEXT NOT NULL, normalized TEXT NOT NULL, snapshots TEXT NOT NULL,
 status TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, product_id TEXT,
 message TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS source_collection_candidates_updated ON source_collection_candidates(updated_at DESC);
CREATE INDEX IF NOT EXISTS source_collection_candidates_source ON source_collection_candidates(json_extract(normalized,'$.source_url'),json_extract(normalized,'$.source_sku'));
CREATE TABLE IF NOT EXISTS source_collection_requests(key TEXT PRIMARY KEY,digest TEXT NOT NULL,result TEXT NOT NULL);
'''


def request_id(body):
    value=body.get('request_id')
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,96}',value):raise Problem('请求编号无效')
    return value


def page_number(value):
    if isinstance(value,bool) or not str(value).isdecimal() or int(value)>100000:raise Problem('页码无效')
    return int(value)


class SourceCollection:
    def __init__(self,app):
        self.app=app;self.store=app.store
        with self.store.connect() as c:
            c.executescript(COLLECTION_SCHEMA_SQL)
            c.execute("UPDATE source_collection_runs SET status='attention',message='采集被中断，请显式重试只读页面',updated_at=? WHERE status IN ('queued','running')",(now(),))

    def unpack(self,row):
        result=dict(row)
        for key in ('raw','normalized','snapshots','seen_cursors','warnings','intent'):
            if key in result and result[key] is not None:result[key]=json.loads(result[key])
        return result

    def get_run(self,c,run_id):
        row=c.execute('SELECT * FROM source_collection_runs WHERE id=?',(run_id,)).fetchone()
        if not row:raise Problem('采集任务不存在',404)
        return self.unpack(row)

    def account(self,account_id,c=None,secret=False):
        account=self.app.channel_accounts.get(account_id,connection=c,with_secret=secret)
        if not account['enabled']:raise Problem('来源账号已停用',409)
        return account

    def state(self,page=0,run_page=0):
        page=page_number(page);run_page=page_number(run_page)
        with self.store.connect() as c:
            total=c.execute('SELECT count(*) FROM source_collection_candidates').fetchone()[0]
            run_total=c.execute('SELECT count(*) FROM source_collection_runs').fetchone()[0]
            pages=max(1,(total+49)//50);run_pages=max(1,(run_total+9)//10)
            page=min(page,pages-1);run_page=min(run_page,run_pages-1)
            candidates=[self.unpack(r) for r in c.execute('SELECT * FROM source_collection_candidates ORDER BY updated_at DESC,id LIMIT 50 OFFSET ?',(page*50,))]
            runs=[self.unpack(r) for r in c.execute('SELECT * FROM source_collection_runs ORDER BY created_at DESC,id LIMIT 10 OFFSET ?',(run_page*10,))]
        return dict(candidates=candidates,rows=candidates,runs=runs,total=total,page=page,pages=pages,run_total=run_total,run_page=run_page,run_pages=run_pages)

    def create(self,body):
        key=request_id(body)
        if body.get('confirmed') is not True:raise Problem('请确认通过已配置来源账号进行只读采集')
        query=body.get('query','');limit=body.get('page_limit',1);aid=body.get('account_id')
        if not isinstance(query,str) or len(query)>500 or type(limit) is not int or not 1<=limit<=20 or not isinstance(aid,str):raise Problem('采集参数无效')
        fingerprint=digest([aid,query,limit])
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT * FROM source_collection_runs WHERE request_key=?',(key,)).fetchone()
            if old:
                if old['digest']!=fingerprint:raise Problem('请求编号已用于不同采集任务',409)
                return {**self.unpack(old),'replayed':True}
            account=self.account(aid,c)
            if account['provider'] not in ('shopify','ebay','custom_json','json_api'):raise Problem('此渠道尚未开放在线采集，请使用供应商文件导入',409)
            if c.execute("SELECT count(*) FROM source_collection_runs WHERE status IN ('queued','running')").fetchone()[0]>=100:raise Problem('待采集任务已达100个，请先处理现有任务',409)
            self.ensure_recovery_clear()
            rid=ident();ts=now()
            c.execute('INSERT INTO source_collection_runs(id,request_key,digest,account_id,account_revision,provider,query,page_limit,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',(rid,key,fingerprint,aid,account['revision'],account['provider'],query,limit,'queued',ts,ts))
            return {**self.get_run(c,rid),'replayed':False}

    def control(self,body):
        action=body.get('action')
        if action not in ('cancel','retry'):raise Problem('采集操作无效')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE');run=self.get_run(c,body.get('run_id'))
            if action=='retry':self.ensure_recovery_clear()
            if action=='cancel':
                if run['status'] not in ('completed','cancelled'):c.execute("UPDATE source_collection_runs SET status='cancelled',message='已取消',updated_at=? WHERE id=?",(now(),run['id']))
            else:
                if run['status'] not in ('attention','failed','cancelled'):raise Problem('只有中断或失败任务可重试',409)
                account=self.account(run['account_id'],c)
                if account['revision']!=run['account_revision']:raise Problem('来源账号已变化，请建立新采集任务',409)
                c.execute("UPDATE source_collection_runs SET status='queued',message='',intent=NULL,updated_at=? WHERE id=?",(now(),run['id']))
            return self.get_run(c,run['id'])

    def normalize(self,item,preserve_missing_sku=False):
        if not isinstance(item,dict):raise Problem('来源记录格式无效')
        external=item.get('external_id')
        if not isinstance(external,str) or not external.strip() or len(external)>500:raise Problem('来源记录缺少稳定规格编号')
        raw={k:item[k] for k in ('external_id','external_product_id','source_title','title_zh','source_url','source_sku','supplier','brand','facts','stock','cost_cny','source_currency','source_price','images') if k in item}
        if any(raw.get(k) is not None and not isinstance(raw[k],str) for k in ('external_product_id','source_title','title_zh','source_url','source_sku','supplier','brand','facts','source_currency')):raise Problem('来源文本字段格式无效')
        images=raw.get('images',[])
        if not isinstance(images,list) or len(images)>50 or any(not isinstance(url,str) or len(url)>4096 for url in images):raise Problem('来源图片引用格式无效')
        if len(json.dumps(raw,ensure_ascii=False).encode())>128*1024:raise Problem('来源记录过大')
        data={k:raw[k] for k in ('title_zh','source_url','source_sku','supplier','brand','facts','stock') if k in raw}
        if raw.get('source_currency')=='CNY' and raw.get('cost_cny') is not None:data['cost_cny']=raw['cost_cny']
        if not data.get('source_sku') and not preserve_missing_sku:data['source_sku']=external
        normalized=clean(data)
        if not normalized['source_url'] or (not normalized['source_sku'] and not preserve_missing_sku):raise Problem('来源记录缺少货源链接或规格货号')
        return external,raw,normalized

    def save_item(self,c,run,item):
        external,raw,data=self.normalize(item);identity=digest([run['provider'],run['account_id'],external]);ts=now()
        snapshot={'run_id':run['id'],'account_revision':run['account_revision'],'captured_at':ts,'raw':raw,'normalized':data}
        old=c.execute('SELECT * FROM source_collection_candidates WHERE identity=?',(identity,)).fetchone()
        if old:
            previous=json.loads(old['normalized']);changed=previous!=data or json.loads(old['raw'])!=raw
            snapshots=json.loads(old['snapshots'])
            # Keep the original observation and latest distinct observation without unbounded history.
            observation_changed=changed or json.loads(old['raw'])!=raw or snapshots[-1]['run_id']!=run['id']
            if observation_changed:snapshots=(snapshots[:1]+[snapshot])[-2:]
            status='conflict' if changed else old['status']
            message='同一来源规格存在不同事实，需人工核对' if changed else old['message']
            c.execute('UPDATE source_collection_candidates SET raw=?,snapshots=?,status=?,message=?,revision=revision+?,updated_at=? WHERE id=?',(json.dumps(raw,ensure_ascii=False),json.dumps(snapshots,ensure_ascii=False),status,message,int(observation_changed),ts,old['id']))
            return
        c.execute('INSERT INTO source_collection_candidates(id,identity,provider,account_id,external_id,run_id,raw,normalized,snapshots,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(ident(),identity,run['provider'],run['account_id'],external,run['id'],json.dumps(raw,ensure_ascii=False),json.dumps(data,ensure_ascii=False),json.dumps([snapshot],ensure_ascii=False),'ready',ts,ts))

    def ensure_recovery_clear(self):
        recovery=getattr(self.app,'recovery',None)
        if recovery is not None and recovery.pending.exists():raise Problem('恢复待执行，采集已暂停',409)

    def run(self,run_id):
        import channel_adapters
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE');run=self.get_run(c,run_id)
            if run['status']!='queued':return run
            try:self.ensure_recovery_clear()
            except Problem:
                c.execute("UPDATE source_collection_runs SET status='attention',message='恢复待执行，采集已暂停' WHERE id=?",(run_id,))
                return self.get_run(c,run_id)
            c.execute("UPDATE source_collection_runs SET status='running',updated_at=? WHERE id=?",(now(),run_id))
        for _ in range(run['page_limit']):
            try:
                with self.store.connect() as c:
                    c.execute('BEGIN IMMEDIATE');run=self.get_run(c,run_id)
                    if run['status']!='running':return run
                    self.ensure_recovery_clear()
                    account=self.account(run['account_id'],c,True)
                    if account['revision']!=run['account_revision']:raise Problem('来源账号已变化，请建立新采集任务',409)
                    if run['collected']>=5000:raise Problem('每个任务最多采集5000条，请缩小查询范围')
                    c.execute('UPDATE source_collection_runs SET intent=?,updated_at=? WHERE id=?',(json.dumps({'cursor':run['cursor'],'page':run['pages']+1}),now(),run_id))
                # Read-only network operation intentionally runs outside any SQL transaction.
                fetch_limit=min(50,5000-run['collected'])
                page=channel_adapters.fetch_page(account,cursor=run['cursor'],query=run['query'],limit=fetch_limit)
                if not isinstance(page,dict) or not isinstance(page.get('items'),list) or len(page['items'])>fetch_limit:raise Problem('来源分页响应格式无效')
                cursor=page.get('next_cursor');warnings=page.get('warnings',[])
                if cursor is not None and (not isinstance(cursor,str) or not cursor or len(cursor)>2048):raise Problem('来源分页游标无效')
                if not isinstance(warnings,list) or any(not isinstance(w,str) for w in warnings):raise Problem('来源警告格式无效')
                with self.store.connect() as c:
                    c.execute('BEGIN IMMEDIATE');live=self.get_run(c,run_id)
                    if live['status']!='running':return live
                    self.ensure_recovery_clear()
                    account=self.account(run['account_id'],c)
                    if account['revision']!=run['account_revision']:raise Problem('采集期间来源账号已变化，当前页未保存',409)
                    errors=[]
                    for item in page['items']:
                        try:self.save_item(c,run,item)
                        except Problem:errors.append('一条来源记录无效，已跳过')
                    seen=run['seen_cursors'];repeated=cursor is not None and cursor in seen
                    if run['cursor'] is not None and run['cursor'] not in seen:seen.append(run['cursor'])
                    if cursor is not None and cursor==run['cursor']:repeated=True
                    count=run['collected']+len(page['items']);status='attention' if repeated or count>=5000 and cursor is not None else ('completed' if cursor is None else 'running')
                    message='来源游标重复或达到5000条上限，请缩小查询范围' if status=='attention' else ''
                    c.execute('UPDATE source_collection_runs SET cursor=?,status=?,pages=pages+1,collected=?,seen_cursors=?,warnings=?,message=?,intent=NULL,updated_at=? WHERE id=?',(cursor,status,count,json.dumps(seen),json.dumps((run['warnings']+warnings+errors)[-100:],ensure_ascii=False),message,now(),run_id))
                    run=self.get_run(c,run_id)
                if run['status']!='running':return run
            except Exception:
                # Never persist adapter exception strings: these can contain credentials or URLs.
                with self.store.connect() as c:
                    c.execute("UPDATE source_collection_runs SET status='attention',message='来源采集失败或账号变化；已保留之前页面，请核对后重试',updated_at=? WHERE id=? AND status='running'",(now(),run_id))
                    return self.get_run(c,run_id)
        with self.store.connect() as c:
            c.execute("UPDATE source_collection_runs SET status='attention',message='本次页数上限已达到，已保存游标，可明确继续采集',intent=NULL,updated_at=? WHERE id=? AND status='running'",(now(),run_id))
            return self.get_run(c,run_id)

    def candidate_ids(self,body):
        ids=body.get('candidate_ids')
        if not isinstance(ids,list) or not 1<=len(ids)<=500 or any(not isinstance(i,str) or len(i)>96 for i in ids) or len(set(ids))!=len(ids):raise Problem('请选择1至500个不同候选商品')
        return ids

    def preview(self,body,connection=None):
        ids=self.candidate_ids(body)
        if connection is None:
            with self.store.connect() as c:return self.preview(body,c)
        c=connection;rows=[];prepared=[];eligible=[];groups={}
        for cid in ids:
            candidate=c.execute('SELECT * FROM source_collection_candidates WHERE id=?',(cid,)).fetchone()
            row={'id':cid,'status':'blocked','reason':'候选记录不存在'}
            if candidate:
                candidate=self.unpack(candidate);p=candidate['normalized'];row.update(title=p['title_zh'],revision=candidate['revision'],status=candidate['status'],reason=candidate['message'],account_id=candidate['account_id'],product_id=candidate['product_id'])
                if candidate['status']=='ready':
                    key=p['source_url']+'|'+p['source_sku'];old=c.execute('SELECT id,revision,data FROM products WHERE source_key=?',(key,)).fetchone()
                    if old:row.update(status='duplicate',reason='商品库已有同一来源规格，请人工核对来源归属',product_id=old['id'],product_revision=old['revision'],product_digest=digest(json.loads(old['data'])))
                    else:row.update(status='ready',reason='可导入待补充商品');groups.setdefault(key,[]).append(row)
                elif candidate['status']=='imported':row['reason']='已导入，保留现有商品'
                else:row['status']='blocked'
                row['values']=p
                row['snapshot_digest']=digest(candidate['snapshots'])
                if row['status']=='ready':
                    peers=c.execute("SELECT count(*) FROM source_collection_candidates WHERE json_extract(normalized,'$.source_url')=? AND json_extract(normalized,'$.source_sku')=? AND id!=?",(p['source_url'],p['source_sku'],cid)).fetchone()[0]
                    if peers:row.update(status='blocked',reason='候选池中其他来源占用相同货源规格，请核对账号和事实')
            rows.append(row)
        for group in groups.values():
            if len(group)>1:
                for row in group:row.update(status='blocked',reason='不同候选占用相同货源规格，请人工核对账号和事实')
        for row in rows:
            if row['status']=='ready':eligible.append(row['id']);prepared.append(row['values'])
        counts={status:sum(r['status']==status for r in rows) for status in ('ready','blocked','duplicate','imported')}
        return {'token':digest(rows),'rows':rows,'eligible_ids':eligible,'counts':counts,'ready':counts['ready'],'prepared':prepared}

    def resolve(self,body):
        if body.get('confirmed') is not True:raise Problem('请确认采用已保存的来源事实，现有商品不会被覆盖')
        cid=body.get('candidate_id');revision=body.get('revision');index=body.get('snapshot_index');note=body.get('note')
        if (not isinstance(cid,str) or len(cid)>96 or type(revision) is not int or revision<1
                or type(index) is not int or index not in (0,1) or not isinstance(note,str)
                or not note.strip() or len(note)>1000):raise Problem('请选择事实快照并填写1000字以内的核对说明')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE');self.ensure_recovery_clear()
            row=c.execute('SELECT * FROM source_collection_candidates WHERE id=?',(cid,)).fetchone()
            if not row:raise Problem('候选记录不存在',404)
            candidate=self.unpack(row)
            if candidate['revision']!=revision:raise Problem('候选事实已变化，请重新查看快照',409)
            if candidate['status']!='conflict':raise Problem('此候选没有待核对的事实冲突',409)
            if index>=len(candidate['snapshots']):raise Problem('事实快照不存在，请重新查看',409)
            snapshot=candidate['snapshots'][index]
            external,raw,data=self.normalize(snapshot['raw'],preserve_missing_sku=candidate['provider'] in ('1688','taobao','pinduoduo'))
            if external!=candidate['external_id'] or data!=snapshot['normalized']:raise Problem('事实快照与来源身份不一致，请重新核对',409)
            status='blocked' if not data.get('source_sku') else 'imported' if candidate['product_id'] else 'ready';ts=now()
            c.execute('UPDATE source_collection_candidates SET raw=?,normalized=?,status=?,message=?,revision=revision+1,updated_at=? WHERE id=?',
                      (json.dumps(raw,ensure_ascii=False),json.dumps(data,ensure_ascii=False),status,'人工核对：'+note.strip(),ts,cid))
            detail={'candidate_id':cid,'account_id':candidate['account_id'],'external_id':external,
                    'previous_revision':revision,'snapshot_index':index,'snapshot_digest':digest(snapshot),
                    'snapshots':candidate['snapshots'],
                    'note':note.strip(),'product_id':candidate['product_id'],'product_unchanged':True}
            self.store.event(c,candidate['product_id'],'采集事实核对',json.dumps(detail,ensure_ascii=False))
            return self.unpack(c.execute('SELECT * FROM source_collection_candidates WHERE id=?',(cid,)).fetchone())

    def apply(self,body):
        key='source-collection-import:'+request_id(body)
        if body.get('confirmed') is not True:raise Problem('请确认只导入已预览的候选商品')
        fingerprint=digest([self.candidate_ids(body),body.get('preview_token')])
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE');old=c.execute('SELECT * FROM source_collection_requests WHERE key=?',(key,)).fetchone()
            if old:
                if old['digest']!=fingerprint:raise Problem('请求编号已用于另一批候选商品',409)
                return {**json.loads(old['result']),'replayed':True}
            self.ensure_recovery_clear()
            pre=self.preview(body,c)
            if pre['token']!=body.get('preview_token'):raise Problem('候选事实或商品库已变化，请重新预览',409)
            if not pre['eligible_ids']:raise Problem('没有可导入候选商品')
            result=self.store.import_rows(pre['prepared'],connection=c)
            if len(result['created'])!=len(pre['eligible_ids']):raise Problem('导入结果变化，请重新预览',409)
            for cid,pid in zip(pre['eligible_ids'],result['created']):
                candidate=self.unpack(c.execute('SELECT * FROM source_collection_candidates WHERE id=?',(cid,)).fetchone())
                provenance={k:candidate[k] for k in ('id','provider','account_id','external_id','run_id','snapshots')}
                row=c.execute('SELECT data FROM products WHERE id=?',(pid,)).fetchone();data=json.loads(row['data']);data['source_collection']=provenance
                serialized=json.dumps(data,ensure_ascii=False)
                c.execute('UPDATE products SET data=? WHERE id=?',(serialized,pid));c.execute('UPDATE revisions SET data=? WHERE product_id=? AND revision=1',(serialized,pid))
                c.execute("UPDATE source_collection_candidates SET status='imported',product_id=?,revision=revision+1,updated_at=? WHERE id=?",(pid,now(),cid))
            result.update(replayed=False,imported_candidates=pre['eligible_ids'],skipped=[r for r in pre['rows'] if r['status']!='ready'])
            c.execute('INSERT INTO source_collection_requests VALUES(?,?,?)',(key,fingerprint,json.dumps(result,ensure_ascii=False)))
            return result
