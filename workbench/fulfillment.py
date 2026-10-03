"""Local, confirmed warehouse waves backed by the existing stock ledger."""
import csv
import hashlib
import io
import json
from datetime import datetime, timezone
from core import Problem, ident, now
from operations import text

SCHEMA_SQL = '''
CREATE TABLE IF NOT EXISTS fulfillment_previews (
 token TEXT PRIMARY KEY, payload TEXT NOT NULL, snapshot TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fulfillment_waves (
 id TEXT PRIMARY KEY, token TEXT NOT NULL UNIQUE, data TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_fulfillment_waves_time ON fulfillment_waves(created_at DESC,id DESC);
CREATE TABLE IF NOT EXISTS fulfillment_requests (
 request_id TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT NOT NULL);
'''

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def tracking_key(carrier, tracking):
    return [carrier.casefold(),tracking.casefold()]

def csv_cell(value):
    value=str(value if value is not None else '')
    # Spreadsheet formula detection also ignores leading whitespace/control characters.
    if value.lstrip().startswith(('=','+','-','@')) or value.startswith(('\t','\r','\n')): value="'"+value
    return value

class Fulfillment:
    def __init__(self,app):
        self.app=app;self.store=app.store;self.ops=app.ops
        with self.store.connect() as c:c.executescript(SCHEMA_SQL)

    def state(self,page=0):
        if isinstance(page,bool) or not str(page).isdecimal() or not 0<=int(page)<=1000000:raise Problem('履约页码无效')
        with self.store.connect() as c:
            c.execute('BEGIN')
            where="kind='order' AND json_extract(data,'$.status') IN ('new','reserved')"
            total=c.execute('SELECT count(*) FROM ops_documents WHERE '+where).fetchone()[0]
            pages=max(1,(total+49)//50);page=min(int(page),pages-1)
            orders=[json.loads(r['data']) for r in c.execute('SELECT data FROM ops_documents WHERE '+where+' ORDER BY updated_at DESC,id DESC LIMIT 50 OFFSET ?',(page*50,))]
            entities=[json.loads(r['data']) for r in c.execute("SELECT data FROM ops_entities WHERE kind IN ('shop','warehouse') ORDER BY kind,name")]
            waves=[json.loads(r['data']) for r in c.execute('SELECT data FROM fulfillment_waves ORDER BY created_at DESC,id DESC LIMIT 20')]
            wave_total=c.execute('SELECT count(*) FROM fulfillment_waves').fetchone()[0]
            return {'orders':orders,'page':page,'pages':pages,'total':total,'warehouses':[e for e in entities if e['kind']=='warehouse'],'shops':[e for e in entities if e['kind']=='shop'],'waves':waves,'wave_total':wave_total,'max_orders':100}

    def tracking_orders(self,c,shipments):
        if not shipments:return []
        keys={tuple(tracking_key(s['carrier'],s['tracking'])) for s in shipments}
        matches={key:[] for key in keys}
        # Read tracking identity only, once per batch; Python casefold handles Unicode
        # consistently (SQLite lower() only handles ASCII).
        for row in c.execute("SELECT id,revision,json_extract(data,'$.carrier') carrier,json_extract(data,'$.tracking') tracking FROM ops_documents WHERE kind='order' AND json_extract(data,'$.tracking') IS NOT NULL ORDER BY id"):
            key=tuple(tracking_key(str(row['carrier'] or '').strip(),str(row['tracking'] or '').strip()))
            if key in matches:matches[key].append(dict(row))
        return [[list(key),matches[key]] for key in sorted(keys)]

    def snapshot(self,c,payload):
        orders=[dict(r) if r else None for oid in payload['order_ids'] for r in [c.execute('SELECT id,data,revision FROM ops_documents WHERE id=?',(oid,)).fetchone()]]
        products=[];stock=[]
        for pick in payload['picks']:
            row=c.execute('SELECT id,data,revision FROM products WHERE id=?',(pick['product_id'],)).fetchone()
            products.append(dict(row) if row else None)
            row=c.execute('SELECT * FROM ops_stock WHERE product_id=? AND warehouse_id=?',(pick['product_id'],pick['warehouse_id'])).fetchone()
            stock.append(dict(row) if row else None)
        return digest([orders,products,stock,self.tracking_orders(c,payload['shipments'])])

    def preview(self,b):
        if not isinstance(b,dict) or b.get('action') not in ('reserve','ship'):raise Problem('请选择批量占用或发货')
        ids=b.get('order_ids')
        if not isinstance(ids,list) or not 1<=len(ids)<=100 or any(not isinstance(i,str) or not i or len(i)>100 for i in ids) or len(set(ids))!=len(ids):raise Problem('请选择1至100张不同订单')
        action=b['action'];shipments=b.get('shipments',[])
        if not isinstance(shipments,list) or len(shipments)>100 or any(not isinstance(s,dict) for s in shipments):raise Problem('发货资料格式无效')
        if action=='reserve' and shipments:raise Problem('占用库存时不需要发货资料')
        errors=[];shortages=[];orders=[];picks={};shipment_map={};seen_tracking=set()
        if action=='ship':
            for s in shipments:
                try:
                    oid=text(s.get('order_id'),'订单编号',100)
                    if oid not in ids or oid in shipment_map:raise Problem('发货资料包含未选择或重复订单')
                    carrier=text(s.get('carrier'),'承运商');tracking=text(s.get('tracking'),'运单号')
                    key=tuple(tracking_key(carrier,tracking))
                    if key in seen_tracking:raise Problem('多张订单不能共用同一承运商和运单号，请分别填写')
                    seen_tracking.add(key);shipment_map[oid]={'order_id':oid,'carrier':carrier,'tracking':tracking}
                except Problem as exc:errors.append({'order_id':s.get('order_id',''),'reason':str(exc)})
            for oid in ids:
                if oid not in shipment_map:errors.append({'order_id':oid,'reason':'请填写每张订单的承运商与运单号'})
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for oid in ids:
                row=c.execute("SELECT data FROM ops_documents WHERE id=? AND kind='order'",(oid,)).fetchone()
                if not row:errors.append({'order_id':oid,'reason':'订单不存在'});continue
                order=json.loads(row['data']);orders.append(order)
            orders.sort(key=lambda d:(d['created_at'],d['id']))
            remaining={}
            for order in orders:
                if order['status']!=('new' if action=='reserve' else 'reserved'):
                    errors.append({'order_id':order['id'],'external_id':order['external_id'],'reason':'仅待处理订单可占用库存' if action=='reserve' else '仅已占用库存订单可发货'});continue
                for line in order['lines']:
                    try:
                        _,product=self.ops.product_row(c,{'product_id':line['product_id']})
                        if product.get('partner_sku')!=line['sku']:
                            raise Problem('订单商品货号与当前商品档案不同，请先核对订单')
                    except Problem as exc:
                        errors.append({'order_id':order['id'],'external_id':order['external_id'],'reason':str(exc)});continue
                    key=(order['warehouse_id'],line['product_id'])
                    if key not in remaining:
                        stock=c.execute('SELECT on_hand,reserved FROM ops_stock WHERE warehouse_id=? AND product_id=?',key).fetchone()
                        on_hand=stock['on_hand'] if stock else 0;reserved=stock['reserved'] if stock else 0
                        remaining[key]=on_hand-reserved if action=='reserve' else reserved
                        picks[key]={'warehouse_id':key[0],'product_id':key[1],'sku':line['sku'],'title':product.get('title_zh',line['title']),'quantity':0,'on_hand':on_hand,'reserved':reserved,'available':on_hand-reserved,'order_ids':[]}
                    available=max(0,remaining[key]);quantity=line['quantity']
                    if available<quantity:
                        shortages.append({'order_id':order['id'],'external_id':order['external_id'],'warehouse_id':key[0],'product_id':key[1],'sku':line['sku'],'required':quantity,'available':available,'shortage':quantity-available})
                    remaining[key]-=quantity;picks[key]['quantity']+=quantity;picks[key]['order_ids'].append(order['id'])
            normalized_shipments=[shipment_map[oid] for oid in sorted(shipment_map)]
            for key,matches in self.tracking_orders(c,normalized_shipments):
                if matches:errors.append({'order_id':'','reason':'承运商和运单号已用于其他订单：'+key[0]+' / '+key[1]})
            payload={'action':action,'order_ids':[o['id'] for o in orders],'picks':[picks[k] for k in sorted(picks)],'shipments':normalized_shipments}
            token=None
            if not errors and not shortages:
                token=ident();c.execute('INSERT INTO fulfillment_previews VALUES(?,?,?,?)',(token,json.dumps(payload,ensure_ascii=False),self.snapshot(c,payload),now()))
            return {'action':action,'orders':orders,'picks':payload['picks'],'shortages':shortages,'errors':errors,'token':token,'can_apply':bool(token),'allocation':'按订单创建时间与编号分配库存'}

    def apply(self,b):
        if not isinstance(b,dict) or b.get('confirmed') is not True:raise Problem('请确认本次仓库操作')
        token=text(b.get('token',b.get('preview_token')),'预检编号',100);request=text(b.get('request_id'),'操作编号',100);fingerprint=digest([token])
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT * FROM fulfillment_requests WHERE request_id=?',(request,)).fetchone()
            if old:
                if old['digest']!=fingerprint:raise Problem('相同操作编号不能用于不同波次',409)
                return json.loads(old['result'])
            old=c.execute('SELECT data FROM fulfillment_waves WHERE token=?',(token,)).fetchone()
            if old:
                c.execute('INSERT INTO fulfillment_requests VALUES(?,?,?)',(request,fingerprint,old['data']));return json.loads(old['data'])
            row=c.execute('SELECT * FROM fulfillment_previews WHERE token=?',(token,)).fetchone()
            if not row:raise Problem('预检不存在，请重新预检',409)
            created=datetime.fromisoformat(row['created_at'].replace('Z','+00:00'))
            if (datetime.now(timezone.utc)-created.replace(tzinfo=created.tzinfo or timezone.utc)).total_seconds()>86400:raise Problem('预检已过期，请重新预检',409)
            payload=json.loads(row['payload'])
            if self.snapshot(c,payload)!=row['snapshot']:raise Problem('订单、商品、库存或运单资料已变化，请重新预检',409)
            shipments={s['order_id']:s for s in payload['shipments']};results=[]
            for oid in payload['order_ids']:
                row=c.execute('SELECT data FROM ops_documents WHERE id=?',(oid,)).fetchone();order=json.loads(row['data'])
                body={'id':oid,'revision':order['revision'],**shipments.get(oid,{})}
                updated=getattr(self.ops,payload['action'])(c,body)
                results.append({'id':oid,'external_id':updated['external_id'],'revision':updated['revision'],'status':updated['status'],'warehouse_id':updated['warehouse_id'],'shop_id':updated['shop_id'],**({k:updated[k] for k in ('carrier','tracking')} if payload['action']=='ship' else {})})
            wave={'id':ident(),'request_id':request,'action':payload['action'],'created_at':now(),'orders':results,'picks':payload['picks'],'order_count':len(results),'units':sum(p['quantity'] for p in payload['picks']),'local_only':True}
            encoded=json.dumps(wave,ensure_ascii=False)
            c.execute('INSERT INTO fulfillment_waves VALUES(?,?,?,?)',(wave['id'],token,encoded,wave['created_at']))
            c.execute('INSERT INTO fulfillment_requests VALUES(?,?,?)',(request,fingerprint,encoded))
            self.store.event(c,None,'仓库批量'+('占用' if payload['action']=='reserve' else '发货'),wave['id']+' · '+str(len(results))+'单')
            return wave

    def export(self,wave_id):
        wave_id=text(wave_id,'拣货单编号',100)
        with self.store.connect() as c:
            row=c.execute('SELECT data FROM fulfillment_waves WHERE id=?',(wave_id,)).fetchone()
            if not row:raise Problem('拣货单不存在',404)
            wave=json.loads(row['data']);entities={r['id']:r['name'] for r in c.execute("SELECT id,name FROM ops_entities WHERE kind='warehouse'")}
        external={o['id']:o['external_id'] for o in wave['orders']};out=io.StringIO(newline='');writer=csv.writer(out)
        writer.writerow(['本地波次','操作','仓库','工作台 SKU','商品','拣货数量','来源订单号','建立时间','说明'])
        for p in wave['picks']:
            writer.writerow([csv_cell(v) for v in [wave['id'],'库存占用' if wave['action']=='reserve' else '发货出库',entities.get(p['warehouse_id'],p['warehouse_id']),p['sku'],p['title'],p['quantity'],' / '.join(external[oid] for oid in p['order_ids']),wave['created_at'],'本地拣货清单；不是承运商电子面单，也不代表平台已发货']])
        return '\ufeff'+out.getvalue()
