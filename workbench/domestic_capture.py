"""Explicit local browser packages into the existing source candidate workflow."""
import json
import math
import re
from datetime import datetime
from urllib.parse import urlsplit,parse_qs,urlunsplit,urlencode
from core import Problem,clean,ident,now
from source_import import digest
from channel_adapters import _plain,_facts,public_url

SCHEMA_SQL='''CREATE TABLE IF NOT EXISTS domestic_capture_requests(request_id TEXT PRIMARY KEY,digest TEXT NOT NULL,result TEXT NOT NULL);'''
FORMAT='noon-domestic-capture-v1'
PROVIDERS=('1688','taobao','pinduoduo')
MAX_BYTES=8*1024*1024
SECRET_KEYS={'token','accesstoken','refreshtoken','apikey','secret','clientsecret','password','authorization','cookie','cookies','credentials','accesskey','privatekey','session','sessionid','headers','csrf','sign','signature'}
ITEM_KEYS={'product_id','sku','title','source_url','source_price','source_currency','stock','images','facts','supplier','brand','captured_at','method'}

def quality_diagnostics(row,previous=None,duplicate_row=None):
    """Read-only diagnoses; unknown observations are never inferred or repaired."""
    raw=row['raw'];issues=[]
    def issue(code,field,severity,message,action):
        issues.append({'code':code,'field':field,'severity':severity,'message':message,'action':action})
    unknown=[]
    for field,label,action in (
        ('source_sku','真实规格货号','根据商品页或供应商规格单人工校对货号并填写依据；缺货号不能创建商品'),
        ('source_price','网页售价','回到来源页核对单一规格售价；区间价格保持未知，不能当采购成本'),
        ('stock','可供数量','联系供应商或核对规格数量；有货提示不等于实际数量'),
        ('supplier','供应商','核对供应商名称后填写人工校对及依据'),
        ('facts','商品事实','核对材质、颜色、尺寸等规格事实后填写人工校对及依据'),
        ('images','公开图片引用','回到明确商品页检查公开图片引用；素材权利仍需独立核对')):
        if raw.get(field) in (None,'',[]):
            unknown.append(field)
            issue('missing_'+field,field,'blocker' if field=='source_sku' else 'warning',label+'未知',action)
    changed=[]
    if row['status']=='conflict':
        changed=[{'field':field,'before':previous.get(field),'after':raw.get(field)} for field in raw if previous.get(field)!=raw.get(field)]
        issue('history_conflict','identity','blocker','同一来源规格已有不同观察','到来源候选比较首个与最新快照，核对后处理冲突；不会覆盖现有商品')
    if row['status']=='duplicate':
        issue('duplicate_observation','identity','info','包内重复观察' if duplicate_row else '已有相同来源观察','跳过重复，不增加候选版本；'+(f'本包首次出现于第{duplicate_row}行' if duplicate_row else '历史候选与事实保持不变'))
    if row['capture']['correction']:
        issue('manual_correction','capture','info','包含有依据的人工校对','核对校对前后值及依据；原始网页观察独立保存')
    return {'issues':issues,'unknown_fields':unknown,'changed_fields':changed,'duplicate_of_row':duplicate_row,
            'source':{'provider_product_id':raw['external_product_id'],'url':raw['source_url'],'method':row['capture']['method'],'observed_sku':row['capture']['sku'],'effective_sku':raw['source_sku']},
            'requires_attention':any(i['severity'] in ('blocker','warning') for i in issues)}

def quality_summary(rows,warnings):
    by_code={};unknown={};methods={}
    for row in rows:
        quality=row['quality']
        for issue in quality['issues']:by_code[issue['code']]=by_code.get(issue['code'],0)+1
        for field in quality['unknown_fields']:unknown[field]=unknown.get(field,0)+1
        method=quality['source']['method'];methods[method]=methods.get(method,0)+1
    return {'total_rows':len(rows),'unique_products':len({r['raw']['external_product_id'] for r in rows}),
            'unique_identities':len({r['identity'] for r in rows}),
            'attention_rows':sum(r['quality']['requires_attention'] for r in rows),
            'blocked_rows':sum(any(i['severity']=='blocker' for i in r['quality']['issues']) for r in rows),
            'by_code':by_code,'unknown_fields':unknown,'methods':methods,'package_warnings':list(warnings)}

