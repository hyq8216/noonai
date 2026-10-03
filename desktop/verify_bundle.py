"""Exercise the shipped backend without a developer Python on its PATH."""
import json
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from urllib.request import urlopen,Request

ROOT=Path(__file__).resolve().parent
BIN=Path(os.environ.get('NOON_VERIFY_APP',str(ROOT/'dist/Noon Studio.app')))/'Contents/Resources/backend/noon-backend'

def request(url,path,token=None,data=None):
    headers={'Content-Type':'application/json'}
    if token:headers['X-Workbench-Token']=token
    with urlopen(Request(url+path,data=json.dumps(data).encode() if data is not None else None,headers=headers),timeout=5) as r:return json.loads(r.read())

def start(root,tag):
    ready=root/(tag+'.json')
    env={'PATH':'/usr/bin:/bin','HOME':os.environ['HOME'],'LANG':'en_US.UTF-8'}
    p=subprocess.Popen([str(BIN),'--port','0','--data',str(root),'--ready-file',str(ready)],env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    for _ in range(150):
        if ready.exists():return p,json.loads(ready.read_text())['url']
        if p.poll() is not None:raise AssertionError(p.stderr.read().decode())
        time.sleep(.1)
    p.terminate();p.wait(timeout=10);raise AssertionError('Backend timeout')

with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);p,url=start(root,'first')
    try:
        s=request(url,'/api/state');token=s['token']
        def call(action,**data):return request(url,'/api/ops/'+action,token,{'request_id':uuid.uuid4().hex,**data})
        assert s['products']==[] and s['ops']['documents']==[]
        pid=request(url,'/api/import',token,{'products':[{'title_zh':'Packaged QA item'}]})['created'][0]
        entities={k:call('entity',kind=k,name='QA-'+k)['id'] for k in ['shop','warehouse','supplier']}
        lines=[{'product_id':pid,'quantity':3,'unit_price':'1.25'}]
        purchase=call('purchase',supplier_id=entities['supplier'],warehouse_id=entities['warehouse'],lines=lines)
        call('receive',id=purchase['id'],revision=purchase['revision'],quantities={pid:3})
        order=call('order',shop_id=entities['shop'],warehouse_id=entities['warehouse'],external_id='BUNDLE-QA',currency='SAR',lines=[{**lines[0],'quantity':2}])
        order=call('reserve',id=order['id'],revision=order['revision'])
        call('ship',id=order['id'],revision=order['revision'],carrier='QA',tracking='QA-TRACK')
        fin=request(url,'/api/finance/entry',token,{'request_id':'packaged-finance','kind':'income','category':'sale','document_id':order['id'],'currency':'SAR','amount':'2.50','fx':'1.9','date':'2026-01-01','evidence_key':'QA-STATEMENT-1','evidence':'Synthetic QA settlement'})
        request(url,'/api/finance/payment',token,{'request_id':'packaged-receipt','id':fin['id'],'revision':fin['revision'],'amount':'1.00','fx':'1.91','date':'2026-01-02','account':'QA account','evidence_key':'QA-RECEIPT-1','evidence':'Synthetic QA receipt'})
        finance_snapshot=request(url,'/api/state')['finance']
        assert finance_snapshot['entries'][0]['remaining_cents']==150
        assert finance_snapshot['summary']['income_cents']==475 and finance_snapshot['summary']['cash_in_cents']==191
        dest=call('entity',kind='warehouse',name='QA-destination')['id']
        tr=call('transfer',source_warehouse_id=entities['warehouse'],warehouse_id=dest,lines=[{'product_id':pid,'quantity':1}],note='Synthetic QA transfer')
        tr=call('transfer_dispatch',id=tr['id'],revision=tr['revision'],evidence='Synthetic dispatch')
        call('transfer_receive',id=tr['id'],revision=tr['revision'],quantities={pid:1},evidence='Synthetic receipt')
        call('replenishment_policy',product_id=pid,warehouse_id=dest,supplier_id=entities['supplier'],minimum=2,target=5,min_order=1,pack_size=1,unit_price='1.25',quote_evidence='QA quotation',enabled=True,revision=0)
        planned=request(url,'/api/state')['ops']['replenishment'][0]
        assert planned['quantity']==4
        call('replenish',confirmed=True,rows=[{k:planned[k] for k in ('product_id','warehouse_id','fingerprint')}])
        snapshot=request(url,'/api/state')['ops'];assert sum(s['on_hand'] for s in snapshot['stock'])==1 and all(s['reserved']==0 for s in snapshot['stock']) and snapshot['replenishment'][0]['quantity']==0
        other=subprocess.run([str(BIN),'--port','0','--data',str(root),'--ready-file',str(root/'second.json')],capture_output=True,timeout=10)
        assert other.returncode!=0 and '另一个实例'.encode() in other.stderr
    finally:p.terminate();p.wait(timeout=10)
    p,url=start(root,'reopened')
    try:
        assert request(url,'/api/state')['finance']==finance_snapshot
        current=request(url,'/api/state')['ops'];assert current==snapshot
        token=request(url,'/api/state')['token'];assets=[]
        for name in ['red','blue']:
            raw=(ROOT/'qa/media-fixtures'/(name+'.png')).read_bytes()
            req=Request(url+'/api/media/upload',raw,{'Content-Type':'application/octet-stream','X-Workbench-Token':token,'X-Media-Name':name+'.png','X-Media-Rights':'Synthetic QA fixture'})
            with urlopen(req,timeout=10) as r:assets.append(json.load(r)['id'])
        # Pause before creation: the packaged check must never call the real subscription.
        request(url,'/api/visuals/control',token,{'action':'pause'})
        visual_req={'request_id':'packaged-visual','product_id':pid,'revision':request(url,'/api/state')['products'][0]['revision'],'asset_ids':[assets[0]],'shots':['hero'],'confirmed':True,'locked_features':'Synthetic QA geometry','style':'Synthetic test only'}
        visual_job=request(url,'/api/visuals/tasks',token,visual_req)['job_ids'][0]
        assert request(url,'/api/visuals/tasks',token,visual_req)['job_ids']==[visual_job]
        visual_state=request(url,'/api/state')['visuals']
        assert visual_state['jobs'][0]['status']=='queued' and visual_state['jobs'][0]['dispatched_at'] is None
        request(url,'/api/visuals/control',token,{'action':'cancel','id':visual_job})
        with urlopen(url+'/visuals.js') as r:assert b'visualPage' in r.read()
        task=request(url,'/api/media/tasks',token,{'request_id':'packaged-media','recipes':[{'kind':'slideshow','asset_ids':assets,'seconds':1}]})['task_ids'][0]
        for _ in range(200):
            media=request(url,'/api/state')['media'];t=next(t for t in media['tasks'] if t['id']==task)
            if t['status'] in ['done','failed']:break
            time.sleep(.1)
        assert t['status']=='done',t
        video=next(a for a in media['assets'] if a['id']==t['result'][0]);assert (video['width'],video['height'])==(1080,1080) and abs(video['duration']-2)<.1
        with urlopen(Request(url+'/media/'+video['file'],headers={'Range':'bytes=0-31'})) as r:assert r.status==206 and len(r.read())==32
        media_snapshot=media
    finally:p.terminate();p.wait(timeout=10)
    p,url=start(root,'media-reopened')
    try:
        assert request(url,'/api/state')['media']==media_snapshot
        token=request(url,'/api/state')['token']
        interrupted=request(url,'/api/media/tasks',token,{'request_id':'interrupt-media','recipes':[{'kind':'slideshow','asset_ids':assets,'seconds':10,'aspect':'portrait'}]})['task_ids'][0]
        for _ in range(100):
            t=next(t for t in request(url,'/api/state')['media']['tasks'] if t['id']==interrupted)
            if t['status']=='running':break
            time.sleep(.01)
        assert t['status']=='running',t
    finally:p.terminate();p.wait(timeout=10)
    p,url=start(root,'interrupted-reopened')
    try:
        t=next(t for t in request(url,'/api/state')['media']['tasks'] if t['id']==interrupted)
        assert t['status']=='interrupted',t
        assert not list((root/'media/work').iterdir())
        before=request(url,'/api/state');token=before['token']
        backup=request(url,'/api/backup/create',token,{})
        with urlopen(url+'/api/backup/download/'+backup['id']) as r:archive=r.read()
        request(url,'/api/import',token,{'products':[{'title_zh':'Must disappear after restore'}]})
        with urlopen(Request(url+'/api/backup/inspect',archive,{'Content-Type':'application/octet-stream','X-Workbench-Token':token}),timeout=20) as r:preview=json.load(r)
        request(url,'/api/backup/schedule',token,{**preview,'confirmed':True})
    finally:p.terminate();p.wait(timeout=10)
    p,url=start(root,'backup-restored')
    try:
        after=request(url,'/api/state')
        assert [x['id'] for x in after['products']]==[pid]
        assert after['ops']==before['ops'] and after['finance']==before['finance']
        assert after['media']['assets']==before['media']['assets']
        assert after['media']['paused'] and after['visuals']['paused']
        assert after['recovery']['last_restore']['status']=='restored'
        rollback=after['recovery']['last_restore']['rollback_archive_id']
        with urlopen(url+'/api/backup/download/'+rollback) as r:assert r.read(2)==b'PK'
        with urlopen(Request(url+'/media/'+video['file'],headers={'Range':'bytes=0-31'})) as r:assert r.status==206 and len(r.read())==32
    finally:p.terminate();p.wait(timeout=10)
report={'standalone_runtime':True,'backup_restore_and_rollback_download':True,'transfer_and_replenishment_roundtrip':True,'finance_partial_receipt_and_persistence':True,'visual_routes_and_dedupe':True,'visual_provider_called':False,'operations_roundtrip':True,'on_hand_after_3_received_2_shipped':1,'second_instance_blocked':True,'reopen_persistence':True,'packaged_ffmpeg_slideshow':{'width':1080,'height':1080,'duration':video['duration']},'media_range_read':True,'media_reopen_persistence':True,'shutdown_during_processing':'interrupted; temporary work removed'}
(ROOT/'qa').mkdir(exist_ok=True)
(ROOT/'qa/bundle-verification.json').write_text(json.dumps(report,indent=2))
print(json.dumps(report))
