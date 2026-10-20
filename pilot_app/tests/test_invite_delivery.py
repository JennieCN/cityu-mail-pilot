"""Tests for the invite mail's deliverability hygiene.

The report says the same thing every day and the recipient is expecting it; the
**invite** is the one message that arrives out of the blue, and it carries a
short token plus a link. That is the exact shape of verification spam, so it is
the message most likely to be filed as junk -- and when it is, the applicant
concludes the pilot ignored them.

What this file can and cannot pin is worth stating, because most of deliverability
is out of reach here:

* **Out of reach**: SPF, DKIM, DMARC and sender reputation. Every outgoing
  message borrows the operator's own consumer mailbox, so those verdicts belong
  to that provider. No test in this repository can move them.
* **In reach, and pinned below**: the message must read like a person wrote it
  (display name, a reply invitation, a sign-off), stay short, keep the links to
  two, carry no shouting, and tell the reader where to look if it is not in the
  inbox.

The last one is the only genuinely effective fix available: a junked message
cannot tell the reader it was junked, so the warning has to arrive *before* the
looking.

**2026-09-22**: registration is open, so this letter is no longer the way in --
it only goes out for a **historical** approval (`docs/open-registration-2026-09-22.md`).
The letter itself, and every rule below, still holds: while any mail goes to a
user, it has to read like a person wrote it.
"""

import os
import pathlib
import re
import unittest

from pilot_app import alerting, mailio

STATIC = pathlib.Path(__file__).resolve().parent.parent / "static"


def invite_body(code: str = "AbC-123_xyz") -> tuple[str, str]:
    """The subject and body the applicant actually receives.

    Called on the real function rather than retyped here: a copy of the text
    would keep passing after somebody edited the message, which is the failure
    mode this whole file is about.
    """
    from pilot_app import web
    return web.invite_letter(code, origin="https://example.test")


class InviteShapeTests(unittest.TestCase):
    """The rules a first-contact message has to obey to look hand-written."""

    def test_the_body_keeps_the_links_down_to_two(self):
        """Every URL in a first-contact message is a spam signal."""
        _, body = invite_body()
        urls = re.findall(r"https?://\S+|(?<![\w.])/(?:app|privacy|terms)\b", body)
        self.assertLessEqual(len(urls), 2, urls)

    def test_it_carries_the_code_as_text(self):
        code = "Zz9-QQ_42"
        _, body = invite_body(code)
        self.assertIn(code, body)

    def test_it_does_not_shout(self):
        """No ALL-CAPS words -- the code is the only uppercase run allowed."""
        code = "Zz9-QQ_42"
        _, body = invite_body(code)
        without_code = body.replace(code, "")
        caps = [word for word in re.findall(r"[A-Za-z]{4,}", without_code) if word.isupper()]
        self.assertEqual(caps, [], f"全大写的词：{caps}")

    def test_it_asks_for_a_reply_and_signs_off(self):
        """A reply is the strongest 'this was wanted' signal a provider gets."""
        _, body = invite_body()
        self.assertIn("回这封信", body)
        self.assertTrue(body.rstrip().endswith(alerting.sender_name()),
                        "结尾应当是署名，而不是一句系统提示")

    def test_it_tells_the_reader_where_to_look(self):
        """The one fix that works: the warning arrives before the looking."""
        _, body = invite_body()
        self.assertIn("垃圾邮件", body)
        self.assertIn("不是垃圾邮件", body)

    def test_it_stays_short(self):
        """A long body with several sections is what a bulk sender looks like."""
        _, body = invite_body()
        self.assertLess(len(body), 900, f"{len(body)} 字")

    def test_the_subject_is_not_a_system_notification(self):
        subject, _ = invite_body()
        self.assertIn("邀请码", subject)
        self.assertNotIn("【", subject)
        self.assertLess(len(subject), 30)

    def test_the_compliance_facts_survive_the_rewrite(self):
        """Shorter is not the same as quieter about who pays.

        The applicant may never open the site again; this letter is where they
        learn whose model account their mail will pass through.
        """
        _, body = invite_body()
        # 「立即清空」 is pinned by test_signup too; it is the retention promise,
        # and the first version of this rewrite dropped it while shortening.
        for fact in ("免费", "管理员", "自己的 key", "大模型服务商", "AI 生成", "立即清空"):
            self.assertIn(fact, body, fact)


