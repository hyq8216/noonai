"""Exact SKU matching for bulk original photos, with content-based safe replay."""
import hashlib
import json
import threading
from pathlib import Path, PurePosixPath
from core import Problem
from media import image_info, string

MAX_FILES=2000
MAX_IMAGE=25*1024*1024

class MediaImport:
    def __init__(self,store,media):
        self.store=store;self.media=media;self.lock=threading.Lock()

    def preview(self,b):
        files=b.get('files')
        if not isinstance(files,list) or not 1<=len(files)<=MAX_FILES:raise Problem('请选择1至2000张原图')
        keys=set()
        for f in files:
            if not isinstance(f,dict) or not isinstance(f.get('path'),str):continue
            path=PurePosixPath(f['path']);parts=f['path'].split('/')
            keys.add(path.stem)
            if '__' in path.stem:keys.add(path.stem.rsplit('__',1)[0])
            if len(parts)>1:keys.add(parts[-2])
        keys=sorted(k for k in keys if k)
        index={};seen_products=set()
        with self.store.connect() as c:
            for start in range(0,len(keys),400):
                part=keys[start:start+400];placeholders=','.join('?' for _ in part)
                for saved in c.execute("SELECT * FROM products WHERE json_extract(data,'$.partner_sku') IN ("+placeholders+") OR json_extract(data,'$.source_sku') IN ("+placeholders+")",part+part):
                    if saved['id'] in seen_products:continue
                    seen_products.add(saved['id']);p=self.store.unpack(saved)
                    for sku in {p['partner_sku'],p.get('source_sku','')}:
                        if sku:index.setdefault(sku,[]).append(p)
        rows=[];seen=set()
        for f in files:
            if not isinstance(f,dict):raise Problem('文件清单格式无效')
            name=f.get('path');size=f.get('size')
            if not isinstance(name,str) or not name or len(name)>1000 or any(ord(x)<32 for x in name):raise Problem('图片路径格式无效')
            if isinstance(size,bool) or not isinstance(size,int):raise Problem('图片大小格式无效')
            path=PurePosixPath(name);parts=name.split('/');reason=''
            if name in seen:raise Problem('文件清单存在同名路径，请分批导入')
            seen.add(name)
            if path.is_absolute() or any(x in ('','.','..') for x in parts) or '\\' in name:reason='文件路径不符合要求'
            elif len(path.name)>250:reason='文件名需在250字符以内'
            elif path.suffix.lower() not in ('.jpg','.jpeg','.png','.webp'):reason='仅支持 JPG、PNG、WebP 原图'
            elif not 0<size<=MAX_IMAGE:reason='单张原图需在25MB以内'
            keys={path.stem}
            if '__' in path.stem:keys.add(path.stem.rsplit('__',1)[0])
            if len(parts)>1:keys.add(parts[-2])
            matches={p['id']:p for k in keys for p in index.get(k,[])}
            p=next(iter(matches.values())) if len(matches)==1 else None
            if not reason:
                if not matches:reason='未匹配 SKU，请按 SKU 文件夹或 SKU__序号 命名'
                elif len(matches)>1:reason='匹配到多个商品，请使用唯一的店铺 SKU'
                elif p.get('demo'):reason='示例商品不能作为真实商品原图归属'
            row={'path':name,'size':size,'product_id':p['id'] if p else '',
                 'title':p['title_zh'] if p else '', 'sku':p['partner_sku'] if p else '',
                 'status':'blocked' if reason else 'ready','reason':reason or '匹配成功，导入时核对图片内容并去重'}
            # Recompute on upload: a renamed SKU or newly ambiguous match invalidates this plan.
            row['token']=hashlib.sha256(json.dumps(row,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            rows.append(row)
        matched={r['product_id'] for r in rows if r['status']=='ready'}
        return {'rows':rows,'ready':sum(r['status']=='ready' for r in rows),'blocked':sum(r['status']=='blocked' for r in rows),'products':len(matched)}

    def ingest(self,path,name,rights,token):
        rights=string(rights,2000,'素材使用依据',True)
        with self.lock:
            row=self.preview({'files':[{'path':name,'size':Path(path).stat().st_size}]})['rows'][0]
            if row['status']!='ready':raise Problem(row['reason'],409)
            if not token or row['token']!=token:raise Problem('商品匹配已变化，请重新预览后导入',409)
            image_info(path)  # Reject disguised videos and corrupt bytes before any import.
            digest=hashlib.sha256(Path(path).read_bytes()).hexdigest()
            with self.store.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                for rec in c.execute("SELECT data FROM media_assets WHERE json_extract(data,'$.product_id')=? AND json_extract(data,'$.kind')='image'",(row['product_id'],)):
                    asset=json.loads(rec['data'])
                    if (asset.get('product_id')!=row['product_id'] or asset.get('kind')!='image' or
                            asset.get('parents') or asset.get('visual_job_id') or asset.get('product_image_snapshot') or
                            asset.get('sha256')!=digest):continue
                    existing=self.media.root/asset['file']
                    if existing.is_file() and hashlib.sha256(existing.read_bytes()).hexdigest()==digest:
                        if asset.get('rights')!=rights:
                            asset['rights']=rights
                            c.execute('UPDATE media_assets SET data=? WHERE id=?',(json.dumps(asset,ensure_ascii=False),asset['id']))
                            self.store.event(c,row['product_id'],'原图使用依据更新',asset['name'])
                            return {'status':'rights_updated','asset':asset,'path':name}
                        return {'status':'reused','asset':asset,'path':name}
            asset=self.media.ingest(path,PurePosixPath(name).name,rights,row['product_id'])
            return {'status':'imported','asset':asset,'path':name}
