"""Preview-first, atomic edits to explicitly selected original product facts."""
import json
from core import Problem, clean, issues, number
from platform_batch import selection
from source_collection import page_number, request_id
from source_import import digest

SCHEMA_SQL = '''CREATE TABLE IF NOT EXISTS batch_editor_requests(
 key TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT NOT NULL);'''
FIELDS = {'supplier':'供应商', 'brand':'品牌', 'facts':'规格事实', 'cost_cny':'采购成本（人民币元）',
          'stock':'来源库存', 'title_zh':'中文商品名称', 'category':'类目编码'}


class BatchEditor:
    def __init__(self, app):
        self.app = app
        self.store = app.store
        with self.store.connect() as c:
            c.executescript(SCHEMA_SQL)

    def state(self, page=0, query='', group='all'):
        page = page_number(page)
        if not isinstance(query, str) or len(query) > 500:
            raise Problem('搜索内容无效')
        groups = {'all':'1', 'missing_facts':"trim(coalesce(json_extract(data,'$.facts'),''))=''",
                  'missing_cost':"json_extract(data,'$.cost_cny') IS NULL",
                  'zero_stock':"json_extract(data,'$.stock')=0",
                  'unreviewed':'approved_revision IS NULL OR approved_revision!=revision'}
        if not isinstance(group, str) or group not in groups:
            raise Problem('商品筛选无效')
        where = '(' + groups[group] + ')'
        params = []
        if query.strip():
            where += " AND instr(lower(coalesce(json_extract(data,'$.title_zh'),'')||' '||coalesce(json_extract(data,'$.partner_sku'),'')||' '||coalesce(json_extract(data,'$.source_sku'),'')||' '||coalesce(json_extract(data,'$.supplier'),'')||' '||coalesce(json_extract(data,'$.brand'),'')),lower(?))>0"
            params.append(query.strip())
        with self.store.connect() as c:
            c.execute('BEGIN')
            total = c.execute('SELECT count(*) FROM products WHERE '+where, params).fetchone()[0]
            pages = max(1, (total+49)//50)
            page = min(page, pages-1)
            rows = []
            for row in c.execute('SELECT id,data,revision,approved_revision FROM products WHERE '+where+' ORDER BY created_at DESC,id LIMIT 50 OFFSET ?', [*params, page*50]):
                data = json.loads(row['data'])
                rows.append(dict(id=row['id'], revision=row['revision'], approved_revision=row['approved_revision'],
                                 partner_sku=data.get('partner_sku'), source_sku=data.get('source_sku'),
                                 **{key:data.get(key) for key in FIELDS}))
        return dict(rows=rows, total=total, page=page, pages=pages, query=query, group=group)

    def _inputs(self, body):
        if not isinstance(body, dict):raise Problem('批量编辑参数无效')
        ids = selection(body)
        patch = body.get('patch')
        mode = body.get('mode')
        if not isinstance(patch, dict) or not patch or any(key not in FIELDS for key in patch):
            raise Problem('请明确选择至少一个可编辑原始事实字段')
        if mode not in ('fill_empty', 'replace'):
            raise Problem('请选择仅填空或明确替换模式')
        for key, value in patch.items():
            if key in ('stock', 'cost_cny'):
                if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float, str))):
                    raise Problem(FIELDS[key]+'格式无效')
                number(value, FIELDS[key], integer=key=='stock')
            elif not isinstance(value, str) or len(value)>24000:
                raise Problem(FIELDS[key]+'须为不超过24000字的文本')
        return ids, patch, mode

    def _tasks(self, c, ids):
        marks = ','.join('?' for unused in ids)
        grouped = {pid:[] for pid in ids}
        specs = [
            ('jobs', 'product_id', "status IN ('queued','running')", 'id,product_id,kind,revision,status,updated_at'),
            ('automation_items', 'product_id', "status NOT IN ('done','cancelled')", 'id,product_id,revision,step,status,attempt,updated_at'),
            ('visual_jobs', "json_extract(recipe,'$.product_id')", "status IN ('queued','waiting','preparing','generating')", 'id,status,recipe,updated_at'),
            ('media_tasks', "json_extract(recipe,'$.product_video.product_id')", "status IN ('queued','running','cancelling')", 'id,status,recipe,updated_at')]
        for table, product_expr, predicate, columns in specs:
            if not c.execute('SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=?', (table,)).fetchone():continue
            sql = f'SELECT {columns},{product_expr} AS bound_product FROM {table} WHERE {product_expr} IN ({marks}) AND {predicate} ORDER BY id'
            for task in c.execute(sql, ids):
                item = dict(task)
                grouped[item.pop('bound_product')].append({'table':table, **item})
        return grouped

    def preview(self, body, connection=None):
        ids, patch, mode = self._inputs(body)
        if connection is None:
            with self.store.connect() as c:
                c.execute('BEGIN')
                return self.preview(body, connection=c)
        c = connection
        tasks = self._tasks(c, ids)
        snapshots = []
        rows = []
        for pid in ids:
            raw = c.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone()
            snapshot = dict(raw) if raw else None
            snapshots.append([pid, snapshot, tasks[pid]])
            row = dict(id=pid, title='商品不存在', revision=None, status='blocked', fields=[], reasons=[], review_reasons=[])
            if not raw:
                row['reasons'] = ['商品已不存在，请移除选择后重新预检']
                rows.append(row)
                continue
            p = self.store.unpack(raw)
            row.update(title=p['title_zh'], revision=p['revision'], partner_sku=p['partner_sku'])
            if tasks[pid]:row['reasons'].append('已有未结束任务，请完成或取消后再编辑')
            candidate = {**p}
            for key, value in patch.items():
                old = p.get(key)
                empty = old is None or isinstance(old, str) and not old.strip()
                if mode == 'replace' or empty:candidate[key] = value
            try:
                normalized = clean(candidate)
                for key in patch:
                    old, new = p.get(key), normalized[key]
                    kept = mode == 'fill_empty' and not (old is None or isinstance(old, str) and not old.strip())
                    if kept:
                        new = old
                    changed = old != new
                    if kept:
                        reason = '已有值，仅填空模式保留（0也是已知值）'
                    elif not changed:reason = '值相同，不改变版本或审核'
                    elif new is None or new == '':reason = '明确替换为空值；请核对清空影响'
                    else:reason = '替换现有值' if mode=='replace' else '补充未知值'
                    row['fields'].append(dict(field=key, label=FIELDS[key], old=old, new=new, changed=changed, reason=reason))
                changed = any(field['changed'] for field in row['fields'])
                if changed:
                    row['review_reasons'].append('资料版本将递增，原审核失效，需要重新核对')
                    keys = {field['field'] for field in row['fields'] if field['changed']}
                    if keys.intersection(('title_zh','facts','brand')):
                        row['review_reasons'].append('双语内容与图片确认将失效，需按新事实复核')
                    if 'category' in keys:
                        row['review_reasons'].append('类目确认将失效，需重新核对类目规则与属性')
                    predicted = {**p, **normalized}
                    if keys.intersection(('title_zh','facts','brand')):
                        predicted.update(content_verified=False, images_verified=False)
                    if 'category' in keys:
                        predicted['category_verified'] = False
                    row['remaining_issues'] = issues(predicted)
                row['status'] = 'blocked' if row['reasons'] else 'change' if changed else 'unchanged'
            except Problem as error:
                row['reasons'].append(str(error))
                row['fields'] = [dict(field=key, label=FIELDS[key], old=p.get(key), new=candidate[key], changed=p.get(key)!=candidate[key], reason='输入不合法，请修正后预检') for key in patch]
            rows.append(row)
        counts = {status:sum(row['status']==status for row in rows) for status in ('change','unchanged','blocked')}
        return dict(rows=rows, changed=counts['change'], unchanged=counts['unchanged'], blocked=counts['blocked'],
                    can_apply=counts['change']>0 and counts['blocked']==0,
                    token=digest([ids, patch, mode, snapshots]), mode=mode, patch=patch)

    def apply(self, body):
        ids, patch, mode = self._inputs(body)
        key = 'batch-editor:' + request_id(body)
        if body.get('confirmed') is not True:
            raise Problem('请核对预检并明确确认修改本批原始事实')
        fingerprint = digest([ids, patch, mode, body.get('preview_token')])
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old = c.execute('SELECT * FROM batch_editor_requests WHERE key=?', (key,)).fetchone()
            if old:
                if old['digest'] != fingerprint:raise Problem('请求编号已用于不同批量编辑', 409)
                return {**json.loads(old['result']), 'replayed':True}
            pre = self.preview(body, connection=c)
            if pre['token'] != body.get('preview_token'):
                raise Problem('商品资料、版本、审核或在途任务已变化，请重新预检', 409)
            if pre['blocked']:
                raise Problem('本批存在无效资料或未结束任务，整批未修改，请先处理', 409)
            if not pre['changed']:
                raise Problem('没有需要修改的字段，商品版本保持不变', 409)
            changed = []
            for row in pre['rows']:
                if row['status'] != 'change':continue
                values = {field['field']:field['new'] for field in row['fields'] if field['changed']}
                updated = self.store.update(row['id'], values, row['revision'], connection=c)
                self.store.event(c, row['id'], '批量事实编辑', '明确选择字段：'+ '、'.join(FIELDS[key] for key in values) + '；' + ('仅填空' if mode=='fill_empty' else '替换'))
                changed.append(dict(id=updated['id'], revision=updated['revision']))
            result = dict(changed=changed, unchanged=[row['id'] for row in pre['rows'] if row['status']=='unchanged'], request_id=body['request_id'], replayed=False)
            c.execute('INSERT INTO batch_editor_requests VALUES(?,?,?)', (key, fingerprint, json.dumps(result, ensure_ascii=False)))
            return result
