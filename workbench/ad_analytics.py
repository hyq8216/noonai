"""Local advertising evidence, independent of accounting and external budgets."""
import csv
import io
import json
from datetime import date,timedelta
from core import Problem,ident,now
from finance import CURRENCIES,day,fingerprint
from operations import cents,text

SCHEMA_SQL='''
CREATE TABLE IF NOT EXISTS ad_records(id TEXT PRIMARY KEY,identity TEXT UNIQUE NOT NULL,shop_id TEXT NOT NULL,
 date TEXT NOT NULL,campaign TEXT NOT NULL,channel TEXT NOT NULL,currency TEXT NOT NULL,sku TEXT NOT NULL,
 attribution_basis TEXT NOT NULL,report_level TEXT NOT NULL,spend_cents INTEGER NOT NULL,
 attributed_sales_cents INTEGER NOT NULL,impressions INTEGER NOT NULL,clicks INTEGER NOT NULL,orders INTEGER NOT NULL,
 revision INTEGER NOT NULL,data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_ad_date ON ad_records(date,shop_id,currency);
CREATE TABLE IF NOT EXISTS ad_batches(id TEXT PRIMARY KEY,created_at TEXT NOT NULL,data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ad_requests(key TEXT PRIMARY KEY,digest TEXT NOT NULL,result TEXT NOT NULL);
'''
FIELDS=['date','campaign','channel','currency','spend','impressions','clicks','orders','attributed_sales','sku','attribution_basis']
REQUIRED=set(FIELDS[:9])
MAX_ROWS=1000
MAX_BYTES=2*1024*1024

def csv_bytes(rows):
    out=io.StringIO(newline='');w=csv.writer(out)
    for row in rows:
        cells=[]
        for value in row:
            value='' if value is None else str(value)
            if value.lstrip().startswith(('=','+','-','@')) or value.startswith(('\t','\r','\n')):value="'"+value
            cells.append(value)
        w.writerow(cells)
    return out.getvalue().encode('utf-8-sig')

def integer(value,label):
    if isinstance(value,bool) or not str(value).isdecimal() or not 0<=int(value)<=1000000000000:raise Problem(label+'须为非负整数，最多1万亿')
    return int(value)

def metrics(row):
    out=dict(row)
    out['ctr']=out['clicks']/out['impressions'] if out['impressions'] else None
    out['cpc_cents']=out['spend_cents']/out['clicks'] if out['clicks'] else None
    out['acos']=out['spend_cents']/out['attributed_sales_cents'] if out['attributed_sales_cents'] else None
    out['roas']=out['attributed_sales_cents']/out['spend_cents'] if out['spend_cents'] else None
    return out

