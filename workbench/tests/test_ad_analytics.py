import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Store,Problem,ident,now
from operations import Operations
from finance import Finance
from ad_analytics import AdAnalytics

class AdAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name);self.ops=Operations(self.store);self.finance=Finance(self.store)
        self.ad=AdAnalytics(SimpleNamespace(store=self.store));self.shop=self.ops.transact('entity',{'request_id':ident(),'kind':'shop','name':'广告测试店'})['id'];self.today=now()[:10]
        self.pid=self.store.import_rows([{'title_zh':'广告映射专用商品'}])['created'][0]
        self.sku=self.store.get(self.pid)['partner_sku']
    def tearDown(self):self.tmp.cleanup()
    def row(self,**extra):return {'date':self.today,'campaign':'合成活动','channel':'noon-search','currency':'SAR','spend':'10','impressions':100,'clicks':10,'orders':2,'attributed_sales':'40','attribution_basis':'点击7天',**extra}
    def preview(self,rows):return self.ad.preview({'shop_id':self.shop,'format':'json','text':json.dumps(rows)})
    def apply(self,b,**extra):return self.ad.apply({'batch_id':b['id'],'snapshot_fingerprint':b['snapshot_fingerprint'],'confirmed':True,'request_id':ident(),**extra})
    def test_preview_is_persistent_readonly_and_confirm_never_writes_finance(self):
        p=self.preview([self.row()]);self.assertEqual(p['summary']['accepted'],1);self.assertEqual(self.ad.state()['summary']['records'],0)
        reopened=AdAnalytics(SimpleNamespace(store=self.store));self.assertEqual(reopened.state({'batch_id':p['id']})['batch']['rows'][0]['status'],'accepted')
        receipt=self.apply(p);self.assertEqual(receipt['imported'],1);self.assertEqual(self.ad.state()['summary']['records'],1)
        self.assertEqual(self.finance.state()['entries'],[]);self.assertEqual(self.ops.state()['stock'],[])
    def test_multicurrency_and_attribution_and_report_levels_never_mix(self):
        p=self.preview([self.row(),self.row(currency='USD',spend='5'),self.row(campaign='另一活动',attribution_basis='展示1天'),self.row(sku=self.sku)])
        self.apply(p);g=self.ad.state()['summary']['groups'];self.assertEqual(len(g),4)
        self.assertEqual(sum(x['records'] for x in g),4)
        sar=next(x for x in g if x['currency']=='SAR' and x['report_level']=='campaign' and x['attribution_basis']=='点击7天')
        self.assertEqual(sar['spend_cents'],1000);self.assertEqual(sar['ctr'],.1);self.assertEqual(sar['cpc_cents'],100);self.assertEqual(sar['acos'],.25);self.assertEqual(sar['roas'],4)
    def test_zero_denominators_are_unknown_and_zero_numerator_is_known(self):
        self.apply(self.preview([self.row(spend='0',impressions=0,clicks=0,orders=0,attributed_sales='0')]))
        g=self.ad.state()['summary']['groups'][0]
        for key in ('ctr','cpc_cents','acos','roas'):self.assertIsNone(g[key])
        self.apply(self.preview([self.row(campaign='非零分母',spend='0',impressions=100,clicks=0,attributed_sales='20')]))
        g=next(x for x in self.ad.state()['record_page']['items'] if x['campaign']=='非零分母')
        self.assertEqual(g['ctr'],0);self.assertEqual(g['acos'],0);self.assertIsNone(g['roas'])
    def test_duplicate_same_file_and_reupload_skip_without_double_count(self):
        p=self.preview([self.row(),self.row()]);self.assertEqual(p['summary']['duplicates'],1);self.apply(p)
        p2=self.preview([self.row()]);self.assertEqual(p2['summary']['duplicates'],1);self.assertEqual(self.apply(p2)['imported'],0)
        self.assertEqual(self.ad.state()['summary']['groups'][0]['spend_cents'],1000)
    def test_conflict_isolated_by_default_and_explicit_replacement_versions_not_adds(self):
        self.apply(self.preview([self.row()]))
        conflict=self.preview([self.row(spend='20')]);self.assertEqual(conflict['summary']['conflicts'],1)
        self.assertEqual(self.apply(conflict)['isolated_conflicts'],1);self.assertEqual(self.ad.state()['record_page']['items'][0]['spend_cents'],1000)
        replacement=self.preview([self.row(spend='20')]);r=self.apply(replacement,replace_conflicts=True);self.assertEqual(r['replaced'],1)
        row=self.ad.state()['record_page']['items'][0];self.assertEqual(row['revision'],2);self.assertEqual(row['spend_cents'],2000)
        self.assertEqual(self.ad.state()['summary']['records'],1)
        with self.store.connect() as c:
            evidence=json.loads(c.execute('SELECT data FROM ad_batches WHERE id=?',(replacement['id'],)).fetchone()['data'])
            self.assertEqual(json.loads(evidence['snapshot'][0][1]['data'])['spend_cents'],1000)
    def test_contradictory_same_file_identity_all_rows_isolated(self):
        p=self.preview([self.row(),self.row(spend='20'),self.row()]);self.assertEqual(p['summary']['accepted'],0);self.assertEqual(p['summary']['errors'],3)
        self.apply(p);self.assertEqual(self.ad.state()['summary']['records'],0)
    def test_request_id_confirm_snapshot_and_stale_preview(self):
        p=self.preview([self.row()]);b={'batch_id':p['id'],'snapshot_fingerprint':p['snapshot_fingerprint'],'confirmed':True,'request_id':ident()}
        with self.assertRaises(Problem):self.ad.apply({**b,'confirmed':False})
        with self.assertRaises(Problem):self.ad.apply({**b,'snapshot_fingerprint':'bad'})
        stale=self.preview([self.row(spend='20')]);result=self.ad.apply(b);self.assertEqual(self.ad.apply(b),result)
        with self.assertRaises(Problem):self.ad.apply({**b,'replace_conflicts':True})
        with self.assertRaises(Problem):self.apply(stale)
        self.assertEqual(self.ad.state()['summary']['records'],1)
    def test_unknown_sku_retained_and_matched_sku_explained(self):
        p=self.preview([self.row(sku='UNKNOWN-SKU'),self.row(sku=self.sku),self.row()]);self.assertEqual(p['summary']['unknown_sku'],1)
        self.apply(p);items=self.ad.state()['record_page']['items'];unknown=next(r for r in items if r['sku']=='UNKNOWN-SKU')
        self.assertEqual(unknown['sku_status'],'unmatched');self.assertIsNone(unknown['product_id']);self.assertEqual(unknown['report_level'],'sku')
        matched=next(r for r in items if r['sku']==self.sku);self.assertEqual(matched['product_id'],self.pid)
    def test_invalid_rows_isolated_without_poisoning_valid_rows(self):
        p=self.preview([self.row(),self.row(campaign='bad',spend='-1'),self.row(campaign='bool',clicks=True),self.row(campaign='decimal',orders='1.5'),self.row(campaign='date',date='2026-02-30'),self.row(campaign='future',date='2999-01-01'),self.row(campaign='currency',currency='BTC')])
        self.assertEqual(p['summary']['errors'],6);self.assertEqual(self.apply(p)['imported'],1)
    def test_pagination_date_shop_currency_campaign_filters(self):
        self.apply(self.preview([self.row(campaign=f'广告活动{i}') for i in range(55)]))
        s=self.ad.state();self.assertEqual(s['record_page']['total'],55);self.assertEqual(len(s['record_page']['items']),50);self.assertEqual(len(self.ad.state(page=1)['record_page']['items']),5)
        self.assertEqual(self.ad.state({'from':self.today,'to':self.today,'shop_id':self.shop,'currency':'SAR','campaign':'广告活动5','channel':'noon-search'})['record_page']['total'],1)
        for body in ({'page':True},{'from':'2024-01-01','to':'2025-01-01'},{'shop_id':'missing'},{'currency':'BTC'}):
            with self.assertRaises(Problem):self.ad.state(body)
    def test_preview_rows_paged_and_limits(self):
        p=self.preview([self.row(campaign=str(i)) for i in range(55)]);self.assertEqual(len(p['rows']),50);self.assertEqual(len(self.ad.state({'batch_id':p['id'],'batch_page':1})['batch']['rows']),5)
        with self.assertRaises(Problem):self.preview([self.row()]*1001)
        with self.assertRaises(Problem):self.ad.preview({'shop_id':self.shop,'format':'json','text':'a'*(2*1024*1024+1)})
        for content in ('{}','null','[]'):
            with self.assertRaises(Problem):self.ad.preview({'shop_id':self.shop,'format':'json','text':content})
    def test_csv_bom_template_empty_and_formula_protection(self):
        self.assertTrue(self.ad.template().startswith(b'\xef\xbb\xbf'));self.assertEqual(len(list(csv.reader(io.StringIO(self.ad.template().decode('utf-8-sig'))))),1)
        self.apply(self.preview([self.row(campaign='=HYPERLINK("x")',channel=' @formula')]))
        rows=list(csv.reader(io.StringIO(self.ad.export().decode('utf-8-sig'))));record=next(r for r in rows if len(r)>2 and r[2].startswith("'=HYPERLINK"));self.assertTrue(record[3].startswith("'"))
        template=self.ad.template().decode('utf-8-sig')+f'{self.today},CSV活动,noon,SAR,1,0,0,0,0,,点击7天\r\n'
        p=self.ad.preview({'shop_id':self.shop,'format':'csv','text':template});self.assertEqual(p['summary']['accepted'],1)

if __name__=='__main__':unittest.main()
