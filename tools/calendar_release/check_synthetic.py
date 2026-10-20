"""Focused release orchestration tests: real temp SQLite, fake system commands."""
import importlib.util
import json
import io
from contextlib import nullcontext
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('release',Path(__file__).with_name('deploy_disabled.py'))
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)


class ReleaseChecks(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        self.patchers=[]
        for name in ('APP','ENV','DB','CONFIG','UNIT','BACKUP','RUNTIME'):
            path=root/name.lower()
            if name=='RUNTIME': path=root/'runtime/venv'
            if name=='BACKUP': path=root/'protected/backup'
            self.patchers.append(patch.object(m,name,path));self.patchers[-1].start()
        self.patchers.append(patch.object(m,'DROPINS',(root/'web.d/disabled.conf',root/'worker.d/disabled.conf')));self.patchers[-1].start()
        m.APP.mkdir();(m.APP/'pilot_app').mkdir();(m.APP/'tools').mkdir()
        (m.APP/'pilot_app/__init__.py').write_text('__version__ = "1.5.43"')
        m.ENV.write_text('SYNTHETIC_SECRET=never-print')
        with sqlite3.connect(m.DB) as c:
            c.execute('CREATE TABLE app_settings(key TEXT PRIMARY KEY,value TEXT)')
            c.execute("INSERT INTO app_settings VALUES('schema_version','existing')")
        m.RUNTIME.mkdir(parents=True)
        (m.RUNTIME.parent/'ready.json').write_text(json.dumps({'lock':m.sha(m.SOURCE/'pilot_app/push-requirements.lock')}))
        self.active={m.WRITERS[0],m.WRITERS[1],m.WRITERS[2],m.WRITERS[3]};self.commands=[];self.fail_migration=False
        def command(*args,**kwargs):
            self.commands.append(args)
            if args[:2]==('systemctl','stop'):self.active.discard(args[2])
            if args[:2]==('systemctl','start'):self.active.add(args[2])
            if args[0]=='runuser':
                if self.fail_migration:raise RuntimeError('injected migration failure')
                self.assertEqual(kwargs['cwd'],m.APP)
                with sqlite3.connect(m.DB) as c:
                    c.executemany('INSERT OR REPLACE INTO app_settings VALUES(?,?)',[('calendar_schema_version','3'),('notification_channel_paused','1')])
        for item in (patch.object(m,'run',side_effect=command),patch.object(m,'active',side_effect=lambda unit:unit in self.active),patch.object(m,'verify_source'),patch.object(m,'verify_live_base'),patch.object(m,'check_health'),patch.object(m.shutil,'chown')):
            self.patchers.append(item);item.start()
    def tearDown(self):
        for p in reversed(self.patchers):p.stop()
        self.tmp.cleanup()
    def test_deploy_disabled_and_repeated_apply_does_not_write(self):
        m.apply();before=m.DB.read_bytes();count=len(self.commands);m.apply()
        self.assertEqual(m.DB.read_bytes(),before);self.assertEqual(len(self.commands),count)
        self.assertEqual(m.load()['phase'],'opened')
        self.assertEqual(m.db_flags(m.DB)['notification_channel_paused'],'1')
        self.assertFalse(any(c[:2]==('systemctl','start') and c[2]==m.SENDER for c in self.commands))
        self.assertFalse(any(c[:2]==('systemctl','enable') for c in self.commands))
        self.assertIn('INFE_PILOT_PUSH_ENABLED=0',(m.CONFIG/'webpush.env').read_text())
        self.assertTrue(all('INFE_PILOT_PUSH_ENABLED=0' in p.read_text() for p in m.DROPINS))
        with self.assertRaisesRegex(RuntimeError,'User writes'):m.rollback_offline()
        m.contain();self.assertEqual(m.DB.read_bytes(),before)
    def test_failed_migration_restores_snapshot_and_old_source(self):
        self.fail_migration=True
        with self.assertRaises(RuntimeError):m.apply()
        self.assertEqual(m.load()['phase'],'backed_up')
        self.assertFalse(any(c[:2]==('systemctl','start') for c in self.commands))
        m.rollback_offline();m.rollback_offline()
        self.assertIn('1.5.43',(m.APP/'pilot_app/__init__.py').read_text())
        self.assertEqual(m.load()['phase'],'rolled_back');self.assertFalse(m.UNIT.exists());self.assertFalse(m.CONFIG.exists())
        self.assertEqual(m.db_flags(m.DB)['calendar_schema_version'],'unknown')
    def test_backup_failure_keeps_old_source_and_stops_writers(self):
        original=m.shutil.copytree
        def fail(source,target,**kwargs):
            if target==m.BACKUP/'tools':raise OSError('synthetic backup failure')
            return original(source,target,**kwargs)
        with patch.object(m.shutil,'copytree',side_effect=fail):
            with self.assertRaises(OSError):m.apply()
        self.assertIn('1.5.43',(m.APP/'pilot_app/__init__.py').read_text())
        self.assertFalse(self.active);m.rollback_offline()
        self.assertTrue(self.active)
    def test_corrupt_snapshot_refuses_restore(self):
        self.fail_migration=True
        with self.assertRaises(RuntimeError):m.apply()
        with (m.BACKUP/'pilot.sqlite3').open('ab') as f:f.write(b'changed')
        with self.assertRaisesRegex(RuntimeError,'Snapshot hash'):m.rollback_offline()
        self.assertFalse(self.active)
    def test_health_failure_after_exposure_prohibits_db_restore(self):
        with patch.object(m,'check_health',side_effect=RuntimeError('health unavailable')):
            with self.assertRaises(RuntimeError):m.apply()
        self.assertEqual(m.load()['phase'],'exposed')
        with self.assertRaisesRegex(RuntimeError,'User writes'):m.rollback_offline()

    def rollback_start_failure(self, nth):
        self.fail_migration=True
        with self.assertRaises(RuntimeError):m.apply()
        original=m.run.side_effect;starts=[]
        def fail_start(*args,**kwargs):
            if args[:2]==('systemctl','start'):
                starts.append(args[2])
                if len(starts)==nth:raise RuntimeError('injected old-service start failure')
            return original(*args,**kwargs)
        m.run.side_effect=fail_start
        with self.assertRaises(RuntimeError):m.rollback_offline()
        self.assertEqual(m.load()['phase'],'restored')
        with sqlite3.connect(m.DB) as c:
            c.execute("INSERT INTO app_settings VALUES('new_user_write','keep')")
        m.run.side_effect=original
        with patch.object(m.shutil,'copy2',wraps=m.shutil.copy2) as copies:
            m.rollback_offline()
            self.assertEqual(copies.call_count,0)
        with sqlite3.connect(m.DB) as c:
            self.assertEqual(c.execute("SELECT value FROM app_settings WHERE key='new_user_write'").fetchone()[0],'keep')
        self.assertTrue(all(u in self.active for u in m.WRITERS[:-1]))
        self.assertEqual(m.load()['phase'],'rolled_back')
        m.check_health.assert_called_with('1.5.43')

    def test_rollback_first_start_failure_retries_services_without_restoring_db(self):
        self.rollback_start_failure(1)

    def test_rollback_partial_start_failure_keeps_new_writes(self):
        self.rollback_start_failure(2)

    def test_prepared_runtime_repeat_does_not_reinstall(self):
        m.prepare_runtime();m.prepare_runtime()
        self.assertFalse(any('install' in c for c in self.commands))
        self.assertFalse(m.BACKUP.exists())

    def test_runtime_failure_does_not_open_maintenance(self):
        with patch.object(m,'run',side_effect=RuntimeError('runtime unavailable')):
            with self.assertRaises(RuntimeError):m.apply()
        self.assertFalse(m.BACKUP.exists());self.assertTrue(self.active)


class HealthChecks(unittest.TestCase):
    def setUp(self):
        self.now=0
        def advance(seconds):self.now+=seconds
        self.clock=patch.object(m.time,'monotonic',side_effect=lambda:self.now);self.clock.start()
        self.sleep=patch.object(m.time,'sleep',side_effect=advance);self.sleep.start()
    def tearDown(self):self.sleep.stop();self.clock.stop()
    @staticmethod
    def response(version='1.5.44'):
        return nullcontext(io.BytesIO(json.dumps({'status':'ok','version':version}).encode()))
    def test_temporary_cold_start_failure_then_exact_version_success(self):
        with patch.object(m,'urlopen',side_effect=[ConnectionRefusedError(),self.response()]) as request:
            m.check_health()
            self.assertEqual(request.call_count,2)
        self.assertEqual(self.now,0.25)
    def test_persistent_failure_stops_at_total_deadline(self):
        with patch.object(m,'urlopen',side_effect=ConnectionRefusedError()) as request:
            with self.assertRaisesRegex(RuntimeError,'deadline'):m.check_health()
            self.assertEqual(request.call_count,80)
        self.assertEqual(self.now,20)
    def test_wrong_version_never_passes_until_deadline(self):
        with patch.object(m,'urlopen',side_effect=lambda *a,**k:self.response('1.5.43')):
            with self.assertRaisesRegex(RuntimeError,'deadline'):m.check_health()
        self.assertEqual(self.now,20)


if __name__=='__main__':unittest.main(verbosity=2)
