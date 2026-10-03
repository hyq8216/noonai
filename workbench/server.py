import argparse
import base64
import csv
import io
import json
import mimetypes
import os
import re
import secrets
import socket
import sys
import threading
import zipfile
import fcntl
import tempfile
import signal
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, unquote, parse_qs, quote
from media import Media, MAX_UPLOAD
from operations import Operations
from models import Models
from automation import Automation
from platform_batch import PlatformBatch
from content_submit_batch import ContentSubmitBatch
from stock_plan import StockPlan
from catalog_campaign import CatalogCampaign
from image_host import ImageHost
from visuals import Visuals
from visual_presets import VisualPresets
from source_leads import SourceLeads
from channel_accounts import ChannelAccounts
from source_collection import SourceCollection
from order_intake import OrderIntake
from domestic_capture import DomesticCapture
from inventory_counts import InventoryCounts
from shipping_manifests import ShippingManifests
from supplier_quotes import SupplierQuotes
from fx_registry import FXRegistry
from replenishment import Replenishment
from pricing_plans import PricingPlans
from ad_analytics import AdAnalytics
from import_profiles import ImportProfiles
from settlement_intake import SettlementIntake
from catalog_groups import CatalogGroups
from analytics import Analytics
from collection_schedules import CollectionSchedules
from backup_schedules import BackupSchedules
from bank_reconciliation import BankReconciliation
from fulfillment import Fulfillment
from procurement import Procurement
from after_sales import AfterSales
from alerts import Alerts
from batch_editor import BatchEditor
from finance import Finance
from recovery import Recovery, MAX_ARCHIVE
from core import Store, Problem, now, payload, normalize_image
from connectors import load_env, settings, translate, Noon, preflight_attributes

BASE=Path(__file__).resolve().parent
load_env(BASE/'.env')

class LocalHTTPServer(ThreadingHTTPServer):
    # The desktop loads styles plus several module scripts concurrently.
    # Keep the accept backlog large enough for a cold start under CPU load.
    request_queue_size=64

def export_package(store, ids):
    if not isinstance(ids,list) or not ids or len(ids)>500: raise Problem('请选择1至500个商品')
    items=[store.get(x) for x in dict.fromkeys(ids)]
    total=sum((store.assets/im[k]).stat().st_size for p in items for im in p['images'] for k in ('source','file') if (store.assets/im[k]).is_file())
    if total > 200*1024*1024: raise Problem('本批图片总量超过200MB，请拆分商品后导出',413)
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
        manifest={'kind':'local-content-draft','exported_at':now(),
          'notice':'本地内容包，不是已上架结果，也不是noon官方NIS模板。类目属性、公开图片地址、报价和库存仍需核对。',
          'products':items}
        z.writestr('商品档案.json',json.dumps(manifest,ensure_ascii=False,indent=2))
        z.writestr('使用说明.txt','这是本地内容包，未提交noon。保留原始资料、双语内容、规范化图片和待补充项。\n商品修改后须重新审核。\nJSON为接口草稿，不能冒充官方NIS表。\n')
        for p in items:
            folder=p['partner_sku']
            z.writestr(folder+'/noon内容草稿.json',json.dumps(payload(p),ensure_ascii=False,indent=2))
            z.writestr(folder+'/检查结果.txt','\n'.join(p['issues']) or '本地检查通过；平台状态尚未验证')
            for im in p['images']:
                for key in ('source','file'):
                    file=store.assets/im[key]
                    if file.is_file(): z.write(file,folder+'/images/'+im[key])
    return buf.getvalue()

