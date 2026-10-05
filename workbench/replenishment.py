"""Manual local replenishment policy and read-only stock suggestions."""
import csv
import io
import json
from core import Problem, now
from finance import fingerprint
from operations import qty, text

SCHEMA_SQL = '''
CREATE TABLE IF NOT EXISTS local_replenishment_policies(
 product_id TEXT NOT NULL,warehouse_id TEXT NOT NULL,revision INTEGER NOT NULL,data TEXT NOT NULL,
 PRIMARY KEY(product_id,warehouse_id));
CREATE TABLE IF NOT EXISTS local_replenishment_requests(
 key TEXT PRIMARY KEY,digest TEXT NOT NULL,result TEXT NOT NULL);
'''
BASIS = [
 '可用库存=良品在库−已占用；只读取库存台账，不计隔离、调拨在途或商品档案参考库存（在途仅计入预计净库存）。未建库存账保留未知，不按零处理。',
 '采购未收=同仓同SKU状态为open/partial的实际本地采购单订购量−累计收货量；取消及已收货单未收按0计。采购订单关联只是分配计划，不再次加到供给。',
 '调拨在途=同仓同SKU已发未收，仅in_transit/partial单据；未占用订单需求仅统计new订单，reserved订单已从可用库存扣除，不再次扣减。预计净库存=可用+采购未收+调拨在途−未占用需求；建议数量=max(目标库存−预计净库存,0)。最低库存仅作预警；提前天数仅为人工参考，不推断到货日期、销量或精确需求预测。',
 '最低、目标、提前天数与库存必须已知才计算建议；空白保留未知。规则保存绑定版本和当前来源预检，库存/采购/调拨/订单变化后实时重算。',
 '仅本地建议与规则，不自动建立采购单、不下单、不付款，不代表真实采购或Noon经营验证。导出包含当前筛选全部记录。']


