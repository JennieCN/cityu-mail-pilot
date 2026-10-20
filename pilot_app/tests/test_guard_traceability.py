"""护栏说「这条该看一眼」时，我们这一侧到底做了什么 —— **只记一行，交付一个字节都不改**。

2026-09-26：用户的护栏日志说「09-23 至今 1823 条请求里 38 条被标记有问题，其中 37 条是
未修正、原样返回给网站的（`X-Guard: escalate`）」，然后问：「最终是否展示给用户取决于网站侧
是否按 `guard.ok=false` 转人工/追问 —— 这一步不在本机，要确认得看网站的处理逻辑」。

查下来的答案是**不按**：`ok=false` 是**业务升级**不是错误（`providers.Generation.guard`），
报告照发、App 照显示；而在这一轮之前，全仓**只有 `manage check-model` 打印这个结论**，
运营者事后连「哪个人哪封信被升级过」都查不到（护栏日志在**另一台**机器上）。

这个文件钉两件事：

1. **可追溯**：升级过的摘要，journal 里必须有一行带着 `report=`/`user=`/`message=`；
2. **不改变交付**：那一行**只是日志** —— 报告照样建、照样发、状态照样 `sent`。

第 2 条比第 1 条重要：如果哪天有人把这一行改成「升级就不发」，用户会静默地少收一封信，
而那正是这个项目最不想要的失败形态。
"""

from __future__ import annotations  # Python 3.9：3.10 的 `X | None` 注解要它

import datetime as dt
import json
import os
import secrets
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["INFE_PILOT_DB"] = _TMP + "/guard-trace.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import database as database_mod  # noqa: E402
from pilot_app import mailio, providers, service as service_mod  # noqa: E402
from pilot_app.security import SecretBox  # noqa: E402


def _generation(text: str, guard: dict | None) -> providers.Generation:
    return providers.Generation(text=text, sources=[], search_mode="none",
                                usage={"input": 10, "output": 20, "total": 30},
                                finish="stop", guard=guard)


