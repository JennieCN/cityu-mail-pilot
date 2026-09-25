# -*- coding: utf-8 -*-
"""Tests for the operator tools in ``pilot_app/manage.py``.

These commands are what an operator runs *while something is wrong*, which makes
two kinds of bug expensive: one that prints a credential into a terminal or an
e-mail, and one that answers confidently when it could not actually check
anything. The tests below are therefore about properties, not line coverage:

* the masking helper shows at most the first two local characters;
* the env and credential readers cannot overwrite a running deployment;
* a command that cannot run says so in text instead of raising;
* unit-failure mail is scrubbed before it leaves the machine, and a send that
  fails still exits non-zero without raising;
* the two commands documented to work on a host with no database still do.
"""

import base64
import contextlib
import datetime as dt
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

from pilot_app import manage, worker

_TMP = tempfile.mkdtemp()
_TEST_KEY = base64.urlsafe_b64encode(b"\x00" * 32).decode()


def main(*args):
    """Run ``manage.main`` the way the shell does, capturing both streams."""
    out, err = io.StringIO(), io.StringIO()
    with mock.patch("sys.argv", ["manage.py", *args]), \
         contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = manage.main()
    return code, out.getvalue(), err.getvalue()


@contextlib.contextmanager
def fake_environment_key():
    """Give ``SecretBox.from_environment`` a valid key without a real secret."""
    with mock.patch("pilot_app.security.SecretBox.from_environment",
                    staticmethod(lambda: manage.SecretBox.from_base64(_TEST_KEY))):
        yield


class MaskTests(unittest.TestCase):
    def test_at_most_two_local_characters_are_shown(self):
        # Deliberately *not* a school address, and deliberately not written out
        # here either. The published tree scrubs real school addresses out of
        # every file it ships, rewriting the local part -- but it cannot rewrite
        # the *expected* string next to it, because a masked local part contains
        # a `*` and the pattern will not match it. So the two halves of an
        # assertion using a real school domain stop describing the same address,
        # it passes on this machine and fails only in CI, on the published tree.
        # That is what the first run of the pipeline found. The domain has
        # nothing to do with what `_mask` does, so it is a neutral one.
        for address, expected in (
            ("someone@example.com", "so***@example.com"),
            ("ab@example.org", "ab***@example.org"),
            ("a@b.com", "a***@b.com"),
        ):
            self.assertEqual(manage._mask(address), expected)

    def test_a_longer_local_part_cannot_be_read_back(self):
        self.assertNotIn("verylongname", manage._mask("verylongname@example.com"))

    def test_something_without_a_domain_is_stars(self):
        for value in ("", "not-an-address", None):
            self.assertEqual(manage._mask(value), "***")


class EnvFileTests(unittest.TestCase):
    def setUp(self):
        self.path = pathlib.Path(_TMP) / "pilot.env"
        self.saved = dict(os.environ)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.saved)

    def test_it_reads_infe_values_and_strips_quotes(self):
        self.path.write_text('INFE_PILOT_A=1\nINFE_PILOT_B="two words"\n', encoding="utf-8")
        os.environ.pop("INFE_PILOT_A", None)
        os.environ.pop("INFE_PILOT_B", None)
        manage._load_env_file(str(self.path))
        self.assertEqual(os.environ["INFE_PILOT_A"], "1")
        self.assertEqual(os.environ["INFE_PILOT_B"], "two words")

    def test_it_ignores_comments_blank_lines_and_foreign_keys(self):
        self.path.write_text("\n# INFE_PILOT_SKIP=nope\nOTHER=1\nbroken line\nINFE_PILOT_OK=yes\n",
                             encoding="utf-8")
        os.environ.pop("INFE_PILOT_SKIP", None)
        os.environ.pop("INFE_PILOT_OK", None)
        manage._load_env_file(str(self.path))
        self.assertNotIn("INFE_PILOT_SKIP", os.environ)
        self.assertNotIn("OTHER", os.environ)
        self.assertEqual(os.environ.get("INFE_PILOT_OK"), "yes")

    def test_it_never_overwrites_a_value_that_is_already_set(self):
        """The file supplies *defaults*. If it could overwrite, running a
        diagnostic with the wrong file would silently point the tool at the
        wrong database or the wrong key -- and say nothing."""
        self.path.write_text("INFE_PILOT_A=from-file\n", encoding="utf-8")
        os.environ["INFE_PILOT_A"] = "from-environment"
        manage._load_env_file(str(self.path))
        self.assertEqual(os.environ["INFE_PILOT_A"], "from-environment")

    def test_a_missing_file_is_reported_not_raised(self):
        with contextlib.redirect_stdout(io.StringIO()) as out_buffer:
            manage._load_env_file(str(pathlib.Path(_TMP) / "nope.env"))
        self.assertIn("读取环境文件失败", out_buffer.getvalue())


