import concurrent.futures
import tempfile
import unittest
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Store, Problem, ident
from operations import Operations

class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name);self.ops=Operations(self.store)
        self.p=self.store.import_rows([{'title_zh':'测试商品甲'},{'title_zh':'测试商品乙'}])['created']
        self.supplier=self.call('entity',kind='supplier',name='供应商')['id']
        self.warehouse=self.call('entity',kind='warehouse',name='测试仓')['id']
        self.shop=self.call('entity',kind='shop',name='测试店')['id']
    def tearDown(self):self.tmp.cleanup()
    def call(self,action,**data):return self.ops.transact(action,{'request_id':ident(),**data})
    def lines(self,q=5):return [{'product_id':p,'quantity':q,'unit_price':'0.10'} for p in self.p]
    def purchase(self,q=5):return self.call('purchase',supplier_id=self.supplier,warehouse_id=self.warehouse,lines=self.lines(q))
    def order(self,q=2,**kwargs):return self.call('order',shop_id=self.shop,warehouse_id=self.warehouse,external_id=ident(),currency='SAR',lines=self.lines(q),**kwargs)
    def action(self,action,d,**extra):return self.call(action,id=d['id'],revision=d['revision'],**extra)
    def receive(self,d,q):return self.action('receive',d,quantities={p:q for p in self.p})
    def balances(self):return [(x['on_hand'],x['reserved'],x['available']) for x in self.ops.state()['stock']]
    def test_purchase_partial_and_exact_money(self):
        d=self.purchase();self.assertEqual(d['total_cents'],100);self.assertEqual(self.balances(),[])
        d=self.receive(d,2);self.assertEqual(d['status'],'partial');self.assertEqual(self.balances(),[(2,0,2)]*2)
        d=self.receive(d,3);self.assertEqual(d['status'],'received');self.assertEqual(self.balances(),[(5,0,5)]*2)
        with self.assertRaises(Problem):self.receive(d,1)
    def test_reserve_ship_deliver_return(self):
        self.receive(self.purchase(),5);d=self.order();d=self.action('reserve',d)
        self.assertEqual(self.balances(),[(5,2,3)]*2)
        d=self.action('ship',d,carrier='测试承运',tracking='TRACK-1');self.assertEqual(self.balances(),[(3,0,3)]*2)
        d=self.action('deliver',d,evidence='测试签收凭证');self.assertEqual(d['status'],'delivered')
        d=self.action('return',d,quantities={p:1 for p in self.p},reason='测试退货',restock=True)
        self.assertEqual(self.balances(),[(4,0,4)]*2)
        with self.assertRaises(Problem):self.action('return',d,quantities={p:2 for p in self.p},reason='超量',restock=True)
        self.assertEqual(self.balances(),[(4,0,4)]*2)
        d=self.action('return',d,quantities={p:1 for p in self.p},reason='不可再售',restock=False)
        self.assertEqual(self.balances(),[(4,0,4)]*2)
    def test_multiline_reservation_rolls_back_if_one_short(self):
        self.call('adjust',product_id=self.p[0],warehouse_id=self.warehouse,quantity=8,direction='in',reason='期初')
        d=self.order()
        with self.assertRaises(Problem):self.action('reserve',d)
        self.assertEqual(self.balances(),[(8,0,8)])
        self.assertEqual(len(self.ops.state()['movements']),1)
    def test_cancel_releases_only_reserved(self):
        self.receive(self.purchase(),5);d=self.action('reserve',self.order());self.action('cancel',d,reason='缺货取消')
        self.assertEqual(self.balances(),[(5,0,5)]*2)
    def test_partial_purchase_cancel_does_not_reverse_received(self):
        d=self.receive(self.purchase(),2);d=self.action('cancel',d,reason='余量不采购');self.assertEqual(self.balances(),[(2,0,2)]*2)
        with self.assertRaises(Problem):self.receive(d,1)
    def test_receive_replay_idempotent_and_conflicting_payload_rejected(self):
        d=self.purchase();b={'request_id':ident(),'id':d['id'],'revision':d['revision'],'quantities':{p:2 for p in self.p}}
        a=self.ops.transact('receive',b);self.assertEqual(self.ops.transact('receive',b),a)
        self.assertEqual(self.balances(),[(2,0,2)]*2)
        with self.assertRaises(Problem):self.ops.transact('receive',{**b,'quantities':{p:3 for p in self.p}})
    def test_cannot_adjust_away_reserved_units(self):
        self.receive(self.purchase(),5);self.action('reserve',self.order(4))
        with self.assertRaises(Problem):self.call('adjust',product_id=self.p[0],warehouse_id=self.warehouse,quantity=2,direction='out',reason='盘亏')
        self.assertEqual(self.balances(),[(5,4,1)]*2)
    def test_unknown_entities_and_duplicates_and_bad_amounts(self):
        with self.assertRaises(Problem):self.call('purchase',supplier_id='missing',warehouse_id=self.warehouse,lines=self.lines())
        with self.assertRaises(Problem):self.call('purchase',supplier_id=self.supplier,warehouse_id=self.warehouse,lines=[self.lines()[0]]*2)
        for amount in ['NaN','Infinity','-1','0.001',True]:
            with self.assertRaises(Problem):self.call('purchase',supplier_id=self.supplier,warehouse_id=self.warehouse,lines=[{**self.lines()[0],'unit_price':amount}])
    def test_unencodable_unicode_is_rejected_before_request_fingerprint(self):
        before=self.ops.state()['entities']
        with self.assertRaises(Problem):
            self.ops.transact('entity',{'request_id':'bad-unicode','kind':'supplier','name':'供应商\ud800'})
        self.assertEqual(self.ops.state()['entities'],before)
    def test_order_identity_is_shop_scoped(self):
        b=dict(shop_id=self.shop,warehouse_id=self.warehouse,external_id='1',currency='SAR',lines=self.lines())
        self.call('order',**b)
        with self.assertRaises(Problem):self.call('order',**b)
        b['shop_id']=self.call('entity',kind='shop',name='另一店')['id'];self.call('order',**b)
    def test_stale_revision_and_invalid_transition(self):
        self.receive(self.purchase(),5);d=self.order()
        with self.assertRaises(Problem):self.action('ship',d,carrier='x',tracking='1')
        updated=self.action('reserve',d)
        with self.assertRaises(Problem):self.action('cancel',d,reason='stale')
        shipped=self.action('ship',updated,carrier='x',tracking='1')
        with self.assertRaises(Problem):self.action('cancel',shipped,reason='shipped')
    def test_concurrent_reservation_cannot_oversell(self):
        self.receive(self.purchase(3),3);a=self.order(2);b=self.order(2)
        def reserve(d):
            try:self.action('reserve',d);return True
            except Problem:return False
        with concurrent.futures.ThreadPoolExecutor(2) as ex:result=list(ex.map(reserve,[a,b]))
        self.assertEqual(sorted(result),[False,True]);self.assertEqual(self.balances(),[(3,2,1)]*2)
    def test_reopen_keeps_ledger(self):
        self.receive(self.purchase(3),3);other=Operations(Store(self.tmp.name))
        self.assertEqual(self.ops.state(),other.state())
    def test_receive_excess_atomic_and_unknown_product(self):
        d=self.purchase();q={self.p[0]:2,self.p[1]:6}
        with self.assertRaises(Problem):self.action('receive',d,quantities=q)
        self.assertEqual(self.balances(),[])
        with self.assertRaises(Problem):self.action('receive',d,quantities={'unknown':2})
    def test_demo_product_not_used_in_real_operations(self):
        pid=self.store.import_rows([{'title_zh':'演示'}],demo=True)['created'][0]
        with self.assertRaises(Problem):self.call('adjust',product_id=pid,warehouse_id=self.warehouse,quantity=1,direction='in',reason='test')

if __name__=='__main__':unittest.main()
