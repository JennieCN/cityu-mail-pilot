"""IMAP failure-message tests: pilot users must get actionable advice."""

import datetime
import email.utils
import imaplib
import ssl
import unittest
from unittest import mock

from pilot_app import mailio
from pilot_app.mailio import explain_imap_failure, normalize_message


class ExplainImapFailureTests(unittest.TestCase):
    def test_basic_auth_disabled_points_at_another_provider(self):
        # Exactly what outlook.office365.com returns for a correct app password.
        message = explain_imap_failure(imaplib.IMAP4.error(b"Basic authentication is disabled."))
        self.assertIn("授权码", message)
        self.assertIn("QQ", message)
        self.assertIn("Gmail", message)
        self.assertNotIn("IMAP 连接失败", message)

    def test_bad_credentials_asks_for_a_new_app_password(self):
        message = explain_imap_failure(imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Invalid credentials"))
        self.assertIn("授权码", message)
        self.assertIn("重新生成", message)

    def test_unknown_user_mentions_the_address(self):
        message = explain_imap_failure(imaplib.IMAP4.error(b"LOGIN failed: Unknown user"))
        self.assertIn("邮箱地址", message)

    def test_network_problem_is_reported_as_network(self):
        message = explain_imap_failure(OSError("Connection refused"))
        self.assertIn("网络", message)

    def test_certificate_problem_mentions_tls(self):
        message = explain_imap_failure(ssl.SSLError("certificate verify failed"))
        self.assertIn("证书", message)

    def test_unknown_error_keeps_the_original_text(self):
        message = explain_imap_failure(imaplib.IMAP4.error(b"NO [SERVERBUG] something odd"))
        self.assertIn("IMAP 连接失败", message)
        self.assertIn("something odd", message)

    def test_qq_login_rejection_lists_the_real_causes(self):
        message = explain_imap_failure(imaplib.IMAP4.error(
            b"Login fail. Account is abnormal, service is not open, password is incorrect"
        ))
        self.assertIn("IMAP/SMTP", message)
        self.assertIn("重新生成授权码", message)

    def test_login_frequency_limit_asks_to_wait(self):
        message = explain_imap_failure(imaplib.IMAP4.error(b"Login frequency limited"))
        self.assertIn("15", message)

    def test_message_id_is_extracted_and_stable(self):
        """Message-ID identifies the mail no matter how often it was forwarded."""
        raw = (
            b'From: "Cap" <noreply_cap275421@cityu.edu.hk>\r\n'
            b"To: student@my.cityu.edu.hk\r\n"
            b"Subject: [CAP] Posting digest\r\n"
            b"Date: Sun, 13 Sep 2026 17:16:27 +0800\r\n"
            b"Message-ID: <20260913091828.5982EBAE32@smtp82.ad.cityu.edu.hk>\r\n"
            b"X-MS-Exchange-ForwardingLoop: ForwardingHandled;2109ce83\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\nbody\r\n"
        )
        first = normalize_message(raw)
        second = normalize_message(raw)
        self.assertEqual(first["message_key"], "<20260913091828.5982EBAE32@smtp82.ad.cityu.edu.hk>")
        self.assertEqual(first["message_key"], second["message_key"])

    def test_missing_message_id_yields_empty_key(self):
        raw = b"Subject: No id\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
        self.assertEqual(normalize_message(raw)["message_key"], "")


class DisclaimerTests(unittest.TestCase):
    """Who is allowed to claim 「AI 生成内容可能出错」.

    Audit §5-1: the operator's own mails -- the invite code, the setup reminder,
    the unit-failure alert and the new-application notice -- all carried that
    sentence. It was appended by `markdown_to_html`, the *fallback* used when a
    caller passes no `html_body`, and every report and alert path passes its own
    rendering. So the only messages that ever reached that line were the four
    that no model had touched.

    The claim was not deleted, it moved to the code that writes the AI text
    (`reports.CONTENT_DISCLAIMER`). These tests hold both halves: the transport
    stays silent, and the report still says it.
    """

    def sent_html(self, html_body=None) -> str:
        """Run the real `send_report` against a stubbed SMTP; return the HTML part.

        Asserted on the assembled message rather than on `markdown_to_html`
        directly: "the fallback is clean" and "the mail the operator receives is
        clean" are two different claims, and only the second one was broken.
        """
        captured = {}

        class FakeSMTP:
            def __init__(self, *args, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def login(self, *args): pass
            def send_message(self, message): captured["message"] = message

        with mock.patch.object(mailio.smtplib, "SMTP_SSL", FakeSMTP):
            mailio.send_report(
                {"email": "me@example.com", "report_to": "you@example.com",
                 "smtp_host": "smtp.example.com", "smtp_port": 465},
                "a-password", "主题", "正文", html_body=html_body)
        parts = [part for part in captured["message"].walk()
                 if part.get_content_type() == "text/html"]
        self.assertEqual(len(parts), 1, "应该正好有一个 HTML 部分")
        return parts[0].get_content()

    def test_an_operator_mail_carries_no_ai_disclaimer(self):
        """Passing no html_body is exactly how the invite and reminder mails go out."""
        html = self.sent_html()
        self.assertNotIn("AI 生成", html)
        self.assertIn("正文", html)

    def test_the_shell_is_still_a_well_formed_card(self):
        html = self.sent_html()
        self.assertIn("CITYU MAIL PILOT", html)
        self.assertEqual(html.count("<div"), html.count("</div>"),
                         "拿掉页脚那一段之后标签必须还是配平的")

    def test_a_caller_supplied_report_is_passed_through_untouched(self):
        """The transport must not edit text it did not write -- in either direction."""
        mine = "<div>报告正文</div><div>AI 生成内容可能出错；仅供参考。</div>"
        self.assertIn("AI 生成内容可能出错", self.sent_html(html_body=mine))

    def test_the_report_side_still_says_it(self):
        """Moved, not removed: the claim lives with the code that writes the AI text."""
        from pilot_app import reports
        self.assertIn("AI 生成内容", reports.CONTENT_DISCLAIMER)
        shell = reports._email_shell("标题", "副标题", "<tr><td>正文</td></tr>")
        self.assertIn(reports.CONTENT_DISCLAIMER, shell)


class FakeImap:
    """A server that behaves like 163 (Coremail), which is the whole point.

    ``LOGIN`` succeeds; every ``EXAMINE``/``SELECT`` is refused with the real
    sentence measured on production -- *until* the client has sent the RFC 2971
    ``ID`` command. QQ and Gmail do not care either way, so a fake that ignored
    ``ID`` would have let the broken version pass.
    """

    REFUSAL = b"EXAMINE Unsafe Login. Please contact kefu@188.com for help"

    def __init__(self, capabilities=("IMAP4REV1", "ID"), require_id=True, uid_rows=b"",
                 sizes=None, size_supported=True, partial_supported=True):
        self.capabilities = capabilities
        self.require_id = require_id
        self.uid_rows = uid_rows
        #: uid → `RFC822.SIZE`。不在里面就是「这封问不出来」。
        self.sizes = sizes or {}
        self.size_supported = size_supported
        self.partial_supported = partial_supported
        self.identified = False
        self.commands = []
        self.uid_calls = []

    def _simple_command(self, name, *args):
        self.commands.append((name, args))
        if name == "ID":
            self.identified = True
        return ("OK", [b""])

    def login(self, user, password):
        self.commands.append(("LOGIN", (user,)))
        return ("OK", [b"LOGIN completed"])

    def select(self, mailbox="INBOX", readonly=False):
        self.commands.append(("EXAMINE" if readonly else "SELECT", (mailbox,)))
        if self.require_id and not self.identified:
            return ("NO", [self.REFUSAL])
        return ("OK", [b"24"])

    def response(self, key):
        return ("UIDVALIDITY", [b"1"])

    def uid(self, verb, *args):
        self.uid_calls.append((verb, args))
        if verb == "fetch" and "RFC822.SIZE" in args[1]:
            if not self.size_supported:
                return ("NO", [b"no size"])
            uid = int(args[0])
            if uid in self.sizes:
                return ("OK", [f"1 (RFC822.SIZE {self.sizes[uid]})".encode()])
            return ("NO", [b"no size"])
        if verb == "fetch" and "<0." in args[1] and not self.partial_supported:
            return ("NO", [b"partial not supported"])
        return ("OK", [self.uid_rows])

    def close(self): pass

    def logout(self): pass


class FakeImapServer:
    """A dumb 163-like IMAP server *behind a fake socket*.

    Unlike `FakeImap` above (which replaces the client wholesale), this one
    keeps the real `imaplib.IMAP4_SSL` -- all of its state machine, its command
    table, its tag bookkeeping -- and only fakes the transport. That distinction
    is not academic: the first version of the fix called
    ``_simple_command("ID", …)`` without registering the verb in
    ``imaplib.Commands``, so the real client raised ``KeyError`` **before
    sending anything**, while `FakeImap` (which implements ``_simple_command``
    itself) stayed perfectly green. Production kept refusing the mailbox.
    """

    REFUSAL = b"EXAMINE Unsafe Login. Please contact kefu@188.com for help"

    def __init__(self, *, capabilities=("IMAP4rev1", "ID"), require_id=True):
        self.capabilities = capabilities
        self.require_id = require_id
        self.identified = False
        self.received: list[str] = []
        self._pending = b""
        self._out: list[bytes] = [b"* OK fake ready\r\n"]

    # --- the socket API imaplib actually uses -------------------------------
    def makefile(self, mode):
        return self

    def readline(self, limit=-1):
        # Python 3.9–3.13: imaplib reads through `sock.makefile('rb')`.
        return self._out.pop(0) if self._out else b""

    def recv(self, size=65536):
        """Python 3.14: imaplib implements its own `readline` and calls `recv`.

        3.14 dropped the buffered `file` object and reads the socket directly
        (`imaplib.readline` → `self.sock.recv(DEFAULT_BUFFER_SIZE)`). A fake that
        only offered `makefile()` passed on the laptop (3.9) and blew up on the
        server's 3.14 with `AttributeError: 'FakeImapServer' object has no
        attribute 'recv'` — which is exactly the "3.9 works" trap the CI matrix
        exists to catch.
        """
        return self._out.pop(0) if self._out else b""

    def sendall(self, data):
        self._pending += data
        while b"\r\n" in self._pending:
            line, _, self._pending = self._pending.partition(b"\r\n")
            self._handle(line.decode("utf-8", "replace"))

    def close(self): pass

    def shutdown(self, *args): pass

    def settimeout(self, *args): pass

    # --- the server ---------------------------------------------------------
    def _reply(self, text: bytes):
        self._out.append(text + b"\r\n")

    def _handle(self, line: str):
        self.received.append(line)
        tag, _, rest = line.partition(" ")
        verb = rest.split(" ")[0].upper() if rest else ""
        if verb == "CAPABILITY":
            self._reply(b"* CAPABILITY " + " ".join(self.capabilities).encode())
            self._reply(f"{tag} OK CAPABILITY completed".encode())
        elif verb == "ID":
            self.identified = True
            # 登录前 163 只回 tagged OK，没有 `* ID`（实测），所以用 xatom 会炸。
            self._reply(f"{tag} OK ID completed".encode())
        elif verb == "LOGIN":
            self._reply(f"{tag} OK LOGIN completed".encode())
        elif verb in ("EXAMINE", "SELECT"):
            if self.require_id and not self.identified:
                self._reply(f"{tag} NO ".encode() + self.REFUSAL)
            else:
                self._reply(b"* 24 EXISTS")
                self._reply(b"* OK [UIDVALIDITY 1] UIDs valid")
                self._reply(f"{tag} OK [READ-ONLY] Examine completed".encode())
        elif verb == "UID":
            if "SEARCH" in rest.upper():
                self._reply(b"* SEARCH")
            self._reply(f"{tag} OK UID completed".encode())
        elif verb in ("CLOSE", "NOOP"):
            self._reply(f"{tag} OK {verb} completed".encode())
        elif verb == "LOGOUT":
            self._reply(b"* BYE bye")
            self._reply(f"{tag} OK LOGOUT completed".encode())
        else:
            self._reply(f"{tag} BAD unknown command".encode())


class RealImaplibTests(unittest.TestCase):
    """走真 imaplib：命令表、标签、状态机都是真的，只有 socket 是假的。"""

    @staticmethod
    def _mailbox():
        return {"imap_host": "imap.163.com", "imap_port": 993, "email": "a@163.com"}

    def _run(self, server, call):
        with mock.patch.object(imaplib.IMAP4_SSL, "_create_socket",
                               lambda self, timeout=None: server):
            return call()

    def test_the_id_command_is_registered_with_imaplib(self):
        """`imaplib` 只认固定动词表，没登记就在发包之前抛 KeyError。"""
        self.assertIn("ID", imaplib.Commands)
        server = FakeImapServer()
        self.assertTrue(mailio.identify_client(
            type("C", (), {"capabilities": ("ID",), "_simple_command":
                           lambda self, *a: ("OK", [b""])})()))

    def test_a_server_that_demands_id_is_satisfied_by_the_real_client(self):
        server = FakeImapServer()
        found = self._run(server, lambda: mailio.fetch_new_messages(self._mailbox(), "pw"))
        self.assertEqual(found, ("1", [], 0))
        self.assertTrue(any(line.split(" ")[1].upper() == "ID" for line in server.received),
                        f"真客户端没有发出 ID：{server.received}")

    def test_without_the_registration_it_would_not_work(self):
        """反向验证：把动词表里那一行拿掉，失败必须回来。

        这条测试是**这次事故的复现**：第一版代码在真客户端上就是这样悄无声息
        地什么都没发出去，而假客户端让测试全绿。
        """
        server = FakeImapServer()
        saved = imaplib.Commands.pop("ID")
        try:
            with self.assertRaises(mailio.MailError) as caught:
                self._run(server, lambda: mailio.fetch_new_messages(self._mailbox(), "pw"))
            self.assertIn("Unsafe Login", str(caught.exception))
            self.assertEqual([line for line in server.received if " ID" in line], [],
                             "没有登记时 ID 根本发不出去")
        finally:
            imaplib.Commands["ID"] = saved


class IdentifyClientTests(unittest.TestCase):
    """163/126 refuse to open INBOX for a client that never sent `ID`.

    Measured on production 2026-09-18, A/B/A/B against one real account: without
    the command ``EXAMINE`` answers ``NO Unsafe Login…``; with it, ``OK
    [READ-ONLY]``. Python's ``imaplib`` never sends it, which is why a 163
    mailbox read as permanently broken while QQ and Gmail worked.
    """

    @staticmethod
    def _mailbox():
        return {"imap_host": "imap.163.com", "imap_port": 993, "email": "a@163.com"}

    def test_it_announces_us_when_the_server_advertises_id(self):
        client = FakeImap()
        self.assertTrue(mailio.identify_client(client))
        name, args = client.commands[0]
        self.assertEqual(name, "ID")
        from pilot_app import __version__
        self.assertIn("CityU Mail Pilot", args[0])
        self.assertIn(__version__, args[0], "版本号要跟着包走，不能手写")

    def test_it_stays_quiet_when_the_server_does_not_offer_id(self):
        client = FakeImap(capabilities=("IMAP4REV1", "UIDPLUS"))
        self.assertFalse(mailio.identify_client(client))
        self.assertEqual(client.commands, [], "服务器没声明 ID 就不要多发命令")

    def test_a_rejected_id_command_is_not_fatal(self):
        client = FakeImap()
        client._simple_command = mock.Mock(side_effect=imaplib.IMAP4.error("NOPE"))
        self.assertFalse(mailio.identify_client(client), "发 ID 失败不能影响收信")

    def test_every_read_path_survives_a_server_that_demands_id(self):
        paths = (
            ("fetch_new_messages", lambda: mailio.fetch_new_messages(self._mailbox(), "pw")),
            ("fetch_recent_messages", lambda: mailio.fetch_recent_messages(self._mailbox(), "pw")),
            ("probe_mailbox", lambda: mailio.probe_mailbox(self._mailbox(), "pw")),
        )
        for name, call in paths:
            with self.subTest(path=name):
                client = FakeImap()
                with mock.patch.object(mailio.imaplib, "IMAP4_SSL", return_value=client):
                    call()  # 这个假服务器不发 ID 就打不开；不抛错即通过
                self.assertTrue(client.identified, f"{name} 没有先报名")
                self.assertEqual(client.commands[0][0], "ID",
                                 f"{name} 必须在登录之前或之后立刻发 ID")

    def test_without_the_id_command_the_same_server_refuses(self):
        """反向验证：把 ID 去掉，失败必须回来（否则上面那条什么也没证明）。"""
        client = FakeImap()
        with mock.patch.object(mailio, "identify_client", return_value=False), \
             mock.patch.object(mailio.imaplib, "IMAP4_SSL", return_value=client):
            with self.assertRaises(mailio.MailError) as caught:
                mailio.fetch_new_messages(self._mailbox(), "pw")
        self.assertIn("Unsafe Login", str(caught.exception))

    def test_the_idle_watcher_identifies_itself_too(self):
        from pilot_app import idle
        client = FakeImap()
        with mock.patch.object(idle.imaplib, "IMAP4_SSL", return_value=client):
            idle._connect(self._mailbox(), "pw")
        self.assertEqual(client.commands[0][0], "ID", "IDLE 那条路也要先报名")
        self.assertTrue(client.identified)


class RefusedInboxMessageTests(unittest.TestCase):
    """The stored error must carry the server's own words.

    It used to be the constant 「无法以只读方式打开 INBOX。」 -- and the panel,
    the alert mail and the AI operations report all repeated that constant, so a
    163 anti-abuse refusal was invisible to everybody, and the only way to learn
    it was a socket-level probe on the server (which is what it took).
    """

    def test_the_servers_sentence_is_included(self):
        message = str(mailio.refused_inbox([FakeImap.REFUSAL]))
        self.assertIn("Unsafe Login", message)
        self.assertIn("无法以只读方式打开 INBOX", message)

    def test_an_unsafe_login_gets_provider_specific_advice(self):
        message = str(mailio.refused_inbox([FakeImap.REFUSAL]))
        self.assertIn("网易", message)
        self.assertIn("安全", message)

    def test_an_unknown_refusal_keeps_the_raw_text_without_invented_advice(self):
        message = str(mailio.refused_inbox([b"[SERVERBUG] mailbox locked"]))
        self.assertIn("mailbox locked", message)
        self.assertNotIn("网易", message)

    def test_no_server_words_still_produces_the_plain_message(self):
        self.assertIn("无法以只读方式打开 INBOX", str(mailio.refused_inbox(None)))


if __name__ == "__main__":
    unittest.main()


class AuthCodeRejectedWordingTests(unittest.TestCase):
    """「授权码不对」是这条通道上最常见的一种失败，必须给一句能照做的话。

    2026-09-18 实测：一个真实用户的邮箱**两条通道都被拒**——
    IMAP `LOGIN Login error or password error`（163 的原话）、
    SMTP `535 Error: authentication failed` —— 而两种拼法都不含旧分支里那些
    关键词（`authenticationfailed` 没有空格、`login failed` 也不等于 `Login error`），
    于是她看到的是一句英文原文，没有下一步。**最常见 = 最该说清楚。**
    """

    def test_163_imap_wording_is_translated(self):
        message = explain_imap_failure(imaplib.IMAP4.error(b"LOGIN Login error or password error"))
        self.assertIn("授权码", message)
        self.assertNotIn("IMAP 连接失败", message, "不该再退回原始异常")

    def test_smtp_535_is_translated_too(self):
        import smtplib
        message = explain_imap_failure(
            smtplib.SMTPAuthenticationError(535, b"Error: authentication failed"))
        self.assertIn("授权码", message)

    def test_it_says_what_the_code_is_called_and_where_to_get_one(self):
        message = explain_imap_failure(imaplib.IMAP4.error(b"LOGIN Login error or password error"))
        self.assertIn("客户端授权密码", message, "163 管它叫这个")
        self.assertIn("不是邮箱的登录密码", message, "最常见的一种填错")
        self.assertIn("IMAP/SMTP", message, "另一种常见原因：服务没开")

    def test_a_really_unknown_error_still_falls_through_raw(self):
        """别把所有东西都套进这句话：认不出来就照实说。"""
        message = explain_imap_failure(imaplib.IMAP4.error(b"NO [SERVERBUG] something odd"))
        self.assertIn("something odd", message)


class FetchOriginalTests(unittest.TestCase):
    """「看原信」的那一次只读取回：**它只读，而且它不猜**。

    两个后果比功能本身更重要：
    ① 邮箱被重建过（UIDVALIDITY 变了）时，同一串 UID 指的是**别的信**——
       那时候宁可说「取不到」，也不能把另一封信当成这封显示给用户；
    ② 它是只读的（EXAMINE + BODY.PEEK[]），永远不许在用户邮箱里留下痕迹。
    """

    @staticmethod
    def _raw(subject: str = "作业截止", body: str = "请提交作业。") -> bytes:
        from email.message import EmailMessage
        message = EmailMessage()
        message["From"] = "老师 <student@my.cityu.edu.hk>"
        message["To"] = "me@example.com"
        message["Subject"] = subject
        message["Date"] = "Mon, 14 Sep 2026 04:00:00 +0000"
        message.set_content(body)
        return message.as_bytes()

    @staticmethod
    def _config() -> dict:
        return {"imap_host": "imap.example.com", "imap_port": 993, "email": "me@example.com"}

    def _fetch(self, fake, **kwargs):
        with mock.patch.object(mailio.imaplib, "IMAP4_SSL", return_value=fake):
            return mailio.fetch_message_by_uid(self._config(), "授权码", kwargs.pop("uid", 7), **kwargs)

    def test_it_reads_the_message_it_was_asked_for(self):
        fake = FakeImap(uid_rows=(b'1 (BODY[] {123}', self._raw()))
        result = self._fetch(fake)
        self.assertEqual(result["state"], "ok")
        self.assertIn("请提交作业", result["message"]["body"])
        self.assertEqual(result["message"]["subject"], "作业截止")
        # 先问一句 `RFC822.SIZE`（P1：按需读也要有体积上限），再取正文。这个桩问不出
        # 大小，所以正文走的是**有界** partial FETCH——两种都是只读取法。
        self.assertIn(("fetch", ("7", "(RFC822.SIZE)")), fake.uid_calls,
                      "取正文之前必须先问大小")
        specs = [args[1] for verb, args in fake.uid_calls
                 if verb == "fetch" and "RFC822.SIZE" not in args[1]]
        self.assertEqual(len(specs), 1, f"正文只该取一次：{fake.uid_calls}")
        self.assertIn("BODY.PEEK[]", specs[0], "用的是 PEEK（不改已读标记）")
        self.assertNotIn("BODY[]", specs[0], "绝不能用会把信标成已读的 BODY[]")
        self.assertFalse(result["truncated"])

    def test_it_opens_the_mailbox_read_only(self):
        fake = FakeImap(uid_rows=(b'1 (BODY[] {5}', self._raw()))
        self._fetch(fake)
        self.assertIn(("EXAMINE", ("INBOX",)), fake.commands,
                      "只读打开（EXAMINE）而不是 SELECT——铁律：绝不改动用户的邮箱")

    def test_a_rebuilt_mailbox_is_not_guessed_at(self):
        """UIDVALIDITY 是门牌号：变了以后同一个 UID 是**另一封信**。"""
        class Rebuilt(FakeImap):
            def response(self, key):
                return ("UIDVALIDITY", [b"99"])

        fake = Rebuilt(uid_rows=(b'1 (BODY[] {5}', self._raw(subject="别人的信")))
        result = self._fetch(fake, uid_validity="1")
        self.assertEqual(result["state"], mailio.ORIGINAL_MOVED)
        self.assertEqual(fake.uid_calls, [], "认不出是哪一封时**连取都不取**")
        self.assertEqual(result["uid_validity"], "99")

    def test_a_message_that_is_no_longer_in_the_mailbox(self):
        fake = FakeImap(uid_rows=None)
        self.assertEqual(self._fetch(fake)["state"], mailio.ORIGINAL_GONE)

    def test_the_same_uid_validity_goes_ahead(self):
        fake = FakeImap(uid_rows=(b'1 (BODY[] {5}', self._raw()))
        self.assertEqual(self._fetch(fake, uid_validity="1")["state"], "ok")

    def test_a_refused_mailbox_raises_the_actionable_error(self):
        """邮箱不肯开箱时，用户要看到**能照着做**的话，而不是一句「加载失败」。"""
        class Refusing(FakeImap):
            def select(self, mailbox="INBOX", readonly=False):
                return ("NO", [self.REFUSAL])

        with self.assertRaises(mailio.MailError) as caught:
            self._fetch(Refusing(require_id=False))
        text = str(caught.exception)
        self.assertIn("安全验证", text, "163 的「不安全登录」要给出下一步怎么做")
        self.assertIn("Unsafe Login", text, "同时把服务器的原话带上，别替它编")

    def test_a_very_long_letter_is_flagged_as_truncated(self):
        """正文上限与生成报告那一条**共用一个常量**，否则会出现
        「报告里看得到、点开原信反而没有」这种对不上的怪事。"""
        long_body = "行" * (mailio.MESSAGE_BODY_LIMIT + 500)
        fake = FakeImap(uid_rows=(b'1 (BODY[] {999999}', self._raw(body=long_body)))
        result = self._fetch(fake)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["message"]["body"]), mailio.MESSAGE_BODY_LIMIT)


