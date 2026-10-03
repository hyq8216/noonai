#!/usr/bin/env python3
"""Reproducible synthetic ERP capacity measurements; never opens business data.

Run: .venv/bin/python scripts/benchmark_erp.py --output /tmp/noonai-capacity.json
Timings are local observations, not acceptance thresholds or service guarantees.
"""
import argparse
import gc
import json
import os
from pathlib import Path
import platform
import resource
import socket
import sys
import tempfile
import threading
import time
import tracemalloc
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'workbench'))


def sanitized_env():
    """Remove provider/account configuration without inspecting or reporting values."""
    prefixes=('NOON_','OPENAI_','TEXT_','IMAGE_HOST_','SHOPIFY_','EBAY_','ANTHROPIC_','MODEL_','CODEX_','AZURE_','AWS_','GOOGLE_','GCP_')
    return {key:value for key,value in os.environ.items() if not key.upper().startswith(prefixes) and not any(marker in key.upper() for marker in ('API_KEY','ACCESS_TOKEN','SECRET','CREDENTIAL','PASSWORD'))}


def json_bytes(value):
    # Default server json.dumps uses spaces; measure the same UTF-8 JSON shape.
    return len(json.dumps(value,ensure_ascii=False).encode('utf-8'))


def measure(name,call,counts):
    samples=[];sizes=[];row_counts=[];encode=[]
    for _ in range(3):
        start=time.perf_counter();value=call();samples.append((time.perf_counter()-start)*1000)
        start=time.perf_counter();sizes.append(json_bytes(value));encode.append((time.perf_counter()-start)*1000)
        row_counts.append(counts(value));del value;gc.collect()
    return {'name':name,'best_ms':round(min(samples),3),'samples_ms':[round(n,3) for n in samples],
            'json_encode_best_ms':round(min(encode),3),'json_bytes':max(sizes),'returned_rows':row_counts[-1]}


def measure_delta_pages(store,token,expected):
    pages=[];current=token;observed={}
    while True:
        value=store.catalog_snapshot(current)
        assert 'products' not in value,'Incremental continuation unexpectedly returned the full catalog'
        changes=value.get('product_changes',[])
        assert len(changes)<=500,'Delta page exceeded 500 products'
        pages.append({'rows':len(changes),'json_bytes':json_bytes(value)})
        observed.update({p['id']:p['revision'] for p in changes});current=value['catalog_token']
        if not value.get('catalog_has_more'):break
        assert len(pages)<=20,'Delta pagination did not advance'
    assert observed==expected,'Delta pagination lost a product or its latest revision'
    return {'pages':pages,'product_changes':len(observed),'json_bytes_total':sum(p['json_bytes'] for p in pages)}


def measure_bootstrap_pages(store,expected_ids):
    samples=[];summary=None
    for _ in range(3):
        started=time.perf_counter();token=None;observed=set();pages=[];boot_count=0
        while True:
            value=store.catalog_snapshot(token,paged=True)
            if value.get('catalog_reset'):observed.clear()
            rows=value.get('products',value.get('product_changes',[]))
            assert len(rows)<=500,'Bootstrap response exceeded 500 products'
            if value.get('catalog_bootstrap'):boot_count+=1
            for pid in value.get('removed_product_ids',[]):observed.discard(pid)
            observed.update(p['id'] for p in rows)
            pages.append({'rows':len(rows),'json_bytes':json_bytes(value),'bootstrap':bool(value.get('catalog_bootstrap'))})
            token=value['catalog_token']
            if not value.get('catalog_has_more'):break
            assert len(pages)<100,'Bootstrap did not finish'
        assert observed==expected_ids,'Bootstrap omitted or duplicated the 10000-product catalog'
        assert boot_count==20 and len(pages)==21,'Bootstrap did not use 20 bounded pages and one delta handoff'
        samples.append((time.perf_counter()-started)*1000)
        summary={'name':'catalog_bootstrap_10000_500_per_page','returned_rows':len(observed),
                 'bootstrap_pages':boot_count,'requests':len(pages),'json_bytes':sum(p['json_bytes'] for p in pages),
                 'max_page_json_bytes':max(p['json_bytes'] for p in pages),'max_page_rows':max(p['rows'] for p in pages)}
        gc.collect()
    summary.update(best_ms=round(min(samples),3),samples_ms=[round(n,3) for n in samples],
                   timing_basis='Complete server traversal including JSON encoding; retains product IDs only, not a browser product cache')
    return summary


