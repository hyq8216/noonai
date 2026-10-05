import json
import sqlite3
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from channel_accounts import ChannelAccounts, validate_base_url
from core import Problem, Store


class ChannelAccountTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name))
        self.accounts = ChannelAccounts(self.store)

    def tearDown(self):
        self.temporary.cleanup()

    def body(self, **extra):
        return dict(provider='custom_json', name='Synthetic supplier',
                    base_url='https://supplier.example.com/products', enabled=True,
                    config={'items_path': 'items'}, **extra)

    def test_registry_honestly_distinguishes_available_and_planned(self):
        providers = self.accounts.state()['providers']
        self.assertEqual({p['id'] for p in providers}, {'1688', 'alibaba', 'taobao', 'tmall', 'jd', 'pinduoduo', 'aliexpress', 'amazon', 'ebay', 'shopify', 'custom_json', 'supplier_file'})
        self.assertEqual({p['id'] for p in providers if p['adapter'] in ('shopify', 'ebay', 'json_api')}, {'shopify', 'ebay', 'custom_json'})
        self.assertTrue(all(p['authorization'] and p['limitations'] for p in providers))

    def test_secret_separate_private_and_never_public_or_database(self):
        secret = 'synthetic-credential-only'
        account = self.accounts.save(self.body(token=secret))
        self.assertNotIn(secret, json.dumps(account))
        self.assertNotIn(secret, json.dumps(self.accounts.state()))
        self.assertNotIn('token', self.accounts.get(account['id']))
        self.assertTrue(account['has_token'])
        self.assertEqual(account['status'], 'configured')
        self.assertEqual(self.accounts.get(account['id'], with_secret=True)['token'], secret)
        path = self.accounts.credentials / (account['id'] + '.json')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(path.parent.parent.stat().st_mode & 0o777, 0o700)
        with self.store.connect() as connection:
            self.assertNotIn(secret, '\n'.join(connection.iterdump()))
        for dbfile in self.store.root.glob('workbench.sqlite3*'):
            self.assertNotIn(secret.encode(), dbfile.read_bytes())
        self.assertEqual(ChannelAccounts(self.store).get(account['id'])['revision'], 1)

    def test_version_conflict_blank_preserves_clear_explicit_and_remove(self):
        first = self.accounts.save(self.body(token='synthetic-old'))
        second = self.accounts.save({**first, 'name': 'New name', 'token': ''})
        self.assertEqual(second['revision'], 2)
        self.assertEqual(self.accounts.get(first['id'], with_secret=True)['token'], 'synthetic-old')
        for payload in ({**first, 'token': 'synthetic-stale'}, {**second, 'revision': True}, {k: v for k, v in second.items() if k != 'revision'}):
            with self.assertRaises(Problem) as error:
                self.accounts.save(payload)
            self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.accounts.get(first['id'], with_secret=True)['token'], 'synthetic-old')
        third = self.accounts.save({**second, 'clear_token': True})
        self.assertFalse(third['has_token'])
        fourth = self.accounts.save({**third, 'token': 'synthetic-restored'})
        self.assertEqual(self.accounts.remove({'id': fourth['id'], 'revision': 4}), {'removed': fourth['id']})
        self.assertFalse((self.accounts.credentials / (fourth['id'] + '.json')).exists())
        self.assertEqual(self.accounts.state()['accounts'], [])

    def test_invalid_config_does_not_persist_anything(self):
        invalid = [{'enabled': 'yes'}, {'provider': 'unknown'}, {'name': ''}, {'config': []},
                   {'config': {'api_key': 'synthetic'}}, {'config': {'nested': {'access_token': 'synthetic'}}},
                   {'config': {'x': float('nan')}}, {'config': {'x': 'a' * 33000}},
                   {'config': {'api_version': 'latest'}}, {'config': {'marketplace_id': []}},
                   {'config': {'items_path': 10}}, {'config': {'field_mapping': {'title_zh': []}}},
                   {'config': {'headers': {'X-Key': 'synthetic'}}},
                   {'token': '\r\nsecret'}, {'clear_token': 'yes'}, {'token': 'x', 'clear_token': True}]
        for values in invalid:
            with self.subTest(values=values):
                with self.assertRaises(Problem):
                    self.accounts.save({**self.body(), **values})
        self.assertEqual(self.accounts.state()['accounts'], [])
        self.assertEqual(list(self.accounts.credentials.iterdir()), [])

    def test_provider_domains_and_private_hosts(self):
        bad = ['http://supplier.example.com', 'https://localhost', 'https://x.local',
               'https://127.0.0.1', 'https://10.0.0.1', 'https://[::1]', 'https://169.254.169.254',
               'https://user:password@example.com', 'https://example.com/#fragment',
               'https://example.com/?token=secret', 'https://example.com:8080',
               'https://2130706433', 'https://127.1', 'https://example.com\\@localhost']
        for url in bad:
            with self.subTest(url=url), self.assertRaises(Problem):
                validate_base_url('custom_json', url)
        self.assertEqual(validate_base_url('shopify', 'https://synthetic-shop.myshopify.com/'), 'https://synthetic-shop.myshopify.com')
        self.assertEqual(validate_base_url('ebay', 'https://api.ebay.com'), 'https://api.ebay.com')
        for provider, url in [('shopify', 'https://myshopify.com'), ('shopify', 'https://shop.myshopify.com.evil.com'), ('ebay', 'https://api.ebay.com.evil.com')]:
            with self.subTest(provider=provider, url=url), self.assertRaises(Problem):
                validate_base_url(provider, url)

    def test_sql_failure_restores_secret_and_revision(self):
        account = self.accounts.save(self.body(token='synthetic-original'))
        with self.store.connect() as connection:
            connection.executescript("CREATE TRIGGER fail_account_update BEFORE UPDATE ON source_channel_accounts BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END;")
        with self.assertRaises(sqlite3.IntegrityError):
            self.accounts.save({**account, 'token': 'synthetic-replacement'})
        self.assertEqual(self.accounts.get(account['id'], with_secret=True)['token'], 'synthetic-original')
        self.assertEqual(self.accounts.get(account['id'])['revision'], 1)
        with self.store.connect() as connection:
            connection.executescript("CREATE TRIGGER fail_account_delete BEFORE DELETE ON source_channel_accounts BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END;")
        with self.assertRaises(sqlite3.IntegrityError):
            self.accounts.remove({'id': account['id'], 'revision': 1})
        self.assertEqual(self.accounts.get(account['id'], with_secret=True)['token'], 'synthetic-original')

    def test_atomic_secret_failure_leaves_database_unchanged(self):
        account = self.accounts.save(self.body(token='synthetic-original'))
        with patch('channel_accounts.os.replace', side_effect=OSError('synthetic disk failure')):
            with self.assertRaises(OSError):
                self.accounts.save({**account, 'token': 'synthetic-replacement'})
        self.assertEqual(self.accounts.get(account['id'], with_secret=True)['token'], 'synthetic-original')
        self.assertEqual(self.accounts.get(account['id'])['revision'], 1)
        self.assertEqual(len(list(self.accounts.credentials.iterdir())), 1)

    def test_new_account_sql_failure_removes_uncommitted_secret(self):
        with self.store.connect() as connection:
            connection.executescript("CREATE TRIGGER fail_account_insert BEFORE INSERT ON source_channel_accounts BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END;")
        with self.assertRaises(sqlite3.IntegrityError):
            self.accounts.save(self.body(token='synthetic-uncommitted'))
        self.assertEqual(self.accounts.state()['accounts'], [])
        self.assertEqual(list(self.accounts.credentials.iterdir()), [])

    def test_competing_services_accept_one_revision_only(self):
        account = self.accounts.save(self.body(token='synthetic-original'))
        other = ChannelAccounts(self.store)
        def update(service):
            try:
                return service.save({**account, 'token': 'synthetic-' + str(id(service))})
            except Problem as error:
                return error.status
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(update, [self.accounts, other]))
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertIn(409, results)
        self.assertEqual(self.accounts.get(account['id'])['revision'], 2)

    def test_endpoint_change_requires_explicit_token_replacement_or_clear(self):
        account = self.accounts.save(self.body(token='synthetic-original'))
        path = self.accounts.credentials / (account['id'] + '.json')
        saved_secret = path.read_bytes()
        for url in ('https://other.example.com/products', 'https://supplier.example.com/other'):
            with self.subTest(url=url), self.assertRaises(Problem) as error:
                self.accounts.save({**account, 'base_url': url, 'token': ''})
            self.assertEqual(error.exception.status, 409)
            self.assertEqual(self.accounts.get(account['id']), account)
            self.assertEqual(path.read_bytes(), saved_secret)
        changed = self.accounts.save({**account, 'base_url': 'https://other.example.com/products', 'token': 'synthetic-other'})
        self.assertEqual(self.accounts.get(account['id'], with_secret=True)['token'], 'synthetic-other')
        cleared = self.accounts.save({**changed, 'base_url': 'https://third.example.com/products', 'clear_token': True})
        self.assertFalse(cleared['has_token'])
        self.assertEqual(self.accounts.get(account['id'], with_secret=True)['token'], '')

    def test_interrupted_destination_binding_blocks_secret_access(self):
        account = self.accounts.save(self.body(token='synthetic-original'))
        path = self.accounts.credentials / (account['id'] + '.json')
        saved = json.loads(path.read_bytes())
        self.assertEqual(saved['provider'], account['provider'])
        self.assertEqual(saved['base_url'], account['base_url'])
        # Simulate replacement reaching disk while the SQL destination update rolled back.
        path.write_text(json.dumps({**saved, 'base_url': 'https://other.example.com/products', 'token': 'synthetic-other'}))
        public = self.accounts.get(account['id'])
        self.assertEqual(public['status'], 'attention')
        self.assertNotIn('synthetic-other', json.dumps(self.accounts.state()))
        with self.assertRaises(Problem) as error:
            self.accounts.get(account['id'], with_secret=True)
        self.assertEqual(error.exception.status, 409)
        with self.assertRaises(Problem):
            self.accounts.save({**public, 'token': ''})
        repaired = self.accounts.save({**public, 'token': 'synthetic-repaired'})
        self.assertEqual(repaired['status'], 'configured')
        self.assertEqual(self.accounts.get(account['id'], with_secret=True)['token'], 'synthetic-repaired')

    def test_unbound_legacy_or_wrong_account_credentials_cannot_be_forwarded(self):
        account = self.accounts.save(self.body(token='synthetic-original'))
        path = self.accounts.credentials / (account['id'] + '.json')
        original = json.loads(path.read_bytes())
        for secret in ({'token': 'synthetic-legacy'}, {**original, 'account_id': 'other'}, {**original, 'provider': 'ebay'}):
            path.write_text(json.dumps(secret))
            self.assertEqual(self.accounts.get(account['id'])['status'], 'attention')
            with self.assertRaises(Problem):
                self.accounts.get(account['id'], with_secret=True)
        repaired = self.accounts.save({**account, 'clear_token': True})
        self.assertFalse(repaired['has_token'])

    def test_no_token_claims_connected_and_ids_cannot_escape(self):
        record = self.accounts.save({**self.body(), 'provider': '1688', 'base_url': '', 'token': 'synthetic'})
        self.assertEqual(record['status'], 'authorization_required')
        with self.assertRaises(Problem):
            self.accounts.get('../../escape')
        path = self.accounts.credentials / (record['id'] + '.json')
        path.unlink()
        path.symlink_to(self.store.db)
        with self.assertRaises(Problem):
            self.accounts.get(record['id'])


if __name__ == '__main__':
    unittest.main()
