"""Explicit local procurement-to-order allocations; never supplier orders."""
import csv
import io
import json
from core import Problem, ident, now
from finance import fingerprint
from operations import qty, text

SCHEMA_SQL = '''
CREATE TABLE IF NOT EXISTS procurement_links(
 id TEXT PRIMARY KEY,order_id TEXT NOT NULL,purchase_id TEXT NOT NULL,product_id TEXT NOT NULL,
 quantity INTEGER NOT NULL CHECK(quantity>0),revision INTEGER NOT NULL,
 order_revision INTEGER NOT NULL,purchase_revision INTEGER NOT NULL,
 order_data TEXT NOT NULL,purchase_data TEXT NOT NULL,data TEXT NOT NULL,
 UNIQUE(order_id,purchase_id,product_id));
CREATE INDEX IF NOT EXISTS idx_procurement_order ON procurement_links(order_id,product_id);
CREATE INDEX IF NOT EXISTS idx_procurement_purchase ON procurement_links(purchase_id,product_id);
CREATE TABLE IF NOT EXISTS procurement_requests(key TEXT PRIMARY KEY,digest TEXT NOT NULL,result TEXT NOT NULL);
'''


class Procurement:
    def __init__(self,app):
        self.store=app.store
        with self.store.connect() as c:c.executescript(SCHEMA_SQL)

    def _page(self,value):
        if isinstance(value,bool) or not str(value).isdecimal() or not 0<=int(value)<=1000000:raise Problem('采购关联页码无效')
        return int(value)

    def _document(self,c,did,kind):
        row=c.execute('SELECT data FROM ops_documents WHERE id=? AND kind=?',(did,kind)).fetchone()
        if not row:raise Problem('订单或采购单不存在',404)
        return json.loads(row['data']),row['data']

    def _facts(self,c,b,unlink=False):
        if not isinstance(b,dict):raise Problem('采购关联格式无效')
        oid=text(b.get('order_id'),'订单编号',100);pid=text(b.get('purchase_id'),'采购编号',100)
        product=text(b.get('product_id'),'商品编号',100)
        order,oraw=self._document(c,oid,'order');purchase,praw=self._document(c,pid,'purchase')
        line=next((l for l in order['lines'] if l['product_id']==product),None)
        pline=next((l for l in purchase['lines'] if l['product_id']==product),None)
        if not line or not pline:raise Problem('关联订单与采购单必须包含同一商品')
        if not unlink and order['warehouse_id']!=purchase['warehouse_id']:raise Problem('订单与采购仓库不同，请先通过独立调拨流程处理')
        link=c.execute('SELECT * FROM procurement_links WHERE order_id=? AND purchase_id=? AND product_id=?',(oid,pid,product)).fetchone()
        current_revision=link['revision'] if link else 0
        for key,expected in (('order_revision',order['revision']),('purchase_revision',purchase['revision']),('link_revision',current_revision)):
            if isinstance(b.get(key),bool) or b.get(key)!=expected:raise Problem('订单、采购或关联已改变，请刷新并重新预览',409)
        if unlink and not link:raise Problem('采购关联不存在',404)
        amount=link['quantity'] if unlink else qty(b.get('quantity'))
        used_order=c.execute('SELECT coalesce(sum(quantity),0) FROM procurement_links WHERE order_id=? AND product_id=? AND purchase_id!=?',(oid,product,pid)).fetchone()[0]
        used_purchase=c.execute('SELECT coalesce(sum(quantity),0) FROM procurement_links WHERE purchase_id=? AND product_id=? AND order_id!=?',(pid,product,oid)).fetchone()[0]
        capacity=pline['received'] if purchase['status']=='cancelled' else pline['quantity']
        if not unlink:
            if order['status']=='cancelled':raise Problem('已取消订单不能关联采购；请解除旧关联')
            if amount+used_order>line['quantity']:raise Problem('关联总量超过订单该SKU订购数量，整笔未保存',409)
            if amount+used_purchase>capacity:raise Problem('关联总量超过采购该SKU可分配数量；取消采购仅可分配已收货量',409)
        history=c.execute("""SELECT count(*) FROM procurement_requests WHERE json_extract(result,'$.product_id')=?
            AND (json_extract(result,'$.order_id')=? OR json_extract(result,'$.purchase_id')=?)""",(product,oid,pid)).fetchone()[0]
        token=fingerprint({'order':order,'purchase':purchase,'link':dict(link) if link else None,
                           'quantity':amount,'product_id':product,'used_order':used_order,'used_purchase':used_purchase,'unlink':unlink,'history_version':history})
        # Received units are not assigned to an order automatically. A link is
        # supply planning evidence; warehouse reservations remain Operations-owned.
        return {'order':order,'purchase':purchase,'oraw':oraw,'praw':praw,'line':line,'pline':pline,'link':link,
                'quantity':amount,'token':token,'used_order':used_order,'used_purchase':used_purchase,'capacity':capacity}

    def _preview(self,f):
        return {'preview_token':f['token'],'order_id':f['order']['id'],'purchase_id':f['purchase']['id'],'product_id':f['line']['product_id'],
                'quantity':f['quantity'],'order_revision':f['order']['revision'],'purchase_revision':f['purchase']['revision'],
                'link_revision':f['link']['revision'] if f['link'] else 0,'order_quantity':f['line']['quantity'],
                'purchase_quantity':f['pline']['quantity'],'purchase_received':f['pline']['received'],
                'purchase_pending':max(0,f['pline']['quantity']-f['pline']['received']) if f['purchase']['status']!='cancelled' else 0,
                'purchase_allocatable':f['capacity'],'other_order_allocations':f['used_order'],'other_purchase_allocations':f['used_purchase'],
                'notice':'关联只记录供应计划，不预占库存、不确认到货归属、不新增采购、不支付或发送供应商订单。'}

    def preview(self,body):
        with self.store.connect() as c:
            c.execute('BEGIN');return self._preview(self._facts(c,body,body.get('action')=='unlink' if isinstance(body,dict) else False))

    def _write(self,action,b):
        if not isinstance(b,dict):raise Problem('采购关联格式无效')
        key=text(b.get('request_id'),'操作编号',100);digest=fingerprint([action,b])
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT * FROM procurement_requests WHERE key=?',(key,)).fetchone()
            if old:
                if old['digest']!=digest:raise Problem('操作编号已用于其他内容',409)
                return json.loads(old['result'])
            if b.get('confirmed') is not True:raise Problem('请确认预览后保存采购关联')
            f=self._facts(c,b,action=='unlink')
            if b.get('preview_token')!=f['token']:raise Problem('预览已过期或缺失，请重新预览关联事实',409)
            if action=='unlink':
                result={'id':f['link']['id'],'unlinked':True,'order_id':f['order']['id'],'purchase_id':f['purchase']['id'],'product_id':f['line']['product_id'],'quantity':f['quantity']}
                c.execute('DELETE FROM procurement_links WHERE id=?',(f['link']['id'],))
            else:
                result={'id':f['link']['id'] if f['link'] else ident(),'order_id':f['order']['id'],'purchase_id':f['purchase']['id'],
                        'product_id':f['line']['product_id'],'quantity':f['quantity'],'revision':(f['link']['revision'] if f['link'] else 0)+1,
                        'order_revision':f['order']['revision'],'purchase_revision':f['purchase']['revision'],'updated_at':now(),
                        'confirmed_at':now(),'needs_review':False}
                c.execute('''INSERT INTO procurement_links VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(order_id,purchase_id,product_id) DO UPDATE SET quantity=excluded.quantity,revision=excluded.revision,
                    order_revision=excluded.order_revision,purchase_revision=excluded.purchase_revision,
                    order_data=excluded.order_data,purchase_data=excluded.purchase_data,data=excluded.data''',
                    (result['id'],result['order_id'],result['purchase_id'],result['product_id'],result['quantity'],result['revision'],
                     result['order_revision'],result['purchase_revision'],f['oraw'],f['praw'],json.dumps(result,ensure_ascii=False)))
            c.execute('INSERT INTO procurement_requests VALUES(?,?,?)',(key,digest,json.dumps(result,ensure_ascii=False)))
            self.store.event(c,None,'采购需求关联 · '+action,json.dumps(result,ensure_ascii=False));return result

    def apply(self,body):return self._write('apply',body)
    def unlink(self,body):return self._write('unlink',body)

    def state(self,body=None,page=0):
        body=body or {}
        if not isinstance(body,dict):raise Problem('采购关联筛选格式无效')
        page=self._page(body.get('page',page));purchase_page=self._page(body.get('purchase_page',0));links_page=self._page(body.get('links_page',0))
        where="d.kind='order' AND json_extract(d.data,'$.status') IN ('new','reserved')";params=[]
        for key in ('order_id','warehouse_id'):
            value=body.get(key) or ''
            if not isinstance(value,str) or len(value)>100:raise Problem('采购关联筛选无效')
            if value:
                where+=(' AND d.id=?' if key=='order_id' else " AND json_extract(d.data,'$.warehouse_id')=?");params.append(value)
        with self.store.connect() as c:
            c.execute('BEGIN')
            entities=[dict(r) for r in c.execute("SELECT id,kind,name FROM ops_entities WHERE kind IN ('shop','supplier','warehouse') ORDER BY kind,name")]
            if body.get('warehouse_id') and not any(e['id']==body['warehouse_id'] and e['kind']=='warehouse' for e in entities):raise Problem('仓库不存在',404)
            cte="""WITH links AS (SELECT l.*,CASE WHEN o.data IS NULL OR p.data IS NULL OR o.data!=l.order_data OR p.data!=l.purchase_data
                OR json_extract(o.data,'$.status')='cancelled' THEN 1 ELSE 0 END needs_review,
                CASE WHEN o.data IS NULL OR p.data IS NULL THEN '关联单据缺失'
                WHEN json_extract(o.data,'$.status')='cancelled' THEN '订单已取消，请解除旧关联'
                WHEN o.data!=l.order_data THEN '订单事实已变化，请重新核对'
                WHEN p.data!=l.purchase_data AND json_extract(p.data,'$.status')='cancelled' THEN '采购已取消，仅已收货量可重新关联'
                WHEN p.data!=l.purchase_data THEN '采购事实或到货数量已变化，请重新核对' ELSE '' END review_reason,
                json_extract(o.data,'$.external_id') external_id,json_extract(o.data,'$.revision') current_order_revision,
                json_extract(p.data,'$.revision') current_purchase_revision,json_extract(p.data,'$.status') purchase_status
                FROM procurement_links l LEFT JOIN ops_documents o ON o.id=l.order_id LEFT JOIN ops_documents p ON p.id=l.purchase_id),
                demand AS (SELECT d.id order_id,json_extract(d.data,'$.external_id') external_id,json_extract(d.data,'$.warehouse_id') warehouse_id,
                json_extract(d.data,'$.shop_id') shop_id,json_extract(d.data,'$.revision') order_revision,
                json_extract(d.data,'$.status') status,json_extract(l.value,'$.product_id') product_id,json_extract(l.value,'$.sku') sku,
                json_extract(l.value,'$.title') title,json_extract(l.value,'$.quantity') quantity,
                coalesce((SELECT sum(quantity) FROM links x WHERE x.order_id=d.id AND x.product_id=json_extract(l.value,'$.product_id')),0) linked_quantity,
                coalesce((SELECT sum(quantity) FROM links x WHERE x.order_id=d.id AND x.product_id=json_extract(l.value,'$.product_id') AND needs_review=0),0) confirmed_quantity,
                coalesce((SELECT sum(quantity) FROM links x WHERE x.order_id=d.id AND x.product_id=json_extract(l.value,'$.product_id') AND needs_review=1),0) review_quantity
                FROM ops_documents d,json_each(d.data,'$.lines') l WHERE """+where+') '
            totals=dict(c.execute(cte+'SELECT count(*) demand_lines,coalesce(sum(quantity),0) demand_units,coalesce(sum(confirmed_quantity),0) confirmed_units,coalesce(sum(max(0,quantity-confirmed_quantity)),0) gap_units,coalesce(sum(review_quantity),0) review_units FROM demand',params).fetchone())
            pages=max(1,(totals['demand_lines']+49)//50);page=min(page,pages-1)
            items=[dict(r) for r in c.execute(cte+'SELECT *,max(0,quantity-confirmed_quantity) gap_quantity FROM demand ORDER BY order_id,sku,product_id LIMIT 50 OFFSET ?',params+[page*50])]
            link_total=c.execute('SELECT count(*) FROM procurement_links').fetchone()[0];link_pages=max(1,(link_total+49)//50);links_page=min(links_page,link_pages-1)
            links=[dict(r) for r in c.execute(cte+'SELECT id,order_id,purchase_id,product_id,quantity,revision,order_revision,purchase_revision,needs_review,review_reason,external_id,current_order_revision,current_purchase_revision,purchase_status FROM links ORDER BY needs_review DESC,id LIMIT 50 OFFSET ?',params+[links_page*50])]
            totals['needs_review_links']=c.execute(cte+'SELECT count(*) FROM links WHERE needs_review=1',params).fetchone()[0]
            pw="kind='purchase'";pp=[]
            if body.get('warehouse_id'):pw+=" AND json_extract(data,'$.warehouse_id')=?";pp.append(body['warehouse_id'])
            purchase_total=c.execute('SELECT count(*) FROM ops_documents WHERE '+pw,pp).fetchone()[0];purchase_pages=max(1,(purchase_total+49)//50);purchase_page=min(purchase_page,purchase_pages-1)
            purchases=[json.loads(r['data']) for r in c.execute('SELECT data FROM ops_documents WHERE '+pw+' ORDER BY updated_at DESC,id LIMIT 50 OFFSET ?',pp+[purchase_page*50])]
            allocations={}
            if purchases:
                placeholders=','.join('?' for _ in purchases)
                allocations={(r['purchase_id'],r['product_id']):r['quantity'] for r in c.execute('SELECT purchase_id,product_id,sum(quantity) quantity FROM procurement_links WHERE purchase_id IN ('+placeholders+') GROUP BY purchase_id,product_id',[p['id'] for p in purchases])}
            for p in purchases:
                for line in p['lines']:
                    line['allocated_quantity']=allocations.get((p['id'],line['product_id']),0)
                    line['allocatable_quantity']=max(0,(line['received'] if p['status']=='cancelled' else line['quantity'])-line['allocated_quantity'])
            return {'filters':{'page':page,'purchase_page':purchase_page,'links_page':links_page,'order_id':body.get('order_id') or '','warehouse_id':body.get('warehouse_id') or ''},'entities':entities,'summary':totals,
                'demand_page':{'items':items,'page':page,'pages':pages,'total':totals['demand_lines'],'page_size':50},
                'links_page':{'items':links,'page':links_page,'pages':link_pages,'total':link_total,'page_size':50},'links':links,
                'purchase_page':{'items':purchases,'page':purchase_page,'pages':purchase_pages,'total':purchase_total,'page_size':50},
                'basis':['需求缺口只统计当前待处理/已占用订单，关联是供应计划，不等于库存预占或订单已满足。',
                    '采购与订单任一事实变化均要求重新核对；过期关联保留分配容量，显式解除或重新预览确认后才释放/恢复。',
                    '同订单SKU关联不超过订购量，同采购SKU关联不超过采购量；取消采购只允许在已收货量内重新关联，取消订单不可新增关联。',
                    '采购到货是采购单累计事实，不自动分配到具体订单；订单占用、发货、财务成本仍通过原业务流程处理。',
                    '关联与需求/采购均每页50条，关联列表为全局审查列表，不随需求仓库筛选隐藏异常。'],'generated_at':now()}

    def export(self,body=None):
        s=self.state(body);out=io.StringIO();w=csv.writer(out)
        def write(row):
            vals=[]
            for value in row:
                value='' if value is None else str(value)
                if value.lstrip().startswith(('=','+','-','@')) or value.startswith(('\t','\r','\n')):value="'"+value
                vals.append(value)
            w.writerow(vals)
        write(['采购需求关联报告','生成时间',s['generated_at']])
        for basis in s['basis']:write(['口径',basis])
        for key,value in s['summary'].items():write(['汇总',key,value])
        write(['需求页',s['demand_page']['page']+1,'总页数',s['demand_page']['pages']])
        keys=['order_id','external_id','product_id','sku','title','quantity','linked_quantity','confirmed_quantity','review_quantity','gap_quantity']
        write(keys)
        for row in s['demand_page']['items']:write([row[k] for k in keys])
        write(['关联页',s['links_page']['page']+1,'总页数',s['links_page']['pages']])
        keys=['id','order_id','purchase_id','product_id','quantity','revision','needs_review']
        write(keys)
        for row in s['links']:write([row[k] for k in keys])
        return ('\ufeff'+out.getvalue()).encode('utf-8')
