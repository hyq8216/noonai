import json
import sys
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from alerts import Alerts,ACCOUNT_DIAGNOSTIC_LIMIT
from core import Problem,ident
from server import App

class AlertsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.app=App(self.root)
        self.current=datetime(2026,10,3,12,tzinfo=timezone.utc);self.alerts=Alerts(self.app,clock=lambda:self.current)
    def tearDown(self):
        for name in ('backup_schedules','collection_schedules','source_inbox','visual_checks','visuals','automation','media'):
            obj=getattr(self.app,name,None)
            if obj:obj.close()
        self.app.models.codex.close();self.app.collection_executor.shutdown(wait=True,cancel_futures=True);self.app.executor.shutdown(wait=True,cancel_futures=True);self.tmp.cleanup()
    def document(self,kind,status,age=0,**extra):
        key=ident();stamp=(self.current-timedelta(hours=age)).isoformat();data={'id':key,'kind':kind,'status':status,'created_at':stamp,'lines':[],'warehouse_id':'warehouse-test',**extra}
        with self.app.store.connect() as c:c.execute('INSERT INTO ops_documents VALUES(?,?,?,?,?,?)',(key,kind,None,json.dumps(data),1,stamp))
        return key
    def purchase(self):return self.document('purchase','open',age=2)
    def row(self,key):return next(r for r in self.alerts.state()['rows'] if r['key']==key)
    def action(self,row,mode='ack',**extra):
        return self.alerts.action({'key':row['key'],'fingerprint':row['fingerprint'],'mode':mode,'confirmed':True,'request_id':ident(),**extra})
    def test_all_source_groups_and_real_sql_evidence(self):
        purchase=self.purchase();order=self.document('order','new',age=25);shipped=self.document('order','shipped',age=220,shipped_at=(self.current-timedelta(days=8)).isoformat(),tracking='LOCAL-TRACK')
        with self.app.store.connect() as c:
            c.execute("INSERT INTO source_collection_runs(id,request_key,digest,account_id,account_revision,provider,query,page_limit,status,message,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",('run-test',ident(),'digest','account-test',2,'custom_json','',1,'attention','采集需要核对',self.current.isoformat(),self.current.isoformat()))
            c.execute('INSERT INTO automation_items VALUES(?,?,?,?,?,?,?,?,?,?)',('auto-test','flow-test','product-test',2,3,'approval',1,'{}','等人工批准',self.current.isoformat()))
            c.execute('INSERT INTO model_profiles VALUES(?,?,?)',('profile-test',1,json.dumps({'enabled':True,'daily_calls':1,'daily_usd':'10','provider':'openai'})))
            c.execute('INSERT INTO model_calls VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',('call-test',ident(),'digest','profile-test','{}','product-test','uncertain',500,500,None,None,'结果待核对',self.current.isoformat(),self.current.isoformat()))
            c.execute('INSERT INTO ops_replenishment VALUES(?,?,?)',('product-test','warehouse-test',json.dumps({'enabled':True,'minimum':5,'target':10,'revision':1,'updated_at':self.current.isoformat()})))
            c.execute('INSERT INTO finance_entries VALUES(?,?,?)',('finance-test','evidence-test',json.dumps({'status':'active','kind':'expense','revision':1,'currency':'CNY','amount_cents':10000,'due_date':'2026-10-01','allocations':[],'updated_at':self.current.isoformat()})))
            c.execute("INSERT INTO backup_schedule_runs(id,request_id,trigger,status,started_at,finished_at,error) VALUES(?,?,?,'failed',?,?,?)",('backup-test',ident(),'scheduled',self.current.isoformat(),self.current.isoformat(),'磁盘满'))
        with patch.object(self.app.finance,'state',side_effect=AssertionError('heavy finance called')),patch.object(self.app.ops.warehouse,'plan',side_effect=AssertionError('heavy warehouse called')):
            s=self.alerts.state()
        keys={r['key'] for r in s['rows']};self.assertIn('order-new:'+order,keys);self.assertIn('order-shipped:'+shipped,keys)
        self.assertIn('purchase:'+purchase,keys);self.assertIn('model-quota:profile-test',keys)
        for group in ('source','automation','models','orders','purchasing','inventory','finance','backup'):self.assertGreater(s['counts'][group]['total'],0)
        self.assertEqual(self.row('finance-due:finance-test')['evidence']['remaining_cents'],10000)
        self.assertEqual(self.row('replenish:product-test:warehouse-test')['evidence']['projected'],0)
    def test_ack_only_current_observation_changes_reopen_and_source_never_mutates(self):
        key=self.purchase();row=self.row('purchase:'+key)
        with self.app.store.connect() as c:before=c.execute('SELECT data,revision FROM ops_documents WHERE id=?',(key,)).fetchone()
        self.action(row);self.assertEqual(self.row(row['key'])['mode'],'ack')
        with self.app.store.connect() as c:
            after=c.execute('SELECT data,revision FROM ops_documents WHERE id=?',(key,)).fetchone();self.assertEqual(tuple(before),tuple(after))
            data=json.loads(after['data']);data['status']='partial';c.execute('UPDATE ops_documents SET data=?,revision=revision+1 WHERE id=?',(json.dumps(data),key))
        changed=self.row(row['key']);self.assertNotEqual(changed['fingerprint'],row['fingerprint']);self.assertEqual(changed['mode'],'open')
        with self.assertRaises(Problem):self.action(row)
        self.action(changed);self.action(changed,mode='reopen');self.assertEqual(self.row(row['key'])['mode'],'open')
    def test_snooze_expiry_source_change_and_seven_day_limit(self):
        row=self.row('purchase:'+self.purchase());self.action(row,'snooze',until=(self.current+timedelta(hours=1)).isoformat());self.assertEqual(self.row(row['key'])['mode'],'snooze')
        self.current+=timedelta(hours=2);self.assertEqual(self.row(row['key'])['mode'],'open')
        for until in ((self.current-timedelta(seconds=1)).isoformat(),(self.current+timedelta(days=7,seconds=1)).isoformat(),'2026-10-05T12:00:00'):
            with self.assertRaises(Problem):self.action(row,'snooze',until=until)
        self.action(row,'snooze',until=(self.current+timedelta(days=7)).isoformat());self.assertEqual(self.row(row['key'])['mode'],'snooze')
        with self.app.store.connect() as c:c.execute('UPDATE ops_documents SET revision=revision+1 WHERE id=?',(row['target_id'],))
        self.assertEqual(self.row(row['key'])['mode'],'open')
    def test_action_confirmation_idempotency_audit_and_restart(self):
        row=self.row('purchase:'+self.purchase());body={'key':row['key'],'fingerprint':row['fingerprint'],'mode':'ack','confirmed':True,'request_id':ident()}
        with self.assertRaises(Problem):self.alerts.action({**body,'confirmed':False})
        first=self.alerts.action(body);second=self.alerts.action(body);self.assertFalse(first['replayed']);self.assertTrue(second['replayed'])
        with self.assertRaises(Problem):self.alerts.action({**body,'mode':'reopen'})
        self.alerts=Alerts(self.app,clock=lambda:self.current);self.assertEqual(self.row(row['key'])['mode'],'ack')
        self.assertEqual(len(self.alerts.state()['events']),1)
    def test_resolved_sources_disappear_stale_action_rejected_but_audit_retained(self):
        key=self.purchase();row=self.row('purchase:'+key);self.action(row)
        with self.app.store.connect() as c:
            data=json.loads(c.execute('SELECT data FROM ops_documents WHERE id=?',(key,)).fetchone()[0]);data['status']='received';c.execute('UPDATE ops_documents SET data=? WHERE id=?',(json.dumps(data),key))
        self.assertEqual(self.alerts.state()['total'],0)
        with self.assertRaises(Problem):self.action(row)
        self.assertEqual(len(self.alerts.state()['events']),1)
    def test_pagination_counts_bounds_invalid_filters_and_no_repeated_observations(self):
        for _ in range(123):self.purchase()
        first=self.alerts.state();second=self.alerts.state(page=1,group='purchasing');third=self.alerts.state(page=2,group='purchasing')
        self.assertEqual(first['total'],123);self.assertEqual(first['pages'],3);self.assertEqual(len(first['rows']),50);self.assertEqual(len(second['rows']),50);self.assertEqual(len(third['rows']),23)
        self.assertFalse({r['key'] for r in first['rows']}&{r['key'] for r in second['rows']})
        self.assertEqual(self.alerts.state()['total'],123)
        for kwargs in ({'page':True},{'page':-1},{'page':100001},{'group':'unknown'}):
            with self.assertRaises(Problem):self.alerts.state(**kwargs)
    def test_local_order_thresholds_require_valid_original_dates(self):
        self.document('order','new',age=23);self.document('order','new',age=24);self.document('order','shipped',age=500)
        self.document('order','new',created_at='bad-date')
        self.assertEqual(self.alerts.state(group='orders')['total'],0)
        self.document('order','new',age=25);self.document('order','shipped',shipped_at=(self.current-timedelta(days=7,seconds=1)).isoformat())
        self.assertEqual(self.alerts.state(group='orders')['total'],2)
    def test_finance_payments_voids_and_allocations_are_measured_without_heavy_state(self):
        with self.app.store.connect() as c:
            c.execute('INSERT INTO finance_entries VALUES(?,?,?)',('entry-test','ev',json.dumps({'status':'active','kind':'expense','revision':1,'currency':'CNY','amount_cents':10000,'due_date':'2026-10-01','allocations':[{'order_id':'order-test','amount_cents':5000}],'updated_at':self.current.isoformat()})))
            c.execute('INSERT INTO finance_payments VALUES(?,?,?,?)',('pay-test','entry-test','pay-ev',json.dumps({'status':'active','amount_cents':10000})))
        self.assertEqual(self.alerts.state(group='finance')['total'],1)
        self.assertEqual(self.row('finance-allocation:entry-test')['evidence']['allocated_cents'],5000)
        with self.app.store.connect() as c:c.execute('UPDATE finance_payments SET data=? WHERE id=?',(json.dumps({'status':'void','amount_cents':10000}),'pay-test'))
        self.assertEqual(self.row('finance-due:entry-test')['evidence']['remaining_cents'],10000)
    def test_replenishment_incoming_and_demand_match_local_projection(self):
        with self.app.store.connect() as c:
            c.execute('INSERT INTO ops_replenishment VALUES(?,?,?)',('p','warehouse-test',json.dumps({'enabled':True,'minimum':5,'target':10,'revision':1,'updated_at':self.current.isoformat()})))
            c.execute('INSERT INTO ops_stock VALUES(?,?,?,?)',('p','warehouse-test',4,1))
        self.document('purchase','partial',lines=[{'product_id':'p','quantity':8,'received':3}])
        self.document('transfer','in_transit',lines=[{'product_id':'p','shipped':5,'received':2}])
        self.document('order','new',lines=[{'product_id':'p','quantity':8}])
        row=self.row('replenish:p:warehouse-test');self.assertEqual(row['evidence']['projected'],3)
        self.document('purchase','open',lines=[{'product_id':'p','quantity':2,'received':0}]);self.assertEqual(self.alerts.state(group='inventory')['total'],0)
    def test_account_attention_no_secret_exposure_and_bounded_diagnostics(self):
        a=self.app.channel_accounts.save({'provider':'custom_json','name':'test','base_url':'https://example.com/api','enabled':True,'config':{},'token':'secret-token','revision':0})
        self.app.channel_accounts._path(a['id']).write_text('malformed secret-token')
        row=self.row('account:'+a['id']);self.assertIn('凭据损坏',row['message']);self.assertNotIn('secret-token',json.dumps(row))
        with self.app.store.connect() as c:
            for _ in range(ACCOUNT_DIAGNOSTIC_LIMIT+2):
                key=ident();c.execute('INSERT INTO source_channel_accounts VALUES(?,?,?,?,?,?,?,?,?)',(key,'supplier_file','test','',1,1,'{}','2020-01-01','2020-01-01'))
        with patch.object(self.app.channel_accounts,'get',wraps=self.app.channel_accounts.get) as get:
            s=self.alerts.state();self.assertEqual(get.call_count,ACCOUNT_DIAGNOSTIC_LIMIT);self.assertTrue(s['account_diagnostics']['limited'])
    def test_disabled_or_inaccessible_account_diagnosis_does_not_accept_stale_action(self):
        a=self.app.channel_accounts.save({'provider':'custom_json','name':'test','base_url':'https://example.com/api','enabled':True,'config':{},'token':'secret-token','revision':0})
        path=self.app.channel_accounts._path(a['id']);path.unlink();target=self.root/'user-secret';target.write_text('user-secret-value');path.symlink_to(target)
        row=self.row('account:'+a['id']);self.assertIn('符号链接',row['message']);self.assertNotIn('user-secret-value',json.dumps(row))
        with self.app.store.connect() as c:c.execute('UPDATE source_channel_accounts SET enabled=0 WHERE id=?',(a['id'],))
        self.assertEqual(self.alerts.state(group='source')['total'],0)
        with self.assertRaises(Problem):self.action(row)

    def test_backup_overdue_disabled_and_success_recovers_failure(self):
        old=(self.current-timedelta(hours=3)).isoformat()
        with self.app.store.connect() as c:
            c.execute('UPDATE backup_schedule_config SET enabled=1,interval_minutes=60,updated_at=?',(old,))
        self.assertEqual(self.alerts.state(group='backup')['total'],1)
        with self.app.store.connect() as c:
            c.execute("INSERT INTO backup_schedule_runs(id,request_id,trigger,status,started_at,finished_at,error) VALUES(?,?,?,'failed',?,?,?)",('b1',ident(),'scheduled',old,old,'disk full'))
        self.assertEqual(self.alerts.state(group='backup')['total'],2)
        with self.app.store.connect() as c:
            c.execute("INSERT INTO backup_schedule_runs(id,request_id,trigger,status,started_at,finished_at) VALUES(?,?,?,'success',?,?)",('b2',ident(),'scheduled',self.current.isoformat(),self.current.isoformat()))
        self.assertEqual(self.alerts.state(group='backup')['total'],0)
        self.current+=timedelta(hours=2);self.assertEqual(self.alerts.state(group='backup')['total'],1)
        with self.app.store.connect() as c:c.execute('UPDATE backup_schedule_config SET enabled=0')
        self.assertEqual(self.alerts.state(group='backup')['total'],0)

if __name__=='__main__':unittest.main()
