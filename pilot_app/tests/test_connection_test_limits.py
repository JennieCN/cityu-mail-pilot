"""A3：连接测试是「花真钱」的端点，要有限流、并发门禁与记账。

审查报告（`handoff/REVIEW-2026-09-22.md`）第三条 P1 的原话是：模拟一个登录用户连续
调用 `/api/test/model` 25 次，**25 次全 200**、供应商方法被调用 25 次；而 nginx 的限流
只盖 `/api/auth/`，不盖这里。走平台兜底 key 时这笔钱是运营者出的。

这个文件钉三件事，每一件都能单独为真而整体仍然是坏的：

* **限流真的挡住出站调用**（不是挡住响应）：被 429 的那几次，供应商方法一次都不该被调用；
* **两道并发闸门**都比「一次一个用户」更严：同一用户同时一次、全局同时 `TEST_MAX_INFLIGHT`；
* **记账**：成功的测试要在 `token_usage` 里留下一行——否则「我用了多少 / 谁付的」那张表
  会漏掉这些调用，而那张表正是用来回答「这笔钱算谁的」。

数字（每分钟 3 次、全局 2）是**建议初值**，所以断言读的是 `web.TEST_*` 常量而不是写死的
数字：改常量的人不该被测试挡住，但「改了之后仍然挡得住」要被证明。
"""

