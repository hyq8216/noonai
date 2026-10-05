"""Evidence-backed local bank-file reconciliation. No bank connection or transfer."""
import csv
import io
import json
import secrets
from decimal import Decimal, InvalidOperation
from core import Problem, ident, now
from operations import text, cents
from finance import CURRENCIES, day, rate, fingerprint
from settlement_intake import csv_bytes
from import_profiles import ImportProfiles

SCHEMA_SQL = '''
CREATE TABLE IF NOT EXISTS bank_batches(id TEXT PRIMARY KEY,created_at TEXT NOT NULL,data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS bank_batches_created ON bank_batches(created_at DESC,id DESC);
CREATE TABLE IF NOT EXISTS bank_rows(id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,account_name TEXT NOT NULL,bank_reference TEXT NOT NULL,currency TEXT NOT NULL,data TEXT NOT NULL,UNIQUE(account_name,bank_reference));
CREATE INDEX IF NOT EXISTS bank_rows_batch ON bank_rows(batch_id,id);
CREATE TABLE IF NOT EXISTS bank_accounts(account_name TEXT PRIMARY KEY,currency TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bank_requests(key TEXT PRIMARY KEY,digest TEXT NOT NULL,result TEXT NOT NULL);
'''
FIELDS = ['account_name','bank_reference','currency','direction','amount','date','fx','evidence']
MAX_CONTENT = 2 * 1024 * 1024


