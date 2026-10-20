"""Deterministic triage rules and the instant arrival alert.

Two properties matter most here:

* the rules must keep the misclassifications that were found during
  development fixed — a CityU announcement must not be demoted to "low" just
  because its footer says "unsubscribe", and a lecture schedule must not alert
  as urgent just because it names a weekday;
* the alert is a heads-up, not a second report: it must carry no model output
  and must never be sent for mail outside the sender allow-list.
"""

import unittest
from unittest import mock

from pilot_app import alerts, reports, triage
from pilot_app.security import SecretBox
from pilot_app.service import PilotService
from pilot_app import service as service_mod


def message(subject: str, body: str, sender: str = "teacher@cityu.edu.hk") -> dict:
    return {
        "id": "msg_1", "subject": subject, "body": body,
        "sender_name": sender.split("@")[0], "sender_address": sender,
        "received": "2026-09-13T02:00:00+00:00", "importance": "normal",
    }


class TriageRuleTests(unittest.TestCase):
    def test_cityu_announcement_is_not_demoted_by_an_unsubscribe_footer(self):
        verdict = triage.triage(message(
            "转发: [CAP] Posting digest - 13 Sep 2026 (5pm)",
            "Unsubscribe from these notifications.",
            "noreply_cap275421@cityu.edu.hk",
        ))
        self.assertNotEqual(verdict["category"], triage.LOW,
                            "校方公告不能因为底部有 unsubscribe 就被降级")
        self.assertFalse(verdict["urgent"], "公告不是需要立刻行动的待办")

    def test_lecture_schedule_is_not_urgent(self):
        verdict = triage.triage(message(
            "转发: Tutorial starts from next week: EE2000 Logic Circuit Design",
            "辅导课自下周开始，时间周三 15:00，地点 AC1-214。",
            "student@my.cityu.edu.hk",
        ))
        self.assertFalse(verdict["urgent"], "提到星期几不等于有截止时间")
        self.assertEqual(verdict["category"], triage.ACADEMIC)

    def test_assignment_deadline_is_urgent(self):
        verdict = triage.triage(message(
            "Assignment 2 deadline extended to Friday 23:59",
            "Please submit your report by Friday 23:59 on Canvas.",
        ))
        self.assertTrue(verdict["urgent"])
        self.assertEqual(verdict["category"], triage.ACADEMIC, "分类仍应为学业")
        self.assertEqual(verdict["effective_category"], triage.URGENT)

    def test_payment_reminder_is_urgent(self):
        verdict = triage.triage(message("Tuition payment reminder",
                                        "Your tuition fee payment is due today.",
                                        "finance@cityu.edu.hk"))
        self.assertTrue(verdict["urgent"])

    def test_marketing_is_low_and_never_urgent(self):
        verdict = triage.triage(message("50% off Grammarly Pro",
                                        "Limited time offer, unsubscribe here.",
                                        "hello@mail.grammarly.com"))
        self.assertEqual(verdict["category"], triage.LOW)
        self.assertFalse(verdict["urgent"])

    def test_safety_notice_beats_the_recruitment_keyword(self):
        verdict = triage.triage(message("警惕招聘欺诈！求职季必看的反诈指南",
                                        "请勿轻信高薪招聘信息。", "10000@qq.com"))
        self.assertEqual(verdict["category"], triage.ADMINISTRATIVE,
                         "防诈公告不应因为含「招聘」被归为机会")

    def test_library_maintenance_is_administrative(self):
        verdict = triage.triage(message("图书馆系统维护通知", "本周六系统维护，期间无法借书。",
                                        "library@cityu.edu.hk"))
        self.assertEqual(verdict["category"], triage.ADMINISTRATIVE)

    def test_every_verdict_explains_itself(self):
        for subject, body, sender in (
            ("Assignment due", "Submit by Friday", "teacher@cityu.edu.hk"),
            ("Newsletter", "unsubscribe promo sale", "hello@example.com"),
            ("随便一封信", "没有关键词的内容", "someone@example.com"),
        ):
            verdict = triage.triage(message(subject, body, sender))
            self.assertTrue(verdict["reasons"], subject)
            self.assertIn(verdict["category"], triage.CATEGORY_LABELS)
            self.assertTrue(0 <= verdict["urgency"] <= 10)

    def test_gist_is_extracted_without_a_model(self):
        gist = triage.summarize(message(
            "Assignment 2 deadline extended",
            "Dear student, please submit your report by Friday 23:59 on Canvas. "
            "Late submissions lose marks.",
        ))
        self.assertTrue(gist)
        self.assertLessEqual(len(gist), 170)
        self.assertIn("submit", gist.lower())


