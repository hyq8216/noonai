import concurrent.futures
import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Store,Problem,ident
from operations import Operations
from fulfillment import Fulfillment,csv_cell

class FulfillmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name);self.ops=Operations(self.store)
        self.app=SimpleNamespace(store=self.store,ops=self.ops);self.fulfillment=Fulfillment(self.app)
        self.pids=self.store.import_rows([{'title_zh':'商品甲'},{'title_zh':'商品乙'}])['created']
        self.warehouse=self.call('entity',kind='warehouse',name='仓库')['id'];self.shop=self.call('entity',kind='shop',name='店铺')['id']
    def tearDown(self):self.tmp.cleanup()
    def call(self,action,**body):return self.ops.transact(action,{'request_id':ident(),**body})
    def stock(self,n=10,pid=None,wid=None):return self.call('adjust',product_id=pid or self.pids[0],warehouse_id=wid or self.warehouse,quantity=n,direction='in',reason='测试期初')
    def order(self,n=2,lines=None,wid=None,external=None):return self.call('order',shop_id=self.shop,warehouse_id=wid or self.warehouse,external_id=external or ident(),currency='SAR',lines=lines or [{'product_id':self.pids[0],'quantity':n,'unit_price':'1.25'}])
    def preview(self,orders,action='reserve',shipments=None):return self.fulfillment.preview({'order_ids':[o['id'] for o in orders],'action':action,**({'shipments':shipments} if shipments is not None else {})})
    def apply(self,p,request=None):return self.fulfillment.apply({'token':p['token'],'request_id':request or ident(),'confirmed':True})
    def balances(self):return self.ops.state('inventory')['stock']
    def shipments(self,orders):return [{'order_id':o['id'],'carrier':'测试物流','tracking':'T-'+str(i)} for i,o in enumerate(orders)]
    def test_cumulative_shortage_blocks_whole_batch_deterministically(self):
        self.stock(3);a=self.order(2);b=self.order(2)
        with self.store.connect() as c:
            c.execute("UPDATE ops_documents SET data=json_set(data,'$.created_at','2020-01-01') WHERE id=?",(a['id'],))
            c.execute("UPDATE ops_documents SET data=json_set(data,'$.created_at','2021-01-01') WHERE id=?",(b['id'],))
        p=self.preview([b,a]);self.assertFalse(p['can_apply']);self.assertEqual(p['shortages'][0]['order_id'],b['id']);self.assertEqual(p['shortages'][0]['shortage'],1)
        self.assertEqual([o['id'] for o in p['orders']],[a['id'],b['id']]);self.assertEqual(self.balances()[0]['reserved'],0)
        self.assertEqual(p['picks'][0]['quantity'],4)
    def test_reserve_ship_conservation_receipts_and_reopen(self):
        self.stock(10);orders=[self.order(2),self.order(3)]
        reserve=self.apply(self.preview(orders));self.assertEqual(reserve['units'],5);self.assertEqual(len(reserve['picks']),1)
        stock=self.balances()[0];self.assertEqual((stock['on_hand'],stock['reserved'],stock['available']),(10,5,5))
        with self.store.connect() as c:reserved=[json.loads(c.execute('SELECT data FROM ops_documents WHERE id=?',(o['id'],)).fetchone()[0]) for o in orders]
        ship=self.apply(self.preview(reserved,'ship',self.shipments(reserved)))
        stock=self.balances()[0];self.assertEqual((stock['on_hand'],stock['reserved'],stock['available']),(5,0,5))
        self.assertEqual(ship['order_count'],2);self.assertTrue(all(o['status']=='shipped' for o in ship['orders']))
        reopened=Fulfillment(self.app);self.assertEqual(reopened.state()['waves'][0],ship);self.assertEqual(reopened.state()['orders'],[])
        exported=list(csv.reader(io.StringIO(reopened.export(reserve['id']).lstrip('\ufeff'))));self.assertEqual(exported[1][5],'5');self.assertIn('不是承运商',exported[1][-1])
    def test_preview_stale_stock_product_and_order(self):
        self.stock(10);order=self.order()
        for target in ('stock','sku','revision'):
            with self.subTest(target=target):
                p=self.preview([order]);self.assertTrue(p['can_apply'])
                with self.store.connect() as c:
                    if target=='stock':c.execute('UPDATE ops_stock SET on_hand=on_hand+1 WHERE product_id=?',(self.pids[0],))
                    elif target=='sku':c.execute("UPDATE products SET data=json_set(data,'$.partner_sku','CHANGED') WHERE id=?",(self.pids[0],))
                    else:c.execute('UPDATE ops_documents SET revision=revision+1,data=json_set(data,\'$.revision\',revision+1) WHERE id=?',(order['id'],))
                with self.assertRaises(Problem):self.apply(p)
                if target=='sku':
                    p2=self.preview([order]);self.assertFalse(p2['can_apply']);self.assertIn('货号',p2['errors'][0]['reason'])
                    with self.store.connect() as c:c.execute("UPDATE products SET data=json_set(data,'$.partner_sku',?) WHERE id=?",(order['lines'][0]['sku'],self.pids[0]))
        self.assertEqual(self.balances()[0]['reserved'],0)
    def test_ship_requires_reserved_carrier_tracking_unique(self):
        self.stock();a=self.order();b=self.order()
        p=self.preview([a],'ship',self.shipments([a]));self.assertFalse(p['can_apply']);self.assertIn('已占用',p['errors'][0]['reason'])
        self.apply(self.preview([a,b]))
        p=self.preview([a,b],'ship',[]);self.assertEqual(len(p['errors']),2)
        duplicate=[{'order_id':o['id'],'carrier':'DHL','tracking':'same'} for o in [a,b]]
        p=self.preview([a,b],'ship',duplicate);self.assertFalse(p['can_apply']);self.assertTrue(any('共用' in e['reason'] for e in p['errors']))
        self.apply(self.preview([a],'ship',[duplicate[0]]))
        p=self.preview([b],'ship',[duplicate[1]]);self.assertFalse(p['can_apply']);self.assertTrue(any('已用于' in e['reason'] for e in p['errors']))
    def test_tracking_race_invalidates_preview(self):
        self.stock();a=self.order();b=self.order();self.apply(self.preview([a,b]))
        shipment={'order_id':a['id'],'carrier':'DHL','tracking':'RACE'};p=self.preview([a],'ship',[shipment]);self.assertTrue(p['can_apply'])
        self.call('ship',id=b['id'],revision=2,carrier='dhl',tracking='race')
        with self.assertRaises(Problem):self.apply(p)
        self.assertEqual(self.balances()[0]['reserved'],2)
    def test_replay_alias_request_conflict_and_parallel_confirm(self):
        self.stock();a=self.order();p=self.preview([a]);request=ident()
        def apply(_):return self.apply(p,request)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(apply,range(2)))
        self.assertEqual(results[0],results[1]);self.assertEqual(self.balances()[0]['reserved'],2)
        alias=ident();self.assertEqual(self.apply(p,alias),results[0])
        b=self.order();other=self.preview([b])
        for key in (request,alias):
            with self.assertRaises(Problem):self.apply(other,key)
        self.assertEqual(len(self.fulfillment.state()['waves']),1)
    def test_competing_previews_do_not_oversell(self):
        self.stock(3);a=self.order(2);b=self.order(2);previews=[self.preview([a]),self.preview([b])]
        def apply(p):
            try:self.apply(p);return True
            except Problem:return False
        with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(apply,previews))
        self.assertEqual(sorted(results),[False,True]);self.assertEqual(self.balances()[0]['reserved'],2)
    def test_second_operation_failure_rolls_back_orders_movements_wave(self):
        self.stock();a=self.order();b=self.order();p=self.preview([a,b]);original=self.ops.reserve;calls=[];before=self.ops.state()
        def fail(c,body):
            calls.append(body['id'])
            if len(calls)==2:raise Problem('测试后单失败')
            return original(c,body)
        self.ops.reserve=fail
        with self.assertRaises(Problem):self.apply(p)
        self.assertEqual(self.ops.state(),before);self.assertEqual(self.fulfillment.state()['waves'],[])
        self.ops.reserve=original;self.assertEqual(self.apply(p)['order_count'],2)
    def test_shipping_failure_is_atomic_and_unicode_tracking_is_unique(self):
        self.stock();a=self.order();b=self.order();self.apply(self.preview([a,b]))
        p=self.preview([a,b],'ship',self.shipments([a,b]));original=self.ops.ship;calls=[];before=self.ops.state()
        def fail(c,body):
            calls.append(body['id'])
            if len(calls)==2:raise Problem('模拟后单发货失败')
            return original(c,body)
        self.ops.ship=fail
        with self.assertRaises(Problem):self.apply(p)
        self.assertEqual(self.ops.state(),before)
        self.ops.ship=original
        self.call('ship',id=a['id'],revision=2,carrier='DHL',tracking='Straße-Ä')
        other=self.preview([b],'ship',[{'order_id':b['id'],'carrier':'dhl','tracking':'STRASSE-ä'}])
        self.assertFalse(other['can_apply']);self.assertTrue(any('已用于' in e['reason'] for e in other['errors']))
    def test_ship_checks_cumulative_reserved_balance(self):
        self.stock();a=self.order();b=self.order();self.apply(self.preview([a,b]))
        with self.store.connect() as c:c.execute('UPDATE ops_stock SET reserved=3 WHERE product_id=?',(self.pids[0],))
        p=self.preview([a,b],'ship',self.shipments([a,b]));self.assertFalse(p['can_apply']);self.assertEqual(sum(s['shortage'] for s in p['shortages']),1)
    def test_multiple_warehouses_do_not_share_balances(self):
        other=self.call('entity',kind='warehouse',name='第二仓')['id'];self.stock(10);orders=[self.order(),self.order(wid=other)]
        p=self.preview(orders);self.assertFalse(p['can_apply']);self.assertEqual(p['shortages'][0]['warehouse_id'],other);self.assertEqual(len(p['picks']),2)
    def test_formula_safe_export_and_bounded_state(self):
        self.stock(200)
        with self.store.connect() as c:c.execute("UPDATE products SET data=json_set(data,'$.title_zh','=EVIL()') WHERE id=?",(self.pids[0],))
        order=self.order(external='@BAD');wave=self.apply(self.preview([order]));rows=list(csv.reader(io.StringIO(self.fulfillment.export(wave['id']).lstrip('\ufeff'))))
        self.assertTrue(rows[1][4].startswith("'="));self.assertTrue(rows[1][6].startswith("'@"))
        for value in [' =CMD()','+CMD()','-CMD()','@CMD()','\tCMD']:self.assertTrue(csv_cell(value).startswith("'"))
        for _ in range(52):self.order()
        self.assertEqual(len(self.fulfillment.state()['orders']),50);self.assertEqual(len(self.fulfillment.state(1)['orders']),3)
        for page in (-1,True,'x'):
            with self.assertRaises(Problem):self.fulfillment.state(page)
    def test_invalid_selection_confirmation_and_expiry(self):
        self.stock();order=self.order()
        for ids in ([],[order['id']]*2,['missing']*101):
            with self.assertRaises(Problem):self.fulfillment.preview({'action':'reserve','order_ids':ids})
        p=self.preview([order])
        with self.assertRaises(Problem):self.fulfillment.apply({'token':p['token'],'request_id':ident()})
        with self.store.connect() as c:c.execute("UPDATE fulfillment_previews SET created_at='2000-01-01T00:00:00+00:00'")
        with self.assertRaises(Problem):self.apply(p)
        with self.assertRaises(Problem):self.fulfillment.export('unknown')

if __name__=='__main__':unittest.main()
