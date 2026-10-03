"""Original photos go to exact SKU owners, never guesses or generated candidates."""
import hashlib
import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from urllib.parse import quote
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from PIL import Image
from core import Store,Problem
from media import Media
from media_import import MediaImport
from server import App,Handler,LocalHTTPServer

class PhotoImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.store=Store(self.root);self.media=Media(self.store);self.batch=MediaImport(self.store,self.media)
        self.a=self.add('SKU-A');self.b=self.add('SKU-B');self.image=self.root/'source.png';Image.new('RGB',(800,800),'red').save(self.image)
    def tearDown(self):self.media.close();self.tmp.cleanup()
    def add(self,sku,demo=False):
        pid=self.store.import_rows([{'title_zh':sku,'source_sku':sku}],demo=demo)['created'][0];return self.store.get(pid)
    def row(self,name,size=None):return {'path':name,'size':size if size is not None else self.image.stat().st_size}
    def plan(self,name):return self.batch.preview({'files':[self.row(name)]})['rows'][0]
    def ingest(self,name='SKU-A__01.png',token=None):return self.batch.ingest(self.image,name,'synthetic fixture',token or self.plan(name)['token'])
    def test_exact_folder_and_filename_and_partner_sku(self):
        files=[self.row(n) for n in ['photos/SKU-A/01.png','SKU-A__02.png','SKU-A.png',self.b['partner_sku']+'__03.png']]
        p=self.batch.preview({'files':files});self.assertEqual((p['ready'],p['products']),(4,2));self.assertEqual([x['product_id'] for x in p['rows']],[self.a['id']]*3+[self.b['id']])
    def test_no_prefix_case_or_ambiguous_guess(self):
        for name in ['SKU-AX__1.png','sku-a.png','SKU-A/SKU-B__1.png','unknown.png']:
            self.assertEqual(self.plan(name)['status'],'blocked',name)
        self.add('SKU-A');self.assertEqual(self.plan('SKU-A.png')['status'],'blocked');self.assertEqual(self.plan(self.a['partner_sku']+'.png')['status'],'ready')
    def test_demo_and_invalid_files(self):
        self.add('DEMO',True)
        for name,size in [('DEMO.png',10),('../SKU-A.png',10),('/SKU-A.png',10),('x//SKU-A.png',10),('SKU-A.mp4',10),('SKU-A.png',0),('SKU-A.png',26*1024*1024)]:
            self.assertEqual(self.batch.preview({'files':[self.row(name,size)]})['blocked'],1)
    def test_capacity_and_duplicate_names(self):
        p=self.batch.preview({'files':[self.row(f'SKU-A__{n}.png') for n in range(2000)]});self.assertEqual(p['ready'],2000)
        for files in [[],[self.row('SKU-A.png')]*2,[self.row(str(n)+'.png') for n in range(2001)]]:
            with self.assertRaises(Problem):self.batch.preview({'files':files})
    def test_raw_bytes_association_and_replay_across_restart(self):
        first=self.ingest();self.assertEqual(first['status'],'imported');a=first['asset'];self.assertEqual(a['product_id'],self.a['id']);self.assertEqual(a['parents'],[]);self.assertEqual((self.media.root/a['file']).read_bytes(),self.image.read_bytes())
        self.batch=MediaImport(self.store,self.media);second=self.ingest('SKU-A__02.png');self.assertEqual(second['status'],'reused');self.assertEqual(second['asset']['id'],a['id']);self.assertEqual(len(self.media.state()['assets']),1)
        # Same bytes for another SKU must not adopt the first SKU's ownership.
        other=self.ingest('SKU-B__01.png');self.assertEqual(other['status'],'imported');self.assertEqual(other['asset']['product_id'],self.b['id']);self.assertEqual(self.store.get(self.a['id'])['images'],[])
    def test_changed_mapping_and_new_ambiguity_rejected(self):
        token=self.plan('SKU-A.png')['token'];self.add('SKU-A')
        with self.assertRaises(Problem):self.ingest('SKU-A.png',token)
        token=self.plan('SKU-B.png')['token'];p=self.store.get(self.b['id']);self.store.update(p['id'],{**p,'source_sku':'NEW'},p['revision'])
        with self.assertRaises(Problem):self.ingest('SKU-B.png',token)
        self.assertEqual(len(self.media.state()['assets']),0)
    def test_invalid_bytes_and_rights_do_not_write(self):
        bad=self.root/'bad.png';bad.write_bytes(b'not an image');token=self.batch.preview({'files':[self.row('SKU-A.png',bad.stat().st_size)]})['rows'][0]['token']
        with self.assertRaises(Problem):self.batch.ingest(bad,'SKU-A.png','fixture',token)
        with self.assertRaises(Problem):self.batch.ingest(self.image,'SKU-A.png','',self.plan('SKU-A.png')['token'])
        self.assertEqual(self.media.state()['assets'],[])
    def test_missing_or_corrupted_original_not_reused(self):
        first=self.ingest()['asset'];(self.media.root/first['file']).write_bytes(b'corrupted');second=self.ingest();self.assertEqual(second['status'],'imported');self.assertNotEqual(first['id'],second['asset']['id'])
    def test_concurrent_replay_one_original(self):
        with ThreadPoolExecutor(max_workers=3) as pool:result=list(pool.map(lambda _:self.ingest(),range(3)))
        self.assertEqual(sum(r['status']=='imported' for r in result),1);self.assertEqual(len(self.media.state()['assets']),1)
    def test_derived_asset_is_not_an_original(self):
        a=self.ingest()['asset'];a['parents']=['synthetic-parent']
        with self.store.connect() as c:c.execute('UPDATE media_assets SET data=? WHERE id=?',(json.dumps(a),a['id']))
        self.assertEqual(self.ingest()['status'],'imported')

class PhotoImportHTTPTests(unittest.TestCase):
    def test_preview_upload_replay_and_auth(self):
        with tempfile.TemporaryDirectory() as temp:
            app=App(temp);app.store.import_rows([{'title_zh':'Synthetic','source_sku':'HTTP-A'}]);server=LocalHTTPServer(('127.0.0.1',0),Handler);server.app=app
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();base=f'http://127.0.0.1:{server.server_port}'
            try:
                img=Path(temp)/'in.png';Image.new('RGB',(400,400),'blue').save(img);data=img.read_bytes()
                req=Request(base+'/api/media/import-preview',data=json.dumps({'files':[{'path':'HTTP-A__1.png','size':len(data)}]}).encode(),headers={'Content-Type':'application/json','X-Workbench-Token':app.token})
                with urlopen(req) as res:row=json.load(res)['rows'][0]
                headers={'Content-Type':'application/octet-stream','X-Workbench-Token':app.token,'X-Media-Name':quote('HTTP-A__1.png'),'X-Media-Rights':quote('synthetic HTTP test'),'X-Media-Match':row['token']}
                for expected in ['imported','reused']:
                    with urlopen(Request(base+'/api/media/bulk-upload',data=data,headers=headers)) as res:self.assertEqual(json.load(res)['status'],expected)
                headers['X-Workbench-Token']='invalid'
                with self.assertRaises(HTTPError) as error:urlopen(Request(base+'/api/media/bulk-upload',data=data,headers=headers))
                self.assertEqual(error.exception.code,403)
            finally:
                server.shutdown();server.server_close();thread.join();app.automation.close();app.visuals.close();app.media.close();app.executor.shutdown()

if __name__=='__main__':unittest.main()