class App:
    def __init__(self,root):
        self.store=Store(root); self.store.recover_jobs()
        load_env(self.store.root/'.env')
        self.ops=Operations(self.store)
        self.finance=Finance(self.store)
        self.media=Media(self.store)
        from media_import import MediaImport
        self.media_import=MediaImport(self.store,self.media)
        self.models=Models(self.store);self.models.recover()
        self.visuals=Visuals(self.store,self.media,self.models.codex)
        self.visual_presets=VisualPresets(self.store)
        self.source_leads=SourceLeads(self.store)
        self.token=secrets.token_urlsafe(32)
        self.executor=ThreadPoolExecutor(max_workers=1)
        self.automation=Automation(self)
        self.catalog_campaign=CatalogCampaign(self)
        from visual_batch import VisualBatch
        self.visual_batch=VisualBatch(self)
        from visual_checks import VisualChecks
        self.visual_checks=VisualChecks(self)
        self.write_lock=threading.RLock()
        from source_inbox import SourceInbox
        self.source_inbox=SourceInbox(self)
        self.channel_accounts=ChannelAccounts(self.store)
        self.source_collection=SourceCollection(self)
        self.collection_executor=ThreadPoolExecutor(max_workers=2,thread_name_prefix='source-collection')
        self.collection_futures=set();self.collection_lock=threading.Lock()
        self.supplier_quotes=SupplierQuotes(self)
        self.fx_registry=FXRegistry(self)
        self.replenishment=Replenishment(self)
        self.import_profiles=ImportProfiles(self)
        self.inventory_counts=InventoryCounts(self)
        self.shipping_manifests=ShippingManifests(self)
        self.pricing_plans=PricingPlans(self)
        self.ad_analytics=AdAnalytics(self)
        self.domestic_capture=DomesticCapture(self)
        self.order_intake=OrderIntake(self)
        self.settlements=SettlementIntake(self)
        self.catalog_groups=CatalogGroups(self)
        self.analytics=Analytics(self)
        self.collection_schedules=CollectionSchedules(self)
        self.backup_schedules=BackupSchedules(self)
        self.bank_reconciliation=BankReconciliation(self)
        self.fulfillment=Fulfillment(self)
        self.procurement=Procurement(self)
        self.after_sales=AfterSales(self)
        self.alerts=Alerts(self)
        self.batch_editor=BatchEditor(self)
        self.recovery=Recovery(self.store.root)
        self.image_host=ImageHost(self)
    def dispatch_collection(self,result):
        if result.get('status')=='queued' and not result.get('replayed'):
            try:future=self.collection_executor.submit(self.source_collection.run,result['id'])
            except RuntimeError:
                with self.store.connect() as c:c.execute("UPDATE source_collection_runs SET status='attention',message='采集工作进程未启动，请明确重试' WHERE id=? AND status='queued'",(result['id'],))
                raise Problem('采集工作进程未启动，任务已保留，请明确重试',409)
            with self.collection_lock:self.collection_futures.add(future)
            def finished(done):
                with self.collection_lock:self.collection_futures.discard(done)
            future.add_done_callback(finished)
        return result
    def config(self):
        config=settings()
        if self.models.configured():
            config.update(text_ready=self.models.ready(),text_model=self.models.resolve('primary')['model'])
        return config
    def category_rules(self,pid,b):
        from category_rules import contract
        p=self.store.get(pid)
        if p['revision']!=b.get('revision'):raise Problem('商品已变化，请刷新后再载入规则',409)
        if not p['category']:raise Problem('请先在货源资料中保存 noon 类目代码')
        source=b.get('source')
        if source=='platform':
            if not self.config()['noon_ready']:raise Problem('店铺尚未接入，暂不能读取平台类目规则',409)
            raw=Noon().attributes(p['category'])
        elif source=='file':raw=b.get('contract')
        else:raise Problem('规则来源无效')
        spec=contract(raw)
        if spec.get('category_code') and spec['category_code']!=p['category']:raise Problem('规则文件的类目与当前商品不一致')
        spec.update(category_code=p['category'],source=source,loaded_at=now())
        return self.store.update(pid,{},p['revision'],{'category_contract':spec,'category_verified':False})
    def validate_product_visuals(self,p):
        for im in p['images']:
            aid=im.get('media_asset_id')
            if aid:
                self.visuals.validate_asset(self.media.get(aid),p['id'])
    def queue(self,pid,kind,revision):
        p=self.store.get(pid)
        config=self.config()
        if kind=='translate' and not config['text_ready']: raise Problem('文字服务尚未接入，请在设置页查看配置方式。双语字段可先手动填写。',409)
        if kind in ('submit','refresh','offers','prices') and not config['noon_ready']: raise Problem('noon店铺尚未接入',409)
        if kind=='submit':
            if not config['submit_enabled']: raise Problem('真实内容提交尚未启用',409)
            if not p['reviewed'] or p['issues']: raise Problem('商品需要完成当前版本审核',409)
            if any(not im.get('public_url') for im in p['images']): raise Problem('请为每张成图填写可公开访问的HTTPS地址',409)
            self.validate_product_visuals(p)
            if (p.get('platform') or {}).get('submitted_revision')==p['revision']:
                raise Problem('当前版本已有平台提交回执，请先回查或修改商品',409)
        if kind=='offers' and p['demo']:raise Problem('示例商品不能读取真实报价',409)
        if kind=='prices' and (p['demo'] or p.get('mode')!='LOCAL'):raise Problem('仅真实且已确认本地模式的商品可读取沙特本地售价',409)
        if kind=='refresh' and not (p.get('platform') or {}).get('sku_parent'): raise Problem('尚无平台商品编号可回查',409)
        jid=self.store.add_job(pid,kind,revision)
        self.executor.submit(self.run,jid,p,kind)
        return {'job_id':jid}
    def run(self,jid,p,kind):
        # Claim only pending work. A queued executor callback may already be cancelled.
        with self.store.connect() as c:
            claimed=c.execute("UPDATE jobs SET status='running',message='正在处理',updated_at=? WHERE id=? AND kind=? AND status='queued'",(now(),jid,kind)).rowcount
        if not claimed:return
        try:
            if kind=='image-host':
                result=self.image_host.publish(p)
                self.store.job_result(jid,'done','图片已上传并通过公网内容核对，请重新审核商品',result)
            elif kind=='translate':
                if self.models.configured():
                    generated=self.models.translate(p,'product-job:'+jid)
                    out,warnings,usage=generated['content'],generated['warnings'],generated['usage']
                else:out,warnings,usage=translate(p,self.models.matched_terms(p))
                updated=self.store.update(p['id'],out,p['revision'],internal={'content_notes':warnings})
                self.store.job_result(jid,'done','双语草稿已生成，请人工核对事实',{'warnings':warnings,'usage':usage,'revision':updated['revision']})
            elif kind=='submit':
                # Recheck time-sensitive supply and review state immediately before the write.
                current=self.store.get(p['id'])
                if current['revision']!=p['revision'] or not current['reviewed'] or current['issues']:
                    raise Problem('提交前检查未通过，请重新审核',409)
                self.validate_product_visuals(current)
                if (current.get('platform') or {}).get('submitted_revision')==current['revision']:
                    raise Problem('当前版本已有平台提交回执，本次未重复发送',409)
                if not self.config()['noon_ready'] or not self.config()['submit_enabled']:
                    raise Problem('店铺内容提交配置已变化，本次未发送',409)
                client=Noon(); data=payload(current)
                preflight_attributes(client.attributes(current['category']),data)
                try: result=client.submit(data)
                except Exception:
                    self.store.job_result(jid,'uncertain','提交请求可能已到达 noon。请先按 SKU 在平台核对，禁止直接重发当前版本')
                    return
                if not isinstance(result,dict) or not result.get('sku_parent'):
                    self.store.job_result(jid,'uncertain','平台未返回商品编号，提交结果待核对；请勿直接重复提交')
                    return
                try:
                    saved={'sku_parent':result['sku_parent'],'submitted_revision':current['revision'],'submit_response':result,'checked_at':now(),'live_verified':False}
                    self.store.record_platform(p['id'],saved)
                    status=result.get('status',{}).get('status_id')
                    self.store.job_result(jid,'done' if status==0 else 'needs_attention',
                        '内容已提交，等待平台审核；尚未确认可售' if status==0 else '平台已保存但内容存在问题，请回查',saved)
                except Exception:
                    self.store.job_result(jid,'uncertain','noon 可能已接收内容，但本地回执未完整保存。请先按 SKU 回查，禁止直接重发当前版本')
            elif kind=='offers':
                result=Noon().offers(p['partner_sku'])
                self.store.record_offer(p['id'],p['partner_sku'],result,p['revision'])
                self.store.job_result(jid,'done','已读取报价、净库存及前台可见反馈；未修改价格库存')
            elif kind=='prices':
                current=self.store.get(p['id'])
                if current['revision']!=p['revision'] or current.get('mode')!='LOCAL' or current['partner_sku']!=p['partner_sku']:
                    raise Problem('经营模式或商品资料已变化，本次售价回查未发送',409)
                result=Noon().pricing_get_sa(p['partner_sku'])
                self.store.record_pricing(p['id'],p['partner_sku'],result,p['revision'])
                item=result['items'][0]
                code=item['status']['status_code']
                self.store.job_result(jid,'done' if code=='OK' and item['status']['status_id']==0 else 'needs_attention',
                    '已读取沙特本地售价与启用意图；未修改价格' if code=='OK' and item['status']['status_id']==0 else '平台未返回可用本地售价：'+code)
            elif kind=='refresh':
                parent=p['platform']['sku_parent']
                if (self.store.get(p['id']).get('platform') or {}).get('sku_parent')!=parent:
                    raise Problem('平台商品编号已变化，本次回查未发送，请重新安排',409)
                result=Noon().content(parent)
                if not isinstance(result,dict) or result.get('sku_parent')!=parent or not isinstance(result.get('statuses'),list):
                    raise Problem('平台回查返回编号不匹配或格式不完整，保留上次有效记录',502)
                self.store.record_platform(p['id'],{**p['platform'],'content_response':result,'checked_at':now(),'live_verified':False},expected_parent=parent)
                self.store.job_result(jid,'done','已读取内容审核结果；售价、库存及实际可售仍需验证')
        except Problem as e:
            self.store.job_result(jid,'failed',str(e))
        except Exception:
            self.store.job_result(jid,'failed','任务处理失败，资料已保留。请检查服务配置或图片文件。')

    def run_transfer_batch(self,dispatch):
        """Claim selected jobs, then make one NGS read for all still-current SKUs."""
        claimed=[]
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for jid,p in dispatch:
                if c.execute("UPDATE jobs SET status='running',message='正在批量读取NGS转移价',updated_at=? WHERE id=? AND kind='transfer' AND status='queued'",(now(),jid)).rowcount:
                    claimed.append((jid,p))
        if not claimed:return
        eligible=[]
        for jid,p in claimed:
            try:
                current=self.store.get(p['id'])
                if current['revision']!=p['revision'] or current.get('mode')!='NGS' or current['partner_sku']!=p['partner_sku']:
                    raise Problem('商品资料或经营模式已变化，本次转移价回查未发送',409)
                eligible.append((jid,p))
            except Problem as e:self.store.job_result(jid,'failed',str(e))
        if not eligible:return
        try:
            from transfer_status import batch_items
            items=batch_items(Noon().transfer_prices_get([p['partner_sku'] for _,p in eligible]),[p['partner_sku'] for _,p in eligible])
        except Problem as e:
            for jid,_ in eligible:self.store.job_result(jid,'failed',str(e))
            return
        except Exception:
            for jid,_ in eligible:self.store.job_result(jid,'failed','转移价批量读取失败；旧记录已保留')
            return
        for jid,p in eligible:
            try:
                item=items.get(p['partner_sku'])
                if item is None:raise Problem('平台未返回此SKU的转移价，保留旧记录',502)
                self.store.record_transfer_price(p['id'],p['partner_sku'],item,p['revision'])
                status=item['status'];ok=status['status_id']==0
                self.store.job_result(jid,'done' if ok else 'needs_attention',
                    '已读取NGS美元转移价；未修改平台报价' if ok else '平台未返回可用转移价：'+status['status_code'])
            except Problem as e:self.store.job_result(jid,'failed',str(e))
            except Exception:self.store.job_result(jid,'failed','转移价结果处理失败，旧记录已保留')

