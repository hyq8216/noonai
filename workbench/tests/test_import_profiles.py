import csv
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from core import Store, Problem, ident
from operations import Operations
from finance import Finance
from import_profiles import ImportProfiles, TYPES
from bank_reconciliation import BankReconciliation
from settlement_intake import SettlementIntake
from order_intake import OrderIntake


class ImportProfilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name);self.ops=Operations(self.store);self.finance=Finance(self.store)
        self.app=SimpleNamespace(store=self.store,finance=self.finance,ops=self.ops)
        self.p=ImportProfiles(self.app);self.app.import_profiles=self.p
        self.bank=BankReconciliation(self.app);self.settlement=SettlementIntake(self.app)
    def tearDown(self):self.tmp.cleanup()
    def columns(self,kind='bank'):return {field:'源_'+field for field in TYPES[kind]['fields']}
    def save(self,**extra):return self.p.save({'request_id':ident(),'confirmed':True,'type':'bank','name':'专用测试银行模板','columns':self.columns(),**extra})
    def row(self,**extra):return {'account_name':'测试SAR账户','bank_reference':ident(),'currency':'SAR','direction':'out','amount':'25.00','date':'2026-01-03','fx':'1.9','evidence':'专用模板测试依据',**extra}
    def source(self,row,kind='bank'):return {self.columns(kind)[key]:value for key,value in row.items()}
    def bank_preview(self,profile,rows,**extra):return self.bank.preview({'format':'json','filename':'合成映射流水.json','content':json.dumps(rows,ensure_ascii=False),'profile_id':profile['id'],'profile_revision':profile['revision'],**extra})
    def bank_import(self,b):return self.bank.import_rows({'request_id':ident(),'confirmed':True,'batch_id':b['id'],'token':b['token']})
    def test_save_and_revision_confirm_request_persistence(self):
        body={'request_id':'persistent-save','confirmed':True,'type':'bank','name':'命名模板','columns':self.columns()}
        p=self.p.save(body);self.assertEqual(p['revision'],1)
        reopened=ImportProfiles(SimpleNamespace(store=Store(self.tmp.name)))
        self.assertEqual(reopened.save(body),p)
        with self.assertRaises(Problem):reopened.save({**body,'name':'不同内容'})
        newer=self.save(id=p['id'],revision=1,name='新名称')
        self.assertEqual(newer['revision'],2)
        with self.assertRaises(Problem):self.save(id=p['id'],revision=1)
    def test_invalid_config_and_unsupported_normalization_are_rejected(self):
        for extra in ({'confirmed':False},{'confirmed':1},{'type':'bad'},{'name':''},{'columns':{}},{'columns':{'unknown':'金额'}},{'fixed_currency':'BTC'},{'columns':{k:'重复' for k in TYPES['bank']['fields']}},{'date_format':'DD/MM/YYYY'},{'decimal_separator':','},{'thousands_separator':','},{'value_maps':{}}):
            with self.assertRaises(Problem):self.save(**extra)
        self.assertEqual(self.p.state()['profiles'],[])
    def test_bounded_state_and_public_schema(self):
        for i in range(51):self.save(name='测试模板'+str(i))
        s=self.p.state();self.assertEqual(len(s['profiles']),50);self.assertEqual(s['total'],51)
        self.assertEqual(len(self.p.state(1)['profiles']),1)
        self.assertEqual(self.p.state(100)['page'],1)
        self.assertEqual(set(self.p.schema()['types']),{'order','settlement','bank'})
        self.assertNotIn('credentials',json.dumps(s))
        for page in (True,-1,'bad',1.2):
            with self.assertRaises(Problem):self.p.state(page)
    def test_remove_preserves_history_and_invalidates_fact(self):
        p=self.save();b=self.bank_preview(p,[self.source(self.row())])
        body={'request_id':ident(),'confirmed':True,'id':p['id'],'revision':1}
        removed=self.p.remove(body)
        self.assertEqual(self.p.remove(body),removed)
        self.assertEqual(removed['status'],'removed');self.assertEqual(removed['revision'],2)
        self.assertEqual(self.p.state()['profiles'],[])
        with self.assertRaises(Problem):self.bank_import(b)
        with self.store.connect() as c:self.assertEqual(self.p.get(c,p['id'])['status'],'removed')
    def test_bank_mapping_preserves_original_mapped_and_no_payment_on_import(self):
        p=self.save();original=self.source(self.row());original['额外备注']='保留源值'
        b=self.bank_preview(p,[original])
        self.assertEqual(b['summary']['ready'],1)
        self.assertEqual(b['rows'][0]['raw'],original)
        self.assertEqual(b['rows'][0]['mapped_raw']['amount'],'25.00')
        self.assertEqual(b['transformations'][0]['mapped']['amount'],'25.00')
        self.assertEqual(b['transformations'][0]['ignored_fields'],['额外备注'])
        receipt=self.bank_import(b)
        self.assertEqual(receipt['summary']['unmatched'],1)
        self.assertEqual(self.finance.state()['payments'],[])
        self.assertEqual(receipt['rows'][0]['raw'],original)
    def test_profile_update_blocks_old_bank_preview_without_any_import(self):
        p=self.save();b=self.bank_preview(p,[self.source(self.row())])
        self.save(id=p['id'],revision=p['revision'],name='修改后模板')
        with self.assertRaises(Problem) as error:self.bank_import(b)
        self.assertEqual(error.exception.status,409)
        with self.store.connect() as c:self.assertEqual(c.execute('SELECT count(*) FROM bank_rows').fetchone()[0],0)
    def test_fixed_currency_missing_source_is_explicit_and_conflicts_isolate(self):
        columns=self.columns();columns.pop('currency')
        p=self.save(columns=columns,fixed_currency='SAR')
        good=self.row();good.pop('currency');source=self.source(good)
        b=self.bank_preview(p,[source])
        self.assertEqual(b['rows'][0]['currency'],'SAR');self.assertEqual(b['summary']['ready'],1)
        conflict={**source,'currency':'USD'}
        b=self.bank_preview(p,[conflict])
        self.assertEqual(b['summary']['invalid'],1)
        self.assertEqual(b['currency_totals'],[])
        self.assertIn('固定币种矛盾',b['rows'][0]['reason'])
        self.assertEqual(b['transformations'][0]['original']['currency'],'USD')
        self.assertEqual(b['transformations'][0]['mapped']['currency'],'SAR')
    def test_mapped_currency_conflict_with_fixed_currency(self):
        p=self.save(fixed_currency='SAR')
        source=self.source(self.row(currency='USD'))
        b=self.bank_preview(p,[source])
        self.assertEqual(b['rows'][0]['status'],'invalid')
        self.assertIn('USD',b['transformations'][0]['problems'][0])
    def test_missing_source_column_and_amount_formats_do_not_guess(self):
        p=self.save();rows=[]
        missing=self.source(self.row());missing.pop('源_amount');rows.append(missing)
        for amount in ('1,000.00','25,00','','NaN','0'):
            rows.append(self.source(self.row(amount=amount)))
        b=self.bank_preview(p,rows)
        self.assertEqual(b['summary']['invalid'],6)
        self.assertTrue(all(r['problems'] for r in b['transformations']))
        self.assertIn('源列缺失',b['transformations'][0]['problems'][0])
        self.assertEqual(b['transformations'][1]['mapped']['amount'],'1,000.00')
        with self.assertRaises(Problem):self.bank_import(b)
    def test_csv_mapping_and_json_duplicate_fields_and_row_limits(self):
        p=self.save();source=self.source(self.row());stream=io.StringIO();w=csv.DictWriter(stream,fieldnames=list(source));w.writeheader();w.writerow(source)
        b=self.bank_preview(p,[],format='csv',content='\ufeff'+stream.getvalue())
        self.assertEqual(b['summary']['ready'],1)
        content=json.dumps([source]).replace('"25.00"','"25.00", "源_amount": "99"')
        with self.assertRaises(Problem):self.bank_preview(p,[],content=content)
        with self.assertRaises(Problem):self.bank_preview(p,[source]*501)
    def test_wrong_type_revision_and_removed_template_cannot_preview(self):
        p=self.save(type='settlement',columns=self.columns('settlement'))
        with self.assertRaises(Problem):self.bank_preview(p,[self.source(self.row())])
        p=self.save()
        with self.assertRaises(Problem):self.bank_preview({**p,'revision':0},[self.source(self.row())])
        with self.assertRaises(Problem):self.bank_preview({**p,'revision':True},[self.source(self.row())])
        with self.assertRaises(Problem):self.bank.preview({'format':'json','content':json.dumps([self.row()]),'profile_revision':1})
    def test_settlement_profile_previews_and_updated_guard(self):
        shop=self.ops.transact('entity',{'request_id':ident(),'kind':'shop','name':'专用模板结算店'})['id']
        p=self.save(type='settlement',columns=self.columns('settlement'))
        source=self.source({'evidence_key':ident(),'external_id':'','category':'platform','currency':'SAR','amount':'5.00','date':'2026-01-01','fx':'1.9','evidence':'模板结算费用','kind':'expense'},'settlement')
        b=self.settlement.preview({'shop_id':shop,'format':'json','content':json.dumps([source]),'profile_id':p['id'],'profile_revision':p['revision']})
        self.assertEqual(b['summary']['ready'],1);self.assertEqual(b['rows'][0]['raw'],source)
        self.save(id=p['id'],revision=1,type='settlement',columns=self.columns('settlement'))
        with self.assertRaises(Problem):self.settlement.apply({'request_id':ident(),'confirmed':True,'batch_id':b['id'],'token':b['token']})
        self.assertEqual(self.finance.state()['entries'],[])
    def test_profile_free_inputs_remain_unchanged(self):
        b=self.bank.preview({'format':'json','content':json.dumps([self.row()])})
        self.assertIsNone(b['profile_fact']);self.assertEqual(b['transformations'],[]);self.assertEqual(b['summary']['ready'],1)
    def test_order_profile_full_intake_guard_and_provenance(self):
        ids=self.store.import_rows([{'title_zh':'模板订单专用测试商品'}])['created']
        with self.store.connect() as c:
            sku=json.loads(c.execute('SELECT data FROM products WHERE id=?',(ids[0],)).fetchone()[0])['partner_sku']
        shop=self.ops.transact('entity',{'request_id':ident(),'kind':'shop','name':'模板订单店铺'})['id']
        warehouse=self.ops.transact('entity',{'request_id':ident(),'kind':'warehouse','name':'模板订单仓库'})['id']
        columns={k:v for k,v in self.columns('order').items() if k in TYPES['order']['required']}
        p=self.save(type='order',columns=columns,fixed_currency='SAR')
        intake=OrderIntake(self.app)
        original=self.source({'external_id':'PROFILE-ORDER','partner_sku':sku,'quantity':'2','unit_price':'12.50'},'order')
        body={'rows':[original],'shop_id':shop,'warehouse_id':warehouse,'profile_id':p['id'],'profile_revision':1}
        preview=intake.preview(body)
        self.assertTrue(preview['can_apply']);self.assertEqual(preview['rows'][0]['total_cents'],2500)
        self.assertEqual(preview['transformations'][0]['original'],original)
        self.assertEqual(intake.state()['profiles'][0]['id'],p['id'])
        newer=self.save(id=p['id'],revision=1,type='order',columns=columns,fixed_currency='SAR')
        with self.assertRaises(Problem):intake.apply({'request_id':ident(),'confirmed':True,'token':preview['token']})
        self.assertEqual(self.ops.state('orders')['documents'],[])
        preview=intake.preview({**body,'profile_revision':newer['revision']})
        result=intake.apply({'request_id':ident(),'confirmed':True,'token':preview['token']})
        self.assertEqual(result['created'],1)
        self.assertEqual(result['profile_fact']['revision'],2)
        self.assertEqual(result['transformations'][0]['original'],original)
        self.assertEqual(self.ops.state('orders')['documents'][0]['total_cents'],2500)
    def test_order_profile_currency_contradiction_and_invalid_amount_stop_whole_order(self):
        pid=self.store.import_rows([{'title_zh':'模板金额测试商品'}])['created'][0]
        with self.store.connect() as c:sku=json.loads(c.execute('SELECT data FROM products WHERE id=?',(pid,)).fetchone()[0])['partner_sku']
        shop=self.ops.transact('entity',{'request_id':ident(),'kind':'shop','name':'模板金额店'})['id']
        warehouse=self.ops.transact('entity',{'request_id':ident(),'kind':'warehouse','name':'模板金额仓'})['id']
        columns={k:v for k,v in self.columns('order').items() if k in TYPES['order']['required']}
        p=self.save(type='order',columns=columns,fixed_currency='SAR')
        intake=OrderIntake(self.app)
        for extra in ({'currency':'USD'},{'unit_price':'1,000.00'},{'unit_price':''},{'quantity':'1.5'}):
            source=self.source({'external_id':'BAD-PROFILE-ORDER','partner_sku':sku,'quantity':'1','unit_price':'25.00',**extra},'order')
            result=intake.preview({'rows':[source],'shop_id':shop,'warehouse_id':warehouse,'profile_id':p['id'],'profile_revision':1})
            self.assertFalse(result['can_apply']);self.assertTrue(result['errors']);self.assertTrue(result['transformations'][0]['problems'])
        self.assertEqual(self.ops.state('orders')['documents'],[])
    def test_order_adapter_returns_existing_rows_contract_and_preserves_context(self):
        p=self.save(type='order',columns=self.columns('order'),fixed_currency='SAR')
        source=self.source({'external_id':'ORDER-1','partner_sku':'SKU-1','quantity':'1','unit_price':'25.00'},'order')
        with self.store.connect() as c:
            body,fact,changes=self.p.apply(c,'order',{'rows':[source],'shop_id':'shop-1','warehouse_id':'warehouse-1','profile_id':p['id'],'profile_revision':1})
            self.p.guard(c,fact)
        self.assertEqual(body['rows'][0]['external_id'],'ORDER-1')
        self.assertEqual(body['rows'][0]['currency'],'SAR')
        self.assertEqual(body['shop_id'],'shop-1');self.assertEqual(body['warehouse_id'],'warehouse-1')
        self.assertEqual(changes[0]['original'],source)


if __name__=='__main__':unittest.main()
