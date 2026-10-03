"""Associate authorized supplier videos by exact SKU without mixing products."""
import hashlib
import json
import threading
from pathlib import Path

from core import Problem
from media import MAX_UPLOAD,string

VIDEO_EXTENSIONS={'.mp4','.mov','.webm'}


class VideoImport:
    def __init__(self,store,media):
        self.store=store;self.media=media;self.lock=threading.Lock()

    def product(self,sku):
        with self.store.connect() as c:
            rows=c.execute("SELECT * FROM products WHERE json_extract(data,'$.partner_sku')=? OR json_extract(data,'$.source_sku')=? LIMIT 3",(sku,sku)).fetchall()
        matches={row['id']:self.store.unpack(row) for row in rows}
        if not matches:raise Problem('未匹配 SKU，请用唯一的店铺 SKU 或货源规格货号命名文件夹',409)
        if len(matches)>1:raise Problem('SKU 匹配多个商品，请改用唯一的店铺 SKU',409)
        product=next(iter(matches.values()))
        if product['demo']:raise Problem('示例商品不能关联供应商视频',409)
        return product

    def ingest(self,path,sku,rights,expected_sha=None):
        path=Path(path)
        if path.suffix.lower() not in VIDEO_EXTENSIONS:raise Problem('仅支持 MP4、MOV、WebM 原视频')
        if not 0<path.stat().st_size<=MAX_UPLOAD:raise Problem('单个原视频需在100MB以内')
        rights=string(rights,2000,'素材使用依据',True)
        with path.open('rb') as f:magic=f.read(16)
        if magic[4:8]!=b'ftyp' and magic[:4]!=b'\x1aE\xdf\xa3':raise Problem('文件内容不是支持的视频格式')
        with self.lock:
            product=self.product(sku)
            digest=hashlib.sha256()
            with path.open('rb') as f:
                for block in iter(lambda:f.read(1024*1024),b''):digest.update(block)
            fingerprint=digest.hexdigest()
            if expected_sha is not None and fingerprint!=expected_sha:raise Problem('原视频在读取期间变化，请等待复制完成后重试',409)
            with self.store.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                for row in c.execute("SELECT data FROM media_assets WHERE json_extract(data,'$.product_id')=? AND json_extract(data,'$.kind')='video'",(product['id'],)):
                    asset=json.loads(row['data'])
                    if asset.get('parents') or asset.get('task_id') or asset.get('sha256')!=fingerprint:continue
                    existing=self.media.root/asset['file']
                    if not existing.is_file():continue
                    check=hashlib.sha256()
                    with existing.open('rb') as f:
                        for block in iter(lambda:f.read(1024*1024),b''):check.update(block)
                    if check.hexdigest()!=fingerprint:continue
                    if asset.get('rights')!=rights:
                        asset['rights']=rights
                        c.execute('UPDATE media_assets SET data=? WHERE id=?',(json.dumps(asset,ensure_ascii=False),asset['id']))
                        self.store.event(c,product['id'],'原视频使用依据更新',asset['name'])
                        return {'status':'rights_updated','asset':asset,'product_id':product['id']}
                    return {'status':'reused','asset':asset,'product_id':product['id']}
            asset=self.media.ingest(path,path.name,rights,product['id'])
            if asset['kind']!='video':raise Problem('原视频格式校验未通过')
            if asset['sha256']!=fingerprint:raise Problem('原视频在复制期间变化，请核对刚导入的素材后再处理',409)
            return {'status':'imported','asset':asset,'product_id':product['id']}
