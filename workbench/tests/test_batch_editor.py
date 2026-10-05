import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Store, Problem, now
from batch_editor import BatchEditor


class BatchEditorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(self.tmp.name)
        self.editor=BatchEditor(SimpleNamespace(store=self.store))
        self.ids=self.store.import_rows([dict(title_zh='盒子'+str(i),supplier='甲',facts='',brand='',cost_cny=None,stock=None) for i in range(2)])['created']
    def tearDown(self):self.tmp.cleanup()
    def body(self, **extra):return {**dict(product_ids=self.ids,patch={'supplier':'乙'},mode='replace'),**extra}
    def apply(self, body=None, request='apply'):
        body=body or self.body()
        pre=self.editor.preview(body)
        return self.editor.apply({**body,'preview_token':pre['token'],'confirmed':True,'request_id':request})
    def set_data(self,pid, **extra):
        with self.store.connect() as c:
            row=c.execute('SELECT data FROM products WHERE id=?',(pid,)).fetchone()
            data={**json.loads(row['data']),**extra}
            c.execute('UPDATE products SET data=? WHERE id=?',(json.dumps(data),pid))
    def test_fill_empty_keeps_known_zero_and_fills_unknown(self):
        self.set_data(self.ids[0],stock=0,cost_cny=0)
        body=self.body(patch={'stock':5,'cost_cny':7,'brand':'品牌'},mode='fill_empty')
        pre=self.editor.preview(body)
        known=next(r for r in pre['rows'] if r['id']==self.ids[0])
        self.assertFalse(next(f for f in known['fields'] if f['field']=='stock')['changed'])
        self.apply(body)
        first,second=[self.store.get(pid) for pid in self.ids]
        self.assertEqual((first['stock'],first['cost_cny']),(0,0))
        self.assertEqual((second['stock'],second['cost_cny']),(5,7))
        self.assertEqual(first['brand'],'品牌')
    def test_unchanged_does_not_invalidate_approval_or_revision(self):
        with self.store.connect() as c:c.execute('UPDATE products SET approved_revision=revision WHERE id=?',(self.ids[0],))
        body=self.body(patch={'supplier':'甲'},mode='fill_empty')
        pre=self.editor.preview(body)
        self.assertEqual(pre['unchanged'],2)
        self.assertFalse(pre['can_apply'])
        with self.assertRaises(Problem):self.apply(body)
        first=self.store.get(self.ids[0])
        self.assertEqual(first['revision'],1)
        self.assertEqual(first['approved_revision'],1)
    def test_mixed_batch_changes_only_different_members(self):
        self.set_data(self.ids[0],supplier='乙')
        with self.store.connect() as c:c.execute('UPDATE products SET approved_revision=revision WHERE id=?',(self.ids[0],))
        result=self.apply()
        self.assertEqual(result['unchanged'],[self.ids[0]])
        self.assertEqual(len(result['changed']),1)
        self.assertEqual(self.store.get(self.ids[0])['revision'],1)
        self.assertEqual(self.store.get(self.ids[0])['approved_revision'],1)
        self.assertEqual(self.store.get(self.ids[1])['revision'],2)

    def test_actual_edits_clear_approval_and_fact_confirmations(self):
        for pid in self.ids:self.set_data(pid,content_verified=True,images_verified=True,category_verified=True)
        with self.store.connect() as c:c.execute('UPDATE products SET approved_revision=revision')
        out=self.apply(self.body(patch={'facts':'真实尺寸10cm','category':'home'}))
        self.assertEqual(len(out['changed']),2)
        for pid in self.ids:
            p=self.store.get(pid)
            self.assertIsNone(p['approved_revision'])
            self.assertEqual(p['revision'],2)
            self.assertFalse(p['content_verified'])
            self.assertFalse(p['images_verified'])
            self.assertFalse(p['category_verified'])
    def test_supplier_cost_change_keeps_content_confirmation(self):
        self.set_data(self.ids[0],content_verified=True,images_verified=True,category_verified=True)
        self.apply(self.body(product_ids=[self.ids[0]],patch={'supplier':'乙','cost_cny':12}))
        p=self.store.get(self.ids[0])
        self.assertTrue(p['content_verified'])
        self.assertTrue(p['images_verified'])
        self.assertTrue(p['category_verified'])
    def test_explicit_replace_clear_brand_and_unknown_numeric(self):
        self.set_data(self.ids[0],brand='ABC',cost_cny=0)
        body=self.body(product_ids=[self.ids[0]],patch={'brand':'','cost_cny':None})
        pre=self.editor.preview(body)
        self.assertIn('清空',pre['rows'][0]['fields'][0]['reason'])
        self.apply(body)
        p=self.store.get(self.ids[0])
        self.assertEqual(p['brand'],'')
        self.assertIsNone(p['cost_cny'])
    def test_illegal_product_title_blocks_whole_batch(self):
        body=self.body(patch={'title_zh':''})
        pre=self.editor.preview(body)
        self.assertEqual(pre['blocked'],2)
        with self.assertRaises(Problem):self.apply(body)
        self.assertEqual([self.store.get(pid)['revision'] for pid in self.ids],[1,1])
    def test_input_rejects_unselected_fields_nan_negative_and_bad_ids(self):
        cases=[{'patch':{}},{'patch':{'images':[]}},{'patch':{'source_url':'x'}},{'patch':{'stock':-1}},
               {'patch':{'cost_cny':float('inf')}},{'patch':{'stock':1.5}},{'patch':{'stock':True}},
               {'patch':{'brand':None}},{'mode':'append'},{'product_ids':[]},{'product_ids':[self.ids[0]]*2}]
        for extra in cases:
            with self.subTest(extra=extra),self.assertRaises(Problem):self.editor.preview({**self.body(),**extra})
    def test_missing_product_blocks_everything(self):
        body=self.body(product_ids=[self.ids[0],'missing'])
        self.assertEqual(self.editor.preview(body)['blocked'],1)
        with self.assertRaises(Problem):self.apply(body)
        self.assertEqual(self.store.get(self.ids[0])['revision'],1)
    def test_existing_jobs_block_and_new_job_makes_preview_stale(self):
        body=self.body()
        pre=self.editor.preview(body)
        jid=self.store.add_job(self.ids[0],'translate',1)
        self.assertEqual(self.editor.preview(body)['blocked'],1)
        with self.assertRaises(Problem):self.editor.apply({**body,'preview_token':pre['token'],'confirmed':True,'request_id':'stale_job'})
        self.assertEqual(self.store.get(self.ids[1])['revision'],1)
        self.store.job_result(jid,'done','done')
        self.apply(body)
    def test_active_automation_visual_media_are_blocked(self):
        with self.store.connect() as c:
            c.executescript('''CREATE TABLE automation_items(id TEXT,product_id TEXT,revision INTEGER,step INTEGER,status TEXT,attempt INTEGER,updated_at TEXT);
CREATE TABLE visual_jobs(id TEXT,status TEXT,recipe TEXT,updated_at TEXT);
CREATE TABLE media_tasks(id TEXT,status TEXT,recipe TEXT,updated_at TEXT);''')
            c.execute('INSERT INTO automation_items VALUES(?,?,?,?,?,?,?)',('auto',self.ids[0],1,1,'approval',0,now()))
            c.execute('INSERT INTO visual_jobs VALUES(?,?,?,?)',('visual','generating',json.dumps({'product_id':self.ids[1]}),now()))
            c.execute('INSERT INTO media_tasks VALUES(?,?,?,?)',('media','running',json.dumps({'product_video':{'product_id':self.ids[1]}}),now()))
        self.assertEqual(self.editor.preview(self.body())['blocked'],2)
        with self.assertRaises(Problem):self.apply()
    def test_token_binds_full_data_approval_revision_and_patch(self):
        body=self.body()
        pre=self.editor.preview(body)
        self.set_data(self.ids[0],platform={'readback':{'status':'new'}})
        with self.assertRaises(Problem):self.editor.apply({**body,'preview_token':pre['token'],'confirmed':True,'request_id':'stale_data'})
        pre=self.editor.preview(body)
        with self.store.connect() as c:c.execute('UPDATE products SET approved_revision=revision WHERE id=?',(self.ids[0],))
        with self.assertRaises(Problem):self.editor.apply({**body,'preview_token':pre['token'],'confirmed':True,'request_id':'stale_approval'})
        pre=self.editor.preview(body)
        with self.assertRaises(Problem):self.editor.apply({**body,'patch':{'supplier':'丙'},'preview_token':pre['token'],'confirmed':True,'request_id':'stale_patch'})
        pre=self.editor.preview(body)
        self.store.update(self.ids[0],{'supplier':'丁'},1)
        with self.assertRaises(Problem):self.editor.apply({**body,'preview_token':pre['token'],'confirmed':True,'request_id':'stale_revision'})
    def test_source_provenance_media_sku_and_unselected_fields_preserved(self):
        pid=self.ids[0]
        source={'account_id':'a','snapshots':[{'stock':0}]}
        images=[{'id':'im','file':'original.png','public_url':'https://example.com/img.png'}]
        self.set_data(pid,source_collection=source,images=images,title_en='Original English',title_ar='عربي',platform={'receipt':'r'})
        before=self.store.get(pid)
        self.apply(self.body(product_ids=[pid],patch={'facts':'供货事实'}))
        after=self.store.get(pid)
        for key in ('source_collection','source_snapshot','images','partner_sku','platform','title_en','title_ar'):
            self.assertEqual(before[key],after[key])
    def test_mid_batch_failure_rolls_back_products_events_revisions_receipt(self):
        original=self.store.update
        count=0
        def fail_second(*args,**kwargs):
            nonlocal count
            count+=1
            result=original(*args,**kwargs)
            if count==2:raise Problem('第二件写入失败')
            return result
        with patch.object(self.store,'update',side_effect=fail_second),self.assertRaises(Problem):self.apply()
        self.assertEqual([self.store.get(pid)['revision'] for pid in self.ids],[1,1])
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM revisions').fetchone()[0],2)
            self.assertEqual(c.execute('SELECT count(*) FROM events').fetchone()[0],2)
            self.assertEqual(c.execute('SELECT count(*) FROM batch_editor_requests').fetchone()[0],0)
        self.assertEqual(len(self.apply()['changed']),2)
    def test_receipt_durable_replay_after_product_changed(self):
        body=self.body()
        pre=self.editor.preview(body)
        apply={**body,'preview_token':pre['token'],'confirmed':True,'request_id':'durable'}
        first=self.editor.apply(apply)
        self.store.update(self.ids[0],{'supplier':'丁'},2)
        second=BatchEditor(SimpleNamespace(store=self.store)).apply(apply)
        self.assertTrue(second['replayed'])
        self.assertEqual(first['changed'],second['changed'])
        with self.assertRaises(Problem):self.editor.apply({**apply,'patch':{'supplier':'改'}})
        self.assertEqual(self.store.get(self.ids[0])['revision'],3)
    def test_concurrent_same_request_writes_one_batch(self):
        body=self.body()
        pre=self.editor.preview(body)
        apply={**body,'preview_token':pre['token'],'confirmed':True,'request_id':'parallel'}
        results=[]
        threads=[threading.Thread(target=lambda:results.append(self.editor.apply(apply))) for unused in range(4)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertEqual(len(results),4)
        self.assertEqual(sum(not r['replayed'] for r in results),1)
        self.assertEqual([self.store.get(pid)['revision'] for pid in self.ids],[2,2])
    def test_confirmation_required(self):
        body=self.body()
        pre=self.editor.preview(body)
        with self.assertRaises(Problem):self.editor.apply({**body,'preview_token':pre['token'],'request_id':'no_confirm'})
    def test_state_50_pagination_literal_search_and_filters(self):
        self.store.import_rows([dict(title_zh='商品'+str(i),supplier='供应商',source_sku=str(i),cost_cny=0,stock=0) for i in range(105)])
        with patch.object(self.store,'list',side_effect=AssertionError('不得整库读取')):
            first=self.editor.state()
            second=self.editor.state(1)
            third=self.editor.state(2)
        self.assertEqual((len(first['rows']),len(second['rows']),len(third['rows'])),(50,50,7))
        self.assertFalse(set(r['id'] for r in first['rows'])&set(r['id'] for r in second['rows']))
        self.assertEqual(self.editor.state(group='missing_cost')['total'],2)
        self.assertEqual(self.editor.state(group='zero_stock')['total'],105)
        self.assertEqual(self.editor.state(query='供应商')['total'],105)
        self.assertEqual(self.editor.state(query='%')['total'],0)
        with self.assertRaises(Problem):self.editor.state(group='unknown')
    def test_selection_boundary_500_and_501(self):
        extra=self.store.import_rows([dict(title_zh='商品'+str(i)) for i in range(499)])['created']
        ids=self.ids+extra
        self.assertEqual(len(self.editor.preview(self.body(product_ids=ids[:500]))['rows']),500)
        with self.assertRaises(Problem):self.editor.preview(self.body(product_ids=ids))


if __name__=='__main__':unittest.main()
