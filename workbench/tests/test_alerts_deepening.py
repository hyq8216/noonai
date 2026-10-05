import csv
import io
import json
import unittest
from datetime import timedelta
from unittest.mock import patch
import test_alerts
from alerts import Alerts
from core import Problem,ident


class AlertsDeepeningTests(unittest.TestCase):
    setUp=test_alerts.AlertsTests.setUp
    tearDown=test_alerts.AlertsTests.tearDown
    document=test_alerts.AlertsTests.document
    purchase=test_alerts.AlertsTests.purchase
    row=test_alerts.AlertsTests.row
    action=test_alerts.AlertsTests.action

    def fee(self,key='fee',due='2026-10-01',message=None):
        data={'status':'active','kind':'expense','revision':1,'currency':'CNY','amount_cents':10000,'due_date':due,'allocations':[],'updated_at':self.current.isoformat()}
        with self.app.store.connect() as c:c.execute('INSERT INTO finance_entries VALUES(?,?,?)',(key,'evidence-'+key,json.dumps(data)))
        return key

    def test_deadline_explanations_use_original_evidence_and_no_purchase_guess(self):
        self.fee();order=self.document('order','new',age=25);shipment=self.document('order','shipped',shipped_at=(self.current-timedelta(days=8)).isoformat())
        purchase=self.row('purchase:'+self.purchase())
        self.assertIsNone(purchase['deadline_at']);self.assertIsNone(purchase['overdue_seconds']);self.assertIn('未记录',purchase['priority_reason'])
        new=self.row('order-new:'+order);self.assertEqual(new['overdue_seconds'],3600);self.assertEqual(new['deadline_basis'],'local_threshold')
        shipped=self.row('order-shipped:'+shipment);self.assertEqual(shipped['overdue_seconds'],86400)
        fee=self.row('finance-due:fee');self.assertEqual(fee['deadline_at'],'2026-10-02T00:00:00Z');self.assertEqual(fee['overdue_seconds'],129600);self.assertEqual(fee['deadline_basis'],'recorded_due_date')
        for row in (purchase,new,shipped,fee):self.assertTrue(row['priority_reason']);self.assertTrue(row['suggested_action'])

    def test_unknown_and_malformed_due_date_do_not_invent_deadline(self):
        self.fee('no-due','');self.fee('bad-due','0000-bad')
        self.assertFalse(any(r['key'].startswith('finance-due:') for r in self.alerts.state()['rows']))

    def test_filters_counts_apply_to_all_rows_before_pagination(self):
        for _ in range(61):self.purchase()
        self.fee();self.document('order','new',age=25)
        first=self.alerts.state(severity='info');second=self.alerts.state(page=1,severity='info')
        self.assertEqual(first['total'],62);self.assertEqual(len(first['rows']),50);self.assertEqual(len(second['rows']),12)
        self.assertFalse({r['key'] for r in first['rows']}&{r['key'] for r in second['rows']})
        self.assertEqual(first['severity_counts']['warning'],2);self.assertEqual(first['severity_counts']['info'],62)
        group=self.alerts.state(group='finance',severity='warning');self.assertEqual(group['total'],1);self.assertEqual(group['severity_counts']['info'],1)
        self.assertEqual(self.alerts.state(page=999,group='finance')['page'],0)

    def test_mode_filters_track_snooze_expiry_and_source_revisions(self):
        row=self.row('purchase:'+self.purchase());body={'key':row['key'],'fingerprint':row['fingerprint'],'mode':'ack','confirmed':True,'request_id':ident()}
        self.alerts.action(body);self.assertTrue(self.alerts.action(body)['replayed'])
        self.assertEqual(self.alerts.state(mode='ack')['total'],1);self.assertEqual(self.alerts.state(mode='open')['total'],0)
        self.action(row,'snooze',until=(self.current+timedelta(hours=1)).isoformat());self.assertEqual(self.alerts.state(mode='snooze')['total'],1)
        self.current+=timedelta(hours=2);self.assertEqual(self.alerts.state(mode='open')['total'],1)
        self.action(row)
        with self.app.store.connect() as c:c.execute('UPDATE ops_documents SET revision=revision+1 WHERE id=?',(row['target_id'],))
        self.assertEqual(self.alerts.state(mode='ack')['total'],0);self.assertEqual(self.alerts.state(mode='open')['total'],1)
        with self.assertRaises(Problem):self.action(row)

    def test_sql_sorting_uses_deadlines_and_category_not_current_page(self):
        self.fee();old=self.document('order','new',age=70);new=self.document('order','new',age=25);self.purchase()
        latest=self.alerts.state(group='orders',sort='latest')['rows'];oldest=self.alerts.state(group='orders',sort='oldest')['rows']
        self.assertEqual(latest[0]['target_id'],new);self.assertEqual(oldest[0]['target_id'],old)
        rows=self.alerts.state(sort='overdue')['rows'];self.assertIsNone(rows[-1]['deadline_at']);self.assertEqual(rows[0]['target_id'],old)
        rows=self.alerts.state(sort='category')['rows'];self.assertEqual([r['group'] for r in rows],sorted(r['group'] for r in rows))

    def test_export_all_filtered_rows_safe_csv_and_no_writes(self):
        for i in range(57):self.document('purchase','open',id_hint=i)
        self.fee();row=self.alerts.state(group='purchasing')['rows'][0];self.action(row)
        with self.app.store.connect() as c:
            c.execute("INSERT INTO source_collection_runs(id,request_key,digest,account_id,account_revision,provider,query,page_limit,status,message,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",('run',ident(),'digest','account',1,'custom_json','',1,'attention',' \t=HYPERLINK("bad")\n,tail',self.current.isoformat(),self.current.isoformat()))
            before=[tuple(r) for r in c.execute('SELECT id,data,revision FROM ops_documents ORDER BY id')]
            events=c.execute('SELECT count(*) FROM alert_events').fetchone()[0]
        with patch.object(self.app.finance,'state',side_effect=AssertionError('heavy state')):
            content=self.alerts.export({'group':'purchasing','severity':'info','mode':'open','page':'1'})
        self.assertTrue(content.startswith(b'\xef\xbb\xbf'));rows=list(csv.DictReader(io.StringIO(content.decode('utf-8-sig'))));self.assertEqual(len(rows),56)
        source=list(csv.DictReader(io.StringIO(self.alerts.export({'group':'source'}).decode('utf-8-sig'))))[0]
        self.assertTrue(source['异常说明'].startswith("'"));self.assertIn('tail',source['异常说明'])
        self.assertEqual(len(list(csv.reader(io.StringIO(self.alerts.export({'severity':'critical'}).decode('utf-8-sig'))))),1)
        with self.app.store.connect() as c:
            self.assertEqual(before,[tuple(r) for r in c.execute('SELECT id,data,revision FROM ops_documents ORDER BY id')]);self.assertEqual(events,c.execute('SELECT count(*) FROM alert_events').fetchone()[0])

    def test_export_limit_rejects_partial_file_and_invalid_filters(self):
        self.purchase();self.purchase()
        with patch('alerts.EXPORT_LIMIT',1):
            with self.assertRaisesRegex(Problem,'未导出部分结果'):self.alerts.export()
        for kwargs in ({'severity':'urgent'},{'mode':'resolved'},{'sort':'arbitrary sql'},{'group':'invalid'}):
            with self.assertRaises(Problem):self.alerts.state(**kwargs)
            with self.assertRaises(Problem):self.alerts.export(kwargs)

    def test_elapsed_time_does_not_invalidate_current_observation_ack(self):
        key=self.document('order','new',age=25);row=self.row('order-new:'+key);self.action(row);self.current+=timedelta(hours=1)
        fresh=self.row(row['key']);self.assertEqual(fresh['fingerprint'],row['fingerprint']);self.assertEqual(fresh['mode'],'ack');self.assertEqual(fresh['overdue_seconds'],7200)
        self.alerts=Alerts(self.app,clock=lambda:self.current);self.assertEqual(self.row(row['key'])['mode'],'ack')

if __name__=='__main__':unittest.main()
