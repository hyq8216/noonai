"""Read-only authorized catalog readers. External data never establishes approval.

Shopify: Admin GraphQL productVariants (read_products; inventory access is
account-dependent). eBay: Browse item_summary/search (application OAuth token).
Generic APIs: configured public HTTPS GET endpoint with Bearer auth only.
"""
import ipaddress
import math
import re
import socket
from html import unescape
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit, quote

import connectors
from core import Problem

SHOPIFY_QUERY = '''query SourceVariants($first: Int!, $after: String, $query: String) {
  shop { currencyCode }
  productVariants(first: $first, after: $after, query: $query) {
    nodes {
      id title sku price inventoryQuantity selectedOptions { name value }
      image { url }
      product { id title vendor handle onlineStoreUrl }
    }
    pageInfo { hasNextPage endCursor }
  }
}'''


def public_url(value, *, resolve=False):
    """Require public HTTPS; API destinations additionally pass a DNS check."""
    if not isinstance(value, str) or not value or len(value) > 4096 or any(ord(ch) < 32 for ch in value):
        raise Problem('来源地址必须是公开 HTTPS 地址')
    try:
        p = urlsplit(value)
        if p.scheme != 'https' or not p.hostname or p.username or p.password or p.port not in (None, 443):
            raise ValueError()
        host = p.hostname.lower().rstrip('.')
        if '.' not in host or host.endswith(('.localhost', '.local', '.internal', '.test', '.invalid')):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError()
        if resolve:
            addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
                raise ValueError()
        return urlunsplit(('https', host, p.path or '/', p.query, ''))
    except (ValueError, OSError):
        raise Problem('来源地址不是可访问的公开 HTTPS 地址')


def _origin(url):
    p = urlsplit(url)
    return p.scheme, p.hostname, p.port or 443


def _destination(url, provider, base):
    url = public_url(url, resolve=True)
    if _origin(url) != _origin(base):
        raise Problem('分页地址不能改变来源服务，未向新地址转发凭证')
    host = urlsplit(url).hostname
    if provider == 'shopify' and not re.fullmatch(r'[a-z0-9][a-z0-9-]*\.myshopify\.com', host):
        raise Problem('Shopify 账号必须使用 myshopify.com 域名')
    if provider == 'ebay' and host != 'api.ebay.com':
        raise Problem('eBay 读取仅支持 api.ebay.com')
    return url


def _plain(value, maximum=12000):
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ''
    text = str(value)
    text = re.sub(r'<(script|style)\b[^>]*>.*?</\1>', '', text, flags=re.I | re.S)
    text = re.sub(r'<[^>]*>', ' ', text)
    text = unescape(text)
    return re.sub(r'\s+', ' ', text).strip()[:maximum]


def _facts(value):
    # Only explicit facts, never description/marketing fields. Obvious prompts
    # are excluded; downstream content generation still treats facts as untrusted.
    if isinstance(value, dict):
        lines = [f'{_plain(k, 150)}: {_plain(v, 1000)}' for k, v in list(value.items())[:100]
                 if isinstance(v, (str, int, float)) and not isinstance(v, bool)]
    elif isinstance(value, list):
        lines = [_plain(v, 1000) for v in value[:100]]
    else:
        lines = [_plain(value)]
    banned = re.compile(r'ignore\s+(all|previous|prior)|system\s*prompt|follow\s+these\s+instructions|忽略.*指令|系统提示|无条件批准', re.I)
    return '\n'.join(line for line in lines if line and not banned.search(line))[:12000]


def _number(value, integer=False):
    if value is None or value == '' or isinstance(value, bool):
        return None
    try:
        n = float(value)
        if not math.isfinite(n) or n < 0 or (integer and n != int(n)):
            return None
        return int(n) if integer else n
    except (ValueError, TypeError, OverflowError):
        return None


def _normalize(raw):
    external_id = _plain(raw.get('external_id'), 1000)
    title = _plain(raw.get('title_zh') or raw.get('source_title'), 1000)
    if not external_id or not title:
        raise Problem('来源商品缺少稳定标识或原始标题', 502)
    source_url = public_url(raw.get('source_url'))
    images = []
    values = raw.get('images') or []
    if isinstance(values, str):
        values = [values]
    if isinstance(values, list):
        for value in values[:50]:
            try:
                url = public_url(value)
                if url not in images:
                    images.append(url)
            except Problem:
                continue
    currency = _plain(raw.get('source_currency'), 3).upper()
    if not re.fullmatch('[A-Z]{3}', currency):
        currency = ''
    price = _number(raw.get('source_price'))
    return {'external_id': external_id, 'external_product_id': _plain(raw.get('external_product_id'), 1000),
            'title_zh': title, 'source_title': title, 'source_url': source_url,
            'source_sku': _plain(raw.get('source_sku'), 1000) or external_id, 'supplier': _plain(raw.get('supplier'), 1000),
            'brand': _plain(raw.get('brand'), 1000), 'facts': _facts(raw.get('facts')),
            'images': images, 'stock': _number(raw.get('stock'), integer=True),
            'cost_cny': _number(raw.get('cost_cny')) if currency == 'CNY' else None,
            'source_currency': currency, 'source_price': price}


