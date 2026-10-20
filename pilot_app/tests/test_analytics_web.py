"""The visit counter, exercised through real HTTP.

The unit tests prove the parts; this proves the *wiring*, which is where this
project has repeatedly been bitten: the handler has to read the address from the
trusted proxy header, decide what is a page view, hand it to analytics, and the
console has to be able to read it back -- and an ordinary user must not be able
to. A function-level test cannot see any of that, and the bug that made every
visitor look like 127.0.0.1 lived exactly here.
"""

from __future__ import annotations

import datetime as dt
import http.cookiejar
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

_TMP = tempfile.mkdtemp()
os.environ["INFE_PILOT_DB"] = _TMP + "/analytics-web.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
os.environ["INFE_PILOT_MAX_USERS"] = "50"
os.environ["INFE_PILOT_ADMIN_EMAILS"] = "boss@example.com"
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import analytics as analytics_mod  # noqa: E402

BROWSER = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
from pilot_app import web  # noqa: E402
from pilot_app.tests import admin_fixture  # noqa: E402
from pilot_app.web import db  # noqa: E402


class Client:
    def __init__(self, base: str) -> None:
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), 
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, method: str, path: str, payload=None, headers=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read().decode()
                return response.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw)
        except urllib.error.HTTPError as error:
            raw = error.read().decode()
            try:
                return error.code, json.loads(raw or "{}")
            except json.JSONDecodeError:
                return error.code, {"detail": raw}

    def get(self, path, headers=None):
        return self.request("GET", path, headers=headers)

    def post(self, path, payload=None):
        return self.request("POST", path, payload)