def update_products(store,ids,expected,label):
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        for i,pid in enumerate(ids):
            expected[pid]+=1
            store.update(pid,{'note':label+' '+str(i)},expected[pid]-1,connection=c)


def pagination_contracts(groups,batch,all_ids):
    results={}
    for name,call,field,size in [('catalog_groups',groups.state,'selectable_products',100),('batch_editor',batch.state,'rows',50)]:
        first=call();pages=first['product_pages'] if name=='catalog_groups' else first['pages'];seen=[]
        started=time.perf_counter()
        for index in range(pages):
            value=call(product_page=index) if name=='catalog_groups' else call(page=index)
            rows=value[field]
            assert len(rows)==min(size,len(all_ids)-index*size),name+' page boundary mismatch'
            seen.extend(row['id'] for row in rows)
        assert len(seen)==len(set(seen))==len(all_ids) and set(seen)==all_ids,name+' omitted or duplicated catalog products'
        results[name]={'pages':pages,'page_size':size,'rows_verified':len(seen),'scan_ms':round((time.perf_counter()-started)*1000,3)}
    return results


def run():
    # Modules are imported after the outgoing connector function has been guarded.
    from core import Store,ident,now
    from operations import Operations
    from finance import Finance
    from order_intake import OrderIntake
    from catalog_groups import CatalogGroups
    from batch_editor import BatchEditor
    from analytics import Analytics
    from procurement import Procurement
    from alerts import Alerts
    from fulfillment import Fulfillment
    from inventory_counts import InventoryCounts
    from pricing_plans import PricingPlans
    from shipping_manifests import ShippingManifests
    from ad_analytics import AdAnalytics
    with tempfile.TemporaryDirectory(prefix='noonai-capacity-') as temporary:
        started=time.perf_counter();store=Store(temporary);ops=Operations(store);finance=Finance(store)
        app=SimpleNamespace(store=store,ops=ops,finance=finance,write_lock=threading.RLock())
        intake=OrderIntake(app);groups=CatalogGroups(app);batch=BatchEditor(app);analytics=Analytics(app);procurement=Procurement(app);alerts=Alerts(app);fulfillment=Fulfillment(app)
        counts_service=InventoryCounts(app);pricing=PricingPlans(app);manifests=ShippingManifests(app);advertising=AdAnalytics(app)
        init_ms=(time.perf_counter()-started)*1000
        ids=[];started=time.perf_counter()
        for base in range(0,10000,500):
            result=store.import_rows([{'title_zh':f'合成容量商品 {i:05d}','source_sku':f'SYN-{i:05d}','supplier':'合成供应商','facts':'合成规格，容量测试专用','cost_cny':5.25,'stock':100,'brand':'Synthetic'} for i in range(base,base+500)])
            assert len(result['created'])==500 and not result['duplicates'];ids.extend(result['created'])
        product_seed_ms=(time.perf_counter()-started)*1000
        started=time.perf_counter()
        with store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            warehouse=ops.entity(c,{'kind':'warehouse','name':'合成测试仓库'})['id']
            shop=ops.entity(c,{'kind':'shop','name':'合成测试店铺'})['id']
            supplier=ops.entity(c,{'kind':'supplier','name':'合成测试供应商'})['id']
            for pid in ids:ops.adjust(c,{'product_id':pid,'warehouse_id':warehouse,'quantity':20,'direction':'in','reason':'合成容量测试期初'})
        inventory_seed_ms=(time.perf_counter()-started)*1000
        created=(datetime.now(timezone.utc)-timedelta(days=2)).isoformat();orders=[];purchases=[];started=time.perf_counter()
        with store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for i in range(1000):
                order=ops.order(c,{'shop_id':shop,'warehouse_id':warehouse,'external_id':f'SYN-ORDER-{i:04d}','currency':'SAR','lines':[{'product_id':ids[2*i+j],'quantity':2,'unit_price':'9.95'} for j in range(2)]})
                order['created_at']=created;ops.write(c,order);orders.append(order)
        order_seed_ms=(time.perf_counter()-started)*1000;started=time.perf_counter()
        with store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for i in range(200):
                purchases.append(ops.purchase(c,{'supplier_id':supplier,'warehouse_id':warehouse,'lines':[{'product_id':ids[2*i+j],'quantity':10,'unit_price':'5.25'} for j in range(2)]}))
        purchase_seed_ms=(time.perf_counter()-started)*1000;started=time.perf_counter()
        with store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for i in range(300):
                finance.create(c,{'kind':'income' if i<150 else 'expense','category':'sale' if i<150 else 'goods','currency':'SAR','amount':'19.90','fx':'1.9','date':created[:10],'due_date':created[:10],'document_id':orders[i]['id'],'evidence_key':f'SYN-FIN-{i:04d}','evidence':'合成容量测试凭证'})
        finance_seed_ms=(time.perf_counter()-started)*1000;started=time.perf_counter()
        # Actual confirmed duplicate intake builds durable history without adding orders.
        first=orders[0]
        rows=[{'external_id':first['external_id'],'partner_sku':l['sku'],'quantity':l['quantity'],'unit_price':'9.95','currency':'SAR'} for l in first['lines']]
        for _ in range(25):
            preview=intake.preview({'rows':rows,'shop_id':shop,'warehouse_id':warehouse})
            assert preview['duplicates']==1 and preview['can_apply']
            result=intake.apply({'token':preview['token'],'request_id':ident(),'confirmed':True});assert result['created']==0
        receipt_seed_ms=(time.perf_counter()-started)*1000
        # Seed the new modules only through their real local preview/confirmation services.
        # Unknown pricing parameters are deliberately retained as blocked draft facts.
        pricing_ids=[];started=time.perf_counter()
        for base in (0,500):
            body={'name':f'合成容量价格草稿 {base//500+1}','currency':'SAR','status':'draft',
                  'parameters':{'tax_basis':'unknown'},'members':[{'product_id':pid,'revision':1} for pid in ids[base:base+500]]}
            preview=pricing.preview(body)
            assert len(preview['rows'])==500 and not preview['precheck_passed']
            saved=pricing.save({**body,'preview_digest':preview['preview_digest'],'request_id':ident(),'confirmed':True})
            assert len(saved['members'])==500 and saved['status']=='draft' and not saved['sendable']
            pricing_ids.append(saved['id'])
        pricing_seed_ms=(time.perf_counter()-started)*1000
        started=time.perf_counter();shipped=[]
        with store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for index,order in enumerate(orders[:100]):
                reserved=ops.reserve(c,{'id':order['id'],'revision':order['revision']})
                dispatched=ops.ship(c,{'id':reserved['id'],'revision':reserved['revision'],'carrier':'合成承运商',
                                       'tracking':f'SYN-CAPACITY-TRACK-{index:04d}'})
                orders[index]=dispatched;shipped.append(dispatched)
        shipping_seed_ms=(time.perf_counter()-started)*1000
        started=time.perf_counter()
        packages=[{'order_id':order['id'],'carrier':order['carrier'],'tracking':order['tracking'],
                   'box_no':f'SYN-BOX-{index:04d}','weight_kg':'1.25','length_cm':'20','width_cm':'15','height_cm':'10'}
                  for index,order in enumerate(shipped)]
        manifest_preview=manifests.preview({'order_ids':[order['id'] for order in shipped],
                                            'packages':packages,'note':'合成容量测试包装测量，非真实发货'})
        assert manifest_preview['can_apply'] and not manifest_preview['missing'] and len(manifest_preview['packages'])==100
        manifest=manifests.apply({'token':manifest_preview['token'],'request_id':ident(),'confirmed':True})
        assert len(manifest['packages'])==100
        manifest_seed_ms=(time.perf_counter()-started)*1000
        started=time.perf_counter()
        with store.connect() as c:
            skus={row['id']:row['sku'] for row in c.execute("SELECT id,json_extract(data,'$.partner_sku') sku FROM products")}
        ad_rows=[{'date':created[:10],'campaign':f'SYN-AD-{index:04d}','channel':'合成只读广告报告','currency':'SAR',
                  'spend':'1.50','impressions':1000,'clicks':20,'orders':1,'attributed_sales':'12.00',
                  'sku':skus[ids[index]],'attribution_basis':'合成7天点击归因，非财务收入'} for index in range(1000)]
        ad_preview=advertising.preview({'shop_id':shop,'format':'json','text':json.dumps(ad_rows,ensure_ascii=False)})
        assert ad_preview['summary']['accepted']==1000 and ad_preview['summary']['errors']==0
        ad_receipt=advertising.apply({'batch_id':ad_preview['id'],'snapshot_fingerprint':ad_preview['snapshot_fingerprint'],
                                       'request_id':ident(),'confirmed':True})
        assert ad_receipt['imported']==1000 and ad_receipt['errors']==0
        advertising_seed_ms=(time.perf_counter()-started)*1000
        sys.stderr.write('Capacity fixture ready: 10000 SKU, 1000 orders (100 actually reserved/shipped), 2x500-SKU price drafts, 100-package manifest, 1000 confirmed ad rows; measuring states.\n')
        expected={pid:1 for pid in ids};metrics=[]
        metrics.append(measure('catalog_bootstrap_first_500',lambda:store.catalog_snapshot(paged=True),lambda v:len(v['products'])))
        metrics.append(measure_bootstrap_pages(store,set(ids)))
        bootstrap_current,bootstrap_peak=tracemalloc.get_traced_memory()
        metrics.append(measure('catalog_initial_10000',store.catalog_snapshot,lambda v:len(v.get('products',[]))))
        initial=store.catalog_snapshot();all_ids={p['id'] for p in initial['products']};assert all_ids==set(ids);token=initial['catalog_token'];del initial;gc.collect()
        metrics.append(measure('catalog_same_token',lambda:store.catalog_snapshot(token),lambda v:len(v.get('product_changes',v.get('products',[])))))
        update_products(store,ids[:1],expected,'single mutation')
        metrics.append(measure('catalog_single_change',lambda:store.catalog_snapshot(token),lambda v:len(v.get('product_changes',[]))))
        change=store.catalog_snapshot(token);assert {p['id']:p['revision'] for p in change['product_changes']}=={ids[0]:2};bulk_token=change['catalog_token'];del change
        update_products(store,ids[:1000],expected,'1000 product mutations');wanted={pid:expected[pid] for pid in ids[:1000]}
        delta_samples=[];delta_summary=None
        for _ in range(3):
            started=time.perf_counter();delta_summary=measure_delta_pages(store,bulk_token,wanted);delta_samples.append((time.perf_counter()-started)*1000)
        metrics.append({'name':'catalog_1000_changes_500_per_page','best_ms':round(min(delta_samples),3),'samples_ms':[round(n,3) for n in delta_samples],'returned_rows':delta_summary['product_changes'],'json_bytes':delta_summary['json_bytes_total'],'pages':delta_summary['pages']})
        metrics.extend([
            measure('catalog_groups_100_skus',groups.state,lambda v:len(v['selectable_products'])),
            measure('batch_editor_50_skus',batch.state,lambda v:len(v['rows'])),
            measure('order_intake_20_receipts',intake.state,lambda v:len(v['receipts'])),
            measure('analytics_50_skus',analytics.state,lambda v:len(v['sku_page']['items'])),
            measure('operations_orders_50',lambda:ops.state('orders'),lambda v:len(v['documents'])),
            measure('operations_purchases_50',lambda:ops.state('purchases'),lambda v:len(v['documents'])),
            measure('procurement_paged',procurement.state,lambda v:{k:len(v[k]['items']) for k in ('demand_page','purchase_page','links_page')}),
            measure('alerts_50',alerts.state,lambda v:len(v['rows'])),
            measure('fulfillment_50_orders',fulfillment.state,lambda v:len(v['orders'])),
            measure('inventory_counts_50_stock_skus',lambda:counts_service.state(warehouse_id=warehouse),lambda v:len(v['rows'])),
            measure('pricing_plans_compact_2x500_and_100_selectable_skus',pricing.state,lambda v:{'plans':len(v['plans']),'selectable_skus':len(v['selectable_products'])}),
            measure('pricing_plan_user_detail_500_skus',lambda:pricing.get(pricing_ids[0]),lambda v:len(v['members'])),
            measure('shipping_manifests_compact_100_package_manifest_and_50_orders',manifests.state,lambda v:{'manifests':len(v['manifests']),'orders':len(v['orders'])}),
            measure('shipping_manifest_user_detail_100_packages',lambda:manifests.get(manifest['id']),lambda v:len(v['packages'])),
            measure('ad_analytics_50_of_1000_records',advertising.state,lambda v:len(v['record_page']['items'])),
            measure('ad_batch_user_detail_50_of_1000_rows',lambda:advertising.state({'batch_id':ad_preview['id']}),lambda v:len(v['batch']['rows'])),
            measure('finance_full_state',finance.state,lambda v:{k:len(v[k]) for k in ('entries','payments','orders')})])
        contracts=pagination_contracts(groups,batch,all_ids)
        # Assert compact lists really omit potentially large 500-SKU and 100-package detail arrays.
        price_list=pricing.state();assert price_list['total']==2 and all(p['member_count']==500 and 'members' not in p for p in price_list['plans'])
        manifest_list=manifests.state();assert manifest_list['order_total']==100 and manifest_list['total']==1
        assert manifest_list['manifests'][0]['package_count']==100 and 'packages' not in manifest_list['manifests'][0]
        stock_list=counts_service.state(warehouse_id=warehouse);assert stock_list['total']==10000 and len(stock_list['rows'])==50
        assert all(row['on_hand'] is not None for row in stock_list['rows'])
        ad_list=advertising.state();assert ad_list['record_page']['total']==1000 and len(ad_list['record_page']['items'])==50
        ad_seen=set()
        for page in range(ad_list['record_page']['pages']):
            current_ads=advertising.state({'page':page})['record_page']['items'];assert len(current_ads)==50
            ad_seen.update(row['id'] for row in current_ads)
        assert len(ad_seen)==1000
        contracts['third_round_compact_states']={'inventory_sku_page':50,'inventory_total':10000,'pricing_plans':2,'pricing_members_each':500,
            'pricing_list_omits_members':True,'manifest_count':1,'manifest_packages':100,'manifest_list_omits_packages':True,
            'shipped_order_total':100,'shipping_order_page':50,'ad_record_pages':20,'ad_unique_rows_verified':len(ad_seen),
            'pricing_detail_explicit_get':500,'manifest_detail_explicit_get':100,'ad_batch_detail_page':50}
        # Add edits between delta pages, including a product outside the initial 1000.
        baseline=store.catalog_snapshot()['catalog_token'];update_products(store,ids[2000:3000],expected,'concurrent sequence A')
        first_page=store.catalog_snapshot(baseline);assert first_page['catalog_has_more'] and len(first_page['product_changes'])==500
        observed={p['id']:p['revision'] for p in first_page['product_changes']}
        injected=[first_page['product_changes'][0]['id'],ids[2999],ids[9999]]
        update_products(store,injected,expected,'between continuation pages')
        cursor=first_page['catalog_token'];page_count=1
        while True:
            value=store.catalog_snapshot(cursor);assert 'products' not in value and len(value.get('product_changes',[]))<=500
            observed.update({p['id']:p['revision'] for p in value.get('product_changes',[])});cursor=value['catalog_token'];page_count+=1
            if not value.get('catalog_has_more'):break
            assert page_count<20
        wanted={pid:expected[pid] for pid in set(ids[2000:3000]+injected)};assert observed==wanted,'Mutation between delta pages was lost'
        contracts['delta_changes_between_pages']={'unique_products_verified':len(wanted),'pages':page_count,'latest_revisions_verified':True}
        with store.connect() as c:
            counts={name:c.execute('SELECT count(*) FROM '+name).fetchone()[0] for name in ('products','ops_documents','ops_stock','finance_entries','order_intake_receipts','pricing_plans','shipping_manifests','shipping_manifest_orders','ad_records','ad_batches')}
        current,peak=tracemalloc.get_traced_memory();rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return {'schema_version':2,'synthetic_only':True,'clock':'perf_counter','repeats':3,'timing_basis':'Best of three local calls; JSON encoding reported separately except multi-page traversal, which includes encoding. tracemalloc is enabled throughout and adds overhead. No warm/cold cache or service-level guarantee.',
                'environment':{'python':platform.python_version(),'platform':platform.system(),'sqlite':__import__('sqlite3').sqlite_version},
                'dataset':{'products':10000,'orders':1000,'purchases':200,'finance_entries':300,'inventory_rows':10000,'intake_receipts':25,'shipped_orders':100,'pricing_drafts':2,'pricing_members_per_draft':500,'shipping_manifests':1,'manifest_packages':100,'ad_reports':1000,'ad_report_batches':1,'document_lines':2,'synthetic_order_age_days':2,'seed_db_counts':counts},
                'seed_ms':{'module_initialization':round(init_ms,3),'products':round(product_seed_ms,3),'inventory':round(inventory_seed_ms,3),'orders':round(order_seed_ms,3),'purchases':round(purchase_seed_ms,3),'finance':round(finance_seed_ms,3),'intake_receipts':round(receipt_seed_ms,3),'pricing_drafts':round(pricing_seed_ms,3),'reserve_and_ship_100_orders':round(shipping_seed_ms,3),'manifest_100_packages':round(manifest_seed_ms,3),'advertising_1000_reports':round(advertising_seed_ms,3)},
                'measurements':metrics,'contracts':contracts,
                'memory':{'bootstrap_phase_current_bytes':bootstrap_current,'bootstrap_phase_peak_bytes':bootstrap_peak,'bootstrap_phase_basis':'Python high-water mark through seeding and three paged server traversals, before any legacy full-catalog request; does not measure the browser cache.', 'tracemalloc_current_bytes':current,'tracemalloc_peak_bytes':peak,'ru_maxrss_raw':rss,'ru_maxrss_unit':'bytes' if platform.system()=='Darwin' else 'KiB','basis':'tracemalloc covers Python allocations from startup through seed and checks, excluding SQLite/native allocations; ru_maxrss is the process lifetime OS resident-memory high-water mark, not per-endpoint usage.'},
                'observed_design_limits':['Legacy default initial snapshot still returns all 10000 enriched products; opt-in paged bootstrap bounds every response at 500 and uses a delta handoff. Total product transfer still covers the full catalog before client rendering.','Finance.state still materializes all entries and all 1000 order contributions.','Paged responses bound transferred rows; their SQL counts/aggregate scans still depend on catalog/document size.','The 2 pricing plans each bind 500 SKU with unknown price/fee parameters and remain blocked local drafts; the manifest binds 100 actually reserved/shipped synthetic orders with one measured synthetic package each. Advertising covers 1000 matched-SKU campaign rows in one confirmed synthetic report batch, not financial income or actual ad spend.','Pricing and manifest list responses omit nested members/packages; state computations still hash current bound products/orders/stock and are measured separately from explicit user detail get.','Inventory counts state observes 10000 existing stock balances; this benchmark does not fabricate a physical count observation or apply stock counts.','This fixture has no images, platform history, real customer data, external API or model traffic.']}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,help='Write synthetic statistics JSON here; default stdout');args=parser.parse_args()
    started=time.perf_counter();tracemalloc.start()
    import connectors
    # Guard both the shared connector and low-level outgoing sockets, including imported aliases.
    with patch.dict(os.environ,sanitized_env(),clear=True),patch.object(connectors,'request_json',side_effect=AssertionError('External requests forbidden during capacity benchmark')) as external,patch.object(socket.socket,'connect',side_effect=AssertionError('Network forbidden during capacity benchmark')) as network:
        result=run();result['external_connector_calls']=external.call_count;result['network_connect_calls']=network.call_count
        assert external.call_count==network.call_count==0
    result['elapsed_seconds']=round(time.perf_counter()-started,3);tracemalloc.stop();encoded=json.dumps(result,ensure_ascii=False,indent=2)+'\n'
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(encoded,encoding='utf-8')
    else:sys.stdout.write(encoded)

if __name__=='__main__':main()
