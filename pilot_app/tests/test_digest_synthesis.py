"""Tests for the optional model-written paragraph in the daily digest.

The whole feature is a bounded addition to something that must not change: the
digest is a deterministic list, one row per processed message, and that is what
makes "no mail is ever silently dropped" true. So most of these tests are about
the boundary -- off by default, off when the setting says off, absent when the
model fails, and never able to remove or rewrite a row.
"""

import datetime as dt
import json
import os
import secrets
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["INFE_PILOT_DB"] = _TMP + "/digest.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"

from pilot_app import digest_synthesis, providers, reports  # noqa: E402
from pilot_app import service as service_mod  # noqa: E402
from pilot_app.database import Database  # noqa: E402
from pilot_app.security import SecretBox  # noqa: E402
from pilot_app.service import PilotService  # noqa: E402


def empty_digest() -> dict:
    return {
        "date": "2026-09-15", "generated_at": "2026-09-15T14:00:00+00:00",
        "tasks": [], "next_deadline": "", "skipped": [], "items": [],
        "sections": {name: [] for name in reports.CATEGORY_ORDER},
        "metrics": {"total": 0, "merged_total": 0, "actionable": 0, "failed": 0,
                    "low_priority": 0, "without_sources": 0, "duplicates": 0, "skipped": 0},
    }


class ToggleTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(os.environ["INFE_PILOT_DB"])
        self.db.initialize()
        self.db.set_setting(digest_synthesis.SETTING_KEY, "")

    def tearDown(self):
        os.environ.pop(digest_synthesis.ENABLED_ENV, None)

    def test_off_by_default(self):
        """A fresh install must not start spending its owner's model budget."""
        self.assertFalse(digest_synthesis.enabled(self.db))

    def test_the_installation_default_can_turn_it_on(self):
        os.environ[digest_synthesis.ENABLED_ENV] = "1"
        self.assertTrue(digest_synthesis.enabled_from_environment())
        self.assertTrue(digest_synthesis.enabled(self.db))

    def test_the_console_setting_wins_over_the_installation_default(self):
        os.environ[digest_synthesis.ENABLED_ENV] = "1"
        digest_synthesis.set_enabled(self.db, False, actor="boss@example.com")
        self.assertFalse(digest_synthesis.enabled(self.db), "控制台关掉之后环境变量不该把它顶回来")
        digest_synthesis.set_enabled(self.db, True, actor="boss@example.com")
        self.assertTrue(digest_synthesis.enabled(self.db))

    def test_off_is_spelled_several_ways(self):
        for value in ("0", "false", "False", "no", ""):
            os.environ[digest_synthesis.ENABLED_ENV] = value
            self.assertFalse(digest_synthesis.enabled_from_environment(), value)


class PromptTests(unittest.TestCase):
    def test_the_prompt_carries_the_items_and_forbids_invention(self):
        digest = empty_digest()
        digest["items"] = [
            {"subject": "COMP3411 作业 2", "sender": "课程组", "deadline": "9月19日"},
            {"subject": "图书馆逾期提醒", "sender": "图书馆", "deadline": ""},
        ]
        digest["metrics"]["total"] = 2
        prompt = digest_synthesis.build_prompt(digest)
        self.assertIn("COMP3411 作业 2", prompt)
        self.assertIn("图书馆逾期提醒", prompt)
        self.assertIn("不要推测", prompt)
        self.assertIn("2–3 句话", prompt)

    def test_the_prompt_adds_no_mail_body(self):
        """The digest is already derived; nothing new may leave the machine."""
        digest = empty_digest()
        digest["items"] = [{"subject": "主题", "sender": "某人", "deadline": "",
                            "conclusion": "CANARY-BODY-TEXT"}]
        prompt = digest_synthesis.build_prompt(digest)
        self.assertNotIn("CANARY-BODY-TEXT", prompt)


class CleanTests(unittest.TestCase):
    def test_markdown_headings_cannot_leak_into_a_numbered_report(self):
        self.assertEqual(digest_synthesis.clean("## 9. 我加的一节"), "9. 我加的一节")

    def test_long_output_is_capped(self):
        self.assertLessEqual(len(digest_synthesis.clean("字" * 5000)), 600)

    def test_blank_lines_are_dropped(self):
        self.assertEqual(digest_synthesis.clean("\n\n 一段话 \n\n"), "一段话")


