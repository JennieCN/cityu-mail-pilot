"""A2：上游把自己的请求凭据回显回来时，我们这一侧不许把它留下。

这是 2026-09-22 那份审查（`handoff/REVIEW-2026-09-22.md`）的第二条 P1。它当时
**只是一条推断**：代码里那句注释写着「must never include the submitted key」，
但没有任何一行在做这件事。这个文件把它变成可验证的：

* 供应商把 key 回显在 **401 正文**、**JSON 字段**、**URL 查询串** 里 —— 三种都要抹掉；
* **异常链也不许留原文**（日志里的 traceback 会印 `__cause__`，而脱敏只作用于我们
  自己拼的那句话）；
* 真正要护住的不是屏幕上那一眼，而是**落库**：`connections.last_error`、
  `messages.last_error`、`reports.last_error` 都是明文列，而数据库每天进备份（滚动 7 天）。
  密钥本身是加密存的，被回显出来的那一份却不是——这个不对称才是这一条的分量所在。

反向验证过：把 `redact_secrets` 换成 `str()`（即修复前的行为），本文件里的
`test_the_error_body_is_scrubbed`、`test_nothing_is_persisted_when_the_provider_echoes_the_key`
两条会当场红。
"""

import datetime as dt
import http.cookiejar
import io
import json
import logging
import os
import tempfile
import threading
import traceback
import unittest
import urllib.error
import urllib.request
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ.setdefault("INFE_PILOT_MASTER_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
os.environ.setdefault("INFE_PILOT_COOKIE_SECURE", "0")
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import database as database_mod  # noqa: E402
from pilot_app import mailio, providers, web  # noqa: E402
from pilot_app.security import outbound_secrets, redact_secrets, token_hash  # noqa: E402


class OwnDatabaseMixin:
    """**自己一个库**，照 `test_password_reset.ResetHarness` 的先例。

    整个套件共用一个进程，而 `web.get_db()` 是进程级单例（路径由最先用它的模块钉死）。
    往那个共享库里注册账号会污染别的套件——2026-09-22 这一版第一稿就是这么把
    52 条测试弄红的（注册数撞上了共享库的名额上限）。所以这里把 HTTP 层指向本文件
    自己的库，跑完把单例还原，后面谁也别想被影响。
    """

    @classmethod
    def setUpClass(cls):
        cls.database = database_mod.Database(os.path.join(_TMP, "redaction.sqlite3"))
        cls.database.initialize()
        cls.server = web.create_server("127.0.0.1", 0)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        db_patch = mock.patch.object(web, "get_db", return_value=self.database)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        # 服务单例可能已被别的套件建过（那时绑的是别人的库）：清掉，让它按当前
        # 的 `get_db()` 重建；收工再清一次，别把绑着本模块库的单例留给下一个套件。
        self._saved_service = web._service_singleton
        web._service_singleton = None
        self.addCleanup(self._restore_service)

    def _restore_service(self):
        web._service_singleton = self._saved_service

    def _raise_the_account_cap(self):
        """名额上限默认 5，而这个库是整个类共用的（每个用例注册一两个账号）。"""
        saved = os.environ.get("INFE_PILOT_MAX_USERS")
        os.environ["INFE_PILOT_MAX_USERS"] = "50"

        def restore():
            if saved is None:
                os.environ.pop("INFE_PILOT_MAX_USERS", None)
            else:
                os.environ["INFE_PILOT_MAX_USERS"] = saved

        self.addCleanup(restore)

#: 虚构凭据。形状像真的，值只存在于这个文件里。
# 公开树的凭据扫描有两条规则会看它：`sk-` 之后要短于 20 个字符，且要含
# PLACEHOLDER_WORDS 里的词（这里用 fixture）——既有夹具 `sk-fixture-not-used` 就是这个形状。
FAKE_KEY = "sk-fixture-redact00"
FAKE_CODE = "AUTHCODE-REDACTION-16"
PASSWORD = "a-long-enough-password"


def _decode(raw: bytes):
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw.decode("utf-8", "replace")


class Client:
    def __init__(self, base: str) -> None:
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                  urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, method: str, path: str, payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(request, timeout=20) as response:
                return response.status, _decode(response.read()), dict(response.headers)
        except urllib.error.HTTPError as error:
            return error.code, _decode(error.read()), dict(error.headers)

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, payload=None):
        return self.request("POST", path, payload=payload)

    def put(self, path, payload=None):
        return self.request("PUT", path, payload=payload)