class GuardTraceabilityTests(unittest.TestCase):
    """升级 → 一行日志；交付行为不变。"""

    def setUp(self):
        self.path = os.path.join(_TMP, f"guard-{os.urandom(4).hex()}.sqlite3")
        self.db = database_mod.Database(self.path)
        self.db.initialize()
        self.box = SecretBox(secrets.token_bytes(32))
        self.service = service_mod.PilotService(self.db, self.box)
        self.email = f"guard-{os.urandom(4).hex()}@example.com"
        self.user = self.db.create_user(self.email, "x" * 60, "")
        # 本机那台是**唯一**会带护栏的供应商（`guard_task_for` 只认它）——
        # 用别的供应商的话，护栏字段根本不会出现在请求里。
        self.db.upsert_connection(self.user["id"], {
            "kind": "model", "provider": "local_openai", "model": "ternary-bonsai-2-27b",
            "base_url": "https://127.0.0.1:8443",
            "encrypted_api_key": self.box.encrypt("local-key", context=f"connection:{self.user['id']}:model"),
            "config_json": "{}", "enabled": 1,
        })
        self.mailbox_id = f"mbx_{self.user['id'][-8:]}"
        with self.db.connect() as connection:
            connection.execute(
                """INSERT INTO mailboxes(id,user_id,email,report_to,imap_host,imap_port,
                   smtp_host,smtp_port,encrypted_password,updated_at)
                   VALUES(?,?,?,?,'imap.example.com',993,'smtp.example.com',465,?,?)""",
                (self.mailbox_id, self.user["id"], self.email, self.email,
                 self.box.encrypt("authcode-16chars", context=f"mailbox:{self.user['id']}"),
                 dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))
        self.message_id = self.db.insert_message(
            self.user["id"], self.mailbox_id, "1", 99,
            {"subject": "作业截止", "sender_name": "老师", "sender_address": "student@my.cityu.edu.hk",
             "received": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
             "importance": "normal",
             "body": self.box.encrypt("请在周五前提交作业。", context=f"message:{self.user['id']}")})
        self.message = next(row for row in self.db.due_messages(limit=50)
                            if row["id"] == self.message_id)

    def _run(self, guard: dict | None):
        """跑一次真的 `process_message`，模型换成桩；返回（日志记录, 发信记录）。"""
        sends = []
        report = ("## 1. 重要程度与一句话结论\n- 等级：高\n- 结论：周五前交。\n"
                  "## 2. 必须采取的行动与截止时间\n- 提交作业（截止：2026-09-25 23:59）\n")

        def fake_send(config, password, subject, markdown, **kwargs):
            sends.append(subject)
            return {"message_id": kwargs.get("message_id") or "generated@x", "refused": {}}

        with mock.patch.object(providers, "generate", return_value=_generation(report, guard)), \
                mock.patch.object(mailio, "send_report", side_effect=fake_send), \
                self.assertLogs(level="INFO") as captured:
            delivered = self.service.process_message(self.message)
        return captured.records, sends, delivered

    # -- 1. 可追溯 ---------------------------------------------------------

    def test_an_escalated_report_leaves_one_line_that_names_the_person_and_the_report(self):
        records, sends, delivered = self._run(
            {"ok": False, "issues": ["日期幻觉：原文没有 9 月 25 日"], "retried": True,
             "task": "summarize"})
        self.assertTrue(delivered)
        escalations = [r for r in records if "guard ESCALATE" in r.getMessage()]
        self.assertEqual(len(escalations), 1, "升级必须正好留一行，不多不少")
        line = escalations[0].getMessage()
        report = self.db.report_for_message(self.message_id)
        self.assertIn(report["id"], line, "要能追到 reports 那一行")
        self.assertIn(self.user["id"], line, "要能追到人")
        self.assertIn(self.message_id, line, "要能追到那封信")
        self.assertIn("日期幻觉", line, "问题原文要跟着走，否则这行日志没法判断严重程度")
        self.assertEqual(escalations[0].levelno, 30, "升级是 WARNING，不是 INFO —— 它要能被筛出来")

    def test_the_line_says_out_loud_that_delivery_was_not_changed(self):
        """这行日志的措辞本身就是判据：读的人必须一眼看出「它没有分流」。"""
        records, _, _ = self._run({"ok": False, "issues": ["日期幻觉"], "retried": True})
        line = next(r.getMessage() for r in records if "guard ESCALATE" in r.getMessage())
        self.assertIn("不按它分流", line)
        self.assertIn("docs/guard-escalation-2026-09-26.md", line)

    # -- 2. 交付行为不变（这一条最重要）-----------------------------------

    def test_an_escalated_report_is_still_created_and_still_sent(self):
        """`ok=false` 是业务升级，不是错误：报告照建、照发、状态 sent。"""
        _, sends, _ = self._run({"ok": False, "issues": ["日期幻觉"], "retried": True})
        # 「到达即回执」也是 mailio 的一次发送，所以这里找的是**报告那一封**。
        self.assertTrue(any("AI邮件摘要" in item for item in sends), "升级不许变成「不发」")
        self.assertEqual(self.db.report_for_message(self.message_id)["status"], "sent")

    def test_a_clean_guard_is_not_an_escalation(self):
        records, sends, _ = self._run({"ok": True, "issues": [], "retried": False})
        self.assertEqual([r for r in records if "guard ESCALATE" in r.getMessage()], [])
        self.assertTrue(any("AI邮件摘要" in item for item in sends))
        analysis = next(r.getMessage() for r in records if "analysis for user" in r.getMessage())
        self.assertIn("guard=ok(", analysis)

    def test_no_guard_verdict_at_all_is_not_an_escalation(self):
        """平台那档（DeepSeek）根本没有护栏 —— `none` 与 `ok` 必须分得开。"""
        records, _, _ = self._run(None)
        self.assertEqual([r for r in records if "guard ESCALATE" in r.getMessage()], [])
        analysis = next(r.getMessage() for r in records if "analysis for user" in r.getMessage())
        self.assertIn("guard=none", analysis)

    # -- 3. 摘要行本身也要带上状态 ----------------------------------------

    def test_the_analysis_line_carries_the_verdict_for_every_report(self):
        records, _, _ = self._run({"ok": False, "issues": ["日期幻觉", "遗漏"], "retried": True})
        analysis = next(r.getMessage() for r in records if "analysis for user" in r.getMessage())
        self.assertIn("guard=ESCALATE(issues=2,retried=yes)", analysis)