class AdAnalytics:
    def __init__(self,app):
        self.store=app.store
        with self.store.connect() as c:c.executescript(SCHEMA_SQL)
    def template(self,body=None):return csv_bytes([FIELDS])
    def _page(self,value):
        if isinstance(value,bool) or not str(value).isdecimal() or not 0<=int(value)<=1000000:raise Problem('广告报表页码无效')
        return int(value)
    def parse(self,b):
        content=b.get('text',b.get('content'))
        if not isinstance(content,str) or not content.strip() or len(content.encode('utf-8'))>MAX_BYTES:raise Problem('请选择非空CSV/JSON，最大2MB')
        content=content.lstrip('\ufeff')
        if b.get('format')=='json':
            try:rows=json.loads(content,parse_float=str,parse_constant=str)
            except (ValueError,RecursionError):raise Problem('广告JSON须为标准字段对象数组')
            if not isinstance(rows,list):raise Problem('广告JSON须为标准字段对象数组')
        elif b.get('format')=='csv':
            try:
                reader=csv.DictReader(io.StringIO(content,newline=''));headers=reader.fieldnames
                if not headers or len(headers)!=len(set(headers)) or not REQUIRED.issubset(headers):raise Problem('CSV须包含标准必填字段，列名不可重复')
                rows=[]
                for row in reader:
                    rows.append(row)
                    if len(rows)>MAX_ROWS:raise Problem('每批最多1000行')
            except csv.Error:raise Problem('广告CSV格式无效')
        else:raise Problem('仅支持广告标准CSV或JSON')
        if not 1<=len(rows)<=MAX_ROWS:raise Problem('每批需1至1000行')
        return rows
    def normalize(self,c,shop,raw):
        if not isinstance(raw,dict) or not REQUIRED.issubset(raw) or set(raw)-set(FIELDS) or any(v is None for v in raw.values()):raise Problem('标准字段缺失、多余或CSV列数不一致')
        result={'shop_id':shop,'date':day(raw['date'],'报告日期'),'campaign':text(raw['campaign'],'活动名称',200),
                'channel':text(raw['channel'],'广告渠道',100),'currency':raw['currency'],
                'sku':text(raw.get('sku',''),'SKU',100,True),'attribution_basis':text(raw.get('attribution_basis','') or '上传报表口径未说明','归因口径',300)}
        if result['currency'] not in CURRENCIES:raise Problem('广告币种仅支持CNY/SAR/USD/AED')
        if result['date']>now()[:10]:raise Problem('广告证据不能使用未来日期')
        result['spend_cents']=cents(raw['spend']);result['attributed_sales_cents']=cents(raw['attributed_sales'])
        for key,label in [('impressions','曝光'),('clicks','点击'),('orders','归因订单')]:result[key]=integer(raw[key],label)
        result['report_level']='sku' if result['sku'] else 'campaign'
        matches=c.execute("SELECT id FROM products WHERE json_extract(data,'$.partner_sku')=? AND coalesce(json_extract(data,'$.demo'),0)=0 LIMIT 2",(result['sku'],)).fetchall() if result['sku'] else []
        result['product_id']=matches[0]['id'] if len(matches)==1 else None
        result['sku_status']='matched' if result['product_id'] else ('unmatched' if result['sku'] else 'campaign')
        return result
    def _identity(self,row):return fingerprint([row[k] for k in ('shop_id','date','campaign','channel','currency','sku')])
    def _snapshot(self,c,identities):
        return [[key,dict(row) if (row:=c.execute('SELECT id,revision,data FROM ad_records WHERE identity=?',(key,)).fetchone()) else None] for key in sorted(set(identities))]
    def _public_batch(self,b,page=0):
        total=len(b['rows']);pages=max(1,(total+49)//50);page=min(page,pages-1)
        return {k:v for k,v in b.items() if k not in ('rows','snapshot')} | {'rows':b['rows'][page*50:(page+1)*50],'row_page':{'page':page,'pages':pages,'total':total,'page_size':50}}
    def preview(self,body):
        if not isinstance(body,dict):raise Problem('广告导入格式无效')
        raw=self.parse(body);shop=text(body.get('shop_id'),'店铺',100)
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if not c.execute("SELECT id FROM ops_entities WHERE id=? AND kind='shop'",(shop,)).fetchone():raise Problem('请先建立并选择店铺')
            rows=[];seen={};summary={'total':len(raw),'accepted':0,'duplicates':0,'conflicts':0,'errors':0,'unknown_sku':0}
            for index,item in enumerate(raw,1):
                row={'line':index,'status':'error','reason':'','raw':item}
                try:
                    normalized=self.normalize(c,shop,item);key=self._identity(normalized);row.update(normalized,identity=key)
                    if key in seen:
                        if fingerprint(normalized)==fingerprint(seen[key]):row.update(status='duplicate',reason='同文件完全重复行，仅保留首次记录')
                        else:
                            for earlier in rows:
                                if earlier.get('identity')==key and earlier['status']!='error':
                                    summary[{'duplicate':'duplicates','conflict':'conflicts','accepted':'accepted'}[earlier['status']]]-=1;summary['errors']+=1
                                    earlier.update(status='error',reason='同文件相同身份存在不同事实，全部相关行隔离；请修正文件后重试')
                            seen[key]=None
                            raise Problem('同文件相同身份存在不同事实，全部相关行隔离；请修正文件后重试')
                    else:
                        seen[key]=normalized;existing=c.execute('SELECT * FROM ad_records WHERE identity=?',(key,)).fetchone()
                        if existing and fingerprint(json.loads(existing['data']))==fingerprint(normalized):row.update(status='duplicate',reason='完全重复，已登记版本保持不变',record_id=existing['id'],revision=existing['revision'])
                        elif existing:row.update(status='conflict',reason='现有报告事实不同；只有明确勾选替换后才建立新版本',record_id=existing['id'],revision=existing['revision'])
                        else:row.update(status='accepted',reason='待人工确认')
                    if normalized['sku_status']=='unmatched':summary['unknown_sku']+=1;row['reason']+='；SKU未匹配，只保留上传SKU维度归因，不视为真实SKU销售'
                except Problem as e:row['reason']=str(e)
                summary[{'error':'errors','duplicate':'duplicates','conflict':'conflicts','accepted':'accepted'}[row['status']]]+=1;rows.append(row)
            snapshot=self._snapshot(c,[r['identity'] for r in rows if 'identity' in r]);batch={'id':ident(),'shop_id':shop,'created_at':now(),'status':'preview','summary':summary,'rows':rows,'snapshot':snapshot,'snapshot_fingerprint':fingerprint(snapshot),'receipt':None}
            c.execute('INSERT INTO ad_batches VALUES(?,?,?)',(batch['id'],batch['created_at'],json.dumps(batch,ensure_ascii=False)));self.store.event(c,None,'广告报表 · 预检',batch['id'])
            return self._public_batch(batch)
    def apply(self,body):
        if not isinstance(body,dict):raise Problem('广告确认格式无效')
        request=text(body.get('request_id'),'操作编号',100);digest=fingerprint(body)
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE');prior=c.execute('SELECT * FROM ad_requests WHERE key=?',(request,)).fetchone()
            if prior:
                if prior['digest']!=digest:raise Problem('操作编号已用于其他内容',409)
                return json.loads(prior['result'])
            if body.get('confirmed') is not True:raise Problem('请核对预览并明确确认广告数据导入')
            found=c.execute('SELECT data FROM ad_batches WHERE id=?',(body.get('batch_id'),)).fetchone()
            if not found:raise Problem('广告预检批次不存在',404)
            batch=json.loads(found['data'])
            if batch['status']!='preview':raise Problem('此批已确认，请查看原导入回执',409)
            if body.get('snapshot_fingerprint')!=batch['snapshot_fingerprint'] or fingerprint(self._snapshot(c,[r[0] for r in batch['snapshot']]))!=batch['snapshot_fingerprint']:raise Problem('现有广告记录已改变或预览指纹缺失，请重新上传预检',409)
            imported=replaced=0
            for row in batch['rows']:
                if row['status'] not in ('accepted','conflict') or (row['status']=='conflict' and body.get('replace_conflicts') is not True):continue
                normalized={k:row[k] for k in ('shop_id','date','campaign','channel','currency','sku','attribution_basis','report_level','spend_cents','attributed_sales_cents','impressions','clicks','orders','product_id','sku_status')}
                revision=row.get('revision',0)+1;rid=row.get('record_id') or ident()
                c.execute('''INSERT INTO ad_records VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(identity) DO UPDATE SET
                    attribution_basis=excluded.attribution_basis,report_level=excluded.report_level,spend_cents=excluded.spend_cents,
                    attributed_sales_cents=excluded.attributed_sales_cents,impressions=excluded.impressions,clicks=excluded.clicks,orders=excluded.orders,
                    revision=excluded.revision,data=excluded.data''',(rid,row['identity'],row['shop_id'],row['date'],row['campaign'],row['channel'],row['currency'],row['sku'],row['attribution_basis'],row['report_level'],row['spend_cents'],row['attributed_sales_cents'],row['impressions'],row['clicks'],row['orders'],revision,json.dumps(normalized,ensure_ascii=False)))
                if row['status']=='conflict':replaced+=1
                else:imported+=1
                row.update(status='imported',record_id=rid,revision=revision,reason='已保存为广告归因记录，未写入财务')
            receipt={'batch_id':batch['id'],'imported':imported,'replaced':replaced,'duplicates':batch['summary']['duplicates'],'isolated_conflicts':batch['summary']['conflicts']-replaced,'errors':batch['summary']['errors'],'at':now()}
            batch.update(status='applied',receipt=receipt);c.execute('UPDATE ad_batches SET data=? WHERE id=?',(json.dumps(batch,ensure_ascii=False),batch['id']))
            c.execute('INSERT INTO ad_requests VALUES(?,?,?)',(request,digest,json.dumps(receipt,ensure_ascii=False)));self.store.event(c,None,'广告报表 · 确认',json.dumps(receipt,ensure_ascii=False));return receipt
    def state(self,body=None,page=0):
        b=body or {}
        if not isinstance(b,dict):raise Problem('广告筛选格式无效')
        today=date.fromisoformat(now()[:10]);f={'from':day(b.get('from') or (today-timedelta(days=29)).isoformat(),'开始日期'),'to':day(b.get('to') or today.isoformat(),'结束日期')}
        if not 0<=(date.fromisoformat(f['to'])-date.fromisoformat(f['from'])).days<366:raise Problem('广告分析范围须为1至366日')
        page=self._page(b.get('page',page));where='date BETWEEN ? AND ?';params=[f['from'],f['to']]
        for key in ('shop_id','currency','campaign','channel'):
            f[key]=text(b.get(key) or '',key,200,True)
            if f[key]:where+=' AND '+key+'=?';params.append(f[key])
        if f['currency'] and f['currency'] not in CURRENCIES:raise Problem('广告币种无效')
        with self.store.connect() as c:
            c.execute('BEGIN');entities=[dict(r) for r in c.execute("SELECT id,kind,name FROM ops_entities WHERE kind='shop' ORDER BY name")]
            if f['shop_id'] and not any(e['id']==f['shop_id'] for e in entities):raise Problem('筛选店铺不存在',404)
            count=c.execute('SELECT count(*) FROM ad_records WHERE '+where,params).fetchone()[0];pages=max(1,(count+49)//50);page=min(page,pages-1);f['page']=page
            groups=[metrics(r) for r in c.execute('''SELECT currency,channel,attribution_basis,report_level,count(*) records,
                sum(spend_cents) spend_cents,sum(attributed_sales_cents) attributed_sales_cents,sum(impressions) impressions,sum(clicks) clicks,sum(orders) orders
                FROM ad_records WHERE '''+where+' GROUP BY currency,channel,attribution_basis,report_level ORDER BY currency,channel,attribution_basis,report_level',params)]
            items=[metrics({**json.loads(r['data']),'id':r['id'],'revision':r['revision']}) for r in c.execute('SELECT id,revision,data FROM ad_records WHERE '+where+' ORDER BY date DESC,campaign,id LIMIT 50 OFFSET ?',params+[page*50])]
            batches=[]
            for row in c.execute('SELECT data FROM ad_batches ORDER BY created_at DESC,id DESC LIMIT 10'):
                batch=json.loads(row['data']);batches.append({k:v for k,v in batch.items() if k not in ('rows','snapshot')})
            selected=None
            if b.get('batch_id'):
                row=c.execute('SELECT data FROM ad_batches WHERE id=?',(b['batch_id'],)).fetchone()
                if not row:raise Problem('广告批次不存在',404)
                selected=self._public_batch(json.loads(row['data']),self._page(b.get('batch_page',0)))
            return {'filters':f,'entities':entities,'summary':{'records':count,'groups':groups},'record_page':{'items':items,'page':page,'pages':pages,'total':count,'page_size':50},'recent_batches':batches,'batch':selected,'generated_at':now(),
                'basis':['本地上传广告归因报告，尚未连接任何广告账户或预算API；不自动登记财务收入、广告费用或实际付款。',
                    '按报告日期含首尾统计；原币、渠道、归因口径和campaign/SKU报告层级分别汇总，防止整活动与SKU明细重叠加总。',
                    '归因口径未说明时不能推断点击/展示归因窗口；归因销售和归因订单不代表实际履约、结算收入或净利润。',
                    'CTR=点击/曝光，CPC=原币花费/点击，ACOS=花费/归因销售，ROAS=归因销售/花费；分母为0显示未知，不输出无穷。',
                    'SKU未匹配时保留上传SKU归因记录，不能据此建立真实SKU销量；无SKU行为活动层级记录。',
                    '相同店铺/日期/活动/渠道/原币/SKU身份仅保留当前版本，重复跳过、冲突隔离；明确确认替换后增加版本，不叠加旧数值。']}
    def export(self,body=None):
        s=self.state(body);rows=[['广告归因分析 · 非财务收入','生成时间',s['generated_at']],['期间',s['filters']['from'],s['filters']['to']]]
        rows += [['口径',v] for v in s['basis']]
        keys=['currency','channel','attribution_basis','report_level','records','spend_cents','attributed_sales_cents','impressions','clicks','orders','ctr','cpc_cents','acos','roas'];rows.append(keys)
        rows += [[g[k] for k in keys] for g in s['summary']['groups']]
        keys=['date','shop_id','campaign','channel','currency','sku','sku_status','revision','spend_cents','attributed_sales_cents','impressions','clicks','orders','ctr','cpc_cents','acos','roas'];rows.append(['当前明细页',s['record_page']['page']+1]);rows.append(keys)
        rows += [[r[k] for k in keys] for r in s['record_page']['items']]
        return csv_bytes(rows)
