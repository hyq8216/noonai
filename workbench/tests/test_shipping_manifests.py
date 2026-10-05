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
from finance import Finance
from shipping_manifests import ShippingManifests

class ShippingManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name);self.ops=Operations(self.store);self.finance=Finance(self.store)
        self.app=SimpleNamespace(store=self.store,ops=self.ops);self.service=ShippingManifests(self.app)
        self.pid=self.store.import_rows([{'title_zh':'包装测试商品'}])['created'][0]
        self.warehouse=self.call('entity',kind='warehouse',name='测试仓')['id'];self.shop=self.call('entity',kind='shop',name='测试店')['id']
        self.call('adjust',product_id=self.pid,warehouse_id=self.warehouse,quantity=1000,direction='in',reason='合成期初')
        self.orders=[self.shipped(i) for i in range(2)]
    def tearDown(self):self.tmp.cleanup()
    def call(self,action,**body):return self.ops.transact(action,{'request_id':ident(),**body})
    def shipped(self,i):
        d=self.call('order',shop_id=self.shop,warehouse_id=self.warehouse,external_id='SYN-PACK-'+str(i),currency='SAR',lines=[{'product_id':self.pid,'quantity':2,'unit_price':'3.25'}])
        d=self.call('reserve',id=d['id'],revision=d['revision'])
        return self.call('ship',id=d['id'],revision=d['revision'],carrier='测试承运商',tracking='T-'+str(i))
    def packages(self,orders=None):
        return [{'order_id':o['id'],'carrier':o['carrier'],'tracking':o['tracking'],'box_no':'BOX-'+str(i),'weight_kg':'0.250','length_cm':'20','width_cm':'10','height_cm':'5'} for i,o in enumerate(orders or self.orders)]
    def preview(self,orders=None,packages=None,**extra):
        orders=orders or self.orders
        return self.service.preview({'order_ids':[o['id'] for o in orders],'packages':packages if packages is not None else self.packages(orders),**extra})
    def apply(self,p,request=None):return self.service.apply({'token':p['token'],'request_id':request or ident(),'confirmed':True})
    def handoff(self,m,**extra):return self.service.handoff({'id':m['id'],'revision':m['revision'],'recipient':'合成接收人','evidence':'合成实际交接凭证','confirmed':True,'request_id':ident(),**extra})
    def test_create_handoff_export_and_no_stock_finance_order_effect(self):
        before=self.ops.state();finance=self.finance.state();p=self.preview();m=self.apply(p)
        self.assertEqual(m['revision'],1);self.assertEqual(m['status'],'draft');self.assertEqual(len(m['packages']),2)
        self.assertEqual(m['packages'][0]['weight_kg'],'0.25')
        handed=self.handoff(m);self.assertEqual(handed['revision'],2);self.assertEqual(handed['status'],'handed_off')
        self.assertEqual(self.ops.state(),before);self.assertEqual(self.finance.state(),finance)
        self.assertTrue(all(o['status']=='shipped' for o in self.ops.state('orders')['documents']))
        for kind in ('packages','sku','handoff'):
            exported=self.service.export(m['id'],kind);self.assertTrue(exported.startswith('\ufeff'));rows=list(csv.reader(io.StringIO(exported.lstrip('\ufeff'))));self.assertEqual(len(rows),3);self.assertIn('不是承运商电子面单',rows[1][-1])
        reopened=ShippingManifests(self.app);s=reopened.state();self.assertEqual(s['manifests'][0]['revision'],2);self.assertEqual(len(reopened.get(s['manifests'][0]['id'])['audit']),2)
    def test_unknown_measurements_remain_missing_until_revision_completes(self):
        packages=self.packages();packages[0]['weight_kg']='';packages[1].pop('height_cm')
        p=self.preview(packages=packages);self.assertTrue(p['can_apply']);self.assertEqual(len(p['missing']),2)
        m=self.apply(p)
        self.assertIn('待实测',self.service.export(m['id']))
        with self.assertRaises(Problem):self.handoff(m)
        revision=self.preview(id=m['id'],revision=m['revision']);updated=self.apply(revision)
        self.assertEqual(updated['revision'],2);self.assertEqual(self.handoff(updated)['revision'],3)
    def test_bad_numeric_values_tracking_box_and_foreign_order(self):
        for field,value in [('weight_kg',True),('weight_kg',0),('weight_kg','NaN'),('weight_kg','0.0001'),('length_cm','-1'),('height_cm','1.001'),('width_cm','Infinity'),('tracking','WRONG'),('carrier','OTHER')]:
            with self.subTest(field=field,value=value):
                packages=self.packages();packages[0][field]=value;p=self.preview(packages=packages);self.assertFalse(p['can_apply']);self.assertTrue(p['errors'])
        packages=self.packages();packages[1]['box_no']=packages[0]['box_no'].lower();self.assertFalse(self.preview(packages=packages)['can_apply'])
        packages=self.packages();packages[1]['order_id']=packages[0]['order_id'];self.assertFalse(self.preview(packages=packages)['can_apply'])
        reserved=self.call('order',shop_id=self.shop,warehouse_id=self.warehouse,external_id='UNSHIPPED',currency='SAR',lines=[{'product_id':self.pid,'quantity':1,'unit_price':'3'}])
        self.assertFalse(self.preview(orders=[reserved],packages=[{'order_id':reserved['id'],'carrier':'X','tracking':'Y','box_no':'B'}])['can_apply'])
    def test_preview_stale_product_order_stock_and_movement_roundtrip(self):
        for target in ('product','order','stock','roundtrip'):
            with self.subTest(target=target):
                p=self.preview()
                if target=='product':self.store.update(self.pid,{'note':ident()},self.store.get(self.pid)['revision'])
                elif target=='order':
                    with self.store.connect() as c:c.execute('UPDATE ops_documents SET revision=revision+1,data=json_set(data,\'$.revision\',revision+1) WHERE id=?',(self.orders[0]['id'],))
                elif target=='stock':self.call('adjust',product_id=self.pid,warehouse_id=self.warehouse,quantity=1,direction='in',reason='合成修改库存')
                else:
                    self.call('adjust',product_id=self.pid,warehouse_id=self.warehouse,quantity=1,direction='out',reason='合成先出')
                    self.call('adjust',product_id=self.pid,warehouse_id=self.warehouse,quantity=1,direction='in',reason='合成后入')
                with self.assertRaises(Problem):self.apply(p)
        self.assertEqual(self.service.state()['total'],0)
    def test_draft_source_changes_require_repreview_historical_handoff_is_explicit(self):
        m=self.apply(self.preview());self.store.update(self.pid,{'title_zh':'新事实商品名'},1)
        self.assertTrue(self.service.state()['manifests'][0]['source_changed'])
        with self.assertRaises(Problem):self.handoff(m)
        with self.assertRaises(Problem):self.service.export(m['id'])
        fixed=self.apply(self.preview(id=m['id'],revision=m['revision']));self.assertEqual(fixed['packages'][0]['lines'][0]['title'],'新事实商品名')
        handed=self.handoff(fixed);self.call('adjust',product_id=self.pid,warehouse_id=self.warehouse,quantity=1,direction='in',reason='新的实际库存')
        self.assertIn('当前来源已变化',self.service.export(handed['id']))
        with self.assertRaises(Problem):self.preview(id=handed['id'],revision=handed['revision'])
    def test_missing_product_or_sku_change_cannot_reuse_old_identity(self):
        with self.store.connect() as c:c.execute("UPDATE products SET data=json_set(data,'$.partner_sku','CHANGED') WHERE id=?",(self.pid,))
        p=self.preview();self.assertFalse(p['can_apply']);self.assertTrue(any('货号' in e['reason'] for e in p['errors']))
    def test_revision_conflicts_and_one_order_cannot_enter_two_manifests(self):
        p=self.preview(orders=[self.orders[0]]);other=self.preview(orders=[self.orders[0]])
        m=self.apply(p)
        with self.assertRaises(Problem):self.apply(other)
        self.assertFalse(self.preview(orders=[self.orders[0]])['can_apply'])
        change=self.preview(orders=[self.orders[0]],id=m['id'],revision=1);competing=self.preview(orders=[self.orders[0]],id=m['id'],revision=1)
        updated=self.apply(change);self.assertEqual(updated['revision'],2)
        with self.assertRaises(Problem):self.apply(competing)
        with self.assertRaises(Problem):self.preview(orders=[self.orders[0]],id=m['id'],revision=1)
        with self.assertRaises(Problem):self.handoff(m)
    def test_replay_alias_key_conflict_parallel_and_handoff_retry(self):
        p=self.preview();key=ident()
        with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:self.apply(p,key),range(2)))
        self.assertEqual(results[0],results[1]);self.assertEqual(self.service.state()['total'],1)
        alias=ident();self.assertEqual(self.apply(p,alias),results[0])
        m=results[0];other=self.preview(id=m['id'],revision=1)
        with self.assertRaises(Problem):self.apply(other,alias)
        body={'id':m['id'],'revision':1,'recipient':'接收人','evidence':'凭据','confirmed':True,'request_id':ident()}
        handed=self.service.handoff(body);self.assertEqual(self.service.handoff(body),handed)
        with self.assertRaises(Problem):self.service.handoff({**body,'recipient':'其他人'})
        self.assertEqual(len(self.service.get(m['id'])['audit']),2)
    def test_transaction_failure_rolls_back_manifest_ownership_audit_receipt(self):
        p=self.preview();original=self.service.save
        def fail(c,d,action):original(c,d,action);raise Problem('模拟保存后故障')
        self.service.save=fail
        with self.assertRaises(Problem):self.apply(p)
        with self.store.connect() as c:
            for table in ('shipping_manifests','shipping_manifest_orders','shipping_manifest_audit','shipping_manifest_receipts','shipping_manifest_requests'):self.assertEqual(c.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
        self.service.save=original;self.assertEqual(self.apply(p)['revision'],1)
    def test_formula_safe_exports_pagination_search_and_bad_pages(self):
        packages=self.packages();packages[0]['box_no']=' =EVIL()'
        with self.store.connect() as c:c.execute("UPDATE products SET data=json_set(data,'$.title_zh','@EVIL()') WHERE id=?",(self.pid,))
        m=self.apply(self.preview(packages=packages));rows=list(csv.reader(io.StringIO(self.service.export(m['id'],'sku').lstrip('\ufeff'))))
        self.assertTrue(any(row[3].startswith("'=") for row in rows[1:]));self.assertTrue(rows[1][6].startswith("'@"))
        for i in range(2,53):
            order=self.shipped(i);self.apply(self.preview(orders=[order]))
        state=self.service.state();self.assertEqual(len(state['manifests']),50);self.assertEqual(len(state['orders']),50)
        self.assertEqual(len(self.service.state(page=1)['manifests']),2);self.assertEqual(len(self.service.state(order_page=1)['orders']),3)
        searched=self.service.state(query='SYN-PACK-52');self.assertEqual(searched['total'],1);self.assertEqual(searched['order_total'],1)
        for page in (True,-1,'bad'):
            with self.assertRaises(Problem):self.service.state(page)
    def test_compact_state_does_not_repeat_full_packages_and_get_returns_one(self):
        # Large manifest is created through real preview/confirmation, not direct SQL.
        orders=self.orders+[self.shipped(i) for i in range(2,100)]
        first=self.apply(self.preview(orders=orders))
        extra=self.shipped(100);second=self.apply(self.preview(orders=[extra]))
        listing=self.service.state();summaries={m['id']:m for m in listing['manifests']}
        self.assertEqual(summaries[first['id']]['package_count'],100)
        self.assertEqual(summaries[first['id']]['order_count'],100)
        for summary in listing['manifests']:
            for key in ('packages','order_ids','source_fingerprint','audit','orders'):self.assertNotIn(key,summary)
        self.assertTrue(all('lines' not in o and len(o['skus'])<=3 for o in listing['orders']))
        fetched=self.service.get(first['id']);self.assertEqual(len(fetched['packages']),100);self.assertEqual(len(fetched['order_ids']),100)
        self.assertEqual(self.service.get(second['id'])['order_ids'],[extra['id']])
        self.assertLess(len(json.dumps(listing['manifests'])),len(json.dumps(fetched))//10)
        self.store.update(self.pid,{'note':'changed sources'},1)
        self.assertTrue(all(m['source_stale'] for m in self.service.state()['manifests']))
        self.assertTrue(self.service.get(first['id'])['source_changed'])
        with self.assertRaises(Problem):self.service.get('missing')
    def test_expiry_confirmation_and_selection_limits(self):
        p=self.preview()
        with self.assertRaises(Problem):self.service.apply({'token':p['token'],'request_id':ident()})
        with self.store.connect() as c:c.execute("UPDATE shipping_manifest_previews SET created_at='2000-01-01T00:00:00+00:00'")
        with self.assertRaises(Problem):self.apply(p)
        for ids in ([],['x']*101,[self.orders[0]['id']]*2):
            with self.assertRaises(Problem):self.service.preview({'order_ids':ids,'packages':[]})
        with self.assertRaises(Problem):self.service.export('missing')

if __name__=='__main__':unittest.main()