class Handler(BaseHTTPRequestHandler):
    server_version='ProductWorkbench/0.1'
    def log_message(self,*args): pass
    @property
    def app(self): return self.server.app
    def respond(self,body,status=200,content_type='application/json; charset=utf-8',filename=None):
        if isinstance(body,(dict,list)): body=json.dumps(body,ensure_ascii=False,allow_nan=False).encode()
        if isinstance(body,str): body=body.encode()
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        if filename:
            safe_name=re.sub(r'[^A-Za-z0-9_.-]','_',filename)
            self.send_header('Content-Disposition',f'attachment; filename="{safe_name}"; filename*=UTF-8\'\'{quote(filename,safe="")}')
        self.end_headers()
        if self.command!='HEAD':self.wfile.write(body)
    def safe_host(self):
        expected={f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}
        if self.headers.get('Host') not in expected: raise Problem('仅允许从本机访问',403)
        origin=self.headers.get('Origin')
        if origin and origin not in {'http://'+x for x in expected}: raise Problem('不允许跨站请求',403)
    def body(self):
        try: length=int(self.headers.get('Content-Length','0'))
        except ValueError: raise Problem('请求长度无效')
        if length<0 or length>16*1024*1024: raise Problem('请求内容超过16MB限制',413)
        try:
            value=json.loads(self.rfile.read(length))
            if not isinstance(value,dict): raise ValueError()
            return value
        except (ValueError,UnicodeDecodeError): raise Problem('请求需要JSON对象')
    def serve_media(self,file,filename=None):
        if not file.is_file():raise Problem('素材不存在',404)
        size=file.stat().st_size;start=0;end=size-1;status=200
        byte_range=self.headers.get('Range')
        if byte_range:
            match=re.fullmatch(r'bytes=(\d*)-(\d*)',byte_range)
            if not match or not any(match.groups()):raise Problem('不支持该读取范围',416)
            if not match[1]:start=max(0,size-int(match[2]))
            else:start=int(match[1]);end=min(end,int(match[2])) if match[2] else end
            if start> end or start>=size:raise Problem('读取范围超过文件大小',416)
            status=206
        self.send_response(status);self.send_header('Content-Type',mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
        if filename:self.send_header('Content-Disposition',f'attachment; filename="{filename}"')
        self.send_header('Accept-Ranges','bytes');self.send_header('Content-Length',str(end-start+1));self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        if status==206:self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
        self.end_headers()
        if self.command=='HEAD':return
        with file.open('rb') as f:
            f.seek(start);remaining=end-start+1
            try:
                while remaining:
                    data=f.read(min(65536,remaining))
                    if not data:break
                    self.wfile.write(data);remaining-=len(data)
            except (BrokenPipeError,ConnectionResetError):pass
    def upload_media(self,bulk=False):
        try:length=int(self.headers.get('Content-Length','0'))
        except ValueError:raise Problem('文件长度无效')
        limit=25*1024*1024 if bulk else MAX_UPLOAD
        if not 0<length<=limit:raise Problem('文件超过大小限制（批量原图25MB，普通素材100MB）',413)
        if self.headers.get('Content-Type')!='application/octet-stream':raise Problem('素材上传需使用二进制文件')
        self.connection.settimeout(30)
        with tempfile.NamedTemporaryFile(dir=self.app.media.temp,suffix='.upload') as f:
            remaining=length
            while remaining:
                chunk=self.rfile.read(min(65536,remaining))
                if not chunk:raise Problem('文件上传中断')
                f.write(chunk);remaining-=len(chunk)
            f.flush()
            if bulk:
                result=self.app.media_import.ingest(f.name,unquote(self.headers.get('X-Media-Name','')),unquote(self.headers.get('X-Media-Rights','')),self.headers.get('X-Media-Match',''))
            else:
                result=self.app.media.ingest(f.name,unquote(self.headers.get('X-Media-Name','')),unquote(self.headers.get('X-Media-Rights','')),self.headers.get('X-Media-Product',''))
        return self.respond(result,201)
    def upload_catalog(self):
        from source_inbox import MAX_CATALOG_FILE
        try:length=int(self.headers.get('Content-Length','0'))
        except ValueError:raise Problem('资料文件长度无效')
        if not 0<length<=MAX_CATALOG_FILE:raise Problem('资料文件需在20MB以内',413)
        if self.headers.get('Content-Type')!='application/octet-stream':raise Problem('请上传二进制 CSV 或 JSON 文件')
        self.connection.settimeout(60)
        with tempfile.NamedTemporaryFile(suffix='.upload') as f:
            remaining=length
            while remaining:
                chunk=self.rfile.read(min(65536,remaining))
                if not chunk:raise Problem('资料上传中断')
                f.write(chunk);remaining-=len(chunk)
            f.flush()
            result=self.app.source_inbox.deposit(f.name,unquote(self.headers.get('X-Catalog-Name','')),self.headers.get('X-Catalog-Kind',''))
        return self.respond(result,201)

    def upload_backup(self):
        try:length=int(self.headers.get('Content-Length','0'))
        except ValueError:raise Problem('备份文件长度无效')
        if not 0<length<=MAX_ARCHIVE:raise Problem('备份文件需在2GB以内',413)
        if self.headers.get('Content-Type')!='application/octet-stream':raise Problem('请上传备份ZIP文件')
        import shutil
        if shutil.disk_usage(self.app.recovery.home).free<length+64*1024**2:raise Problem('磁盘空间不足以接收备份')
        self.connection.settimeout(120)
        with tempfile.NamedTemporaryFile(dir=self.app.recovery.home,suffix='.upload') as f:
            remaining=length
            while remaining:
                block=self.rfile.read(min(1024*1024,remaining))
                if not block:raise Problem('备份上传中断')
                f.write(block);remaining-=len(block)
            f.flush();return self.respond(self.app.recovery.inspect(f.name))
    def do_HEAD(self):
        self.do_GET()
    def do_GET(self):
        try:
            self.safe_host(); path=urlsplit(self.path).path
            query=parse_qs(urlsplit(self.path).query)
            params={key:values[0] for key,values in query.items()}
            if path=='/api/bank-reconciliation/state':return self.respond(self.app.bank_reconciliation.state(params.get('page','0'),params.get('entry_page','0')))
            if path=='/api/bank-reconciliation/template':return self.respond(self.app.bank_reconciliation.template(),content_type='text/csv; charset=utf-8',filename='银行流水核对模板.csv')
            if path=='/api/bank-reconciliation/export':return self.respond(self.app.bank_reconciliation.export(params.get('id')),content_type='text/csv; charset=utf-8',filename='银行流水核对.csv')
            if path=='/api/fulfillment/state':return self.respond(self.app.fulfillment.state(params.get('page','0')))
            if path=='/api/supplier-quotes/state':return self.respond(self.app.supplier_quotes.state(params.get('page','0'),params.get('query',''),params.get('product_page','0'),params.get('supplier_page','0')))
            if path=='/api/supplier-quotes/get':return self.respond(self.app.supplier_quotes.get(params.get('id','')))
            if path=='/api/supplier-quotes/export':return self.respond(self.app.supplier_quotes.export(),content_type='text/csv; charset=utf-8',filename='供应商报价比较.csv')
            if path=='/api/fx-registry/state':return self.respond(self.app.fx_registry.state(params.get('page','0'),params.get('query','')))
            if path=='/api/fx-registry/get':return self.respond(self.app.fx_registry.get(params.get('id',''),params.get('history_page','0')))
            if path=='/api/fx-registry/export':return self.respond(self.app.fx_registry.export(),content_type='text/csv; charset=utf-8',filename='汇率档案.csv')
            if path=='/api/replenishment/state':return self.respond(self.app.replenishment.state(params))
            if path=='/api/replenishment/export':return self.respond(self.app.replenishment.export(params),content_type='text/csv; charset=utf-8',filename='库存补货建议.csv')
            if path=='/api/domestic-capture/state':return self.respond(self.app.domestic_capture.state(params.get('page','0')))
            if path=='/api/domestic-capture/extension':
                buffer=io.BytesIO()
                directory=BASE/'domestic-extension' if getattr(sys,'frozen',False) else BASE.parent/'browser-extension'/'domestic-capture'
                with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
                    for name in ('manifest.json','parser.js','popup.html','popup.css','popup.js'):
                        source=directory/name
                        if not source.is_file() or source.is_symlink():raise Problem('采集扩展源码缺失，请核对软件安装',503)
                        archive.writestr('noon-domestic-capture/'+name,source.read_bytes())
                return self.respond(buffer.getvalue(),content_type='application/zip',filename='国内商品采集扩展.zip')
            if path=='/api/inventory-counts/state':return self.respond(self.app.inventory_counts.state(params.get('page','0'),params.get('warehouse_id',''),params.get('query','')))
            if path=='/api/inventory-counts/template':return self.respond(self.app.inventory_counts.template(params.get('warehouse_id',''),params.get('query',''),params.get('page','0')),content_type='text/csv; charset=utf-8',filename='库存实盘模板.csv')
            if path=='/api/inventory-counts/export':return self.respond(self.app.inventory_counts.export(params.get('document_id','')),content_type='text/csv; charset=utf-8',filename='库存盘点单.csv')
            if path=='/api/shipping-manifests/get':return self.respond(self.app.shipping_manifests.get(params.get('id','')))
            if path=='/api/shipping-manifests/state':return self.respond(self.app.shipping_manifests.state(params.get('page','0'),params.get('query',''),params.get('order_page','0')))
            if path=='/api/shipping-manifests/export':return self.respond(self.app.shipping_manifests.export(params.get('id',''),params.get('kind','packages')),content_type='text/csv; charset=utf-8',filename='物流包装清单.csv')
            if path=='/api/pricing-plans/get':return self.respond(self.app.pricing_plans.get(params.get('id','')))
            if path=='/api/pricing-plans/state':return self.respond(self.app.pricing_plans.state(params.get('page','0'),params.get('query',''),params.get('product_page','0')))
            if path=='/api/pricing-plans/export':return self.respond(self.app.pricing_plans.export(),content_type='text/csv; charset=utf-8',filename='本地价格方案.csv')
            if path=='/api/ad-analytics/state':return self.respond(self.app.ad_analytics.state(params))
            if path=='/api/ad-analytics/template':return self.respond(self.app.ad_analytics.template(params),content_type='text/csv; charset=utf-8',filename='广告归因模板.csv')
            if path=='/api/ad-analytics/export':return self.respond(self.app.ad_analytics.export(params),content_type='text/csv; charset=utf-8',filename='广告归因分析.csv')
            if path=='/api/import-profiles/state':return self.respond(self.app.import_profiles.state(params.get('page','0')))
            if path=='/api/import-profiles/schema':return self.respond(self.app.import_profiles.schema())
            if path=='/api/fulfillment/export':return self.respond(self.app.fulfillment.export(params.get('id')),content_type='text/csv; charset=utf-8',filename='波次拣货单.csv')
            if path=='/api/procurement/state':return self.respond(self.app.procurement.state(params))
            if path=='/api/procurement/export':return self.respond(self.app.procurement.export(params),content_type='text/csv; charset=utf-8',filename='采购订单关联.csv')
            if path=='/api/after-sales/state':return self.respond(self.app.after_sales.state(params.get('page','0'),params.get('query',''),params.get('order_page','0')))
            if path=='/api/after-sales/export':return self.respond(self.app.after_sales.export(),content_type='text/csv; charset=utf-8',filename='售后核对.csv')
            if path=='/api/alerts/state':return self.respond(self.app.alerts.state(params.get('page','0'),params.get('group','all'),params.get('severity','all'),params.get('sort','severity'),params.get('mode','all')))
            if path=='/api/alerts/export':return self.respond(self.app.alerts.export(params),content_type='text/csv; charset=utf-8',filename='本地异常核对.csv')
            if path=='/api/batch-editor/state':return self.respond(self.app.batch_editor.state(params.get('page','0'),params.get('query',''),params.get('group','all')))
            if path=='/api/order-intake/state':return self.respond(self.app.order_intake.state(params.get('page','0')))
            if path=='/api/order-intake/template':return self.respond(self.app.order_intake.template().encode('utf-8'),content_type='text/csv; charset=utf-8',filename='订单导入模板.csv')
            if path=='/api/settlements/state':return self.respond(self.app.settlements.state(params.get('page','0')))
            if path=='/api/settlements/template':return self.respond(self.app.settlements.template(),content_type='text/csv; charset=utf-8',filename='结算核对模板.csv')
            if path=='/api/settlements/export':return self.respond(self.app.settlements.export(params.get('id')),content_type='text/csv; charset=utf-8',filename='结算核对.csv')
            if path=='/api/catalog-groups/export':return self.respond(self.app.catalog_groups.export_csv(params.get('id','')),content_type='text/csv; charset=utf-8',filename='catalog-group-quality.csv')
            if path=='/api/catalog-groups/state':return self.respond(self.app.catalog_groups.state(params.get('page','0'),params.get('query',''),params.get('product_page','0')))
            if path=='/api/analytics/state':return self.respond(self.app.analytics.state(params))
            if path=='/api/analytics/export':return self.respond(self.app.analytics.export(params),content_type='text/csv; charset=utf-8',filename='运营分析.csv')
            if path=='/api/collection-schedules/state':return self.respond(self.app.collection_schedules.state(params.get('page','0')))
            if path=='/api/backup-schedules/state':return self.respond(self.app.backup_schedules.state())
            if path=='/api/channels/state':return self.respond(self.app.channel_accounts.state())
            if path=='/api/collection/state':
                query=parse_qs(urlsplit(self.path).query)
                return self.respond(self.app.source_collection.state(query.get('page',['0'])[0],query.get('run_page',['0'])[0]))
            if path=='/api/platform-batch/status':
                return self.respond(PlatformBatch(self.app,parse_qs(urlsplit(self.path).query).get('kind',['refresh'])[0]).status(parse_qs(urlsplit(self.path).query).get('request_id',[''])[0]))
            if path=='/api/content-submit-batch/status':
                return self.respond(ContentSubmitBatch(self.app).status(parse_qs(urlsplit(self.path).query).get('request_id',[''])[0]))
            if path=='/api/catalog-campaign/history':
                return self.respond(self.app.catalog_campaign.history(parse_qs(urlsplit(self.path).query).get('page',['0'])[0]))
            if path=='/api/catalog-campaign/exceptions':
                return self.respond(self.app.catalog_campaign.exceptions(parse_qs(urlsplit(self.path).query).get('request_id',[''])[0]))
            if path=='/api/source-leads/list':
                return self.respond(self.app.source_leads.list(parse_qs(urlsplit(self.path).query).get('page',['0'])[0]))
            if path=='/api/source-leads/export':
                return self.respond(self.app.source_leads.export(parse_qs(urlsplit(self.path).query).get('page',['0'])[0]))
            if path=='/api/history/list':
                query=parse_qs(urlsplit(self.path).query);page=query.get('page',['0'])[0]
                if not page.isdecimal():raise Problem('记录页码无效')
                return self.respond(self.app.store.history_page(query.get('stream',['jobs'])[0],int(page),query.get('group',['all'])[0],query.get('kind',['all'])[0]))
            if path=='/api/automation/attention':
                query=parse_qs(urlsplit(self.path).query)
                return self.respond(self.app.automation.attention(query.get('page',['0'])[0],query.get('group',['all'])[0]))
            if path=='/api/visual-checks/list':
                query=parse_qs(urlsplit(self.path).query);page=query.get('page',['0'])[0];candidate_page=query.get('candidate_page',['0'])[0]
                if not page.isdecimal() or not candidate_page.isdecimal():raise Problem('检查页码无效')
                return self.respond(self.app.visual_checks.state(int(page),query.get('group',['all'])[0],candidate_page=int(candidate_page)))
            if path=='/api/visuals/list':
                query=parse_qs(urlsplit(self.path).query)
                return self.respond(self.app.visuals.list_jobs(query.get('page',['0'])[0],query.get('group',['review'])[0]))
            if path=='/api/source-inbox/list':
                query=parse_qs(urlsplit(self.path).query)
                return self.respond(self.app.source_inbox.list_files(query.get('page',['0'])[0],query.get('group',['attention'])[0],query.get('kind',['all'])[0],query.get('query',[''])[0]))
            if path=='/api/source-inbox/detail':
                query=parse_qs(urlsplit(self.path).query)
                return self.respond(self.app.source_inbox.file_detail(query.get('name',[''])[0],query.get('digest',[''])[0]))
            if path=='/api/media/library':
                query=parse_qs(urlsplit(self.path).query);page=query.get('page',['0'])[0]
                if not page.isdecimal():raise Problem('素材页码无效')
                return self.respond(self.app.media.library(int(page),query.get('kind',['all'])[0],query.get('search',[''])[0]))
            if path=='/api/media/task-list':
                query=parse_qs(urlsplit(self.path).query);page=query.get('page',['0'])[0]
                if not page.isdecimal():raise Problem('加工任务页码无效')
                return self.respond(self.app.media.list_tasks(int(page),query.get('group',['all'])[0]))
            if path=='/api/media/references':
                query=parse_qs(urlsplit(self.path).query);page=query.get('page',['0'])[0]
                if not page.isdecimal():raise Problem('原图页码无效')
                return self.respond(self.app.media.references(query.get('product_id',[''])[0],int(page),query.get('search',[''])[0],query.get('include_unassigned',['0'])[0]=='1'))
            if path=='/api/media/asset':
                query=parse_qs(urlsplit(self.path).query)
                return self.respond(self.app.media.get(query.get('id',[''])[0]))
            if path=='/api/state':
                query=parse_qs(urlsplit(self.path).query)
                surface=query.get('surface',[''])[0]
                channels_state={'channels':self.app.channel_accounts.state(),'source_collection':self.app.source_collection.state()} if surface=='channels' else {}
                ops_views={'overview','orders','purchases','inventory','warehouse','partners','finance'}
                return self.respond({**channels_state,**self.app.store.catalog_snapshot(query.get('catalog_token',[None])[0],paged=query.get('catalog_paged',['0'])[0]=='1'),**self.app.store.history(False),'ops':self.app.ops.state(surface,query.get('ops_page',['0'])[0]) if surface in ops_views else None,'finance':self.app.finance.state() if surface=='finance' else None,'media':self.app.media.state(False,surface=='batch',False),'visuals':self.app.visuals.state(False),'visual_presets':self.app.visual_presets.list() if surface in ('batch','import') else [],'visual_checks':self.app.visual_checks.state(include_detail=False),'models':self.app.models.state(),'source_inbox':self.app.source_inbox.state(False),'recovery':self.app.recovery.state(),'image_host':self.app.image_host.state(),'image_host_batch':self.app.image_host.status(),'platform_batch_latest':PlatformBatch(self.app).latest(),'content_submit_batch_latest':ContentSubmitBatch(self.app).latest(),'catalog_campaign_latest':self.app.catalog_campaign.latest(),'automation':self.app.automation.state(query.get('auto_page',['0'])[0],query.get('auto_group',['all'])[0],query.get('auto_query',[''])[0],query.get('auto_item_pages',['{}'])[0],surface=='automation',surface=='batch'),'config':{**self.app.config(),'data_location':str(self.app.store.root)},'token':self.app.token})
            if path=='/api/catalog/snapshot':
                query=parse_qs(urlsplit(self.path).query)
                return self.respond(self.app.store.catalog_snapshot(query.get('catalog_token',[None])[0],paged=query.get('catalog_paged',['0'])[0]=='1'))
            if path.startswith('/api/backup/download/'):
                key=path.removeprefix('/api/backup/download/')
                return self.serve_media(self.app.recovery.archive_path(key),'Noon-Studio-backup-'+key+'.zip')
            if path.startswith('/media/'):
                name=path.removeprefix('/media/')
                if not re.fullmatch(r'[a-f0-9]{32}(?:-preview)?\.(jpg|png|webp|mp4|webm)',name):raise Problem('素材不存在',404)
                return self.serve_media(self.app.media.root/name)
            if path.startswith('/assets/'):
                name=path.split('/')[-1]
                if not re.fullmatch(r'[a-f0-9]{32}(?:-source)?\.(jpg|png|webp)',name): raise Problem('图片不存在',404)
                file=self.app.store.assets/name
            else:
                name={'/':'index.html','/supplier_quotes.js':'supplier_quotes.js','/fx_registry.js':'fx_registry.js','/replenishment.js':'replenishment.js','/domestic_capture.js':'domestic_capture.js','/inventory_counts.js':'inventory_counts.js','/shipping_manifests.js':'shipping_manifests.js','/pricing_plans.js':'pricing_plans.js','/ad_analytics.js':'ad_analytics.js','/import_profiles.js':'import_profiles.js','/bank_reconciliation.js':'bank_reconciliation.js','/fulfillment.js':'fulfillment.js','/procurement.js':'procurement.js','/after_sales.js':'after_sales.js','/alerts.js':'alerts.js','/batch_editor.js':'batch_editor.js','/order_intake.js':'order_intake.js','/settlement_intake.js':'settlement_intake.js','/catalog_groups.js':'catalog_groups.js','/analytics.js':'analytics.js','/collection_schedules.js':'collection_schedules.js','/backup_schedules.js':'backup_schedules.js','/channels.js':'channels.js','/platform.js':'platform.js','/content_submit_batch.js':'content_submit_batch.js','/catalog.js':'catalog.js','/video_batch.js':'video_batch.js','/source_import.js':'source_import.js','/app.js':'app.js','/style.css':'style.css','/favicon.svg':'favicon.svg','/operations.js':'operations.js','/media.js':'media.js','/media_import.js':'media_import.js','/automation.js':'automation.js','/visuals.js':'visuals.js','/visual_checks.js':'visual_checks.js','/finance.js':'finance.js','/warehouse.js':'warehouse.js','/stock_plan.js':'stock_plan.js','/recovery.js':'recovery.js','/image_host.js':'image_host.js','/workflow_visual.js':'workflow_visual.js','/approval_batch.js':'approval_batch.js','/batch.js':'batch.js','/category.js':'category.js','/category_batch.js':'category_batch.js','/visual_batch.js':'visual_batch.js'}.get(path)
                if not name: raise Problem('页面不存在',404)
                file=BASE/'static'/name
            if not file.is_file(): raise Problem('文件不存在',404)
            return self.respond(file.read_bytes(),content_type=mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
        except Problem as e: self.respond({'error':str(e)},e.status)
    def do_POST(self):
        with self.app.write_lock:self.post_locked()
    def post_locked(self):
        try:
            self.safe_host()
            if not secrets.compare_digest(self.headers.get('X-Workbench-Token',''),self.app.token): raise Problem('会话已过期，请刷新页面',403)
            path=urlsplit(self.path).path; store=self.app.store
            if self.app.recovery.pending.exists() and not path.startswith('/api/backup/'):
                raise Problem('已安排资料恢复，请退出并重新打开应用，或在备份页面取消恢复',409)
            if path=='/api/backup/inspect':return self.upload_backup()
            if path=='/api/media/upload':return self.upload_media()
            if path=='/api/media/bulk-upload':return self.upload_media(bulk=True)
            if path=='/api/source-inbox/upload':return self.upload_catalog()
            b=self.body()
            extension_actions={
                '/api/supplier-quotes/preview':self.app.supplier_quotes.preview,
                '/api/supplier-quotes/save':self.app.supplier_quotes.save,
                '/api/supplier-quotes/cancel':self.app.supplier_quotes.cancel,
                '/api/fx-registry/preview':self.app.fx_registry.preview,
                '/api/fx-registry/save':self.app.fx_registry.save,
                '/api/fx-registry/cancel':self.app.fx_registry.cancel,
                '/api/fx-registry/convert':self.app.fx_registry.convert,
                '/api/replenishment/preview':self.app.replenishment.preview,
                '/api/replenishment/apply':self.app.replenishment.apply,
                '/api/domestic-capture/preview':self.app.domestic_capture.preview,
                '/api/domestic-capture/apply':self.app.domestic_capture.apply,
                '/api/inventory-counts/preview':self.app.inventory_counts.preview,
                '/api/inventory-counts/apply':self.app.inventory_counts.apply,
                '/api/shipping-manifests/preview':self.app.shipping_manifests.preview,
                '/api/shipping-manifests/apply':self.app.shipping_manifests.apply,
                '/api/shipping-manifests/handoff':self.app.shipping_manifests.handoff,
                '/api/pricing-plans/preview':self.app.pricing_plans.preview,
                '/api/pricing-plans/save':self.app.pricing_plans.save,
                '/api/pricing-plans/cancel':self.app.pricing_plans.cancel,
                '/api/ad-analytics/preview':self.app.ad_analytics.preview,
                '/api/ad-analytics/apply':self.app.ad_analytics.apply,
                '/api/import-profiles/save':self.app.import_profiles.save,
                '/api/import-profiles/remove':self.app.import_profiles.remove,
                '/api/bank-reconciliation/preview':self.app.bank_reconciliation.preview,
                '/api/bank-reconciliation/import':self.app.bank_reconciliation.import_rows,
                '/api/bank-reconciliation/match':self.app.bank_reconciliation.match,
                '/api/fulfillment/preview':self.app.fulfillment.preview,
                '/api/fulfillment/apply':self.app.fulfillment.apply,
                '/api/procurement/preview':self.app.procurement.preview,
                '/api/procurement/apply':self.app.procurement.apply,
                '/api/procurement/unlink':self.app.procurement.unlink,
                '/api/after-sales/preview':self.app.after_sales.preview,
                '/api/after-sales/create':self.app.after_sales.create,
                '/api/after-sales/receive':self.app.after_sales.receive,
                '/api/after-sales/refund':self.app.after_sales.refund,
                '/api/after-sales/dispose':self.app.after_sales.dispose,
                '/api/alerts/action':self.app.alerts.action,
                '/api/batch-editor/preview':self.app.batch_editor.preview,
                '/api/batch-editor/apply':self.app.batch_editor.apply,
                '/api/order-intake/preview':self.app.order_intake.preview,
                '/api/order-intake/apply':self.app.order_intake.apply,
                '/api/settlements/preview':self.app.settlements.preview,
                '/api/settlements/apply':self.app.settlements.apply,
                '/api/catalog-groups/diagnose':self.app.catalog_groups.diagnose,
                '/api/catalog-groups/preview':self.app.catalog_groups.preview,
                '/api/catalog-groups/save':self.app.catalog_groups.save,
                '/api/catalog-groups/remove':self.app.catalog_groups.remove,
                '/api/collection-schedules/save':self.app.collection_schedules.save,
                '/api/collection-schedules/control':self.app.collection_schedules.control,
                '/api/backup-schedules/save':self.app.backup_schedules.save,
                '/api/backup-schedules/run':self.app.backup_schedules.run,
            }
            if path in extension_actions:return self.respond(extension_actions[path](b))
            if path=='/api/channels/save':return self.respond(self.app.channel_accounts.save(b))
            if path=='/api/channels/remove':return self.respond(self.app.channel_accounts.remove(b))
            if path=='/api/collection/create':return self.respond(self.app.dispatch_collection(self.app.source_collection.create(b)),202)
            if path=='/api/collection/control':return self.respond(self.app.dispatch_collection(self.app.source_collection.control(b)))
            if path=='/api/collection/preview':return self.respond(self.app.source_collection.preview(b))
            if path=='/api/collection/apply':return self.respond(self.app.source_collection.apply(b))
            if path=='/api/collection/resolve':return self.respond(self.app.source_collection.resolve(b))
            if path.startswith('/api/image-host/'):
                action=path.removeprefix('/api/image-host/')
                if action not in ('save','preview','apply','cancel'):raise Problem('操作不存在',404)
                return self.respond(getattr(self.app.image_host,action)(b))
            if path=='/api/media/import-preview':return self.respond(self.app.media_import.preview(b))
            if path=='/api/stock-plan/preview':return self.respond(StockPlan(self.app).preview(b))
            if path=='/api/catalog-campaign/preview':return self.respond(self.app.catalog_campaign.preview(b))
            if path=='/api/catalog-campaign/apply':return self.respond(self.app.catalog_campaign.apply(b))
            if path=='/api/source-leads/preview':return self.respond(self.app.source_leads.preview(b))
            if path=='/api/source-leads/add':return self.respond(self.app.source_leads.add(b))
            if path=='/api/backup/create':return self.respond(self.app.recovery.create())
            if path=='/api/backup/schedule':
                with self.app.collection_lock:
                    if self.app.collection_futures:raise Problem('采集仍有读取请求未结束，请等待完成后安排恢复',409)
                    if self.app.backup_schedules.inflight:raise Problem('周期备份仍在执行，请等待完成后安排恢复',409)
                    return self.respond(self.app.recovery.schedule(b))
            if path=='/api/backup/cancel':return self.respond(self.app.recovery.cancel())
            if path=='/api/finance/export':return self.respond(self.app.finance.csv(),content_type='text/csv; charset=utf-8',filename='noon-finance.csv')
            if path.startswith('/api/finance/'):return self.respond(self.app.finance.transact(path.removeprefix('/api/finance/'),b))
            if path=='/api/models/codex/status':return self.respond(self.app.models.codex.refresh())
            if path=='/api/source-inbox/config':return self.respond(self.app.source_inbox.configure(b))
            if path=='/api/source-inbox/visual-config':return self.respond(self.app.source_inbox.configure_visual(b))
            if path=='/api/source-inbox/video-config':return self.respond(self.app.source_inbox.configure_video(b))
            if path=='/api/source-inbox/open':return self.respond(self.app.source_inbox.open_folder(b.get('kind','new')))
            if path=='/api/models/save':return self.respond(self.app.models.save(b))
            if path=='/api/models/term/save':return self.respond(self.app.models.save_term(b))
            if path=='/api/models/term/delete':return self.respond(self.app.models.delete_term(b))
            if path=='/api/models/route':return self.respond(self.app.models.route(b))
            if path=='/api/models/probe':
                from models import text
                key=text(b.get('request_id'),'请求编号',100)
                return self.respond(self.app.models.call(b.get('profile_id'),{'test':'connection'},'probe:'+key,'probe'))
            if path=='/api/platform-batch/cancel':return self.respond(PlatformBatch(self.app,b.get('kind','refresh')).cancel(b.get('request_id')))
            if path.startswith('/api/content-submit-batch/'):
                action=path.removeprefix('/api/content-submit-batch/')
                if action not in ('preview','apply','cancel'):raise Problem('操作不存在',404)
                batch=ContentSubmitBatch(self.app)
                return self.respond(getattr(batch,action)(b))
            if path in ('/api/platform-batch/preview','/api/platform-batch/apply'):
                batch=PlatformBatch(self.app,b.get('kind','refresh'))
                return self.respond(batch.preview(b) if path.endswith('/preview') else batch.apply(b))
            if path in ('/api/approval-batch/preview','/api/approval-batch/apply'):
                from approval_batch import ApprovalBatch
                batch=ApprovalBatch(store,self.app.validate_product_visuals)
                return self.respond(batch.apply(b) if path.endswith('/apply') else batch.preview(b))
            if path=='/api/automation/preflight':return self.respond(self.app.automation.preflight(b))
            if path=='/api/category-batch/preview':
                from category_batch import CategoryBatch
                preview=CategoryBatch(self.app.store).preview(b)
                preview.pop('updates',None)
                return self.respond(preview)
            if path=='/api/automation/create':return self.respond(self.app.automation.create(b),202)
            if path=='/api/automation/control':return self.respond(self.app.automation.control(b))
            if path=='/api/automation/visual-rework':return self.respond(self.app.automation.rework_visual(b),202)
            match=re.fullmatch(r'/api/products/([a-f0-9]+)/category-rules',path)
            if match:return self.respond(self.app.category_rules(match.group(1),b))
            if path=='/api/media/tasks':return self.respond(self.app.media.submit(b),202)
            if path=='/api/visual-checks/preview':
                result=self.app.visual_checks.preview(b);result.pop('plans',None);return self.respond(result)
            if path=='/api/visual-checks/submit':return self.respond(self.app.visual_checks.submit(b),202)
            if path=='/api/visual-checks/control':return self.respond(self.app.visual_checks.control(b))
            if path=='/api/visuals/batch-preview':
                preview=self.app.visual_batch.preview(b);preview.pop('plans',None);return self.respond(preview)
            if path=='/api/visuals/batch-apply':return self.respond(self.app.visual_batch.apply(b),202)
            if path=='/api/visuals/get':return self.respond(self.app.visuals.detail(b.get('id')))
            if path=='/api/visuals/tasks':return self.respond(self.app.visuals.submit(b),202)
            if path=='/api/visuals/control':return self.respond(self.app.visuals.control(b))
            if path=='/api/visual-presets/save':return self.respond(self.app.visual_presets.save(b))
            if path=='/api/visual-presets/delete':return self.respond(self.app.visual_presets.delete(b))
            if path=='/api/visuals/review':return self.respond(self.app.visuals.review(b))
            if path in ('/api/media/video-batch-preview','/api/media/video-batch-apply'):
                from video_batch import VideoBatch
                batch=VideoBatch(self.app)
                return self.respond(batch.apply(b) if path.endswith('-apply') else batch.preview(b))
            if path=='/api/media/video-review':
                from video_batch import VideoBatch
                return self.respond(VideoBatch(self.app).review(b))
            if path=='/api/media/control':return self.respond(self.app.media.control(b))
            if path=='/api/media/templates':return self.respond(self.app.media.save_template(b))
            if path=='/api/media/attach':return self.respond(self.app.media.attach(b))
            if path.startswith('/api/ops/'):
                return self.respond(self.app.ops.transact(path.removeprefix('/api/ops/'),b))
            if path in ('/api/supply-update/preview','/api/supply-update/apply'):
                from source_updates import SourceUpdates
                updater=SourceUpdates(store)
                return self.respond(updater.apply(b) if path.endswith('/apply') else updater.preview(b))
            if path in ('/api/import/preview','/api/import/apply'):
                from source_import import SourceImport
                importer=SourceImport(store,self.app.automation)
                if path.endswith('/apply'):return self.respond(importer.apply(b))
                preview=importer.preview(b);preview.pop('prepared',None);return self.respond(preview)
            if path=='/api/import':
                rows=b.get('products')
                if 'csv' in b:
                    rows=list(csv.DictReader(io.StringIO(str(b['csv']).lstrip('\ufeff'))))
                return self.respond(store.import_rows(rows))
            if path=='/api/demo':
                return self.respond(store.import_rows(json.loads((BASE/'examples'/'sample.json').read_text()),demo=True))
            if path=='/api/export': return self.respond(export_package(store,b.get('ids')),content_type='application/zip',filename='noon-content-drafts.zip')
            if path=='/api/noon/categories': return self.respond(Noon().categories())
            match=re.fullmatch(r'/api/products/([a-f0-9]{32})/(save|approve|image|images|translate|submit|refresh|offers|prices)',path)
            if not match: raise Problem('操作不存在',404)
            pid,action=match.groups(); revision=b.get('revision')
            if action=='save': return self.respond(store.update(pid,b.get('data',{}),revision))
            if action=='approve': return self.respond(store.approve(pid,revision))
            if action=='image':
                p=store.get(pid)
                if revision!=p['revision']: raise Problem('版本变化，请刷新后再上传',409)
                if len(p['images'])>=8: raise Problem('首版每个商品最多8张图片')
                image=normalize_image(store,b.get('base64',''),b.get('template','square'))
                return self.respond(store.update(pid,{},revision,{'images':p['images']+[image],'images_verified':False}))
            if action=='images':
                p=store.get(pid); images=p['images']
                target=next((x for x in images if x['id']==b.get('image_id')),None)
                if not target: raise Problem('图片不存在',404)
                if b.get('remove'): images=[x for x in images if x['id']!=target['id']]
                else:
                    url=str(b.get('public_url','')).strip()
                    parsed=urlsplit(url)
                    if url and (parsed.scheme!='https' or not parsed.hostname or parsed.username): raise Problem('成图地址须为公开HTTPS链接')
                    target['public_url']=url
                return self.respond(store.update(pid,{},revision,{'images':images,'images_verified':False}))
            return self.respond(self.app.queue(pid,action,revision),202)
        except Problem as e: self.respond({'error':str(e)},e.status)
        except Exception: self.respond({'error':'本地操作失败，请检查输入后重试；已保存的数据仍然保留'},500)

def start(root,port,ready_file=None):
    Path(root).mkdir(parents=True,exist_ok=True)
    lock=open(Path(root)/'.server.lock','a')
    try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError: raise SystemExit('此资料库已由另一个实例打开')
    Recovery(root,expected=[]).apply_pending()
    app=App(root); app.media.start(); app.automation.start(); app.visuals.start(); app.visual_checks.start(); app.source_inbox.start(); server=LocalHTTPServer(('127.0.0.1',port),Handler); server.app=app
    print(f'商品工作台已启动：http://127.0.0.1:{server.server_port}',flush=True)
    if ready_file:
        target=Path(ready_file); target.write_text(json.dumps({'url':f'http://127.0.0.1:{server.server_port}','pid':os.getpid()})); target.chmod(0o600)
    def stop(signum,frame):raise KeyboardInterrupt
    signal.signal(signal.SIGTERM,stop)
    app.collection_schedules.start();app.backup_schedules.start()
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close(); app.collection_schedules.close(); app.backup_schedules.close(); app.collection_executor.shutdown(wait=False,cancel_futures=True); app.source_inbox.close(); app.models.codex.close(); app.visual_checks.close(); app.visuals.close(); app.automation.close(); app.media.close(); app.executor.shutdown(wait=False,cancel_futures=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--port',type=int,default=8791); parser.add_argument('--data',default=str(BASE/'data'))
    parser.add_argument('--ready-file')
    args=parser.parse_args(); start(args.data,args.port,args.ready_file)
