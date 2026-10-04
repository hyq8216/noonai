"""Optional local supplier-file inbox with durable, idempotent intake records."""
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import threading
import tempfile
import time
from bisect import bisect_right
from pathlib import Path

from core import Problem, now
from media import MAX_UPLOAD
from media_import import MAX_IMAGE
from video_import import VIDEO_EXTENSIONS,VideoImport
from source_import import SourceImport
from source_updates import SourceUpdates

MAX_FILE=4*1024*1024
MAX_CATALOG_FILE=20*1024*1024
MAX_CATALOG_ROWS=5000
CATALOG_SCAN_LIMIT=100
PHOTO_SCAN_LIMIT=2000
VIDEO_SCAN_LIMIT=500

def rotating_paths(paths,cursor,limit):
    if not paths:return [],None
    start=bisect_right(paths,cursor) if cursor is not None else 0
    count=min(len(paths),limit)
    selected=paths[start:start+count]
    if len(selected)<count:selected+=paths[:count-len(selected)]
    return selected,selected[-1]
SCHEMA_SQL='''
            CREATE TABLE IF NOT EXISTS source_inbox_config(id INTEGER PRIMARY KEY CHECK(id=1),enabled INTEGER NOT NULL,translate INTEGER NOT NULL,review INTEGER NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS source_inbox_files(name TEXT NOT NULL,digest TEXT NOT NULL,status TEXT NOT NULL,message TEXT NOT NULL,result TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(name,digest));
            CREATE INDEX IF NOT EXISTS idx_source_inbox_files_status_updated ON source_inbox_files(status,updated_at DESC);
            CREATE TABLE IF NOT EXISTS source_inbox_visual_config(id INTEGER PRIMARY KEY CHECK(id=1),enabled INTEGER NOT NULL,recipe TEXT NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS source_inbox_video_config(id INTEGER PRIMARY KEY CHECK(id=1),enabled INTEGER NOT NULL,recipe TEXT NOT NULL,updated_at TEXT NOT NULL);
            INSERT OR IGNORE INTO source_inbox_config VALUES(1,0,0,0,'');
            '''

DEFAULT_VISUAL={'shots':['hero','scene','detail'],'model':'gpt-6-sol','aspect':'square',
                'style':'统一柔和光线，真实材质与商品比例；主图纯白背景，辅图展示经核对的适用场景。',
                'brief':'','auto_check':False}
DEFAULT_VIDEO={'aspect':'square','mute':True,'max_seconds':15,'cover':True}


