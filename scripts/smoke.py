"""Actual isolated server boot, import, replay and restart; no external requests."""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'workbench'))
from core import Store


def start(data, ready):
    ready.unlink(missing_ok=True)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('NOON_', 'OPENAI_', 'TEXT_', 'IMAGE_HOST_'))}
    process = subprocess.Popen([sys.executable, str(ROOT / 'workbench/server.py'),
                                '--data', str(data), '--port', '0', '--ready-file', str(ready)],
                               env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for _ in range(200):
        if ready.exists():
            return process, json.loads(ready.read_text())['url']
        if process.poll() is not None:
            raise RuntimeError(process.stderr.read().decode())
        time.sleep(.05)
    stop(process)
    raise RuntimeError('Server boot exceeded 10 seconds')


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    if process.stderr:
        process.stderr.close()


def call(url, path, body=None, token=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['X-Workbench-Token'] = token
    request = urllib.request.Request(url + path,
        data=json.dumps(body).encode() if body is not None else None, headers=headers)
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def main():
    with tempfile.TemporaryDirectory(prefix='noonai-smoke-') as directory:
        data = Path(directory)
        ready = data / 'ready.json'
        process, url = start(data, ready)
        try:
            state = call(url, '/api/state')
            assert state['products'] == []
            assert state['automation']['scheduler_running']
            body = {'products': [{'title_zh': '云端隔离验收商品', 'source_sku': 'SMOKE-1',
                'source_url': 'https://detail.1688.com/offer/123.html',
                'supplier': '测试供应商', 'facts': '黑色，5件装。'}]}
            try:
                call(url, '/api/import', body)
            except urllib.error.HTTPError as error:
                assert error.code == 403
            else:
                raise AssertionError('Write without session token was accepted')
            first = call(url, '/api/import', body, state['token'])
            call(url, '/api/import', body, state['token'])
            assert len(call(url, '/api/state')['products']) == 1
            identifier = first['created'][0]
            store = Store(data)
            product = store.get(identifier)
            job_id = store.add_job(identifier, 'submit', product['revision'])
            with store.connect() as connection:
                connection.execute("UPDATE jobs SET status='running',result=? WHERE id=?",
                                   (json.dumps({'phase': 'submit_dispatching'}), job_id))
            second_ready = data / 'second-ready.json'
            second = subprocess.run([sys.executable, str(ROOT / 'workbench/server.py'),
                                     '--data', str(data), '--port', '0', '--ready-file', str(second_ready)],
                                    env={k: v for k, v in os.environ.items()
                                         if not k.startswith(('NOON_', 'OPENAI_', 'TEXT_', 'IMAGE_HOST_'))},
                                    capture_output=True, text=True, timeout=10)
            assert second.returncode != 0 and ('another instance' in second.stderr or '另一个实例打开' in second.stderr), second.stderr
            assert not second_ready.exists()
            with store.connect() as connection:
                status = connection.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()[0]
            assert status == 'running', 'a blocked second instance must not recover a live worker'
        finally:
            stop(process)
        process, url = start(data, ready)
        try:
            assert call(url, '/api/state')['products'][0]['id'] == identifier
            with Store(data).connect() as connection:
                status = connection.execute('SELECT status FROM jobs WHERE id=?', (job_id,)).fetchone()[0]
            assert status == 'interrupted', 'the next exclusive startup must recover the unverified write as uncertain'
        finally:
            stop(process)
    print('PASS isolated boot, scheduler, write guard, import deduplication, live-instance exclusion, uncertain-write restart recovery and persistence')


if __name__ == '__main__':
    main()
