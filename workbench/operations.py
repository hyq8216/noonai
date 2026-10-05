"""Local purchasing and fulfillment ledger. No marketplace or supplier writes."""
import hashlib
import json
from decimal import Decimal, InvalidOperation
from core import Problem, ident, now


def text(value, label, maximum=250, optional=False):
    if not isinstance(value, str): raise Problem(label+'格式无效')
    value=value.strip()
    if (not value and not optional) or len(value)>maximum: raise Problem(label+'不能为空或过长')
    return value


def qty(value, zero=False):
    if isinstance(value,bool): raise Problem('数量必须为整数')
    try: n=Decimal(str(value))
    except InvalidOperation: raise Problem('数量必须为整数')
    if not n.is_finite() or n!=n.to_integral_value() or n<(0 if zero else 1) or n>1000000: raise Problem('数量必须是有效整数，最多1000000')
    return int(n)


def cents(value):
    if isinstance(value,bool): raise Problem('金额格式错误')
    try: n=Decimal(str(value))
    except InvalidOperation: raise Problem('金额格式错误')
    if not n.is_finite() or n<0 or n>100000000 or n.as_tuple().exponent < -2: raise Problem('金额须非负且最多两位小数')
    return int(n*100)


class Operations:
    def __init__(self, store):
        self.store=store
        with store.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS ops_entities(id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL, data TEXT NOT NULL, UNIQUE(kind,name));
            CREATE TABLE IF NOT EXISTS ops_documents(id TEXT PRIMARY KEY, kind TEXT NOT NULL, external_key TEXT UNIQUE, data TEXT NOT NULL, revision INTEGER NOT NULL, updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_ops_documents_kind_updated ON ops_documents(kind,updated_at DESC,id DESC);
            CREATE TABLE IF NOT EXISTS ops_stock(product_id TEXT NOT NULL, warehouse_id TEXT NOT NULL, on_hand INTEGER NOT NULL DEFAULT 0 CHECK(on_hand>=0), reserved INTEGER NOT NULL DEFAULT 0 CHECK(reserved>=0 AND reserved<=on_hand), PRIMARY KEY(product_id,warehouse_id));
            CREATE TABLE IF NOT EXISTS ops_movements(id TEXT PRIMARY KEY, product_id TEXT NOT NULL, warehouse_id TEXT NOT NULL, delta INTEGER NOT NULL, reserved_delta INTEGER NOT NULL, reference TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ops_requests(key TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT NOT NULL);
            ''')

        from warehouse import Warehouse
        self.warehouse=Warehouse(self)

    def transact(self, action, body):
        if not isinstance(body,dict): raise Problem('操作格式无效')
        key=text(body.get('request_id'), '操作编号',100)
        try:digest=hashlib.sha256(json.dumps([action,body],sort_keys=True,ensure_ascii=False).encode('utf-8')).hexdigest()
        except UnicodeEncodeError:raise Problem('操作内容含有无效Unicode字符，请检查后重试')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT * FROM ops_requests WHERE key=?',(key,)).fetchone()
            if old:
                if old['digest']!=digest: raise Problem('相同操作编号不能用于不同内容',409)
                return json.loads(old['result'])
            methods={'entity':self.entity,'purchase':self.purchase,'order':self.order,'adjust':self.adjust,'receive':self.receive,'reserve':self.reserve,'ship':self.ship,'deliver':self.deliver,'cancel':self.cancel,'return':self.return_order}
            from category_batch import CategoryBatch
            methods['category_batch']=CategoryBatch(self.store).apply
            methods.update(transfer=self.warehouse.transfer,transfer_dispatch=self.warehouse.dispatch,transfer_receive=self.warehouse.receive,transfer_cancel=self.warehouse.cancel,replenishment_policy=self.warehouse.policy,replenish=self.warehouse.replenish)
            if action not in methods: raise Problem('业务操作不存在',404)
            out=methods[action](c,body)
            c.execute('INSERT INTO ops_requests VALUES(?,?,?)',(key,digest,json.dumps(out,ensure_ascii=False)))
            self.store.event(c,None,'运营业务 · '+action,str(out.get('id','')))
            return out

    def entity(self,c,b):
        kind=b.get('kind')
        if kind not in ('supplier','warehouse','shop'): raise Problem('档案类型无效')
        name=text(b.get('name'),'名称')
        if c.execute('SELECT id FROM ops_entities WHERE kind=? AND name=?',(kind,name)).fetchone(): raise Problem('已有同名档案',409)
        e={'id':ident(),'kind':kind,'name':name,'contact':text(b.get('contact',''),'联系信息',500,True),'note':text(b.get('note',''),'备注',2000,True),'created_at':now(),'connection':'local_only'}
        c.execute('INSERT INTO ops_entities VALUES(?,?,?,?)',(e['id'],kind,name,json.dumps(e,ensure_ascii=False)))
        return e

    def entity_id(self,c,value,kind):
        e=c.execute('SELECT id FROM ops_entities WHERE id=? AND kind=?',(value,kind)).fetchone()
        if not e: raise Problem('请先建立并选择'+{'supplier':'供应商','warehouse':'仓库','shop':'店铺'}[kind])
        return value

    def product_row(self,c,body):
        pid=body.get('product_id')
        if pid:
            row=c.execute('SELECT id,data FROM products WHERE id=?',(pid,)).fetchone()
        else:
            sku=str(body.get('partner_sku') or '').strip()
            if not sku:raise Problem('请填写工作台 SKU')
            matches=c.execute("SELECT id,data FROM products WHERE json_extract(data,'$.partner_sku')=? LIMIT 2",(sku,)).fetchall()
            if len(matches)>1:raise Problem('工作台 SKU 对应多个商品，请先修正商品档案')
            row=matches[0] if matches else None
        if not row:raise Problem('工作台 SKU 未找到商品，请核对完整货号')
        product=json.loads(row['data'])
        if product.get('demo'):raise Problem('示例商品不能用于运营单据，请建立真实或专用测试商品')
        return row['id'],product

    def lines(self,c,b):
        rows=b.get('lines')
        if not isinstance(rows,list) or not 1<=len(rows)<=100: raise Problem('每单需1至100个商品')
        out=[]; seen=set()
        for row in rows:
            if not isinstance(row,dict): raise Problem('商品行格式无效')
            pid,p=self.product_row(c,row)
            if pid in seen: raise Problem('同一单据的商品不能重复，请合并数量')
            seen.add(pid)
            out.append({'product_id':pid,'sku':p['partner_sku'],'title':p['title_zh'],'quantity':qty(row.get('quantity')),'unit_cents':cents(row.get('unit_price')),'received':0,'returned':0})
        return out

    def write(self,c,d,new=False):
        d['updated_at']=now(); d['revision']=1 if new else d['revision']+1
        if new: c.execute('INSERT INTO ops_documents VALUES(?,?,?,?,?,?)',(d['id'],d['kind'],d.get('external_key'),json.dumps(d,ensure_ascii=False),d['revision'],d['updated_at']))
        else: c.execute('UPDATE ops_documents SET data=?,revision=?,updated_at=? WHERE id=?',(json.dumps(d,ensure_ascii=False),d['revision'],d['updated_at'],d['id']))
        return d

    def document(self,c,b,kind=None):
        row=c.execute('SELECT data FROM ops_documents WHERE id=?',(b.get('id'),)).fetchone()
        if not row: raise Problem('单据不存在',404)
        d=json.loads(row['data'])
        if kind and d['kind']!=kind: raise Problem('单据类型不正确')
        if isinstance(b.get('revision'),bool) or b.get('revision')!=d['revision']: raise Problem('单据已更新，请刷新后重试',409)
        return d

    def purchase(self,c,b):
        supplier=self.entity_id(c,b.get('supplier_id'),'supplier'); warehouse=self.entity_id(c,b.get('warehouse_id'),'warehouse')
        rows=self.lines(c,b)
        d={'id':ident(),'kind':'purchase','supplier_id':supplier,'warehouse_id':warehouse,'lines':rows,'currency':'CNY','total_cents':sum(l['quantity']*l['unit_cents'] for l in rows),'status':'open','created_at':now(),'note':text(b.get('note',''),'备注',2000,True)}
        return self.write(c,d,True)

    def order(self,c,b):
        shop=self.entity_id(c,b.get('shop_id'),'shop'); warehouse=self.entity_id(c,b.get('warehouse_id'),'warehouse')
        external=text(b.get('external_id'),'来源订单号'); key=json.dumps([shop,external])
        if c.execute('SELECT id FROM ops_documents WHERE external_key=?',(key,)).fetchone(): raise Problem('该店铺订单号已存在，未重复导入',409)
        currency=b.get('currency')
        if currency not in ('SAR','USD','CNY','AED'): raise Problem('币种无效')
        rows=self.lines(c,b)
        d={'id':ident(),'kind':'order','external_key':key,'external_id':external,'shop_id':shop,'warehouse_id':warehouse,'lines':rows,'currency':currency,'total_cents':sum(l['quantity']*l['unit_cents'] for l in rows),'status':'new','created_at':now(),'origin':'manual','note':text(b.get('note',''),'备注',2000,True)}
        return self.write(c,d,True)

    def move(self,c,pid,wid,delta,reserved,reference,reason):
        c.execute('INSERT OR IGNORE INTO ops_stock(product_id,warehouse_id) VALUES(?,?)',(pid,wid))
        s=c.execute('SELECT * FROM ops_stock WHERE product_id=? AND warehouse_id=?',(pid,wid)).fetchone()
        hand=s['on_hand']+delta; held=s['reserved']+reserved
        if held<0 or hand<held: raise Problem('可用库存不足或已被其他订单占用，整笔操作未执行',409)
        c.execute('UPDATE ops_stock SET on_hand=?,reserved=? WHERE product_id=? AND warehouse_id=?',(hand,held,pid,wid))
        c.execute('INSERT INTO ops_movements VALUES(?,?,?,?,?,?,?,?)',(ident(),pid,wid,delta,reserved,reference,reason,now()))

    def adjust(self,c,b):
        pid,_=self.product_row(c,b)
        wid=self.entity_id(c,b.get('warehouse_id'),'warehouse'); amount=qty(b.get('quantity')); direction=b.get('direction')
        if direction not in ('in','out'): raise Problem('库存方向无效')
        reason=text(b.get('reason'),'调整原因',500)
        mid=ident(); self.move(c,pid,wid,amount if direction=='in' else -amount,0,mid,'盘点调整：'+reason)
        return {'id':mid}

    def receive(self,c,b):
        d=self.document(c,b,'purchase')
        if d['status'] not in ('open','partial'): raise Problem('当前采购单不能入库',409)
        rows=b.get('quantities')
        if not isinstance(rows,dict) or set(rows)-{l['product_id'] for l in d['lines']}: raise Problem('入库商品不属于该采购单')
        positive=False
        for l in d['lines']:
            n=qty(rows.get(l['product_id'],0),True)
            if n>l['quantity']-l['received']: raise Problem('入库数量超过待收数量')
            if n:
                self.move(c,l['product_id'],d['warehouse_id'],n,0,d['id'],'采购收货'); l['received']+=n; positive=True
        if not positive: raise Problem('请填写至少一个入库数量')
        d['status']='received' if all(l['received']==l['quantity'] for l in d['lines']) else 'partial'
        return self.write(c,d)

    def reserve(self,c,b):
        d=self.document(c,b,'order')
        if d['status']!='new': raise Problem('仅待处理订单可占用库存',409)
        for l in d['lines']: self.move(c,l['product_id'],d['warehouse_id'],0,l['quantity'],d['id'],'订单占用')
        d['status']='reserved'; return self.write(c,d)

    def ship(self,c,b):
        d=self.document(c,b,'order')
        if d['status']!='reserved': raise Problem('请先占用库存再发货',409)
        carrier=text(b.get('carrier'),'承运商'); tracking=text(b.get('tracking'),'运单号')
        for l in d['lines']: self.move(c,l['product_id'],d['warehouse_id'],-l['quantity'],-l['quantity'],d['id'],'发货出库')
        d.update(status='shipped',carrier=carrier,tracking=tracking,shipped_at=now()); return self.write(c,d)

    def deliver(self,c,b):
        d=self.document(c,b,'order')
        if d['status']!='shipped': raise Problem('仅已发货订单可以确认签收',409)
        d.update(status='delivered',delivered_at=now(),delivery_evidence=text(b.get('evidence'),'签收核对依据',1000))
        return self.write(c,d)

    def cancel(self,c,b):
        d=self.document(c,b)
        allowed=('open','partial') if d['kind']=='purchase' else ('new','reserved')
        if d['status'] not in allowed: raise Problem('当前状态不能取消；已发货订单请走退货',409)
        if d['kind']=='order' and d['status']=='reserved':
            for l in d['lines']: self.move(c,l['product_id'],d['warehouse_id'],0,-l['quantity'],d['id'],'取消释放')
        d.update(status='cancelled',cancel_reason=text(b.get('reason'),'取消原因',500)); return self.write(c,d)

    def return_order(self,c,b):
        d=self.document(c,b,'order')
        if d['status'] not in ('shipped','delivered'): raise Problem('仅已出库订单能记录退货',409)
        rows=b.get('quantities'); reason=text(b.get('reason'),'退货原因',500)
        if not isinstance(rows,dict) or set(rows)-{l['product_id'] for l in d['lines']}: raise Problem('退货商品不属于订单')
        if not isinstance(b.get('restock'),bool): raise Problem('请选择是否恢复可用库存')
        positive=False; returns=[]
        for l in d['lines']:
            n=qty(rows.get(l['product_id'],0),True)
            if n>l['quantity']-l['returned']: raise Problem('退货数量超过尚未退回数量')
            if n:
                if b['restock']: self.move(c,l['product_id'],d['warehouse_id'],n,0,d['id'],'退货验收：'+reason)
                l['returned']+=n; positive=True; returns.append({'product_id':l['product_id'],'quantity':n})
        if not positive: raise Problem('请填写退货数量')
        d.setdefault('returns',[]).append({'at':now(),'reason':reason,'restock':b['restock'],'lines':returns})
        return self.write(c,d)

    def state(self,surface=None,page=0):
        if isinstance(page,bool) or not str(page).isdecimal() or not 0<=int(page)<=1000000:
            raise Problem('单据页码无效')
        page=int(page)
        with self.store.connect() as c:
            # A single read transaction prevents mixing balances and documents across a write.
            c.execute('BEGIN')
            entities=[json.loads(r['data']) for r in c.execute('SELECT data FROM ops_entities ORDER BY kind,name')]
            kind={'orders':'order','purchases':'purchase','warehouse':'transfer'}.get(surface)
            document_page=None
            if kind:
                total=c.execute('SELECT count(*) FROM ops_documents WHERE kind=?',(kind,)).fetchone()[0]
                pages=max(1,(total+49)//50);page=min(page,pages-1)
                docs=[json.loads(r['data']) for r in c.execute('SELECT data FROM ops_documents WHERE kind=? ORDER BY updated_at DESC,id DESC LIMIT 50 OFFSET ?',(kind,page*50))]
                document_page={'kind':kind,'page':page,'pages':pages,'total':total}
            elif surface in ('overview','partners','inventory'):
                docs=[]
            else:
                docs=[json.loads(r['data']) for r in c.execute('SELECT data FROM ops_documents ORDER BY updated_at DESC,id DESC')]
            summary=None
            if surface=='overview':
                summary={'pending_orders':c.execute("SELECT count(*) FROM ops_documents WHERE kind='order' AND json_extract(data,'$.status')='new'").fetchone()[0],
                         'pending_purchases':c.execute("SELECT count(*) FROM ops_documents WHERE kind='purchase' AND json_extract(data,'$.status') IN ('open','partial')").fetchone()[0],
                         'reserved_units':c.execute('SELECT coalesce(sum(reserved),0) FROM ops_stock').fetchone()[0]}
            stock=[dict(r) for r in c.execute('SELECT *,on_hand-reserved available FROM ops_stock ORDER BY warehouse_id,product_id')] if surface in (None,'inventory') else []
            moves=[dict(r) for r in c.execute('SELECT * FROM ops_movements ORDER BY created_at DESC,id DESC LIMIT 200')] if surface in (None,'overview','inventory') else []
            replenishment=self.warehouse.plan(c) if surface in (None,'warehouse') else []
            return {'surface':surface,'entities':entities,'documents':docs,'document_page':document_page,'summary':summary,'stock':stock,'movements':moves,'replenishment':replenishment}