class CredentialFileTests(unittest.TestCase):
    def setUp(self):
        self.path = pathlib.Path(_TMP) / "cred.txt"

    def _write(self, text: str):
        self.path.write_text(text, encoding="utf-8")
        return str(self.path)

    def test_the_email_and_password_shape(self):
        self.assertEqual(manage.read_credential_file(self._write("email=me@example.com\npassword=app-pass\n")),
                         ("me@example.com", "app-pass"))

    def test_the_aliases_the_help_text_promises(self):
        self.assertEqual(manage.read_credential_file(self._write("user=me@example.com\ncode=app-pass\n")),
                         ("me@example.com", "app-pass"))

    def test_a_lone_password_line(self):
        """Some providers hand you only a code; the file may hold just that."""
        self.assertEqual(manage.read_credential_file(self._write("# 注释\n\napp-pass\n")),
                         ("", "app-pass"))

    def test_the_last_lone_line_wins(self):
        self.assertEqual(manage.read_credential_file(self._write("first\nsecond\n")), ("", "second"))

    def test_a_missing_file_returns_nothing_rather_than_raising(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            result = manage.read_credential_file(str(pathlib.Path(_TMP) / "nope.txt"))
        self.assertEqual(result, ("", ""))
        self.assertIn("读取凭据文件失败", out.getvalue())


class DiagnoseForwardingArgumentTests(unittest.TestCase):
    """The argument pre-flight, which runs before anything touches a network."""

    def test_a_missing_address_is_refused(self):
        with mock.patch.object(manage, "diagnose_forwarding") as diag:
            code, out, _ = main("diagnose-forwarding")
        self.assertEqual(code, 2)
        self.assertIn("--email", out)
        diag.assert_not_called()

    def test_an_empty_password_is_refused(self):
        with mock.patch("getpass.getpass", return_value=""), \
             mock.patch.object(manage, "diagnose_forwarding") as diag:
            code, out, _ = main("diagnose-forwarding", "--email", "me@example.com",
                                "--password-env", "INFE_DIAG_PASSWORD_ABSENT")
        self.assertEqual(code, 2)
        self.assertIn("没有提供授权码", out)
        diag.assert_not_called()

    def test_the_credential_file_fills_in_both_fields(self):
        path = pathlib.Path(_TMP) / "diag.txt"
        path.write_text("user=me@example.com\ncode=app-pass\n", encoding="utf-8")
        with mock.patch.object(manage, "diagnose_forwarding", return_value=0) as diag, \
             fake_environment_key():
            code, out, _ = main("diagnose-forwarding", "--password-file", str(path))
        self.assertEqual(code, 0)
        args, kwargs = diag.call_args
        self.assertEqual(args[0], "me@example.com")
        self.assertEqual(args[1], "app-pass")
        # Invariant 2: the file may hold a credential, the terminal output may not.
        self.assertNotIn("app-pass", out)

    def test_the_env_file_is_read_before_the_password_env(self):
        env = pathlib.Path(_TMP) / "diag.env"
        env.write_text("INFE_DIAG_FROM_FILE=from-file\n", encoding="utf-8")
        saved = os.environ.pop("INFE_DIAG_FROM_FILE", None)
        try:
            with mock.patch.object(manage, "diagnose_forwarding", return_value=0) as diag, \
                 fake_environment_key():
                main("diagnose-forwarding", "--email", "me@example.com",
                     "--env-file", str(env), "--password-env", "INFE_DIAG_FROM_FILE")
            self.assertEqual(diag.call_args[0][1], "from-file")
        finally:
            os.environ.pop("INFE_DIAG_FROM_FILE", None)
            if saved is not None:
                os.environ["INFE_DIAG_FROM_FILE"] = saved


def _headers(message_id: str, subject: str = "Hello", to: str = "me@example.com",
             delivered_to: str = "me@example.com") -> bytes:
    return (f"Message-ID: <{message_id}>\r\nSubject: {subject}\r\n"
            f"Date: Thu, 11 Sep 2026 10:00:00 +0800\r\nTo: {to}\r\n"
            f"Delivered-To: {delivered_to}\r\n\r\n").encode("utf-8")


class FakeIMAP:
    """The smallest IMAP server that can answer this one diagnosis."""

    def __init__(self, messages: dict[str, bytes], fail_login: bool = False,
                 fail_search: bool = False):
        self.messages = messages
        self.fail_login = fail_login
        self.fail_search = fail_search
        self.commands: list[tuple] = []
        self.closed = False
        self.logged_out = False

    def login(self, address, password):
        self.commands.append(("login", address, password))
        if self.fail_login:
            raise RuntimeError("login failed: authenticationfailed")
        return "OK", [b"1"]

    def select(self, folder, readonly=False):
        self.commands.append(("select", folder, readonly))
        return "OK", [b"1"]

    def uid(self, command, *args):
        self.commands.append(("uid", command, args))
        if command == "search":
            if self.fail_search:
                raise RuntimeError("search blew up")
            return "OK", [b" ".join(uid.encode() for uid in self.messages)]
        return "OK", [(b"x", self.messages[args[0]])]

    def close(self):
        self.closed = True
        return "OK", []

    def logout(self):
        self.logged_out = True
        return "BYE", []


class DiagnoseForwardingTests(unittest.TestCase):
    """Invariant 2 and the read-only rule, on the one tool that opens a real
    user's mailbox by hand."""

    def setUp(self):
        self.server = None

    def _install(self, messages, **kwargs):
        self.server = FakeIMAP(messages, **kwargs)
        patcher = mock.patch("imaplib.IMAP4_SSL", return_value=self.server)
        patcher.start()
        self.addCleanup(patcher.stop)
        return self.server

    def _run(self, messages, **kwargs):
        self._install(messages, **kwargs)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = manage.diagnose_forwarding("someone@example.com", "app-pass")
        return code, out.getvalue()

    def test_the_folder_is_opened_read_only(self):
        """The whole tool exists to look; a write here would mark a user's real
        mail as read."""
        self._run({"1": _headers("a@b")})
        selects = [c for c in self.server.commands if c[0] == "select"]
        self.assertEqual(selects, [("select", "INBOX", True)])

    def test_only_headers_are_fetched_and_never_a_body(self):
        self._run({"1": _headers("a@b")})
        fetches = [c for c in self.server.commands if c[0] == "uid" and c[1] == "fetch"]
        self.assertTrue(fetches)
        for _, _, args in fetches:
            spec = args[1]
            self.assertIn("BODY.PEEK[HEADER.FIELDS", spec)
            self.assertNotIn("BODY[", spec)
            self.assertNotIn("TEXT", spec)

    def test_the_password_never_reaches_the_output(self):
        code, out = self._run({"1": _headers("a@b")})
        self.assertEqual(code, 0)
        self.assertNotIn("app-pass", out)

    def test_the_whole_address_is_masked(self):
        code, out = self._run({"1": _headers("a@b")})
        self.assertNotIn("someone@example.com", out)
        self.assertIn("som***@example.com", out)

    def test_two_copies_of_one_message_are_the_duplicate_verdict(self):
        code, out = self._run({
            "1": _headers("same@id", delivered_to="me@example.com"),
            "2": _headers("same@id", delivered_to="me2@example.com"),
        })
        self.assertEqual(code, 0)
        self.assertIn("重复 Message-ID：1 组", out)
        self.assertIn("同一封邮件被投递了两遍", out)

    def test_different_message_ids_are_not_a_duplicate(self):
        code, out = self._run({"1": _headers("one@id"), "2": _headers("two@id")})
        self.assertEqual(code, 0)
        self.assertIn("重复 Message-ID：0 组", out)
        self.assertIn("未发现相同 Message-ID", out)

    def test_a_message_without_an_id_is_not_grouped_with_another(self):
        """Two forwards can legitimately arrive with no Message-ID; calling them
        a duplicate would send the operator chasing a rule that is fine."""
        code, out = self._run({"1": b"Subject: A\r\n\r\n", "2": b"Subject: B\r\n\r\n"})
        self.assertEqual(code, 0)
        self.assertIn("重复 Message-ID：0 组", out)

    def test_only_the_most_recent_messages_are_checked(self):
        self._install({str(n): _headers(f"{n}@id") for n in range(1, 11)})
        with contextlib.redirect_stdout(io.StringIO()):
            manage.diagnose_forwarding("someone@example.com", "app-pass", limit=3)
        fetched = [c[2][0] for c in self.server.commands if c[0] == "uid" and c[1] == "fetch"]
        self.assertEqual(fetched, ["8", "9", "10"])

    def test_a_login_failure_is_reported_and_the_connection_is_dropped(self):
        self._install({}, fail_login=True)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = manage.diagnose_forwarding("someone@example.com", "app-pass")
        self.assertEqual(code, 1)
        self.assertIn("登录失败", out.getvalue())
        self.assertIn("授权码", out.getvalue())

    def test_the_connection_is_dropped_even_when_the_search_raises(self):
        """A leaked connection keeps the mailbox locked for everyone else."""
        self._install({}, fail_search=True)
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                manage.diagnose_forwarding("someone@example.com", "app-pass")
        self.assertTrue(self.server.closed)
        self.assertTrue(self.server.logged_out)


class RunTests(unittest.TestCase):
    def test_a_missing_command_is_text_not_an_exception(self):
        """``_run`` feeds text that gets e-mailed; a traceback here would replace
        the unit's real failure with ours."""
        self.assertIn("无法运行", manage._run(["definitely-not-a-real-command-xyz"]))

    def test_output_is_returned(self):
        self.assertIn("ok", manage._run(["echo", "ok"]))


class NotifyUnitFailureTests(unittest.TestCase):
    """The ``OnFailure=`` path, which has to work while other things are broken."""

    def setUp(self):
        self.db = mock.MagicMock()
        self.sent: list[tuple] = []
        # This handler is dispatched *after* the database is opened, because it
        # borrows a real mailbox to send. It is the one command whose whole job
        # is to work when something else has already broken, so the storage it
        # needs is worth naming: it is not optional.
        patcher = mock.patch.object(manage, "Database", return_value=self.db)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _send(self, db, box, subject, text):
        self.sent.append((subject, text))
        return ["ops@example.com"]

    def test_an_empty_unit_is_refused_without_mailing(self):
        with mock.patch("pilot_app.alerting.send_admin_mail", side_effect=self._send):
            code, out, _ = main("notify-unit-failure", "--unit", "   ")
        self.assertEqual(code, 2)
        self.assertIn("缺少 --unit", out)
        self.assertEqual(self.sent, [])

    def test_the_mail_carries_no_credential_from_the_environment(self):
        """Invariant 2, with a message that literally embeds command output."""
        secret = "SUPER-SECRET-APP-PASSWORD-0123456789"
        with mock.patch.object(manage, "_run", return_value=f"Environment=CODE={secret}\nboom"), \
             mock.patch("pilot_app.alerting.collect_secret_values", return_value={secret}), \
             mock.patch("pilot_app.alerting.send_admin_mail", side_effect=self._send), \
             fake_environment_key():
            code, out, _ = main("notify-unit-failure", "--unit", "cityu-mail-pilot-worker")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.sent), 1)
        subject, text = self.sent[0]
        self.assertNotIn(secret, text)
        self.assertIn("cityu-mail-pilot-worker", subject)

    def test_the_mail_says_how_to_look_further(self):
        with mock.patch.object(manage, "_run", return_value="status"), \
             mock.patch("pilot_app.alerting.collect_secret_values", return_value=set()), \
             mock.patch("pilot_app.alerting.send_admin_mail", side_effect=self._send), \
             fake_environment_key():
            main("notify-unit-failure", "--unit", "cityu-mail-pilot-web")
        self.assertIn("systemctl status --full cityu-mail-pilot-web", self.sent[0][1])

    def test_the_journal_tail_is_capped(self):
        captured: dict = {}

        def fake_run(command):
            captured["command"] = command
            return "x"

        with mock.patch.object(manage, "_run", side_effect=fake_run), \
             mock.patch("pilot_app.alerting.collect_secret_values", return_value=set()), \
             mock.patch("pilot_app.alerting.send_admin_mail", side_effect=self._send), \
             fake_environment_key():
            main("notify-unit-failure", "--unit", "cityu-mail-pilot-worker", "--lines", "9999")
        self.assertIn("200", captured["command"])
        self.assertNotIn("9999", captured["command"])

    def test_a_send_failure_exits_non_zero_and_does_not_raise(self):
        """A handler that raises would obscure the original failure, and
        ``OnFailure=`` must not recurse."""
        with mock.patch.object(manage, "_run", return_value="status"), \
             mock.patch("pilot_app.alerting.collect_secret_values", return_value=set()), \
             mock.patch("pilot_app.security.SecretBox.from_environment",
                        side_effect=RuntimeError("no master key")):
            code, out, err = main("notify-unit-failure", "--unit", "cityu-mail-pilot-backup")
        self.assertEqual(code, 1)
        self.assertIn("无法发出单元失败告警", err)