class AdminAnalyticsTests(unittest.TestCase):
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
        analytics_mod.forget_recent()
        db.initialize()
        with db.connect() as connection:
            for table in ("page_views", "announcement_deliveries", "announcement_dismissals",
                          "announcements", "feedback", "reports", "messages", "mailboxes",
                          "connections", "sessions", "invites", "profiles", "users"):
                connection.execute(f"DELETE FROM {table}")
        self.stamp = dt.datetime.now().timestamp()

    def _admin(self) -> Client:
        """保留地址不能走开放注册（见 admin_fixture）：建号 + 授权 + 登录。"""
        return admin_fixture.admin_session(db, Client(self.base), "boss@example.com")

    def _stored_rows(self) -> list[dict]:
        with db.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM page_views")]

    def test_a_visit_over_http_is_counted_without_its_address(self):
        # The visitor arrives through the real handler, from behind the proxy
        # that sets X-Real-IP.
        visitor = Client(self.base)
        status, _ = visitor.get("/", headers={"X-Real-IP": "8.8.8.8", "User-Agent":
                                              "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                                              "AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1"})
        self.assertEqual(status, 200)
        rows = self._stored_rows()
        self.assertEqual(len(rows), 1, "首页的一次访问应当被记下来")
        self.assertEqual(rows[0]["path"], "/")
        self.assertEqual(rows[0]["bot"], 0)
        self.assertEqual(rows[0]["client_hash"], web.get_service().secrets.anonymized("8.8.8.8"))
        blob = " ".join(str(value) for value in rows[0].values())
        self.assertNotIn("8.8.8.8", blob, "地址只能以摘要形式入库")

    def test_the_console_reports_it_and_the_live_view_has_the_address(self):
        admin = self._admin()
        # A browser user agent on purpose: urllib's own string is
        # "Python-urllib/3.x", which the classifier correctly calls a robot.
        Client(self.base).get("/demo", headers={"X-Real-IP": "8.8.8.9", "User-Agent": BROWSER})
        status, body = admin.get("/api/admin/analytics?days=7")
        self.assertEqual(status, 200, body)
        for key in ("today", "totals", "daily", "paths", "referrers", "countries",
                    "cities", "recent", "geo", "timezone"):
            self.assertIn(key, body)
        self.assertEqual(body["totals"]["human_pv"], 1)
        self.assertIn("/demo", [row["label"] for row in body["paths"]])
        # The address the operator asked to see is here -- from memory.
        self.assertEqual(body["recent"][0]["ip"], "8.8.8.9")
        self.assertEqual(body["geo"]["retention_days"], analytics_mod.retention_days())

    def test_api_and_asset_traffic_is_not_a_visit(self) -> None:
        self._admin()
        Client(self.base).get("/api/me")
        Client(self.base).get("/static/app.js")
        Client(self.base).get("/health")
        self.assertEqual(self._stored_rows(), [], "只应统计页面访问")

    def test_a_global_privacy_control_is_honoured(self) -> None:
        Client(self.base).get("/", headers={"X-Real-IP": "8.8.8.9", "Sec-GPC": "1"})
        Client(self.base).get("/", headers={"X-Real-IP": "8.8.8.9", "DNT": "1"})
        self.assertEqual(self._stored_rows(), [], "DNT/Sec-GPC 的那次访问必须完全不记")

    def test_robots_are_kept_out_of_the_human_number(self) -> None:
        Client(self.base).get("/", headers={"X-Real-IP": "8.8.8.9", "User-Agent": "odin-scanner/0.4"})
        admin = self._admin()
        status, body = admin.get("/api/admin/analytics?days=7")
        self.assertEqual(status, 200)
        self.assertEqual(body["totals"]["human_pv"], 0)
        self.assertEqual(body["totals"]["bot_pv"], 1)

    def test_ordinary_users_and_anonymous_visitors_are_refused(self):
        invite = db.create_invite(f"member-{self.stamp}", 1)
        member = Client(self.base)
        web.reset_signup_rate_limit()  # 见 web.reset_signup_rate_limit：限速按 IP，单测得自己清
        member.post("/api/auth/register", {
            "email": "member@example.com", "password": "a-long-enough-password",
            "invite_code": invite, "accepted_terms": True})
        status, _ = member.get("/api/admin/analytics")
        self.assertEqual(status, 404, "非管理员必须 404，不能让这个接口看起来存在")
        status, _ = Client(self.base).get("/api/admin/analytics")
        self.assertEqual(status, 401)

    def test_the_operator_is_not_counted_as_a_visitor(self):
        """管理员自己浏览不算访客，而且他能一键删掉自己留下的记录。"""
        admin = self._admin()
        # 先以管理员身份看一次自己的首页（带会话 → 会被标记成运营者）。
        status, _ = admin.get("/", headers={"X-Real-IP": "192.0.2.7", "User-Agent": BROWSER})
        self.assertEqual(status, 200)
        _, body = admin.get("/api/admin/analytics")
        self.assertEqual(body["totals"]["human_pv"], 0, "运营者那次不该进人数")
        # 真访客（没有会话）来一次，就该被算进去。
        Client(self.base).get("/", headers={"X-Real-IP": "8.8.8.8", "User-Agent": BROWSER})
        _, body = admin.get("/api/admin/analytics")
        self.assertEqual(body["totals"]["human_pv"], 1)

        status, purged = admin.post("/api/admin/analytics/purge", {})
        self.assertEqual(status, 200, purged)
        self.assertGreaterEqual(purged["removed"], 1)
        _, after = admin.get("/api/admin/analytics")
        self.assertEqual(after["totals"]["human_pv"], 1, "真访客那条必须留着")
        with db.connect() as connection:
            left = [dict(row) for row in connection.execute("SELECT * FROM page_views")]
        self.assertTrue(all(row["admin"] == 0 for row in left), "运营者的行应当被删干净")

    def test_only_an_admin_can_purge(self):
        invite = db.create_invite(f"member-purge-{self.stamp}", 1)
        member = Client(self.base)
        web.reset_signup_rate_limit()  # 见 web.reset_signup_rate_limit：限速按 IP，单测得自己清
        member.post("/api/auth/register", {
            "email": "member@example.com", "password": "a-long-enough-password",
            "invite_code": invite, "accepted_terms": True})
        self.assertEqual(member.post("/api/admin/analytics/purge", {})[0], 404)
        self.assertEqual(Client(self.base).post("/api/admin/analytics/purge", {})[0], 401)

    def test_the_endpoint_never_returns_another_visitors_identity(self):
        # The digest is the only link between two visits, and it is not in the
        # response: the console shows counts and the in-memory live list, not a
        # per-visitor history that could be joined against anything.
        admin = self._admin()
        Client(self.base).get("/", headers={"X-Real-IP": "8.8.8.9"})
        _, body = admin.get("/api/admin/analytics")
        self.assertNotIn("client_hash", json.dumps(body))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
