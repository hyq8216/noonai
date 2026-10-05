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
from core import Store,Problem,ident,now
from pricing_plans import PricingPlans

class PricingPlansTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name);self.service=PricingPlans(SimpleNamespace(store=self.store))
        self.ids=self.store.import_rows([{'title_zh':'定价测试商品','cost_cny':19},{'title_zh':'未知成本商品'}])['created']
    def body(self,**extra):
        return {'name':'本地SAR建议价','currency':'SAR','status':'draft','parameters':{'fx':'1.9','fx_date':now()[:10],'fx_evidence':'合成日期汇率依据','platform_fee_rate':'0.10','logistics':'2','packing':'1','advertising':'1','tax_basis':'inclusive','tax_rate':'0.15','max_drop_rate':'0.20'},'members':[{'product_id':self.ids[0],'revision':self.store.get(self.ids[0])['revision'],'target_price':'25.00','cost_cny':'19.00','cost_evidence':'采购成本凭证','min_price':'20.00','max_price':'30.00','reference_price':'25.00'}],**extra}
    def confirmed(self,body):return {**body,'confirmed':True,'request_id':ident(),'preview_digest':self.service.preview(body)['preview_digest']}
    def save(self,body):return self.service.save(self.confirmed(body))
    def counts(self):
        with self.store.connect() as c:return [c.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('pricing_plans','pricing_plan_requests','pricing_plan_audit')]
    def facts(self):
        with self.store.connect() as c:return [tuple(r) for r in c.execute('SELECT * FROM products ORDER BY id')]
    def test_inclusive_tax_contribution_and_upward_break_even(self):
        p=self.service.preview(self.body());r=p['rows'][0]['calculation']
        self.assertEqual(r['net_revenue'],'21.74');self.assertEqual(r['platform_fee'],'2.50');self.assertEqual(r['cost_in_currency'],'10.00');self.assertEqual(r['fixed_cost'],'14.00');self.assertEqual(r['contribution'],'5.24');self.assertEqual(r['break_even_price'],'18.20');self.assertEqual(r['protection_floor'],'20.00');self.assertTrue(p['precheck_passed']);self.assertFalse(p['sendable'])
    def test_exclusive_tax_fee_is_applied_to_buyer_tax_included_total(self):
        body=self.body();body['parameters']['tax_basis']='exclusive';r=self.service.preview(body)['rows'][0]['calculation']
        self.assertEqual(r['net_revenue'],'25.00');self.assertEqual(r['customer_total'],'28.75');self.assertEqual(r['platform_fee'],'2.88');self.assertEqual(r['contribution'],'8.13');self.assertEqual(r['break_even_price'],'15.82')
    def test_missing_cost_fx_and_other_unknowns_are_not_zeros(self):
        for field,where in [('cost_cny','member'),('fx','parameters'),('logistics','parameters'),('tax_rate','parameters')]:
            body=self.body();target=body['members'][0] if where=='member' else body['parameters'];target[field]=''
            preview=self.service.preview(body);self.assertIsNone(preview['rows'][0]['calculation']['contribution']);self.assertFalse(preview['precheck_passed'])
            plan=self.save(body);self.assertEqual(plan['status'],'draft');self.assertFalse(plan['precheck_current'])
            with self.assertRaises(Problem):self.service.preview({**body,'status':'checked'})
    def test_explicit_zero_cost_requires_evidence_and_is_not_missing(self):
        body=self.body();body['members'][0]['cost_cny']='0';body['members'][0]['cost_evidence']='免费样品依据';p=self.service.preview(body);self.assertTrue(p['precheck_passed']);self.assertEqual(p['rows'][0]['calculation']['cost_in_currency'],'0.00')
        body['members'][0]['cost_evidence']='';self.assertFalse(self.service.preview(body)['precheck_passed'])
    def test_negative_contribution_protection_and_limits_remain_draft(self):
        bodies=[]
        b=self.body();b['members'][0]['target_price']='10.00';bodies.append((b,'预计贡献为负'))
        b=self.body();b['members'][0]['target_price']='31.00';bodies.append((b,'目标价高于最高保护价'))
        b=self.body();b['members'][0]['reference_price']='40.00';bodies.append((b,'目标价触发跌价保护'))
        for body,blocker in bodies:
            with self.subTest(blocker=blocker):
                self.assertIn(blocker,self.service.preview(body)['rows'][0]['calculation']['blockers']);self.assertEqual(self.save(body)['status'],'draft')
                with self.assertRaises(Problem):self.service.preview({**body,'status':'checked'})
    def test_no_finite_break_even_with_tax_and_fee_consuming_revenue(self):
        body=self.body();body['parameters']['platform_fee_rate']='0.9';body['parameters']['tax_rate']='0.5';r=self.service.preview(body)['rows'][0]['calculation'];self.assertIsNone(r['break_even_price']);self.assertIn('平台费与税费口径下无有限保本价',r['blockers'])
    def test_exact_decimal_negative_contribution_is_not_hidden_by_rounding(self):
        b=self.body();b['parameters'].update(fx='1',platform_fee_rate='0.0001',logistics='0',packing='0',advertising='0',tax_rate='0');b['members'][0].update(cost_cny='25',target_price='25')
        r=self.service.preview(b)['rows'][0]['calculation'];self.assertIn('预计贡献为负',r['blockers']);self.assertEqual(r['contribution'],'-0.00');self.assertEqual(r['break_even_price'],'25.01')
    def test_save_update_cancel_and_product_facts_approvals_unchanged(self):
        facts=self.facts();p=self.save(self.body(status='checked'));self.assertEqual(p['revision'],1);self.assertTrue(p['precheck_current']);self.assertFalse(p['sendable'])
        b=self.body(id=p['id'],revision=1,name='改方案');p=self.save(b);self.assertEqual(p['revision'],2)
        cancel={'id':p['id'],'revision':2,'reason':'审核后撤销','confirmed':True,'request_id':ident()};p=self.service.cancel(cancel);self.assertEqual(p['revision'],3);self.assertEqual(p['status'],'cancelled');self.assertFalse(p['precheck_current']);self.assertEqual(self.service.cancel(cancel),p)
        self.assertEqual(facts,self.facts());self.assertEqual(self.counts(),[1,3,3])
        with self.assertRaises(Problem):self.service.preview(self.body(id=p['id'],revision=3))
    def test_preview_token_is_persistent_across_service_restart(self):
        b=self.confirmed(self.body());restarted=PricingPlans(SimpleNamespace(store=self.store));self.assertEqual(restarted.save(b)['revision'],1)
    def test_product_edit_invalidates_preview_and_saved_precheck(self):
        plan=self.save(self.body(status='checked'));b=self.confirmed(self.body(id=plan['id'],revision=1))
        p=self.store.get(self.ids[0]);self.store.update(p['id'],{**p,'facts':'新增成本事实'},p['revision'])
        loaded=self.service.state()['rows'][0];self.assertTrue(loaded['review_required']);self.assertFalse(loaded['precheck_current'])
        with self.assertRaises(Problem):self.service.save(b)
        fresh=self.body(id=plan['id'],revision=1,status='checked');self.assertTrue(self.save(fresh)['precheck_current'])
    def test_same_revision_identity_and_full_facts_change_invalidates_confirmation(self):
        b=self.confirmed(self.body())
        with self.store.connect() as c:
            row=c.execute('SELECT data FROM products WHERE id=?',(self.ids[0],)).fetchone();d=json.loads(row['data']);d['platform']={'pricing_readback':{'new':'observation'}};c.execute('UPDATE products SET data=?,source_key=? WHERE id=?',(json.dumps(d),'changed-identity',self.ids[0]))
        with self.assertRaises(Problem):self.service.save(b)
        self.assertEqual(self.counts(),[0,0,0])
    def test_any_parameters_or_prices_changed_after_preview_fail(self):
        b=self.confirmed(self.body())
        changes=[copy.deepcopy(b) for _ in range(3)];changes[0]['parameters']['fx']='2';changes[1]['members'][0]['target_price']='26';changes[2]['parameters']['fx_evidence']='另一个依据'
        for changed in changes:
            with self.assertRaises(Problem):self.service.save(changed)
        self.assertEqual(self.counts(),[0,0,0])
    def test_request_replay_conflict_and_concurrent_confirmation(self):
        b=self.confirmed(self.body())
        with concurrent.futures.ThreadPoolExecutor(2) as pool:out=list(pool.map(lambda _:self.service.save(b),range(2)))
        self.assertEqual(out[0],out[1]);self.assertEqual(self.counts(),[1,1,1])
        with self.assertRaises(Problem):self.service.save({**b,'name':'不同操作'})
    def test_rollback_after_audit_failure_keeps_plan_version_and_request_clean(self):
        p=self.save(self.body());b=self.confirmed(self.body(id=p['id'],revision=1,name='待回滚更新'));before=self.counts()
        with patch.object(self.store,'event',side_effect=RuntimeError('audit fail')):
            with self.assertRaises(RuntimeError):self.service.save(b)
        self.assertEqual(before,self.counts());self.assertEqual(self.service.state()['rows'][0]['revision'],1);self.assertEqual(self.service.save(b)['revision'],2)
    def test_decimal_currency_rate_precision_date_and_boolean_guards(self):
        for mutate in [lambda b:b.update(currency='EGP'),lambda b:b.update(currency='CNY'),lambda b:b['parameters'].update(fx='0'),lambda b:b['parameters'].update(fx=True),lambda b:b['parameters'].update(tax_rate='1'),lambda b:b['parameters'].update(platform_fee_rate='NaN'),lambda b:b['parameters'].update(fx_date='2999-01-01'),lambda b:b['members'][0].update(target_price='25.001'),lambda b:b['members'][0].update(min_price='31'),lambda b:b['members'][0].update(revision=True)]:
            b=self.body();mutate(b)
            with self.assertRaises(Problem):self.service.preview(b)
        b=self.body(currency='CNY');b['parameters']['fx']='1';self.assertEqual(self.service.preview(b)['plan']['currency'],'CNY')
    def test_maximum_members_and_duplicate_ids(self):
        ids=self.store.import_rows([{'title_zh':'批量定价'+str(i)} for i in range(500)])['created'];base=self.body();m=base['members'][0]
        base['members']=[{**m,'product_id':pid,'revision':1} for pid in ids];self.assertEqual(len(self.service.preview(base)['rows']),500)
        base['members'].append(m)
        with self.assertRaises(Problem):self.service.preview(base)
        b=self.body();b['members'].append(copy.deepcopy(b['members'][0]))
        with self.assertRaises(Problem):self.service.preview(b)
    def test_bounded_product_and_plan_pagination_literal_search(self):
        self.store.import_rows([{'title_zh':'商品分页'+str(i)} for i in range(101)])
        first=self.service.state();second=self.service.state(product_page=1);self.assertEqual((len(first['products']),len(second['products']),first['product_total']),(100,3,103));self.assertFalse({p['id'] for p in first['products']} & {p['id'] for p in second['products']})
        for i in range(51):self.save(self.body(name='方案分页'+str(i)))
        first=self.service.state();second=self.service.state(page=1);self.assertEqual((first['total'],len(first['rows']),len(second['rows'])),(51,50,1));self.assertEqual(self.service.state(query='%')['total'],0)
        with self.assertRaises(Problem):self.service.state(product_page=True)
    def test_csv_bom_formula_safety_unknowns_and_current_review_state(self):
        b=self.body(name='=危险公式');b['members'][0]['cost_cny']='';b['members'][0]['cost_evidence']='@成本依据';self.save(b)
        raw=self.service.export();self.assertTrue(raw.startswith(b'\xef\xbb\xbf'));rows=list(csv.reader(io.StringIO(raw.decode('utf-8-sig'))));self.assertEqual(rows[1][1],"'=危险公式");self.assertEqual(rows[1][10],"'@成本依据");self.assertEqual(rows[1][24],'');self.assertIn('参考采购成本待确认',rows[1][26])
    def test_two_large_plans_state_has_only_compact_summaries_and_get_is_scoped(self):
        ids=self.store.import_rows([{'title_zh':'容量商品'+str(i)} for i in range(500)])['created']
        body=self.body();member=body['members'][0]
        body['members']=[{**member,'product_id':pid,'revision':1,'cost_evidence':'成本凭证长文本'*150} for pid in ids]
        first=self.save(body);second=self.save({**body,'name':'第二个500SKU方案'})
        with patch.object(self.service,'snapshot',wraps=self.service.snapshot) as hashes:
            listing=self.service.state()
            self.assertEqual(hashes.call_count,500,'shared product facts must be hashed once per summary page')
        self.assertEqual(listing['total'],2);self.assertEqual(len(listing['products']),100)
        for summary in listing['rows']:
            self.assertEqual(summary['member_count'],500);self.assertEqual(summary['blocked_count'],0);self.assertEqual(summary['complete_count'],500)
            for key in ('members','parameters','facts_digest','preview_digest','changed_product_ids'):self.assertNotIn(key,summary)
        self.assertLess(len(json.dumps(listing,ensure_ascii=False).encode()),100000,'periodic summary refresh must not return 1000 SKU evidence records')
        detail=self.service.get(first['id']);self.assertEqual(detail['id'],first['id']);self.assertEqual(len(detail['members']),500);self.assertIn('parameters',detail)
        self.assertNotIn(second['id'],json.dumps(detail,ensure_ascii=False))
        product=self.store.get(ids[0]);self.store.update(ids[0],{**product,'facts':'改动后的商品事实'},product['revision'])
        updated=self.service.state()
        for summary in updated['rows']:
            self.assertTrue(summary['review_required']);self.assertEqual(summary['changed_product_count'],1);self.assertFalse(summary['precheck_current'])
        self.assertEqual(self.service.get(first['id'])['members'][0]['current_revision'],2)

    def test_get_rejects_missing_plan_and_summary_revision_still_guards_cancel(self):
        with self.assertRaises(Problem) as error:self.service.get('not-a-plan')
        self.assertEqual(error.exception.status,404)
        plan=self.save(self.body());summary=self.service.state()['rows'][0]
        self.save(self.body(id=plan['id'],revision=1,name='新版本'))
        with self.assertRaises(Problem):self.service.cancel({'id':plan['id'],'revision':summary['revision'],'reason':'旧列表的取消','confirmed':True,'request_id':ident()})
        self.assertEqual(self.service.get(plan['id'])['revision'],2)

    def test_human_confirmation_and_durable_preview_required(self):
        b=self.confirmed(self.body())
        for change in ({'confirmed':False},{'request_id':'bad id'},{'preview_digest':'invalid'}):
            with self.assertRaises(Problem):self.service.save({**b,**change})
        self.assertEqual(self.counts(),[0,0,0])

if __name__=='__main__':unittest.main()