class Replenishment:
    PAGE_SIZE = 50

    def __init__(self, app):
        self.store = app.store
        with self.store.connect() as c:
            c.executescript(SCHEMA_SQL)

    def filters(self, body=None):
        if body is None: body = {}
        if not isinstance(body, dict): raise Problem('补货筛选格式无效')
        page = body.get('page', 0)
        if isinstance(page, bool) or not str(page).isdecimal() or not 0 <= int(page) <= 1000000:
            raise Problem('补货页码无效')
        risk=text(body.get('risk', ''),'风险',30,True);suggestion=text(body.get('suggestion',''),'建议',30,True)
        if risk not in ('','unknown','below_minimum','shortfall','covered') or suggestion not in ('','needed','none','unknown'): raise Problem('补货风险或建议筛选无效')
        return {'risk':risk,'suggestion':suggestion,'warehouse_id': text(body.get('warehouse_id', ''), '仓库', 100, True),
                'search': text(body.get('search', ''), '搜索', 200, True), 'page': int(page)}

    def _query(self, c, f):
        if f['warehouse_id'] and not c.execute("SELECT 1 FROM ops_entities WHERE kind='warehouse' AND id=?", (f['warehouse_id'],)).fetchone():
            raise Problem('仓库不存在', 404)
        where = "w.kind='warehouse' AND coalesce(json_extract(p.data,'$.demo'),0)=0"
        params = []
        if f['warehouse_id']: where += ' AND w.id=?'; params.append(f['warehouse_id'])
        if f['search']:
            # instr implements literal search: % and _ are not wildcard shortcuts.
            where += " AND (instr(lower(coalesce(json_extract(p.data,'$.partner_sku'),'')),lower(?))>0 OR instr(lower(coalesce(json_extract(p.data,'$.title_zh'),'')),lower(?))>0 OR instr(lower(w.name),lower(?))>0)"
            params += [f['search']]*3
        return " FROM products p CROSS JOIN ops_entities w WHERE "+where, params

    def _filtered_query(self, f, sql, params):
        # Aggregate each supply/demand ledger once; source details are loaded only
        # for the selected page. Missing stock/policy parameters remain SQL NULL.
        cte="""WITH candidates AS (
            SELECT p.id product_id,w.id warehouse_id,w.name warehouse_name"""+sql+"""),
        supply AS MATERIALIZED (
            SELECT json_extract(d.data,'$.warehouse_id') warehouse_id,
                json_extract(l.value,'$.product_id') product_id,
                sum(CASE WHEN d.kind='purchase' AND json_extract(d.data,'$.status') IN ('open','partial')
                    THEN max(json_extract(l.value,'$.quantity')-json_extract(l.value,'$.received'),0) ELSE 0 END) purchase_pending,
                sum(CASE WHEN d.kind='transfer' AND json_extract(d.data,'$.status') IN ('in_transit','partial')
                    THEN max(json_extract(l.value,'$.shipped')-json_extract(l.value,'$.received'),0) ELSE 0 END) transfer_pending,
                sum(CASE WHEN d.kind='order' AND json_extract(d.data,'$.status')='new'
                    THEN json_extract(l.value,'$.quantity') ELSE 0 END) unreserved_demand
            FROM ops_documents d,json_each(d.data,'$.lines') l
            WHERE d.kind IN ('purchase','transfer','order')
            GROUP BY json_extract(d.data,'$.warehouse_id'),json_extract(l.value,'$.product_id')
        ), facts AS (
            SELECT a.*,s.on_hand-s.reserved available,
                s.on_hand-s.reserved+coalesce(v.purchase_pending,0)+coalesce(v.transfer_pending,0)-coalesce(v.unreserved_demand,0) projected,
                json_extract(r.data,'$.minimum') minimum,json_extract(r.data,'$.target') target,json_extract(r.data,'$.lead_days') lead_days
            FROM candidates a LEFT JOIN ops_stock s ON s.product_id=a.product_id AND s.warehouse_id=a.warehouse_id
            LEFT JOIN supply v ON v.product_id=a.product_id AND v.warehouse_id=a.warehouse_id
            LEFT JOIN local_replenishment_policies r ON r.product_id=a.product_id AND r.warehouse_id=a.warehouse_id
        ), known AS (
            SELECT *,available IS NOT NULL AND minimum IS NOT NULL AND target IS NOT NULL AND lead_days IS NOT NULL computable FROM facts
        ), classified AS (
            SELECT *,CASE WHEN NOT computable THEN 'unknown' WHEN projected<0 THEN 'shortfall'
                WHEN available<minimum THEN 'below_minimum' ELSE 'covered' END risk,
                CASE WHEN NOT computable THEN 'unknown' WHEN target>projected THEN 'needed' ELSE 'none' END suggestion FROM known
        ) """
        where=' FROM classified WHERE 1=1';values=list(params)
        for key in ('risk','suggestion'):
            if f[key]:where+=' AND '+key+'=?';values.append(f[key])
        return cte,where,values

    def _row(self, c, pid, wid):
        p = c.execute('SELECT data FROM products WHERE id=?', (pid,)).fetchone()
        w = c.execute("SELECT name FROM ops_entities WHERE id=? AND kind='warehouse'", (wid,)).fetchone()
        if not p or not w or json.loads(p['data']).get('demo'): raise Problem('请选择有效非示例商品与仓库', 404)
        product = json.loads(p['data'])
        stock = c.execute('SELECT on_hand,reserved FROM ops_stock WHERE product_id=? AND warehouse_id=?', (pid,wid)).fetchone()
        s = dict(stock) if stock else {'on_hand': None, 'reserved': None}
        history = dict(c.execute('SELECT count(*) movements,max(rowid) last_movement FROM ops_movements WHERE product_id=? AND warehouse_id=?',(pid,wid)).fetchone())
        policy = c.execute('SELECT data FROM local_replenishment_policies WHERE product_id=? AND warehouse_id=?',(pid,wid)).fetchone()
        rule = json.loads(policy['data']) if policy else {'product_id':pid,'warehouse_id':wid,'revision':0,'minimum':None,'target':None,'lead_days':None}
        purchases = []
        for row in c.execute("""SELECT d.id,d.revision,d.data,json_extract(l.value,'$.quantity') quantity,
            json_extract(l.value,'$.received') received,json_extract(d.data,'$.status') status
            FROM ops_documents d,json_each(d.data,'$.lines') l WHERE d.kind='purchase'
            AND json_extract(d.data,'$.warehouse_id')=? AND json_extract(l.value,'$.product_id')=? ORDER BY d.id""", (wid,pid)):
            purchases.append({'purchase_id':row['id'],'revision':row['revision'],'status':row['status'],'quantity':row['quantity'],
                              'received':row['received'],'pending':max(row['quantity']-row['received'],0) if row['status'] in ('open','partial') else 0})
        transfers=[];orders=[]
        for doc in c.execute("""SELECT d.id,d.revision,d.kind,d.data,l.value line
            FROM ops_documents d,json_each(d.data,'$.lines') l
            WHERE d.kind IN ('transfer','order') AND json_extract(d.data,'$.warehouse_id')=?
            AND json_extract(l.value,'$.product_id')=? ORDER BY d.id""",(wid,pid)):
            d=json.loads(doc['data']);line=json.loads(doc['line'])
            if doc['kind']=='transfer':
                transfers.append({'transfer_id':doc['id'],'revision':doc['revision'],'status':d['status'],
                    'source_warehouse_id':d['source_warehouse_id'],'quantity':line['quantity'],'shipped':line['shipped'],'received':line['received'],
                    'pending':max(line['shipped']-line['received'],0) if d['status'] in ('in_transit','partial') else 0})
            else:
                orders.append({'order_id':doc['id'],'revision':doc['revision'],'external_id':d.get('external_id',''),'status':d['status'],
                    'quantity':line['quantity'],'demand':line['quantity'] if d['status']=='new' else 0,
                    'basis':'未占用需求' if d['status']=='new' else ('已计入库存占用，不重复扣减' if d['status']=='reserved' else '已出库或取消，不计当前需求')})
        incoming = sum(x['pending'] for x in purchases)
        moving=sum(x['pending'] for x in transfers);demand=sum(x['demand'] for x in orders)
        available = s['on_hand']-s['reserved'] if stock else None
        projected=available+incoming+moving-demand if available is not None else None
        known = available is not None and all(rule.get(k) is not None for k in ('minimum','target','lead_days'))
        source = {'stock':s,'stock_history':history,'purchases':purchases,'transfers':transfers,'orders':orders,'product':fingerprint(p['data'])}
        result = {**rule,**s,'sku':product.get('partner_sku',pid),'title':product.get('title_zh',''),'warehouse_name':w['name'],
                  'available':available,'purchase_pending':incoming,'purchases':purchases,'transfer_pending':moving,'unreserved_demand':demand,'projected':projected,'transfers':transfers,'orders':orders,'quantity':max(rule['target']-projected,0) if known else None,
                  'below_minimum':available<rule['minimum'] if available is not None and rule.get('minimum') is not None else None,
                  'source_version':fingerprint(source),'stock_version':fingerprint([s,history]),'purchase_version':fingerprint(purchases),
                  'transfer_version':fingerprint(transfers),'order_version':fingerprint(orders),
                  'status':'已知参数 · 本地建议' if known else '待确认参数 / 库存'}
        result['risk']='unknown' if not known else ('shortfall' if projected<0 else ('below_minimum' if result['below_minimum'] else 'covered'))
        result['suggestion']='unknown' if not known else ('needed' if result['quantity']>0 else 'none')
        result['calculation'] = f"max({rule['target']}−({available}+{incoming}+{moving}−{demand}),0)={result['quantity']}" if known else '未知参数或未建库存账，不能计算数量'
        return result

    def _facts(self, c, body):
        if not isinstance(body,dict): raise Problem('补货规则格式无效')
        pid=text(body.get('product_id'),'商品编号',100);wid=text(body.get('warehouse_id'),'仓库编号',100)
        current=self._row(c,pid,wid)
        if type(body.get('revision')) is not int or body['revision']!=current['revision']:
            raise Problem('补货规则版本已变化，请刷新重新预检',409)
        values={k:None if body.get(k) is None or body.get(k)=='' else qty(body[k],True) for k in ('minimum','target','lead_days')}
        if values['minimum'] is not None and values['target'] is not None and values['minimum']>values['target']:
            raise Problem('目标库存不能低于最低库存')
        if values['lead_days'] is not None and values['lead_days']>3650: raise Problem('采购提前天数最多3650天')
        note=text(body.get('note',''),'设置依据',2000,True)
        rule={'product_id':pid,'warehouse_id':wid,'revision':current['revision'],**values,'note':note}
        token=fingerprint({'rule':rule,'current':current})
        known=current['available'] is not None and all(v is not None for v in values.values())
        return {**rule,'preview_token':token,'source_version':current['source_version'],'available':current['available'],
                'purchase_pending':current['purchase_pending'],'purchases':current['purchases'],
                **{k:current[k] for k in ('transfer_pending','unreserved_demand','projected','transfers','orders','stock_version','purchase_version','transfer_version','order_version')},
                'quantity':max(values['target']-current['projected'],0) if known else None,
                'notice':BASIS[4]}

    def preview(self, body):
        with self.store.connect() as c:
            c.execute('BEGIN'); return self._facts(c,body)

    def apply(self, body):
        if not isinstance(body,dict): raise Problem('补货规则格式无效')
        key=text(body.get('request_id'),'操作编号',100);digest=fingerprint(body)
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT digest,result FROM local_replenishment_requests WHERE key=?',(key,)).fetchone()
            if old:
                if old['digest']!=digest: raise Problem('操作编号已用于其他内容',409)
                return json.loads(old['result'])
            if body.get('confirmed') is not True: raise Problem('请先确认补货规则预检')
            f=self._facts(c,body)
            if body.get('preview_token')!=f['preview_token']: raise Problem('库存、采购、调拨、订单或规则已变化，请重新预检确认',409)
            result={k:f[k] for k in ('product_id','warehouse_id','revision','minimum','target','lead_days','note')}
            result.update(revision=result['revision']+1,updated_at=now(),confirmed_source_version=f['source_version'])
            c.execute('INSERT INTO local_replenishment_policies VALUES(?,?,?,?) ON CONFLICT(product_id,warehouse_id) DO UPDATE SET revision=excluded.revision,data=excluded.data',
                      (result['product_id'],result['warehouse_id'],result['revision'],json.dumps(result,ensure_ascii=False)))
            c.execute('INSERT INTO local_replenishment_requests VALUES(?,?,?)',(key,digest,json.dumps(result,ensure_ascii=False)))
            self.store.event(c,None,'本地补货规则 · 人工确认',json.dumps(result,ensure_ascii=False))
            return result

    def state(self, body=None):
        f=self.filters(body)
        with self.store.connect() as c:
            c.execute('BEGIN');sql,params=self._query(c,f)
            if f['risk'] or f['suggestion']:
                cte,where,values=self._filtered_query(f,sql,params)
                total=c.execute(cte+'SELECT count(*)'+where,values).fetchone()[0]
                pages=max(1,(total+self.PAGE_SIZE-1)//self.PAGE_SIZE);f['page']=min(f['page'],pages-1)
                ids=c.execute(cte+'SELECT product_id,warehouse_id'+where+' ORDER BY warehouse_name,product_id LIMIT ? OFFSET ?',
                              values+[self.PAGE_SIZE,f['page']*self.PAGE_SIZE]).fetchall()
                rows=[self._row(c,r['product_id'],r['warehouse_id']) for r in ids]
            else:
                total=c.execute('SELECT count(*)'+sql,params).fetchone()[0];pages=max(1,(total+49)//50);f['page']=min(f['page'],pages-1)
                rows=[self._row(c,r['product_id'],r['warehouse_id']) for r in c.execute('SELECT p.id product_id,w.id warehouse_id'+sql+' ORDER BY w.name,p.id LIMIT 50 OFFSET ?',params+[f['page']*50]).fetchall()]
            return {'filters':f,'entities':[dict(r) for r in c.execute("SELECT id,kind,name FROM ops_entities WHERE kind='warehouse' ORDER BY name,id")],
                    'rows':rows,'sku_page':{'items':rows,'page':f['page'],'pages':pages,'total':total,'page_size':50},'basis':BASIS,'generated_at':now()}

    def _filtered_rows(self,c,f,sql,params):
        for ids in c.execute('SELECT p.id product_id,w.id warehouse_id'+sql+' ORDER BY w.name,p.id',params):
            row=self._row(c,ids['product_id'],ids['warehouse_id'])
            if f['risk'] and row['risk']!=f['risk']:continue
            if f['suggestion'] and row['suggestion']!=f['suggestion']:continue
            yield row

    def export(self, body=None):
        f=self.filters(body);out=io.StringIO();w=csv.writer(out)
        def write(row):
            vals=[]
            for v in row:
                v='' if v is None else str(v)
                if v.lstrip().startswith(('=','+','-','@')) or v.startswith(('\t','\r','\n')):v="'"+v
                vals.append(v)
            w.writerow(vals)
        with self.store.connect() as c:
            c.execute('BEGIN');sql,params=self._query(c,f)
            write(['本地补货建议','生成时间',now(),'仓库筛选',f['warehouse_id'],'搜索',f['search'],'风险',f['risk'],'建议',f['suggestion'],'范围','当前筛选全部记录'])
            for basis in BASIS:write(['口径',basis])
            keys=['warehouse_name','product_id','sku','title','minimum','target','lead_days','on_hand','reserved','available','purchase_pending','transfer_pending','unreserved_demand','projected','quantity','risk','suggestion','revision','source_version','calculation']
            write(keys)
            for row in self._filtered_rows(c,f,sql,params):
                write([row[k] for k in keys])
                for p in row['purchases']:write(['采购依据',p['purchase_id'],'版本',p['revision'],'状态',p['status'],'订购',p['quantity'],'已收',p['received'],'未收',p['pending']])
                for t in row['transfers']:write(['调拨依据',t['transfer_id'],'版本',t['revision'],'状态',t['status'],'调出仓',t['source_warehouse_id'],'已发',t['shipped'],'已收',t['received'],'在途',t['pending']])
                for o in row['orders']:write(['订单依据',o['order_id'],o['external_id'],'版本',o['revision'],'状态',o['status'],'订购',o['quantity'],'未占用需求',o['demand'],o['basis']])
        return ('\ufeff'+out.getvalue()).encode('utf-8')
