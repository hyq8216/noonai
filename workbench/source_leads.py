"""Durable URL candidates, separate from verified supplier product records."""
import hashlib
import json
import re
from core import Problem,normalize_source_url,now

SCHEMA_SQL='''CREATE TABLE IF NOT EXISTS source_leads(
  url TEXT PRIMARY KEY,created_at TEXT NOT NULL);'''
LIMIT=5000

class SourceLeads:
    def __init__(self,store):
        self.store=store
        with store.connect() as c:c.executescript(SCHEMA_SQL)

    def cataloged(self,c,urls):
        found=set()
        for start in range(0,len(urls),400):
            part=urls[start:start+400]
            if part:
                marks=','.join('?' for _ in part)
                found.update(row[0] for row in c.execute(
                    f"SELECT DISTINCT json_extract(data,'$.source_url') FROM products WHERE json_extract(data,'$.source_url') IN ({marks})",part))
        return found

    def preview(self,body,connection=None):
        raw=body.get('links')
        if not isinstance(raw,str) or not raw.strip():raise Problem('请粘贴不超过1MB的货源链接清单')
        try:size=len(raw.encode('utf-8'))
        except UnicodeEncodeError:raise Problem('货源链接清单含有无效Unicode字符，请检查后重试')
        if size>1024*1024:raise Problem('请粘贴不超过1MB的货源链接清单')
        lines=[line.strip() for line in raw.splitlines() if line.strip()]
        if len(lines)>LIMIT:raise Problem('每批最多5000条链接，请拆分导入')
        if connection is None:
            with self.store.connect() as c:return self.preview(body,c)
        c=connection;rows=[];seen=set();valid=[]
        for index,line in enumerate(lines,1):
            row={'line':index,'input':line[:240],'url':'','status':'invalid','reason':''}
            try:
                if len(line)>2048:raise Problem('链接超过2048字')
                url=normalize_source_url(line);row['url']=url
                if url in seen:row.update(status='duplicate',reason='本清单前面已有相同货源链接')
                else:seen.add(url);valid.append(url)
            except Problem as exc:row['reason']=str(exc)
            rows.append(row)
        existing=set()
        for start in range(0,len(valid),400):
            part=valid[start:start+400]
            marks=','.join('?' for _ in part)
            existing.update(row[0] for row in c.execute(f'SELECT url FROM source_leads WHERE url IN ({marks})',part))
        cataloged=self.cataloged(c,valid)
        for row in rows:
            if row['status']=='duplicate' or not row['url']:continue
            if row['url'] in cataloged:row.update(status='cataloged',reason='商品库已有此货源链接对应的商品')
            elif row['url'] in existing:row.update(status='queued',reason='候选池已有此链接')
            else:row.update(status='ready',reason='可加入候选池，等待补齐商品事实与授权素材')
        counts={key:sum(row['status']==key for row in rows) for key in ('ready','cataloged','queued','duplicate','invalid')}
        token=hashlib.sha256(json.dumps(rows,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        return {'rows':rows,'counts':counts,'token':token}

    def add(self,body):
        key=body.get('request_id')
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9-]{1,96}',key):raise Problem('请求编号无效')
        if body.get('confirmed') is not True:raise Problem('请确认只登记尚未存在的货源链接')
        request_key='source-leads:'+key
        try:fingerprint=hashlib.sha256(json.dumps([body.get('links'),body.get('preview_token')],ensure_ascii=False).encode('utf-8')).hexdigest()
        except UnicodeEncodeError:raise Problem('货源链接清单含有无效Unicode字符，请重新预览')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT digest,result FROM ops_requests WHERE key=?',(request_key,)).fetchone()
            if old:
                if old['digest']!=fingerprint:raise Problem('请求编号已用于另一批链接',409)
                return {**json.loads(old['result']),'replayed':True}
            preview=self.preview(body,c)
            if preview['token']!=body.get('preview_token'):raise Problem('候选池或商品库已变化，请重新预览',409)
            urls=[row['url'] for row in preview['rows'] if row['status']=='ready']
            if not urls:raise Problem('此清单没有新的货源链接')
            c.executemany('INSERT INTO source_leads(url,created_at) VALUES(?,?)',[(url,now()) for url in urls])
            result={'added':len(urls),'skipped':len(preview['rows'])-len(urls),'replayed':False}
            c.execute('INSERT INTO ops_requests VALUES(?,?,?)',(request_key,fingerprint,json.dumps(result)))
        return result

    def list(self,page=0):
        if isinstance(page,bool) or not str(page).isdecimal() or not 0<=int(page)<=100000:raise Problem('候选池页码无效')
        with self.store.connect() as c:
            total=c.execute('SELECT count(*) FROM source_leads').fetchone()[0]
            pages=max(1,(total+49)//50);index=min(int(page),pages-1)
            rows=[dict(row) for row in c.execute('SELECT url,created_at FROM source_leads ORDER BY created_at DESC,rowid DESC LIMIT 50 OFFSET ?',(index*50,))]
            found=self.cataloged(c,[row['url'] for row in rows])
        return {'rows':[{**row,'cataloged':row['url'] in found} for row in rows],
                'total':total,'page':index,'pages':pages}

    def export(self,page=0):
        if isinstance(page,bool) or not str(page).isdecimal() or not 0<=int(page)<=100000:raise Problem('导出页码无效')
        with self.store.connect() as c:
            c.execute('BEGIN')
            index=int(page)
            total=c.execute('SELECT count(*) FROM source_leads').fetchone()[0]
            urls=[row[0] for row in c.execute('SELECT url FROM source_leads ORDER BY created_at,rowid LIMIT 5000 OFFSET ?',(index*5000,))]
            found=self.cataloged(c,urls)
        return {'urls':[url for url in urls if url not in found],'page':index,'source_count':len(urls),
                'has_more':(index+1)*5000<total}
