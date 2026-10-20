"""A1：出站请求不许被重定向带走，邮件服务器的目的地要**在连接时**校验。

审查报告（`handoff/REVIEW-2026-09-22.md` 第一条 P1）说：自定义 API 地址的检查在保存时
做，而真正发请求用的是默认 `urlopen`——**会跟 302**，而 Python 的重定向处理器会把
`Authorization` 一起带过去。这不是理论：本机实测（Python 3.9，一个只回 302 的替身服务）
目标那侧收到的请求头里仍有 `Bearer sk-…`。

这个文件钉三件事：

* **不跟重定向**，而且**目标连一次都没被访问过**（替身服务自己记账，不是看返回值猜的）；
* 判决写在**一个地方**（`_NoRedirects`）且只有**一个 opener**（`_OUTBOUND_OPEN`），
  不给「某处漏用默认 opener」留口子；
* IMAP/SMTP 的目的地**连接时再验一次**：保存时那次与实际连接之间隔着任意长的时间，
  中间 DNS 可以变。

**诚实边界**（也写在 `docs/outbound-guard-2026-09-22.md`）：这里关掉的是「跟重定向」
与「保存后 DNS 变了」这两个口子；解析到连接之间那点竞态（DNS 重绑定）**没有被根治**——
根治它要么自己钉住对端 IP（标准库做不到完整正确），要么在系统层加出站规则。
"""

import http.server
import imaplib
import os
import smtplib
import socket
import ssl
import tempfile
import threading
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ.setdefault("INFE_PILOT_MASTER_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
os.environ.setdefault("INFE_PILOT_COOKIE_SECURE", "0")
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import mailio, providers  # noqa: E402

FAKE_KEY = "sk-fixture-redirect0"


class _Redirector(http.server.BaseHTTPRequestHandler):
    """一个只做两件事的替身：回 302，或者记下「有人在 /target 上敲过门」。"""

    hits: list[tuple[str, str]] = []

    def log_message(self, *args):  # 测试里不要访问日志
        pass

    def _record(self):
        type(self).hits.append((self.path.split("?")[0], self.headers.get("Authorization") or ""))

    def do_GET(self):
        self._record()
        if self.path.split("?")[0] == "/start":
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/target")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = do_GET


class RedirectRefusalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _Redirector.hits = []
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), _Redirector)
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def test_a_302_is_refused_and_the_target_is_never_visited(self):
        """**替身服务自己记账**：判决不能只看「抛没抛异常」。"""
        _Redirector.hits = []
        with self.assertRaises(providers.ProviderError) as caught:
            providers._json_request(f"http://127.0.0.1:{self.port}/start",
                                    headers={"Authorization": f"Bearer {FAKE_KEY}"}, payload={"a": 1})
        self.assertIn("重定向", str(caught.exception))
        visited = [path for path, _ in _Redirector.hits]
        self.assertEqual(visited, ["/start"], f"重定向目标被访问了：{visited}")
        self.assertNotIn("/target", visited, "302 那一跳之后不该再有第二个请求")

    def test_the_message_says_what_to_do(self):
        with self.assertRaises(providers.ProviderError) as caught:
            providers._json_request(f"http://127.0.0.1:{self.port}/start", headers={}, payload={"a": 1})
        self.assertIn("请直接填最终地址", str(caught.exception))

    def test_the_redirect_target_is_redacted_in_the_message(self):
        """目标地址会进异常文本，所以它也要过脱敏（那是外站可控的字符串）。"""
        with self.assertRaises(providers.ProviderError) as caught:
            providers._json_request(f"http://127.0.0.1:{self.port}/start?api_key={FAKE_KEY}",
                                    headers={"Authorization": f"Bearer {FAKE_KEY}"}, payload={"a": 1})
        printed = str(caught.exception)
        self.assertNotIn(FAKE_KEY, printed)

    def test_a_normal_answer_still_works(self):
        """没有重定向时一切照旧——否则这条修复会把正常供应商也挡掉。"""
        answer = providers._json_request(f"http://127.0.0.1:{self.port}/target", headers={}, method="GET")
        self.assertEqual(answer, {"ok": True})

    def test_there_is_only_one_opener(self):
        """防的是「某处漏用默认 opener」：出站请求只有这一个入口。"""
        self.assertTrue(any(isinstance(handler, providers._NoRedirects)
                            for handler in providers._OUTBOUND_OPENER.handlers),
                        "默认的重定向处理器还在，302 会被跟走")
        with mock.patch.object(providers, "_outbound_open", side_effect=RuntimeError("拦截")) as fake:
            with self.assertRaises(RuntimeError):
                providers._json_request("https://api.example.com/v1/chat", headers={}, payload={"a": 1})
        self.assertEqual(fake.call_count, 1)


