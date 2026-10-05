"""Local periodic, verified archives. Retention owns only explicitly registered ZIPs."""
import hashlib
import json
import os
import re
import stat
import threading
import time
from datetime import datetime, timezone
from core import Problem, ident

SCHEMA_SQL = '''
CREATE TABLE IF NOT EXISTS backup_schedule_config (
 id INTEGER PRIMARY KEY CHECK(id=1), enabled INTEGER NOT NULL DEFAULT 0,
 interval_minutes INTEGER NOT NULL DEFAULT 1440, retain_count INTEGER NOT NULL DEFAULT 7,
 version INTEGER NOT NULL DEFAULT 0, next_run_at REAL, updated_at TEXT NOT NULL DEFAULT '');
INSERT OR IGNORE INTO backup_schedule_config(id) VALUES(1);
CREATE TABLE IF NOT EXISTS backup_schedule_runs (
 id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, trigger TEXT NOT NULL,
 status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
 archive_id TEXT, archive_json TEXT, error TEXT NOT NULL DEFAULT '',
 cleanup_error TEXT NOT NULL DEFAULT '', deleted_at TEXT);
CREATE TABLE IF NOT EXISTS backup_schedule_requests (
 request_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL);
'''

class BackupSchedules:
    def __init__(self, app, clock=None):
        self.app=app; self.store=app.store; self.clock=clock or time.time
        self._run_lock=threading.Lock(); self._wake=threading.Event(); self._stop=threading.Event(); self._thread=None; self._scheduler_error=''
        with self.store.connect() as c:
            c.executescript(SCHEMA_SQL)
            c.execute("UPDATE backup_schedule_runs SET status='interrupted',finished_at=?,error='应用在备份完成登记前中断；未登记的文件不会自动清理' WHERE status='running'",(self._iso(),))
            cfg=c.execute('SELECT * FROM backup_schedule_config WHERE id=1').fetchone()
            if cfg['enabled'] and (cfg['next_run_at'] is None or cfg['next_run_at']<=self.clock()):
                c.execute('UPDATE backup_schedule_config SET next_run_at=? WHERE id=1',(self.clock()+cfg['interval_minutes']*60,))

    def _iso(self):
        return datetime.fromtimestamp(self.clock(), timezone.utc).isoformat()

    def _pending(self):
        return self.app.recovery.pending.exists()

    def state(self):
        with self.store.connect() as c:
            cfg=dict(c.execute('SELECT * FROM backup_schedule_config WHERE id=1').fetchone())
            rows=c.execute('SELECT * FROM backup_schedule_runs ORDER BY rowid DESC LIMIT 50').fetchall()
        cfg['enabled']=bool(cfg['enabled'])
        cfg['next_run_at']=datetime.fromtimestamp(cfg['next_run_at'],timezone.utc).isoformat() if cfg['next_run_at'] is not None else None
        history=[self._record(row) for row in rows]
        return {**cfg,'history':history,'latest':history[0] if history else None,
                'running':self._run_lock.locked(),'recovery_pending':self._pending(),
                'directory':str(self.app.recovery.archives),'local_only':True,'scheduler_error':self._scheduler_error}

    def _record(self,row):
        out=dict(row); raw=out.pop('archive_json'); out['archive']=json.loads(raw) if raw else None
        return out

    def _request(self,body,kind):
        key=body.get('request_id')
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}',key):
            raise Problem('请提供有效的备份操作请求编号')
        fingerprint=hashlib.sha256(json.dumps([kind,body],sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        return key,fingerprint

    def _replay(self,c,key,fingerprint):
        row=c.execute('SELECT * FROM backup_schedule_requests WHERE request_id=?',(key,)).fetchone()
        if row:
            if row['fingerprint']!=fingerprint:raise Problem('同一请求编号不能用于不同操作',409)
            result=json.loads(row['result'])
            if result.get('status')=='running':
                run=c.execute('SELECT * FROM backup_schedule_runs WHERE request_id=?',(key,)).fetchone()
                if run:result=self._record(run)
            return {**result,'replayed':True}
        run=c.execute('SELECT * FROM backup_schedule_runs WHERE request_id=?',(key,)).fetchone()
        if run:raise Problem('该请求已执行或中断，请查看备份历史',409)

    def _version(self,c,body):
        cfg=c.execute('SELECT * FROM backup_schedule_config WHERE id=1').fetchone()
        if type(body.get('version')) is not int or body['version']!=cfg['version']:
            raise Problem('备份规则已变化，请刷新后重试',409)
        return cfg

    def save(self,body):
        key,fp=self._request(body,'save')
        with self.app.write_lock,self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE'); old=self._replay(c,key,fp)
            if old:return old
            self._version(c,body)
            enabled=body.get('enabled'); interval=body.get('interval_minutes'); retain=body.get('retain_count')
            if type(enabled) is not bool or type(interval) is not int or not 60<=interval<=525600 or type(retain) is not int or not 1<=retain<=30:
                raise Problem('间隔须为60至525600分钟，保留数量须为1至30份')
            if enabled and body.get('confirmed') is not True:raise Problem('请明确确认启用本地周期备份及保留清理规则')
            if self._pending():raise Problem('已安排恢复，恢复完成并核对后再修改周期规则',409)
            c.execute('UPDATE backup_schedule_config SET enabled=?,interval_minutes=?,retain_count=?,version=version+1,next_run_at=?,updated_at=? WHERE id=1',
                      (enabled,interval,retain,self.clock()+interval*60 if enabled else None,self._iso()))
            result={'saved':True,'version':body['version']+1,'replayed':False}
            c.execute('INSERT INTO backup_schedule_requests VALUES(?,?,?)',(key,fp,json.dumps(result)))
        self._wake.set(); return result

    def run(self,body):
        return self._execute(body,False)

    def _execute(self,body,scheduled):
        key,fp=self._request(body,'run')
        with self.app.write_lock,self.app.recovery.lock:
            if not self._run_lock.acquire(blocking=False):raise Problem('已有备份正在运行',409)
            try:
                with self.store.connect() as c:
                    c.execute('BEGIN IMMEDIATE'); old=self._replay(c,key,fp)
                    if old:return old
                    cfg=self._version(c,body)
                    if not scheduled and body.get('confirmed') is not True:raise Problem('请明确确认创建完整本地备份并按保留规则清理周期副本')
                    if self._pending():raise Problem('已安排恢复，周期备份暂不执行',409)
                    if scheduled and (not cfg['enabled'] or cfg['next_run_at'] is None or cfg['next_run_at']>self.clock()):return None
                    rid=ident()
                    c.execute("INSERT INTO backup_schedule_runs(id,request_id,trigger,status,started_at) VALUES(?,?,?,'running',?)",(rid,key,'scheduled' if scheduled else 'manual',self._iso()))
                    # Advance before creating a potentially expensive archive. Errors never busy-retry.
                    if cfg['enabled']:c.execute('UPDATE backup_schedule_config SET next_run_at=? WHERE id=1',(self.clock()+cfg['interval_minutes']*60,))
                    c.execute('INSERT INTO backup_schedule_requests VALUES(?,?,?)',(key,fp,json.dumps({'id':rid,'status':'running','replayed':False})))
                try:
                    archive=self.app.recovery.create('scheduled:'+rid)
                    with self.store.connect() as c:
                        c.execute("UPDATE backup_schedule_runs SET status='success',archive_id=?,archive_json=?,finished_at=? WHERE id=?",(archive['id'],json.dumps(archive,ensure_ascii=False),self._iso(),rid))
                    cleanup=self._retain(cfg['retain_count'])
                    if cleanup:
                        with self.store.connect() as c:c.execute('UPDATE backup_schedule_runs SET cleanup_error=? WHERE id=?',('\n'.join(cleanup),rid))
                except Exception as exc:
                    with self.store.connect() as c:c.execute("UPDATE backup_schedule_runs SET status='failed',finished_at=?,error=? WHERE id=?",(self._iso(),str(exc),rid))
                with self.store.connect() as c:
                    result={**self._record(c.execute('SELECT * FROM backup_schedule_runs WHERE id=?',(rid,)).fetchone()),'replayed':False}
                    c.execute('UPDATE backup_schedule_requests SET result=? WHERE request_id=?',(json.dumps(result,ensure_ascii=False),key))
                return result
            finally:self._run_lock.release()

    def _retain(self,count):
        with self.store.connect() as c:
            rows=c.execute("SELECT * FROM backup_schedule_runs WHERE status='success' AND deleted_at IS NULL ORDER BY rowid DESC").fetchall()
        errors=[]
        for row in rows[count:]:
            try:
                self._delete_owned(row)
                with self.store.connect() as c:c.execute('UPDATE backup_schedule_runs SET deleted_at=? WHERE id=?',(self._iso(),row['id']))
            except Exception as exc:
                errors.append(f"副本 {row['archive_id']} 未清理：{exc}")
        return errors

    def _delete_owned(self,row):
        """Never follow symlinks or delete based solely on a label or filename."""
        archive=json.loads(row['archive_json']); key=row['archive_id']; folder=self.app.recovery.archives
        if not isinstance(key,str) or not re.fullmatch('[a-f0-9]{32}',key) or archive['id']!=key or archive['label']!='scheduled:'+row['id']:
            raise Problem('副本归属登记不一致')
        if folder.is_symlink() or folder.parent.is_symlink() or folder.resolve()!=self.store.root.resolve()/'recovery'/'archives':
            raise Problem('备份目录存在链接或路径变化')
        root_fd=os.open(self.store.root.resolve(),os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            home_fd=os.open('recovery',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=root_fd)
            try:directory=os.open('archives',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=home_fd)
            finally:os.close(home_fd)
        finally:os.close(root_fd)
        try:
            metadata=os.open(key+'.json',os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory)
            with os.fdopen(metadata,'r') as f:
                if not stat.S_ISREG(os.fstat(f.fileno()).st_mode) or json.load(f)!=archive:raise Problem('副本清单已变化')
            fd=os.open(key+'.zip',os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory)
            with os.fdopen(fd,'rb') as f:
                info=os.fstat(f.fileno()); h=hashlib.sha256()
                if not stat.S_ISREG(info.st_mode) or info.st_size!=archive['bytes']:raise Problem('副本文件已变化')
                for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
                if h.hexdigest()!=archive['sha256']:raise Problem('副本校验值已变化')
                current=os.stat(key+'.zip',dir_fd=directory,follow_symlinks=False)
                if (current.st_dev,current.st_ino)!=(info.st_dev,info.st_ino):raise Problem('副本在清理时发生变化')
                os.unlink(key+'.zip',dir_fd=directory)
            # Keep metadata and durable history, including every failure and cleanup warning.
        finally:os.close(directory)

    def tick(self):
        if self._pending():return None
        with self.store.connect() as c:cfg=c.execute('SELECT * FROM backup_schedule_config WHERE id=1').fetchone()
        if not cfg['enabled'] or cfg['next_run_at'] is None or cfg['next_run_at']>self.clock():return None
        return self._execute({'version':cfg['version'],'request_id':'schedule-'+ident()},True)

    @property
    def inflight(self):
        return self._run_lock.locked()

    def start(self):
        if self._thread and self._thread.is_alive():return
        self._stop.clear(); self._wake.clear()
        self._thread=threading.Thread(target=self._loop,name='local-backup-schedule',daemon=True); self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.tick(); self._scheduler_error=''
            except Exception as exc:
                # Storage failures wait a minute and remain visible until recovery.
                self._scheduler_error=str(exc)
            self._wake.wait(60); self._wake.clear()

    def close(self):
        self._stop.set();self._wake.set()
        if self._thread and self._thread is not threading.current_thread():self._thread.join(timeout=5)