class RenderTests(unittest.TestCase):
    def test_the_three_renderers_show_it_only_when_it_is_there(self):
        digest = empty_digest()
        markdown = reports.digest_markdown(digest)
        text = reports.render_digest_text(digest, subject="每日简报")
        rendered = reports.render_digest(digest, subject="每日简报")
        for page in (markdown, text, rendered["html"], rendered["text"]):
            self.assertNotIn(reports.SYNTHESIS_HEADING, page, "关掉时一个字都不该有")

        digest_synthesis.attach(digest, "三门课都有作业。")
        markdown = reports.digest_markdown(digest)
        text = reports.render_digest_text(digest, subject="每日简报")
        rendered = reports.render_digest(digest, subject="每日简报")
        for page in (markdown, text, rendered["html"], rendered["text"]):
            self.assertIn(reports.SYNTHESIS_HEADING, page)
            self.assertIn("三门课都有作业。", page)

    def test_it_never_renumbers_or_replaces_a_section(self):
        digest = empty_digest()
        digest["sections"]["urgent"] = [{
            "subject": "缴费", "sender": "财务处", "sender_address": "fees@example.com",
            "received": "", "received_display": "9月15日", "status": "sent", "last_error": "",
            "priority": reports.PRIORITY_UNKNOWN, "priority_label": "未知",
            "conclusion": "需要缴费。", "actions": [], "deadline": "", "sources": [],
            "relevance": "", "search_unavailable": False, "duplicates": 0, "category": "urgent",
        }]
        before = reports.digest_markdown(digest)
        digest_synthesis.attach(digest, "三门课都有作业。")
        after = reports.digest_markdown(digest)
        self.assertIn("缴费", after, "综览不能把任何一行挤掉")
        self.assertIn("## 2. 紧急待办", after, "章节编号不该被重排")
        self.assertLess(len(before), len(after))


class SynthesizeTests(unittest.TestCase):
    def setUp(self):
        self.db = mock.MagicMock()
        self.service = PilotService(self.db, SecretBox(secrets.token_bytes(32)))

    def _user(self):
        return {"id": "usr_1", "timezone": "Asia/Hong_Kong"}

    def test_a_provider_failure_is_a_missing_paragraph_not_a_failure(self):
        """"The model broke" must reach this module as a broken model, not as a
        crash somewhere upstream that happens to be caught by the same except."""
        self.db.key_circuit_open.return_value = False
        self.service.model_connection = mock.Mock(return_value={
            "provider": "deepseek", "model": "deepseek-flash", "base_url": "",
            "config_json": "{}", "encrypted_api_key": b"x"})
        self.service.connection_key = mock.Mock(return_value="k")
        with mock.patch.object(providers, "generate",
                               side_effect=providers.ProviderError("boom")) as called:
            text, usage, connection = digest_synthesis.synthesize(self.service, self._user(), empty_digest())
        called.assert_called_once()
        self.assertEqual(text, "")
        self.assertEqual(usage, {})
        self.assertIsNone(connection)

    def test_a_dry_platform_account_is_not_poked_either(self):
        """日报综览走的是同一个钱闸（2026-09-24 补）：账上没钱就不该再花一次。"""
        self.db.key_circuit_open.return_value = False
        self.service.model_connection = mock.Mock(return_value={
            "provider": "deepseek", "model": "deepseek-flash", "base_url": "",
            "platform": True, "config_json": "{}", "encrypted_api_key": b"x"})
        self.service.connection_key = mock.Mock(return_value="k")
        self.service.require_budget_for = mock.Mock(
            side_effect=providers.ProviderError("代付的模型额度已经用尽"))
        with mock.patch.object(providers, "generate") as called:
            text, usage, connection = digest_synthesis.synthesize(
                self.service, self._user(), empty_digest())
        called.assert_not_called()
        self.assertEqual(text, "")
        self.assertIsNone(connection)

    def test_an_open_circuit_is_not_poked_again(self):
        self.db.key_circuit_open.return_value = True
        self.service.model_connection = mock.Mock()
        with mock.patch.object(providers, "generate") as called:
            text, _, _ = digest_synthesis.synthesize(self.service, self._user(), empty_digest())
        self.assertEqual(text, "")
        called.assert_not_called()
        self.service.model_connection.assert_not_called()

    def test_no_connection_means_no_call(self):
        self.db.key_circuit_open.return_value = False
        self.service.model_connection = mock.Mock(return_value=None)
        with mock.patch.object(providers, "generate") as called:
            text, _, _ = digest_synthesis.synthesize(self.service, self._user(), empty_digest())
        self.assertEqual(text, "")
        called.assert_not_called()


