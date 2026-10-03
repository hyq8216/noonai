"""Evidence-backed local supplier RFQs. No supplier messaging, buying or payment."""
import csv
import io
import json
import re
from decimal import Decimal
from core import Problem, ident, now
from finance import CURRENCIES, day, fingerprint
from operations import qty, text
from pricing_plans import decimal, page_number

SCHEMA_SQL = '''
CREATE TABLE IF NOT EXISTS supplier_quote_plans(id TEXT PRIMARY KEY,name TEXT NOT NULL,status TEXT NOT NULL,revision INTEGER NOT NULL,updated_at TEXT NOT NULL,data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS supplier_quote_plans_updated ON supplier_quote_plans(updated_at DESC,id);
CREATE TABLE IF NOT EXISTS supplier_quote_previews(token TEXT PRIMARY KEY,data TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS supplier_quote_requests(key TEXT PRIMARY KEY,digest TEXT NOT NULL,result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS supplier_quote_audit(id INTEGER PRIMARY KEY AUTOINCREMENT,plan_id TEXT NOT NULL,action TEXT NOT NULL,revision INTEGER NOT NULL,data TEXT NOT NULL,created_at TEXT NOT NULL);
'''
NOTICE = '仅记录本地询价与人工录入报价，不联系供应商、不下单、不付款；单价比较不含未明确的运费、税费及质量差异，不折算汇率。'


