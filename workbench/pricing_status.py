"""Validate a Saudi BatchGetPricing item before retaining it as a dated readback."""
import json
import math
from core import Problem


def validate(raw, partner_sku):
    try:
        json.dumps(raw, allow_nan=False)
    except (ValueError, TypeError):
        raise Problem('售价返回包含无效值，保留旧记录', 502)
    if not isinstance(raw, dict) or not isinstance(raw.get('items'), list) or len(raw['items']) != 1:
        raise Problem('售价返回条目数量不符，保留旧记录', 502)
    item = raw['items'][0]
    if not isinstance(item, dict) or item.get('partner_sku') != partner_sku or item.get('country_code') != 'sa':
        raise Problem('售价返回SKU或站点不匹配，保留旧记录', 502)
    status = item.get('status')
    if not isinstance(status, dict) or type(status.get('status_id')) is not int or not isinstance(status.get('status_code'), str) or not status['status_code']:
        raise Problem('售价返回状态无效，保留旧记录', 502)
    ok = status['status_id'] == 0 and status['status_code'] == 'OK'
    if ok:
        price = item.get('price')
        msrp = item.get('msrp')
        if type(price) not in (int, float) or not math.isfinite(price) or price < 0:
            raise Problem('平台售价无效，保留旧记录', 502)
        if msrp is not None and (type(msrp) not in (int, float) or not math.isfinite(msrp) or msrp < 0):
            raise Problem('平台划线价无效，保留旧记录', 502)
        if type(item.get('is_active')) is not bool:
            raise Problem('平台启用状态无效，保留旧记录', 502)
    elif any(item.get(field) is not None for field in ('price', 'msrp', 'is_active')):
        raise Problem('失败报价却返回可用售价，保留旧记录', 502)
    return item


def summarize(platform, partner_sku, revision):
    record = (platform or {}).get('pricing_readback')
    out = {'group': 'unchecked', 'label': '尚未读取本地售价', 'checked_at': None,
           'price': None, 'msrp': None, 'is_active': None, 'note': ''}
    if not isinstance(record, dict):
        return out
    out['checked_at'] = record.get('checked_at')
    try:
        item = validate(record.get('response'), partner_sku)
    except Problem:
        out.update(group='unknown', label='售价记录待核对')
        return out
    status = item['status']
    if status['status_id'] == 0 and status['status_code'] == 'OK':
        out.update(group='priced', label='已读取平台本地售价', price=item['price'],
                   msrp=item.get('msrp'), is_active=item['is_active'])
    elif status['status_code'] == 'NOT_FOUND':
        out.update(group='missing', label='平台未配置沙特本地售价')
    else:
        out.update(group='error', label='平台读取售价失败', note=str(status.get('message') or status['status_code'])[:500])
    if record.get('local_revision') != revision:
        out['note'] = (out['note'] + '；' if out['note'] else '') + '本地商品资料已变化，请重新回查'
    return out
