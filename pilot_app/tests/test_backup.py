"""Tests for backup retention, the offsite push, and the freshness alerts.

The retention tests exist because of a bug that had already fired in production:
retention used to be "the newest seven", and the pre-upgrade backup runs through
the same function, so one afternoon of deploys rotated out every daily copy. The
directory really did hold seven backups from the same afternoon.

The offsite tests talk to a real HTTP server on a loopback port rather than
mocking the request. Signing and encoding a PUT is the part that goes wrong, and
a mock would agree with whatever the code does.
"""

import base64
import datetime as dt
import gzip
import hashlib
import http.server
import io
import json
import os
import pathlib
import sqlite3
import subprocess
import tempfile
import threading
import unittest
import urllib.error
from unittest import mock

from pilot_app import alerting, backup


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.now = dt.datetime(2026, 9, 14, 12, 0, tzinfo=dt.timezone.utc)

    def make(self, name: str, *, days_old: float):
        path = self.dir / name
        path.write_bytes(b"x")
        moment = (self.now - dt.timedelta(days=days_old)).timestamp()
        os.utime(path, (moment, moment))
        return path

    def test_copies_inside_the_window_are_kept(self):
        for index in range(3):
            self.make(f"pilot-2026091{index}T030000Z.sqlite3", days_old=index)
        removed = backup.prune(self.dir, now=self.now)
        self.assertEqual(removed, [])
        self.assertEqual(len(list(self.dir.glob("pilot-*.sqlite3"))), 3)

    def test_copies_older_than_the_window_are_removed(self):
        """Needs more than the floor's worth of fresh copies, or nothing is old."""
        for index in range(backup.BACKUP_KEEP_MIN + 1):
            self.make(f"pilot-202609{10 + index:02d}T030000Z.sqlite3", days_old=index)
        old = self.make("pilot-20260801T030000Z.sqlite3", days_old=44)
        backup.prune(self.dir, now=self.now)
        self.assertFalse(old.exists())
        self.assertEqual(len(list(self.dir.glob("pilot-*.sqlite3"))), backup.BACKUP_KEEP_MIN + 1)

    def test_a_run_of_deploys_cannot_evict_the_daily_chain(self):
        """The exact production incident: same afternoon, seven copies, no dailies.

        Age-based retention keeps the older daily even when newer copies exist,
        which is the whole point of the change.
        """
        daily = self.make("pilot-20260913T032000Z.sqlite3", days_old=1)
        for minute in range(7):
            self.make(f"pilot-20260914T15{minute}00Z.sqlite3", days_old=minute / 1440)
        backup.prune(self.dir, now=self.now)
        self.assertTrue(daily.exists(), "一整天的部署不该把前一天的日报备份挤掉")

    def test_a_few_old_copies_are_never_all_deleted(self):
        """The floor is a floor, not a target: it cannot invent the missing ones.

        A machine that was off for a month must not come back, run one backup and
        delete its only remaining copies for being old.
        """
        for index in range(3):
            self.make(f"pilot-2026010{index}T030000Z.sqlite3", days_old=100 + index)
        removed = backup.prune(self.dir, now=self.now)
        self.assertEqual(removed, [])
        self.assertEqual(len(list(self.dir.glob("pilot-*.sqlite3"))), 3)

    def test_the_ceiling_caps_a_large_history(self):
        for index in range(backup.BACKUP_KEEP_MAX + 5):
            self.make(f"pilot-20260914T{index:06d}Z.sqlite3", days_old=0)
        backup.prune(self.dir, now=self.now)
        self.assertLessEqual(len(list(self.dir.glob("pilot-*.sqlite3"))), backup.BACKUP_KEEP_MAX)

    def test_a_fresh_backup_is_a_readable_database(self):
        source = self.dir / "pilot.sqlite3"
        connection = sqlite3.connect(source)
        connection.execute("CREATE TABLE t(x)")
        connection.execute("INSERT INTO t VALUES(42)")
        connection.commit()
        connection.close()
        destination = backup.create_backup(source, self.dir / "copies")
        copied = sqlite3.connect(destination)
        self.assertEqual(copied.execute("SELECT x FROM t").fetchone()[0], 42)
        copied.close()
        mode = destination.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, oct(mode))