# ---------------------------------------------------------------------------
# 边界本身：三种回显形状 + 异常链
# ---------------------------------------------------------------------------


class RedactionHelperTests(unittest.TestCase):
    def test_values_come_from_headers_and_the_query_string(self):
        secrets = outbound_secrets({"Authorization": f"Bearer {FAKE_KEY}"},
                                   "https://api.example.com/v1?api_key=fixture-query-key-0001")
        self.assertIn(FAKE_KEY, secrets)
        self.assertIn(FAKE_KEY, secrets)  # 头原值
        self.assertIn("fixture-query-key-0001", secrets)

    def test_short_values_are_left_alone(self):
        """短串不参与精确替换：`str.replace("")` 会把整句话打成筛子（第一稿的 bug）。"""
        text = "max_tokens: 4000 exceeded (total tokens 12)"
        self.assertEqual(redact_secrets(text, ["", "ab"]), text)
        self.assertIn("max_tokens", redact_secrets(text, ["", "ab"]))

    def test_an_unrecognised_shape_is_swept_anyway(self):
        """我们没直接持有的那一份（例如被拼进 URL 的 key）靠形状兜底。"""
        text = "GET https://api.example.com/v1?key=NOTOUCHED-BY-US-1234 failed, password=hunter2hunter2"
        cleaned = redact_secrets(text)
        self.assertNotIn("NOTOUCHED-BY-US-1234", cleaned)
        self.assertNotIn("hunter2hunter2", cleaned)
        self.assertIn("api.example.com", cleaned, "抹的是凭据，不是整句话")


class JsonRequestRedactionTests(unittest.TestCase):
    """直接打 `_json_request`：四种上游回显形状 + 异常链。"""

    def _request_with_error(self, exc: Exception):
        with mock.patch.object(providers, "_outbound_open", side_effect=exc):
            with self.assertRaises(providers.ProviderError) as caught:
                providers._json_request(
                    f"https://api.example.com/v1/chat?api_key={FAKE_KEY}",
                    headers={"Authorization": f"Bearer {FAKE_KEY}"}, payload={"a": 1})
        return caught.exception

    def test_the_error_body_is_scrubbed(self):
        body = io.BytesIO(json.dumps({"error": {"message": f"Incorrect API key provided: {FAKE_KEY}"}}).encode())
        error = urllib.error.HTTPError("https://api.example.com/v1/chat", 401, "Unauthorized", {}, body)
        message = str(self._request_with_error(error))
        self.assertNotIn(FAKE_KEY, message)
        self.assertIn("401", message, "状态码要留下——它是有用的那一半")
        self.assertIn("Incorrect API key", message, "供应商的原话也要留下（有信息量的部分）")

    def test_a_key_in_the_url_is_scrubbed_even_when_the_reason_quotes_it(self):
        error = urllib.error.URLError(f"cannot reach https://api.example.com/v1?api_key={FAKE_KEY}")
        message = str(self._request_with_error(error))
        self.assertNotIn(FAKE_KEY, message)

    def test_the_exception_chain_carries_no_key(self):
        """脱敏只作用于我们拼的那句话，而 `__cause__` 会原样保留上游文本。

        所以凡原文可能带 URL/正文的那几支都断了链；这条断言看的是**日志里真正会印的
        那一整串**（`traceback.format_exception`），不是只有 message。
        """
        error = urllib.error.URLError(f"cannot reach https://api.example.com/v1?api_key={FAKE_KEY}")
        caught = self._request_with_error(error)
        printed = "".join(traceback.format_exception(type(caught), caught, caught.__traceback__))
        self.assertNotIn(FAKE_KEY, printed)

    def test_a_non_json_body_is_not_echoed_at_all(self):
        """200 + HTML 错误页：正文可能带着请求凭据，所以只报形状、不回显。"""
        class Response:
            _sent = False

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, size: int = -1):      # `_read_bounded` 会带 size 调它
                # 一次给完、之后报 EOF —— 假的响应也要守 HTTP 的规矩（一直吐会被上限拦住，
                # 那条路有自己的测试）。
                if self._sent:
                    return b""
                self._sent = True
                return f"<html><body>blocked {FAKE_KEY} {FAKE_CODE}</body></html>".encode()

        with mock.patch.object(providers, "_outbound_open", return_value=Response()):
            with self.assertRaises(providers.ProviderError) as caught:
                providers._json_request("https://api.example.com/v1/chat",
                                        headers={"Authorization": f"Bearer {FAKE_KEY}"}, payload={"a": 1})
        message = str(caught.exception)
        self.assertNotIn(FAKE_KEY, message)
        self.assertIn("不是 JSON", message)


