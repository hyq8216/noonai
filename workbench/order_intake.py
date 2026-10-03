"""Confirmed local order-file intake; shares the operations ledger transaction."""
import csv
import hashlib
import io
import json
from datetime import datetime, timezone
from core import Problem, ident, now
from operations import cents, qty, text
from import_profiles import ImportProfiles

SCHEMA_SQL = '''
CREATE TABLE IF NOT EXISTS order_intake_previews (
 token TEXT PRIMARY KEY, payload TEXT NOT NULL, snapshot TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS order_intake_receipts (
 request_id TEXT PRIMARY KEY, digest TEXT NOT NULL, token TEXT NOT NULL UNIQUE,
 result TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS order_intake_requests (
 request_id TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_order_intake_receipts_time ON order_intake_receipts(created_at DESC,request_id DESC);
'''
FIELDS = {'external_id':'来源订单号','partner_sku':'工作台 SKU','quantity':'数量','unit_price':'单价','currency':'币种','order_total':'订单总额（可选）','order_date':'下单时间（可选）','note':'备注（可选）'}
ALIASES = {'external_id':['order_id','order_number','订单号','来源订单号'], 'partner_sku':['sku','SKU','工作台 SKU','货号'], 'quantity':['qty','数量'], 'unit_price':['price','单价'], 'currency':['币种'], 'order_total':['total','订单总额','订单总额（可选）'], 'order_date':['date','下单时间','下单时间（可选）'], 'note':['备注','备注（可选）']}

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def norm(value):
    return ''.join(value.lower().split()).replace('_','')

