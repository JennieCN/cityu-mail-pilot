"""Tests for the public message board.

The board is the **second** unauthenticated write in this API, and a write that
nobody is logged in for is the kind that quietly grows capabilities. So the
tests are mostly about what it cannot do: it cannot create an account, cannot
mint an invite, cannot read anything back, and cannot put a single character on
the public page without a person deciding to.

The other half is the moderation contract -- pending by default, the optional
address never published, and the visitor's text escaped on the one path where it
becomes HTML.
"""

import datetime as dt
import http.cookiejar
import json
import os
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["INFE_PILOT_DB"] = _TMP + "/guestbook.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
os.environ["INFE_PILOT_MAX_USERS"] = "50"
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import web  # noqa: E402
from pilot_app.security import token_hash  # noqa: E402
from pilot_app.web import db  # noqa: E402


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
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self.jar))

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

    def raw(self, path: str) -> str:
        with self.opener.open(self.base + path, timeout=20) as response:
            return response.read().decode("utf-8")


class GuestbookTests(unittest.TestCase):
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
        # The throttle is process-global on purpose (it is keyed by client, and
        # the tests all arrive from 127.0.0.1), so each test starts with a clean
        # budget instead of inheriting the previous test's spending.
        web._guestbook_attempts.clear()
        self.client = Client(self.base)
        self.admin = None
        # The whole suite shares one database (that is how this project's tests
        # have always worked), and a published message from the previous test
        # would still be on the landing page. Clearing the board keeps each test
        # about one thing.
        with db.connect() as connection:
            connection.execute("DELETE FROM guest_messages")

    # -- helpers ----------------------------------------------------------

    def post_message(self, body: str = "这个工具挺好用的。", **extra):
        payload = {"body": body, "elapsed_ms": 5000}
        payload.update(extra)
        # The operator notification opens a real SMTP connection; a test must not.
        with mock.patch.object(web, "_notify_new_guest_message", return_value=None):
            return self.client.post("/api/guestbook", payload)

    def make_admin(self) -> Client:
        stamp = dt.datetime.now().timestamp()
        code = f"gb-admin-{stamp}"
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with db.connect() as connection:
            connection.execute("INSERT INTO invites(code_hash,expires_at) VALUES(?,?)",
                               (token_hash(code), expiry))
        client = Client(self.base)
        web.reset_signup_rate_limit()  # 见 web.reset_signup_rate_limit：限速按 IP，单测得自己清
        status, body, _ = client.post("/api/auth/register", {
            "email": f"gb-admin-{stamp}@example.com", "password": "a-long-enough-password",
            "invite_code": code, "accepted_terms": True})
        self.assertEqual(status, 200, body)
        with db.connect() as connection:
            connection.execute("UPDATE users SET is_admin=1 WHERE id=?", (body["id"],))
        return client

    def stored(self) -> list[dict]:
        """Every message, newest first -- so ``[0]`` is the one just posted."""
        return db.guest_messages()

    def latest(self) -> dict:
        rows = self.stored()
        self.assertTrue(rows, "刚刚应该存下了一条留言")
        return rows[0]

    # -- it cannot do anything except store one message -------------------

    def test_a_message_creates_no_account_and_no_invite(self):
        """The application form's test, copied: this endpoint is unauthenticated.

        Anything that can be reached without logging in has to be proved
        incapable of granting access, not merely documented as such.
        """
        with db.connect() as connection:
            users_before = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            invites_before = connection.execute("SELECT COUNT(*) FROM invites").fetchone()[0]
        status, body, _ = self.post_message("给我也开一个账号吧", nickname="路人")
        self.assertEqual(status, 200, body)
        with db.connect() as connection:
            users_after = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            invites_after = connection.execute("SELECT COUNT(*) FROM invites").fetchone()[0]
        self.assertEqual(users_after, users_before, "一条留言不该建号")
        self.assertEqual(invites_after, invites_before, "一条留言不该发码")

    def test_the_reply_never_reads_anything_back(self):
        status, body, _ = self.post_message("悄悄写一句")
        self.assertEqual(status, 200, body)
        self.assertEqual(set(body), {"ok"}, "未认证的接口不该回吐任何内容")
        row = self.latest()
        self.assertNotIn(row["id"], json.dumps(body))

    def test_an_empty_message_is_refused(self):
        status, body, _ = self.post_message("   ")
        self.assertEqual(status, 422, body)

    def test_the_body_is_refused_not_truncated_when_it_is_too_long(self):
        """Truncating would publish half a sentence without telling the author."""
        status, body, _ = self.post_message("字" * 900)
        self.assertEqual(status, 422, body)
        self.assertEqual(self.stored(), [])

    def test_a_message_that_is_mostly_links_is_refused(self):
        status, body, _ = self.post_message("看看 http://a.example 和 http://b.example 和 www.c.example")
        self.assertEqual(status, 422, body)
        self.assertEqual(self.stored(), [])

    def test_two_links_are_still_fine(self):
        status, body, _ = self.post_message("两个链接可以：http://a.example 与 https://b.example")
        self.assertEqual(status, 200, body)

    # -- anti-abuse -------------------------------------------------------

    def test_the_honeypot_is_answered_and_dropped(self):
        """Answering "ok" is the point: a rejection teaches a robot which field
        to skip next time."""
        status, body, _ = self.post_message("买点什么东西", website="http://spam.example")
        self.assertEqual(status, 200, body)
        self.assertEqual(self.stored(), [], "蜜罐填了就不能入库")

    def test_a_submit_that_is_too_fast_is_refused(self):
        status, body, _ = self.post_message("秒填", elapsed_ms=200)
        self.assertEqual(status, 422, body)
        self.assertEqual(self.stored(), [])

    def test_the_client_address_is_not_stored_in_the_clear(self):
        self.post_message("查一下存了什么")
        row = self.latest()
        self.assertNotIn("127.0.0.1", repr(row), "IP 不该以原文落库")
        self.assertTrue(row["client_hash"], "但要有东西能认出同一个客户端")

    def test_flooding_is_throttled(self):
        for index in range(web.GUESTBOOK_RATE_LIMIT):
            status, body, _ = self.post_message(f"第 {index} 条")
            self.assertEqual(status, 200, body)
        status, body, _ = self.post_message("第六条")
        self.assertEqual(status, 429, body)

    # -- moderation -------------------------------------------------------

    def test_nothing_is_public_until_a_person_publishes_it(self):
        self.post_message("先别公开这一句", nickname="小林")
        page = self.client.raw("/")
        self.assertNotIn("先别公开这一句", page)
        self.assertIn("还没有公开的留言", page)

    def test_publishing_puts_it_on_the_landing_page(self):
        self.post_message("已经可以公开了", nickname="小林")
        admin = self.make_admin()
        message_id = self.latest()["id"]
        status, body, _ = admin.put(f"/api/admin/guestbook/{message_id}", {"status": "published"})
        self.assertEqual(status, 200, body)
        page = Client(self.base).raw("/")
        self.assertIn("已经可以公开了", page)
        self.assertIn("小林", page)

    def test_the_visitor_text_is_escaped_on_the_public_page(self):
        """The one path where a stranger's words become HTML."""
        payload = '<script>alert(1)</script> & <img src=x onerror=alert(2)>'
        self.post_message(payload)
        admin = self.make_admin()
        admin.put(f"/api/admin/guestbook/{self.latest()['id']}", {"status": "published"})
        page = Client(self.base).raw("/")
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;", page)

    def test_the_optional_address_is_never_published(self):
        self.post_message("留了邮箱", email="someone@example.com")
        admin = self.make_admin()
        admin.put(f"/api/admin/guestbook/{self.latest()['id']}", {"status": "published"})
        page = Client(self.base).raw("/")
        self.assertNotIn("someone@example.com", page)

    def test_the_operator_can_see_the_optional_address(self):
        self.post_message("留了邮箱", email="someone@example.com")
        admin = self.make_admin()
        status, body, _ = admin.get("/api/admin/guestbook")
        self.assertEqual(status, 200, body)
        row = [item for item in body["messages"] if item["body"] == "留了邮箱"][0]
        self.assertEqual(row["email"], "someone@example.com")

    def test_unpublishing_takes_it_off_the_page_again(self):
        self.post_message("一会儿就撤")
        admin = self.make_admin()
        message_id = self.latest()["id"]
        admin.put(f"/api/admin/guestbook/{message_id}", {"status": "published"})
        self.assertIn("一会儿就撤", Client(self.base).raw("/"))
        admin.put(f"/api/admin/guestbook/{message_id}", {"status": "pending"})
        self.assertNotIn("一会儿就撤", Client(self.base).raw("/"))

    def test_deleting_really_removes_the_row(self):
        self.post_message("删掉吧")
        admin = self.make_admin()
        message_id = self.latest()["id"]
        status, body, _ = admin.put(f"/api/admin/guestbook/{message_id}", {"status": "deleted"})
        self.assertEqual(status, 200, body)
        self.assertNotIn(message_id, [row["id"] for row in self.stored()])

    def test_the_audit_line_records_the_decision_and_not_the_text(self):
        self.post_message("审计里不该出现这句话")
        admin = self.make_admin()
        message_id = self.latest()["id"]
        admin.put(f"/api/admin/guestbook/{message_id}", {"status": "published"})
        with db.connect() as connection:
            rows = connection.execute(
                "SELECT action,detail FROM audit_log WHERE action LIKE 'guest_message%'").fetchall()
        self.assertTrue(rows)
        self.assertNotIn("审计里不该出现这句话", " ".join(str(row["detail"]) for row in rows))

    def test_an_unknown_status_is_refused(self):
        self.post_message("改个奇怪的状态")
        admin = self.make_admin()
        message_id = self.latest()["id"]
        status, body, _ = admin.put(f"/api/admin/guestbook/{message_id}", {"status": "featured"})
        self.assertEqual(status, 422, body)

    def test_a_missing_message_is_a_404_not_a_crash(self):
        admin = self.make_admin()
        status, body, _ = admin.put("/api/admin/guestbook/msg_nope", {"status": "published"})
        self.assertEqual(status, 404, body)

    def test_the_database_refuses_an_unencrypted_address(self):
        """The column holds ciphertext; a plaintext write is a programming error.

        The first version of this method did `str(email)` on the encrypted blob,
        which stored the *repr* of the bytes and produced a row nothing could
        decrypt. A type check is what makes that impossible rather than unlikely.
        """
        with self.assertRaises(ValueError):
            db.create_guest_message(body="明文邮箱", sealed_email="someone@example.com")

    # -- boundaries -------------------------------------------------------

    def test_the_moderation_list_is_not_reachable_without_being_an_admin(self):
        # 未登录是 401；404 留给「登录了但不是管理员」——这条边界在本项目
        # 是刻意区分的，测试跟着区分，免得以后有人把它统一成 403。
        anonymous = Client(self.base)
        self.assertEqual(anonymous.get("/api/admin/guestbook")[0], 401)

        member = Client(self.base)
        stamp = dt.datetime.now().timestamp()
        code = f"gb-member-{stamp}"
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with db.connect() as connection:
            connection.execute("INSERT INTO invites(code_hash,expires_at) VALUES(?,?)",
                               (token_hash(code), expiry))
        web.reset_signup_rate_limit()  # 见 web.reset_signup_rate_limit：限速按 IP，单测得自己清
        status, body, _ = member.post("/api/auth/register", {
            "email": f"gb-member-{stamp}@example.com", "password": "a-long-enough-password",
            "invite_code": code, "accepted_terms": True})
        self.assertEqual(status, 200, body)
        self.assertEqual(member.get("/api/admin/guestbook")[0], 404, "非管理员一律 404")
        self.assertEqual(member.put("/api/admin/guestbook/msg_x", {"status": "published"})[0], 404)

    def test_the_form_is_on_the_landing_page_and_not_in_the_app(self):
        page = Client(self.base).raw("/")
        self.assertIn('action="/api/guestbook"', page)
        self.assertIn('id="guestbook-website"', page, "蜜罐字段必须在页面上，否则它拦不到任何东西")
        app = Client(self.base).raw("/app")
        self.assertNotIn('action="/api/guestbook"', app, "留言板在官网，不在应用里")

    def test_the_privacy_policy_discloses_the_board(self):
        page = Client(self.base).raw("/privacy")
        for phrase in ("留言", "IP", "永不刊登"):
            self.assertIn(phrase, page, f"隐私政策里少了「{phrase}」")


if __name__ == "__main__":
    unittest.main()