class ArrivalAlertTests(unittest.TestCase):
    def test_alert_subject_is_prefixed_and_carries_the_verdict(self):
        alert = alerts.build_alert(message(
            "Assignment 2 deadline extended to Friday 23:59",
            "Please submit your report by Friday 23:59 on Canvas.",
        ))
        self.assertTrue(alert["subject"].startswith("【已收到】"))
        self.assertTrue(alert["urgent"])
        self.assertIn("紧急", alert["text"])
        self.assertIn("deadline", alert["subject"])

    def test_alert_never_claims_to_be_ai_output(self):
        """It must read as a receipt, so the user still gets exactly one report."""
        alert = alerts.build_alert(message("Library maintenance", "系统维护通知。",
                                           "library@cityu.edu.hk"))
        self.assertIn("不含 AI 分析", alert["text"])
        self.assertIn("稍后单独发送", alert["text"])

    def test_alert_html_is_conservative(self):
        alert = alerts.build_alert(message("Assignment due", "Submit by Friday"))
        for forbidden in ("<script", "<style", "<details", "@media", "display:flex",
                          "display:grid", "background-image"):
            self.assertNotIn(forbidden, alert["html"], forbidden)
        self.assertIn('role="presentation"', alert["html"])
        self.assertIn("width:100%", alert["html"])
        self.assertLess(len(alert["html"]), 20_000)

    def test_alert_escapes_untrusted_subject(self):
        alert = alerts.build_alert(message("<img src=x onerror=alert(1)>", "body"))
        self.assertNotIn("<img src=x", alert["html"])
        self.assertIn("&lt;img", alert["html"])

    def test_urgent_only_mode_keeps_routine_mail_quiet(self):
        routine = triage.triage(message("Tutorial schedule", "辅导课下周开始。",
                                       "student@my.cityu.edu.hk"))
        self.assertFalse(alerts.should_alert(routine, urgent_only=True))
        urgent = triage.triage(message("Assignment due", "Submit by Friday"))
        self.assertTrue(alerts.should_alert(urgent, urgent_only=True))