class CheckAlertsTests(unittest.TestCase):
    def test_dry_run_prints_and_sends_nothing(self):
        findings = [{"severity": "warning", "key": "mailbox_error:usr_1",
                     "title": "收信失败", "detail": "授权码被拒"}]
        database = mock.MagicMock()
        database.list_alert_states.return_value = []
        with mock.patch.object(manage, "Database", return_value=database), \
             mock.patch("pilot_app.alerting.evaluate", return_value=findings), \
             mock.patch("pilot_app.alerting.run_checks") as ran:
            code, out, _ = main("check-alerts", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("mailbox_error:usr_1", out)
        self.assertIn("DRY RUN", out)
        ran.assert_not_called()

    def test_dry_run_does_not_call_a_muted_or_console_only_finding_an_alert(self):
        """「会告警」这句话必须与哨兵真正会做的事一致。

        以前它只是把活跃异常全部列出来，于是**按过「已知晓」的、只在面板显示的**
        都被算进「N 项会告警」。2026-09-16 在生产上真印出过「2 项会告警」，其中
        一项是运营者自己静音掉的——一个诊断命令说出与它诊断的对象相反的话，
        比它不说话更糟。
        """
        findings = [
            {"severity": "critical", "key": "tls_cert", "title": "证书快到期", "detail": "还剩 9 天"},
            {"severity": "warning", "key": "mailbox_error:usr_1", "title": "收信失败",
             "detail": "授权码被拒"},
            {"severity": "warning", "key": "setup_stalled:usr_2", "title": "注册后没配完",
             "detail": "注册超过 12 小时仍未完成"},
            {"severity": "warning", "key": "mailbox_error:usr_3", "title": "收信失败",
             "detail": "授权码被拒"},
        ]
        database = mock.MagicMock()
        # `setup_stalled` 那一行的时间戳必须**相对现在**算：汇总档的窗口是
        # 「这一档上一次发信到现在满没满 24 小时」，写死一个日期的话，测试会在那一天
        # 之后自己变红 —— 2026-09-17 就真发生了（10:2x UTC 跑的时候，写死的
        # 2026-09-16T09:00Z 已经过了一天，于是那一档从「等汇总窗口」变成「会进今天的
        # 汇总」，断言「1 项现在会发信」当场失败，而产品没有任何问题）。
        recent = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat(timespec="seconds")
        database.list_alert_states.return_value = [
            {"key": "mailbox_error:usr_1", "acknowledged_at": "2026-09-16T10:00:00+00:00",
             "open": 1, "detail": "授权码被拒", "last_sent_at": recent},
            {"key": "setup_stalled:usr_2", "acknowledged_at": None, "open": 1,
             "detail": "注册超过 12 小时仍未完成", "last_sent_at": recent},
        ]
        with mock.patch.object(manage, "Database", return_value=database), \
             mock.patch("pilot_app.alerting.evaluate", return_value=findings), \
             mock.patch("pilot_app.alerting.run_checks") as ran:
            code, out, _ = main("check-alerts", "--dry-run")
        self.assertEqual(code, 0)
        ran.assert_not_called()
        self.assertIn("1 项现在会发信", out, out)
        self.assertIn("1 项已被「已知晓」静音", out, out)
        # 面板档那一条（没被静音的那个）：它本来就不发信，所以既不计入会发信，
        # 也不计入静音，而且那行自己写着「只在面板显示」。
        self.assertIn("只在面板显示", out, out)
        self.assertIn("2 项这一轮不发", out, out)
        self.assertNotIn("4 项会告警", out)

    def test_a_real_run_reports_failure_through_the_exit_code(self):
        """``OnFailure=`` and cron read the exit code, not the prose."""
        with mock.patch.object(manage, "Database"), fake_environment_key(), \
             mock.patch("pilot_app.alerting.run_checks",
                        return_value={"errors": ["boom"], "sent": 0}):
            code, out, _ = main("check-alerts")
        self.assertEqual(code, 1)
        self.assertIn("哨兵结果", out)

    def test_a_clean_run_exits_zero(self):
        with mock.patch.object(manage, "Database"), fake_environment_key(), \
             mock.patch("pilot_app.alerting.run_checks",
                        return_value={"errors": [], "sent": 1}):
            code, _, _ = main("check-alerts")
        self.assertEqual(code, 0)


class DigTests(unittest.TestCase):
    def test_it_walks_a_nested_path(self):
        self.assertEqual(manage._dig({"a": {"b": {"c": 1}}}, "a.b.c"), 1)

    def test_a_missing_key_is_none_not_an_error(self):
        self.assertIsNone(manage._dig({"a": {}}, "a.b.c"))

    def test_a_non_dict_in_the_middle_is_none(self):
        self.assertIsNone(manage._dig({"a": [1, 2]}, "a.b"))

    def test_a_top_level_scalar_is_none(self):
        self.assertIsNone(manage._dig({}, "a"))


class NoDatabaseNeededTests(unittest.TestCase):
    """Two commands are documented to work on a host with no database.

    ``check-model`` and ``check-search`` are what someone runs *while setting a
    key up*, which is exactly when the app may not be installed yet. Opening a
    database there made them fail for a reason that had nothing to do with the
    key. That is a property worth pinning down, not a comment worth trusting.
    """

    def test_check_model_never_opens_the_database(self):
        with mock.patch.object(manage, "Database") as database, \
             mock.patch.object(manage, "check_model", return_value=0) as called:
            code, _, _ = main("check-model")
        self.assertEqual(code, 0)
        called.assert_called_once()
        database.assert_not_called()

    def test_check_search_never_opens_the_database(self):
        with mock.patch.object(manage, "Database") as database, \
             mock.patch.object(manage, "check_search", return_value=0):
            main("check-search")
        database.assert_not_called()

    def test_generate_master_key_prints_one_usable_key_and_touches_nothing(self):
        with mock.patch.object(manage, "Database") as database:
            code, out, _ = main("generate-master-key")
        self.assertEqual(code, 0)
        database.assert_not_called()
        value = out.strip()
        self.assertEqual(len(value), 44, "32 字节的 base64 应该正好 44 个字符")
        self.assertEqual(len(base64.urlsafe_b64decode(value)), 32)

    def test_a_command_that_needs_storage_does_open_it(self):
        """The other half: the exceptions above must not have swallowed the rule."""
        db = mock.MagicMock()
        with mock.patch.object(manage, "Database", return_value=db) as database, \
             mock.patch.object(manage, "check_metrics", return_value=0):
            main("check-metrics")
        database.assert_called_once()
        db.initialize.assert_called_once()


class CreateInviteTests(unittest.TestCase):
    def test_it_prints_a_code_and_stores_only_its_hash(self):
        """The code is the credential. If the plaintext ever reached the table,
        a database leak would hand out working invitations."""
        db = mock.MagicMock()
        connection = db.connect.return_value.__enter__.return_value
        with mock.patch.object(manage, "Database", return_value=db):
            code, out, _ = main("create-invite", "--label", "pilot", "--days", "7")
        self.assertEqual(code, 0)
        value = out.strip()
        self.assertGreaterEqual(len(value), 20)
        statement, params = connection.execute.call_args[0]
        self.assertIn("INSERT INTO invites", statement)
        self.assertNotIn(value, params)
        self.assertEqual(params[1], "pilot")


class VerifyE2ETests(unittest.TestCase):
    def test_an_unknown_account_is_reported_masked(self):
        db = mock.MagicMock()
        db.find_user_for_login.return_value = None
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = manage.verify_e2e(db, "nobody@example.com", 5, False, False)
        self.assertEqual(code, 2)
        self.assertIn("找不到试点用户", out.getvalue())
        self.assertNotIn("nobody@example.com", out.getvalue())

    def test_a_deleted_account_is_refused(self):
        db = mock.MagicMock()
        db.find_user_for_login.return_value = {"id": "usr_1", "email": "gone@example.com",
                                              "status": "deleted"}
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = manage.verify_e2e(db, "gone@example.com", 5, False, False)
        self.assertEqual(code, 2)
        self.assertIn("已删除", out.getvalue())

    def test_an_account_without_a_mailbox_is_told_so(self):
        db = mock.MagicMock()
        db.find_user_for_login.return_value = {"id": "usr_1", "email": "new@example.com",
                                              "status": "active"}
        db.get_mailbox.return_value = None
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = manage.verify_e2e(db, "new@example.com", 5, False, False)
        self.assertEqual(code, 2)
        self.assertIn("还没有配置邮箱", out.getvalue())


class MasterKeyVerifiedTests(unittest.TestCase):
    """`manage master-key-verified` —— 只是把「人核对过」这件事记上日期。

    它**看不到**离线副本，这一条是它的定义而不是缺陷：能记的只有「什么时候有人比过」，
    所以它必须（a）写下一个可解析的时间与当时那把钥匙的指纹，（b）`--show` 绝不写入，
    （c）发现记下的指纹与现在这把不同就说出来——那正是换钥匙或恢复旧备份的样子。
    """

    def _db(self, stored=None):
        db = mock.MagicMock()
        values = dict(stored or {})
        db.get_setting.side_effect = lambda key, default="": values.get(key, default)
        db.set_setting.side_effect = lambda key, value, **_: values.__setitem__(key, value)
        return db, values

    def test_it_records_a_parseable_time_and_the_live_fingerprint(self):
        from pilot_app.security import SecretBox
        import datetime as dt

        expected = SecretBox.from_base64(_TEST_KEY).fingerprint()
        db, values = self._db()
        with fake_environment_key(), mock.patch.object(manage, "Database", return_value=db):
            code, out, _ = main("master-key-verified", "--note", "密码管理器 + 打印件")
        self.assertEqual(code, 0)
        self.assertTrue(dt.datetime.fromisoformat(values["master_key_verified_at"]),
                        "必须是可解析的 UTC ISO")
        self.assertEqual(values["master_key_verified_fingerprint"], expected)
        self.assertEqual(values["master_key_verified_note"], "密码管理器 + 打印件")
        self.assertIn(expected, out)
        self.assertNotIn(_TEST_KEY, out, "指纹可以打印，钥匙不行")

    def test_show_only_reads(self):
        db, _ = self._db()
        with fake_environment_key(), mock.patch.object(manage, "Database", return_value=db):
            code, out, _ = main("master-key-verified", "--show")
        self.assertEqual(code, 0)
        self.assertIn("从未记录", out)
        db.set_setting.assert_not_called()

    def test_a_changed_key_is_pointed_out(self):
        db, _ = self._db({"master_key_verified_fingerprint": "AAAA-BBBB-CCCC"})
        with fake_environment_key(), mock.patch.object(manage, "Database", return_value=db):
            code, out, _ = main("master-key-verified")
        self.assertEqual(code, 0)
        self.assertIn("AAAA-BBBB-CCCC", out)
        self.assertIn("注意", out)


if __name__ == "__main__":
    unittest.main()


class CheckNativeSearchTests(unittest.TestCase):
    """`manage check-native-search`（第 16 项：方舟原生联网搜索）。

    它存在的原因是**这条路径写的时候验证不了**：方舟的联网内容插件只在
    `/api/v3/responses` 上，而这台机器的三把 key 没有一把是方舟的（拿真端点试过，
    全部 `AuthenticationError: The API key format is incorrect`）。一个从没被执行过的
    成功路径是**主张**不是事实，所以给运营者（或任何有方舟 key 的人）留了这条命令。
    """

    def test_an_unsupported_provider_is_refused_by_name(self):
        code, out, _ = main("check-native-search", "--provider", "deepseek", "--model", "x")
        self.assertEqual(code, 2)
        self.assertIn("不支持原生联网搜索", out)
        # 拒绝的时候顺手把「谁会搜索」列出来，省得去翻代码。
        self.assertIn("volcengine_ark_responses", out)

    def test_ark_needs_an_explicit_model(self):
        """方舟的模型名是账号里开通的 ID/接入点，不能替用户猜。"""
        with mock.patch.object(manage.providers, "platform_model_key", return_value="k"):
            code, out, _ = main("check-native-search", "--provider", "volcengine_ark_responses")
        self.assertEqual(code, 2)
        self.assertIn("--model", out)

    def test_a_named_provider_does_not_borrow_the_platform_model(self):
        """显式点名 volcengine_ark_responses 时，不该拿平台那把 key 的模型名
        （deepseek-flash 发到方舟端点上是个 404，看起来像 key 坏了）。"""
        platform = {"provider": "deepseek", "model": "deepseek-flash", "base_url": ""}
        with mock.patch.object(manage.providers, "platform_model_default", return_value=platform), \
             mock.patch.object(manage.providers, "platform_model_key", return_value="k"):
            code, out, _ = main("check-native-search", "--provider", "volcengine_ark_responses")
        self.assertEqual(code, 2)
        self.assertIn("--model", out)
        self.assertNotIn("deepseek-flash", out)

    def test_no_key_says_why_it_cannot_check(self):
        with mock.patch.object(manage.providers, "platform_model_key", return_value=""):
            code, out, _ = main("check-native-search", "--provider", "volcengine_ark_responses",
                                "--model", "doubao-test")
        self.assertEqual(code, 1)
        self.assertIn("INFE_PILOT_DEFAULT_MODEL_KEY", out)

    def test_a_call_without_citations_is_a_failure(self):
        """「调用成功」不是断言：模型不搜索也会答得很好看。**零引用 = 没证明**。"""
        result = manage.providers.Generation("我凭记忆回答", [], "native", None)
        with mock.patch.object(manage.providers, "platform_model_key", return_value="secret-key-value"), \
             mock.patch.object(manage.providers, "generate", return_value=result):
            code, out, _ = main("check-native-search", "--provider", "volcengine_ark_responses",
                                "--model", "doubao-test")
        self.assertEqual(code, 1)
        self.assertIn("没有拿到任何引用来源", out)

    def test_citations_make_it_pass_and_the_key_is_never_printed(self):
        result = manage.providers.Generation(
            "答案", [{"title": "城大", "url": "https://www.cityu.edu.hk/", "summary": ""}], "native", None)
        with mock.patch.object(manage.providers, "platform_model_key", return_value="secret-key-value"), \
             mock.patch.object(manage.providers, "generate", return_value=result) as called:
            code, out, _ = main("check-native-search", "--provider", "volcengine_ark_responses",
                                "--model", "doubao-test", "--query", "City University of Hong Kong")
        self.assertEqual(code, 0)
        self.assertNotIn("secret-key-value", out)
        self.assertIn("来源 1 条", out)
        self.assertIn("https://www.cityu.edu.hk/", out)
        # 真的带上了原生搜索开关，而不是只调了一次模型。
        self.assertTrue(called.call_args.kwargs["native_search"])

    def test_the_keyword_limit_is_clamped_before_it_reaches_the_api(self):
        result = manage.providers.Generation("x", [{"title": "t", "url": "https://e.com/", "summary": ""}], "native", None)
        with mock.patch.object(manage.providers, "platform_model_key", return_value="k"), \
             mock.patch.object(manage.providers, "generate", return_value=result) as called:
            main("check-native-search", "--provider", "volcengine_ark_responses",
                 "--model", "m", "--keyword-limit", "999")
        self.assertEqual(called.call_args.kwargs["config"], {"search_max_keyword": 50})

    def test_it_needs_no_database(self):
        """和 check-model / check-search 一样，装都还没装好时就该能用。"""
        with mock.patch.object(manage, "Database") as database, \
             mock.patch.object(manage.providers, "platform_model_key", return_value="k"):
            main("check-native-search", "--provider", "deepseek")
        database.assert_not_called()


class PollIntervalCommandTests(unittest.TestCase):
    """`manage poll-interval`：把「轮询间隔」这个旋钮的账印出来。

    2026-09-26 的由来：这个值在 2026-09-24 为了 1500 个邮箱的规模从 60 秒被改成 300 秒，
    而当时真实的规模是 15 个邮箱；用户两天后报「从收到转发邮件到收到处理好的邮件太久了」。
    改动本身没错，错在这个值**只写在一个环境变量里，没有任何地方告诉你它的代价**。
    这几条钉住那条命令说得对：数字算得对、把 Gmail 那一档分出来、跑不完时非零退出。
    """

    @staticmethod
    def _boxes(count, *, host="imap.qq.com", enabled=1):
        return [{"id": f"mbx_{i}", "email": f"u{i}@qq.com", "report_to": f"u{i}@qq.com",
                 "imap_host": host, "imap_port": 993, "enabled": enabled,
                 "status": "active"} for i in range(count)]

    def _run(self, boxes, *argv, **worker_values):
        database = mock.MagicMock()
        database.all_mailboxes.return_value = list(boxes)
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(manage, "Database", return_value=database))
            for name, value in worker_values.items():
                stack.enter_context(mock.patch.object(worker, name, value))
            return main("poll-interval", *argv)

    def test_it_prints_the_rate_the_round_and_the_delay(self):
        code, out, _ = self._run(self._boxes(20), POLL_SECONDS=60, POLL_WORKERS=4)
        self.assertEqual(code, 0)
        self.assertIn("轮询间隔    60 秒", out)
        self.assertIn("在用邮箱    20 个", out)
        self.assertIn("0.33 次/秒", out)
        self.assertIn("28,800 次/天", out)
        self.assertIn("一轮轮询    约 10 秒", out)
        self.assertIn("发现延迟    0–60 秒", out)
        self.assertIn("中位约 30 秒", out)

    def test_a_round_that_cannot_finish_exits_non_zero(self):
        """一轮比间隔还长 = 实际间隔会被悄悄拉长、邮箱会一路显示成「轮询停了」。"""
        code, out, _ = self._run(self._boxes(100), POLL_SECONDS=60, POLL_WORKERS=1)
        self.assertEqual(code, 1)
        self.assertIn("跑不完", out)

    def test_the_slower_provider_floor_is_named_not_smoothed_over(self):
        """Gmail 的 900 秒是供应商的红线：同一句「间隔」下它其实是另一档。"""
        boxes = self._boxes(4) + self._boxes(2, host="imap.gmail.com")
        code, out, _ = self._run(boxes, POLL_SECONDS=60, POLL_WORKERS=4)
        self.assertEqual(code, 0)
        self.assertIn("其中 2 个有更慢的供应商下限", out)

    def test_paused_mailboxes_are_not_counted_as_load(self):
        """暂停的邮箱我们按设计不轮询 —— 算进登录量就是把账算多了。"""
        _, out, _ = self._run(self._boxes(3) + self._boxes(7, enabled=0),
                              POLL_SECONDS=60, POLL_WORKERS=4)
        self.assertIn("在用邮箱    3 个", out)
        self.assertIn("0.05 次/秒", out)

    def test_json_output_is_machine_readable(self):
        code, out, _ = self._run(self._boxes(20), "--json", POLL_SECONDS=60, POLL_WORKERS=4)
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["now"]["mailboxes"], 20)
        self.assertTrue(payload["now"]["fits"])
        self.assertEqual(payload["at_scale_target"]["mailboxes"],
                         worker.SCALE_TARGET_MAILBOXES)
        # 这条命令**不连任何邮箱**：没有网络调用、没有探针。
        self.assertEqual(set(payload), {"now", "at_scale_target",
                                        "paused_or_disabled_ignored",
                                        "slower_provider_floor"})
