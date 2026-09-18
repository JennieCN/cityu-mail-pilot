"""End-to-end tests for the standard-library web layer.

These drive a real ``ThreadingHTTPServer`` over loopback HTTP with a cookie
jar, so routing, cookies, security headers, throttling and JSON validation are
all exercised the way the browser will exercise them.
"""

import datetime as dt
import http.cookiejar
import json
import os
import tempfile
import threading
import pathlib
import unittest
import urllib.error
import urllib.request
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["INFE_PILOT_DB"] = _TMP + "/web.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
os.environ["INFE_PILOT_MAX_USERS"] = "50"
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import web  # noqa: E402
from pilot_app.security import token_hash  # noqa: E402
from pilot_app.web import db, service  # noqa: E402


class Client:
    """Minimal cookie-aware JSON client built on the standard library."""

    def __init__(self, base: str) -> None:
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, method: str, path: str, payload=None, headers=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read()
                return response.status, _decode(raw), dict(response.headers)
        except urllib.error.HTTPError as error:
            raw = error.read()
            return error.code, _decode(raw), dict(error.headers)

    def get(self, path, **kwargs):
        return self.request("GET", path, **kwargs)

    def post(self, path, payload=None, **kwargs):
        return self.request("POST", path, payload=payload, **kwargs)

    def put(self, path, payload=None, **kwargs):
        return self.request("PUT", path, payload=payload, **kwargs)