class ArrivalAlertWiringTests(unittest.TestCase):
    def setUp(self):
        from unittest import mock as _mock
        self.db = _mock.MagicMock()
        self.box = SecretBox(b"0" * 32)
        self.service = PilotService(self.db, self.box)

    def _mailbox(self, **extra):
        base = {"id": "mbx", "user_id": "usr", "email": "me@qq.com", "report_to": "me@qq.com"}
        base.update(extra)
        return base

    def test_alert_is_skipped_for_senders_outside_the_allow_list(self):
        with mock.patch.object(service_mod, "ALLOWED_SENDER_DOMAINS", ("cityu.edu.hk",)), \
                mock.patch("pilot_app.service.mailio.send_report") as send:
            sent = self.service._send_arrival_alert(
                self._mailbox(), "pw",
                {"id": "m", "subject": "Promo", "sender_address": "promo@shop.example", "body": b""},
            )
        self.assertFalse(sent)
        send.assert_not_called()

    def test_alert_failure_never_raises(self):
        """A failed heads-up must not stop the report that follows it."""
        with mock.patch.object(service_mod, "ALLOWED_SENDER_DOMAINS", ("cityu.edu.hk",)), \
                mock.patch("pilot_app.service.mailio.send_report", side_effect=RuntimeError("smtp down")):
            sent = self.service._send_arrival_alert(
                self._mailbox(), "pw",
                {"id": "m", "subject": "Assignment due", "sender_address": "t@cityu.edu.hk",
                 "sender_name": "Teacher", "body": "Please submit by Friday."},
            )
        self.assertFalse(sent)

    def test_alert_can_be_switched_off(self):
        with mock.patch.object(service_mod, "ALERT_ON_ARRIVAL", False), \
                mock.patch("pilot_app.service.mailio.send_report") as send:
            sent = self.service._send_arrival_alert(
                self._mailbox(), "pw",
                {"id": "m", "subject": "Assignment due", "sender_address": "t@cityu.edu.hk", "body": "x"},
            )
        self.assertFalse(sent)
        send.assert_not_called()

    def test_alert_is_sent_before_the_slow_analysis(self):
        """The whole point is ordering: heads-up first, model afterwards."""
        order = []
        message = {"id": "msg", "user_id": "usr", "subject": "Assignment due",
                   "sender_name": "Teacher", "sender_address": "teacher@cityu.edu.hk",
                   "received_at": "2026-09-13T02:00:00+00:00", "importance": "normal",
                   "body": self.box.encrypt("Please submit by Friday.", context="message:usr")}
        self.db.mark_message_processing.return_value = True
        self.db.get_mailbox.return_value = {
            "id": "mbx", "user_id": "usr", "email": "me@qq.com", "report_to": "me@qq.com",
            "encrypted_password": self.box.encrypt("pw", context="mailbox:usr"),
        }
        self.db.get_profile.return_value = {"timezone": "Asia/Hong_Kong"}
        self.db.report_for_message.return_value = None
        # A real id, because the generated report is encrypted for it afterwards.
        self.db.create_report.return_value = "rpt_1"

        def fake_analyse(user_id, payload, **kwargs):   # `guard=` 收集器（可选）
            order.append("analyse")
            return "## 1. 重要程度与一句话结论\n- 等级：高\n- 结论：交作业"

        # Patch the *instance* attribute: patching the class method leaves the
        # stub unbound, so the fake would be called with (self, user_id, payload)
        # and raise TypeError — which process_message swallows, making the test
        # pass for the wrong reason.
        with mock.patch.object(service_mod, "ALLOWED_SENDER_DOMAINS", ("cityu.edu.hk",)), \
                mock.patch.object(self.service, "_analyse", side_effect=fake_analyse), \
                mock.patch("pilot_app.service.mailio.send_report") as send:
            self.assertTrue(self.service.process_message(message))

        subjects = [call.args[2] for call in send.call_args_list]
        self.assertEqual(len(subjects), 2, "应各发一封：回执 + 报告")
        self.assertTrue(subjects[0].startswith("【已收到】"), subjects)
        self.assertTrue(subjects[1].startswith("【AI邮件摘要】"), subjects)
        self.assertIn("analyse", order, "分析必须发生在回执之后")

