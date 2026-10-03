"""Supplier cost/availability refresh; never changes warehouse stock or platform offers."""
import json
from core import Problem,clean
from source_import import SourceImport,ALIASES,LABELS,digest

class SourceUpdates(SourceImport):
    supports_fact_columns=False
    fields=['partner_sku','source_url','source_sku','title_zh','brand','facts','cost_cny','stock']
    aliases={**ALIASES,'partner_sku':['工作台SKU','工作台货号']}
    labels={**LABELS,'partner_sku':'工作台 SKU','stock':'供应商可供数量'}
    def preview(self,b,connection=None):
        if connection is None:
            with self.store.connect() as c:return self.preview(b,c)
        c=connection;columns,source,mapping,fingerprint=self.prepare(b)
        rows=[];groups={}
        for number,values in source:
            row={'row':number,'title':'','status':'blocked','reason':'','warnings':[]}
            try:
                if len(values)!=len(columns):raise Problem('本行列数与表头不一致')
                raw={k:values[i] for k,i in mapping.items()}
                patch={k:v for k,v in raw.items() if k in ('cost_cny','stock') and v is not None and str(v).strip()!=''}
                identity={k:v for k,v in raw.items() if k in ('title_zh','brand','facts') and v is not None and str(v).strip()!=''}
                if not patch and not identity:raise Problem('本行没有价格、可供数量或可核对的商品身份资料；空白保留原值')
                normalized=clean({'title_zh':identity.get('title_zh') or '供货更新',**identity,**patch,'source_url':raw.get('source_url'),'source_sku':raw.get('source_sku')})
                patch={k:normalized[k] for k in patch}
                partner=str(raw.get('partner_sku') or '').strip();url=normalized['source_url'];sku=normalized['source_sku']
                if partner:
                    matches=c.execute("SELECT * FROM products WHERE json_extract(data,'$.partner_sku')=?",(partner,)).fetchall()
                elif url:
                    matches=c.execute('SELECT * FROM products WHERE source_key=?',(url+'|'+sku,)).fetchall()
                else:raise Problem('请提供工作台SKU，或完整货源链接与对应规格货号')
                if len(matches)!=1:raise Problem('没有唯一匹配商品；请核对工作台SKU或货源链接与规格货号')
                p=self.store.unpack(matches[0]);row.update(title=p['title_zh'],product_id=p['id'],revision=p['revision'],partner_sku=p['partner_sku'],values=patch)
                if p['demo']:raise Problem('示例商品不接受供应商更新')
                if partner and ((url and url!=p['source_url']) or (sku and sku!=p['source_sku'])):raise Problem('工作台SKU与提供的货源或规格不一致，请核对')
                identity_changes={k:{'before':p.get(k),'after':normalized[k]} for k in identity if normalized[k]!=p.get(k)}
                if identity_changes:
                    row['identity_changes']=identity_changes
                    labels='、'.join(self.labels[k] for k in identity_changes)
                    raise Problem('供应商'+labels+'与当前商品不同；请先核对实物、原图和英阿内容，再到商品档案修改。本行未自动覆盖')
                if not patch:raise Problem('商品身份资料未变化，且本行没有价格或可供数量；无需更新')
                groups.setdefault(p['id'],[]).append((number,digest(patch)))
                row['changes']={k:{'before':p.get(k),'after':v} for k,v in patch.items() if p.get(k)!=v}
                if c.execute("SELECT 1 FROM jobs WHERE product_id=? AND kind='submit' AND status IN ('queued','running')",(p['id'],)).fetchone():raise Problem('商品正在提交，待提交结束后重新预览')
                row.update(status='ready' if row['changes'] else 'unchanged',reason='更新后需重新核对供货与审核；未同步noon' if row['changes'] else '价格与可供数量未变化，无需更新')
                if patch.get('stock')==0:row['warnings'].append('可供数量将为0，阻止后续本地刊登；不会自动下架平台商品')
                if 'cost_cny' in row['changes']:row['warnings'].append('采购成本变化，请重新检查利润与售价')
            except Problem as e:
                message=str(e)
                for k,label in self.labels.items():message=message.replace(k,label)
                row.update(status='blocked',reason=message)
            rows.append(row)
        indexed={r['row']:r for r in rows}
        for items in groups.values():
            if len(items)<2:continue
            if len({h for _,h in items})>1:
                for number,_ in items:indexed[number].update(status='blocked',reason='同一商品有多组不同更新值，请合并或修正冲突行')
            else:
                for number,_ in items[1:]:indexed[number].update(status='duplicate',reason='相同商品重复更新行，仅处理第一条')
        return {'columns':[{'index':i,'name':name or '未命名列','example':''} for i,name in enumerate(columns)],'mapping':mapping,'fields':{k:self.labels[k] for k in self.fields},'rows':rows,
            'ready':sum(r['status']=='ready' for r in rows),'blocked':sum(r['status']=='blocked' for r in rows),'duplicates':sum(r['status']=='duplicate' for r in rows),'unchanged':sum(r['status']=='unchanged' for r in rows),'token':digest([fingerprint,rows])}
    def apply(self,b):
        if b.get('confirmed') is not True:raise Problem('请确认仅更新预览中可用记录，商品需要重新核对供货与审核')
        _,_,_,fingerprint=self.prepare(b);token=b.get('preview_token')
        if not isinstance(token,str) or len(token)!=64:raise Problem('请先预览更新',409)
        # Receipt identifies this particular preview, not every future reuse of the supplier file.
        key='source-update:'+digest([fingerprint,token])
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE');old=c.execute('SELECT result FROM ops_requests WHERE key=?',(key,)).fetchone()
            if old:return {**json.loads(old['result']),'replayed':True}
            pre=self.preview(b,c)
            if token!=pre['token']:raise Problem('商品或提交任务已变化，请重新预览',409)
            if not pre['ready']:raise Problem('没有需要更新的可用记录')
            updated=[]
            for row in pre['rows']:
                if row['status']!='ready':continue
                self.store.update(row['product_id'],{**row['values'],'supply_checked_at':''},row['revision'],connection=c)
                self.store.event(c,row['product_id'],'批量更新供货',json.dumps({'row':row['row'],'changes':row['changes']},ensure_ascii=False))
                updated.append(row['product_id'])
            result={'updated':updated,'skipped':[{'row':r['row'],'status':r['status'],'reason':r['reason']} for r in pre['rows'] if r['status']!='ready'],'replayed':False}
            c.execute('INSERT INTO ops_requests VALUES(?,?,?)',(key,fingerprint,json.dumps(result,ensure_ascii=False)))
            return result