def _get_path(data, path, default=None):
    if not isinstance(path, str) or len(path) > 200:
        raise Problem('字段映射路径无效')
    for part in path.split('.') if path else []:
        if isinstance(data, dict):
            data = data.get(part, default)
        else:
            return default
    return data


def _response(data):
    if not isinstance(data, dict):
        raise Problem('来源服务返回格式不符合约定', 502)
    return data


def fetch_page(account, cursor=None, query='', limit=50):
    """Fetch one bounded catalog page; no writes, media download or live approval."""
    if not isinstance(account, dict):
        raise Problem('来源账号配置无效')
    provider = account.get('provider')
    if provider not in ('shopify', 'ebay', 'custom_json', 'json_api'):
        raise Problem('该渠道尚未提供已验证的商品读取接口', 409)
    if not account.get('enabled', True):
        raise Problem('来源账号已停用', 409)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise Problem('每页商品数量必须为 1 至 100')
    if cursor is not None and (not isinstance(cursor, str) or len(cursor) > 4096):
        raise Problem('分页游标格式无效')
    if not isinstance(query, str) or len(query) > 1000:
        raise Problem('搜索词格式无效')
    config = account.get('config') or {}
    if not isinstance(config, dict):
        raise Problem('来源账号配置无效')
    token = account.get('token', '')
    if not isinstance(token, str) or '\n' in token or '\r' in token:
        raise Problem('来源账号凭证格式无效')
    if provider in ('shopify', 'ebay') and not token:
        raise Problem('请先配置来源账号 API 凭证', 409)
    base = public_url(account.get('base_url'))
    warnings = []
    next_cursor = None
    if provider == 'shopify':
        version = config.get('api_version', '2026-07')
        if not isinstance(version, str) or not re.fullmatch(r'20\d{2}-(01|04|07|10)', version):
            raise Problem('Shopify API 版本格式无效')
        if urlsplit(base).path != '/' or urlsplit(base).query:
            raise Problem('Shopify 地址只能包含店铺域名')
        url = _destination(base.rstrip('/') + '/admin/api/' + version + '/graphql.json', provider, base)
        result = _response(connectors.request_json(url, {'query': SHOPIFY_QUERY, 'variables': {'first': limit, 'after': cursor, 'query': query or None}},
                                                  {'X-Shopify-Access-Token': token}))
        if result.get('errors'):
            raise Problem('Shopify 商品读取失败，请核对 read_products 和库存读取权限', 502)
        try:
            data = result['data']; connection = data['productVariants']; rows = connection['nodes']
            info = connection['pageInfo']; currency = data['shop']['currencyCode']
            if info.get('hasNextPage'):
                next_cursor = info['endCursor']
                if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
                    raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise Problem('Shopify 商品分页响应不完整', 502)
        raw_items = []
        if not isinstance(rows, list) or len(rows) > limit:
            raise Problem('Shopify 商品数量超出分页约定', 502)
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('product'), dict) or not isinstance(row.get('selectedOptions', []), list):
                raise Problem('Shopify 商品响应不完整', 502)
            p = row['product']
            facts = {x['name']: x['value'] for x in row.get('selectedOptions', []) if isinstance(x, dict) and 'name' in x and 'value' in x}
            raw_items.append({'external_id': row.get('id'), 'external_product_id': p.get('id'),
                'title_zh': p.get('title'), 'source_url': p.get('onlineStoreUrl') or base.rstrip('/') + '/products/' + quote(str(p.get('handle') or ''), safe=''),
                'source_sku': row.get('sku'), 'supplier': p.get('vendor'), 'facts': facts,
                'images': [row['image'].get('url')] if isinstance(row.get('image'), dict) else [],
                'stock': row.get('inventoryQuantity'), 'source_currency': currency, 'source_price': row.get('price')})
    elif provider == 'ebay':
        if not query.strip():
            raise Problem('eBay 商品搜索需要关键词')
        endpoint = 'https://api.ebay.com/buy/browse/v1/item_summary/search'
        if _origin(base) != _origin(endpoint):
            raise Problem('eBay 读取仅支持 api.ebay.com')
        url = cursor or endpoint + '?' + urlencode({'q': query, 'limit': limit})
        url = _destination(url, provider, base)
        if urlsplit(url).path != urlsplit(endpoint).path:
            raise Problem('eBay 分页只能使用 Browse 搜索接口')
        params = dict(parse_qsl(urlsplit(url).query))
        try:
            if params.get('q') != query or int(params.get('limit', limit)) != limit or not 0 <= int(params.get('offset', '0')) <= 9999:
                raise ValueError()
        except ValueError:
            raise Problem('eBay 分页超出搜索范围')
        marketplace = config.get('marketplace_id', 'EBAY_US')
        if not isinstance(marketplace, str) or not re.fullmatch('EBAY_[A-Z]{2}', marketplace):
            raise Problem('eBay 市场代码无效')
        result = _response(connectors.request_json(url, None, {'Authorization': 'Bearer ' + token, 'X-EBAY-C-MARKETPLACE-ID': marketplace}, method='GET'))
        if result.get('errors'):
            raise Problem('eBay 商品读取失败，请核对 Browse API 权限', 502)
        rows = result.get('itemSummaries', [])
        next_cursor = result.get('next') or None
        raw_items = []
        if not isinstance(rows, list) or len(rows) > limit:
            raise Problem('eBay 商品数量超出分页约定', 502)
        for row in rows:
            if not isinstance(row, dict):
                raise Problem('eBay 商品响应不完整', 502)
            price = row.get('price', {})
            if not isinstance(price, dict) or not isinstance(row.get('seller', {}), dict) or not isinstance(row.get('image', {}), dict):
                raise Problem('eBay 商品响应不完整', 502)
            raw_items.append({'external_id': row.get('itemId'), 'title_zh': row.get('title'),
                'source_url': row.get('itemWebUrl'), 'supplier': (row.get('seller') or {}).get('username'),
                'facts': {'condition': row['condition']} if row.get('condition') else {},
                'images': [(row.get('image') or {}).get('imageUrl')],
                'source_currency': price.get('currency'), 'source_price': price.get('value')})
    else:
        if config.get('headers'):
            raise Problem('通用来源不接受自定义凭证请求头')
        url = base
        if cursor and cursor.startswith(('http:', '//')):
            raise Problem('来源服务返回不安全分页地址', 502)
        if cursor:
            if cursor.startswith('?'):
                url = urlunsplit((*urlsplit(base)[:3], cursor[1:], ''))
            elif cursor.startswith('https://'):
                url = cursor
            else:
                parts = urlsplit(base); params = dict(parse_qsl(parts.query)); params['cursor'] = cursor
                url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), ''))
        parts = urlsplit(url); params = dict(parse_qsl(parts.query)); params['limit'] = limit
        if query:
            params['q'] = query
        url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), ''))
        url = _destination(url, provider, base)
        headers = {'Authorization': 'Bearer ' + token} if token else {}
        result = _response(connectors.request_json(url, None, headers, method='GET'))
        rows = _get_path(result, config.get('items_path', 'items'))
        next_cursor = _get_path(result, config.get('cursor_path', 'next_cursor')) or None
        if not isinstance(rows, list) or len(rows) > limit:
            raise Problem('通用来源商品列表缺失或超出分页约定', 502)
        mapping = config.get('field_mapping', {})
        if not isinstance(mapping, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in mapping.items()):
            raise Problem('来源字段映射无效')
        raw_items = []
        fields = ('external_id', 'external_product_id', 'title_zh', 'source_title', 'source_url', 'source_sku', 'supplier', 'brand', 'facts', 'images', 'stock', 'cost_cny', 'source_currency', 'source_price')
        for row in rows:
            if not isinstance(row, dict):
                raise Problem('来源商品响应不完整', 502)
            raw_items.append({field: _get_path(row, mapping.get(field, field)) for field in fields})
    if next_cursor is not None:
        if not isinstance(next_cursor, str) or len(next_cursor) > 4096 or next_cursor == cursor:
            raise Problem('来源服务返回无效分页游标', 502)
        if next_cursor.startswith('https://'):
            _destination(next_cursor, provider, base)
            if provider == 'ebay':
                parts = urlsplit(next_cursor); params = dict(parse_qsl(parts.query))
                try:
                    offset = int(params.get('offset', '0'))
                    if parts.path != '/buy/browse/v1/item_summary/search' or params.get('q') != query or int(params.get('limit', limit)) != limit or offset < 0:
                        raise ValueError()
                except ValueError:
                    raise Problem('eBay 服务返回无效分页地址', 502)
                if offset > 9999:
                    next_cursor = None
                    warnings.append('eBay 搜索已达到 10000 条接口上限，请缩小关键词范围')
        elif provider == 'ebay' or next_cursor.startswith(('http:', '//')):
            raise Problem('来源服务返回不安全分页地址', 502)
    items = []
    for index, raw in enumerate(raw_items):
        try:
            items.append(_normalize(raw))
        except Problem:
            warnings.append(f'第 {index + 1} 个来源商品缺少标识、标题或公开来源链接，已隔离')
    return {'items': items, 'next_cursor': next_cursor, 'warnings': warnings}
