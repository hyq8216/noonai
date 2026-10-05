import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from analytics import Analytics
from core import Store, Problem, ident, now
from finance import Finance
from operations import Operations


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
        self.ops=Operations(self.store);self.finance=Finance(self.store)
        self.a=Analytics(SimpleNamespace(store=self.store));self.today=now()[:10]
        self.pid=self.store.import_rows([{'title_zh':'分析专用测试商品'}])['created'][0]
        self.shop=self.op('entity',kind='shop',name='分析店')['id']
        self.warehouse=self.op('entity',kind='warehouse',name='分析仓')['id']
        self.supplier=self.op('entity',kind='supplier',name='分析供应商')['id']

    def tearDown(self):self.tmp.cleanup()
    def op(self,action,**b):return self.ops.transact(action,{'request_id':ident(),**b})
    def order(self,currency='SAR',quantity=2,price='10',**extra):
        return self.op('order',shop_id=self.shop,warehouse_id=self.warehouse,external_id=ident(),currency=currency,lines=[{'product_id':self.pid,'quantity':quantity,'unit_price':price}],**extra)
    def state(self,**filters):return self.a.state({'from':self.today,'to':self.today,**filters})
    def entry(self,**extra):
        return self.finance.transact('entry',{'request_id':ident(),'kind':'expense','category':'other','currency':'CNY','amount':'10','fx':'1','date':self.today,'evidence_key':ident(),'evidence':'测试凭证',**extra})
    def update_doc(self,d,**changes):
        d.update(changes)
        with self.store.connect() as c:c.execute('UPDATE ops_documents SET data=? WHERE id=?',(json.dumps(d),d['id']))
        return d
    def deliver(self,d):
        self.op('adjust',product_id=self.pid,warehouse_id=self.warehouse,direction='in',quantity=10,reason='专用测试')
        d=self.op('reserve',id=d['id'],revision=d['revision']);d=self.op('ship',id=d['id'],revision=d['revision'],carrier='测试',tracking='TEST')
        return self.op('deliver',id=d['id'],revision=d['revision'],evidence='测试签收')

    def test_currency_grouping_cancel_excluded_and_status_counts(self):
        self.order('SAR');self.order('USD',1,'7');d=self.order('SAR',5,'100')
        self.op('cancel',id=d['id'],revision=d['revision'],reason='测试取消')
        s=self.state();self.assertEqual(s['orders']['total'],3);self.assertEqual(s['orders']['statuses']['cancelled'],1)
        self.assertEqual({r['currency']:r['amount_cents'] for r in s['order_currencies']},{'SAR':2000,'USD':700})
        self.assertEqual(s['orders']['ordered_units'],3);self.assertEqual(s['finance']['income_cents'],0)

    def test_shipment_and_return_have_independent_event_dates(self):
        d=self.deliver(self.order(quantity=3));d=self.op('return',id=d['id'],revision=d['revision'],quantities={self.pid:1},restock=False,reason='测试退货')
        self.update_doc(d,created_at='2025-01-01T00:00:00Z')
        s=self.state();self.assertEqual(s['orders']['total'],0);self.assertEqual(s['orders']['shipped_units'],3)
        self.assertEqual(s['orders']['returned_units'],1);self.assertEqual(s['orders']['returned_orders'],1)
        self.assertEqual(s['finance']['expense_cents'],0)
        self.assertEqual(s['sku_page']['items'][0]['shipped_units'],3)

    def test_stock_unknown_cost_not_zero_and_reference_estimate(self):
        self.op('adjust',product_id=self.pid,warehouse_id=self.warehouse,direction='in',quantity=5,reason='测试期初')
        self.assertEqual(self.state()['inventory']['unknown_cost_units'],5)
        self.assertIsNone(self.state()['sku_page']['items'][0]['estimated_value_cents'])
        with self.store.connect() as c:
            p=json.loads(c.execute('SELECT data FROM products WHERE id=?',(self.pid,)).fetchone()['data']);p['cost_cny']=2.35
            c.execute('UPDATE products SET data=? WHERE id=?',(json.dumps(p),self.pid))
        self.assertEqual(self.state()['inventory']['estimated_value_cents'],1175)
        self.assertEqual(self.state()['inventory']['unknown_cost_units'],0)

    def test_purchases_received_and_cancelled_unreceived_not_in_transit(self):
        p=self.op('purchase',supplier_id=self.supplier,warehouse_id=self.warehouse,lines=[{'product_id':self.pid,'quantity':5,'unit_price':'2'}])
        p=self.op('receive',id=p['id'],revision=p['revision'],quantities={self.pid:2})
        self.assertEqual(self.state()['purchases']['in_transit_units'],3)
        self.op('cancel',id=p['id'],revision=p['revision'],reason='供应商缺货')
        s=self.state();self.assertEqual(s['purchases']['received_units'],2);self.assertEqual(s['purchases']['in_transit_units'],0)
        self.assertFalse(self.state(shop_id=self.shop)['purchases']['shop_attributable'])

    def test_exact_apportionment_and_filtered_unallocated_visibility(self):
        d1=self.order();d2=self.order();e=self.entry(currency='USD',amount='.03',fx='.5')
        self.finance.transact('allocate',{'request_id':ident(),'id':e['id'],'revision':e['revision'],'allocations':[{'order_id':d1['id'],'amount':'.01'},{'order_id':d2['id'],'amount':'.01'}]})
        f=self.state()['finance'];self.assertEqual(f['expense_cents'],2)
        self.assertEqual(f['allocated_expense_cents']+f['global_unallocated_expense_cents'],2)
        self.assertEqual(f['pending_difference_cents'],-2)
        filtered=self.state(shop_id=self.shop)['finance'];self.assertEqual(filtered['expense_cents'],2)
        self.assertEqual(filtered['unallocated_scope'],'global')

    def test_review_reuses_full_finance_fingerprint_and_refund_invalidates(self):
        d=self.deliver(self.order());self.entry(kind='income',category='sale',amount='30',document_id=d['id']);self.entry(category='goods',document_id=d['id'])
        o=self.finance.state()['orders'][0]
        self.finance.transact('review',{'request_id':ident(),'order_id':d['id'],'fingerprint':o['fingerprint'],'income_checked':True,'costs_checked':True,'refunds_checked':True,'note':'测试完整核对'})
        f=self.state()['finance'];self.assertEqual(f['verified_contribution_cents'],2000);self.assertEqual(f['pending_difference_cents'],0)
        self.entry(category='refund',amount='3',document_id=d['id'])
        f=self.state()['finance'];self.assertEqual(f['verified_orders'],0);self.assertEqual(f['pending_difference_cents'],1700)

    def test_shop_and_warehouse_filters_use_exact_allocations_not_whole_entry(self):
        other_shop=self.op('entity',kind='shop',name='另一店')['id']
        other_warehouse=self.op('entity',kind='warehouse',name='另一仓')['id']
        d1=self.order()
        d2=self.op('order',shop_id=other_shop,warehouse_id=other_warehouse,external_id=ident(),currency='USD',lines=[{'product_id':self.pid,'quantity':1,'unit_price':'1'}])
        e=self.entry(amount='10')
        self.finance.transact('allocate',{'request_id':ident(),'id':e['id'],'revision':e['revision'],'allocations':[{'order_id':d1['id'],'amount':'3'},{'order_id':d2['id'],'amount':'5'}]})
        self.op('adjust',product_id=self.pid,warehouse_id=self.warehouse,direction='in',quantity=4,reason='测试')
        self.op('adjust',product_id=self.pid,warehouse_id=other_warehouse,direction='in',quantity=7,reason='测试')
        filtered=self.state(shop_id=self.shop,warehouse_id=self.warehouse)
        self.assertEqual(filtered['orders']['total'],1)
        self.assertEqual(filtered['inventory']['on_hand'],4)
        self.assertEqual(filtered['finance']['expense_cents'],300)
        self.assertEqual(filtered['finance']['global_unallocated_expense_cents'],200)
        whole=self.state()['finance']
        self.assertEqual(whole['expense_cents'],1000)
        self.assertEqual(whole['allocated_expense_cents'],800)

    def test_period_contribution_keeps_full_history_review_and_payments_do_not_double_count(self):
        d=self.deliver(self.order())
        income=self.entry(kind='income',category='sale',amount='30',document_id=d['id'],date='2026-01-01')
        self.entry(category='goods',document_id=d['id'])
        self.finance.transact('payment',{'request_id':ident(),'id':income['id'],'revision':income['revision'],'amount':'30','fx':'1','date':self.today,'evidence_key':ident(),'account':'测试','evidence':'测试到账'})
        o=self.finance.state()['orders'][0]
        self.finance.transact('review',{'request_id':ident(),'order_id':d['id'],'fingerprint':o['fingerprint'],'income_checked':True,'costs_checked':True,'refunds_checked':True,'note':'测试期间及完整历史核对'})
        f=self.state()['finance']
        self.assertEqual(f['income_cents'],0)
        self.assertEqual(f['expense_cents'],1000)
        self.assertEqual(f['verified_contribution_cents'],-1000)
        self.assertEqual(f['missing_revenue_orders'],0)

    def test_date_boundaries_and_invalid_filter(self):
        d=self.order();self.update_doc(d,created_at='2026-01-01T23:59:59Z')
        self.assertEqual(self.a.state({'from':'2026-01-01','to':'2026-01-01'})['orders']['total'],1)
        self.assertEqual(self.a.state({'from':'2026-01-02','to':'2026-01-02'})['orders']['total'],0)
        self.a.state({'from':'2024-01-01','to':'2024-12-31'})
        for body in ({'from':'2024-01-01','to':'2025-01-01'},{'from':'2026-02-30'},{'from':'2026-02-01','to':'2026-01-01'},{'page':True},{'shop_id':'missing'}):
            with self.assertRaises(Problem):self.a.state(body)

    def test_pagination_and_csv_formula_injection(self):
        ids=self.store.import_rows([{'title_zh':f'库存测试{i}'} for i in range(55)])['created']
        with self.store.connect() as c:
            for pid in ids:c.execute('INSERT INTO ops_stock(product_id,warehouse_id,on_hand) VALUES(?,?,1)',(pid,self.warehouse))
            p=json.loads(c.execute('SELECT data FROM products WHERE id=?',(ids[0],)).fetchone()['data']);p['partner_sku']='=HYPERLINK("x")';p['title_zh']=' \t@SUM(1)'
            c.execute('UPDATE products SET data=? WHERE id=?',(json.dumps(p),ids[0]))
        s=self.state();self.assertEqual(len(s['sku_page']['items']),50);self.assertEqual(s['sku_page']['total'],55)
        self.assertEqual(len(self.state(page=1)['sku_page']['items']),5)
        rows=list(csv.reader(io.StringIO(self.a.export({'from':self.today,'to':self.today}).decode('utf-8-sig'))))
        sku=next(row for row in rows if row and row[0].startswith("'=HYPERLINK"));self.assertTrue(sku[1].startswith("'"))

    def test_empty_no_sample_data(self):
        s=self.state();self.assertTrue(s['empty']);self.assertEqual(s['sku_page']['items'],[])
        self.assertEqual(s['orders']['total'],0);self.assertEqual(s['finance']['verified_orders'],0)


if __name__=='__main__':unittest.main()