import datetime as dt
import http.cookiejar
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ.setdefault("INFE_PILOT_MASTER_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
os.environ.setdefault("INFE_PILOT_COOKIE_SECURE", "0")
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import database as database_mod  # noqa: E402
from pilot_app import providers, web  # noqa: E402
from pilot_app.security import token_hash  # noqa: E402

PASSWORD = "a-long-enough-password"
FAKE_KEY = "sk-fixture-limit000"


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


def _answer(text: str = "连接成功"):
    return providers.Generation(text, [], "stop", {"input": 11, "output": 4, "total": 15}, "stop")


class ConnectionTestLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # **自己一个库**，照 `test_password_reset.ResetHarness` 的先例：整个套件共用一个
        # 进程、`web.get_db()` 是进程级单例，往共享库里注册账号会污染别的套件。
        cls.database = database_mod.Database(os.path.join(_TMP, "testlimit.sqlite3"))
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
        # 每个用例一个干净的窗口与名额：限流是**进程级**状态，用例之间会互相污染。
        web._test_attempts.clear()
        web._test_inflight.clear()
        db_patch = mock.patch.object(web, "get_db", return_value=self.database)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        saved_service = web._service_singleton
        web._service_singleton = None
        self.addCleanup(lambda: setattr(web, "_service_singleton", saved_service))
        self._raise_the_account_cap()
        self.stamp = dt.datetime.now().timestamp()
        self.client = Client(self.base)
        self.user_id = self._register(f"limit-{self.stamp}@example.com")
        self._save_connection(self.client)

    def tearDown(self):
        web._test_attempts.clear()
        web._test_inflight.clear()

    def _raise_the_account_cap(self):
        """这个库现在归本模块用，名额上限要抬起来。

        `INFE_PILOT_MAX_USERS` 默认 5，而一个类里的用例共用一个库——每个用例注册一两个
        账号就会撞上限，报「试点名额已满」，看起来像注册坏了。改环境变量而不是改
        `app_settings`：SETTING 会留在库里，而这个库是本模块自己的，无所谓；环境变量
        是**进程级**的，所以必须还原。
        """
        saved = os.environ.get("INFE_PILOT_MAX_USERS")
        os.environ["INFE_PILOT_MAX_USERS"] = "50"

        def restore():
            if saved is None:
                os.environ.pop("INFE_PILOT_MAX_USERS", None)
            else:
                os.environ["INFE_PILOT_MAX_USERS"] = saved

        self.addCleanup(restore)

    def _register(self, email: str, client=None) -> str:
        """注册一个账号。

        `client` 可以指定：用**另一个 cookie jar** 注册第二个人，否则注册会把
        自己这边的会话换成新账号——那样「两个人的额度互不影响」这条就测不出来了
        （第一版就是这么写的，测试当场红）。
        """
        client = client or self.client
        code = f"limit-invite-{email}"
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with self.database.connect() as connection:
            connection.execute("INSERT INTO invites(code_hash,expires_at) VALUES(?,?)",
                               (token_hash(code), expiry))
        web.reset_signup_rate_limit()  # 见 web.reset_signup_rate_limit：限速按 IP，单测得自己清
        status, body, _ = client.post("/api/auth/register", {
            "email": email, "password": PASSWORD, "invite_code": code, "accepted_terms": True})
        self.assertEqual(status, 200, body)
        return (body.get("user") or body)["id"]

    @staticmethod
    def _offline_dns():
        return mock.patch.object(providers, "validate_outbound_https_url",
                                 side_effect=lambda url, **kw: url.strip().rstrip("/"))

    def _save_connection(self, client):
        with self._offline_dns():
            status, body, _ = client.put("/api/connections/model", {
                "provider": "custom_openai", "api_key": FAKE_KEY,
                "model": "gpt-4o-mini", "base_url": "https://api.example.com/v1"})
        self.assertEqual(status, 200, body)

    def test_the_limit_blocks_the_call_not_just_the_response(self):
        """被 429 的那几次，供应商方法**一次都不该被调用**——否则钱已经花了。"""
        with self._offline_dns(), mock.patch.object(providers, "generate", return_value=_answer()) as generate:
            statuses = [self.client.post("/api/test/model", {})[0]
                        for _ in range(web.TEST_RATE_LIMIT + 2)]
        self.assertEqual(statuses[:web.TEST_RATE_LIMIT], [200] * web.TEST_RATE_LIMIT)
        self.assertEqual(statuses[web.TEST_RATE_LIMIT:], [429] * 2, statuses)
        self.assertEqual(generate.call_count, web.TEST_RATE_LIMIT,
                         "429 的那两次不许真的打到供应商")

    def test_the_429_says_what_to_do(self):
        with self._offline_dns(), mock.patch.object(providers, "generate", return_value=_answer()):
            for _ in range(web.TEST_RATE_LIMIT):
                self.client.post("/api/test/model", {})
            status, body, _ = self.client.post("/api/test/model", {})
        self.assertEqual(status, 429)
        self.assertIn("每分钟", body["detail"])
        self.assertIn("稍等", body["detail"], "429 要给出下一步，不能只说『不行』")

    def test_a_second_user_is_not_blocked_by_the_first(self):
        """按用户限流：一个人的窗口不关别人的事。"""
        other = Client(self.base)
        self._register(f"limit-other-{self.stamp}@example.com", client=other)
        self._save_connection(other)
        with self._offline_dns(), mock.patch.object(providers, "generate", return_value=_answer()):
            for _ in range(web.TEST_RATE_LIMIT):
                self.client.post("/api/test/model", {})
            self.assertEqual(self.client.post("/api/test/model", {})[0], 429)
            self.assertEqual(other.post("/api/test/model", {})[0], 200,
                             "另一个人第一次点，不该被别人的额度挡住")

    def test_the_same_user_cannot_run_two_at_once(self):
        """同一用户同时一次：上一次还没回来就再点，多半是在乱点。"""
        started = threading.Event()
        release = threading.Event()

        def slow_generate(**kwargs):
            started.set()
            release.wait(timeout=5)
            return _answer()

        with self._offline_dns(), mock.patch.object(providers, "generate", side_effect=slow_generate):
            first = threading.Thread(target=lambda: self.client.post("/api/test/model", {}))
            first.start()
            self.assertTrue(started.wait(timeout=5), "第一次测试没起来")
            try:
                status, body, _ = self.client.post("/api/test/model", {})
                self.assertEqual(status, 429, body)
                self.assertIn("还没出结果", body["detail"])
            finally:
                release.set()
                first.join(timeout=5)

    def test_the_global_gate_caps_concurrent_tests(self):
        """全局并发上限：这一条只有**不同用户同时**才触发。"""
        import contextlib
        held = []
        try:
            for index in range(web.TEST_MAX_INFLIGHT):
                stack = contextlib.ExitStack()
                stack.enter_context(web._connection_test_slot(f"usr_synthetic_{index}"))
                held.append(stack)
            with self.assertRaises(web.ApiError) as caught:
                with web._connection_test_slot("usr_synthetic_extra"):
                    pass
            self.assertEqual(caught.exception.status, 429)
            self.assertIn("别的连接测试", caught.exception.detail)
        finally:
            for stack in held:
                stack.close()
        # 名额放回去了：现在能再占一个
        with web._connection_test_slot("usr_synthetic_after"):
            pass

    def test_a_failed_test_gives_the_slot_back(self):
        """失败也要放行：否则一次错误就把名额永久占住，用户再也点不动。"""
        with self._offline_dns(), mock.patch.object(providers, "generate",
                                                    side_effect=providers.ProviderError("boom")):
            self.assertEqual(self.client.post("/api/test/model", {})[0], 400)
        self.assertEqual(web._test_inflight, set(), "失败之后名额要还回去")

    def test_a_successful_test_is_written_into_the_usage_table(self):
        with self._offline_dns(), mock.patch.object(providers, "generate", return_value=_answer()):
            self.assertEqual(self.client.post("/api/test/model", {})[0], 200)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT kind, provider, model, total_tokens FROM token_usage WHERE user_id=?",
                (self.user_id,)).fetchone()
        self.assertIsNotNone(row, "成功的连接测试也要记账，否则用量表会说少了")
        self.assertEqual(row[0], "test-model")
        self.assertEqual(row[3], 15)
        # 而且它出现在用户自己那张用量视图里（不是只躺在表里）
        view = self.database.usage_for_user(self.user_id)
        self.assertGreaterEqual(view["totals"]["calls"], 1)
