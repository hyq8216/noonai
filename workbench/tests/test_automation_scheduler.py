"""Deterministic scheduler acceptance; no external accounts or model calls."""
import json
import sys
import tempfile
import unittest
from datetime import timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from automation import Automation
from core import Problem
from server import App
from workflow_clock import ManualClock


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = App(self.tmp.name)
        self.clock = ManualClock()
        self.auto = Automation(self.app, self.clock, lambda: self.clock.elapsed)
        self.app.automation = self.auto

    def tearDown(self):
        self.auto.close()
        self.app.visual_checks.close()
        self.app.visuals.close()
        self.app.media.close()
        self.app.models.codex.close()
        self.app.executor.shutdown(wait=True)
        self.tmp.cleanup()

    def create(self, count=1, **extra):
        ids = self.app.store.import_rows([
            {'title_zh': f'调度验收 {n}', 'source_url': f'https://example.test/{n}',
             'source_sku': str(n), 'supplier': 'QA', 'facts': 'Synthetic fixture'}
            for n in range(count)])['created']
        return self.auto.create({'product_ids': ids, 'request_id': 'schedule',
                                 'name': '调度验收', **extra})['id']

    def rows(self):
        with self.app.store.connect() as c:
            return [dict(r) for r in c.execute('SELECT * FROM automation_items ORDER BY rowid')]

    def test_idle_sleep_and_scheduled_deadline(self):
        self.assertEqual(self.auto.next_delay(),30)
        self.create(run_at=(self.clock()+timedelta(seconds=3)).isoformat())
        self.assertTrue(self.auto.changed.is_set())
        self.assertAlmostEqual(self.auto.next_delay(),3)
        self.clock.advance(3)
        self.assertEqual(self.auto.next_delay(),.05)

    def test_approval_event_clears_only_approval_poll_not_model_quota(self):
        self.create(2)
        rows=self.rows()
        with self.app.store.connect() as c:
            for row,status,step in [(rows[0],'approval',2),(rows[1],'waiting',0)]:
                c.execute('UPDATE automation_items SET status=?,step=?,data=? WHERE id=?',
                    (status,step,json.dumps({'retry_at':(self.clock()+timedelta(hours=1)).isoformat()}),row['id']))
        self.auto.wake(approval=True)
        self.assertNotIn('retry_at',json.loads(self.rows()[0]['data']))
        self.assertIn('retry_at',json.loads(self.rows()[1]['data']))
        self.auto.tick()
        self.assertNotEqual(self.rows()[0]['status'],'done')
        self.assertEqual(self.rows()[1]['step'],0)

    def test_finished_run_summary_updates_without_five_second_delay(self):
        rid=self.create()
        self.auto.tick()
        with self.app.store.connect() as c:
            c.execute("UPDATE automation_items SET step=3,status='queued' WHERE run_id=?",(rid,))
        self.auto.tick()
        with self.app.store.connect() as c:
            self.assertEqual(c.execute('SELECT status FROM automation_runs WHERE id=?',(rid,)).fetchone()[0],'done')

    def test_create_wakes_actual_idle_worker(self):
        import threading
        idle=threading.Event();processed=threading.Event()
        original_delay=self.auto.next_delay
        original_process=self.auto.process
        def delay():
            value=original_delay()
            if value==30:idle.set()
            return value
        def process(item):
            original_process(item);processed.set()
        with patch.object(self.auto,'next_delay',side_effect=delay),patch.object(self.auto,'process',side_effect=process):
            self.auto.start()
            self.assertTrue(idle.wait(2),'worker must reach the idle wait')
            self.create()
            self.assertTrue(processed.wait(2),'new run must wake the 30-second idle wait')
            self.auto.close()
        self.assertGreaterEqual(self.rows()[0]['step'],1)

    def test_wake_during_tick_is_not_lost(self):
        import threading
        completed=threading.Event()
        count=[]
        def tick():
            count.append(1)
            if len(count)==1:self.auto.wake()
            else:self.auto.stop.set();self.auto.changed.set();completed.set()
        with patch.object(self.auto,'tick',side_effect=tick),patch.object(self.auto,'next_delay',return_value=30):
            self.auto.thread=threading.Thread(target=self.auto.loop)
            self.auto.thread.start()
            self.assertTrue(completed.wait(2),'wake arriving during tick must interrupt the following wait')
            self.auto.close()
        self.assertEqual(len(count),2)

    def test_close_wakes_idle_thread(self):
        import threading
        self.auto.thread=threading.Thread(target=self.auto.loop)
        self.auto.thread.start()
        self.auto.close()
        self.assertFalse(self.auto.thread.is_alive())

    def test_scheduled_run_uses_timezone_and_exact_deadline(self):
        due = self.clock() + timedelta(seconds=60)
        self.create(run_at=due.astimezone(timezone(timedelta(hours=8))).isoformat())
        self.auto.tick(); self.assertEqual(self.rows()[0]['step'], 0)
        self.clock.advance(59); self.auto.tick(); self.assertEqual(self.rows()[0]['step'], 0)
        self.clock.advance(1); self.auto.tick(); self.assertEqual(self.rows()[0]['step'], 1)

    def test_wait_poll_does_not_run_early_or_duplicate_events(self):
        self.create()
        with patch.object(self.auto, 'process', side_effect=lambda i: self.auto.save(i, 'waiting', '等待子任务')) as process:
            self.auto.tick(); self.assertEqual(process.call_count, 1)
            self.auto.tick(); self.clock.advance(4); self.auto.tick(); self.assertEqual(process.call_count, 1)
            self.clock.advance(1); self.auto.tick(); self.assertEqual(process.call_count, 2)
        with self.app.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM automation_events').fetchone()[0], 1)

    def test_restart_preserves_unsent_deadline(self):
        self.create()
        with patch.object(self.auto, 'process', side_effect=lambda i: self.auto.save(i, 'waiting', '等待额度')):
            self.auto.tick()
        deadline = json.loads(self.rows()[0]['data'])['retry_at']
        other = Automation(self.app, self.clock, lambda: self.clock.elapsed)
        with patch.object(other, 'loop', return_value=None):
            other.start(); other.thread.join()
        with patch.object(other, 'process') as process:
            other.tick(); process.assert_not_called()
            self.clock.advance(5); other.tick(); process.assert_called_once()
        self.assertEqual(json.loads(self.rows()[0]['data'])['retry_at'], deadline)
        other.close()

    def test_paused_run_remains_paused_when_deadline_passes(self):
        rid = self.create(run_at=(self.clock()+timedelta(seconds=5)).isoformat())
        self.auto.control({'action': 'pause', 'run_id': rid})
        self.clock.advance(60); self.auto.tick(); self.assertEqual(self.rows()[0]['step'], 0)
        self.auto.control({'action': 'resume', 'run_id': rid})
        self.auto.tick(); self.assertEqual(self.rows()[0]['step'], 1)

    def test_bounded_pass_rotates_through_large_batch(self):
        self.create(250)
        for _ in range(3):
            self.clock.advance(1); self.auto.tick()
        rows = self.rows()
        self.assertEqual(len(rows), 250)
        self.assertTrue(all(r['step'] >= 1 for r in rows))
        self.assertTrue(all(r['status'] != 'processing' for r in rows))

    def test_unexpected_failure_isolates_one_item(self):
        self.create(2)
        original = self.auto.process
        bad_id = self.rows()[0]['id']
        def process(item):
            if item['id'] == bad_id:
                raise RuntimeError('Injected execution failure')
            return original(item)
        with patch.object(self.auto, 'process', side_effect=process):
            self.auto.tick()
        rows = self.rows()
        self.assertEqual((rows[0]['status'], rows[1]['step']), ('attention', 1))
        with self.app.store.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM automation_events').fetchone()[0], 2)

    def test_naive_schedule_rejected_without_creating_run(self):
        with self.assertRaises(Problem):
            self.create(run_at='2026-10-03T08:00:00')
        self.assertEqual(self.rows(), [])

    def test_explicit_retry_clears_approval_delay_without_advancing_clock(self):
        self.create()
        with patch.object(self.auto, 'process', side_effect=lambda i: self.auto.save(i, 'approval', '请核对成图')):
            self.auto.tick()
        item = self.rows()[0]
        self.assertIn('retry_at', json.loads(item['data']))
        self.auto.control({'action': 'retry', 'item_id': item['id'], 'revision': item['revision']})
        self.assertNotIn('retry_at', json.loads(self.rows()[0]['data']))
        self.auto.tick()
        self.assertEqual(self.rows()[0]['step'], 1)