def _decode(raw: bytes):
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw.decode("utf-8", "replace")


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
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
        self.client = Client(self.base)

    @staticmethod
    def invite(code: str) -> None:
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with db.connect() as connection:
            connection.execute(
                "INSERT INTO invites(code_hash,expires_at) VALUES(?,?)", (token_hash(code), expiry)
            )

    # -- original behavioural guarantees -----------------------------------

    def test_complete_onboarding_never_returns_secrets(self):
        self.invite("pilot-invite-test")
        status, user, _ = self.client.post("/api/auth/register", {
            "email": "pilot@example.com", "password": "a-long-pilot-password", "invite_code": "pilot-invite-test", "accepted_terms": True,
        })
        self.assertEqual(status, 200, user)
        user_id = user["id"]

        status, body, _ = self.client.put("/api/profile", {
            "school_email": "student@my.cityu.edu.hk", "major": "通信工程", "year_of_study": "大二", "courses": ["C++", "密码学"],
            "interests": ["网络安全"], "career_goals": ["通信工程师"], "focus_topics": ["实习"],
            "less_interested": ["广告"], "custom_instructions": "优先说明截止日期", "language": "bilingual",
            "timezone": "Asia/Hong_Kong", "immediate_enabled": True, "daily_enabled": True, "daily_time": "22:00",
        })
        self.assertEqual(status, 200, body)

        status, body, _ = self.client.put("/api/mailbox", {
            "email": "pilot@qq.com", "report_to": "reports@qq.com", "imap_host": "imap.qq.com",
            "imap_port": 993, "smtp_host": "smtp.qq.com", "smtp_port": 465, "app_password": "mail-secret",
            "accepted_terms": True,
        })
        self.assertEqual(status, 200, body)

        status, body, _ = self.client.put("/api/connections/model", {
            "provider": "openai", "model": "gpt-test", "api_key": "model-secret",
        })
        self.assertEqual(status, 200, body)

        status, data, _ = self.client.get("/api/me")
        self.assertEqual(status, 200, data)
        rendered = str(data)
        self.assertEqual(data["profile"]["major"], "通信工程")
        self.assertEqual(data["profile"]["school_email"], "student@my.cityu.edu.hk")
        self.assertNotIn("mail-secret", rendered)
        self.assertNotIn("model-secret", rendered)
        self.assertNotIn("encrypted_password", rendered)
        self.assertNotIn("encrypted_api_key", rendered)

        encrypted = service.encrypt_report("private report body", user_id)
        db.create_report(user_id=user_id, message_id=None, kind="test", subject="test report",
                         body=encrypted, sent_to="pilot@example.com")

        status, reports, _ = self.client.get("/api/reports")
        self.assertEqual(status, 200, reports)
        self.assertEqual(reports[0]["body_markdown"], "private report body")

        with db.connect() as connection:
            raw = connection.execute(
                "SELECT body_markdown FROM reports WHERE user_id=?", (user_id,)
            ).fetchone()[0]
        self.assertIsInstance(raw, bytes)
        self.assertNotIn(b"private report body", raw)

    def test_two_browser_sessions_never_cross_user_data(self):
        first = Client(self.base)
        second = Client(self.base)
        users = []
        for client, suffix in ((first, "one"), (second, "two")):
            code = "isolation-" + suffix
            self.invite(code)
            status, user, _ = client.post("/api/auth/register", {
                "email": suffix + "@example.com",
                "password": "a-long-pilot-password",
                "invite_code": code, "accepted_terms": True,
            })
            self.assertEqual(status, 200)
            users.append(user)
            status, _, _ = client.put("/api/profile", {
                "major": "major-" + suffix,
                "daily_time": "22:00",
            })
            self.assertEqual(status, 200)
            db.create_report(
                user_id=user["id"], message_id=None, kind="test",
                subject="report-" + suffix,
                body=service.encrypt_report("body-" + suffix, user["id"]),
                sent_to=suffix + "@example.com",
            )

        first_me = first.get("/api/me")[1]
        second_me = second.get("/api/me")[1]
        self.assertEqual(first_me["profile"]["major"], "major-one")
        self.assertEqual(second_me["profile"]["major"], "major-two")
        self.assertEqual([row["subject"] for row in first.get("/api/reports")[1]], ["report-one"])
        self.assertEqual([row["subject"] for row in second.get("/api/reports")[1]], ["report-two"])
        self.assertNotIn("body-two", str(first.get("/api/reports")[1]))
        self.assertNotIn("body-one", str(second.get("/api/reports")[1]))

    def test_health_and_catalog_are_public(self):
        status, body, _ = self.client.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        status, catalog, _ = self.client.get("/api/catalog")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(catalog["models"]), 10)
        self.assertGreaterEqual(len(catalog["search"]), 3)
        mailbox = catalog["mailbox"]
        self.assertGreaterEqual(len(mailbox["presets"]), 5)
        self.assertIn("imap", mailbox["glossary"])
        qq = next(item for item in mailbox["presets"] if item["id"] == "qq")
        self.assertEqual(qq["imap_host"], "imap.qq.com")
        self.assertTrue(qq["steps"])
        # `blocked_reason` / `recommended` are what the "换一个邮箱" box is built
        # from: a non-empty reason means this provider cannot work at all, and
        # the recommended ones are the alternatives it offers.
        # `where` 是「在你邮箱的哪一块」那句（设置 → 账户 → IMAP/SMTP 服务）。
        # 第 3 步的示意图上画的就是这同一句，`test_appcode_shots` 逐字比对两边。
        allowed = {"id", "label", "short_label", "domains", "imap_host", "imap_port",
                   "smtp_host", "smtp_port", "steps", "help_url", "help_label", "caution",
                   "blocked_reason", "recommended", "where"}
        for item in mailbox["presets"]:
            self.assertEqual(set(item), allowed, item["id"])

    # -- standard-library layer specifics ----------------------------------

    def test_static_assets_and_security_headers(self):
        # The application moved to /app when the marketing page took the root.
        status, body, headers = self.client.get("/app")
        self.assertEqual(status, 200)
        self.assertIn("app.js", body)
        self.assertIn("复制私人邮箱", body)
        self.assertIn("outlook.office.com/mail/options/mail/forwarding", body)
        self.assertIn("outlook.office.com/mail/options/mail/rules", body)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])

        status, script, headers = self.client.get("/app.js")
        self.assertEqual(status, 200)
        self.assertIn("api(", script)
        self.assertIn("copyForwardTarget", script)
        self.assertIn("renderForwardingWizard", script)
        self.assertTrue(headers["Content-Type"].startswith("application/javascript"))

        status, _, _ = self.client.get("/manifest.webmanifest")
        self.assertEqual(status, 200)

    def test_unknown_and_traversal_paths_are_not_served(self):
        for path in ("/nope", "/../pilot_app/web.py", "/static/app.js"):
            status, body, _ = self.client.get(path)
            self.assertEqual(status, 404, path)
            self.assertIn("detail", body)

    def test_wrong_method_is_reported(self):
        status, body, _ = self.client.get("/api/auth/register")
        self.assertEqual(status, 405)
        self.assertIn("detail", body)

    def test_anonymous_access_is_rejected(self):
        for path in ("/api/me", "/api/reports", "/api/usage"):
            status, body, _ = self.client.get(path)
            self.assertEqual(status, 401, path)
            self.assertIn("detail", body)
        status, _, _ = self.client.put("/api/profile", {"major": "x"})
        self.assertEqual(status, 401)

    def test_origin_fence_blocks_cross_site_writes(self):
        os.environ["INFE_PILOT_ORIGIN"] = "https://mail.example.com"
        try:
            status, _, _ = self.client.post("/api/auth/login",
                                            {"email": "a@b.com", "password": "x"},
                                            headers={"Origin": "https://evil.example.com"})
            self.assertEqual(status, 403)
            status, _, _ = self.client.post("/api/auth/login",
                                            {"email": "a@b.com", "password": "x"},
                                            headers={"Origin": "https://mail.example.com"})
            self.assertEqual(status, 401)
            status, _, _ = self.client.get("/health", headers={"Origin": "https://evil.example.com"})
            self.assertEqual(status, 200)
        finally:
            os.environ.pop("INFE_PILOT_ORIGIN", None)

    def test_login_throttle_locks_out_after_repeated_failures(self):
        web._login_attempts.clear()
        try:
            for attempt in range(8):
                status, _, _ = self.client.post("/api/auth/login",
                                                {"email": "throttle@example.com", "password": "wrong-password"})
                self.assertEqual(status, 401, "attempt %d" % attempt)
            status, body, _ = self.client.post("/api/auth/login",
                                               {"email": "throttle@example.com", "password": "wrong-password"})
            self.assertEqual(status, 429)
            self.assertIn("detail", body)
        finally:
            web._login_attempts.clear()

    def test_oversized_body_is_refused(self):
        status, body, _ = self.client.post("/api/auth/register", {"email": "x" * 100_000})
        self.assertEqual(status, 413)
        self.assertIn("detail", body)

    def test_invalid_payloads_are_rejected(self):
        self.invite("payload-invite")
        status, _, _ = self.client.post("/api/auth/register",
                                        {"email": "not-an-email", "password": "a-long-pilot-password",
                                         "invite_code": "payload-invite", "accepted_terms": True})
        self.assertEqual(status, 422)

        status, _, _ = self.client.post("/api/auth/register",
                                        {"email": "payload@example.com", "password": "a-long-pilot-password",
                                         "invite_code": "payload-invite", "accepted_terms": True})
        self.assertEqual(status, 200)

        status, _, _ = self.client.put("/api/profile", {"major": "x", "daily_time": "25:99"})
        self.assertEqual(status, 422)

        status, _, _ = self.client.put("/api/mailbox", {
            "email": "a@qq.com", "report_to": "b@qq.com", "imap_host": "imap.qq.com", "imap_port": 0,
            "smtp_host": "smtp.qq.com", "smtp_port": 465, "app_password": "x",
            "accepted_terms": True,
        })
        self.assertEqual(status, 422)

        status, _, _ = self.client.post("/api/auth/register", "not-an-object")
        self.assertEqual(status, 422)

    def test_invite_is_single_use_and_account_lifecycle_works(self):
        self.invite("lifecycle-invite")
        credentials = {"email": "lifecycle@example.com", "password": "a-long-pilot-password",
                       "invite_code": "lifecycle-invite", "accepted_terms": True}
        status, body, _ = self.client.post("/api/auth/register", credentials)
        self.assertEqual(status, 200, body)

        status, _, _ = self.client.post("/api/auth/register", credentials)
        self.assertEqual(status, 400)

        status, body, headers = self.client.post("/api/auth/login", {
            "email": "lifecycle@example.com", "password": "a-long-pilot-password"})
        self.assertEqual(status, 200, body)
        session_cookie = headers.get("Set-Cookie", "")
        self.assertIn("cityu_mail_session=", session_cookie)
        self.assertIn("HttpOnly", session_cookie)
        self.assertIn("SameSite=Lax", session_cookie)
        self.assertNotIn("Secure", session_cookie)  # INFE_PILOT_COOKIE_SECURE=0 in this suite

        status, _, _ = self.client.put("/api/account/status/paused")
        self.assertEqual(status, 200)
        status, body, _ = self.client.get("/api/me")
        self.assertEqual(status, 200)
        self.assertEqual(body["user"]["status"], "paused")

        status, _, _ = self.client.put("/api/account/status/bogus")
        self.assertEqual(status, 422)

        status, _, _ = self.client.put("/api/account/status/deleted")
        self.assertEqual(status, 200)
        status, _, _ = self.client.get("/api/me")
        self.assertEqual(status, 401)


    def test_deletion_purges_encrypted_data_and_retires_invite(self):
        self.invite("purge-invite")
        status, user, _ = self.client.post("/api/auth/register", {
            "email": "purge@example.com", "password": "a-long-pilot-password", "invite_code": "purge-invite", "accepted_terms": True})
        self.assertEqual(status, 200, user)
        user_id = user["id"]

        self.assertEqual(self.client.put("/api/mailbox", {
            "email": "purge@qq.com", "report_to": "purge@qq.com", "imap_host": "imap.qq.com",
            "imap_port": 993, "smtp_host": "smtp.qq.com", "smtp_port": 465, "app_password": "purge-secret",
            "accepted_terms": True,
        })[0], 200)
        self.assertEqual(self.client.put("/api/connections/model", {
            "provider": "openai", "model": "gpt-test", "api_key": "purge-key"})[0], 200)
        db.create_report(user_id=user_id, message_id=None, kind="test", subject="s",
                         body=service.encrypt_report("body", user_id), sent_to="purge@example.com")

        status, _, _ = self.client.put("/api/account/status/deleted")
        self.assertEqual(status, 200)

        with db.connect() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM users WHERE id=?", (user_id,)).fetchone()[0], 0)
            for table in ("sessions", "mailboxes", "connections", "reports", "profiles"):
                remaining = connection.execute(
                    "SELECT COUNT(*) FROM %s WHERE user_id=?" % table, (user_id,)).fetchone()[0]
                self.assertEqual(remaining, 0, table)
            invite = connection.execute(
                "SELECT used_by, expires_at FROM invites WHERE code_hash=?", (token_hash("purge-invite"),)).fetchone()
        self.assertIsNone(invite[0])
        self.assertLess(invite[1], dt.datetime.now(dt.timezone.utc).isoformat())

        # The retired code must not come back to life after its user leaves.
        status, _, _ = self.client.post("/api/auth/register", {
            "email": "purge2@example.com", "password": "a-long-pilot-password", "invite_code": "purge-invite", "accepted_terms": True})
        self.assertEqual(status, 400)


    def test_registration_respects_user_cap(self):
        os.environ["INFE_PILOT_MAX_USERS"] = str(db.count_users())
        try:
            self.invite("cap-invite")
            status, body, _ = self.client.post("/api/auth/register", {
                "email": "cap@example.com", "password": "a-long-pilot-password", "invite_code": "cap-invite", "accepted_terms": True})
            self.assertEqual(status, 403)
            self.assertIn("detail", body)
        finally:
            os.environ["INFE_PILOT_MAX_USERS"] = "50"


    def test_missing_api_key_is_reported_clearly(self):
        self.invite("key-invite")
        status, _, _ = self.client.post("/api/auth/register", {
            "email": "key@example.com", "password": "a-long-pilot-password", "invite_code": "key-invite", "accepted_terms": True})
        self.assertEqual(status, 200)
        for kind in ("model", "search"):
            provider = "openai" if kind == "model" else "tavily"
            status, body, _ = self.client.put("/api/connections/%s" % kind,
                                              {"provider": provider, "api_key": ""})
            self.assertEqual(status, 422, kind)
            self.assertEqual(body["detail"], "请填写 API key。", kind)

    def test_blank_report_address_defaults_to_the_mailbox(self):
        self.invite("report-invite")
        status, _, _ = self.client.post("/api/auth/register", {
            "email": "report@example.com", "password": "a-long-pilot-password", "invite_code": "report-invite", "accepted_terms": True})
        self.assertEqual(status, 200)

        # The form says "leave blank = send back to the same mailbox".
        status, body, _ = self.client.put("/api/mailbox", {
            "email": "report@qq.com", "report_to": "", "imap_host": "imap.qq.com", "imap_port": 993,
            "smtp_host": "smtp.qq.com", "smtp_port": 465, "app_password": "mail-secret",
            "accepted_terms": True,
        })
        self.assertEqual(status, 200, body)
        self.assertEqual(self.client.get("/api/me")[1]["mailbox"]["report_to"], "report@qq.com")

        # A real address is still honoured, and a malformed one is still refused.
        status, _, _ = self.client.put("/api/mailbox", {
            "email": "report@qq.com", "report_to": "other@qq.com", "imap_host": "imap.qq.com",
            "imap_port": 993, "smtp_host": "smtp.qq.com", "smtp_port": 465, "app_password": "mail-secret",
            "accepted_terms": True,
        })
        self.assertEqual(status, 200)
        self.assertEqual(self.client.get("/api/me")[1]["mailbox"]["report_to"], "other@qq.com")

    def test_school_email_requires_cityu_domain(self):
        self.invite("school-email-invite")
        status, _, _ = self.client.post("/api/auth/register", {
            "email": "school@example.com", "password": "a-long-pilot-password", "invite_code": "school-email-invite", "accepted_terms": True})
        self.assertEqual(status, 200)
        status, body, _ = self.client.put("/api/profile", {"school_email": "student@example.com"})
        self.assertEqual(status, 422)
        self.assertIn("CityU", body["detail"])

        # `accepted_terms` is here so this reaches the address check at all: the
        # authorization gate in front of it answers 400 for a first-time save
        # (see `MailAuthorizationGateTests` in test_compliance).
        status, body, _ = self.client.put("/api/mailbox", {
            "email": "report@qq.com", "report_to": "not-an-address", "imap_host": "imap.qq.com",
            "imap_port": 993, "smtp_host": "smtp.qq.com", "smtp_port": 465, "app_password": "mail-secret",
            "accepted_terms": True,
        })
        self.assertEqual(status, 422)
        self.assertIn("邮箱地址格式不正确", body["detail"])

    # -- PWA assets -------------------------------------------------------

    def test_pwa_icons_are_served_and_are_real_pngs(self):
        """Installing to a home screen needs valid, reachable icons."""
        for path in ("/icon-192.png", "/icon-512.png", "/apple-touch-icon.png"):
            response = self.client.request("GET", path)
            status, body, headers = response
            self.assertEqual(status, 200, path)
            self.assertEqual(headers.get("Content-Type"), "image/png", path)
            self.assertTrue(isinstance(body, str), "PNG 会被当作非 JSON 解码")
        # The manifest must reference the icons so browsers can find them.
        status, manifest, _ = self.client.get("/manifest.webmanifest")
        self.assertEqual(status, 200)
        with open(web.STATIC_ROOT / "manifest.webmanifest", encoding="utf-8") as handle:
            import json as _json
            data = _json.load(handle)
        self.assertEqual(data["display"], "standalone")
        sources = {icon["src"] for icon in data["icons"]}
        self.assertIn("/icon-192.png", sources)
        self.assertIn("/icon-512.png", sources)

    def test_icon_files_are_valid_png_on_disk(self):
        import struct
        for name in ("icon-192.png", "icon-512.png", "apple-touch-icon.png"):
            data = (web.STATIC_ROOT / name).read_bytes()
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n", name)
            width, height = struct.unpack(">II", data[16:24])
            self.assertEqual(width, height, name)
            self.assertGreaterEqual(width, 180, name)

    # -- account security -------------------------------------------------

    def test_security_endpoints_require_login(self):
        for method, path in (("GET", "/api/account/security"),
                             ("PUT", "/api/account/password"),
                             ("POST", "/api/account/sessions/revoke")):
            status, _, _ = self.client.request(method, path, {"current_password": "x", "new_password": "abcdefghijkl"})
            self.assertEqual(status, 401, f"{method} {path}")

    # -- what an unexpected failure leaves behind --------------------------

    def test_an_unexpected_error_logs_the_route_it_failed_on(self):
        """A reported 500 has to name the endpoint, not just the stack.

        On 2026-09-15 the operator reported "点分析助手显示服务器内部错误", and the
        only way to find out which button that was, was to read raw tracebacks
        out of the journal and match line numbers against release tarballs --
        the traceback says *what* broke, never *where* in terms the access log
        can be joined to. This pins the one line that answers it.
        """
        with mock.patch.object(web, "dispatch", side_effect=RuntimeError("boom")):
            with self.assertLogs(level="ERROR") as captured:
                status, _, _ = self.client.get("/api/admin/agent")
        self.assertEqual(status, 500, "对外仍然是笼统的 500，不泄露内部信息")
        self.assertTrue(
            any("unhandled RuntimeError on GET /api/admin/agent" in line
                for line in captured.output),
            captured.output)

    def test_the_error_line_carries_no_user_supplied_text(self):
        """It goes to the log, so it names the path and the class -- nothing else."""
        with mock.patch.object(web, "dispatch", side_effect=RuntimeError("secret-canary")):
            with self.assertLogs(level="ERROR") as captured:
                self.client.get("/api/admin/agent?q=another-canary")
        joined = "\n".join(captured.output)
        self.assertNotIn("secret-canary", joined, "异常消息可能带用户数据，不能进日志")
        self.assertNotIn("another-canary", joined, "查询串也不进日志")


