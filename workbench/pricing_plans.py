"""Evidence-backed local suggested prices; no marketplace price mutation."""
import csv
import io
import json
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, ROUND_CEILING
from core import Problem, ident, now
from finance import CURRENCIES, day, fingerprint
from operations import text

SCHEMA_SQL='''
CREATE TABLE IF NOT EXISTS pricing_plans(id TEXT PRIMARY KEY,name TEXT NOT NULL,status TEXT NOT NULL,data TEXT NOT NULL,revision INTEGER NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS pricing_plans_updated ON pricing_plans(updated_at DESC,id);
CREATE TABLE IF NOT EXISTS pricing_plan_previews(token TEXT PRIMARY KEY,data TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pricing_plan_requests(key TEXT PRIMARY KEY,digest TEXT NOT NULL,result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pricing_plan_audit(id INTEGER PRIMARY KEY AUTOINCREMENT,plan_id TEXT NOT NULL,action TEXT NOT NULL,revision INTEGER NOT NULL,data TEXT NOT NULL,created_at TEXT NOT NULL);
'''


def decimal(value,label,places=2,maximum='100000000',positive=False,ratio=False):
    if value is None or value=='':return None
    if isinstance(value,bool):raise Problem(label+'必须为有效数字')
    try:n=Decimal(str(value))
    except (InvalidOperation,ValueError):raise Problem(label+'必须为有效数字')
    if not n.is_finite() or n<0 or n>Decimal(maximum) or n.as_tuple().exponent < -places or (positive and n<=0) or (ratio and n>=1):raise Problem(label+'须在有效范围内且精度符合要求')
    return format(n,'f')


def money(value,up=False):
    return format(value.quantize(Decimal('0.01'),rounding=ROUND_CEILING if up else ROUND_HALF_UP),'f')


def page_number(value):
    if isinstance(value,bool) or not str(value).isdecimal() or int(value)>100000:raise Problem('页码无效')
    return int(value)


