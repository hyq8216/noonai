import csv
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Store, Problem, ident
from catalog_groups import CatalogGroups


class CatalogQualityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name);self.groups=CatalogGroups(SimpleNamespace(store=self.store))
        self.ids=self.store.import_rows([dict(title_zh='规格质检'+str(i),brand='品牌',category='类目',source_sku=str(i)) for i in range(4)])['created']

    def body(self):
        return dict(name='组合质检',axes=['颜色','尺寸'],members=[dict(product_id=p,revision=1,values=dict(zip(['颜色','尺寸'],v))) for p,v in zip(self.ids,[('红','S'),('红','M'),('蓝','S')])])

    def save(self,body=None):
        b=body or self.body();return self.groups.save({**b,'preview_digest':self.groups.preview(b)['preview_digest'],'request_id':ident(),'confirmed':True})

    def facts(self):
        with self.store.connect() as c:return [tuple(r) for r in c.execute('SELECT * FROM products ORDER BY id')]

    def test_observed_matrix_gap_and_read_only_diagnosis(self):
        before=self.facts();b=self.body();q=self.groups.diagnose(b)['quality']
        self.assertEqual((q['expected_combinations'],q['occupied_combinations'],q['missing_combinations'],q['coverage']),(4,3,1,.75))
        gap=next(x for x in q['matrix'] if x['status']=='missing');self.assertEqual(gap['values'],{'颜色':'蓝','尺寸':'M'})
        self.assertEqual(gap['product_ids'],[]);self.assertEqual(q['error_count'],0)
        group=self.save();self.assertFalse(group['review_required']);self.assertEqual(self.facts(),before)
        edit={**b,'id':group['id'],'revision':1}
        self.assertEqual(self.groups.preview(edit)['group']['quality']['error_count'],0)
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM products').fetchone()[0],4)

    def test_incomplete_duplicate_and_cross_group_drafts_are_actionable_but_save_stays_strict(self):
        saved=self.save();b=self.body();b['members'][1]['values']=dict(b['members'][0]['values']);b['members'][2]['values']['尺寸']=''
        q=self.groups.diagnose(b)['quality'];codes={i['code'] for i in q['issues']}
        self.assertTrue({'duplicate_combination','missing_axis_value','other_group'}<=codes)
        with self.assertRaises(Problem):self.groups.preview(b)
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT revision FROM catalog_groups WHERE id=?',(saved['id'],)).fetchone()[0],1)

    def test_current_facts_membership_integrity_and_deleted_product(self):
        saved=self.save();before=self.facts()
        with self.store.connect() as c:
            row=c.execute('SELECT data FROM products WHERE id=?',(self.ids[0],)).fetchone();d=json.loads(row['data']);d['brand']='新品牌';c.execute('UPDATE products SET data=? WHERE id=?',(json.dumps(d),self.ids[0]))
            c.execute('DELETE FROM catalog_group_members WHERE product_id=?',(self.ids[1],))
            c.execute('INSERT INTO catalog_group_members VALUES(?,?)',(self.ids[3],saved['id']))
            c.execute('DELETE FROM products WHERE id=?',(self.ids[2],))
        changed=self.facts();group=self.groups.state()['rows'][0];codes={i['code'] for i in group['quality']['issues']}
        self.assertTrue({'facts_changed','brand_mismatch','mixed_brands','missing_membership','extra_membership','missing_product'}<=codes)
        self.assertTrue(group['review_required']);self.assertEqual(self.facts(),changed);self.assertNotEqual(before,changed)
        self.assertIn('已删除',self.groups.export_csv(saved['id']))

    def test_bounded_three_axis_matrix_and_strict_payload_limits(self):
        ids=self.store.import_rows([dict(title_zh='矩阵'+str(i),brand='品牌',category='类目') for i in range(100)])['created']
        b=dict(name='稀疏矩阵',axes=['a','b','c'],members=[dict(product_id=p,revision=1,values={a:str(i) for a in ['a','b','c']}) for i,p in enumerate(ids)])
        q=self.groups.diagnose(b)['quality'];self.assertEqual(q['expected_combinations'],1000000);self.assertEqual(q['missing_combinations'],999900)
        self.assertEqual(len(q['matrix']),200);self.assertTrue(q['matrix_truncated'])
        for bad in ({**b,'unknown':True},{**b,'members':b['members']*2},{**b,'axes':['a']*4}):
            with self.assertRaises(Problem):self.groups.diagnose(bad)
        b['members'][0]['values']['a']='x'*101
        with self.assertRaises(Problem):self.groups.diagnose(b)

    def test_safe_csv_formula_delimiters_unicode_and_fresh_facts(self):
        b=self.body();b['name']='=SUM(1,2)';b['members'][0]['values']['颜色']=' @恶意,值\n下一行';g=self.save(b);before=self.facts()
        csv_text=self.groups.export_csv(g['id']);self.assertTrue(csv_text.startswith('\ufeff'));rows=list(csv.reader(io.StringIO(csv_text.lstrip('\ufeff'))))
        cells=[cell for row in rows for cell in row];self.assertIn("'@恶意,值\n下一行",cells);self.assertIn("'=SUM(1,2)",cells)
        self.assertTrue(any('不会自动生成SKU' in x for x in cells));self.assertEqual(self.facts(),before)
        with self.assertRaises(Problem):self.groups.export_csv('missing')

if __name__=='__main__':unittest.main()
