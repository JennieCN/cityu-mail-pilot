#!/usr/bin/env python3
"""Fixed 1.5.43 -> 1.5.44 offline deployment. Requires a separate production approval.
Only --prepare-runtime, --apply, --rollback-offline or --contain; no sending enable.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from urllib.request import urlopen
import shlex

VERSION = '1.5.44'
SOURCE = Path(__file__).resolve().parents[2]
APP = Path('/opt/cityu-mail-pilot')
ENV = Path('/etc/cityu-mail-pilot/pilot.env')
DB = Path('/var/lib/cityu-mail-pilot/pilot.sqlite3')
CONFIG = Path('/etc/cityu-mail-pilot/webpush')
UNIT = Path('/etc/systemd/system/cityu-mail-pilot-reminder.service')
BACKUP = Path('/var/backups/cityu-calendar-releases/1.5.44')
RUNTIME = Path('/opt/cityu-mail-pilot-sender/1.5.44/venv')
LOCK = Path('/run/lock/cityu-calendar-release.lock')
DROPINS = tuple(Path('/etc/systemd/system') / (u + '.d/calendar-disabled.conf')
                for u in ('cityu-mail-pilot-web.service','cityu-mail-pilot-worker.service'))
WRITERS = ('cityu-mail-pilot-web.service', 'cityu-mail-pilot-worker.service',
           'cityu-mail-pilot-backup.timer', 'cityu-mail-pilot-backup-request.path',
           'cityu-mail-pilot-backup.service')
SENDER = 'cityu-mail-pilot-reminder.service'


def run(*args, cwd=None):
    # Do not expose arbitrary subprocess output, env or exception payloads.
    subprocess.run(args, check=True, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def active(unit):
    return subprocess.run(('systemctl', 'is-active', '--quiet', unit),
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_source():
    if any(p.is_symlink() for p in SOURCE.rglob('*') if '.git' not in p.relative_to(SOURCE).parts):
        raise RuntimeError('Symlink in staged source')
    entries = []
    for line in (SOURCE / 'RELEASE-SHA256SUMS').read_text().splitlines():
        digest, name = line.split('  ', 1)
        path = SOURCE / name
        if path.is_symlink() or not path.resolve().is_relative_to(SOURCE) or sha(path) != digest:
            raise RuntimeError('Source manifest mismatch')
        entries.append(name)
    actual = {str(p.relative_to(SOURCE)) for p in SOURCE.rglob('*')
              if p.is_file() and '.git' not in p.relative_to(SOURCE).parts
              and p.name != 'RELEASE-SHA256SUMS'}
    if actual != set(entries):
        raise RuntimeError('Unexpected source files')


def verify_live_base():
    names = set()
    for line in (SOURCE / 'tools/calendar_release/BASELINE-SHA256SUMS').read_text().splitlines():
        digest, name = line.split('  ', 1)
        names.add(name)
        if (APP / name).is_symlink() or sha(APP / name) != digest:
            raise RuntimeError('Production source drift; coordinate release writer')
    actual = {str(p.relative_to(APP)) for p in (APP / 'pilot_app').rglob('*')
              if p.is_file() and not {'tests','systemd','__pycache__'}.intersection(p.relative_to(APP / 'pilot_app').parts)}
    if actual != names:
        raise RuntimeError('Unexpected production source files')
    values = []
    with ENV.open() as stream:
        for line in stream:
            if line.lstrip().startswith('INFE_PILOT_DB='):
                values.append(shlex.split(line.strip().split('=', 1)[1], comments=True))
    if values != [[str(DB)]]:
        raise RuntimeError('Production database configuration drift')


def assert_disabled():
    if active(SENDER) or (CONFIG / 'sending-approved').exists():
        raise RuntimeError('Sending gate changed; contain and review')
    if (CONFIG / 'webpush.env').read_bytes() != (SOURCE / 'tools/calendar_release/webpush.env.template').read_bytes():
        raise RuntimeError('Notification config drift')
    if UNIT.read_bytes() != (SOURCE / 'tools/calendar_release/cityu-mail-pilot-reminder.service').read_bytes():
        raise RuntimeError('Sender unit drift')
    if any(p.read_text() != '[Service]\nEnvironment=INFE_PILOT_PUSH_ENABLED=0\n' for p in DROPINS):
        raise RuntimeError('Core sending gate drift')
    if db_flags(DB).get('notification_channel_paused') != '1':
        raise RuntimeError('Database notification gate drift')


def check_health():
    with urlopen('http://127.0.0.1:8787/health', timeout=10) as response:
        value = json.loads(response.read(4096))
    if value.get('version') != VERSION or value.get('status') != 'ok':
        raise RuntimeError('New web health not confirmed; contain and review')


def save(state):
    temporary = BACKUP / 'state.tmp'
    with temporary.open('w') as stream:
        stream.write(json.dumps(state, sort_keys=True))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    temporary.replace(BACKUP / 'state.json')
    descriptor = os.open(BACKUP, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def load():
    return json.loads((BACKUP / 'state.json').read_text())


def stop_writers():
    for unit in WRITERS:
        run('systemctl', 'stop', unit)
    if any(active(unit) for unit in WRITERS):
        raise RuntimeError('Writer still active')


def resume(state):
    # A one-shot backup service is not replayed; its timer/path regain scheduling.
    for unit in WRITERS[:-1]:
        if unit in state['active_before']:
            run('systemctl', 'start', unit)


def db_flags(path):
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as c:
        c.execute('PRAGMA query_only=ON')
        c.execute('BEGIN')
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE name IN ('app_settings','calendar_recovery_state')")}
        result = {}
        if 'app_settings' in tables:
            for key in ('calendar_schema_version', 'notification_channel_paused'):
                row = c.execute('SELECT value FROM app_settings WHERE key=?', (key,)).fetchone()
                result[key] = row[0] if row and row[0] in ('0','1','2','3') else 'unknown'
        if 'calendar_recovery_state' in tables:
            row = c.execute('SELECT status FROM calendar_recovery_state WHERE singleton_id=1').fetchone()
            result['recovery'] = row[0] if row and row[0] in ('required','running','complete') else 'unknown'
        return result


def prepare_runtime():
    verify_source()
    if RUNTIME.exists():
        ready = json.loads((RUNTIME.parent / 'ready.json').read_text())
        if ready.get('lock') != sha(SOURCE / 'pilot_app/push-requirements.lock'):
            raise RuntimeError('Runtime already exists with mismatched lock')
        run(str(RUNTIME / 'bin/python'), '-I', '-m', 'pip', 'check')
        run(str(RUNTIME / 'bin/python'), '-I', '-c', 'import pywebpush,py_vapid,http_ece')
        print('runtime already prepared; no reinstall')
        return
    RUNTIME.parent.mkdir(parents=True, mode=0o755, exist_ok=True)
    run('/usr/bin/python3.14', '-I', '-m', 'venv', str(RUNTIME))
    run(str(RUNTIME / 'bin/python'), '-I', '-m', 'pip', '--isolated', 'install',
        '--disable-pip-version-check', '--no-cache-dir', '--require-hashes',
        '-r', str(SOURCE / 'pilot_app/push-requirements.lock'))
    run(str(RUNTIME / 'bin/python'), '-I', '-m', 'pip', 'check')
    (RUNTIME.parent / 'ready.json').write_text(json.dumps({'lock': sha(SOURCE / 'pilot_app/push-requirements.lock')}))


def apply():
    verify_source()
    if BACKUP.exists():
        state = load()
        if state['phase'] == 'opened':
            assert_disabled()
            check_health()
            print('already deployed; sending remains disabled')
            return
        raise RuntimeError('Existing incomplete deployment; inspect and rollback offline')
    if CONFIG.exists() or UNIT.exists() or any(p.exists() for p in DROPINS) or active(SENDER):
        raise RuntimeError('Unexpected existing push configuration or sender')
    if '__version__ = "1.5.43"' not in (APP / 'pilot_app/__init__.py').read_text():
        raise RuntimeError('Live base changed; coordinate release writer')
    verify_live_base()
    ready = json.loads((RUNTIME.parent / 'ready.json').read_text())
    if ready['lock'] != sha(SOURCE / 'pilot_app/push-requirements.lock'):
        raise RuntimeError('Runtime lock mismatch')
    run(str(RUNTIME / 'bin/python'), '-I', '-c', 'import pywebpush,py_vapid,http_ece')
    run(str(APP / '.venv/bin/python'), '-I', '-c', 'import cryptography')
    if not all(active(u) for u in WRITERS[:2]):
        raise RuntimeError('Expected web/worker not active; review baseline')
    if BACKUP.parent.exists():
        info = BACKUP.parent.stat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError('Backup parent not protected by root')
    BACKUP.mkdir(parents=True, mode=0o700)
    state = {'phase': 'stopping', 'active_before': [u for u in WRITERS if active(u)]}
    save(state)
    stop_writers()
    # No writer may restart during this reviewed maintenance window; external
    # deployers/cron jobs must have been frozen by the operator before execution.
    state['phase'] = 'backing_up'; save(state)
    shutil.copytree(APP / 'pilot_app', BACKUP / 'pilot_app', symlinks=True)
    shutil.copy2(ENV, BACKUP / 'pilot.env')
    state['tools_existed'] = (APP / 'tools').exists()
    if state['tools_existed']:
        shutil.copytree(APP / 'tools', BACKUP / 'tools', symlinks=True)
    with sqlite3.connect(DB.as_uri() + '?mode=ro', uri=True) as source:
        with sqlite3.connect(BACKUP / 'pilot.sqlite3') as target:
            source.backup(target)
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RuntimeError('Backup integrity failed')
    (BACKUP / 'pilot.sqlite3').chmod(0o600)
    state['snapshot_flags'] = db_flags(BACKUP / 'pilot.sqlite3')
    state['snapshot_sha256'] = sha(BACKUP / 'pilot.sqlite3')
    # Backup failure leaves old code untouched and all writers stopped.
    state['phase'] = 'backed_up'; save(state)
    shutil.rmtree(APP / 'pilot_app')
    shutil.copytree(SOURCE / 'pilot_app', APP / 'pilot_app')
    for path in (APP / 'pilot_app').rglob('*'):
        path.chmod(0o755 if path.is_dir() else 0o644)
    # Keep unrelated operations tooling intact; install this reviewed helper only.
    target_tools = APP / 'tools/calendar_release'
    if target_tools.exists():
        raise RuntimeError('Unexpected release helper already installed')
    target_tools.parent.mkdir(mode=0o755, exist_ok=True)
    shutil.copytree(SOURCE / 'tools/calendar_release', target_tools)
    for path in target_tools.rglob('*'):
        path.chmod(0o755 if path.is_dir() else 0o644)
    CONFIG.mkdir(mode=0o750)
    shutil.chown(CONFIG, user='root', group='cityumail')
    shutil.copyfile(SOURCE / 'tools/calendar_release/webpush.env.template', CONFIG / 'webpush.env')
    (CONFIG / 'webpush.env').chmod(0o640)
    shutil.chown(CONFIG / 'webpush.env', user='root', group='cityumail')
    shutil.copyfile(SOURCE / 'tools/calendar_release/cityu-mail-pilot-reminder.service', UNIT)
    UNIT.chmod(0o644)
    for dropin in DROPINS:
        dropin.parent.mkdir(parents=True, exist_ok=True)
        dropin.write_text('[Service]\nEnvironment=INFE_PILOT_PUSH_ENABLED=0\n')
        dropin.chmod(0o644)
    run('systemctl', 'daemon-reload')
    run('systemctl', 'disable', SENDER)
    # No start/restart/enable sender occurs anywhere in this script.
    run('runuser', '-u', 'cityumail', '--', str(APP / '.venv/bin/python'), '-m',
        'tools.calendar_release.migrate_disabled', cwd=APP)
    flags = db_flags(DB)
    if flags.get('calendar_schema_version') != '3' or flags.get('notification_channel_paused') != '1':
        raise RuntimeError('Migration did not leave schema3 paused')
    state['migrated_flags'] = flags
    # Record exposure BEFORE starting web/worker. Any crash after here prohibits
    # restoring the old DB, even if a start command later fails.
    state['phase'] = 'exposed'; save(state)
    resume(state)
    check_health()
    assert_disabled()
    state['phase'] = 'opened'; save(state)
    if active(SENDER):
        raise RuntimeError('Unexpected sender active; contain immediately')
    print('deployed disabled; validate health/recovery and UX before enable review')


def rollback_offline():
    state = load()
    if state['phase'] in ('exposed', 'opened', 'contained'):
        raise RuntimeError('User writes may exist; DB restore prohibited; contain and review forward recovery')
    if state['phase'] == 'rolled_back':
        print('already rolled back'); return
    stop_writers()
    if state['phase'] in ('backed_up',):
        if sha(BACKUP / 'pilot.sqlite3') != state['snapshot_sha256']:
            raise RuntimeError('Snapshot hash mismatch')
        if (APP / 'pilot_app').exists(): shutil.rmtree(APP / 'pilot_app')
        shutil.copytree(BACKUP / 'pilot_app', APP / 'pilot_app', symlinks=True)
        if (APP / 'tools').exists(): shutil.rmtree(APP / 'tools')
        if state['tools_existed']:
            shutil.copytree(BACKUP / 'tools', APP / 'tools', symlinks=True)
        for suffix in ('-wal', '-shm', '-journal'):
            Path(str(DB) + suffix).unlink(missing_ok=True)
        shutil.copy2(BACKUP / 'pilot.sqlite3', DB)
        shutil.chown(DB, user='cityumail', group='cityumail')
        DB.chmod(0o600)
        if CONFIG.exists(): shutil.rmtree(CONFIG)
        UNIT.unlink(missing_ok=True)
        for dropin in DROPINS: dropin.unlink(missing_ok=True)
        run('systemctl', 'daemon-reload')
    # stopping/backing_up never changed app/config/DB. No DB restore required.
    state['phase'] = 'rolled_back'; save(state)
    resume(state)
    print('offline rollback finished; verify old health and services')


def contain():
    state = load()
    # Stop writers first. Preserve DB, env, accepted/unknown and backup evidence.
    run('systemctl', 'stop', SENDER)
    stop_writers()
    state['phase'] = 'contained'; save(state)
    print('contained; no DB restore or automatic retries; explicit forward recovery required')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare-runtime','apply','rollback-offline','contain'))
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('Use a separately approved fixed operator execution')
    os.umask(0o077)
    with LOCK.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Runtime contains no credentials and must be readable by cityumail.
        # Keep protected backups/config under the surrounding 0077 umask.
        if args.action == 'prepare-runtime':
            os.umask(0o022)
        {'prepare-runtime': prepare_runtime, 'apply': apply,
         'rollback-offline': rollback_offline, 'contain': contain}[args.action]()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('calendar release failed; keep sending disabled; inspect persisted phase before recovery', file=sys.stderr)
        raise SystemExit(1)
