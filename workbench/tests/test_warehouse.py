import concurrent.futures
import tempfile
import unittest
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Store,Problem,ident
from operations import Operations
from finance import Finance

class WarehouseTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name);self.ops=Operations(self.store)
  self.pids=self.store.import_rows([{'title_zh':'仓储测试A'},{'title_zh':'仓储测试B'}])['created']
  self.src=self.call('entity',kind='warehouse',name='调出仓')['id'];self.dest=self.call('entity',kind='warehouse',name='调入仓')['id'];self.supplier=self.call('entity',kind='supplier',name='补货供应商')['id'];self.shop=self.call('entity',kind='shop',name='测试店铺')['id']
 def tearDown(self):self.tmp.cleanup()
 def call(self,action,**body):return self.ops.transact(action,{'request_id':ident(),**body})
 def action(self,action,d,**body):return self.call(action,id=d['id'],revision=d['revision'],**body)
 def stock(self,wid,pid=None):return next((s for s in self.ops.state()['stock'] if s['warehouse_id']==wid and s['product_id']==(pid or self.pids[0])),{'on_hand':0,'reserved':0,'available':0})
 def add(self,n=10,wid=None,pid=None):return self.call('adjust',product_id=pid or self.pids[0],warehouse_id=wid or self.src,direction='in',quantity=n,reason='专用测试期初')
 def transfer(self,n=6,**extra):return self.call('transfer',**{'source_warehouse_id':self.src,'warehouse_id':self.dest,'lines':[{'product_id':self.pids[0],'quantity':n}],'note':'测试调拨',**extra})
 def order(self,n=2,wid=None):return self.call('order',shop_id=self.shop,warehouse_id=wid or self.dest,external_id=ident(),currency='SAR',lines=[{'product_id':self.pids[0],'quantity':n,'unit_price':'2'}])
 def policy(self,**extra):return self.call('replenishment_policy',**{'product_id':self.pids[0],'warehouse_id':self.dest,'supplier_id':self.supplier,'minimum':5,'target':20,'min_order':1,'pack_size':1,'unit_price':'1.25','quote_evidence':'测试报价单','enabled':True,'revision':0,**extra})
 def plan(self):return self.ops.state()['replenishment']
 def replenish_body(self):return {'rows':[{k:r[k] for k in ('product_id','warehouse_id','fingerprint')} for r in self.plan() if r['quantity']],'confirmed':True}
 def test_transfer_dispatch_partial_receipt_conserves_physical_stock(self):
  self.add();d=self.transfer();self.assertEqual(self.stock(self.src)['on_hand'],10);self.assertEqual(self.stock(self.dest)['on_hand'],0)
  d=self.action('transfer_dispatch',d,evidence='装车单');self.assertEqual(self.stock(self.src)['on_hand'],4);self.assertEqual(self.stock(self.dest)['on_hand'],0)
  d=self.action('transfer_receive',d,quantities={self.pids[0]:2},evidence='验收单1');self.assertEqual(d['status'],'partial');self.assertEqual(self.stock(self.dest)['on_hand'],2)
  self.assertEqual(self.stock(self.src)['on_hand']+self.stock(self.dest)['on_hand']+d['lines'][0]['shipped']-d['lines'][0]['received'],10)
  d=self.action('transfer_receive',d,quantities={self.pids[0]:4},evidence='验收单2');self.assertEqual(d['status'],'received');self.assertEqual(self.stock(self.dest)['on_hand'],6);self.assertEqual(len(d['receipts']),2)
  with self.assertRaises(Problem):self.action('transfer_receive',d,quantities={self.pids[0]:1},evidence='多收')
 def test_reserved_stock_cannot_be_moved_and_multiline_is_atomic(self):
  self.add(10);order=self.order(7,self.src);self.action('reserve',order)
  d=self.transfer(4)
  with self.assertRaises(Problem):self.action('transfer_dispatch',d,evidence='测试')
  self.assertEqual(self.stock(self.src)['on_hand'],10);self.assertEqual(self.stock(self.src)['reserved'],7)
  d=self.transfer(lines=[{'product_id':self.pids[0],'quantity':2},{'product_id':self.pids[1],'quantity':1}])
  before=self.ops.state()
  with self.assertRaises(Problem):self.action('transfer_dispatch',d,evidence='第二行缺货')
  self.assertEqual(self.ops.state(),before)
 def test_transfer_identity_states_and_replay(self):
  with self.assertRaises(Problem):self.transfer(warehouse_id=self.src)
  d=self.transfer();self.action('transfer_cancel',d,reason='撤销计划')
  with self.assertRaises(Problem):self.action('transfer_dispatch',d,evidence='旧单')
  self.add();d=self.transfer();b={'request_id':'once','id':d['id'],'revision':d['revision'],'evidence':'出库依据'}
  sent=self.ops.transact('transfer_dispatch',b);self.assertEqual(sent,self.ops.transact('transfer_dispatch',b));self.assertEqual(self.stock(self.src)['on_hand'],4)
  with self.assertRaises(Problem):self.action('transfer_cancel',sent,reason='不能丢失在途货')
 def test_demand_reserved_and_incoming_not_double_counted(self):
  self.policy();self.add(10,self.dest);order=self.order(7)
  row=self.plan()[0];self.assertEqual((row['available'],row['unreserved_demand'],row['projected'],row['quantity']),(10,7,3,17))
  self.action('reserve',order);row=self.plan()[0];self.assertEqual((row['available'],row['unreserved_demand'],row['projected'],row['quantity']),(3,0,3,17))
  self.call('purchase',supplier_id=self.supplier,warehouse_id=self.dest,lines=[{'product_id':self.pids[0],'quantity':8,'unit_price':'1.25'}]);self.assertEqual(self.plan()[0]['projected'],11);self.assertEqual(self.plan()[0]['quantity'],0)
 def test_transfer_incoming_counted_only_after_dispatch(self):
  self.policy(target=10);self.add();d=self.transfer(6);self.assertEqual(self.plan()[0]['quantity'],10)
  d=self.action('transfer_dispatch',d,evidence='发出');r=self.plan()[0];self.assertEqual(r['transfer_incoming'],6);self.assertEqual(r['quantity'],0)
  self.action('transfer_receive',d,quantities={self.pids[0]:2},evidence='到仓');r=self.plan()[0];self.assertEqual((r['available'],r['transfer_incoming'],r['projected']),(2,4,6))
 def test_purchase_creation_is_grouped_and_suppresses_duplicate_suggestion(self):
  self.policy();self.policy(product_id=self.pids[1],unit_price='2.50');body={'request_id':'buy-once',**self.replenish_body()}
  out=self.ops.transact('replenish',body);self.assertEqual(len(out['purchase_ids']),1);self.assertEqual(out,self.ops.transact('replenish',body))
  d=next(d for d in self.ops.state()['documents'] if d['id']==out['purchase_ids'][0]);self.assertEqual(d['total_cents'],7500);self.assertEqual(len(d['replenishment_snapshot']),2)
  self.assertTrue(all(r['quantity']==0 for r in self.plan()))
  with self.assertRaises(Problem):self.call('replenish',**{k:v for k,v in body.items() if k!='request_id'})
 def test_stale_plan_rejected_and_concurrent_clicks_cannot_double_buy(self):
  self.policy();body=self.replenish_body();self.add(1,self.dest)
  with self.assertRaises(Problem):self.call('replenish',**body)
  body=self.replenish_body()
  def run(_):
   try:self.call('replenish',**body);return True
   except Problem:return False
  with concurrent.futures.ThreadPoolExecutor(max_workers=2) as p:self.assertEqual(sum(p.map(run,[1,2])),1)
  self.assertEqual(len([d for d in self.ops.state()['documents'] if d['kind']=='purchase']),1)
 def test_partial_receive_cancel_and_moq_pack_rounding(self):
  self.policy(target=13,min_order=17,pack_size=6);self.assertEqual(self.plan()[0]['quantity'],18)
  out=self.call('replenish',**self.replenish_body());d=next(d for d in self.ops.state()['documents'] if d['id']==out['purchase_ids'][0])
  d=self.action('receive',d,quantities={self.pids[0]:3});r=self.plan()[0];self.assertEqual((r['available'],r['purchase_incoming'],r['projected']),(3,15,18))
  self.action('cancel',d,reason='余量取消');r=self.plan()[0];self.assertEqual((r['purchase_incoming'],r['quantity']),(0,18))
 def test_policy_validation_disable_and_stale_update(self):
  for change in ({'minimum':30,'target':20},{'pack_size':0},{'minimum':1.5},{'unit_price':0},{'enabled':'yes'},{'supplier_id':'unknown'}):
   with self.assertRaises(Problem):self.policy(**change)
  self.assertEqual(self.plan(),[]);self.policy();self.policy(revision=1,enabled=False);self.assertEqual(self.plan()[0]['quantity'],0)
  with self.assertRaises(Problem):self.policy(revision=1)
 def test_two_suppliers_separate_po_and_bad_selection_atomic(self):
  self.policy();other=self.call('entity',kind='supplier',name='第二供应商')['id'];self.policy(product_id=self.pids[1],supplier_id=other)
  body=self.replenish_body();bad={'rows':[body['rows'][0],{**body['rows'][1],'fingerprint':'stale'}],'confirmed':True}
  with self.assertRaises(Problem):self.call('replenish',**bad)
  self.assertFalse([d for d in self.ops.state()['documents'] if d['kind']=='purchase'])
  self.assertEqual(len(self.call('replenish',**body)['purchase_ids']),2)
 def test_finance_rejects_transfer_as_order_or_purchase(self):
  d=self.transfer();f=Finance(self.store)
  with self.store.connect() as c:
   with self.assertRaises(Problem):f.document(c,d['id'])
 def test_reopen_policy_transit_and_plan_persist(self):
  self.policy();self.add();d=self.transfer();self.action('transfer_dispatch',d,evidence='测试出库')
  self.assertEqual(self.ops.state(),Operations(Store(self.tmp.name)).state())

if __name__=='__main__':unittest.main()