if __name__ == "__main__":
    unittest.main()


class WebServiceNameTests(unittest.TestCase):
    """`web.service` 是那个**惰性的 Service 单例**，不是一个模块。

    `pilot_app/web.py` 末尾的模块级 `__getattr__` 就是为它写的（那里的注释：
    「Keep ``from pilot_app.web import db, service`` working lazily」），而且不止
    测试这么用。v0.63.28 加接口时顺手写了 `from . import service`，
    **这个名字当场被模块顶掉了**——`service.secrets`（实例属性）从此
    AttributeError，而报错落在别的测试模块里，看起来像那些模块坏了。
    **一个名字两种含义**是这个项目已经栽过的那类坑（`is_admin` 一次），
    所以这里钉住两件事：名字仍然是那个代理，且模块不许再被裸导入。
    """

    def test_the_name_is_the_lazy_service_not_a_module(self):
        import types
        from pilot_app import web as web_module

        self.assertNotIsInstance(web_module.service, types.ModuleType,
                                 "web.service 被模块顶掉了——用 from . import service as service_mod")
        # 它是实例（惰性代理在取属性时才构造），至少有 Service 的方法。
        self.assertTrue(hasattr(web_module.service, "process_message"))

    def test_the_module_is_imported_under_an_alias(self):
        source = pathlib.Path(web_module_path()).read_text(encoding="utf-8")
        self.assertNotIn("\nfrom . import service\n", source,
                         "裸导入会把 web.service 这个单例名字顶掉；请用 as service_mod")
        self.assertIn("from . import service as service_mod", source)


def web_module_path() -> str:
    from pilot_app import web as web_module

    return web_module.__file__
