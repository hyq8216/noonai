"""Versioned local packing/handoff records. Never calls a carrier or re-ships orders."""
import csv
import hashlib
import io
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from core import Problem, ident, now
from operations import text
from fulfillment import csv_cell

SCHEMA_SQL='''
CREATE TABLE IF NOT EXISTS shipping_manifests(id TEXT PRIMARY KEY,data TEXT NOT NULL,revision INTEGER NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_shipping_manifests_updated ON shipping_manifests(updated_at DESC,id);
CREATE TABLE IF NOT EXISTS shipping_manifest_orders(order_id TEXT PRIMARY KEY,manifest_id TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_shipping_manifest_orders_manifest ON shipping_manifest_orders(manifest_id);
CREATE TABLE IF NOT EXISTS shipping_manifest_previews(token TEXT PRIMARY KEY,payload TEXT NOT NULL,snapshot TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shipping_manifest_receipts(token TEXT PRIMARY KEY,result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shipping_manifest_requests(request_id TEXT PRIMARY KEY,digest TEXT NOT NULL,result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shipping_manifest_audit(id INTEGER PRIMARY KEY AUTOINCREMENT,manifest_id TEXT NOT NULL,revision INTEGER NOT NULL,action TEXT NOT NULL,data TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_shipping_manifest_audit_manifest ON shipping_manifest_audit(manifest_id,id DESC);
CREATE INDEX IF NOT EXISTS idx_shipping_manifest_source_movements ON ops_movements(product_id,warehouse_id);
'''
MEASURES={'weight_kg':'实测重量kg','length_cm':'长cm','width_cm':'宽cm','height_cm':'高cm'}

