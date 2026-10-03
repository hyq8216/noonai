import concurrent.futures
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Store, Problem, ident
from catalog_groups import CatalogGroups


class CatalogGroupsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name);self.groups=CatalogGroups(SimpleNamespace(store=self.store))
        self.ids=self.store.import_rows([{'title_zh':'规格商品'+str(i),'brand':'测试品牌','category':'test-category','source_url':'https://supplier.example/product','source_sku':str(i)} for i in range(3)])['created']

    def body(self,ids=None,**extra):
        return {'name':'测试SPU','axes':['颜色'],'members':[{'product_id':pid,'revision':self.store.get(pid)['revision'],'values':{'颜色':str(i)}} for i,pid in enumerate(ids or self.ids[:2])],**extra}

    def confirmed(self,body):
        return {**body,'preview_digest':self.groups.preview(body)['preview_digest'],'confirmed':True,'request_id':ident()}

    def facts(self):
        with self.store.connect() as c:return [tuple(r) for r in c.execute('SELECT * FROM products ORDER BY id')]

    def counts(self):
        with self.store.connect() as c:return [c.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('catalog_groups','catalog_group_members','catalog_group_requests','catalog_group_audit')]

    def test_create_update_remove_and_dissolve_preserve_product_facts(self):
        before=self.facts();g=self.groups.save(self.confirmed(self.body()))
        self.assertEqual(g['revision'],1);self.assertFalse(g['review_required']);self.assertFalse(g['noon_variants_published'])
        body=self.body(self.ids,name='扩展SPU',id=g['id'],revision=g['revision'])
        g=self.groups.save(self.confirmed(body));self.assertEqual(g['revision'],2)
        g=self.groups.remove({'id':g['id'],'revision':g['revision'],'product_id':self.ids[0],'confirmed':True,'request_id':ident()})
        self.assertEqual(len(g['members']),2);self.assertEqual(g['revision'],3)
        out=self.groups.remove({'id':g['id'],'revision':3,'confirmed':True,'request_id':ident()})
        self.assertTrue(out['dissolved']);self.assertEqual(self.facts(),before);self.assertEqual(self.counts(),[0,0,4,4])

    def test_replay_and_conflicting_request_id(self):
        b=self.confirmed(self.body());g=self.groups.save(b)
        self.assertEqual(self.groups.save(b),g);self.assertEqual(self.counts(),[1,2,1,1])
        with self.assertRaises(Problem):self.groups.save({**b,'name':'不同操作'})
        r={'id':g['id'],'revision':1,'confirmed':True,'request_id':ident()}
        dissolved=self.groups.remove(r);self.assertEqual(self.groups.remove(r),dissolved)

    def test_concurrent_identical_confirmation_commits_once(self):
        b=self.confirmed(self.body())
        with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:self.groups.save(b),range(2)))
        self.assertEqual(results[0],results[1]);self.assertEqual(self.counts(),[1,2,1,1])

    def test_cross_group_exclusion_under_concurrent_saves(self):
        bodies=[self.confirmed(self.body(name=n)) for n in ('甲','乙')]
        def save(body):
            try:return self.groups.save(body)
            except Problem as e:return e
        with concurrent.futures.ThreadPoolExecutor(2) as pool:out=list(pool.map(save,bodies))
        self.assertEqual(sum(isinstance(o,Problem) for o in out),1);self.assertEqual(self.counts(),[1,2,1,1])

    def test_rollback_audit_failure_restores_existing_members(self):
        g=self.groups.save(self.confirmed(self.body()));before=self.counts()
        b=self.confirmed(self.body(self.ids,name='改组',id=g['id'],revision=1))
        with patch.object(self.store,'event',side_effect=RuntimeError('audit unavailable')):
            with self.assertRaises(RuntimeError):self.groups.save(b)
        self.assertEqual(self.counts(),before);self.assertEqual(self.groups.state()['rows'][0]['name'],'测试SPU')
        self.assertEqual(len(self.groups.save(b)['members']),3)

    def test_product_edit_requires_review_and_stale_preview_rejected(self):
        b=self.confirmed(self.body());g=self.groups.save(b);edit=self.body(id=g['id'],revision=1)
        preview=self.confirmed(edit)
        p=self.store.get(self.ids[0]);self.store.update(p['id'],{**p,'facts':'增加事实'},p['revision'])
        loaded=self.groups.state()['rows'][0];self.assertTrue(loaded['review_required']);self.assertIn(p['id'],loaded['changed_product_ids'])
        with self.assertRaises(Problem):self.groups.save(preview)
        fresh=self.body(id=g['id'],revision=1);g=self.groups.save(self.confirmed(fresh));self.assertFalse(g['review_required'])

    def test_same_revision_identity_or_facts_change_invalidates_preview(self):
        b=self.confirmed(self.body())
        with self.store.connect() as c:
            r=c.execute('SELECT data FROM products WHERE id=?',(self.ids[0],)).fetchone();d=json.loads(r['data']);d['facts']='同revision下身份或事实变化';c.execute('UPDATE products SET data=?,source_key=? WHERE id=?',(json.dumps(d),'changed-identity',self.ids[0]))
        with self.assertRaises(Problem):self.groups.save(b)
        self.assertEqual(self.counts(),[0,0,0,0])

    def test_same_revision_facts_change_marks_existing_group_review(self):
        self.groups.save(self.confirmed(self.body()))
        with self.store.connect() as c:c.execute('UPDATE products SET source_key=? WHERE id=?',('new-source',self.ids[0]))
        self.assertTrue(self.groups.state()['rows'][0]['review_required'])

    def test_stale_group_revision_and_remove_fail_without_changes(self):
        g=self.groups.save(self.confirmed(self.body()));b=self.confirmed(self.body(id=g['id'],revision=1))
        self.groups.remove({'id':g['id'],'revision':1,'product_id':self.ids[1],'confirmed':True,'request_id':ident()})
        before=self.counts()
        with self.assertRaises(Problem):self.groups.save(b)
        with self.assertRaises(Problem):self.groups.remove({'id':g['id'],'revision':1,'confirmed':True,'request_id':ident()})
        with self.assertRaises(Problem):self.groups.remove({'id':g['id'],'revision':2,'product_id':self.ids[0],'confirmed':True,'request_id':ident()})
        self.assertEqual(before,self.counts())

    def test_brand_and_category_mismatch_are_blocked(self):
        for field in ('brand','category'):
            p=self.store.get(self.ids[1]);self.store.update(p['id'],{**p,field:'不同'},p['revision'])
            with self.assertRaises(Problem):self.groups.preview(self.body())
            p=self.store.get(self.ids[1]);self.store.update(p['id'],{**p,field:'测试品牌' if field=='brand' else 'test-category'},p['revision'])

    def test_axes_member_and_combination_validation(self):
        bad=[]
        b=self.body();b['members'][1]['values']={'颜色':'0'};bad.append(b)
        b=self.body();b['members'][1]=copy.deepcopy(b['members'][0]);bad.append(b)
        for axes in ([],['颜色','颜色'],['a','b','c','d']):bad.append(self.body(axes=axes))
        b=self.body();b['members'][0]['values']={};bad.append(b)
        b=self.body();b['members'][0]['values']['颜色']='';bad.append(b)
        b=self.body();b['members'][0]['revision']=True;bad.append(b)
        for body in bad:
            with self.subTest(body=body),self.assertRaises(Problem):self.groups.preview(body)
        self.assertEqual(self.counts(),[0,0,0,0])

    def test_confirmation_preview_and_request_are_required(self):
        b=self.confirmed(self.body())
        for change in ({'confirmed':False},{'preview_digest':''},{'request_id':'invalid space'}):
            with self.assertRaises(Problem):self.groups.save({**b,**change})
        self.assertEqual(self.counts(),[0,0,0,0])

    def test_source_parent_provenance_is_candidate_only(self):
        with self.store.connect() as c:
            row=c.execute('SELECT data FROM products WHERE id=?',(self.ids[0],)).fetchone();d=json.loads(row['data']);d['source_collection']={'provider':'shopify','account_id':'account-1','snapshots':[{'raw':{'external_product_id':'parent-1'}}]};c.execute('UPDATE products SET data=? WHERE id=?',(json.dumps(d),self.ids[0]))
        state=self.groups.state();candidate=next(p['parent_candidate'] for p in state['selectable_products'] if p['id']==self.ids[0])
        self.assertEqual(candidate['source_parent_id'],'parent-1');self.assertTrue(candidate['requires_confirmation']);self.assertEqual(state['total'],0)
        group=self.groups.save(self.confirmed(self.body(source_channel='shopify',source_parent_id='parent-1')))
        self.assertEqual(group['source_parent_id'],'parent-1');self.assertEqual(group['connection'],'local_only')

    def test_pagination_search_and_maximum_members(self):
        ids=self.store.import_rows([{'title_zh':'量产'+str(i),'brand':'测试品牌','category':'test-category'} for i in range(101)])['created']
        state=self.groups.state();self.assertEqual(len(state['selectable_products']),100);self.assertEqual(state['product_total'],104)
        last=self.groups.state(page=0,product_page=1);self.assertEqual(len(last['selectable_products']),4)
        self.assertFalse({p['id'] for p in state['selectable_products']} & {p['id'] for p in last['selectable_products']})
        body=self.body(ids[:100]);group=self.groups.save(self.confirmed(body));self.assertEqual(len(group['members']),100)
        with self.assertRaises(Problem):self.groups.preview(self.body(ids))
        sku=self.store.get(ids[0])['partner_sku'];self.assertEqual(self.groups.state(query=sku)['total'],1)
        self.assertEqual(self.groups.state(query='%')['product_total'],0)
        self.assertEqual(self.groups.state(query='量产')['product_total'],101)
        with self.assertRaises(Problem):self.groups.state(product_page=True)

if __name__=='__main__':unittest.main()
