#!/usr/bin/env python3
"""Synthetic 10000-SKU capacity check for supplier quotes, FX and replenishment."""
import argparse
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
from benchmark_erp import measure, sanitized_env
from core import Store
from operations import Operations
from supplier_quotes import SupplierQuotes
from fx_registry import FXRegistry
from replenishment import Replenishment
import connectors


def run():
    with tempfile.TemporaryDirectory(prefix='noonai-supply-capacity-') as folder:
        store=Store(folder);ops=Operations(store)
        app=SimpleNamespace(store=store,ops=ops,write_lock=threading.RLock())
        quotes=SupplierQuotes(app);fx=FXRegistry(app);replenishment=Replenishment(app)
        ids=[]
        for start in range(0,10000,500):
            ids.extend(store.import_rows([{'title_zh':f'合成补货商品 {i:05d}','source_sku':f'SUP-{i:05d}',
                'supplier':'合成供货商','facts':'合成容量资料','cost_cny':5,'stock':20}
                for i in range(start,start+500)])['created'])
        with store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            warehouse=ops.entity(c,{'kind':'warehouse','name':'合成容量仓'})['id']
            suppliers=[ops.entity(c,{'kind':'supplier','name':f'合成报价供应商 {i}'})['id'] for i in range(10)]
            for pid in ids:ops.adjust(c,{'product_id':pid,'warehouse_id':warehouse,'quantity':20,'direction':'in','reason':'合成容量期初'})
            for pid in ids[:100]:ops.purchase(c,{'supplier_id':suppliers[0],'warehouse_id':warehouse,
                'lines':[{'product_id':pid,'quantity':10,'unit_price':'5'}]})
        quote_body={'name':'100SKU×10供应商合成询价','status':'draft','supplier_ids':suppliers,
            'members':[{'product_id':pid,'revision':store.get(pid)['revision'],'quantity':100} for pid in ids[:100]],'quotes':[]}
        preview=quotes.preview(quote_body)
        quote=quotes.save({**quote_body,'preview_digest':preview['preview_digest'],'confirmed':True,'request_id':'capacity-quote'})
        for i,pid in enumerate(ids[:5]):
            body={'product_id':pid,'warehouse_id':warehouse,'revision':0,'minimum':30,'target':100,'lead_days':7,'note':'合成手动参数'}
            p=replenishment.preview(body)
            replenishment.apply({**body,'preview_token':p['preview_token'],'confirmed':True,'request_id':f'capacity-rule-{i}'})
        record=None
        for i in range(61):
            body={'name':'合成SAR-CNY档案','from_currency':'SAR','to_currency':'CNY','direction':'to_per_from',
                'rate':str(1.8+i/1000),'effective_from':'2026-10-03','evidence':f'合成汇率版本依据 {i}'}
            if record:body.update(id=record['id'],revision=record['revision'])
            p=fx.preview(body)
            record=fx.save({**body,'preview_digest':p['preview_digest'],'confirmed':True,'request_id':f'capacity-fx-{i}'})
        rows=replenishment.state({'warehouse_id':warehouse})
        assert rows['sku_page']['total']==10000 and len(rows['rows'])==50
        with store.connect() as c:
            for pid in ids[:5]:assert replenishment._row(c,pid,warehouse)['quantity']==70
            assert c.execute("SELECT count(*) FROM ops_documents WHERE kind='purchase'").fetchone()[0]==100
            assert c.execute('SELECT sum(on_hand) FROM ops_stock').fetchone()[0]==200000
        compact=quotes.state();assert compact['rows'][0]['member_count']==100
        assert 'quotes' not in compact['rows'][0]
        detail=quotes.get(quote['id']);assert len(detail['quotes'])==1000 and not detail['sendable']
        history=fx.get(record['id']);assert len(history['versions'])==50 and history['history_total']==61
        history2=fx.get(record['id'],1);assert len(history2['versions'])==11
        return {'synthetic_only':True,'products':10000,'stock_rows':10000,'open_purchases':100,
            'quote_members':100,'quote_suppliers':10,'quote_rows':1000,'fx_versions':61,'manual_replenishment_rules':5,
            'measurements':[
                measure('replenishment_50_of_10000',lambda:replenishment.state({'warehouse_id':warehouse}),lambda v:len(v['rows'])),
                measure('supplier_quote_compact_1000_quotes',quotes.state,lambda v:len(v['rows'])),
                measure('supplier_quote_explicit_detail_1000_quotes',lambda:quotes.get(quote['id']),lambda v:len(v['quotes'])),
                measure('fx_history_50_of_61',lambda:fx.get(record['id']),lambda v:len(v['versions']))],
            'basis':'Best of three local calls, encoding separate; no tracemalloc. Synthetic unknown quotes remain drafts. No images, account access, service-level or multi-user guarantee.'}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    started=time.perf_counter()
    with patch.dict(os.environ,sanitized_env(),clear=True),patch.object(connectors,'request_json',side_effect=AssertionError('No external calls')) as external,patch.object(socket.socket,'connect',side_effect=AssertionError('No network')) as network:
        result=run();assert external.call_count==network.call_count==0
        result.update(external_connector_calls=external.call_count,network_connect_calls=network.call_count,elapsed_seconds=round(time.perf_counter()-started,3))
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

if __name__=='__main__':main()
