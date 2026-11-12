"""日报也要有「结果不明就不重发」这一档（与即时报告同一套约定）。

原来的顺序是 SMTP → `mark_report_sent`，第二句失败就 `fail_report` + 重抛，
`run_daily_due` 按 DIGEST_RETRY_BACKOFF 再发一整封。用户会在同一晚收到两三封
内容几乎一样的日报。
"""
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pilot_app import mailio
from pilot_app.database import Database
from pilot_app.security import SecretBox
from pilot_app.service import SENDING_MARKER, PilotService


class DailyDeliveryStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "daily.sqlite3")
        self.db.initialize()
        self.box = SecretBox(bytes(range(32)))
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        with self.db.connect() as c:
            c.execute("INSERT INTO users(id,email,password_hash,created_at) VALUES('a','a@e.t','x',?)", (now,))
            c.execute("INSERT INTO profiles(user_id,timezone,daily_time,updated_at) VALUES('a','Asia/Hong_Kong','22:00',?)", (now,))
            c.execute("""INSERT INTO mailboxes(id,user_id,email,report_to,imap_host,imap_port,
                                              smtp_host,smtp_port,encrypted_password,updated_at)
                         VALUES('mbx','a','a@e.t','a@e.t','imap.example.test',993,'smtp.example.test',465,?,?)""",
                      (self.box.encrypt("pw", context="mailbox:a"), now))
            c.execute("""INSERT INTO messages(id,user_id,mailbox_id,uid_validity,imap_uid,subject,
                                              received_at,body,created_at)
                         VALUES('m1','a','mbx','1',1,'作业截止','2026-09-13T02:00:00+00:00',X'00',?)""",
                      (now,))
        self.service = PilotService(self.db, self.box)
        self.user = {"id": "a", "timezone": "Asia/Hong_Kong", "report_to": "a@e.t"}

    def tearDown(self):
        self.tmp.cleanup()

    def daily_row(self):
        with self.db.connect() as c:
            return dict(c.execute("SELECT * FROM reports WHERE kind='daily'").fetchone())

    def counting_send(self, calls):
        def fake(config, password, subject, markdown, **kwargs):
            calls.append(subject)
            return {"message_id": "x", "refused": {}}
        return fake

    def test_a_failed_sent_marking_does_not_resend_the_digest(self):
        calls = []
        with mock.patch.object(mailio, "send_report", side_effect=self.counting_send(calls)), \
                mock.patch.object(self.db, "mark_report_sent", side_effect=RuntimeError("数据库打个嗝")):
            self.assertTrue(self.service.send_daily(self.user, "2026-09-13"))
        self.assertEqual(len(calls), 1, "SMTP 只该被调用一次")
        row = self.daily_row()
        self.assertNotEqual(row["status"], "failed", "退回 failed 会让下一轮再发一整封")
        self.assertTrue(str(row["last_error"]).startswith(SENDING_MARKER))

    def test_the_next_pass_is_stopped_by_the_marker(self):
        calls = []
        with mock.patch.object(mailio, "send_report", side_effect=self.counting_send(calls)), \
                mock.patch.object(self.db, "mark_report_sent", side_effect=RuntimeError("打个嗝")):
            self.service.send_daily(self.user, "2026-09-13")
        with mock.patch.object(mailio, "send_report", side_effect=self.counting_send(calls)):
            self.assertTrue(self.service.send_daily(self.user, "2026-09-13"))
        self.assertEqual(len(calls), 1, "结果不明时不许再发")

    def test_a_real_smtp_failure_is_still_retried(self):
        with mock.patch.object(mailio, "send_report", side_effect=mailio.MailError("535 认证失败")):
            with self.assertRaises(mailio.MailError):
                self.service.send_daily(self.user, "2026-09-13")
        row = self.daily_row()
        self.assertEqual(row["status"], "failed", "真失败要能被看见、能被重试")
        self.assertFalse(str(row["last_error"]).startswith(SENDING_MARKER))
        calls = []
        with mock.patch.object(mailio, "send_report", side_effect=self.counting_send(calls)):
            self.assertTrue(self.service.send_daily(self.user, "2026-09-13"))
        self.assertEqual(len(calls), 1, "失败之后的那一轮才是真正发出去的一次")

    def test_a_successful_digest_clears_the_marker(self):
        calls = []
        with mock.patch.object(mailio, "send_report", side_effect=self.counting_send(calls)):
            self.assertTrue(self.service.send_daily(self.user, "2026-09-13"))
        row = self.daily_row()
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["last_error"], "")
        self.assertIsNotNone(row["coverage_seq"], "覆盖进度要写在这封日报上")