class SendDailyTests(unittest.TestCase):
    """The digest still goes out, with or without the paragraph."""

    def setUp(self):
        self.db = mock.MagicMock()
        self.box = SecretBox(secrets.token_bytes(32))
        self.service = PilotService(self.db, self.box)
        self.user = {"id": "usr_1", "timezone": "Asia/Hong_Kong", "daily_time": "22:00",
                     "report_to": "me@example.com"}
        self.db.get_profile.return_value = {"timezone": "Asia/Hong_Kong"}
        self.db.messages_between.return_value = [{
            "id": "msg_1", "message_id": "msg_1", "subject": "选课通知",
            "sender_name": "教务处", "sender_address": "reg@example.com",
            "received_at": "2026-09-15T02:00:00+00:00", "status": "sent",
            "body_markdown": None, "last_error": "", "skip_reason": "",
        }]
        self.db.daily_report_for_date.return_value = None
        self.db.create_report.return_value = "rep_1"
        self.db.get_mailbox.return_value = {
            "id": "mbx_1", "user_id": "usr_1", "email": "me@example.com",
            "report_to": "me@example.com", "smtp_host": "smtp.example.com", "smtp_port": 465,
            "encrypted_password": self.box.encrypt("pw", context="mailbox:usr_1"),
        }
        self.sent: dict = {}

    def _send(self):
        def capture(mailbox, password, subject, markdown, *, html_body, text_body):
            self.sent.update({"subject": subject, "markdown": markdown,
                              "html": html_body, "text": text_body})
        with mock.patch.object(service_mod.mailio, "send_report", side_effect=capture):
            return self.service.send_daily(self.user, "2026-09-15")

    def test_off_by_default_the_mail_is_byte_for_byte_what_it_was(self):
        self.db.get_setting.return_value = ""
        with mock.patch.object(digest_synthesis, "synthesize") as called:
            self.assertTrue(self._send())
        called.assert_not_called()
        self.assertNotIn(reports.SYNTHESIS_HEADING, self.sent["markdown"])

    def test_on_attaches_the_paragraph_and_records_the_spend(self):
        self.db.get_setting.return_value = "1"
        with mock.patch.object(digest_synthesis, "synthesize",
                               return_value=("三门课都有作业。", {"input": 120, "output": 30, "total": 150},
                                             {"provider": "deepseek", "model": "deepseek-flash", "platform": False})):
            self.assertTrue(self._send())
        self.assertIn(reports.SYNTHESIS_HEADING, self.sent["markdown"])
        self.assertIn("三门课都有作业。", self.sent["html"])
        self.assertTrue(self.db.record_usage.called, "这一次调用要计费")
        self.assertEqual(self.db.record_usage.call_args.kwargs["kind"], "digest")

    def test_a_failing_model_still_sends_the_list(self):
        self.db.get_setting.return_value = "1"
        self.service.model_connection = mock.Mock(return_value=None)
        self.assertTrue(self._send(), "综览拿不到不该让整封简报失败")
        self.assertIn("选课通知", self.sent["markdown"], "清单必须照旧发出")
        self.assertNotIn(reports.SYNTHESIS_HEADING, self.sent["markdown"])

    def test_the_row_survives_with_the_paragraph_on(self):
        self.db.get_setting.return_value = "1"
        with mock.patch.object(digest_synthesis, "synthesize",
                               return_value=("一段话。", {}, {"provider": "deepseek", "model": "deepseek-flash"})):
            self._send()
        self.assertIn("选课通知", self.sent["markdown"])


if __name__ == "__main__":
    unittest.main()
