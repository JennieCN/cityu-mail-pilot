"""两段式：精简版发出去之后，完整版失败重试时不许再发一封精简版。

原来精简版的发送没有任何持久记录（它发生在 create_report 之前），所以完整版一失败，
下一轮 process_message 看到的 existing 还是 None，于是重新生成、重新发一封精简版。
"""
import datetime as dt
import os
import secrets
import tempfile
import unittest
from unittest import mock

from pilot_app import mailio
from pilot_app import service as service_mod
from pilot_app.database import Database
from pilot_app.security import SecretBox, token_hash


class BriefDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "brief.sqlite3"))
        self.db.initialize()
        self.box = SecretBox(secrets.token_bytes(32))
        self.service = service_mod.PilotService(self.db, self.box)
        code = f"brief-{os.urandom(4).hex()}"
        with self.db.connect() as c:
            c.execute("INSERT INTO invites(code_hash,expires_at) VALUES(?,?)",
                      (token_hash(code), (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()))
        self.email = f"brief-{os.urandom(4).hex()}@example.com"
        self.user = self.db.create_user(self.email, "x" * 60, token_hash(code))
        self.mailbox_id = f"mbx_{self.user['id'][-8:]}"
        with self.db.connect() as c:
            c.execute("""INSERT INTO mailboxes(id,user_id,email,report_to,imap_host,imap_port,
                                              smtp_host,smtp_port,encrypted_password,updated_at)
                         VALUES(?,?,?,?,'imap.example.com',993,'smtp.example.com',465,?,?)""",
                      (self.mailbox_id, self.user["id"], self.email, self.email,
                       self.box.encrypt("authcode-16chars", context=f"mailbox:{self.user['id']}"),
                       dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))
        self.message_id = self.db.insert_message(
            self.user["id"], self.mailbox_id, "1", 7,
            {"subject": "作业截止", "sender_name": "老师", "sender_address": "student@my.cityu.edu.hk",
             "received": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
             "importance": "normal",
             "body": self.box.encrypt("请提交作业。", context=f"message:{self.user['id']}")})
        self.message = next(row for row in self.db.due_messages(limit=50) if row["id"] == self.message_id)

    def tearDown(self):
        self.tmp.cleanup()

    def two_stage(self):
        return mock.patch.object(service_mod, "BRIEF_FIRST", True), \
               mock.patch.object(service_mod, "FULL_REPORT", True)

    def test_the_brief_is_sent_once_even_when_the_full_report_keeps_failing(self):
        subjects = []

        def fake_send(config, password, subject, markdown, **kwargs):
            subjects.append(subject)
            return {"message_id": "x", "refused": {}}

        brief_a, brief_b = self.two_stage()
        with brief_a, brief_b, \
                mock.patch.object(mailio, "send_report", side_effect=fake_send), \
                mock.patch.object(self.service, "_analyse_brief",
                                  return_value="## 1. 重要程度\n- 等级：高\n- 结论：要交作业"), \
                mock.patch.object(self.service, "_analyse", side_effect=RuntimeError("模型超时")):
            self.assertFalse(self.service.process_message(self.message))
            briefs_after_first = [s for s in subjects if "精简" in s]
        with brief_a, brief_b, \
                mock.patch.object(mailio, "send_report", side_effect=fake_send), \
                mock.patch.object(self.service, "_analyse_brief",
                                  return_value="## 1. 重要程度\n- 等级：高\n- 结论：要交作业"), \
                mock.patch.object(self.service, "_analyse", side_effect=RuntimeError("模型还超时")):
            self.service.process_message(self.message)
        briefs = [s for s in subjects if "精简" in s]
        self.assertEqual(len(briefs_after_first), 1, "第一次应该发一封精简版")
        self.assertEqual(len(briefs), 1, "完整版失败重试时不许再发一封精简版")

    def test_a_failed_brief_is_retried(self):
        calls = {"brief": 0}

        def fake_send(config, password, subject, markdown, **kwargs):
            if "精简" in subject:
                calls["brief"] += 1
                if calls["brief"] == 1:
                    raise mailio.MailError("535 认证失败")
            return {"message_id": "x", "refused": {}}

        brief_a, brief_b = self.two_stage()
        with brief_a, brief_b, mock.patch.object(mailio, "send_report", side_effect=fake_send), \
                mock.patch.object(self.service, "_analyse_brief",
                                  return_value="## 1. 重要程度\n- 等级：高\n- 结论：要交作业"), \
                mock.patch.object(self.service, "_analyse", side_effect=RuntimeError("模型超时")):
            self.service.process_message(self.message)
        with self.db.connect() as c:
            flagged = c.execute("SELECT brief_sent_at FROM messages WHERE id=?",
                                (self.message_id,)).fetchone()[0]
        self.assertIsNone(flagged, "没发出去就不能记成已发")
        with brief_a, brief_b, mock.patch.object(mailio, "send_report", side_effect=fake_send), \
                mock.patch.object(self.service, "_analyse_brief",
                                  return_value="## 1. 重要程度\n- 等级：高\n- 结论：要交作业"), \
                mock.patch.object(self.service, "_analyse", side_effect=RuntimeError("模型超时")):
            self.service.process_message(self.message)
        self.assertEqual(calls["brief"], 2, "失败过的那一封要能重试")

    def test_single_stage_never_touches_the_brief_flag(self):
        with mock.patch.object(self.db, "brief_already_sent") as already, \
                mock.patch.object(mailio, "send_report", return_value={"message_id": "x", "refused": {}}), \
                mock.patch.object(self.service, "_analyse", return_value="## 1. 重要程度\n- 等级：高"):
            self.assertTrue(self.service.process_message(self.message))
        already.assert_not_called()


if __name__ == "__main__":
    unittest.main()