class MailboxErrorRedactionTests(unittest.TestCase):
    """邮箱那侧的同一件事：授权码不许出现在错误文本里（明文列 `last_error`）。"""

    def test_the_imap_fallback_scrubs_the_authorisation_code(self):
        import imaplib
        message = mailio.explain_imap_failure(
            imaplib.IMAP4.error(f"weird server answer containing {FAKE_CODE}".encode()), secret=FAKE_CODE)
        self.assertNotIn(FAKE_CODE, message)
        self.assertIn("IMAP 连接失败", message)

    def test_a_known_shape_still_gets_its_actionable_wording(self):
        import imaplib
        message = mailio.explain_imap_failure(imaplib.IMAP4.error(b"LOGIN Login error or password error"),
                                              secret=FAKE_CODE)
        self.assertIn("授权码", message)


# ---------------------------------------------------------------------------
# 真正要护住的地方：落库与日志
# ---------------------------------------------------------------------------


class PersistenceTests(OwnDatabaseMixin, unittest.TestCase):
    """走真实 HTTP 面：失败一次连接测试，看数据库里留下了什么。"""

    def setUp(self):
        super().setUp()
        self._raise_the_account_cap()
        self.stamp = dt.datetime.now().timestamp()
        self.client = Client(self.base)
        self._register()

    def _register(self):
        code = f"redaction-invite-{self.stamp}"
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with self.database.connect() as connection:
            connection.execute("INSERT INTO invites(code_hash,expires_at) VALUES(?,?)",
                               (token_hash(code), expiry))
        web.reset_signup_rate_limit()  # 见 web.reset_signup_rate_limit：限速按 IP，单测得自己清
        status, body, _ = self.client.post("/api/auth/register", {
            "email": f"redaction-{self.stamp}@example.com", "password": PASSWORD,
            "invite_code": code, "accepted_terms": True})
        assert status == 200, body
        self.user_id = (body.get("user") or body)["id"]

    @staticmethod
    def _offline_dns():
        """测试不碰真 DNS：把「解析得到公网地址」固定住，其余逻辑照走。"""
        return mock.patch.object(providers, "validate_outbound_https_url",
                                 side_effect=lambda url, **kw: url.strip().rstrip("/"))

    def _save_custom_connection(self, client):
        """存一条自带地址的连接。

        `PUT /api/connections/*` 保存时会解析域名（`security.validate_outbound_https_url`），
        而测试不该依赖真 DNS：把「解析得到公网地址」这一步固定住，其余逻辑照走。
        """
        with self._offline_dns():
            return client.put("/api/connections/model", {
                "provider": "custom_openai", "api_key": FAKE_KEY,
                "model": "gpt-4o-mini", "base_url": "https://api.example.com/v1"})

    def test_nothing_is_persisted_when_the_provider_echoes_the_key(self):
        status, body, _ = self._save_custom_connection(self.client)
        self.assertEqual(status, 200, body)

        body = io.BytesIO(json.dumps({"error": {"message": f"Incorrect API key provided: {FAKE_KEY}"}}).encode())
        error = urllib.error.HTTPError("https://api.example.com/v1/chat/completions", 401, "Unauthorized", {}, body)

        with self._offline_dns(), mock.patch.object(providers, "_outbound_open", side_effect=error):
            status, payload, _ = self.client.post("/api/test/model", {})
        # 失败就是 400 + 一句人能读的 detail（这一条不是本次要改的行为，只钉住形状）
        self.assertEqual(status, 400, payload)
        self.assertNotIn(FAKE_KEY, json.dumps(payload, ensure_ascii=False),
                         "回执里不许有明文 key")
        self.assertIn("401", payload["detail"], "供应商的状态码要留下")

        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT last_error, last_test_at FROM connections WHERE user_id=? AND kind='model'",
                (self.user_id,)).fetchone()
        persisted = " ".join(str(item or "") for item in row)
        self.assertNotIn(FAKE_KEY, persisted, "落库的错误文本里不许有明文 key")
        self.assertIn("401", persisted, "但状态码要留下，否则这条记录就没用了")

    def test_the_log_does_not_carry_the_key(self):
        self._save_custom_connection(self.client)
        body = io.BytesIO(json.dumps({"error": {"message": f"bad key {FAKE_KEY}"}}).encode())
        error = urllib.error.HTTPError("https://api.example.com/v1/chat/completions", 401, "Unauthorized", {}, body)
        records: list[str] = []

        class Capture(logging.Handler):
            def emit(self, record):
                records.append(self.format(record))
                if record.exc_info:
                    records.append("".join(traceback.format_exception(*record.exc_info)))

        handler = Capture()
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            with self._offline_dns(), mock.patch.object(providers, "_outbound_open",
                                                        side_effect=error):
                self.client.post("/api/test/model", {})
        finally:
            root.removeHandler(handler)
        self.assertNotIn(FAKE_KEY, "\n".join(records))