class BriefFirstModeTests(unittest.TestCase):
    """Two-stage delivery: condensed report first, full report right after."""

    def setUp(self):
        from unittest import mock as _mock
        self.db = _mock.MagicMock()
        self.box = SecretBox(b"1" * 32)
        self.service = PilotService(self.db, self.box)
        self.message = {
            "id": "msg", "user_id": "usr", "subject": "Assignment due",
            "sender_name": "Teacher", "sender_address": "teacher@cityu.edu.hk",
            "received_at": "2026-09-13T02:00:00+00:00", "importance": "normal",
            "body": self.box.encrypt("Please submit by Friday.", context="message:usr"),
        }
        self.db.mark_message_processing.return_value = True
        self.db.get_mailbox.return_value = {
            "id": "mbx", "user_id": "usr", "email": "me@qq.com", "report_to": "me@qq.com",
            "encrypted_password": self.box.encrypt("pw", context="mailbox:usr"),
        }
        self.db.get_profile.return_value = {"timezone": "Asia/Hong_Kong"}
        self.db.report_for_message.return_value = None
        self.db.create_report.return_value = "rpt_1"

    def test_brief_then_full_in_order(self):
        order = []
        brief_text = "\n".join([
            "## 1. 重要程度与一句话结论",
            "- 等级：高",
            "- 结论：作业截止时间延后，需要重新提交。",
            "## 2. 必须采取的行动与截止时间",
            "- 周五 23:59 前在 Canvas 提交",
            "## 3. 邮件内容要点",
            "- 提交入口：Canvas",
        ])

        def fake_brief(user_id, payload, **kwargs):     # `guard=` 收集器（可选）
            order.append("brief")
            return brief_text

        def fake_full(user_id, payload, **kwargs):      # `guard=` 收集器（可选）
            order.append("full")
            return "## 1. 重要程度与一句话结论\n- 等级：高\n- 结论：作业延期。\n## 7. English summary\nSubmit."

        with mock.patch.object(service_mod, "BRIEF_FIRST", True), \
                mock.patch.object(self.service, "_analyse_brief", side_effect=fake_brief), \
                mock.patch.object(self.service, "_analyse", side_effect=fake_full), \
                mock.patch.object(self.service, "_send_arrival_alert") as alert, \
                mock.patch("pilot_app.service.mailio.send_report") as send:
            self.assertTrue(self.service.process_message(self.message))

        subjects = [call.args[2] for call in send.call_args_list]
        self.assertEqual(order, ["brief", "full"], "先生成精简，再生成完整")
        self.assertEqual(len(subjects), 2)
        self.assertTrue(subjects[0].startswith("【AI邮件摘要·精简】"), subjects)
        self.assertTrue(subjects[1].startswith("【AI邮件摘要】"), subjects)
        alert.assert_not_called()  # 精简报告本身就是到达通知

    def test_brief_body_has_no_full_sections_and_says_so(self):
        brief_text = "\n".join([
            "## 1. 重要程度与一句话结论",
            "- 等级：高",
            "- 结论：作业截止时间延后，需要重新提交。",
            "## 2. 必须采取的行动与截止时间",
            "- 周五 23:59 前在 Canvas 提交",
            "## 3. 邮件内容要点",
            "- 提交入口：Canvas",
        ])
        self.assertTrue(reports.is_brief(brief_text))
        rendered = reports.render_brief(brief_text, self.message, subject="【AI邮件摘要·精简】x")
        text = rendered["text"]
        self.assertIn("【你要做什么】", text)
        self.assertIn("【邮件内容要点】", text)
        self.assertNotIn("【联网搜索后的建议】", text)
        self.assertNotIn("【风险、未知与推测】", text)
        self.assertIn("完整", text, "必须告知完整报告随后发送")
        # 结论必须来自模型的「结论：」行，而不是后面的要点
        self.assertIn("作业截止时间延后", text)
        self.assertNotIn("结论：入口", text)
        self.assertLess(len(text), 900, "精简版必须真的短")

    def test_mode_is_off_by_default(self):
        self.assertFalse(service_mod.BRIEF_FIRST, "默认必须是单段完整报告，便于回档")

