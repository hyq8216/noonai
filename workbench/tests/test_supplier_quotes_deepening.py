"""Meaningful whole-lot supplier comparison with isolated synthetic ledgers."""
import copy
import csv
import io
import json
import unittest
from unittest.mock import patch
import test_supplier_quotes as baseline
from core import Problem, ident

class SupplierQuotesDeepeningTests(unittest.TestCase):
    setUp = baseline.SupplierQuotesTests.setUp
    body = baseline.SupplierQuotesTests.body
    confirmed = baseline.SupplierQuotesTests.confirmed
    save = baseline.SupplierQuotesTests.save
    counts = baseline.SupplierQuotesTests.counts
    def complete(self):
        b=self.body()
        b['members'][0].update(quantity=11, required_by='2026-10-10', selected_supplier_id=self.suppliers[0])
        for q in b['quotes']: q.update(pack_quantity=6,available_quantity=100,lead_days=2)
        return b

    def test_whole_lot_moq_rounding_budget_and_currency_partitions(self):
        with patch('supplier_quotes.now',return_value='2026-10-03T12:00:00Z'):
            b=self.complete();b['quotes'][0].update(min_quantity=13,unit_price='1.1234')
            p=self.save(b);line=p['procurement_draft']['lines'][0]
        self.assertEqual(line['order_quantity'],18);self.assertEqual(line['excess_quantity'],7)
        self.assertEqual(line['whole_lot_budget'],'20.2212');self.assertEqual(line['shortage_quantity'],0)
        self.assertEqual(line['estimated_arrival'],'2026-10-05');self.assertTrue(line['fulfillable'])
        self.assertTrue(p['procurement_draft']['precheck_passed'])
        self.assertEqual(p['procurement_draft']['budgets'][0]['total'],'20.2212')
        self.assertEqual({r['currency'] for r in p['fulfillment']},{'CNY','USD'})
        self.assertFalse(p['procurement_draft']['purchase_created'])
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM ops_documents').fetchone()[0],0)

    def test_unknown_capacity_pack_price_preserved_and_budget_not_zero(self):
        b=self.complete();b['quotes'][0].update(pack_quantity='',available_quantity='',unit_price='')
        p=self.save(b);line=p['procurement_draft']['lines'][0];budget=p['procurement_draft']['budgets'][0]
        for key in ('order_quantity','excess_quantity','shortage_quantity','whole_lot_budget'):self.assertIsNone(line[key])
        self.assertIsNone(budget['total']);self.assertEqual(budget['unknown_lines'],1)
        self.assertIn('包装倍数待确认',line['fulfillment_blockers']);self.assertFalse(line['fulfillable'])

    def test_shortage_deadline_expiry_and_cancel_block_selection(self):
        with patch('supplier_quotes.now',return_value='2026-10-03T12:00:00Z'):
            b=self.complete();b['quotes'][0].update(available_quantity=8,lead_days=10,valid_until='2026-10-02')
            p=self.save(b);line=p['procurement_draft']['lines'][0]
            self.assertEqual(line['shortage_quantity'],3)
            for message in ('报价已过期','预计到货晚于需求日期','可供数量不足整批采购'):self.assertIn(message,line['fulfillment_blockers'])
            p=self.service.cancel({'id':p['id'],'revision':1,'reason':'合成撤销','confirmed':True,'request_id':ident()})
        self.assertFalse(p['procurement_draft']['precheck_passed']);self.assertTrue(all(not r['fulfillable'] for r in p['fulfillment']))

    def test_selection_and_date_changes_require_new_preview(self):
        with patch('supplier_quotes.now',return_value='2026-10-03T12:00:00Z'):
            b=self.confirmed(self.complete())
            changed=copy.deepcopy(b);changed['members'][0]['selected_supplier_id']=self.suppliers[1]
            with self.assertRaises(Problem):self.service.save(changed)
        with patch('supplier_quotes.now',return_value='2026-10-04T12:00:00Z'):
            with self.assertRaises(Problem):self.service.save(b)
        self.assertEqual(self.counts(),[0,0,0])

    def test_source_drift_unknown_selection_and_export_traceability(self):
        b=self.complete();p=self.save(b)
        product=self.store.get(self.pid);self.store.update(self.pid,{**product,'facts':'变更依据'},product['revision'])
        p=self.service.get(p['id']);self.assertFalse(p['procurement_draft']['precheck_passed'])
        self.assertTrue(all(not r['lowest_whole_lot_budget'] for r in p['fulfillment']))
        rows=list(csv.reader(io.StringIO(self.service.export().decode('utf-8-sig'))))
        self.assertIn('整批原币预算',rows[0]);self.assertIn('选入采购草稿',rows[0])
        self.assertIn('商品或供应商依据变化',rows[1][-1])
        b=self.complete();b['members'][0]['selected_supplier_id']='foreign'
        with self.assertRaises(Problem):self.service.preview(b)
        b=self.complete();b['members'][0]['selected_supplier_id']=''
        p=self.service.preview(b)['plan'];self.assertEqual(p['procurement_draft']['unselected_count'],1)
        self.assertEqual(p['procurement_draft']['lines'][0]['shortage_quantity'],11)

    def test_quantity_validation_and_large_rounding_gate(self):
        for key,value in [('pack_quantity',0),('pack_quantity',True),('available_quantity',-1),('available_quantity','1.5')]:
            b=self.complete();b['quotes'][0][key]=value
            with self.assertRaises(Problem):self.service.preview(b)
        b=self.complete();b['members'][0]['quantity']=1000000;b['quotes'][0].update(pack_quantity=999999,available_quantity=1000000)
        p=self.service.preview(b)['plan'];self.assertIn('整批采购数量超过1000000',p['fulfillment'][0]['fulfillment_blockers'])

if __name__=='__main__':unittest.main()
