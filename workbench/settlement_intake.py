"""Local standard-template settlement intake; never reads Noon or bank services."""
import csv
import io
import json
import secrets
from decimal import Decimal, InvalidOperation
from core import Problem, ident, now
from operations import text, cents
from finance import CURRENCIES, day, rate, fingerprint
from import_profiles import ImportProfiles

SCHEMA_SQL = '''
CREATE TABLE IF NOT EXISTS settlement_batches(id TEXT PRIMARY KEY, created_at TEXT NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS settlement_batches_created ON settlement_batches(created_at DESC,id DESC);
CREATE TABLE IF NOT EXISTS settlement_requests(key TEXT PRIMARY KEY,digest TEXT NOT NULL,result TEXT NOT NULL);
'''
FIELDS = ['evidence_key','external_id','category','currency','amount','date','fx','evidence','kind']
CATEGORIES = ('sale','refund','platform','logistics','ads','other')
MAX_ROWS = 500
MAX_CONTENT = 2 * 1024 * 1024


def csv_bytes(rows):
    stream = io.StringIO(newline='')
    writer = csv.writer(stream)
    def safe(value):
        value = str(value)
        return "'" + value if value.lstrip().startswith(('=','+','-','@')) or value.startswith(('\t','\r','\n')) else value
    for row in rows:
        writer.writerow([safe(v) for v in row])
    return stream.getvalue().encode('utf-8-sig')