class GuardNoteTests(unittest.TestCase):
    """`guard_note` / `log_guard_escalation` 的边界：没结论 ≠ 结论是 ok。"""

    def test_none_ok_and_escalate_are_three_different_strings(self):
        self.assertEqual(service_mod.guard_note(None), "none")
        self.assertEqual(service_mod.guard_note({}), "none")
        self.assertEqual(service_mod.guard_note({"ok": True, "issues": []}),
                         "ok(issues=0,retried=no)")
        self.assertEqual(service_mod.guard_note({"ok": False, "issues": ["a", "b"], "retried": True}),
                         "ESCALATE(issues=2,retried=yes)")

    def test_a_verdict_without_a_usable_issue_count_still_prints(self):
        self.assertEqual(service_mod.guard_note({"ok": False}), "ESCALATE(issues=?,retried=no)")

    def test_only_a_real_escalation_is_logged(self):
        # `assertNoLogs` 要 Python 3.10，而本机是 3.9 —— 这里直接盯住那个函数。
        with mock.patch.object(service_mod.logging, "warning") as warned:
            for guard in (None, {}, {"ok": True, "issues": []}):
                service_mod.log_guard_escalation(guard, user_id="usr_1", report_id="rpt_1")
            self.assertEqual(warned.call_count, 0, "没有真升级就不许写 WARNING")
            service_mod.log_guard_escalation({"ok": False, "issues": ["x"]},
                                             user_id="usr_1", report_id="rpt_1", message_id="msg_1")
            self.assertEqual(warned.call_count, 1)
            self.assertIn("rpt_1", str(warned.call_args))


class GuardTraceabilityOnTheAssistPathTests(unittest.TestCase):
    """翻译/总结是**当场交付**给用户的一段模型输出，同样要能追（那条路不落库，用信 + kind）。"""

    def setUp(self):
        self.path = os.path.join(_TMP, f"assist-{os.urandom(4).hex()}.sqlite3")
        self.db = database_mod.Database(self.path)
        self.db.initialize()
        self.box = SecretBox(secrets.token_bytes(32))
        self.service = service_mod.PilotService(self.db, self.box)
        self.user = self.db.create_user(f"assist-{os.urandom(4).hex()}@example.com", "x" * 60, "")
        self.model = {"user_id": self.user["id"], "kind": "model", "enabled": 1,
                      "provider": "local_openai", "model": "ternary-bonsai-2-27b",
                      "base_url": "https://127.0.0.1:8443", "config_json": "{}",
                      "encrypted_api_key": self.box.encrypt("k", context=f"connection:{self.user['id']}:model")}

    def test_an_escalated_translation_names_the_mail_it_came_from(self):
        with mock.patch.object(providers, "generate",
                               return_value=_generation("译文", {"ok": False, "issues": ["遗漏"]})), \
                self.assertLogs(level="WARNING") as captured:
            self.service._assist_call(self.user["id"], "msg_42", self.model, "translate", "hello")
        line = next(r.getMessage() for r in captured.records if "guard ESCALATE" in r.getMessage())
        self.assertIn("msg_42", line)
        self.assertIn("kind=assist-translate", line)

    def test_the_translation_still_comes_back_to_the_user(self):
        """护栏有意见**不等于**告诉用户「翻译失败」——这条路上用户正等着看结果。"""
        with mock.patch.object(providers, "generate",
                               return_value=_generation("译文内容", {"ok": False, "issues": ["遗漏"]})), \
                self.assertLogs(level="WARNING"):
            text, finish, clipped = self.service._assist_call(
                self.user["id"], "msg_42", self.model, "translate", "hello")
        self.assertEqual(text, "译文内容")
        self.assertFalse(clipped)


if __name__ == "__main__":
    unittest.main()
