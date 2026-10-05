"""Verified local workspace archives and restart-only, journaled restore.

SQLite's backup API captures a consistent database. Referenced media and supplier
inbox files are copied with hashes. Credentials, temporary work and logs are not portable.
"""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
import zipfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from core import Problem, ident, now
from backup_agent import sync_launch_agent, _locked, release_schedule_lock

FORMAT=1
MAX_ARCHIVE=2*1024**3
MAX_EXPANDED=8*1024**3
MAX_FILES=100000
COMPONENTS=('workbench.sqlite3','workbench.sqlite3-wal','workbench.sqlite3-shm','assets','media','source-inbox')
MIGRATION_INDEXES=('idx_automation_items_run_id','idx_products_partner_sku','idx_products_source_sku','idx_media_assets_product_kind','idx_ops_documents_kind_updated','idx_visual_jobs_status_created','idx_source_inbox_files_status_updated','idx_media_tasks_product_video','idx_media_tasks_active','idx_media_tasks_status_updated','idx_media_tasks_updated','idx_automation_items_product_id','idx_automation_items_status','idx_automation_items_runnable','idx_automation_runs_status_at','idx_jobs_visual_check_status_created','idx_jobs_visual_check_source','idx_jobs_status_updated','idx_jobs_updated')
MIGRATION_INDEX_SQL='''
CREATE INDEX IF NOT EXISTS idx_automation_items_run_id ON automation_items(run_id);
CREATE INDEX IF NOT EXISTS idx_products_partner_sku ON products(json_extract(data,'$.partner_sku'));
CREATE INDEX IF NOT EXISTS idx_products_source_sku ON products(json_extract(data,'$.source_sku'));
CREATE INDEX IF NOT EXISTS idx_media_assets_product_kind ON media_assets(json_extract(data,'$.product_id'),json_extract(data,'$.kind'));
CREATE INDEX IF NOT EXISTS idx_ops_documents_kind_updated ON ops_documents(kind,updated_at DESC,id DESC);
CREATE INDEX IF NOT EXISTS idx_visual_jobs_status_created ON visual_jobs(status,created_at);
CREATE INDEX IF NOT EXISTS idx_source_inbox_files_status_updated ON source_inbox_files(status,updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_media_tasks_product_video ON media_tasks(json_extract(recipe,'$.product_video.product_id'));
CREATE INDEX IF NOT EXISTS idx_media_tasks_active ON media_tasks(status) WHERE status IN ('queued','running','cancelling');
CREATE INDEX IF NOT EXISTS idx_media_tasks_status_updated ON media_tasks(status,updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_media_tasks_updated ON media_tasks(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_automation_items_product_id ON automation_items(product_id);
CREATE INDEX IF NOT EXISTS idx_automation_items_status ON automation_items(status);
CREATE INDEX IF NOT EXISTS idx_automation_items_runnable ON automation_items(updated_at) WHERE status IN ('queued','waiting','approval');
CREATE INDEX IF NOT EXISTS idx_automation_runs_status_at ON automation_runs(status,run_at);
CREATE INDEX IF NOT EXISTS idx_jobs_visual_check_status_created ON jobs(kind,status,created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_visual_check_source ON jobs(json_extract(result,'$.visual_job_id')) WHERE kind='visual-check';
CREATE INDEX IF NOT EXISTS idx_jobs_status_updated ON jobs(status,updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_updated ON jobs(updated_at DESC);
CREATE TABLE IF NOT EXISTS runtime_state (
              key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
'''

def compatible_legacy_schema(saved,expected):
    if not isinstance(saved,list) or any(not isinstance(row,list) or len(row)!=4 or not isinstance(row[1],str) for row in saved):return False
    present={row[1] for row in saved}
    missing_indexes={name for name in MIGRATION_INDEXES if name not in present}
    runtime_variants=((),('runtime_state',))
    inbox_variants=[(),('source_inbox_config','source_inbox_files'),('source_inbox_video_config',),
        ('source_inbox_visual_config','source_inbox_video_config'),
        ('source_inbox_config','source_inbox_files','source_inbox_visual_config','source_inbox_video_config')]
    return any(saved==[row for row in expected if row[1] not in missing_indexes.union(missing_inbox,missing_terms,missing_budget,missing_presets,missing_leads,missing_runtime)]
               for missing_inbox in inbox_variants for missing_terms in ((),('model_terms',))
               for missing_budget in ((),('visual_budget',)) for missing_presets in ((),('visual_presets',)) for missing_runtime in runtime_variants
               for missing_leads in ((),('source_leads',)))
