"""Preview supplier catalog columns and import only explicitly confirmed valid rows."""
import csv,hashlib,io,json
from core import Problem,clean,TEXT_FIELDS,NUM_FIELDS,source_identity_keys
LABELS={'title_zh':'商品名称','source_url':'货源链接','source_sku':'规格货号 / SKU','supplier':'供应商','facts':'规格事实','brand':'品牌','category':'类目编码','cost_cny':'采购成本（人民币元）','stock':'库存数量','title_en':'英文标题','description_en':'英文描述','title_ar':'阿文标题','description_ar':'阿文描述','rights_evidence':'素材使用依据','note':'备注','supply_checked_at':'供货核对时间','mode':'经营模式（NGS / LOCAL）','domestic_shipping_cny':'国内运费（人民币元）','packing_cny':'包装费（人民币元）','other_cny':'其他成本（人民币元）','transfer_usd':'转移价（美元）','fx':'美元兑人民币汇率','loss_rate':'损失比例（0至1）','collection_rate':'收款费率（0至1）','acquisition_cny':'获客成本（人民币元）','attribute_values':'类目属性（JSON）'}
ALIASES={'title_zh':['商品名称','商品标题','产品名称','中文标题'],'source_url':['货源链接','商品链接','1688链接'],'source_sku':['规格货号','规格SKU','SKU','货号'],'supplier':['供应商','供应商名称'],'facts':['规格事实','规格说明','商品规格'],'brand':['品牌'],'category':['类目编码'],'cost_cny':['采购成本（人民币元）','采购成本(人民币元)','采购价（元）','采购价(元)'],'stock':['库存数量','库存'],'note':['备注'],'rights_evidence':['素材使用依据']}
FIELDS=TEXT_FIELDS+NUM_FIELDS+['attribute_values']
FACT_HEADERS={'颜色','颜色分类','材质','尺寸','规格尺寸','容量','型号','款式','面料','成分','电压','功率'}
def digest(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def norm(value):return ''.join(str(value).lower().split()).replace('_','')
NORMALIZED_FACT_HEADERS={norm(header) for header in FACT_HEADERS}

def parse(b):
    if ('csv' in b)==('products' in b):raise Problem('请选择一种CSV或JSON商品资料')
    if 'csv' in b:
        text=b['csv']
        if not isinstance(text,str):raise Problem('CSV资料需为文本且在4MB以内')
        try:size=len(text.encode('utf-8'))
        except UnicodeEncodeError:raise Problem('CSV含有无效Unicode字符，请另存为UTF-8 CSV')
        if size>4*1024*1024:raise Problem('CSV资料需在4MB以内')
        reader=csv.reader(io.StringIO(text.lstrip('\ufeff')),strict=True)
        try:
            columns=next(reader);rows=[]
            for n,row in enumerate(reader,1):
                if any(v.strip() for v in row):rows.append((n,row))
                if len(rows)>500:raise Problem('每批最多500条商品资料，请拆分文件')
        except (csv.Error,StopIteration):raise Problem('CSV格式无效或缺少表头，请另存为UTF-8 CSV')
        columns=[c.strip() for c in columns]
    else:
        source=b['products']
        if not isinstance(source,list) or not 1<=len(source)<=500 or any(not isinstance(r,dict) for r in source):raise Problem('JSON需包含1至500个商品对象')
        try:size=len(json.dumps(source,ensure_ascii=False).encode('utf-8'))
        except UnicodeEncodeError:raise Problem('JSON商品资料含有无效Unicode字符，请修正后重试')
        if size>4*1024*1024:raise Problem('JSON资料需在4MB以内')
        columns=list(dict.fromkeys(k for row in source for k in row))
        rows=[(i+1,[row.get(k) for k in columns]) for i,row in enumerate(source)]
    if not columns or len(columns)>80 or any(not isinstance(c,str) or len(c)>200 for c in columns):raise Problem('表头需为1至80列，每列名称不超过200字')
    if not rows:raise Problem('文件内没有商品数据')
    return columns,rows

class SourceImport:
    fields=FIELDS
    aliases=ALIASES
    supports_fact_columns=True
    def __init__(self,store,automation=None):self.store=store;self.automation=automation
    def processing(self,b):
        raw=b.get('processing')
        if raw is None:return None
        if not isinstance(raw,dict) or set(raw)-{'translate','review'} or any(type(v) is not bool for v in raw.values()):raise Problem('导入后处理配置无效')
        if not any(raw.values()):return None
        if self.automation is None:raise Problem('导入后流程服务不可用，请关闭自动处理后导入',409)
        return {'translate':raw.get('translate',False),'review':raw.get('review',False),'missing_only':True,'image_template':'','submit':False}
    def prepare(self,b):
        columns,rows=parse(b);mapping=b.get('mapping')
        if mapping is None:
            mapping={}
            for field in self.fields:
                names={norm(field),*(norm(a) for a in self.aliases.get(field,[]))};matches=[i for i,c in enumerate(columns) if norm(c) in names]
                if len(matches)==1:mapping[field]=matches[0]
        if not isinstance(mapping,dict) or any(k not in self.fields or type(v) is not int or not 0<=v<len(columns) for k,v in mapping.items()):raise Problem('列对应关系无效，请重新选择')
        if len(set(mapping.values()))!=len(mapping):raise Problem('同一来源列不能同时对应多个商品字段')
        fact_columns=self.fact_columns(b,columns,mapping)
        # Keep old receipt keys when no specification columns are selected.
        fingerprint=digest([columns,rows,mapping,fact_columns] if fact_columns else [columns,rows,mapping])
        return columns,rows,mapping,fingerprint
    def fact_columns(self,b,columns,mapping):
        selected=b.get('fact_columns')
        if not self.supports_fact_columns:
            if selected:raise Problem('供货更新不支持规格列归集，请在商品档案核对身份变化')
            return []
        if selected is None:
            counts={norm(name):sum(norm(other)==norm(name) for other in columns) for name in columns}
            selected=[i for i,name in enumerate(columns) if norm(name) in NORMALIZED_FACT_HEADERS and counts[norm(name)]==1 and i not in mapping.values()]
        if (not isinstance(selected,list) or len(selected)>16 or any(type(i) is not int or not 0<=i<len(columns) for i in selected)
                or len(set(selected))!=len(selected) or any(i in mapping.values() for i in selected)
                or any(not columns[i].strip() for i in selected)
                or len({norm(columns[i]) for i in selected})!=len(selected)):
            raise Problem('规格列需选择不与其他字段重复的1至16个不同列')
        return selected
    def row_data(self,columns,values,mapping,fact_columns):
        raw={field:values[index] for field,index in mapping.items()}
        pieces=[]
        for index in fact_columns:
            value=values[index]
            if value is None or str(value).strip()=='':continue
            if isinstance(value,(dict,list,bool)) or len(str(value).strip())>300:raise Problem('规格列内容过长或格式无效，请核对供应商资料')
            pieces.append(columns[index].strip()+'：'+str(value).strip())
        if pieces:
            original=str(raw.get('facts') or '').strip()
            raw['facts']='\n'.join(([original] if original else [])+pieces)
            if len(raw['facts'])>3000:raise Problem('归集后的规格事实超过3000字，请减少规格列或拆分商品')
        return raw
    def preview(self,b,connection=None):
        if connection is None:
            with self.store.connect() as c:return self.preview(b,c)
        c=connection;columns,source,mapping,fingerprint=self.prepare(b);fact_columns=self.fact_columns(b,columns,mapping);key='source-import:'+fingerprint
        previous=c.execute('SELECT result FROM ops_requests WHERE key=?',(key,)).fetchone()
        if not previous and fact_columns:
            legacy='source-import:'+digest([columns,source,mapping])
            previous=c.execute('SELECT result FROM ops_requests WHERE key=?',(legacy,)).fetchone()
        rows=[];prepared={};groups={};existing={}
        for number,values in source:
            row={'row':number,'title':'','status':'blocked','reason':'','warnings':[]}
            try:
                if len(values)!=len(columns):raise Problem('本行列数与表头不一致，请检查逗号和引号')
                if 'title_zh' not in mapping:raise Problem('请先选择商品名称对应的列')
                raw=self.row_data(columns,values,mapping,fact_columns);p=clean(raw)
                # Only product data is mapped; supplied approval or platform flags never survive.
                prepared[number]=raw;row['title']=p['title_zh'];row['status']='ready';row['reason']='可导入为待补充商品'
                row['values']={k:p[k] for k in mapping}
                if fact_columns:row['values']['facts']=p['facts']
                source_key=(p['source_url'],p['source_sku']) if p['source_url'] else None
                if not source_key:row['warnings'].append('缺少货源链接，修改文件后无法可靠判断是否重复商品')
                if p['source_url'] and not p['source_sku']:row['warnings'].append('未填写规格货号；同一链接下的不同规格请分配不同货号')
                if source_key:
                    groups.setdefault(source_key,[]).append((number,digest(p)))
                    old=c.execute('SELECT id,revision FROM products WHERE source_key IN (?,?)',source_identity_keys(*source_key)).fetchall()
                    if len(old)>1:row.update(status='blocked',reason='该货源与规格对应多份商品档案，请先人工核对')
                    elif old:existing[number]=dict(old[0]);row.update(status='duplicate',reason='商品库已有同一货源与规格，保留现有资料',existing_id=old[0]['id'])
            except Problem as e:
                message=str(e)
                for k,label in LABELS.items():message=message.replace(k,label)
                row['reason']=message
            rows.append(row)
        by_number={r['row']:r for r in rows}
        for items in groups.values():
            if len(items)<2:continue
            if len({h for _,h in items})>1:
                for number,_ in items:by_number[number].update(status='blocked',reason='本文件中同一货源与规格存在不同资料，请修正货号或合并冲突行')
            else:
                for number,_ in items[1:]:
                    if by_number[number]['status']=='ready':
                        by_number[number].update(status='duplicate',reason='与本文件前面的商品重复，仅保留第一条')
        if previous:
            for row in rows:
                if row['status']=='ready':row.update(status='imported',reason='相同文件内容与列对应关系已导入，未重复建立商品')
        plan=self.processing(b);processing=None
        if plan:
            ready={role:self.automation.app.models.ready(role) for role in ('primary','review')}
            waiting=[];eligible=[];calls=0
            from models import CONTENT_FIELDS
            for row in rows:
                if row['status']!='ready':continue
                p=clean(prepared[row['row']]);reasons=[];missing=any(not p[k] for k in CONTENT_FIELDS)
                translate=plan['translate'] and missing
                for k,label in [('source_url','货源链接'),('supplier','供应商'),('facts','规格事实')]:
                    if not p[k]:reasons.append('缺少'+label)
                if translate and not ready['primary']:reasons.append('批量处理模型未配置或已停用')
                if plan['review'] and not ready['review']:reasons.append('复核模型未配置或已停用')
                if plan['review'] and missing and not translate:reasons.append('双语未齐全，请启用补齐双语')
                row['processing_reasons']=reasons
                if reasons:waiting.append(row['row'])
                else:eligible.append(row['row']);calls+=int(translate)+int(plan['review'])
            processing={'plan':plan,'eligible_rows':eligible,'waiting_rows':waiting,'max_calls':calls}
        count=sum(r['status']=='ready' for r in rows)
        examples=[]
        for i,col in enumerate(columns):
            value=next((v[i] for _,v in source if i<len(v) and v[i] not in ('',None)),None)
            examples.append({'index':i,'name':col or '未命名列','example':str(value)[:120] if value is not None else ''})
        return {'columns':examples,'mapping':mapping,'fact_columns':fact_columns,'fields':{k:LABELS.get(k,k) for k in FIELDS},'rows':rows,'ready':count,'blocked':sum(r['status']=='blocked' for r in rows),'duplicates':sum(r['status']=='duplicate' for r in rows),
            'already_imported':bool(previous),'processing':processing,'token':digest([fingerprint,rows,existing,processing]) if processing else digest([fingerprint,rows,existing]),'fingerprint':fingerprint,'prepared':[prepared[r['row']] for r in rows if r['status']=='ready']}
    def apply(self,b):
        if b.get('confirmed') is not True:raise Problem('请确认只导入预览中可用的记录，错误行和重复行将跳过')
        columns,rows,mapping,fingerprint=self.prepare(b);key='source-import:'+fingerprint
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE');old=c.execute('SELECT result FROM ops_requests WHERE key=?',(key,)).fetchone()
            if not old and self.fact_columns(b,columns,mapping):
                old=c.execute('SELECT result FROM ops_requests WHERE key=?',('source-import:'+digest([columns,rows,mapping]),)).fetchone()
            if old:return {**json.loads(old['result']),'replayed':True}
            pre=self.preview(b,c)
            if b.get('preview_token')!=pre['token']:raise Problem('商品库或列对应关系已变化，请重新预览',409)
            if not pre['ready']:raise Problem('没有可导入记录，请修正错误或列对应关系')
            result=self.store.import_rows(pre['prepared'],connection=c)
            ready_rows=[row for row in pre['rows'] if row['status']=='ready']
            if len(result['created'])!=len(ready_rows):
                raise Problem('导入结果与预览行号不一致，请重新预览后核对',409)
            ids=result['created']
            placeholders=','.join('?' for _ in ids)
            stored={row['id']:json.loads(row['data']) for row in c.execute(
                f'SELECT id,data FROM products WHERE id IN ({placeholders})',ids)}
            result['created_rows']=[{'row':row['row'],'product_id':pid,'title':row['title'],
                                     'partner_sku':stored[pid]['partner_sku'],
                                     'source_sku':stored[pid]['source_sku'],
                                     'source_url':stored[pid]['source_url']}
                                    for row,pid in zip(ready_rows,ids)]
            result.update(skipped=[{'row':r['row'],'status':r['status'],'reason':r['reason']} for r in pre['rows'] if r['status']!='ready'],replayed=False)
            if pre['processing']:
                plan=pre['processing']['plan'];request={'product_ids':result['created'],'plan':plan}
                check=self.automation.preflight(request,c)
                result['processing']={'run_id':None,'queued':len(check['eligible_ids']),'waiting':[{'product_id':r['id'],'title':r['title'],'reasons':r['reasons']} for r in check['rows'] if r['status']!='ready']}
                if check['eligible_ids']:
                    run=self.automation.create({**request,'name':'导入后自动加工','request_id':'intake:'+fingerprint,'preflight_token':check['token']},connection=c)
                    result['processing']['run_id']=run['id']
            c.execute('INSERT INTO ops_requests VALUES(?,?,?)',(key,fingerprint,json.dumps(result,ensure_ascii=False)))
            return result
