"""B2：已经发出去的报告不许被「收尾失败」退回失败——那会重复发信。

审查报告（`handoff/REVIEW-2026-09-22.md` 第五条 P2）描述的链路是：

    SMTP 收下 → `mark_report_sent`（报告 = sent）→ `finish_message`（收尾：清正文、清重试）
    最后一步出错 → 统一异常处理 → `fail_message`（邮件放回队列）+ `fail_report`（报告 → failed）
    → 下一轮重试看到 `status != 'sent'`，**再发一封**。

报告作者的模拟结论是「SMTP 被调用两次」，我核了代码：`process_message` 开头**本来就有**
「报告已 sent 就只收尾、不再发」的守卫，所以重复发信的入口只有 `fail_report` 那一条路。
修法就是让 `sent` 成为终态。这个文件用**故障注入**证明它：

* 收尾失败时：SMTP 只被调用一次，报告保持 `sent`，错误记在邮件那一行，收尾在下一次补完；
* 投递本身失败（SMTP 抛错）时：照旧记 failed 并重试——**不能因为怕重复就把真失败吞掉**；
* 重试复用同一个 `Message-ID`：真出现不确定投递时，两封信至少是同一个 id。
"""

import datetime as dt
import json
import os
import secrets
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["INFE_PILOT_DB"] = _TMP + "/sent-terminal.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import database as database_mod  # noqa: E402
from pilot_app import mailio, service as service_mod  # noqa: E402
from pilot_app.security import SecretBox, token_hash  # noqa: E402


