"""Evidence-backed local obligations, settlements and order contribution.

Not a bank connector or statutory accounting system. Source documents are never
silently recognized as revenue/cost. Cash settles an obligation without posting it
twice. Original currency and a user-supplied dated CNY conversion are retained.
"""
import csv
import hashlib
import io
import json
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from core import Problem,ident,now
from operations import cents,text

CATEGORIES={'sale':'卖家结算收入','refund':'买家退款','goods':'商品采购成本','platform':'平台费用','logistics':'物流运费','packing':'包装费用','ads':'广告费用','supplier_refund':'供应商返款','other':'其他收入或费用'}
CURRENCIES=('CNY','SAR','USD','AED')

def fingerprint(value):return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
def day(value,label):
    value=text(value,label,10)
    try:parsed=date.fromisoformat(value)
    except ValueError:raise Problem(label+'应为YYYY-MM-DD')
    if parsed.isoformat()!=value:raise Problem(label+'应为YYYY-MM-DD')
    return value

def rate(value,currency):
    if isinstance(value,bool):raise Problem('汇率格式无效')
    try:n=Decimal(str(value))
    except InvalidOperation:raise Problem('汇率格式无效')
    if not n.is_finite() or not 0<n<=10000 or n.as_tuple().exponent < -6:raise Problem('汇率须大于0，最多六位小数')
    if currency=='CNY' and n!=1:raise Problem('人民币折算汇率必须为1')
    return str(n.normalize())

