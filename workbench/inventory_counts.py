"""Confirmed physical good-stock counts applied as atomic ledger differences.

Quarantined returns and transfer stock are excluded. Missing balances remain
unknown until an operator explicitly supplies a physical count and evidence.
"""
import csv
import io
import json
from core import Problem, ident, now
from operations import qty, text
from source_collection import page_number, request_id
from source_import import digest

SCHEMA_SQL = '''
CREATE TABLE IF NOT EXISTS inventory_count_previews(token TEXT PRIMARY KEY,data TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS inventory_count_documents(id TEXT PRIMARY KEY,warehouse_id TEXT NOT NULL,data TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS inventory_count_documents_created ON inventory_count_documents(created_at DESC,id);
CREATE TABLE IF NOT EXISTS inventory_count_requests(key TEXT PRIMARY KEY,digest TEXT NOT NULL,result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS inventory_count_audit(id INTEGER PRIMARY KEY AUTOINCREMENT,document_id TEXT NOT NULL,action TEXT NOT NULL,data TEXT NOT NULL,created_at TEXT NOT NULL);
'''


def csv_bytes(headers, rows):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    writer.writerow(headers)
    for values in rows:
        writer.writerow(["'"+str(value) if str(value).lstrip().startswith(('=', '+', '-', '@')) or str(value).startswith(('\t', '\r', '\n')) else '' if value is None else value for value in values])
    return ('\ufeff'+stream.getvalue()).encode('utf-8')