class SupplierQuotes:
    def __init__(self, app):
        self.store = app.store
        with self.store.connect() as c:
            c.executescript(SCHEMA_SQL)

    def snapshot(self, row):
        return fingerprint([row['id'], row['revision'], row['source_key'], json.loads(row['data'])])

    def checks(self, quote, quantity):
        labels = {'currency':'币种','unit_price':'单价','min_quantity':'最小数量','valid_until':'有效期','lead_days':'交期天数','evidence':'报价依据'}
        blockers = [label+'待确认' for key, label in labels.items() if quote.get(key) is None or quote.get(key) == '']
        if quote['valid_until'] and quote['valid_until'] < now()[:10]: blockers.append('报价已过期')
        if quote['min_quantity'] is not None and quantity < quote['min_quantity']: blockers.append('需求数量低于起订量')
        return blockers

    def prepare(self, c, b):
        if not isinstance(b, dict): raise Problem('询价单格式无效')
        old = None
        if b.get('id'):
            row = c.execute('SELECT data FROM supplier_quote_plans WHERE id=?', (text(b['id'],'询价编号',100),)).fetchone()
            if not row: raise Problem('询价单不存在',404)
            old = json.loads(row['data'])
            if type(b.get('revision')) is not int or b['revision'] != old['revision']: raise Problem('询价版本已变化，请刷新',409)
            if old['status'] == 'cancelled': raise Problem('已撤销询价不可修改',409)
        raw = b.get('members')
        if not isinstance(raw, list) or not 1 <= len(raw) <= 100: raise Problem('每单需1至100个SKU')
        members = []; pids = set()
        for m in raw:
            if not isinstance(m, dict): raise Problem('SKU需求格式无效')
            pid = text(m.get('product_id'),'商品编号',100)
            if pid in pids: raise Problem('询价SKU不能重复')
            row = c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone()
            if not row: raise Problem('商品不存在',404)
            p = json.loads(row['data'])
            if p.get('demo'): raise Problem('示例商品不能用于询价')
            if type(m.get('revision')) is not int or m['revision'] != row['revision']: raise Problem('商品已变化，请刷新SKU版本并重新预览',409)
            members.append({'product_id':pid,'revision':row['revision'],'facts_digest':self.snapshot(row),'sku':p.get('partner_sku',''),'title':p.get('title_zh',''),'quantity':qty(m.get('quantity'))})
            pids.add(pid)
        ids = b.get('supplier_ids')
        if not isinstance(ids,list) or not 1 <= len(ids) <= 50: raise Problem('需选择1至50个已登记供应商')
        suppliers = []; sids = set()
        for sid in ids:
            sid = text(sid,'供应商编号',100)
            if sid in sids: raise Problem('供应商不能重复')
            row = c.execute("SELECT * FROM ops_entities WHERE id=? AND kind='supplier'",(sid,)).fetchone()
            if not row: raise Problem('请先登记并选择供应商',404)
            suppliers.append({'id':sid,'name':row['name'],'facts_digest':fingerprint(json.loads(row['data']))})
            sids.add(sid)
        rawquotes = b.get('quotes',[])
        if not isinstance(rawquotes,list) or len(rawquotes)>5000: raise Problem('报价明细格式无效或超过5000行')
        lookup = {}
        for q in rawquotes:
            if not isinstance(q,dict): raise Problem('报价行格式无效')
            pair = (text(q.get('product_id'),'报价商品编号',100),text(q.get('supplier_id'),'报价供应商编号',100))
            if pair[0] not in pids or pair[1] not in sids: raise Problem('报价必须属于本询价的SKU和供应商')
            if pair in lookup: raise Problem('同一供应商与SKU只能填写一条报价')
            lookup[pair] = q
        quotes = []
        for m in members:
            for s in suppliers:
                q = lookup.get((m['product_id'],s['id']),{})
                currency = q.get('currency')
                if currency is None or currency=='': currency=None
                if currency is not None and (not isinstance(currency,str) or currency not in CURRENCIES): raise Problem('报价币种仅支持CNY/SAR/AED/USD')
                def integer(key,zero=False):
                    value = q.get(key)
                    return None if value is None or value=='' else qty(value,zero)
                quote = {'product_id':m['product_id'],'supplier_id':s['id'],'supplier_name':s['name'],'sku':m['sku'],'currency':currency,'unit_price':decimal(q.get('unit_price'),'报价单价',places=4),'min_quantity':integer('min_quantity'),'lead_days':integer('lead_days',True),'valid_until':day(q['valid_until'],'报价有效期') if q.get('valid_until') else None,'evidence':text(q.get('evidence',''),'报价依据',2000,True)}
                quote['blockers'] = self.checks(quote,m['quantity'])
                quotes.append(quote)
        ready = all(not q['blockers'] for q in quotes)
        status = b.get('status','draft')
        if status not in ('draft','checked'): raise Problem('只能保存草稿或本地预检通过状态')
        if status=='checked' and not ready: raise Problem('报价信息不完整、过期或起订量不符，仅可保存草稿')
        plan = {'name':text(b.get('name'),'询价名称',200),'status':status,'members':members,'supplier_ids':[s['id'] for s in suppliers],'suppliers':suppliers,'quotes':quotes,'precheck_passed':ready,'connection':'local_only','sendable':False,'supplier_order_sent':False,'payment_made':False,'notice':NOTICE}
        token = fingerprint([old['id'] if old else None,old['revision'] if old else 0,plan])
        return plan, old, token

    def enrich(self, c, plan, include_comparison=True):
        result = dict(plan); changed = []; supplier_changed = []
        for m in plan['members']:
            row = c.execute('SELECT * FROM products WHERE id=?',(m['product_id'],)).fetchone()
            if not row or self.snapshot(row)!=m['facts_digest']: changed.append(m['product_id'])
        for s in plan['suppliers']:
            row = c.execute("SELECT data FROM ops_entities WHERE id=? AND kind='supplier'",(s['id'],)).fetchone()
            if not row or fingerprint(json.loads(row['data']))!=s['facts_digest']: supplier_changed.append(s['id'])
        quantities = {m['product_id']:m['quantity'] for m in plan['members']}
        result['quotes'] = [{**q,'blockers':self.checks(q,quantities[q['product_id']])} for q in plan['quotes']]
        result.update(review_required=bool(changed or supplier_changed),changed_product_ids=changed,changed_supplier_ids=supplier_changed,precheck_current=not changed and not supplier_changed and not any(q['blockers'] for q in result['quotes']) and plan['status']!='cancelled')
        result['members'] = [{**m,'current_revision':(r['revision'] if (r:=c.execute('SELECT revision FROM products WHERE id=?',(m['product_id'],)).fetchone()) else None)} for m in plan['members']]
        if not include_comparison: return result
        groups = []
        for m in plan['members']:
            currencies = sorted({q['currency'] for q in result['quotes'] if q['product_id']==m['product_id'] and q['currency']})
            for currency in currencies:
                candidates = [q for q in result['quotes'] if q['product_id']==m['product_id'] and q['currency']==currency and not q['blockers']]
                candidates.sort(key=lambda q:(Decimal(q['unit_price']),q['supplier_id']))
                lowest = candidates[0]['unit_price'] if candidates else None
                groups.append({'product_id':m['product_id'],'sku':m['sku'],'currency':currency,'quantity':m['quantity'],'comparable':bool(candidates) and not result['review_required'] and plan['status']!='cancelled','rows':[{'supplier_id':q['supplier_id'],'supplier_name':q['supplier_name'],'unit_price':q['unit_price'],'quoted_subtotal':format(Decimal(q['unit_price'])*m['quantity'],'f'),'lowest_unit_price':Decimal(q['unit_price'])==Decimal(lowest)} for q in candidates] if not result['review_required'] and plan['status']!='cancelled' else []})
        result['comparison'] = groups
        return result

    def preview(self, body):
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE'); plan, _, token = self.prepare(c,body)
            c.execute('INSERT OR IGNORE INTO supplier_quote_previews VALUES(?,?,?)',(token,json.dumps(plan,ensure_ascii=False),now()))
            return {'plan':self.enrich(c,plan),'preview_digest':token,'precheck_passed':plan['precheck_passed'],'sendable':False,'message':NOTICE}

    def mutation(self, action, body):
        if not isinstance(body,dict): raise Problem('操作格式无效')
        key=body.get('request_id')
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,96}',key): raise Problem('请求编号无效')
        digest=fingerprint([action,body])
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            prior=c.execute('SELECT * FROM supplier_quote_requests WHERE key=?',(key,)).fetchone()
            if prior:
                if prior['digest']!=digest: raise Problem('请求编号已用于不同内容',409)
                return json.loads(prior['result'])
            if body.get('confirmed') is not True: raise Problem('请核对预览并人工确认')
            if action=='save':
                plan,old,token=self.prepare(c,body)
                if body.get('preview_digest')!=token or not c.execute('SELECT 1 FROM supplier_quote_previews WHERE token=?',(token,)).fetchone(): raise Problem('持久预览缺失或已失效，请重新预览',409)
                plan.update(id=old['id'] if old else ident(),revision=old['revision']+1 if old else 1,created_at=old['created_at'] if old else now(),updated_at=now())
            elif action=='cancel':
                row=c.execute('SELECT data FROM supplier_quote_plans WHERE id=?',(text(body.get('id'),'询价编号',100),)).fetchone()
                if not row: raise Problem('询价单不存在',404)
                plan=json.loads(row['data'])
                if type(body.get('revision')) is not int or body['revision']!=plan['revision']: raise Problem('询价版本已变化',409)
                if plan['status']=='cancelled': raise Problem('询价已撤销',409)
                plan.update(status='cancelled',revision=plan['revision']+1,cancel_reason=text(body.get('reason'),'撤销原因',1000),updated_at=now())
            else: raise Problem('操作不存在',404)
            c.execute('INSERT INTO supplier_quote_plans VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,status=excluded.status,revision=excluded.revision,updated_at=excluded.updated_at,data=excluded.data',(plan['id'],plan['name'],plan['status'],plan['revision'],plan['updated_at'],json.dumps(plan,ensure_ascii=False)))
            c.execute('INSERT INTO supplier_quote_audit(plan_id,action,revision,data,created_at) VALUES(?,?,?,?,?)',(plan['id'],action,plan['revision'],json.dumps({'request_id':key,'plan':plan},ensure_ascii=False),now()))
            self.store.event(c,None,'本地供应商询价 · '+action,plan['id'])
            result=self.enrich(c,plan)
            c.execute('INSERT INTO supplier_quote_requests VALUES(?,?,?)',(key,digest,json.dumps(result,ensure_ascii=False)))
            return result

    def save(self,body): return self.mutation('save',body)
    def cancel(self,body): return self.mutation('cancel',body)

    def get(self,plan_id):
        with self.store.connect() as c:
            c.execute('BEGIN')
            row=c.execute('SELECT data FROM supplier_quote_plans WHERE id=?',(text(plan_id,'询价编号',100),)).fetchone()
            if not row: raise Problem('询价单不存在',404)
            result=self.enrich(c,json.loads(row['data']))
            result['audit']=[dict(r) for r in c.execute('SELECT action,revision,created_at FROM supplier_quote_audit WHERE plan_id=? ORDER BY id DESC LIMIT 100',(plan_id,))]
            return result

    def state(self,page=0,query='',product_page=0,supplier_page=0):
        page=page_number(page);product_page=page_number(product_page);supplier_page=page_number(supplier_page);query=text(query,'搜索',200,True)
        pattern='%'+query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
        with self.store.connect() as c:
            c.execute('BEGIN')
            where="name LIKE ? ESCAPE '\\' OR EXISTS(SELECT 1 FROM json_each(data,'$.members') m WHERE json_extract(m.value,'$.sku') LIKE ? ESCAPE '\\')"
            total=c.execute('SELECT count(*) FROM supplier_quote_plans WHERE '+where,(pattern,pattern)).fetchone()[0];pages=max(1,(total+49)//50);page=min(page,pages-1)
            rows=[]
            for row in c.execute('SELECT data FROM supplier_quote_plans WHERE '+where+' ORDER BY updated_at DESC,id LIMIT 50 OFFSET ?',(pattern,pattern,page*50)):
                plan=self.enrich(c,json.loads(row['data']),include_comparison=False)
                rows.append({**{k:plan[k] for k in ('id','name','status','revision','updated_at','precheck_current','review_required')},'member_count':len(plan['members']),'supplier_count':len(plan['suppliers']),'blocked_count':sum(bool(q['blockers']) for q in plan['quotes'])})
            pw="coalesce(json_extract(data,'$.demo'),0)=0 AND (json_extract(data,'$.partner_sku') LIKE ? ESCAPE '\\' OR json_extract(data,'$.title_zh') LIKE ? ESCAPE '\\')"
            pt=c.execute('SELECT count(*) FROM products WHERE '+pw,(pattern,pattern)).fetchone()[0];pp=max(1,(pt+99)//100);product_page=min(product_page,pp-1)
            products=[]
            for row in c.execute('SELECT id,revision,data FROM products WHERE '+pw+' ORDER BY created_at DESC,id LIMIT 100 OFFSET ?',(pattern,pattern,product_page*100)):
                p=json.loads(row['data']);products.append({'id':row['id'],'revision':row['revision'],'sku':p.get('partner_sku',''),'title':p.get('title_zh','')})
            st=c.execute("SELECT count(*) FROM ops_entities WHERE kind='supplier'").fetchone()[0];sp=max(1,(st+99)//100);supplier_page=min(supplier_page,sp-1)
            suppliers=[{'id':r['id'],'name':r['name']} for r in c.execute("SELECT id,name FROM ops_entities WHERE kind='supplier' ORDER BY name,id LIMIT 100 OFFSET ?",(supplier_page*100,))]
            return {'rows':rows,'total':total,'page':page,'pages':pages,'products':products,'product_total':pt,'product_page':product_page,'product_pages':pp,'suppliers':suppliers,'supplier_total':st,'supplier_page':supplier_page,'supplier_pages':sp,'currencies':list(CURRENCIES),'query':query,'notice':NOTICE}

    def export(self):
        out=io.StringIO();w=csv.writer(out)
        w.writerow(['询价编号','询价名称','版本','状态','需复核','SKU','需求数量','供应商','币种','单价','最小数量','有效期','交期天数','报价依据','门禁','仅本地询价'])
        with self.store.connect() as c:
            c.execute('BEGIN')
            for r in c.execute('SELECT data FROM supplier_quote_plans ORDER BY updated_at DESC,id'):
                p=self.enrich(c,json.loads(r['data']))
                for q in p['quotes']:
                    quantity=next(m['quantity'] for m in p['members'] if m['product_id']==q['product_id'])
                    values=[p['id'],p['name'],p['revision'],p['status'],p['review_required'],q['sku'],quantity,q['supplier_name'],q['currency'],q['unit_price'],q['min_quantity'],q['valid_until'],q['lead_days'],q['evidence'],'；'.join(q['blockers']),True]
                    w.writerow(['' if v is None else "'"+str(v) if str(v).lstrip().startswith(('=','+','-','@')) or str(v).startswith(('\t','\r','\n')) else v for v in values])
        return ('\ufeff'+out.getvalue()).encode('utf-8')