class SettlementIntake:
    def __init__(self, app):
        self.app = app
        self.store = app.store
        self.finance = app.finance
        self.profiles = getattr(app,'import_profiles',None) or ImportProfiles(app)
        with self.store.connect() as c:
            c.executescript(SCHEMA_SQL)

    def template(self):
        return csv_bytes([FIELDS, ['SETTLEMENT-001-L1','ORDER-001','sale','SAR','100.00','2026-01-01','1.90','结算文件编号及行号','income'],
                          ['SETTLEMENT-001-L2','ORDER-001','refund','SAR','5.00','2026-01-01','1.90','退款凭证编号及行号','expense']])

    def parse(self, b):
        content = b.get('content')
        if not isinstance(content, str) or not content.strip() or len(content.encode('utf-8')) > MAX_CONTENT:
            raise Problem('请选择非空标准模板文件，最大2MB')
        content = content.lstrip('\ufeff')
        if b.get('format') == 'json':
            try:
                rows = json.loads(content, parse_float=str, parse_constant=str)
            except (ValueError, RecursionError):
                raise Problem('JSON格式无效，应为标准字段的行数组')
            if not isinstance(rows, list):
                raise Problem('JSON应为行数组')
        elif b.get('format') == 'csv':
            try:
                reader = csv.DictReader(io.StringIO(content, newline=''))
                headers = reader.fieldnames
                if not headers or len(headers) != len(set(headers)) or not set(FIELDS[:-1]).issubset(headers):
                    raise Problem('CSV需包含标准模板字段且列名不可重复')
                rows = []
                for row in reader:
                    rows.append(row)
                    if len(rows) > MAX_ROWS:
                        raise Problem('每批最多500行，请拆分文件')
            except csv.Error:
                raise Problem('CSV格式无效')
        else:
            raise Problem('仅支持标准模板CSV或JSON')
        if not rows or len(rows) > MAX_ROWS:
            raise Problem('每批需1至500行')
        return rows

    def dependencies(self, c, shop, rows):
        external = sorted({r.get('external_id') for r in rows if isinstance(r.get('external_id'), str) and r['external_id']})
        keys = sorted({r.get('evidence_key') for r in rows if isinstance(r.get('evidence_key'), str) and r['evidence_key']})
        entity = c.execute('SELECT data FROM ops_entities WHERE id=? AND kind=\'shop\'', (shop,)).fetchone()
        orders = []
        for ext in external:
            matches = c.execute("SELECT id,data FROM ops_documents WHERE kind='order' AND json_extract(data,'$.shop_id')=? AND json_extract(data,'$.external_id')=? ORDER BY id", (shop, ext)).fetchall()
            orders.append([ext, [[r['id'], r['data']] for r in matches]])
        entries = []
        for key in keys:
            found = c.execute('SELECT id,data FROM finance_entries WHERE evidence_key=?', (key,)).fetchone()
            entries.append([key, dict(found) if found else None])
        return {'shop': entity['data'] if entity else None, 'orders': orders, 'entries': entries}

    def reconcile(self, c, shop, raw_rows):
        result = []
        seen = {}
        for index, raw in enumerate(raw_rows, 1):
            row = {'line': index, 'raw': raw, 'status': 'invalid', 'reason': '', 'entry_id': ''}
            result.append(row)
            try:
                if not isinstance(raw, dict) or None in raw or any(v is None for v in raw.values()):
                    raise Problem('字段缺失或CSV列数不一致')
                unknown = set(raw) - set(FIELDS)
                if unknown:
                    raise Problem('未识别字段：' + ', '.join(sorted(str(k) for k in unknown)))
                key = text(raw.get('evidence_key'), '凭证编号', 150)
                row['evidence_key'] = key
                ext = text(raw.get('external_id',''), '外部订单号', 250, True)
                row['external_id'] = ext
                category = raw.get('category')
                currency = raw.get('currency')
                if category not in CATEGORIES or currency not in CURRENCIES:
                    raise Problem('类别或币种未识别')
                kind = raw.get('kind') or ('income' if category == 'sale' else 'expense' if category != 'other' else '')
                if kind not in ('income','expense') or (category == 'sale' and kind != 'income') or (category not in ('sale','other') and kind != 'expense'):
                    raise Problem('类别与收支方向矛盾；other必须明确kind')
                value = raw.get('amount')
                if isinstance(value, (bool, float)):
                    raise Problem('原币金额请使用十进制文本')
                try:
                    amount = Decimal(str(value))
                except InvalidOperation:
                    raise Problem('原币金额无效')
                if not amount.is_finite() or amount == 0 or amount.as_tuple().exponent < -2 or abs(amount) > 100000000:
                    raise Problem('原币金额须非零且最多两位小数')
                if amount < 0 and kind == 'income':
                    raise Problem('收入不能使用负数；退款请选refund')
                absolute = format(abs(amount), 'f')
                amount_cents = cents(absolute)
                dated = day(raw.get('date'), '结算日期')
                if dated > now()[:10]:
                    raise Problem('结算确认日期不能晚于今天')
                fx = rate(raw.get('fx'), currency)
                evidence = text(raw.get('evidence'), '凭证依据', 1500)
                row.update(category=category, currency=currency, amount=format(amount,'f'), amount_cents=amount_cents, kind=kind, date=dated, fx=fx, evidence=evidence)
                if key in seen:
                    first = seen[key]
                    if any(first.get(k) != row.get(k) for k in ('external_id','category','currency','amount_cents','kind','date','fx','evidence')):
                        first.update(status='conflict', reason='同一凭证编号出现矛盾内容，整组隔离')
                        row.update(status='conflict', reason='同一凭证编号出现矛盾内容，整组隔离')
                    elif first['status'] == 'conflict':
                        row.update(status='conflict', reason='同一凭证编号出现矛盾内容，整组隔离')
                    else:
                        row.update(status='duplicate', reason='本批凭证编号重复')
                    continue
                seen[key] = row
                prior = c.execute('SELECT id,data FROM finance_entries WHERE evidence_key=?', (key,)).fetchone()
                if prior:
                    old = json.loads(prior['data'])
                    same = all(old.get(k) == row.get(k) for k in ('category','currency','amount_cents','kind','date','fx','evidence'))
                    if ext:
                        d = c.execute('SELECT data FROM ops_documents WHERE id=?',(old.get('document_id'),)).fetchone()
                        doc = json.loads(d['data']) if d else {}
                        same = same and doc.get('shop_id') == shop and doc.get('external_id') == ext
                    else:
                        same = same and not old.get('document_id')
                    row.update(status='duplicate' if same else 'conflict', reason='凭证已登记（包括已作废凭证），不可再次导入' if same else '凭证编号已登记但内容或店铺订单映射矛盾', entry_id=old['id'])
                    continue
                matches = c.execute("SELECT data FROM ops_documents WHERE kind='order' AND json_extract(data,'$.shop_id')=? AND json_extract(data,'$.external_id')=?", (shop, ext)).fetchall() if ext else []
                if ext and not matches:
                    row.update(status='unmatched', reason='所选店铺未找到此外部订单号')
                    continue
                if len(matches) > 1:
                    row.update(status='conflict', reason='店铺订单号映射到多个订单')
                    continue
                if not ext and category in ('sale','refund'):
                    row.update(status='unmatched', reason='销售与退款必须填写外部订单号')
                    continue
                doc = json.loads(matches[0]['data']) if matches else None
                if doc and category == 'sale' and doc['status'] == 'cancelled':
                    row.update(status='conflict', reason='销售结算关联已取消订单，请先核对业务单据')
                    continue
                row.update(status='ready', reason='已映射订单，可确认记账' if doc else '未分摊店铺费用，可确认记账',
                           order_id=doc['id'] if doc else '', order_revision=doc['revision'] if doc else None,
                           order_currency=doc['currency'] if doc else '', order_total_cents=doc['total_cents'] if doc else None,
                           cross_currency=bool(doc and doc['currency'] != currency))
                row['posting'] = {'kind': kind, 'category': category, 'currency': currency, 'amount': absolute, 'date': dated,
                                  'fx': fx, 'evidence': evidence, 'evidence_key': key, 'document_id': row['order_id'], 'note': '标准结算导入；行'+str(index)}
            except Problem as error:
                row['reason'] = str(error)
        return result

    def summary(self, rows):
        counts = {status: sum(r['status'] == status for r in rows) for status in ('ready','invalid','unmatched','duplicate','conflict','imported')}
        counts.update(total=len(rows), mapped=sum(bool(r.get('order_id')) for r in rows), unallocated=sum(r['status'] in ('ready','imported') and not r.get('order_id') for r in rows))
        totals = {}
        for r in rows:
            if 'amount_cents' not in r:
                continue
            group = totals.setdefault(r['currency'], {'currency':r['currency'],'source_income_cents':0,'source_expense_cents':0,'importable_income_cents':0,'importable_expense_cents':0})
            direction = 'income' if r['kind'] == 'income' else 'expense'
            group['source_'+direction+'_cents'] += r['amount_cents']
            if r['status'] in ('ready','imported'):
                group['importable_'+direction+'_cents'] += r['amount_cents']
        return counts, list(totals.values())

    def order_checks(self, rows):
        groups = {}
        for r in rows:
            if r['status'] not in ('ready','imported') or r.get('category') != 'sale' or not r.get('order_id'):
                continue
            key = (r['order_id'],r['currency'])
            g = groups.setdefault(key, {'order_id':r['order_id'],'external_id':r['external_id'],'currency':r['currency'],'order_currency':r['order_currency'],'order_total_cents':r['order_total_cents'],'batch_sale_cents':0,'comparable':not r['cross_currency']})
            g['batch_sale_cents'] += r['amount_cents']
        for g in groups.values():
            g['difference_cents'] = g['batch_sale_cents'] - g['order_total_cents'] if g['comparable'] else None
        return list(groups.values())

    def get(self, c, batch_id):
        batch_id = text(batch_id, '批次编号',100)
        row = c.execute('SELECT data FROM settlement_batches WHERE id=?', (batch_id,)).fetchone()
        if not row:
            raise Problem('结算批次不存在',404)
        return json.loads(row['data'])

    def public(self, batch):
        return {k:v for k,v in batch.items() if k != 'dependencies'}

    def preview(self, b):
        if not isinstance(b,dict):
            raise Problem('预检格式无效')
        shop = text(b.get('shop_id'), '店铺编号',100)
        filename = text(b.get('filename','结算文件'), '文件名',250)
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if not c.execute("SELECT id FROM ops_entities WHERE kind='shop' AND id=?", (shop,)).fetchone():
                raise Problem('请先建立并选择本地店铺')
            mapped,profile_fact,changes = self.profiles.apply(c,'settlement',b)
            raw = self.parse(mapped)
            rows = self.reconcile(c,shop,raw)
            self.profiles.annotate(rows,changes)
            summary, totals = self.summary(rows)
            batch = {'id':ident(), 'created_at':now(), 'filename':filename, 'shop_id':shop, 'format':b['format'], 'status':'preview',
                     'token':secrets.token_urlsafe(32),'profile_fact':profile_fact,'transformations':changes,'rows':rows,'summary':summary,'currency_totals':totals,
                     'order_checks':self.order_checks(rows),'dependencies':self.dependencies(c,shop,rows),'notice':'仅支持本地标准模板；未连接noon或银行，确认只登记收入费用，不产生收付款。'}
            c.execute('INSERT INTO settlement_batches VALUES(?,?,?)', (batch['id'],batch['created_at'],json.dumps(batch,ensure_ascii=False)))
            self.store.event(c,None,'结算文件预检',batch['id'])
            return self.public(batch)

    def apply(self, b):
        if not isinstance(b,dict):
            raise Problem('确认格式无效')
        key = text(b.get('request_id'), '操作编号',100)
        digest = fingerprint(b)
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            prior = c.execute('SELECT * FROM settlement_requests WHERE key=?',(key,)).fetchone()
            if prior:
                if prior['digest'] != digest:
                    raise Problem('操作编号已用于其他内容',409)
                return json.loads(prior['result'])
            batch = self.get(c,b.get('batch_id'))
            if b.get('confirmed') is not True:
                raise Problem('请明确确认已核对原币、汇率、凭证及订单映射')
            if batch['status'] != 'preview':
                raise Problem('此批次已确认导入，请查看历史回执',409)
            if not isinstance(b.get('token'),str) or not secrets.compare_digest(b['token'],batch['token']):
                raise Problem('确认令牌无效，请重新预检',409)
            self.profiles.guard(c,batch.get('profile_fact'))
            if fingerprint(self.dependencies(c,batch['shop_id'],batch['rows'])) != fingerprint(batch['dependencies']):
                raise Problem('订单或已有凭证状态已改变，请重新预检文件',409)
            if not batch['summary']['ready']:
                raise Problem('没有可导入的合法行，请修正文件后重新预检')
            for row in batch['rows']:
                if row['status'] == 'ready':
                    entry = self.finance.create(c,row['posting'])
                    row.update(status='imported',entry_id=entry['id'],reason='已确认登记；尚未登记实际收付款')
            batch.update(status='applied',applied_at=now(),request_id=key)
            batch['summary'], batch['currency_totals'] = self.summary(batch['rows'])
            batch['remaining_rows'] = [r['line'] for r in batch['rows'] if r['status'] != 'imported']
            c.execute('UPDATE settlement_batches SET data=? WHERE id=?',(json.dumps(batch,ensure_ascii=False),batch['id']))
            result = self.public(batch)
            c.execute('INSERT INTO settlement_requests VALUES(?,?,?)',(key,digest,json.dumps(result,ensure_ascii=False)))
            self.store.event(c,None,'结算确认导入',batch['id']+' · '+str(batch['summary']['imported'])+'行')
            return result

    def state(self, page=0):
        if isinstance(page,bool) or not str(page).isdecimal() or not 0 <= int(page) <= 1000000:
            raise Problem('结算页码无效')
        with self.store.connect() as c:
            c.execute('BEGIN')
            total = c.execute('SELECT count(*) FROM settlement_batches').fetchone()[0]
            pages = max(1,(total+9)//10)
            page = min(int(page),pages-1)
            batches = [self.public(json.loads(r['data'])) for r in c.execute('SELECT data FROM settlement_batches ORDER BY created_at DESC,id DESC LIMIT 10 OFFSET ?',(page*10,))]
            entities = [json.loads(r['data']) for r in c.execute("SELECT data FROM ops_entities WHERE kind='shop' ORDER BY name,id")]
            return {'profiles':self.profiles.choices(c,'settlement'),'entities':entities,'batches':batches,'page':page,'pages':pages,'total':total,'template_fields':FIELDS,'categories':list(CATEGORIES),'currencies':list(CURRENCIES)}

    def export(self, batch_id):
        with self.store.connect() as c:
            batch = self.get(c,batch_id)
        headers = ['batch_id','shop_id','line','status','reason']+FIELDS+['order_id','order_revision','order_currency','order_total_cents','cross_currency','entry_id','raw_json']
        rows = [headers]
        for row in batch['rows']:
            rows.append([batch['id'],batch['shop_id'],row['line'],row['status'],row['reason']]+[row.get(k,'') for k in FIELDS]+[row.get(k,'') for k in headers[14:-1]]+[json.dumps(row['raw'],ensure_ascii=False)])
        return csv_bytes(rows)
