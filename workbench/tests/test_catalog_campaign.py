import sys,tempfile,unittest,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from server import App
from catalog_campaign import CatalogCampaign
from core import Problem


class CatalogCampaignTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.app=App(self.tmp.name);self.store=self.app.store
  self.campaign=CatalogCampaign(self.app)
 def tearDown(self):
  self.app.automation.close();self.app.media.close();self.app.executor.shutdown(wait=True);self.tmp.cleanup()
 def rows(self,count,missing_facts=()):
  return [{'title_zh':f'合成铺货商品 {i:05d}','source_url':f'https://detail.1688.com/offer/{uuid.uuid4().int}.html',
           'source_sku':f'SOURCE-{i:05d}','supplier':'合成供应商','facts':'' if i in missing_facts else '黑色，5件装'}
          for i in range(count)]
 def import_rows(self,rows):
  ids=[]
  for start in range(0,len(rows),500):ids.extend(self.store.import_rows(rows[start:start+500])['created'])
  return ids
 def body(self,ids,request_id=None):
  return {'product_ids':ids,'plan':{'translate':False,'review':False,'submit':False},
          'request_id':request_id or uuid.uuid4().hex,'name':'合成5000件铺货批次','confirmed':True}

 def test_five_thousand_preview_respects_ten_five_hundred_item_chunks_without_side_effects(self):
  ids=self.import_rows(self.rows(5000));before=self.store.list()
  preview=self.campaign.preview({'product_ids':ids,'plan':{'translate':False}})
  self.assertEqual(preview['count'],5000)
  self.assertEqual([c['count'] for c in preview['chunks']],[500]*10)
  self.assertEqual([c['index'] for c in preview['chunks']],list(range(1,11)))
  self.assertEqual(preview['totals'],{'ready':5000,'blocked':0,'active':0,'calls':0,'image_calls':0,'image_checks':0})
  self.assertFalse(preview['exception_rows']);self.assertTrue(preview['token'])
  self.assertEqual(len(self.store.history(False)['jobs']),0)
  self.assertEqual(len(self.store.list()),len(before))
  with self.assertRaisesRegex(Problem,'1至5000'):
   self.campaign.preview({'product_ids':ids+['overflow'],'plan':{}})

 def test_apply_splits_at_five_hundred_isolates_items_and_replays_once(self):
  ids=self.import_rows(self.rows(1001,missing_facts={1000}));plan={'translate':False}
  active=self.app.automation.create({'request_id':'existing-active','name':'已有合成流程','product_ids':[ids[1]],'plan':plan})
  body=self.body(ids)
  preview=self.campaign.preview(body)
  self.assertEqual([c['count'] for c in preview['chunks']],[500,500,1])
  self.assertEqual(preview['totals']['ready'],999)
  self.assertEqual(preview['totals']['active'],1)
  self.assertEqual(preview['totals']['blocked'],1)
  body['preview_token']=preview['token']
  result=self.campaign.apply(body);replay=self.campaign.apply(body)
  self.assertEqual(sorted(r['ready'] for r in result['runs']),[499,500])
  self.assertTrue(all(r['ready']<=500 for r in result['runs']))
  self.assertEqual(result['totals']['ready'],999);self.assertEqual(result['skipped'],2)
  self.assertEqual(replay['runs'],result['runs']);self.assertTrue(replay['replayed'])
  with self.store.connect() as c:
   created=c.execute('SELECT id,run_id FROM automation_items WHERE run_id IN (?,?)',(result['runs'][0]['id'],result['runs'][1]['id'])).fetchall()
   self.assertEqual(len(created),999)
   self.assertEqual(c.execute("SELECT count(*) FROM ops_requests WHERE key=?",('catalog-campaign:'+body['request_id'],)).fetchone()[0],1)
  exceptions=self.campaign.exceptions(body['request_id'])
  self.assertTrue(exceptions['created_snapshot'])
  self.assertEqual({row['status'] for row in exceptions['rows']},{'active','blocked'})
  self.assertEqual(sum(row['id']==ids[1] for row in exceptions['rows']),1)
  self.assertEqual(sum(row['id']==ids[1000] for row in exceptions['rows']),1)
  self.assertEqual(self.campaign.latest()['request_id'],body['request_id'])
  self.assertEqual(active['id'],next(r['id'] for r in self.app.automation.state()['runs'] if r['name']=='已有合成流程'))

 def test_stale_preview_and_request_key_collision_create_no_extra_runs(self):
  ids=self.import_rows(self.rows(2));body=self.body(ids)
  body['preview_token']=self.campaign.preview(body)['token']
  product=self.store.get(ids[0]);self.store.update(product['id'],{'facts':'新事实'},product['revision'])
  with self.assertRaisesRegex(Problem,'重新预览'):
   self.campaign.apply(body)
  with self.store.connect() as c:
   self.assertEqual(c.execute("SELECT count(*) FROM automation_runs WHERE request_key LIKE ?",('stale-'+body['request_id']+'%',)).fetchone()[0],0)
  fresh=self.body(ids);fresh['preview_token']=self.campaign.preview(fresh)['token']
  result=self.campaign.apply(fresh)
  with self.assertRaisesRegex(Problem,'编号已用于不同内容'):
   self.campaign.apply({**fresh,'name':'改过名称'})
  self.assertEqual(len(self.app.automation.state()['runs']),len(result['runs']))


if __name__=='__main__':unittest.main()