class OutboundRequiredHeadersTests(unittest.TestCase):
    """发出去的信必须有 `Date` —— 这条是从**收件方留存的报头**里发现的。

    2026-09-26 的经过：用户报「刚刚那封 AI 摘要没收到」。把 QQ 收件箱里那一封的**原始报头**
    拉下来看（`INBOX` uid 2646，`Received: … by newxmesmtplogicsvrszc50-0.qq.com … 00:32:46 +0800`），
    `Received` 是 QQ 自己盖的，而 **`Date` 一行都没有** —— `EmailMessage()` 不会替你补，
    `smtplib` 也不补。也就是说从上线起发出去的每一封（报告 / 邀请码 / 提醒 / 告警 / 广播）
    都缺这个字段，只是收件端都恰好替我们兜住了（QQ 拿 Received 排序，所以没人看出来）。

    RFC 5322 §3.6：`Date` 与 `From` 是仅有的两个**必填**字段。收件方不替我们兜的时候，
    这是白送出去的一条垃圾邮件规则（SpamAssassin 的 MISSING_DATE 一类）。
    """

    def sent_message(self, **kwargs):
        """跑真的 `send_report`，但 SMTP 是假的；返回**装配好的那一封**。"""
        captured = {}

        class FakeSMTP:
            def __init__(self, *args, **kwargs): pass
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def login(self, *args): pass
            def send_message(self, message): captured["message"] = message

        with mock.patch.object(mailio.smtplib, "SMTP_SSL", FakeSMTP):
            mailio.send_report(
                {"email": "me@example.com", "report_to": "you@example.com",
                 "smtp_host": "smtp.example.com", "smtp_port": 465},
                "a-password", "主题", "正文", **kwargs)
        return captured["message"]

    def test_the_required_headers_are_all_there(self):
        message = self.sent_message()
        for name in ("Date", "From", "To", "Subject", "Message-ID"):
            self.assertTrue(message[name], f"{name} 缺失（RFC 5322 §3.6）")

    def test_the_date_is_the_moment_of_sending_with_a_real_offset(self):
        """`-0000` 那种「不知道时区」不算：我们知道自己那一刻的偏移，就如实写。"""
        message = self.sent_message()
        when = email.utils.parsedate_to_datetime(message["Date"])
        self.assertIsNotNone(when, f"Date 解析不出来：{message['Date']!r}")
        self.assertIsNotNone(when.tzinfo, "Date 要带时区偏移，不能是裸的本地时间")
        drift = abs((datetime.datetime.now(datetime.timezone.utc) - when).total_seconds())
        self.assertLess(drift, 120, f"Date 与发送时刻差了 {drift:.0f} 秒")

    def test_the_operator_mails_take_the_same_road(self):
        """邀请码/提醒走的是同一个装配点（`alerting.send_as_operator` → 这里）。"""
        message = self.sent_message(html_body="<div>正文</div>", from_name="CityU Mail Pilot",
                                    reply_to="me@example.com")
        self.assertTrue(message["Date"], "运营者的信也缺 Date")
        self.assertEqual(message["Reply-To"], "me@example.com")


