import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from connectors import Noon
from core import Problem
from platform_batch import PlatformBatch
from server import App
from transfer_status import batch_items,validate


def item(sku,code='OK',price=18):
    ok=code=='OK'
    return {'partner_sku':sku,'status':{'status_id':0 if ok else 5,'status_code':code,'message':''},
            'transfer_price_usd':price if ok else None,'msrp_usd':None,'is_active':True if ok else None}


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.app=App(self.tmp.name);self.store=self.app.store
    def tearDown(self):
        self.app.models.codex.close();self.app.visual_checks.close();self.app.visuals.close()
        self.app.automation.close();self.app.media.close();self.app.executor.shutdown(wait=True)
        self.tmp.cleanup()
    def products(self,n=2,mode='NGS'):
        ids=self.store.import_rows([{'title_zh':f'跨境商品 {i}','mode':mode,'transfer_usd':18} for i in range(n)])['created']
        return [self.store.get(pid) for pid in ids]
    def request(self,products,batch):
        ids=[p['id'] for p in products]
        return {'product_ids':ids,'preview_token':batch.preview({'product_ids':ids})['token'],'request_id':'transfer-test','confirmed':True}

    def test_official_batch_get_payload_and_limit(self):
        client=Noon.__new__(Noon)
        with patch.object(client,'post',return_value={}) as post:client.transfer_prices_get(['A','B'])
        post.assert_called_once_with('/xborder-pricing/v1/transfer-price/get',{'items':[{'partner_sku':'A'},{'partner_sku':'B'}]})
        with self.assertRaises(Problem):client.transfer_prices_get(['A','A'])

    def test_strict_result_and_preserve_previous(self):
        p=self.products(1)[0]
        self.store.record_transfer_price(p['id'],p['partner_sku'],item(p['partner_sku']),p['revision'])
        before=self.store.get(p['id'])['platform']
        self.assertEqual(self.store.get(p['id'])['transfer_summary']['matches_local'],True)
        self.store.record_platform(p['id'],{'sku_parent':'P'})
        self.assertEqual(self.store.get(p['id'])['transfer_summary']['group'],'priced')
        for bad in (item('wrong'),{**item(p['partner_sku']),'transfer_price_usd':True},
                    {**item(p['partner_sku']),'is_active':'yes'},
                    {**item(p['partner_sku']),'status':{'status_id':0,'status_code':'NOT_FOUND'}}):
            with self.assertRaises(Problem):validate(bad,p['partner_sku'])
        self.assertEqual(self.store.get(p['id'])['platform']['transfer_price_readback'],before['transfer_price_readback'])
        with self.assertRaises(Problem):batch_items({'items':[item('OTHER')]},[p['partner_sku']])

    def test_one_network_call_partial_result_and_missing_sku(self):
        products=self.products(3);batch=PlatformBatch(self.app,'transfer')
        with patch.object(self.app,'config',return_value={'noon_ready':True}),patch.object(self.app.executor,'submit') as submit:
            req=self.request(products,batch);result=batch.apply(req)
            self.assertEqual(len(result['jobs']),3)
            self.assertEqual(batch.apply(req)['jobs'],result['jobs'])
            self.assertEqual(submit.call_count,1)
            dispatch=submit.call_args.args[1]
        skus=[p['partner_sku'] for _,p in dispatch]
        with patch('server.Noon') as noon:
            noon.return_value.transfer_prices_get.return_value={'items':[item(skus[0],price=19),item(skus[1],'NOT_FOUND')]}
            self.app.run_transfer_batch(dispatch)
            noon.return_value.transfer_prices_get.assert_called_once_with(skus)
        self.assertEqual(batch.status(req['request_id'])['counts'],{'done':1,'needs_attention':1,'failed':1})
        first=self.store.get(dispatch[0][1]['id'])['transfer_summary']
        self.assertEqual((first['group'],first['matches_local']),('priced',False))
        self.assertEqual(self.store.get(dispatch[1][1]['id'])['transfer_summary']['group'],'missing')
        self.assertEqual(self.store.get(dispatch[2][1]['id'])['transfer_summary']['group'],'unchecked')

    def test_mode_guard_and_cancel_prevent_network(self):
        ngs=self.products(1)[0];local=self.products(1,'LOCAL')[0];batch=PlatformBatch(self.app,'transfer')
        with patch.object(self.app,'config',return_value={'noon_ready':True}),patch.object(self.app.executor,'submit') as submit:
            req=self.request([ngs,local],batch);pre=batch.preview(req)
            self.assertEqual((pre['ready'],pre['blocked']),(1,1))
            result=batch.apply(req);dispatch=submit.call_args.args[1]
        self.assertEqual(len(result['jobs']),1)
        batch.cancel(req['request_id'])
        with patch('server.Noon') as noon:self.app.run_transfer_batch(dispatch);noon.assert_not_called()
        self.assertEqual(batch.status(req['request_id'])['counts'],{'cancelled':1})

    def test_change_after_queue_does_not_send(self):
        p=self.products(1)[0];batch=PlatformBatch(self.app,'transfer')
        with patch.object(self.app,'config',return_value={'noon_ready':True}),patch.object(self.app.executor,'submit') as submit:
            req=self.request([p],batch);batch.apply(req);dispatch=submit.call_args.args[1]
        self.store.update(p['id'],{'mode':'LOCAL'},p['revision'])
        with patch('server.Noon') as noon:self.app.run_transfer_batch(dispatch);noon.assert_not_called()
        self.assertEqual(batch.status(req['request_id'])['counts'],{'failed':1})

    def test_500_skus_one_dispatch(self):
        products=self.products(500);batch=PlatformBatch(self.app,'transfer')
        with patch.object(self.app,'config',return_value={'noon_ready':True}),patch.object(self.app.executor,'submit') as submit:
            self.assertEqual(batch.preview({'product_ids':[p['id'] for p in products]})['max_content_reads'],1)
            result=batch.apply(self.request(products,batch))
        self.assertEqual(len(result['jobs']),500)
        self.assertEqual(submit.call_count,1)
        self.assertEqual(len(submit.call_args.args[1]),500)


if __name__=='__main__':unittest.main()
