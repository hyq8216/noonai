import concurrent.futures
import csv
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Store, Problem, ident
from finance import Finance
from operations import Operations
from bank_reconciliation import BankReconciliation, FIELDS


class BankReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
        self.ops=Operations(self.store);self.finance=Finance(self.store)
        self.app=SimpleNamespace(store=self.store,finance=self.finance)
        self.bank=BankReconciliation(self.app)
    def tearDown(self):self.tmp.cleanup()
    def entry(self,**extra):
        return self.finance.transact('entry',{'request_id':ident(),'kind':'expense','category':'other','currency':'SAR','amount':'100','fx':'1.9','date':'2026-01-01','evidence':'银行核对专用测试依据','evidence_key':ident(),**extra})
    def row(self,**extra):
        return {'account_name':'专用测试SAR账户','bank_reference':ident(),'currency':'SAR','direction':'out','amount':'40.00','date':'2026-01-03','fx':'1.95','evidence':'专用测试银行流水依据',**extra}
    def preview(self,rows,**extra):
        return self.bank.preview({'format':'json','content':json.dumps(rows,ensure_ascii=False),'filename':'专用测试流水.json',**extra})
    def import_body(self,b,**extra):return {'batch_id':b['id'],'token':b['token'],'request_id':ident(),'confirmed':True,**extra}
    def import_rows(self,b,**extra):return self.bank.import_rows(self.import_body(b,**extra))
    def fresh_row(self,row_id):
        return next(r for b in self.bank.state()['batches'] for r in b['rows'] if r.get('id')==row_id)
    def match_body(self,row,entry,**extra):return {'row_id':row['id'],'row_token':row['row_token'],'entry_id':entry['id'],'entry_revision':entry['revision'],'confirmed':True,'request_id':ident(),**extra}
    def match(self,row,entry,**extra):return self.bank.match(self.match_body(row,entry,**extra))
    def test_import_and_payment_are_two_distinct_explicit_steps(self):
        e=self.entry();p=self.preview([self.row()])
        self.assertEqual(self.finance.state()['payments'],[])
        imported=self.import_rows(p)
        self.assertEqual(imported['summary']['unmatched'],1)
        self.assertEqual(self.finance.state()['payments'],[])
        r=imported['rows'][0]
        self.assertEqual(r['suggestions'][0]['id'],e['id'])
        receipt=self.match(r,e)
        self.assertEqual(receipt['payment']['evidence_key'],'bank-row:'+r['id'])
        self.assertEqual(receipt['payment']['amount_cents'],4000)
        self.assertEqual(receipt['payment']['account'],r['account_name'])
        self.assertEqual(self.bank.state()['batches'][0]['summary']['matched'],1)
        self.assertEqual(self.finance.state()['summary']['cash_out_cents'],7800)
    def test_partial_multiple_rows_can_settle_one_entry_without_overpayment(self):
        e=self.entry();b=self.import_rows(self.preview([self.row(amount='40'),self.row(amount='60'),self.row(amount='1')]))
        first=self.match(b['rows'][0],e)
        with self.store.connect() as c:e=self.finance.get(c,e['id'])
        self.match(b['rows'][1],e)
        e=self.finance.state()['entries'][0]
        with self.assertRaises(Problem):self.match(b['rows'][2],e)
        self.assertEqual(len(self.finance.state()['payments']),2)
        self.assertEqual(e['remaining_cents'],0)
    def test_direction_currency_date_and_revision_are_enforced_server_side(self):
        e=self.entry();b=self.import_rows(self.preview([self.row(direction='in'),self.row(currency='USD',account_name='USD测试账户',fx='7.2'),self.row(date='2025-12-31'),self.row()]))
        for r in b['rows'][:3]:
            self.assertEqual(r['suggestions'],[])
            with self.assertRaises(Problem):self.match(r,e)
        with self.assertRaises(Problem):self.match(b['rows'][3],e,entry_revision=0)
        self.assertEqual(self.finance.state()['payments'],[])
    def test_payment_uses_only_stored_bank_row_values(self):
        e=self.entry();r=self.import_rows(self.preview([self.row(amount='.03',fx='.5')]))['rows'][0]
        receipt=self.match(r,e,amount='99',fx='7',date='2020-01-01',evidence_key='attacker-key',account='attacker-account')
        p=receipt['payment']
        self.assertEqual((p['amount_cents'],p['base_cents'],p['date'],p['account'],p['evidence_key']),(3,2,r['date'],r['account_name'],'bank-row:'+r['id']))
    def test_void_entry_never_suggested_or_matchable(self):
        e=self.entry();r=self.import_rows(self.preview([self.row()]))['rows'][0]
        self.finance.transact('void_entry',{'request_id':ident(),'id':e['id'],'revision':e['revision'],'reason':'测试撤销条目'})
        s=self.bank.state()
        self.assertEqual(s['finance_entries'],[])
        self.assertEqual(s['batches'][0]['rows'][0]['suggestions'],[])
        with self.assertRaises(Problem):self.match(r,{**e,'revision':2})
        self.assertEqual(self.finance.state()['payments'],[])
    def test_void_payment_preserves_bank_receipt_and_prevents_rematch(self):
        e=self.entry();r=self.import_rows(self.preview([self.row()]))['rows'][0]
        receipt=self.match(r,e)
        self.finance.transact('void_payment',{'request_id':ident(),'payment_id':receipt['payment']['id'],'revision':2,'reason':'测试撤销收付款'})
        row=self.fresh_row(r['id'])
        self.assertEqual(row['status'],'payment_void')
        self.assertEqual(row['suggestions'],[])
        with self.assertRaises(Problem):self.match(row,{**e,'revision':3})
        self.assertEqual(len(self.finance.state()['payments']),1)
        self.assertEqual(self.finance.state()['summary']['cash_out_cents'],0)
        self.assertIn('payment_void',self.bank.export(r['batch_id']).decode('utf-8-sig'))
    def test_duplicate_and_conflicting_references_are_isolated(self):
        row=self.row();b=self.preview([row,row,{**row,'amount':'39'}])
        self.assertEqual([r['status'] for r in b['rows']],['conflict','conflict','conflict'])
        with self.assertRaises(Problem):self.import_rows(b)
        b=self.import_rows(self.preview([row]))
        newer=self.preview([row,{**row,'amount':'41'},self.row()])
        self.assertEqual([r['status'] for r in newer['rows']],['conflict','conflict','ready'])
        receipt=self.import_rows(newer)
        self.assertEqual(receipt['summary']['unmatched'],1)
        self.assertEqual(receipt['summary']['conflict'],2)
    def test_exact_duplicate_after_import_never_imported_or_paid_again(self):
        row=self.row();self.import_rows(self.preview([row]))
        b=self.preview([row])
        self.assertEqual(b['summary']['duplicate'],1)
        with self.assertRaises(Problem):self.import_rows(b)
        self.assertEqual(self.finance.state()['payments'],[])
    def test_reference_is_unique_per_account(self):
        row=self.row();b=self.preview([row,{**row,'account_name':'另一个SAR账户'}])
        self.assertEqual(b['summary']['ready'],2)
        self.assertEqual(self.import_rows(b)['summary']['unmatched'],2)
    def test_account_currency_conflicts_whole_batch_or_existing_registry(self):
        row=self.row();b=self.preview([row,self.row(account_name=row['account_name'],currency='USD',fx='7.2')])
        self.assertEqual([r['status'] for r in b['rows']],['conflict','conflict'])
        self.import_rows(self.preview([row]))
        b=self.preview([self.row(currency='USD',fx='7.2')])
        self.assertEqual(b['rows'][0]['status'],'conflict')
    def test_preview_token_invalidates_on_new_currency_or_duplicate_import(self):
        row=self.row();first=self.preview([row]);other=self.preview([row])
        self.import_rows(first)
        with self.assertRaises(Problem):self.import_rows(other)
        row2=self.row(account_name='尚未建立账户');b=self.preview([row2])
        self.import_rows(self.preview([self.row(account_name=row2['account_name'],currency='USD',fx='7.2')]))
        with self.assertRaises(Problem):self.import_rows(b)
    def test_confirm_and_row_token_guards(self):
        e=self.entry();b=self.preview([self.row()])
        for extra in ({'confirmed':False},{'confirmed':1},{'token':'bad'}):
            with self.assertRaises(Problem):self.import_rows(b,**extra)
        r=self.import_rows(b)['rows'][0]
        for extra in ({'confirmed':False},{'confirmed':1},{'row_token':'bad'}):
            with self.assertRaises(Problem):self.match(r,e,**extra)
        self.assertEqual(self.finance.state()['payments'],[])
    def test_import_match_request_replay_survives_reopen(self):
        e=self.entry();b=self.preview([self.row()]);ib=self.import_body(b)
        imported=self.bank.import_rows(ib)
        mb=self.match_body(imported['rows'][0],e)
        matched=self.bank.match(mb)
        reopened=BankReconciliation(SimpleNamespace(store=Store(self.tmp.name),finance=Finance(Store(self.tmp.name))))
        self.assertEqual(reopened.import_rows(ib),imported)
        self.assertEqual(reopened.match(mb),matched)
        with self.assertRaises(Problem):reopened.match({**mb,'entry_revision':99})
        with self.assertRaises(Problem):self.match(imported['rows'][0],{**e,'revision':2})
        self.assertEqual(len(self.finance.state()['payments']),1)
    def test_concurrent_matching_one_row_is_not_double_counted(self):
        e=self.entry();r=self.import_rows(self.preview([self.row()]))['rows'][0]
        def work(_):
            try:self.match(r,e);return True
            except Problem:return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:self.assertEqual(sum(pool.map(work,[1,2])),1)
        self.assertEqual(len(self.finance.state()['payments']),1)
    def test_concurrent_rows_cannot_overpay_same_entry(self):
        e=self.entry();rows=self.import_rows(self.preview([self.row(amount='80'),self.row(amount='80')]))['rows']
        def work(r):
            try:self.match(r,e);return True
            except Problem:return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:self.assertEqual(sum(pool.map(work,rows)),1)
        self.assertEqual(self.finance.state()['entries'][0]['remaining_cents'],2000)
    def test_csv_template_roundtrip_amount_decimal_and_limits(self):
        template=self.bank.template().decode('utf-8-sig')
        b=self.bank.preview({'format':'csv','content':'\ufeff'+template})
        self.assertEqual(b['summary']['ready'],2)
        b=self.preview([self.row() for _ in range(500)])
        self.assertEqual(b['summary']['ready'],500)
        for extra in ({'content':json.dumps([self.row()]*501)},{'content':'[]'},{'content':'{}'},{'content':'bad'},{'format':'csv','content':'amount\n1'},{'format':'csv','content':','.join(FIELDS+[FIELDS[0]])+'\n'},{'format':'xml'},{'content':'x'*(2*1024*1024+1)}):
            with self.assertRaises(Problem):self.preview([self.row()],**extra)
    def test_invalid_rows_preserved_with_reasons_and_partial_import(self):
        cases=[{'amount':'-1'},{'amount':'NaN'},{'amount':'0'},{'amount':True},{'amount':'0.001'},{'currency':'BTC'},{'direction':'expense'},{'date':'2999-01-01'},{'date':'2026-02-30'},{'currency':'CNY','fx':'2'},{'fx':'Infinity'},{'unexpected':'x'}]
        b=self.preview([self.row(**extra) for extra in cases]+['bad',self.row()])
        self.assertTrue(all(r['status']=='invalid' and r['reason'] for r in b['rows'][:-1]))
        imported=self.import_rows(b)
        self.assertEqual(imported['summary']['unmatched'],1)
        self.assertEqual(len(imported['remaining_rows']),13)
    def test_state_entry_pagination_is_summary_not_full_ledger(self):
        for _ in range(51):self.entry()
        s=self.bank.state();self.assertEqual(len(s['finance_entries']),50)
        self.assertEqual(s['entry_pagination']['total'],51)
        self.assertEqual(len(self.bank.state(entry_page=1)['finance_entries']),1)
        self.assertNotIn('allocations',s['finance_entries'][0])
        for _ in range(11):self.preview([self.row()])
        self.assertEqual(len(self.bank.state()['batches']),10)
        self.assertEqual(len(self.bank.state(1)['batches']),1)
        for p in (True,-1,'bad',1.2):
            with self.assertRaises(Problem):self.bank.state(p)
    def test_existing_manual_payment_account_currency_is_respected(self):
        e=self.entry(currency='USD',fx='7.2')
        self.finance.transact('payment',{'request_id':ident(),'id':e['id'],'revision':1,'amount':'1','fx':'7.2','date':'2026-01-03','account':'专用测试SAR账户','evidence':'测试USD收付款','evidence_key':ident()})
        b=self.preview([self.row()])
        self.assertEqual(b['rows'][0]['status'],'conflict')
        with self.assertRaises(Problem):self.import_rows(b)
    def test_account_currency_changed_after_import_blocks_matching(self):
        e=self.entry();r=self.import_rows(self.preview([self.row()]))['rows'][0]
        other=self.entry(currency='USD',fx='7.2')
        self.finance.transact('payment',{'request_id':ident(),'id':other['id'],'revision':1,'amount':'1','fx':'7.2','date':'2026-01-03','account':r['account_name'],'evidence':'测试矛盾账户','evidence_key':ident()})
        fresh=self.fresh_row(r['id'])
        self.assertEqual(fresh['status'],'conflict')
        with self.assertRaises(Problem):self.match(r,e)
        self.assertEqual(len(self.finance.state()['payments']),1)
    def test_match_failure_rolls_back_payment_entry_and_row_then_retry(self):
        e=self.entry();r=self.import_rows(self.preview([self.row()]))['rows'][0]
        original=self.finance.pay
        def failing(c,body):
            original(c,body)
            raise Problem('模拟写付款后失败')
        self.finance.pay=failing;body=self.match_body(r,e)
        with self.assertRaises(Problem):self.bank.match(body)
        self.assertEqual(self.finance.state()['payments'],[])
        self.assertEqual(self.finance.state()['entries'][0]['revision'],1)
        self.assertEqual(self.fresh_row(r['id'])['status'],'unmatched')
        self.finance.pay=original
        result=self.bank.match(body)
        self.assertEqual(result['payment']['amount_cents'],4000)
    def test_import_failure_rolls_back_accounts_rows_receipt_and_request(self):
        b=self.preview([self.row(),self.row(bank_reference='FAIL-REF')]);body=self.import_body(b)
        with self.store.connect() as c:
            c.execute("CREATE TRIGGER test_fail_bank_row BEFORE INSERT ON bank_rows WHEN NEW.bank_reference='FAIL-REF' BEGIN SELECT RAISE(ABORT,'simulated persistence failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.bank.import_rows(body)
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM bank_rows').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM bank_accounts').fetchone()[0],0)
            self.assertEqual(c.execute('SELECT count(*) FROM bank_requests').fetchone()[0],0)
            c.execute('DROP TRIGGER test_fail_bank_row')
        self.assertEqual(self.bank.state()['batches'][0]['status'],'preview')
        self.assertEqual(self.bank.import_rows(body)['summary']['unmatched'],2)
    def test_json_numeric_amount_and_nonfinite_are_handled_without_float(self):
        row=self.row();content=json.dumps([row]).replace('"40.00"','0.03').replace('"1.95"','0.5')
        b=self.bank.preview({'format':'json','content':content})
        self.assertEqual(b['rows'][0]['amount'],'0.03')
        bad=content.replace('0.03','NaN')
        b=self.bank.preview({'format':'json','content':bad})
        self.assertEqual(b['rows'][0]['status'],'invalid')
        self.assertEqual(b['rows'][0]['raw']['amount'],'NaN')
    def test_duplicate_json_fields_are_rejected_without_silent_overwrite(self):
        content=json.dumps([self.row()]).replace('"40.00"','"40.00", "amount": "99"')
        with self.assertRaises(Problem):self.bank.preview({'format':'json','content':content})
        self.assertEqual(self.bank.state()['batches'],[])
    def test_suggestions_exclude_wrong_currency_direction_date_and_insufficient_balance(self):
        valid=self.entry(amount='50')
        self.entry(amount='30')
        self.entry(currency='USD',fx='7.2')
        self.entry(kind='income')
        self.entry(date='2026-01-04')
        r=self.import_rows(self.preview([self.row()]))['rows'][0]
        self.assertEqual([e['id'] for e in r['suggestions']],[valid['id']])
    def test_suggestions_are_bounded_and_all_remaining_rows_stay_selectable(self):
        self.entry(amount='100000')
        b=self.import_rows(self.preview([self.row() for _ in range(25)]))
        self.assertEqual(len(b['rows']),25)
        self.assertEqual(sum(bool(r['suggestions']) for r in b['rows']),20)
        self.assertTrue(b['rows'][24]['row_token'])
        self.assertTrue(b['rows'][24]['suggestion_notice'])
    def test_export_formula_safety_and_column_alignment(self):
        e=self.entry();r=self.import_rows(self.preview([self.row(account_name='@account',bank_reference='=ref',evidence='=HYPERLINK("x")')]))['rows'][0]
        self.match(r,e)
        exported=list(csv.DictReader(io.StringIO(self.bank.export(r['batch_id']).decode('utf-8-sig'))))
        self.assertEqual(exported[0]['account_name'],"'@account")
        self.assertEqual(exported[0]['bank_reference'],"'=ref")
        self.assertEqual(exported[0]['evidence'],"'=HYPERLINK(\"x\")")
        self.assertEqual(exported[0]['bank_row_id'],r['id'])
        self.assertEqual(exported[0]['entry_id'],e['id'])
        self.assertEqual(exported[0]['status'],'matched')
        self.assertEqual(json.loads(exported[0]['raw_json'])['bank_reference'],'=ref')


if __name__=='__main__':unittest.main()
