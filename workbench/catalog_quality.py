"""Read-only catalog collision hints; never merge or assert product identity."""
import json
import re
import unicodedata


def normalized(value):
    value=unicodedata.normalize('NFKC',str(value or '')).casefold()
    return re.sub(r'\s+',' ',value).strip()


def preview(store,ids,connection=None):
    if connection is None:
        with store.connect() as c:return preview(store,ids,c)
    products=[]
    for start in range(0,len(ids),500):
        part=ids[start:start+500]
        for row in connection.execute('SELECT id,data FROM products WHERE id IN ('+','.join('?' for _ in part)+')',part):
            p=json.loads(row['data']);p['id']=row['id'];products.append(p)
    by_source={};by_identity={}
    for p in products:
        source=normalized(p.get('source_url'))
        if source:by_source.setdefault(source,[]).append(p)
        identity=tuple(normalized(p.get(k)) for k in ('supplier','brand','title_zh','facts'))
        if identity[0] and identity[2] and identity[3]:by_identity.setdefault(identity,[]).append(p)
    variants=[];similar=[]
    for source,group in by_source.items():
        if len(group)<2:continue
        inconsistencies=[]
        for key,label in (('brand','品牌'),('category','类目'),('mode','经营模式')):
            values={normalized(p.get(key)) for p in group if normalized(p.get(key))}
            if len(values)>1:inconsistencies.append(label+'不一致')
        skus=[normalized(p.get('source_sku')) for p in group]
        if any(not sku for sku in skus):inconsistencies.append('有规格货号缺失')
        if len(set(skus))!=len(skus):inconsistencies.append('规格货号重复')
        variants.append({'source_url':group[0]['source_url'],'count':len(group),
                         'product_ids':[p['id'] for p in group[:20]],
                         'skus':[p.get('source_sku','') for p in group[:20]],
                         'issues':inconsistencies})
    for identity,group in by_identity.items():
        if len(group)<2 or len({normalized(p.get('source_url')) for p in group})<2:continue
        similar.append({'title':group[0].get('title_zh',''),'supplier':group[0].get('supplier',''),
                        'count':len(group),'product_ids':[p['id'] for p in group[:20]],
                        'source_urls':[p.get('source_url','') for p in group[:20]],
                        'reason':'供应商、品牌、中文标题与规格事实相同，但货源链接不同'})
    variants.sort(key=lambda x:(-len(x['issues']),-x['count'],x['source_url']))
    similar.sort(key=lambda x:(-x['count'],x['title']))
    return {'same_source_groups':len(variants),'same_source_inconsistent':sum(bool(g['issues']) for g in variants),
            'similar_groups':len(similar),'same_source_sample':variants[:30],
            'similar_sample':similar[:30]}