class OriginalSizeGateTests(unittest.TestCase):
    """P1（GPT 审计第二条）：**按需读原信也要有体积上限**。

    worker 取信那条路一直先问 `RFC822.SIZE`（`fetch_new_messages`），而「看原信 /
    翻译 / 总结」走的 `fetch_message_by_uid` 原来直接 `BODY.PEEK[]`：一封带大附件的
    邮件会在 **web 进程**里整封进内存（生产 2 GB），少量并发就能把服务拖垮。

    这里钉住三件事：

    ① 超限的邮件**一个字节正文都不取**——不是「取回来再返回一个错误」；
    ② 正常小邮件照旧可读；
    ③ `RFC822.SIZE` 问不出来时走**有界**的 partial FETCH，硬上限就是
       `mailio.MAX_MESSAGE_BYTES`（与 worker 同一个常量），partial 被拒也不回头取整封。
    """

    @staticmethod
    def _raw(subject: str = "作业截止", body: str = "请提交作业。") -> bytes:
        return FetchOriginalTests._raw(subject, body)

    @staticmethod
    def _config() -> dict:
        return FetchOriginalTests._config()

    def _fetch(self, fake, **kwargs):
        with mock.patch.object(mailio.imaplib, "IMAP4_SSL", return_value=fake):
            return mailio.fetch_message_by_uid(self._config(), "授权码", kwargs.pop("uid", 7), **kwargs)

    @staticmethod
    def _body_specs(fake) -> list[str]:
        """真正取正文用的取法（问大小那一句不算）。"""
        return [args[1] for verb, args in fake.uid_calls
                if verb == "fetch" and "RFC822.SIZE" not in args[1]]

    def test_an_oversized_letter_is_refused_without_fetching_the_body(self):
        """关键断言：**不许出现任何取正文的命令**。

        只测「返回了错误」是不够的——最坏的那种改法就是先整封取回来、再报一句
        「太大了」，内存已经花掉了。
        """
        fake = FakeImap(uid_rows=(b'1 (BODY[] {123}', self._raw()),
                        sizes={7: mailio.MAX_MESSAGE_BYTES + 1})
        result = self._fetch(fake)
        self.assertEqual(result["state"], mailio.ORIGINAL_TOO_LARGE)
        self.assertEqual(self._body_specs(fake), [],
                         f"超限时连取正文的命令都不该出现，实际：{fake.uid_calls}")
        self.assertNotIn("message", result, "拒绝时不许带回任何正文")
        self.assertEqual(result["size_bytes"], mailio.MAX_MESSAGE_BYTES + 1)
        self.assertEqual(result["limit_bytes"], mailio.MAX_MESSAGE_BYTES)
        self.assertTrue(result["size_exact"])

    def test_a_normal_letter_is_still_readable(self):
        fake = FakeImap(uid_rows=(b'1 (BODY[] {123}', self._raw()), sizes={7: 40000})
        result = self._fetch(fake)
        self.assertEqual(result["state"], "ok")
        self.assertIn("请提交作业", result["message"]["body"])
        self.assertEqual(self._body_specs(fake), ["(BODY.PEEK[])"],
                         "限内照旧整封取，而且只用 PEEK")

    def test_without_size_support_the_fetch_is_still_bounded(self):
        """问不出大小**不能**退化成无上限整封读：改取有界的一段。"""
        fake = FakeImap(uid_rows=(b'1 (BODY[] {123}', self._raw()), size_supported=False)
        result = self._fetch(fake)
        self.assertEqual(result["state"], "ok", "问不出大小的小邮件仍然要能读")
        self.assertIn("请提交作业", result["message"]["body"])
        self.assertEqual(self._body_specs(fake),
                         [f"(BODY.PEEK[]<0.{mailio.MAX_MESSAGE_BYTES + 1}>)"],
                         "硬上限与 worker 是同一个常量")

    def test_without_size_support_a_huge_answer_is_refused(self):
        """有界 partial 拿满「上限+1」⇒ 这封信比上限大，按超限拒绝。"""
        cap = 4096                     # 用小上限，免得单测真的搬 25 MB
        fake = FakeImap(uid_rows=(b'1 (BODY[] {99999}', b"x" * (cap + 1)),
                        size_supported=False)
        with mock.patch.object(mailio, "MAX_MESSAGE_BYTES", cap):
            result = self._fetch(fake)
        self.assertEqual(result["state"], mailio.ORIGINAL_TOO_LARGE)
        self.assertEqual(self._body_specs(fake), [f"(BODY.PEEK[]<0.{cap + 1}>)"],
                         "只允许这一次有界取法，不许回头再取整封")
        self.assertFalse(result["size_exact"], "它只是「至少这么大」，不是精确大小")

    def test_a_server_that_refuses_partial_never_falls_back_to_the_whole_body(self):
        """partial 被拒时报错——**不赌**、也不改取整封。"""
        fake = FakeImap(uid_rows=(b'1 (BODY[] {123}', self._raw()),
                        size_supported=False, partial_supported=False)
        with self.assertRaises(mailio.MailError):
            self._fetch(fake)
        unbounded = [spec for spec in self._body_specs(fake) if "<0." not in spec]
        self.assertEqual(unbounded, [], f"partial 被拒之后不许改取整封：{fake.uid_calls}")


