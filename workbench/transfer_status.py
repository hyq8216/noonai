"""Strict NGS transfer-price readback; USD is never a Saudi selling price."""
import json
import math
from core import Problem


def batch_items(raw, requested):
    try:json.dumps(raw, allow_nan=False)
    except (ValueError, TypeError):raise Problem('转移价返回包含无效值，保留旧记录',502)
    if not isinstance(raw,dict) or not isinstance(raw.get('items'),list) or len(raw['items'])>len(requested):
        raise Problem('转移价批量返回结构无效，保留旧记录',502)
    expected=set(requested);found={}
    for item in raw['items']:
        if not isinstance(item,dict) or item.get('partner_sku') not in expected or item['partner_sku'] in found:
            raise Problem('转移价返回未知或重复SKU，保留旧记录',502)
        found[item['partner_sku']]=item
    return found


def validate(item,sku):
    try:json.dumps(item,allow_nan=False)
    except (ValueError,TypeError):raise Problem('转移价返回包含无效值，保留旧记录',502)
    if not isinstance(item,dict) or item.get('partner_sku')!=sku:raise Problem('转移价返回SKU不匹配，保留旧记录',502)
    status=item.get('status')
    if not isinstance(status,dict) or type(status.get('status_id')) is not int or not isinstance(status.get('status_code'),str) or not status['status_code']:
        raise Problem('转移价状态字段无效，保留旧记录',502)
    ok=status['status_id']==0 and status['status_code']=='OK'
    if (status['status_id']==0)!= (status['status_code']=='OK'):
        raise Problem('转移价状态字段矛盾，保留旧记录',502)
    if ok:
        price=item.get('transfer_price_usd');msrp=item.get('msrp_usd')
        if type(price) not in (int,float) or not math.isfinite(price) or price<0:
            raise Problem('美元转移价无效，保留旧记录',502)
        if msrp is not None and (type(msrp) not in (int,float) or not math.isfinite(msrp) or msrp<0):
            raise Problem('美元划线价无效，保留旧记录',502)
        if type(item.get('is_active')) is not bool:raise Problem('转移价启用状态无效，保留旧记录',502)
    elif any(item.get(k) is not None for k in ('transfer_price_usd','msrp_usd','is_active')):
        raise Problem('失败转移价含有可用金额，保留旧记录',502)
    return item


def summarize(platform,sku,revision,local_price,mode):
    out={'group':'unchecked','label':'尚未读取NGS转移价','checked_at':None,'transfer_price_usd':None,
         'msrp_usd':None,'is_active':None,'matches_local':None,'note':''}
    if mode!='NGS':return out
    record=(platform or {}).get('transfer_price_readback')
    if not isinstance(record,dict):return out
    out['checked_at']=record.get('checked_at')
    try:item=validate(record.get('item'),sku)
    except Problem:out.update(group='unknown',label='转移价记录待核对');return out
    status=item['status']
    if status['status_id']==0:
        price=item['transfer_price_usd']
        out.update(group='priced',label='已读取NGS美元转移价',transfer_price_usd=price,
                   msrp_usd=item.get('msrp_usd'),is_active=item['is_active'],
                   matches_local=None if local_price is None else abs(price-local_price)<0.000001)
    elif status['status_code']=='NOT_FOUND':out.update(group='missing',label='平台未配置NGS转移价')
    else:out.update(group='error',label='平台读取转移价失败',note=str(status.get('message') or status['status_code'])[:500])
    if record.get('local_revision')!=revision:
        out['note']=(out['note']+'；' if out['note'] else '')+'本地商品资料已变化，请重新回查'
    return out
