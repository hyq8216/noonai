"""Manual, versioned local exchange-rate evidence. Never posts accounting entries."""
import csv
import io
import json
import re
from decimal import Decimal, InvalidOperation, localcontext, ROUND_HALF_UP, ROUND_HALF_EVEN, ROUND_DOWN
from core import Problem, ident, now
from finance import CURRENCIES, day, fingerprint
from operations import text

SCHEMA_SQL = '''
CREATE TABLE IF NOT EXISTS fx_registry(id TEXT PRIMARY KEY,name TEXT NOT NULL,status TEXT NOT NULL,revision INTEGER NOT NULL,data TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS fx_registry_updated ON fx_registry(updated_at DESC,id);
CREATE TABLE IF NOT EXISTS fx_registry_versions(record_id TEXT NOT NULL,revision INTEGER NOT NULL,data TEXT NOT NULL,PRIMARY KEY(record_id,revision));
CREATE TABLE IF NOT EXISTS fx_registry_previews(token TEXT PRIMARY KEY,data TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fx_registry_requests(key TEXT PRIMARY KEY,digest TEXT NOT NULL,result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fx_registry_audit(id INTEGER PRIMARY KEY AUTOINCREMENT,record_id TEXT NOT NULL,action TEXT NOT NULL,revision INTEGER NOT NULL,data TEXT NOT NULL,created_at TEXT NOT NULL);
'''
ROUNDINGS = {'half_up': ROUND_HALF_UP, 'half_even': ROUND_HALF_EVEN, 'down': ROUND_DOWN}


def number(value, label, places, maximum, positive=False):
    if isinstance(value, bool) or value is None or value == '':
        raise Problem(label + '必须明确填写有效数字')
    try:
        n = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise Problem(label + '必须为有效数字')
    if not n.is_finite() or n < 0 or n > Decimal(maximum) or n.as_tuple().exponent < -places or (positive and n <= 0):
        raise Problem(label + '须在有效范围内且精度符合要求')
    return format(n, 'f')


