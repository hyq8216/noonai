"""Supply/demand conservation from explicit local ledgers, with no forecast."""
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

class ReplenishmentDeepeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name);self.ops=Operations(self.store)
        self.r=Replenishment(SimpleNamespace(store=self.store));self.proc=Procurement(SimpleNamespace(store=self.store))
        self.pid=self.store.import_rows([{'title_zh':'=深化测试','source_sku':'R-D'}])['created'][0]
        self.wh=self.op('entity',kind='warehouse',name='目标仓')['id'];self.src=self.op('entity',kind='warehouse',name='调出仓')['id']
        self.shop=self.op('entity',kind='shop',name='合成店')['id'];self.supplier=self.op('entity',kind='supplier',name='合成供货')['id']
    def tearDown(self):self.tmp.cleanup()
    def op(self,action,**b):return self.ops.transact(action,{'request_id':ident(),**b})
    def stock(self,n=10,wid=None):return self.op('adjust',product_id=self.pid,warehouse_id=wid or self.wh,quantity=n,direction='in',reason='合成盘点')
    def order(self,n=4):return self.op('order',shop_id=self.shop,warehouse_id=self.wh,external_id='=合成订单'+ident(),currency='SAR',lines=[{'product_id':self.pid,'quantity':n,'unit_price':'1'}])
    def transfer(self,n=6):
        self.stock(n,self.src)
        return self.op('transfer',source_warehouse_id=self.src,warehouse_id=self.wh,lines=[{'product_id':self.pid,'quantity':n}],note='合成调拨')
    def body(self,**kw):return {'product_id':self.pid,'warehouse_id':self.wh,'revision':0,'minimum':12,'target':30,'lead_days':7,**kw}
    def row(self):return self.r.state({'warehouse_id':self.wh})['rows'][0]
    def save(self,**kw):return self.r.apply({**self.r.preview(self.body(**kw)),'confirmed':True,'request_id':ident()})
    def stale(self,v):
        with self.assertRaises(Problem) as ctx:self.r.apply({**v,'confirmed':True,'request_id':ident()})
        self.assertEqual(ctx.exception.status,409)

    def test_new_reserved_shipped_cancelled_demands_count_once(self):
        self.stock();o=self.order();self.save();r=self.row()
        self.assertEqual((r['available'],r['unreserved_demand'],r['projected'],r['quantity']),(10,4,6,24))
        v=self.r.preview(self.body(revision=1));o=self.op('reserve',id=o['id'],revision=o['revision']);self.stale(v)
        r=self.row();self.assertEqual((r['available'],r['unreserved_demand'],r['projected'],r['quantity']),(6,0,6,24))
        v=self.r.preview(self.body(revision=1));o=self.op('ship',id=o['id'],revision=o['revision'],carrier='合成',tracking='T');self.stale(v)
        self.assertEqual(self.row()['quantity'],24)
        other=self.order(2);v=self.r.preview(self.body(revision=1));self.op('cancel',id=other['id'],revision=other['revision'],reason='合成取消');self.stale(v)
        self.assertEqual(self.row()['unreserved_demand'],0)
        held=self.order(2);held=self.op('reserve',id=held['id'],revision=held['revision'])
        v=self.r.preview(self.body(revision=1));self.op('cancel',id=held['id'],revision=held['revision'],reason='合成释放占用');self.stale(v)
        self.assertEqual((self.row()['available'],self.row()['unreserved_demand'],self.row()['quantity']),(6,0,24))

    def test_dispatch_and_partial_receipts_conserve_projected_supply(self):
        self.stock();t=self.transfer();self.save();self.assertEqual(self.row()['transfer_pending'],0)
        v=self.r.preview(self.body(revision=1));t=self.op('transfer_dispatch',id=t['id'],revision=t['revision'],evidence='合成出库');self.stale(v)
        self.assertEqual((self.row()['transfer_pending'],self.row()['projected'],self.row()['quantity']),(6,16,14))
        for n in (2,4):
            v=self.r.preview(self.body(revision=1));t=self.op('transfer_receive',id=t['id'],revision=t['revision'],quantities={self.pid:n},evidence='合成验收');self.stale(v)
            self.assertEqual((self.row()['projected'],self.row()['quantity']),(16,14))
        self.assertEqual(self.row()['transfer_pending'],0)
        self.assertEqual(sum(s['on_hand'] for s in self.ops.state()['stock']),16)

    def test_purchase_link_does_not_duplicate_supply_or_erase_new_demand(self):
        self.stock();o=self.order();p=self.op('purchase',supplier_id=self.supplier,warehouse_id=self.wh,lines=[{'product_id':self.pid,'quantity':8,'unit_price':'2'}]);self.save()
        b={'order_id':o['id'],'purchase_id':p['id'],'product_id':self.pid,'quantity':4,'order_revision':o['revision'],'purchase_revision':p['revision'],'link_revision':0}
        before=self.row();self.proc.apply({**self.proc.preview(b),'confirmed':True,'request_id':ident()});after=self.row()
        self.assertEqual((after['purchase_pending'],after['unreserved_demand'],after['quantity']),(8,4,16))
        self.assertEqual(before['quantity'],after['quantity'])
        self.op('receive',id=p['id'],revision=p['revision'],quantities={self.pid:3});self.assertEqual(self.row()['quantity'],16)

    def test_unknown_stock_remains_unknown_with_documented_supply_and_demand(self):
        self.order(9);t=self.transfer();self.op('transfer_dispatch',id=t['id'],revision=t['revision'],evidence='合成出库');self.save()
        r=self.row();self.assertEqual((r['transfer_pending'],r['unreserved_demand']),(6,9))
        for k in ('on_hand','available','projected','quantity'):self.assertIsNone(r[k])
        self.assertEqual(r['risk'],'unknown');self.stock();self.save(revision=1,lead_days=None);self.assertIsNone(self.row()['quantity'])
        self.save(revision=2,minimum=0,target=0,lead_days=0);self.assertEqual(self.row()['quantity'],0)
        self.assertEqual(self.row()['suggestion'],'none')

    def test_filter_and_all_pages_export_follow_same_snapshot_and_csv_safety(self):
        self.stock();self.order(20);self.save()
        self.store.import_rows([{'title_zh':'合成分页'+str(i),'source_sku':'RD-'+str(i)} for i in range(53)])
        s=self.r.state({'warehouse_id':self.wh,'risk':'unknown'});self.assertEqual(s['sku_page']['total'],53)
        last=self.r.state({'warehouse_id':self.wh,'risk':'unknown','page':999});self.assertEqual(last['sku_page']['page'],1);self.assertEqual(len(last['rows']),3)
        rows=list(csv.reader(io.StringIO(self.r.export({'warehouse_id':self.wh,'risk':'unknown','page':1}).decode('utf-8-sig'))))
        header=next(i for i,r in enumerate(rows) if r and r[0]=='warehouse_name');self.assertEqual(len(rows[header+1:]),53)
        needed=self.r.state({'warehouse_id':self.wh,'risk':'shortfall','suggestion':'needed'});self.assertEqual(needed['sku_page']['total'],1)
        data=self.r.export({'warehouse_id':self.wh,'suggestion':'needed'}).decode('utf-8-sig')
        self.assertIn("'=深化测试",data);self.assertIn("'=合成订单",data);self.assertIn('订单依据',data);self.assertNotIn('合成分页',data)
        for b in ({'risk':'bad'},{'suggestion':False}):
            with self.assertRaises(Problem):self.r.state(b)

    def test_sql_filter_classification_matches_source_rows_at_boundaries(self):
        def crosscheck():
            with self.store.connect() as c:
                expected=[self.r._row(c,self.pid,wid) for wid in (self.wh,self.src)]
            for risk in ('','unknown','below_minimum','shortfall','covered'):
                for suggestion in ('','unknown','needed','none'):
                    if not risk and not suggestion:continue
                    want={(r['product_id'],r['warehouse_id']) for r in expected
                          if (not risk or r['risk']==risk) and (not suggestion or r['suggestion']==suggestion)}
                    actual=self.r.state({'risk':risk,'suggestion':suggestion,'page':999})
                    self.assertEqual(actual['sku_page']['total'],len(want),(risk,suggestion))
                    self.assertEqual({(r['product_id'],r['warehouse_id']) for r in actual['rows']},want)
                    self.assertEqual(actual['sku_page']['page'],0)
        # Supply and demand never substitute for an absent stock ledger.
        o=self.order(4);t=self.transfer(6);crosscheck()
        self.stock(10);self.save(minimum=10,target=10,lead_days=0);crosscheck()
        # Projected==target is none, projected==0 is not shortfall.
        t=self.op('transfer_dispatch',id=t['id'],revision=t['revision'],evidence='合成出库');crosscheck()
        self.save(revision=1,minimum=10,target=12,lead_days=0);crosscheck() # projected==target has no suggestion.
        o=self.op('reserve',id=o['id'],revision=o['revision']);crosscheck()
        t=self.op('transfer_receive',id=t['id'],revision=t['revision'],quantities={self.pid:2},evidence='合成部分到仓');crosscheck()
        self.op('cancel',id=o['id'],revision=o['revision'],reason='合成释放');self.order(16);crosscheck()
        self.order(1);crosscheck() # projected=-1 wins over below minimum.
        p=self.op('purchase',supplier_id=self.supplier,warehouse_id=self.wh,lines=[{'product_id':self.pid,'quantity':3,'unit_price':'1'}]);crosscheck()
        p=self.op('receive',id=p['id'],revision=p['revision'],quantities={self.pid:1});crosscheck()
        self.op('cancel',id=p['id'],revision=p['revision'],reason='合成采购取消');crosscheck()
        self.order(1);crosscheck()
        self.save(revision=2,lead_days=None);crosscheck() # Unknown wins over negative projection.

    def test_filtered_page_loads_only_selected_source_details(self):
        from unittest.mock import patch
        self.store.import_rows([{'title_zh':'查询边界'+str(i),'source_sku':'SQL-R'+str(i)} for i in range(53)])
        with patch.object(self.r,'_row',wraps=self.r._row) as detail:
            s=self.r.state({'warehouse_id':self.wh,'risk':'unknown','page':1})
            self.assertEqual(s['sku_page']['total'],54);self.assertEqual(len(s['rows']),4)
            self.assertEqual(detail.call_count,4,'Count/filter must not build details for unselected SKUs')
        with patch.object(self.r,'_row',wraps=self.r._row) as detail:
            s=self.r.state({'warehouse_id':self.wh,'suggestion':'needed'})
            self.assertEqual(s['sku_page']['total'],0);self.assertEqual(detail.call_count,0)
        # Literal search remains part of the SQL candidate filter.
        self.assertEqual(self.r.state({'warehouse_id':self.wh,'risk':'unknown','search':'%'})['sku_page']['total'],0)

if __name__=='__main__':unittest.main()
