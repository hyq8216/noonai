"""Strict Offer API readback. Platform visibility is a dated observation, not an order guarantee."""
import math,json
from core import Problem

def validate(raw,sku):
    try:json.dumps(raw,allow_nan=False)
    except (ValueError,TypeError):raise Problem('报价返回包含无效值，保留旧记录',502)
    if not isinstance(raw,dict) or raw.get('partner_sku')!=sku or not isinstance(raw.get('offers'),list) or not isinstance(raw.get('sku'),str) or not raw['sku']:raise Problem('报价返回商品编号或结构不匹配，保留上次有效记录',502)
    seen=set()
    for o in raw['offers']:
        if not isinstance(o,dict) or any(not isinstance(o.get(k),str) or not o[k] for k in ('offer_code','country_code','business_model')):raise Problem('报价身份字段不完整，保留上次有效记录',502)
        if o['offer_code'] in seen:raise Problem('报价返回重复编号，保留上次有效记录',502)
        seen.add(o['offer_code'])
        if type(o.get('is_active')) is not bool or type(o.get('live_status')) is not bool or type(o.get('active_net_stock')) is not int or not isinstance(o.get('offer_issues'),list):raise Problem('报价状态、库存或问题字段无效，保留上次有效记录',502)
        for issue in o['offer_issues']:
            if not isinstance(issue,dict) or any(not isinstance(issue.get(k),str) for k in ('reason','subreason','description')):raise Problem('报价问题明细无效，保留上次有效记录',502)
        price=o.get('price')
        if price is not None and (not isinstance(price,dict) or type(price.get('amount')) not in (int,float) or not math.isfinite(price['amount']) or price['amount']<0 or not isinstance(price.get('currency'),str) or not price['currency']):raise Problem('报价金额或币种无效，保留上次有效记录',502)
    return raw

def summarize(platform,partner_sku,revision):
    record=(platform or {}).get('offer_readback');out={'group':'unchecked','label':'尚未读取沙特报价','offers':[],'checked_at':None,'notes':[],'order_verified':False}
    if not isinstance(record,dict):return out
    out['checked_at']=record.get('checked_at')
    try:raw=validate(record.get('response'),partner_sku)
    except Problem:out.update(group='unknown',label='报价记录待核对');return out
    stale=record.get('local_revision')!=revision
    if stale:out['notes'].append('本地资料已有变化；此报价是上次平台读取结果。')
    for o in raw['offers']:
        if o['country_code']!='sa':continue
        # A live flag alone is not enough for effective sale readiness: noon's
        # listing guidance requires both price and available stock above zero.
        inconsistent=o['live_status'] and (not o['is_active'] or bool(o['offer_issues']) or o['active_net_stock']<=0 or o.get('price') is None or o['price']['amount']<=0)
        status='unknown' if inconsistent or stale else 'visible' if o['live_status'] else 'not_live'
        label='报价回读已过期' if stale else {'unknown':'返回信息矛盾，待核对','visible':'平台报告前台可见','not_live':'平台报告未上线'}[status]
        out['offers'].append({**o,'group':status,'label':label})
    if stale:out.update(group='unknown',label='本地资料已变化，报价回读过期')
    elif not out['offers']:out.update(group='missing',label='本次未返回沙特报价')
    elif any(o['group']=='unknown' for o in out['offers']):out.update(group='unknown',label='部分报价需核对')
    elif any(o['group']=='visible' for o in out['offers']):out.update(group='visible',label='至少一个沙特报价报告可见')
    else:out.update(group='not_live',label='沙特报价尚未上线')
    return out
