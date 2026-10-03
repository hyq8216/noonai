import concurrent.futures
import copy
import csv
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Store, Problem, ident, now
from operations import Operations
from supplier_quotes import SupplierQuotes

class SupplierQuotesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name);self.ops=Operations(self.store);self.app=SimpleNamespace(store=self.store);self.service=SupplierQuotes(self.app)
        self.pid=self.store.import_rows([{'title_zh':'合成询价SKU'}])['created'][0]
        self.suppliers=[self.ops.transact('entity',{'kind':'supplier','name':n,'request_id':ident()})['id'] for n in ['国内甲','国内乙','国内丙']]
    def body(self,**extra):
        return {'name':'本地询价','members':[{'product_id':self.pid,'revision':self.store.get(self.pid)['revision'],'quantity':10}],'supplier_ids':self.suppliers,'quotes':[{'product_id':self.pid,'supplier_id':sid,'currency':currency,'unit_price':price,'min_quantity':5,'valid_until':'2099-01-01','lead_days':0,'evidence':'合成供应商报价依据'} for sid,currency,price in zip(self.suppliers,['CNY','CNY','USD'],['10.1234','8','1'])],**extra}
    def confirmed(self,b):return {**b,'confirmed':True,'request_id':ident(),'preview_digest':self.service.preview(b)['preview_digest']}
    def save(self,b):return self.service.save(self.confirmed(b))
    def counts(self):
        with self.store.connect() as c:return [c.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ['supplier_quote_plans','supplier_quote_requests','supplier_quote_audit']]
    def test_comparison_partition_decimal_ties_and_no_external_side_effects(self):
        before=self.store.get(self.pid);p=self.save(self.body(status='checked'))
        self.assertTrue(p['precheck_current']);self.assertFalse(p['sendable']);self.assertFalse(p['supplier_order_sent']);self.assertFalse(p['payment_made'])
        cny=next(g for g in p['comparison'] if g['currency']=='CNY');usd=next(g for g in p['comparison'] if g['currency']=='USD')
        self.assertEqual(cny['rows'][0]['unit_price'],'8');self.assertEqual(cny['rows'][1]['quoted_subtotal'],'101.2340');self.assertEqual(len(usd['rows']),1)
        self.assertEqual(self.store.get(self.pid),before)
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM ops_documents').fetchone()[0],0)
        b=self.body();b['quotes'][0]['unit_price']='8';p=self.service.preview(b)['plan'];self.assertTrue(all(r['lowest_unit_price'] for r in next(g for g in p['comparison'] if g['currency']=='CNY')['rows']))
    def test_unknown_fields_are_null_not_zero_and_valid_groups_still_compare(self):
        b=self.body();b['quotes']=b['quotes'][1:];p=self.save(b);q=p['quotes'][0]
        for key in ['currency','unit_price','min_quantity','valid_until','lead_days']:self.assertIsNone(q[key])
        self.assertEqual(len(q['blockers']),6);self.assertFalse(p['precheck_current'])
        self.assertTrue(next(g for g in p['comparison'] if g['currency']=='USD')['comparable'])
        with self.assertRaises(Problem):self.service.preview({**b,'status':'checked'})
    def test_expiry_moq_and_date_rollover_recompute_gate(self):
        b=self.body();b['quotes'][0]['valid_until']='2000-01-01';b['quotes'][1]['min_quantity']=11
        p=self.save(b);self.assertIn('报价已过期',p['quotes'][0]['blockers']);self.assertIn('需求数量低于起订量',p['quotes'][1]['blockers'])
        self.assertEqual(next(g for g in p['comparison'] if g['currency']=='CNY')['rows'],[])
        b=self.body();b['quotes'][0]['valid_until']=now()[:10];p=self.save(b)
        with patch('supplier_quotes.now',return_value='2100-01-01T00:00:00Z'):
            self.assertFalse(self.service.get(p['id'])['precheck_current']);self.assertFalse(self.service.state()['rows'][0]['precheck_current'])
    def test_persisted_preview_request_replay_concurrency_conflict_and_audit(self):
        b=self.confirmed(self.body());service=SupplierQuotes(self.app)
        with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:service.save(b),range(2)))
        self.assertEqual(results[0],results[1]);self.assertEqual(self.counts(),[1,1,1])
        with self.assertRaises(Problem):service.save({**b,'name':'冲突请求'})
        p=self.service.get(results[0]['id']);self.assertEqual(p['audit'][0]['action'],'save')
    def test_source_and_supplier_changes_invalidate_preview_and_comparison(self):
        p=self.save(self.body(status='checked'));b=self.confirmed(self.body(id=p['id'],revision=1))
        product=self.store.get(self.pid);self.store.update(self.pid,{**product,'facts':'修改事实'},product['revision'])
        with self.assertRaises(Problem):self.service.save(b)
        p=self.service.get(p['id']);self.assertTrue(p['review_required']);self.assertFalse(p['precheck_current']);self.assertTrue(all(not g['rows'] for g in p['comparison']))
        p=self.save(self.body(id=p['id'],revision=1,status='checked'));b=self.confirmed(self.body(id=p['id'],revision=2))
        with self.store.connect() as c:
            row=c.execute('SELECT data FROM ops_entities WHERE id=?',(self.suppliers[0],)).fetchone();data=json.loads(row['data']);data['contact']='更改联系人';c.execute('UPDATE ops_entities SET data=? WHERE id=?',(json.dumps(data),self.suppliers[0]))
        with self.assertRaises(Problem):self.service.save(b)
        self.assertTrue(self.service.get(p['id'])['review_required'])
    def test_version_cancel_and_rollback(self):
        p=self.save(self.body());b=self.confirmed(self.body(id=p['id'],revision=1,name='更新'))
        with patch.object(self.store,'event',side_effect=RuntimeError('audit fail')):
            with self.assertRaises(RuntimeError):self.service.save(b)
        self.assertEqual(self.counts(),[1,1,1]);p=self.service.save(b);self.assertEqual(p['revision'],2)
        with self.assertRaises(Problem):self.service.preview(self.body(id=p['id'],revision=1))
        cancel={'id':p['id'],'revision':2,'confirmed':True,'request_id':ident(),'reason':'撤销本地询价'};p=self.service.cancel(cancel);self.assertEqual(p['revision'],3);self.assertFalse(p['precheck_current']);self.assertEqual(self.service.cancel(cancel),p)
        with self.assertRaises(Problem):self.service.preview(self.body(id=p['id'],revision=3))
    def test_invalid_quotes_references_and_fields_rejected(self):
        mutations=[lambda b:b['quotes'][0].update(product_id=[]),lambda b:b['quotes'][0].update(currency=False),lambda b:b['quotes'][0].update(unit_price=True),lambda b:b['quotes'][0].update(unit_price='NaN'),lambda b:b['quotes'][0].update(unit_price='1.00001'),lambda b:b['quotes'][0].update(currency='EGP'),lambda b:b['quotes'][0].update(lead_days=True),lambda b:b['quotes'][0].update(min_quantity=0),lambda b:b['quotes'][0].update(valid_until='2026-02-30'),lambda b:b['quotes'][0].update(product_id='unknown'),lambda b:b['members'][0].update(revision=True),lambda b:b['supplier_ids'].append(b['supplier_ids'][0]),lambda b:b['quotes'].append(copy.deepcopy(b['quotes'][0]))]
        for mutate in mutations:
            b=self.body();mutate(b)
            with self.assertRaises(Problem):self.service.preview(b)
        self.assertEqual(self.counts(),[0,0,0])
    def test_previews_do_not_save_confirmation_and_modified_content_rejected(self):
        b=self.confirmed(self.body());self.assertEqual(self.counts(),[0,0,0])
        for change in [{'confirmed':False},{'preview_digest':'bad'},{'name':'changed'}]:
            with self.assertRaises(Problem):self.service.save({**b,**change})
        b['quotes'][0]['evidence']='不同依据'
        with self.assertRaises(Problem):self.service.save(b)
    def test_maximum_quote_matrix_has_bounded_summary_and_no_comparison_build(self):
        ids=self.store.import_rows([{'title_zh':'矩阵'+str(i)} for i in range(99)])['created']+[self.pid]
        suppliers=self.suppliers+[self.ops.transact('entity',{'kind':'supplier','name':'矩阵供应商'+str(i),'request_id':ident()})['id'] for i in range(47)]
        b={'name':'5000报价矩阵','members':[{'product_id':pid,'revision':1,'quantity':10} for pid in ids],'supplier_ids':suppliers,'quotes':[]}
        p=self.save(b);self.assertEqual(len(p['quotes']),5000)
        original=self.service.enrich
        calls=[]
        def recorded(c,plan,include_comparison=True):
            calls.append(include_comparison)
            return original(c,plan,include_comparison)
        with patch.object(self.service,'enrich',side_effect=recorded):state=self.service.state()
        self.assertEqual(calls,[False]);self.assertEqual(state['rows'][0]['blocked_count'],5000)
        self.assertLess(len(json.dumps(state['rows'])),1000)

    def test_state_paging_summaries_search_and_csv_safety(self):
        b=self.body(name='=公式名称');b['quotes'][0]['evidence']='  @恶意公式';p=self.save(b)
        state=self.service.state(page=999,product_page=999,supplier_page=999);self.assertEqual(state['page'],0);self.assertEqual(state['rows'][0]['id'],p['id']);self.assertNotIn('quotes',state['rows'][0]);self.assertNotIn('comparison',state['rows'][0]);self.assertEqual(self.service.state(query='%')['total'],0)
        csvtext=self.service.export().decode('utf-8-sig');rows=list(csv.reader(io.StringIO(csvtext)));self.assertEqual(rows[1][1],"'=公式名称");self.assertEqual(rows[1][13],"'@恶意公式")
        for key in ['page','product_page','supplier_page']:
            with self.assertRaises(Problem):self.service.state(**{key:True})
        self.store.import_rows([{'title_zh':'分页'+str(i)} for i in range(110)])
        state=self.service.state(product_page=1);self.assertEqual(state['product_total'],111);self.assertEqual(len(state['products']),11)

if __name__=='__main__':unittest.main()
