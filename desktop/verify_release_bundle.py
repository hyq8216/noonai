"""Start an actual bundled backend in a fresh temporary workspace and verify resources."""
import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.request
import urllib.error
import zipfile

SURFACES = ('supplier-quotes', 'fx-registry', 'replenishment', 'domestic-capture', 'inventory-counts',
            'shipping-manifests', 'pricing-plans', 'ad-analytics', 'import-profiles', 'order-intake',
            'settlements', 'catalog-groups', 'analytics', 'collection-schedules', 'backup-schedules',
            'bank-reconciliation', 'fulfillment', 'procurement', 'after-sales', 'alerts', 'batch-editor')


def clean_environment():
    clean = {key: value for key, value in os.environ.items()
            if not key.startswith(('NOON_', 'OPENAI_', 'TEXT_', 'IMAGE_HOST_', 'PYTHON'))
            and not any(marker in key.upper() for marker in ('API_KEY', 'ACCESS_TOKEN', 'SECRET', 'CREDENTIAL', 'PASSWORD'))}
    clean['PATH'] = '/usr/bin:/bin'
    return clean


def verify(app):
    backend = Path(app) / 'Contents/Resources/backend/noon-backend'
    if not backend.is_file():
        raise SystemExit('Bundled backend is missing.')
    with tempfile.TemporaryDirectory(prefix='noon-bundle-smoke-') as folder:
        root = Path(folder)
        ready = root / 'ready.json'
        # Do not inherit seller/model configuration or touch the default business workspace.
        with (root / 'backend.log').open('wb') as log:
            process = subprocess.Popen([str(backend), '--data', str(root / 'data'), '--port', '0',
                                        '--ready-file', str(ready)], env=clean_environment(), stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 60
                while not ready.is_file():
                    if process.poll() is not None:
                        raise RuntimeError('Bundled backend exited before becoming ready; no existing business data was opened.')
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Bundled backend startup timed out.')
                    time.sleep(0.1)
                url = json.loads(ready.read_text(encoding='utf-8'))['url']
                from urllib.parse import urlsplit
                parsed = urlsplit(url)
                if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1':
                    raise RuntimeError('Backend must bind only to local loopback.')
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                def read(path):
                    with opener.open(url + path, timeout=15) as response:
                        return response.read()
                state = json.loads(read('/api/state'))
                if state.get('products'):
                    raise RuntimeError('Fresh bundled workspace unexpectedly contains business products.')
                for surface in SURFACES:
                    if not isinstance(json.loads(read('/api/' + surface + '/state')), dict):
                        raise RuntimeError('Invalid module response: ' + surface)
                for name in ('navigation', 'supplier_quotes', 'fx_registry', 'replenishment', 'domestic_capture',
                             'inventory_counts', 'shipping_manifests', 'pricing_plans', 'ad_analytics', 'import_profiles'):
                    if not read('/' + name + '.js'):
                        raise RuntimeError('Bundled JavaScript is missing: ' + name)
                with zipfile.ZipFile(io.BytesIO(read('/api/domestic-capture/extension'))) as archive:
                    expected = {'noon-domestic-capture/' + name for name in
                                ('manifest.json', 'parser.js', 'popup.html', 'popup.css', 'popup.js')}
                    if set(archive.namelist()) != expected:
                        raise RuntimeError('Bundled domestic capture resources are incomplete.')
                token = state['token']
                def post(path, body):
                    request = urllib.request.Request(url + path, data=json.dumps(body).encode(),
                        headers={'Content-Type': 'application/json', 'X-Workbench-Token': token})
                    with opener.open(request, timeout=15) as response:
                        return json.loads(response.read())
                # Exercise the frozen runtime's actual stock/operations code.
                import uuid
                def ops(action, **body):
                    return post('/api/ops/' + action, {'request_id': uuid.uuid4().hex, **body})
                pid = post('/api/import', {'products': [{'title_zh': '合成打包验收SKU',
                    'source_sku': 'BUNDLE-ONLY', 'facts': '本地合成验收，无经营资料'}]})['created'][0]
                warehouse = ops('entity', kind='warehouse', name='合成打包仓')['id']
                shop = ops('entity', kind='shop', name='合成打包店')['id']
                ops('adjust', product_id=pid, warehouse_id=warehouse, quantity=3, direction='in', reason='合成期初')
                order = ops('order', shop_id=shop, warehouse_id=warehouse, external_id='BUNDLE-ONLY-ORDER',
                    currency='SAR', lines=[{'product_id':pid, 'quantity':2, 'unit_price':'1.25'}])
                reserved = ops('reserve', id=order['id'], revision=order['revision'])
                ops('ship', id=reserved['id'], revision=reserved['revision'], carrier='Synthetic', tracking='BUNDLE-ONLY')
                inventory = json.loads(read('/api/state?surface=inventory'))['ops']['stock']
                balance = next(r for r in inventory if r['product_id'] == pid and r['warehouse_id'] == warehouse)
                if balance['on_hand'] != 1 or balance['reserved'] != 0:
                    raise RuntimeError('Frozen stock roundtrip violated quantity conservation.')
                # Generate only synthetic PNG bytes, then invoke the bundled FFmpeg.
                import struct, zlib
                def chunk(kind, data):
                    return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
                png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB',128,128,8,2,0,0,0))
                png += chunk(b'IDAT', zlib.compress((b'\0' + bytes([255,0,0])*128)*128)) + chunk(b'IEND', b'')
                upload = urllib.request.Request(url + '/api/media/upload', data=png,
                    headers={'Content-Type':'application/octet-stream', 'X-Workbench-Token':token,
                             'X-Media-Name':'synthetic-bundle.png', 'X-Media-Rights':'Synthetic packaging fixture'})
                with opener.open(upload, timeout=15) as response:
                    asset = json.loads(response.read())['id']
                task = post('/api/media/tasks', {'request_id':'bundle-video',
                    'recipes':[{'kind':'slideshow', 'asset_ids':[asset], 'seconds':1}]})['task_ids'][0]
                deadline = time.monotonic() + 60
                while True:
                    media = json.loads(read('/api/media/task-list'))
                    job = next(r for r in media['tasks'] if r['id'] == task)
                    if job['status'] in ('done', 'failed', 'interrupted'):
                        if job['status'] != 'done':
                            raise RuntimeError('Bundled FFmpeg slideshow failed.')
                        break
                    if time.monotonic() > deadline:
                        raise RuntimeError('Bundled FFmpeg slideshow timed out.')
                    time.sleep(0.1)
                try:
                    with opener.open(urllib.request.Request(url + '/api/replenishment/apply', data=b'{}',
                           headers={'Content-Type': 'application/json'}), timeout=15):
                        raise RuntimeError('Unauthenticated write was accepted.')
                except urllib.error.HTTPError as error:
                    if error.code != 403:
                        raise
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
    return {'bundled_backend_verified': True, 'module_surfaces': len(SURFACES),
            'domestic_extension_verified': True, 'stock_roundtrip_verified': True, 'bundled_ffmpeg_verified': True, 'real_noon_verified': False, 'native_window_verified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.app), ensure_ascii=False))


if __name__ == '__main__':
    main()