class ReportFailureRedactionTests(OwnDatabaseMixin, unittest.TestCase):
    """报告生成那条路：失败文本会进 `messages.last_error` 与 `reports.last_error`。"""

    def setUp(self):
        super().setUp()
        self._raise_the_account_cap()
        self.stamp = dt.datetime.now().timestamp()

    def test_a_provider_error_never_reaches_the_database_with_the_key(self):
        client = Client(self.base)
        code = f"redaction-report-{self.stamp}"
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with self.database.connect() as connection:
            connection.execute("INSERT INTO invites(code_hash,expires_at) VALUES(?,?)",
                               (token_hash(code), expiry))
        web.reset_signup_rate_limit()  # 见 web.reset_signup_rate_limit：限速按 IP，单测得自己清
        status, body, _ = client.post("/api/auth/register", {
            "email": f"redaction-report-{self.stamp}@example.com", "password": PASSWORD,
            "invite_code": code, "accepted_terms": True})
        self.assertEqual(status, 200, body)
        user_id = (body.get("user") or body)["id"]
        with self.database.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO mailboxes(id,user_id,email,report_to,imap_host,imap_port,
                   smtp_host,smtp_port,encrypted_password,updated_at)
                   VALUES('mbx_redact',?,?,?,'h',993,'h',465,X'00',?)""",
                (user_id, "redact@example.com", "redact@example.com", "2026-09-22T00:00:00+00:00"))
        message_id = self.database.insert_message(
            user_id, "mbx_redact", "1", 4242,
            {"subject": "作业", "sender_name": "老师", "sender_address": "t@cityu.edu.hk",
             "received": "2026-09-22T00:00:00+00:00", "importance": "normal", "body": b"\x00"})

        body = io.BytesIO(json.dumps({"error": {"message": f"Invalid key: {FAKE_KEY}"}}).encode())
        error = urllib.error.HTTPError("https://api.example.com/v1/chat/completions", 401, "Unauthorized", {}, body)

        with mock.patch.object(providers, "_outbound_open", side_effect=error):
            with self.assertRaises(Exception) as caught:
                providers.generate_report(
                    {"provider": "custom_openai", "model": "gpt-4o-mini",
                     "api_key": FAKE_KEY, "base_url": "https://api.example.com/v1"},
                    {"subject": "作业", "sender_name": "老师", "sender_address": "t@cityu.edu.hk",
                     "received": "2026-09-22T00:00:00+00:00", "body": "请交作业"},
                    {"school_email": "", "major": "", "timezone": "Asia/Hong_Kong"}, [])
        self.assertNotIn(FAKE_KEY, str(caught.exception))
        printed = "".join(traceback.format_exception(*(
            type(caught.exception), caught.exception, caught.exception.__traceback__)))
        self.assertNotIn(FAKE_KEY, printed)
        # 让这条路真的把失败写进去，再读回来核一遍（这才是「落库」那一条）。
        self.database.fail_message(message_id, str(caught.exception), None)
        with self.database.connect() as connection:
            stored = connection.execute("SELECT last_error FROM messages WHERE id=?",
                                        (message_id,)).fetchone()[0]
        self.assertNotIn(FAKE_KEY, stored or "")
