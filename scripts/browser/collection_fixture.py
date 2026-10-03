"""Local browser fixture server. Never calls a supplier or seller account."""
import argparse
import json
import signal
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'workbench'))
from server import App, Handler, LocalHTTPServer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', required=True)
    parser.add_argument('--ready-file', required=True)
    args = parser.parse_args()
    app = App(args.data)
    server = LocalHTTPServer(('127.0.0.1', 0), Handler)
    server.app = app
    def stop(*_):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, stop)
    items = [{'external_id': 'fixture-black', 'title_zh': '合成黑色夹子',
              'source_url': 'https://example.com/products/clips', 'source_sku': 'BLACK',
              'supplier': '合成供应商', 'facts': '黑色；5件装', 'stock': 0,
              'source_currency': 'USD', 'source_price': 9.50, 'images': []},
             {'external_id': 'fixture-blue', 'title_zh': '合成蓝色夹子',
              'source_url': 'https://example.com/products/clips', 'source_sku': 'BLUE',
              'supplier': '合成供应商', 'facts': '蓝色；5件装', 'stock': 3,
              'source_currency': 'CNY', 'cost_cny': 5, 'images': []}]
    Path(args.ready_file).write_text(json.dumps({'url':f'http://127.0.0.1:{server.server_port}'}))
    try:
        def fetch_fixture(account, cursor=None, query='', limit=50):
            rows = [dict(item) for item in items]
            if query == 'changed synthetic clips':
                rows[0]['stock'] = 7
                rows[0]['facts'] = '黑色；10件装'
            return {'items': rows, 'next_cursor': None, 'warnings': []}
        with patch('channel_adapters.fetch_page', side_effect=fetch_fixture):
            server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.collection_executor.shutdown(wait=True, cancel_futures=True)
        app.source_inbox.close(); app.automation.close(); app.visual_checks.close()
        app.visuals.close(); app.media.close(); app.models.codex.close()
        app.executor.shutdown(wait=True)


if __name__ == '__main__':
    main()
