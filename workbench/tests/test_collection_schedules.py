import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import Problem, Store
from source_collection import SourceCollection
from collection_schedules import CollectionSchedules


class Clock:
    def __init__(self):
        self.wall = 1800000000
        self.mono = 1000
    def time(self):return self.wall
    def monotonic(self):return self.mono
    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


class Accounts:
    def __init__(self):
        self.row = dict(id='account', name='Test', provider='custom_json', enabled=True,
                        revision=1, base_url='https://supplier.example/api', config={}, has_token=False)
        self.exists = True
    def get(self, aid, connection=None, with_secret=False):
        if aid != 'account' or not self.exists:raise Problem('账号不存在', 404)
        return {**self.row, **({'token': ''} if with_secret else {})}


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(self.tmp.name)
        self.clock = Clock()
        self.sent = []
        self.app = SimpleNamespace(store=self.store, channel_accounts=Accounts(), write_lock=threading.RLock())
        self.app.source_collection = SourceCollection(self.app)
        self.app.dispatch_collection = lambda run: self.sent.append(run)
        self.schedules = CollectionSchedules(self.app, clock=self.clock)
    def tearDown(self):
        self.schedules.close()
        self.tmp.cleanup()
    def body(self, **extra):
        return dict(name='货源', account_id='account', account_revision=1, query='box',
                    page_limit=2, interval_minutes=15, enabled=True, confirmed=True,
                    request_id='save', **extra)
    def save(self, **extra):return self.schedules.save(self.body(**extra))
    def rules(self):return self.schedules.state()['rules']
    def complete(self, run=None, status='completed'):
        rid = (run or self.sent[-1])['id']
        with self.store.connect() as c:
            c.execute('UPDATE source_collection_runs SET status=?,message=? WHERE id=?', (status, '核对原因' if status=='attention' else '', rid))
    def control(self, row, action, **extra):
        return self.schedules.control(dict(id=row['id'], revision=row['revision'], account_revision=1,
                                          action=action, confirmed=True, request_id='control_'+action, **extra))
    def test_confirmed_version_validation_and_durable_receipts(self):
        row = self.save()
        with self.assertRaises(Problem):self.schedules.save({**self.body(), 'confirmed':False, 'request_id':'bad'})
        for changes in ({'interval_minutes':14}, {'page_limit':21}, {'enabled':1}, {'account_revision':2}):
            with self.assertRaises(Problem):self.schedules.save({**self.body(), **changes, 'request_id':'invalid'})
        self.app.channel_accounts.row['enabled'] = False
        replay = self.save()
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['id'], row['id'])
        with self.assertRaises(Problem):self.schedules.save({**self.body(), 'query':'different'})
        other = CollectionSchedules(self.app, self.clock)
        self.assertTrue(other.save(self.body())['replayed'])
        with self.assertRaises(Problem):self.schedules.save({**self.body(), 'id':row['id'], 'revision':0, 'request_id':'stale'})
    def test_one_due_run_no_backlog_no_automatic_import(self):
        row = self.save()
        self.schedules.tick()
        self.clock.advance(899)
        self.assertEqual(self.schedules.tick(), 0)
        self.clock.advance(1)
        self.assertEqual(self.schedules.tick(), 1)
        self.assertEqual(self.sent[0]['query'], 'box')
        self.assertEqual(self.sent[0]['page_limit'], 2)
        self.assertEqual(self.store.list(), [])
        self.complete()
        self.clock.advance(86400)
        self.assertEqual(self.schedules.tick(), 1)
        self.assertEqual(self.schedules.tick(), 0)
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.rules()[0]['next_run'], self.clock.wall + 900)
    def test_inflight_never_accumulates_and_pause_does_not_cancel(self):
        row = self.save()
        self.clock.advance(900)
        self.schedules.tick()
        for unused in range(4):
            self.clock.advance(900)
            self.assertEqual(self.schedules.tick(), 0)
        self.assertEqual(len(self.sent), 1)
        paused = self.control(self.rules()[0], 'pause')
        self.assertFalse(paused['enabled'])
        with self.assertRaises(Problem):self.control(paused, 'resume')
        with self.store.connect() as c:
            self.assertEqual(c.execute('SELECT status FROM source_collection_runs').fetchone()[0], 'queued')
        self.schedules.close()
        self.complete()
        self.assertEqual(self.schedules.tick(), 0)
    def test_attention_requires_review_and_preserves_old_run(self):
        self.save()
        self.clock.advance(900)
        self.schedules.tick()
        self.complete(status='attention')
        paused = self.rules()[0]
        self.assertFalse(paused['enabled'])
        self.assertTrue(paused['attention'])
        self.clock.advance(10000)
        self.assertEqual(self.schedules.tick(), 0)
        with self.assertRaises(Problem):self.control(paused, 'resume')
        resumed = self.control(paused, 'resume', attention_checked=True)
        self.assertEqual(resumed['last_run_id'], self.sent[0]['id'])
        self.assertTrue(self.rules()[0]['enabled'])
        self.assertFalse(self.rules()[0]['attention'])
        self.clock.advance(900)
        self.assertEqual(self.schedules.tick(), 1)
    def test_account_changed_disabled_removed_auto_pause(self):
        for change in ('revision', 'enabled', 'removed'):
            with self.subTest(change=change):
                self.app.channel_accounts.exists = True
                self.app.channel_accounts.row.update(enabled=True, revision=1)
                self.schedules.save({**self.body(), 'request_id':change})
                if change == 'revision':self.app.channel_accounts.row['revision'] = 2
                elif change == 'enabled':self.app.channel_accounts.row['enabled'] = False
                else:self.app.channel_accounts.exists = False
                self.schedules.tick()
                self.assertFalse(self.rules()[0]['enabled'])
                self.assertEqual(len(self.sent), 0)
    def test_restart_next_interval_and_no_missed_catchup(self):
        self.save()
        self.clock.advance(50000)
        other = CollectionSchedules(self.app, self.clock)
        self.assertEqual(other.state()['rules'][0]['next_run'], self.clock.wall + 900)
        self.assertEqual(other.tick(), 0)
        self.clock.advance(900)
        self.assertEqual(other.tick(), 1)
        other.close()
    def test_pending_dispatch_restart_is_attention_never_replayed(self):
        row = self.save()
        run = self.app.source_collection.create(dict(request_id='schedule_lost', confirmed=True, account_id='account'))
        with self.store.connect() as c:
            c.execute('UPDATE collection_schedule_rules SET pending_request=? WHERE id=?', ('schedule_lost', row['id']))
        other = CollectionSchedules(self.app, self.clock)
        paused = other.state()['rules'][0]
        self.assertFalse(paused['enabled'])
        self.assertTrue(paused['attention'])
        self.assertEqual(paused['last_run_id'], run['id'])
        self.assertEqual(paused['last_run_status'], 'attention')
        self.clock.advance(99999)
        self.assertEqual(other.tick(), 0)
        self.assertEqual(self.sent, [])
        other.close()
    def test_intent_without_run_is_visible_attention(self):
        row = self.save()
        with self.store.connect() as c:
            c.execute("UPDATE collection_schedule_rules SET pending_request='schedule_missing' WHERE id=?", (row['id'],))
        other = CollectionSchedules(self.app, self.clock)
        paused = other.state()['rules'][0]
        self.assertFalse(paused['enabled'])
        self.assertTrue(paused['attention'])
        self.assertEqual(other.tick(), 0)
        other.close()
    def test_submit_failure_queued_run_attention_and_bounded(self):
        self.save()
        self.app.dispatch_collection = lambda run: (_ for _ in ()).throw(RuntimeError('private token'))
        self.clock.advance(900)
        self.assertEqual(self.schedules.tick(), 0)
        paused = self.rules()[0]
        self.assertFalse(paused['enabled'])
        self.assertEqual(paused['last_run_status'], 'attention')
        self.assertNotIn('private token', str(paused))
        for unused in range(4):
            self.clock.advance(900)
            self.schedules.tick()
        self.assertEqual(self.app.source_collection.state()['run_total'], 1)
    def test_create_committed_then_error_keeps_receipt(self):
        self.save()
        create = self.app.source_collection.create
        def interrupted(body):
            create(body)
            raise RuntimeError('after commit')
        with patch.object(self.app.source_collection, 'create', side_effect=interrupted):
            self.clock.advance(900)
            self.schedules.tick()
        self.assertEqual(self.rules()[0]['last_run_status'], 'attention')
        self.assertIsNotNone(self.rules()[0]['last_run_id'])
    def test_restore_pending_pauses_and_does_not_resume_on_clear(self):
        self.save()
        pending = Path(self.tmp.name) / 'pending-restore.json'
        pending.write_text('{}')
        self.app.recovery = SimpleNamespace(pending=pending)
        self.clock.advance(900)
        self.schedules.tick()
        paused = self.rules()[0]
        self.assertFalse(paused['enabled'])
        self.assertIn('恢复', paused['pause_reason'])
        pending.unlink()
        self.clock.advance(99999)
        self.schedules.tick()
        self.assertFalse(self.rules()[0]['enabled'])
        self.assertEqual(self.sent, [])
    def test_monotonic_deadline_resists_wall_clock_jump(self):
        self.save()
        self.schedules.tick()
        self.clock.wall += 999999
        self.assertEqual(self.schedules.tick(), 0)
        self.clock.mono += 900
        self.assertEqual(self.schedules.tick(), 1)
    def test_lifecycle_explicit_thread_and_stop(self):
        self.assertIsNone(self.schedules.thread)
        self.schedules.start()
        original = self.schedules.thread
        self.schedules.start()
        self.assertIs(self.schedules.thread, original)
        self.schedules.close()
        self.assertFalse(original.is_alive())
        self.assertEqual(self.schedules.tick(), 0)
    def test_control_receipt_replays_without_double_revision(self):
        row = self.save()
        first = self.control(row, 'pause')
        again = self.control(row, 'pause')
        self.assertTrue(again['replayed'])
        self.assertEqual(first['revision'], again['revision'])
        self.assertEqual(self.rules()[0]['revision'], first['revision'])
        with self.assertRaises(Problem):
            self.control(first, 'pause')  # same request key with different revision
        with self.assertRaises(Problem):
            self.schedules.control(dict(id=first['id'], revision=first['revision'], action='resume',
                                        account_revision=True, confirmed=True, request_id='bool_version'))

    def test_scheduler_uses_collection_pipeline_without_auto_import(self):
        self.save()
        self.app.dispatch_collection = lambda run: self.app.source_collection.run(run['id'])
        item = dict(external_id='sku1', title_zh='盒子', source_url='https://supplier.example/box',
                    source_sku='box', supplier='供货商', facts='尺寸10cm', stock=0,
                    source_price=8, source_currency='CNY', images=[])
        with patch('channel_adapters.fetch_page', return_value={'items':[item], 'next_cursor':None, 'warnings':[]}) as fetch:
            self.clock.advance(900)
            self.assertEqual(self.schedules.tick(), 1)
            fetch.assert_called_once()
        self.assertEqual(self.rules()[0]['last_run_status'], 'completed')
        self.assertEqual(self.app.source_collection.state()['total'], 1)
        self.assertEqual(self.store.list(), [])

    def test_concurrent_tick_calls_create_only_one_run(self):
        self.save()
        self.clock.advance(900)
        threads = [threading.Thread(target=self.schedules.tick) for unused in range(4)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.app.source_collection.state()['run_total'], 1)

    def test_account_race_after_create_pauses_without_dispatch(self):
        self.save()
        create = self.app.source_collection.create
        def changed(body):
            self.app.channel_accounts.row['revision'] = 2
            return create(body)
        with patch.object(self.app.source_collection, 'create', side_effect=changed):
            self.clock.advance(900)
            self.schedules.tick()
        self.assertEqual(self.sent, [])
        self.assertFalse(self.rules()[0]['enabled'])
        self.assertEqual(self.rules()[0]['last_run_status'], 'attention')


if __name__ == '__main__':unittest.main()