class BriefOnlyModeTests(unittest.TestCase):
    """FULL_REPORT=0: the instant notification is only the condensed report."""

    def setUp(self):
        from unittest import mock as _mock
        self.db = _mock.MagicMock()
        self.box = SecretBox(b"2" * 32)
        self.service = PilotService(self.db, self.box)
        self.message = {
            "id": "msg", "user_id": "usr", "subject": "Assignment due",
            "sender_name": "Teacher", "sender_address": "teacher@cityu.edu.hk",
            "received_at": "2026-09-13T02:00:00+00:00", "importance": "normal",
            "body": self.box.encrypt("Please submit by Friday.", context="message:usr"),
        }
        self.brief = "\n".join([
            "## 1. 重要程度与一句话结论",
            "- 等级：高",
            "- 结论：作业截止延后到周五 23:59。",
            "## 2. 必须采取的行动与截止时间",
            "- 周五 23:59 前在 Canvas 提交",
            "## 3. 邮件内容要点",
            "- 入口：Canvas",
        ])
        self.db.mark_message_processing.return_value = True
        self.db.get_mailbox.return_value = {
            "id": "mbx", "user_id": "usr", "email": "me@qq.com", "report_to": "me@qq.com",
            "encrypted_password": self.box.encrypt("pw", context="mailbox:usr"),
        }
        self.db.get_profile.return_value = {"timezone": "Asia/Hong_Kong"}
        self.db.report_for_message.return_value = None
        self.db.create_report.return_value = "rpt_1"

    def _patches(self):
        return [
            mock.patch.object(service_mod, "BRIEF_FIRST", True),
            mock.patch.object(service_mod, "FULL_REPORT", False),
            mock.patch.object(service_mod, "ALERT_ON_ARRIVAL", False),
            mock.patch.object(self.service, "_send_arrival_alert"),
            mock.patch("pilot_app.service.mailio.send_report"),
        ]

    def test_only_the_brief_is_sent_and_no_full_analysis_runs(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for item in self._patches():
                stack.enter_context(item)
            stack.enter_context(mock.patch.object(self.service, "_analyse_brief", return_value=self.brief))
            analyse = stack.enter_context(mock.patch.object(self.service, "_analyse"))
            sent = stack.enter_context(mock.patch("pilot_app.service.mailio.send_report"))
            self.assertTrue(self.service.process_message(self.message))

        analyse.assert_not_called()
        self.assertEqual(len(sent.call_args_list), 1, "只应发一封")
        # In brief-only mode the brief IS the report, so it uses the normal
        # subject (no "·精简" marker meant to distinguish it from a following
        # full report) and its body is the condensed sections.
        subject = sent.call_args_list[0].args[2]
        self.assertTrue(subject.startswith("【AI邮件摘要】"), subject)
        self.assertNotIn("精简", subject)
        self.assertIn(self.brief, sent.call_args_list[0].args[3])

    def test_only_one_message_is_ever_sent_in_brief_only_mode(self):
        """regression: the brief must not be followed by a duplicate report."""
        from contextlib import ExitStack
        with ExitStack() as stack:
            for item in self._patches():
                stack.enter_context(item)
            stack.enter_context(mock.patch.object(self.service, "_analyse_brief", return_value=self.brief))
            analyse = stack.enter_context(mock.patch.object(self.service, "_analyse"))
            send = stack.enter_context(mock.patch("pilot_app.service.mailio.send_report"))
            subjects = []
            send.side_effect = lambda cfg, pw, subject, md, **kw: subjects.append(subject)
            self.assertTrue(self.service.process_message(self.message))
        analyse.assert_not_called()
        self.assertEqual(len(subjects), 1, f"只发精简时不应有第二封，实际 {subjects}")

    def test_full_mode_still_runs_when_configured(self):
        from contextlib import ExitStack
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(service_mod, "BRIEF_FIRST", True))
            stack.enter_context(mock.patch.object(service_mod, "FULL_REPORT", True))
            stack.enter_context(mock.patch.object(service_mod, "ALERT_ON_ARRIVAL", False))
            stack.enter_context(mock.patch.object(self.service, "_send_arrival_alert"))
            stack.enter_context(mock.patch.object(self.service, "_analyse_brief", return_value=self.brief))
            full = stack.enter_context(mock.patch.object(
                self.service, "_analyse", return_value="## 1. 重要程度\n- 等级：高"))
            sent = stack.enter_context(mock.patch("pilot_app.service.mailio.send_report"))
            self.assertTrue(self.service.process_message(self.message))
        full.assert_called_once()
        self.assertEqual(len(sent.call_args_list), 2, "两段模式应发两封")


if __name__ == "__main__":
    unittest.main()


