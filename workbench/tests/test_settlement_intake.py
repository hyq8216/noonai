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
from core import Store, Problem, ident
from operations import Operations
from finance import Finance
from settlement_intake import SettlementIntake, FIELDS


class SettlementIntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(self.tmp.name)
        self.ops=Operations(self.store)
        self.finance=Finance(self.store)
        self.app=SimpleNamespace(store=self.store,finance=self.finance)
        self.s=SettlementIntake(self.app)
        self.shop=self.op('entity',kind='shop',name='专用测试店铺')['id']
        self.shop2=self.op('entity',kind='shop',name='另一个测试店铺')['id']
        self.warehouse=self.op('entity',kind='warehouse',name='测试仓库')['id']
        self.pid=self.store.import_rows([{'title_zh':'结算专用测试商品'}])['created'][0]
        self.order=self.op('order',shop_id=self.shop,warehouse_id=self.warehouse,external_id='ORDER-1',currency='SAR',lines=[{'product_id':self.pid,'quantity':1,'unit_price':'100'}])
    def tearDown(self):
        self.tmp.cleanup()
    def op(self,action,**body):
        return self.ops.transact(action,{'request_id':ident(),**body})
    def row(self,**extra):
        return dict(evidence_key=ident(),external_id='ORDER-1',category='sale',currency='SAR',amount='100.00',date='2026-01-01',fx='1.9',evidence='专用测试凭证',**{}) | extra
    def preview(self,rows,**extra):
        return self.s.preview({'shop_id':self.shop,'format':'json','content':json.dumps(rows,ensure_ascii=False),'filename':'测试结算.json',**extra})
    def apply_body(self,b,**extra):
        return {'batch_id':b['id'],'token':b['token'],'request_id':ident(),'confirmed':True,**extra}
    def apply(self,b,**extra):
        return self.s.apply(self.apply_body(b,**extra))
    def test_preview_is_not_posting_and_apply_preserves_decimal_no_payment(self):
        b=self.preview([self.row(amount='0.03',fx='.5'),self.row(category='refund',amount='-0.01',fx='.5'),self.row(category='platform',external_id='',amount='0.02',currency='CNY',fx='1')])
        self.assertEqual(self.finance.state()['entries'],[])
        self.assertEqual(b['summary']['ready'],3)
        result=self.apply(b)
        self.assertEqual(result['summary']['imported'],3)
        s=self.finance.state()
        self.assertEqual(s['summary']['income_cents'],2)
        self.assertEqual(s['summary']['expense_cents'],3)
        self.assertEqual(s['payments'],[])
        self.assertEqual(result['rows'][1]['amount'],'-0.01')
        self.assertEqual(result['rows'][1]['amount_cents'],1)
        self.assertEqual(result['rows'][1]['kind'],'expense')
        self.assertEqual(len(result['currency_totals']),2)
    def test_partial_import_keeps_complete_remaining_receipt(self):
        valid=self.row()
        b=self.preview([valid,self.row(external_id='UNKNOWN'),valid,self.row(category='invented'),self.row(amount='NaN')])
        self.assertEqual([r['status'] for r in b['rows']],['ready','unmatched','duplicate','invalid','invalid'])
        result=self.apply(b)
        self.assertEqual(result['remaining_rows'],[2,3,4,5])
        self.assertEqual(result['summary']['imported'],1)
        self.assertEqual(len(self.finance.state()['entries']),1)
        self.assertEqual(self.s.state()['batches'][0],result)
    def test_unknown_shop_order_does_not_map_other_shop(self):
        b=self.preview([self.row()],shop_id=self.shop2)
        self.assertEqual(b['rows'][0]['status'],'unmatched')
        with self.assertRaises(Problem):self.apply(b)
        self.assertEqual(self.finance.state()['entries'],[])
    def test_cross_currency_keeps_original_and_no_false_difference(self):
        b=self.preview([self.row(currency='USD',amount='20',fx='7.2')])
        self.assertTrue(b['rows'][0]['cross_currency'])
        self.assertIsNone(b['order_checks'][0]['difference_cents'])
        self.apply(b)
        e=self.finance.state()['entries'][0]
        self.assertEqual((e['currency'],e['amount_cents'],e['base_cents']),('USD',2000,14400))
    def test_sale_difference_groups_only_this_batch_sales(self):
        b=self.preview([self.row(amount='40'),self.row(amount='50'),self.row(category='refund',amount='5')])
        self.assertEqual(b['order_checks'][0]['batch_sale_cents'],9000)
        self.assertEqual(b['order_checks'][0]['difference_cents'],-1000)
    def test_confirm_bound_to_order_version(self):
        b=self.preview([self.row()])
        self.op('cancel',id=self.order['id'],revision=self.order['revision'],reason='测试取消')
        with self.assertRaises(Problem) as error:self.apply(b)
        self.assertEqual(error.exception.status,409)
        self.assertEqual(self.finance.state()['entries'],[])
        b2=self.preview([self.row()])
        self.assertEqual(b2['rows'][0]['status'],'conflict')
    def test_confirm_bound_to_new_mapping_and_evidence_state(self):
        row=self.row()
        b=self.preview([row])
        self.finance.transact('entry',{'request_id':ident(),'kind':'income','document_id':self.order['id'],**{k:v for k,v in row.items() if k not in ('external_id',)}})
        with self.assertRaises(Problem):self.apply(b)
        b2=self.preview([row])
        self.assertEqual(b2['rows'][0]['status'],'duplicate')
        with self.assertRaises(Problem):self.apply(b2)
        self.assertEqual(len(self.finance.state()['entries']),1)
    def test_void_evidence_still_blocks_reuse_and_invalidates_token(self):
        row=self.row()
        self.apply(self.preview([row]))
        b=self.preview([row,self.row()])
        e=self.finance.state()['entries'][0]
        self.finance.transact('void_entry',{'request_id':ident(),'id':e['id'],'revision':e['revision'],'reason':'测试作废'})
        with self.assertRaises(Problem):self.apply(b)
        newer=self.preview([row])
        self.assertEqual(newer['rows'][0]['status'],'duplicate')
    def test_duplicate_contradiction_isolates_whole_group(self):
        row=self.row()
        b=self.preview([row,{**row,'amount':'99'},row])
        self.assertEqual([r['status'] for r in b['rows']],['conflict','conflict','conflict'])
        with self.assertRaises(Problem):self.apply(b)
    def test_existing_voucher_conflicting_order_or_amount(self):
        row=self.row()
        self.apply(self.preview([row]))
        b=self.preview([{**row,'amount':'99'}])
        self.assertEqual(b['rows'][0]['status'],'conflict')
        b=self.preview([row],shop_id=self.shop2)
        self.assertEqual(b['rows'][0]['status'],'conflict')
    def test_invalid_sign_kind_dates_rates_and_rows_are_isolated(self):
        cases=[{'amount':'1.001'},{'amount':True},{'amount':'Infinity'},{'amount':'0'},{'amount':'-1'},{'category':'refund','kind':'income'},{'category':'other'}, {'currency':'BTC'},{'date':'2999-01-01'},{'fx':'0'},{'currency':'CNY','fx':'2'},{'unexpected':'x'},{'date':'2026-02-30'}]
        b=self.preview([self.row(**extra) for extra in cases]+['bad-row'])
        self.assertTrue(all(r['status']=='invalid' and r['reason'] for r in b['rows']))
        self.assertEqual(self.finance.state()['entries'],[])
    def test_explicit_confirmation_and_token_cannot_be_skipped(self):
        b=self.preview([self.row()])
        for extra in ({'confirmed':False},{'confirmed':1},{'token':'bad'}):
            with self.assertRaises(Problem):self.apply(b,**extra)
        self.assertEqual(self.finance.state()['entries'],[])
    def test_request_id_replay_after_reopen_and_conflicting_body(self):
        b=self.preview([self.row()]);body=self.apply_body(b,request_id='persistent-request')
        result=self.s.apply(body)
        reopened=SettlementIntake(SimpleNamespace(store=Store(self.tmp.name),finance=Finance(Store(self.tmp.name))))
        self.assertEqual(reopened.apply(body),result)
        with self.assertRaises(Problem):reopened.apply({**body,'token':'changed'})
        with self.assertRaises(Problem):self.apply(b)
        self.assertEqual(len(self.finance.state()['entries']),1)
    def test_concurrent_confirm_cannot_double_post(self):
        b=self.preview([self.row()])
        def work(_):
            try:self.apply(b);return True
            except Problem:return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(work,[1,2])),1)
        self.assertEqual(len(self.finance.state()['entries']),1)
    def test_csv_template_roundtrip_bom_extra_columns_and_limits(self):
        template=self.s.template().decode('utf-8-sig')
        rows=list(csv.DictReader(io.StringIO(template)))
        rows[0]['external_id']='ORDER-1';rows[1]['external_id']='ORDER-1'
        stream=io.StringIO();writer=csv.DictWriter(stream,fieldnames=FIELDS);writer.writeheader();writer.writerows(rows)
        b=self.s.preview({'shop_id':self.shop,'format':'csv','content':'\ufeff'+stream.getvalue()})
        self.assertEqual(b['summary']['ready'],2)
        for body in ({'format':'json','content':'[]'},{'format':'json','content':'{}'},{'format':'csv','content':'amount\n1'},{'format':'xml','content':'x'},{'format':'json','content':'x'},{'format':'json','content':json.dumps([self.row()]*501)}):
            with self.assertRaises(Problem):self.s.preview({'shop_id':self.shop,**body})
        b=self.preview([self.row() for _ in range(500)])
        self.assertEqual(b['summary']['ready'],500)
    def test_export_formula_protection_and_receipt_column_alignment(self):
        b=self.preview([self.row(evidence='=HYPERLINK("x")',evidence_key='@voucher')])
        b=self.apply(b)
        rows=list(csv.DictReader(io.StringIO(self.s.export(b['id']).decode('utf-8-sig'))))
        self.assertEqual(rows[0]['evidence_key'],"'@voucher")
        self.assertEqual(rows[0]['evidence'],"'=HYPERLINK(\"x\")")
        self.assertEqual(rows[0]['order_id'],self.order['id'])
        self.assertEqual(rows[0]['entry_id'],b['rows'][0]['entry_id'])
        self.assertEqual(json.loads(rows[0]['raw_json'])['evidence'],'=HYPERLINK("x")')
    def test_new_order_mapping_invalidates_preview_before_partial_apply(self):
        b=self.preview([self.row(),self.row(external_id='ORDER-NEW')])
        self.op('order',shop_id=self.shop,warehouse_id=self.warehouse,external_id='ORDER-NEW',currency='SAR',lines=[{'product_id':self.pid,'quantity':1,'unit_price':'100'}])
        with self.assertRaises(Problem):self.apply(b)
        self.assertEqual(self.finance.state()['entries'],[])
        newer=self.preview([self.row(),self.row(external_id='ORDER-NEW')])
        self.assertEqual(newer['summary']['ready'],2)
    def test_posting_failure_rolls_back_entries_receipt_and_request(self):
        b=self.preview([self.row(),self.row()]);body=self.apply_body(b)
        original=self.finance.create
        calls=[]
        def failing(c,row):
            calls.append(row)
            if len(calls)==2:raise Problem('模拟第二笔记账失败')
            return original(c,row)
        self.finance.create=failing
        with self.assertRaises(Problem):self.s.apply(body)
        self.assertEqual(self.finance.state()['entries'],[])
        self.assertEqual(self.s.state()['batches'][0]['status'],'preview')
        self.finance.create=original
        result=self.s.apply(body)
        self.assertEqual(result['summary']['imported'],2)
    def test_json_numeric_decimals_keep_exact_text_and_mapping_ambiguity(self):
        row=self.row();content=json.dumps([row]).replace('"100.00"','0.03').replace('"1.9"','0.5')
        b=self.s.preview({'shop_id':self.shop,'format':'json','content':content})
        self.assertEqual(b['rows'][0]['amount'],'0.03')
        with self.store.connect() as c:
            duplicate={**self.order,'id':ident(),'external_key':None}
            self.ops.write(c,duplicate,True)
        newer=self.preview([self.row()])
        self.assertEqual(newer['rows'][0]['status'],'conflict')
        with self.assertRaises(Problem):self.apply(b)
    def test_json_nonfinite_constants_remain_invalid_text_safe_for_browser(self):
        content=json.dumps([self.row()]).replace('"100.00"','NaN')
        batch=self.s.preview({'shop_id':self.shop,'format':'json','content':content})
        self.assertEqual(batch['rows'][0]['status'],'invalid')
        self.assertEqual(batch['rows'][0]['raw']['amount'],'NaN')
        json.dumps(batch,allow_nan=False)
    def test_paginated_history_and_shop_entities(self):
        for _ in range(11):self.preview([self.row()])
        self.assertEqual(len(self.s.state()['batches']),10)
        self.assertEqual(len(self.s.state(1)['batches']),1)
        self.assertEqual(self.s.state(100)['page'],1)
        self.assertEqual(len(self.s.state()['entities']),2)
        for page in (True,-1,'bad',1.2):
            with self.assertRaises(Problem):self.s.state(page)


if __name__=='__main__':unittest.main()
