"""Bootstrap is bounded and catches changes while the client assembles its catalog."""
import json
import time
from datetime import datetime,timedelta,timezone
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Store

class CatalogBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
        self.ids=self.seed(1001)
    def tearDown(self):self.tmp.cleanup()
    def seed(self,n):
        ids=[]
        for base in range(0,n,500):ids.extend(self.store.import_rows([{'title_zh':'分页合成商品 '+str(i)} for i in range(base,min(n,base+500))])['created'])
        return ids
    def merge(self,cache,value):
        if value.get('catalog_reset'):cache={}
        if value.get('catalog_bootstrap'):
            for p in value['products']:cache[p['id']]=p
        elif 'products' in value:cache={p['id']:p for p in value['products']}
        else:
            for pid in value.get('removed_product_ids',[]):cache.pop(pid,None)
            for p in value.get('product_changes',[]):cache[p['id']]=p
        return cache
    def drain(self,first,cache=None):
        cache={} if cache is None else cache;value=first;pages=[]
        while True:
            pages.append(value);cache=self.merge(cache,value)
            if not value.get('catalog_has_more'):break
            self.assertLess(len(pages),100)
            value=self.store.catalog_snapshot(value['catalog_token'],paged=True)
        return cache,pages
    def test_10000_twenty_pages_then_delta_handoff_matches_legacy(self):
        self.seed(8999);legacy=self.store.catalog_snapshot();first=self.store.catalog_snapshot(paged=True)
        cache,pages=self.drain(first,{'old-cache':{'id':'old-cache'}})
        boot=[p for p in pages if p.get('catalog_bootstrap')]
        self.assertEqual(len(boot),20);self.assertEqual(len(pages),21)
        self.assertEqual([len(p['products']) for p in boot],[500]*20)
        self.assertTrue(first['catalog_reset']);self.assertTrue(all('catalog_reset' not in p for p in boot[1:]))
        self.assertEqual(cache,{p['id']:p for p in legacy['products']});self.assertTrue(pages[-1]['catalog_unchanged'])
        self.assertEqual(len(boot[-1]['catalog_token'].split(':')),3);self.assertTrue(boot[-1]['catalog_has_more'])
    def test_changes_before_and_after_cursor_updates_deletes_are_caught(self):
        first=self.store.catalog_snapshot(paged=True);read=[p['id'] for p in first['products']];cursor=read[-1];future=sorted(set(self.ids)-set(read))
        updated=read[1];self.store.update(updated,{'note':'updated between pages'},1)
        for pid in [read[0],future[-1]]:
            with self.store.connect() as c:
                c.execute('DELETE FROM products WHERE id=?',(pid,));self.store.event(c,pid,'删除','合成翻页测试')
        before='0'*31+'1';after='f'*32;self.assertLess(before,cursor);self.assertGreater(after,cursor)
        for pid in (before,after):
            with patch('core.ident',return_value=pid):self.store.import_rows([{'title_zh':'翻页间插入 '+pid[:2]}])
        cache,pages=self.drain(first)
        self.assertEqual(cache,{p['id']:p for p in self.store.catalog_snapshot()['products']})
        self.assertEqual(cache[updated]['revision'],2);self.assertIn(before,cache);self.assertIn(after,cache)
        self.assertNotIn(read[0],cache);self.assertNotIn(future[-1],cache)
        self.assertTrue(any(read[0] in p.get('removed_product_ids',[]) for p in pages))
    def test_deleted_cursor_is_valid_continuation(self):
        first=self.store.catalog_snapshot(paged=True);cursor=first['products'][-1]['id']
        with self.store.connect() as c:c.execute('DELETE FROM products WHERE id=?',(cursor,));self.store.event(c,cursor,'删除','删除keyset游标')
        second=self.store.catalog_snapshot(first['catalog_token'],paged=True);self.assertNotIn('catalog_reset',second)
        cache,_=self.drain(first);self.assertNotIn(cursor,cache);self.assertEqual(len(cache),1000)
    def test_invalid_tokens_and_new_session_reset_bounded_catalog(self):
        first=self.store.catalog_snapshot(paged=True);parts=first['catalog_token'].split(':')
        invalid=['wrong','x'*150,':'.join(parts[:-1]),':'.join(parts+[parts[-1]]),first['catalog_token'].replace(':b:',':q:'),':'.join([parts[0],'b','9'*20,parts[3],parts[4]]),':'.join([parts[0],'b',str(10**6),parts[3],parts[4]]),':'.join([parts[0],'b',parts[2],parts[3],'NOT-ID']),':'.join([parts[0],'b',parts[2],str(int(parts[3])+1),parts[4]])]
        for token in invalid:
            with self.subTest(token=token):
                value=self.store.catalog_snapshot(token,paged=True);self.assertTrue(value['catalog_reset']);self.assertEqual(len(value['products']),500)
        other=Store(self.tmp.name);value=other.catalog_snapshot(first['catalog_token'],paged=True);self.assertTrue(value['catalog_reset']);self.assertEqual(len(value['products']),500)
    def test_expired_bootstrap_and_delta_restart_without_silent_loss(self):
        with patch('core.time.time',return_value=1800000000):first=self.store.catalog_snapshot(paged=True)
        with patch('core.time.time',return_value=1800086415):
            restart=self.store.catalog_snapshot(first['catalog_token'],paged=True)
            self.assertTrue(restart['catalog_reset']);cache,pages=self.drain(restart,{'stale':{'id':'stale'}})
            self.assertEqual(len(cache),1001);self.assertNotIn('stale',cache)
            regular=self.store.catalog_snapshot()['catalog_token']
        with patch('core.time.time',return_value=1800172830):self.assertTrue(self.store.catalog_snapshot(regular,paged=True)['catalog_reset'])
    def test_many_time_expired_products_restart_with_bounded_bootstrap(self):
        checked=(datetime.now(timezone.utc)-timedelta(seconds=86401)).isoformat()
        with self.store.connect() as c:
            c.execute("UPDATE products SET data=json_set(data,'$.supply_checked_at',?)",(checked,))
            watermark=c.execute('SELECT max(id) FROM events').fetchone()[0]
        token=f'{self.store.catalog_session}:{watermark}:{int((time.time()-30)//15)}'
        value=self.store.catalog_snapshot(token,paged=True)
        self.assertTrue(value['catalog_bootstrap']);self.assertTrue(value['catalog_reset']);self.assertEqual(len(value['products']),500)
    def test_default_full_snapshot_and_incremental_compatibility(self):
        legacy=self.store.catalog_snapshot();self.assertEqual(len(legacy['products']),1001);self.assertNotIn('catalog_bootstrap',legacy)
        self.assertTrue(self.store.catalog_snapshot(legacy['catalog_token'])['catalog_unchanged'])
        pid=self.ids[0];self.store.update(pid,{'stock':1},1)
        delta=self.store.catalog_snapshot(legacy['catalog_token'],paged=True);self.assertEqual([p['id'] for p in delta['product_changes']],[pid]);self.assertNotIn('catalog_bootstrap',delta)
    def test_empty_catalog_still_hands_off_and_does_not_keep_stale_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty=Store(tmp);value=empty.catalog_snapshot(paged=True)
            self.assertEqual(value['products'],[]);self.assertTrue(value['catalog_reset']);self.assertTrue(value['catalog_has_more'])
            final=empty.catalog_snapshot(value['catalog_token'],paged=True);self.assertTrue(final['catalog_unchanged'])
    def test_client_merge_resets_accumulates_sorts_then_applies_delta(self):
        script=Path(__file__).resolve().parents[1]/'static'/'catalog.js'
        js="""const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const context=vm.createContext({});vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context);
const merge=context.mergeCatalog;let previous={products:[{id:'stale',created_at:'2020'}]};
previous=merge({catalog_bootstrap:true,catalog_reset:true,products:[{id:'b',created_at:'2024'},{id:'a',created_at:'2024'}]},previous);
assert.deepEqual(Array.from(previous.products,p=>p.id),['a','b']);
previous=merge({catalog_bootstrap:true,products:[{id:'c',created_at:'2025'},{id:'b',created_at:'2024',revision:2}]},previous);
assert.deepEqual(Array.from(previous.products,p=>p.id),['c','a','b']);assert.equal(previous.products[2].revision,2);
previous=merge({product_changes:[{id:'d',created_at:'2026'}],removed_product_ids:['a']},previous);
assert.deepEqual(Array.from(previous.products,p=>p.id),['d','c','b']);
const same=merge({catalog_unchanged:true},previous);assert.equal(same.products,previous.products);
assert.throws(()=>merge({catalog_bootstrap:true},previous));
"""
        completed=subprocess.run(['node','-e',js,str(script)],capture_output=True,text=True)
        self.assertEqual(completed.returncode,0,completed.stderr)

if __name__=='__main__':unittest.main()
