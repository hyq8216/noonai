"""Category-driven content fields, using noon's documented attribute contract.

Local rule files are preparation aids only. Submission validates the freshly
fetched platform contract, never trusts a local file as account verification.
"""
import json
import math
import re
from core import Problem

BUILTINS={'product_title':('title_en','title_ar'),'long_description':('description_en','description_ar')}
SUPPORTED={'ATTRIBUTE_TYPE_'+x for x in ('TEXT','SELECT','NUMERIC','METRIC','BOOL')}
LANGUAGES={'en':'LANGUAGE_EN','ar':'LANGUAGE_AR'}

def contract(raw):
    if not isinstance(raw,dict) or len(json.dumps(raw,ensure_ascii=False))>1000000:raise Problem('类目规则文件无效或过大')
    attrs=raw.get('attributes')
    if not isinstance(attrs,list) or not 1<=len(attrs)<=500:raise Problem('类目规则需要包含 1 至 500 个属性')
    seen=set()
    for a in attrs:
        if not isinstance(a,dict):raise Problem('类目属性格式无效')
        code=a.get('attribute_code')
        if not isinstance(code,str) or not re.fullmatch(r'[A-Za-z0-9_]{1,120}',code) or code in ('__proto__','constructor','prototype') or code in seen:raise Problem('类目属性编号无效或重复')
        seen.add(code)
        if not isinstance(a.get('attribute_type'),str) or not isinstance(a.get('is_mandatory'),bool):raise Problem(code+' 缺少属性类型或必填标记')
        for key in ('is_localizable','is_multivalued','is_negative_allowed','is_html_allowed'):
            if key in a and not isinstance(a[key],bool):raise Problem(code+' 的规则标记无效')
        for key in ('attribute_options','attribute_metric_units'):
            if key in a and (not isinstance(a[key],list) or any(not isinstance(v,str) for v in a[key])):raise Problem(code+' 的选项规则无效')
        for key in ('min_characters','max_characters','max_values','number_min','number_max'):
            v=a.get(key)
            if v is not None and (isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v)):raise Problem(code+' 的范围规则无效')
    return json.loads(json.dumps(raw))

def clean_values(raw):
    if not isinstance(raw,dict) or len(raw)>500 or len(json.dumps(raw,ensure_ascii=False))>200000:raise Problem('类目属性资料无效或过大')
    for code,entry in raw.items():
        if code in ('__proto__','constructor','prototype') or not re.fullmatch(r'[A-Za-z0-9_]{1,120}',code) or not isinstance(entry,dict) or set(entry)-{'en','ar','value','unit'}:raise Problem('属性填写格式无效')
        if any(not isinstance(v,str) or len(v)>24000 for v in entry.values()):raise Problem(code+' 的填写内容无效或过长')
    return {code:{k:v.strip() for k,v in entry.items()} for code,entry in raw.items()}

def typed(value,kind):
    if kind in ('ATTRIBUTE_TYPE_NUMERIC','ATTRIBUTE_TYPE_METRIC'):
        try:
            n=float(value)
            if not math.isfinite(n):return value
            return int(n) if n.is_integer() else n
        except (ValueError,TypeError):return value
    if kind=='ATTRIBUTE_TYPE_BOOL':return {'true':True,'false':False}.get(value,value)
    return value

def attributes(p):
    result={code:{'values':[{'value':p.get(en,''),'language':'LANGUAGE_EN'},{'value':p.get(ar,''),'language':'LANGUAGE_AR'}]} for code,(en,ar) in BUILTINS.items()}
    spec=p.get('category_contract') or {};rules={a['attribute_code']:a for a in spec.get('attributes',[])}
    for code,entry in p.get('attribute_values',{}).items():
        if code in BUILTINS:continue
        a=rules.get(code,{})
        values=[];units=[]
        for key in (('en','ar') if a.get('is_localizable') else ('value',)):
            raw=entry.get(key,'');parts=[v.strip() for v in raw.splitlines() if v.strip()] if a.get('is_multivalued') else ([raw] if raw else [])
            for index,v in enumerate(parts,1):
                meta={**({'language':LANGUAGES[key]} if key in LANGUAGES else {}),**({'sort':index} if a.get('is_multivalued') else {})}
                values.append({'value':typed(v,a.get('attribute_type')),**meta})
                if a.get('attribute_type')=='ATTRIBUTE_TYPE_METRIC':units.append({'value':entry.get('unit',''),**meta})
        if values:result[code]={'values':values}
        if units:result[code+'_unit']={'values':units}
    return result

