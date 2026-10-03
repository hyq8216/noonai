"""Versioned supplier connections; credentials never enter SQLite or public state."""
import ipaddress
import json
import os
import re
import tempfile
import threading
from urllib.parse import urlsplit, urlunsplit
from core import Problem, ident, now

ACCOUNT_SCHEMA_SQL = '''CREATE TABLE IF NOT EXISTS source_channel_accounts(
 id TEXT PRIMARY KEY, provider TEXT NOT NULL, name TEXT NOT NULL,
 base_url TEXT NOT NULL, enabled INTEGER NOT NULL, revision INTEGER NOT NULL,
 config TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);'''


def _provider(pid, name, adapter=None, host_hint='', authorization='平台开放接口需单独申请授权', limitations='API/OAuth 接入待实现；当前可通过供应商文件导入'):
    return dict(id=pid, name=name, adapter=adapter, host_hint=host_hint,
                authorization=authorization, limitations=limitations)

PROVIDERS = [
    _provider('1688', '1688'), _provider('alibaba', 'Alibaba 国际站'),
    _provider('taobao', '淘宝'), _provider('tmall', '天猫'), _provider('jd', '京东'),
    _provider('pinduoduo', '拼多多'), _provider('aliexpress', 'AliExpress'),
    _provider('amazon', 'Amazon'),
    _provider('ebay', 'eBay', 'ebay', 'https://api.ebay.com',
              '需用户提供有权限的 OAuth access token', '只读 Browse API；真实账号与商品采集尚未验证'),
    _provider('shopify', 'Shopify', 'shopify', 'https://店铺.myshopify.com',
              '需店铺授权的 Admin API access token 与商品读取权限', '只读商品 API；真实店铺权限尚未验证'),
    _provider('custom_json', '授权 JSON API', 'json_api', 'https://供应商公共域名/商品接口',
              '需供应商授权，可配置 Bearer token', '只读 JSON 商品接口；字段映射与真实接口需核验'),
    _provider('supplier_file', '供应商 CSV/JSON 文件', 'file', '',
              '需有权使用供应商文件与素材', '通过现有文件导入流程；不代表在线连接'),
]
for _entry in PROVIDERS:
    _entry['preferred']=_entry['id'] in ('1688','taobao','pinduoduo','supplier_file')
    _entry['capture_mode']='local-browser-package' if _entry['id'] in ('1688','taobao','pinduoduo') else ('file' if _entry['id']=='supplier_file' else 'legacy-api' if _entry['adapter'] else 'not-integrated')
    if _entry['capture_mode']=='local-browser-package':
        _entry['authorization']='用户在本机浏览器自行登录，主动采集当前商品页；不保存登录会话'
        _entry['limitations']='本机网页字段采集已提供；真实网页兼容性待验证，不是官方API或自动分页'
PROVIDER_IDS = {row['id'] for row in PROVIDERS}
SECRET_KEYS = {'token', 'accesstoken', 'refreshtoken', 'apikey', 'secret', 'clientsecret',
               'password', 'authorization', 'cookie', 'credentials', 'accesskey', 'privatekey'}