def converted(amount,fx):return int((Decimal(amount)*Decimal(fx)).quantize(Decimal('1'),rounding=ROUND_HALF_UP))
def apportioned(total,weights):
    """Largest remainder keeps each cent, including the unallocated remainder."""
    size=sum(weights)
    if not size:return [0]*len(weights)
    values=[total*w//size for w in weights]
    order=sorted(range(len(weights)),key=lambda i:(-(total*weights[i]%size),i))
    for i in order[:total-sum(values)]:values[i]+=1
    return values

class Finance:
    def __init__(self,store):
        self.store=store
        with store.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS finance_entries(id TEXT PRIMARY KEY, evidence_key TEXT UNIQUE NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS finance_payments(id TEXT PRIMARY KEY, entry_id TEXT NOT NULL, evidence_key TEXT UNIQUE NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS finance_requests(key TEXT PRIMARY KEY,digest TEXT NOT NULL,result TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS finance_reviews(order_id TEXT PRIMARY KEY,data TEXT NOT NULL);
            ''')
    def transact(self,action,b):
        if not isinstance(b,dict):raise Problem('财务操作格式无效')
        key=text(b.get('request_id'),'操作编号',100);h=fingerprint([action,b])
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            prior=c.execute('SELECT * FROM finance_requests WHERE key=?',(key,)).fetchone()
            if prior:
                if prior['digest']!=h:raise Problem('操作编号已用于其他内容',409)
                return json.loads(prior['result'])
            methods={'entry':self.create,'payment':self.pay,'allocate':self.allocate,'void_entry':self.void_entry,'void_payment':self.void_payment,'review':self.review}
            if action not in methods:raise Problem('财务操作不存在',404)
            result=methods[action](c,b)
            c.execute('INSERT INTO finance_requests VALUES(?,?,?)',(key,h,json.dumps(result,ensure_ascii=False)))
            self.store.event(c,None,'财务 · '+action,result.get('id',result.get('order_id','')))
            return result
    def get(self,c,eid):
        row=c.execute('SELECT data FROM finance_entries WHERE id=?',(eid,)).fetchone()
        if not row:raise Problem('财务条目不存在',404)
        return json.loads(row['data'])
    def document(self,c,did,kind=None):
        row=c.execute('SELECT data FROM ops_documents WHERE id=?',(did,)).fetchone()
        if not row:raise Problem('关联业务单据不存在',404)
        d=json.loads(row['data'])
        if d['kind'] not in ('order','purchase'):raise Problem('财务条目仅可关联订单或采购单')
        if kind and d['kind']!=kind:raise Problem('请选择正确的业务单据')
        return d
    def guard(self,e,b):
        if isinstance(b.get('revision'),bool) or b.get('revision')!=e['revision']:raise Problem('财务条目已更新，请刷新后重试',409)
        if e['status']!='active':raise Problem('已作废条目不可继续处理',409)
    def save(self,c,e):
        e['revision']+=1;e['updated_at']=now()
        c.execute('UPDATE finance_entries SET data=? WHERE id=?',(json.dumps(e,ensure_ascii=False),e['id']));return e
    def allocations(self,c,rows,amount):
        if not isinstance(rows,list) or len(rows)>100:raise Problem('一次最多分摊100个订单')
        out=[];seen=set()
        for row in rows:
            if not isinstance(row,dict):raise Problem('分摊格式无效')
            oid=text(row.get('order_id'),'订单编号',100);self.document(c,oid,'order');n=cents(row.get('amount'))
            if not n or oid in seen:raise Problem('分摊订单不可重复，金额必须大于0')
            seen.add(oid);out.append({'order_id':oid,'amount_cents':n})
        if sum(r['amount_cents'] for r in out)>amount:raise Problem('分摊总额超过条目原币金额，整笔操作未保存')
        return out
    def create(self,c,b):
        kind=b.get('kind');category=b.get('category');currency=b.get('currency')
        if kind not in ('income','expense') or category not in CATEGORIES or currency not in CURRENCIES:raise Problem('收支类型、分类或币种无效')
        if category in ('sale','supplier_refund') and kind!='income':raise Problem('此分类只能登记为收入')
        if category not in ('sale','supplier_refund','other') and kind!='expense':raise Problem('此分类只能登记为费用')
        amount=cents(b.get('amount'))
        if amount<=0:raise Problem('金额必须大于0')
        evidence=text(b.get('evidence'),'凭证依据',1500);key=text(b.get('evidence_key'),'凭证行编号',150)
        if c.execute('SELECT id FROM finance_entries WHERE evidence_key=?',(key,)).fetchone():raise Problem('此凭证行已登记；如需纠错，请作废后使用新的行编号',409)
        fx=rate(b.get('fx'),currency);dated=day(b.get('date'),'确认日期')
        if dated>now()[:10]:raise Problem('已确认收入或费用不能使用未来日期')
        due=day(b['due_date'],'到期日期') if b.get('due_date') else ''
        document_id=text(b.get('document_id',''),'关联单据',100,True)
        doc=self.document(c,document_id) if document_id else None
        allocation=[]
        if doc and doc['kind']=='order':allocation=[{'order_id':document_id,'amount_cents':amount}]
        if category=='sale' and (not doc or doc['kind']!='order'):raise Problem('卖家结算收入需要关联订单')
        e={'id':ident(),'kind':kind,'category':category,'currency':currency,'amount_cents':amount,'fx':fx,'base_cents':converted(amount,fx),
           'date':dated,'due_date':due,'document_id':document_id,'allocations':allocation,'evidence':evidence,'evidence_key':key,
           'note':text(b.get('note',''),'备注',2000,True),'status':'active','revision':1,'created_at':now(),'updated_at':now()}
        c.execute('INSERT INTO finance_entries VALUES(?,?,?)',(e['id'],key,json.dumps(e,ensure_ascii=False)));return e
    def payments(self,c,eid):return [json.loads(r['data']) for r in c.execute('SELECT data FROM finance_payments WHERE entry_id=? ORDER BY id',(eid,))]
    def pay(self,c,b):
        e=self.get(c,b.get('id'));self.guard(e,b)
        paid=sum(p['amount_cents'] for p in self.payments(c,e['id']) if p['status']=='active')
        amount=cents(b.get('amount'))
        if amount<=0 or amount>e['amount_cents']-paid:raise Problem('本次收付款超过未结金额或不是正数')
        fx=rate(b.get('fx'),e['currency']);dated=day(b.get('date'),'收付款日期')
        if dated<e['date'] or dated>now()[:10]:raise Problem('收付款日期不得早于条目确认日期或晚于今天；预付款请等待专用流程')
        key=text(b.get('evidence_key'),'收付款凭证行编号',150)
        if c.execute('SELECT id FROM finance_payments WHERE evidence_key=?',(key,)).fetchone():raise Problem('此收付款凭证行已登记',409)
        p={'id':ident(),'entry_id':e['id'],'amount_cents':amount,'currency':e['currency'],'fx':fx,'base_cents':converted(amount,fx),
           'date':dated,'evidence_key':key,'evidence':text(b.get('evidence'),'收付款依据',1500),'account':text(b.get('account'),'收付款账户名称',150),
           'status':'active','created_at':now()}
        c.execute('INSERT INTO finance_payments VALUES(?,?,?,?)',(p['id'],e['id'],key,json.dumps(p,ensure_ascii=False)))
        self.save(c,e);return p
    def allocate(self,c,b):
        e=self.get(c,b.get('id'));self.guard(e,b)
        if e['document_id'] and self.document(c,e['document_id'])['kind']=='order':raise Problem('直接关联订单的条目已全额归属该订单；重新分摊请先作废并纠正来源',409)
        e['allocations']=self.allocations(c,b.get('allocations'),e['amount_cents']);return self.save(c,e)
    def void_entry(self,c,b):
        e=self.get(c,b.get('id'));self.guard(e,b)
        if any(p['status']=='active' for p in self.payments(c,e['id'])):raise Problem('请先撤销此条目的收付款记录，再作废条目',409)
        e.update(status='void',void_reason=text(b.get('reason'),'作废原因',1000),voided_at=now());return self.save(c,e)
    def void_payment(self,c,b):
        row=c.execute('SELECT data FROM finance_payments WHERE id=?',(b.get('payment_id'),)).fetchone()
        if not row:raise Problem('收付款记录不存在',404)
        p=json.loads(row['data']);e=self.get(c,p['entry_id']);self.guard(e,b)
        if p['status']!='active':raise Problem('收付款记录已撤销',409)
        p.update(status='void',void_reason=text(b.get('reason'),'撤销原因',1000),voided_at=now())
        c.execute('UPDATE finance_payments SET data=? WHERE id=?',(json.dumps(p,ensure_ascii=False),p['id']));self.save(c,e);return p
    def snapshot(self,c):
        entries=[json.loads(r['data']) for r in c.execute('SELECT data FROM finance_entries ORDER BY rowid DESC')]
        payments=[json.loads(r['data']) for r in c.execute('SELECT data FROM finance_payments ORDER BY rowid DESC')]
        docs=[json.loads(r['data']) for r in c.execute("SELECT data FROM ops_documents WHERE kind='order' ORDER BY updated_at DESC")]
        reviews={r['order_id']:json.loads(r['data']) for r in c.execute('SELECT * FROM finance_reviews')}
        by_entry={}
        for p in payments:by_entry.setdefault(p['entry_id'],[]).append(p)
        currency={};income=expense=cash_in=cash_out=fx_total=unallocated=0;order_lines={}
        for e in entries:
            ps=by_entry.get(e['id'],[]);active=[p for p in ps if p['status']=='active']
            paid=sum(p['amount_cents'] for p in active);remaining=e['amount_cents']-paid
            e['paid_cents']=paid;e['remaining_cents']=remaining if e['status']=='active' else 0
            # Carrying value release is calculated on cumulative paid amount so
            # splitting a payment does not create extra rounding gains/losses.
            released=converted(e['base_cents'],Decimal(paid)/Decimal(e['amount_cents']))
            sign=1 if e['kind']=='income' else -1
            e['remaining_base_cents']=e['base_cents']-released if e['status']=='active' else 0
            e['realized_fx_cents']=sign*(sum(p['base_cents'] for p in active)-released) if e['status']=='active' else 0
            e['overdue']=e['status']=='active' and remaining>0 and bool(e['due_date']) and e['due_date']<now()[:10]
            e['payment_ids']=[p['id'] for p in ps]
            if e['status']!='active':continue
            if e['kind']=='income':income+=e['base_cents'];cash_in+=sum(p['base_cents'] for p in active)
            else:expense+=e['base_cents'];cash_out+=sum(p['base_cents'] for p in active)
            fx_total+=e['realized_fx_cents']
            cur=currency.setdefault(e['currency'],{'currency':e['currency'],'receivable_cents':0,'payable_cents':0})
            cur['receivable_cents' if sign==1 else 'payable_cents']+=remaining
            weights=[a['amount_cents'] for a in e['allocations']];left=e['amount_cents']-sum(weights)
            shares=apportioned(e['base_cents'],weights+[left]);unallocated+=sign*shares[-1]
            for a,share in zip(e['allocations'],shares):
                order_lines.setdefault(a['order_id'],[]).append({'entry_id':e['id'],'entry_revision':e['revision'],'kind':e['kind'],'category':e['category'],'amount_cents':a['amount_cents'],'currency':e['currency'],'base_cents':share,'remaining_entry_cents':remaining})
        orders=[]
        for d in docs:
            lines=order_lines.get(d['id'],[]);inc=sum(x['base_cents'] for x in lines if x['kind']=='income');exp=sum(x['base_cents'] for x in lines if x['kind']=='expense')
            stamp=fingerprint({'document':d,'lines':lines});review=reviews.get(d['id'])
            orders.append({'id':d['id'],'external_id':d['external_id'],'shop_id':d['shop_id'],'status':d['status'],'lines':lines,
                'income_cents':inc,'expense_cents':exp,'contribution_cents':inc-exp if lines else None,
                'fingerprint':stamp,'review':review,'reconciled':bool(review and review['fingerprint']==stamp),
                'has_revenue':any(x['category']=='sale' for x in lines),'has_cost':any(x['kind']=='expense' for x in lines)})
        return {'entries':entries,'payments':payments,'orders':orders,'currencies':list(currency.values()),'categories':CATEGORIES,
            'summary':{'income_cents':income,'expense_cents':expense,'recorded_difference_cents':income-expense,'cash_in_cents':cash_in,'cash_out_cents':cash_out,'cash_net_cents':cash_in-cash_out,'realized_fx_cents':fx_total,'unallocated_net_cents':unallocated,'active_entries':sum(e['status']=='active' for e in entries)}}
    def state(self):
        with self.store.connect() as c:c.execute('BEGIN');return self.snapshot(c)
    def review(self,c,b):
        s=self.snapshot(c);o=next((o for o in s['orders'] if o['id']==b.get('order_id')),None)
        if not o:raise Problem('订单不存在',404)
        if o['fingerprint']!=b.get('fingerprint'):raise Problem('订单或财务记录已改变，请刷新后重新核对',409)
        if o['status']!='delivered' or not o['has_revenue'] or not o['has_cost']:raise Problem('订单需已签收，并登记卖家收入和成本后才能核对完成',409)
        if any(b.get(k) is not True for k in ('income_checked','costs_checked','refunds_checked')):raise Problem('请核对收入、完整成本和退款情况')
        review={'order_id':o['id'],'fingerprint':o['fingerprint'],'note':text(b.get('note'),'核对依据',2000),'at':now()}
        c.execute('INSERT INTO finance_reviews VALUES(?,?) ON CONFLICT(order_id) DO UPDATE SET data=excluded.data',(o['id'],json.dumps(review,ensure_ascii=False)));return review
    def csv(self):
        s=self.state();out=io.StringIO();w=csv.writer(out)
        w.writerow(['类型','编号','状态','日期','方向','分类','原币','金额','折算汇率CNY','人民币金额','未结原币','关联单据','凭证行编号','凭证依据'])
        def safe(v):
            value=str(v)
            return "'"+value if value.lstrip().startswith(('=','+','-','@')) or value.startswith(('\t','\r','\n')) else value
        for e in s['entries']:
            w.writerow([safe(v) for v in ['条目',e['id'],e['status'],e['date'],e['kind'],CATEGORIES[e['category']],e['currency'],format(Decimal(e['amount_cents'])/100,'.2f'),e['fx'],format(Decimal(e['base_cents'])/100,'.2f'),format(Decimal(e['remaining_cents'])/100,'.2f'),e['document_id'],e['evidence_key'],e['evidence']]])
        entry_map={e['id']:e for e in s['entries']}
        for p in s['payments']:
            e=entry_map[p['entry_id']]
            w.writerow([safe(v) for v in ['收付款',p['id'],p['status'],p['date'],e['kind'],CATEGORIES[e['category']],p['currency'],format(Decimal(p['amount_cents'])/100,'.2f'),p['fx'],format(Decimal(p['base_cents'])/100,'.2f'),'',p['entry_id'],p['evidence_key'],p['evidence']]])
        return ('\ufeff'+out.getvalue()).encode('utf-8')
