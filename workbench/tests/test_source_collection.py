import copy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Store,Problem
from source_collection import SourceCollection

class Accounts:
    def __init__(self):self.row=dict(id='account',provider='custom_json',name='Test',base_url='https://supplier.example/api',enabled=True,revision=1,config={},has_token=False)
    def get(self,aid,connection=None,with_secret=False):
        if aid not in ('account','other'):raise Problem('账号不存在',404)
        return {**self.row,'id':aid,**({'token':''} if with_secret else {})}

class CollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
        self.app=SimpleNamespace(store=self.store,channel_accounts=Accounts())
        self.collection=SourceCollection(self.app)
    def tearDown(self):self.tmp.cleanup()
    def item(self,external='variant',**extra):
        return dict(external_id=external,title_zh='盒子',source_url='https://supplier.example/box',source_sku=external,supplier='供应商',facts='尺寸：10cm',stock=0,source_price=12,source_currency='CNY',images=['https://supplier.example/box.jpg'],**extra)
    def create(self,key='collect',**extra):return self.collection.create({'request_id':key,'account_id':'account','page_limit':2,'confirmed':True,**extra})
    def collect(self,items=None,**extra):
        run=self.create(**extra)
        with patch('channel_adapters.fetch_page',return_value={'items':items or [self.item()],'next_cursor':None,'warnings':[]}):out=self.collection.run(run['id'])
        return out
    def body(self,request='import'):
        ids=[r['id'] for r in self.collection.state()['candidates']];pre=self.collection.preview({'candidate_ids':ids})
        return dict(candidate_ids=ids,preview_token=pre['token'],request_id=request,confirmed=True)
    def test_no_automatic_products_real_zero_and_explicit_import(self):
        self.collect();self.assertEqual(self.store.list(),[])
        candidate=self.collection.state()['candidates'][0];self.assertEqual(candidate['normalized']['stock'],0);self.assertIsNone(candidate['normalized']['cost_cny'])
        out=self.collection.apply(self.body());p=self.store.get(out['created'][0]);self.assertEqual(p['stock'],0);self.assertEqual(p['images'],[]);self.assertFalse(p['content_verified']);self.assertIn('source_collection',p);self.assertEqual(p['source_collection']['snapshots'][0]['raw']['images'],['https://supplier.example/box.jpg'])
    def test_create_and_import_receipts_replay_before_live_preflight(self):
        run=self.collect();self.assertTrue(self.create()['replayed']);body=self.body();first=self.collection.apply(body)
        self.app.channel_accounts.row['enabled']=False
        second=self.collection.apply(body);self.assertTrue(second['replayed']);self.assertEqual(first['created'],second['created']);self.assertEqual(len(self.store.list()),1)
        with self.assertRaises(Problem):self.create(query='changed')
        with self.assertRaises(Problem):self.collection.apply({**body,'candidate_ids':['other']})
    def test_pagination_partial_failure_retry_and_cancel(self):
        run=self.create()
        with patch('channel_adapters.fetch_page',side_effect=[{'items':[self.item()],'next_cursor':'p2','warnings':[]},RuntimeError('secret')]):out=self.collection.run(run['id'])
        self.assertEqual(out['status'],'attention');self.assertEqual(out['pages'],1);self.assertNotIn('secret',str(out));self.assertEqual(self.collection.state()['total'],1)
        self.collection.control({'action':'retry','run_id':run['id']})
        with patch('channel_adapters.fetch_page',return_value={'items':[self.item('b')],'next_cursor':None,'warnings':[]}) as fetch:out=self.collection.run(run['id'])
        self.assertEqual(fetch.call_args.kwargs['cursor'],'p2');self.assertEqual(out['status'],'completed');self.assertEqual(self.collection.state()['total'],2)
        run=self.create(key='cancel');self.collection.control({'action':'cancel','run_id':run['id']})
        with patch('channel_adapters.fetch_page') as fetch:self.collection.run(run['id']);fetch.assert_not_called()
    def test_cancel_during_fetch_discards_page(self):
        run=self.create()
        def fetch(*args,**kwargs):
            self.collection.control({'action':'cancel','run_id':run['id']});return {'items':[self.item()],'next_cursor':None,'warnings':[]}
        with patch('channel_adapters.fetch_page',side_effect=fetch):out=self.collection.run(run['id'])
        self.assertEqual(out['status'],'cancelled');self.assertEqual(self.collection.state()['total'],0)
    def test_account_revision_during_fetch_discards_and_blocks_retry(self):
        run=self.create()
        def fetch(*args,**kwargs):
            self.app.channel_accounts.row['revision']=2;return {'items':[self.item()],'next_cursor':None,'warnings':[]}
        with patch('channel_adapters.fetch_page',side_effect=fetch):out=self.collection.run(run['id'])
        self.assertEqual(out['status'],'attention');self.assertEqual(self.collection.state()['total'],0)
        with self.assertRaises(Problem):self.collection.control({'action':'retry','run_id':run['id']})
    def test_bounded_page_limit_requires_explicit_resume(self):
        run=self.create(page_limit=1)
        with patch('channel_adapters.fetch_page',return_value={'items':[self.item()],'next_cursor':'next','warnings':[]}):out=self.collection.run(run['id'])
        self.assertEqual(out['status'],'attention');self.assertEqual(out['cursor'],'next')
        self.collection.control({'action':'retry','run_id':run['id']})
        with patch('channel_adapters.fetch_page',return_value={'items':[self.item('b')],'next_cursor':None,'warnings':[]}) as fetch:out=self.collection.run(run['id'])
        self.assertEqual(fetch.call_args.kwargs['cursor'],'next');self.assertEqual(out['status'],'completed')

    def test_restart_requires_explicit_retry(self):
        run=self.create()
        with self.store.connect() as c:c.execute("UPDATE source_collection_runs SET status='running' WHERE id=?",(run['id'],))
        other=SourceCollection(self.app);self.assertEqual(other.state()['runs'][0]['status'],'attention')
        with patch('channel_adapters.fetch_page') as fetch:other.run(run['id']);fetch.assert_not_called()
    def test_identity_conflict_retains_product_and_observations(self):
        self.collect();first=self.collection.apply(self.body());pid=first['created'][0]
        changed=self.item();changed['stock']=99
        self.collect(items=[changed],key='next')
        candidate=self.collection.state()['candidates'][0];self.assertEqual(candidate['status'],'conflict');self.assertEqual(len(candidate['snapshots']),2);self.assertEqual(self.store.get(pid)['stock'],0)
        pre=self.collection.preview({'candidate_ids':[candidate['id']]});self.assertEqual(pre['counts']['blocked'],1)
    def conflict(self):
        self.collect();changed=self.item();changed['stock']=7
        self.collect(items=[changed],key='changed')
        return self.collection.state()['candidates'][0]
    def resolve_body(self,candidate,index=1):
        return dict(candidate_id=candidate['id'],revision=candidate['revision'],snapshot_index=index,confirmed=True,note='已向供应商核对')
    def test_resolution_latest_fact_and_identical_read_stays_ready(self):
        candidate=self.conflict();resolved=self.collection.resolve(self.resolve_body(candidate))
        self.assertEqual(resolved['status'],'ready');self.assertEqual(resolved['normalized']['stock'],7)
        self.assertEqual(len(resolved['snapshots']),2);self.assertEqual(self.store.list(),[])
        changed=self.item();changed['stock']=7;self.collect(items=[changed],key='again')
        self.assertEqual(self.collection.state()['candidates'][0]['status'],'ready')
        with self.store.connect() as c:
            event=c.execute("SELECT detail FROM events WHERE action='采集事实核对'").fetchone()
            self.assertIn('已向供应商核对',event['detail'])
    def test_resolution_original_preserves_snapshots_and_stale_revision_rejected(self):
        candidate=self.conflict();body=self.resolve_body(candidate,0)
        resolved=self.collection.resolve(body);self.assertEqual(resolved['normalized']['stock'],0)
        self.assertEqual(resolved['snapshots'][1]['normalized']['stock'],7)
        with self.assertRaises(Problem):self.collection.resolve(body)
        changed=self.item();changed['stock']=7;self.collect(items=[changed],key='new-contradiction')
        self.assertEqual(self.collection.state()['candidates'][0]['status'],'conflict')
    def test_resolution_confirmation_and_note_required(self):
        candidate=self.conflict();body=self.resolve_body(candidate)
        for extra in ({'confirmed':False},{'revision':True},{'snapshot_index':True},{'snapshot_index':2},{'note':''},{'note':'x'*1001}):
            with self.assertRaises(Problem):self.collection.resolve({**body,**extra})
        self.assertEqual(self.collection.state()['candidates'][0]['status'],'conflict')
    def test_imported_resolution_never_updates_product(self):
        self.collect();out=self.collection.apply(self.body());pid=out['created'][0];original=self.store.get(pid)
        changed=self.item();changed['stock']=7;self.collect(items=[changed],key='changed')
        candidate=self.collection.state()['candidates'][0];resolved=self.collection.resolve(self.resolve_body(candidate))
        self.assertEqual(resolved['status'],'imported');self.assertEqual(resolved['product_id'],pid)
        self.assertEqual(self.store.get(pid),original);self.assertEqual(len(self.store.list()),1)

    def test_account_identities_do_not_silently_clash(self):
        self.collect();self.collect(key='second',account_id='other')
        rows=self.collection.state()['candidates'];self.assertEqual(len(rows),2)
        pre=self.collection.preview({'candidate_ids':[r['id'] for r in rows]});self.assertEqual(pre['counts']['blocked'],2);self.assertEqual(self.store.list(),[])
        single=self.collection.preview({'candidate_ids':[rows[0]['id']]});self.assertEqual(single['counts']['blocked'],1)
    def test_stale_preview_and_atomic_rollback(self):
        self.collect(items=[self.item('a'),self.item('b')]);body=self.body();original=self.store.event;calls=[]
        def fail(*args):
            calls.append(1)
            if len(calls)==2:raise Problem('injected')
            return original(*args)
        with patch.object(self.store,'event',side_effect=fail):
            with self.assertRaises(Problem):self.collection.apply(body)
        self.assertEqual(self.store.list(),[]);self.assertTrue(all(r['status']=='ready' for r in self.collection.state()['candidates']))
        self.store.import_rows([self.item('a')])
        with self.assertRaises(Problem):self.collection.apply(body)
    def test_repeated_cursor_malformed_page_and_invalid_row_isolation(self):
        run=self.create()
        page={'items':[self.item(),{'external_id':'bad'}],'next_cursor':'same','warnings':[]}
        with patch('channel_adapters.fetch_page',return_value=page):out=self.collection.run(run['id'])
        self.assertEqual(out['status'],'attention');self.assertEqual(self.collection.state()['total'],1)
        run=self.create(key='bad')
        with patch('channel_adapters.fetch_page',return_value={'items':{},'next_cursor':False}):out=self.collection.run(run['id'])
        self.assertEqual(out['status'],'attention')
    def test_candidate_pagination_and_limits(self):
        self.collect(items=[self.item(str(i)) for i in range(50)])
        self.collect(items=[self.item('50')],key='extra')
        state=self.collection.state(page=1);self.assertEqual(state['total'],51);self.assertEqual(len(state['candidates']),1)
        with self.assertRaises(Problem):self.collection.state(page=True)
        with self.assertRaises(Problem):self.collection.preview({'candidate_ids':['x']*501})
        with self.assertRaises(Problem):self.create(key='limit',page_limit=21)
        self.app.channel_accounts.row['provider']='1688'
        with self.assertRaises(Problem):self.create(key='unsupported')
    def test_run_limit_does_not_exceed_5000(self):
        run=self.create()
        with self.store.connect() as c:c.execute('UPDATE source_collection_runs SET collected=4999 WHERE id=?',(run['id'],))
        with patch('channel_adapters.fetch_page',return_value={'items':[self.item()],'next_cursor':'more','warnings':[]}) as fetch:out=self.collection.run(run['id'])
        self.assertEqual(fetch.call_args.kwargs['limit'],1);self.assertEqual(out['collected'],5000);self.assertEqual(out['status'],'attention')

    def test_restart_unsent_queued_task_requires_retry(self):
        run=self.create();other=SourceCollection(self.app)
        self.assertEqual(other.state()['runs'][0]['status'],'attention')
        with patch('channel_adapters.fetch_page') as fetch:other.run(run['id']);fetch.assert_not_called()
        self.assertEqual(other.control({'action':'retry','run_id':run['id']})['status'],'queued')

    def test_restore_pending_prevents_collection_and_discards_inflight(self):
        pending=Path(self.tmp.name)/'pending';self.app.recovery=SimpleNamespace(pending=pending)
        run=self.create()
        def fetch(*args,**kwargs):pending.touch();return {'items':[self.item()],'next_cursor':None,'warnings':[]}
        with patch('channel_adapters.fetch_page',side_effect=fetch):out=self.collection.run(run['id'])
        self.assertEqual(out['status'],'attention');self.assertEqual(self.collection.state()['total'],0)
        with self.assertRaises(Problem):self.collection.control({'action':'retry','run_id':run['id']})

if __name__=='__main__':unittest.main()
