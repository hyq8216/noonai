"""Confirmed local returns, quarantined stock and financial obligations.

No marketplace refund or payment is sent. Operations remains the return counter
of record; Finance remains the expense ledger, including manually entered refunds.
"""
import csv
import io
import json
import re
from decimal import Decimal
from core import Problem, ident, now
from operations import text, qty, cents
from finance import fingerprint, rate, day, converted

SCHEMA_SQL = '''
CREATE TABLE IF NOT EXISTS after_sales_cases(id TEXT PRIMARY KEY, order_id TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL, revision INTEGER NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS after_sales_cases_order_status ON after_sales_cases(order_id,status);
CREATE INDEX IF NOT EXISTS after_sales_cases_updated ON after_sales_cases(updated_at DESC,id);
CREATE TABLE IF NOT EXISTS after_sales_quarantine(case_id TEXT NOT NULL, product_id TEXT NOT NULL, warehouse_id TEXT NOT NULL, received INTEGER NOT NULL CHECK(received>0), released INTEGER NOT NULL DEFAULT 0 CHECK(released>=0), discarded INTEGER NOT NULL DEFAULT 0 CHECK(discarded>=0 AND released+discarded<=received), PRIMARY KEY(case_id,product_id));
CREATE TABLE IF NOT EXISTS after_sales_requests(key TEXT PRIMARY KEY,digest TEXT NOT NULL,result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS after_sales_audit(id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT NOT NULL, action TEXT NOT NULL, revision INTEGER NOT NULL, data TEXT NOT NULL, created_at TEXT NOT NULL);
'''


def page_number(value):
    if isinstance(value,bool) or not str(value).isdecimal() or int(value)>100000:raise Problem('页码无效')
    return int(value)


