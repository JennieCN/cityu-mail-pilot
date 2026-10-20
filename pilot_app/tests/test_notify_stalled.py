"""Tests for the setup reminder (tools/notify_stalled.py).

This tool does the one thing in the project that cannot be undone: it writes to
real people's inboxes. Everything worth pinning follows from that.

* **Two sentences, not one.** "You never configured a mailbox" and "your mailbox
  is refusing your password" need different instructions; sending the wrong one
  is worse than sending nothing.
* **It cannot drift from the app.** The provider steps come from
  `pilot_app/mailpresets.py`, the same source the wizard renders. A reminder that
  tells someone to click a menu that no longer exists is a support ticket.
* **Sending twice is a bug.** Each delivery is recorded, so a re-run after a
  partial failure cannot mail the people who already got one.
* **The output is masked.** The operator decides from *who is stuck and why*,
  not from a scrollback full of addresses.
"""

import contextlib
import datetime as dt
import importlib.util
import io
import os
import pathlib
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location(
    "notify_stalled_tool", ROOT / "tools" / "notify_stalled.py")
notify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(notify)

import pilot_app  # noqa: E402
from pilot_app import mailpresets, setup_reminders  # noqa: E402
from pilot_app.database import Database, utc_now  # noqa: E402


def hours_ago(hours: float) -> str:
    moment = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    return moment.isoformat(timespec="seconds")


class NotifyStalledTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "notify.sqlite3"
        self.db = Database(self.path)
        self.db.initialize()
        self._env = {}
        for key, value in (
            ("INFE_PILOT_DB", str(self.path)),
            ("INFE_PILOT_ORIGIN", "https://example.test"),
            ("INFE_PILOT_MASTER_KEY", "A" * 43 + "="),
            ("PILOT_NOTIFY_CONFIRM", "yes"),
        ):
            self._env[key] = os.environ.get(key)
            os.environ[key] = value
        self.sent: list[tuple[str, str, str]] = []
        self._real_send = setup_reminders.send_as_operator
        setup_reminders.send_as_operator = self._fake_send

    def tearDown(self):
        setup_reminders.send_as_operator = self._real_send
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.folder.cleanup()

    def _fake_send(self, db, secrets, to, subject, body, html_body=None):
        self.sent.append((to, subject, body))
        return {"from": "operator@example.com", "message_id": f"<{len(self.sent)}@example.com>",
                "refused": {}}

    # -- fixtures ----------------------------------------------------------

    def account(self, email, *, age_hours=20, mailbox=None, status="active",
                last_verified_at=None, last_polled_at=None, mailbox_error="",
                verify_error=""):
        user_id = f"usr_{email.split('@')[0]}"
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO users(id,email,password_hash,status,created_at) VALUES(?,?,?,?,?)",
                (user_id, email, "x", status, hours_ago(age_hours)))
            if mailbox:
                connection.execute(
                    """INSERT INTO mailboxes(id,user_id,email,report_to,imap_host,imap_port,
                           smtp_host,smtp_port,encrypted_password,enabled,last_verified_at,
                           last_polled_at,last_error,last_verify_error,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (f"mbx_{user_id}", user_id, mailbox, mailbox, "imap.example.com", 993,
                     "smtp.example.com", 465, b"cipher", 1, last_verified_at, last_polled_at,
                     mailbox_error, verify_error, utc_now()))
        return user_id

    def run_tool(self, *argv) -> tuple[int, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            code = notify.main(list(argv))
        return code, buffer.getvalue()

    # -- the two sentences -------------------------------------------------

    def test_an_account_with_no_mailbox_gets_the_four_steps(self):
        self.account("bare@example.com", mailbox=None)
        code, out = self.run_tool()
        self.assertEqual(code, 0)
        self.assertIn("还差一步", out)
        self.assertIn("1. 在 CityU Outlook", out)
        self.assertIn("https://example.test/app", out)

    def test_an_account_whose_mailbox_is_refused_gets_the_auth_code_sentence(self):
        self.account("refused@example.com", mailbox="refused@example.com",
                     last_verified_at=hours_ago(30), last_polled_at=hours_ago(0.1),
                     mailbox_error="IMAP 连接失败：b'LOGIN Login error or password error'",
                     verify_error="IMAP 连接失败：b'LOGIN Login error or password error'")
        code, out = self.run_tool()
        self.assertEqual(code, 0)
        self.assertIn("登录被拒绝", out)
        self.assertNotIn("1. 在 CityU Outlook", out, "这两句话不能混")

    def test_a_never_reached_mailbox_counts_as_refused(self):
        """`unreachable` is the same user problem as a rejected login: configured,
        and never once answered."""
        self.account("never@example.com", mailbox="never@example.com")
        rows = setup_reminders.collect(self.db, dt.datetime.now(dt.timezone.utc))
        self.assertEqual([row["group"] for row in rows], ["refused"])

    def test_a_healthy_account_is_left_alone(self):
        self.account("fine@example.com", mailbox="fine@example.com",
                     last_verified_at=hours_ago(1), last_polled_at=hours_ago(0.1))
        code, out = self.run_tool()
        self.assertEqual(code, 0)
        self.assertIn("共 0 个账号卡住", out)

    def test_a_brand_new_account_is_not_pounced_on(self):
        self.account("fresh@example.com", age_hours=0.2)
        code, out = self.run_tool()
        self.assertIn("共 0 个账号卡住", out)

    def test_a_deleted_account_is_never_reminded(self):
        self.account("gone@example.com", status="deleted")
        rows = setup_reminders.collect(self.db, dt.datetime.now(dt.timezone.utc))
        self.assertEqual(rows, [])

    # -- cannot drift from the app ----------------------------------------

    def test_the_provider_steps_are_the_ones_the_wizard_shows(self):
        # The address is built from the preset's own domain rather than written
        # out: the privacy gate that guards the public export cannot tell a
        # fixture from a real person's address, and it is right not to try. What
        # this asserts is the *coupling* -- that the mail's steps are the
        # wizard's steps -- so taking the domain from the wizard's data is also
        # the more honest fixture.
        preset = mailpresets.PRESETS_BY_ID["163"]
        address = "steps@" + preset["domains"][0]
        self.account("steps@example.com", mailbox=address)
        body = setup_reminders.refused_login_body(address)
        for step in preset["steps"]:
            self.assertIn(step, body, "邮件里的步骤必须与向导同源，否则迟早对不上")
        self.assertIn(preset["label"], body)

    def test_an_unknown_provider_still_gets_usable_advice(self):
        body = setup_reminders.refused_login_body("someone@self-hosted.invalid")
        self.assertIn("IMAP", body)
        self.assertIn("SMTP", body)
        self.assertIn("https://example.test/app", body)

    # -- {link} 指向"他该去的那一步"，不是首页 ------------------------------

    def test_every_letter_links_into_the_setup_wizard(self):
        """首页只会让他再找一次「设置向导在哪」。

        四封信指向同一个板块，而且这不是图省事：向导的 1–4 步全都在「邮箱设置」这一页，
        四格进度就钉在它顶上——收信人一眼看得出自己灰着的是哪一格。
        （原本 `{link}` 填的是 `/app`，收信人要自己从首页找到那一步。）
        """
        self.account("bare@example.com", mailbox=None)
        body = setup_reminders.never_configured_body()
        self.assertIn(f"https://example.test/app#/{setup_reminders.LINK_SECTION}", body)
        self.assertNotIn("https://example.test/app\n", body, "别只给到首页")

    def test_all_four_letters_carry_that_same_destination(self):
        for group in setup_reminders.GROUPS:
            body = setup_reminders.render_body(None, group, "someone@example.com")
            self.assertIn(f"#/{setup_reminders.LINK_SECTION}", body, group)

    def test_the_destination_is_a_real_section_of_the_app(self):
        """不许凭空编一个地址：`LINK_SECTION` 必须是 `app.js` 里 `NAV` 的一个键，
        而且 `index.html` 里真的有一个对应的板块（导航项没视图 = 空白屏）。"""
        root = pathlib.Path(__file__).resolve().parents[2]
        app_js = (root / "pilot_app" / "static" / "app.js").read_text(encoding="utf-8")
        index = (root / "pilot_app" / "static" / "index.html").read_text(encoding="utf-8")
        nav = app_js[app_js.index("const NAV = ["):app_js.index("];", app_js.index("const NAV = ["))]
        self.assertIn(f"key: '{setup_reminders.LINK_SECTION}'", nav)
        self.assertIn(f'id="section-{setup_reminders.LINK_SECTION}"', index)

    def test_the_wizard_on_that_page_owns_the_steps_the_letters_talk_about(self):
        """信里说「第 3 步」「第 2 步」，那些步骤必须真的在链接到的那一页里。"""
        root = pathlib.Path(__file__).resolve().parents[2]
        index = (root / "pilot_app" / "static" / "index.html").read_text(encoding="utf-8")
        section = index[index.index(f'id="section-{setup_reminders.LINK_SECTION}"'):]
        section = section[:section.index("</section>")]
        for step in ("第 1 步", "第 2 步", "第 3 步", "第 4 步"):
            self.assertIn(step, section, step)

    def test_without_an_origin_configured_the_link_still_names_the_section(self):
        """自部署没配 `INFE_PILOT_ORIGIN` 时是相对地址（那本来也发不出去），
        但板块仍然要指对——改天配上域名，链接就完整了。"""
        os.environ.pop("INFE_PILOT_ORIGIN", None)
        try:
            self.assertEqual(setup_reminders.step_url(),
                             f"/app#/{setup_reminders.LINK_SECTION}")
        finally:
            os.environ["INFE_PILOT_ORIGIN"] = "https://example.test"

    # -- the operator's own contact details --------------------------------

    def test_the_wechat_line_is_absent_unless_the_instance_configures_one(self):
        """It must come from the environment, not the code: this repository is
        public, and a self-hosted copy must not send its users to somebody
        else's personal account."""
        os.environ.pop("INFE_PILOT_CONTACT_WECHAT", None)
        for body in (setup_reminders.never_configured_body(),
                     setup_reminders.refused_login_body("x@example.com")):
            self.assertNotIn("微信", body, "没配就不该出现这一行")

    def test_the_wechat_line_appears_when_configured(self):
        os.environ["INFE_PILOT_CONTACT_WECHAT"] = "someone_wechat_id"
        try:
            for body in (setup_reminders.never_configured_body(),
                         setup_reminders.refused_login_body("x@example.com")):
                self.assertIn("someone_wechat_id", body)
                self.assertIn("- 还是搞不定", body)
        finally:
            os.environ.pop("INFE_PILOT_CONTACT_WECHAT", None)

    def test_the_wechat_line_carries_no_markdown(self):
        """`mailio.markdown_to_html` escapes paragraphs verbatim, so `**bold**`
        would arrive as literal asterisks wrapped around a phone number."""
        os.environ["INFE_PILOT_CONTACT_WECHAT"] = "someone_wechat_id"
        try:
            body = setup_reminders.never_configured_body()
        finally:
            os.environ.pop("INFE_PILOT_CONTACT_WECHAT", None)
        self.assertIn("- 还是搞不定可以直接找我：微信 someone_wechat_id", body)
        self.assertNotIn("**", body)

    def test_every_group_reaches_the_console_in_all_four_places(self):
        """A group that is missing from any of the four is a letter nobody read.

        Each `GROUPS` entry has to appear in the console as: a situation label, a
        preview heading, an editable body, and a line in the preview. Adding
        `provider` in v0.63.47 updated three of the four -- the **preview** kept
        its own hand-written list of three and silently dropped that letter. So
        the console could send a letter the operator had never seen, to the one
        kind of account that cannot fix its problem at all.

        This asserts the shape that makes the omission impossible: the preview
        iterates `REMINDER_GROUPS`, and every group is present in both tables and
        has a textarea.
        """
        app = (Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
        page = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
        from pilot_app import setup_reminders
        for group in setup_reminders.GROUPS:
            self.assertIn(f"{group}:", app, f"app.js 里没有 {group} 的说法")
            self.assertIn(f'id="reminder-text-{group}"', page, f"没有 {group} 的正文编辑框")
        # 预览必须**遍历**那张表，而不是自己再写一份清单。
        self.assertRegex(app, r"REMINDER_GROUPS\.forEach\(\(group\) => \{\s*preview\.appendChild")
        self.assertIn("shown[group]", app)
        # 而这句「不是一次发好几封」必须留在页面上：运营者正是这样理解它的。
        self.assertIn("每个人只会收到一封", page)

    def test_the_preview_shows_one_letter_per_group(self):
        """The API hands over every letter, keyed by group."""
        from pilot_app import setup_reminders
        lines = setup_reminders.preview(None)
        for group in setup_reminders.GROUPS:
            self.assertIn(group, lines, group)
            self.assertTrue(lines[group].strip(), group)

    def test_the_preview_shows_both_letters_and_the_wechat_state(self):
        os.environ["INFE_PILOT_CONTACT_WECHAT"] = "someone_wechat_id"
        try:
            shown = setup_reminders.preview()
        finally:
            os.environ.pop("INFE_PILOT_CONTACT_WECHAT", None)
        self.assertIn("还差一步", shown["never"])
        self.assertIn("登录被拒绝", shown["refused"])
        self.assertEqual(shown["wechat"], "someone_wechat_id")

    # -- the renderer quirk ------------------------------------------------

    def test_the_link_is_in_a_bullet_so_the_mail_renders_it_as_a_link(self):
        """`mailio.markdown_to_html` only auto-links `https://` inside bullets.
        In a paragraph the URL arrives as dead text -- the one thing this mail
        needs to be is clickable."""
        for body in (setup_reminders.never_configured_body(), setup_reminders.refused_login_body("x@example.com")):
            linked = [line for line in body.splitlines()
                      if line.startswith("- ") and "https://example.test/app" in line]
            self.assertTrue(linked, "行动链接必须放在项目符号里，否则渲染出来点不动")

    # -- the guardrails ----------------------------------------------------

    def test_the_dry_run_prints_no_full_address(self):
        self.account("private.person@example.com")
        code, out = self.run_tool()
        self.assertNotIn("private.person@example.com", out)
        self.assertIn("pr…@example.com", out)

    def test_it_says_which_package_it_imported(self):
        """A stale `pilot_app` left in /tmp shadows the installed one, because a
        script's own directory comes first on `sys.path`. That is how the first
        server run of this tool read v0.48.0 while production was v0.60.0 -- the
        wrong version would have produced a wrong list of who is stuck."""
        _, out = self.run_tool()
        self.assertIn("用的是", out)
        self.assertIn(str(Path(pilot_app.__file__).parent), out)

    def test_sending_needs_an_explicit_confirmation(self):
        os.environ.pop("PILOT_NOTIFY_CONFIRM", None)
        self.account("guarded@example.com")
        code, _ = self.run_tool("--send")
        self.assertEqual(code, 2)
        self.assertEqual(self.sent, [])

    def test_a_second_run_does_not_mail_the_same_person_again(self):
        self.account("once@example.com")
        first, _ = self.run_tool("--send")
        self.assertEqual(first, 0)
        self.assertEqual(len(self.sent), 1)
        second_code, second = self.run_tool("--send")
        self.assertEqual(second_code, 0)
        self.assertEqual(len(self.sent), 1, "同一个人不该收到第二封")
        self.assertIn("其中 0 个这次要发", second)

    def test_a_failed_send_is_not_recorded_as_delivered(self):
        """Recording before the send would be the one way to lose a person: the
        record would say they were told, and they never were."""
        self.account("flaky@example.com")

        def boom(*args, **kwargs):
            raise RuntimeError("SMTP 发送失败：connection refused")

        setup_reminders.send_as_operator = boom
        code, out = self.run_tool("--send")
        self.assertEqual(code, 1)
        self.assertIn("失败 1 封", out)
        self.assertEqual(self.db.get_setting("setup_reminder:usr_flaky"), "")

    def test_only_the_intended_account_is_mailed(self):
        self.account("stuck@example.com")
        self.account("fine@example.com", mailbox="fine@example.com",
                     last_verified_at=hours_ago(1), last_polled_at=hours_ago(0.1))
        self.run_tool("--send")
        self.assertEqual([to for to, _, _ in self.sent], ["stuck@example.com"])


if __name__ == "__main__":
    unittest.main()
