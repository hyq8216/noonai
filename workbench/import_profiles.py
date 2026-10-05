"""Named explicit local import column mappings; no guessing or external file reads."""
import csv
import io
import json
from core import Problem, ident, now
from operations import text
from finance import CURRENCIES, fingerprint

SCHEMA_SQL = '''
CREATE TABLE IF NOT EXISTS import_profiles(id TEXT PRIMARY KEY,type TEXT NOT NULL,revision INTEGER NOT NULL,status TEXT NOT NULL,data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS import_profiles_type_status ON import_profiles(type,status,id);
CREATE TABLE IF NOT EXISTS import_profile_requests(key TEXT PRIMARY KEY,digest TEXT NOT NULL,result TEXT NOT NULL);
'''
SCHEMA_VERSION = 1
TYPES = {
    'order': {'label':'订单','fields':['external_id','partner_sku','quantity','unit_price','currency','order_total','order_date','note'],
              'required':['external_id','partner_sku','quantity','unit_price','currency']},
    'settlement': {'label':'结算','fields':['evidence_key','external_id','category','currency','amount','date','fx','evidence','kind'],
                   'required':['evidence_key','category','currency','amount','date','fx','evidence']},
    'bank': {'label':'银行流水','fields':['account_name','bank_reference','currency','direction','amount','date','fx','evidence'],
             'required':['account_name','bank_reference','currency','direction','amount','date','fx','evidence']}
}
NOTICE = '仅显式列名映射与固定币种；金额使用点号小数且不支持千位分隔符，日期只支持YYYY-MM-DD。不猜金额，不默认零，不转换汇率；订单下单时间遵循订单模块的ISO日期时间规则。'