class PerUserReportModeTests(unittest.TestCase):
    """用户自己的选择必须赢过站点设置（第 4 项）。

    这是那个功能的全部意义：面板上的三选一如果真的改了发出去的东西，就得在这里
    证明。三条性质：

    * 选了 full 的人拿到**完整的七段**，而且**只有一封**（不是精简+完整两封）；
    * 选了 brief 的人只拿精简；
    * 什么都没选的人（''）**跟着站点走**——这是上线时不动任何人邮件的那条保证。
    """

    def setUp(self):
        self.db = mock.MagicMock()
        self.box = SecretBox(b"2" * 32)
        self.service = PilotService(self.db, self.box)
        self.db.mark_message_processing.return_value = True
        self.message = {
            "id": "msg_1", "user_id": "usr", "subject": "作业延期", "sender_name": "教务",
            "sender_address": "teacher@cityu.edu.hk", "received_at": "2026-09-16T00:00:00+00:00",
            "importance": "high",
            "body": self.box.encrypt("Please resubmit by Friday.", context="message:usr"),
        }
        self.db.get_mailbox.return_value = {
            "id": "mbx", "user_id": "usr", "email": "me@qq.com", "report_to": "me@qq.com",
            "encrypted_password": self.box.encrypt("pw", context="mailbox:usr"),
        }
        self.db.report_for_message.return_value = None
        self.db.create_report.return_value = "rpt_1"

    def _run(self, report_mode, *, brief_first, full_report):
        self.db.get_profile.return_value = {"timezone": "Asia/Hong_Kong", "report_mode": report_mode}
        called = []
        brief = "## 1. 重要程度与一句话结论\n- 等级：高\n- 结论：精简结论。\n## 3. 邮件内容要点\n- 要点"
        full = "## 1. 重要程度与一句话结论\n- 等级：高\n- 结论：完整结论。\n## 7. English summary\nSubmit."
        with mock.patch.object(service_mod, "BRIEF_FIRST", brief_first), \
                mock.patch.object(service_mod, "FULL_REPORT", full_report), \
                mock.patch.object(self.service, "_analyse_brief",
                                  side_effect=lambda *a, **k: (called.append("brief"), brief)[1]), \
                mock.patch.object(self.service, "_analyse",
                                  side_effect=lambda *a, **k: (called.append("full"), full)[1]), \
                mock.patch.object(self.service, "_send_arrival_alert"), \
                mock.patch("pilot_app.service.mailio.send_report") as send:
            self.assertTrue(self.service.process_message(self.message))
        return called, [call.args[3] for call in send.call_args_list]

    def test_full_choice_gives_one_full_report_even_when_the_site_is_brief_only(self):
        called, bodies = self._run("full", brief_first=True, full_report=False)
        self.assertEqual(called, ["full"], "选了完整版就不该再生成精简版")
        self.assertEqual(len(bodies), 1, "一封邮件只发一封报告")
        self.assertIn("## 7. English summary", bodies[0])
        self.assertNotIn("精简结论", bodies[0])

    def test_brief_choice_gives_one_brief_report_even_when_the_site_is_full(self):
        called, bodies = self._run("brief", brief_first=False, full_report=True)
        self.assertEqual(called, ["brief"])
        self.assertEqual(len(bodies), 1)
        self.assertIn("精简结论", bodies[0])
        self.assertNotIn("## 7. English summary", bodies[0])

    def test_no_choice_still_follows_the_site(self):
        called, bodies = self._run("", brief_first=True, full_report=False)
        self.assertEqual(called, ["brief"])
        self.assertEqual(len(bodies), 1)

    def test_no_choice_follows_the_site_into_two_stage_mode(self):
        """站点设成两封时，跟着站点的人还是两封——用户不选就不会被改掉。"""
        called, bodies = self._run("", brief_first=True, full_report=True)
        self.assertEqual(called, ["brief", "full"])
        self.assertEqual(len(bodies), 2)

    def test_an_explicit_choice_is_never_two_stage(self):
        """两封那个模式是站点级实验，不给用户选；选了 full 就只发一封。"""
        called, bodies = self._run("full", brief_first=True, full_report=True)
        self.assertEqual(called, ["full"])
        self.assertEqual(len(bodies), 1)
