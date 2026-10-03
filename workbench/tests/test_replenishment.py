import concurrent.futures
import csv
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Store, Problem, ident
from operations import Operations
from procurement import Procurement
from replenishment import Replenishment


class ReplenishmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name);self.ops=Operations(self.store)
        self.r=Replenishment(SimpleNamespace(store=self.store));self.proc=Procurement(SimpleNamespace(store=self.store))
        self.pid=self.store.import_rows([{'title_zh':'=补货测试','source_sku':'R-A'}])['created'][0]
        self.wh=self.op('entity',kind='warehouse',name='测试仓')['id']
        self.supplier=self.op('entity',kind='supplier',name='测试供应商')['id']
    def tearDown(self):self.tmp.cleanup()
    def op(self,action,**b):return self.ops.transact(action,{'request_id':ident(),**b})
    def stock(self,n=10):return self.op('adjust',product_id=self.pid,warehouse_id=self.wh,quantity=n,direction='in',reason='专用合成入库')
    def purchase(self,n=8):return self.op('purchase',supplier_id=self.supplier,warehouse_id=self.wh,lines=[{'product_id':self.pid,'quantity':n,'unit_price':'2'}])
    def row(self):return self.r.state({'warehouse_id':self.wh})['rows'][0]
    def body(self,**kw):return {'product_id':self.pid,'warehouse_id':self.wh,'revision':0,'minimum':12,'target':30,'lead_days':7,**kw}
    def save(self,**kw):return self.r.apply({**self.r.preview(self.body(**kw)),'confirmed':True,'request_id':ident()})

    def test_unknown_parameters_and_unopened_stock_do_not_invent_zero(self):
        self.assertIsNone(self.row()['available']);self.assertIsNone(self.row()['quantity'])
        self.save();self.assertIsNone(self.row()['quantity'])
        self.stock();self.assertEqual(self.row()['quantity'],20)
        self.save(revision=1,target=None);self.assertIsNone(self.row()['quantity']);self.assertIsNone(self.row()['target'])
        self.save(revision=2,target=30,lead_days='');self.assertIsNone(self.row()['quantity'])

    def test_actual_purchase_remaining_reserved_and_links_count_once(self):
        self.stock();p=self.purchase();shop=self.op('entity',kind='shop',name='合成店')['id']
        o=self.op('order',shop_id=shop,warehouse_id=self.wh,external_id=ident(),currency='SAR',lines=[{'product_id':self.pid,'quantity':4,'unit_price':10}])
        self.op('reserve',id=o['id'],revision=o['revision'])
        b={'order_id':o['id'],'purchase_id':p['id'],'product_id':self.pid,'quantity':4,'order_revision':2,'purchase_revision':1,'link_revision':0}
        self.proc.apply({**self.proc.preview(b),'confirmed':True,'request_id':ident()})
        self.save();r=self.row();self.assertEqual((r['on_hand'],r['reserved'],r['available'],r['purchase_pending'],r['quantity']),(10,4,6,8,16))
        before=r['source_version'];p=self.op('receive',id=p['id'],revision=p['revision'],quantities={self.pid:3})
        r=self.row();self.assertEqual((r['available'],r['purchase_pending'],r['quantity']),(9,5,16));self.assertNotEqual(before,r['source_version'])
        self.op('cancel',id=p['id'],revision=p['revision'],reason='合成取消')
        r=self.row();self.assertEqual((r['on_hand'],r['purchase_pending'],r['quantity']),(13,0,21));self.assertEqual(r['purchases'][0]['received'],3)

    def test_confirmation_idempotency_collision_and_stale_sources(self):
        self.stock();v=self.r.preview(self.body());b={**v,'confirmed':True,'request_id':ident()}
        with self.assertRaises(Problem):self.r.apply({**b,'confirmed':False})
        result=self.r.apply(b);self.assertEqual(self.r.apply(b),result)
        with self.assertRaises(Problem):self.r.apply({**b,'target':31})
        with self.assertRaises(Problem):self.r.apply({**b,'request_id':ident()})
        v=self.r.preview(self.body(revision=1));self.purchase()
        with self.assertRaises(Problem):self.r.apply({**v,'confirmed':True,'request_id':ident()})
        v=self.r.preview(self.body(revision=1));self.stock(1)
        with self.assertRaises(Problem):self.r.apply({**v,'confirmed':True,'request_id':ident()})
        self.assertEqual(self.row()['revision'],1)

    def test_stock_round_trip_invalidates_preview_even_when_balance_same(self):
        self.stock();v=self.r.preview(self.body());self.stock(1)
        self.op('adjust',product_id=self.pid,warehouse_id=self.wh,quantity=1,direction='out',reason='合成往返')
        self.assertEqual(self.row()['on_hand'],10)
        with self.assertRaises(Problem):self.r.apply({**v,'confirmed':True,'request_id':ident()})

    def test_concurrent_confirms_only_one_revision_wins(self):
        self.stock();v=self.r.preview(self.body())
        def save(_):
            try:self.r.apply({**v,'confirmed':True,'request_id':ident()});return True
            except Problem:return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:self.assertEqual(sum(pool.map(save,range(2))),1)
        self.assertEqual(self.row()['revision'],1)

    def test_no_procurement_or_inventory_mutation_and_zero_suggestion(self):
        self.stock(40);self.purchase();before=self.ops.state();self.save();after=self.ops.state()
        self.assertEqual(before['documents'],after['documents']);self.assertEqual(before['stock'],after['stock']);self.assertEqual(before['movements'],after['movements'])
        self.assertEqual(self.row()['quantity'],0)

    def test_pagination_search_warehouse_and_safe_export_all_filtered_rows(self):
        self.store.import_rows([{'title_zh':'分页补货 '+str(i),'source_sku':'RP-'+str(i)} for i in range(55)])
        other=self.op('entity',kind='warehouse',name='另一仓')['id']
        s=self.r.state({'warehouse_id':self.wh});self.assertEqual(s['sku_page']['total'],56);self.assertEqual(len(s['rows']),50)
        self.assertEqual(len(self.r.state({'warehouse_id':self.wh,'page':1})['rows']),6)
        self.assertEqual(self.r.state({'search':'另一仓'})['sku_page']['total'],56)
        self.assertEqual(self.r.state({'search':'%'})['sku_page']['total'],0)
        exported=self.r.export({'warehouse_id':self.wh,'page':1});self.assertTrue(exported.startswith(b'\xef\xbb\xbf'))
        rows=list(csv.reader(io.StringIO(exported.decode('utf-8-sig'))));head=next(i for i,r in enumerate(rows) if r and r[0]=='warehouse_name')
        self.assertEqual(len(rows[head+1:]),56);self.assertIn("'=补货测试",exported.decode())
        self.assertEqual(self.r.state({'warehouse_id':other,'search':'=补货测试'})['sku_page']['total'],1)

    def test_validation_does_not_partially_save(self):
        for change in ({'target':1},{'minimum':True},{'lead_days':3651},{'target':-1},{'lead_days':1.2},{'revision':True}):
            with self.assertRaises(Problem):self.r.preview(self.body(**change))
        for change in ({'page':True},{'page':'-1'},{'search':5},{'warehouse_id':'missing'}):
            with self.assertRaises(Problem):self.r.state(change)
        self.assertEqual(self.row()['revision'],0)

if __name__=='__main__':unittest.main()