def validate_base_url(provider, value):
    if not isinstance(value, str) or len(value) > 2048:
        raise Problem('接口地址格式无效')
    value = value.strip()
    if not value:
        return ''
    if any(ord(char) < 33 for char in value) or '\\' in value:
        raise Problem('接口地址格式无效')
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or '').lower()
        port = parsed.port
    except ValueError:
        raise Problem('接口地址格式无效')
    if parsed.scheme != 'https' or not host or parsed.username is not None or parsed.password is not None or parsed.fragment or parsed.query:
        raise Problem('接口地址必须为 HTTPS，且不可包含登录信息、查询参数或片段')
    if port not in (None, 443) or host == 'localhost' or host.endswith(('.localhost', '.local', '.internal')):
        raise Problem('接口必须使用公共 HTTPS 域名')
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not re.fullmatch(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', host):
            raise Problem('接口必须使用有效公共域名')
    else:
        if not address.is_global:
            raise Problem('接口不可使用内网或本机 IP')
    if provider == 'shopify' and not re.fullmatch(r'[a-z0-9][a-z0-9-]*\.myshopify\.com', host):
        raise Problem('Shopify 接口必须使用店铺的 myshopify.com 域名')
    if provider == 'ebay' and host != 'api.ebay.com':
        raise Problem('eBay 接口必须使用 api.ebay.com')
    netloc = '[' + host + ']' if ':' in host else host
    return urlunsplit(('https', netloc, parsed.path.rstrip('/'), '', ''))


def _config(value):
    if not isinstance(value, dict):
        raise Problem('连接配置必须为对象')
    if 'api_version' in value and (not isinstance(value['api_version'], str) or not re.fullmatch(r'20[0-9]{2}-(01|04|07|10)', value['api_version'])):
        raise Problem('Shopify API 版本格式无效')
    if 'marketplace_id' in value and (not isinstance(value['marketplace_id'], str) or not re.fullmatch(r'EBAY_[A-Z]{2,8}', value['marketplace_id'])):
        raise Problem('eBay 市场编号格式无效')
    for key in ('items_path', 'cursor_path'):
        if key in value and (not isinstance(value[key], str) or len(value[key]) > 200 or not re.fullmatch(r'[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*', value[key])):
            raise Problem('JSON 数据路径格式无效')
    if 'field_mapping' in value:
        fields = {'external_id', 'external_product_id', 'title_zh', 'source_title', 'source_url', 'source_sku', 'supplier', 'brand', 'facts', 'images', 'stock', 'cost_cny', 'source_currency', 'source_price'}
        mapping = value['field_mapping']
        if not isinstance(mapping, dict) or any(key not in fields or not isinstance(path, str) or len(path) > 200 or not re.fullmatch(r'[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*', path) for key, path in mapping.items()):
            raise Problem('JSON 商品字段映射格式无效')
    if 'headers' in value:
        raise Problem('不支持自定义请求头；请使用独立令牌字段')
    def check(item, depth=0):
        if depth > 8:
            raise Problem('连接配置层级过深')
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or len(key) > 100:
                    raise Problem('连接配置字段无效')
                normalized = re.sub(r'[^a-z]', '', key.lower())
                if any(normalized.endswith(secret) for secret in SECRET_KEYS):
                    raise Problem('凭据只能放在独立令牌字段，不能写入公开配置')
                check(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                check(child, depth + 1)
        elif item is not None and not isinstance(item, (str, int, float, bool)):
            raise Problem('连接配置不是有效 JSON')
    check(value)
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError):
        raise Problem('连接配置不是有效 JSON')
    if len(encoded.encode()) > 32000:
        raise Problem('连接配置超过32KB')
    return encoded


class ChannelAccounts:
    def __init__(self, store):
        self.store = store
        self.lock = threading.RLock()
        self.credentials = store.root / 'credentials' / 'source-channels'
        for directory in (self.credentials.parent, self.credentials):
            if directory.is_symlink():
                raise Problem('凭据目录不能为符号链接')
            directory.mkdir(mode=0o700, exist_ok=True)
            directory.chmod(0o700)
        with store.connect() as connection:
            connection.executescript(ACCOUNT_SCHEMA_SQL)

    def _path(self, account_id):
        if not isinstance(account_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,96}', account_id):
            raise Problem('连接编号无效')
        path = self.credentials / (account_id + '.json')
        if self.credentials.is_symlink() or self.credentials.parent.is_symlink() or path.is_symlink():
            raise Problem('凭据路径不能为符号链接')
        return path

    def _read(self, account_id):
        path = self._path(account_id)
        if not path.exists():
            return None
        try:
            return path.read_bytes()
        except OSError:
            raise Problem('连接凭据不可读取')

    def _write(self, account_id, content):
        path = self._path(account_id)
        if content is None:
            path.unlink(missing_ok=True)
            return
        fd, temporary = tempfile.mkstemp(prefix='.account-', dir=self.credentials)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def get(self, account_id, connection=None, with_secret=False):
        self._path(account_id)
        if connection is None:
            with self.lock, self.store.connect() as connection:
                return self.get(account_id, connection, with_secret)
        row = connection.execute('SELECT * FROM source_channel_accounts WHERE id=?', (account_id,)).fetchone()
        if row is None:
            raise Problem('货源连接不存在', 404)
        record = {key: row[key] for key in ('id', 'provider', 'name', 'base_url', 'revision')}
        record.update(enabled=bool(row['enabled']), config=json.loads(row['config']))
        secret = self._read(account_id)
        token = ''
        credential_problem = ''
        if secret:
            try:
                saved = json.loads(secret)
                if not isinstance(saved, dict):
                    raise ValueError('invalid credential record')
                token = saved['token']
                if not isinstance(token, str):
                    raise ValueError('invalid token')
                if saved.get('account_id') != account_id or saved.get('provider') != row['provider'] or saved.get('base_url') != row['base_url']:
                    credential_problem = '令牌绑定与连接配置不一致，请重新填写或清除令牌'
            except (ValueError, KeyError, TypeError):
                credential_problem = '连接凭据损坏，请重新填写或清除令牌'
        record['has_token'] = bool(token)
        api = record['provider'] in ('shopify', 'ebay', 'custom_json')
        record['status'] = ('disabled' if not record['enabled'] else 'configured' if api and record['base_url'] else 'file_import' if record['provider'] == 'supplier_file' else 'authorization_required')
        if credential_problem:
            record.update(status='attention', status_detail=credential_problem)
        if with_secret:
            if credential_problem:
                raise Problem(credential_problem, 409)
            record['token'] = token
        return record

    def state(self):
        with self.lock, self.store.connect() as connection:
            accounts = [self.get(row['id'], connection) for row in connection.execute('SELECT id FROM source_channel_accounts ORDER BY created_at,id')]
        return {'providers': json.loads(json.dumps(PROVIDERS)), 'accounts': accounts}

    def save(self, body):
        if not isinstance(body, dict):
            raise Problem('连接配置格式无效')
        account_id = body.get('id') or ident()
        self._path(account_id)
        provider = body.get('provider')
        if provider not in PROVIDER_IDS:
            raise Problem('不支持的货源渠道')
        name = body.get('name')
        if not isinstance(name, str) or not name.strip() or len(name) > 120:
            raise Problem('请填写不超过120字的连接名称')
        enabled = body.get('enabled', True)
        if not isinstance(enabled, bool):
            raise Problem('连接启用状态必须为布尔值')
        base_url = validate_base_url(provider, body.get('base_url', ''))
        config = _config(body.get('config', {}))
        token = body.get('token', '')
        if not isinstance(token, str) or len(token) > 8192 or '\r' in token or '\n' in token:
            raise Problem('令牌格式无效')
        token = token.strip()
        clear = body.get('clear_token', False)
        if not isinstance(clear, bool) or clear and token:
            raise Problem('清除令牌参数无效')
        with self.lock:
            original = None
            changed = False
            try:
                with self.store.connect() as connection:
                    connection.execute('BEGIN IMMEDIATE')
                    old = connection.execute('SELECT * FROM source_channel_accounts WHERE id=?', (account_id,)).fetchone()
                    if old:
                        revision = body.get('revision')
                        if not isinstance(revision, int) or isinstance(revision, bool) or revision != old['revision']:
                            raise Problem('连接配置已变化，请刷新后重试', 409)
                        if old['provider'] != provider:
                            raise Problem('已有连接不能更改渠道，请创建新连接')
                    elif body.get('id'):
                        raise Problem('货源连接不存在', 404)
                    revision = old['revision'] + 1 if old else 1
                    original = self._read(account_id)
                    if original and not token and not clear:
                        if old and old['base_url'] != base_url:
                            raise Problem('接口地址改变时必须重新填写或明确清除令牌', 409)
                        if old:
                            # Never silently rebind an interrupted or legacy credential.
                            self.get(account_id, connection, with_secret=True)
                    replacement = None if clear else json.dumps({'token': token, 'account_id': account_id,
                                                                  'provider': provider, 'base_url': base_url}).encode() if token else original
                    if replacement != original:
                        self._write(account_id, replacement)
                        changed = True
                    timestamp = now()
                    connection.execute('INSERT INTO source_channel_accounts VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,base_url=excluded.base_url,enabled=excluded.enabled,revision=excluded.revision,config=excluded.config,updated_at=excluded.updated_at',
                                       (account_id, provider, name.strip(), base_url, int(enabled), revision, config, old['created_at'] if old else timestamp, timestamp))
                    result = self.get(account_id, connection)
            except BaseException:
                if changed:
                    self._write(account_id, original)
                raise
            return result

    def remove(self, body):
        if not isinstance(body, dict):
            raise Problem('连接删除请求格式无效')
        account_id = body.get('id')
        self._path(account_id)
        with self.lock:
            original = None
            changed = False
            try:
                with self.store.connect() as connection:
                    connection.execute('BEGIN IMMEDIATE')
                    record = self.get(account_id, connection)
                    revision = body.get('revision')
                    if not isinstance(revision, int) or isinstance(revision, bool) or revision != record['revision']:
                        raise Problem('连接配置已变化，请刷新后重试', 409)
                    original = self._read(account_id)
                    if original is not None:
                        self._write(account_id, None)
                        changed = True
                    connection.execute('DELETE FROM source_channel_accounts WHERE id=?', (account_id,))
            except BaseException:
                if changed:
                    self._write(account_id, original)
                raise
        return {'removed': account_id}
