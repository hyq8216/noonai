import copy,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Store,Problem,payload
from server import App
from category_rules import contract,attributes,validate,product_issues

def rule(code,kind='TEXT',**flags):return {'attribute_code':code,'attribute_type':'ATTRIBUTE_TYPE_'+kind,'is_mandatory':True,'is_localizable':False,'is_multivalued':False,**flags}
SPEC={'category_code':'test-category','attributes':[rule('product_title',is_localizable=True),rule('long_description',is_localizable=True),rule('material','SELECT',attribute_options=['Plastic','Steel']),rule('length','METRIC',number_min=1,number_max=100,attribute_metric_units=['centimeter']),rule('reusable','BOOL'),rule('feature_bullet',is_localizable=True,is_multivalued=True,max_values=2)]}
class CategoryTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.app=App(self.tmp.name);self.store=self.app.store
  self.p=self.store.get(self.store.import_rows([{'title_zh':'QA','category':'test-category','title_en':'Box','title_ar':'صندوق','description_en':'A plastic box','description_ar':'صندوق بلاستيكي'}])['created'][0])
 def tearDown(self):self.app.executor.shutdown();self.app.visuals.close();self.tmp.cleanup()
 def loaded(self):return self.app.category_rules(self.p['id'],{'revision':self.p['revision'],'source':'file','contract':SPEC})
 def filled(self):
  p=self.loaded();return self.store.update(p['id'],{'attribute_values':{'material':{'value':'Plastic'},'length':{'value':'20','unit':'centimeter'},'reusable':{'value':'false'},'feature_bullet':{'en':'One box\nPlastic','ar':'صندوق واحد\nبلاستيك'}}},p['revision'])
 def test_file_provenance_and_values_preserved_after_other_edits(self):
  p=self.filled();self.assertEqual(p['category_contract']['source'],'file');self.assertFalse(p['category_verified'])
  updated=self.store.update(p['id'],{'note':'keep values'},p['revision']);self.assertEqual(updated['attribute_values'],p['attribute_values']);self.assertEqual(updated['category_contract'],p['category_contract'])
  self.assertEqual(product_issues(updated),[])
 def test_exact_typed_payload_and_metric_pair(self):
  p=self.filled();data=payload(p)['attributes'];self.assertEqual(validate(SPEC,data),[])
  self.assertEqual(data['length']['values'],[{'value':20}]);self.assertEqual(data['length_unit']['values'],[{'value':'centimeter'}]);self.assertIs(data['reusable']['values'][0]['value'],False)
  self.assertEqual(data['feature_bullet']['values'][1],{'value':'Plastic','language':'LANGUAGE_EN','sort':2})
 def test_missing_values_are_actionable_before_submission(self):
  p=self.loaded();self.assertTrue(any('material' in s for s in p['issues']));self.assertTrue(any('length' in s for s in p['issues']))
 def test_unknown_option_bad_number_and_missing_unit(self):
  p=self.filled();p=self.store.update(p['id'],{'attribute_values':{**p['attribute_values'],'material':{'value':'plastic'},'length':{'value':'NaN','unit':''}}},p['revision'])
  errors=product_issues(p);self.assertTrue(any('允许的选项' in e for e in errors));self.assertTrue(any('有限数值' in e for e in errors));self.assertTrue(any('单位' in e for e in errors))
 def test_range_boolean_language_and_multivalue_validation(self):
  p=self.filled();data=attributes(p);data['length']['values'][0]['value']=101;data['reusable']['values'][0]['value']='false';data['feature_bullet']['values']=[{'value':'a','language':'LANGUAGE_EN','sort':5}]
  e=validate(SPEC,data);self.assertTrue(any('范围' in x for x in e));self.assertTrue(any('是或否' in x for x in e));self.assertTrue(any('AR' in x for x in e));self.assertTrue(any('排序' in x for x in e))
 def test_rule_change_and_attribute_edit_invalidate_confirmation(self):
  p=self.filled();p=self.store.update(p['id'],{'category_verified':True},p['revision']);self.assertTrue(p['category_verified'])
  p=self.store.update(p['id'],{'attribute_values':{**p['attribute_values'],'length':{'value':'21','unit':'centimeter'}}},p['revision']);self.assertFalse(p['category_verified'])
  p=self.store.update(p['id'],{'category':'other','category_verified':True},p['revision']);self.assertFalse(p['category_verified']);self.assertTrue(any('类目已变化' in x for x in p['issues']))
 def test_stale_and_wrong_category_rule_import_rejected(self):
  p=self.loaded()
  with self.assertRaises(Problem):self.app.category_rules(p['id'],{'revision':self.p['revision'],'source':'file','contract':SPEC})
  with self.assertRaises(Problem):self.app.category_rules(p['id'],{'revision':p['revision'],'source':'file','contract':{**SPEC,'category_code':'other'}})
 def test_platform_fetch_uses_saved_category_and_live_rules_block_stale_values(self):
  with patch.object(self.app,'config',return_value={'noon_ready':True}),patch('server.Noon') as client:
   client.return_value.attributes.return_value=SPEC;p=self.app.category_rules(self.p['id'],{'revision':self.p['revision'],'source':'platform'})
   client.return_value.attributes.assert_called_once_with('test-category');self.assertEqual(p['category_contract']['source'],'platform')
  data={'material':{'values':[{'value':'Plastic'}]}};live={'attributes':[rule('material','SELECT',attribute_options=['Steel'])]}
  from connectors import preflight_attributes
  with self.assertRaises(Problem):preflight_attributes(live,{'attributes':data})
 def test_contract_rejects_duplicates_and_invalid_metadata(self):
  for spec in [{'attributes':[]},{'attributes':[rule('x'),rule('x')]},{'attributes':[rule('__proto__')]},{'attributes':[rule('x',number_min='a')]},{'attributes':[rule('x',attribute_options=[{}])]}]:
   with self.assertRaises(Problem):contract(spec)
 def test_unsupported_and_html_regex_fail_closed(self):
  for r,value in [(rule('x','FILE'),'https://example.com/x'),(rule('x'),'<script>bad</script>'),(rule('x',additional_validation_regex='(a+)+'),'aaaa')]:self.assertTrue(validate({'attributes':[r]},{'x':{'values':[{'value':value}]}}))
 def test_orphan_localized_data_visible_not_silently_dropped(self):
  p=self.filled();p=self.store.update(p['id'],{'attribute_values':{**p['attribute_values'],'old_code':{'en':'old value'}}},p['revision']);self.assertTrue(any('old_code' in e for e in p['issues']))
 def test_metric_unit_must_match_language_and_sort(self):
  spec={'attributes':[rule('length','METRIC',is_localizable=True,attribute_metric_units=['cm'])]};data={'length':{'values':[{'value':2,'language':'LANGUAGE_EN'},{'value':2,'language':'LANGUAGE_AR'}]},'length_unit':{'values':[{'value':'cm','language':'LANGUAGE_AR'},{'value':'cm','language':'LANGUAGE_EN'}]}}
  self.assertTrue(any('不一致' in e for e in validate(spec,data)))
if __name__=='__main__':unittest.main()