class PricingPlans:
    def __init__(self,app):
        self.app=app;self.store=app.store
        with self.store.connect() as c:c.executescript(SCHEMA_SQL)

    def snapshot(self,row):return fingerprint([row['id'],row['revision'],row['source_key'],json.loads(row['data'])])

    def calculate(self,member,params):
        blockers=[]
        required={'target_price':'目标价','cost_cny':'参考采购成本','cost_evidence':'成本依据','min_price':'最低保护价','max_price':'最高保护价','reference_price':'参考原售价'}
        for k,label in required.items():
            if member.get(k) is None or member.get(k)=='':blockers.append(label+'待确认')
        labels={'fx':'CNY折算汇率','fx_date':'汇率日期','fx_evidence':'汇率依据','platform_fee_rate':'平台费率','logistics':'原币物流费用','packing':'原币包装费用','advertising':'原币广告预留','tax_rate':'销项税率','max_drop_rate':'最大允许跌价比例'}
        for k,label in labels.items():
            if params.get(k) is None or params.get(k)=='':blockers.append(label+'待确认')
        if params['tax_basis']=='unknown':blockers.append('售价含税口径待确认')
        result={'contribution':None,'break_even_price':None,'net_revenue':None,'customer_total':None,'platform_fee':None,'cost_in_currency':None,'fixed_cost':None,'protection_floor':None,'complete':False,'blockers':blockers}
        if member['min_price'] is not None and member['max_price'] is not None and Decimal(member['min_price'])>Decimal(member['max_price']):raise Problem('最低保护价不能高于最高保护价')
        price=Decimal(member['target_price']) if member['target_price'] is not None else None
        if price is not None:
            if member['min_price'] is not None and price<Decimal(member['min_price']):blockers.append('目标价低于最低保护价')
            if member['max_price'] is not None and price>Decimal(member['max_price']):blockers.append('目标价高于最高保护价')
            if member['reference_price'] is not None and params['max_drop_rate'] is not None:
                floor=Decimal(member['reference_price'])*(1-Decimal(params['max_drop_rate']));result['protection_floor']=money(floor,True)
                if price<floor:blockers.append('目标价触发跌价保护')
        economic_keys=('fx','platform_fee_rate','logistics','packing','advertising','tax_rate')
        if price is None or member['cost_cny'] is None or params['tax_basis']=='unknown' or any(params[k] is None for k in economic_keys):return result
        fx=Decimal(params['fx']);fee_rate=Decimal(params['platform_fee_rate']);tax=Decimal(params['tax_rate'])
        cost=Decimal(member['cost_cny'])/fx
        fixed=cost+sum(Decimal(params[k]) for k in ('logistics','packing','advertising'))
        inclusive=params['tax_basis']=='inclusive'
        customer=price if inclusive else price*(1+tax)
        revenue=price/(1+tax) if inclusive else price
        platform_fee=customer*fee_rate
        contribution=revenue-platform_fee-fixed
        coefficient=1/(1+tax)-fee_rate if inclusive else 1-fee_rate*(1+tax)
        result.update(net_revenue=money(revenue),customer_total=money(customer),platform_fee=money(platform_fee),cost_in_currency=money(cost),fixed_cost=money(fixed),contribution=money(contribution))
        if coefficient<=0:blockers.append('平台费与税费口径下无有限保本价')
        else:result['break_even_price']=money(fixed/coefficient,True)
        if contribution<0:blockers.append('预计贡献为负')
        result['complete']=not any('待确认' in b for b in blockers)
        return result

    def prepare(self,c,body):
        if not isinstance(body,dict):raise Problem('价格方案格式无效')
        old=None;pid=body.get('id')
        if pid:
            row=c.execute('SELECT data FROM pricing_plans WHERE id=?',(text(pid,'计划编号',100),)).fetchone()
            if not row:raise Problem('价格方案不存在',404)
            old=json.loads(row['data'])
            if type(body.get('revision')) is not int or body['revision']!=old['revision']:raise Problem('价格方案已变化，请刷新',409)
            if old['status']=='cancelled':raise Problem('已撤销方案不可修改，请新建方案',409)
        currency=body.get('currency')
        if currency not in CURRENCIES:raise Problem('仅支持现有CNY/SAR/AED/USD币种，EGP尚未接入')
        raw=body.get('parameters')
        if not isinstance(raw,dict):raise Problem('费用和汇率参数无效')
        params={}
        for key,label in [('fx','CNY折算汇率'),('platform_fee_rate','平台费率'),('tax_rate','税率'),('max_drop_rate','最大跌价比例'),('logistics','物流费用'),('packing','包装费用'),('advertising','广告预留')]:
            ratio=key in ('platform_fee_rate','tax_rate','max_drop_rate')
            params[key]=decimal(raw.get(key),label,6 if key=='fx' or ratio else 2,'10000' if key=='fx' else '100000000',positive=key=='fx',ratio=ratio)
        if currency=='CNY' and params['fx'] is not None and Decimal(params['fx'])!=1:raise Problem('CNY方案汇率必须为1')
        params['fx_date']=day(raw['fx_date'],'汇率日期') if raw.get('fx_date') else ''
        if params['fx_date'] and params['fx_date']>now()[:10]:raise Problem('汇率日期不能在未来')
        params['fx_evidence']=text(raw.get('fx_evidence',''),'汇率依据',1500,True)
        basis=raw.get('tax_basis','unknown')
        if basis not in ('inclusive','exclusive','unknown'):raise Problem('售价税费口径无效')
        params['tax_basis']=basis
        members=body.get('members')
        if not isinstance(members,list) or not 1<=len(members)<=500:raise Problem('每方案需1至500个SKU')
        rows=[];seen=set()
        for member in members:
            if not isinstance(member,dict):raise Problem('SKU价格参数无效')
            product_id=text(member.get('product_id'),'商品编号',100)
            if product_id in seen:raise Problem('方案SKU不能重复')
            seen.add(product_id)
            product=c.execute('SELECT * FROM products WHERE id=?',(product_id,)).fetchone()
            if not product:raise Problem('商品不存在',404)
            if type(member.get('revision')) is not int or member['revision']!=product['revision']:raise Problem('商品版本已变化，请刷新并重新预览',409)
            data=json.loads(product['data'])
            item={'product_id':product_id,'revision':product['revision'],'facts_digest':self.snapshot(product),'sku':data.get('partner_sku',''),'title':data.get('title_zh',''),'cost_evidence':text(member.get('cost_evidence',''),'成本依据',1500,True)}
            for key,label in [('target_price','目标价'),('cost_cny','参考成本CNY'),('min_price','最低保护价'),('max_price','最高保护价'),('reference_price','参考原售价')]:item[key]=decimal(member.get(key),label,positive=key in ('target_price','max_price','reference_price'))
            item['calculation']=self.calculate(item,params);rows.append(item)
        status=body.get('status','draft')
        if status not in ('draft','checked'):raise Problem('仅可保存草稿或本地预检通过方案')
        ready=all(not row['calculation']['blockers'] for row in rows)
        if status=='checked' and not ready:raise Problem('资料缺失、贡献为负或价格保护未通过，仅可保存草稿')
        plan={'name':text(body.get('name'),'方案名称',200),'currency':currency,'parameters':params,'members':rows,'status':status,'precheck_passed':ready,'sendable':False,'connection':'local_only','platform_price_written':False,'formula':'参考采购成本CNY÷汇率+原币物流/包装/广告；平台费按买家含税金额；销项税从收入扣除，不计进项抵扣；预计贡献不等于利润'}
        token=fingerprint([old['id'] if old else None,old['revision'] if old else 0,plan])
        return plan,old,token

    def preview(self,body):
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE');plan,old,token=self.prepare(c,body)
            c.execute('INSERT OR IGNORE INTO pricing_plan_previews VALUES(?,?,?)',(token,json.dumps(plan,ensure_ascii=False),now()))
            return {'plan':plan,'rows':plan['members'],'preview_digest':token,'precheck_passed':plan['precheck_passed'],'sendable':False,'message':'仅保存本地建议价格方案与预检结果，不写入商品事实，不向Noon发送价格'}

    def enrich(self,c,plan):
        result=dict(plan);ids=[m['product_id'] for m in plan['members']]
        products={r['id']:r for r in c.execute('SELECT * FROM products WHERE id IN ('+','.join('?' for _ in ids)+')',ids)}
        changed=[m['product_id'] for m in plan['members'] if m['product_id'] not in products or self.snapshot(products[m['product_id']])!=m['facts_digest']]
        result.update(review_required=bool(changed),changed_product_ids=changed,precheck_current=plan['precheck_passed'] and not changed and plan['status']!='cancelled',sendable=False)
        result['members']=[{**m,'current_revision':products[m['product_id']]['revision'] if m['product_id'] in products else None} for m in plan['members']]
        return result

    def get(self,plan_id):
        plan_id=text(plan_id,'价格方案编号',100)
        with self.store.connect() as c:
            c.execute('BEGIN')
            row=c.execute('SELECT data FROM pricing_plans WHERE id=?',(plan_id,)).fetchone()
            if not row:raise Problem('价格方案不存在',404)
            return self.enrich(c,json.loads(row['data']))

    def summary(self,c,row,product_cache):
        result={k:row[k] for k in ('id','name','status','revision','updated_at','currency','created_at','member_count')}
        changed=blocked=complete=0
        # Read only the bindings and readiness fields, not user evidence, prices,
        # parameters or saved calculations. Current products are hashed once per page.
        bindings=c.execute("SELECT json_extract(m.value,'$.product_id') pid,json_extract(m.value,'$.facts_digest') facts_digest,json_array_length(m.value,'$.calculation.blockers') blockers,json_extract(m.value,'$.calculation.complete') complete FROM pricing_plans p,json_each(p.data,'$.members') m WHERE p.id=?",(row['id'],)).fetchall()
        for member in bindings:
            pid=member['pid']
            changed+=product_cache.get(pid)!=member['facts_digest']
            blocked+=bool(member['blockers'])
            complete+=bool(member['complete'])
        passed=bool(row['precheck_passed'])
        result.update(review_required=bool(changed),changed_product_count=changed,blocked_count=blocked,complete_count=complete,precheck_passed=passed,precheck_current=passed and not changed and row['status']!='cancelled',sendable=False,connection='local_only',platform_price_written=False)
        return result

    def mutation(self,action,body):
        if not isinstance(body,dict):raise Problem('操作格式无效')
        key=body.get('request_id')
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,96}',key):raise Problem('请求编号无效')
        if body.get('confirmed') is not True:raise Problem('请人工核对并确认本地价格方案')
        h=fingerprint([action,body])
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE');prior=c.execute('SELECT * FROM pricing_plan_requests WHERE key=?',(key,)).fetchone()
            if prior:
                if prior['digest']!=h:raise Problem('请求编号已用于不同内容',409)
                return json.loads(prior['result'])
            if action=='save':
                plan,old,token=self.prepare(c,body)
                if body.get('preview_digest')!=token or not c.execute('SELECT 1 FROM pricing_plan_previews WHERE token=?',(token,)).fetchone():raise Problem('持久预览已失效，请重新预览确认',409)
                plan.update(id=old['id'] if old else ident(),revision=old['revision']+1 if old else 1,created_at=old['created_at'] if old else now(),updated_at=now(),preview_digest=token)
            else:
                row=c.execute('SELECT data FROM pricing_plans WHERE id=?',(text(body.get('id'),'计划编号',100),)).fetchone()
                if not row:raise Problem('价格方案不存在',404)
                plan=json.loads(row['data'])
                if type(body.get('revision')) is not int or body['revision']!=plan['revision']:raise Problem('价格方案版本已变化',409)
                if plan['status']=='cancelled':raise Problem('价格方案已撤销',409)
                plan.update(status='cancelled',revision=plan['revision']+1,cancel_reason=text(body.get('reason'),'撤销原因',1000),updated_at=now())
            c.execute('INSERT INTO pricing_plans VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,status=excluded.status,data=excluded.data,revision=excluded.revision,updated_at=excluded.updated_at',(plan['id'],plan['name'],plan['status'],json.dumps(plan,ensure_ascii=False),plan['revision'],plan['updated_at']))
            c.execute('INSERT INTO pricing_plan_audit(plan_id,action,revision,data,created_at) VALUES(?,?,?,?,?)',(plan['id'],action,plan['revision'],json.dumps({'request_id':key,'plan':plan},ensure_ascii=False),now()))
            self.store.event(c,None,'本地价格方案 · '+action,plan['id'])
            result=self.enrich(c,plan);c.execute('INSERT INTO pricing_plan_requests VALUES(?,?,?)',(key,h,json.dumps(result,ensure_ascii=False)));return result

    def save(self,body):return self.mutation('save',body)
    def cancel(self,body):return self.mutation('cancel',body)

    def state(self,page=0,query='',product_page=0):
        page=page_number(page);product_page=page_number(product_page);query=text(query,'搜索',200,True);pattern='%'+query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
        with self.store.connect() as c:
            c.execute('BEGIN');where="name LIKE ? ESCAPE '\\' OR EXISTS(SELECT 1 FROM json_each(pricing_plans.data,'$.members') m WHERE json_extract(m.value,'$.sku') LIKE ? ESCAPE '\\')"
            total=c.execute('SELECT count(*) FROM pricing_plans WHERE '+where,(pattern,pattern)).fetchone()[0];pages=max(1,(total+49)//50);page=min(page,pages-1)
            summary_rows=list(c.execute("SELECT id,name,status,revision,updated_at,json_extract(data,'$.currency') currency,json_extract(data,'$.created_at') created_at,json_array_length(data,'$.members') member_count,json_extract(data,'$.precheck_passed') precheck_passed FROM pricing_plans WHERE "+where+' ORDER BY updated_at DESC,id LIMIT 50 OFFSET ?',(pattern,pattern,page*50)))
            plan_ids=[r['id'] for r in summary_rows]
            product_cache={}
            if plan_ids:
                for r in c.execute("SELECT DISTINCT p.* FROM pricing_plans plan JOIN json_each(plan.data,'$.members') member JOIN products p ON p.id=json_extract(member.value,'$.product_id') WHERE plan.id IN ("+','.join('?' for _ in plan_ids)+')',plan_ids):product_cache[r['id']]=self.snapshot(r)
            plans=[self.summary(c,r,product_cache) for r in summary_rows]
            pw="json_extract(data,'$.partner_sku') LIKE ? ESCAPE '\\' OR json_extract(data,'$.title_zh') LIKE ? ESCAPE '\\'"
            product_total=c.execute('SELECT count(*) FROM products WHERE '+pw,(pattern,pattern)).fetchone()[0];product_pages=max(1,(product_total+99)//100);product_page=min(product_page,product_pages-1)
            products=[]
            for r in c.execute('SELECT * FROM products WHERE '+pw+' ORDER BY created_at DESC,id LIMIT 100 OFFSET ?',(pattern,pattern,product_page*100)):
                data=json.loads(r['data']);products.append({'id':r['id'],'revision':r['revision'],'sku':data.get('partner_sku',''),'title':data.get('title_zh',''),'cost_cny':data.get('cost_cny'),'cost_source':'商品参考成本，需人工确认依据'})
            return {'rows':plans,'plans':plans,'total':total,'page':page,'pages':pages,'products':products,'selectable_products':products,'product_total':product_total,'product_page':product_page,'product_pages':product_pages,'currencies':list(CURRENCIES),'query':query}

    def export(self):
        out=io.StringIO();w=csv.writer(out);w.writerow(['方案编号','方案名称','版本','状态','需复核','币种','SKU','商品版本','目标价','参考成本CNY','成本依据','汇率CNY每原币','汇率日期','汇率依据','平台费率','物流原币','包装原币','广告原币','税费口径','税率','最低保护价','最高保护价','参考售价','最大跌价比例','预计贡献非利润','保本价','预检门禁','仅本地方案'])
        with self.store.connect() as c:
            c.execute('BEGIN')
            for r in c.execute('SELECT data FROM pricing_plans ORDER BY updated_at DESC,id'):
                plan=self.enrich(c,json.loads(r['data']));p=plan['parameters']
                for m in plan['members']:
                    values=[plan['id'],plan['name'],plan['revision'],plan['status'],plan['review_required'],plan['currency'],m['sku'],m['revision'],m['target_price'],m['cost_cny'],m['cost_evidence'],p['fx'],p['fx_date'],p['fx_evidence'],p['platform_fee_rate'],p['logistics'],p['packing'],p['advertising'],p['tax_basis'],p['tax_rate'],m['min_price'],m['max_price'],m['reference_price'],p['max_drop_rate'],m['calculation']['contribution'],m['calculation']['break_even_price'],'；'.join(m['calculation']['blockers']),True]
                    w.writerow(["'"+str(v) if str(v).lstrip().startswith(('=','+','-','@')) or str(v).startswith(('\t','\r','\n')) else '' if v is None else v for v in values])
        return ('\ufeff'+out.getvalue()).encode('utf-8')