def secret_key(value):
    value=re.sub('[^a-z0-9]','',value.lower())
    return value in SECRET_KEYS or value.endswith('signature') or bool(re.search('token|cookie|auth|secret|session|password|credential|csrf',value))

def secret_fields(value,depth=0):
    if depth>20:raise Problem('采集包结构层数过多')
    if isinstance(value,dict):
        for key,child in value.items():
            if not isinstance(key,str) or secret_key(key):raise Problem('采集包不能包含登录凭据或会话字段')
            secret_fields(child,depth+1)
    elif isinstance(value,list):
        for child in value:secret_fields(child,depth+1)

def item_url(value,provider,product_id):
    value=public_url(value);p=urlsplit(value);host=p.hostname
    roots={'1688':('1688.com',),'taobao':('taobao.com','tmall.com'),'pinduoduo':('yangkeduo.com','pinduoduo.com')}[provider]
    if not any(host==root or host.endswith('.'+root) for root in roots):raise Problem('商品地址不属于所选国内平台')
    query=parse_qs(p.query,keep_blank_values=True)
    if any(secret_key(k) for k in query):raise Problem('商品地址不能包含凭据或会话参数')
    if provider=='1688':
        m=re.fullmatch(r'/offer/([0-9]{1,32})\.html',p.path)
        found=m.group(1) if m else None;canonical=urlunsplit(('https',host,p.path,'',''))
    elif provider=='taobao':
        if p.path not in ('/item.htm','/item.html'):raise Problem('仅接受淘宝或天猫明确商品详情地址')
        ids=query.get('id',[]);found=ids[0] if len(ids)==1 else None;canonical=urlunsplit(('https',host,p.path,urlencode({'id':found or ''}),''))
    else:
        if p.path not in ('/goods.html','/goods1.html','/goods2.html','/goods/detail'):raise Problem('仅接受拼多多明确商品详情地址')
        ids=query.get('goods_id',[]);found=ids[0] if len(ids)==1 else None;canonical=urlunsplit(('https',host,p.path,urlencode({'goods_id':found or ''}),''))
    if found!=product_id:raise Problem('商品地址中的商品编号与采集记录不一致')
    return canonical

def timestamp(value):
    try:
        if not isinstance(value,str) or len(value)>80:raise ValueError()
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        if parsed.tzinfo is None:raise ValueError()
        return parsed.isoformat()
    except ValueError:raise Problem('采集时间须为含时区的ISO时间')

def number(value,integer=False):
    if value in (None,''):return None
    if isinstance(value,bool) or not isinstance(value,(str,int,float)):raise Problem('采集价格或库存格式无效')
    try:n=float(value)
    except (ValueError,OverflowError):raise Problem('采集价格须是单一数字，区间不能自动作为价格')
    if not math.isfinite(n) or n<0 or n>100000000 or integer and (n!=int(n) or n>1000000):raise Problem('采集价格或库存超出范围')
    return int(n) if integer else n