class StubWebDAV:
    """A real HTTP server that accepts PUT, so the request is actually encoded."""

    def __init__(self, *, status=201):
        self.status = status
        self.received: dict[str, bytes] = {}
        self.headers: list[dict[str, str]] = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_PUT(self):  # noqa: N802 - http.server's naming
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                outer.received[self.path] = body
                outer.headers.append({k.lower(): v for k, v in self.headers.items()})
                self.send_response(outer.status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args):  # keep the test output readable
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/dav"
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class OffsitePushTests(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.database = self.dir / "pilot-20260914T030000Z.sqlite3"
        self.database.write_bytes(b"SQLite format 3\x00" + b"payload" * 100)
        self.saved = {name: os.environ.get(name) for name in (
            backup.WEBDAV_URL_ENV, backup.WEBDAV_USER_ENV, backup.WEBDAV_PASSWORD_ENV)}
        for name in self.saved:
            os.environ.pop(name, None)

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_without_a_target_nothing_is_attempted(self):
        result = backup.push_offsite(self.database)
        self.assertFalse(result["configured"])
        self.assertFalse(result["ok"])

    def test_a_configured_target_receives_both_rolling_names(self):
        with StubWebDAV() as stub:
            os.environ[backup.WEBDAV_URL_ENV] = stub.url
            result = backup.push_offsite(self.database,
                                         now=dt.datetime(2026, 9, 14, tzinfo=dt.timezone.utc))
        self.assertTrue(result["ok"], result)
        self.assertIn("/dav/pilot-14.sqlite3.gz", stub.received)
        self.assertIn("/dav/pilot-latest.sqlite3.gz", stub.received)

    def test_what_arrives_is_the_database(self):
        with StubWebDAV() as stub:
            os.environ[backup.WEBDAV_URL_ENV] = stub.url
            backup.push_offsite(self.database)
        uploaded = gzip.decompress(stub.received["/dav/pilot-latest.sqlite3.gz"])
        self.assertEqual(uploaded, self.database.read_bytes())

    def test_credentials_travel_in_the_authorization_header(self):
        with StubWebDAV() as stub:
            os.environ[backup.WEBDAV_URL_ENV] = stub.url
            os.environ[backup.WEBDAV_USER_ENV] = "operator@example.com"
            os.environ[backup.WEBDAV_PASSWORD_ENV] = "app-password-123456"
            backup.push_offsite(self.database)
        header = stub.headers[0]["authorization"]
        self.assertTrue(header.startswith("Basic "))
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode()
        self.assertEqual(decoded, "operator@example.com:app-password-123456")

    def test_a_refused_upload_is_reported_not_raised(self):
        with StubWebDAV(status=507) as stub:
            os.environ[backup.WEBDAV_URL_ENV] = stub.url
            result = backup.push_offsite(self.database)
        self.assertFalse(result["ok"])
        self.assertIn("507", result["error"])

    def test_an_unreachable_host_is_reported_not_raised(self):
        os.environ[backup.WEBDAV_URL_ENV] = "http://127.0.0.1:9/dav"
        result = backup.push_offsite(self.database)
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])

    def test_the_password_is_never_in_the_reported_target(self):
        """Includes the credentials-in-URL form, which is a common provider shape."""
        safe = backup.safe_url("https://user:secret@dav.example.com/x?token=abc")
        self.assertNotIn("secret", safe)
        self.assertNotIn("token=abc", safe)
        self.assertIn("dav.example.com", safe)
        os.environ[backup.WEBDAV_URL_ENV] = "https://user:secret@dav.example.com/x?token=abc"
        result = backup.push_offsite(self.database, dry_run=True)
        self.assertNotIn("secret", json.dumps(result, ensure_ascii=False))
        self.assertNotIn("token=abc", json.dumps(result, ensure_ascii=False))

    def test_credentials_embedded_in_the_url_become_a_header(self):
        """urllib refuses such a URL, so it has to be split before use."""
        os.environ[backup.WEBDAV_URL_ENV] = "https://me:app-pass-123@dav.example.com/dav"
        config = backup.webdav_config()
        self.assertEqual(config["url"], "https://dav.example.com/dav")
        self.assertEqual(config["user"], "me")
        self.assertEqual(config["password"], "app-pass-123")
        with StubWebDAV() as stub:
            os.environ[backup.WEBDAV_URL_ENV] = f"http://me:pw@127.0.0.1:{stub.server.server_address[1]}/dav"
            result = backup.push_offsite(self.database)
        self.assertTrue(result["ok"], result)
        self.assertIn("/dav/pilot-latest.sqlite3.gz", stub.received)

    def test_a_dry_run_reaches_no_server(self):
        with StubWebDAV() as stub:
            os.environ[backup.WEBDAV_URL_ENV] = stub.url
            result = backup.push_offsite(self.database, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(stub.received, {})

    def test_the_state_file_records_what_happened(self):
        with StubWebDAV() as stub:
            os.environ[backup.WEBDAV_URL_ENV] = stub.url
            backup.write_offsite_state(self.dir, backup.push_offsite(self.database))
        state = backup.read_offsite_state(self.dir)
        self.assertTrue(state["ok"])
        self.assertEqual(state["target"], stub.url)

    def test_a_missing_state_file_is_not_an_error(self):
        self.assertIsNone(backup.read_offsite_state(self.dir))


class BackupAlertTests(unittest.TestCase):
    """The sentinel notices the one failure that looks like nothing at all."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())

    def findings(self, **kwargs):
        # A real-looking empty database: `evaluate` iterates both of these, and a
        # bare MagicMock would raise instead of reporting "no users".
        database = mock.MagicMock()
        database.get_setting.return_value = ""
        database.list_users_overview.return_value = []
        database.stalled_setups.return_value = []
        # Injected rather than set through the environment: `evaluate` reads the
        # backup directory now, and a unit test must not depend on what happens
        # to exist at /var/backups on the machine running it.
        return alerting.evaluate(database, disk_percent=1.0, certificate_days=365,
                                 backup_dir=self.dir, **kwargs)

    def make_backup(self, *, hours_old: float):
        path = self.dir / "pilot-20260914T030000Z.sqlite3"
        path.write_bytes(b"x")
        moment = (dt.datetime.now(dt.timezone.utc)
                  - dt.timedelta(hours=hours_old)).timestamp()
        os.utime(path, (moment, moment))
        return path

    def test_no_backups_at_all_is_critical(self):
        keys = {item["key"]: item for item in self.findings()}
        self.assertIn("backup_missing", keys)
        self.assertEqual(keys["backup_missing"]["severity"], "critical")

    def test_a_stale_backup_is_critical_and_explains_the_onfailure_gap(self):
        self.make_backup(hours_old=alerting.ALERT_BACKUP_HOURS + 5)
        keys = {item["key"]: item for item in self.findings()}
        self.assertIn("backup_stale", keys)
        self.assertEqual(keys["backup_stale"]["severity"], "critical")
        self.assertIn("根本没跑", keys["backup_stale"]["detail"])

    def test_a_fresh_backup_produces_no_finding_at_all(self):
        """A healthy check is silent. For a while this one was not.

        A fresh copy used to emit a "备份正常" finding at severity `info`, and it
        behaved exactly like an alert: the detail carried the copy's age in whole
        hours, and `_should_send` deliberately re-mails a finding whose detail
        changed -- so the operator got a cheerful e-mail every hour. The previous
        version of this test only asserted the severity, which is why it passed.
        Silence is the property worth pinning.
        """
        self.make_backup(hours_old=1)
        keys = {item["key"]: item for item in self.findings()}
        self.assertNotIn("backup_stale", keys, "备份正常不该产生一条 finding")
        self.assertNotIn("backup_missing", keys)
        self.assertEqual(keys, {}, "健康时哨兵对备份这件事应该完全沉默")

    def test_a_failed_offsite_push_is_surfaced(self):
        backup.write_offsite_state(self.dir, {
            "configured": True, "ok": False, "error": "URLError: 连接超时",
            "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        })
        keys = {item["key"]: item for item in self.findings()}
        self.assertIn("offsite_failed", keys)
        self.assertIn("连接超时", keys["offsite_failed"]["detail"])

    def test_an_offsite_push_that_stopped_is_surfaced(self):
        long_ago = (dt.datetime.now(dt.timezone.utc)
                    - dt.timedelta(hours=alerting.ALERT_OFFSITE_HOURS + 5))
        backup.write_offsite_state(self.dir, {
            "configured": True, "ok": True, "bytes": 100,
            "at": long_ago.isoformat(timespec="seconds"),
        })
        keys = {item["key"]: item for item in self.findings()}
        self.assertIn("offsite_stale", keys)

    def test_an_unconfigured_offsite_target_is_not_an_alert(self):
        """The self-hosted default is "no third party", and that is not a fault."""
        self.make_backup(hours_old=1)
        keys = {item["key"] for item in self.findings()}
        self.assertNotIn("offsite_failed", keys)
        self.assertNotIn("offsite_stale", keys)

    def test_an_unreadable_backup_directory_says_nothing(self):
        """`/var/backups` is root-only on macOS; the sentinel must survive it.

        Reporting "no backups" there would fire on every developer machine, and
        raising would take the whole sentinel down -- including the checks that
        have nothing to do with backups.
        """
        database = mock.MagicMock()
        database.get_setting.return_value = ""
        database.list_users_overview.return_value = []
        database.stalled_setups.return_value = []
        findings = alerting.evaluate(database, disk_percent=1.0, certificate_days=365,
                                    backup_dir=pathlib.Path("/var/backups/does-not-exist"))
        keys = {item["key"] for item in findings}
        self.assertNotIn("backup_missing", keys)
        self.assertNotIn("backup_stale", keys)

    def test_backup_alerts_do_not_repeat_every_six_hours(self):
        self.assertEqual(alerting._repeat_for("backup_stale"),
                         alerting.ALERT_BACKUP_REPEAT_SECONDS)
        self.assertGreaterEqual(alerting.ALERT_BACKUP_REPEAT_SECONDS, 24 * 3600)


if __name__ == "__main__":
    unittest.main()


class MasterKeyFingerprintTests(unittest.TestCase):
    """Identifying the key without ever showing it.

    The point of the fingerprint is that an operator compares twelve characters
    read aloud against the copy in their password manager. That only works if one
    key has exactly one fingerprint -- the first version hashed whatever form it was
    handed, so the base64 text in `pilot.env` and the decoded bytes inside
    `SecretBox` produced *different* strings for the same key.
    """

    KEY_B64 = base64.urlsafe_b64encode(bytes(range(32))).decode()
    OTHER_B64 = base64.urlsafe_b64encode(bytes(range(1, 33))).decode()

    def test_the_same_key_has_one_fingerprint_whichever_form_it_arrives_in(self):
        from pilot_app.security import SecretBox, key_fingerprint
        from_base64 = key_fingerprint(self.KEY_B64)
        from_bytes = key_fingerprint(SecretBox.from_base64(self.KEY_B64).key)
        from_box = SecretBox.from_base64(self.KEY_B64).fingerprint()
        self.assertEqual(from_base64, from_bytes)
        self.assertEqual(from_base64, from_box)

    def test_different_keys_do_not_collide(self):
        from pilot_app.security import key_fingerprint
        self.assertNotEqual(key_fingerprint(self.KEY_B64), key_fingerprint(self.OTHER_B64))

    def test_the_format_survives_being_copied_off_paper(self):
        from pilot_app.security import key_fingerprint
        value = key_fingerprint(self.KEY_B64)
        self.assertRegex(value, r"^[A-Z2-7]{4}-[A-Z2-7]{4}-[A-Z2-7]{4}$")
        # The digits 0, 1, 8 and 9 cannot appear in base32. That is what makes the
        # letters O and I safe to write down: the digits they are confused with
        # are absent. (O and I themselves DO appear -- the first version of this
        # test asserted otherwise and failed, which is the test doing its job.)
        for absent_digit in "0189":
            self.assertNotIn(absent_digit, value)

    def test_it_does_not_contain_the_key(self):
        from pilot_app.security import key_fingerprint
        value = key_fingerprint(self.KEY_B64)
        self.assertNotIn(self.KEY_B64, value)
        self.assertNotIn(self.KEY_B64[:12], value)

    def test_check_prints_the_fingerprint_of_the_live_key(self):
        from pilot_app.security import key_fingerprint
        directory = pathlib.Path(tempfile.mkdtemp())
        env_file = directory / "pilot.env"
        env_file.write_text(f"INFE_PILOT_MASTER_KEY={self.KEY_B64}\n", encoding="utf-8")
        saved = os.environ.pop("INFE_PILOT_MASTER_KEY", None)
        try:
            buffer = io.StringIO()
            with mock.patch("sys.stdout", buffer):
                backup.check(directory, True, env_file=env_file)
        finally:
            if saved is not None:
                os.environ["INFE_PILOT_MASTER_KEY"] = saved
        text = buffer.getvalue()
        self.assertIn(key_fingerprint(self.KEY_B64), text)
        self.assertNotIn(self.KEY_B64, text, "--check 绝不能打印密钥本身")

    def test_a_missing_or_broken_key_does_not_break_the_report(self):
        """A fingerprint must never be the reason `--check` fails."""
        directory = pathlib.Path(tempfile.mkdtemp())
        env_file = directory / "pilot.env"
        # Deliberately too short, and containing a character base64 cannot use:
        # the release scanner looks for INFE_PILOT_MASTER_KEY=<16+ key characters>
        # and flagged the longer-looking placeholder. A plainly-not-a-key fixture
        # beats exempting the whole file from the scan.
        env_file.write_text("INFE_PILOT_MASTER_KEY=broken!\n", encoding="utf-8")
        saved = os.environ.pop("INFE_PILOT_MASTER_KEY", None)
        try:
            buffer = io.StringIO()
            with mock.patch("sys.stdout", buffer):
                code = backup.check(directory, True, env_file=env_file)
        finally:
            if saved is not None:
                os.environ["INFE_PILOT_MASTER_KEY"] = saved
        self.assertIn("本地备份", buffer.getvalue())
        self.assertEqual(code, 1, "没有备份仍然是需要处理的状态")


class SetBackupTargetScriptTests(unittest.TestCase):
    """The installer, exercised as an operator runs it.

    Same contract as `set_platform_key.sh`: the secret is read with `read -s` so it
    never reaches argv, the shell history or a transcript, the env file is rewritten
    without `sed` (a password containing `/`, `&` or `\\` would break the expression),
    and every other line in the file survives.
    """

    SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "set_backup_target.sh"
    BASE_ENV = ("INFE_PILOT_DB=/var/lib/cityu-mail-pilot/pilot.sqlite3\n"
                "INFE_PILOT_MASTER_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=\n"
                "INFE_PILOT_ADMIN_EMAILS=boss@example.com\n")

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.env_file = self.dir / "pilot.env"
        self.env_file.write_text(self.BASE_ENV, encoding="utf-8")
        self.env_file.chmod(0o600)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_script(self, *args, stdin=""):
        environment = dict(os.environ)
        environment["INFE_PILOT_PREDEPLOY_DIR"] = str(self.dir / "backups")
        environment["LC_CTYPE"] = "C"
        done = subprocess.run(
            ["bash", str(self.SCRIPT), "--env-file", str(self.env_file),
             "--no-verify", *args],
            input=stdin.encode("utf-8"), capture_output=True, env=environment, timeout=60,
        )
        return subprocess.CompletedProcess(
            done.args, done.returncode,
            done.stdout.decode("utf-8", "replace"), done.stderr.decode("utf-8", "replace"))

    def text(self):
        return self.env_file.read_text(encoding="utf-8")

    def test_the_three_variables_are_written_and_nothing_else_changes(self):
        result = self.run_script("--url", "https://app.koofr.net/dav/Koofr",
                                 "--user", "me@example.com", "--stdin",
                                 stdin="app-password-123456\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("INFE_PILOT_BACKUP_WEBDAV_URL=https://app.koofr.net/dav/Koofr", self.text())
        self.assertIn("INFE_PILOT_BACKUP_WEBDAV_USER=me@example.com", self.text())
        self.assertIn("INFE_PILOT_BACKUP_WEBDAV_PASSWORD=app-password-123456", self.text())
        self.assertIn("INFE_PILOT_MASTER_KEY=", self.text(), "主密钥被弄丢了")

    def test_shell_metacharacters_in_the_password_survive_verbatim(self):
        tricky = "app-pass/with&and=eq\\ual"
        result = self.run_script("--url", "https://dav.example.com/x", "--user", "u@e.com",
                                 "--stdin", stdin=tricky + "\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"INFE_PILOT_BACKUP_WEBDAV_PASSWORD={tricky}", self.text())

    def test_the_password_is_never_printed(self):
        secret = "app-password-should-not-be-echoed"
        result = self.run_script("--url", "https://dav.example.com/x", "--user", "u@e.com",
                                 "--stdin", stdin=secret + "\n")
        self.assertNotIn(secret, result.stdout)
        self.assertNotIn(secret, result.stderr)
        self.assertIn(f"密码长度 {len(secret)}", result.stdout)

    def test_a_non_http_url_is_refused(self):
        result = self.run_script("--url", "ftp://dav.example.com/x", "--user", "u@e.com",
                                 "--stdin", stdin="pw-123456\n")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("WEBDAV", self.text())

    def test_an_empty_password_changes_nothing(self):
        result = self.run_script("--url", "https://dav.example.com/x", "--user", "u@e.com",
                                 "--stdin", stdin="\n")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("WEBDAV", self.text())

    def test_an_empty_user_is_refused(self):
        result = self.run_script("--url", "https://dav.example.com/x", "--user", "",
                                 "--stdin", stdin="pw-123456\n")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("WEBDAV", self.text())

    def test_setting_the_target_does_not_touch_the_model_key(self):
        """Both installers rewrite the same file; one must not evict the other."""
        self.run_script("--url", "https://dav.example.com/x", "--user", "u@e.com", "--stdin",
                        stdin="pw-123456\n")
        self.assertIn("INFE_PILOT_MASTER_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                      self.text())

    def test_remove_clears_only_the_three_variables(self):
        self.run_script("--url", "https://dav.example.com/x", "--user", "u@e.com", "--stdin",
                        stdin="pw-123456\n")
        result = self.run_script("--remove")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("WEBDAV", self.text())
        self.assertIn("INFE_PILOT_MASTER_KEY=", self.text())
        self.assertIn("INFE_PILOT_ADMIN_EMAILS=", self.text())

    def test_the_file_stays_private(self):
        self.run_script("--url", "https://dav.example.com/x", "--user", "u@e.com", "--stdin",
                        stdin="pw-123456\n")
        mode = self.env_file.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, oct(mode))

    def test_the_original_file_is_backed_up_first(self):
        self.run_script("--url", "https://dav.example.com/x", "--user", "u@e.com", "--stdin",
                        stdin="pw-123456\n")
        copies = list((self.dir / "backups").glob("pilot-*.env"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_text(encoding="utf-8"), self.BASE_ENV)

    def test_verification_uses_the_real_systemd_unit(self):
        """Hand-running the module would not prove the unit can reach the network.

        The unit runs as `cityumail` under `ProtectSystem=strict` with a restricted
        `ReadWritePaths`; only starting it exercises those constraints.
        """
        source = self.SCRIPT.read_text(encoding="utf-8")
        self.assertIn("systemctl start", source)
        self.assertIn("cityu-mail-pilot-backup.service", source)
        self.assertIn("journalctl", source, "失败时要能看到 WebDAV 的报错原文")


class OffsiteReadBackTests(unittest.TestCase):
    """`--verify-offsite`：把异地那份**读回来**，而不是相信推送时那个 201。

    `ok:true` 只等于「服务器接受了这次 PUT」。文件还在不在、是不是完整的、是不是一个
    数据库而不是一页错误页，只有读回来才知道——这条命令存在，就是为了不必靠人去看网页。
    """

    def setUp(self):
        import gzip as gziplib
        self.gzip = gziplib
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.db_file = self.dir / "pilot.sqlite3"
        connection = sqlite3.connect(self.db_file)
        connection.execute("CREATE TABLE t(x)")
        connection.execute("INSERT INTO t VALUES (1)")
        connection.commit()
        connection.close()
        self.local = self.dir / "pilot-20260916T000000Z.sqlite3"
        self.local.write_bytes(self.db_file.read_bytes())
        self.payload = self.db_file.read_bytes()

    def _verify(self, *, fetch=None, name="pilot-latest.sqlite3.gz", with_target=True):
        env = {"INFE_PILOT_BACKUP_WEBDAV_URL": "https://dav.example.com/box"} if with_target else {}
        with mock.patch.dict(os.environ, env, clear=False):
            if not with_target:
                os.environ.pop("INFE_PILOT_BACKUP_WEBDAV_URL", None)
            return backup.verify_offsite(
                self.dir, name=name,
                fetch=fetch or (lambda url, user, password: self.gzip.compress(self.payload)))

    def test_a_good_remote_copy_is_proved_byte_for_byte(self):
        result = self._verify()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["matches_local"], [self.local.name])
        self.assertEqual(result["integrity"], "ok")
        self.assertEqual(result["sha256"], hashlib.sha256(self.payload).hexdigest())

    def test_it_asks_for_the_deterministic_name(self):
        """远端命名是固定的（`pilot-<日>.sqlite3.gz`），所以核对**不需要 PROPFIND**
        ——那是最可能被服务商关掉的动词，备份任务不该因为它而挂。"""
        seen = []
        self.assertEqual(self._verify(fetch=lambda u, x, y: seen.append(u) or
                                      self.gzip.compress(self.payload))["ok"], True)
        self.assertEqual(seen, ["https://dav.example.com/box/pilot-latest.sqlite3.gz"])

    def test_a_remote_file_that_is_not_ours_is_reported_not_guessed(self):
        other = self.dir / "other.sqlite3"
        connection = sqlite3.connect(other)
        connection.execute("CREATE TABLE t(x)")
        connection.commit()
        connection.close()
        result = self._verify(fetch=lambda u, x, y: self.gzip.compress(other.read_bytes()))
        self.assertFalse(result["ok"])
        self.assertEqual(result["matches_local"], [])
        self.assertIn("与本地现存任何一份都不同", result["error"])

    def test_a_truncated_upload_is_not_a_database(self):
        result = self._verify(fetch=lambda u, x, y: self.gzip.compress(b"<html>500</html>"))
        self.assertFalse(result["ok"])
        self.assertIn("不是 SQLite 数据库", result["error"])
        self.assertNotIn("integrity", result)

    def test_a_body_that_is_not_gzip_at_all_is_reported(self):
        result = self._verify(fetch=lambda u, x, y: b"<html>404</html>")
        self.assertFalse(result["ok"])
        self.assertIn("不是 gzip", result["error"])

    def test_a_fetch_failure_never_raises_and_says_what_happened(self):
        def boom(url, user, password):
            raise urllib.error.URLError("connection refused")
        result = self._verify(fetch=boom)
        self.assertFalse(result["ok"])
        self.assertIn("URLError", result["error"])

    def test_nothing_configured_is_its_own_answer(self):
        result = self._verify(with_target=False)
        self.assertFalse(result["configured"])
        self.assertFalse(result["ok"])

    def test_the_report_never_prints_the_credentials(self):
        """WebDAV 的地址可能自带用户名密码，报表里只允许出现去掉凭据的那一半。"""
        with mock.patch.dict(os.environ, {
                "INFE_PILOT_BACKUP_WEBDAV_URL": "https://someone:s3cret@dav.example.com/box"},
                clear=False):
            result = backup.verify_offsite(self.dir, fetch=lambda u, x, y:
                                           self.gzip.compress(self.payload))
        self.assertEqual(result["target"], "https://dav.example.com/box")
        buffer = io.StringIO()
        with mock.patch("sys.stdout", buffer):
            code = backup.report_offsite(result)
        self.assertEqual(code, 0)
        self.assertNotIn("s3cret", buffer.getvalue())
        self.assertIn("逐字节相同", buffer.getvalue())

    def test_a_bad_read_back_exits_non_zero(self):
        """一条只能靠人读的命令没有价值：它的结论必须能让脚本判红。"""
        result = self._verify(fetch=lambda u, x, y: self.gzip.compress(b"junk"))
        buffer = io.StringIO()
        with mock.patch("sys.stdout", buffer):
            code = backup.report_offsite(result)
        self.assertEqual(code, 1)
        self.assertIn("这次核对没通过", buffer.getvalue())

    def test_it_reads_a_real_http_target_end_to_end(self):
        """真的走一遍 HTTP：起一个小服务端，验 Basic 头、状态码与字节都算数。

        用注入的 fetch 测不到 `_get` 本身——而 `_get` 正是与真实 WebDAV 打交道的那一层
        （认证头拼错、把 404 当成内容读回来，这类错只有真发一次请求才会现形）。
        """
        import base64 as b64
        import http.server
        import threading

        served: dict = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - stdlib naming
                served["path"] = self.path
                served["auth"] = self.headers.get("Authorization", "")
                if self.path != "/box/pilot-latest.sqlite3.gz":
                    self.send_error(404)
                    return
                body = self.server.payload
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # keep the test output clean
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        server.payload = self.gzip.compress(self.payload)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}/box"
        with mock.patch.dict(os.environ, {
                "INFE_PILOT_BACKUP_WEBDAV_URL": url,
                "INFE_PILOT_BACKUP_WEBDAV_USER": "someone",
                "INFE_PILOT_BACKUP_WEBDAV_PASSWORD": "s3cret"}, clear=False):
            result = backup.verify_offsite(self.dir)
        self.assertTrue(result["ok"], result)
        self.assertEqual(served["path"], "/box/pilot-latest.sqlite3.gz")
        self.assertEqual(served["auth"],
                         "Basic " + b64.b64encode(b"someone:s3cret").decode())

        # 404：读回来的是「没有」而不是一页 HTML
        with mock.patch.dict(os.environ, {
                "INFE_PILOT_BACKUP_WEBDAV_URL": url + "/missing",
                "INFE_PILOT_BACKUP_WEBDAV_USER": "someone",
                "INFE_PILOT_BACKUP_WEBDAV_PASSWORD": "s3cret"}, clear=False):
            result = backup.verify_offsite(self.dir)
        self.assertFalse(result["ok"])
        self.assertIn("HTTPError", result["error"])


class OfflineCopyTests(unittest.TestCase):
    """The offline copy is the project's one un-backup-able secret.

    No machine can see the operator's password manager, so what is pinned here is
    the honest half: the record of **when a human last compared**, what happens
    when that record is missing or a year old, and the one thing that *is*
    checkable — whether the copy that was confirmed is still the live key.
    """

    KEY_B64 = base64.urlsafe_b64encode(bytes(range(32))).decode()
    OTHER_B64 = base64.urlsafe_b64encode(bytes(range(1, 33))).decode()
    NOW = dt.datetime(2026, 9, 16, 12, 0, tzinfo=dt.timezone.utc)

    def setUp(self):
        from pilot_app.security import key_fingerprint
        self.dir = pathlib.Path(tempfile.mkdtemp())
        self.env_file = self.dir / "pilot.env"
        self.env_file.write_text(f"INFE_PILOT_MASTER_KEY={self.KEY_B64}\n", encoding="utf-8")
        self.fingerprint = key_fingerprint(self.KEY_B64)
        saved = os.environ.pop("INFE_PILOT_MASTER_KEY", None)
        self.addCleanup(lambda: os.environ.__setitem__("INFE_PILOT_MASTER_KEY", saved)
                        if saved is not None else None)

    def run_check(self, key_copy=None):
        buffer = io.StringIO()
        with mock.patch("sys.stdout", buffer):
            code = backup.check(self.dir, True, now=self.NOW, env_file=self.env_file,
                                key_copy=key_copy)
        return code, buffer.getvalue()

    def test_a_missing_record_says_so_and_asks_for_one(self):
        code, text = self.run_check(None)
        self.assertIn("离线副本：**没有任何核对记录**", text)
        self.assertIn("manage master-key-verified", text)
        self.assertEqual(code, 1, "从未核对是「需要处理」，否则这句提醒永远不痛不痒")

    def test_the_sentence_carries_what_to_compare(self):
        """「请核对」而没有可比的东西，是一条死路。

        这句话有两个消费者：`--check` 印它，管理后台的「巡检」面板也印它——面板那一处
        没有别的行告诉他该拿什么去对，所以指纹必须在这句话里（它是全项目唯一允许出现的
        密钥衍生值）。"""
        _, missing = backup.key_copy_state(None, self.NOW, self.fingerprint)
        self.assertIn(self.fingerprint, missing)
        at = (self.NOW - dt.timedelta(days=400)).isoformat(timespec="seconds")
        _, stale = backup.key_copy_state({"at": at, "fingerprint": self.fingerprint},
                                        self.NOW, self.fingerprint)
        self.assertIn(self.fingerprint, stale)

    def test_without_a_fingerprint_it_says_so_instead_of_nothing(self):
        state, sentence = backup.key_copy_state(None, self.NOW, "")
        self.assertEqual(state, "missing")
        self.assertIn("manage backup --check", sentence)

    def test_a_recent_record_is_reported_with_its_age_and_is_not_a_problem(self):
        at = (self.NOW - dt.timedelta(days=30)).isoformat(timespec="seconds")
        code, text = self.run_check({"at": at, "fingerprint": self.fingerprint})
        self.assertIn("离线副本：上次核对 30 天前", text)
        self.assertNotIn("离线副本：**", text)

    def test_a_record_older_than_a_year_is_a_problem(self):
        at = (self.NOW - dt.timedelta(days=400)).isoformat(timespec="seconds")
        code, text = self.run_check({"at": at, "fingerprint": self.fingerprint})
        self.assertIn("上次核对是 400 天前", text)
        self.assertIn("超过一年", text)
        self.assertEqual(code, 1)

    def test_a_recorded_fingerprint_that_is_no_longer_the_live_key_is_flagged(self):
        from pilot_app.security import key_fingerprint
        at = (self.NOW - dt.timedelta(days=1)).isoformat(timespec="seconds")
        code, text = self.run_check({"at": at, "fingerprint": key_fingerprint(self.OTHER_B64)})
        self.assertIn("记录里核对过的是另一把钥匙", text)
        self.assertEqual(code, 1)

    def test_an_unreadable_record_is_not_silently_treated_as_verified(self):
        code, text = self.run_check({"at": "昨天下午", "fingerprint": self.fingerprint})
        self.assertIn("读不出来", text)
        self.assertEqual(code, 1)

    def test_the_record_is_read_from_the_database_read_only(self):
        from pilot_app import database
        path = self.dir / "pilot.sqlite3"
        db = database.Database(str(path))
        db.initialize()
        self.assertEqual(backup.read_key_copy_check(path), {"at": "", "fingerprint": ""})
        db.set_setting("master_key_verified_at", "2026-09-01T00:00:00+00:00")
        db.set_setting("master_key_verified_fingerprint", self.fingerprint)
        self.assertEqual(backup.read_key_copy_check(path),
                         {"at": "2026-09-01T00:00:00+00:00", "fingerprint": self.fingerprint})

    def test_a_missing_database_is_not_an_error(self):
        # `backup --check` exists to report exactly this kind of broken state, so
        # "读不到" must come back as "没有记录" instead of an exception. The empty
        # dict is the documented answer here -- `check()` uses `.get()` on it, and
        # an empty dict is what "there is nothing to read" looks like.
        self.assertEqual(backup.read_key_copy_check(self.dir / "nope.sqlite3"), {})
        (self.dir / "garbage.sqlite3").write_bytes(b"not a database")
        self.assertEqual(backup.read_key_copy_check(self.dir / "garbage.sqlite3"), {})
