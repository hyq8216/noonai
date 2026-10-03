"""Cross-entrypoint source facts remain reviewable without invented SKUs."""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Store, now
from channel_accounts import ChannelAccounts
from domestic_capture import DomesticCapture
from source_collection import SourceCollection


class DomesticCollectionContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=Store(self.tmp.name)
        self.app=SimpleNamespace(store=self.store,channel_accounts=ChannelAccounts(self.store),
                                 write_lock=threading.RLock(),recovery=None)
        self.app.source_collection=SourceCollection(self.app)
        self.capture=DomesticCapture(self.app)
        self.account=self.app.channel_accounts.save({"provider":"1688","name":"合成国内来源","enabled":True,"config":{},"base_url":""})

    def tearDown(self):self.tmp.cleanup()

    def test_missing_sku_price_conflict_can_be_reviewed_but_never_imported(self):
        stamp=now()
        item={'product_id':'123456','sku':'','title':'合成夹子','source_url':'https://detail.1688.com/offer/123456.html',
              'source_price':1,'source_currency':'CNY','stock':None,'images':[],
              'facts':'黑色；数量待核','supplier':'合成供应商','brand':'',
              'captured_at':stamp,'method':'visible-dom'}
        body={'account_id':self.account['id'],'account_revision':self.account['revision'],'package':
              {'format':'noon-domestic-capture-v1','provider':'1688','captured_at':stamp,'items':[item]}}
        for index,price in enumerate((1,2)):
            body['package']['items'][0]['source_price']=price
            preview=self.capture.preview(body)
            self.capture.apply({**body,'preview_token':preview['token'],'confirmed':True,
                                'request_id':'domestic-contract-'+str(index)})
        row=self.app.source_collection.state()['candidates'][0]
        self.assertEqual(row['status'],'conflict')
        adopted=self.app.source_collection.resolve({'candidate_id':row['id'],'revision':row['revision'],
                'snapshot_index':1,'note':'合成验收核对价格，尚无真实规格编号','confirmed':True})
        self.assertEqual(adopted['status'],'blocked')
        self.assertEqual(adopted['normalized']['source_sku'],'')
        self.assertEqual(adopted['raw']['source_price'],2)
        self.assertIsNone(adopted['normalized']['cost_cny'])
        preview=self.app.source_collection.preview({'candidate_ids':[row['id']]})
        self.assertEqual(preview['ready'],0)
        self.assertEqual(preview['eligible_ids'],[])
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM products').fetchone()[0],0)

    def test_source_price_and_picture_changes_require_fact_review(self):
        run={'id':'run-one','provider':'custom_json','account_id':'contract-account','account_revision':1}
        item={'external_id':'variant-one','source_sku':'BLACK','source_url':'https://example.com/item',
              'title_zh':'合成夹子','facts':'黑色','source_currency':'CNY','source_price':1,
              'images':['https://example.com/first.jpg']}
        with self.store.connect() as c:
            self.app.source_collection.save_item(c,run,item)
            old=dict(c.execute('SELECT * FROM source_collection_candidates').fetchone())
            self.app.source_collection.save_item(c,{**run,'id':'run-two'},{**item,'source_price':9})
            changed=dict(c.execute('SELECT * FROM source_collection_candidates').fetchone())
            self.assertEqual(changed['status'],'conflict')
            self.assertEqual(changed['normalized'],old['normalized'])
            self.assertGreater(changed['revision'],old['revision'])
            self.app.source_collection.save_item(c,{**run,'id':'run-three'},
                {**item,'source_price':9,'images':['https://example.com/second.jpg']})
            changed=dict(c.execute('SELECT * FROM source_collection_candidates').fetchone())
            self.assertEqual(changed['status'],'conflict')
            snapshots=json.loads(changed['snapshots'])
            self.assertEqual(snapshots[0]['raw']['source_price'],1)
            self.assertEqual(snapshots[-1]['raw']['images'],['https://example.com/second.jpg'])


if __name__=='__main__':unittest.main()