class OrderIntake:
    def __init__(self, app):
        self.app=app; self.store=app.store; self.ops=app.ops
        self.profiles=getattr(app,'import_profiles',None) or ImportProfiles(app)
        with self.store.connect() as c: c.executescript(SCHEMA_SQL)

    def template(self):
        out=io.StringIO(newline=''); writer=csv.writer(out)
        writer.writerow(list(FIELDS)); writer.writerow(['ORDER-001','YOUR-SKU',1,'19.90','SAR','19.90','2026-10-03',''])
        return '\ufeff'+out.getvalue()

    def state(self, page=0):
        if isinstance(page,bool) or not str(page).isdecimal() or not 0<=int(page)<=1000000: raise Problem('导入历史页码无效')
        with self.store.connect() as c:
            c.execute('BEGIN')
            total=c.execute('SELECT count(*) FROM order_intake_receipts').fetchone()[0]
            pages=max(1,(total+19)//20); page=min(int(page),pages-1)
            receipts=[json.loads(r['result']) for r in c.execute('SELECT result FROM order_intake_receipts ORDER BY created_at DESC,request_id DESC LIMIT 20 OFFSET ?',(page*20,))]
            entities=[json.loads(r['data']) for r in c.execute("SELECT data FROM ops_entities WHERE kind IN ('shop','warehouse') ORDER BY kind,name")]
            return {'profiles':self.profiles.choices(c,'order'),'receipts':receipts,'page':page,'pages':pages,'total':total,'entities':entities,'fields':FIELDS,'max_rows':5000}

    def parse(self, b):
        if not isinstance(b,dict): raise Problem('导入资料格式无效')
        if ('csv' in b)+('rows' in b)+('json' in b) != 1: raise Problem('请选择一种 CSV 或 JSON 订单资料')
        if 'csv' in b:
            raw=b['csv']
            if not isinstance(raw,str) or len(raw.encode())>8*1024*1024: raise Problem('文件需在8MB以内')
            try:
                reader=csv.reader(io.StringIO(raw.lstrip('\ufeff')),strict=True)
                columns=next(reader); rows=[]
                for number, values in enumerate(reader,2):
                    if not any(v.strip() for v in values): continue
                    rows.append((number,values))
                    if len(rows)>5000: raise Problem('每批最多5000行，请拆分文件')
            except (csv.Error,StopIteration): raise Problem('CSV无效或缺少表头，请使用UTF-8 CSV')
        else:
            raw=b.get('rows',b.get('json'))
            if isinstance(raw,str):
                if len(raw.encode())>8*1024*1024: raise Problem('文件需在8MB以内')
                try: raw=json.loads(raw.lstrip('\ufeff'))
                except ValueError: raise Problem('JSON格式无效')
            if isinstance(raw,dict): raw=raw.get('rows')
            if not isinstance(raw,list) or not 1<=len(raw)<=5000 or any(not isinstance(r,dict) for r in raw): raise Problem('JSON需包含1至5000个订单商品行对象')
            if len(json.dumps(raw,ensure_ascii=False).encode())>8*1024*1024: raise Problem('文件需在8MB以内')
            columns=list(dict.fromkeys(k for r in raw for k in r)); rows=[(i+1,[r.get(k,'') for k in columns]) for i,r in enumerate(raw)]
        if not rows: raise Problem('文件中没有订单商品行')
        if not 1<=len(columns)<=80 or any(not isinstance(k,str) or len(k)>200 for k in columns): raise Problem('表头需为1至80列，每列最多200字')
        columns=[k.strip() for k in columns]
        if len(set(columns))!=len(columns): raise Problem('表头不能重复，请先修正文件')
        mapping=b.get('mapping')
        if mapping is None:
            mapping={}
            for field in FIELDS:
                names={norm(field),*(norm(a) for a in ALIASES[field])}
                matches=[i for i,k in enumerate(columns) if norm(k) in names]
                if len(matches)==1: mapping[field]=matches[0]
        if not isinstance(mapping,dict) or any(k not in FIELDS or type(v) is not int or not 0<=v<len(columns) for k,v in mapping.items()) or len(set(mapping.values()))!=len(mapping): raise Problem('列对应关系无效')
        return columns,rows,mapping

    def snapshot(self,c,payload):
        products=[]
        for sku in sorted(payload['skus']):
            products.append([sku,[dict(r) for r in c.execute("SELECT id,data,revision FROM products WHERE json_extract(data,'$.partner_sku')=? ORDER BY id",(sku,))]])
        orders=[]
        for key in sorted(payload['keys']):
            row=c.execute('SELECT id,data,revision FROM ops_documents WHERE external_key=?',(key,)).fetchone()
            orders.append([key,dict(row) if row else None])
        entities=[dict(r) for r in c.execute('SELECT id,kind,data FROM ops_entities WHERE id IN (?,?) ORDER BY id',(payload['shop_id'],payload['warehouse_id']))]
        return digest([products,orders,entities])

    def signature(self,d):
        return {'warehouse_id':d['warehouse_id'],'currency':d['currency'],'total_cents':d['total_cents'],
                'lines':sorted([[l['product_id'],l['quantity'],l['unit_cents']] for l in d['lines']]),
                'order_date':d.get('order_date',''),'note':d.get('note','')}

    def preview(self,b):
        if not isinstance(b,dict):raise Problem('导入资料格式无效')
        errors=[];groups={};skus=set(); keys=set()
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            mapped,profile_fact,transformations=self.profiles.apply(c,'order',b)
            columns,source,mapping=self.parse(mapped)
            shop=self.ops.entity_id(c,b.get('shop_id'),'shop'); warehouse=self.ops.entity_id(c,b.get('warehouse_id'),'warehouse')
            missing=[FIELDS[k] for k in ('external_id','partner_sku','quantity','unit_price','currency') if k not in mapping]
            if missing: errors.append({'row':0,'external_id':'','reason':'请对应必填列：'+'、'.join(missing)})
            product_cache={}
            for number,values in source:
                external=''
                try:
                    if len(values)!=len(columns): raise Problem('商品行列数与表头不一致')
                    raw={k:values[v] for k,v in mapping.items()}
                    external=text(raw.get('external_id'),'来源订单号')
                    key=json.dumps([shop,external]); keys.add(key)
                    if transformations and transformations[number-1]['problems']:
                        raise Problem('；'.join(transformations[number-1]['problems']))
                    sku=text(raw.get('partner_sku'),'工作台 SKU'); skus.add(sku)
                    if sku not in product_cache:
                        product_cache[sku]=c.execute("SELECT id,data FROM products WHERE json_extract(data,'$.partner_sku')=? LIMIT 2",(sku,)).fetchall()
                    matches=product_cache[sku]
                    if not matches: raise Problem('未映射商品：'+sku)
                    if len(matches)>1: raise Problem('货号冲突：'+sku+'对应多个商品')
                    product=json.loads(matches[0]['data'])
                    if product.get('demo'): raise Problem('示例商品不能用于订单')
                    quantity=qty(raw.get('quantity')); unit=cents(raw.get('unit_price'))
                    currency=text(raw.get('currency'),'币种').upper()
                    if currency not in ('SAR','AED','USD','CNY'): raise Problem('币种须为SAR、AED、USD或CNY')
                    total=None if raw.get('order_total') in ('',None) else cents(raw['order_total'])
                    order_date=raw.get('order_date','')
                    if order_date not in ('',None):
                        order_date=text(order_date,'下单时间',80)
                        try:
                            parsed=datetime.fromisoformat(order_date.replace('Z','+00:00'))
                            order_date=parsed.isoformat()
                        except ValueError: raise Problem('下单时间无效，请使用YYYY-MM-DD或ISO时间')
                    else: order_date=''
                    note=text(raw.get('note','') or '','备注',2000,True)
                    group=groups.setdefault(external,{'external_id':external,'shop_id':shop,'warehouse_id':warehouse,'currency':currency,'order_date':order_date,'note':note,'declared_total':total,'lines':{},'source_rows':[]})
                    if [group['currency'],group['order_date'],group['note'],group['declared_total']] != [currency,order_date,note,total]: raise Problem('同一订单的币种、总额、时间和备注必须一致')
                    pid=matches[0]['id']; line=group['lines'].get(pid)
                    if line and line['unit_cents']!=unit: raise Problem('同一订单同一商品单价不同，请先核对')
                    if line:
                        line['quantity']=qty(line['quantity']+quantity)
                    else: group['lines'][pid]={'product_id':pid,'sku':sku,'title':product.get('title_zh',''),'quantity':quantity,'unit_cents':unit}
                    group['source_rows'].append(number)
                except Problem as exc: errors.append({'row':number,'external_id':external,'reason':str(exc)})
            orders=[]
            for g in groups.values():
                g['lines']=list(g['lines'].values());g['total_cents']=sum(l['quantity']*l['unit_cents'] for l in g['lines']);g['status']='ready'
                reason=''
                if len(g['lines'])>100: reason='每单最多100种商品'
                if g['declared_total'] is not None and g['declared_total']!=g['total_cents']: reason='订单总额与商品数量×单价合计不一致'
                old=c.execute('SELECT data FROM ops_documents WHERE external_key=?',(json.dumps([shop,g['external_id']]),)).fetchone()
                if old:
                    g['status']='duplicate' if self.signature(json.loads(old['data']))==self.signature(g) else 'conflict'
                    if g['status']=='conflict': reason='已有同店铺订单号，但商品、数量、金额或来源资料不同'
                if reason:
                    g['status']='blocked'; errors.append({'row':g['source_rows'][0] if g['source_rows'] else 0,'external_id':g['external_id'],'reason':reason})
                orders.append(g)
            row_errors={}
            for error in errors:row_errors.setdefault(error['row'],[]).append(error['reason'])
            for change in transformations:
                for reason in row_errors.get(change['line'],[]):
                    if reason not in change['problems']:change['problems'].append(reason)
            payload={'profile_fact':profile_fact,'transformations':transformations,'orders':orders,'skus':sorted(skus),'keys':sorted(keys),'shop_id':shop,'warehouse_id':warehouse,'row_count':len(source)}
            ready=sum(o['status']=='ready' for o in orders); duplicates=sum(o['status']=='duplicate' for o in orders)
            token=None
            if not errors and orders:
                token=ident(); c.execute('INSERT INTO order_intake_previews VALUES(?,?,?,?)',(token,json.dumps(payload,ensure_ascii=False),self.snapshot(c,payload),now()))
            return {'profile_fact':profile_fact,'transformations':transformations,'columns':columns,'mapping':mapping,'fields':FIELDS,'rows':orders,'errors':errors,'row_count':len(source),'ready':ready,'duplicates':duplicates,'blocked':len(errors),'token':token,'can_apply':bool(token)}

    def apply(self,b):
        if not isinstance(b,dict) or b.get('confirmed') is not True: raise Problem('请先确认订单预检结果')
        token=text(b.get('token',b.get('preview_token')),'预检编号',100); request=text(b.get('request_id'),'操作编号',100)
        fingerprint=digest([token])
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT digest,result FROM order_intake_requests WHERE request_id=?',(request,)).fetchone()
            if old:
                if old['digest']!=fingerprint: raise Problem('同一操作编号不能用于不同导入',409)
                return json.loads(old['result'])
            old=c.execute('SELECT result FROM order_intake_receipts WHERE token=?',(token,)).fetchone()
            if old:
                c.execute('INSERT INTO order_intake_requests VALUES(?,?,?)',(request,fingerprint,old['result']))
                return json.loads(old['result'])
            preview=c.execute('SELECT * FROM order_intake_previews WHERE token=?',(token,)).fetchone()
            if not preview: raise Problem('预检不存在，请重新预检',409)
            created=datetime.fromisoformat(preview['created_at'].replace('Z','+00:00'))
            if (datetime.now(timezone.utc)-created.replace(tzinfo=created.tzinfo or timezone.utc)).total_seconds()>86400: raise Problem('预检已过期，请重新预检',409)
            payload=json.loads(preview['payload'])
            self.profiles.guard(c,payload.get('profile_fact'))
            if self.snapshot(c,payload)!=preview['snapshot']: raise Problem('商品、店铺、仓库或订单资料已变化，请重新预检',409)
            results=[]
            for order in payload['orders']:
                if order['status']=='duplicate': continue
                body={**order,'lines':[{'product_id':l['product_id'],'quantity':l['quantity'],'unit_price':f"{l['unit_cents']//100}.{l['unit_cents']%100:02d}"} for l in order['lines']]}
                doc=self.ops.order(c,body)
                doc.update(origin='file_import',order_date=order['order_date'],intake_request_id=request)
                # Persist provenance without creating a spurious business revision.
                c.execute('UPDATE ops_documents SET data=? WHERE id=?',(json.dumps(doc,ensure_ascii=False),doc['id']))
                results.append({'id':doc['id'],'external_id':doc['external_id']})
            result={'profile_fact':payload.get('profile_fact'),'transformations':payload.get('transformations',[]),'request_id':request,'created_at':now(),'created':len(results),'skipped':len(payload['orders'])-len(results),'row_count':payload['row_count'],'orders':results,'shop_id':payload['shop_id'],'warehouse_id':payload['warehouse_id']}
            c.execute('INSERT INTO order_intake_receipts VALUES(?,?,?,?,?)',(request,fingerprint,token,json.dumps(result,ensure_ascii=False),result['created_at']))
            c.execute('INSERT INTO order_intake_requests VALUES(?,?,?)',(request,fingerprint,json.dumps(result,ensure_ascii=False)))
            self.store.event(c,None,'订单文件导入',f"新增{len(results)}单，跳过{result['skipped']}单")
            return result