class DomesticCapture:
    def __init__(self,app):
        self.app=app;self.store=app.store
        with self.store.connect() as c:c.executescript(SCHEMA_SQL)

    def state(self,page=0):
        if isinstance(page,bool) or not str(page).isdecimal() or int(page)>100000:raise Problem('采集历史页码无效')
        page=int(page)
        with self.store.connect() as c:
            accounts=[dict(r) for r in c.execute("SELECT id,provider,name,revision,enabled FROM source_channel_accounts WHERE provider IN ('1688','taobao','pinduoduo') ORDER BY name,id LIMIT 200")]
            total=c.execute("SELECT count(*) FROM source_collection_runs WHERE request_key LIKE 'domestic:%'").fetchone()[0]
            runs=[dict(r) for r in c.execute("SELECT id,provider,account_id,account_revision,status,collected,message,created_at FROM source_collection_runs WHERE request_key LIKE 'domestic:%' ORDER BY created_at DESC,id LIMIT 20 OFFSET ?",(page*20,))]
        return {'accounts':accounts,'runs':runs,'page':page,'pages':max(1,(total+19)//20),'total':total,'max_bytes':MAX_BYTES,'max_items':500,'format':FORMAT,'extension_url':'/api/domestic-capture/extension','local_only':True,'notice':'本机主动采集当前商品页可见资料，不绕过登录或验证码、不读取登录会话；真实国内网页兼容性尚未验证。缺规格货号的观察仅保留为待补候选，售价不是采购成本。'}

    def _prepare(self,body,c):
        if set(body)-{'account_id','account_revision','package','corrections','preview_token','confirmed','request_id'}:raise Problem('采集参数存在不支持字段，不能包含登录凭据')
        package=body.get('package')
        try:size=len(json.dumps(package,ensure_ascii=False,allow_nan=False).encode())
        except (ValueError,TypeError,RecursionError):raise Problem('采集包JSON格式无效')
        if size>MAX_BYTES:raise Problem('采集包最多8MB')
        secret_fields(package)
        if not isinstance(package,dict) or set(package)-{'format','provider','captured_at','items','warnings'} or package.get('format')!=FORMAT or package.get('provider') not in PROVIDERS:raise Problem('请使用国内网页采集扩展导出的完整JSON包')
        warnings=package.get('warnings',[])
        if not isinstance(warnings,list) or len(warnings)>100 or any(not isinstance(w,str) or len(w)>1000 for w in warnings):raise Problem('采集包提示格式无效')
        provider=package['provider'];captured=timestamp(package.get('captured_at'));items=package.get('items')
        if not isinstance(items,list) or not 1<=len(items)<=500:raise Problem('一次采集包须包含1至500条观察')
        if type(body.get('account_revision')) is not int or not isinstance(body.get('account_id'),str):raise Problem('请选择国内来源账号及当前版本')
        account=c.execute('SELECT * FROM source_channel_accounts WHERE id=?',(body['account_id'],)).fetchone()
        if not account or account['provider']!=provider or not account['enabled'] or account['revision']!=body['account_revision']:raise Problem('来源账号平台、启用状态或版本已变化，请重新选择',409)
        corrections=body.get('corrections',[])
        if not isinstance(corrections,list) or len(corrections)>500:raise Problem('人工校对最多500条')
        secret_fields(corrections);correction_map={}
        for correction in corrections:
            if not isinstance(correction,dict) or set(correction)-{'row','sku','supplier','facts','evidence'}:raise Problem('人工校对仅允许规格货号、供应商与事实')
            row=correction.get('row');evidence=correction.get('evidence')
            if type(row) is not int or not 1<=row<=len(items) or row in correction_map:raise Problem('人工校对行号不存在或重复')
            if not isinstance(evidence,str) or not evidence.strip() or len(evidence)>2000:raise Problem('请填写人工校对依据（最多2000字），不能将手填值冒充网页原值')
            for field,limit in (('sku',200),('supplier',1000),('facts',12000),('evidence',2000)):
                if field not in correction:continue
                value=correction[field]
                if not isinstance(value,str) or len(value)>limit or field=='sku' and (not value.strip() or any(ord(ch)<32 for ch in value)):raise Problem('人工校对文字或真实规格货号格式无效')
                if re.search(r'(?i)(?:cookie|authorization|auth|secret|password|credentials|(?:access[_ -]?|refresh[_ -]?)?token|api[_ -]?key|session[_ -]?(?:id|token)|csrf(?:[_ -]?token)?)\s*[:=]|bearer\s+[A-Za-z0-9_.-]{8,}',value):raise Problem('人工校对值与依据不能包含凭据或登录会话')
            if not any(field in correction for field in ('sku','supplier','facts')):raise Problem('请选择至少一个人工校对字段')
            correction_map[row]=correction
        rows=[];seen={};first_rows={}
        for index,item in enumerate(items):
            if not isinstance(item,dict) or set(item)-ITEM_KEYS:raise Problem('采集记录存在不支持字段')
            if len(json.dumps(item,ensure_ascii=False).encode())>128*1024:raise Problem('单条采集观察最多128KB')
            pid=item.get('product_id');sku=item.get('sku','')
            if not isinstance(pid,str) or not re.fullmatch('[0-9]{1,32}',pid) or not isinstance(sku,str) or len(sku)>200 or any(ord(ch)<32 for ch in sku):raise Problem('商品编号或真实规格货号格式无效')
            sku=sku.strip();url=item_url(item.get('source_url'),provider,pid)
            title=item.get('title')
            if not isinstance(title,str) or not title.strip() or len(title)>1000:raise Problem('采集记录缺少商品标题')
            for field in ('facts','supplier','brand'):
                if not isinstance(item.get(field,''),str) or len(item.get(field,''))>12000:raise Problem('采集文字字段格式无效')
            if item.get('source_currency')!='CNY':raise Problem('国内采集当前仅记录原网页人民币售价')
            method=item.get('method','visible-dom')
            if method not in ('visible-dom','json-ld','public-json'):raise Problem('采集观察方式无效')
            images=item.get('images',[])
            if not isinstance(images,list) or len(images)>50:raise Problem('单条观察最多50张公开图片引用')
            safe_images=[]
            for image in images:
                image=public_url(image)
                if any(secret_key(k) for k in parse_qs(urlsplit(image).query)):raise Problem('图片地址包含会话或凭据参数')
                if image not in safe_images:safe_images.append(image)
            raw={'external_id':pid+(':'+sku if sku else ':product-observation'),'external_product_id':pid,'title_zh':_plain(title,1000),'source_title':_plain(title,1000),'source_url':url,'source_sku':sku,'supplier':_plain(item.get('supplier',''),1000),'brand':_plain(item.get('brand',''),1000),'facts':_facts(item.get('facts','')),'stock':number(item.get('stock'),True),'cost_cny':None,'source_currency':'CNY','source_price':number(item.get('source_price')),'images':safe_images}
            original_raw=json.loads(json.dumps(raw,ensure_ascii=False));correction=correction_map.get(index+1);changes={}
            if correction:
                for field in ('sku','supplier','facts'):
                    if field not in correction:continue
                    raw_field='source_sku' if field=='sku' else field
                    value=_facts(correction[field]) if field=='facts' else _plain(correction[field],200 if field=='sku' else 1000)
                    if value!=raw[raw_field]:changes[field]={'before':raw[raw_field],'after':value}
                    raw[raw_field]=value
                sku=raw['source_sku'];raw['external_id']=pid+(':'+sku if sku else ':product-observation')
            normalized=clean({k:raw[k] for k in ('title_zh','source_url','source_sku','supplier','brand','facts','stock')})
            identity=digest([provider,account['id'],raw['external_id']])
            old=c.execute('SELECT * FROM source_collection_candidates WHERE identity=?',(identity,)).fetchone()
            status='blocked' if not sku else 'new';reason='缺少真实规格货号，不能自动创建商品' if not sku else '可录入来源候选，后续需再次确认商品导入'
            if identity in seen:
                if seen[identity]!=digest([raw,normalized]):raise Problem('包内同一来源身份存在不同事实，请拆分核对')
                status='duplicate';reason='包内相同来源身份重复，保留第一条'
            elif old:
                latest=json.loads(old['snapshots'])[-1]
                if latest['raw']==raw and latest['normalized']==normalized:status='duplicate';reason='已有相同来源观察，跳过且不修改历史'
                else:status='conflict';reason='已有同一来源身份的不同观察，保留原事实并标记冲突'
            seen[identity]=digest([raw,normalized])
            missing=[label for field,label in (('source_sku','规格货号'),('source_price','网页售价'),('stock','可供数量')) if raw.get(field) in (None,'')]
            rows.append({'row':index+1,'identity':identity,'status':status,'reason':reason,'raw':raw,'normalized':normalized,'missing':missing,'old_id':old['id'] if old else None,'old_revision':old['revision'] if old else None,'capture':{'captured_at':timestamp(item.get('captured_at',captured)),'method':method,'product_id':pid,'sku':original_raw['source_sku'],'source_url':url,'original_raw':original_raw,'correction':{'fields':changes,'evidence':correction['evidence'].strip(),'manual':True} if correction else None}})
            rows[-1]['quality']=quality_diagnostics(rows[-1],json.loads(old['snapshots'])[-1]['raw'] if old else None,first_rows.get(identity))
            first_rows.setdefault(identity,index+1)
        counts={status:sum(r['status']==status for r in rows) for status in ('new','blocked','conflict','duplicate')}
        return {'token':digest([account['id'],account['revision'],package,corrections,rows]),'provider':provider,'account_id':account['id'],'account_revision':account['revision'],'rows':rows,'counts':counts,'quality_summary':quality_summary(rows,warnings),'captured_at':captured}

    def preview(self,body):
        if not isinstance(body,dict):raise Problem('采集预检参数无效')
        with self.store.connect() as c:return self._prepare(body,c)

    def apply(self,body):
        if not isinstance(body,dict) or body.get('confirmed') is not True:raise Problem('请确认只录入已预检的来源候选，不直接创建或覆盖商品')
        key=body.get('request_id')
        if not isinstance(key,str) or not re.fullmatch('[A-Za-z0-9_-]{8,96}',key):raise Problem('采集录入请求编号无效')
        fingerprint=digest(body)
        with self.app.write_lock,self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE');old=c.execute('SELECT * FROM domestic_capture_requests WHERE request_id=?',(key,)).fetchone()
            if old:
                if old['digest']!=fingerprint:raise Problem('请求编号不能用于另一份采集观察',409)
                return {**json.loads(old['result']),'replayed':True}
            self.app.source_collection.ensure_recovery_clear();pre=self._prepare(body,c)
            if pre['token']!=body.get('preview_token'):raise Problem('采集观察、账号或候选事实已变化，请重新预检',409)
            rid=ident();stamp=now()
            c.execute("INSERT INTO source_collection_runs(id,request_key,digest,account_id,account_revision,provider,query,page_limit,status,pages,collected,message,created_at,updated_at) VALUES(?,?,?,?,?,?,?,1,'completed',1,?,?,?,?)",(rid,'domestic:'+key,fingerprint,pre['account_id'],pre['account_revision'],pre['provider'],'本机当前商品页JSON包',len(pre['rows']),'本机主动观察；未读取登录会话，未通过官方API，未直接建立商品',stamp,stamp))
            candidate_ids=[];created=[];conflicts=[];skipped=[];blocked=[]
            for row in pre['rows']:
                if row['status']=='duplicate':
                    skipped.append(row['row'])
                    if row['old_id']:candidate_ids.append(row['old_id'])
                    continue
                snapshot={'run_id':rid,'account_revision':pre['account_revision'],'captured_at':row['capture']['captured_at'],'capture':row['capture'],'raw':row['raw'],'normalized':row['normalized']}
                cid=row['old_id'] or ident()
                if row['old_id']:
                    saved=c.execute('SELECT snapshots FROM source_collection_candidates WHERE id=?',(cid,)).fetchone();snapshots=(json.loads(saved[0])[:1]+[snapshot])[-2:]
                    c.execute("UPDATE source_collection_candidates SET raw=?,snapshots=?,status='conflict',message=?,revision=revision+1,updated_at=? WHERE id=?",(json.dumps(row['raw'],ensure_ascii=False),json.dumps(snapshots,ensure_ascii=False),row['reason'],stamp,cid));conflicts.append(cid)
                else:
                    status='blocked' if row['status']=='blocked' else 'ready'
                    c.execute('INSERT INTO source_collection_candidates(id,identity,provider,account_id,external_id,run_id,raw,normalized,snapshots,status,message,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',(cid,row['identity'],pre['provider'],pre['account_id'],row['raw']['external_id'],rid,json.dumps(row['raw'],ensure_ascii=False),json.dumps(row['normalized'],ensure_ascii=False),json.dumps([snapshot],ensure_ascii=False),status,row['reason'],stamp,stamp));created.append(cid)
                    if status=='blocked':blocked.append(cid)
                candidate_ids.append(cid)
            result={'run_id':rid,'candidate_ids':list(dict.fromkeys(candidate_ids)),'created':created,'conflicts':conflicts,'blocked':blocked,'skipped':skipped,'replayed':False,'message':'来源观察已保存到候选；商品仍需单独预览与确认导入'}
            c.execute('INSERT INTO domestic_capture_requests VALUES(?,?,?)',(key,fingerprint,json.dumps(result,ensure_ascii=False)))
            self.store.event(c,None,'国内网页采集录入',json.dumps({'run_id':rid,'counts':pre['counts'],'account_id':pre['account_id'],'manual_corrections':[{'row':r['row'],'capture':r['capture']} for r in pre['rows'] if r['capture']['correction']]},ensure_ascii=False))
            return result
