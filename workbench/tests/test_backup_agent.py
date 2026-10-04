import fcntl
import json
import plistlib
import tempfile
import subprocess
import unittest
import sys
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backup_agent import agent_label, launch_agent_plist, run_backup_schedule_once, sync_launch_agent
from core import Problem
from recovery import Recovery, write_json
from server import App


class BackupAgentTests(unittest.TestCase):
    def test_launch_agent_definition_uses_bundled_one_shot_command_and_no_secrets(self):
        with tempfile.TemporaryDirectory() as root:
            plist = launch_agent_plist(root, '/Applications/Noon Studio.app/Contents/Resources/backend/noon-backend')
            self.assertEqual(plist['Label'], agent_label(root))
            self.assertEqual(plist['ProgramArguments'], [
                '/Applications/Noon Studio.app/Contents/Resources/backend/noon-backend',
                '--backup-schedule-once', '--data', str(Path(root).resolve()),
            ])
            self.assertEqual(plist['StartInterval'], 300)
            self.assertTrue(plist['RunAtLoad'])
            self.assertNotIn('EnvironmentVariables', plist)
            encoded = plistlib.dumps(plist).decode()
            self.assertNotIn('API_KEY', encoded)
            self.assertNotIn('token', encoded.lower())

    def test_enable_and_disable_registers_or_removes_agent_with_private_plist(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as agents:
            runner = Mock(return_value=type('Result', (), {'returncode': 0, 'stdout': '', 'stderr': ''})())
            status = sync_launch_agent(root, True, platform='darwin', frozen=True,
                executable='/Applications/Noon.app/Contents/Resources/backend/noon-backend',
                launch_agents=agents, uid=501, runner=runner)
            target = Path(agents) / f'{agent_label(root)}.plist'
            self.assertEqual(status, 'registered')
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            value = plistlib.loads(target.read_bytes())
            self.assertEqual(value['ProgramArguments'][-3:], ['--backup-schedule-once', '--data', str(Path(root).resolve())])
            self.assertEqual(runner.call_args_list[-1].args[0][:3], ['/bin/launchctl', 'bootstrap', 'gui/501'])

            self.assertEqual(sync_launch_agent(root, False, platform='darwin', frozen=True,
                launch_agents=agents, uid=501, runner=runner), 'disabled')
            self.assertFalse(target.exists())
            self.assertEqual(runner.call_args_list[-1].args[0][:3], ['/bin/launchctl', 'bootout', 'gui/501'])

    def test_repeated_startup_keeps_loaded_agent_without_bootout(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as agents:
            result = type('Result', (), {'returncode': 0, 'stdout': '', 'stderr': ''})()
            install = Mock(return_value=result)
            kwargs = {'platform': 'darwin', 'frozen': True,
                'executable': '/Applications/Noon.app/Contents/Resources/backend/noon-backend',
                'launch_agents': agents, 'uid': 501}
            self.assertEqual(sync_launch_agent(root, True, runner=install, **kwargs), 'registered')
            verify = Mock(return_value=result)
            self.assertEqual(sync_launch_agent(root, True, runner=verify, **kwargs), 'registered')
            verify.assert_called_once_with(['/bin/launchctl', 'print', f'gui/501/{agent_label(root)}'],
                capture_output=True, text=True)

    def test_bootstrap_failure_removes_partially_installed_agent(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as agents:
            runner = Mock(side_effect=[
                type('Result', (), {'returncode': 0, 'stdout': '', 'stderr': ''})(),
                type('Result', (), {'returncode': 5, 'stdout': '', 'stderr': 'permission denied'})(),
            ])
            with self.assertRaisesRegex(Problem, '后台备份服务注册失败'):
                sync_launch_agent(root, True, platform='darwin', frozen=True,
                    executable='/Applications/Noon.app/Contents/Resources/backend/noon-backend',
                    launch_agents=agents, uid=501, runner=runner)
            self.assertFalse((Path(agents) / f'{agent_label(root)}.plist').exists())
            self.assertFalse((Path(agents) / f'{agent_label(root)}.plist.tmp').exists())

    def test_different_data_roots_get_independent_agents_and_disable_is_scoped(self):
        with tempfile.TemporaryDirectory() as root_a, tempfile.TemporaryDirectory() as root_b, tempfile.TemporaryDirectory() as agents:
            ok = type('Result', (), {'returncode': 0, 'stdout': '', 'stderr': ''})()
            runner = Mock(return_value=ok)
            opts = {'platform': 'darwin', 'frozen': True,
                'executable': '/Applications/Noon.app/Contents/Resources/backend/noon-backend',
                'launch_agents': agents, 'uid': 501, 'runner': runner}
            self.assertEqual(sync_launch_agent(root_a, True, **opts), 'registered')
            self.assertEqual(sync_launch_agent(root_b, True, **opts), 'registered')
            target_a = Path(agents) / f'{agent_label(root_a)}.plist'
            target_b = Path(agents) / f'{agent_label(root_b)}.plist'
            self.assertTrue(target_a.exists() and target_b.exists())
            sync_launch_agent(root_b, False, **opts)
            self.assertTrue(target_a.exists())
            self.assertFalse(target_b.exists())
            bootout = runner.call_args.args[0]
            self.assertIn(agent_label(root_b), bootout[-1])
            self.assertNotIn(agent_label(root_a), bootout[-1])

    def test_failed_registration_does_not_leave_schedule_enabled(self):
        with tempfile.TemporaryDirectory() as root:
            recovery = Recovery(root, expected=[])
            with patch('recovery.sync_launch_agent', side_effect=Problem('测试注册失败')):
                with self.assertRaisesRegex(Problem, '测试注册失败'):
                    recovery.configure_schedule({'enabled': True, 'interval_hours': 6, 'keep_count': 2})
            schedule = recovery.state()['schedule']
            self.assertFalse(schedule['enabled'])
            self.assertEqual(schedule['background_agent'], 'registration_failed')
            self.assertIn('测试注册失败', schedule['last_error'])

    def test_due_backup_runner_executes_once_and_skips_while_app_owns_database(self):
        with tempfile.TemporaryDirectory() as root:
            app = App(root)
            try:
                app.store.import_rows([{'title_zh': '合成定时备份商品'}])
                recovery = app.recovery
                schedule = recovery.configure_schedule({'enabled': True, 'interval_hours': 6, 'keep_count': 2})
                schedule.update(next_run_at='2000-01-01T00:00:00+00:00', status='scheduled')
                write_json(recovery.schedule_file, schedule)

                backup_lock = open(Path(root) / 'recovery' / '.backup-agent.lock', 'a')
                fcntl.flock(backup_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertFalse(recovery.tick_schedule())
                self.assertEqual(run_backup_schedule_once(root), {'status': 'skipped', 'reason': 'backup_check_running'})
                fcntl.flock(backup_lock, fcntl.LOCK_UN)
                backup_lock.close()

                app_lock = open(Path(root) / '.server.lock', 'a')
                fcntl.flock(app_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertEqual(run_backup_schedule_once(root), {'status': 'skipped', 'reason': 'application_running'})
                fcntl.flock(app_lock, fcntl.LOCK_UN)
                app_lock.close()

                result = run_backup_schedule_once(root)
                self.assertEqual(result['status'], 'ran')
                self.assertEqual(result['schedule']['status'], 'success')
                self.assertEqual(len([p for p in recovery.archives.glob('*.zip')]), 1)
                self.assertEqual(run_backup_schedule_once(root)['status'], 'not_due')
            finally:
                app.models.codex.close()
                app.visuals.close()
                app.automation.close()
                app.media.close()
                app.executor.shutdown(wait=True, cancel_futures=True)

    def test_bundled_backend_one_shot_cli_runs_due_backup(self):
        with tempfile.TemporaryDirectory() as root:
            app = App(root)
            try:
                app.store.import_rows([{'title_zh': 'CLI 合成备份商品'}])
                schedule = app.recovery.configure_schedule({'enabled': True, 'interval_hours': 6, 'keep_count': 2})
                schedule.update(next_run_at='2000-01-01T00:00:00+00:00', status='scheduled')
                write_json(app.recovery.schedule_file, schedule)
            finally:
                app.models.codex.close()
                app.visuals.close()
                app.automation.close()
                app.media.close()
                app.executor.shutdown(wait=True, cancel_futures=True)

            backend = Path(__file__).resolve().parents[1] / 'server.py'
            result = subprocess.run([sys.executable, str(backend), '--backup-schedule-once', '--data', root],
                capture_output=True, text=True, timeout=30, check=True)
            payload = json.loads(result.stdout)
            self.assertEqual(payload['status'], 'ran')
            self.assertEqual(payload['schedule']['status'], 'success')
            self.assertTrue(list((Path(root) / 'recovery' / 'archives').glob('*.zip')))


if __name__ == '__main__':
    unittest.main()