class InventoryCounts:
    def __init__(self, app):
        self.app, self.store, self.ops = app, app.store, app.ops
        with self.store.connect() as c:
            c.executescript(SCHEMA_SQL)

    def _warehouse(self, c, warehouse_id):
        if not isinstance(warehouse_id, str) or not warehouse_id:
            raise Problem('请选择实际盘点仓库')
        row = c.execute("SELECT * FROM ops_entities WHERE id=? AND kind='warehouse'", (warehouse_id,)).fetchone()
        if not row:
            raise Problem('盘点仓库不存在，请重新选择', 409)
        return dict(row)

    def _context(self, c, warehouse_id, product_ids):
        result = {pid:dict(quarantine=[], transfers=[], movements=[]) for pid in product_ids}
        if not product_ids:return result
        marks = ','.join('?' for pid in product_ids)
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='after_sales_quarantine'").fetchone():
            for row in c.execute(f'SELECT * FROM after_sales_quarantine WHERE warehouse_id=? AND product_id IN ({marks}) AND received>released+discarded ORDER BY case_id,product_id', [warehouse_id, *product_ids]):
                result[row['product_id']]['quarantine'].append(dict(row))
        for row in c.execute(f"SELECT d.id,d.revision,d.data,json_extract(line.value,'$.product_id') AS product_id FROM ops_documents d,json_each(d.data,'$.lines') line WHERE d.kind='transfer' AND json_extract(d.data,'$.warehouse_id')=? AND json_extract(d.data,'$.status') IN ('in_transit','partial') AND json_extract(line.value,'$.product_id') IN ({marks}) ORDER BY d.id", [warehouse_id, *product_ids]):
            result[row['product_id']]['transfers'].append(dict(row))
        # Bind the movement identity as well as balances, detecting changed-and-restored stock.
        for row in c.execute(f'SELECT * FROM (SELECT m.*,row_number() OVER(PARTITION BY product_id ORDER BY created_at DESC,id DESC) AS snapshot_rank FROM ops_movements m WHERE m.warehouse_id=? AND m.product_id IN ({marks})) WHERE snapshot_rank=1 ORDER BY product_id', [warehouse_id, *product_ids]):
            result[row['product_id']]['movements'].append(dict(row))
        return result

    def _enrich(self, product, stock, context):
        quarantine = sum(q['received']-q['released']-q['discarded'] for q in context['quarantine'])
        incoming = 0
        for transfer in context['transfers']:
            incoming += sum(line['shipped']-line['received'] for line in json.loads(transfer['data'])['lines'] if line['product_id']==product['id'])
        return dict(product_id=product['id'], title=product['title_zh'], sku=product['partner_sku'],
                    product_revision=product['revision'], on_hand=stock['on_hand'] if stock else None,
                    reserved=stock['reserved'] if stock else 0,
                    available=stock['on_hand']-stock['reserved'] if stock else None,
                    quarantined=quarantine, transfer_incoming=incoming)

    def _where(self, query):
        query = text(query, '搜索内容', 300, True)
        return "coalesce(json_extract(p.data,'$.demo'),0)=0 AND instr(lower(coalesce(json_extract(p.data,'$.title_zh'),'')||' '||coalesce(json_extract(p.data,'$.partner_sku'),'')||' '||coalesce(json_extract(p.data,'$.source_sku'),'')),lower(?))>0", [query]

    def state(self, page=0, warehouse_id='', query=''):
        page = page_number(page)
        where, parameters = self._where(query)
        with self.store.connect() as c:
            c.execute('BEGIN')
            warehouses = [dict(id=row['id'], name=row['name']) for row in c.execute("SELECT id,name FROM ops_entities WHERE kind='warehouse' ORDER BY name,id")]
            documents = []
            for saved in c.execute('SELECT data FROM inventory_count_documents ORDER BY created_at DESC,id LIMIT 20'):
                doc=json.loads(saved['data'])
                documents.append({key:doc[key] for key in ('id','warehouse_id','warehouse_name','evidence','changed','unchanged','created_at','status')})
            if not warehouse_id:
                return dict(warehouses=warehouses,warehouse_id='',rows=[],total=0,page=0,pages=1,query=query,documents=documents)
            self._warehouse(c, warehouse_id)
            total = c.execute('SELECT count(*) FROM products p WHERE '+where,parameters).fetchone()[0]
            pages = max(1,(total+49)//50);page=min(page,pages-1)
            raw = c.execute('SELECT p.* FROM products p WHERE '+where+' ORDER BY p.created_at DESC,p.id LIMIT 50 OFFSET ?',[*parameters,page*50]).fetchall()
            context = self._context(c,warehouse_id,[p['id'] for p in raw])
            rows = []
            for saved in raw:
                product={**json.loads(saved['data']),'id':saved['id'],'revision':saved['revision']}
                stock=c.execute('SELECT * FROM ops_stock WHERE product_id=? AND warehouse_id=?',(saved['id'],warehouse_id)).fetchone()
                rows.append(self._enrich(product,stock,context[saved['id']]))
            return dict(warehouses=warehouses,warehouse_id=warehouse_id,rows=rows,total=total,page=page,pages=pages,query=query,documents=documents)

    def _inputs(self, body):
        if not isinstance(body,dict):raise Problem('盘点参数无效')
        warehouse_id=text(body.get('warehouse_id'),'仓库编号',100)
        evidence=text(body.get('evidence'),'实盘核对依据（盘点人、日期、范围或记录编号）',1500)
        if ('rows' in body)==('csv' in body):raise Problem('请选择表格录入或CSV之一')
        if 'csv' in body:
            value=body['csv']
            if not isinstance(value,str) or len(value)>4*1024*1024:raise Problem('CSV内容无效或超过4MB')
            try:
                reader=csv.DictReader(io.StringIO(value.lstrip('\ufeff')),restkey='_extra')
                if not reader.fieldnames or not {'counted_quantity'}.issubset(reader.fieldnames) or not any(field in reader.fieldnames for field in ('product_id','partner_sku')):
                    raise Problem('CSV需product_id或partner_sku及counted_quantity列')
                rows=[]
                for row in reader:
                    if len(rows)>=500:raise Problem('每批最多500行，请分批盘点')
                    if row.get('_extra'):raise Problem('CSV列数量超出表头，请检查引号与逗号')
                    rows.append(row)
            except csv.Error:raise Problem('CSV格式无效')
        else:rows=body['rows']
        if not isinstance(rows,list) or not 1<=len(rows)<=500 or any(not isinstance(row,dict) for row in rows):raise Problem('请录入1至500行真实实盘数量')
        return warehouse_id,evidence,rows

    def _prepare(self, c, body):
        warehouse_id,evidence,raw_rows=self._inputs(body)
        warehouse=self._warehouse(c,warehouse_id)
        parsed=[];seen=set();snapshots=[]
        for index,raw in enumerate(raw_rows):
            row=dict(line=index+1,product_id=None,title=raw.get('partner_sku') or raw.get('product_id') or '商品待确认',sku=raw.get('partner_sku') or '',status='blocked',reasons=[],counted_quantity=None,delta=None)
            product=None;stock=None
            try:
                if raw.get('warehouse_id') and raw['warehouse_id']!=warehouse_id:raise Problem('CSV仓库与当前所选盘点仓库不一致')
                if raw.get('product_id') is not None and not isinstance(raw.get('product_id'),str):raise Problem('商品编号格式无效')
                if raw.get('partner_sku') is not None and not isinstance(raw.get('partner_sku'),str):raise Problem('SKU格式无效')
                pid,p=self.ops.product_row(c,raw)
                if pid in seen:raise Problem('同一商品重复录入，请合并为一个实际数量')
                seen.add(pid)
                saved=c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone()
                product={**p,'id':pid,'revision':saved['revision']}
                stock=c.execute('SELECT * FROM ops_stock WHERE product_id=? AND warehouse_id=?',(pid,warehouse_id)).fetchone()
                row.update(product_id=pid,title=p['title_zh'],sku=p['partner_sku'],product_revision=saved['revision'])
                supplied=raw.get('counted_quantity')
                if supplied is None or isinstance(supplied,str) and not supplied.strip():raise Problem('实盘数量为空或未知；不会自动按0处理')
                counted=qty(supplied,True)
                row['counted_quantity']=counted
                reserved=stock['reserved'] if stock else 0
                if counted<reserved:raise Problem('实盘在库总数低于已占用数量，请先核对订单占用',409)
                row.update(delta=counted-(stock['on_hand'] if stock else 0),status='opening' if stock is None else 'change' if counted!=stock['on_hand'] else 'unchanged')
                snapshots.append([index,dict(saved),dict(stock) if stock else None])
            except Problem as error:
                row['reasons'].append(str(error))
                snapshots.append([index,dict(saved) if product else None,dict(stock) if stock else None,raw])
            parsed.append((row,product,stock))
        context=self._context(c,warehouse_id,list(seen))
        rows=[]
        for row,product,stock in parsed:
            if product:
                row.update(self._enrich(product,stock,context[product['id']]))
                row['excluded_note']='实盘仅包含良品在库总数；已占用包含在内，隔离与在途不包含'
            rows.append(row)
        token=digest([warehouse,evidence,raw_rows,snapshots,context])
        blocked=sum(row['status']=='blocked' for row in rows)
        changed=sum(row['status'] in ('change','opening') for row in rows)
        return dict(warehouse_id=warehouse_id,warehouse_name=warehouse['name'],evidence=evidence,rows=rows,
                    changed=changed,unchanged=len(rows)-blocked-changed,blocked=blocked,can_apply=blocked==0,
                    token=token,connection='local_only')

    def preview(self, body):
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            result=self._prepare(c,body)
            c.execute('INSERT OR IGNORE INTO inventory_count_previews VALUES(?,?,?)',(result['token'],json.dumps(result,ensure_ascii=False),now()))
            return result

    def apply(self, body):
        warehouse_id,evidence,rows=self._inputs(body)
        key=request_id(body)
        if body.get('confirmed') is not True:raise Problem('请核对实际盘点依据与差异，并明确确认本地库存调整')
        fingerprint=digest([warehouse_id,evidence,rows,body.get('preview_token')])
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT * FROM inventory_count_requests WHERE key=?',(key,)).fetchone()
            if old:
                if old['digest']!=fingerprint:raise Problem('请求编号已用于不同盘点内容',409)
                return {**json.loads(old['result']),'replayed':True}
            result=self._prepare(c,body)
            if result['token']!=body.get('preview_token'):raise Problem('库存、占用、商品、隔离、在途或仓库依据已变化，请重新实盘核对并预检',409)
            prior=c.execute('SELECT data FROM inventory_count_previews WHERE token=?',(result['token'],)).fetchone()
            if not prior:raise Problem('请先完成本批真实盘点预检',409)
            if result['blocked']:raise Problem('存在未知数量、占用不足或无效商品，整批库存未调整',409)
            did=ident();stamp=now()
            for row in result['rows']:
                if row['status'] in ('change','opening'):
                    self.ops.move(c,row['product_id'],warehouse_id,row['delta'],0,did,'实盘差量调整：'+evidence)
            document=dict(id=did,warehouse_id=warehouse_id,warehouse_name=result['warehouse_name'],evidence=evidence,
                          lines=result['rows'],changed=result['changed'],unchanged=result['unchanged'],created_at=stamp,
                          preview_token=result['token'],request_id=key,status='confirmed',connection='local_only')
            c.execute('INSERT INTO inventory_count_documents VALUES(?,?,?,?)',(did,warehouse_id,json.dumps(document,ensure_ascii=False),stamp))
            c.execute('INSERT INTO inventory_count_audit(document_id,action,data,created_at) VALUES(?,?,?,?)',(did,'confirmed',json.dumps({'request_id':key,'preview_token':result['token'],'evidence':evidence,'lines':result['rows']},ensure_ascii=False),stamp))
            self.store.event(c,None,'实盘库存调整',did)
            out=dict(document=document,replayed=False)
            c.execute('INSERT INTO inventory_count_requests VALUES(?,?,?)',(key,fingerprint,json.dumps(out,ensure_ascii=False)))
            return out

    def template(self, warehouse_id='', query='', page=0):
        # Only the visible 50-SKU page is exported; actual counts are intentionally blank.
        result=self.state(page,warehouse_id,query)
        rows=[[warehouse_id,r['product_id'],r['sku'],r['title'],r['on_hand'],r['reserved'],r['quarantined'],r['transfer_incoming'],''] for r in result['rows']]
        return csv_bytes(['warehouse_id','product_id','partner_sku','title','current_on_hand','reserved','quarantined','transfer_incoming','counted_quantity'],rows)

    def export(self, document_id=''):
        with self.store.connect() as c:
            if document_id:
                documents=c.execute('SELECT data FROM inventory_count_documents WHERE id=?',(text(document_id,'盘点单编号',100),)).fetchall()
                if not documents:raise Problem('盘点单不存在',404)
            else:documents=c.execute('SELECT data FROM inventory_count_documents ORDER BY created_at DESC,id LIMIT 100').fetchall()
            rows=[]
            for saved in documents:
                doc=json.loads(saved['data'])
                for row in doc['lines']:
                    rows.append([doc['id'],doc['warehouse_name'],doc['created_at'],doc['evidence'],row['sku'],row['title'],row['on_hand'],row['reserved'],row['quarantined'],row['transfer_incoming'],row['counted_quantity'],row['delta'],row['status']])
        return csv_bytes(['盘点单号','仓库','确认时间','实盘依据','SKU','商品','原良品在库','已占用','隔离不计入','在途不计入','实盘良品在库','调整差量','结果'],rows)
