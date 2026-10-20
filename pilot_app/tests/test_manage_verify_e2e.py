# -*- coding: utf-8 -*-
"""Tests for ``manage verify-e2e`` -- the command nobody could run in CI.

``docs/release-audit-2026-09-15.md`` §5.2 left this open: the tool was only ever
proved by running it against production, by hand. Most of it genuinely needs a
real mailbox and a real model, so what is pinned here is the part that does not:

* **it refuses before it touches anything** -- an unknown address, a deleted
  account, an account with no mailbox each end in a printed reason and exit 2,
  never in a stack trace and never in a half-run;
* **it does not print the address it was given** when it cannot find it: this
  output is meant to be pasted into a chat when asking for help, which is
  exactly the moment a full school address must not ride along;
* **``--pause`` leaves the mailbox as it found it**, including when the run
  blows up halfway -- a verification tool that silently disables a real user's
  mailbox is worse than no verification tool;
* the two throwaway-row helpers write what they claim to (status ``sent``, so
  the "never send a duplicate" rule keeps holding).
"""

from __future__ import annotations

import base64
import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from pilot_app import database, manage
from pilot_app.security import SecretBox, hash_password, token_hash

_TEST_KEY = base64.urlsafe_b64encode(b"\x02" * 32).decode()
PASSWORD = "a-long-enough-password"


def run_captured(func, *args, **kwargs):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = func(*args, **kwargs)
    return code, out.getvalue()