class BankReconciliation:
    def __init__(self, app):
        self.store = app.store
        self.finance = app.finance
        self.profiles = getattr(app,'import_profiles',None) or ImportProfiles(app)
        with self.store.connect() as c:
            c.executescript(SCHEMA_SQL)

    def template(self):
        return csv_bytes([FIELDS,['测试SAR账户','BANK-001','SAR','in','100.00','2026-01-03','1.90','银行文件编号及行号'],
                          ['测试SAR账户','BANK-002','SAR','out','5.00','2026-01-03','1.90','银行文件编号及行号']])

    def parse(self, b):
        content = b.get('content')
        if not isinstance(content,str) or not content.strip() or len(content.encode('utf-8')) > MAX_CONTENT:
            raise Problem('请选择非空标准模板文件，最大2MB')
        content = content.lstrip('\ufeff')
        if b.get('format') == 'json':
            def unique_fields(pairs):
                result = {}
                for key,value in pairs:
                    if key in result:
                        raise Problem('JSON对象字段不可重复：'+key)
                    result[key] = value
                return result
            try:
                rows = json.loads(content,parse_float=str,parse_constant=str,object_pairs_hook=unique_fields)
            except (ValueError,RecursionError):
                raise Problem('JSON格式无效，应为标准字段行数组')
            if not isinstance(rows,list):
                raise Problem('JSON应为行数组')
        elif b.get('format') == 'csv':
            try:
                reader = csv.DictReader(io.StringIO(content,newline=''))
                headers = reader.fieldnames
                if not headers or len(headers)!=len(set(headers)) or not set(FIELDS).issubset(headers):
                    raise Problem('CSV需包含全部标准字段，列名不可重复')
                rows = []
                for row in reader:
                    rows.append(row)
                    if len(rows)>500:
                        raise Problem('每批最多500行，请拆分文件')
            except csv.Error:
                raise Problem('CSV格式无效')
        else:
            raise Problem('仅支持标准模板CSV或JSON')
        if not rows or len(rows)>500:
            raise Problem('每批需1至500行')
        return rows

    def canonical(self, raw):
        if not isinstance(raw,dict) or None in raw or any(v is None for v in raw.values()):
            raise Problem('字段缺失或CSV列数不一致')
        if set(raw)-set(FIELDS):
            raise Problem('未识别字段：'+', '.join(sorted(str(k) for k in set(raw)-set(FIELDS))))
        account = text(raw.get('account_name'),'账户名称',150)
        reference = text(raw.get('bank_reference'),'银行凭证编号',200)
        currency = raw.get('currency')
        direction = raw.get('direction')
        if currency not in CURRENCIES or direction not in ('in','out'):
            raise Problem('币种或收支方向无效；direction须为in/out')
        value = raw.get('amount')
        if isinstance(value,(bool,float)):
            raise Problem('流水金额请使用十进制文本')
        try:
            amount = Decimal(str(value))
        except InvalidOperation:
            raise Problem('流水金额无效')
        if not amount.is_finite() or amount<=0 or amount.as_tuple().exponent < -2 or amount>100000000:
            raise Problem('流水金额须大于0且最多两位小数，收支方向单独填写')
        amount_text = format(amount,'f')
        dated = day(raw.get('date'),'流水日期')
        if dated>now()[:10]:
            raise Problem('流水日期不能晚于今天')
        fx = rate(raw.get('fx'),currency)
        evidence = text(raw.get('evidence'),'流水依据',1500)
        return {'account_name':account,'bank_reference':reference,'currency':currency,'direction':direction,'amount':amount_text,
                'amount_cents':cents(amount_text),'date':dated,'fx':fx,'evidence':evidence}

    def signature(self, row):
        return {k:row[k] for k in FIELDS if k!='amount'} | {'amount_cents':row['amount_cents']}

    def account_currencies(self, c, account):
        currencies = {r['currency'] for r in c.execute('SELECT currency FROM bank_accounts WHERE account_name=?',(account,))}
        currencies.update(r[0] for r in c.execute("SELECT DISTINCT json_extract(data,'$.currency') FROM finance_payments WHERE json_extract(data,'$.account')=?",(account,)) if r[0])
        return sorted(currencies)

    def dependencies(self, c, rows):
        accounts = sorted({r['account_name'] for r in rows if 'account_name' in r})
        keys = sorted({(r['account_name'],r['bank_reference']) for r in rows if 'account_name' in r and 'bank_reference' in r})
        currencies = []
        for account in accounts:
            currencies.append([account,self.account_currencies(c,account)])
        existing = []
        for account,ref in keys:
            row = c.execute('SELECT data FROM bank_rows WHERE account_name=? AND bank_reference=?',(account,ref)).fetchone()
            existing.append([account,ref,row['data'] if row else None])
        return {'accounts':currencies,'rows':existing}

    def reconcile(self, c, raw_rows):
        rows = []
        seen = {}
        account_groups = {}
        key_groups = {}
        for index, raw in enumerate(raw_rows,1):
            row = {'line':index,'raw':raw,'status':'invalid','reason':'','bank_row_id':''}
            rows.append(row)
            try:
                row.update(self.canonical(raw))
                account_groups.setdefault(row['account_name'],[]).append(row)
                key = (row['account_name'],row['bank_reference'])
                key_groups.setdefault(key,[]).append(row)
                if key in seen:
                    first = seen[key]
                    if self.signature(first)!=self.signature(row) or first['status']=='conflict':
                        first.update(status='conflict',reason='同账户同银行凭证内容矛盾，整组隔离')
                        row.update(status='conflict',reason='同账户同银行凭证内容矛盾，整组隔离')
                    else:
                        row.update(status='duplicate',reason='本批同账户银行凭证重复')
                    continue
                seen[key] = row
                prior = c.execute('SELECT id,data FROM bank_rows WHERE account_name=? AND bank_reference=?',key).fetchone()
                if prior:
                    old = json.loads(prior['data'])
                    same = self.signature(old)==self.signature(row)
                    row.update(status='duplicate' if same else 'conflict',reason='银行流水已导入，不能重复入账' if same else '已导入的同账户银行凭证内容矛盾',bank_row_id=prior['id'])
                else:
                    row.update(status='ready',reason='可导入本地流水；导入后仍须另行选择财务条目并确认匹配')
            except Problem as error:
                row['reason'] = str(error)
        for group in key_groups.values():
            if any(r['status']=='conflict' for r in group):
                for row in group:
                    row.update(status='conflict',reason='同账户同银行凭证内容矛盾，整组隔离')
        for account,group in account_groups.items():
            stored = self.account_currencies(c,account)
            mixed = len({r['currency'] for r in group})>1
            for row in group:
                if mixed or any(currency!=row['currency'] for currency in stored):
                    row.update(status='conflict',reason='同账户币种不一致；不同币种请使用独立账户名称')
        return rows

    def summary(self, rows):
        counts = {s:sum(r['status']==s for r in rows) for s in ('ready','invalid','duplicate','conflict','unmatched','matched','payment_void')}
        counts['total'] = len(rows)
        totals = {}
        for row in rows:
            if 'amount_cents' not in row:
                continue
            group = totals.setdefault(row['currency'],{'currency':row['currency'],'source_in_cents':0,'source_out_cents':0,'importable_in_cents':0,'importable_out_cents':0})
            group['source_'+row['direction']+'_cents'] += row['amount_cents']
            if row['status'] in ('ready','unmatched','matched','payment_void'):
                group['importable_'+row['direction']+'_cents'] += row['amount_cents']
        return counts,list(totals.values())

    def get_batch(self, c, batch_id):
        batch_id = text(batch_id,'批次编号',100)
        row = c.execute('SELECT data FROM bank_batches WHERE id=?',(batch_id,)).fetchone()
        if not row:
            raise Problem('银行流水批次不存在',404)
        return json.loads(row['data'])

    def get_row(self, c, row_id):
        row_id = text(row_id,'流水编号',100)
        row = c.execute('SELECT data FROM bank_rows WHERE id=?',(row_id,)).fetchone()
        if not row:
            raise Problem('银行流水不存在',404)
        return json.loads(row['data'])

    def row_view(self, c, row):
        row = dict(row)
        payment = None
        if row.get('payment_id'):
            p = c.execute('SELECT data FROM finance_payments WHERE id=?',(row['payment_id'],)).fetchone()
            payment = json.loads(p['data']) if p else None
            row['payment_status'] = payment['status'] if payment else 'missing'
            if not payment or payment['status']!='active':
                row.update(status='payment_void',reason='匹配的收付款记录已撤销或缺失；保留原始回执，不自动重记，需在财务台账核对纠错')
        if row['status']=='unmatched' and any(currency!=row['currency'] for currency in self.account_currencies(c,row['account_name'])):
            row.update(status='conflict',reason='账户已有财务收付款币种与流水矛盾，请先核对账户记录')
        row['row_token'] = fingerprint({'row':row,'payment':payment})
        return row

    # Aggregate only payment amounts; no complete finance snapshot or order scan.
    ENTRY_SELECT = '''SELECT e.id,e.data,
        CAST(json_extract(e.data,'$.amount_cents') AS INTEGER)-COALESCE(p.paid,0) AS remaining_cents
        FROM finance_entries e LEFT JOIN
        (SELECT entry_id,SUM(CAST(json_extract(data,'$.amount_cents') AS INTEGER)) AS paid
         FROM finance_payments WHERE json_extract(data,'$.status')='active' GROUP BY entry_id) p ON p.entry_id=e.id
        WHERE json_extract(e.data,'$.status')='active' '''

    def entry_summary(self, row):
        e = json.loads(row['data'])
        return {k:e.get(k) for k in ('id','revision','kind','category','currency','amount_cents','date','evidence_key','document_id','evidence')} | {'remaining_cents':row['remaining_cents']}

    def suggestions(self, c, row):
        if row['status']!='unmatched':
            return []
        kind = 'income' if row['direction']=='in' else 'expense'
        matches = c.execute(self.ENTRY_SELECT+'''
          AND json_extract(e.data,'$.currency')=? AND json_extract(e.data,'$.kind')=?
          AND json_extract(e.data,'$.date')<=? AND remaining_cents>=?
          ORDER BY CASE WHEN json_extract(e.data,'$.evidence_key')=? THEN 0 ELSE 1 END,
          remaining_cents-?,json_extract(e.data,'$.date') DESC,e.id LIMIT 5''',
          (row['currency'],kind,row['date'],row['amount_cents'],row['bank_reference'],row['amount_cents'])).fetchall()
        return [self.entry_summary(m)|{'suggestion_reason':'币种、方向、日期与未结余额符合；须人工核对凭证后选择'} for m in matches]

    def public_batch(self, c, batch, suggestions=False, suggestion_limit=20):
        out = {k:v for k,v in batch.items() if k!='dependencies'}
        rows = []
        suggested_rows = 0
        for source in batch['rows']:
            if batch['status']=='imported' and source.get('bank_row_id') and source['status']=='unmatched':
                row = self.row_view(c,self.get_row(c,source['bank_row_id']))
                if suggestions:
                    if suggested_rows < suggestion_limit and row['status']=='unmatched':
                        row['suggestions'] = self.suggestions(c,row)
                        suggested_rows += 1
                    else:
                        row['suggestions'] = []
                        row['suggestion_notice'] = '历史批次前20条未匹配流水提供快速建议，其余请从分页候选选择核对' if row['status']=='unmatched' else ''
                rows.append(row)
            else:
                rows.append(dict(source))
        out['rows'] = rows
        out['summary'],out['currency_totals'] = self.summary(rows)
        return out

    def preview(self, b):
        if not isinstance(b,dict):
            raise Problem('预检格式无效')
        filename = text(b.get('filename','银行流水文件'),'文件名',250)
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            mapped,profile_fact,changes = self.profiles.apply(c,'bank',b)
            raw = self.parse(mapped)
            rows = self.reconcile(c,raw)
            self.profiles.annotate(rows,changes)
            batch = {'id':ident(),'created_at':now(),'filename':filename,'format':b['format'],'status':'preview',
                     'token':secrets.token_urlsafe(32),'profile_fact':profile_fact,'transformations':changes,'rows':rows,'dependencies':self.dependencies(c,rows),
                     'notice':'本地标准模板流水；未连接银行。导入流水不记账，人工确认匹配才登记已有资金流水凭证，不发起转账。'}
            c.execute('INSERT INTO bank_batches VALUES(?,?,?)',(batch['id'],batch['created_at'],json.dumps(batch,ensure_ascii=False)))
            self.store.event(c,None,'银行文件预检',batch['id'])
            return self.public_batch(c,batch)

    def transact(self, action, b, fn):
        if not isinstance(b,dict):
            raise Problem('银行核对操作格式无效')
        key = text(b.get('request_id'),'操作编号',100)
        digest = fingerprint([action,b])
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            prior = c.execute('SELECT * FROM bank_requests WHERE key=?',(key,)).fetchone()
            if prior:
                if prior['digest']!=digest:
                    raise Problem('操作编号已用于其他内容',409)
                return json.loads(prior['result'])
            if b.get('confirmed') is not True:
                raise Problem('请明确确认已核对流水与凭证')
            result = fn(c,b)
            c.execute('INSERT INTO bank_requests VALUES(?,?,?)',(key,digest,json.dumps(result,ensure_ascii=False)))
            self.store.event(c,None,'银行本地核对 · '+action,result.get('id',result.get('row_id','')))
            return result

    def import_rows(self, b):
        def commit(c,body):
            batch = self.get_batch(c,body.get('batch_id'))
            if batch['status']!='preview':
                raise Problem('此批次已导入，请查看流水匹配状态',409)
            if not isinstance(body.get('token'),str) or not secrets.compare_digest(body['token'],batch['token']):
                raise Problem('预检令牌无效，请重新预检',409)
            self.profiles.guard(c,batch.get('profile_fact'))
            if fingerprint(self.dependencies(c,batch['rows']))!=fingerprint(batch['dependencies']):
                raise Problem('账户币种或银行凭证已改变，请重新预检文件',409)
            if not any(r['status']=='ready' for r in batch['rows']):
                raise Problem('没有可导入流水，请修正问题后重新预检')
            for source in batch['rows']:
                if source['status']!='ready':
                    continue
                row_id = ident()
                source.update(bank_row_id=row_id,status='unmatched',reason='已导入本地流水，尚未匹配财务条目')
                row = dict(source)|{'id':row_id,'batch_id':batch['id'],'imported_at':now(),'payment_id':'','entry_id':''}
                c.execute('INSERT OR IGNORE INTO bank_accounts VALUES(?,?)',(row['account_name'],row['currency']))
                c.execute('INSERT INTO bank_rows VALUES(?,?,?,?,?,?)',(row_id,batch['id'],row['account_name'],row['bank_reference'],row['currency'],json.dumps(row,ensure_ascii=False)))
            batch.update(status='imported',imported_at=now(),request_id=body['request_id'])
            batch['remaining_rows'] = [r['line'] for r in batch['rows'] if r['status'] not in ('unmatched',)]
            c.execute('UPDATE bank_batches SET data=? WHERE id=?',(json.dumps(batch,ensure_ascii=False),batch['id']))
            return self.public_batch(c,batch,True)
        return self.transact('import',b,commit)

    def match(self, b):
        def commit(c,body):
            row = self.get_row(c,body.get('row_id'))
            view = self.row_view(c,row)
            if view['status']!='unmatched' or row.get('payment_id'):
                raise Problem('此流水已有匹配回执（含撤销记录），不能重复匹配',409)
            if not isinstance(body.get('row_token'),str) or not secrets.compare_digest(body['row_token'],view['row_token']):
                raise Problem('流水状态已改变，请刷新后重新核对',409)
            eid = text(body.get('entry_id'),'财务条目编号',100)
            entry = self.finance.get(c,eid)
            revision = body.get('entry_revision')
            self.finance.guard(entry,{'revision':revision})
            if entry['currency']!=row['currency'] or entry['kind']!=('income' if row['direction']=='in' else 'expense'):
                raise Problem('流水与财务条目币种或收支方向不一致')
            payment = self.finance.pay(c,{'id':eid,'revision':revision,'amount':row['amount'],'fx':row['fx'],'date':row['date'],
                                        'account':row['account_name'],'evidence':row['evidence'],'evidence_key':'bank-row:'+row['id']})
            row.update(status='matched',reason='人工确认匹配，已登记本地收付款；未发起银行转账',entry_id=eid,
                       entry_revision_at_match=revision,payment_id=payment['id'],matched_at=now(),match_request_id=body['request_id'])
            c.execute('UPDATE bank_rows SET data=? WHERE id=?',(json.dumps(row,ensure_ascii=False),row['id']))
            return {'id':row['id'],'row_id':row['id'],'row':self.row_view(c,row),'payment':payment}
        return self.transact('match',b,commit)

    def state(self, page=0, entry_page=0):
        for value in (page,entry_page):
            if isinstance(value,bool) or not str(value).isdecimal() or not 0<=int(value)<=1000000:
                raise Problem('银行核对页码无效')
        with self.store.connect() as c:
            c.execute('BEGIN')
            total = c.execute('SELECT count(*) FROM bank_batches').fetchone()[0]
            pages = max(1,(total+9)//10)
            page = min(int(page),pages-1)
            batches = [self.public_batch(c,json.loads(r['data']),True) for r in c.execute('SELECT data FROM bank_batches ORDER BY created_at DESC,id DESC LIMIT 10 OFFSET ?',(page*10,))]
            candidate_query = self.ENTRY_SELECT+' AND remaining_cents>0'
            entry_total = c.execute('SELECT count(*) FROM ('+candidate_query+')').fetchone()[0]
            entry_pages = max(1,(entry_total+49)//50)
            entry_page = min(int(entry_page),entry_pages-1)
            entries = [self.entry_summary(r) for r in c.execute(candidate_query+" ORDER BY json_extract(e.data,'$.date') DESC,e.id DESC LIMIT 50 OFFSET ?",(entry_page*50,))]
            return {'profiles':self.profiles.choices(c,'bank'),'batches':batches,'page':page,'pages':pages,'total':total,'finance_entries':entries,
                    'entry_pagination':{'page':entry_page,'pages':entry_pages,'total':entry_total},'template_fields':FIELDS,
                    'accounts':[dict(r) for r in c.execute('SELECT * FROM bank_accounts ORDER BY account_name')]}

    def export(self, batch_id=None):
        with self.store.connect() as c:
            if batch_id:
                batches = [self.public_batch(c,self.get_batch(c,batch_id))]
            else:
                batches = [self.public_batch(c,json.loads(r['data'])) for r in c.execute('SELECT data FROM bank_batches ORDER BY created_at DESC,id DESC')]
        headers = ['batch_id','line','status','reason']+FIELDS+['bank_row_id','entry_id','payment_id','payment_status','raw_json']
        rows = [headers]
        for batch in batches:
            for row in batch['rows']:
                rows.append([batch['id'],row['line'],row['status'],row['reason']]+[row.get(k,'') for k in FIELDS]+[row.get(k,'') for k in headers[12:-1]]+[json.dumps(row['raw'],ensure_ascii=False)])
        return csv_bytes(rows)
