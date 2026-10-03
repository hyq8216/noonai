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
from pathlib import Path
from core import Problem, ident, now

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
'''

def compatible_legacy_schema(saved,expected):
    if not isinstance(saved,list) or any(not isinstance(row,list) or len(row)!=4 or not isinstance(row[1],str) for row in saved):return False
    present={row[1] for row in saved}
    missing_indexes={name for name in MIGRATION_INDEXES if name not in present}
    inbox_variants=[(),('source_inbox_config','source_inbox_files'),('source_inbox_video_config',),
        ('source_inbox_visual_config','source_inbox_video_config'),
        ('source_inbox_config','source_inbox_files','source_inbox_visual_config','source_inbox_video_config')]
    return any(saved==[row for row in expected if row[1] not in missing_indexes.union(missing_inbox,missing_terms,missing_budget,missing_presets,missing_leads)]
               for missing_inbox in inbox_variants for missing_terms in ((),('model_terms',))
               for missing_budget in ((),('visual_budget',)) for missing_presets in ((),('visual_presets',))
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
        self.pending=self.home/'pending.json';self.result=self.home/'last-restore.json';self.lock=threading.RLock()
        if expected is None:
            with closing(sqlite3.connect(self.root/'workbench.sqlite3')) as c:expected=schema(c)
        self.expected=expected
    def state(self):
        records=[]
        for f in self.archives.glob('*.json'):
            try:
                v=json.loads(f.read_text())
                if (self.archives/(v['id']+'.zip')).is_file():records.append(v)
            except (ValueError,KeyError,OSError):continue
        return {'archives':sorted(records,key=lambda r:r['created_at'],reverse=True)[:50],
                'pending':{k:v for k,v in json.loads(self.pending.read_text()).items() if k!='schema'} if self.pending.exists() else None,
                'last_restore':json.loads(self.result.read_text()) if self.result.exists() else None,
                'directory':str(self.archives),'max_archive_bytes':MAX_ARCHIVE}
    def archive_path(self,key):
        if not isinstance(key,str) or not re.fullmatch('[a-f0-9]{32}',key):raise Problem('备份编号无效')
        path=self.archives/(key+'.zip')
        if not path.is_file():raise Problem('备份不存在',404)
        return path
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
            result.update(id=key,label=label,bytes=out.stat().st_size,sha256=digest(out),files=len(entries))
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
        write_json(self.result,{'status':'rolled_back','finished_at':now(),'message':'恢复切换中断，已退回切换前资料；请检查后重新导入备份。','rollback_archive_id':p['rollback_archive_id']})
        self.pending.unlink(missing_ok=True)
    def apply_pending(self):
        """Called with the workspace process lock, before any App/worker is created."""
        if not self.pending.exists():return
        p=json.loads(self.pending.read_text());self.expected=p['schema']
        if p['phase']=='switching':self.rollback(p);return
        if p['phase']=='committed':
            self.finish(p);return
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
                write_json(self.result,{'status':'failed','finished_at':now(),'message':str(e) if isinstance(e,Problem) else '恢复检查未通过，原资料未替换；请检查磁盘空间和备份文件。'})
                self.pending.unlink(missing_ok=True)
    def finish(self,p):
        write_json(self.result,{'status':'restored','finished_at':now(),'source_created_at':p['created_at'],'rollback_archive_id':p['rollback_archive_id'],'message':'资料已恢复。自动化队列已暂停、模型服务已停用、商品上架审核已重置，请核对真实平台状态后再启用。'})
        self.pending.unlink(missing_ok=True)