def validate(raw,values):
    try:rules={a['attribute_code']:a for a in contract(raw)['attributes']}
    except Problem as e:return [str(e)]
    errors=[]
    metric_units={k+'_unit':a for k,a in rules.items() if a['attribute_type']=='ATTRIBUTE_TYPE_METRIC'}
    for code,a in rules.items():
        if a['is_mandatory'] and not values.get(code,{}).get('values'):errors.append(code+'：缺少必填属性')
    for code,item in values.items():
        unit_parent=metric_units.get(code)
        a=rules.get(code)
        if unit_parent:
            if code[:-5] not in values:errors.append(code+'：单位必须与数值同时填写')
            continue
        if a is None:errors.append(code+'：当前类目不接受此属性');continue
        kind=a['attribute_type']
        if kind not in SUPPORTED:errors.append(code+'：该类型尚未适配，请先完成适配后提交');continue
        entries=item.get('values') if isinstance(item,dict) else None
        if not isinstance(entries,list) or not entries:errors.append(code+'：属性值为空');continue
        groups={}
        for v in entries:
            if not isinstance(v,dict) or 'value' not in v:errors.append(code+'：属性值结构无效');continue
            language=v.get('language','');groups.setdefault(language,[]).append(v)
            value=v['value'];label=code+(' / '+language.removeprefix('LANGUAGE_') if language else '')
            if a.get('is_localizable') and language not in LANGUAGES.values():errors.append(label+'：缺少有效语言')
            if not a.get('is_localizable') and language:errors.append(label+'：此属性不分语言')
            if kind in ('ATTRIBUTE_TYPE_TEXT','ATTRIBUTE_TYPE_SELECT'):
                if not isinstance(value,str) or not value.strip():errors.append(label+'：请填写文字');continue
                if kind=='ATTRIBUTE_TYPE_SELECT' and value not in a.get('attribute_options',[]):errors.append(label+'：请选择类目允许的选项')
                if kind=='ATTRIBUTE_TYPE_TEXT':
                    if len(value)<(a.get('min_characters') or 0) or a.get('max_characters') and len(value)>a['max_characters']:errors.append(label+'：文字长度不符合规则')
                    if re.search(r'<\s*/?\s*[A-Za-z][^>]*>',value):errors.append(label+'：请先使用纯文本，HTML 内容尚未适配')
                    if a.get('additional_validation_regex'):errors.append(label+'：含额外格式规则，需完成专项适配')
            elif kind in ('ATTRIBUTE_TYPE_NUMERIC','ATTRIBUTE_TYPE_METRIC'):
                if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):errors.append(label+'：请填写有限数值');continue
                if value<0 and not a.get('is_negative_allowed'):errors.append(label+'：不接受负数')
                if a.get('number_min') is not None and value<a['number_min'] or a.get('number_max') is not None and value>a['number_max']:errors.append(label+'：数值超出类目范围')
            elif kind=='ATTRIBUTE_TYPE_BOOL' and not isinstance(value,bool):errors.append(label+'：请选择是或否')
        if a.get('is_localizable'):
            for language in LANGUAGES.values():
                if language not in groups:errors.append(code+'：缺少 '+language.removeprefix('LANGUAGE_')+' 内容（本地双语标准）')
        for entries_in_language in groups.values():
            if not a.get('is_multivalued') and len(entries_in_language)>1:errors.append(code+'：此属性只接受一个值')
            if a.get('max_values') and len(entries_in_language)>a['max_values']:errors.append(code+'：值的数量超过类目上限')
            if a.get('is_multivalued') and [v.get('sort') for v in entries_in_language]!=list(range(1,len(entries_in_language)+1)):errors.append(code+'：多值排序须从 1 连续排列')
        if kind=='ATTRIBUTE_TYPE_METRIC':
            units=values.get(code+'_unit',{}).get('values',[])
            if len(units)!=len(entries):errors.append(code+'：请同时填写对应单位')
            for index,u in enumerate(units):
                if not isinstance(u,dict) or u.get('value') not in a.get('attribute_metric_units',[]):errors.append(code+'：单位不在允许范围');continue
                if index<len(entries) and any(u.get(k)!=entries[index].get(k) for k in ('language','sort')):errors.append(code+'：数值与单位的语言或顺序不一致')
    return list(dict.fromkeys(errors))

def product_issues(p):
    spec=p.get('category_contract')
    if not spec:return []
    if spec.get('category_code')!=p.get('category'):return ['类目已变化，请重新载入对应类目规则']
    codes={a['attribute_code'] for a in spec['attributes']}
    extra=[code+'：当前类目不接受此属性，请移除不适用值' for code,entry in p.get('attribute_values',{}).items() if code not in codes and any(entry.values())]
    return extra+validate(spec,attributes(p))
