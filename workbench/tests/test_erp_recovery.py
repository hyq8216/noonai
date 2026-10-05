"""Actual legacy archive migration for all ERP modules, using synthetic data."""
import importlib
import json
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from recovery import Recovery, schema
from server import App

MODULES=('order_intake','settlement_intake','catalog_groups','collection_schedules',
         'backup_schedules','bank_reconciliation','fulfillment','procurement',
         'after_sales','alerts','batch_editor','inventory_counts','shipping_manifests',
         'pricing_plans','ad_analytics','import_profiles','domestic_capture',
         'supplier_quotes','fx_registry','replenishment')


class ERPRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.app=App(self.root)
        self.product=self.app.store.import_rows([{'title_zh':'合成ERP恢复商品','source_url':'https://example.com/restore',
            'source_sku':'RESTORE-1','supplier':'合成供应商','facts':'黑色；5件装'}])['created'][0]

    def tearDown(self):
        self.app.collection_schedules.close();self.app.backup_schedules.close()
        self.app.collection_executor.shutdown(wait=True,cancel_futures=True)
        self.app.source_inbox.close();self.app.automation.close()
        self.app.visual_checks.close();self.app.visuals.close();self.app.media.close()
        self.app.models.codex.close();self.app.executor.shutdown(wait=True,cancel_futures=True)
        self.tmp.cleanup()

    def test_archive_missing_every_new_module_migrates_to_exact_schema(self):
        with tempfile.TemporaryDirectory() as old,tempfile.TemporaryDirectory() as restored:
            old=Path(old);restored=Path(restored)
            with self.app.store.connect() as source,sqlite3.connect(old/'workbench.sqlite3') as target:
                source.backup(target)
            with sqlite3.connect(':memory:') as module_db:
                # The manifest module indexes the existing movement ledger.
                module_db.execute('CREATE TABLE ops_movements(product_id TEXT,warehouse_id TEXT)')
                for name in MODULES:module_db.executescript(importlib.import_module(name).SCHEMA_SQL)
                tables=[row[1] for row in schema(module_db) if row[0]=='table' and row[1]!='ops_movements']
            with sqlite3.connect(old/'workbench.sqlite3') as connection:
                for name in tables:connection.execute('DROP TABLE '+name)
            legacy=Recovery(old);archive=legacy.create()
            self.app.recovery.validate(legacy.archive_path(archive['id']),restored)
            with sqlite3.connect(restored/'workbench.sqlite3') as connection:
                self.assertEqual(schema(connection),self.app.recovery.expected)
                self.assertEqual(connection.execute('SELECT count(*) FROM products').fetchone()[0],1)
                self.assertEqual(connection.execute('SELECT enabled FROM backup_schedule_config').fetchone()[0],0)

    def test_current_archive_preserves_paid_bank_receipt_and_local_group_without_credentials(self):
        entry=self.app.finance.transact('entry',{'kind':'expense','category':'other','currency':'CNY',
            'amount':'25.00','date':'2026-10-03','fx':'1','evidence':'合成费用第1行',
            'evidence_key':'erp-restore-expense','request_id':'erp-restore-expense-request'})
        preview=self.app.bank_reconciliation.preview({'format':'json','content':json.dumps([
            {'account_name':'合成账户','bank_reference':'SYNTHETIC-RESTORE-1','currency':'CNY','direction':'out',
             'amount':'25.00','date':'2026-10-03','fx':'1','evidence':'合成银行第1行'}])})
        self.app.bank_reconciliation.import_rows({'batch_id':preview['id'],'token':preview['token'],
            'confirmed':True,'request_id':'erp-restore-bank-import'})
        row=self.app.bank_reconciliation.state()['batches'][0]['rows'][0]
        request={'row_id':row['id'],'row_token':row['row_token'],'entry_id':entry['id'],
                 'entry_revision':entry['revision'],'confirmed':True,'request_id':'erp-restore-bank-match'}
        self.app.bank_reconciliation.match(request)
        product=self.app.store.get(self.product)
        group={'name':'合成恢复规格组','axes':['颜色'],'members':[
            {'product_id':product['id'],'revision':product['revision'],'values':{'颜色':'黑色'}}]}
        group_preview=self.app.catalog_groups.preview(group)
        self.app.catalog_groups.save({**group,'preview_digest':group_preview['preview_digest'],
            'confirmed':True,'request_id':'erp-restore-group'})
        account=self.app.channel_accounts.save({'provider':'custom_json','name':'合成授权源',
            'base_url':'https://example.com/catalog','enabled':True,'config':{},'token':'synthetic-erp-restore-secret'})
        archive=self.app.recovery.create()
        path=self.app.recovery.archive_path(archive['id'])
        with zipfile.ZipFile(path) as bundle:
            self.assertFalse(any(name.startswith('credentials/') for name in bundle.namelist()))
            self.assertFalse(any(b'synthetic-erp-restore-secret' in bundle.read(name) for name in bundle.namelist()))
        with tempfile.TemporaryDirectory() as restored:
            self.app.recovery.validate(path,restored)
            db=Path(restored)/'workbench.sqlite3';self.app.recovery.paused_copy(db)
            with sqlite3.connect(db) as connection:
                self.assertEqual(connection.execute('SELECT count(*) FROM finance_payments').fetchone()[0],1)
                self.assertEqual(connection.execute('SELECT count(*) FROM bank_requests').fetchone()[0],2)
                self.assertEqual(connection.execute('SELECT count(*) FROM catalog_groups').fetchone()[0],1)
                self.assertEqual(connection.execute('SELECT enabled FROM source_channel_accounts WHERE id=?',(account['id'],)).fetchone()[0],0)
                receipts=[json.loads(row[0]) for row in connection.execute('SELECT result FROM bank_requests')]
                self.assertTrue(any(receipt.get('payment', {}).get('id') for receipt in receipts))


if __name__=='__main__':unittest.main()