class SourceInbox:
    def __init__(self,app):
        self.app=app
        self.folder=app.store.root/'source-inbox'
        if self.folder.is_symlink():raise Problem('投递箱目录不能是快捷链接')
        self.folder.mkdir(mode=0o700,exist_ok=True)
        self.updates=self.folder/'updates'
        if self.updates.is_symlink():raise Problem('供货更新目录不能是快捷链接')
        self.updates.mkdir(mode=0o700,exist_ok=True)
        self.photos=self.folder/'photos'
        if self.photos.is_symlink():raise Problem('原图投递目录不能是快捷链接')
        self.photos.mkdir(mode=0o700,exist_ok=True)
        self.videos=self.folder/'videos'
        if self.videos.is_symlink():raise Problem('原视频投递目录不能是快捷链接')
        self.videos.mkdir(mode=0o700,exist_ok=True)
        self.video_import=VideoImport(app.store,app.media)
        self.stop=threading.Event()
        self.lock=threading.RLock()
        self.seen={}
        self.finished={}
        self.visual_seen={}
        self.video_seen={}
        self.video_ready={}
        self.photo_cursor=None
        self.video_cursor=None
        self.catalog_cursors={'new':None,'updates':None}
        self.scan_progress={kind:{'checked':0,'total':0} for kind in ('new','updates','photos','videos')}
        self.thread=None
        self.last_error=None
        with app.store.connect() as c:
            c.executescript(SCHEMA_SQL)
            c.execute('INSERT OR IGNORE INTO source_inbox_visual_config VALUES(1,0,?,?)',
                      (json.dumps(DEFAULT_VISUAL,ensure_ascii=False),now()))
            c.execute('INSERT OR IGNORE INTO source_inbox_video_config VALUES(1,0,?,?)',
                      (json.dumps(DEFAULT_VIDEO,ensure_ascii=False),now()))

    def start(self):
        if self.thread and self.thread.is_alive():return
        self.stop.clear()
        self.thread=threading.Thread(target=self.loop,name='source-inbox',daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:self.thread.join(timeout=10)

    def loop(self):
        while not self.stop.wait(5):
            try:self.last_error=None;self.tick()
            except Exception:self.last_error='投递箱暂时无法检查，请刷新记录或重启软件'

    def config(self,c):
        return dict(c.execute('SELECT enabled,translate,review,updated_at FROM source_inbox_config WHERE id=1').fetchone())

    def state(self,include_files=True):
        with self.app.store.connect() as c:
            config=self.config(c)
            visual=self.visual_config(c)
            video=self.video_config(c)
            files=[dict(r) for r in c.execute('SELECT name,digest,status,message,result,updated_at FROM source_inbox_files ORDER BY updated_at DESC LIMIT 50')] if include_files else []
        return {'folder':str(self.folder),'updates_folder':str(self.updates),'photos_folder':str(self.photos),'videos_folder':str(self.videos),'enabled':bool(config['enabled']),'translate':bool(config['translate']),'review':bool(config['review']),'visual':visual,'video':video,'scan_progress':self.scan_progress,'updated_at':config['updated_at'],'files':[{**r,'result':json.loads(r['result'])} for r in files],'last_error':self.last_error,'running':bool(self.thread and self.thread.is_alive())}

    def list_files(self,page=0,group='attention',kind='all',query=''):
        if isinstance(page,bool) or not str(page).isdecimal() or not 0<=int(page)<=100000:raise Problem('投递箱记录页码无效')
        if group not in ('all','attention','processing','done'):raise Problem('投递箱状态筛选无效')
        if kind not in ('all','new','updates','photos','videos'):raise Problem('投递箱资料类型无效')
        if not isinstance(query,str) or len(query)>100:raise Problem('投递箱搜索词过长')
        parts=[];args=[]
        if group!='all':parts.append('status=?');args.append(group)
        if kind=='new':parts.append("name NOT LIKE '%/%'")
        elif kind!='all':parts.append('name LIKE ?');args.append(kind+'/%')
        if query.strip():parts.append('instr(name,?)>0');args.append(query.strip())
        where=' WHERE '+' AND '.join(parts) if parts else ''
        with self.app.store.connect() as c:
            c.execute('BEGIN')
            total=c.execute('SELECT count(*) FROM source_inbox_files'+where,args).fetchone()[0]
            pages=max(1,(total+19)//20);page=min(int(page),pages-1)
            rows=c.execute('SELECT name,digest,status,message,result,updated_at FROM source_inbox_files'+where+
                           ' ORDER BY updated_at DESC,rowid DESC LIMIT 20 OFFSET ?',(*args,page*20)).fetchall()
        files=[]
        for row in rows:
            result=json.loads(row['result'] or '{}')
            summary_keys=('issue_examples','error_row_count','asset_id','batches_done','batches_total','cataloged','updated','skipped','unchanged')
            files.append({**dict(row),'result':{k:result[k] for k in summary_keys if k in result},'row_count':len(result.get('rows') or [])})
        return {'files':files,'page':page,'pages':pages,'total':total,'group':group,'kind':kind,'query':query.strip()}

    def file_detail(self,name,digest):
        if not isinstance(name,str) or not isinstance(digest,str) or len(name)>400 or len(digest)>100:raise Problem('投递箱记录标识无效')
        with self.app.store.connect() as c:
            row=c.execute('SELECT name,digest,status,message,result,updated_at FROM source_inbox_files WHERE name=? AND digest=?',(name,digest)).fetchone()
        if not row:raise Problem('投递箱记录不存在',404)
        return {**dict(row),'result':json.loads(row['result'] or '{}')}

    def visual_config(self,c):
        row=c.execute('SELECT enabled,recipe,updated_at FROM source_inbox_visual_config WHERE id=1').fetchone()
        return {'enabled':bool(row['enabled']),'recipe':json.loads(row['recipe']),'updated_at':row['updated_at']}

    def video_config(self,c):
        row=c.execute('SELECT enabled,recipe,updated_at FROM source_inbox_video_config WHERE id=1').fetchone()
        return {'enabled':bool(row['enabled']),'recipe':json.loads(row['recipe']),'updated_at':row['updated_at']}

    def configure_video(self,body):
        if not isinstance(body,dict) or set(body)!={'enabled','recipe'} or type(body['enabled']) is not bool:raise Problem('原视频自动处理设置无效')
        raw=body['recipe']
        if not isinstance(raw,dict) or set(raw)!=set(DEFAULT_VIDEO):raise Problem('原视频处理配方无效')
        if raw['aspect'] not in ('square','portrait','landscape') or type(raw['mute']) is not bool or type(raw['cover']) is not bool:
            raise Problem('原视频画幅、静音或封面选项无效')
        try:seconds=int(raw['max_seconds'])
        except (TypeError,ValueError):raise Problem('原视频最长秒数无效')
        if isinstance(raw['max_seconds'],bool) or str(seconds)!=str(raw['max_seconds']) or not 3<=seconds<=60:
            raise Problem('原视频最长秒数需为3至60的整数')
        recipe={**raw,'max_seconds':seconds}
        with self.lock,self.app.store.connect() as c:
            c.execute('UPDATE source_inbox_video_config SET enabled=?,recipe=?,updated_at=? WHERE id=1',
                      (int(body['enabled']),json.dumps(recipe,ensure_ascii=False),now()))
            self.video_seen.clear()
        return self.state()

    def configure_visual(self,body):
        if not isinstance(body,dict) or set(body)!={'enabled','recipe'} or type(body['enabled']) is not bool:raise Problem('自动视觉设置无效')
        from workflow_visual import options
        recipe=options(body['recipe'])
        for key in ('image_host','submit'):
            if key in body['recipe'] and type(body['recipe'][key]) is not bool:raise Problem('自动视觉托管或刊登开关无效')
        if body['recipe'].get('image_host'):recipe['image_host']=True
        if body['recipe'].get('submit'):
            if not recipe.get('image_host'):raise Problem('自动刊登新成图前必须启用图片托管',409)
            recipe['submit']=True
        if body['recipe'].get('video') is not None:
            from video_batch import VideoBatch
            if not isinstance(body['recipe']['video'],dict):raise Problem('自动视频配方无效')
            recipe['video']=VideoBatch.settings({'product_ids':['validation'],**body['recipe']['video']})[1]
        if recipe['auto_check']:
            profile=self.app.models.get(recipe['check_profile_id'])
            if not profile['enabled'] or profile['provider']!='codex-subscription':raise Problem('请选择已启用的Codex订阅检查模型',409)
        if body['enabled'] and recipe.get('image_host') and not self.app.image_host.state()['configured']:
            raise Problem('图片托管尚未配置，请先连接可用的存储空间',409)
        if body['enabled'] and recipe.get('submit'):
            current=self.app.config()
            if not current['noon_ready'] or not current['submit_enabled']:
                raise Problem('noon 店铺内容提交尚未启用，请先完成连接设置',409)
        with self.lock,self.app.store.connect() as c:
            c.execute('UPDATE source_inbox_visual_config SET enabled=?,recipe=?,updated_at=? WHERE id=1',
                      (int(body['enabled']),json.dumps(recipe,ensure_ascii=False),now()))
            self.visual_seen.clear()
        return self.state()

    def configure(self,body):
        if set(body)!={'enabled','translate','review'} or any(type(body[k]) is not bool for k in body):raise Problem('投递箱设置无效')
        with self.lock,self.app.store.connect() as c:
            c.execute('UPDATE source_inbox_config SET enabled=?,translate=?,review=?,updated_at=? WHERE id=1',(int(body['enabled']),int(body['translate']),int(body['review']),now()))
            self.seen.clear()
        return self.state()

    def open_folder(self,kind='new'):
        if kind not in ('new','updates','photos','videos'):raise Problem('投递箱目录无效')
        folder={'new':self.folder,'updates':self.updates,'photos':self.photos,'videos':self.videos}[kind]
        subprocess.Popen(['/usr/bin/open',str(folder)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        return {'opened':True}

    def deposit(self,path,filename,kind):
        if kind not in ('new','updates'):raise Problem('请选择新商品或供货更新')
        if not isinstance(filename,str) or not filename or len(filename)>200 or '/' in filename or '\\' in filename:raise Problem('文件名无效')
        suffix=Path(filename).suffix.lower()
        if suffix not in ('.csv','.json'):raise Problem('请上传 CSV 或 JSON 资料')
        raw=Path(path).read_bytes()
        if not 0<len(raw)<=MAX_CATALOG_FILE:raise Problem('资料文件需在20MB以内',413)
        try:content=raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            if suffix!='.csv':raise Problem('JSON 资料需使用 UTF-8 编码')
            try:content=raw.decode('gb18030')
            except UnicodeDecodeError:raise Problem('CSV 编码无法识别，请另存为 UTF-8')
        try:columns,rows=self.large_source_rows(suffix,content)
        except (ValueError,TypeError,AttributeError,csv.Error):raise Problem('资料格式无效，请检查 CSV 表头或 JSON products 数组')
        if not rows or (suffix=='.json' and any(not isinstance(row,dict) for row in rows)):raise Problem('资料需包含有效商品记录')
        digest=hashlib.sha256(raw).hexdigest()
        stem=re.sub(r'[^\w.-]','_',Path(filename).stem)[:64] or 'supplier-catalog'
        basename=stem+'-'+digest[:16]+suffix
        folder=self.updates if kind=='updates' else self.folder
        with self.lock:
            with self.app.store.connect() as c:config=self.config(c)
            if not config['enabled']:raise Problem('请先保存并启用自动接收文件设置')
            if folder.is_symlink():raise Problem('投递箱目录不能是快捷链接')
            target=folder/basename;reused=False
            if target.exists() or target.is_symlink():
                if target.is_symlink() or not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest()!=digest:raise Problem('投递文件同名冲突，请更换文件名')
                reused=True
            else:
                with tempfile.NamedTemporaryFile(dir=folder,prefix='.catalog-',suffix='.upload') as staging:
                    staging.write(raw);staging.flush();os.fsync(staging.fileno())
                    os.link(staging.name,target)
        return {'name':('updates/' if kind=='updates' else '')+basename,'digest':digest,'rows':len(rows),'reused':reused,'status':'deposited'}

    def record(self,name,digest,status,message,result=None):
        with self.app.store.connect() as c:
            c.execute('INSERT INTO source_inbox_files VALUES(?,?,?,?,?,?) ON CONFLICT(name,digest) DO UPDATE SET status=excluded.status,message=excluded.message,result=excluded.result,updated_at=excluded.updated_at',(name,digest,status,message,json.dumps(result or {},ensure_ascii=False),now()))

    def record_if_changed(self,name,digest,status,message,result=None):
        encoded=json.dumps(result or {},ensure_ascii=False)
        with self.app.store.connect() as c:
            old=c.execute('SELECT status,message,result FROM source_inbox_files WHERE name=? AND digest=?',(name,digest)).fetchone()
        if not old or (old['status'],old['message'],old['result'])!=(status,message,encoded):
            self.record(name,digest,status,message,result)

    def tick(self):
        with self.lock:
            with self.app.store.connect() as c:config=self.config(c)
            if not config['enabled']:return
            if self.folder.is_symlink() or self.updates.is_symlink() or self.photos.is_symlink() or self.videos.is_symlink():raise Problem('投递箱目录不能是快捷链接')
            for kind,folder in (('new',self.folder),('updates',self.updates)):
                names=sorted((p for p in folder.iterdir() if p.suffix.lower() in ('.csv','.json') and not p.is_symlink() and p.is_file()),key=lambda p:p.name)
                selected,self.catalog_cursors[kind]=rotating_paths(names,self.catalog_cursors[kind],CATALOG_SCAN_LIMIT)
                self.scan_progress[kind]={'checked':len(selected),'total':len(names)}
                for path in selected:
                    try:self.check_file(path,config,kind)
                    except (OSError,UnicodeError,ValueError,Problem) as exc:
                        self.last_error=f'{kind}/{path.name}：{exc}'
            with self.app.store.connect() as c:
                catalog_event=c.execute("SELECT coalesce(max(id),0) FROM events WHERE action IN ('导入','规格标识更新')").fetchone()[0]
                auto_video_enabled=self.video_config(c)['enabled']
            photo_paths=[]
            for folder in sorted(p for p in self.photos.iterdir() if p.is_dir() and not p.is_symlink()):
                if not re.fullmatch(r'[A-Za-z0-9._-]{1,100}',folder.name):
                    self.last_error='原图 SKU 文件夹只能使用英文字母、数字、点、下划线和连字符'
                    continue
                for path in sorted(folder.iterdir()):
                    if path.suffix.lower() not in ('.jpg','.jpeg','.png','.webp') or path.is_symlink() or not path.is_file():continue
                    if not re.fullmatch(r'[A-Za-z0-9._-]{1,250}',path.name):
                        self.last_error=f'photos/{folder.name}：图片文件名含不支持的字符'
                        continue
                    photo_paths.append(path)
            selected,self.photo_cursor=rotating_paths(photo_paths,self.photo_cursor,PHOTO_SCAN_LIMIT)
            self.scan_progress['photos']={'checked':len(selected),'total':len(photo_paths)}
            touched_folders=set()
            for path in selected:
                touched_folders.add(path.parent)
                try:self.check_photo(path,catalog_event)
                except (OSError,UnicodeError,ValueError,Problem) as exc:
                    self.last_error=f'photos/{path.parent.name}/{path.name}：{exc}'
            for folder in sorted(touched_folders):
                try:self.schedule_visual(folder)
                except (OSError,UnicodeError,ValueError,Problem) as exc:
                    self.last_error=f'photos/{folder.name}/自动生图：{exc}'
            video_paths=[]
            for folder in sorted(p for p in self.videos.iterdir() if p.is_dir() and not p.is_symlink()):
                if not re.fullmatch(r'[A-Za-z0-9._-]{1,100}',folder.name):
                    self.last_error='原视频 SKU 文件夹只能使用英文字母、数字、点、下划线和连字符'
                    continue
                for path in sorted(folder.iterdir()):
                    if path.suffix.lower() not in VIDEO_EXTENSIONS or path.is_symlink() or not path.is_file():continue
                    if not re.fullmatch(r'[A-Za-z0-9._-]{1,250}',path.name):
                        self.last_error=f'videos/{folder.name}：视频文件名含不支持的字符'
                        continue
                    video_paths.append(path)
            selected,self.video_cursor=rotating_paths(video_paths,self.video_cursor,VIDEO_SCAN_LIMIT)
            self.scan_progress['videos']={'checked':len(selected),'total':len(video_paths)}
            for path in selected:
                folder=path.parent
                try:
                    imported=self.check_video(path,catalog_event)
                    if not imported:imported=self.video_ready.get('videos/'+folder.name+'/'+path.name)
                    observed=self.video_seen.get('videos/'+folder.name+'/'+path.name+'/自动优化')
                    if imported and auto_video_enabled and (not observed or observed[1] is None or time.monotonic()-observed[1]>=30):
                        self.schedule_video(path,imported)
                except (OSError,UnicodeError,ValueError,Problem) as exc:
                    self.last_error=f'videos/{folder.name}/{path.name}：{exc}'

    def schedule_video(self,path,imported):
        with self.app.store.connect() as c:config=self.video_config(c)
        if not config['enabled']:return
        asset=self.app.media.get(imported['asset_id'])
        if asset['kind']!='video' or asset['product_id']!=imported['product_id']:
            raise Problem('原视频归属已变化，请核对素材库',409)
        recipe=config['recipe']
        marker=hashlib.sha256(json.dumps([asset['id'],asset['sha256'],recipe],sort_keys=True).encode()).hexdigest()
        name='videos/'+path.parent.name+'/'+path.name+'/自动优化'
        observed=self.video_seen.get(name)
        if observed and observed[0]==marker and observed[1] is not None and time.monotonic()-observed[1]<30:return
        self.video_seen[name]=(marker,time.monotonic())
        request_id='inbox-video-'+marker[:48]
        with self.app.store.connect() as c:
            old=c.execute('SELECT id,status FROM media_tasks WHERE request_key IN (?,?) ORDER BY request_key',
                          (request_id+':0',request_id+':1')).fetchall()
        if old:
            needs_attention=any(r['status'] in ('failed','interrupted','cancelled') for r in old)
            expected=2 if recipe['cover'] else 1
            finished=len(old)==expected and all(r['status']=='done' for r in old)
            if needs_attention:
                status,message='attention','原视频优化任务需要处理；请到图片与视频查看原任务并重试'
            elif len(old)!=expected:
                status,message='attention','原视频优化任务不完整；请到图片与视频核对'
            elif finished:
                status,message='done','统一画幅视频'+('和封面' if recipe['cover'] else '')+'已生成；请播放核对'
            else:
                status,message='processing','原视频优化任务正在排队或处理中；请到图片与视频查看进度'
            self.record_if_changed(name,marker,status,message,
                                   {'product_id':asset['product_id'],'asset_id':asset['id'],'task_ids':[r['id'] for r in old]})
            self.video_seen[name]=(marker,time.monotonic())
            return
        duration=min(float(recipe['max_seconds']),asset['duration'])
        if duration<.05:
            self.record_if_changed(name,marker,'attention','原视频时长太短，无法自动制作')
            return
        recipes=[{'kind':'video','asset_ids':[asset['id']],'aspect':recipe['aspect'],'background':'#ffffff',
                  'mute':recipe['mute'],'start':0,'duration':duration}]
        if recipe['cover']:recipes.append({'kind':'cover','asset_ids':[asset['id']],'start':0})
        try:result=self.app.media.submit({'request_id':request_id,'recipes':recipes})
        except Problem as exc:
            self.record_if_changed(name,marker,'attention','原视频自动优化待处理：'+str(exc),
                        {'product_id':asset['product_id'],'asset_id':asset['id']})
            return
        self.record_if_changed(name,marker,'processing','已排队制作统一画幅视频'+('和封面' if recipe['cover'] else '')+'；成品需人工播放核对',
                    {'product_id':asset['product_id'],'asset_id':asset['id'],'task_ids':result['task_ids']})
        self.video_seen[name]=(marker,time.monotonic())

    def visual_reference_photos(self,folder,photos):
        manifest=folder/'references.txt'
        if not manifest.exists() and not manifest.is_symlink():
            if len(photos)>6:raise Problem('原图超过6张；请在 references.txt 逐行写入1至6个准确图片文件名')
            return photos,None
        if manifest.is_symlink() or not manifest.is_file():raise Problem('references.txt 必须是普通文本文件，不能是快捷链接')
        if not 0<manifest.stat().st_size<=2048:raise Problem('references.txt 需在2KB以内，逐行列出1至6张原图')
        try:names=[line.strip() for line in manifest.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
        except UnicodeError:raise Problem('references.txt 请保存为 UTF-8 文本')
        if not 1<=len(names)<=6 or len(set(names))!=len(names):raise Problem('references.txt 需列出1至6个不同的图片文件名')
        by_name={p.name:p for p in photos}
        if any(name not in by_name for name in names):raise Problem('references.txt 有图片文件名不存在或不是支持的原图格式')
        return [by_name[name] for name in names],manifest

    def schedule_visual(self,folder):
        with self.app.store.connect() as c:
            config=self.visual_config(c)
            intake=self.config(c)
        if not config['enabled']:return
        all_photos=sorted(p for p in folder.iterdir() if p.is_file() and not p.is_symlink() and
                          p.suffix.lower() in ('.jpg','.jpeg','.png','.webp') and re.fullmatch(r'[A-Za-z0-9._-]{1,250}',p.name))
        if not all_photos:return
        try:photos,manifest=self.visual_reference_photos(folder,all_photos)
        except Problem as exc:
            reference_file=folder/'references.txt'
            reference_stamp=reference_file.lstat().st_mtime_ns if reference_file.exists() or reference_file.is_symlink() else None
            marker=hashlib.sha256(json.dumps([folder.name,[p.name for p in all_photos],reference_stamp,config],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            self.record_if_changed('photos/'+folder.name+'/自动生图',marker,'attention',str(exc))
            return
        rights=folder/'rights.txt'
        if not rights.is_file() or rights.is_symlink():return
        newest=max([rights.stat().st_mtime_ns]+[p.stat().st_mtime_ns for p in photos]+([manifest.stat().st_mtime_ns] if manifest else []))
        if time.time_ns()-newest<30_000_000_000:return
        names=['photos/'+folder.name+'/'+p.name for p in photos]
        with self.app.store.connect() as c:
            records={}
            for record in c.execute('SELECT name,digest,status,result FROM source_inbox_files WHERE name IN ('+
                                    ','.join('?' for _ in names)+') ORDER BY updated_at DESC,rowid DESC',names):
                records.setdefault(record['name'],record)
        if any(name not in records or records[name]['status']!='done' for name in names):return
        product_ids={json.loads(records[name]['result']).get('product_id') for name in names}
        if len(product_ids)!=1 or not next(iter(product_ids)):return
        pid=next(iter(product_ids));self.app.store.get(pid)
        reference_ids=[json.loads(records['photos/'+folder.name+'/'+x.name]['result']).get('asset_id') for x in photos]
        if any(not aid for aid in reference_ids):return
        marker=hashlib.sha256(json.dumps([pid,config['recipe'],bool(intake['translate']),bool(intake['review']),
                                          [(x.name,records['photos/'+folder.name+'/'+x.name]['digest'],
                                            json.loads(records['photos/'+folder.name+'/'+x.name]['result']).get('asset_id')) for x in photos]],
                                         sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        observed=self.visual_seen.get(folder.name)
        if observed and observed[0]==marker and (observed[1] is None or time.monotonic()-observed[1]<300):return
        self.visual_seen[folder.name]=(marker,time.monotonic())
        name='photos/'+folder.name+'/自动生图'
        try:
            request_id='inbox-'+marker[:48]
            with self.app.store.connect() as c:
                existing=c.execute('SELECT id,status FROM automation_runs WHERE request_key=?',(request_id,)).fetchone()
            if existing:
                self.record(name,marker,'done',
                            '已关联原有图文商品流程（'+{'done':'已完成','cancelled':'已取消'}.get(existing['status'],'处理中')+'）',
                            {'product_id':pid,'run_id':existing['id']})
                self.visual_seen[folder.name]=(marker,None)
                return
            recipe=config['recipe']
            plan={'translate':bool(intake['translate']),'review':bool(intake['review']),
                  'ai_visual':{**{k:v for k,v in recipe.items() if k not in ('video','image_host','submit')},'reference_asset_ids':reference_ids},
                  'submit':bool(recipe.get('submit'))}
            if recipe.get('video'):plan['video']=recipe['video']
            if recipe.get('image_host'):plan['image_host']=True
            request={'product_ids':[pid],'plan':plan}
            preview=self.app.automation.preflight(request)
            row=preview['rows'][0]
            if row['status']!='ready':
                self.record(name,marker,'attention','自动生图待处理：'+'；'.join(row['reasons'][:4]),{'product_id':pid})
                return
            result=self.app.automation.create({**request,'request_id':request_id,
                                               'name':'原图到齐自动加工 '+folder.name[:40],
                                               'preflight_token':preview['token']})
            self.record(name,marker,'done','已建立图文商品流程；视觉候选需逐张验收，商品须审核后才会制作视频',
                        {'product_id':pid,'run_id':result['id'],'image_calls':preview['image_calls']})
            self.visual_seen[folder.name]=(marker,None)
        except Problem as exc:
            self.record(name,marker,'attention','自动生图待处理：'+str(exc),{'product_id':pid})

    def check_photo(self,path,catalog_event=None):
        """Import a stable SKU/photo file only with explicit per-SKU rights text."""
        name='photos/'+path.parent.name+'/'+path.name
        rights_file=path.parent/'rights.txt'
        if rights_file.is_symlink():raise Problem('素材使用依据不能是快捷链接')
        photo_stat=path.stat()
        rights_stat=rights_file.stat() if rights_file.is_file() else None
        if catalog_event is None:
            with self.app.store.connect() as c:
                catalog_event=c.execute("SELECT coalesce(max(id),0) FROM events WHERE action IN ('导入','规格标识更新')").fetchone()[0]
        marker=(photo_stat.st_size,photo_stat.st_mtime_ns,photo_stat.st_ctime_ns,
                (rights_stat.st_size,rights_stat.st_mtime_ns,rights_stat.st_ctime_ns) if rights_stat else None,catalog_event)
        if self.seen.get(name)!=marker:
            self.seen[name]=marker
            return
        if self.finished.get(name)==marker:return
        if time.time_ns()-photo_stat.st_mtime_ns<3_000_000_000:return
        if rights_stat and time.time_ns()-rights_stat.st_mtime_ns<3_000_000_000:return
        if not 0<photo_stat.st_size<=MAX_IMAGE:
            digest='oversize:'+hashlib.sha256(repr(marker).encode()).hexdigest()
            self.record(name,digest,'attention','原图必须在25MB以内')
            self.finished[name]=marker
            return
        rights_bytes=rights_file.read_bytes() if rights_stat and rights_stat.st_size<=3000 else b''
        rights=rights_bytes.decode('utf-8-sig').strip()
        photo_bytes=path.read_bytes()
        if path.stat().st_mtime_ns!=photo_stat.st_mtime_ns or (rights_stat and rights_file.stat().st_mtime_ns!=rights_stat.st_mtime_ns):return
        digest=hashlib.sha256(photo_bytes+b'\0'+rights_bytes).hexdigest()
        with self.app.store.connect() as c:
            old=c.execute('SELECT status,result FROM source_inbox_files WHERE name=? AND digest=?',(name,digest)).fetchone()
        if old and old['status']=='done':
            asset_id=json.loads(old['result']).get('asset_id')
            if asset_id:
                try:
                    asset=self.app.media.get(asset_id)
                    current=self.app.media_import.preview({'files':[{'path':path.parent.name+'/'+path.name,'size':photo_stat.st_size}]})['rows'][0]
                    if (current['status']=='ready' and current['product_id']==asset.get('product_id') and
                            asset['rights']==rights and asset['sha256']==hashlib.sha256(photo_bytes).hexdigest() and
                            (self.app.media.root/asset['file']).is_file()):
                        self.finished[name]=marker
                        return
                except Problem:pass
        if not rights or len(rights)>2000:
            self.record(name,digest,'attention','请在此 SKU 文件夹的 rights.txt 写明素材使用依据（不超过2000字）')
            self.finished[name]=marker
            return
        match_path=path.parent.name+'/'+path.name
        plan=self.app.media_import.preview({'files':[{'path':match_path,'size':photo_stat.st_size}]})['rows'][0]
        if plan['status']!='ready':
            self.record(name,digest,'attention',plan['reason'])
            self.finished[name]=marker
            return
        self.record(name,digest,'processing','正在核对原图并关联商品')
        result=self.app.media_import.ingest(path,match_path,rights,plan['token'])
        self.record(name,digest,'done',{'imported':'已关联商品原图','rights_updated':'相同原图已存在，使用依据已更新',
                                        'reused':'相同原图已存在，已复用'}[result['status']],
                    {'product_id':plan['product_id'],'asset_id':result['asset']['id'],'status':result['status']})
        self.finished[name]=marker

    def check_video(self,path,catalog_event=None):
        """Import a stable supplier video with an explicit per-SKU rights note."""
        name='videos/'+path.parent.name+'/'+path.name
        rights_file=path.parent/'rights.txt'
        if rights_file.is_symlink():raise Problem('原视频使用依据不能是快捷链接')
        video_stat=path.stat()
        rights_stat=rights_file.stat() if rights_file.is_file() else None
        if catalog_event is None:
            with self.app.store.connect() as c:
                catalog_event=c.execute("SELECT coalesce(max(id),0) FROM events WHERE action IN ('导入','规格标识更新')").fetchone()[0]
        marker=(video_stat.st_size,video_stat.st_mtime_ns,video_stat.st_ctime_ns,
                (rights_stat.st_size,rights_stat.st_mtime_ns,rights_stat.st_ctime_ns) if rights_stat else None,catalog_event)
        if self.seen.get(name)!=marker:
            self.seen[name]=marker
            self.video_ready.pop(name,None)
            self.video_seen.pop(name+'/自动优化',None)
            return
        if self.finished.get(name)==marker:return
        if time.time_ns()-video_stat.st_mtime_ns<3_000_000_000:return
        if rights_stat and time.time_ns()-rights_stat.st_mtime_ns<3_000_000_000:return
        if not 0<video_stat.st_size<=MAX_UPLOAD:
            digest='oversize:'+hashlib.sha256(repr(marker).encode()).hexdigest()
            self.record(name,digest,'attention','单个原视频需在100MB以内')
            self.finished[name]=marker
            return
        if not rights_stat or not 0<rights_stat.st_size<=3000:
            digest='rights:'+hashlib.sha256(repr(marker).encode()).hexdigest()
            self.record(name,digest,'attention','请在此 SKU 文件夹的 rights.txt 写明视频来源和使用依据（不超过2000字）')
            self.finished[name]=marker
            return
        rights_bytes=rights_file.read_bytes()
        try:rights=rights_bytes.decode('utf-8-sig').strip()
        except UnicodeDecodeError:
            digest='rights:'+hashlib.sha256(repr(marker).encode()).hexdigest()
            self.record(name,digest,'attention','原视频 rights.txt 需使用 UTF-8 编码')
            self.finished[name]=marker
            return
        if not rights or len(rights)>2000:
            digest='rights:'+hashlib.sha256(repr(marker).encode()).hexdigest()
            self.record(name,digest,'attention','视频使用依据不能为空且不得超过2000字')
            self.finished[name]=marker
            return
        h=hashlib.sha256()
        with path.open('rb') as f:
            for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
        video_sha=h.hexdigest()
        h.update(b'\0');h.update(rights_bytes)
        if path.stat().st_mtime_ns!=video_stat.st_mtime_ns or path.stat().st_size!=video_stat.st_size or rights_file.stat().st_mtime_ns!=rights_stat.st_mtime_ns:return
        digest=h.hexdigest()
        with self.app.store.connect() as c:
            old=c.execute('SELECT status,result FROM source_inbox_files WHERE name=? AND digest=?',(name,digest)).fetchone()
        if old and old['status']=='done':
            asset_id=json.loads(old['result']).get('asset_id')
            if asset_id:
                try:
                    asset=self.app.media.get(asset_id)
                    current=self.video_import.product(path.parent.name)
                    if (current['id']==asset.get('product_id') and asset['rights']==rights and
                            asset['sha256']==video_sha and (self.app.media.root/asset['file']).is_file()):
                        self.finished[name]=marker
                        ready={'asset_id':asset_id,'product_id':current['id']}
                        self.video_ready[name]=ready
                        return ready
                except Problem:pass
        self.record(name,digest,'processing','正在核对视频内容、素材使用依据与商品归属')
        try:result=self.video_import.ingest(path,path.parent.name,rights,video_sha)
        except Problem as exc:
            self.record(name,digest,'attention',str(exc))
            self.finished[name]=marker
            return
        self.record(name,digest,'done',{'imported':'已关联商品原视频，待制作与人工核对',
                                        'rights_updated':'相同原视频已存在，使用依据已更新',
                                        'reused':'相同原视频已存在，已复用'}[result['status']],
                    {'product_id':result['product_id'],'asset_id':result['asset']['id'],'status':result['status']})
        self.finished[name]=marker
        ready={'asset_id':result['asset']['id'],'product_id':result['product_id']}
        self.video_ready[name]=ready
        return ready

    def large_source_rows(self,suffix,content):
        if suffix=='.json':
            source=json.loads(content)
            products=source if isinstance(source,list) else source.get('products')
            if not isinstance(products,list):raise Problem('JSON需包含商品对象数组')
            rows=products
            columns=None
        else:
            try:
                reader=csv.reader(io.StringIO(content),strict=True)
                columns=next(reader)
                rows=[]
                for row in reader:
                    if any(value.strip() for value in row):
                        rows.append(row)
                        if len(rows)>MAX_CATALOG_ROWS:raise Problem('单个投递文件最多5000行，请分成多个文件投递')
            except (csv.Error,StopIteration):raise Problem('CSV格式无效或缺少表头，请另存为UTF-8 CSV')
        if len(rows)>MAX_CATALOG_ROWS:raise Problem('单个投递文件最多5000行，请分成多个文件投递')
        if columns is not None and (not columns or len(columns)>80):raise Problem('CSV表头需为1至80列')
        return columns,rows

    def source_chunk(self,columns,rows):
        if columns is None:return {'products':rows}
        output=io.StringIO(newline='')
        writer=csv.writer(output)
        writer.writerow(columns)
        writer.writerows(rows)
        return {'csv':output.getvalue()}

    def process_large_catalog(self,name,file_digest,suffix,content,config):
        """Split a stable supplier catalog into idempotent 500-product imports."""
        columns,rows=self.large_source_rows(suffix,content)
        if len(rows)<=500:return False
        with self.app.store.connect() as c:auto_visual=self.visual_config(c)['enabled']
        processing={'translate':bool(config['translate']),'review':bool(config['review'])} if (config['translate'] or config['review']) and not auto_visual else None
        importer=SourceImport(self.app.store,self.app.automation)
        total=(len(rows)+499)//500
        batches=[];source_groups={}
        for index in range(total):
            group=rows[index*500:(index+1)*500]
            data=self.source_chunk(columns,group)
            if processing:data['processing']=processing
            preview=importer.preview(data)
            batches.append((group,preview))
            for item in preview['rows']:
                values=item.get('values') or {}
                if not values.get('source_url'):continue
                key=values['source_url']+'|'+values.get('source_sku','')
                fingerprint=hashlib.sha256(json.dumps(values,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
                source_groups.setdefault(key,[]).append((index*500+item['row'],fingerprint))
        duplicate_rows=set();conflicting_rows=set()
        for group in source_groups.values():
            if len(group)<2:continue
            if len({fingerprint for _,fingerprint in group})>1:
                conflicting_rows.update(number for number,_ in group)
            else:
                duplicate_rows.update(number for number,_ in group[1:])
        cataloged=newly_created=skipped=queued=waiting=0
        created_rows=[]
        run_ids=[]
        issue_examples=[]
        error_rows={}
        def error_row(number,item,status=None,reason=None):
            values=item.get('values') or {}
            error_rows[number]={
                'row':number,'title':item.get('title') or values.get('title_zh',''),
                'source_sku':values.get('source_sku',''),'source_url':values.get('source_url',''),
                'stock':values.get('stock'),'cost_cny':values.get('cost_cny'),
                'status':status or item.get('status','blocked'),
                'reason':str(reason if reason is not None else item.get('reason','未导入'))[:300],
            }
        seen_fingerprints=set()
        for index,(group,initial_preview) in enumerate(batches):
            kept=[];original_rows=[]
            for position,raw in enumerate(group,1):
                number=index*500+position
                if number in conflicting_rows or number in duplicate_rows:
                    skipped+=1
                    initial_item=next((item for item in initial_preview['rows'] if item['row']==position),{})
                    error_row(number,initial_item,'blocked' if number in conflicting_rows else 'duplicate',
                              '同一货源与规格在文件不同位置的商品资料矛盾，请修正后重新投递' if number in conflicting_rows else
                              '同一货源与规格在文件中重复，仅保留第一条')
                    if len(issue_examples)<5:
                        issue_examples.append({'row':number,'reason':
                            '同一货源与规格在文件不同位置的商品资料矛盾，请修正后重新投递' if number in conflicting_rows else
                            '同一货源与规格在文件中重复，仅保留第一条'})
                else:
                    kept.append(raw);original_rows.append(number)
            if not kept:
                self.record(name,file_digest,'processing',f'已处理 {index+1}/{total} 批，已归集 {cataloged} 件',
                            {'batches_done':index+1,'batches_total':total,'cataloged':cataloged,'skipped':skipped,'run_ids':run_ids})
                continue
            data=self.source_chunk(columns,kept)
            if processing:data['processing']=processing
            _,_,_,fingerprint=importer.prepare(data)
            if fingerprint in seen_fingerprints:
                skipped+=len(kept)
                self.record(name,file_digest,'processing',f'已处理 {index+1}/{total} 批，已归集 {cataloged} 件',
                            {'batches_done':index+1,'batches_total':total,'cataloged':cataloged,'skipped':skipped,'run_ids':run_ids})
                continue
            seen_fingerprints.add(fingerprint)
            with self.app.store.connect() as c:
                receipt=c.execute('SELECT result FROM ops_requests WHERE key=?',('source-import:'+fingerprint,)).fetchone()
            if receipt:
                result=json.loads(receipt['result'])
            else:
                preview=importer.preview(data)
                if preview['ready']:
                    result=importer.apply({**data,'preview_token':preview['token'],'confirmed':True})
                    if not result.get('replayed'):newly_created+=len(result['created'])
                else:
                    result={'created':[],'skipped':[r for r in preview['rows'] if r['status']!='ready']}
            cataloged+=len(result['created'])
            for item in result.get('created_rows',[]):
                position=int(item['row'])-1
                if 0<=position<len(original_rows):
                    created_rows.append({**item,'row':original_rows[position]})
            skipped+=len(kept)-len(result['created'])
            for issue in result.get('skipped',[]):
                position=int(issue.get('row',0))-1
                if not 0<=position<len(original_rows):continue
                number=original_rows[position]
                item=next((candidate for candidate in initial_preview['rows']
                           if candidate['row']==number-index*500),{})
                error_row(number,item,issue.get('status'),issue.get('reason','未导入'))
                if len(issue_examples)<5:
                    issue_examples.append({'row':number,'reason':str(issue.get('reason','未导入'))[:200]})
            queued+=result.get('processing',{}).get('queued',0)
            waiting+=len(result.get('processing',{}).get('waiting',[]))
            run_id=result.get('processing',{}).get('run_id')
            if run_id:run_ids.append(run_id)
            self.record(name,file_digest,'processing',f'已处理 {index+1}/{total} 批，已归集 {cataloged} 件',
                        {'batches_done':index+1,'batches_total':total,'cataloged':cataloged,'skipped':skipped,'run_ids':run_ids})
        message=f'自动分成 {total} 批；已归集 {cataloged} 件，跳过 {skipped} 行；'+(
            '等待原图到齐后启动图文流程' if auto_visual else f'自动加工排队 {queued} 件，待补资料或配置 {waiting} 件')
        self.record(name,file_digest,'done' if cataloged else 'attention',message if cataloged else message+'；没有可归集的商品，请核对表头和前几项原因',
                    {'batches_done':total,'batches_total':total,'cataloged':cataloged,'newly_created_this_scan':newly_created,'skipped':skipped,
                     'queued':queued,'waiting':waiting,'run_ids':run_ids,'issue_examples':issue_examples,
                     'error_rows':[error_rows[row] for row in sorted(error_rows)],'error_row_count':len(error_rows),
                     'created_rows':created_rows,'mapping_complete':len(created_rows)==cataloged})
        return True

    def process_large_updates(self,name,file_digest,suffix,content):
        """Preflight the whole supplier feed before applying isolated 500-row updates."""
        columns,rows=self.large_source_rows(suffix,content)
        if len(rows)<=500:return False
        importer=SourceUpdates(self.app.store)
        total=(len(rows)+499)//500
        batches=[]
        product_counts={}
        for index in range(total):
            group=rows[index*500:(index+1)*500]
            preview=importer.preview(self.source_chunk(columns,group))
            batches.append((group,preview))
            for item in preview['rows']:
                pid=item.get('product_id')
                if pid:product_counts[pid]=product_counts.get(pid,0)+1
        conflicting={pid for pid,count in product_counts.items() if count>1}
        updated=unchanged=skipped=0
        issue_examples=[]
        for index,(group,initial) in enumerate(batches):
            kept=[];original_rows=[]
            for position,(raw,item) in enumerate(zip(group,initial['rows']),1):
                if item.get('product_id') in conflicting:
                    skipped+=1
                    if len(issue_examples)<5:
                        issue_examples.append({'row':index*500+position,
                                               'reason':'同一商品在文件中出现多次；请合并更新资料后重新投递'})
                else:
                    kept.append(raw);original_rows.append(index*500+position)
            if kept:
                data=self.source_chunk(columns,kept)
                preview=importer.preview(data)
                if preview['ready']:
                    result=importer.apply({**data,'preview_token':preview['token'],'confirmed':True})
                    updated+=len(result['updated'])
                else:result={'updated':[]}
                unchanged+=preview['unchanged']
                skipped+=len(kept)-len(result['updated'])-preview['unchanged']
                for item in preview['rows']:
                    if len(issue_examples)>=5:break
                    if item['status'] in ('blocked','duplicate'):
                        issue_examples.append({'row':original_rows[item['row']-1],
                                               'reason':item['reason'][:200]})
            self.record(name,file_digest,'processing',f'已处理 {index+1}/{total} 批，本轮更新 {updated} 件',
                        {'batches_done':index+1,'batches_total':total,'updated':updated,
                         'unchanged':unchanged,'skipped':skipped})
        status='done' if updated or unchanged else 'attention'
        message=f'自动分成 {total} 批；本轮更新 {updated} 件，未变化 {unchanged} 件，跳过 {skipped} 行；需重新核对供货与审核，未同步 noon'
        self.record(name,file_digest,status,message,
                    {'batches_done':total,'batches_total':total,'updated':updated,
                     'unchanged':unchanged,'skipped':skipped,'issue_examples':issue_examples})
        return True

    def check_file(self,path,config,kind='new'):
        name=('updates/' if kind=='updates' else '')+path.name
        stat=path.stat()
        marker=(stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
        if self.seen.get(name)!=marker:
            self.seen[name]=marker
            return
        if self.finished.get(name)==marker:return
        if time.time_ns()-stat.st_mtime_ns<3_000_000_000:return
        limit=MAX_CATALOG_FILE
        if stat.st_size>limit:
            digest='oversize:'+hashlib.sha256(repr(marker).encode()).hexdigest()
            self.record_if_new(name,digest,'attention',f'文件超过{limit//(1024*1024)}MB，请拆分后重新投递')
            self.finished[name]=marker
            return
        raw=path.read_bytes()
        after=path.stat()
        if marker!=(after.st_size,after.st_mtime_ns,after.st_ctime_ns):
            self.seen[name]=(after.st_size,after.st_mtime_ns,after.st_ctime_ns)
            return
        digest=hashlib.sha256(raw).hexdigest()
        with self.app.store.connect() as c:
            old=c.execute('SELECT status FROM source_inbox_files WHERE name=? AND digest=?',(name,digest)).fetchone()
        if old and old['status'] in ('done','attention'):
            self.finished[name]=marker
            return
        try:
            try:content=raw.decode('utf-8-sig')
            except UnicodeDecodeError:
                if path.suffix.lower()!='.csv':raise Problem('JSON文件需要UTF-8编码')
                content=raw.decode('gb18030')
            if kind=='new' and self.process_large_catalog(name,digest,path.suffix.lower(),content,config):
                self.finished[name]=marker
                return
            if kind=='updates' and self.process_large_updates(name,digest,path.suffix.lower(),content):
                self.finished[name]=marker
                return
            if path.suffix.lower()=='.json':
                data=json.loads(content);data={'products':data if isinstance(data,list) else data.get('products')}
            else:data={'csv':content}
            with self.app.store.connect() as c:auto_visual=self.visual_config(c)['enabled']
            if kind=='new' and (config['translate'] or config['review']) and not auto_visual:
                data['processing']={'translate':bool(config['translate']),'review':bool(config['review'])}
            importer=SourceUpdates(self.app.store) if kind=='updates' else SourceImport(self.app.store,self.app.automation)
            preview=importer.preview(data)
            if not preview['ready']:
                unchanged=kind=='updates' and preview['unchanged'] and not preview['blocked']
                self.record(name,digest,'done' if unchanged else 'attention',
                    '供货资料未变化；未修改商品' if unchanged else '没有可处理记录；请核对列名、标识或错误行',
                    {'blocked':preview['blocked'],'duplicates':preview['duplicates'],'unchanged':preview.get('unchanged',0),
                     'rows':[{'row':r['row'],'title':r['title'],'product_id':r.get('product_id'),'status':r['status'],'reason':r['reason'],'identity_changes':r.get('identity_changes')} for r in preview['rows']] if kind=='updates' else []})
                self.finished[name]=marker
                return
            self.record(name,digest,'processing','正在处理已通过预检的记录')
            result=importer.apply({**data,'preview_token':preview['token'],'confirmed':True})
            if kind=='updates':
                message=f'更新 {len(result["updated"])} 件；跳过 {len(result["skipped"])} 行；需重新核对供货与审核，未同步 noon'
                details={'updated':len(result['updated']),'skipped':len(result['skipped']),'product_ids':result['updated'],
                         'rows':[{'row':r['row'],'title':r['title'],'status':'updated','reason':'采购成本或可供数量已更新','changes':r['changes']} for r in preview['rows'] if r['status']=='ready']+
                                [{'row':r['row'],'title':r['title'],'product_id':r.get('product_id'),'status':r['status'],'reason':r['reason'],'identity_changes':r.get('identity_changes')} for r in preview['rows'] if r['status']!='ready']}
            else:
                processing=result.get('processing') or {}
                count=len(result['created'])
                message=f'新增 {count} 件；跳过 {len(result["skipped"])} 件；'+('等待原图到齐后启动同一条图文流程' if auto_visual else f'自动加工排队 {processing.get("queued",0)} 件；待补资料或配置 {len(processing.get("waiting",[]))} 件')
                details={'created':count,'skipped':len(result['skipped']),'run_id':processing.get('run_id'),'queued':processing.get('queued',0),'waiting':len(processing.get('waiting',[])),
                         'created_rows':result.get('created_rows',[]),'mapping_complete':len(result.get('created_rows',[]))==count}
            self.record(name,digest,'done',message,details)
            self.finished[name]=marker
        except (UnicodeError,ValueError,TypeError,AttributeError,Problem) as exc:
            self.record(name,digest,'attention',str(exc))
            self.finished[name]=marker

    def record_if_new(self,name,digest,status,message):
        with self.app.store.connect() as c:
            old=c.execute('SELECT 1 FROM source_inbox_files WHERE name=? AND digest=?',(name,digest)).fetchone()
        if not old:self.record(name,digest,status,message)
