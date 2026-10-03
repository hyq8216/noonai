"""Synthetic recovery exercises; no account or supplier API is contacted."""
import itertools
import json
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import Problem
from recovery import Recovery, compatible_legacy_schema, schema
from server import App


class ChannelRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.app = App(self.root)
        self.recovery = self.app.recovery
        self.product_id = self.app.store.import_rows([{'title_zh': '合成恢复商品'}])['created'][0]

    def tearDown(self):
        self.app.models.codex.close()
        self.app.visuals.close()
        self.app.automation.close()
        self.app.media.close()
        if hasattr(self.app, 'collection_executor'):
            self.app.collection_executor.shutdown(wait=True, cancel_futures=True)
        self.app.executor.shutdown(wait=True, cancel_futures=True)
        self.tmp.cleanup()

    def test_expected_schema_contains_channel_tables(self):
        names = {row[1] for row in self.recovery.expected}
        self.assertTrue({'source_channel_accounts', 'source_collection_runs', 'source_collection_candidates'} <= names)

    def test_all_additive_table_combinations_are_compatible(self):
        tables = ('source_inbox_config', 'source_inbox_files', 'source_inbox_visual_config',
                  'source_inbox_video_config', 'model_terms', 'visual_budget', 'visual_presets',
                  'source_leads', 'source_channel_accounts', 'source_collection_runs',
                  'source_collection_candidates', 'source_collection_requests')
        expected = self.recovery.expected
        for mask in itertools.product((False, True), repeat=len(tables)):
            missing = {table for table, absent in zip(tables, mask) if absent}
            saved = [row for row in expected if row[2] not in missing]
            self.assertTrue(compatible_legacy_schema(saved, expected), missing)
        self.assertFalse(compatible_legacy_schema([], expected))
        changed = [list(row) for row in expected]
        changed[-1][3] += ' /* altered */'
        self.assertFalse(compatible_legacy_schema(changed, expected))

    def test_legacy_archives_migrate_from_real_saved_database(self):
        cases = [
            ('source_channel_accounts', 'source_collection_runs', 'source_collection_candidates', 'source_collection_requests'),
            ('source_collection_candidates',),
            ('source_channel_accounts', 'source_inbox_visual_config', 'model_terms', 'visual_presets'),
            ('source_collection_runs', 'source_inbox_config', 'source_inbox_files', 'visual_budget', 'source_leads'),
        ]
        for absent in cases:
            with self.subTest(absent=absent), tempfile.TemporaryDirectory() as old, tempfile.TemporaryDirectory() as extracted:
                old = Path(old)
                with sqlite3.connect(self.app.store.db) as source, sqlite3.connect(old / 'workbench.sqlite3') as target:
                    source.backup(target)
                with sqlite3.connect(old / 'workbench.sqlite3') as connection:
                    for table in absent:
                        connection.execute('DROP TABLE ' + table)
                    connection.execute('DROP INDEX idx_products_partner_sku')
                legacy = Recovery(old)
                archive = legacy.create()
                self.recovery.validate(legacy.archive_path(archive['id']), extracted)
                with sqlite3.connect(Path(extracted) / 'workbench.sqlite3') as connection:
                    self.assertEqual(schema(connection), self.recovery.expected)
                    self.assertEqual(connection.execute('SELECT id FROM products').fetchone()[0], self.product_id)

    def account(self):
        return self.app.channel_accounts.save({'provider': 'custom_json', 'name': '合成供货连接',
            'base_url': 'https://supplier.example.com/products', 'enabled': True,
            'config': {'currency': 'CNY'}, 'token': 'synthetic-backup-secret-marker'})

    def run_record(self, account, request, status='queued'):
        run = self.app.source_collection.create({'request_id': request, 'confirmed': True,
            'account_id': account['id'], 'query': '合成产品', 'page_limit': 2})
        with self.app.store.connect() as connection:
            connection.execute('UPDATE source_collection_runs SET status=? WHERE id=?', (status, run['id']))
        return run

    def test_roundtrip_preserves_provenance_receipts_and_excludes_credentials(self):
        account = self.account()
        run = self.run_record(account, 'synthetic-provenance', 'completed')
        with self.app.store.connect() as connection:
            self.app.source_collection.save_item(connection, run, {'external_id': 'synthetic-variant',
                'title_zh': '合成采集商品', 'source_url': 'https://supplier.example.com/item',
                'source_sku': 'synthetic-sku', 'facts': '合成黑色商品', 'source_currency': 'CNY',
                'source_price': 12})
            candidate_id = connection.execute('SELECT id FROM source_collection_candidates').fetchone()[0]
        preview = self.app.source_collection.preview({'candidate_ids': [candidate_id]})
        apply_body = {'request_id': 'synthetic-import', 'candidate_ids': [candidate_id],
            'preview_token': preview['token'], 'confirmed': True}
        receipt = self.app.source_collection.apply(apply_body)
        product_id = receipt['created'][0]
        queued = self.run_record(account, 'synthetic-queued')
        running = self.run_record(account, 'synthetic-running', 'running')
        cancelled = self.run_record(account, 'synthetic-cancelled', 'cancelled')
        with self.app.store.connect() as connection:
            before = dict(connection.execute('SELECT * FROM source_collection_candidates').fetchone())
        archive = self.recovery.create()
        with zipfile.ZipFile(self.recovery.archive_path(archive['id'])) as bundle:
            self.assertFalse(any('credentials' in name for name in bundle.namelist()))
            for name in bundle.namelist():
                self.assertNotIn(b'synthetic-backup-secret-marker', bundle.read(name), name)
        # A different workspace has no original credential files to supply these accounts.
        with tempfile.TemporaryDirectory() as destination:
            self.recovery.validate(self.recovery.archive_path(archive['id']), destination)
            destination = Path(destination)
            self.recovery.paused_copy(destination / 'workbench.sqlite3')
            self.assertFalse((destination / 'credentials').exists())
            with sqlite3.connect(destination / 'workbench.sqlite3') as connection:
                connection.row_factory = sqlite3.Row
                restored = dict(connection.execute('SELECT * FROM source_channel_accounts').fetchone())
                self.assertEqual(restored['enabled'], 0)
                self.assertEqual(restored['revision'], account['revision'] + 1)
                self.assertEqual(restored['provider'], account['provider'])
                self.assertEqual(json.loads(restored['config']), account['config'])
                self.assertEqual(dict(connection.execute('SELECT * FROM source_collection_candidates').fetchone()), before)
                self.assertEqual(before['product_id'], product_id)
                statuses = dict(connection.execute('SELECT id,status FROM source_collection_runs'))
                self.assertEqual(statuses[queued['id']], 'attention')
                self.assertEqual(statuses[running['id']], 'attention')
                self.assertEqual(statuses[run['id']], 'completed')
                self.assertEqual(statuses[cancelled['id']], 'cancelled')
                data = json.loads(connection.execute('SELECT data FROM products WHERE id=?', (product_id,)).fetchone()[0])
                self.assertEqual(data['source_collection']['id'], candidate_id)
                stored_receipt = json.loads(connection.execute('SELECT result FROM source_collection_requests').fetchone()[0])
                self.assertEqual(stored_receipt, receipt)
                snapshots = json.loads(before['snapshots'])
                self.assertEqual(snapshots[0]['account_revision'], account['revision'])

    def test_restore_schedule_rejects_active_reads_and_cancel_keeps_queued_reads_stopped(self):
        account = self.account()
        run = self.run_record(account, 'synthetic-schedule', 'running')
        archive = self.recovery.create()
        inspected = self.recovery.inspect(self.recovery.archive_path(archive['id']))
        with self.assertRaises(Problem):
            self.recovery.schedule({**inspected, 'confirmed': True})
        self.assertFalse(self.recovery.pending.exists())
        with self.app.store.connect() as connection:
            connection.execute("UPDATE source_collection_runs SET status='queued' WHERE id=?", (run['id'],))
        self.recovery.schedule({**inspected, 'confirmed': True})
        self.recovery.cancel()
        with self.app.store.connect() as connection:
            self.assertEqual(connection.execute('SELECT status FROM source_collection_runs').fetchone()[0], 'attention')
        # Even direct worker invocation cannot dispatch a stopped queue entry.
        self.assertEqual(self.app.source_collection.run(run['id'])['status'], 'attention')


if __name__ == '__main__':
    unittest.main()
