import datetime as dt
import json
import secrets
import unittest
from unittest import mock

from pilot_app import providers
from pilot_app import security
from pilot_app import service as service_mod
from pilot_app.security import SecretBox
from pilot_app.service import PilotService


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.db = mock.MagicMock()
        self.box = SecretBox(secrets.token_bytes(32))
        self.service = PilotService(self.db, self.box)

    def test_filtered_generated_messages_still_advance_imap_cursor(self):
        mailbox = {
            "id": "mbx", "user_id": "usr", "last_uid": 10, "uid_validity": "123",
            "encrypted_password": self.box.encrypt("pw", context="mailbox:usr"),
        }
        with mock.patch("pilot_app.service.mailio.fetch_new_messages", return_value=("123", [], 42)):
            count = self.service.poll_mailbox(mailbox)
        self.assertEqual(count, 0)
        self.db.update_mailbox_poll.assert_called_once_with("mbx", last_uid=42, uid_validity="123")

    def test_a_stored_legacy_alias_is_recorded_under_the_official_name(self):
        """The usage row must name the model we actually sent.

        A stored connection may still say ``deepseek-chat``; the request goes out
        as ``deepseek-flash`` (the alias is mapped at the provider boundary), so
        the row has to say the same thing -- otherwise "what we asked for" and
        "what we billed it as" become two different strings on the very page
        that tells a user what they owe.
        """
        self.service._record_usage(
            "usr", "immediate",
            {"provider": "deepseek", "model": "deepseek-chat", "platform": False},
            {"input": 1000, "output": 500, "total": 1500, "cached_input": 0, "reasoning": 0},
            message_id="msg",
        )
        self.assertTrue(self.db.record_usage.called, "记账不该被吞掉")
        kwargs = self.db.record_usage.call_args.kwargs
        self.assertEqual(kwargs["model"], "deepseek-flash")
        self.assertEqual(kwargs["provider"], "deepseek")
        self.assertIsNotNone(kwargs["cost"], "别名必须仍查得到价，否则这条会显示成「价格未配置」")

    def test_failed_smtp_retry_reuses_generated_report_without_second_model_call(self):
        message = {"id": "msg", "user_id": "usr", "subject": "Course", "attempts": 1}
        mailbox = {
            "id": "mbx", "user_id": "usr", "email": "me@example.com", "report_to": "me@example.com",
            "smtp_host": "smtp.example.com", "smtp_port": 465,
            "encrypted_password": self.box.encrypt("pw", context="mailbox:usr"),
        }
        self.db.mark_message_processing.return_value = True
        self.db.get_mailbox.return_value = mailbox
        self.db.get_profile.return_value = {"timezone": "Asia/Hong_Kong"}
        self.db.report_for_message.return_value = {
            "id": "rpt", "status": "failed", "subject": "summary", "body_markdown": "existing report",
        }
        with mock.patch.object(self.service, "_analyse") as analyse, mock.patch("pilot_app.service.mailio.send_report") as send:
            self.assertTrue(self.service.process_message(message))
        analyse.assert_not_called()
        self.assertEqual(send.call_args.args, (mailbox, "pw", "summary", "existing report"))
        rendered = send.call_args.kwargs
        self.assertIn("你应该做什么", rendered["html_body"])
        self.assertIn("邮件讲了什么", rendered["html_body"])
        self.assertIn("existing report", rendered["text_body"])
        self.db.mark_report_sent.assert_called_once_with("rpt")
        self.db.finish_message.assert_called_once_with("msg")

    def test_process_message_tolerates_missing_message_fields(self):
        """Worker rows can lack optional columns; rendering must not explode."""
        message = {"id": "msg2", "user_id": "usr", "subject": "Bare", "attempts": 0,
                   "sender_name": "", "sender_address": "", "received_at": "2026-09-13T00:00:00+00:00",
                   "importance": "normal", "body": self.box.encrypt("raw", context="message:usr")}
        mailbox = {
            "id": "mbx", "user_id": "usr", "email": "me@example.com", "report_to": "me@example.com",
            "smtp_host": "smtp.example.com", "smtp_port": 465,
            "encrypted_password": self.box.encrypt("pw", context="mailbox:usr"),
        }
        self.db.mark_message_processing.return_value = True
        self.db.get_mailbox.return_value = mailbox
        self.db.get_profile.return_value = {}
        self.db.report_for_message.return_value = None
        self.db.messages_between.return_value = []
        with mock.patch.object(self.service, "_analyse", return_value="## 1. 重要程度\n- 等级：高"), \
                mock.patch("pilot_app.service.mailio.send_report"):
            self.assertTrue(self.service.process_message(message))

    def test_daily_retry_reuses_report_and_uses_hong_kong_day_window(self):
        user = {"id": "usr", "timezone": "Asia/Hong_Kong", "report_to": "me@example.com"}
        mailbox = {
            "id": "mbx", "user_id": "usr", "email": "me@example.com", "report_to": "me@example.com",
            "smtp_host": "smtp.example.com", "smtp_port": 465,
            "encrypted_password": self.box.encrypt("pw", context="mailbox:usr"),
        }
        self.db.get_profile.return_value = {}
        self.db.daily_report_for_date.return_value = {
            "id": "daily", "status": "failed", "subject": "daily summary", "body_markdown": "existing daily",
        }
        self.db.get_mailbox.return_value = mailbox
        self.db.messages_between.return_value = []
        with mock.patch("pilot_app.service.providers.generate_text") as generate, mock.patch("pilot_app.service.mailio.send_report"):
            self.assertTrue(self.service.send_daily(user, "2026-09-13"))
        generate.assert_not_called()
        args = self.db.messages_between.call_args.args
        self.assertEqual(args[1], "2026-09-12T16:00:00+00:00")
        self.assertEqual(args[2], "2026-09-13T16:00:00+00:00")
        self.db.mark_report_sent.assert_called_once_with("daily")

    def test_daily_digest_needs_no_model_and_never_drops_a_message(self):
        """The 22:00 brief is composed locally so no mail can be lost by a model."""
        user = {"id": "usr", "timezone": "Asia/Hong_Kong", "report_to": "me@example.com"}
        mailbox = {
            "id": "mbx", "user_id": "usr", "email": "me@example.com", "report_to": "me@example.com",
            "smtp_host": "smtp.example.com", "smtp_port": 465,
            "encrypted_password": self.box.encrypt("pw", context="mailbox:usr"),
        }
        self.db.get_profile.return_value = {}
        self.db.daily_report_for_date.return_value = None
        self.db.get_mailbox.return_value = mailbox
        good = "## 1. 重要程度\n- 等级：高\n- 结论：要交作业\n## 2. 必须采取的行动\n- 周五前提交"
        rows = [
            {"id": "m1", "subject": "Course deadline", "sender_name": "Teacher", "sender_address": "t@x.hk",
             "received_at": "2026-09-13T02:00:00+00:00", "importance": "normal", "status": "sent",
             "last_error": "", "body_markdown": self.box.encrypt(good, context="report:usr")},
            {"id": "m2", "subject": "Promo", "sender_name": "Shop", "sender_address": "s@x.hk",
             "received_at": "2026-09-13T03:00:00+00:00", "importance": "low", "status": "failed",
             "last_error": "smtp down", "body_markdown": None},
        ]
        self.db.messages_between.return_value = rows
        with mock.patch("pilot_app.service.providers.generate_text") as generate, \
                mock.patch("pilot_app.service.mailio.send_report") as send:
            self.assertTrue(self.service.send_daily(user, "2026-09-13"))
        generate.assert_not_called()
        html = send.call_args.kwargs["html_body"]
        text = send.call_args.kwargs["text_body"]
        for subject in ("Course deadline", "Promo"):
            self.assertIn(subject, html)
            self.assertIn(subject, text)
        self.assertIn("smtp down", html)
        self.assertIn("今天有 1 件事需要处理", html)


    # -- native search replaces the second API key --------------------------

    def _model_connection(self, provider):
        return {
            "kind": "model", "user_id": "usr", "provider": provider, "model": "test-model", "base_url": "",
            "enabled": 1, "config_json": "{}",
            "encrypted_api_key": self.box.encrypt("model-key", context="connection:usr:model"),
        }

    def test_analyse_prefers_native_search_and_skips_external_api(self):
        self.db.get_profile.return_value = {}
        sources = [{"title": "Source", "url": "https://example.com/a", "summary": ""}]
        self.db.get_connection.side_effect = lambda user_id, kind: (
            self._model_connection("openai") if kind == "model" else None
        )
        with mock.patch(
            "pilot_app.service.providers.generate",
            return_value=providers.Generation("## 1. 邮件内容总结\nsee https://example.com/a", sources, "native"),
        ) as generate, mock.patch("pilot_app.service.providers.web_search") as web_search:
            report = self.service._analyse("usr", {"subject": "Course update", "body": "hello"})
        web_search.assert_not_called()
        self.assertTrue(generate.call_args.kwargs["native_search"])
        self.assertIsInstance(report, str)
        self.assertIn("https://example.com/a", report)

    def test_analyse_survives_native_search_failure(self):
        self.db.get_profile.return_value = {}
        self.db.get_connection.side_effect = lambda user_id, kind: (
            self._model_connection("anthropic") if kind == "model" else None
        )

        def fake_generate(**kwargs):
            if kwargs.get("native_search"):
                raise providers.ProviderError("native search exploded")
            return providers.Generation("## 3. 邮件内容总结\nno live verification", [], "none")

        with mock.patch("pilot_app.service.providers.generate", side_effect=fake_generate) as generate:
            report = self.service._analyse("usr", {"subject": "Course update", "body": "hello"})
        self.assertIsInstance(report, str)
        self.assertTrue(report.strip())
        # First attempt used native search, the retry did not.
        self.assertTrue(generate.call_args_list[0].kwargs["native_search"])
        self.assertFalse(generate.call_args_list[1].kwargs.get("native_search"))

    def test_analyse_still_uses_external_search_for_plain_providers(self):
        self.db.get_profile.return_value = {}
        connections = {
            "model": self._model_connection("deepseek"),
            "search": {
                "kind": "search", "user_id": "usr", "provider": "tavily", "enabled": 1,
                "encrypted_api_key": self.box.encrypt("search-key", context="connection:usr:search"),
            },
        }
        self.db.get_connection.side_effect = lambda user_id, kind: connections.get(kind)
        hits = [{"title": "Source", "url": "https://example.com/a", "summary": ""}]
        with mock.patch("pilot_app.service.providers.web_search", return_value=hits) as web_search, \
                mock.patch("pilot_app.service.providers.generate",
                           return_value=providers.Generation("## 3. 邮件内容总结\nsee https://example.com/a", [], "none")) as generate:
            self.service._analyse("usr", {"subject": "Course update", "body": "hello"})
        web_search.assert_called_once()
        self.assertFalse(generate.call_args.kwargs.get("native_search"))


    def test_native_failure_falls_back_to_external_search(self):
        self.db.get_profile.return_value = {}
        connections = {
            "model": self._model_connection("gemini"),
            "search": {
                "kind": "search", "user_id": "usr", "provider": "tavily", "enabled": 1,
                "encrypted_api_key": self.box.encrypt("search-key", context="connection:usr:search"),
            },
        }
        self.db.get_connection.side_effect = lambda user_id, kind: connections.get(kind)

        def fake_generate(**kwargs):
            if kwargs.get("native_search"):
                raise providers.ProviderError("native search unavailable")
            return providers.Generation("## 5. 联网搜索\nsee https://example.com/e", [], "none")

        hits = [{"title": "Source", "url": "https://example.com/e", "summary": ""}]
        with mock.patch("pilot_app.service.providers.generate", side_effect=fake_generate), \
                mock.patch("pilot_app.service.providers.web_search", return_value=hits) as web_search:
            report = self.service._analyse("usr", {"subject": "Course update", "body": "hello"})
        web_search.assert_called_once()
        self.assertIn("https://example.com/e", report)

    def test_model_test_rejects_an_empty_answer(self):
        """The connection test used to pass on an empty answer, which is how a
        reasoning model that answered every real prompt with "" looked healthy."""
        self.db.get_connection.return_value = {
            "user_id": "usr", "kind": "model",
            "provider": "deepseek", "model": "deepseek-flash", "base_url": "",
            "config_json": "{}", "enabled": 1,
            "encrypted_api_key": self.box.encrypt("k", context="connection:usr:model"),
        }
        with mock.patch("pilot_app.service.providers.generate_text", return_value="   "):
            with self.assertRaises(providers.ProviderError) as caught:
                self.service.test_model("usr")
        self.assertIn("没有返回任何文本", str(caught.exception))

    def test_model_test_accepts_a_real_answer(self):
        self.db.get_connection.return_value = {
            "user_id": "usr", "kind": "model",
            "provider": "deepseek", "model": "deepseek-chat", "base_url": "",
            "config_json": "{}", "enabled": 1,
            "encrypted_api_key": self.box.encrypt("k", context="connection:usr:model"),
        }
        with mock.patch("pilot_app.service.providers.generate_text", return_value="连接成功"):
            self.assertEqual(self.service.test_model("usr"), "连接成功")

    # -- transient provider failures ---------------------------------------

    def test_transient_model_failure_is_retried_once(self):
        """A dropped long generation must not lose the email."""
        calls = []

        def flaky(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise providers.TransientProviderError("接口连接被中断（长回答可能超时）")
            return providers.Generation("## 3. 内容\nok", [], "none")

        with mock.patch("pilot_app.service.time.sleep") as pause, \
                mock.patch("pilot_app.service.providers.generate", side_effect=flaky):
            result = self.service._generate_with_retry("usr", provider="deepseek", model="m", api_key="k", prompt="p")
        self.assertEqual(result.text, "## 3. 内容\nok")
        self.assertEqual(len(calls), 2)
        pause.assert_called_once()

    def test_permanent_model_failure_is_not_retried(self):
        with mock.patch("pilot_app.service.providers.generate",
                        side_effect=providers.ProviderError("API 返回 HTTP 401: bad key")) as generate:
            with self.assertRaises(providers.ProviderError):
                self.service._generate_with_retry("usr", provider="deepseek", model="m", api_key="k", prompt="p")
        self.assertEqual(generate.call_count, 1)

    def test_a_timeout_is_not_retried_immediately(self):
        """A timeout already spent the whole budget (~234 s of a 300 s ceiling
        for a real report), so retrying it would hold that generation slot for
        twice as long. The message is marked failed and the queue's backoff
        retries it instead."""
        with mock.patch("pilot_app.service.providers.generate",
                        side_effect=providers.ProviderTimeout("接口响应超时")) as generate:
            with self.assertRaises(providers.ProviderTimeout):
                self.service._generate_with_retry("usr", provider="deepseek", model="m", api_key="k", prompt="p")
        self.assertEqual(generate.call_count, 1, "超时不应立即重试")
        # Still classified as transient, so the rest of the system treats it as
        # a retryable failure rather than a permanent one.
        self.assertTrue(self.service._transient(providers.ProviderTimeout("x")))
        self.assertTrue(issubclass(providers.ProviderTimeout, providers.TransientProviderError))

    def test_retry_gives_up_after_the_configured_attempts(self):
        with mock.patch.dict("os.environ", {"INFE_PILOT_MODEL_ATTEMPTS": "3"}), \
                mock.patch("pilot_app.service.time.sleep"), \
                mock.patch("pilot_app.service.providers.generate",
                           side_effect=providers.TransientProviderError("超时")) as generate:
            with self.assertRaises(providers.TransientProviderError):
                self.service._generate_with_retry("usr", provider="deepseek", model="m", api_key="k", prompt="p")
        self.assertEqual(generate.call_count, 3)

    def test_transient_classification(self):
        self.assertTrue(issubclass(providers.TransientProviderError, providers.ProviderError))
        self.assertTrue(self.service._transient(TimeoutError("timed out")))
        self.assertFalse(self.service._transient(ValueError("not transient")))

    # -- sender allow-list at ingestion ------------------------------------

    def test_poll_skips_non_allowed_senders_without_queueing_them(self):
        """Personal mail is stored and marked, but never becomes a report."""
        mailbox = {
            "id": "mbx", "user_id": "usr", "last_uid": 10, "uid_validity": "123",
            "encrypted_password": self.box.encrypt("pw", context="mailbox:usr"),
        }
        allowed = {"subject": "Tutorial notice", "sender_name": "CityU", "sender_address": "student@my.cityu.edu.hk",
                   "received": "2026-09-13T02:00:00+00:00", "importance": "normal", "body": "b",
                   "message_key": "<a@cityu>"}
        personal = {"subject": "50% off", "sender_name": "Grammarly", "sender_address": "hello@mail.grammarly.com",
                    "received": "2026-09-13T02:01:00+00:00", "importance": "normal", "body": "b",
                    "message_key": "<b@grammarly>"}
        self.db.insert_message.return_value = "msg_ok"
        with mock.patch.object(service_mod, "ALLOWED_SENDER_DOMAINS", ("cityu.edu.hk",)), \
                mock.patch("pilot_app.service.mailio.fetch_new_messages",
                           return_value=("123", [(11, allowed), (12, personal)], 12)):
            stored = self.service.poll_mailbox(mailbox)

        self.assertEqual(stored, 1, "只应入库 1 封（CityU），另一封被跳过")
        self.assertEqual(self.db.insert_message.call_count, 2, "两封都要落库以便审计")
        skipped_calls = self.db.mark_message_skipped_by_uid.call_args_list
        self.assertEqual(len(skipped_calls), 1)
        args = skipped_calls[0].args
        self.assertEqual(args[0], "mbx")
        self.assertEqual(args[1], "123")
        self.assertEqual(args[2], 12)
        self.assertIn("hello@mail.grammarly.com", args[3])
        # The privacy policy says a skipped mail keeps metadata only. Encrypting
        # the body and then marking the row skipped would leave an unread body
        # in the database, so the discarded body must never reach storage.
        stored_messages = [call.args[4] for call in self.db.insert_message.call_args_list]
        self.assertEqual(stored_messages[1]["body"], b"", "非本校邮件的正文不得入库")
        self.assertNotEqual(stored_messages[0]["body"], b"", "本校邮件的正文要加密保存，否则无法生成报告")
        self.assertEqual(stored_messages[1]["subject"], "50% off", "跳过的邮件仍要留元数据以便如实汇报")

    def test_poll_processes_everything_when_allow_list_is_empty(self):
        mailbox = {
            "id": "mbx", "user_id": "usr", "last_uid": 10, "uid_validity": "123",
            "encrypted_password": self.box.encrypt("pw", context="mailbox:usr"),
        }
        personal = {"subject": "Promo", "sender_name": "Shop", "sender_address": "promo@shop.example",
                    "received": "2026-09-13T02:00:00+00:00", "importance": "normal", "body": "b",
                    "message_key": "<c@shop>"}
        self.db.insert_message.return_value = "msg_ok"
        with mock.patch.object(service_mod, "ALLOWED_SENDER_DOMAINS", ()), \
                mock.patch("pilot_app.service.mailio.fetch_new_messages",
                           return_value=("123", [(11, personal)], 11)):
            stored = self.service.poll_mailbox(mailbox)
        self.assertEqual(stored, 1)
        self.db.mark_message_skipped_by_uid.assert_not_called()


class DailyDigestRetryTests(unittest.TestCase):
    """每日简报失败后**不许每 15 秒重发一遍**。

    2026-09-18 用户报「后台显示 5 个报告失败，但刷新下发情况又没有」。查数字的时候
    发现了底下这个真问题：`run_daily_due` 由主循环每 15 秒调一次，而
    `daily_report_exists` 只认 `status='sent'`，于是一封发不出去的简报会被重发到当天结束——
    生产日志里一个授权码坏掉的账号一晚上 **1185 次** SMTP 尝试，而 163 回给我们的原话里
    就有「IP is rejected」：我们可能正在自己把发信的路走坏。

    这里钉住三件事：会重试、退避、试够就停。
    """

    def setUp(self):
        self.db = mock.MagicMock()
        self.service = PilotService(self.db, SecretBox(secrets.token_bytes(32)))
        self.user = {"id": "usr_x", "email": "x@example.com", "timezone": "Asia/Hong_Kong",
                     "daily_time": "22:00", "report_to": "x@example.com"}
        self.db.daily_users.return_value = [self.user]
        self.db.daily_report_exists.return_value = False
        # 时间由测试推着走：退避是用 monotonic 算的。
        self.clock = {"now": 1000.0}
        self.service.daily_due = lambda user, now_utc=None: (True, "2026-09-18")

    def _monotonic(self):
        return self.clock["now"]

    def test_a_failed_digest_is_retried_but_not_every_pass(self):
        import pilot_app.service as service_mod
        self.service.send_daily = mock.MagicMock(side_effect=service_mod.mailio.MailError("550 User has no permission"))
        with mock.patch.object(service_mod.time, "monotonic", side_effect=self._monotonic), \
             mock.patch.object(service_mod, "log_job_failure") as logged:
            self.service.run_daily_due()
            self.assertEqual(self.service.send_daily.call_count, 1)
            # 紧接着的几轮（15 秒一次）不许再打——这就是那 1185 次的来源
            for _ in range(10):
                self.clock["now"] += 15
                self.service.run_daily_due()
            self.assertEqual(self.service.send_daily.call_count, 1, "退避窗口内不许重试")
            # 退避过去之后要再试一次（网络抖动值得再给机会）
            self.clock["now"] += 300
            self.service.run_daily_due()
            self.assertEqual(self.service.send_daily.call_count, 2)
        self.assertTrue(logged.called, "失败要走那一处共用的日志口径，而不是留堆栈")

    def test_it_gives_up_for_the_day_after_the_budget(self):
        import pilot_app.service as service_mod
        self.service.send_daily = mock.MagicMock(side_effect=service_mod.mailio.MailError("550 nope"))
        with mock.patch.object(service_mod.time, "monotonic", side_effect=self._monotonic), \
             mock.patch.object(service_mod, "log_job_failure"):
            for _ in range(24):                      # 推着时钟走一整天
                self.clock["now"] += 3600
                self.service.run_daily_due()
            # 1 次首发 + 退避表里的 3 次，之后当天不再试
            self.assertEqual(self.service.send_daily.call_count, 1 + len(service_mod.DIGEST_RETRY_BACKOFF))

    def test_a_success_clears_the_budget(self):
        self.service.send_daily = mock.MagicMock(return_value=True)
        with mock.patch("pilot_app.service.time.monotonic", return_value=1000.0):
            self.service.run_daily_due()
        self.assertEqual(self.service._digest_retry, {})

    def test_old_entries_do_not_pile_up(self):
        self.service._digest_retry[("usr_old", "2026-09-01")] = (3, 0.0)
        self.service._digest_retry[("usr_new", "2026-09-18")] = (1, 0.0)
        self.service._prune_digest_retries("2026-09-18")
        self.assertEqual(list(self.service._digest_retry), [("usr_new", "2026-09-18")])


class FailedReportAccountingTests(unittest.TestCase):
    """「失败报告 N 份」与「下发情况」那张表必须能对上账。

    用户报的原话：「后台显示有5个报告失败，但是我刷新下发情况又没有」。两个数字都
    没错——**5 个全是每日简报**，而简报按设计没有 message_id，所以那张一行一封邮件的
    表里永远看不到它们。修法是让两个数分开站着，并且把简报那几行交出去。
    """

    def _db(self):
        import os
        import tempfile
        from pilot_app.database import Database
        path = os.path.join(tempfile.mkdtemp(), "failed.sqlite3")
        database = Database(path)
        database.initialize()
        # 外键是真的：`reports.user_id` 指向 `users`，所以先放一个账号进去。
        with database.connect() as connection:
            connection.execute(
                "INSERT INTO users(id,email,password_hash,status,created_at)"
                " VALUES('usr_1','one@example.com','x','active','2026-09-18T00:00:00+00:00')")
        return database

    _day = 0

    def _seed(self, database, *, kind: str, status: str):
        """一行报告。**不带 message_id**：这一组测的是按 kind 分开数，
        而带 message_id 就要先造出真实的用户与邮件行（外键是真的）。

        每天只能有一封简报（`(user_id, kind, report_date)` 是唯一的，这是设计），
        所以连着造几封失败简报要各自换一天。
        """
        FailedReportAccountingTests._day += 1
        report_id = database.create_report(
            user_id="usr_1", message_id=None, kind=kind, subject="s", body="b",
            sent_to="a@example.com",
            report_date=(f"2026-09-{FailedReportAccountingTests._day:02d}" if kind == "daily" else ""))
        # 按 id 改这一行——id 是随机串，`MAX(id)` 拿到的是别的行（第一版就是这么错的）。
        with database.connect() as connection:
            connection.execute("UPDATE reports SET status=? WHERE id=?", (status, report_id))

    def test_the_two_numbers_are_split(self):
        database = self._db()
        self._seed(database, kind="immediate", status="failed")
        self._seed(database, kind="immediate", status="sent")
        for _ in range(5):
            self._seed(database, kind="daily", status="failed")
        self._seed(database, kind="daily", status="sent")
        summary = database.failed_reports_summary()
        self.assertEqual(summary["total"], 6)
        self.assertEqual(summary["digests"], 5, "简报失败要单独数出来")
        self.assertEqual(summary["per_mail"], 1, "逐封邮件的失败才是列表里看得到的那些")
        self.assertEqual(len(database.failed_digests(10)), 5)

    def test_a_digest_failure_carries_enough_for_the_panel_to_explain_itself(self):
        database = self._db()
        self._seed(database, kind="daily", status="failed")
        with database.connect() as connection:
            connection.execute("UPDATE reports SET last_error='SMTP 发送失败：(550, …)'")
        row = database.failed_digests(10)[0]
        for field in ("report_date", "sent_to", "last_error"):
            self.assertIn(field, row, f"面板要拿 {field} 说话")


class CredentialClassificationTests(unittest.TestCase):
    """Which model failures may be blamed on the user's key -- and which may not.

    `_generate_with_retry` is the only place that decides this, because it is
    also the only place that already knows what "worth retrying" means. The
    asymmetry is the whole design:

    * a **credential** failure (4xx, wrong key, wrong model name) repeats forever
      if we keep trying, so it counts towards the breaker;
    * a **transient** failure is the provider's problem, and counting it would
      suspend innocent users -- far worse than the queue waste being fixed.

    Most of these tests are about the second case, because that is the bug a
    future edit is most likely to introduce.
    """

    def setUp(self):
        self.db = mock.MagicMock()
        self.service = PilotService(self.db, SecretBox(secrets.token_bytes(32)))

    def test_a_rejected_key_is_counted(self):
        with mock.patch("pilot_app.service.providers.generate",
                        side_effect=providers.ProviderError("API 返回 HTTP 401：invalid api key")):
            with self.assertRaises(providers.ProviderError):
                self.service._generate_with_retry("usr", provider="deepseek")
        self.db.record_key_failure.assert_called_once()
        self.assertEqual(self.db.record_key_failure.call_args.args[:2], ("usr", "model"))

    def test_a_transient_failure_is_retried_and_never_counted(self):
        calls = {"n": 0}

        def flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise providers.TransientProviderError("API 返回 HTTP 429：rate limited")
            return "answer"

        with mock.patch("pilot_app.service.providers.generate", side_effect=flaky), \
                mock.patch("pilot_app.service.time.sleep"):
            self.assertEqual(self.service._generate_with_retry("usr", provider="deepseek"), "answer")
        self.assertEqual(calls["n"], 2, "瞬时错误应当重试一次")
        self.db.record_key_failure.assert_not_called()
        self.db.clear_key_failures.assert_called_once_with("usr", "model")

    def test_a_transient_failure_that_never_recovers_still_is_not_counted(self):
        """The provider being down for a whole retry budget is not the user's key."""
        with mock.patch("pilot_app.service.providers.generate",
                        side_effect=providers.TransientProviderError("API 返回 HTTP 500")), \
                mock.patch("pilot_app.service.time.sleep"):
            with self.assertRaises(providers.TransientProviderError):
                self.service._generate_with_retry("usr", provider="deepseek")
        self.db.record_key_failure.assert_not_called()

    def test_a_timeout_is_left_to_the_queue_and_not_counted(self):
        with mock.patch("pilot_app.service.providers.generate",
                        side_effect=providers.ProviderTimeout("接口响应超时")):
            with self.assertRaises(providers.ProviderTimeout):
                self.service._generate_with_retry("usr", provider="deepseek")
        self.db.record_key_failure.assert_not_called()

    def test_a_failure_that_is_not_the_credentials_fault_is_never_counted(self):
        """The bug a real run found, pinned.

        An unresolvable API host raises `SecurityError` from the outbound-URL
        gate -- not a `ProviderError`, and not on the transient list. The first
        version of this feature asked "is it transient?" and counted everything
        else, so three DNS hiccups suspended an account whose key was fine. The
        rule is now an allow-list: only a non-retryable answer *from the
        provider* may be blamed on the credential.
        """
        for exc in (security.SecurityError("API 域名目前无法解析。"),
                    ValueError("unexpected response shape"),
                    KeyError("choices"),
                    json.JSONDecodeError("Expecting value", "", 0)):
            self.db.reset_mock()
            with mock.patch("pilot_app.service.providers.generate", side_effect=exc):
                with self.assertRaises(type(exc)):
                    self.service._generate_with_retry("usr", provider="deepseek")
            self.db.record_key_failure.assert_not_called()
            self.db.clear_key_failures.assert_not_called()

    def test_a_wrong_model_name_is_counted_like_a_wrong_key(self):
        """Both are permanent configuration errors the provider answers with 4xx."""
        with mock.patch("pilot_app.service.providers.generate",
                        side_effect=providers.ProviderError("API 返回 HTTP 400：Model Not Exist")):
            with self.assertRaises(providers.ProviderError):
                self.service._generate_with_retry("usr", provider="deepseek")
        self.db.record_key_failure.assert_called_once()

    def test_a_real_answer_clears_the_breaker(self):
        with mock.patch("pilot_app.service.providers.generate", return_value="answer"):
            self.assertEqual(self.service._generate_with_retry("usr", provider="deepseek"), "answer")
        self.db.clear_key_failures.assert_called_once_with("usr", "model")

    def test_suspending_is_logged_without_any_key_material(self):
        """The operator has to be able to explain afterwards why reports stopped."""
        self.db.record_key_failure.return_value = {"failures": 3, "open_until": "2026-09-15T11:00:00+00:00"}
        with self.assertLogs(level="WARNING") as captured:
            self.service._note_bad_credential("usr", providers.ProviderError("API 返回 HTTP 401"))
        text = "\n".join(captured.output)
        self.assertIn("usr", text)
        self.assertIn("2026-09-15T11:00:00+00:00", text)

    def test_the_queue_gate_fails_open_when_the_check_cannot_answer(self):
        """A double, an unmigrated schema, an odd return value: try the mail.

        Failing closed would look exactly like the bug this protects against --
        messages pointing at nothing, with nothing logged. The gate therefore
        requires an explicit True rather than a truthy value.
        """
        self.db.key_circuit_open.return_value = None  # cannot answer
        self.db.due_messages.return_value = [{"id": "msg", "user_id": "usr"}]
        self.service.process_message = mock.Mock(return_value=True)
        self.assertEqual(self.service.process_due(), (1, 0))
        self.service.process_message.assert_called_once()


if __name__ == "__main__":
    unittest.main()
