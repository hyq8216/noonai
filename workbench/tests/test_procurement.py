import csv
import concurrent.futures
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Store, Problem, ident
from operations import Operations
from procurement import Procurement


class ProcurementTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name);self.ops=Operations(self.store)
        self.p=Procurement(SimpleNamespace(store=self.store))
        self.pid=self.store.import_rows([{'title_zh':'采购关联专用测试'}])['created'][0]
        self.shop=self.op('entity',kind='shop',name='测试店')['id'];self.wh=self.op('entity',kind='warehouse',name='测试仓')['id']
        self.supplier=self.op('entity',kind='supplier',name='测试供应商')['id']
        self.order=self.make_order();self.purchase=self.make_purchase()
    def tearDown(self):self.tmp.cleanup()
    def op(self,action,**b):return self.ops.transact(action,{'request_id':ident(),**b})
    def make_order(self,quantity=5):return self.op('order',shop_id=self.shop,warehouse_id=self.wh,external_id=ident(),currency='SAR',lines=[{'product_id':self.pid,'quantity':quantity,'unit_price':'10'}])
    def make_purchase(self,quantity=8):return self.op('purchase',supplier_id=self.supplier,warehouse_id=self.wh,lines=[{'product_id':self.pid,'quantity':quantity,'unit_price':'2'}])
    def body(self,quantity=3,order=None,purchase=None,revision=0):
        o=order or self.order;p=purchase or self.purchase
        return {'order_id':o['id'],'purchase_id':p['id'],'product_id':self.pid,'quantity':quantity,'order_revision':o['revision'],'purchase_revision':p['revision'],'link_revision':revision}
    def apply(self,body=None):
        preview=self.p.preview(body or self.body());return self.p.apply({**preview,'confirmed':True,'request_id':ident()})

    def test_link_only_does_not_mutate_business_or_stock(self):
        before=self.ops.state();link=self.apply();after=self.ops.state()
        self.assertEqual(before['documents'],after['documents']);self.assertEqual(before['stock'],after['stock']);self.assertEqual(before['movements'],after['movements'])
        s=self.p.state();self.assertEqual(s['summary']['confirmed_units'],3);self.assertEqual(s['summary']['gap_units'],2)
        self.assertEqual(s['links'][0]['quantity'],3);self.assertFalse(link['needs_review'])

    def test_order_and_purchase_allocations_conserve_across_multiple_documents(self):
        self.apply(self.body(5));o2=self.make_order();p2=self.make_purchase()
        with self.assertRaises(Problem):self.apply(self.body(1,purchase=p2))
        with self.assertRaises(Problem):self.apply(self.body(4,order=o2))
        self.apply(self.body(3,order=o2));self.apply(self.body(2,order=o2,purchase=p2))
        s=self.p.state();self.assertEqual(s['summary']['confirmed_units'],10);self.assertEqual(s['summary']['gap_units'],0)

    def test_setting_quantity_replaces_not_adds_and_requires_link_revision(self):
        link=self.apply()
        with self.assertRaises(Problem):self.p.preview(self.body(2))
        link2=self.apply(self.body(2,revision=link['revision']))
        self.assertEqual(link2['revision'],2);self.assertEqual(self.p.state()['summary']['confirmed_units'],2)

    def test_receive_marks_review_and_explicit_reconfirmation_restores_plan(self):
        link=self.apply();self.purchase=self.op('receive',id=self.purchase['id'],revision=self.purchase['revision'],quantities={self.pid:2})
        s=self.p.state();self.assertEqual(s['summary']['needs_review_links'],1);self.assertEqual(s['summary']['confirmed_units'],0);self.assertEqual(s['summary']['gap_units'],5)
        self.assertEqual(s['links'][0]['review_reason'],'采购事实或到货数量已变化，请重新核对')
        preview=self.p.preview(self.body(3,revision=link['revision']));self.assertEqual(preview['purchase_received'],2);self.assertEqual(preview['purchase_pending'],6)
        self.p.apply({**preview,'confirmed':True,'request_id':ident()});self.assertEqual(self.p.state()['summary']['confirmed_units'],3)

    def test_cancel_purchase_keeps_capacity_until_unlink_and_only_received_relink(self):
        link=self.apply()
        self.purchase=self.op('receive',id=self.purchase['id'],revision=self.purchase['revision'],quantities={self.pid:2})
        self.purchase=self.op('cancel',id=self.purchase['id'],revision=self.purchase['revision'],reason='测试取消')
        self.assertEqual(self.p.state()['summary']['needs_review_links'],1)
        self.assertEqual(self.p.state()['links'][0]['review_reason'],'采购已取消，仅已收货量可重新关联')
        with self.assertRaises(Problem):self.p.preview(self.body(3,revision=link['revision']))
        fixed=self.apply(self.body(2,revision=link['revision']));self.assertEqual(fixed['quantity'],2)
        preview=self.p.preview({**self.body(2,revision=fixed['revision']),'action':'unlink'})
        result=self.p.unlink({**preview,'confirmed':True,'request_id':ident()});self.assertTrue(result['unlinked']);self.assertEqual(self.p.state()['links'],[])

    def test_cancel_order_requires_unlink_not_new_link(self):
        link=self.apply();self.order=self.op('cancel',id=self.order['id'],revision=self.order['revision'],reason='测试取消')
        self.assertEqual(self.p.state()['summary']['needs_review_links'],1);self.assertEqual(self.p.state()['summary']['demand_units'],0)
        self.assertEqual(self.p.state()['links'][0]['review_reason'],'订单已取消，请解除旧关联')
        with self.assertRaises(Problem):self.p.preview(self.body(2,revision=link['revision']))
        preview=self.p.preview({**self.body(revision=link['revision']),'action':'unlink'})
        self.p.unlink({**preview,'confirmed':True,'request_id':ident()});self.assertEqual(self.p.state()['links'],[])

    def test_idempotency_confirmation_and_stale_preview(self):
        preview=self.p.preview(self.body());b={**preview,'confirmed':True,'request_id':ident()}
        with self.assertRaises(Problem):self.p.apply({**b,'confirmed':False})
        result=self.p.apply(b);self.assertEqual(self.p.apply(b),result)
        with self.assertRaises(Problem):self.p.apply({**b,'quantity':2})
        with self.assertRaises(Problem):self.p.apply({**b,'request_id':ident()})
        with self.assertRaises(Problem):self.p.apply({**self.body(2,revision=1),'confirmed':True,'request_id':ident()})

    def test_other_allocation_changes_invalidate_preview_atomically(self):
        o2=self.make_order();first=self.p.preview(self.body(5));self.apply(self.body(4,order=o2))
        with self.assertRaises(Problem):self.p.apply({**first,'confirmed':True,'request_id':ident()})
        self.assertEqual(len(self.p.state()['links']),1)

    def test_deleted_link_does_not_resurrect_from_old_initial_preview(self):
        original=self.p.preview(self.body());link=self.apply()
        undo=self.p.preview({**self.body(revision=link['revision']),'action':'unlink'})
        self.p.unlink({**undo,'confirmed':True,'request_id':ident()})
        with self.assertRaises(Problem):self.p.apply({**original,'confirmed':True,'request_id':ident()})
        self.assertEqual(self.p.state()['links'],[])

    def test_concurrent_orders_cannot_overallocate_purchase(self):
        o2=self.make_order();previews=[self.p.preview(self.body(5)),self.p.preview(self.body(5,order=o2))]
        def apply(preview):
            try:self.p.apply({**preview,'confirmed':True,'request_id':ident()});return True
            except Problem:return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(apply,previews))
        self.assertEqual(sum(results),1);self.assertEqual(sum(x['quantity'] for x in self.p.state()['links']),5)

    def test_unlink_replay_does_not_release_any_other_allocation(self):
        link=self.apply();o2=self.make_order();self.apply(self.body(3,order=o2))
        preview=self.p.preview({**self.body(revision=link['revision']),'action':'unlink'});b={**preview,'confirmed':True,'request_id':ident()}
        self.assertEqual(self.p.unlink(b),self.p.unlink(b));self.assertEqual(len(self.p.state()['links']),1)
        self.assertEqual(self.p.state()['links'][0]['order_id'],o2['id'])

    def test_fingerprint_drift_without_revision_change_marks_review(self):
        self.apply()
        with self.store.connect() as c:
            d=json.loads(c.execute('SELECT data FROM ops_documents WHERE id=?',(self.order['id'],)).fetchone()['data']);d['note']='另一事实'
            c.execute('UPDATE ops_documents SET data=? WHERE id=?',(json.dumps(d,ensure_ascii=False),self.order['id']))
        self.assertTrue(self.p.state()['links'][0]['needs_review'])

    def test_product_and_warehouse_must_match_and_invalid_numbers_rejected(self):
        for amount in (0,True,'1.5','NaN',-1):
            with self.assertRaises(Problem):self.p.preview(self.body(amount))
        with self.assertRaises(Problem):self.p.preview({**self.body(),'product_id':'missing'})
        wh=self.op('entity',kind='warehouse',name='另一仓')['id']
        p=self.op('purchase',supplier_id=self.supplier,warehouse_id=wh,lines=[{'product_id':self.pid,'quantity':8,'unit_price':'2'}])
        with self.assertRaises(Problem):self.p.preview(self.body(purchase=p))

    def test_pages_empty_and_export_formula_safety(self):
        with self.store.connect() as c:
            d=json.loads(c.execute('SELECT data FROM ops_documents WHERE id=?',(self.order['id'],)).fetchone()['data']);d['external_id']='=HYPERLINK("x")';d['lines'][0]['sku']=' @SUM(1)'
            c.execute('UPDATE ops_documents SET data=? WHERE id=?',(json.dumps(d,ensure_ascii=False),self.order['id']))
        for _ in range(52):self.make_order(1)
        s=self.p.state();self.assertEqual(s['demand_page']['total'],53);self.assertEqual(len(s['demand_page']['items']),50);self.assertEqual(len(self.p.state(page=1)['demand_page']['items']),3)
        rows=list(csv.reader(io.StringIO(self.p.export({'page':0}).decode('utf-8-sig'))))+list(csv.reader(io.StringIO(self.p.export({'page':1}).decode('utf-8-sig'))))
        match=next(row for row in rows if len(row)>1 and row[1].startswith("'=HYPERLINK"));self.assertTrue(match[3].startswith("'"))
        with self.assertRaises(Problem):self.p.state({'page':True})


if __name__=='__main__':unittest.main()
