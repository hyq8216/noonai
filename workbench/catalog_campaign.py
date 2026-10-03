"""Explicit multi-run catalog processing with per-item preflight and one replay key."""
import json
import re

from automation import workflow_input
from core import Problem
from source_import import digest
from catalog_quality import preview as quality_preview

LIMIT=5000
CHUNK=500


class CatalogCampaign:
    def __init__(self,app):
        self.app=app
        self.store=app.store

    def latest(self):
        with self.store.connect() as c:
            saved=c.execute("SELECT key,result FROM ops_requests WHERE key LIKE 'catalog-campaign:%' ORDER BY rowid DESC LIMIT 1").fetchone()
            if not saved:return None
            return self.summary(c,saved)

    def history(self,page=0):
        try:index=int(page)
        except (TypeError,ValueError):raise Problem('铺货历史页码无效')
        if str(index)!=str(page) or not 0<=index<=100000:raise Problem('铺货历史页码无效')
        with self.store.connect() as c:
            total=c.execute("SELECT count(*) FROM ops_requests WHERE key LIKE 'catalog-campaign:%'").fetchone()[0]
            pages=max(1,(total+9)//10)
            index=min(index,pages-1)
            saved=c.execute("SELECT key,result FROM ops_requests WHERE key LIKE 'catalog-campaign:%' ORDER BY rowid DESC LIMIT 10 OFFSET ?",(index*10,)).fetchall()
            rows=[self.summary(c,row) for row in saved]
        return {'page':index,'pages':pages,'total':total,'rows':rows}

    def exceptions(self,request_id):
        if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9-]{1,96}',request_id):
            raise Problem('批次请求编号无效')
        with self.store.connect() as c:
            saved=c.execute('SELECT result FROM ops_requests WHERE key=?',('catalog-campaign:'+request_id,)).fetchone()
        if not saved:raise Problem('目录铺货批次不存在',404)
        result=json.loads(saved['result'])
        if 'exception_rows' not in result:raise Problem('此历史批次建立时未保存逐件异常清单，请重新预检当前商品',409)
        return {'name':result['name'],'created_snapshot':True,'rows':result['exception_rows']}

    @staticmethod
    def summary(c,saved):
        result=json.loads(saved['result']);refs=result['runs'];ids=[ref['id'] for ref in refs]
        runs_by_id={};counts_by_id={};counts={}
        if ids:
            marks=','.join('?' for _ in ids)
            runs_by_id={r['id']:r for r in c.execute(f'SELECT id,name,status,created_at FROM automation_runs WHERE id IN ({marks})',ids)}
            for row in c.execute(f'SELECT run_id,status,count(*) AS count FROM automation_items WHERE run_id IN ({marks}) GROUP BY run_id,status',ids):
                counts_by_id.setdefault(row['run_id'],{})[row['status']]=row['count']
                counts[row['status']]=counts.get(row['status'],0)+row['count']
        runs=[]
        for ref in refs:
            run=runs_by_id.get(ref['id'])
            if run:runs.append({**ref,'name':run['name'],'status':run['status'],'created_at':run['created_at'],
                                'item_counts':counts_by_id.get(ref['id'],{})})
        return {'name':result.get('name') or (runs[0]['name'].split(' · 第')[0] if runs else '目录铺货'),
                'request_id':saved['key'].removeprefix('catalog-campaign:'),'count':result['count'],
                'totals':result['totals'],'runs':runs,'item_counts':counts,
                'exceptions_saved':'exception_rows' in result,
                'created_at':runs[0]['created_at'] if runs else None}

    def input(self,body):
        ids=body.get('product_ids')
        if not isinstance(ids,list) or not 1<=len(ids)<=LIMIT or any(not isinstance(i,str) for i in ids) or len(set(ids))!=len(ids):
            raise Problem('每次请选择1至5000件不同商品')
        _,plan=workflow_input({'product_ids':ids[:CHUNK],'plan':body.get('plan')})
        return ids,plan

    def preview(self,body,connection=None,include_exceptions=True,include_quality=True):
        ids,plan=self.input(body)
        if connection is None:
            with self.store.connect() as c:
                c.execute('BEGIN')
                return self.preview(body,c,include_exceptions,include_quality)
        chunks=[]
        exceptions=[]
        totals={'ready':0,'blocked':0,'active':0,'calls':0,'image_calls':0,'image_checks':0}
        for start in range(0,len(ids),CHUNK):
            part=ids[start:start+CHUNK]
            pre=self.app.automation.preflight({'product_ids':part,'plan':plan},connection)
            summary={'index':len(chunks)+1,'count':len(part),'ready':len(pre['eligible_ids']),
                     'blocked':sum(r['status']=='blocked' for r in pre['rows']),
                     'active':sum(r['status']=='active' for r in pre['rows']),
                     'calls':pre['calls'],'image_calls':pre['image_calls'],'image_checks':pre['image_checks'],
                     'token':pre['token']}
            for key in totals:totals[key]+=summary[key]
            chunks.append(summary)
            if include_exceptions:
                for row in pre['rows']:
                    if row['status']!='ready':
                        exceptions.append({'id':row['id'],'sku':row['sku'],'title':row['title'],
                                           'status':row['status'],'reasons':row['reasons']})
        token=digest([ids,plan,[(c['index'],c['token']) for c in chunks]])
        return {'count':len(ids),'chunks':chunks,'totals':totals,'blocked_sample':exceptions[:100],
                'exception_rows':exceptions,
                'quality':quality_preview(self.store,ids,connection) if include_quality else None,'token':token}

    def apply(self,body):
        ids,plan=self.input(body)
        key=body.get('request_id')
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9-]{1,96}',key):raise Problem('批次请求编号无效')
        name=body.get('name')
        if not isinstance(name,str) or not 1<=len(name.strip())<=60:raise Problem('批次名称需为1至60字')
        if body.get('confirmed') is not True:raise Problem('请确认本次预览的调用量和可处理商品')
        operation='catalog-campaign:'+key
        fingerprint=digest([ids,plan,name,body.get('preview_token')])
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT digest,result FROM ops_requests WHERE key=?',(operation,)).fetchone()
            if old:
                if old['digest']!=fingerprint:raise Problem('批次请求编号已用于不同内容',409)
                result=json.loads(old['result']);result.pop('exception_rows',None)
                return {**result,'replayed':True}
            preview=self.preview({'product_ids':ids,'plan':plan},c,include_quality=False)
            if preview['token']!=body.get('preview_token'):raise Problem('商品、素材或任务已变化，请重新预览再安排',409)
            if not preview['totals']['ready']:raise Problem('当前筛选结果没有可处理商品')
            runs=[]
            for start,chunk in zip(range(0,len(ids),CHUNK),preview['chunks']):
                if not chunk['ready']:continue
                request={'product_ids':ids[start:start+CHUNK],'plan':plan}
                result=self.app.automation.create({**request,'name':name.strip()+f' · 第{chunk["index"]}批',
                                                   'request_id':key+'-'+str(chunk['index']),
                                                   'preflight_token':chunk['token']},connection=c)
                runs.append({'id':result['id'],'index':chunk['index'],'ready':chunk['ready']})
            result={'name':name.strip(),'runs':runs,'count':preview['count'],'totals':preview['totals'],
                    'skipped':preview['totals']['blocked']+preview['totals']['active'],'replayed':False}
            c.execute('INSERT INTO ops_requests VALUES(?,?,?)',(operation,fingerprint,json.dumps({**result,'exception_rows':preview['exception_rows']},ensure_ascii=False)))
            return result
