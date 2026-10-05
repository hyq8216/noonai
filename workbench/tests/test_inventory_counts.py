import csv
import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Store, Problem
from operations import Operations
from inventory_counts import InventoryCounts
from after_sales import SCHEMA_SQL as QUARANTINE_SQL


class InventoryCountsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name);self.ops=Operations(self.store)
        self.app=SimpleNamespace(store=self.store,ops=self.ops);self.counts=InventoryCounts(self.app)
        self.ids=self.store.import_rows([{'title_zh':'盒子A','stock':999},{'title_zh':'盒子B','stock':None}])['created']
        self.warehouse=self.ops.transact('entity',{'kind':'warehouse','name':'良品仓','request_id':'warehouse'})
        self.other=self.ops.transact('entity',{'kind':'warehouse','name':'调出仓','request_id':'other'})
        self.shop=self.ops.transact('entity',{'kind':'shop','name':'店铺','request_id':'shop'})
        self.ops.transact('adjust',{'product_id':self.ids[0],'warehouse_id':self.warehouse['id'],'quantity':10,'direction':'in','reason':'测试初始库存','request_id':'stock'})
    def tearDown(self):self.tmp.cleanup()
    def body(self, **extra):
        body={**dict(warehouse_id=self.warehouse['id'],evidence='操作员甲；合成实盘记录2026-10-03；良品范围',rows=[{'product_id':self.ids[0],'counted_quantity':13}]),**extra}
        if 'csv' in extra:body.pop('rows',None)
        return body
    def apply(self,body=None,key='apply'):
        body=body or self.body();preview=self.counts.preview(body)
        return self.counts.apply({**body,'preview_token':preview['token'],'request_id':key,'confirmed':True})
    def balance(self,pid=None,wid=None):
        with self.store.connect() as c:
            row=c.execute('SELECT * FROM ops_stock WHERE product_id=? AND warehouse_id=?',(pid or self.ids[0],wid or self.warehouse['id'])).fetchone()
            return dict(row) if row else None
    def test_difference_adjusts_only_ledger_not_product_source_stock(self):
        out=self.apply();self.assertEqual(self.balance()['on_hand'],13)
        self.assertEqual(self.store.get(self.ids[0])['stock'],999)
        self.assertEqual(self.store.get(self.ids[0])['revision'],1)
        doc=out['document'];self.assertEqual(doc['lines'][0]['delta'],3)
        with self.store.connect() as c:
            movement=c.execute('SELECT * FROM ops_movements WHERE reference=?',(doc['id'],)).fetchone()
            self.assertEqual(movement['delta'],3);self.assertEqual(movement['reserved_delta'],0)
            self.assertEqual(c.execute('SELECT count(*) FROM inventory_count_audit').fetchone()[0],1)
    def test_count_zero_explicit_loss_and_unknown_not_assumed_zero(self):
        state=self.counts.state(warehouse_id=self.warehouse['id'])
        unknown=next(r for r in state['rows'] if r['product_id']==self.ids[1])
        self.assertIsNone(unknown['on_hand']);self.assertIsNone(unknown['available'])
        body=self.body(rows=[{'product_id':self.ids[0],'counted_quantity':0},{'product_id':self.ids[1],'counted_quantity':0}])
        preview=self.counts.preview(body)
        opening=next(r for r in preview['rows'] if r['product_id']==self.ids[1])
        self.assertEqual(opening['status'],'opening');self.assertIsNone(opening['on_hand'])
        self.apply(body)
        self.assertEqual(self.balance()['on_hand'],0)
        self.assertEqual(self.balance(self.ids[1])['on_hand'],0)
    def test_blank_null_negative_fraction_and_boolean_block_whole_batch(self):
        for value in ('',None,-1,1.5,True,'NaN'):
            with self.subTest(value=value):
                body=self.body(rows=[{'product_id':self.ids[0],'counted_quantity':12},{'product_id':self.ids[1],'counted_quantity':value}])
                preview=self.counts.preview(body);self.assertEqual(preview['blocked'],1)
                with self.assertRaises(Problem):self.apply(body)
                self.assertEqual(self.balance()['on_hand'],10);self.assertIsNone(self.balance(self.ids[1]))
    def test_reserved_orders_preserved_count_below_held_blocks(self):
        order=self.ops.transact('order',{'shop_id':self.shop['id'],'warehouse_id':self.warehouse['id'],'external_id':'O1','currency':'SAR','lines':[{'product_id':self.ids[0],'quantity':4,'unit_price':2}],'request_id':'order'})
        self.ops.transact('reserve',{'id':order['id'],'revision':order['revision'],'request_id':'reserve'})
        bad=self.body(rows=[{'product_id':self.ids[0],'counted_quantity':3}])
        self.assertEqual(self.counts.preview(bad)['blocked'],1)
        with self.assertRaises(Problem):self.apply(bad)
        self.apply(self.body(rows=[{'product_id':self.ids[0],'counted_quantity':5}]))
        self.assertEqual((self.balance()['on_hand'],self.balance()['reserved']),(5,4))
    def test_quarantine_and_transfer_not_added_to_good_count(self):
        with self.store.connect() as c:
            c.executescript(QUARANTINE_SQL)
            c.execute('INSERT INTO after_sales_quarantine(case_id,product_id,warehouse_id,received) VALUES(?,?,?,?)',('case',self.ids[0],self.warehouse['id'],3))
        self.ops.transact('adjust',{'product_id':self.ids[0],'warehouse_id':self.other['id'],'quantity':5,'direction':'in','reason':'调拨来源','request_id':'source'})
        transfer=self.ops.transact('transfer',{'source_warehouse_id':self.other['id'],'warehouse_id':self.warehouse['id'],'lines':[{'product_id':self.ids[0],'quantity':5}],'note':'调拨依据','request_id':'transfer'})
        self.ops.transact('transfer_dispatch',{'id':transfer['id'],'revision':transfer['revision'],'evidence':'已调出','request_id':'dispatch'})
        preview=self.counts.preview(self.body());row=preview['rows'][0]
        self.assertEqual((row['quarantined'],row['transfer_incoming'],row['delta']),(3,5,3))
        self.apply();self.assertEqual(self.balance()['on_hand'],13)
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT received FROM after_sales_quarantine').fetchone()[0],3)
    def test_stock_and_reserved_change_invalidates_preview(self):
        body=self.body();pre=self.counts.preview(body)
        self.ops.transact('adjust',{'product_id':self.ids[0],'warehouse_id':self.warehouse['id'],'quantity':1,'direction':'in','reason':'其他收货','request_id':'concurrent'})
        with self.assertRaises(Problem) as caught:self.counts.apply({**body,'preview_token':pre['token'],'request_id':'apply','confirmed':True})
        self.assertEqual(caught.exception.status,409);self.assertEqual(self.balance()['on_hand'],11)
    def test_changed_then_restored_balance_still_invalidates_token(self):
        body=self.body();pre=self.counts.preview(body)
        for direction in ('in','out'):
            self.ops.transact('adjust',{'product_id':self.ids[0],'warehouse_id':self.warehouse['id'],'quantity':1,'direction':direction,'reason':'并发流水','request_id':direction})
        self.assertEqual(self.balance()['on_hand'],10)
        with self.assertRaises(Problem):self.counts.apply({**body,'preview_token':pre['token'],'request_id':'apply','confirmed':True})
    def test_product_warehouse_and_quarantine_basis_changes_stale(self):
        for change in ('product','warehouse','quarantine'):
            with self.subTest(change=change):
                body=self.body();pre=self.counts.preview(body)
                if change=='product':self.store.update(self.ids[0],{'facts':'规格新资料'},self.store.get(self.ids[0])['revision'])
                elif change=='warehouse':
                    with self.store.connect() as c:c.execute('UPDATE ops_entities SET name=? WHERE id=?',('新仓名',self.warehouse['id']))
                else:
                    with self.store.connect() as c:
                        c.executescript(QUARANTINE_SQL)
                        c.execute('INSERT INTO after_sales_quarantine(case_id,product_id,warehouse_id,received) VALUES(?,?,?,?)',('case',self.ids[0],self.warehouse['id'],2))
                with self.assertRaises(Problem):self.counts.apply({**body,'preview_token':pre['token'],'request_id':change,'confirmed':True})
        self.assertEqual(self.balance()['on_hand'],10)
    def test_missing_duplicate_demo_and_wrong_csv_warehouse_block(self):
        demo=self.store.import_rows([{'title_zh':'示例'}],demo=True)['created'][0]
        cases=[self.body(rows=[{'product_id':'missing','counted_quantity':1}]),self.body(rows=[{'product_id':demo,'counted_quantity':1}]),self.body(rows=[{'product_id':self.ids[0],'counted_quantity':1}]*2),self.body(csv='warehouse_id,product_id,counted_quantity\nwrong,'+self.ids[0]+',1\n')]
        for body in cases:
            with self.subTest(body=body):
                self.assertGreater(self.counts.preview(body)['blocked'],0)
                with self.assertRaises(Problem):self.apply(body)
    def test_atomic_mid_batch_failure_rolls_back_opening_documents_audit_receipt(self):
        body=self.body(rows=[{'product_id':self.ids[0],'counted_quantity':13},{'product_id':self.ids[1],'counted_quantity':7}])
        original=self.ops.move;calls=0
        def interrupted(*args,**kwargs):
            nonlocal calls
            calls+=1;original(*args,**kwargs)
            if calls==2:raise Problem('写入被中断')
        with patch.object(self.ops,'move',side_effect=interrupted),self.assertRaises(Problem):self.apply(body)
        self.assertEqual(self.balance()['on_hand'],10);self.assertIsNone(self.balance(self.ids[1]))
        with self.store.connect() as c:
            for table in ('inventory_count_documents','inventory_count_audit','inventory_count_requests'):self.assertEqual(c.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM ops_movements').fetchone()[0],1)
    def test_durable_receipt_replays_before_live_stock_checks(self):
        body=self.body();pre=self.counts.preview(body)
        payload={**body,'preview_token':pre['token'],'request_id':'durable','confirmed':True}
        first=self.counts.apply(payload)
        self.ops.transact('adjust',{'product_id':self.ids[0],'warehouse_id':self.warehouse['id'],'quantity':1,'direction':'in','reason':'后续库存','request_id':'later'})
        second=InventoryCounts(self.app).apply(payload)
        self.assertTrue(second['replayed']);self.assertEqual(second['document']['id'],first['document']['id'])
        self.assertEqual(self.balance()['on_hand'],14)
        with self.assertRaises(Problem):self.counts.apply({**payload,'evidence':'不同依据'})
    def test_concurrent_same_request_records_one_difference(self):
        body=self.body();pre=self.counts.preview(body);payload={**body,'preview_token':pre['token'],'request_id':'parallel','confirmed':True}
        results=[];errors=[]
        def run():
            try:results.append(self.counts.apply(payload))
            except Exception as error:errors.append(error)
        threads=[threading.Thread(target=run) for unused in range(4)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertEqual(errors,[]);self.assertEqual(len(results),4);self.assertEqual(sum(not r['replayed'] for r in results),1)
        self.assertEqual(self.balance()['on_hand'],13)
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM inventory_count_documents').fetchone()[0],1)
    def test_preview_persists_and_can_apply_after_restart(self):
        body=self.body();pre=self.counts.preview(body)
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM inventory_count_previews').fetchone()[0],1)
        result=InventoryCounts(self.app).apply({**body,'preview_token':pre['token'],'request_id':'restarted','confirmed':True})
        self.assertEqual(result['document']['status'],'confirmed')
    def test_confirmation_and_evidence_required(self):
        with self.assertRaises(Problem):self.counts.preview(self.body(evidence=''))
        body=self.body();pre=self.counts.preview(body)
        with self.assertRaises(Problem):self.counts.apply({**body,'preview_token':pre['token'],'request_id':'no_confirm'})
    def test_csv_blank_template_formula_safe_bom_and_roundtrip(self):
        pid=self.ids[0];self.store.update(pid,{'title_zh':'=恶意公式'},1)
        raw=self.counts.template(self.warehouse['id']);self.assertTrue(raw.startswith(b'\xef\xbb\xbf'))
        records=list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
        self.assertTrue(next(r for r in records if r['product_id']==pid)['title'].startswith("'="))
        self.assertTrue(all(r['counted_quantity']=='' for r in records))
        body=self.body(csv='partner_sku,counted_quantity\n'+self.store.get(pid)['partner_sku']+',8\n')
        out=self.apply(body)
        exported=self.counts.export(out['document']['id'])
        self.assertTrue(exported.startswith(b'\xef\xbb\xbf'))
        values=list(csv.DictReader(io.StringIO(exported.decode('utf-8-sig'))))
        self.assertEqual(values[0]['商品'],"'=恶意公式")
        self.assertEqual(values[0]['调整差量'],"'-2")
        self.assertEqual(values[0]['实盘良品在库'],'8')
    def test_csv_blank_rows_do_not_wipe_ledger(self):
        body=self.body(csv='product_id,counted_quantity\n'+self.ids[0]+',\n')
        self.assertEqual(self.counts.preview(body)['blocked'],1)
        with self.assertRaises(Problem):self.apply(body)
        self.assertEqual(self.balance()['on_hand'],10)
    def test_pagination_no_whole_product_list_and_500_limit(self):
        self.store.import_rows([{'title_zh':'分页商品'+str(i)} for i in range(100)])
        with patch.object(self.store,'list',side_effect=AssertionError('不能整库读取')):
            first=self.counts.state(0,self.warehouse['id']);second=self.counts.state(1,self.warehouse['id']);last=self.counts.state(2,self.warehouse['id'])
        self.assertEqual((len(first['rows']),len(second['rows']),len(last['rows'])),(50,50,2))
        self.assertFalse(set(r['product_id'] for r in first['rows'])&set(r['product_id'] for r in second['rows']))
        self.assertEqual(self.counts.state(0,self.warehouse['id'],'%')['total'],0)
        body=self.body(rows=[{'product_id':'missing'+str(i),'counted_quantity':1} for i in range(500)])
        self.assertEqual(len(self.counts.preview(body)['rows']),500)
        with self.assertRaises(Problem):self.counts.preview({**body,'rows':body['rows']+[{'product_id':'x','counted_quantity':1}]})
    def test_same_quantity_records_observation_without_stock_movement(self):
        self.apply(self.body(rows=[{'product_id':self.ids[0],'counted_quantity':10}]))
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM ops_movements').fetchone()[0],1)
        self.assertEqual(self.counts.state()['documents'][0]['unchanged'],1)


if __name__=='__main__':unittest.main()