def digest(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def page_number(value):
    if isinstance(value,bool) or not str(value).isdecimal() or not 0<=int(value)<=1000000:raise Problem('物流清单页码无效')
    return int(value)
def measurement(value,label,weight=False):
    if value in ('',None):return None
    if isinstance(value,bool):raise Problem(label+'格式无效')
    try:n=Decimal(str(value))
    except InvalidOperation:raise Problem(label+'格式无效')
    if not n.is_finite() or not 0<n<=(100000 if weight else 10000) or n.as_tuple().exponent<(-3 if weight else -2):raise Problem(label+'须为正数，重量最多三位、尺寸最多两位小数')
    return format(n.normalize(),'f')

class ShippingManifests:
    def __init__(self,app):
        self.app=app;self.store=app.store;self.ops=app.ops
        with self.store.connect() as c:c.executescript(SCHEMA_SQL)

    def sources(self,c,ids):
        placeholders=','.join('?' for _ in ids)
        rows=[dict(r) for r in c.execute('SELECT id,data,revision FROM ops_documents WHERE id IN ('+placeholders+') ORDER BY id',ids)]
        pids=set();warehouses={}
        for row in rows:
            d=json.loads(row['data'])
            for line in d.get('lines',[]):
                pids.add(line['product_id']);warehouses.setdefault(d['warehouse_id'],set()).add(line['product_id'])
        products=[]
        pids=sorted(pids)
        for start in range(0,len(pids),400):
            chunk=pids[start:start+400];products.extend(dict(r) for r in c.execute('SELECT id,data,revision FROM products WHERE id IN ('+','.join('?' for _ in chunk)+') ORDER BY id',chunk))
        stocks=[];movements=[]
        for wid,products_in_warehouse in sorted(warehouses.items()):
            selected=sorted(products_in_warehouse)
            for start in range(0,len(selected),400):
                chunk=selected[start:start+400];where='warehouse_id=? AND product_id IN ('+','.join('?' for _ in chunk)+')';params=[wid,*chunk]
                stocks.extend(dict(r) for r in c.execute('SELECT * FROM ops_stock WHERE '+where+' ORDER BY product_id',params))
                movements.extend(dict(r) for r in c.execute('SELECT warehouse_id,product_id,count(*) movement_count,max(created_at) latest_movement FROM ops_movements WHERE '+where+' GROUP BY warehouse_id,product_id ORDER BY product_id',params))
        return {'orders':rows,'products':products,'stock':stocks,'movements':movements}

    def snapshot(self,c,payload):
        old=c.execute('SELECT data,revision FROM shipping_manifests WHERE id=?',(payload.get('id'),)).fetchone()
        owners=[dict(r) for r in c.execute('SELECT order_id,manifest_id FROM shipping_manifest_orders WHERE order_id IN ('+','.join('?' for _ in payload['order_ids'])+') ORDER BY order_id',payload['order_ids'])]
        return digest([self.sources(c,payload['order_ids']),dict(old) if old else None,owners])

    def missing(self,packages):
        return [{'order_id':p['order_id'],'box_no':p['box_no'],'fields':[MEASURES[k] for k in MEASURES if p.get(k) is None]} for p in packages if any(p.get(k) is None for k in MEASURES)]

    def unpack(self,c,row):
        d=json.loads(row['data']);d['source_changed']=digest(self.sources(c,d['order_ids']))!=d['source_fingerprint'];d['missing']=self.missing(d['packages'])
        d['audit']=[{'revision':r['revision'],'action':r['action'],'created_at':r['created_at']} for r in c.execute('SELECT revision,action,created_at FROM shipping_manifest_audit WHERE manifest_id=? ORDER BY id DESC LIMIT 20',(d['id'],))]
        return d

    def summary(self,c,row):
        d=json.loads(row['data']);stale=digest(self.sources(c,d['order_ids']))!=d['source_fingerprint'];missing=len(self.missing(d['packages']))
        warnings=[]
        if stale:warnings.append('来源订单、商品或库存已变化；草稿需重新预检，已交接记录保留历史版本')
        if missing:warnings.append(str(missing)+'个包裹的重量或尺寸待实测')
        return {'id':d['id'],'revision':d['revision'],'status':d['status'],'order_count':len(d['order_ids']),'package_count':len(d['packages']),
                'note':d['note'],'updated_at':d['updated_at'],'source_changed':stale,'source_stale':stale,'missing_count':missing,'warnings':warnings,
                **({'handoff':d['handoff']} if d.get('handoff') else {})}

    def get(self,mid):
        mid=text(mid,'清单编号',100)
        with self.store.connect() as c:
            c.execute('BEGIN');row=c.execute('SELECT * FROM shipping_manifests WHERE id=?',(mid,)).fetchone()
            if not row:raise Problem('物流清单不存在',404)
            return self.unpack(c,row)

    def state(self,page=0,query='',order_page=0):
        page=page_number(page);order_page=page_number(order_page);query=text(query,'搜索',200,True)
        with self.store.connect() as c:
            c.execute('BEGIN')
            params=[query];where="(?='' OR instr(lower(data),lower(?))>0)";params=[query,query]
            total=c.execute('SELECT count(*) FROM shipping_manifests WHERE '+where,params).fetchone()[0];pages=max(1,(total+49)//50);page=min(page,pages-1)
            manifests=[self.summary(c,r) for r in c.execute('SELECT * FROM shipping_manifests WHERE '+where+' ORDER BY updated_at DESC,id LIMIT 50 OFFSET ?',[*params,page*50])]
            ow="d.kind='order' AND json_extract(d.data,'$.status')='shipped' AND (?='' OR instr(lower(json_extract(d.data,'$.external_id')||' '||json_extract(d.data,'$.tracking')),lower(?))>0)"
            order_total=c.execute('SELECT count(*) FROM ops_documents d WHERE '+ow,params).fetchone()[0];order_pages=max(1,(order_total+49)//50);order_page=min(order_page,order_pages-1)
            orders=[]
            for r in c.execute('SELECT d.data,m.manifest_id FROM ops_documents d LEFT JOIN shipping_manifest_orders m ON m.order_id=d.id WHERE '+ow+' ORDER BY d.updated_at DESC,d.id LIMIT 50 OFFSET ?',[*params,order_page*50]):
                order=json.loads(r['data']);orders.append({key:order[key] for key in ('id','revision','external_id','carrier','tracking','warehouse_id','shop_id')}|{'manifest_id':r['manifest_id'],'line_count':len(order['lines']),'unit_count':sum(l['quantity'] for l in order['lines']),'skus':[l['sku'] for l in order['lines'][:3]]})
            entities=[dict(r) for r in c.execute("SELECT id,kind,name FROM ops_entities WHERE kind IN ('shop','warehouse') ORDER BY kind,name")]
            return {'manifests':manifests,'page':page,'pages':pages,'total':total,'orders':orders,'order_page':order_page,'order_pages':order_pages,'order_total':order_total,'query':query,'warehouses':[e for e in entities if e['kind']=='warehouse'],'shops':[e for e in entities if e['kind']=='shop'],'max_orders':100,'local_only':True}

    def preview(self,b):
        if not isinstance(b,dict):raise Problem('包装资料格式无效')
        ids=b.get('order_ids');packages=b.get('packages')
        if not isinstance(ids,list) or not 1<=len(ids)<=100 or any(not isinstance(i,str) or not i or len(i)>100 for i in ids) or len(set(ids))!=len(ids):raise Problem('请选择1至100张不同的已发货订单')
        if not isinstance(packages,list) or len(packages)!=len(ids) or any(not isinstance(p,dict) for p in packages):raise Problem('每张订单需填写一个独立包裹，不拆合原订单')
        note=text(b.get('note',''),'备注',2000,True);errors=[];normalized=[];boxes=set();seen=set()
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            mid=b.get('id');old=None
            if mid:
                mid=text(mid,'清单编号',100);row=c.execute('SELECT data,revision FROM shipping_manifests WHERE id=?',(mid,)).fetchone()
                if not row:raise Problem('物流清单不存在',404)
                old=json.loads(row['data'])
                if type(b.get('revision')) is not int or b['revision']!=row['revision']:raise Problem('清单版本已变化，请重新读取',409)
                if old['status']!='draft':raise Problem('已登记交接的清单不可修订，请保留原交接记录',409)
            source=self.sources(c,ids);orders={r['id']:json.loads(r['data']) for r in source['orders']};products={r['id']:json.loads(r['data']) for r in source['products']}
            for raw in packages:
                try:
                    oid=text(raw.get('order_id'),'订单编号',100)
                    if oid not in ids or oid in seen:raise Problem('包裹订单未选择或重复')
                    seen.add(oid);order=orders.get(oid)
                    if not order or order.get('kind')!='order' or order.get('status')!='shipped':raise Problem('仅已发货订单可建立包装清单')
                    owner=c.execute('SELECT manifest_id FROM shipping_manifest_orders WHERE order_id=?',(oid,)).fetchone()
                    if owner and owner['manifest_id']!=mid:raise Problem('订单已登记在其他物流清单，未重复登记')
                    carrier=text(raw.get('carrier'),'承运商');tracking=text(raw.get('tracking'),'运单号')
                    if [carrier,tracking]!=[order.get('carrier'),order.get('tracking')]:raise Problem('承运商或运单号与发货订单不一致，请先核对来源订单')
                    box=text(raw.get('box_no'),'箱号',100)
                    if box.casefold() in boxes:raise Problem('同一清单的箱号不能重复')
                    boxes.add(box.casefold());lines=[]
                    for line in order['lines']:
                        product=products.get(line['product_id'])
                        if not product or product.get('demo') or product.get('partner_sku')!=line['sku']:raise Problem('订单商品缺失、为示例或货号发生变化，请核对来源订单')
                        lines.append({'product_id':line['product_id'],'sku':line['sku'],'title':product.get('title_zh',line['title']),'quantity':line['quantity']})
                    normalized.append({'order_id':oid,'external_id':order['external_id'],'warehouse_id':order['warehouse_id'],'shop_id':order['shop_id'],'carrier':carrier,'tracking':tracking,'box_no':box,'lines':lines,**{key:measurement(raw.get(key),MEASURES[key],key=='weight_kg') for key in MEASURES}})
                except Problem as exc:errors.append({'order_id':raw.get('order_id',''),'reason':str(exc)})
            if seen!=set(ids):errors.append({'order_id':'','reason':'包裹没有完整对应所有选择订单'})
            normalized.sort(key=lambda p:(p['box_no'],p['order_id']));payload={'id':mid,'revision':old['revision'] if old else 0,'order_ids':sorted(ids),'packages':normalized,'note':note}
            token=None
            if not errors:
                token=ident();c.execute('INSERT INTO shipping_manifest_previews VALUES(?,?,?,?)',(token,json.dumps(payload,ensure_ascii=False),self.snapshot(c,payload),now()))
            missing=self.missing(normalized)
            return {'token':token,'can_apply':bool(token),'packages':normalized,'errors':errors,'missing':missing,'warnings':['重量或尺寸待实测；可保存草稿，资料完整后才可确认交接'] if missing else [],'id':mid,'revision':payload['revision'],'local_only':True}

    def request(self,c,key,fingerprint):
        old=c.execute('SELECT * FROM shipping_manifest_requests WHERE request_id=?',(key,)).fetchone()
        if old:
            if old['digest']!=fingerprint:raise Problem('同一操作编号不能用于不同物流操作',409)
            return json.loads(old['result'])

    def save(self,c,d,action):
        d['updated_at']=now();encoded=json.dumps(d,ensure_ascii=False)
        c.execute('INSERT INTO shipping_manifests VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data,revision=excluded.revision,updated_at=excluded.updated_at',(d['id'],encoded,d['revision'],d['updated_at']))
        c.execute('INSERT INTO shipping_manifest_audit(manifest_id,revision,action,data,created_at) VALUES(?,?,?,?,?)',(d['id'],d['revision'],action,encoded,d['updated_at']))
        self.store.event(c,None,'物流清单 · '+action,d['id'])
        return encoded

    def apply(self,b):
        if not isinstance(b,dict) or b.get('confirmed') is not True:raise Problem('请确认包装资料仅保存为本地清单')
        token=text(b.get('token',b.get('preview_token')),'预检编号',100);request=text(b.get('request_id'),'操作编号',100);fingerprint=digest(['apply',token])
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE');old=self.request(c,request,fingerprint)
            if old:return old
            receipt=c.execute('SELECT result FROM shipping_manifest_receipts WHERE token=?',(token,)).fetchone()
            if receipt:
                c.execute('INSERT INTO shipping_manifest_requests VALUES(?,?,?)',(request,fingerprint,receipt['result']));return json.loads(receipt['result'])
            row=c.execute('SELECT * FROM shipping_manifest_previews WHERE token=?',(token,)).fetchone()
            if not row:raise Problem('预检不存在，请重新预检',409)
            created=datetime.fromisoformat(row['created_at'].replace('Z','+00:00'))
            if (datetime.now(timezone.utc)-created.replace(tzinfo=created.tzinfo or timezone.utc)).total_seconds()>86400:raise Problem('预检已过期，请重新预检',409)
            payload=json.loads(row['payload'])
            if self.snapshot(c,payload)!=row['snapshot']:raise Problem('清单、来源订单、商品或库存已变化，请重新预检',409)
            d={**payload,'id':payload['id'] or ident(),'revision':payload['revision']+1,'status':'draft','source_fingerprint':digest(self.sources(c,payload['order_ids'])),'created_at':now(),'request_id':request,'local_only':True}
            if payload['id']:
                previous=json.loads(c.execute('SELECT data FROM shipping_manifests WHERE id=?',(payload['id'],)).fetchone()[0]);d['created_at']=previous['created_at']
            c.execute('DELETE FROM shipping_manifest_orders WHERE manifest_id=?',(d['id'],))
            for oid in d['order_ids']:c.execute('INSERT INTO shipping_manifest_orders VALUES(?,?)',(oid,d['id']))
            encoded=self.save(c,d,'修订' if payload['id'] else '建立')
            c.execute('INSERT INTO shipping_manifest_receipts VALUES(?,?)',(token,encoded));c.execute('INSERT INTO shipping_manifest_requests VALUES(?,?,?)',(request,fingerprint,encoded));return d

    def handoff(self,b):
        if not isinstance(b,dict) or b.get('confirmed') is not True:raise Problem('请确认交接实际依据，此记录不代表签收')
        request=text(b.get('request_id'),'操作编号',100);mid=text(b.get('id'),'清单编号',100);fingerprint=digest(['handoff',b])
        recipient=text(b.get('recipient'),'交接接收人',250);evidence=text(b.get('evidence'),'实际交接依据',1500)
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE');old=self.request(c,request,fingerprint)
            if old:return old
            row=c.execute('SELECT data,revision FROM shipping_manifests WHERE id=?',(mid,)).fetchone()
            if not row:raise Problem('物流清单不存在',404)
            d=json.loads(row['data'])
            if type(b.get('revision')) is not int or b['revision']!=row['revision']:raise Problem('清单版本已变化，请刷新后核对',409)
            if d['status']!='draft':raise Problem('清单已经登记交接，不重复登记',409)
            if digest(self.sources(c,d['order_ids']))!=d['source_fingerprint']:raise Problem('来源订单、商品或库存已变化，请重新预检修订清单',409)
            if self.missing(d['packages']):raise Problem('包裹重量或尺寸尚未实测，请补齐后交接')
            d.update(status='handed_off',revision=d['revision']+1,handoff={'recipient':recipient,'evidence':evidence,'at':now()})
            encoded=self.save(c,d,'登记交接');c.execute('INSERT INTO shipping_manifest_requests VALUES(?,?,?)',(request,fingerprint,encoded));return d

    def export(self,mid,kind='packages'):
        mid=text(mid,'清单编号',100)
        if kind not in ('packages','sku','handoff'):raise Problem('清单导出类型无效')
        with self.store.connect() as c:
            row=c.execute('SELECT * FROM shipping_manifests WHERE id=?',(mid,)).fetchone()
            if not row:raise Problem('物流清单不存在',404)
            d=self.unpack(c,row)
        if d['source_changed'] and d['status']=='draft':raise Problem('来源资料已变化，请重新预检修订后导出',409)
        out=io.StringIO(newline='');writer=csv.writer(out)
        notice='本地版本记录；不是承运商电子面单，不代表平台发货或买家签收'+('；当前来源已变化，以下为已登记交接时的历史版本' if d['source_changed'] else '')
        common=[d['id'],d['revision'],'已登记本地交接' if d['status']=='handed_off' else '包装草稿']
        if kind=='sku':
            writer.writerow(['清单','版本','状态','箱号','来源订单号','SKU','商品','包装数量','说明'])
            for p in d['packages']:
                for line in p['lines']:writer.writerow([csv_cell(v) for v in [*common,p['box_no'],p['external_id'],line['sku'],line['title'],line['quantity'],notice]])
        else:
            writer.writerow(['清单','版本','状态','箱号','来源订单号','承运商','运单号','实测重量kg','长cm','宽cm','高cm','交接接收人','实际交接依据','交接时间','说明'])
            h=d.get('handoff',{})
            for p in d['packages']:writer.writerow([csv_cell(v) for v in [*common,p['box_no'],p['external_id'],p['carrier'],p['tracking'],*[p[k] if p[k] is not None else '待实测' for k in MEASURES],h.get('recipient','未登记'),h.get('evidence','未登记'),h.get('at','未登记'),notice]])
        return '\ufeff'+out.getvalue()