class AfterSales:
    def __init__(self,app):
        self.app=app;self.store=app.store;self.ops=app.ops;self.finance=app.finance
        with self.store.connect() as c:c.executescript(SCHEMA_SQL)

    def order(self,c,oid,revision=None):
        row=c.execute("SELECT data FROM ops_documents WHERE id=? AND kind='order'",(text(oid,'订单编号',100),)).fetchone()
        if not row:raise Problem('订单不存在',404)
        order=json.loads(row['data'])
        if revision is not None and (type(revision) is not int or revision!=order['revision']):raise Problem('订单版本已变化，请刷新并重新预览',409)
        if order['status'] not in ('shipped','delivered'):raise Problem('仅已发货或签收订单可登记售后',409)
        return order

    def case(self,c,body):
        row=c.execute('SELECT data FROM after_sales_cases WHERE id=?',(text(body.get('id'),'售后编号',100),)).fetchone()
        if not row:raise Problem('售后单不存在',404)
        case=json.loads(row['data'])
        if type(body.get('revision')) is not int or body['revision']!=case['revision']:raise Problem('售后版本已变化，请刷新',409)
        return case

    def held(self,c,oid,exclude=None):
        held={}
        for row in c.execute("SELECT id,data FROM after_sales_cases WHERE order_id=? AND status='registered'",(oid,)):
            if row['id']==exclude:continue
            for pid,n in json.loads(row['data'])['quantities'].items():held[pid]=held.get(pid,0)+n
        return held

    def quarantine(self,c,cid):
        return [dict(r) for r in c.execute('SELECT *,received-released-discarded remaining FROM after_sales_quarantine WHERE case_id=? ORDER BY product_id',(cid,))]

    def money(self,c,order):
        # Refunds may have been entered directly in Finance or allocated from a batch.
        # Include every active allocated refund, rather than only this module's entries.
        refund=compensation=0;foreign=[];entries=[]
        relevant=c.execute("SELECT e.data FROM finance_entries e WHERE json_extract(e.data,'$.status')='active' AND (json_extract(e.data,'$.document_id')=? OR EXISTS (SELECT 1 FROM json_each(e.data,'$.allocations') allocation WHERE json_extract(allocation.value,'$.order_id')=?)) AND (json_extract(e.data,'$.category')='refund' OR e.id IN (SELECT json_extract(j.value,'$.entry_id') FROM after_sales_cases a,json_each(a.data,'$.financial_entries') j WHERE a.order_id=?))",(order['id'],order['id'],order['id']))
        for row in relevant:
            entry=json.loads(row['data'])
            amount=sum(a['amount_cents'] for a in entry.get('allocations',[]) if a['order_id']==order['id'])
            if not amount and entry.get('document_id')==order['id']:amount=entry['amount_cents']
            if not amount:continue
            entries.append({'id':entry['id'],'category':entry['category'],'currency':entry['currency'],'amount_cents':amount,'revision':entry['revision']})
            if entry['currency']!=order['currency']:foreign.append(entry['id']);continue
            if entry['category']=='refund':refund+=amount
            else:compensation+=amount
        return {'currency':order['currency'],'refund_cents':refund,'compensation_cents':compensation,'combined_cents':refund+compensation,'limit_cents':order['total_cents'],'remaining_cents':max(0,order['total_cents']-refund-compensation),'unreconciled_currency_entries':foreign,'entries':entries}

    def quantities(self,raw,allowed):
        if not isinstance(raw,dict) or not 1<=len(raw)<=100 or set(raw)-set(allowed):raise Problem('数量商品必须属于此订单或隔离库存')
        values={pid:qty(n,True) for pid,n in raw.items()}
        values={pid:n for pid,n in values.items() if n}
        if not values:raise Problem('请填写至少一件正数数量')
        if any(n>allowed[pid] for pid,n in values.items()):raise Problem('数量超过可登记、可收货或隔离待处理数量',409)
        return values

    def prepare(self,c,body,action=None):
        if not isinstance(body,dict):raise Problem('售后操作格式无效')
        action=action or body.get('action')
        if action not in ('create','receive','refund','dispose'):raise Problem('售后操作无效')
        case=None if action=='create' else self.case(c,body)
        order=self.order(c,body.get('order_id') if case is None else case['order_id'])
        if type(body.get('order_revision')) is not int or body['order_revision']!=order['revision']:raise Problem('订单版本已变化，请刷新并重新预览',409)
        held=self.held(c,order['id'],case['id'] if case else None)
        quarantine=self.quarantine(c,case['id']) if case else []
        money=self.money(c,order)
        plan={'action':action,'order_id':order['id'],'order_revision':order['revision'],'case_id':case['id'] if case else None}
        if action=='create':
            disposition=body.get('return_disposition')
            if disposition not in ('restock','quarantine','discard'):raise Problem('请选择退货验收去向')
            allowed={l['product_id']:max(0,l['quantity']-l['returned']-held.get(l['product_id'],0)) for l in order['lines']}
            plan.update(reason=text(body.get('reason'),'售后原因',500),quantities=self.quantities(body.get('quantities'),allowed),return_disposition=disposition)
        elif action=='receive':
            if case['status']!='registered':raise Problem('此售后已收货或取消，不能再次收货',409)
            allowed={l['product_id']:max(0,l['quantity']-l['returned']-held.get(l['product_id'],0)) for l in order['lines']}
            plan.update(quantities=self.quantities(case['quantities'],allowed),evidence=text(body.get('evidence'),'验收依据',1500),return_disposition=case['return_disposition'])
        elif action=='refund':
            if case['status']=='cancelled':raise Problem('已取消售后不能登记退款或赔付',409)
            category=body.get('category')
            if category not in ('refund','other'):raise Problem('请选择退款或独立赔付')
            if body.get('currency')!=order['currency']:raise Problem('请使用订单原币登记；跨币退款须先核对原币依据')
            if money['unreconciled_currency_entries']:raise Problem('订单存在跨币退款或赔付，原币累计无法核对，请先在财务核对',409)
            amount=cents(body.get('amount'))
            if not amount or amount>money['remaining_cents']:raise Problem('退款与本地赔付累计不能超过可核对订单总额',409)
            evidence_key=text(body.get('evidence_key'),'凭证行编号',150)
            if c.execute('SELECT id FROM finance_entries WHERE evidence_key=?',(evidence_key,)).fetchone():raise Problem('该财务凭证行已登记，请勿重复计入',409)
            dated=day(body.get('date'),'确认日期')
            if dated>now()[:10]:raise Problem('确认日期不能在未来')
            fx=rate(body.get('fx'),order['currency'])
            reason=text(body.get('reason'),'退款或赔付原因',500)
            plan.update(finance={'kind':'expense','category':category,'currency':order['currency'],'amount':format(Decimal(amount)/100,'.2f'),'fx':fx,'date':dated,'evidence':text(body.get('evidence'),'凭证依据',1500),'evidence_key':evidence_key,'document_id':order['id'],'note':'售后 '+case['id']+' · '+reason},reason=reason,amount_cents=amount,base_cents=converted(amount,fx),money_before=money)
        else:
            disposition=body.get('disposition')
            if disposition=='cancel':
                if case['status']!='registered' or case.get('financial_entries'):raise Problem('仅尚未收货且无关联财务条目的登记可取消；已有财务请先核对',409)
                plan.update(disposition='cancel',evidence=text(body.get('evidence'),'取消依据',1500))
            else:
                if disposition not in ('release','discard') or case['return_disposition']!='quarantine' or case['status']!='received':raise Problem('仅已验收隔离库存可复核释放或报废',409)
                plan.update(disposition=disposition,quantities=self.quantities(body.get('quantities'),{r['product_id']:r['remaining'] for r in quarantine}),evidence=text(body.get('evidence'),'复核依据',1500))
        return plan,case,order,fingerprint([plan,case,order,held,quarantine,money])

    def preview(self,body):
        with self.store.connect() as c:
            c.execute('BEGIN');plan,case,order,token=self.prepare(c,body)
            return {'plan':plan,'preview_digest':token,'connection':'local_only','message':'仅登记本地售后、库存和财务费用；不会向Noon发起退款或支付'}

    def write(self,c,case):
        case['updated_at']=now()
        c.execute('INSERT INTO after_sales_cases VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,data=excluded.data,revision=excluded.revision,updated_at=excluded.updated_at',(case['id'],case['order_id'],case['status'],json.dumps(case,ensure_ascii=False),case['revision'],case['updated_at']))

    def transact(self,action,body):
        if not isinstance(body,dict):raise Problem('操作格式无效')
        key=body.get('request_id')
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,96}',key):raise Problem('操作编号无效')
        if body.get('confirmed') is not True:raise Problem('请人工核对并确认售后操作')
        h=fingerprint([action,body])
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            prior=c.execute('SELECT * FROM after_sales_requests WHERE key=?',(key,)).fetchone()
            if prior:
                if prior['digest']!=h:raise Problem('操作编号已用于不同售后内容',409)
                return json.loads(prior['result'])
            plan,case,order,token=self.prepare(c,body,action)
            if body.get('preview_digest')!=token:raise Problem('订单、售后或财务已变化，请重新预览确认',409)
            if action=='create':
                case={'id':ident(),'order_id':order['id'],'external_id':order['external_id'],'warehouse_id':order['warehouse_id'],'currency':order['currency'],'registered_order_revision':order['revision'],'order_revision':order['revision'],'reason':plan['reason'],'quantities':plan['quantities'],'return_disposition':plan['return_disposition'],'status':'registered','revision':1,'financial_entries':[],'created_at':now()}
            else:
                case['revision']+=1
                if action=='receive':
                    returned=self.ops.return_order(c,{'id':order['id'],'revision':order['revision'],'reason':case['reason'][:400]+' · 售后 '+case['id'],'quantities':case['quantities'],'restock':case['return_disposition']=='restock'})
                    case.update(status='received',received_at=now(),receive_evidence=plan['evidence'],order_revision=returned['revision'])
                    if case['return_disposition']=='quarantine':
                        for pid,n in case['quantities'].items():c.execute('INSERT INTO after_sales_quarantine(case_id,product_id,warehouse_id,received) VALUES(?,?,?,?)',(case['id'],pid,order['warehouse_id'],n))
                elif action=='refund':
                    entry=self.finance.create(c,plan['finance']);case['financial_entries'].append({'entry_id':entry['id'],'category':entry['category'],'amount_cents':entry['amount_cents'],'currency':entry['currency'],'reason':plan['reason']})
                elif plan['disposition']=='cancel':case.update(status='cancelled',cancel_evidence=plan['evidence'],cancelled_at=now())
                else:
                    for pid,n in plan['quantities'].items():
                        if plan['disposition']=='release':self.ops.move(c,pid,order['warehouse_id'],n,0,case['id'],'隔离退货复核释放：'+plan['evidence'])
                        column='released' if plan['disposition']=='release' else 'discarded'
                        c.execute('UPDATE after_sales_quarantine SET '+column+'='+column+'+? WHERE case_id=? AND product_id=?',(n,case['id'],pid))
                    case.setdefault('dispositions',[]).append({'at':now(),'disposition':plan['disposition'],'quantities':plan['quantities'],'evidence':plan['evidence']})
            self.write(c,case)
            c.execute('INSERT INTO after_sales_audit(case_id,action,revision,data,created_at) VALUES(?,?,?,?,?)',(case['id'],action,case['revision'],json.dumps({'request_id':key,'plan':plan},ensure_ascii=False),now()))
            self.store.event(c,None,'本地售后 · '+action,case['id'])
            result=self.enrich(c,case)
            c.execute('INSERT INTO after_sales_requests VALUES(?,?,?)',(key,h,json.dumps(result,ensure_ascii=False)))
            return result

    def create(self,body):return self.transact('create',body)
    def receive(self,body):return self.transact('receive',body)
    def refund(self,body):return self.transact('refund',body)
    def dispose(self,body):return self.transact('dispose',body)

    def enrich(self,c,case):
        result=dict(case);order=self.order(c,case['order_id'])
        result.update(current_order_revision=order['revision'],order_revision=order['revision'],order_lines=order['lines'],quarantine=self.quarantine(c,case['id']),money=self.money(c,order),connection='local_only',platform_refund_sent=False)
        return result

    def state(self,page=0,query='',order_page=0):
        page=page_number(page);order_page=page_number(order_page);query=text(query,'搜索',200,True)
        pattern='%'+query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
        with self.store.connect() as c:
            c.execute('BEGIN')
            where="json_extract(data,'$.external_id') LIKE ? ESCAPE '\\' OR json_extract(data,'$.reason') LIKE ? ESCAPE '\\' OR id LIKE ? ESCAPE '\\'"
            total=c.execute('SELECT count(*) FROM after_sales_cases WHERE '+where,(pattern,)*3).fetchone()[0];pages=max(1,(total+24)//25);page=min(page,pages-1)
            cases=[self.enrich(c,json.loads(r['data'])) for r in c.execute('SELECT data FROM after_sales_cases WHERE '+where+' ORDER BY updated_at DESC,id LIMIT 25 OFFSET ?',(pattern,pattern,pattern,page*25))]
            ow="kind='order' AND json_extract(data,'$.status') IN ('shipped','delivered') AND (json_extract(data,'$.external_id') LIKE ? ESCAPE '\\' OR id LIKE ? ESCAPE '\\')"
            order_total=c.execute('SELECT count(*) FROM ops_documents WHERE '+ow,(pattern,pattern)).fetchone()[0];order_pages=max(1,(order_total+49)//50);order_page=min(order_page,order_pages-1)
            orders=[]
            for r in c.execute('SELECT data FROM ops_documents WHERE '+ow+' ORDER BY updated_at DESC,id LIMIT 50 OFFSET ?',(pattern,pattern,order_page*50)):
                order=json.loads(r['data']);held=self.held(c,order['id']);order['after_sales_available']={l['product_id']:max(0,l['quantity']-l['returned']-held.get(l['product_id'],0)) for l in order['lines']};orders.append(order)
            return {'rows':cases,'cases':cases,'total':total,'page':page,'pages':pages,'orders':orders,'order_total':order_total,'order_page':order_page,'order_pages':order_pages,'query':query}

    def export(self):
        out=io.StringIO();writer=csv.writer(out);writer.writerow(['售后编号','订单编号','来源订单','状态','原因','退货去向','商品编号','登记数量','隔离待处理','释放数量','报废数量','订单原币','订单退款累计','订单赔付累计','跨币待核对','版本'])
        with self.store.connect() as c:
            c.execute('BEGIN')
            for row in c.execute('SELECT data FROM after_sales_cases ORDER BY updated_at DESC,id'):
                case=self.enrich(c,json.loads(row['data']));quarantine={r['product_id']:r for r in case['quarantine']}
                for pid,n in case['quantities'].items():
                    q=quarantine.get(pid,{});values=[case['id'],case['order_id'],case['external_id'],case['status'],case['reason'],case['return_disposition'],pid,n,q.get('remaining',0),q.get('released',0),q.get('discarded',0),case['currency'],format(Decimal(case['money']['refund_cents'])/100,'.2f'),format(Decimal(case['money']['compensation_cents'])/100,'.2f'),'|'.join(case['money']['unreconciled_currency_entries']),case['revision']]
                    writer.writerow(["'"+str(v) if str(v).lstrip().startswith(('=','+','-','@')) or str(v).startswith(('\t','\r','\n')) else v for v in values])
        return ('\ufeff'+out.getvalue()).encode('utf-8')
