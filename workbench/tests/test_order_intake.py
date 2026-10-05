import concurrent.futures
import csv
import io
import tempfile
import unittest
import sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Store, Problem, ident
from operations import Operations
from order_intake import OrderIntake

class OrderIntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.store=Store(self.tmp.name); self.ops=Operations(self.store)
        self.app=SimpleNamespace(store=self.store,ops=self.ops); self.intake=OrderIntake(self.app)
        self.ids=self.store.import_rows([{'title_zh':'商品甲'},{'title_zh':'商品乙'}])['created']
        with self.store.connect() as c:
            import json
            self.skus=[json.loads(c.execute('SELECT data FROM products WHERE id=?',(p,)).fetchone()[0])['partner_sku'] for p in self.ids]
        self.shop=self.entity('shop','店铺');self.warehouse=self.entity('warehouse','仓库')
    def tearDown(self): self.tmp.cleanup()
    def entity(self,kind,name): return self.ops.transact('entity',{'request_id':ident(),'kind':kind,'name':name})['id']
    def rows(self,external='ONE'):
        return [{'external_id':external,'partner_sku':s,'quantity':2,'unit_price':'0.10','currency':'SAR','order_total':'0.40','order_date':'2026-10-03'} for s in self.skus]
    def preview(self,rows=None,**extra): return self.intake.preview({'rows':rows if rows is not None else self.rows(),'shop_id':self.shop,'warehouse_id':self.warehouse,**extra})
    def apply(self,p,request=None): return self.intake.apply({'token':p['token'],'request_id':request or ident(),'confirmed':True})
    def orders(self): return self.ops.state('orders')['documents']
    def test_multiline_money_provenance_stock_and_reopen(self):
        p=self.preview();self.assertEqual(p['ready'],1);self.assertTrue(p['can_apply'])
        result=self.apply(p);self.assertEqual(result['created'],1)
        d=self.orders()[0];self.assertEqual(d['total_cents'],40);self.assertEqual(d['status'],'new');self.assertEqual(d['origin'],'file_import');self.assertEqual(d['order_date'],'2026-10-03T00:00:00')
        self.assertEqual(self.ops.state('inventory')['stock'],[])
        self.assertEqual(OrderIntake(self.app).state()['receipts'][0],result)
    def test_duplicates_and_conflicts_shop_scope(self):
        self.apply(self.preview());p=self.preview();self.assertEqual(p['duplicates'],1);self.assertEqual(self.apply(p)['created'],0)
        rows=self.rows();rows[0]['quantity']=3;p=self.preview(rows);self.assertFalse(p['can_apply']);self.assertTrue(any('总额' in e['reason'] or '已有' in e['reason'] for e in p['errors']))
        other=self.entity('shop','另一店');p=self.preview(shop_id=other);self.assertEqual(p['ready'],1);self.apply(p);self.assertEqual(len(self.orders()),2)
    def test_bad_quantity_amount_currency_dates_total(self):
        for field,value in [('quantity',True),('quantity','1.5'),('unit_price','NaN'),('unit_price','0.001'),('unit_price','-1'),('currency','EUR'),('order_date','2026-02-30'),('order_total','99')]:
            with self.subTest(field=field,value=value):
                rows=self.rows();rows[0][field]=value;p=self.preview(rows);self.assertFalse(p['can_apply']);self.assertTrue(p['errors'])
        self.assertEqual(self.orders(),[])
    def test_unmapped_ambiguous_demo_and_inconsistent_order(self):
        rows=self.rows();rows[0]['partner_sku']='unknown';p=self.preview(rows);self.assertIn('未映射',p['errors'][0]['reason'])
        with self.store.connect() as c:
            c.execute("UPDATE products SET data=json_set(data,'$.partner_sku',?) WHERE id=?",(self.skus[0],self.ids[1]))
        p=self.preview();self.assertIn('货号冲突',p['errors'][0]['reason'])
        with self.store.connect() as c:
            c.execute("UPDATE products SET data=json_set(data,'$.partner_sku',?,'$.demo',json('true')) WHERE id=?",(self.skus[1],self.ids[1]))
        p=self.preview();self.assertTrue(any('示例' in e['reason'] for e in p['errors']))
        rows=self.rows();rows[1]['currency']='USD';p=self.preview(rows);self.assertTrue(p['errors'])
    def test_preview_binds_product_order_entity_state(self):
        for kind in ('product','order','entity'):
            with self.subTest(kind=kind):
                p=self.preview(self.rows(ident()))
                with self.store.connect() as c:
                    if kind=='product': c.execute('UPDATE products SET revision=revision+1 WHERE id=?',(self.ids[0],))
                    elif kind=='entity': c.execute("UPDATE ops_entities SET data=json_set(data,'$.name','已变更') WHERE id=?",(self.shop,))
                    else:
                        order=p['rows'][0];self.ops.order(c,{**order,'lines':[{'product_id':l['product_id'],'quantity':l['quantity'],'unit_price':'0.10'} for l in order['lines']]})
                before=len(self.orders())
                with self.assertRaises(Problem):self.apply(p)
                self.assertEqual(len(self.orders()),before)
    def test_request_replay_conflict_and_concurrency(self):
        p=self.preview();request=ident();r=self.apply(p,request);self.assertEqual(self.apply(p,request),r)
        p2=self.preview(self.rows('TWO'))
        with self.assertRaises(Problem):self.apply(p2,request)
        def apply(_):return self.apply(p2)
        with concurrent.futures.ThreadPoolExecutor(2) as pool: results=list(pool.map(apply,range(2)))
        self.assertEqual(results[0],results[1]);self.assertEqual(len(self.orders()),2)
        alias=ident();self.assertEqual(self.apply(p2,alias),results[0])
        p3=self.preview(self.rows('THREE'))
        with self.assertRaises(Problem):self.apply(p3,alias)
    def test_transaction_rolls_back_failed_shared_order(self):
        rows=self.rows('ONE')+self.rows('TWO');p=self.preview(rows);original=self.ops.order;calls=[]
        def fail(c,b):
            calls.append(b['external_id'])
            if len(calls)==2:raise Problem('模拟第二单失败')
            return original(c,b)
        self.ops.order=fail
        with self.assertRaises(Problem):self.apply(p)
        self.assertEqual(self.orders(),[]);self.assertEqual(self.intake.state()['total'],0)
        self.ops.order=original;self.assertEqual(self.apply(p)['created'],2)
    def test_csv_mapping_json_template_and_required_columns(self):
        out=io.StringIO();writer=csv.writer(out);writer.writerow(['单号','货号','件数','价格','币种']);writer.writerow(['CSV',self.skus[0],2,'0.10','SAR'])
        p=self.intake.preview({'csv':out.getvalue(),'shop_id':self.shop,'warehouse_id':self.warehouse,'mapping':{'external_id':0,'partner_sku':1,'quantity':2,'unit_price':3,'currency':4}});self.assertEqual(p['ready'],1);self.apply(p)
        import json
        p=self.intake.preview({'json':json.dumps(self.rows('JSON')),'shop_id':self.shop,'warehouse_id':self.warehouse});self.assertEqual(p['ready'],1)
        self.assertIn('external_id',self.intake.template())
        p=self.preview(mapping={'external_id':0});self.assertFalse(p['can_apply'])
        with self.assertRaises(Problem):self.intake.preview({'csv':'a,a\nx,x','shop_id':self.shop,'warehouse_id':self.warehouse})
    def test_limit_5000_merge_lines_and_history_pagination(self):
        row=self.rows()[0];row.pop('order_total');row.pop('order_date')
        p=self.preview([row]*5000);self.assertEqual(p['row_count'],5000);self.assertEqual(p['rows'][0]['lines'][0]['quantity'],10000);self.apply(p)
        with self.assertRaises(Problem):self.preview([row]*5001)
        for i in range(21):self.apply(self.preview([{**row,'external_id':str(i)}]))
        self.assertEqual(len(self.intake.state()['receipts']),20);self.assertEqual(len(self.intake.state(1)['receipts']),2)
        for page in (-1,True,'x'):
            with self.assertRaises(Problem):self.intake.state(page)
    def test_expired_confirmation(self):
        p=self.preview()
        with self.store.connect() as c:c.execute("UPDATE order_intake_previews SET created_at='2000-01-01T00:00:00+00:00' WHERE token=?",(p['token'],))
        with self.assertRaises(Problem):self.apply(p)
        self.assertEqual(self.orders(),[])
    def test_confirmation_missing_or_invalid_token(self):
        p=self.preview()
        for body in ({'token':p['token'],'request_id':ident()},{'token':'invalid','request_id':ident(),'confirmed':True}):
            with self.assertRaises(Problem):self.intake.apply(body)
        self.assertEqual(self.orders(),[])

if __name__=='__main__':unittest.main()
