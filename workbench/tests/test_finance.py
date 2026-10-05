import concurrent.futures
import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Store,Problem,ident,now
from operations import Operations
from finance import Finance,apportioned

class FinanceTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name);self.ops=Operations(self.store);self.f=Finance(self.store)
  self.pid=self.store.import_rows([{'title_zh':'专用财务测试商品'}])['created'][0]
  self.shop=self.op('entity',kind='shop',name='测试店')['id'];self.warehouse=self.op('entity',kind='warehouse',name='测试仓')['id'];self.supplier=self.op('entity',kind='supplier',name='测试供应商')['id']
  self.order=self.make_order();self.purchase=self.op('purchase',supplier_id=self.supplier,warehouse_id=self.warehouse,lines=[{'product_id':self.pid,'quantity':2,'unit_price':'8'}])
 def tearDown(self):self.tmp.cleanup()
 def op(self,action,**b):return self.ops.transact(action,{'request_id':ident(),**b})
 def make_order(self):return self.op('order',shop_id=self.shop,warehouse_id=self.warehouse,external_id=ident(),currency='SAR',lines=[{'product_id':self.pid,'quantity':1,'unit_price':'100'}])
 def call(self,action,**b):return self.f.transact(action,{'request_id':ident(),**b})
 def create(self,**extra):
  return self.call('entry',**{'kind':'income','category':'sale','document_id':self.order['id'],'currency':'USD','amount':'100','fx':'7.2','date':'2026-01-01','due_date':'2026-01-10','evidence':'测试结算明细','evidence_key':ident(),**extra})
 def entry(self,e):return next(x for x in self.f.state()['entries'] if x['id']==e['id'])
 def pay(self,e,**extra):
  e=self.entry(e);return self.call('payment',**{'id':e['id'],'revision':e['revision'],'amount':'50','fx':'7.1','date':'2026-01-03','account':'测试收款账户','evidence':'测试到账单','evidence_key':ident(),**extra})
 def allocate(self,e,allocations):
  e=self.entry(e);return self.call('allocate',id=e['id'],revision=e['revision'],allocations=allocations)
 def deliver(self):
  self.op('adjust',product_id=self.pid,warehouse_id=self.warehouse,direction='in',quantity=1,reason='测试期初')
  d=self.op('reserve',id=self.order['id'],revision=self.order['revision']);d=self.op('ship',id=d['id'],revision=d['revision'],carrier='测试',tracking='TEST');self.order=self.op('deliver',id=d['id'],revision=d['revision'],evidence='测试签收')
 def review(self,**extra):
  o=next(o for o in self.f.state()['orders'] if o['id']==self.order['id']);return self.call('review',**{'order_id':o['id'],'fingerprint':o['fingerprint'],'income_checked':True,'costs_checked':True,'refunds_checked':True,'note':'逐笔核对测试结算、物流、成本和售后凭证',**extra})
 def test_unencodable_unicode_is_rejected_before_finance_receipt(self):
  before=self.f.state()
  with self.assertRaises(Problem):self.f.transact('entry',{'request_id':'bad-\ud800','kind':'income'})
  after=self.f.state()
  self.assertEqual(after['entries'],before['entries'])
  with self.store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM finance_requests').fetchone()[0],0)
 def test_source_orders_and_purchase_plans_never_become_revenue_or_cost(self):
  s=self.f.state();self.assertEqual(s['entries'],[]);self.assertIsNone(s['orders'][0]['contribution_cents']);self.assertEqual(s['summary']['active_entries'],0)
 def test_partial_receipt_fx_and_no_double_count(self):
  e=self.create();self.pay(e);s=self.f.state();row=self.entry(e)
  self.assertEqual(row['remaining_cents'],5000);self.assertEqual(row['remaining_base_cents'],36000);self.assertTrue(row['overdue']);self.assertEqual(row['realized_fx_cents'],-500)
  self.assertEqual(s['summary']['income_cents'],72000);self.assertEqual(s['summary']['cash_in_cents'],35500)
  self.pay(e,fx='7.3');s=self.f.state();self.assertEqual(s['summary']['income_cents'],72000);self.assertEqual(s['summary']['cash_in_cents'],72000);self.assertEqual(s['summary']['realized_fx_cents'],0);self.assertFalse(self.entry(e)['overdue'])
  with self.assertRaises(Problem):self.pay(e,amount='0.01')
 def test_purchase_cost_allocation_and_cash_not_counted_twice(self):
  e=self.create(kind='expense',category='goods',document_id=self.purchase['id'],currency='CNY',amount='16',fx='1')
  o2=self.make_order();self.allocate(e,[{'order_id':self.order['id'],'amount':'5'},{'order_id':o2['id'],'amount':'7'}]);self.pay(e,amount='16',fx='1')
  s=self.f.state();self.assertEqual(s['summary']['expense_cents'],1600);self.assertEqual(s['summary']['cash_out_cents'],1600);self.assertEqual(s['summary']['unallocated_net_cents'],-400)
  self.assertEqual(sorted(o['expense_cents'] for o in s['orders']),[500,700])
  self.assertEqual(s['summary']['recorded_difference_cents'],-1600)
  with self.assertRaises(Problem):self.allocate(e,[{'order_id':o2['id'],'amount':'17'}])
  self.assertEqual(self.entry(e)['allocations'][0]['amount_cents'],500)
 def test_empty_library_and_unassigned_only_expense_summary(self):
  with tempfile.TemporaryDirectory() as root:
   store=Store(root);Operations(store);f=Finance(store)
   self.assertEqual(f.state()['summary']['recorded_difference_cents'],0)
   f.transact('entry',{'request_id':ident(),'kind':'expense','category':'other','currency':'CNY','amount':'1.25','fx':'1','date':'2026-01-01','evidence':'测试','evidence_key':'unassigned'})
   self.assertEqual(f.state()['summary']['recorded_difference_cents'],-125)
 def test_penny_allocation_and_cumulative_payment_rounding(self):
  self.assertEqual(apportioned(2,[1,1,1,0]),[1,1,0,0])
  e=self.create(kind='expense',category='platform',document_id='',amount='.03',fx='.5');orders=[self.order,self.make_order(),self.make_order()]
  self.allocate(e,[{'order_id':d['id'],'amount':'.01'} for d in orders]);s=self.f.state();self.assertEqual(sum(o['expense_cents'] for o in s['orders']),2)
  for _ in range(3):self.pay(e,amount='.01',fx='.5')
  self.assertEqual(self.entry(e)['remaining_base_cents'],0);self.assertEqual(self.f.state()['summary']['realized_fx_cents'],-1)
 def test_original_currency_balances_never_combined(self):
  self.create(currency='SAR',amount='100',fx='1.9');self.create(currency='USD',amount='20',fx='7.2')
  balances={x['currency']:x['receivable_cents'] for x in self.f.state()['currencies']};self.assertEqual(balances,{'USD':2000,'SAR':10000})
 def test_payment_void_reopens_balance_and_entry_void_preserves_records(self):
  e=self.create();p=self.pay(e)
  with self.assertRaises(Problem):self.call('void_entry',id=e['id'],revision=self.entry(e)['revision'],reason='测试纠错')
  self.call('void_payment',payment_id=p['id'],revision=self.entry(e)['revision'],reason='重复凭证纠正')
  self.assertEqual(self.entry(e)['remaining_cents'],10000);self.assertEqual(self.f.state()['summary']['cash_in_cents'],0)
  self.call('void_entry',id=e['id'],revision=self.entry(e)['revision'],reason='已作废原结算单')
  s=self.f.state();self.assertEqual(s['summary']['income_cents'],0);self.assertEqual(len(s['entries']),1);self.assertEqual(len(s['payments']),1);self.assertEqual(s['payments'][0]['void_reason'],'重复凭证纠正')
 def test_duplicate_request_and_evidence_guard(self):
  e=self.create(evidence_key='line-one')
  with self.assertRaises(Problem):self.create(evidence_key='line-one')
  b={'request_id':'same','id':e['id'],'revision':e['revision'],'amount':'20','fx':'7.2','date':'2026-01-04','evidence_key':'bank-line','evidence':'测试','account':'测试账户'}
  a=self.f.transact('payment',b);self.assertEqual(a,self.f.transact('payment',b));self.assertEqual(len(self.f.state()['payments']),1)
  with self.assertRaises(Problem):self.f.transact('payment',{**b,'amount':'30'})
  with self.assertRaises(Problem):self.pay(e,evidence_key='bank-line')
 def test_concurrent_settlement_does_not_overpay(self):
  e=self.create();b={'id':e['id'],'revision':1,'amount':'80','fx':'7.2','date':'2026-01-04','evidence':'测试','account':'账户'}
  def run(i):
   try:self.call('payment',**b,evidence_key=str(i));return True
   except Problem:return False
  with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:self.assertEqual(sum(pool.map(run,[1,2])),1)
  self.assertEqual(self.entry(e)['remaining_cents'],2000)
 def test_order_reconciliation_requires_facts_and_invalidates_after_new_cost(self):
  self.create();self.create(kind='expense',category='goods',amount='10',currency='CNY',fx='1')
  with self.assertRaises(Problem):self.review()
  self.deliver()
  with self.assertRaises(Problem):self.review(costs_checked=False)
  self.review();self.assertTrue(self.f.state()['orders'][0]['reconciled'])
  self.create(kind='expense',category='refund',amount='5',currency='CNY',fx='1');self.assertFalse(self.f.state()['orders'][0]['reconciled'])
  self.review();self.op('return',id=self.order['id'],revision=self.order['revision'],quantities={self.pid:1},restock=False,reason='售后测试');self.assertFalse(self.f.state()['orders'][0]['reconciled'])
 def test_invalid_numbers_currency_dates_and_categories_are_rejected_atomically(self):
  for extra in ({'amount':True},{'amount':'NaN'},{'amount':'0'},{'amount':'1.001'},{'fx':'Infinity'},{'fx':'0'},{'fx':'1.0000001'},{'currency':'CNY','fx':'2'},{'currency':'BTC'},{'date':'2026-02-30'},{'date':'2999-01-01'},{'category':'goods','kind':'income'},{'document_id':''}):
   with self.assertRaises(Problem):self.create(**extra)
  self.assertEqual(self.f.state()['entries'],[])
 def test_allocation_duplicates_and_direct_order_reassignment_fail(self):
  e=self.create()
  with self.assertRaises(Problem):self.allocate(e,[])
  e=self.create(kind='expense',category='logistics',document_id='')
  with self.assertRaises(Problem):self.allocate(e,[{'order_id':self.order['id'],'amount':'1'}]*2)
  self.assertEqual(self.entry(e)['allocations'],[])
 def test_csv_export_formula_protection_and_reopen(self):
  e=self.create(evidence='=HYPERLINK("x")',evidence_key='@bad');self.pay(e)
  rows=list(csv.reader(io.StringIO(self.f.csv().decode('utf-8-sig'))));self.assertEqual(rows[1][-1],"'=HYPERLINK(\"x\")");self.assertEqual(rows[1][-2],"'@bad")
  self.assertEqual(self.f.state(),Finance(Store(self.tmp.name)).state())

if __name__=='__main__':unittest.main()
