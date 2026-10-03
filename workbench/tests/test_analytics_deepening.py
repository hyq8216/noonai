"""Trend business boundaries using only an isolated synthetic database."""
import csv
import io
from datetime import date, timedelta
import unittest
import test_analytics as baseline


class AnalyticsDeepeningTests(unittest.TestCase):
    # Reuse isolated fixture helpers without rerunning inherited test methods.
    setUp = baseline.AnalyticsTests.setUp
    tearDown = baseline.AnalyticsTests.tearDown
    op = baseline.AnalyticsTests.op
    order = baseline.AnalyticsTests.order
    state = baseline.AnalyticsTests.state
    entry = baseline.AnalyticsTests.entry
    update_doc = baseline.AnalyticsTests.update_doc
    deliver = baseline.AnalyticsTests.deliver
    def test_daily_events_previous_cross_year_and_zero_baseline(self):
        d = self.deliver(self.order(quantity=3))
        d = self.op('return', id=d['id'], revision=d['revision'], quantities={self.pid:1}, restock=False, reason='合成退货')
        d['returns'][0]['at'] = '2026-01-02T00:00:00Z'
        self.update_doc(d, created_at='2025-12-31T23:59:59Z', shipped_at='2026-01-01T00:00:00Z')
        t = self.a.state({'from':'2026-01-01','to':'2026-01-02'})['trends']
        self.assertEqual((t['previous']['from'],t['previous']['to']),('2025-12-30','2025-12-31'))
        self.assertEqual(t['comparison']['created_orders']['delta'],-1)
        self.assertEqual(t['comparison']['shipped_units']['percent'],None)
        self.assertEqual([r['shipped_units'] for r in t['daily']],[3,0])
        self.assertEqual([r['returned_units'] for r in t['daily']],[0,1])
        self.assertEqual(t['comparison']['created_orders']['percent'],-100)

    def test_monday_clipped_weeks_and_366_day_limit(self):
        t = self.a.state({'from':'2025-12-31','to':'2026-01-06'})['trends']
        self.assertEqual([(r['from'],r['to']) for r in t['weekly']],[('2025-12-31','2026-01-04'),('2026-01-05','2026-01-06')])
        t = self.a.state({'from':'2024-01-01','to':'2024-12-31'})['trends']
        self.assertEqual(len(t['daily']),366)
        for key in ('created_orders','ordered_units','shipped_units','returned_units'):
            self.assertEqual(sum(r[key] for r in t['weekly']),t['current'][key])

    def test_original_currency_exact_filter_payment_and_void(self):
        d1 = self.order()
        other = self.op('entity',kind='shop',name='上期对比另一店')['id']
        d2 = self.op('order',shop_id=other,warehouse_id=self.warehouse,external_id='other-trend-order',currency='SAR',lines=[{'product_id':self.pid,'quantity':1,'unit_price':'1'}])
        e = self.entry(currency='USD',amount='.03',fx='.5')
        e = self.finance.transact('allocate',{'request_id':'trend-allocation','id':e['id'],'revision':e['revision'],'allocations':[{'order_id':d1['id'],'amount':'.01'},{'order_id':d2['id'],'amount':'.01'}]})
        self.entry(currency='SAR',amount='7',fx='2')
        self.finance.transact('payment',{'request_id':'trend-payment','id':e['id'],'revision':e['revision'],'amount':'.01','fx':'.5','date':self.today,'evidence_key':'trend-paid','account':'合成','evidence':'合成'})
        t = self.state()['trends']
        self.assertEqual(t['current']['finance_currencies'],{'USD':{'income_cents':0,'expense_cents':3},'SAR':{'income_cents':0,'expense_cents':700}})
        filtered = self.state(shop_id=self.shop)['trends']
        self.assertEqual(filtered['current']['finance_currencies'],{'USD':{'income_cents':0,'expense_cents':1}})
        self.assertEqual(filtered['finance_scope'],'allocated_orders')
        void = self.entry(currency='AED',amount='4',fx='2')
        self.finance.transact('void_entry',{'request_id':'trend-void','id':void['id'],'revision':void['revision'],'reason':'合成作废'})
        self.assertNotIn('AED',self.state()['trends']['current']['finance_currencies'])

    def test_observation_window_risk_does_not_claim_stock_age(self):
        self.op('adjust',product_id=self.pid,warehouse_id=self.warehouse,direction='in',quantity=5,reason='合成期初')
        short = self.state()['sku_page']['items'][0]
        self.assertFalse(any('滞销' in x for x in short['risk_flags']))
        start = (date.fromisoformat(self.today)-timedelta(days=29)).isoformat()
        long = self.a.state({'from':start,'to':self.today})['sku_page']['items'][0]
        self.assertTrue(any('观测期内无发货' in x for x in long['risk_flags']))
        self.assertIn(start,long['risk_evidence'])
        self.assertIn('非库龄或历史周转',long['risk_evidence'])
        self.assertIn('参考成本缺失',long['risk_flags'])

    def test_csv_contains_both_granularities_comparisons_and_risks(self):
        self.op('adjust',product_id=self.pid,warehouse_id=self.warehouse,direction='in',quantity=1,reason='合成导出')
        rows = list(csv.reader(io.StringIO(self.a.export({'from':self.today,'to':self.today}).decode('utf-8-sig'))))
        for label in ('上期范围','上期对比','daily','weekly'):
            self.assertTrue(any(r[0]==label for r in rows))
        self.assertTrue(any('参考成本缺失' in cell for r in rows for cell in r))

    def test_previous_period_must_be_representable(self):
        from core import Problem
        with self.assertRaisesRegex(Problem, '上期'):
            self.a.state({'from':'0001-01-01','to':'0001-01-02'})

    def test_cancelled_creation_and_previous_only_income_currency(self):
        d = self.order(quantity=4)
        self.op('cancel',id=d['id'],revision=d['revision'],reason='合成取消')
        old = (date.fromisoformat(self.today)-timedelta(days=1)).isoformat()
        self.entry(kind='income',category='other',currency='SAR',amount='2',fx='2',date=old)
        t = self.state()['trends']
        self.assertEqual(t['current']['created_orders'],1)
        self.assertEqual(t['current']['ordered_units'],0)
        self.assertEqual(t['currency_comparison']['SAR']['income_cents'],{'current':0,'previous':200,'delta':-200,'percent':-100.0})
