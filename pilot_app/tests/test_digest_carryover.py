"""日报不能漏掉「发出去之后到午夜」进来的信，也不能把上一封最后一行再发一次。

真实 SQLite，不看 mock 的调用参数——断言的是**下一封日报的正文里到底有哪几封**。
"""
import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pilot_app import mailio
from pilot_app.database import Database
from pilot_app.security import SecretBox
from pilot_app.service import PilotService


class DigestCarryoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "digest.sqlite3")
        self.db.initialize()
        self.box = SecretBox(bytes(range(32)))
        self.user_id = "a"
        with self.db.connect() as c:
            c.execute("INSERT INTO users(id,email,password_hash,created_at) VALUES('a','a@example.test','x',?)",
                      (dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),))
            c.execute("INSERT INTO profiles(user_id,timezone,daily_time,updated_at) VALUES('a','Asia/Hong_Kong','22:00',?)",
                      (dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),))
        self.mailbox_id = "mbx_a"
        with self.db.connect() as c:
            c.execute("""INSERT INTO mailboxes(id,user_id,email,report_to,imap_host,imap_port,
                                              smtp_host,smtp_port,encrypted_password,updated_at)
                         VALUES('mbx_a','a','a@example.test','a@example.test','imap.example.test',993,
                                'smtp.example.test',465,?,?)""",
                      (self.box.encrypt("pw", context="mailbox:a"),
                       dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))
        self.service = PilotService(self.db, self.box)
        self.user = {"id": "a", "timezone": "Asia/Hong_Kong", "report_to": "a@example.test",
                     "daily_time": "22:00"}
        self.uid = 0

    def tearDown(self):
        self.tmp.cleanup()

    def add_mail(self, subject: str, received_at: str) -> None:
        self.uid += 1
        self.db.insert_message("a", self.mailbox_id, "1", self.uid, {
            "subject": subject, "sender_name": "老师", "sender_address": "x@my.cityu.edu.hk",
            "received": received_at, "importance": "normal", "body": b"hi"})

    def digest_text(self, report_date: str) -> str:
        captured = {}

        def fake_send(config, password, subject, markdown, **kwargs):
            captured["text"] = markdown
            return {"message_id": "x", "refused": {}}

        with mock.patch.object(mailio, "send_report", side_effect=fake_send):
            self.assertTrue(self.service.send_daily(self.user, report_date))
        return captured["text"]

    def test_the_last_mail_of_a_digest_is_not_repeated(self):
        self.add_mail("九月十二日的信", "2026-09-12T13:00:00+00:00")   # 21:00 HKT
        self.assertIn("九月十二日的信", self.digest_text("2026-09-12"))
        self.assertNotIn("九月十二日的信", self.digest_text("2026-09-13"),
                         "上一封已经写过的那一条不该在下一封里再来一次")

    def test_mail_that_arrived_after_the_cutoff_is_carried(self):
        self.add_mail("二十二点前的信", "2026-09-12T13:00:00+00:00")
        self.digest_text("2026-09-12")
        self.add_mail("二十二点半的信", "2026-09-12T14:30:00+00:00")   # 22:30 HKT
        text = self.digest_text("2026-09-13")
        self.assertIn("二十二点半的信", text, "发出去之后进来的信必须补进下一封")
        self.assertNotIn("二十二点前的信", text)

    def test_an_empty_digest_still_advances_the_boundary(self):
        self.digest_text("2026-09-12")                       # 当天没有任何信
        self.add_mail("午夜前的信", "2026-09-12T15:00:00+00:00")   # 23:00 HKT
        self.assertIn("午夜前的信", self.digest_text("2026-09-13"),
                      "空日报也必须推进边界，否则这段永远补不上")

    def test_an_old_boundary_is_honoured_instead_of_dropped(self):
        self.add_mail("九月五日的信", "2026-09-05T13:00:00+00:00")
        self.digest_text("2026-09-05")
        self.add_mail("九月六日的信", "2026-09-06T14:30:00+00:00")
        text = self.digest_text("2026-09-13")
        self.assertIn("九月六日的信", text, "边界再老也要照用：宁可多列几行，不能漏")

    def test_same_second_mail_added_later_is_not_lost(self):
        self.add_mail("同一秒的第一封", "2026-09-12T13:00:00+00:00")
        self.digest_text("2026-09-12")
        self.add_mail("同一秒的第二封", "2026-09-12T13:00:00+00:00")
        self.assertIn("同一秒的第二封", self.digest_text("2026-09-13"),
                      "同一时间戳、晚一点入库的那封不能因为边界比较被跳过")
