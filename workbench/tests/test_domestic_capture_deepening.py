"""Quality diagnostics stay observational and preserve the intake safety gates."""
import copy
import unittest
import test_domestic_capture as fixtures
from core import Problem


class DomesticCaptureDeepeningTests(unittest.TestCase):
    setUp=fixtures.DomesticCaptureTests.setUp
    tearDown=fixtures.DomesticCaptureTests.tearDown
    body=fixtures.DomesticCaptureTests.body
    apply=fixtures.DomesticCaptureTests.apply
    candidates=fixtures.DomesticCaptureTests.candidates

    def test_unknowns_do_not_include_explicit_zero_and_preview_is_read_only(self):
        b=self.body(source_price=0,stock=0,supplier='',facts='',images=[])
        p=self.d.preview(b);q=p['rows'][0]['quality']
        self.assertEqual(q['unknown_fields'],['supplier','facts','images'])
        self.assertNotIn('missing_stock',p['quality_summary']['by_code'])
        self.assertNotIn('missing_source_price',p['quality_summary']['by_code'])
        self.assertEqual(p['quality_summary']['attention_rows'],1)
        self.assertEqual(p['quality_summary']['blocked_rows'],0)
        self.assertEqual(self.candidates(),[]);self.assertEqual(self.app.store.list(),[])
        self.assertEqual(self.d.preview(b)['token'],p['token'])

    def test_missing_spec_action_and_all_unknown_fields(self):
        p=self.d.preview(self.body(sku='',source_price=None,stock=None,facts='',images=[]))
        q=p['rows'][0]['quality'];issue=next(i for i in q['issues'] if i['code']=='missing_source_sku')
        self.assertEqual(issue['severity'],'blocker');self.assertIn('依据',issue['action'])
        self.assertEqual(len(q['unknown_fields']),6)
        self.assertEqual(p['quality_summary']['blocked_rows'],1)
        self.assertEqual(q['source']['observed_sku'],'')

    def test_batch_duplicate_uses_first_row_and_counts_observations(self):
        b=self.body();item=b['package']['items'][0]
        b['package']['items']=[copy.deepcopy(item),copy.deepcopy(item),{**item,'sku':'SECOND'}]
        b['package']['warnings']=['合成公开DOM提示']
        p=self.d.preview(b);summary=p['quality_summary']
        self.assertEqual(summary['unique_products'],1);self.assertEqual(summary['unique_identities'],2)
        self.assertEqual(summary['total_rows'],3)
        self.assertEqual(summary['by_code']['duplicate_observation'],1)
        self.assertEqual(summary['methods'],{'visible-dom':3})
        self.assertEqual(summary['package_warnings'],['合成公开DOM提示'])
        self.assertEqual(p['rows'][1]['quality']['duplicate_of_row'],1)
        self.assertIsNone(p['rows'][2]['quality']['duplicate_of_row'])
        self.assertEqual(len(self.apply(b)['created']),2)

    def test_history_duplicate_does_not_claim_package_duplicate(self):
        b=self.body();self.apply(b)
        p=self.d.preview(b);q=p['rows'][0]['quality']
        self.assertIsNone(q['duplicate_of_row']);self.assertEqual(q['changed_fields'],[])
        self.assertIn('已有相同',next(i for i in q['issues'] if i['code']=='duplicate_observation')['message'])

    def test_conflict_comparison_is_against_latest_and_does_not_write(self):
        self.apply(self.body())
        b=self.body(source_price=0,stock=None,images=[])
        p=self.d.preview(b);q=p['rows'][0]['quality']
        changed={i['field']:i for i in q['changed_fields']}
        self.assertEqual(set(changed),{'source_price','stock','images'})
        self.assertEqual(changed['source_price'],{'field':'source_price','before':2.5,'after':0.0})
        self.assertEqual(p['quality_summary']['blocked_rows'],1)
        self.assertEqual(self.candidates()[0]['raw']['source_price'],2.5)
        self.apply(b)
        newest=self.d.preview(self.body(source_price=3,stock=None,images=[]))
        self.assertEqual(newest['rows'][0]['quality']['changed_fields'],[{'field':'source_price','before':0.0,'after':3.0}])

    def test_manual_spec_preserves_observed_unknown_and_cannot_bypass_stale_token(self):
        b=self.body(sku='');old=self.d.preview(b)
        b['corrections']=[{'row':1,'sku':'VERIFIED','evidence':'合成规格单第1行'}]
        p=self.d.preview(b);q=p['rows'][0]['quality']
        self.assertEqual(q['source']['observed_sku'],'');self.assertEqual(q['source']['effective_sku'],'VERIFIED')
        self.assertNotIn('source_sku',q['unknown_fields']);self.assertIn('manual_correction',[i['code'] for i in q['issues']])
        with self.assertRaises(Problem):self.d.apply({**b,'preview_token':old['token'],'confirmed':True,'request_id':'stale-quality-test'})
        self.assertEqual(self.candidates(),[])

    def test_quality_does_not_soften_security_or_conflicting_package_rejection(self):
        for changes in ({'source_currency':''},{'source_url':'https://detail.1688.com.evil.test/offer/123456.html'},{'stock':False}):
            with self.assertRaises(Problem):self.d.preview(self.body(**changes))
        b=self.body();b['package']['items'].append({**b['package']['items'][0],'source_price':3})
        with self.assertRaises(Problem):self.d.preview(b)
        self.assertEqual(self.candidates(),[])

    def test_500_observations_are_diagnosed_without_collapsing_zero(self):
        b=self.body(source_price=0,stock=0)
        b['package']['items']=[{**b['package']['items'][0],'sku':f'SKU-{i}'} for i in range(500)]
        p=self.d.preview(b)
        self.assertEqual(p['quality_summary']['unique_identities'],500)
        self.assertEqual(p['quality_summary']['unknown_fields'],{'supplier':500})
        self.assertEqual(p['counts']['new'],500)