class SentIsTerminalTests(unittest.TestCase):
    def setUp(self):
        self.db = database_mod.Database(os.path.join(_TMP, "sent-terminal.sqlite3"))
        self.db.initialize()
        # 每个用例一个新库、但同一个文件名会让 setUp 反复建库；邀请码用随机后缀，
        # 免得同一个进程里的第二个用例撞上唯一约束。
        code = f"sent-terminal-{os.urandom(4).hex()}"
        with self.db.connect() as connection:
            connection.execute("INSERT INTO invites(code_hash,expires_at) VALUES(?,?)",
                               (token_hash(code),
                                (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()))
        self.email = f"sent-{os.urandom(4).hex()}@example.com"
        self.user = self.db.create_user(self.email, "x" * 60, token_hash(code))
        self.box = SecretBox(secrets.token_bytes(32))
        self.service = service_mod.PilotService(self.db, self.box)
        with self.db.connect() as connection:
            connection.execute(
                """INSERT INTO mailboxes(id,user_id,email,report_to,imap_host,imap_port,
                   smtp_host,smtp_port,encrypted_password,updated_at)
                   VALUES(?,?,?,?,'imap.example.com',993,'smtp.example.com',465,?,?)""",
                (f"mbx_{self.user['id'][-8:]}", self.user["id"], self.email, self.email,
                 self.box.encrypt("authcode-16chars", context=f"mailbox:{self.user['id']}"),
                 dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))
        self.mailbox_id = f"mbx_{self.user['id'][-8:]}"
        self.message_id = self.db.insert_message(
            self.user["id"], self.mailbox_id, "1", 99,
            {"subject": "作业截止", "sender_name": "老师", "sender_address": "student@my.cityu.edu.hk",
             "received": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
             "importance": "normal",
             "body": self.box.encrypt("请提交作业。", context=f"message:{self.user['id']}")})
        self.report_body = ("## 1. 重要程度与一句话结论\n- 等级：高\n- 结论：周五前交作业。\n"
                            "## 2. 必须采取的行动与截止时间\n- 提交作业到 Canvas（截止：2026-09-25 23:59）\n")
        self.report_id = self.db.create_report(
            user_id=self.user["id"], message_id=self.message_id, kind="immediate",
            subject="【AI邮件摘要】作业截止", body=self.box.encrypt(
                self.report_body, context=f"report:{self.user['id']}"),
            sent_to=self.email)
        # 取一封「待处理」的邮件：走 `due_messages`（这是 worker 真正用的入口），
        # 这样测的是真实的取件形状，而不是我自己拼的字典。
        self.message = next(row for row in self.db.due_messages(limit=50)
                            if row["id"] == self.message_id)

    # -- 收尾失败：不许退回首发的路 ---------------------------------------

    def test_a_finish_failure_does_not_downgrade_a_sent_report(self):
        sends = []

        def fake_send(config, password, subject, markdown, **kwargs):
            sends.append(kwargs.get("message_id"))
            return {"message_id": kwargs.get("message_id") or "generated@x", "refused": {}}

        with mock.patch.object(mailio, "send_report", side_effect=fake_send), \
                mock.patch.object(self.db, "finish_message", side_effect=RuntimeError("数据库打了个嗝")) as finish:
            first = self.service.process_message(self.message)
        self.assertTrue(first, "投递本身是成功的，返回值不该说失败")
        self.assertEqual(len(sends), 1, "SMTP 只该被调用一次")
        self.assertEqual(self.db.report_for_message(self.message_id)["status"], "sent",
                         "已经发出去的不能被收尾失败退回 failed")
        self.assertTrue(finish.called)

    def test_the_retry_finishes_the_bookkeeping_without_sending_again(self):
        """第二次进来时：报告已是 sent → 只收尾，**一封都不再发**。"""
        sends = []

        def fake_send(config, password, subject, markdown, **kwargs):
            sends.append(1)
            return {"message_id": kwargs.get("message_id") or "generated@x", "refused": {}}

        with mock.patch.object(mailio, "send_report", side_effect=fake_send), \
                mock.patch.object(self.db, "finish_message", side_effect=RuntimeError("数据库打了个嗝")):
            self.service.process_message(self.message)
        self.assertEqual(len(sends), 1)

        with mock.patch.object(mailio, "send_report", side_effect=fake_send):
            self.assertTrue(self.service.process_message(self.message))
        self.assertEqual(len(sends), 1, "重试不该再发一封")
        with self.db.connect() as connection:
            status = connection.execute("SELECT status FROM messages WHERE id=?",
                                        (self.message_id,)).fetchone()[0]
        self.assertEqual(status, "sent", "收尾这一次要做完")

    def test_a_real_delivery_failure_is_still_recorded_and_retried(self):
        """**别把真失败吞掉**：SMTP 自己抛错时，报告记 failed、邮件回队列。"""
        with mock.patch.object(mailio, "send_report",
                               side_effect=mailio.MailError("SMTP 发送失败：535 认证失败")):
            self.assertFalse(self.service.process_message(self.message))
        self.assertEqual(self.db.report_for_message(self.message_id)["status"], "failed")
        with self.db.connect() as connection:
            row = connection.execute("SELECT status, attempts, next_attempt_at FROM messages WHERE id=?",
                                     (self.message_id,)).fetchone()
        self.assertEqual(row[0], "failed")
        self.assertGreaterEqual(row[1], 1)
        self.assertTrue(row[2], "失败要有下一次重试的时间")

    def test_fail_report_refuses_only_the_sent_rows(self):
        """守卫的边界：sent 不动，其他状态照旧改。"""
        self.db.mark_report_sent(self.report_id)
        self.db.fail_report(self.report_id, "收尾失败")
        self.assertEqual(self.db.report_for_message(self.message_id)["status"], "sent")

        other = self.db.create_report(
            user_id=self.user["id"], message_id=self.message_id, kind="daily",
            subject="【CityU 每日简报】",
            body=self.box.encrypt("## 1. 重要程度与一句话结论\n- 等级：低\n",
                                  context=f"report:{self.user['id']}"),
            sent_to=self.email)
        self.db.fail_report(other, "真的失败了")
        with self.db.connect() as connection:
            status = connection.execute("SELECT status FROM reports WHERE id=?", (other,)).fetchone()[0]
        self.assertEqual(status, "failed", "没发出去的那份照旧记失败")


class StableMessageIdTests(unittest.TestCase):
    def test_the_same_report_gets_the_same_id(self):
        first = mailio.stable_message_id("rpt_abc", "me@qq.com")
        second = mailio.stable_message_id("rpt_abc", "me@qq.com")
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("<rpt_abc@"))
        self.assertTrue(first.endswith(">"))

    def test_different_reports_get_different_ids(self):
        self.assertNotEqual(mailio.stable_message_id("rpt_a", "me@qq.com"),
                            mailio.stable_message_id("rpt_b", "me@qq.com"))

    def test_it_does_not_leak_the_hostname(self):
        self.assertIn("@qq.com>", mailio.stable_message_id("rpt_a", "me@qq.com"))
        self.assertNotIn("cityu", mailio.stable_message_id("rpt_a", "me@qq.com").lower())

    def test_send_report_honours_a_given_message_id(self):
        sent = {}

        class FakeSMTP:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def ehlo(self):
                pass

            def starttls(self, context=None):
                pass

            def login(self, user, password):
                pass

            def send_message(self, message):
                sent["id"] = message["Message-ID"]
                sent["subject"] = message["Subject"]
                return {}

        config = {"email": "me@qq.com", "report_to": "me@qq.com", "smtp_host": "smtp.example.com",
                  "smtp_port": 465, "imap_host": "imap.example.com", "imap_port": 993}
        with mock.patch.object(mailio.smtplib, "SMTP_SSL", FakeSMTP):
            mailio.send_report(config, "authcode-16chars", "主题", "正文",
                               message_id="<boss@qq.com>")
        self.assertEqual(sent["id"], "<boss@qq.com>")