class ImportProfiles:
    def __init__(self, app):
        self.store = app.store
        with self.store.connect() as c:
            c.executescript(SCHEMA_SQL)

    def schema(self):
        return {'schema_version':SCHEMA_VERSION,'types':TYPES,'currencies':list(CURRENCIES),'notice':NOTICE,
                'max_profiles_per_page':50,'date_format':'YYYY-MM-DD','decimal_separator':'.','thousands_separator':None}

    def get(self, c, pid):
        pid = text(pid,'导入模板编号',100)
        row = c.execute('SELECT data FROM import_profiles WHERE id=?',(pid,)).fetchone()
        if not row:
            raise Problem('导入模板不存在',404)
        return json.loads(row['data'])

    def guard(self, c, fact):
        if not fact:
            return
        p = self.get(c,fact.get('id'))
        if p['status']!='active' or p['revision']!=fact.get('revision') or fingerprint(p['config'])!=fact.get('config_hash'):
            raise Problem('导入模板已修改或撤销，请重新预检文件',409)

    def transact(self, action, b, fn):
        if not isinstance(b,dict):
            raise Problem('模板操作格式无效')
        key = text(b.get('request_id'),'操作编号',100)
        digest = fingerprint([action,b])
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            prior = c.execute('SELECT * FROM import_profile_requests WHERE key=?',(key,)).fetchone()
            if prior:
                if prior['digest']!=digest:
                    raise Problem('操作编号已用于其他内容',409)
                return json.loads(prior['result'])
            if b.get('confirmed') is not True:
                raise Problem('请明确确认保存或撤销导入模板')
            result = fn(c,b)
            c.execute('INSERT INTO import_profile_requests VALUES(?,?,?)',(key,digest,json.dumps(result,ensure_ascii=False)))
            self.store.event(c,None,'导入模板 · '+action,result['id'])
            return result

    def save(self, b):
        def commit(c,body):
            name = text(body.get('name'),'模板名称',150)
            kind = body.get('type')
            if kind not in TYPES:
                raise Problem('模板类型无效')
            columns = body.get('columns')
            if not isinstance(columns,dict) or any(k not in TYPES[kind]['fields'] for k in columns):
                raise Problem('列映射格式或标准字段无效')
            clean = {k:text(v,'源列名',200) for k,v in columns.items()}
            if len(set(clean.values()))!=len(clean):
                raise Problem('每个源列只能映射一个标准字段')
            currency = body.get('fixed_currency','')
            if currency not in ('',*CURRENCIES):
                raise Problem('固定币种无效')
            required = set(TYPES[kind]['required'])-({'currency'} if currency else set())
            if not required.issubset(clean):
                raise Problem('请映射全部必需字段：'+', '.join(sorted(required-set(clean))))
            # Reject unsupported normalization rather than saving settings that are ignored.
            if any(k in body for k in ('date_format','decimal_separator','thousands_separator','value_maps')):
                raise Problem('此版本仅支持列名映射与显式固定币种，不支持金额、日期或字段值转换配置')
            old = self.get(c,body['id']) if body.get('id') else None
            if old and (old['status']!='active' or type(body.get('revision')) is not int or body['revision']!=old['revision']):
                raise Problem('模板已更新或撤销，请刷新后重试',409)
            if old and old['type']!=kind:
                raise Problem('已有模板类型不能改变，请另建模板')
            profile = {'id':old['id'] if old else ident(),'name':name,'type':kind,'revision':old['revision']+1 if old else 1,
                       'status':'active','schema_version':SCHEMA_VERSION,'config':{'columns':clean,'fixed_currency':currency},
                       'created_at':old['created_at'] if old else now(),'updated_at':now()}
            c.execute('INSERT INTO import_profiles VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,status=excluded.status,data=excluded.data',
                      (profile['id'],kind,profile['revision'],profile['status'],json.dumps(profile,ensure_ascii=False)))
            return profile
        return self.transact('save',b,commit)

    def remove(self, b):
        def commit(c,body):
            p = self.get(c,body.get('id'))
            if p['status']!='active' or type(body.get('revision')) is not int or p['revision']!=body['revision']:
                raise Problem('模板已更新或撤销，请刷新后重试',409)
            p.update(status='removed',revision=p['revision']+1,updated_at=now())
            c.execute('UPDATE import_profiles SET revision=?,status=?,data=? WHERE id=?',(p['revision'],p['status'],json.dumps(p,ensure_ascii=False),p['id']))
            return p
        return self.transact('remove',b,commit)

    def state(self, page=0):
        if isinstance(page,bool) or not str(page).isdecimal() or not 0<=int(page)<=1000000:
            raise Problem('模板页码无效')
        with self.store.connect() as c:
            c.execute('BEGIN')
            total = c.execute("SELECT count(*) FROM import_profiles WHERE status='active'").fetchone()[0]
            pages = max(1,(total+49)//50)
            page = min(int(page),pages-1)
            profiles = [json.loads(r['data']) for r in c.execute("SELECT data FROM import_profiles WHERE status='active' ORDER BY rowid DESC LIMIT 50 OFFSET ?",(page*50,))]
            return {'profiles':profiles,'page':page,'pages':pages,'total':total,**self.schema()}

    def choices(self, c, kind):
        return [json.loads(r['data']) for r in c.execute("SELECT data FROM import_profiles WHERE status='active' AND type=? ORDER BY rowid DESC LIMIT 50",(kind,))]

    def apply(self, c, kind, b):
        if not b.get('profile_id'):
            if b.get('profile_revision') is not None:
                raise Problem('指定模板版本时必须同时指定模板编号')
            return b,None,[]
        p = self.get(c,b['profile_id'])
        if p['type']!=kind or p['status']!='active':
            raise Problem('模板类型不适用或模板已撤销')
        if type(b.get('profile_revision')) is not int or b['profile_revision']!=p['revision']:
            raise Problem('模板版本已改变，请刷新并重新选择',409)
        # Order support uses its existing csv/rows/json body, preserving the caller's shop/warehouse fields.
        order = kind=='order'
        if order and sum(key in b for key in ('csv','rows','json'))!=1:
            raise Problem('请选择一种CSV或JSON订单资料')
        content = b.get('csv') if order and 'csv' in b else b.get('rows',b.get('json')) if order else b.get('content')
        fmt = 'csv' if order and 'csv' in b else 'json' if order else b.get('format')
        maximum = 5000 if order else 500
        size = 8*1024*1024 if order else 2*1024*1024
        if isinstance(content,str):
            if not content.strip() or len(content.encode('utf-8'))>size:
                raise Problem('导入文件为空或超过大小限制')
            content = content.lstrip('\ufeff')
        if fmt=='csv':
            if not isinstance(content,str):
                raise Problem('CSV内容必须为文本')
            try:
                reader = csv.DictReader(io.StringIO(content,newline=''),strict=True)
                headers = reader.fieldnames
                if not headers or len(headers)>80 or len(headers)!=len(set(headers)):
                    raise Problem('源文件列名不可重复，最多80列')
                raw = []
                for row in reader:
                    raw.append(row)
                    if len(raw)>maximum:
                        raise Problem('导入行数超过限制')
            except csv.Error:
                raise Problem('CSV格式无效')
        elif fmt=='json':
            def unique(pairs):
                obj = {}
                for key,value in pairs:
                    if key in obj:
                        raise Problem('JSON对象字段不可重复：'+key)
                    obj[key]=value
                return obj
            if isinstance(content,str):
                try:
                    raw=json.loads(content,parse_float=str,parse_constant=str,object_pairs_hook=unique)
                except (ValueError,RecursionError):
                    raise Problem('JSON格式无效')
            else:
                raw=content
            if order and isinstance(raw,dict):
                raw=raw.get('rows')
            if not isinstance(raw,list):
                raise Problem('JSON应为行数组')
            if len(json.dumps(raw,ensure_ascii=False).encode('utf-8'))>size:
                raise Problem('导入文件超过大小限制')
        else:
            raise Problem('仅支持CSV或JSON')
        if not 1<=len(raw)<=maximum:
            raise Problem('导入文件行数无效')
        mapped=[]; transformations=[]
        for index, source in enumerate(raw,1):
            result={}; issues=[]
            if not isinstance(source,dict) or None in source:
                issues.append('源行格式无效或CSV列数不一致')
            else:
                for field,column in p['config']['columns'].items():
                    if column not in source or source[column] is None:
                        if field=='currency' and p['config']['fixed_currency']:
                            continue
                        issues.append('源列缺失：'+column+' → '+field)
                    else:
                        result[field]=source[column]
                currency=p['config']['fixed_currency']
                if currency:
                    observed=result.get('currency',source.get('currency',''))
                    if observed not in ('',None,currency):
                        issues.append('源币种与模板固定币种矛盾：'+str(observed)+' / '+currency)
                    result['currency']=currency
            mapped.append(result)
            transformations.append({'line':index,'original':source,'mapped':result,'problems':issues,
                                    'ignored_fields':[str(k) for k in source if k not in p['config']['columns'].values()] if isinstance(source,dict) else []})
        out={k:v for k,v in b.items() if k not in ('csv','rows','json','content','format','mapping')}
        if order:
            out['rows']=mapped
        else:
            out.update(format='json',content=json.dumps(mapped,ensure_ascii=False))
        fact={'id':p['id'],'revision':p['revision'],'name':p['name'],'config_hash':fingerprint(p['config'])}
        return out,fact,transformations

    def annotate(self, rows, transformations):
        for row,change in zip(rows,transformations):
            row['mapped_raw']=row['raw']
            row['raw']=change['original']
            if change['problems']:
                row.update(status='invalid',reason='；'.join(change['problems']))
                # A contradicted fixed currency cannot contribute a trusted original-currency total.
                row.pop('amount_cents',None)
            elif row['status'] not in ('ready','imported','unmatched','matched'):
                change['problems'].append(row['reason'])
