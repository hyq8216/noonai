"""macOS LaunchAgent support for scheduled backups while Noon Studio is closed."""
import fcntl
import hashlib
import os
import plistlib
import subprocess
import sys
from pathlib import Path

from core import Problem

LABEL = 'com.noonstudio.backup'
CHECK_INTERVAL_SECONDS = 300


def agent_label(root):
    root = Path(root).resolve()
    suffix = hashlib.sha256(os.fsencode(root)).hexdigest()[:12]
    return f'{LABEL}.{suffix}'


def launch_agent_plist(root, executable, label=None):
    root = Path(root).resolve()
    label = label or agent_label(root)
    executable = Path(executable).resolve()
    log = root / 'recovery' / 'backup-agent.log'
    return {
        'Label': label,
        'ProgramArguments': [str(executable), '--backup-schedule-once', '--data', str(root)],
        'RunAtLoad': True,
        'StartInterval': CHECK_INTERVAL_SECONDS,
        'ProcessType': 'Background',
        'StandardOutPath': str(log),
        'StandardErrorPath': str(log),
    }


def sync_launch_agent(root, enabled, *, platform=None, frozen=None, executable=None,
                      launch_agents=None, uid=None, runner=subprocess.run):
    """Register the bundled app's one-shot backup check in the current user's GUI session."""
    platform = sys.platform if platform is None else platform
    frozen = bool(getattr(sys, 'frozen', False)) if frozen is None else frozen
    if platform != 'darwin' or not frozen:
        return 'application_only'
    executable = Path(executable or sys.executable).resolve()
    root = Path(root).resolve()
    launch_agents = Path(launch_agents or Path.home() / 'Library' / 'LaunchAgents')
    uid = os.getuid() if uid is None else uid
    domain = f'gui/{uid}'
    label = agent_label(root)
    target = launch_agents / f'{label}.plist'
    launch_agents.mkdir(mode=0o700, parents=True, exist_ok=True)
    launch_agents.chmod(0o700)

    if not enabled:
        runner(['/bin/launchctl', 'bootout', domain, str(target)], capture_output=True, text=True)
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        return 'disabled'

    payload = launch_agent_plist(root, executable, label)
    encoded = plistlib.dumps(payload, sort_keys=True)
    if target.is_file() and not target.is_symlink():
        try:
            current = target.read_bytes()
        except OSError:
            current = b''
        if current == encoded:
            loaded = runner(['/bin/launchctl', 'print', f'{domain}/{label}'], capture_output=True, text=True)
            if loaded.returncode == 0:
                return 'registered'
    temporary = target.with_suffix('.plist.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        runner(['/bin/launchctl', 'bootout', domain, str(target)], capture_output=True, text=True)
        os.replace(temporary, target)
        result = runner(['/bin/launchctl', 'bootstrap', domain, str(target)], capture_output=True, text=True)
        if result.returncode:
            try:
                target.unlink()
            except FileNotFoundError:
                pass
            detail = (result.stderr or result.stdout or 'launchctl bootstrap 失败').strip()[:240]
            raise Problem('macOS 后台备份服务注册失败：' + detail)
        return 'registered'
    except Problem:
        raise
    except OSError as exc:
        raise Problem('无法写入 macOS 后台备份设置：' + str(exc)[:180]) from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _locked(path):
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except BlockingIOError:
        os.close(fd)
        return None


def release_schedule_lock(fd):
    if fd is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def run_backup_schedule_once(root):
    """Run due scheduled backup work only while the foreground app is closed.

    The application owns `.server.lock`; this helper additionally takes a distinct
    lock so overlapping launchd invocations cannot race schedule JSON or retention.
    """
    root = Path(root).resolve()
    if not (root / 'workbench.sqlite3').is_file():
        raise Problem('Noon Studio 资料库不存在，未运行后台备份')
    backup_lock = _locked(root / 'recovery' / '.backup-agent.lock')
    if backup_lock is None:
        return {'status': 'skipped', 'reason': 'backup_check_running'}
    try:
        server_lock = _locked(root / '.server.lock')
        if server_lock is None:
            return {'status': 'skipped', 'reason': 'application_running'}
        # Probe only: do not hold the app startup lock during the potentially long
        # SQLite/media snapshot. A racing app start is safe; tick_schedule shares
        # this process lock, and Recovery verifies asset hashes around the copy.
        release_schedule_lock(server_lock)
        from recovery import Recovery
        recovery = Recovery(root)
        ran = recovery.tick_schedule(lock_held=True)
        schedule = recovery.state()['schedule']
        return {'status': 'ran' if ran else 'not_due', 'schedule': schedule}
    finally:
        release_schedule_lock(backup_lock)