ASSET=re.compile(r'(assets|media)/[a-f0-9]{32}(?:-source|-preview)?\.(jpg|png|webp|mp4|webm)\Z')
INBOX=re.compile(r'(?:source-inbox/(?:updates/)?[^/\\]{1,255}\.(?:csv|json)|source-inbox/photos/[A-Za-z0-9._-]{1,100}/(?:rights\.txt|references\.txt|[A-Za-z0-9._-]{1,250}\.(?:jpg|jpeg|png|webp))|source-inbox/videos/[A-Za-z0-9._-]{1,100}/(?:rights\.txt|[A-Za-z0-9._-]{1,250}\.(?:mp4|mov|webm)))\Z',re.IGNORECASE)

def digest(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def write_json(path,value):
    path=Path(path);tmp=path.with_suffix('.tmp')
    with tmp.open('w') as f:
        os.chmod(tmp,0o600);json.dump(value,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
    os.replace(tmp,path)
    fd=os.open(path.parent,os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)

def schema(c):
    return [list(r) for r in c.execute("SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name")]

def inspect_database(path,expected):
    with closing(sqlite3.connect('file:'+str(Path(path).resolve())+'?mode=ro',uri=True)) as c:
        c.execute('PRAGMA trusted_schema=OFF')
        if schema(c)!=expected:raise Problem('备份的数据结构与此版本不兼容，请使用对应版本的软件恢复')
        if c.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise Problem('备份数据库完整性检查失败')
        files=set()
        for table in ('products','revisions'):
            for (data,) in c.execute(f'SELECT data FROM {table}'):
                for image in json.loads(data).get('images',[]):
                    for k in ('source','file'):
                        if image.get(k):files.add('assets/'+image[k])
        for (data,) in c.execute('SELECT data FROM media_assets'):
            item=json.loads(data)
            for k in ('file','preview'):
                if item.get(k):files.add('media/'+item[k])
        if any(not ASSET.fullmatch(f) for f in files):raise Problem('档案中存在无效素材路径，无法完整备份或恢复')
        counts={table:c.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in ('products','revisions','media_assets','ops_documents','finance_entries','automation_runs','visual_jobs')}
    return files,counts

def remove_component(path):
    if path.is_symlink() or path.is_file():path.unlink()
    elif path.exists():shutil.rmtree(path)

class Recovery:
    def __init__(self,root,expected=None):
        self.root=Path(root);self.home=self.root/'recovery';self.home.mkdir(mode=0o700,exist_ok=True);self.home.chmod(0o700)
        self.archives=self.home/'archives';self.imports=self.home/'imports'
        self.archives.mkdir(exist_ok=True);self.imports.mkdir(exist_ok=True)
        self.pending=self.home/'pending.json';self.result=self.home/'last-restore.json';self.schedule_file=self.home/'schedule.json';self.lock=threading.RLock()
        self.clock=lambda:datetime.now(timezone.utc);self.stop=threading.Event();self.thread=None
        if not self.schedule_file.exists():write_json(self.schedule_file,self.default_schedule())
        if expected is None:
            with closing(sqlite3.connect(self.root/'workbench.sqlite3')) as c:expected=schema(c)
        self.expected=expected
    def default_schedule(self):
        return {'enabled':False,'interval_hours':24,'keep_count':7,'next_run_at':None,'last_run_at':None,'last_backup_id':None,'last_error':None,'status':'disabled','background_agent':'application_only'}
    def _schedule(self):
        try:
            if self.schedule_file.is_symlink():raise ValueError('设置文件是符号链接')
            value=json.loads(self.schedule_file.read_text())
            if not isinstance(value,dict) or type(value.get('enabled')) is not bool:raise ValueError('设置结构无效')
            merged={**self.default_schedule(),**value}
            if (type(merged['interval_hours']) is not int or merged['interval_hours'] not in (6,12,24,48,168)
                or type(merged['keep_count']) is not int or not 1<=merged['keep_count']<=100
                or merged['status'] not in ('disabled','scheduled','running','interrupted','success','failed')):raise ValueError('周期或保留策略无效')
            if merged['enabled']:
                due=datetime.fromisoformat(merged['next_run_at'])
                if due.tzinfo is None:raise ValueError('执行时间缺少时区')
            return merged
        except (ValueError,OSError,TypeError,KeyError):pass
        return {**self.default_schedule(),'status':'failed','last_error':'定时备份设置文件无效，已暂停；请重新保存策略'}
    def configure_schedule(self,body):
        if not isinstance(body,dict) or type(body.get('enabled')) is not bool:raise Problem('请明确选择是否启用定时备份')
        interval=body.get('interval_hours',24);keep=body.get('keep_count',7)
        if type(interval) is not int or interval not in (6,12,24,48,168):raise Problem('备份周期需为6、12、24、48或168小时')
        if type(keep) is not int or not 1<=keep<=100:raise Problem('定时备份保留份数需为1到100')
        with self.lock:
            if self.pending.exists():raise Problem('已安排资料恢复，暂不能修改定时备份')
            current=self._schedule();enabled=body['enabled']
            current.update(enabled=enabled,interval_hours=interval,keep_count=keep,
                next_run_at=(self.clock()+timedelta(hours=interval)).isoformat() if enabled else None,
                last_error=None,status='scheduled' if enabled else 'disabled')
            write_json(self.schedule_file,current)
            try:
                current['background_agent']=sync_launch_agent(self.root,enabled)
            except Problem as exc:
                failed={**current,'enabled':False,'next_run_at':None,'status':'failed','background_agent':'registration_failed','last_error':str(exc)[:300]}
                write_json(self.schedule_file,failed)
                raise
            write_json(self.schedule_file,current)
            return current
    def tick_schedule(self,lock_held=False):
        fd=None
        if not lock_held:
            fd=_locked(self.home/'.backup-agent.lock')
            if fd is None:return False
        try:return self._tick_schedule_serialized()
        finally:release_schedule_lock(fd)
    def _tick_schedule_serialized(self):
        with self.lock:
            schedule=self._schedule()
            if not schedule.get('enabled') or not schedule.get('next_run_at'):return False
            try:due=datetime.fromisoformat(schedule['next_run_at'])<=self.clock()
            except (TypeError,ValueError):due=True
            if not due:return False
            started=self.clock();schedule.update(status='running',last_error=None,next_run_at=(started+timedelta(hours=schedule['interval_hours'])).isoformat());write_json(self.schedule_file,schedule)
            archive=None
            try:
                archive=self.create('scheduled')
                schedule.update(last_backup_id=archive['id'])
                deleted=self._prune_scheduled(schedule['keep_count'],archive['id'])
                schedule.update(status='success',last_run_at=started.isoformat(),last_error=None,last_pruned_count=deleted)
            except Exception as e:
                schedule.update(status='failed',last_run_at=started.isoformat(),last_error=str(e)[:300],last_pruned_count=0)
                if archive:schedule['last_backup_id']=archive['id']
            write_json(self.schedule_file,schedule)
            return True
    def _prune_scheduled(self,keep,protected_id):
        records=[]
        for f in self.archives.glob('*.json'):
            try:
                if f.is_symlink():continue
                value=json.loads(f.read_text());key=value['id'];path=self.archives/(key+'.zip')
                if value.get('label')=='scheduled' and re.fullmatch('[a-f0-9]{32}',key) and f.name==key+'.json' and path.is_file() and not path.is_symlink():records.append((value.get('created_at',''),key,f,path))
            except (ValueError,KeyError,OSError,TypeError):continue
        records.sort(reverse=True)
        protected=[r for r in records if r[1]==protected_id]
        if not protected:raise Problem('新定时备份未出现在保留清单中，已停止清理')
        retained=protected+ [r for r in records if r[1]!=protected_id][:keep-1]
        retained_ids={r[1] for r in retained};candidates=[r for r in records if r[1] not in retained_ids]
        for _,_,_,path in candidates:self.validate(path)
        for _,_,meta,path in candidates:
            meta.unlink()
            path.unlink()
        return len(candidates)
    def start(self):
        if self.thread and self.thread.is_alive():return
        self.stop.clear()
        schedule=self._schedule()
        try:
            schedule['background_agent']=sync_launch_agent(self.root,bool(schedule.get('enabled')))
            if schedule.get('background_agent')=='registered':schedule['last_error']=None
        except Problem as exc:
            schedule['background_agent']='registration_failed'
            schedule['last_error']=str(exc)[:300]
        write_json(self.schedule_file,schedule)
        if schedule.get('enabled') and schedule.get('status')=='running':
            schedule.update(status='interrupted',last_error='应用在定时备份过程中关闭；将尽快重新执行',next_run_at=self.clock().isoformat());write_json(self.schedule_file,schedule)
        self.thread=threading.Thread(target=self._schedule_loop,name='backup-scheduler',daemon=True);self.thread.start()
    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=65)
    def _schedule_loop(self):
        while not self.stop.wait(30):
            try:self.tick_schedule()
            except Exception:continue
    def state(self):
        records=[]
        for f in self.archives.glob('*.json'):
            try:
                if f.is_symlink():continue
                v=json.loads(f.read_text())
                if (not isinstance(v,dict) or not isinstance(v.get('id'),str)
                    or not re.fullmatch('[a-f0-9]{32}',v['id']) or f.name!=v['id']+'.json'
                    or not isinstance(v.get('created_at'),str) or type(v.get('bytes')) is not int
                    or v['bytes']<0 or not isinstance(v.get('counts'),dict)):continue
                archive=self.archives/(v['id']+'.zip')
                if archive.is_file() and not archive.is_symlink():records.append(v)
            except (ValueError,KeyError,OSError,TypeError):continue
        records=sorted(records,key=lambda r:r['created_at'],reverse=True)
        archive_files=[p for p in self.archives.glob('*.zip') if p.is_file() and not p.is_symlink()]
        bytes_used=sum(p.stat().st_size for p in archive_files)
        paired={r['id'] for r in records}
        orphan_count=sum(1 for p in archive_files if p.stem not in paired)
        newest=records[0] if records else None
        try:age_seconds=max(0,int((self.clock()-datetime.fromisoformat(newest['created_at'])).total_seconds())) if newest else None
        except (TypeError,ValueError):age_seconds=None
        try:
            disk=shutil.disk_usage(self.home)
            expanded=newest.get('expanded_bytes') if newest else None
            required=2*expanded+64*1024**2 if type(expanded) is int and expanded>=0 else None
            disk_state={'total_bytes':disk.total,'free_bytes':disk.free,'estimated_required_bytes':required,
                        'sufficient_for_recent_backup_size':disk.free>=required if required is not None else None}
        except OSError:disk_state={'total_bytes':None,'free_bytes':None,'estimated_required_bytes':None,'sufficient_for_recent_backup_size':None}
        schedule=self._schedule();schedule_overdue=False
        if schedule.get('enabled') and schedule.get('next_run_at'):
            try:schedule_overdue=datetime.fromisoformat(schedule['next_run_at'])<=self.clock()
            except (TypeError,ValueError):schedule_overdue=True
        return {'archives':records[:50],
                'archive_count':len(archive_files),'archive_bytes':bytes_used,
                'oldest_archive_at':records[-1]['created_at'] if records else None,
                'newest_archive_at':newest['created_at'] if newest else None,'newest_archive_age_seconds':age_seconds,
                'unpaired_archive_count':orphan_count,
                'pending':{k:v for k,v in json.loads(self.pending.read_text()).items() if k!='schema'} if self.pending.exists() else None,
                'last_restore':json.loads(self.result.read_text()) if self.result.exists() else None,
                'schedule':schedule,'schedule_overdue':schedule_overdue,'disk':disk_state,
                'directory':str(self.archives),'max_archive_bytes':MAX_ARCHIVE}
    def retention_preview(self,keep=10):
        if type(keep) is not int or not 1<=keep<=1000:raise Problem('保留数量需为1到1000之间的整数')
        records=[]
        with self.lock:
            for f in self.archives.glob('*.json'):
                try:
                    if f.is_symlink():continue
                    v=json.loads(f.read_text());key=v['id'];created=v['created_at']
                    if not re.fullmatch('[a-f0-9]{32}',key) or f.name!=key+'.json' or not isinstance(created,str):continue
                    path=self.archives/(key+'.zip')
                    if path.is_file() and not path.is_symlink():records.append({**v,'bytes':path.stat().st_size})
                except (ValueError,KeyError,OSError,TypeError):continue
            records.sort(key=lambda r:(r['created_at'],r['id']),reverse=True)
            retained=records[:keep];candidates=records[keep:]
            return {'keep':keep,'archive_count':len(records),'retained_count':len(retained),
                    'candidate_count':len(candidates),'candidate_bytes':sum(r['bytes'] for r in candidates),
                    'retained':[{'id':r['id'],'created_at':r['created_at'],'bytes':r['bytes'],'label':r.get('label','')} for r in retained],
                    'candidates':[{'id':r['id'],'created_at':r['created_at'],'bytes':r['bytes'],'label':r.get('label','')} for r in candidates],
                    'deletes_nothing':True}
    def archive_path(self,key):
        if not isinstance(key,str) or not re.fullmatch('[a-f0-9]{32}',key):raise Problem('备份编号无效')
        path=self.archives/(key+'.zip')
        if not path.is_file() or path.is_symlink():raise Problem('备份不存在',404)
        return path
    def restore_metrics(self,p,finished_at=None):
        finished=datetime.fromisoformat(finished_at) if finished_at else self.clock()
        started_value=p.get('restore_started_at') or p.get('scheduled_at')
        result={}
        try:
            started=datetime.fromisoformat(started_value)
            source=datetime.fromisoformat(p['created_at'])
            if started.tzinfo is None:started=started.replace(tzinfo=timezone.utc)
            if source.tzinfo is None:source=source.replace(tzinfo=timezone.utc)
            if finished.tzinfo is None:finished=finished.replace(tzinfo=timezone.utc)
            result['restore_duration_seconds']=max(0,int((finished-started).total_seconds()))
            result['backup_age_at_restore_seconds']=max(0,int((started-source).total_seconds()))
        except (TypeError,ValueError,KeyError):pass
        return result
    def create(self,label='manual'):
        with self.lock,tempfile.TemporaryDirectory(dir=self.home) as tmp:
            tmp=Path(tmp);db=tmp/'workbench.sqlite3'
            with closing(sqlite3.connect(self.root/'workbench.sqlite3')) as src,closing(sqlite3.connect(db)) as dst:src.backup(dst)
            files,counts=inspect_database(db,self.expected)
            inbox=self.root/'source-inbox'
            if inbox.is_dir():
                if inbox.is_symlink():raise Problem('投递箱目录不能是快捷链接')
                updates=inbox/'updates'
                if updates.is_symlink():raise Problem('供货更新目录不能是快捷链接')
                for folder in (inbox,updates):
                    if not folder.is_dir():continue
                    for path in folder.iterdir():
                        if path.suffix.lower() in ('.csv','.json'):
                            name=path.relative_to(self.root).as_posix()
                            if not INBOX.fullmatch(name) or path.is_symlink() or not path.is_file():raise Problem('投递箱包含无法安全备份的文件：'+name)
                            files.add(name)
                photos=inbox/'photos'
                if photos.is_symlink():raise Problem('原图投递目录不能是快捷链接')
                if photos.is_dir():
                    for folder in photos.iterdir():
                        if folder.name.startswith('.') and folder.name!='..':continue
                        if folder.is_symlink() or not folder.is_dir():raise Problem('原图投递箱包含无效的 SKU 文件夹')
                        for path in folder.iterdir():
                            if path.name in ('rights.txt','references.txt') or path.suffix.lower() in ('.jpg','.jpeg','.png','.webp'):
                                name=path.relative_to(self.root).as_posix()
                                if not INBOX.fullmatch(name) or path.is_symlink() or not path.is_file():raise Problem('原图投递箱包含无法安全备份的文件：'+name)
                                files.add(name)
                videos=inbox/'videos'
                if videos.is_symlink():raise Problem('原视频投递目录不能是快捷链接')
                if videos.is_dir():
                    for folder in videos.iterdir():
                        if folder.name.startswith('.') and folder.name!='..':continue
                        if folder.is_symlink() or not folder.is_dir():raise Problem('原视频投递箱包含无效的 SKU 文件夹')
                        for path in folder.iterdir():
                            if path.name=='rights.txt' or path.suffix.lower() in ('.mp4','.mov','.webm'):
                                name=path.relative_to(self.root).as_posix()
                                if not INBOX.fullmatch(name) or path.is_symlink() or not path.is_file():raise Problem('原视频投递箱包含无法安全备份的文件：'+name)
                                files.add(name)
            sizes=db.stat().st_size
            for name in files:
                path=self.root/name
                if path.is_symlink() or path.parent.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()) or not path.is_file():raise Problem('引用的素材缺失，未生成不完整备份：'+name)
                sizes+=path.stat().st_size
            if sizes>MAX_EXPANDED or len(files)+1>MAX_FILES:raise Problem('资料超过当前备份容量（8GB / 10万个文件）')
            if shutil.disk_usage(self.home).free<2*sizes+64*1024**2:raise Problem('磁盘空间不足，无法保存完整备份')
            key=ident();out=tmp/'archive.zip';entries={}
            with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED,compresslevel=3) as z:
                for name in ['workbench.sqlite3',*sorted(files)]:
                    path=db if name=='workbench.sqlite3' else self.root/name
                    before=digest(path);size=path.stat().st_size;z.write(path,name)
                    if digest(path)!=before:raise Problem('素材在备份过程中发生变化，请核对后重新备份')
                    entries[name]={'bytes':size,'sha256':before}
                manifest={'format':FORMAT,'app_version':'0.9.0','created_at':now(),'schema':self.expected,'counts':counts,'files':entries,'excluded':['credentials','.env','Codex login','logs','temporary work']}
                z.writestr('manifest.json',json.dumps(manifest,ensure_ascii=False))
            if out.stat().st_size>MAX_ARCHIVE:raise Problem('压缩备份超过2GB，当前版本无法导出')
            # Read every member, check references and SQLite before exposing a backup.
            self.validate(out)
            result={k:manifest[k] for k in ('created_at','app_version','counts')}
            result.update(id=key,label=label,bytes=out.stat().st_size,expanded_bytes=sizes,sha256=digest(out),files=len(entries))
            dest=self.archives/(key+'.zip');os.chmod(out,0o600);os.replace(out,dest)
            write_json(self.archives/(key+'.json'),result)
            return result
    def validate(self,archive,destination=None):
        try:
            if Path(archive).stat().st_size>MAX_ARCHIVE:raise Problem('备份文件超过2GB')
            with zipfile.ZipFile(archive) as z:
                infos=z.infolist();names=[i.filename for i in infos]
                if len(infos)>MAX_FILES+1 or len(set(names))!=len(names):raise Problem('备份文件数量过多或存在重复路径')
                if 'manifest.json' not in names or z.getinfo('manifest.json').file_size>16*1024**2:raise Problem('缺少有效的备份清单')
                if sum(i.file_size for i in infos)>MAX_EXPANDED+16*1024**2:raise Problem('备份解压大小超过8GB')
                for i in infos:
                    if i.filename not in ('manifest.json','workbench.sqlite3') and not (ASSET.fullmatch(i.filename) or INBOX.fullmatch(i.filename)):raise Problem('备份包含非许可路径或配置文件')
                    mode=i.external_attr>>16
                    if stat.S_ISLNK(mode) or i.is_dir() or i.flag_bits&1:raise Problem('不支持链接、目录条目或加密ZIP')
                m=json.loads(z.read('manifest.json'))
                migrate_schema=m.get('schema')!=self.expected and compatible_legacy_schema(m.get('schema'),self.expected)
                if m.get('format')!=FORMAT or (m.get('schema')!=self.expected and not migrate_schema):raise Problem('备份格式或数据结构与此版本不兼容')
                if set(m['files'])!=set(names)-{'manifest.json'} or 'workbench.sqlite3' not in m['files']:raise Problem('备份清单与实际文件不一致')
                if shutil.disk_usage(self.home).free<sum(i.file_size for i in infos)+64*1024**2:raise Problem('磁盘空间不足以检查备份')
                with tempfile.TemporaryDirectory(dir=self.home) as tmp:
                    target=Path(destination) if destination else Path(tmp)
                    target.mkdir(parents=True,exist_ok=True)
                    for name,entry in m['files'].items():
                        info=z.getinfo(name)
                        if type(entry.get('bytes')) is not int or entry['bytes']!=info.file_size:raise Problem('备份文件长度不一致')
                        h=hashlib.sha256();path=target/name;path.parent.mkdir(parents=True,exist_ok=True)
                        with z.open(name) as src,path.open('wb') as dst:
                            for block in iter(lambda:src.read(1024*1024),b''):h.update(block);dst.write(block)
                        if h.hexdigest()!=entry['sha256']:raise Problem('备份校验失败：'+name)
                    if migrate_schema:
                        from source_inbox import SCHEMA_SQL
                        from models import TERMS_SCHEMA_SQL
                        from visuals import VISUAL_BUDGET_SQL
                        from visual_presets import SCHEMA_SQL as VISUAL_PRESET_SQL
                        from source_leads import SCHEMA_SQL as SOURCE_LEADS_SQL
                        with closing(sqlite3.connect(target/'workbench.sqlite3')) as c:
                            c.executescript(SCHEMA_SQL+TERMS_SCHEMA_SQL+VISUAL_BUDGET_SQL+VISUAL_PRESET_SQL+SOURCE_LEADS_SQL+MIGRATION_INDEX_SQL)
                    refs,counts=inspect_database(target/'workbench.sqlite3',self.expected)
                    if refs!={name for name in m['files'] if ASSET.fullmatch(name)}:raise Problem('素材引用与备份文件不一致')
                    if counts!=m['counts']:raise Problem('备份记录数量与清单不一致')
                return {k:m[k] for k in ('created_at','app_version','counts')}
        except Problem:raise
        except (zipfile.BadZipFile,KeyError,TypeError,ValueError,sqlite3.Error,RuntimeError,OSError) as e:
            raise Problem('备份无法验证，请使用完整的 Noon Studio 备份文件') from e
    def inspect(self,path):
        with self.lock:
            info=self.validate(path);key=ident();dest=self.imports/(key+'.zip');shutil.copyfile(path,dest);dest.chmod(0o600)
            info.update(id=key,sha256=digest(dest),bytes=dest.stat().st_size)
            write_json(self.imports/(key+'.json'),info);return info
    def schedule(self,body):
        with self.lock:
            if self.pending.exists():raise Problem('已有待恢复任务，请先取消或重新打开应用',409)
            key=body.get('id')
            if not isinstance(key,str) or not re.fullmatch('[a-f0-9]{32}',key):raise Problem('请先检查备份文件')
            path=self.imports/(key+'.zip')
            if body.get('confirmed') is not True:raise Problem('请确认替换本地资料和暂停自动化')
            if not path.is_file() or digest(path)!=body.get('sha256'):raise Problem('备份文件发生变化，请重新检查',409)
            preview=self.validate(path)
            # This transaction both checks active work and prevents workers from taking
            # new jobs. HTTP mutations are serialized by the app's write lock.
            with closing(sqlite3.connect(self.root/'workbench.sqlite3')) as c:
                with c:
                    c.execute('BEGIN IMMEDIATE')
                    for table,where in [('jobs',"status IN ('queued','running')"),('model_calls',"status='calling'"),('media_tasks',"status IN ('running','cancelling')"),('visual_jobs',"status IN ('preparing','generating')"),('automation_items',"status='processing'")]:
                        if c.execute(f'SELECT 1 FROM {table} WHERE {where} LIMIT 1').fetchone():raise Problem('仍有正在执行或等待执行的商品任务，请先完成或暂停后再安排恢复',409)
                    c.execute('UPDATE media_control SET paused=1');c.execute('UPDATE visual_control SET paused=1')
                    c.execute("UPDATE automation_runs SET status='paused' WHERE status NOT IN ('done','cancelled')")
            pending={**preview,'id':key,'sha256':body['sha256'],'phase':'scheduled','scheduled_at':now(),'schema':self.expected}
            write_json(self.pending,pending)
            return {'scheduled':True,'message':'恢复已安排。请退出并重新打开应用；取消恢复也不会自动重启队列。'}
    def cancel(self):
        with self.lock:
            if self.pending.exists():
                p=json.loads(self.pending.read_text())
                if p['phase']!='scheduled':raise Problem('恢复已进入切换阶段，不能取消',409)
                self.pending.unlink()
            return {'cancelled':True,'message':'已取消恢复；队列保持暂停，请核对后手动恢复。'}
    def paused_copy(self,path):
        with closing(sqlite3.connect(path)) as c:
            with c:
                c.execute('UPDATE media_control SET paused=1');c.execute('UPDATE visual_control SET paused=1')
                c.execute("UPDATE automation_runs SET status='paused' WHERE status NOT IN ('done','cancelled')")
                c.execute("UPDATE automation_items SET status='attention',message='从备份恢复，请先核对实际执行结果' WHERE status='processing'")
                c.execute("UPDATE jobs SET status='failed',message='从备份恢复，旧请求不会自动重发；请核对平台或模型结果' WHERE status IN ('queued','running')")
                c.execute("UPDATE media_tasks SET status='interrupted',message='从备份恢复，请核对后手动重试' WHERE status IN ('running','cancelling')")
                c.execute("UPDATE visual_jobs SET status=CASE WHEN dispatched_at IS NULL THEN 'blocked' ELSE 'uncertain' END,message='从备份恢复，旧请求不会自动重发' WHERE status IN ('preparing','generating')")
                c.execute("UPDATE jobs SET status='paused',message='从备份恢复，请核对后恢复检查' WHERE kind='visual-check' AND status IN ('waiting_image','waiting','paused')")
                c.execute("UPDATE products SET approved_revision=NULL")
                if c.execute("SELECT 1 FROM sqlite_master WHERE name='source_inbox_config'").fetchone():c.execute('UPDATE source_inbox_config SET enabled=0')
                for pid,data in c.execute('SELECT id,data FROM model_profiles').fetchall():
                    item=json.loads(data);item['enabled']=False
                    c.execute('UPDATE model_profiles SET data=?,revision=revision+1 WHERE id=?',(json.dumps(item,ensure_ascii=False),pid))
            c.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            c.execute('PRAGMA journal_mode=DELETE')
    def rollback(self,p):
        old=self.home/p['rollback_dir']
        for name in COMPONENTS:
            current=self.root/name;saved=old/name
            if saved.exists():
                remove_component(current);os.replace(saved,current)
            elif name not in p['original_components']:remove_component(current)
        finished=now();write_json(self.result,{'status':'rolled_back','finished_at':finished,'message':'恢复切换中断，已退回切换前资料；请检查后重新导入备份。','rollback_archive_id':p['rollback_archive_id'],**self.restore_metrics(p,finished)})
        self.pending.unlink(missing_ok=True)
    def apply_pending(self):
        """Called with the workspace process lock, before any App/worker is created."""
        if not self.pending.exists():return
        p=json.loads(self.pending.read_text());self.expected=p['schema']
        if p['phase']=='switching':self.rollback(p);return
        if p['phase']=='committed':
            self.finish(p);return
        if not p.get('restore_started_at'):
            p['restore_started_at']=self.clock().isoformat();write_json(self.pending,p)
        try:
            archive=self.imports/(p['id']+'.zip')
            if digest(archive)!=p['sha256']:raise Problem('待恢复文件已改变，原资料未替换')
            fresh=self.home/('restore-'+p['id'])
            if fresh.exists():shutil.rmtree(fresh)
            # Reserve for extracted files and the pre-restore archive, without
            # counting compression as free space.
            with zipfile.ZipFile(archive) as z:expanded=sum(i.file_size for i in z.infolist())
            current_size=sum(f.stat().st_size for name in ('assets','media','source-inbox') for f in (self.root/name).rglob('*') if f.is_file())
            if shutil.disk_usage(self.home).free<expanded+current_size+(self.root/'workbench.sqlite3').stat().st_size+128*1024**2:raise Problem('恢复所需磁盘空间不足，原资料未替换')
            self.validate(archive,fresh);self.paused_copy(fresh/'workbench.sqlite3')
            rollback=self.create('before-restore')
            old=self.home/('rollback-'+ident());old.mkdir()
            p.update(phase='switching',rollback_dir=old.name,rollback_archive_id=rollback['id'],original_components=[n for n in COMPONENTS if (self.root/n).exists()])
            write_json(self.pending,p)
            for name in COMPONENTS:
                src=self.root/name
                if src.exists():os.replace(src,old/name)
                if (fresh/name).exists():os.replace(fresh/name,src)
            p['phase']='committed';write_json(self.pending,p);self.finish(p)
        except Exception as e:
            if p['phase']=='switching':self.rollback(p)
            elif p['phase']=='committed':raise  # Keep the journal; next startup completes the success record.
            else:
                finished=now();write_json(self.result,{'status':'failed','finished_at':finished,'message':str(e) if isinstance(e,Problem) else '恢复检查未通过，原资料未替换；请检查磁盘空间和备份文件。',**self.restore_metrics(p,finished)})
                self.pending.unlink(missing_ok=True)
    def finish(self,p):
        finished=now();write_json(self.result,{'status':'restored','finished_at':finished,'source_created_at':p['created_at'],'rollback_archive_id':p['rollback_archive_id'],'message':'资料已恢复。自动化队列已暂停、模型服务已停用、商品上架审核已重置，请核对真实平台状态后再启用。',**self.restore_metrics(p,finished)})
        self.pending.unlink(missing_ok=True)