class VerifyE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db = database.Database(os.path.join(self.temporary.name, "pilot.sqlite3"))
        self.db.initialize()
        self.box = SecretBox.from_base64(_TEST_KEY)

    # -- helpers -----------------------------------------------------------

    def _user(self, email: str = "student@example.com") -> dict:
        invite = self.db.create_invite("e2e", 1)
        return self.db.create_user(email, hash_password(PASSWORD), token_hash(invite))

    def _mailbox(self, user_id: str) -> None:
        self.db.upsert_mailbox(user_id, {
            "email": "private@example.com", "report_to": "private@example.com",
            "imap_host": "imap.example.com", "imap_port": 993,
            "smtp_host": "smtp.example.com", "smtp_port": 465,
            "encrypted_password": self.box.encrypt("app-password", context=f"mailbox:{user_id}"),
            "enabled": 1, "uid_validity": "", "last_uid": 0, "last_polled_at": "",
            "last_error": "",
        })

    def _enabled(self, user_id: str) -> int:
        return int(self.db.get_mailbox(user_id)["enabled"])

    # -- refusals ----------------------------------------------------------

    def test_an_unknown_address_is_refused_and_not_echoed(self) -> None:
        # 用保留域名，不用真实学校域名：开源导出时会把真实域名与真实地址一起改写，
        # 而**期望值**里那个含 `*` 的字符串它改不掉（模式匹配不上），于是断言的两半
        # 描述的是两个不同的地址 —— 本机绿、公开树红，而公开树才是别人拿到的那一份。
        # 这个坑 CI 第一次跑就抓到过一次（见 HANDOVER 第 15 轮），别再踩第二次。
        code, printed = run_captured(manage.verify_e2e, self.db, "nobody@example.com",
                                     1, False, False)
        self.assertEqual(code, 2)
        self.assertIn("找不到试点用户", printed)
        self.assertNotIn("nobody@example.com", printed, "完整地址不许进输出")
        self.assertIn("no***@example.com", printed)

    def test_a_deleted_account_is_refused(self) -> None:
        # 删除流程走的是 **DELETE**（`set_user_status` 里 deleted 分支），所以删号之后
        # 这个邮箱在库里根本不存在，工具说的是「找不到试点用户」。代码里那一支
        # `status == 'deleted'` 是给「历史遗留的 deleted 行」留的防御（全项目同款），
        # 今天不可达——这里钉的是**真的会发生的那件事**，不假装可达。
        user = self._user()
        self.db.set_user_status(user["id"], "deleted")
        self.assertIsNone(self.db.find_user_for_login("student@example.com"))
        code, printed = run_captured(manage.verify_e2e, self.db, "student@example.com",
                                     1, False, False)
        self.assertEqual(code, 2)
        self.assertIn("找不到试点用户", printed)
        self.assertNotIn("student@example.com", printed)

    def test_an_account_without_a_mailbox_is_refused(self) -> None:
        self._user()
        code, printed = run_captured(manage.verify_e2e, self.db, "student@example.com",
                                     1, False, False)
        self.assertEqual(code, 2)
        self.assertIn("还没有配置邮箱", printed)

    def test_it_refuses_before_needing_the_master_key(self) -> None:
        # The refusals happen before SecretBox.from_environment(), so a machine
        # without the key still gets the useful answer instead of a security
        # error about a key it was never going to use.
        self._user()
        with mock.patch("pilot_app.security.SecretBox.from_environment",
                        side_effect=AssertionError("不该走到读主密钥这一步")):
            code, printed = run_captured(manage.verify_e2e, self.db, "student@example.com",
                                         1, False, False)
        self.assertEqual(code, 2)
        self.assertIn("还没有配置邮箱", printed)

    # -- --pause must be reversible ---------------------------------------

    def test_pause_disables_the_mailbox_and_restores_it(self) -> None:
        user = self._user()
        self._mailbox(user["id"])
        seen = {}

        def fake_run(db, box, user, mailbox, profile, model, search, limit, send, show_body,
                     force_resend, send_digest, started, pull, target_day, store, measure,
                     measure_model):
            seen["during"] = int(db.get_mailbox(user["id"])["enabled"])
            return 0

        with mock.patch.object(manage, "_e2e_run", side_effect=fake_run), \
             mock.patch("pilot_app.security.SecretBox.from_environment",
                        staticmethod(lambda: self.box)):
            code, printed = run_captured(manage.verify_e2e, self.db, "student@example.com",
                                         1, False, False, pause=True)
        self.assertEqual(code, 0)
        self.assertEqual(seen["during"], 0, "验证期间该邮箱必须是禁用的（第二个 worker 抢不走）")
        self.assertEqual(self._enabled(user["id"]), 1, "验证结束后必须恢复")
        self.assertIn("已恢复该邮箱", printed)

    def test_pause_restores_the_mailbox_even_when_the_run_raises(self) -> None:
        user = self._user()
        self._mailbox(user["id"])
        with mock.patch.object(manage, "_e2e_run", side_effect=RuntimeError("模型挂了")), \
             mock.patch("pilot_app.security.SecretBox.from_environment",
                        staticmethod(lambda: self.box)):
            with self.assertRaises(RuntimeError):
                run_captured(manage.verify_e2e, self.db, "student@example.com",
                             1, False, False, pause=True)
        self.assertEqual(self._enabled(user["id"]), 1, "抛异常也必须恢复邮箱")

    def test_without_pause_the_mailbox_is_never_touched(self) -> None:
        user = self._user()
        self._mailbox(user["id"])
        with mock.patch.object(manage, "_e2e_run", return_value=0), \
             mock.patch("pilot_app.security.SecretBox.from_environment",
                        staticmethod(lambda: self.box)):
            code, _ = run_captured(manage.verify_e2e, self.db, "student@example.com",
                                   1, False, False)
        self.assertEqual(code, 0)
        self.assertEqual(self._enabled(user["id"]), 1)

    # -- --store cannot claim delivery by accident -------------------------
    #
    # ``--store`` writes rows that say "已下发" (``_store_message`` sets
    # ``status='sent'``, ``_store_report`` calls ``mark_report_sent``). Rule 8
    # makes ``messages.status`` the record of what was delivered, and ``main()``
    # opens ``INFE_PILOT_DB`` -- on the server, the production database -- so an
    # unnamed ``--store`` would leave a real account's mail looking reported.

    def test_store_refuses_without_naming_the_target(self) -> None:
        user = self._user()
        self._mailbox(user["id"])
        with mock.patch.object(manage, "_e2e_run") as run, \
             mock.patch("pilot_app.security.SecretBox.from_environment",
                        staticmethod(lambda: self.box)):
            code, printed = run_captured(manage.verify_e2e, self.db, "student@example.com",
                                         1, False, False, store=True, pull_date="2026-01-05")
        self.assertEqual(code, 2)
        self.assertIn("拒绝执行", printed)
        self.assertIn(manage.STORE_TARGET_ENV, printed, "必须告诉人怎么写才算指名了目标")
        run.assert_not_called()

    def test_store_refuses_when_the_date_is_missing(self) -> None:
        # It used to skip every write and still print "已落库到 …（仅限本次验证
        # 数据库）", so the operator could believe rows had been written.
        user = self._user()
        self._mailbox(user["id"])
        with mock.patch.object(manage, "_e2e_run") as run:
            code, printed = run_captured(manage.verify_e2e, self.db, "student@example.com",
                                         1, False, False, store=True)
        self.assertEqual(code, 2)
        self.assertIn("--pull-date", printed)
        run.assert_not_called()

    def test_store_refuses_before_needing_the_master_key(self) -> None:
        # Same ordering rule as the other refusals: a machine without the key
        # gets the answer that explains what to do, not a security error about a
        # key this run was never going to reach.
        user = self._user()
        self._mailbox(user["id"])
        with mock.patch("pilot_app.security.SecretBox.from_environment",
                        side_effect=AssertionError("不该走到读主密钥这一步")):
            code, printed = run_captured(manage.verify_e2e, self.db, "student@example.com",
                                         1, False, False, store=True, pull_date="2026-01-05")
        self.assertEqual(code, 2)
        self.assertIn("拒绝执行", printed)

    def test_store_proceeds_when_the_target_is_named(self) -> None:
        # The guard must not become a wall: naming the throwaway database is the
        # whole point, and then the run has to actually reach _e2e_run.
        user = self._user()
        self._mailbox(user["id"])
        with mock.patch.object(manage, "_e2e_run", return_value=0) as run, \
             mock.patch("pilot_app.security.SecretBox.from_environment",
                        staticmethod(lambda: self.box)), \
             mock.patch.dict(os.environ, {manage.STORE_TARGET_ENV: self.db.path}):
            code, _ = run_captured(manage.verify_e2e, self.db, "student@example.com",
                                   1, False, False, store=True, pull_date="2026-01-05")
        self.assertEqual(code, 0)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[14].isoformat(), "2026-01-05",
                         "闸门放行后应把已解析的日期交给 _e2e_run（target_day）")

    def test_naming_a_different_database_is_still_refused(self) -> None:
        # The hole in the first cut of this guard: it only checked that the
        # variable was *set*. The server's unit file need not set INFE_PILOT_DB
        # at all, so "E2E_STORE_DB=/tmp/x" plus an unset INFE_PILOT_DB would have
        # written to the production default while looking sanctioned. The name
        # has to agree with the database this run actually opens.
        user = self._user()
        self._mailbox(user["id"])
        with mock.patch.object(manage, "_e2e_run") as run, \
             mock.patch("pilot_app.security.SecretBox.from_environment",
                        staticmethod(lambda: self.box)), \
             mock.patch.dict(os.environ, {manage.STORE_TARGET_ENV: "/tmp/somewhere-else.sqlite3"}):
            code, printed = run_captured(manage.verify_e2e, self.db, "student@example.com",
                                         1, False, False, store=True, pull_date="2026-01-05")
        self.assertEqual(code, 2)
        self.assertIn("拒绝执行", printed)
        self.assertIn("/tmp/somewhere-else.sqlite3", printed, "要把两个路径都摆出来给人对")
        self.assertIn(self.db.path, printed)
        run.assert_not_called()

    def test_reading_runs_are_not_gated(self) -> None:
        # No --store means no writes, so the guard must stay out of the way --
        # otherwise the guard would have "fixed" a hazard by disabling the tool.
        self.assertIsNone(manage._store_rows_refusal(False, "", "/var/lib/cityu-mail-pilot/pilot.sqlite3"))
        self.assertIsNone(manage._store_rows_refusal(False, "2026-01-05", "/tmp/x.sqlite3"))
        self.assertEqual(manage._store_rows_refusal(True, "not-a-date", "/tmp/x.sqlite3")[0], 2)

    def test_path_comparison_ignores_spelling(self) -> None:
        # /tmp/x and /tmp/./x are the same file, and a guard that says otherwise
        # would refuse a run that is actually pointed at the right database.
        self.assertTrue(manage._same_path("/tmp/x.sqlite3", "/tmp/./x.sqlite3"))
        self.assertTrue(manage._same_path("/var/lib/cityu-mail-pilot/pilot.sqlite3",
                                          "/var/lib/cityu-mail-pilot/pilot.sqlite3"))
        self.assertFalse(manage._same_path("/tmp/x.sqlite3", "/tmp/y.sqlite3"))
        self.assertFalse(manage._same_path("/tmp/x.sqlite3", ""))

    # -- pure helpers ------------------------------------------------------

    def test_the_model_override_only_touches_model_connections(self) -> None:
        connection = {"provider": "deepseek", "model": "deepseek-chat"}
        self.assertEqual(manage._override_model(connection, "model", "deepseek-flash")["model"],
                         "deepseek-flash")
        self.assertEqual(manage._override_model(connection, "search", "whatever")["model"],
                         "deepseek-chat")
        self.assertIsNone(manage._override_model(None, "model", "deepseek-flash"))

    def test_the_override_copies_instead_of_mutating(self) -> None:
        # The caller passes the row it read from the database; editing it in
        # place would leak the override into everything else holding that dict.
        connection = {"provider": "deepseek", "model": "deepseek-chat"}
        manage._override_model(connection, "model", "deepseek-flash")
        self.assertEqual(connection["model"], "deepseek-chat")