class FXRegistry:
    def __init__(self, app):
        self.app = app
        self.store = app.store
        with self.store.connect() as c:
            c.executescript(SCHEMA_SQL)

    def current(self, c, record_id):
        row = c.execute('SELECT data FROM fx_registry WHERE id=?', (text(record_id, '汇率编号', 100),)).fetchone()
        if not row:
            raise Problem('汇率档案不存在', 404)
        return json.loads(row['data'])

    def prepare(self, c, body):
        if not isinstance(body, dict):
            raise Problem('汇率资料格式无效')
        old = self.current(c, body['id']) if body.get('id') else None
        if old:
            if type(body.get('revision')) is not int or body['revision'] != old['revision']:
                raise Problem('汇率版本已变化，请刷新', 409)
            if old['status'] != 'active':
                raise Problem('已撤销档案不可修订，请新建档案', 409)
        source, target = body.get('from_currency'), body.get('to_currency')
        if source not in CURRENCIES or target not in CURRENCIES:
            raise Problem('币种仅支持CNY/SAR/USD/AED')
        if source == target:
            raise Problem('同币种无需汇率，请选择不同币种')
        direction = body.get('direction')
        if direction not in ('to_per_from', 'from_per_to'):
            raise Problem('请明确汇率方向')
        start = day(body.get('effective_from'), '生效日期')
        end = day(body['effective_to'], '失效日期') if body.get('effective_to') else ''
        if end and end < start:
            raise Problem('失效日期不得早于生效日期')
        record = {'name': text(body.get('name'), '档案名称', 200), 'from_currency': source, 'to_currency': target,
                  'direction': direction, 'rate': number(body.get('rate'), '汇率', 12, '1000000', True),
                  'effective_from': start, 'effective_to': end, 'evidence': text(body.get('evidence'), '来源依据', 2000),
                  'note': text(body.get('note', ''), '备注', 2000, True), 'status': 'active', 'local_only': True,
                  'accounting_written': False, 'formula': ('原币金额×汇率' if direction == 'to_per_from' else '原币金额÷汇率')}
        if old and (source != old['from_currency'] or target != old['to_currency']):
            raise Problem('修订不能改变币种对，请新建档案')
        token = fingerprint([old['id'] if old else None, old['revision'] if old else 0, record])
        return record, old, token

    def preview(self, body):
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            record, old, token = self.prepare(c, body)
            c.execute('INSERT OR IGNORE INTO fx_registry_previews VALUES(?,?,?)', (token, json.dumps(record, ensure_ascii=False), now()))
            return {'record': record, 'preview_digest': token, 'local_only': True,
                    'message': '人工核对币种对、方向、生效期间和来源依据后保存；不会修改账务或价格。'}

    def mutation(self, action, body):
        if not isinstance(body, dict):
            raise Problem('汇率操作格式无效')
        key = body.get('request_id')
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,96}', key):
            raise Problem('请求编号无效')
        if body.get('confirmed') is not True:
            raise Problem('请人工核对并确认汇率操作')
        digest = fingerprint([action, body])
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            prior = c.execute('SELECT * FROM fx_registry_requests WHERE key=?', (key,)).fetchone()
            if prior:
                if prior['digest'] != digest:
                    raise Problem('请求编号已用于其他内容', 409)
                return json.loads(prior['result'])
            if action == 'save':
                record, old, token = self.prepare(c, body)
                if body.get('preview_digest') != token or not c.execute('SELECT 1 FROM fx_registry_previews WHERE token=?', (token,)).fetchone():
                    raise Problem('预览失效，请重新核对确认', 409)
                record.update(id=old['id'] if old else ident(), revision=old['revision'] + 1 if old else 1,
                              created_at=old['created_at'] if old else now(), updated_at=now())
            else:
                record = self.current(c, body.get('id'))
                if type(body.get('revision')) is not int or body['revision'] != record['revision']:
                    raise Problem('汇率版本已变化，请刷新', 409)
                if record['status'] != 'active':
                    raise Problem('档案已撤销', 409)
                record.update(status='cancelled', revision=record['revision'] + 1,
                              cancel_reason=text(body.get('reason'), '撤销原因', 1000), updated_at=now())
            encoded = json.dumps(record, ensure_ascii=False)
            c.execute('INSERT INTO fx_registry VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,status=excluded.status,revision=excluded.revision,data=excluded.data,updated_at=excluded.updated_at',
                      (record['id'], record['name'], record['status'], record['revision'], encoded, record['updated_at']))
            c.execute('INSERT INTO fx_registry_versions VALUES(?,?,?)', (record['id'], record['revision'], encoded))
            c.execute('INSERT INTO fx_registry_audit(record_id,action,revision,data,created_at) VALUES(?,?,?,?,?)',
                      (record['id'], action, record['revision'], json.dumps({'request_id': key, 'record': record}, ensure_ascii=False), now()))
            self.store.event(c, None, '本地汇率档案 · ' + action, record['id'])
            c.execute('INSERT INTO fx_registry_requests VALUES(?,?,?)', (key, digest, encoded))
            return record

    def save(self, body):
        return self.mutation('save', body)

    def cancel(self, body):
        return self.mutation('cancel', body)

    def get(self, record_id, history_page=0):
        if isinstance(history_page, bool) or not str(history_page).isdecimal() or int(history_page) > 100000:
            raise Problem('历史页码无效')
        history_page = int(history_page)
        with self.store.connect() as c:
            c.execute('BEGIN')
            result = self.current(c, record_id)
            total = c.execute('SELECT count(*) FROM fx_registry_versions WHERE record_id=?', (result['id'],)).fetchone()[0]
            pages = max(1, (total + 49) // 50)
            history_page = min(history_page, pages - 1)
            result.update(history_page=history_page, history_pages=pages, history_total=total)
            result['versions'] = [json.loads(r['data']) for r in c.execute('SELECT data FROM fx_registry_versions WHERE record_id=? ORDER BY revision DESC LIMIT 50 OFFSET ?', (result['id'], history_page * 50))]
            result['audit'] = [dict(r) for r in c.execute('SELECT action,revision,created_at FROM fx_registry_audit WHERE record_id=? ORDER BY id DESC LIMIT 50 OFFSET ?', (result['id'], history_page * 50))]
            return result

    def convert(self, body):
        if not isinstance(body, dict):
            raise Problem('换算参数无效')
        version = body.get('revision')
        if type(version) is not int or version < 1:
            raise Problem('请明确选择精确汇率版本')
        amount = number(body.get('amount'), '原币金额', 6, '1000000000000')
        dated = day(body.get('date'), '换算日期')
        rounding = body.get('rounding')
        if rounding not in ROUNDINGS:
            raise Problem('请明确选择舍入口径')
        with self.store.connect() as c:
            c.execute('BEGIN')
            current = self.current(c, body.get('id'))
            if current['status'] != 'active':
                raise Problem('撤销档案的任何版本均不能用于换算', 409)
            row = c.execute('SELECT data FROM fx_registry_versions WHERE record_id=? AND revision=?', (current['id'], version)).fetchone()
            if not row:
                raise Problem('所选汇率版本不存在', 404)
            record = json.loads(row['data'])
            if record['status'] != 'active' or dated < record['effective_from'] or (record['effective_to'] and dated > record['effective_to']):
                raise Problem('所选版本在换算日期不适用', 409)
            with localcontext() as ctx:
                ctx.prec = 60
                raw = Decimal(amount) * Decimal(record['rate']) if record['direction'] == 'to_per_from' else Decimal(amount) / Decimal(record['rate'])
                result = format(raw.quantize(Decimal('0.01'), rounding=ROUNDINGS[rounding]), 'f')
            return {'record_id': record['id'], 'revision': version, 'from_currency': record['from_currency'], 'to_currency': record['to_currency'],
                    'direction': record['direction'], 'rate': record['rate'], 'evidence': record['evidence'], 'amount': amount, 'date': dated,
                    'converted_amount': result, 'rounding': rounding, 'decimal_places': 2, 'formula': record['formula'],
                    'local_only': True, 'accounting_written': False, 'message': '仅展示换算预览，不记账，不更新已有账务或价格。'}

    def state(self, page=0, query=''):
        if isinstance(page, bool) or not str(page).isdecimal() or int(page) > 100000:
            raise Problem('页码无效')
        page = int(page)
        query = text(query, '搜索', 200, True)
        pattern = '%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
        with self.store.connect() as c:
            c.execute('BEGIN')
            where = "name LIKE ? ESCAPE '\\' OR json_extract(data,'$.from_currency') LIKE ? ESCAPE '\\' OR json_extract(data,'$.to_currency') LIKE ? ESCAPE '\\'"
            params = (pattern, pattern, pattern)
            total = c.execute('SELECT count(*) FROM fx_registry WHERE ' + where, params).fetchone()[0]
            pages = max(1, (total + 49) // 50)
            page = min(page, pages - 1)
            rows = [json.loads(r['data']) for r in c.execute('SELECT data FROM fx_registry WHERE ' + where + ' ORDER BY updated_at DESC,id LIMIT 50 OFFSET ?', (*params, page * 50))]
            return {'rows': rows, 'total': total, 'page': page, 'pages': pages, 'query': query, 'currencies': list(CURRENCIES), 'local_only': True}

    def export(self):
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(['档案编号','名称','版本','状态','原币','目标币','方向','汇率','生效日期','失效日期','来源依据','备注','撤销原因','仅展示不记账'])
        with self.store.connect() as c:
            for row in c.execute('SELECT data FROM fx_registry_versions ORDER BY record_id,revision'):
                r = json.loads(row['data'])
                values = [r.get(k, '') for k in ('id','name','revision','status','from_currency','to_currency','direction','rate','effective_from','effective_to','evidence','note','cancel_reason')] + [True]
                writer.writerow(["'" + str(v) if str(v).lstrip().startswith(('=','+','-','@')) or str(v).startswith(('\t','\r','\n')) else v for v in values])
        return ('\ufeff' + out.getvalue()).encode('utf-8')