class _StubDb:
    def __init__(self, row):
        self.row = row

    def message_for_user(self, user_id, message_id):
        return self.row


class OversizedRefusalWordingTests(unittest.TestCase):
    """过大的信到用户眼前必须是**一句能照着做的话**，而不是一片空白的原信。

    网页层只认 gone/moved；过大这一档由 `service.read_original` 当场翻成
    `MailError`（两个路由都是 `MailError` → 400 说人话）。翻译/总结与看原信共用
    `read_original`，所以这里连**装配**一起测：拒绝之后绝不许再去调模型（那是花钱，
    而且是把一整封信发出去）。
    """

    TOO_LARGE = {"state": mailio.ORIGINAL_TOO_LARGE, "size_bytes": 30 * 1024 * 1024,
                 "size_exact": True, "limit_bytes": mailio.MAX_MESSAGE_BYTES}

    @staticmethod
    def _service():
        from pilot_app import service as service_mod
        from pilot_app.security import SecretBox

        db = _StubDb({"imap_host": "imap.example.com", "imap_port": 993,
                      "mailbox_email": "me@example.com", "imap_uid": 7, "uid_validity": "1"})
        service = service_mod.PilotService(db, SecretBox(b"7" * 32))
        service.mailbox_password = lambda row: "授权码"
        return service

    def test_reading_an_oversized_letter_says_what_to_do_instead(self):
        service = self._service()
        with mock.patch.object(mailio, "fetch_message_by_uid", return_value=self.TOO_LARGE):
            with self.assertRaises(mailio.MailError) as caught:
                service.read_original("usr_1", "msg_1")
        text = str(caught.exception)
        self.assertIn("太大", text)
        self.assertIn("30.0 MB", text, "服务器报了多少就说多少")
        self.assertIn(f"{mailio.MAX_MESSAGE_BYTES // 1048576} MB", text)
        self.assertIn("无法在网页里打开", text)
        self.assertIn("邮箱", text, "要给下一步：去哪儿还能看到这封信")

    def test_an_unknown_size_is_reported_as_a_lower_bound(self):
        """有界 partial 拿满时，那个数只是下界，不能写成「服务器报的大小」。"""
        service = self._service()
        lower = {**self.TOO_LARGE, "size_bytes": mailio.MAX_MESSAGE_BYTES + 1, "size_exact": False}
        with mock.patch.object(mailio, "fetch_message_by_uid", return_value=lower):
            with self.assertRaises(mailio.MailError) as caught:
                service.read_original("usr_1", "msg_1")
        self.assertIn("至少", str(caught.exception))

    def test_translate_and_summary_share_the_same_gate(self):
        from pilot_app import service as service_mod

        for kind in service_mod.PilotService.ASSIST_KINDS:
            with self.subTest(kind=kind):
                service = self._service()
                service.model_connection = mock.Mock(
                    side_effect=AssertionError("拒绝之后不许再调模型"))
                with mock.patch.object(mailio, "fetch_message_by_uid", return_value=self.TOO_LARGE):
                    with self.assertRaises(mailio.MailError) as caught:
                        service.assist("usr_1", "msg_1", kind)
                self.assertIn("无法在网页里打开", str(caught.exception))
