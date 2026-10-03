"""Confirmed, bounded schedules for existing read-only source collection.

A durable dispatch intent is never replayed automatically. An interruption between
SQLite and executor submission becomes visible attention, requiring human review.
"""
import json
import threading
import time
from contextlib import nullcontext
from core import Problem, ident
from source_collection import page_number, request_id
from source_import import digest

SCHEMA_SQL = '''
CREATE TABLE IF NOT EXISTS collection_schedule_rules(
 id TEXT PRIMARY KEY, name TEXT NOT NULL, account_id TEXT NOT NULL,
 account_revision INTEGER NOT NULL, query TEXT NOT NULL, page_limit INTEGER NOT NULL,
 interval_minutes INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
 revision INTEGER NOT NULL DEFAULT 1, next_run REAL, last_run REAL,
 last_run_id TEXT, acknowledged_run_id TEXT, pending_request TEXT, pause_reason TEXT NOT NULL DEFAULT '',
 failure TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, updated_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS collection_schedule_due ON collection_schedule_rules(enabled,next_run);
CREATE TABLE IF NOT EXISTS collection_schedule_requests(
 key TEXT PRIMARY KEY, digest TEXT NOT NULL, result TEXT NOT NULL);
'''


class CollectionSchedules:
    def __init__(self, app, clock=None):
        self.app = app
        self.store = app.store
        self.clock = clock or time
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.deadlines = {}
        with self.store.connect() as c:
            c.executescript(SCHEMA_SQL)
            # Restart never catches up missed periods. A saved intent is uncertain.
            timestamp = self.clock.time()
            c.execute('UPDATE collection_schedule_rules SET next_run=?+interval_minutes*60 WHERE enabled=1', (timestamp,))

    def _get(self, c, rule_id):
        row = c.execute('SELECT * FROM collection_schedule_rules WHERE id=?', (rule_id,)).fetchone()
        if row is None:
            raise Problem('周期采集规则不存在', 404)
        result = dict(row)
        result['enabled'] = bool(result['enabled'])
        result['attention'] = bool(result['failure'] or result['pending_request'])
        result['last_run_status'] = None
        result['last_run_message'] = ''
        if result['last_run_id']:
            run = c.execute('SELECT status,message FROM source_collection_runs WHERE id=?', (result['last_run_id'],)).fetchone()
            if run:
                result['last_run_status'] = run['status']
                result['last_run_message'] = run['message']
                result['attention'] |= run['status'] in ('attention', 'failed') and result['acknowledged_run_id'] != result['last_run_id']
        return result

    def _pause(self, c, row, reason, failure=''):
        c.execute('UPDATE collection_schedule_rules SET enabled=0,next_run=NULL,pause_reason=?,failure=?,revision=revision+1,updated_at=? WHERE id=?',
                  (reason, failure, self.clock.time(), row['id']))
        self.deadlines.pop(row['id'], None)

    def _account(self, account_id, revision, c):
        account = self.app.channel_accounts.get(account_id, connection=c)
        if not account['enabled']:
            raise Problem('来源账号已停用', 409)
        if type(revision) is not int or revision != account['revision']:
            raise Problem('来源账号版本已变化，请重新核对规则', 409)
        if account['provider'] not in ('shopify', 'ebay', 'custom_json', 'json_api'):
            raise Problem('此渠道尚未开放在线只读采集', 409)
        if account.get('status') == 'attention':
            raise Problem('来源账号凭据需要核对', 409)
        return account

    def _reconcile(self):
        with self.lock, self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for raw in c.execute('SELECT * FROM collection_schedule_rules WHERE enabled=1 OR pending_request IS NOT NULL').fetchall():
                row = dict(raw)
                if row['pending_request']:
                    run = c.execute('SELECT id,status FROM source_collection_runs WHERE request_key=?', (row['pending_request'],)).fetchone()
                    if run:
                        c.execute('UPDATE collection_schedule_rules SET last_run_id=?,pending_request=NULL WHERE id=?', (run['id'], row['id']))
                        if run['status'] == 'queued':
                            c.execute("UPDATE source_collection_runs SET status='attention',message='周期调度提交被中断，请核对后明确处理采集任务' WHERE id=?", (run['id'],))
                    else:
                        c.execute('UPDATE collection_schedule_rules SET pending_request=NULL WHERE id=?', (row['id'],))
                    self._pause(c, row, '周期调度被中断，请核对后重新启用', '调度回执未确认；未自动重新发送')
                    continue
                if not row['enabled']:
                    continue
                try:
                    self._account(row['account_id'], row['account_revision'], c)
                    self.app.source_collection.ensure_recovery_clear()
                except Problem as error:
                    self._pause(c, row, str(error))
                    continue
                if row['last_run_id'] and row['last_run_id'] != row['acknowledged_run_id']:
                    run = c.execute('SELECT status,message FROM source_collection_runs WHERE id=?', (row['last_run_id'],)).fetchone()
                    if run is None or run['status'] in ('attention', 'failed', 'cancelled'):
                        reason = '最近采集需核对，请处理后明确恢复规则'
                        self._pause(c, row, reason, (run['message'] or reason) if run else '最近采集回执不存在')

    def state(self, page=0):
        page = page_number(page)
        self._reconcile()
        with self.lock, self.store.connect() as c:
            total = c.execute('SELECT count(*) FROM collection_schedule_rules').fetchone()[0]
            pages = max(1, (total + 49) // 50)
            page = min(page, pages - 1)
            rows = [self._get(c, r['id']) for r in c.execute('SELECT id FROM collection_schedule_rules ORDER BY created_at DESC,id LIMIT 50 OFFSET ?', (page * 50,))]
            enabled = c.execute('SELECT count(*) FROM collection_schedule_rules WHERE enabled=1').fetchone()[0]
        return dict(rules=rows, rows=rows, total=total, page=page, pages=pages, enabled_count=enabled)

    def _receipt(self, c, body, operation):
        if not isinstance(body, dict):
            raise Problem('规则参数无效')
        key = operation + ':' + request_id(body)
        if body.get('confirmed') is not True:
            raise Problem('请明确确认周期只读采集操作')
        fingerprint = digest(body)
        old = c.execute('SELECT digest,result FROM collection_schedule_requests WHERE key=?', (key,)).fetchone()
        if old:
            if old['digest'] != fingerprint:
                raise Problem('请求编号已用于不同规则操作', 409)
            return key, fingerprint, {**json.loads(old['result']), 'replayed': True}
        return key, fingerprint, None

    def _record(self, c, key, fingerprint, result):
        result['replayed'] = False
        c.execute('INSERT INTO collection_schedule_requests VALUES(?,?,?)', (key, fingerprint, json.dumps(result, ensure_ascii=False)))
        return result

    def _revision(self, body, row):
        if type(body.get('revision')) is not int or body['revision'] != row['revision']:
            raise Problem('规则版本已变化，请刷新后重试', 409)

    def _enable_check(self, c, row, body):
        self.app.source_collection.ensure_recovery_clear()
        if row and row['pending_request']:
            raise Problem('周期调度回执尚未核对，请刷新', 409)
        if row and row['last_run_id']:
            run = c.execute('SELECT status FROM source_collection_runs WHERE id=?', (row['last_run_id'],)).fetchone()
            if run and run['status'] in ('queued', 'running'):
                raise Problem('最近采集仍在执行，请等待结束后启用', 409)
            if (not run or run['status'] in ('attention', 'failed', 'cancelled')) and body.get('attention_checked') is not True:
                raise Problem('请先核对最近采集任务，并明确确认异常已处理', 409)
        if row and row['failure'] and body.get('attention_checked') is not True:
            raise Problem('请明确确认已核对调度异常', 409)

    def save(self, body):
        with self.lock, self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            key, fingerprint, replay = self._receipt(c, body, 'save')
            if replay:
                return replay
            name = body.get('name', '')
            query = body.get('query', '')
            limit = body.get('page_limit', 1)
            interval = body.get('interval_minutes')
            enabled = body.get('enabled', False)
            if not isinstance(name, str) or not name.strip() or len(name) > 120:
                raise Problem('请填写不超过120字的规则名称')
            if not isinstance(query, str) or len(query) > 500 or type(limit) is not int or not 1 <= limit <= 20:
                raise Problem('查询或采集页数无效（1至20页）')
            if type(interval) is not int or not 15 <= interval <= 525600 or type(enabled) is not bool:
                raise Problem('采集间隔须为15至525600分钟，启用状态须为布尔值')
            row = self._get(c, body['id']) if body.get('id') else None
            if row:
                self._revision(body, row)
            aid = body.get('account_id')
            if not isinstance(aid, str):
                raise Problem('请选择来源账号')
            account = self._account(aid, body.get('account_revision'), c)
            if account['provider'] == 'ebay' and not query.strip():
                raise Problem('eBay 周期采集需要搜索词')
            if enabled:
                self._enable_check(c, row, body)
            rid = row['id'] if row else ident()
            stamp = self.clock.time()
            if row:
                c.execute('UPDATE collection_schedule_rules SET name=?,account_id=?,account_revision=?,query=?,page_limit=?,interval_minutes=?,enabled=?,revision=revision+1,next_run=?,pause_reason=?,failure=?,updated_at=? WHERE id=?',
                          (name.strip(), aid, account['revision'], query, limit, interval, int(enabled), stamp + interval * 60 if enabled else None, '' if enabled else '手动暂停', '' if enabled else row['failure'], stamp, rid))
            else:
                if c.execute('SELECT count(*) FROM collection_schedule_rules').fetchone()[0] >= 1000:
                    raise Problem('周期规则最多1000条，请整理已有规则', 409)
                c.execute('INSERT INTO collection_schedule_rules(id,name,account_id,account_revision,query,page_limit,interval_minutes,enabled,next_run,pause_reason,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                          (rid, name.strip(), aid, account['revision'], query, limit, interval, int(enabled), stamp + interval * 60 if enabled else None, '' if enabled else '尚未启用', stamp, stamp))
            if enabled and body.get('attention_checked') is True:
                c.execute('UPDATE collection_schedule_rules SET acknowledged_run_id=last_run_id WHERE id=?', (rid,))
            self.deadlines.pop(rid, None)
            return self._record(c, key, fingerprint, self._get(c, rid))

    def control(self, body):
        # Reconcile first so a stale revision cannot erase a newly found pause.
        self._reconcile()
        with self.lock, self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            key, fingerprint, replay = self._receipt(c, body, 'control')
            if replay:
                return replay
            row = self._get(c, body.get('id'))
            self._revision(body, row)
            action = body.get('action')
            if action == 'pause':
                self._pause(c, row, '手动暂停', row['failure'])
            elif action == 'resume':
                self._account(row['account_id'], row['account_revision'], c)
                if type(body.get('account_revision')) is not int or body['account_revision'] != row['account_revision']:
                    raise Problem('请核对来源账号版本', 409)
                self._enable_check(c, row, body)
                c.execute("UPDATE collection_schedule_rules SET enabled=1,revision=revision+1,next_run=?,pause_reason='',failure='',updated_at=? WHERE id=?", (self.clock.time() + row['interval_minutes'] * 60, self.clock.time(), row['id']))
                # Keep the old task visible but acknowledge its attention once.
                if body.get('attention_checked') is True:
                    c.execute('UPDATE collection_schedule_rules SET acknowledged_run_id=last_run_id WHERE id=?', (row['id'],))
                self.deadlines.pop(row['id'], None)
            else:
                raise Problem('规则操作须为暂停或恢复')
            return self._record(c, key, fingerprint, self._get(c, row['id']))

    def tick(self):
        """Scan once; one rule never accumulates runs or replays an intent."""
        with getattr(self.app, 'write_lock', nullcontext()), self.lock:
            if self.stop_event.is_set():
                return 0
            self._reconcile()
            with self.store.connect() as c:
                rules = [dict(row) for row in c.execute('SELECT * FROM collection_schedule_rules WHERE enabled=1')]
            dispatched = 0
            for row in rules:
                stamp, mono = self.clock.time(), self.clock.monotonic()
                revision, deadline = self.deadlines.get(row['id'], (None, None))
                if revision != row['revision']:
                    deadline = mono + max(0, row['next_run'] - stamp)
                    self.deadlines[row['id']] = (row['revision'], deadline)
                if mono < deadline:
                    continue
                with self.store.connect() as c:
                    c.execute('BEGIN IMMEDIATE')
                    current = self._get(c, row['id'])
                    if not current['enabled'] or current['revision'] != row['revision']:
                        continue
                    if current['last_run_status'] in ('queued', 'running'):
                        # Wait for this task, then one future interval, never a backlog.
                        c.execute('UPDATE collection_schedule_rules SET next_run=? WHERE id=?', (stamp + row['interval_minutes'] * 60, row['id']))
                        self.deadlines[row['id']] = (row['revision'], mono + row['interval_minutes'] * 60)
                        continue
                    request = 'schedule_' + ident()
                    c.execute('UPDATE collection_schedule_rules SET pending_request=?,last_run=?,next_run=?,updated_at=? WHERE id=?', (request, stamp, stamp + row['interval_minutes'] * 60, stamp, row['id']))
                self.deadlines[row['id']] = (row['revision'], mono + row['interval_minutes'] * 60)
                run = None
                try:
                    run = self.app.source_collection.create(dict(request_id=request, account_id=row['account_id'], query=row['query'], page_limit=row['page_limit'], confirmed=True))
                    if run['account_revision'] != row['account_revision']:
                        raise Problem('来源账号在调度时变化，请核对后重新启用', 409)
                    with self.store.connect() as c:
                        c.execute('UPDATE collection_schedule_rules SET last_run_id=?,pending_request=NULL WHERE id=?', (run['id'], row['id']))
                    self.app.dispatch_collection(run)
                    dispatched += 1
                except Exception:
                    with self.store.connect() as c:
                        if run is None:
                            saved = c.execute('SELECT id FROM source_collection_runs WHERE request_key=?', (request,)).fetchone()
                            if saved:
                                run = dict(saved)
                        if run:
                            c.execute("UPDATE source_collection_runs SET status='attention',message='周期调度未确认提交，请核对后明确处理' WHERE id=? AND status='queued'", (run['id'],))
                            c.execute('UPDATE collection_schedule_rules SET last_run_id=?,pending_request=NULL WHERE id=?', (run['id'], row['id']))
                        else:
                            c.execute('UPDATE collection_schedule_rules SET pending_request=NULL WHERE id=?', (row['id'],))
                        self._pause(c, row, '周期采集调度失败，请核对后恢复', '调度未完成；未自动重试')
            return dispatched

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._work, name='collection-schedules', daemon=True)
            self.thread.start()

    def _work(self):
        while not self.stop_event.wait(5):
            try:
                self.tick()
            except Exception:
                # Persist visible attention rather than silently retrying forever.
                with self.store.connect() as c:
                    c.execute("UPDATE collection_schedule_rules SET enabled=0,next_run=NULL,revision=revision+1,pause_reason='周期调度器异常，请核对后恢复',failure='调度扫描失败' WHERE enabled=1")

    def close(self):
        self.stop_event.set()
        thread = self.thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=10)
