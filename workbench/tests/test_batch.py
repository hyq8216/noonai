"""Batch preflight and missing-only processing: no network or paid calls."""
import json
import sys
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import App
from core import Problem
from models import CONTENT_FIELDS

CONTENT={'title_en':'Five black clips','description_en':'Five black plastic clips.','title_ar':'مشابك سوداء','description_ar':'خمسة مشابك بلاستيكية سوداء'}
class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.app=App(self.tmp.name);self.store=self.app.store;self.auto=self.app.automation
    def tearDown(self):
        self.app.executor.shutdown(wait=True);self.app.visuals.close();self.tmp.cleanup()
    def product(self,**extra):
        return self.store.get(self.store.import_rows([{'title_zh':'QA夹子','source_url':'https://example.com/'+uuid.uuid4().hex,'supplier':'QA供货','facts':'五个黑色塑料夹子',**extra}])['created'][0])
    def body(self,products,**plan):
        return {'name':'QA batch','request_id':uuid.uuid4().hex,'product_ids':[p['id'] for p in products],'plan':{'missing_only':True,**plan}}
    def start(self,b):
        preview=self.auto.preflight(b);b={**b,'preflight_token':preview['token']};return self.auto.create(b),b
    def item(self,rid):return next(i for i in self.auto.state()['items'] if i['run_id']==rid)
    def test_preflight_is_read_only_and_isolates_missing_and_active(self):
        good=self.product();bad=self.product(facts='');active=self.product()
        self.auto.create(self.body([active]))
        b=self.body([good,bad,active]);before=len(self.auto.state()['runs']);preview=self.auto.preflight(b)
        self.assertEqual([r['status'] for r in preview['rows']],['ready','blocked','active']);self.assertEqual(before,len(self.auto.state()['runs']))
        result=self.auto.create({**b,'preflight_token':preview['token']})
        items=[i for i in self.auto.state()['items'] if i['run_id']==result['id']];self.assertEqual([i['product_id'] for i in items],[good['id']])
    def test_stale_preview_fails_atomically_then_can_recheck(self):
        a=self.product();b=self.product();body=self.body([a,b]);preview=self.auto.preflight(body)
        self.store.update(b['id'],{'facts':'六个夹子'},b['revision'])
        with self.assertRaises(Problem):self.auto.create({**body,'preflight_token':preview['token']})
        self.assertEqual(self.auto.state()['runs'],[])
        self.start(body);self.assertEqual(len(self.auto.state()['items']),2)
    def test_idempotent_replay_after_work_and_restart(self):
        p=self.product();result,b=self.start(self.body([p]));self.auto.tick()
        self.assertEqual(result,self.auto.create(b));self.assertEqual(len(self.auto.state()['items']),1)
        from automation import Automation
        self.assertEqual(result,Automation(self.app).create(b))
        with self.assertRaises(Problem):self.auto.create({**b,'plan':{'translate':True,'missing_only':True}})
    def test_concurrent_double_click_creates_one_run(self):
        p=self.product();b=self.body([p]);b['preflight_token']=self.auto.preflight(b)['token']
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(self.auto.create,[b,b]))
        self.assertEqual(results[0],results[1]);self.assertEqual(len(self.auto.state()['runs']),1)
    def test_missing_only_preserves_existing_text_and_fills_blank_fields(self):
        p=self.product(title_en='Curated, exact wording',description_en='Curated description.');b=self.body([p],translate=True)
        with patch.object(self.app.models,'ready',return_value=True):result,_=self.start(b)
        self.auto.tick()
        with patch.object(self.app.models,'translate',return_value={'content':CONTENT,'call_id':'qa','model':'qa','warnings':[]}) as call:self.auto.tick();self.assertEqual(call.call_count,1)
        current=self.store.get(p['id']);self.assertEqual(current['title_en'],p['title_en']);self.assertEqual(current['description_en'],p['description_en']);self.assertEqual(current['title_ar'],CONTENT['title_ar']);self.assertFalse(current['reviewed'])
    def test_complete_content_and_verified_images_skip_calls_and_preserve_revision(self):
        p=self.product(**CONTENT)
        images=[{'id':'qa','source':'qa.png','file':'qa.jpg','public_url':'https://example.com/qa.jpg'}]
        p=self.store.update(p['id'],{'content_verified':True,'images_verified':True},p['revision'],{'images':images})
        b=self.body([p],translate=True,review=True,image_template='square')
        preview=self.auto.preflight(b);self.assertEqual(preview['calls'],0);self.assertFalse(preview['rows'][0]['images'])
        result,_=self.start(b)
        with patch.object(self.app.models,'translate') as translate,patch.object(self.app.models,'review') as review,patch.object(self.app.media,'submit') as image:
            for _ in range(5):self.auto.tick()
            translate.assert_not_called();review.assert_not_called();image.assert_not_called()
        current=self.store.get(p['id']);self.assertEqual(current['revision'],p['revision']);self.assertEqual(current['images'],images)
    def test_models_and_submit_checked_only_when_needed(self):
        p=self.product();b=self.body([p],translate=True)
        preview=self.auto.preflight(b);self.assertEqual(preview['rows'][0]['status'],'blocked');self.assertIn('模型',preview['rows'][0]['reasons'][0])
        preview=self.auto.preflight(self.body([p],submit=True));self.assertEqual(preview['eligible_ids'],[])
        with self.assertRaises(Problem):self.auto.create({**b,'preflight_token':self.auto.preflight(b)['token']})
    def test_busy_job_and_changed_model_settings_invalidate_preview(self):
        p=self.product();b=self.body([p]);preview=self.auto.preflight(b);self.store.add_job(p['id'],'translate',p['revision'])
        with self.assertRaises(Problem):self.auto.create({**b,'preflight_token':preview['token']})
        self.assertEqual(self.auto.preflight(b)['rows'][0]['status'],'active')
        other=self.product();b=self.body([other],translate=True)
        with patch.object(self.app.models,'ready',return_value=True):preview=self.auto.preflight(b)
        with self.assertRaises(Problem):self.auto.create({**b,'preflight_token':preview['token']})
    def test_500_products_partitioned_without_partial_creation(self):
        rows=[{'title_zh':'QA '+str(i),'source_url':'https://example.com/'+str(i),'supplier':'QA','facts':'5 clips' if i%2==0 else ''} for i in range(500)]
        ids=self.store.import_rows(rows)['created'];b={'product_ids':ids,'plan':{'missing_only':True},'name':'500 QA','request_id':'500qa'}
        preview=self.auto.preflight(b);self.assertEqual(len(preview['eligible_ids']),250);self.assertEqual(len(preview['rows']),500)
        self.auto.create({**b,'preflight_token':preview['token']});self.assertEqual(len(self.auto.state()['items']),250)
        with self.assertRaises(Problem):self.auto.preflight({**b,'product_ids':ids+[ids[0]]})

if __name__=='__main__':unittest.main()
