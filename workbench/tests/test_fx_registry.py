import concurrent.futures
import csv
import io
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import Store, Problem, ident
from fx_registry import FXRegistry


class FXRegistryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Store(tmp.name)
        self.service = FXRegistry(SimpleNamespace(store=self.store))

    def body(self, **extra):
        return {'name':'合成汇率','from_currency':'SAR','to_currency':'CNY','direction':'to_per_from','rate':'1.900000',
                'effective_from':'2026-01-01','effective_to':'2026-12-31','evidence':'人工核对合成凭据','note':'',**extra}

    def confirmation(self, body):
        return {**body,'request_id':ident(),'confirmed':True,'preview_digest':self.service.preview(body)['preview_digest']}

    def save(self, body=None):
        return self.service.save(self.confirmation(body or self.body()))

    def convert(self, r, **extra):
        return self.service.convert({'id':r['id'],'revision':r['revision'],'amount':'10','date':'2026-10-03','rounding':'half_up',**extra})

    def counts(self):
        with self.store.connect() as c:
            return [c.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('fx_registry','fx_registry_versions','fx_registry_requests','fx_registry_audit')]

    def test_preview_has_no_record_and_confirmation_survives_restart(self):
        b=self.confirmation(self.body())
        self.assertEqual(self.counts(),[0,0,0,0])
        r=FXRegistry(SimpleNamespace(store=self.store)).save(b)
        self.assertEqual(r['revision'],1)
        self.assertEqual(self.counts(),[1,1,1,1])

    def test_explicit_direction_rounding_and_date_boundaries(self):
        r=self.save(self.body(rate='1.005'))
        self.assertEqual(self.convert(r,amount='1')['converted_amount'],'1.01')
        self.assertEqual(self.convert(r,amount='1',rounding='half_even')['converted_amount'],'1.00')
        self.assertEqual(self.convert(r,amount='1',rounding='down')['converted_amount'],'1.00')
        self.assertEqual(self.convert(r,amount='0')['converted_amount'],'0.00')
        for dated in ['2026-01-01','2026-12-31']:
            self.assertEqual(self.convert(r,date=dated)['converted_amount'],'10.05')
        for dated in ['2025-12-31','2027-01-01']:
            with self.assertRaises(Problem):self.convert(r,date=dated)
        reverse=self.save(self.body(direction='from_per_to',rate='3'))
        self.assertEqual(self.convert(reverse)['converted_amount'],'3.33')
        self.assertFalse(self.convert(reverse)['accounting_written'])

    def test_versions_are_immutable_and_cancellation_blocks_all_versions(self):
        old=self.save()
        revised=self.save(self.body(id=old['id'],revision=1,rate='2',effective_from='2026-06-01'))
        self.assertEqual(self.convert(old)['converted_amount'],'19.00')
        self.assertEqual(self.convert(revised)['converted_amount'],'20.00')
        self.assertEqual(self.service.get(old['id'])['versions'][-1]['rate'],'1.900000')
        b={'id':old['id'],'revision':2,'confirmed':True,'request_id':ident(),'reason':'依据错误'}
        cancelled=self.service.cancel(b)
        self.assertEqual(cancelled['revision'],3)
        self.assertEqual(self.service.cancel(b),cancelled)
        self.assertEqual(len(self.service.get(old['id'])['versions']),3)
        for r in [old,revised,cancelled]:
            with self.assertRaises(Problem):self.convert(r)
        with self.assertRaises(Problem):self.service.preview(self.body(id=old['id'],revision=3))

    def test_invalid_rate_currency_direction_dates_and_required_evidence(self):
        for changes in [{'rate':True},{'rate':'0'},{'rate':'-1'},{'rate':'NaN'},{'rate':'Infinity'},
                        {'rate':'0.0000000000001'},{'rate':'1000001'},{'rate':''},{'from_currency':'EGP'},
                        {'to_currency':'SAR'},{'direction':'auto'},{'effective_from':'2026-1-1'},
                        {'effective_to':'2025-12-31'},{'evidence':''}]:
            with self.subTest(changes=changes),self.assertRaises(Problem):self.service.preview(self.body(**changes))
        self.assertEqual(self.counts(),[0,0,0,0])

    def test_conversion_requires_exact_version_and_explicit_rounding(self):
        r=self.save()
        for changes in [{'revision':None},{'revision':True},{'revision':'1'},{'revision':99},{'rounding':None},
                        {'amount':True},{'amount':'-1'},{'amount':'NaN'},{'amount':'0.0000001'},{'date':'2026-2-1'}]:
            with self.subTest(changes=changes),self.assertRaises(Problem):self.convert(r,**changes)

    def test_confirmation_stale_preview_and_same_currency_pair_guard(self):
        b=self.confirmation(self.body())
        for changes in [{'confirmed':False},{'rate':'2'},{'evidence':'更改依据'},{'direction':'from_per_to'}]:
            with self.assertRaises(Problem):self.service.save({**b,**changes})
        r=self.service.save(b)
        stale=self.confirmation(self.body(id=r['id'],revision=1))
        self.save(self.body(id=r['id'],revision=1,rate='2'))
        with self.assertRaises(Problem):self.service.save(stale)
        with self.assertRaises(Problem):self.service.preview(self.body(id=r['id'],revision=2,to_currency='USD'))
        with self.assertRaises(Problem):self.service.preview(self.body(id=r['id'],revision=True))

    def test_concurrent_idempotency_conflict_and_event_rollback(self):
        b=self.confirmation(self.body())
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            out=list(pool.map(lambda _:self.service.save(b),range(2)))
        self.assertEqual(out[0],out[1]);self.assertEqual(self.counts(),[1,1,1,1])
        with self.assertRaises(Problem):self.service.save({**b,'name':'另一次操作'})
        fresh=self.confirmation(self.body(id=out[0]['id'],revision=1))
        with patch.object(self.store,'event',side_effect=RuntimeError('audit fail')):
            with self.assertRaises(RuntimeError):self.service.save(fresh)
        self.assertEqual(self.counts(),[1,1,1,1])
        self.assertEqual(self.service.save(fresh)['revision'],2)

    def test_pagination_escaped_search_and_csv_all_history_safe(self):
        for i in range(52):self.save(self.body(name='合成'+str(i)))
        formula=self.save(self.body(name='=SUM(1,2)',evidence=' @source',note='\tunsafe'))
        self.save(self.body(id=formula['id'],revision=1,name='=SUM(1,2)',rate='2'))
        self.assertEqual(len(self.service.state()['rows']),50)
        self.assertEqual(len(self.service.state(page=1)['rows']),3)
        self.assertEqual(self.service.state(page=999)['page'],1)
        self.assertEqual(self.service.state(query='%')['total'],0)
        self.assertEqual(self.service.state(query='CNY')['total'],53)
        for page in [True,-1,'1.0',100001]:
            with self.assertRaises(Problem):self.service.state(page=page)
        rows=list(csv.reader(io.StringIO(self.service.export().decode('utf-8-sig'))))
        self.assertEqual(len(rows),55)
        self.assertTrue(any(r[1]=="'=SUM(1,2)" and r[10]=="'@source" and r[11]=="unsafe" for r in rows[1:]))

    def test_history_pagination_preserves_old_versions_and_bounds_audit(self):
        r=self.save()
        for i in range(51):
            r=self.save(self.body(id=r['id'],revision=r['revision'],rate=str(i+2)))
        latest=self.service.get(r['id'])
        self.assertEqual(latest['history_total'],52)
        self.assertEqual(len(latest['versions']),50)
        self.assertEqual(len(latest['audit']),50)
        older=self.service.get(r['id'],history_page=1)
        self.assertEqual([v['revision'] for v in older['versions']],[2,1])
        self.assertEqual(len(older['audit']),2)
        self.assertEqual(self.service.get(r['id'],history_page=999)['history_page'],1)
        self.assertEqual(self.convert(r,revision=1)['converted_amount'],'19.00')
        with self.assertRaises(Problem):self.service.get(r['id'],history_page=True)

    def test_preview_and_conversion_do_not_change_product_or_finance_rows(self):
        self.store.import_rows([{'title_zh':'合成商品','cost_cny':19}])
        from finance import Finance
        Finance(self.store)
        def snapshot():
            with self.store.connect() as c:
                return {t:[tuple(r) for r in c.execute('SELECT * FROM '+t)] for t in ('products','finance_entries','finance_payments')}
        before=snapshot();r=self.save();self.convert(r)
        self.service.cancel({'id':r['id'],'revision':1,'confirmed':True,'request_id':ident(),'reason':'测试撤销'})
        self.assertEqual(snapshot(),before)
