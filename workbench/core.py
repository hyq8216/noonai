"""Persistent single-user product workflow; no implicit external calls."""
import base64
import csv
import hashlib
import io
import json
import math
import os
import re
import sqlite3
import time
import uuid
from platform_status import summarize
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

class Problem(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status

def now():
    return datetime.now(timezone.utc).isoformat()

def ident():
    return uuid.uuid4().hex

def number(value, label, integer=False):
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        raise Problem(f'{label}必须为非负数')
    try:
        result = float(value)
    except (ValueError, TypeError):
        raise Problem(f'{label}必须为非负数')
    if not math.isfinite(result) or result < 0 or (integer and result != int(result)):
        raise Problem(f'{label}必须为非负' + ('整数' if integer else '数'))
    return int(result) if integer else result

TEXT_FIELDS = ['title_zh', 'source_url', 'supplier', 'source_sku', 'brand', 'category',
               'facts', 'title_en', 'description_en', 'title_ar', 'description_ar',
               'rights_evidence', 'supply_checked_at', 'mode', 'note']
NUM_FIELDS = ['cost_cny', 'stock', 'domestic_shipping_cny', 'packing_cny', 'other_cny',
              'transfer_usd', 'fx', 'loss_rate', 'collection_rate', 'acquisition_cny']

def normalize_source_url(value):
    if not isinstance(value,str) or not value.strip():raise Problem('货源链接格式无效')
    try: parsed=urlsplit(value.strip())
    except ValueError:raise Problem('货源链接格式无效，请检查域名和括号')
    if parsed.scheme not in ('https','http') or not parsed.hostname or parsed.username:
        raise Problem('货源链接应为完整的 http 或 https 地址')
    try:port=parsed.port
    except ValueError:raise Problem('货源链接端口无效')
    is_1688=(parsed.hostname=='1688.com' or parsed.hostname.endswith('.1688.com')) and port in (None,80,443)
    offer=re.fullmatch(r'/offer/([0-9]+)\.html/?',parsed.path,re.IGNORECASE) if is_1688 else None
    if offer:
        # A numeric offer ID identifies the listing across mobile and tracking links.
        return f'https://detail.1688.com/offer/{offer.group(1)}.html'
    # Keep query parameters on other paths; they may identify a distinct product.
    return urlunsplit((parsed.scheme,parsed.netloc,parsed.path,parsed.query,''))

def clean(raw):
    out = {k: str(raw.get(k) or '').strip() for k in TEXT_FIELDS}
    if not out['title_zh']:
        raise Problem('请填写中文商品名称')
    if any(len(v) > 24000 for v in out.values()):
        raise Problem('单个文本字段过长，请缩减至24000字以内')
    if out['mode'] not in ('', 'NGS', 'LOCAL'):
        raise Problem('经营模式无效')
    if out['source_url']:out['source_url']=normalize_source_url(out['source_url'])
    for k in NUM_FIELDS:
        out[k] = number(raw.get(k), k, k == 'stock')
    for k in ('loss_rate', 'collection_rate'):
        if out[k] is not None and out[k] >= 1:
            raise Problem('损失比例和收款费率必须小于1，例如8%填写0.08')
    for k in ('content_verified', 'images_verified', 'category_verified'):
        out[k] = raw.get(k) is True
    from category_rules import clean_values
    out['attribute_values']=clean_values(raw.get('attribute_values',{}))
    return out

class Store:
    def __init__(self, root):
        self.catalog_session=ident()
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.assets = self.root / 'assets'
        self.assets.mkdir(exist_ok=True)
        self.db = self.root / 'workbench.sqlite3'
        with self.connect() as c:
            c.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS products (
              id TEXT PRIMARY KEY, source_key TEXT UNIQUE, data TEXT NOT NULL,
              revision INTEGER NOT NULL DEFAULT 1, approved_revision INTEGER,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS revisions (
              product_id TEXT NOT NULL, revision INTEGER NOT NULL, data TEXT NOT NULL, saved_at TEXT NOT NULL,
              PRIMARY KEY(product_id, revision));
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, product_id TEXT,
              action TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, product_id TEXT NOT NULL, kind TEXT NOT NULL,
              revision INTEGER NOT NULL, status TEXT NOT NULL, message TEXT NOT NULL,
              result TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS submit_reconciliations (
              job_id TEXT PRIMARY KEY, product_id TEXT NOT NULL, outcome TEXT NOT NULL,
              partner_sku TEXT NOT NULL, sku_parent TEXT NOT NULL DEFAULT '',
              evidence TEXT NOT NULL, checked_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runtime_state (
              key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_products_partner_sku ON products(json_extract(data,'$.partner_sku'));
            CREATE INDEX IF NOT EXISTS idx_products_source_sku ON products(json_extract(data,'$.source_sku'));
            CREATE INDEX IF NOT EXISTS idx_products_supply_checked_jd_id ON products(julianday(json_extract(data,'$.supply_checked_at')),id)
              WHERE json_extract(data,'$.supply_checked_at') IS NOT NULL;
            DROP INDEX IF EXISTS idx_products_supply_checked_jd;
            CREATE INDEX IF NOT EXISTS idx_jobs_status_updated ON jobs(status,updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_jobs_updated ON jobs(updated_at DESC);
            ''')
    def connect(self):
        c = sqlite3.connect(self.db, timeout=15)
        c.row_factory = sqlite3.Row
        return c
    def event(self, c, pid, action, detail):
        c.execute('INSERT INTO events(product_id,action,detail,created_at) VALUES(?,?,?,?)', (pid, action, detail, now()))
    def unpack(self, row):
        if row is None:
            raise Problem('商品不存在', 404)
        p = json.loads(row['data'])
        p.update({k: row[k] for k in ('id','revision','approved_revision','created_at','updated_at')})
        p['issues'] = issues(p)
        p['reviewed'] = p['approved_revision'] == p['revision'] and not p['issues']
        p['state'] = '已审核' if p['reviewed'] else ('待补充' if p['issues'] else '待审核')
        p['economics'] = economics(p)
        p['platform_summary'] = summarize(p.get('platform'),p['revision'])
        from offer_status import summarize as offer_summary
        p['offer_summary']=offer_summary(p.get('platform'),p['partner_sku'],p['revision'])
        from pricing_status import summarize as pricing_summary
        p['pricing_summary']=pricing_summary(p.get('platform'),p['partner_sku'],p['revision'])
        from transfer_status import summarize as transfer_summary
        p['transfer_summary']=transfer_summary(p.get('platform'),p['partner_sku'],p['revision'],p.get('transfer_usd'),p.get('mode'))
        return p
    def get(self, pid):
        with self.connect() as c:
            return self.unpack(c.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone())
    def list(self):
        with self.connect() as c:
            return [self.unpack(r) for r in c.execute('SELECT * FROM products ORDER BY created_at DESC, id')]
    def catalog_snapshot(self,known_token=None):
        # All supported product mutations append a product event in the same transaction.
        # Re-read time-dependent checks periodically without retransmitting the whole catalog.
        # Approval/submission always validate live.
        with self.connect() as c:
            c.execute('BEGIN')
            latest=c.execute('SELECT coalesce(max(id),0) FROM events').fetchone()[0]
            bucket=int(time.time()//15);token=f'{self.catalog_session}:{latest}:{bucket}'
            valid=False;last=0;prior_bucket=0;time_state=None
            if isinstance(known_token,str) and len(known_token)<200:
                parts=known_token.split(':')
                if (len(parts) in (3,7) and parts[0]==self.catalog_session and parts[1].isdigit() and
                        len(parts[1])<=19 and parts[2].isdigit() and len(parts[2])<=19):
                    last=int(parts[1]);prior_bucket=int(parts[2])
                    if len(parts)==7:
                        try:target_micros=int(parts[3])
                        except ValueError:target_micros=-1
                        phase,cursor_jd,cursor_id=parts[4:7]
                        cursor_valid=((not cursor_jd and not cursor_id) or
                            (cursor_jd and cursor_id and len(cursor_id)==32 and
                             all(ch in '0123456789abcdef' for ch in cursor_id)))
                        try:cursor_number=float(cursor_jd) if cursor_jd else 0.0
                        except ValueError:cursor_number=-1.0;cursor_valid=False
                        target_bucket=target_micros//15_000_000 if target_micros>=0 else -1
                        valid=(cursor_valid and math.isfinite(cursor_number) and cursor_number>=0 and phase in ('e','f') and
                            0<=last<=latest and 0<=prior_bucket<=target_bucket<=bucket and
                            target_micros<10**20 and bucket-target_bucket<=5760)
                        if valid:time_state=(target_micros,phase,cursor_jd,cursor_id)
                    else:
                        valid=0<=last<=latest and 0<=prior_bucket<=bucket and bucket-prior_bucket<=5760
            if valid:
                # A large import must not turn the next refresh into a full-catalog download.
                # Advance by event ID, including events without a product, so no mutation is skipped.
                changes=c.execute('SELECT id,product_id FROM events WHERE id>? ORDER BY id LIMIT 501',(last,)).fetchall()
                events_more=len(changes)>500
                if events_more:
                    changes=changes[:500]
                    last=changes[-1]['id']
                ids={r['product_id'] for r in changes if r['product_id'] is not None}
                if events_more:
                    if time_state:
                        page_token=f'{self.catalog_session}:{last}:{prior_bucket}:{time_state[0]}:{time_state[1]}:{time_state[2]}:{time_state[3]}'
                    else:page_token=f'{self.catalog_session}:{last}:{prior_bucket}'
                    ordered_ids=sorted(ids)
                    items=[self.unpack(r) for r in c.execute('SELECT * FROM products WHERE id IN ('+','.join('?' for _ in ordered_ids)+')',ordered_ids)] if ordered_ids else []
                    present={p['id'] for p in items}
                    return {'catalog_token':page_token,'catalog_has_more':True,'product_changes':items,'removed_product_ids':[i for i in ordered_ids if i not in present]}

                time_more=False
                final_bucket=bucket
                if time_state or prior_bucket!=bucket:
                    if time_state:
                        target_micros,phase,cursor_jd,cursor_id=time_state
                        current=datetime(1970,1,1,tzinfo=timezone.utc)+timedelta(microseconds=target_micros)
                    else:
                        current=datetime.now(timezone.utc)
                        delta=current-datetime(1970,1,1,tzinfo=timezone.utc)
                        target_micros=(delta.days*86400+delta.seconds)*1_000_000+delta.microseconds
                        phase='e';cursor_jd='';cursor_id=''
                    final_bucket=target_micros//15_000_000
                    before=datetime.fromtimestamp(prior_bucket*15,timezone.utc)
                    expression="julianday(json_extract(data,'$.supply_checked_at'))"
                    def read_time_candidates(which,jd,id_,limit):
                        if not limit:return [],True
                        if which=='e':
                            lower=(before-timedelta(days=1,seconds=1)).isoformat()
                            upper=(current-timedelta(days=1)+timedelta(seconds=1)).isoformat()
                        else:
                            lower=(before+timedelta(seconds=299)).isoformat()
                            upper=(current+timedelta(seconds=301)).isoformat()
                        after=f'AND ({expression},id)>(?,?)' if jd else ''
                        params=[lower,upper]
                        if jd:params.extend((float(jd),id_))
                        params.append(limit+1)
                        rows=c.execute(f"""SELECT id,json_extract(data,'$.supply_checked_at') AS checked_at,{expression} AS checked_jd
                          FROM products WHERE json_extract(data,'$.supply_checked_at') IS NOT NULL
                            AND {expression} BETWEEN julianday(?) AND julianday(?) {after}
                          ORDER BY {expression},id LIMIT ?""",params).fetchall()
                        return rows[:limit],len(rows)>limit
                    def include_changed(rows):
                        for row in rows:
                            if supply_check_valid_at(row['checked_at'],before)!=supply_check_valid_at(row['checked_at'],current):ids.add(row['id'])
                    for which in (['e','f'] if phase=='e' else ['f']):
                        remaining=500-len(ids)
                        if remaining<=0:
                            time_more=True;phase=which
                            break
                        rows,more=read_time_candidates(which,cursor_jd if which==phase else '',cursor_id if which==phase else '',remaining)
                        include_changed(rows)
                        if more:
                            time_more=True;phase=which
                            if rows:cursor_jd=format(rows[-1]['checked_jd'],'.17g');cursor_id=rows[-1]['id']
                            break
                        cursor_jd='';cursor_id=''
                        if which=='e':phase='f'
                    if time_more:
                        page_token=f'{self.catalog_session}:{latest}:{prior_bucket}:{target_micros}:{phase}:{cursor_jd}:{cursor_id}'
                    else:page_token=f'{self.catalog_session}:{latest}:{final_bucket}'
                else:
                    page_token=token
                if not ids and not time_more:return {'catalog_token':page_token,'catalog_unchanged':True}
                if len(ids)<=500:
                    ids=sorted(ids)
                    if ids:
                        placeholders=','.join('?' for _ in ids)
                        items=[self.unpack(r) for r in c.execute(f'SELECT * FROM products WHERE id IN ({placeholders})',ids)]
                    else:items=[]
                    present={p['id'] for p in items}
                    return {'catalog_token':page_token,'catalog_has_more':time_more,'product_changes':items,'removed_product_ids':[i for i in ids if i not in present]}
            return {'catalog_token':token,'products':[self.unpack(r) for r in c.execute('SELECT * FROM products ORDER BY created_at DESC,id')]}
    def import_rows(self, rows, demo=False, connection=None):
        if connection is None:
            with self.connect() as c:
                c.execute('BEGIN IMMEDIATE');return self.import_rows(rows,demo,connection=c)
        if not isinstance(rows, list) or not rows or len(rows) > 500:
            raise Problem('每批导入1至500条商品记录')
        prepared = []
        for i, raw in enumerate(rows):
            if not isinstance(raw, dict):
                raise Problem(f'第{i+1}行不是商品对象')
            try:
                p = clean(raw)
            except Problem as e:
                raise Problem(f'第{i+1}行：{e}')
            # Import never trusts supplied approval flags.
            p['source_snapshot'] = {k: p[k] for k in ('title_zh','source_url','source_sku','supplier','facts','cost_cny','stock')}
            p['source_snapshot']['captured_at'] = now()
            p.update(content_verified=False, images_verified=False, category_verified=False,
                     images=[], content_notes=[], demo=demo, platform=None)
            key = p['source_url'] + '|' + p['source_sku'] if p['source_url'] else ''
            if demo:
                key = 'demo|' + p['title_zh']
            prepared.append((p, key or None))
        result = {'created': [], 'duplicates': []}
        c=connection
        for p, key in prepared:
            existing = c.execute('SELECT id FROM products WHERE source_key=?', (key,)).fetchone() if key else None
            if existing:
                result['duplicates'].append(existing['id'])
                continue
            pid = ident()
            p['partner_sku'] = 'SA-' + pid[:14].upper()
            ts = now()
            c.execute('INSERT INTO products(id,source_key,data,created_at,updated_at) VALUES(?,?,?,?,?)',
                      (pid, key, json.dumps(p, ensure_ascii=False), ts, ts))
            c.execute('INSERT INTO revisions VALUES(?,?,?,?)',(pid,1,json.dumps(p,ensure_ascii=False),ts))
            self.event(c, pid, '导入', '示例记录（不可提交）' if demo else '保存货源资料')
            result['created'].append(pid)
        return result
    def update(self, pid, raw, expected_revision, internal=None, connection=None):
        if connection is None:
            with self.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                return self.update(pid,raw,expected_revision,internal,connection=c)
        c=connection
        p = self.unpack(c.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone())
        if p['revision'] != expected_revision:
            raise Problem('商品已在其他操作中更新，请刷新后再保存', 409)
        if c.execute("SELECT 1 FROM jobs WHERE product_id=? AND status IN ('queued','running') AND kind='submit'", (pid,)).fetchone():
            raise Problem('商品正在提交，请等待结果后再编辑', 409)
        data = clean({**p, **raw})
        data['category_contract']=p.get('category_contract')
        # Actual edits invalidate confirmations, even if the caller sends stale booleans.
        fact_keys = ['title_zh','facts','source_url','source_sku','brand','title_en','title_ar','description_en','description_ar']
        content_changed=any(data[k] != p.get(k) for k in fact_keys)
        data['content_notes']=[] if content_changed else p.get('content_notes',[])
        if content_changed:
            data['content_verified'] = False
        identity_keys = ('title_zh','facts','source_sku','source_url','brand')
        if any(data[k] != p.get(k) for k in identity_keys) or data['attribute_values'] != p.get('attribute_values',{}):
            data['images_verified'] = False
        if data['category'] != p.get('category') or data['attribute_values'] != p.get('attribute_values',{}):
            data['category_verified'] = False
        for k in ('partner_sku','images','demo','platform','source_snapshot'):
            data[k] = p.get(k)
        if 'image_urls' in raw:
            urls = raw['image_urls']
            if not isinstance(urls, dict) or any(k not in {im['id'] for im in data['images']} for k in urls):
                raise Problem('图片地址记录无效')
            data['images'] = [dict(im) for im in data['images']]
            for im in data['images']:
                if im['id'] not in urls: continue
                url = str(urls[im['id']]).strip()
                parsed = urlsplit(url)
                if url and (parsed.scheme != 'https' or not parsed.hostname or parsed.username):
                    raise Problem('成图地址须为公开HTTPS链接')
                if im.get('public_url') != url: data['images_verified'] = False
                im['public_url'] = url
        if internal:
            data.update(internal)
        key = data['source_url'] + '|' + data['source_sku'] if data['source_url'] else None
        if data['demo']:
            key = 'demo|' + pid
        try:
            c.execute('UPDATE products SET source_key=?,data=?,revision=revision+1,approved_revision=NULL,updated_at=? WHERE id=?',
                      (key,json.dumps(data,ensure_ascii=False),now(),pid))
        except sqlite3.IntegrityError:
            raise Problem('该货源链接与供应商规格已存在，请保留一份商品档案',409)
        c.execute('INSERT INTO revisions VALUES(?,?,?,?)',(pid,p['revision']+1,json.dumps(data,ensure_ascii=False),now()))
        self.event(c,pid,'保存','资料更新，原审核已失效')
        if data['source_sku']!=p.get('source_sku'):self.event(c,pid,'规格标识更新','货源规格货号已变化，需重新匹配待归属素材')
        return self.unpack(c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone())
    def approve(self, pid, revision, connection=None):
        if connection is None:
            with self.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                return self.approve(pid,revision,connection=c)
        c=connection
        p = self.unpack(c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone())
        if revision != p['revision']:
            raise Problem('版本已变化，请重新核对',409)
        if p['issues']:
            raise Problem('尚不能审核：' + '；'.join(p['issues']))
        c.execute('UPDATE products SET approved_revision=revision WHERE id=?',(pid,))
        self.event(c,pid,'审核',f'确认内容版本 {revision}，尚未提交平台')
        return self.unpack(c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone())
    def add_job(self, pid, kind, revision):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            p = self.unpack(c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone())
            if p['revision'] != revision:
                raise Problem('商品版本已变化，请刷新',409)
            if kind == 'translate':
                try: limit = max(0, min(1000, int(os.environ.get('TEXT_DAILY_LIMIT', '50'))))
                except ValueError: raise Problem('TEXT_DAILY_LIMIT 配置无效', 409)
                used = c.execute("SELECT count(*) FROM jobs WHERE kind='translate' AND created_at>=?", (now()[:10],)).fetchone()[0]
                if used >= limit: raise Problem(f'今日翻译任务已达到 {limit} 项上限（UTC日），请明天继续或调整本地配置', 409)
            if c.execute("SELECT 1 FROM jobs WHERE product_id=? AND status IN ('queued','running')", (pid,)).fetchone():
                raise Problem('此商品已有任务在处理',409)
            if kind == 'submit':
                unresolved=c.execute("""SELECT 1 FROM jobs j WHERE j.product_id=? AND j.kind='submit'
                    AND j.status IN ('uncertain','needs_attention','interrupted')
                    AND NOT EXISTS (SELECT 1 FROM submit_reconciliations r WHERE r.job_id=j.id)""",(pid,)).fetchone()
                if unresolved:
                    raise Problem('此商品此前的 noon 提交结果待核对；修改商品版本也不能解除拦截，请先在任务记录中核对回执',409)
                if (p.get('platform') or {}).get('submitted_revision')==revision:
                    raise Problem('当前版本已有 Noon 提交回执，本次未重复发送',409)
            jid=ident(); ts=now()
            c.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',(jid,pid,kind,revision,'queued','等待处理',None,ts,ts))
        return jid
    def job_result(self, jid, status, message, result=None):
        with self.connect() as c:
            c.execute('UPDATE jobs SET status=?,message=?,result=?,updated_at=? WHERE id=?',
                      (status,message,json.dumps(result,ensure_ascii=False) if result is not None else None,now(),jid))
    def reconcile_submit(self, job_id, outcome, partner_sku, evidence, sku_parent=''):
        if outcome not in ('found','absent'):raise Problem('核对结果无效')
        if not isinstance(evidence,str) or len(evidence.strip())<8 or len(evidence)>500:
            raise Problem('请记录至少8个字的核对依据，最多500字')
        if not isinstance(partner_sku,str) or not partner_sku.strip() or len(partner_sku)>100:
            raise Problem('请输入并核对店铺 SKU')
        if not isinstance(sku_parent,str):raise Problem('Noon 商品编号格式无效')
        if outcome=='found' and (not isinstance(sku_parent,str) or not sku_parent.strip() or len(sku_parent)>160):
            raise Problem('已找到刊登时必须填写 Noon 商品编号')
        if outcome=='absent' and sku_parent:
            raise Problem('未找到刊登时不能填写 Noon 商品编号')
        checked=now()
        partner_sku=partner_sku.strip();evidence=evidence.strip();sku_parent=sku_parent.strip()
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            job=c.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
            if not job or job['kind']!='submit':raise Problem('提交任务不存在',404)
            existing=c.execute('SELECT * FROM submit_reconciliations WHERE job_id=?',(job_id,)).fetchone()
            if existing:
                if (existing['outcome'],existing['partner_sku'],existing['sku_parent'],existing['evidence'])==(outcome,partner_sku,sku_parent,evidence):
                    return {'job_id':job_id,'outcome':outcome,'partner_sku':partner_sku,'sku_parent':sku_parent,
                            'checked_at':existing['checked_at'],'replayed':True}
                raise Problem('此提交任务已有人工核对记录，不允许覆盖；请检查任务历史',409)
            if job['status'] not in ('uncertain','needs_attention','interrupted'):
                raise Problem('此任务当前无需人工对账',409)
            product=c.execute('SELECT data FROM products WHERE id=?',(job['product_id'],)).fetchone()
            if not product:raise Problem('商品不存在',404)
            data=json.loads(product['data']);sku=data.get('partner_sku','')
            if partner_sku!=sku:raise Problem('店铺 SKU 不匹配，请核对商品后重试',409)
            platform=data.get('platform') or {}
            if outcome=='absent' and (platform.get('sku_parent') or platform.get('submitted_revision')==job['revision']):
                raise Problem('本地已有平台提交回执，不能登记为“未找到”',409)
            if job['status']=='needs_attention' and outcome=='absent':
                raise Problem('平台已返回需处理结果，不能登记为“未找到”；请按已找到刊登记录',409)
            c.execute('INSERT INTO submit_reconciliations VALUES(?,?,?,?,?,?,?)',
                      (job_id,job['product_id'],outcome,sku,sku_parent,evidence,checked))
            if outcome=='found':
                if platform.get('sku_parent') and platform['sku_parent']!=sku_parent:
                    raise Problem('已保存的平台商品编号不同，请先核对冲突记录',409)
                platform.update({'sku_parent':sku_parent,'submitted_revision':job['revision'],
                    'checked_at':checked,'live_verified':False,'manual_reconciliation':{
                        'job_id':job_id,'outcome':'found','evidence':evidence,'checked_at':checked}})
                c.execute('UPDATE products SET data=? WHERE id=?',(json.dumps({**data,'platform':platform},ensure_ascii=False),job['product_id']))
            self.event(c,job['product_id'],'提交结果人工对账',
                f"{sku} · {'已找到并记录 Noon 商品 '+sku_parent if outcome=='found' else '已确认未找到刊登，可由操作员重新安排提交'} · 依据：{evidence}")
        return {'job_id':job_id,'outcome':outcome,'partner_sku':partner_sku,'sku_parent':sku_parent,'checked_at':checked,'replayed':False}
    def recover_jobs(self):
        with self.connect() as c:
            ts=now()
            # A queued job has not crossed App.run's atomic queued->running
            # claim, so it cannot have entered an external call. Keep this
            # distinct from running work whose request may already have left.
            # The visual-check worker has its own durable phase contract and
            # restarts jobs whose saved phase proves dispatch never occurred.
            c.execute("UPDATE jobs SET status='failed',message='服务重启时任务仍在本地队列，尚未开始外部调用；可重新安排。',updated_at=? WHERE status='queued' AND kind!='visual-check'",(ts,))
            c.execute("UPDATE jobs SET status='interrupted',message='服务重启时任务已开始执行，外部请求结果可能不确定；提交类任务请先在平台核对，避免重复操作。',updated_at=? WHERE status='running'",(ts,))
    def history(self,include_detail=True):
        with self.connect() as c:
            active_jobs=c.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
            jobs=[dict(r) for r in c.execute('SELECT id,product_id,kind,revision,status,message,created_at,updated_at FROM jobs ORDER BY created_at DESC LIMIT 100')] if include_detail else []
            events=[dict(r) for r in c.execute('SELECT * FROM events ORDER BY id DESC LIMIT 150')] if include_detail else []
            return {'jobs':jobs,'events':events,'active_jobs':active_jobs}
    def history_page(self,stream='jobs',page=0,group='all',job_kind='all'):
        if type(page) is not int or not 0<=page<=100000:raise Problem('记录页码无效')
        if stream not in ('jobs','events'):raise Problem('记录类型无效')
        if stream=='events':
            with self.connect() as c:
                c.execute('BEGIN')
                total=c.execute('SELECT count(*) FROM events').fetchone()[0]
                pages=max(1,(total+49)//50);page=min(page,pages-1)
                rows=[dict(r) for r in c.execute('SELECT * FROM events ORDER BY id DESC LIMIT 50 OFFSET ?',(page*50,))]
            return {'events':rows,'total':total,'page':page,'pages':pages,'page_size':50}
        groups={'all':'1','active':"j.status IN ('queued','running','waiting','waiting_image','paused')",
                'attention':"j.status IN ('failed','interrupted','needs_attention','uncertain','blocked')",
                'done':"j.status='done'",'cancelled':"j.status='cancelled'"}
        kinds=('all','translate','submit','refresh','offers','prices','image-host','visual-check')
        if group not in groups or job_kind not in kinds:raise Problem('任务筛选无效')
        where=groups[group];params=[]
        if job_kind!='all':where+=' AND j.kind=?';params.append(job_kind)
        with self.connect() as c:
            c.execute('BEGIN')
            total=c.execute('SELECT count(*) FROM jobs j WHERE '+where,params).fetchone()[0]
            pages=max(1,(total+49)//50);page=min(page,pages-1)
            jobs=[dict(r) for r in c.execute('''SELECT j.id,j.product_id,j.kind,j.revision,j.status,j.message,j.created_at,j.updated_at,
                                            r.outcome AS reconciliation_outcome,r.evidence AS reconciliation_evidence,
                                            r.checked_at AS reconciled_at,r.sku_parent AS reconciled_sku_parent
                                            FROM jobs j LEFT JOIN submit_reconciliations r ON r.job_id=j.id WHERE '''+where+
                                            ' ORDER BY j.updated_at DESC,j.rowid DESC LIMIT 50 OFFSET ?',params+[page*50])]
        return {'jobs':jobs,'total':total,'page':page,'pages':pages,'group':group,'kind':job_kind,'page_size':50}
    def record_offer(self,pid,partner_sku,result,local_revision):
        from offer_status import validate
        validate(result,partner_sku)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT data FROM products WHERE id=?',(pid,)).fetchone()
            if not row:raise Problem('商品不存在',404)
            data=json.loads(row['data'])
            if data['partner_sku']!=partner_sku:raise Problem('商品SKU已变化，未保存报价',409)
            data['platform']={**(data.get('platform') or {}),'offer_readback':{'response':result,'checked_at':now(),'local_revision':local_revision}}
            c.execute('UPDATE products SET data=? WHERE id=?',(json.dumps(data,ensure_ascii=False),pid))
            self.event(c,pid,'报价回查','已保存平台报价、库存及前台可见反馈；不代表已验证下单')
    def record_pricing(self,pid,partner_sku,result,local_revision):
        from pricing_status import validate
        item=validate(result,partner_sku)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT data FROM products WHERE id=?',(pid,)).fetchone()
            if not row:raise Problem('商品不存在',404)
            data=json.loads(row['data'])
            if data['partner_sku']!=partner_sku or data.get('mode')!='LOCAL':raise Problem('商品SKU或经营模式已变化，未保存售价',409)
            data['platform']={**(data.get('platform') or {}),'pricing_readback':{'response':result,'checked_at':now(),'local_revision':local_revision}}
            c.execute('UPDATE products SET data=? WHERE id=?',(json.dumps(data,ensure_ascii=False),pid))
            self.event(c,pid,'本地售价回查','读取沙特本地售价：'+str(item['status']['status_code'])+'；未修改平台价格')
    def record_transfer_price(self,pid,partner_sku,item,local_revision):
        from transfer_status import validate
        validate(item,partner_sku)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT data FROM products WHERE id=?',(pid,)).fetchone()
            if not row:raise Problem('商品不存在',404)
            data=json.loads(row['data'])
            if data['partner_sku']!=partner_sku or data.get('mode')!='NGS':raise Problem('商品SKU或经营模式已变化，未保存转移价',409)
            data['platform']={**(data.get('platform') or {}),'transfer_price_readback':{'item':item,'checked_at':now(),'local_revision':local_revision}}
            c.execute('UPDATE products SET data=? WHERE id=?',(json.dumps(data,ensure_ascii=False),pid))
            self.event(c,pid,'NGS转移价回查','读取美元转移价：'+str(item['status']['status_code'])+'；未修改平台报价')
    def record_platform(self, pid, result, expected_parent=None):
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT data FROM products WHERE id=?',(pid,)).fetchone()
            if not row: raise Problem('商品不存在',404)
            data=json.loads(row['data'])
            if expected_parent is not None and (data.get('platform') or {}).get('sku_parent')!=expected_parent:raise Problem('平台商品编号已变化，本次回查未覆盖新记录',409)
            # Content replacement may drop the old parent, but a separate dated pricing
            # observation must survive it for audit. Other content fields are replaced.
            readings={k:v for k,v in (data.get('platform') or {}).items() if k in ('pricing_readback','transfer_price_readback')}
            data['platform']={**result}
            for key,value in readings.items():data['platform'].setdefault(key,value)
            c.execute('UPDATE products SET data=? WHERE id=?',(json.dumps(data,ensure_ascii=False),pid))
            self.event(c,pid,'平台反馈','已保存平台返回结果；实际可售仍需价格和库存验证')

def issues(p):
    result=[]
    for key,label in [('source_url','货源链接'),('supplier','供应商'),('facts','规格事实'),('brand','品牌字段'),
                      ('category','noon类目代码'),('title_en','英文标题'),('description_en','英文描述'),
                      ('title_ar','阿文标题'),('description_ar','阿文描述'),('rights_evidence','图片使用依据'),('mode','经营模式')]:
        if not p.get(key): result.append('缺少'+label)
    if p.get('title_en') and not re.search('[A-Za-z]',p['title_en']): result.append('英文标题未包含英文')
    if p.get('title_ar') and not re.search('[\u0600-\u06ff]',p['title_ar']): result.append('阿文标题未包含阿文')
    if p.get('cost_cny') is None: result.append('采购成本待确认')
    if not p.get('stock'): result.append('可供数量不足或未确认')
    try:
        ts=datetime.fromisoformat(p.get('supply_checked_at','').replace('Z','+00:00'))
        if ts.tzinfo is None: raise ValueError()
        age=(datetime.now(timezone.utc)-ts).total_seconds()
        if age < -300 or age > 86400: result.append('供货记录已过期，请重新核对（本地24小时规则）')
    except (ValueError,TypeError): result.append('尚未核对供货时间')
    if not p.get('images'): result.append('尚未上传商品照片')
    for k,label in [('content_verified','双语事实'),('images_verified','图片与实物'),('category_verified','类目属性')]:
        if not p.get(k): result.append(label+'尚未人工核对')
    if p.get('demo'): result.append('示例商品不能进入发布审核')
    from category_rules import product_issues
    result.extend(product_issues(p))
    return result

def supply_check_valid_at(value,moment):
    try:
        checked=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if checked.tzinfo is None:return False
        age=(moment-checked).total_seconds()
        return -300<=age<=86400
    except (ValueError,TypeError,OverflowError):return False

def economics(p):
    if p.get('mode') != 'NGS': return None
    fields=['cost_cny','domestic_shipping_cny','packing_cny','other_cny','transfer_usd','fx','loss_rate','collection_rate','acquisition_cny']
    if any(p.get(k) is None for k in fields) or p['fx'] <= 0: return None
    retained=p['transfer_usd']*p['fx']*(1-p['loss_rate'])
    contribution=retained*(1-p['collection_rate'])-sum(p[k] for k in ['cost_cny','domestic_shipping_cny','packing_cny','other_cny','acquisition_cny'])
    return {'retained_cny':round(retained,2),'contribution_cny':round(contribution,2),'margin':round(contribution/retained,4) if retained else None}

def payload(p):
    from category_rules import attributes
    return {'skus':[{'partner_sku':p['partner_sku']}], 'brand':p['brand'],'category':p['category'],
            'images':[{'url':i['public_url'],'sort':n+1} for n,i in enumerate(p['images']) if i.get('public_url')],
            'attributes':attributes(p)}

def normalize_image(store, encoded, template):
    from PIL import Image, ImageOps, ImageCms, UnidentifiedImageError
    Image.MAX_IMAGE_PIXELS=24_000_000
    if template not in ('square','portrait'): raise Problem('图片模板不存在')
    try: raw=base64.b64decode(encoded,validate=True)
    except Exception: raise Problem('图片编码无效')
    if not raw or len(raw)>10*1024*1024: raise Problem('请使用10MB以内的图片')
    try:
        with Image.open(io.BytesIO(raw)) as original:
            if original.format not in ('JPEG','PNG','WEBP'): raise Problem('支持JPG、PNG和WebP图片')
            w,h=original.size
            if w*h>24_000_000 or min(w,h)<100: raise Problem('图片尺寸不合适，最短边至少100像素且总像素不超过2400万')
            ext={'JPEG':'jpg','PNG':'png','WEBP':'webp'}[original.format]
            oriented=ImageOps.exif_transpose(original)
            srgb=ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB'))
            if original.info.get('icc_profile'):
                try:
                    alpha=oriented.getchannel('A') if 'A' in oriented.getbands() else None
                    colour=ImageCms.profileToProfile(oriented,ImageCms.ImageCmsProfile(io.BytesIO(original.info['icc_profile'])),srgb,outputMode='RGB')
                    src=colour.convert('RGBA')
                    if alpha is not None: src.putalpha(alpha)
                except Exception: raise Problem('原图颜色配置无法转换，请提供标准sRGB照片')
            else:
                src=oriented.convert('RGBA')
            size=(1600,1600) if template=='square' else (1600,2000)
            # No AI alterations: fit without stretching or upscaling; insufficient source resolution is flagged.
            src.thumbnail((int(size[0]*.88),int(size[1]*.84)),Image.Resampling.LANCZOS)
            dst=Image.new('RGB',size,'white'); dst.paste(src,((size[0]-src.width)//2,(size[1]-src.height)//2),src)
            aid=ident(); source=f'{aid}-source.{ext}'; name=f'{aid}.jpg'
            (store.assets/source).write_bytes(raw)
            dst.save(store.assets/name,'JPEG',quality=94,dpi=(72,72),optimize=True,icc_profile=srgb.tobytes())
    except (UnidentifiedImageError,OSError,Image.DecompressionBombError,Image.DecompressionBombWarning):
        raise Problem('无法读取这张图片，请换用正常商品照片')
    return {'id':aid,'source':source,'file':name,'source_size':[w,h],'size':list(size),'template':template,
            'public_url':'','sha256':hashlib.sha256(raw).hexdigest(),
            'warning': '原图分辨率不足，请补充高清照片' if w<660 or min(w,h)<600 else '仅统一画布与格式，原背景与商品内容保留；请人工检查白底、中文和水印'}
