import concurrent.futures
import copy
import csv
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Store, Problem, ident, now
from operations import Operations
from finance import Finance
from after_sales import AfterSales


class AfterSalesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name);self.ops=Operations(self.store);self.finance=Finance(self.store)
        self.service=AfterSales(SimpleNamespace(store=self.store,ops=self.ops,finance=self.finance))
        self.ids=self.store.import_rows([{'title_zh':'退货商品甲'},{'title_zh':'退货商品乙'}])['created']
        self.shop=self.op('entity',kind='shop',name='测试店')['id'];self.warehouse=self.op('entity',kind='warehouse',name='测试仓')['id']
        for pid in self.ids:self.op('adjust',product_id=pid,warehouse_id=self.warehouse,direction='in',quantity=10,reason='测试库存')
        d=self.op('order',shop_id=self.shop,warehouse_id=self.warehouse,external_id=ident(),currency='SAR',lines=[{'product_id':pid,'quantity':2,'unit_price':'10.00'} for pid in self.ids])
        d=self.op('reserve',id=d['id'],revision=d['revision']);self.order=self.op('ship',id=d['id'],revision=d['revision'],carrier='测试',tracking='TEST')

    def op(self,action,**body):return self.ops.transact(action,{'request_id':ident(),**body})
    def current_order(self):
        with self.store.connect() as c:return json.loads(c.execute('SELECT data FROM ops_documents WHERE id=?',(self.order['id'],)).fetchone()['data'])
    def current_case(self,cid):return next(c for c in self.service.state()['rows'] if c['id']==cid)
    def create_body(self,disposition='quarantine',quantities=None):
        return {'action':'create','order_id':self.order['id'],'order_revision':self.current_order()['revision'],'reason':'破损退货','quantities':quantities or {pid:1 for pid in self.ids},'return_disposition':disposition}
    def confirmed(self,body):return {**body,'request_id':ident(),'confirmed':True,'preview_digest':self.service.preview(body)['preview_digest']}
    def call(self,body):return getattr(self.service,body['action'])(self.confirmed(body))
    def case_body(self,action,case,**extra):
        case=self.current_case(case['id']);return {'action':action,'id':case['id'],'revision':case['revision'],'order_revision':self.current_order()['revision'],**extra}
    def register(self,disposition='quarantine',quantities=None):return self.call(self.create_body(disposition,quantities))
    def receive(self,case):return self.call(self.case_body('receive',case,evidence='仓库人工验收'))
    def financial_body(self,case,amount='10.00',category='refund',**extra):
        return self.case_body('refund',case,amount=amount,category=category,currency='SAR',fx='1.90',date=now()[:10],evidence='退款审核凭证',evidence_key=ident(),reason='经核对退款' if category=='refund' else '运费赔付',**extra)
    def stock(self):
        with self.store.connect() as c:return {r['product_id']:r['on_hand'] for r in c.execute('SELECT * FROM ops_stock')}
    def counts(self):
        with self.store.connect() as c:return [c.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('after_sales_cases','after_sales_quarantine','after_sales_requests','after_sales_audit','finance_entries','finance_payments')]

    def test_registration_does_not_return_or_change_stock(self):
        stock=self.stock();before=self.current_order();case=self.register()
        self.assertEqual(case['status'],'registered');self.assertEqual(self.stock(),stock);self.assertEqual(self.current_order(),before)
        self.assertEqual(case['quarantine'],[]);self.assertFalse(case['platform_refund_sent'])
        self.assertEqual(self.service.state()['orders'][0]['after_sales_available'],{pid:1 for pid in self.ids})

    def test_restock_receive_replays_without_stock_or_returns_duplication(self):
        case=self.register('restock');body=self.confirmed(self.case_body('receive',case,evidence='商品完好'))
        received=self.service.receive(body);self.assertEqual(self.service.receive(body),received)
        self.assertEqual(self.stock(),{pid:9 for pid in self.ids});self.assertEqual([l['returned'] for l in self.current_order()['lines']],[1,1]);self.assertEqual(len(self.current_order()['returns']),1)
        self.assertEqual(received['quarantine'],[])
        with self.assertRaises(Problem):self.service.receive({**body,'request_id':ident()})

    def test_quarantine_partial_release_and_discard_conserve_stock(self):
        case=self.receive(self.register());self.assertEqual(self.stock(),{pid:8 for pid in self.ids})
        self.assertEqual(sum(r['remaining'] for r in case['quarantine']),2)
        body=self.confirmed(self.case_body('dispose',case,disposition='release',quantities={self.ids[0]:1},evidence='已清洁且商品完好'))
        case=self.service.dispose(body);self.assertEqual(self.service.dispose(body),case)
        self.assertEqual(self.stock(),{self.ids[0]:9,self.ids[1]:8})
        case=self.call(self.case_body('dispose',case,disposition='discard',quantities={self.ids[1]:1},evidence='破损报废'))
        self.assertEqual(sum(q['remaining'] for q in case['quarantine']),0)
        self.assertEqual(sum(q['received'] for q in case['quarantine']),sum(q['released']+q['discarded'] for q in case['quarantine']))
        self.assertEqual([l['returned'] for l in self.current_order()['lines']],[1,1]);self.assertEqual(len(self.current_order()['returns']),1)
        with self.assertRaises(Problem):self.service.preview(self.case_body('dispose',case,disposition='release',quantities={self.ids[0]:1},evidence='重复释放'))

    def test_direct_discard_updates_return_counts_without_available_stock(self):
        case=self.receive(self.register('discard'));self.assertEqual(self.stock(),{pid:8 for pid in self.ids})
        self.assertEqual(case['quarantine'],[]);self.assertEqual([l['returned'] for l in self.current_order()['lines']],[1,1])
        self.assertFalse(self.current_order()['returns'][0]['restock'])

    def test_manual_returns_reduce_remaining_registration_capacity(self):
        d=self.current_order();self.op('return',id=d['id'],revision=d['revision'],quantities={self.ids[0]:2},reason='既有手工退货',restock=False)
        with self.assertRaises(Problem):self.service.preview(self.create_body(quantities={self.ids[0]:1}))
        case=self.register(quantities={self.ids[1]:2});self.receive(case)
        self.assertEqual([l['returned'] for l in self.current_order()['lines']],[2,2])

    def test_manual_return_after_registration_blocks_stale_receive_and_can_cancel(self):
        case=self.register(quantities={self.ids[0]:2});body=self.confirmed(self.case_body('receive',case,evidence='预览收货'))
        d=self.current_order();self.op('return',id=d['id'],revision=d['revision'],quantities={self.ids[0]:1},reason='手工已退',restock=False)
        with self.assertRaises(Problem):self.service.receive(body)
        with self.assertRaises(Problem):self.service.preview(self.case_body('receive',case,evidence='重新收货'))
        case=self.call(self.case_body('dispose',case,disposition='cancel',evidence='已经手工登记一件，需要重建数量'))
        self.assertEqual(case['status'],'cancelled');self.assertEqual(self.service.state()['orders'][0]['after_sales_available'][self.ids[0]],1)

    def test_outstanding_cases_cannot_overbook_and_receive_updates_each_once(self):
        first=self.register(quantities={self.ids[0]:1});second=self.register(quantities={self.ids[0]:1})
        with self.assertRaises(Problem):self.service.preview(self.create_body(quantities={self.ids[0]:1}))
        self.receive(first);self.receive(second)
        self.assertEqual(self.current_order()['lines'][0]['returned'],2);self.assertEqual(len(self.current_order()['returns']),2)

    def test_receive_transaction_rollback_after_operations_return(self):
        case=self.register();before=self.counts();order=self.current_order();stock=self.stock();body=self.confirmed(self.case_body('receive',case,evidence='验收凭证'))
        with patch.object(self.service,'write',side_effect=RuntimeError('write failure')):
            with self.assertRaises(RuntimeError):self.service.receive(body)
        self.assertEqual(self.counts(),before);self.assertEqual(self.current_order(),order);self.assertEqual(self.stock(),stock)
        self.assertEqual(self.service.receive(body)['status'],'received')

    def test_dispose_rollback_reverts_movement_and_quarantine(self):
        case=self.receive(self.register());stock=self.stock();body=self.confirmed(self.case_body('dispose',case,disposition='release',quantities={self.ids[0]:1},evidence='完好'))
        with patch.object(self.store,'event',side_effect=RuntimeError('audit unavailable')):
            with self.assertRaises(RuntimeError):self.service.dispose(body)
        self.assertEqual(self.stock(),stock);self.assertEqual(sum(q['remaining'] for q in self.current_case(case['id'])['quarantine']),2)
        self.service.dispose(body);self.assertEqual(self.stock()[self.ids[0]],9)

    def test_financial_entry_uses_original_currency_and_does_not_record_payment(self):
        case=self.register();body=self.confirmed(self.financial_body(case,amount='10.50'));case=self.service.refund(body)
        self.assertEqual(self.service.refund(body),case)
        entry=self.finance.state()['entries'][0];self.assertEqual((entry['kind'],entry['category'],entry['currency'],entry['amount_cents'],entry['base_cents']),('expense','refund','SAR',1050,1995))
        self.assertEqual(entry['allocations'],[{'order_id':self.order['id'],'amount_cents':1050}]);self.assertEqual(self.finance.state()['payments'],[])
        self.assertEqual(case['money']['refund_cents'],1050);self.assertEqual(self.counts()[-2:],[1,0])

    def test_manual_finance_refund_counts_towards_cap_and_void_frees_amount(self):
        case=self.register();manual=self.finance.transact('entry',{'request_id':ident(),'kind':'expense','category':'refund','currency':'SAR','amount':'35.00','fx':'1.9','date':now()[:10],'document_id':self.order['id'],'evidence':'手工退款','evidence_key':ident()})
        self.assertEqual(self.current_case(case['id'])['money']['remaining_cents'],500)
        with self.assertRaises(Problem):self.service.preview(self.financial_body(case,'6.00'))
        case=self.call(self.financial_body(case,'5.00'));self.assertEqual(case['money']['refund_cents'],4000)
        self.finance.transact('void_entry',{'request_id':ident(),'id':manual['id'],'revision':manual['revision'],'reason':'手工退款未发生'})
        self.assertEqual(self.current_case(case['id'])['money']['remaining_cents'],3500)

    def test_manually_allocated_refund_is_also_counted(self):
        case=self.register();entry=self.finance.transact('entry',{'request_id':ident(),'kind':'expense','category':'refund','currency':'SAR','amount':'50.00','fx':'1.9','date':now()[:10],'evidence':'多订单退款','evidence_key':ident()})
        self.finance.transact('allocate',{'request_id':ident(),'id':entry['id'],'revision':1,'allocations':[{'order_id':self.order['id'],'amount':'30.00'}]})
        self.assertEqual(self.current_case(case['id'])['money']['refund_cents'],3000)
        with self.assertRaises(Problem):self.service.preview(self.financial_body(case,'10.01'))

    def test_compensation_and_refund_share_cap_and_require_reason(self):
        case=self.register();case=self.call(self.financial_body(case,'30.00','refund'));case=self.call(self.financial_body(case,'10.00','other'))
        self.assertEqual((case['money']['refund_cents'],case['money']['compensation_cents'],case['money']['remaining_cents']),(3000,1000,0))
        with self.assertRaises(Problem):self.service.preview(self.financial_body(case,'0.01','other'))
        fresh=self.register(quantities={self.ids[0]:1})
        with self.assertRaises(Problem):self.service.preview({**self.financial_body(fresh,'1.00','other'),'reason':''})

    def test_cross_currency_refunds_hold_cap_instead_of_guessing_conversion(self):
        case=self.register();self.finance.transact('entry',{'request_id':ident(),'kind':'expense','category':'refund','currency':'USD','amount':'2.00','fx':'7','date':now()[:10],'document_id':self.order['id'],'evidence':'跨币待核对','evidence_key':ident()})
        self.assertEqual(len(self.current_case(case['id'])['money']['unreconciled_currency_entries']),1)
        with self.assertRaises(Problem):self.service.preview(self.financial_body(case,'1.00'))
        with self.assertRaises(Problem):self.service.preview({**self.financial_body(case,'1.00'),'currency':'USD'})

    def test_financial_preview_invalidated_by_other_manual_expense_and_case_versions(self):
        case=self.register();body=self.confirmed(self.financial_body(case,'10.00'))
        self.finance.transact('entry',{'request_id':ident(),'kind':'expense','category':'refund','currency':'SAR','amount':'1.00','fx':'1.9','date':now()[:10],'document_id':self.order['id'],'evidence':'新增手工退款','evidence_key':ident()})
        with self.assertRaises(Problem):self.service.refund(body)
        body=self.confirmed(self.financial_body(case,'10.00'));self.receive(case)
        with self.assertRaises(Problem):self.service.refund(body)

    def test_finance_create_rollback_prevents_double_count(self):
        case=self.register();body=self.confirmed(self.financial_body(case));before=self.counts()
        with patch.object(self.store,'event',side_effect=RuntimeError('audit fail')):
            with self.assertRaises(RuntimeError):self.service.refund(body)
        self.assertEqual(self.counts(),before);self.assertEqual(self.current_case(case['id'])['money']['refund_cents'],0)
        self.service.refund(body);self.assertEqual(self.counts()[-2:],[1,0])

    def test_concurrent_receive_replay_commits_once(self):
        case=self.register('restock');body=self.confirmed(self.case_body('receive',case,evidence='实际收货'))
        with concurrent.futures.ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:self.service.receive(body),range(2)))
        self.assertEqual(results[0],results[1]);self.assertEqual(self.stock(),{pid:9 for pid in self.ids});self.assertEqual(len(self.current_order()['returns']),1)

    def test_concurrent_case_registration_and_refunds_revalidate_live_capacity(self):
        body=self.create_body(quantities={self.ids[0]:2});bodies=[self.confirmed(body),self.confirmed(body)]
        def execute(action,b):
            try:return getattr(self.service,action)(b)
            except Problem as e:return e
        with concurrent.futures.ThreadPoolExecutor(2) as pool:out=list(pool.map(lambda b:execute('create',b),bodies))
        self.assertEqual(sum(isinstance(r,Problem) for r in out),1);case=next(r for r in out if not isinstance(r,Problem))
        bodies=[self.confirmed(self.financial_body(case,'25.00')) for _ in range(2)]
        with concurrent.futures.ThreadPoolExecutor(2) as pool:out=list(pool.map(lambda b:execute('refund',b),bodies))
        self.assertEqual(sum(isinstance(r,Problem) for r in out),1);self.assertEqual(self.current_case(case['id'])['money']['refund_cents'],2500)

    def test_evidence_and_request_id_conflicts(self):
        case=self.register();body=self.confirmed(self.financial_body(case));self.service.refund(body)
        with self.assertRaises(Problem):self.service.refund({**body,'amount':'11.00'})
        with self.assertRaises(Problem):self.service.preview({**self.financial_body(case),'evidence_key':body['evidence_key']})
        with self.assertRaises(Problem):self.service.receive({'request_id':ident(),'confirmed':False})
        with self.assertRaises(Problem):self.service.create({**self.confirmed(self.create_body(quantities={self.ids[1]:1})),'preview_digest':''})

    def test_validation_future_date_fractional_quantities_and_cancelled_finance(self):
        for extra in ({'return_disposition':'invalid'},{'quantities':{self.ids[0]:'0.5'}},{'quantities':{self.ids[0]:True}},{'quantities':{self.ids[0]:0}},{'quantities':{'unknown':1}},{'order_revision':True}):
            with self.subTest(extra=extra),self.assertRaises(Problem):self.service.preview({**self.create_body(),**extra})
        case=self.register();case=self.call(self.case_body('dispose',case,disposition='cancel',evidence='取消申请'))
        with self.assertRaises(Problem):self.service.preview(self.financial_body(case))
        fresh=self.register()
        with self.assertRaises(Problem):self.service.preview({**self.financial_body(fresh),'date':'2999-01-01'})

    def test_payment_settles_financial_obligation_without_double_counting_refund(self):
        case=self.call(self.financial_body(self.register(),'10.00'))
        entry=self.finance.state()['entries'][0]
        self.finance.transact('payment',{'request_id':ident(),'id':entry['id'],'revision':entry['revision'],'amount':'10.00','fx':'1.95','date':now()[:10],'evidence':'真实支付后手工登记测试凭证','evidence_key':ident(),'account':'测试账户'})
        money=self.current_case(case['id'])['money'];self.assertEqual(money['refund_cents'],1000);self.assertEqual(money['remaining_cents'],3000)
        self.assertEqual(len(self.finance.state()['payments']),1)

    def test_long_registration_reason_remains_traceable_during_receipt(self):
        body=self.create_body();body['reason']='因'*500
        case=self.receive(self.call(body));self.assertEqual(case['reason'],'因'*500)
        self.assertIn(case['id'],self.current_order()['returns'][0]['reason'])

    def test_order_selection_is_independently_paged_and_excludes_unshipped(self):
        self.op('adjust',product_id=self.ids[0],warehouse_id=self.warehouse,direction='in',quantity=60,reason='分页订单测试库存')
        for i in range(51):
            d=self.op('order',shop_id=self.shop,warehouse_id=self.warehouse,external_id='分页已发'+str(i),currency='SAR',lines=[{'product_id':self.ids[0],'quantity':1,'unit_price':'1.00'}])
            d=self.op('reserve',id=d['id'],revision=d['revision']);self.op('ship',id=d['id'],revision=d['revision'],carrier='测试',tracking=str(i))
        self.op('order',shop_id=self.shop,warehouse_id=self.warehouse,external_id='尚未发货',currency='SAR',lines=[{'product_id':self.ids[0],'quantity':1,'unit_price':'1.00'}])
        first=self.service.state();second=self.service.state(order_page=1)
        self.assertEqual((first['order_total'],len(first['orders']),len(second['orders'])),(52,50,2))
        self.assertFalse({o['id'] for o in first['orders']} & {o['id'] for o in second['orders']})
        self.assertEqual(self.service.state(query='尚未发货')['order_total'],0)
        self.assertEqual(self.service.state(query='分页已发')['order_total'],51)
        with self.assertRaises(Problem):self.service.state(order_page=True)

    def test_case_pagination_search_and_csv_formula_safety(self):
        # Cancelled registrations exercise pagination without consuming real return counts.
        for i in range(26):
            body=self.create_body(quantities={self.ids[0]:1});body['reason']='=危险公式' if i==0 else '分页售后'+str(i)
            case=self.call(body);self.call(self.case_body('dispose',case,disposition='cancel',evidence='测试取消'))
        first=self.service.state();second=self.service.state(page=1)
        self.assertEqual((first['total'],len(first['rows']),len(second['rows'])),(26,25,1));self.assertEqual(self.service.state(query='%')['total'],0)
        self.assertEqual(self.service.state(query='分页售后')['total'],25)
        rows=list(csv.reader(io.StringIO(self.service.export().decode('utf-8-sig'))));self.assertEqual(len(rows),27);self.assertIn("'=危险公式",[r[4] for r in rows[1:]])

if __name__=='__main__':unittest.main()
