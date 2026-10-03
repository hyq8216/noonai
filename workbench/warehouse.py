"""Warehouse transfers and replenishment planning; no supplier/network actions."""
import hashlib
import json
from decimal import Decimal
from core import Problem,ident,now
from operations import qty,cents,text

class Warehouse:
    def __init__(self,ops):
        self.ops=ops
        with ops.store.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS ops_replenishment(product_id TEXT NOT NULL,warehouse_id TEXT NOT NULL,data TEXT NOT NULL,PRIMARY KEY(product_id,warehouse_id))')
    def transfer(self,c,b):
        src=self.ops.entity_id(c,b.get('source_warehouse_id'),'warehouse');dest=self.ops.entity_id(c,b.get('warehouse_id'),'warehouse')
        if src==dest:raise Problem('调出仓和调入仓不能相同')
        raw=b.get('lines')
        if not isinstance(raw,list) or not 1<=len(raw)<=100:raise Problem('调拨单需要1至100个商品')
        lines=[];seen=set()
        for line in raw:
            if not isinstance(line,dict):raise Problem('调拨商品行无效')
            pid,p=self.ops.product_row(c,line)
            if pid in seen:raise Problem('调拨商品不可重复，请合并数量')
            seen.add(pid);lines.append({'product_id':pid,'title':p['title_zh'],'sku':p['partner_sku'],'quantity':qty(line.get('quantity')),'shipped':0,'received':0})
        d={'id':ident(),'kind':'transfer','source_warehouse_id':src,'warehouse_id':dest,'lines':lines,'status':'draft','created_at':now(),'note':text(b.get('note'),'调拨依据',2000)}
        return self.ops.write(c,d,True)
    def product(self,c,pid):
        row=c.execute('SELECT data FROM products WHERE id=?',(pid,)).fetchone()
        if not row or json.loads(row['data']).get('demo'):raise Problem('请选择非示例商品')
        return json.loads(row['data'])
    def dispatch(self,c,b):
        d=self.ops.document(c,b,'transfer')
        if d['status']!='draft':raise Problem('仅待调出单据可以登记出库',409)
        evidence=text(b.get('evidence'),'调出依据',1000)
        for line in d['lines']:
            self.ops.move(c,line['product_id'],d['source_warehouse_id'],-line['quantity'],0,d['id'],'调拨出库')
            line['shipped']=line['quantity']
        d.update(status='in_transit',dispatch_evidence=evidence,dispatched_at=now());return self.ops.write(c,d)
    def receive(self,c,b):
        d=self.ops.document(c,b,'transfer')
        if d['status'] not in ('in_transit','partial'):raise Problem('仅在途调拨可以登记到仓',409)
        quantities=b.get('quantities');evidence=text(b.get('evidence'),'到仓验收依据',1000)
        if not isinstance(quantities,dict) or set(quantities)-{l['product_id'] for l in d['lines']}:raise Problem('到仓商品不属于此调拨单')
        received=[]
        for line in d['lines']:
            n=qty(quantities.get(line['product_id'],0),True)
            if n>line['shipped']-line['received']:raise Problem('到仓数量超过尚未到仓数量')
            if n:
                self.ops.move(c,line['product_id'],d['warehouse_id'],n,0,d['id'],'调拨到仓验收');line['received']+=n;received.append({'product_id':line['product_id'],'quantity':n})
        if not received:raise Problem('请填写本次实际验收数量')
        d.setdefault('receipts',[]).append({'at':now(),'evidence':evidence,'lines':received})
        d['status']='received' if all(l['received']==l['quantity'] for l in d['lines']) else 'partial'
        return self.ops.write(c,d)
    def cancel(self,c,b):
        d=self.ops.document(c,b,'transfer')
        if d['status']!='draft':raise Problem('已调出的货物不能直接取消；请先核实实际去向与到仓情况',409)
        d.update(status='cancelled',cancel_reason=text(b.get('reason'),'取消原因',1000));return self.ops.write(c,d)
    def policy(self,c,b):
        pid,_=self.ops.product_row(c,b);wid=self.ops.entity_id(c,b.get('warehouse_id'),'warehouse');supplier=self.ops.entity_id(c,b.get('supplier_id'),'supplier')
        old=c.execute('SELECT data FROM ops_replenishment WHERE product_id=? AND warehouse_id=?',(pid,wid)).fetchone();old=json.loads(old['data']) if old else None
        expected=old['revision'] if old else 0
        if type(b.get('revision')) is not int or b['revision']!=expected:raise Problem('补货规则已改变，请刷新后编辑',409)
        minimum=qty(b.get('minimum'),True);target=qty(b.get('target'),True);pack=qty(b.get('pack_size'));moq=qty(b.get('min_order'));cost=cents(b.get('unit_price'))
        if minimum>target:raise Problem('目标库存不能低于安全库存')
        if cost<=0:raise Problem('请填写有依据的正数参考采购价；未知成本不能按零元采购')
        if type(b.get('enabled')) is not bool:raise Problem('规则启用状态无效')
        p={'product_id':pid,'warehouse_id':wid,'supplier_id':supplier,'minimum':minimum,'target':target,'pack_size':pack,'min_order':moq,'unit_cents':cost,'quote_evidence':text(b.get('quote_evidence'),'参考报价依据',1500),'enabled':b['enabled'],'revision':expected+1,'updated_at':now()}
        c.execute('INSERT INTO ops_replenishment VALUES(?,?,?) ON CONFLICT(product_id,warehouse_id) DO UPDATE SET data=excluded.data',(pid,wid,json.dumps(p,ensure_ascii=False)));return p
    def plan(self,c):
        policies=[json.loads(r['data']) for r in c.execute('SELECT data FROM ops_replenishment ORDER BY warehouse_id,product_id')]
        stocks={(r['product_id'],r['warehouse_id']):dict(r) for r in c.execute('SELECT * FROM ops_stock')}
        incoming={};demands={};transit={}
        for row in c.execute('SELECT data FROM ops_documents'):
            d=json.loads(row['data']);kind=d['kind'];status=d['status']
            if kind=='purchase' and status in ('open','partial'):
                for l in d['lines']:
                    key=(l['product_id'],d['warehouse_id']);incoming[key]=incoming.get(key,0)+l['quantity']-l['received']
            elif kind=='transfer' and status in ('in_transit','partial'):
                for l in d['lines']:
                    key=(l['product_id'],d['warehouse_id']);transit[key]=transit.get(key,0)+l['shipped']-l['received']
            elif kind=='order' and status=='new':
                for l in d['lines']:
                    key=(l['product_id'],d['warehouse_id']);demands[key]=demands.get(key,0)+l['quantity']
        rows=[]
        for p in policies:
            key=(p['product_id'],p['warehouse_id']);stock=stocks.get(key,{'on_hand':0,'reserved':0});product=self.product(c,p['product_id'])
            available=stock['on_hand']-stock['reserved'];po=incoming.get(key,0);moving=transit.get(key,0);demand=demands.get(key,0);projected=available+po+moving-demand
            needed=max(0,p['target']-projected) if p['enabled'] and projected<p['minimum'] else 0
            quantity=((max(needed,p['min_order'])+p['pack_size']-1)//p['pack_size'])*p['pack_size'] if needed else 0
            r={**p,'title':product['title_zh'],'sku':product['partner_sku'],'on_hand':stock['on_hand'],'reserved':stock['reserved'],'available':available,'purchase_incoming':po,'transfer_incoming':moving,'unreserved_demand':demand,'projected':projected,'needed':needed,'quantity':quantity,'total_cents':quantity*p['unit_cents']}
            r['fingerprint']=hashlib.sha256(json.dumps(r,sort_keys=True,ensure_ascii=False).encode()).hexdigest();rows.append(r)
        return rows
    def replenish(self,c,b):
        selected=b.get('rows')
        if not isinstance(selected,list) or not 1<=len(selected)<=100:raise Problem('请选择1至100条补货建议')
        if b.get('confirmed') is not True:raise Problem('请确认本次参考价、数量和供应商，仅生成本地采购单')
        live={(r['product_id'],r['warehouse_id']):r for r in self.plan(c)};groups={};seen=set()
        for item in selected:
            if not isinstance(item,dict):raise Problem('补货建议格式无效')
            key=(text(item.get('product_id'),'商品编号',100),text(item.get('warehouse_id'),'仓库编号',100))
            if key in seen:raise Problem('不可重复选择补货建议')
            seen.add(key);r=live.get(key)
            if not r or not r['quantity'] or r['fingerprint']!=item.get('fingerprint'):raise Problem('库存、需求、在途采购或规则已改变，请刷新补货建议后重试',409)
            qty(r['quantity']);groups.setdefault((r['supplier_id'],r['warehouse_id']),[]).append(r)
        purchases=[]
        for (supplier,warehouse),rows in groups.items():
            d=self.ops.purchase(c,{'supplier_id':supplier,'warehouse_id':warehouse,'note':'根据已核对的本地补货建议生成；尚未向供应商下单或付款',
                'lines':[{'product_id':r['product_id'],'quantity':r['quantity'],'unit_price':str(Decimal(r['unit_cents'])/100)} for r in rows]})
            d['replenishment_snapshot']=rows
            # Keep the first revision; this metadata is part of the same atomic creation.
            c.execute('UPDATE ops_documents SET data=? WHERE id=?',(json.dumps(d,ensure_ascii=False),d['id']));purchases.append(d['id'])
        return {'id':ident(),'purchase_ids':purchases}