class MessageHeaderTests(unittest.TestCase):
    """What actually goes on the wire (headers, not prose)."""

    def _message(self, **kwargs):
        import email
        sent = {}

        class FakeSMTP:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def login(self, *a): pass
            def send_message(self, message):
                sent["raw"] = message
                return {}
            def ehlo(self): pass
            def starttls(self, **k): pass
            def quit(self): pass

        original_ssl, original_plain = mailio.smtplib.SMTP_SSL, mailio.smtplib.SMTP
        mailio.smtplib.SMTP_SSL = FakeSMTP
        mailio.smtplib.SMTP = FakeSMTP
        try:
            mailio.send_report(
                {"email": "sender@example.com", "report_to": "applicant@example.com",
                 "smtp_host": "smtp.example.com", "smtp_port": 465},
                "pw", "你要的 CityU Mail Pilot 邀请码", "body",
                text_body="body", html_body="<p>body</p>", **kwargs)
        finally:
            mailio.smtplib.SMTP_SSL, mailio.smtplib.SMTP = original_ssl, original_plain
        return email.message_from_string(sent["raw"].as_string())

    def test_the_sender_carries_a_display_name(self):
        message = self._message(from_name="CityU 邮件助手", reply_to="sender@example.com")
        raw = str(message["From"])
        self.assertIn("sender@example.com", raw)
        # A Chinese display name has to be RFC 2047 encoded, not sent raw.
        self.assertIn("=?", raw)
        self.assertEqual(str(message["Reply-To"]), "sender@example.com")

    def test_without_a_name_nothing_changes_for_the_report_path(self):
        message = self._message()
        self.assertEqual(str(message["From"]), "sender@example.com")
        self.assertIsNone(message["Reply-To"])

    def test_a_header_injection_in_the_name_is_removed_not_escaped(self):
        """The display name is operator-set text that goes into a header.

        An environment value containing a newline would end the header and
        whatever follows would be read as another one -- so control characters
        are dropped, not escaped.
        """
        os.environ[alerting.SENDER_NAME_ENV] = "CityU\r\nBcc: victim@example.com"
        try:
            self.assertEqual(alerting.sender_name(), "CityUBcc: victim@example.com")
        finally:
            os.environ.pop(alerting.SENDER_NAME_ENV, None)

    def test_it_is_still_a_plain_text_message_with_an_html_twin(self):
        message = self._message(from_name="CityU 邮件助手", reply_to="sender@example.com")
        kinds = [part.get_content_type() for part in message.walk()]
        self.assertIn("text/plain", kinds)
        self.assertIn("text/html", kinds)
        self.assertTrue(message["Message-ID"])


class SiteCopyTests(unittest.TestCase):
    """What the pages say now that registration is open (2026-09-22).

    The old class here pinned the *warning at the moment of applying* -- 「没收到
    就看垃圾邮件」, because the invite mail was the one message that arrived out of
    the blue and the applicant was the only person who could not tell it had been
    junked. There is no application any more (`docs/open-registration-2026-09-22.md`),
    so those two assertions went with the form. What survives is the half that is
    still true: the register screen has to say, in the place a stranger is
    standing, that he does **not** need a code to sign up.
    """

    def test_the_register_screen_says_registration_is_open(self):
        """那一栏原来写着「去首页申请邀请码」。现在没有码可要了。

        这是第一次用的人唯一会看的几行字：他已经在注册页上，所以「不用邀请码、
        填了就能建号」必须写在这里，而不是一页之外。
        """
        page = (STATIC / "index.html").read_text(encoding="utf-8")
        row = page[page.index('id="open-registration"'):page.index('id="consent-row"')]
        self.assertIn("开放", row)
        self.assertIn("任何邮箱填了就能建号", row)
        self.assertNotIn("由试点管理员生成", row)
        self.assertNotIn("/#apply", row, "开放注册之后没有「去哪儿要一个码」这回事了")

    def test_the_form_no_longer_sends_a_code(self):
        """服务端仍然收 `invite_code`（老客户端/历史码向后兼容），但界面不再发它。"""
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertNotIn("invite_code: $('invite')", script)
        self.assertNotIn("请先填邀请码", script)
        # 铁律 10：同意必须由服务端校验 —— 客户端这一栏只是先把话说清楚。
        self.assertIn("accepted_terms: true", script)

    def test_the_email_field_says_not_to_register_twice(self):
        """已经注册过的人，唯一的答案要在**他正要填的那一格**下面。

        2026-09-19 用户报「注册显示服务器内部错误」：拿一个已注册的邮箱再点一次注册，
        `create_user` 撞唯一约束抛 IntegrityError，一路冒成 **500**（那一侧的修复见
        `database.create_user` 与 `test_signup.RegisterWithATakenEmailTests`）。
        服务端现在会说人话了，但**更好的是根本不走到那一步**——所以这句话必须静态地
        写在他填邮箱的地方，不能等服务端回话才出现。
        """
        page = (STATIC / "index.html").read_text(encoding="utf-8")
        row = page[page.index('id="auth-email"'):page.index('id="auth-password"')]
        self.assertIn("已有账户登录", row, "要告诉他该按哪个按钮")
        self.assertIn("不用再注册", row, "要说清「同一个邮箱别注册两次」")
        self.assertIn("/privacy", row, "忘了密码要有去处——这个项目没有自助重设")


if __name__ == "__main__":
    unittest.main()
