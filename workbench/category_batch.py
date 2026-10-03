"""Preview-first, atomic reuse of category rules and explicitly chosen shared values."""
import hashlib
import json
from core import Problem
from category_rules import contract,attributes,validate,product_issues,BUILTINS

class CategoryBatch:
    def __init__(self,store):self.store=store
    def inputs(self,b):
        ids=b.get('product_ids');codes=b.get('attribute_codes',[])
        if not isinstance(ids,list) or not 1<=len(ids)<=500 or any(not isinstance(x,str) for x in ids) or len(set(ids))!=len(ids):raise Problem('请选择 1 至 500 件不同商品')
        if not isinstance(codes,list) or len(codes)>500 or any(not isinstance(x,str) for x in codes) or len(set(codes))!=len(codes):raise Problem('公共属性选择无效')
        if not isinstance(b.get('source_id'),str):raise Problem('请选择提供规则的商品')
        if not b.get('copy_rules') is True and not codes:raise Problem('请启用规则复用或选择至少一个公共属性')
        return ids,codes
    def preview(self,b,connection=None):
        ids,codes=self.inputs(b)
        if connection is None:
            with self.store.connect() as c:
                c.execute('BEGIN')
                return self.preview(b,c)
        c=connection
        source=self.store.unpack(c.execute('SELECT * FROM products WHERE id=?',(b['source_id'],)).fetchone())
        if source['demo']:raise Problem('示例商品不能作为批量应用来源')
        spec=source.get('category_contract')
        if not spec or spec.get('category_code')!=source['category']:raise Problem('来源商品尚未载入当前类目的有效规则')
        contract(spec);rules={a['attribute_code']:a for a in spec['attributes']};source_values=attributes(source)
        for code in codes:
            if code not in rules or code in BUILTINS or code.endswith('_unit'):raise Problem('此属性不能作为公共属性复用：'+code)
            entry=source.get('attribute_values',{}).get(code,{})
            if not any(entry.values()) or code not in source_values:raise Problem(code+' 在来源商品中尚未填写')
            relevant={k:v for k,v in source_values.items() if k in (code,code+'_unit')}
            errors=validate({'attributes':[rules[code]]},relevant)
            if errors:raise Problem('来源属性尚不合格：'+'；'.join(errors))
        copied={**spec,'copied_from':{'id':source['id'],'title':source['title_zh'],'revision':source['revision']}}
        rows=[];updates={}
        for pid in ids:
            p=self.store.unpack(c.execute('SELECT * FROM products WHERE id=?',(pid,)).fetchone());reasons=[];changes=[];kept=[]
            if p['demo']:reasons.append('示例商品不参与批量应用')
            if pid==source['id']:reasons.append('来源商品保持不变')
            if p['category']!=source['category']:reasons.append('类目不一致，请先单独确认类目')
            if c.execute("SELECT 1 FROM automation_items WHERE product_id=? AND status NOT IN ('done','cancelled')",(pid,)).fetchone() or c.execute("SELECT 1 FROM jobs WHERE product_id=? AND status IN ('queued','running')",(pid,)).fetchone() or c.execute("SELECT 1 FROM visual_jobs WHERE json_extract(recipe,'$.product_id')=? AND status IN ('queued','waiting','preparing','generating')",(pid,)).fetchone():reasons.append('已有未结束任务，完成或取消后再应用')
            target_spec=copied if b.get('copy_rules') is True else p.get('category_contract')
            if p['category']==source['category'] and (not target_spec or target_spec.get('category_code')!=p['category']):reasons.append('请同时复用类目规则，或先为此商品载入规则')
            values=json.loads(json.dumps(p.get('attribute_values',{})))
            for code in codes:
                if any(values.get(code,{}).values()):kept.append(code);continue
                values[code]=dict(source['attribute_values'][code]);changes.append({'code':code,'value':values[code]})
            rule_changed=bool(b.get('copy_rules') is True and p.get('category_contract')!=copied)
            candidate={**p,'attribute_values':values,'category_contract':target_spec}
            if not reasons and target_spec:
                # A value valid in the source can be invalid under a different target rule version.
                target_rules={a['attribute_code']:a for a in target_spec['attributes']};candidate_values=attributes(candidate)
                for item in changes:
                    code=item['code']
                    if code not in target_rules:reasons.append(code+' 不在目标商品规则中');continue
                    relevant={k:v for k,v in candidate_values.items() if k in (code,code+'_unit')}
                    reasons.extend(validate({'attributes':[target_rules[code]]},relevant))
            status='blocked' if reasons else 'change' if rule_changed or changes else 'unchanged'
            row={'id':pid,'title':p['title_zh'],'revision':p['revision'],'status':status,'reasons':reasons,'rules_changed':rule_changed,'changes':changes,'preserved':kept,'remaining_issues':product_issues(candidate) if not reasons else []}
            rows.append(row)
            if status=='change':updates[pid]={'attribute_values':values,'category_contract':target_spec}
        digest=hashlib.sha256(json.dumps([source['id'],source['revision'],spec,sorted(codes),b.get('copy_rules') is True,rows],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        return {'token':digest,'source':{'id':source['id'],'title':source['title_zh'],'category':source['category'],'revision':source['revision']},'rows':rows,'updates':updates}
    def apply(self,c,b):
        preview=self.preview(b,c)
        if preview['token']!=b.get('preview_token'):raise Problem('来源、目标商品或任务状态已变化，请重新预览',409)
        if not preview['updates']:raise Problem('没有需要应用的更改',409)
        changed=[]
        for row in preview['rows']:
            if row['id'] not in preview['updates']:continue
            update=preview['updates'][row['id']]
            p=self.store.update(row['id'],{'attribute_values':update['attribute_values']},row['revision'],{'category_contract':update['category_contract'],'category_verified':False},connection=c)
            self.store.event(c,p['id'],'批量类目补齐','来源：'+preview['source']['title']+'；仅填空属性：'+','.join(x['code'] for x in row['changes']))
            changed.append({'id':p['id'],'revision':p['revision']})
        return {'id':b['request_id'],'updated':changed,'skipped':[r['id'] for r in preview['rows'] if r['status']!='change']}