class MailboxDestinationTests(unittest.TestCase):
    """IMAP/SMTP：保存时验过一次，**连接前再验一次**。"""

    MAILBOX = {"email": "me@example.com", "report_to": "me@example.com",
               "imap_host": "mail.example.com", "imap_port": 993,
               "smtp_host": "smtp.example.com", "smtp_port": 465, "last_uid": 0,
               "uid_validity": "", "enabled": 1}

    def test_imap_refuses_a_host_that_now_resolves_private(self):
        calls = []

        def fake_getaddrinfo(host, port, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

        with mock.patch.object(socket, "getaddrinfo", side_effect=fake_getaddrinfo), \
                mock.patch.object(imaplib, "IMAP4_SSL", side_effect=lambda *a, **k: calls.append(a)):
            with self.assertRaises(mailio.MailError) as caught:
                mailio.fetch_new_messages(dict(self.MAILBOX), "authcode-16chars")
        self.assertIn("不被允许", str(caught.exception))
        self.assertEqual(calls, [], "目的地不合规时**一次连接都不该发起**")

    def test_smtp_refuses_a_host_that_now_resolves_private(self):
        calls = []

        def fake_getaddrinfo(host, port, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))]

        with mock.patch.object(socket, "getaddrinfo", side_effect=fake_getaddrinfo), \
                mock.patch.object(smtplib, "SMTP_SSL", side_effect=lambda *a, **k: calls.append(a)):
            with self.assertRaises(mailio.MailError) as caught:
                mailio.send_report(dict(self.MAILBOX), "authcode-16chars", "主题", "正文")
        self.assertIn("不被允许", str(caught.exception))
        self.assertEqual(calls, [], "目的地不合规时一次连接都不该发起")

    def test_an_unresolvable_host_is_left_to_the_connection(self):
        """解析不出来 ≠ 地址不被允许。

        这条是实测出来的：把「解析不出来」也当成拒绝之后，`test_read_original` 与
        `test_mailio` 里 29 条测试红了——它们的夹具主机是 `imap.example.com`（有意不可解析，
        连接本身是打桩的）。更重要的是**生产**：DNS 临时抽风时用户看到的会是
        「地址不被允许」，那是个假话，而且他会去改一个没写错的配置。
        """
        connected = []

        def fake_getaddrinfo(host, port, *args, **kwargs):
            raise socket.gaierror("Name or service not known")

        class FakeIMAP:
            # `ssl_context=`：2026-09-24 起 IMAP 必须带一个校验证书的上下文
            # （见 `test_imap_tls.py`）；这里顺手断言一下，免得这条守卫偷偷退化。
            def __init__(self, host, port, timeout=None, ssl_context=None):
                if getattr(ssl_context, "verify_mode", None) != ssl.CERT_REQUIRED:
                    raise AssertionError("IMAP4_SSL 没有收到校验证书的 ssl_context")
                connected.append(host)

        with mock.patch.object(socket, "getaddrinfo", side_effect=fake_getaddrinfo), \
                mock.patch.object(mailio.imaplib, "IMAP4_SSL", FakeIMAP):
            try:
                mailio.fetch_new_messages(dict(self.MAILBOX), "authcode-16chars")
            except Exception as exc:      # 连上之后的步骤由别的测试负责，这里只看它有没有去连
                self.assertNotIn("不被允许", str(exc))
        self.assertEqual(connected, ["mail.example.com"], "解析不出来时要真的去连一次")

    def test_a_public_host_still_connects(self):
        """别把正常邮箱挡掉：解析到公网地址时照常往下走（这里在连接那一步停住）。"""
        class Refused(Exception):
            pass

        def fake_getaddrinfo(host, port, *args, **kwargs):
            # 8.8.8.x 是仓库里**已经批准**的测试地址（公开解析器，指不到任何人/我们的机器）
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]

        def explode(*args, **kwargs):
            raise Refused("到这里说明目的地检查放行了")

        with mock.patch.object(socket, "getaddrinfo", side_effect=fake_getaddrinfo), \
                mock.patch.object(imaplib, "IMAP4_SSL", side_effect=explode):
            with self.assertRaises(Refused):
                mailio.fetch_new_messages(dict(self.MAILBOX), "authcode-16chars")
