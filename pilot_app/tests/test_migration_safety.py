"""缺口 B / C 回归测试：迁移必须校验 UIDVALIDITY，并报告回扫窗口外的空洞。"""

import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pilot_app import mailio
from pilot_app.database import Database
from pilot_app.security import SecretBox, hash_password, token_hash

# 与 test_web.py 一致：固定主密钥，便于构造可解密的邮箱授权码。
os.environ.setdefault("INFE_PILOT_MASTER_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
os.environ.setdefault("INFE_PILOT_COOKIE_SECURE", "0")


class ProbeMailboxTests(unittest.TestCase):
    """probe_mailbox 必须只读、不发信、不动游标地返回三样东西。"""

    def _client(self, uid_validity: bytes = b"1576751763", all_uids=b"10 11 12", recent=b"12"):
        client = mock.MagicMock()
        client.login.return_value = ("OK", [b""])
        client.select.return_value = ("OK", [b"17"])
        client.response.return_value = ("UIDVALIDITY", [uid_validity])
        calls = {"n": 0}
        results = [all_uids, recent]

        def uid(verb, _none, *criteria):
            calls["n"] += 1
            return ("OK", [results[0] if calls["n"] == 1 else results[1]])

        client.uid.side_effect = uid
        return client

    def test_returns_validity_present_and_recent(self):
        with mock.patch.object(mailio.imaplib, "IMAP4_SSL", return_value=self._client()):
            result = mailio.probe_mailbox(
                {"imap_host": "imap.qq.com", "imap_port": 993, "email": "a@qq.com"}, "pw"
            )
        self.assertEqual(result["uid_validity"], "1576751763")
        self.assertEqual(result["present"], [10, 11, 12])
        self.assertEqual(result["recent"], [12])

    def test_never_writes_or_fetches_bodies(self):
        client = self._client()
        with mock.patch.object(mailio.imaplib, "IMAP4_SSL", return_value=client):
            mailio.probe_mailbox({"imap_host": "h", "imap_port": 993, "email": "a@b.c"}, "pw")
        verbs = [call.args[0] for call in client.uid.call_args_list]
        self.assertEqual(verbs, ["search", "search"], "只允许 search，不得 fetch")
        client.select.assert_called_once_with("INBOX", readonly=True)

    def test_connection_failure_is_a_mail_error(self):
        with mock.patch.object(mailio.imaplib, "IMAP4_SSL", side_effect=OSError("boom")):
            with self.assertRaises(mailio.MailError):
                mailio.probe_mailbox({"imap_host": "h", "imap_port": 993, "email": "a@b.c"}, "pw")


class MigrationUidValidityGuardTests(unittest.TestCase):
    """缺口 B：UIDVALIDITY 不一致时必须 fail-closed。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "p.sqlite3")
        self.db.initialize()
        expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with self.db.connect() as c:
            c.execute("INSERT INTO invites(code_hash,expires_at) VALUES(?,?)", (token_hash("i"), expires))
        self.user = self.db.create_user("u@example.com", hash_password("long-enough-password"), token_hash("i"))
        self.box = SecretBox.from_environment()
        self.db.upsert_mailbox(self.user["id"], {
            "email": "u@qq.com", "report_to": "u@qq.com", "imap_host": "imap.qq.com", "imap_port": 993,
            "smtp_host": "smtp.qq.com", "smtp_port": 465,
            "encrypted_password": self.box.encrypt("pw", context=f"mailbox:{self.user['id']}"),
            "enabled": True,
        })
        self.db.set_user_status(self.user["id"], "paused")
        self.state = Path(self.tmp.name) / "state.json"
        self.state.write_text(json.dumps({"processed_uids": ["imap:10", "imap:11"]}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, uid_validity, probe, **flags):
        from pilot_app import manage
        argv = ["manage.py", "migrate-legacy-imap-state", "--user-email", "u@example.com",
                "--state", str(self.state), "--uid-validity", uid_validity]
        if flags.get("apply"):
            argv.append("--apply")
        if flags.get("allow_unverified_uid_validity"):
            argv.append("--allow-unverified-uid-validity")
        patcher = (
            mock.patch.object(mailio, "probe_mailbox", side_effect=probe)
            if isinstance(probe, BaseException)
            else mock.patch.object(mailio, "probe_mailbox", return_value=probe)
        )
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.dict(os.environ, {"INFE_PILOT_DB": str(self.db.path)}), patcher:
            return manage.main()

    def test_mismatched_uidvalidity_is_refused(self):
        probe = {"uid_validity": "999", "present": [10, 11, 12], "recent": [12]}
        with self.assertRaises(SystemExit):
            self._run("123", probe)
        with self.db.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)

    def test_unreadable_mailbox_is_refused(self):
        with self.assertRaises(SystemExit):
            self._run("123", RuntimeError("imap down"))
        with self.db.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)

    def test_matching_uidvalidity_applies_and_reports_holes(self):
        probe = {"uid_validity": "123", "present": [10, 11, 12], "recent": [12]}
        self.assertEqual(self._run("123", probe, apply=True), 0)
        with self.db.connect() as c:
            rows = c.execute("SELECT imap_uid,status FROM messages ORDER BY imap_uid").fetchall()
            mailbox = c.execute("SELECT uid_validity,last_uid FROM mailboxes").fetchone()
        self.assertEqual([(r["imap_uid"], r["status"]) for r in rows], [(10, "sent"), (11, "sent")])
        self.assertEqual((mailbox["uid_validity"], mailbox["last_uid"]), ("123", 0))

    def test_override_flag_allows_mismatch(self):
        probe = {"uid_validity": "999", "present": [10, 11], "recent": [10, 11]}
        self.assertEqual(self._run("123", probe, apply=True, allow_unverified_uid_validity=True), 0)


class ConfigurableLookbackTests(unittest.TestCase):
    """缺口 C：回扫窗口必须可配置，否则窗口外的空洞永远补不回来。"""

    def test_env_override_is_honoured(self):
        import importlib
        from pilot_app import service
        with mock.patch.dict(os.environ, {"INFE_PILOT_INITIAL_LOOKBACK_HOURS": "720"}):
            reloaded = importlib.reload(service)
            self.assertEqual(reloaded.INITIAL_LOOKBACK_HOURS, 720)
        importlib.reload(service)
        self.assertEqual(service.INITIAL_LOOKBACK_HOURS, 48)

    def test_poll_mailbox_passes_the_configured_window(self):
        from pilot_app.service import PilotService
        db = mock.MagicMock()
        box = SecretBox(os.urandom(32))
        svc = PilotService(db, box)
        mailbox = {"id": "m", "user_id": "u", "email": "a@b.c", "imap_host": "h", "imap_port": 993,
                   "encrypted_password": box.encrypt("pw", context="mailbox:u")}
        calls = {}

        def fetch(config, password, *, initial_lookback_hours=48):
            calls["hours"] = initial_lookback_hours
            return "1", [], 0

        with mock.patch.object(mailio, "fetch_new_messages", side_effect=fetch):
            svc.poll_mailbox(mailbox)
        self.assertEqual(calls["hours"], 48)


class UnverifiableImportIsRefusedTests(unittest.TestCase):
    """数据库层也必须 fail-closed，不能靠调用方自觉。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "p.sqlite3")
        self.db.initialize()
        expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with self.db.connect() as c:
            c.execute("INSERT INTO invites(code_hash,expires_at) VALUES(?,?)", (token_hash("i"), expires))
        self.user = self.db.create_user("v@example.com", hash_password("long-enough-password"), token_hash("i"))
        self.db.upsert_mailbox(self.user["id"], {
            "email": "v@qq.com", "report_to": "v@qq.com", "imap_host": "imap.qq.com", "imap_port": 993,
            "smtp_host": "smtp.qq.com", "smtp_port": 465,
            "encrypted_password": b"cipher", "enabled": True,
        })
        self.db.set_user_status(self.user["id"], "paused")

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_verification_is_refused(self):
        with self.assertRaises(TypeError):
            self.db.seed_legacy_processed_uids(self.user["id"], "12345", [1, 2])  # type: ignore[call-arg]

    def test_unknown_verification_value_is_refused(self):
        for bad in ("", "trust me", "yes", None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.db.seed_legacy_processed_uids(self.user["id"], "12345", [1, 2], verification=bad)

    def test_operator_override_is_explicitly_allowed(self):
        result = self.db.seed_legacy_processed_uids(
            self.user["id"], "12345", [1, 2], verification="operator-override"
        )
        self.assertEqual(result["inserted"], 2)


if __name__ == "__main__":
    unittest.main()
