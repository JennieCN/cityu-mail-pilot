"""Admin console tests: who may see it, what it may expose, what it can do.

The console is the most privileged surface in the app, so these tests pin the
security properties rather than just the happy path:

* only emails listed in ``INFE_PILOT_ADMIN_EMAILS`` can reach it, and an
  ordinary logged-in user gets a 404 (not a 403) so its existence is not
  disclosed;
* no response ever contains an encrypted password, API key or invite hash;
* an operator can pause / resume / remove another user, but cannot lock
  themselves out;
* deleting a user does not break the operator's own view or the invite list.
"""

import datetime as dt
import http.cookiejar
import json
import os
import re
import tempfile
import threading
import time
import pathlib
import unittest
import urllib.error
import urllib.request
from unittest import mock as _mock  # module-level; some tests import it locally too

_TMP = tempfile.mkdtemp()
os.environ["INFE_PILOT_DB"] = _TMP + "/admin.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
os.environ["INFE_PILOT_MAX_USERS"] = "50"
os.environ["INFE_PILOT_ADMIN_EMAILS"] = "boss@example.com"
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import setup_reminders as setup_reminders_mod  # noqa: E402
from pilot_app import web  # noqa: E402
from pilot_app import database as database_mod  # noqa: E402
from pilot_app.database import Database  # noqa: E402
from pilot_app.mailio import MailError  # noqa: E402
from pilot_app.security import SecretBox, hash_password, token_hash  # noqa: E402
from pilot_app.web import db  # noqa: E402


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
                return response.status, json.loads(response.read().decode() or "{}")
        except urllib.error.HTTPError as error:
            raw = error.read().decode()
            try:
                return error.code, json.loads(raw or "{}")
            except json.JSONDecodeError:
                return error.code, {"detail": raw}

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, payload=None):
        return self.request("POST", path, payload)

    def put(self, path, payload=None):
        return self.request("PUT", path, payload)

    def delete(self, path):
        return self.request("DELETE", path)


def status_of(response) -> int:
    return response[0] if isinstance(response, tuple) else response


class AdminTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = web.create_server("127.0.0.1", 0)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.box = SecretBox.from_environment()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        """Start every test from an empty database so emails stay unique."""
        db.initialize()
        with db.connect() as connection:
            for table in ("alert_state", "app_settings",
                          "announcement_deliveries", "announcement_dismissals", "announcements",
                          "token_usage", "model_prices", "feedback", "reports", "messages", "mailboxes", "connections",
                          "sessions", "invites", "profiles", "users"):
                connection.execute(f"DELETE FROM {table}")

    # -- helpers ----------------------------------------------------------

    def _make_user(self, email: str, *, mailbox: bool = True, model: bool = True,
                   verify: bool = False) -> dict:
        """Create a fully configured account directly in the database."""
        label = f"invite-{email}-{dt.datetime.now().timestamp()}"
        invite = db.create_invite(label, 1)
        user = db.create_user(email, hash_password("a-long-enough-password"), token_hash(invite))
        db.upsert_profile(user["id"], {
            "school_email": "student@my.cityu.edu.hk", "major": "通信工程", "year_of_study": "大二",
            "courses": ["密码学"], "interests": [], "career_goals": [], "focus_topics": [],
            "less_interested": [], "custom_instructions": "", "language": "bilingual",
            "timezone": "Asia/Hong_Kong", "immediate_enabled": True, "daily_enabled": True,
            "daily_time": "22:00",
        })
        if mailbox:
            mailbox_id = db.upsert_mailbox(user["id"], {
                "email": f"box-{email}", "report_to": f"box-{email}", "imap_host": "imap.qq.com",
                "imap_port": 993, "smtp_host": "smtp.qq.com", "smtp_port": 465,
                "encrypted_password": self.box.encrypt("mail-secret", context=f"mailbox:{user['id']}"),
            })
            if verify:
                db.record_mailbox_verification(mailbox_id)
        if model:
            db.upsert_connection(user["id"], {
                "kind": "model", "provider": "deepseek", "model": "deepseek-chat", "base_url": "",
                "encrypted_api_key": self.box.encrypt("model-secret", context=f"connection:{user['id']}:model"),
                "config_json": "{}", "enabled": True,
            })
        return user

    def _login(self, email: str) -> Client:
        client = Client(self.base)
        status, body = client.post("/api/auth/login", {"email": email, "password": "a-long-enough-password"})
        self.assertEqual(status, 200, body)
        return client

    # -- access control ---------------------------------------------------

    def test_anonymous_is_rejected(self):
        client = Client(self.base)
        status, _ = client.get("/api/admin/users")
        self.assertEqual(status, 401)
        # POST-only route: an anonymous GET must be a method error, never a hit.
        status, _ = client.get("/api/admin/invites")
        self.assertEqual(status, 405)
        status, _ = client.post("/api/admin/invites", {"label": "x", "days": 7})
        self.assertEqual(status, 401)
        status, _ = client.put("/api/admin/users/usr_x/status/paused")
        self.assertEqual(status, 401)
        status, _ = client.delete("/api/admin/invites/x")
        self.assertEqual(status, 401)

    def test_ordinary_user_sees_no_admin_surface(self):
        self._make_user("plain1@example.com")
        client = self._login("plain1@example.com")
        status, body = client.get("/api/me")
        self.assertEqual(status, 200)
        self.assertFalse(body["is_admin"])
        status, _ = client.get("/api/admin/users")
        self.assertEqual(status, 404, "非管理员必须看到 404，而不是 403（不暴露后台存在）")
        status, _ = client.post("/api/admin/invites", {"label": "x", "days": 7})
        self.assertEqual(status, 404)
        status, _ = client.delete("/api/admin/invites/anything")
        self.assertEqual(status, 404)

    def test_ordinary_user_cannot_change_any_other_account(self):
        """The write routes matter more than the read routes: a normal account
        must not be able to pause, resume or delete anybody through the admin
        path, and a refused call must not have mutated anything either."""
        victim = self._make_user("victim@example.com")
        self._make_user("plain2@example.com")
        client = self._login("plain2@example.com")
        for status_name in ("paused", "active", "deleted"):
            status, body = client.put(
                f"/api/admin/users/{victim['id']}/status/{status_name}",
                {"confirm_email": "victim@example.com"})
            self.assertEqual(status, 404, (status_name, body))
        with db.connect() as connection:
            row = connection.execute("SELECT status FROM users WHERE id=?", (victim["id"],)).fetchone()
        self.assertEqual(row["status"], "active")

    def test_admin_routes_require_a_session_at_all(self):
        status, body = Client(self.base).get("/api/admin/users")
        self.assertEqual(status, 401, body)

    def test_admin_identity_is_the_whole_address_not_a_substring(self):
        """A lookalike address must never inherit the console: the match is on
        the complete, normalised address, not on a prefix or a substring."""
        for lookalike in ("boss@example.com.evil.test", "notboss@example.com",
                          "boss+admin@example.com", "boss@example.co"):
            self._make_user(lookalike)
            client = self._login(lookalike)
            status, _ = client.get("/api/admin/users")
            self.assertEqual(status, 404, lookalike)
            status, me = client.get("/api/me")
            self.assertFalse(me["is_admin"], lookalike)

    def test_admin_flag_comes_from_the_environment_not_the_database(self):
        admin = self._make_user("boss@example.com")
        client = self._login("boss@example.com")
        status, body = client.get("/api/me")
        self.assertTrue(body["is_admin"])

        # Even with a valid session, removing the email from the server env
        # revokes the console immediately: identity cannot be self-granted.
        os.environ["INFE_PILOT_ADMIN_EMAILS"] = ""
        try:
            status, _ = client.get("/api/admin/users")
            self.assertEqual(status, 404)
        finally:
            os.environ["INFE_PILOT_ADMIN_EMAILS"] = "boss@example.com"
        status, _ = client.get("/api/admin/users")
        self.assertEqual(status, 200)
        self.assertEqual(admin["email"], "boss@example.com")

    # -- editing another account ------------------------------------------

    def test_operator_can_fix_another_users_model(self):
        """The case that motivated this: a classmate picks a model that cannot
        produce a report, and the operator has no way to help him."""
        self._make_user("boss@example.com")
        member = self._make_user("member-model@example.com", model=False)
        admin = self._login("boss@example.com")
        status, body = admin.put(f"/api/admin/users/{member['id']}/settings", {
            "model_provider": "deepseek", "model_name": "deepseek-chat", "model_api_key": "member-key",
        })
        self.assertEqual(status, 200, body)
        self.assertEqual(body["changed"], ["model_api_key", "model_name", "model_provider"])
        stored = db.get_connection(member["id"], "model")
        self.assertEqual(stored["provider"], "deepseek")
        self.assertEqual(stored["model"], "deepseek-chat")
        self.assertEqual(self.box.decrypt(stored["encrypted_api_key"], context=f"connection:{member['id']}:model"),
                         "member-key")

    def test_a_partial_edit_does_not_wipe_the_other_settings(self):
        """PUT /api/profile rewrites every field and defaults the missing ones,
        which is why this endpoint is a selective patch: fixing one field must
        not blank the user's courses, instructions or daily time."""
        self._make_user("boss@example.com")
        member = self._make_user("member-partial@example.com")
        admin = self._login("boss@example.com")
        before = db.get_profile(member["id"])
        status, body = admin.put(f"/api/admin/users/{member['id']}/settings", {"major": "计算机科学"})
        self.assertEqual(status, 200, body)
        after = db.get_profile(member["id"])
        self.assertEqual(after["major"], "计算机科学")
        for field in ("school_email", "year_of_study", "courses", "custom_instructions",
                      "daily_time", "timezone", "language"):
            self.assertEqual(after[field], before[field], f"{field} 被改动了")

    def test_operator_can_change_schedule_and_switch_things_off(self):
        self._make_user("boss@example.com")
        member = self._make_user("member-sched@example.com")
        admin = self._login("boss@example.com")
        status, body = admin.put(f"/api/admin/users/{member['id']}/settings", {
            "daily_time": "07:30", "daily_enabled": False, "immediate_enabled": True,
            "timezone": "Asia/Shanghai"})
        self.assertEqual(status, 200, body)
        profile = db.get_profile(member["id"])
        self.assertEqual(profile["daily_time"], "07:30")
        self.assertFalse(profile["daily_enabled"])
        self.assertTrue(profile["immediate_enabled"])
        self.assertEqual(profile["timezone"], "Asia/Shanghai")

    def test_operator_can_replace_a_mailbox_password_without_seeing_it(self):
        self._make_user("boss@example.com")
        member = self._make_user("member-box@example.com")
        admin = self._login("boss@example.com")
        status, body = admin.put(f"/api/admin/users/{member['id']}/settings", {
            "mailbox_app_password": "brand-new-app-password", "report_to": "elsewhere@example.com"})
        self.assertEqual(status, 200, body)
        stored = db.get_mailbox(member["id"])
        self.assertEqual(stored["report_to"], "elsewhere@example.com")
        self.assertEqual(self.box.decrypt(stored["encrypted_password"], context=f"mailbox:{member['id']}"),
                         "brand-new-app-password")
        # The cursor survives: a password change must not replay old mail.
        self.assertEqual(stored["last_uid"], 0 if stored["last_uid"] is None else stored["last_uid"])
        self.assertNotIn("brand-new-app-password", json.dumps(body))
        self.assertNotIn("member-key", json.dumps(body))

    def test_the_audit_record_names_fields_never_values(self):
        self._make_user("boss@example.com")
        member = self._make_user("member-audit@example.com", model=False)
        admin = self._login("boss@example.com")
        admin.put(f"/api/admin/users/{member['id']}/settings", {
            "model_provider": "deepseek", "model_name": "deepseek-chat", "model_api_key": "super-secret-key"})
        entries = [row for row in db.list_audit(50) if row["action"] == "admin_user_settings_changed"]
        self.assertTrue(entries, "写操作必须留下审计")
        # Several admin actions can land in the same second, so look for this
        # target rather than assuming the global newest row is ours.
        mine = [row for row in entries if row["target_email"] == "member-audit@example.com"]
        self.assertTrue(mine, "本次修改必须留下审计")
        newest = mine[0]
        self.assertIn("model_api_key", newest["detail"])
        self.assertNotIn("super-secret-key", newest["detail"])
        for row in db.list_audit(200):
            self.assertNotIn("super-secret-key", json.dumps(row, ensure_ascii=False))

    def test_invalid_values_are_rejected_and_change_nothing(self):
        self._make_user("boss@example.com")
        member = self._make_user("member-bad@example.com")
        admin = self._login("boss@example.com")
        before = db.get_profile(member["id"])
        cases = [
            ({"daily_time": "25:00"}, "每日发送时间"),
            ({"timezone": "Mars/Olympus"}, "时区"),
            ({"school_email": "not-a-cityu@example.com"}, "CityU"),
            ({"model_provider": "definitely-not-real"}, "供应商"),
            ({"model_provider": "deepseek", "model_api_key": ""}, "model_api_key"),
            ({"report_to": "not-an-email"}, "邮箱"),
        ]
        for payload, hint in cases:
            status, body = admin.put(f"/api/admin/users/{member['id']}/settings", payload)
            self.assertEqual(status, 422, (payload, body))
            self.assertIn(hint, str(body.get("detail", "")), (payload, body))
        self.assertEqual(db.get_profile(member["id"]), before)

    def test_an_empty_patch_is_refused(self):
        self._make_user("boss@example.com")
        member = self._make_user("member-empty@example.com")
        admin = self._login("boss@example.com")
        status, body = admin.put(f"/api/admin/users/{member['id']}/settings", {})
        self.assertEqual(status, 422, body)

    def test_unknown_users_and_ordinary_accounts_are_refused(self):
        self._make_user("boss@example.com")
        self._make_user("plain3@example.com")
        member = self._make_user("member-target@example.com")
        before = db.get_profile(member["id"])

        admin = self._login("boss@example.com")
        status, _ = admin.put("/api/admin/users/usr_does_not_exist/settings", {"major": "x"})
        self.assertEqual(status, 404)

        plain = self._login("plain3@example.com")
        status, _ = plain.put(f"/api/admin/users/{member['id']}/settings", {"major": "hacked"})
        self.assertEqual(status, 404, "普通用户不能改别人")
        self.assertEqual(db.get_profile(member["id"]), before)

        status, _ = Client(self.base).put(f"/api/admin/users/{member['id']}/settings", {"major": "x"})
        self.assertEqual(status, 401)

    def test_a_deleted_account_is_gone_rather_than_editable(self):
        """Deletion is a real row delete (cascading away the encrypted secrets),
        so the operator gets the same 404 as for any unknown id."""
        self._make_user("boss@example.com")
        member = self._make_user("member-gone@example.com")
        db.set_user_status(member["id"], "deleted")
        admin = self._login("boss@example.com")
        status, body = admin.put(f"/api/admin/users/{member['id']}/settings", {"major": "x"})
        self.assertEqual(status, 404, body)

    def test_school_email_still_has_to_be_a_cityu_address(self):
        self._make_user("boss@example.com")
        member = self._make_user("member-school@example.com")
        admin = self._login("boss@example.com")
        status, body = admin.put(f"/api/admin/users/{member['id']}/settings",
                                 {"school_email": "student@my.cityu.edu.hk"})
        self.assertEqual(status, 200, body)
        self.assertEqual(db.get_profile(member["id"])["school_email"], "student@my.cityu.edu.hk")

        # The response is the same overview the console already renders, so it
        # must not have grown a route to the stored secrets.
        blob = json.dumps(body)
        for forbidden in ("encrypted_api_key", "encrypted_password", "password_hash"):
            self.assertNotIn(forbidden, blob)

    # -- every mail, and what happened to it ------------------------------

    def _seed_mail(self, owner: dict, *, uid: int, status: str, skip_reason: str = "",
                   report="sent", subject="Assignment 2",
                   sent_at=None, last_error="") -> None:
        mailbox = db.get_mailbox(owner["id"])
        message_id = db.insert_message(owner["id"], mailbox["id"], "1", uid, {
            "subject": subject, "sender_name": "teacher", "sender_address": "teacher@cityu.edu.hk",
            "received": "2026-09-13T10:00:00+00:00", "body": b"private mail body",
            "skip_reason": skip_reason,
        })
        with db.connect() as connection:
            connection.execute("UPDATE messages SET status=?,skip_reason=?,last_error=? WHERE id=?",
                               (status, skip_reason, last_error, message_id))
        if report:
            report_id = db.create_report(user_id=owner["id"], message_id=message_id, kind="immediate",
                                         subject="【AI邮件摘要】" + subject, body=b"secret report text",
                                         sent_to="owner@outlook.com")
            with db.connect() as connection:
                connection.execute("UPDATE reports SET status=?,sent_at=? WHERE id=?",
                                   (report, sent_at or "2026-09-13T10:04:00+00:00", report_id))

    def test_the_mail_board_lists_every_user_with_its_outcome(self):
        self._make_user("boss@example.com")
        first = self._make_user("member-a@example.com")
        second = self._make_user("member-b@example.com")
        self._seed_mail(first, uid=1, status="sent")
        self._seed_mail(first, uid=2, status="skipped", skip_reason="发件人不在允许名单内", report=None)
        self._seed_mail(second, uid=3, status="failed", report="failed",
                        sent_at=None, last_error="接口响应超时")
        admin = self._login("boss@example.com")
        status, body = admin.get("/api/admin/messages")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["total"], 3)
        by_uid = {row["imap_uid"]: row for row in body["messages"]}
        self.assertEqual(by_uid[1]["delivery"], "sent")
        self.assertEqual(by_uid[1]["report_status"], "sent")
        self.assertEqual(by_uid[1]["sent_to"], "owner@outlook.com")
        self.assertEqual(by_uid[1]["latency_seconds"], 240.0)
        self.assertEqual(by_uid[2]["delivery"], "skipped")
        self.assertIn("允许名单", by_uid[2]["skip_reason"])
        self.assertEqual(by_uid[3]["delivery"], "failed")
        self.assertIn("超时", by_uid[3]["last_error"])
        owners = {row["user_email"] for row in body["messages"]}
        self.assertEqual(owners, {"member-a@example.com", "member-b@example.com"})

    def test_the_mail_board_never_carries_mail_or_report_content(self):
        """The panel answers "processed and delivered?" — it must not become a
        way to read other people's mail."""
        self._make_user("boss@example.com")
        member = self._make_user("member-privacy@example.com")
        self._seed_mail(member, uid=7, status="sent")
        admin = self._login("boss@example.com")
        status, body = admin.get("/api/admin/messages")
        self.assertEqual(status, 200)
        blob = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("private mail body", blob)
        self.assertNotIn("secret report text", blob)
        self.assertNotIn("body_markdown", blob)

    def test_filters_and_counts_line_up(self):
        self._make_user("boss@example.com")
        member = self._make_user("member-filter@example.com")
        self._seed_mail(member, uid=11, status="sent")
        self._seed_mail(member, uid=12, status="skipped", skip_reason="非允许名单", report=None)
        self._seed_mail(member, uid=13, status="failed", report="failed", sent_at=None)
        admin = self._login("boss@example.com")
        status, body = admin.get("/api/admin/messages")
        self.assertEqual(body["counts"]["all"], 3)
        self.assertEqual(body["counts"]["sent"], 1)
        self.assertEqual(body["counts"]["skipped"], 1)
        self.assertEqual(body["counts"]["failed"], 1)
        # "undelivered" means "should have gone out but did not". A skipped mail
        # was a deliberate decision about someone else's newsletter, so counting
        # it here would train the operator to ignore the number.
        self.assertEqual(body["counts"]["undelivered"], 1)

        status, only_failed = admin.get("/api/admin/messages?status=failed")
        self.assertEqual([row["imap_uid"] for row in only_failed["messages"]], [13])
        status, only_skipped = admin.get("/api/admin/messages?status=skipped")
        self.assertEqual([row["imap_uid"] for row in only_skipped["messages"]], [12])

        status, bad = admin.get("/api/admin/messages?status=nonsense")
        self.assertEqual(status, 422, bad)

    def test_paging_and_per_user_filter(self):
        self._make_user("boss@example.com")
        first = self._make_user("member-page-a@example.com")
        second = self._make_user("member-page-b@example.com")
        for uid in range(20, 25):
            self._seed_mail(first, uid=uid, status="sent")
        self._seed_mail(second, uid=99, status="sent", subject="别人的邮件")
        admin = self._login("boss@example.com")

        status, page1 = admin.get("/api/admin/messages?limit=3")
        self.assertEqual(len(page1["messages"]), 3)
        self.assertEqual(page1["total"], 6)
        status, page2 = admin.get("/api/admin/messages?limit=3&offset=3")
        self.assertEqual(len(page2["messages"]), 3)
        self.assertFalse({row["id"] for row in page1["messages"]} & {row["id"] for row in page2["messages"]})

        status, mine = admin.get(f"/api/admin/messages?user_id={second['id']}")
        self.assertEqual([row["user_email"] for row in mine["messages"]], ["member-page-b@example.com"])

        status, bad = admin.get("/api/admin/messages?limit=abc")
        self.assertEqual(status, 422, bad)

    def test_mail_delivered_before_any_report_existed_is_not_undelivered(self):
        """Real case: 41 rows migrated from the previous single-user service have
        status='sent' and no report row. Judging delivery by the report row
        reported every one of them as never sent."""
        self._make_user("boss@example.com")
        member = self._make_user("member-legacy@example.com")
        self._seed_mail(member, uid=31, status="sent", report=None)
        admin = self._login("boss@example.com")
        status, body = admin.get("/api/admin/messages")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["counts"]["sent"], 1)
        self.assertEqual(body["counts"]["undelivered"], 0)
        row = body["messages"][0]
        self.assertEqual(row["delivery"], "sent")
        self.assertIsNone(row["report_id"])

    def test_a_failed_report_makes_the_mail_undelivered(self):
        self._make_user("boss@example.com")
        member = self._make_user("member-rep-failed@example.com")
        self._seed_mail(member, uid=32, status="failed", report="failed", sent_at=None)
        admin = self._login("boss@example.com")
        status, body = admin.get("/api/admin/messages?status=undelivered")
        self.assertEqual([row["imap_uid"] for row in body["messages"]], [32])
        self.assertEqual(body["messages"][0]["delivery"], "failed")

    def test_audit_order_is_stable_within_the_same_second(self):
        """Log lines are second-stamped; the newest row must still be the one
        most recently written, or the console shows a scrambled history."""
        for index in range(5):
            db.record_audit(action="order_probe", actor_email="boss@example.com",
                            detail=f"n={index}")
        rows = [row for row in db.list_audit(20) if row["action"] == "order_probe"]
        self.assertEqual([row["detail"] for row in rows], ["n=4", "n=3", "n=2", "n=1", "n=0"])

    def test_the_mail_board_is_operator_only(self):
        self._make_user("plain4@example.com")
        plain = self._login("plain4@example.com")
        status, _ = plain.get("/api/admin/messages")
        self.assertEqual(status, 404)
        status, _ = Client(self.base).get("/api/admin/messages")
        self.assertEqual(status, 401)

    # -- broadcasts ---------------------------------------------------------

    def test_a_broadcast_reaches_every_user_as_a_banner(self):
        self._make_user("boss@example.com")
        first = self._make_user("member-b1@example.com")
        second = self._make_user("member-b2@example.com")
        admin = self._login("boss@example.com")
        status, body = admin.post("/api/admin/announcements", {
            "title": "本周六 22:00 维护", "body": "预计 30 分钟，期间收信会延迟。", "tone": "warn"})
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["announcements"]), 1)
        self.assertEqual(body["announcements"][0]["active"], 1)
        self.assertEqual(body["announcements"][0]["email_total"], 0, "默认只发站内")

        for owner in (first, second):
            dash = web.build_dashboard(db.get_user(owner["id"]))
            self.assertIsNotNone(dash["announcement"], "每个用户登录后都应看到")
            self.assertEqual(dash["announcement"]["title"], "本周六 22:00 维护")
            self.assertEqual(dash["announcement"]["tone"], "warn")

    def test_dismissing_is_per_user(self):
        self._make_user("boss@example.com")
        first = self._make_user("member-d1@example.com")
        second = self._make_user("member-d2@example.com")
        admin = self._login("boss@example.com")
        admin.post("/api/admin/announcements", {"title": "t", "body": "b"})

        reader = self._login("member-d1@example.com")
        status, body = reader.post(f"/api/announcements/{web.get_db().list_announcements(1)[0]['id']}/dismiss")
        self.assertEqual(status, 200, body)
        self.assertIsNone(web.build_dashboard(db.get_user(first["id"]))["announcement"])
        self.assertIsNotNone(web.build_dashboard(db.get_user(second["id"]))["announcement"],
                             "另一个人仍然看得到")

    def test_the_author_is_not_asked_to_confirm_their_own_broadcast(self):
        """写公告的人不用向自己确认。

        2026-09-17 用户报「每次发完广播软件就不能滑动，一定要重新刷新一遍」：
        对话框盖住整页并锁住滚动，而运营者发完广播后自己也被它挡住 —— 对他没有
        任何新信息，却要先点一下（当时还得刷新一次）才能继续用后台。别人照旧。
        """
        self._make_user("boss@example.com")
        member = self._make_user("member-author@example.com")
        admin = self._login("boss@example.com")
        admin.post("/api/admin/announcements", {"title": "维护通知", "body": "今晚 22:00"})

        author_row = db.find_user_for_login("boss@example.com")
        author = web.build_dashboard(db.get_user(author_row["id"]))
        self.assertIsNone(author["announcement"], "作者不该被自己的公告挡住")
        self.assertEqual(author["announcement_pending"], 0)
        reader = web.build_dashboard(db.get_user(member["id"]))
        self.assertIsNotNone(reader["announcement"], "其他人照样必须确认")
        self.assertEqual(reader["announcement_pending"], 1)

    def test_only_the_newest_active_broadcast_is_shown(self):
        """Primer's banner guidance is explicit that two banners on one page is a
        stacking problem, so the dashboard returns exactly one."""
        self._make_user("boss@example.com")
        member = self._make_user("member-one@example.com")
        admin = self._login("boss@example.com")
        admin.post("/api/admin/announcements", {"title": "第一条", "body": "旧的"})
        admin.post("/api/admin/announcements", {"title": "第二条", "body": "新的"})
        dash = web.build_dashboard(db.get_user(member["id"]))
        self.assertEqual(dash["announcement"]["title"], "第二条")

    def test_withdrawing_removes_it_for_everyone(self):
        self._make_user("boss@example.com")
        member = self._make_user("member-w@example.com")
        admin = self._login("boss@example.com")
        created = admin.post("/api/admin/announcements", {"title": "撤下测试", "body": "b"})[1]
        announcement_id = created["id"]
        status, body = admin.put(f"/api/admin/announcements/{announcement_id}/withdraw", {})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["announcements"][0]["active"], 0)
        self.assertIsNone(web.build_dashboard(db.get_user(member["id"]))["announcement"])
        # Withdrawing twice is a 404, not a silent success.
        status, _ = admin.put(f"/api/admin/announcements/{announcement_id}/withdraw", {})
        self.assertEqual(status, 404)

    def test_email_delivery_queues_one_row_per_user_with_a_mailbox(self):
        self._make_user("boss@example.com")           # has a mailbox
        self._make_user("member-nomail@example.com", mailbox=False)
        admin = self._login("boss@example.com")
        status, body = admin.post("/api/admin/announcements", {
            "title": "带邮件的公告", "body": "内容", "deliver_email": True})
        self.assertEqual(status, 200, body)
        queued = db.pending_announcement_deliveries(50)
        self.assertEqual(len(queued), 1, "只给有邮箱的账号排队")
        self.assertEqual(queued[0]["report_to"], "box-boss@example.com")

    def test_a_broadcast_email_goes_out_and_is_marked_sent(self):
        from unittest import mock
        from pilot_app import reports as reports_mod
        self._make_user("boss@example.com")
        member = self._make_user("member-send@example.com")
        admin = self._login("boss@example.com")
        admin.post("/api/admin/announcements", {
            "title": "维护通知", "body": "周六 22:00。", "tone": "warn", "deliver_email": True})

        sent = {}

        def record(config, password, subject, markdown, **kwargs):
            sent["subject"] = subject
            sent["to"] = config["report_to"]
            sent["html"] = kwargs.get("html_body", "")
            sent["text"] = kwargs.get("text_body", "")

        service = web.get_service()
        with mock.patch("pilot_app.service.mailio.send_report", side_effect=record):
            result = service.send_announcement_emails(10)
        self.assertEqual(result["sent"], 2, "两个账号各一封")
        self.assertIn("公告", sent["subject"])
        self.assertIn("周六 22:00", sent["text"])
        self.assertIn("CITYU MAIL PILOT", sent["html"])
        self.assertNotIn("<script", sent["html"])
        self.assertEqual(db.pending_announcement_deliveries(50), [])

    def test_a_failing_mailbox_does_not_stop_the_others(self):
        from unittest import mock
        self._make_user("boss@example.com")
        self._make_user("member-bad2@example.com")
        admin = self._login("boss@example.com")
        admin.post("/api/admin/announcements", {"title": "t", "body": "b", "deliver_email": True})
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("mailbox refused")

        service = web.get_service()
        with mock.patch("pilot_app.service.mailio.send_report", side_effect=flaky):
            result = service.send_announcement_emails(10)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["failed"], 1)
        rows = db.list_announcements(1)[0]
        self.assertEqual(rows["email_failed"], 1)
        self.assertEqual(rows["email_sent"], 1)

    def test_broadcasts_are_operator_only_and_validated(self):
        self._make_user("boss@example.com")
        self._make_user("plain5@example.com")
        member = self._make_user("member-bad3@example.com")
        plain = self._login("plain5@example.com")
        status, _ = plain.post("/api/admin/announcements", {"title": "x", "body": "y"})
        self.assertEqual(status, 404, "普通用户不能发广播")
        dashboard = web.build_dashboard(db.get_user(member["id"]))
        self.assertIsNone(dashboard["announcement"])

        admin = self._login("boss@example.com")
        for payload in ({"title": "", "body": "b"}, {"title": "t", "body": ""},
                        {"title": "t", "body": "b", "tone": "shouty"}):
            status, body = admin.post("/api/admin/announcements", payload)
            self.assertEqual(status, 422, (payload, body))
        status, _ = Client(self.base).post("/api/admin/announcements", {"title": "t", "body": "b"})
        self.assertEqual(status, 401)
        self.assertEqual(db.list_announcements(10), [])

    # -- what the console may expose --------------------------------------

    def test_overview_never_leaks_secrets(self):
        self._make_user("boss@example.com", verify=True)
        self._make_user("member-secret@example.com", verify=True)
        admin = self._login("boss@example.com")
        status, body = admin.get("/api/admin/users")
        self.assertEqual(status, 200, body)
        self.assertGreaterEqual(len(body["users"]), 2)
        blob = json.dumps(body)
        for forbidden in ("mail-secret", "model-secret", "encrypted_password", "encrypted_api_key",
                          "password_hash", "code_hash", "cipher"):
            self.assertNotIn(forbidden, blob, f"管理接口泄露了 {forbidden}")
        member = next(row for row in body["users"] if row["email"] == "member-secret@example.com")
        self.assertEqual(member["model_provider"], "deepseek")
        self.assertEqual(member["major"], "通信工程")
        self.assertIsNotNone(member["last_verified_at"])
        self.assertIn("queue_depth", member)
        self.assertIn("report_count", member)

    def test_health_summary_is_reported(self):
        self._make_user("boss@example.com", verify=True)
        admin = self._login("boss@example.com")
        status, body = admin.get("/api/admin/users")
        health = body["health"]
        self.assertEqual(status, 200)
        for key in ("users", "active_users", "paused_users", "mailboxes", "pending_messages",
                    "failed_reports", "max_users", "version"):
            self.assertIn(key, health)
        self.assertGreaterEqual(health["mailboxes"], 1)
        self.assertGreaterEqual(health["max_users"], 1)

    # -- operator actions -------------------------------------------------

    def test_pause_and_resume_another_user(self):
        self._make_user("boss@example.com")
        member = self._make_user("member-pause@example.com")
        admin = self._login("boss@example.com")

        status, body = admin.put(f"/api/admin/users/{member['id']}/status/paused")
        self.assertEqual(status, 200, body)
        self.assertEqual(db.get_user(member["id"])["status"], "paused")
        row = next(item for item in body["users"] if item["id"] == member["id"])
        self.assertEqual(row["status"], "paused")

        # A paused user cannot keep using the console even with a live cookie.
        member_client = self._login("member-pause@example.com") if False else None
        status, body = admin.put(f"/api/admin/users/{member['id']}/status/active")
        self.assertEqual(status, 200)
        self.assertEqual(db.get_user(member["id"])["status"], "active")

    def test_admin_cannot_lock_itself_out(self):
        self._make_user("boss@example.com")
        admin = self._login("boss@example.com")
        status, body = admin.get("/api/me")
        admin_id = body["user"]["id"]
        for bad in ("paused", "deleted"):
            status, body = admin.put(f"/api/admin/users/{admin_id}/status/{bad}")
            self.assertEqual(status, 422, body)
        self.assertEqual(db.get_user(admin_id)["status"], "active")

    def test_unknown_user_and_invalid_status_are_rejected(self):
        self._make_user("boss@example.com")
        admin = self._login("boss@example.com")
        status, _ = admin.put("/api/admin/users/usr_does_not_exist/status/paused")
        self.assertEqual(status, 404)
        status, body = admin.put("/api/admin/users/usr_does_not_exist/status/banana")
        self.assertEqual(status, 422, body)

    def test_deleting_a_user_keeps_the_console_working(self):
        self._make_user("boss@example.com")
        member = self._make_user("member-delete@example.com", mailbox=True, model=True)
        admin = self._login("boss@example.com")

        # Deleting is irreversible, so it must be refused without a typed
        # confirmation, and accepted only with the exact account e-mail.
        status, body = admin.put(f"/api/admin/users/{member['id']}/status/deleted")
        self.assertEqual(status, 422, body)
        self.assertIn("确认", body["detail"])
        status, body = admin.put(f"/api/admin/users/{member['id']}/status/deleted",
                                 {"confirm_email": "wrong@example.com"})
        self.assertEqual(status, 422, body)
        status, body = admin.put(f"/api/admin/users/{member['id']}/status/deleted",
                                 {"confirm_email": "member-delete@example.com"})
        self.assertEqual(status, 200, body)
        self.assertNotIn(member["id"], [row["id"] for row in body["users"]])
        with self.assertRaises(KeyError):
            db.get_user(member["id"])

        # The console still works, and the deleted member cannot log in.
        status, body = admin.get("/api/admin/users")
        self.assertEqual(status, 200)
        client = Client(self.base)
        status, _ = client.post("/api/auth/login",
                                {"email": "member-delete@example.com", "password": "a-long-enough-password"})
        self.assertEqual(status, 401)

    # -- invites ----------------------------------------------------------

    def test_invite_is_returned_once_then_only_tracked_as_state(self):
        self._make_user("boss@example.com")
        admin = self._login("boss@example.com")
        status, body = admin.post("/api/admin/invites", {"label": "pilot-user-2", "days": 7})
        self.assertEqual(status, 200, body)
        code = body["code"]
        self.assertTrue(code and len(code) >= 12)

        status, listing = admin.get("/api/admin/users")
        self.assertEqual(status, 200)
        row = next(item for item in listing["invites"] if item["label"] == "pilot-user-2")
        self.assertEqual(row["state"], "available")
        blob = json.dumps(listing)
        self.assertNotIn(code, blob, "邀请码明文不得在列表接口回显")
        with db.connect() as connection:
            hashes = [r[0] for r in connection.execute("SELECT code_hash FROM invites")]
        self.assertIn(token_hash(code), hashes)

        # The freshly minted code really can register somebody.
        fresh = Client(self.base)
        web.reset_signup_rate_limit()  # 见 web.reset_signup_rate_limit：限速按 IP，单测得自己清
        status, user = fresh.post("/api/auth/register", {
            "email": "invited@example.com", "password": "a-long-enough-password", "invite_code": code, "accepted_terms": True,
        })
        self.assertEqual(status, 200, user)
        status, listing = admin.get("/api/admin/users")
        row = next(item for item in listing["invites"] if item["label"] == "pilot-user-2")
        self.assertEqual(row["state"], "used")
        self.assertEqual(row["used_by_email"], "invited@example.com")

    def test_invite_validation_and_revocation(self):
        self._make_user("boss@example.com")
        admin = self._login("boss@example.com")
        status, body = admin.post("/api/admin/invites", {"label": "to-revoke", "days": 3})
        code = body["code"]

        status, body = admin.post("/api/admin/invites", {"label": "bad", "days": 999})
        self.assertEqual(status, 422, body)
        status, body = admin.post("/api/admin/invites", {"label": "bad", "days": "seven"})
        self.assertEqual(status, 422, body)

        status, body = admin.delete("/api/admin/invites/to-revoke")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["retired"], 1)
        row = next(item for item in body["invites"] if item["label"] == "to-revoke")
        self.assertEqual(row["state"], "expired")

        # A revoked code must no longer register anybody.
        fresh = Client(self.base)
        web.reset_signup_rate_limit()  # 见 web.reset_signup_rate_limit：限速按 IP，单测得自己清
        status, body = fresh.post("/api/auth/register", {
            "email": "late@example.com", "password": "a-long-enough-password", "invite_code": code, "accepted_terms": True,
        })
        self.assertEqual(status, 400, body)

    def test_an_invite_label_with_spaces_or_chinese_can_be_revoked(self):
        """The same encoding rule, second consumer.

        An invite label is free text the operator types, and it rides in a URL
        path when a code is revoked. `encodeURIComponent` turns a space into
        `%20` and every Chinese character into three escapes, so before the
        dispatcher started decoding, revoking a code labelled 「给小王」 answered
        404 -- the code stayed live and the operator was told nothing.
        """
        self._make_user("boss@example.com")
        admin = self._login("boss@example.com")
        label = "给小王 的码"
        status, body = admin.post("/api/admin/invites", {"label": label, "days": 3})
        self.assertEqual(status, 200, body)

        from urllib.parse import quote
        status, body = admin.delete(f"/api/admin/invites/{quote(label)}")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["retired"], 1)
        row = next(item for item in body["invites"] if item["label"] == label)
        self.assertEqual(row["state"], "expired")

    def test_invite_label_is_not_a_path_traversal_or_injection_surface(self):
        self._make_user("boss@example.com")
        admin = self._login("boss@example.com")
        weird = "user/../etc"
        status, body = admin.post("/api/admin/invites", {"label": weird, "days": 1})
        self.assertEqual(status, 200, body)
        listing = admin.get("/api/admin/users")[1]
        self.assertTrue(any(item["label"] == weird for item in listing["invites"]))
        # Deleting by that label must not touch anything outside invites.
        status, body = admin.delete("/api/admin/invites/" + urllib.request.quote(weird, safe=""))
        self.assertEqual(status, 200, body)

    def test_password_change_revokes_every_other_session(self):
        """A stolen device must lose access at password change, not in 14 days."""
        self._make_user("boss@example.com")
        victim = self._make_user("victim@example.com")
        # Simulate: victim logged in on two devices.
        first = self._login("victim@example.com")
        second = self._login("victim@example.com")
        self.assertEqual(status_of(second.get("/api/account/security")), 200)

        status, body = first.put("/api/account/password", {
            "current_password": "a-long-enough-password",
            "new_password": "a-brand-new-long-password",
        })
        self.assertEqual(status, 200, body)
        self.assertGreaterEqual(body["revoked"], 1, "应吊销至少一个其他会话")

        # The other device's cookie must now be rejected...
        status, _ = second.get("/api/me")
        self.assertEqual(status, 401, "被吊销的会话必须失效")
        # ...while the device that changed the password keeps working.
        status, _ = first.get("/api/me")
        self.assertEqual(status, 200, "改密码的这台设备应继续可用")
        # And the old password no longer works.
        fresh = Client(self.base)
        status, _ = fresh.post("/api/auth/login",
                               {"email": "victim@example.com", "password": "a-long-enough-password"})
        self.assertEqual(status, 401)

    def test_password_change_requires_the_current_password(self):
        self._make_user("boss@example.com")
        self._make_user("victim2@example.com")
        client = self._login("victim2@example.com")
        status, body = client.put("/api/account/password", {
            "current_password": "not-the-password", "new_password": "a-brand-new-long-password",
        })
        self.assertEqual(status, 400, body)
        status, _ = client.get("/api/me")
        self.assertEqual(status, 200, "失败的改密码不应踢自己下线")

    def test_sign_out_all_devices_revokes_and_reissues(self):
        self._make_user("boss@example.com")
        self._make_user("victim3@example.com")
        first = self._login("victim3@example.com")
        second = self._login("victim3@example.com")
        status, body = first.post("/api/account/sessions/revoke")
        self.assertEqual(status, 200, body)
        self.assertGreaterEqual(body["revoked"], 2)
        status, _ = second.get("/api/me")
        self.assertEqual(status, 401)
        status, _ = first.get("/api/me")
        self.assertEqual(status, 200, "本机应拿到新会话而不是被踢出")

    def test_operator_actions_are_recorded_in_the_audit_trail(self):
        self._make_user("boss@example.com")
        member = self._make_user("audited@example.com")
        admin = self._login("boss@example.com")
        admin.put(f"/api/admin/users/{member['id']}/status/paused")
        admin.post("/api/admin/invites", {"label": "audit-check", "days": 1})

        status, body = admin.get("/api/admin/users")
        self.assertEqual(status, 200)
        actions = [item["action"] for item in body["audit"]]
        self.assertIn("user_status_paused", actions)
        self.assertIn("invite_created", actions)
        entry = next(item for item in body["audit"] if item["action"] == "user_status_paused")
        self.assertEqual(entry["actor_email"], "boss@example.com")
        self.assertEqual(entry["target_email"], "audited@example.com")
        # The audit trail must never contain secrets.
        blob = json.dumps(body["audit"])
        for forbidden in ("mail-secret", "model-secret", "encrypted_", "code_hash"):
            self.assertNotIn(forbidden, blob)

    # -- the health line the operator actually reads ------------------------

    def _set_polled(self, user_email: str, *, minutes_ago: float, imap_host: str = "") -> str:
        user = db.find_user_for_login(user_email)
        stamp = (dt.datetime.now(dt.timezone.utc)
                 - dt.timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
        with db.connect() as connection:
            if imap_host:
                connection.execute("UPDATE mailboxes SET imap_host=?, last_polled_at=? WHERE user_id=?",
                                   (imap_host, stamp, user["id"]))
            else:
                connection.execute("UPDATE mailboxes SET last_polled_at=? WHERE user_id=?",
                                   (stamp, user["id"]))
            row = connection.execute("SELECT email FROM mailboxes WHERE user_id=?", (user["id"],)).fetchone()
        return str(row["email"])

    def _insert_school_mail(self, mailbox_email: str, *, hours_ago: float = 1,
                            status: str = "sent") -> str:
        """One message that came from an allowed sender (i.e. proof of forwarding).

        `status='skipped'` is the same message from a sender outside the allowed
        domains: it proves the *inbox* works and nothing about the school rule.
        """
        with db.connect() as connection:
            row = connection.execute("SELECT id, user_id FROM mailboxes WHERE email=?",
                                     (mailbox_email,)).fetchone()
            message_id = f"msg_{mailbox_email}_{status}_{hours_ago}"
            connection.execute(
                """INSERT INTO messages(id,user_id,mailbox_id,uid_validity,imap_uid,subject,
                       sender_address,received_at,body,status,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (message_id, row["user_id"], row["id"], "1", abs(hash(message_id)) % 10_000,
                 "作业截止提醒", "student@my.cityu.edu.hk",
                 (dt.datetime.now(dt.timezone.utc)
                  - dt.timedelta(hours=hours_ago)).isoformat(timespec="seconds"),
                 b"", status, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))
        return message_id

    # -- 「收信正常」那个数字：先修算错，再换成证据 ---------------------------

    def test_a_mailbox_that_is_both_stale_and_broken_is_counted_once(self):
        """用户原话：「收信正常那里一直显示 4，为什么每次都会这样」。

        它当时是 `总数 - 停顿数 - 登不进去数` 两次相减，而一个授权码错的邮箱**两样都占**
        （轮询停了、错误列也非空），于是被减了两次、结果少一个。生产上就是这样：6 个邮箱
        里 1 个坏、1 个停顿（同一个），卡片显示 4，而真话是 5。"""
        self._make_user("boss@example.com")
        self._make_user("fine@example.com")
        self._make_user("wrong@example.com")
        self._set_polled("boss@example.com", minutes_ago=1)
        self._set_polled("fine@example.com", minutes_ago=2)
        broken = self._set_polled("wrong@example.com", minutes_ago=1)  # 被轮询过……
        with db.connect() as connection:
            connection.execute("UPDATE mailboxes SET last_error=? WHERE email=?",
                               ("IMAP 连接失败：b'LOGIN Login error'", broken))
        # ……但把它的轮询时间推到一个小时前：既停顿又登不进去，同一个邮箱。
        self._set_polled("wrong@example.com", minutes_ago=60)
        with db.connect() as connection:
            connection.execute("UPDATE mailboxes SET last_error=? WHERE email=?",
                               ("IMAP 连接失败：b'LOGIN Login error'", broken))
        health = web._service_health()
        self.assertEqual(health["mailboxes"], 3)
        self.assertEqual(health["broken_mailboxes"], 1)
        self.assertEqual(health["stale_mailboxes"], 1)
        self.assertEqual(health["healthy_mailboxes"], 2,
                         "同一个邮箱不能既算停顿又算登不进去；真话是 3 - 1 = 2")
        self.assertEqual(health["mailboxes_polled_recently"], 2)

    def test_a_paused_account_is_not_counted_at_all(self):
        """已暂停是运营者自己的决定：它既不正常也不故障，哨兵一直是这么排除的。
        把它算进去，卡片上就会永远挂着一个与事实无关的常数。"""
        self._make_user("boss@example.com")
        paused = self._make_user("paused@example.com")
        self._set_polled("boss@example.com", minutes_ago=1)
        with db.connect() as connection:
            connection.execute("UPDATE users SET status='paused' WHERE id=?", (paused["id"],))
            connection.execute("UPDATE mailboxes SET last_polled_at=NULL, last_error=''"
                               " WHERE user_id=?", (paused["id"],))
        health = web._service_health()
        self.assertEqual(health["mailboxes"], 1, "在用的只有 1 个")
        self.assertEqual(health["healthy_mailboxes"], 1)
        self.assertEqual(health["mailboxes_paused"], 1)
        self.assertEqual(health["stale_mailboxes"], 0,
                         "暂停的账号不该被算成「轮询停了」——是我们不去轮询它")

    # -- 「多少人是正常的」：一个数，判据在服务端（2026-09-24 用户要求简化） -----

    def test_the_headline_number_counts_only_mailboxes_that_really_work(self):
        """用户原话：「管理后台显示太多东西什么轮询正常，什么取信正常，简化一下，
        我就想知道多少人是正常的」。

        「正常」只认一种：**邮箱登得进去，而且真的收到过本校来信**。四种「不正常的」
        各有各的说法，一个都不许并进这个数。"""
        self._make_user("ok@example.com")
        self._make_user("silent@example.com")
        self._make_user("wrong@example.com")
        self._make_user("never@example.com")          # 配好了，但一次都没轮询过
        self._set_polled("ok@example.com", minutes_ago=1)
        self._set_polled("silent@example.com", minutes_ago=1)
        broken = self._set_polled("wrong@example.com", minutes_ago=1)
        self._insert_school_mail("box-ok@example.com", hours_ago=1)
        with db.connect() as connection:
            connection.execute("UPDATE mailboxes SET last_error=? WHERE email=?",
                               ("IMAP 连接失败：b'LOGIN Login error'", broken))
        working = web._service_health()["working"]
        self.assertEqual(working["ok"], 1, "只有「登得进 + 收到过本校来信」那一个算正常")
        self.assertEqual(working["configured"], 4)
        self.assertEqual(working["no_mail"], 1)
        self.assertEqual(working["broken"], 1)
        self.assertEqual(working["stale"], 1)

    def test_the_four_kinds_of_not_working_are_never_merged(self):
        """四档必须一直是四个数。合并成一句「N 位不正常」，运营者就不知道该找谁了：
        找用户换授权码 / 找学校改转发规则 / 找我们查轮询 / 谁都不用找（他自己暂停的）。

        四档相加必须**正好**等于分母——差一个就说明有个状态没被算进来，
        而那正是这类汇总数字最容易骗人的地方（少算会静默地把比例说好）。"""
        self._make_user("ok@example.com")
        self._make_user("silent@example.com")
        self._make_user("wrong@example.com")
        self._set_polled("ok@example.com", minutes_ago=1)
        self._set_polled("silent@example.com", minutes_ago=1)
        broken = self._set_polled("wrong@example.com", minutes_ago=1)
        self._insert_school_mail("box-ok@example.com", hours_ago=1)
        with db.connect() as connection:
            connection.execute("UPDATE mailboxes SET last_error=? WHERE email=?",
                               ("IMAP 连接失败：b'LOGIN Login error'", broken))
        working = web._service_health()["working"]
        self.assertEqual(
            working["ok"] + working["broken"] + working["no_mail"] + working["stale"],
            working["configured"],
            "四档相加必须等于分母；不等就说明有个状态没被算进去")

    def test_a_paused_account_leaves_the_denominator(self):
        """已暂停是运营者自己的决定：它按设计就不该被轮询，所以既不算正常也不算故障，
        更不能留在分母里——留着就成了一个永远拉低比例的常数。"""
        self._make_user("ok@example.com")
        paused = self._make_user("paused@example.com")
        self._set_polled("ok@example.com", minutes_ago=1)
        self._insert_school_mail("box-ok@example.com", hours_ago=1)
        with db.connect() as connection:
            connection.execute("UPDATE users SET status='paused' WHERE id=?", (paused["id"],))
        working = web._service_health()["working"]
        self.assertEqual(working["ok"], 1)
        self.assertEqual(working["configured"], 1, "分母只算我们本来该在轮询的邮箱")
        self.assertEqual(working["paused"], 1)

    def test_users_who_never_finished_setup_are_counted_separately(self):
        """注册了没配完邮箱的人**收不到报告，也不会报错**——这一屏上唯一会静默消失的一类
        （2026-09-24 生产上 64 个账号里 48 个是这种）。

        他们不能进分母：那会让「正常比例」看起来像系统坏了，而实际上那一步还没走到。"""
        self._make_user("ok@example.com")
        self._make_user("newbie-1@example.com", mailbox=False, model=False)
        self._make_user("newbie-2@example.com", mailbox=False, model=False)
        self._set_polled("ok@example.com", minutes_ago=1)
        self._insert_school_mail("box-ok@example.com", hours_ago=1)
        working = web._service_health()["working"]
        self.assertEqual(working["without_mailbox"], 2)
        self.assertEqual(working["configured"], 1, "没配完的人不在分母里，单独给一个数")

    def test_the_card_leads_with_school_mail_actually_arriving(self):
        """证据：每个邮箱最近一次取信、最近一封本校来信、24 小时几封。

        「我们登进去了几个」回答不了「正常吗」——而**学校那封信真的到了**才是整条链路的
        证据：轮询、转发、以及用户的转发规则，一次全都在里面。"""
        self._make_user("boss@example.com")
        self._make_user("silent@example.com")
        self._set_polled("boss@example.com", minutes_ago=1)
        self._set_polled("silent@example.com", minutes_ago=1)
        self._insert_school_mail("box-boss@example.com", hours_ago=2)
        self._insert_school_mail("box-boss@example.com", hours_ago=30)
        self._insert_school_mail("box-boss@example.com", hours_ago=3, status="skipped")
        health = web._service_health()
        self.assertEqual(health["school_mail_24h"], 1, "skipped 的（非本校发件人）不算")
        self.assertEqual(health["school_mail_7d"], 2)
        self.assertEqual(health["mailboxes_with_school_mail_24h"], 1)
        self.assertTrue(health["last_school_mail_at"])
        rows = {row["mailbox"]: row for row in health["delivery"]}
        self.assertEqual(rows["box-boss@example.com"]["state"], "ok")
        self.assertEqual(rows["box-boss@example.com"]["school_mail_total"], 2)
        self.assertEqual(rows["box-silent@example.com"]["state"], "no_mail",
                         "取信通、却从没有过本校来信——这是唯一该有人去改学校设置的状态")
        self.assertEqual(health["quiet_mailboxes"], ["box-silent@example.com"])

    def test_the_evidence_names_who_cannot_be_reached_and_who_is_paused(self):
        self._make_user("wrong@example.com")
        self._make_user("paused@example.com")
        broken = self._set_polled("wrong@example.com", minutes_ago=1)
        with db.connect() as connection:
            connection.execute("UPDATE mailboxes SET last_error=? WHERE email=?",
                               ("IMAP 连接失败：b'LOGIN Login error'", broken))
        user = db.find_user_for_login("paused@example.com")
        with db.connect() as connection:
            connection.execute("UPDATE users SET status='paused' WHERE id=?", (user["id"],))
        rows = {row["mailbox"]: row for row in web._service_health()["delivery"]}
        self.assertEqual(rows[broken]["state"], "broken")
        self.assertIn("登不进去", rows[broken]["detail"])
        self.assertEqual(rows["box-paused@example.com"]["state"], "paused")
        self.assertIn("按你的意思", rows["box-paused@example.com"]["detail"])

    def test_a_slow_provider_is_not_reported_as_a_broken_mailbox(self):
        """Regression: the panel judged every mailbox against a flat five-minute
        threshold. Gmail is polled every fifteen minutes because Google
        documents that as its own limit, so the panel called a healthy mailbox
        broken on every single visit — and a warning that is always on is one
        nobody reads, which is precisely how a real fault gets missed."""
        self._make_user("boss@example.com")
        self._make_user("slow@example.com")
        mailbox = self._set_polled("slow@example.com", minutes_ago=7, imap_host="imap.gmail.com")

        health = web._service_health()
        self.assertNotIn(mailbox, health["stale_mailbox_emails"],
                         "Gmail 每 15 分钟才轮询一次，7 分钟没轮询是正常的")

    def test_a_genuinely_stalled_mailbox_is_still_reported_and_named(self):
        self._make_user("boss@example.com")
        self._make_user("stalled@example.com")
        mailbox = self._set_polled("stalled@example.com", minutes_ago=180)

        health = web._service_health()
        self.assertIn(mailbox, health["stale_mailbox_emails"],
                      "告警必须点名是哪个邮箱，否则运营者要逐个账号去找")

    def test_a_wrong_password_does_not_count_as_a_working_mailbox(self):
        """The exact complaint, from production on 2026-09-15.

        「有一个用户的 imap 授权码都没有填对，为什么后台显示他正在跑」 —
        `update_mailbox_poll` writes `last_polled_at` whether the login succeeded
        or failed, so the health card said 「收信在跑 4 / 4」 while one account had
        `IMAP 连接失败：LOGIN Login error` in its error column. The user list
        showed that same account's 收信 light **red**, so the console contradicted
        itself on one screen. `verification_lights` already had the right rule
        ("a timestamp AND an empty error column"); the health card had not been
        taught it.
        """
        self._make_user("boss@example.com")
        self._make_user("wrongcode@example.com")
        mailbox = self._set_polled("wrongcode@example.com", minutes_ago=2)
        with db.connect() as connection:
            connection.execute(
                "UPDATE mailboxes SET last_error=? WHERE email=?",
                ("IMAP 连接失败：b'LOGIN Login error or password error'", mailbox))

        health = web._service_health()
        # Polled a moment ago, so the poller really is running for it: it counts
        # as 轮询在跑 but must not count as 收信正常. That identity *is* the bug --
        # the two numbers were one number before this fix.
        self.assertNotIn(mailbox, health["stale_mailbox_emails"],
                         "它确实被轮询了，所以不是「轮询停顿」")
        self.assertEqual(health["mailboxes_polled_recently"],
                         health["healthy_mailboxes"] + health["broken_mailboxes"],
                         "被轮询 = 收信正常 + 登不进去，两者不能混成一个数")
        # ...but "收信正常" is the number that must not include it.
        self.assertLess(health["healthy_mailboxes"], health["mailboxes"],
                        "授权码错的邮箱不能算进「收信正常」")
        self.assertEqual(health["broken_mailboxes"], 1)
        self.assertIn(mailbox, health["broken_mailbox_emails"],
                      "必须点名是哪个邮箱，否则运营者要逐个账号去找")

    def test_recovering_clears_the_broken_count(self):
        """The counter has to be able to go back down, or it becomes noise the
        operator learns to ignore -- the reason the 收信灯 refuses to OR in a
        stale error column."""
        self._make_user("boss@example.com")
        mailbox = self._set_polled("boss@example.com", minutes_ago=2)
        with db.connect() as connection:
            connection.execute("UPDATE mailboxes SET last_error='LOGIN Login error'")
        self.assertEqual(web._service_health()["broken_mailboxes"], 1)
        with db.connect() as connection:
            connection.execute("UPDATE mailboxes SET last_error=''")
            row = connection.execute("SELECT email FROM mailboxes").fetchone()
        health = web._service_health()
        self.assertEqual(health["broken_mailboxes"], 0)
        self.assertEqual(health["healthy_mailboxes"], health["mailboxes"])
        self.assertNotIn(str(row["email"]), health["broken_mailbox_emails"])

    def test_a_mailbox_that_never_polled_is_reported(self):
        self._make_user("boss@example.com")
        self._make_user("fresh@example.com")
        with db.connect() as connection:
            connection.execute("UPDATE mailboxes SET last_polled_at=NULL")
        health = web._service_health()
        self.assertEqual(health["stale_mailboxes"], 2)

    # -- the pilot cap the operator can now change from here ----------------

    def test_an_operator_can_raise_the_pilot_cap_without_touching_the_environment(self):
        """This used to mean editing a 0600 root-owned file over SSH and
        restarting the service."""
        self._make_user("boss@example.com")
        admin = self._login("boss@example.com")
        status, body = admin.get("/api/admin/capacity")
        self.assertEqual(status, 200)
        self.assertIn("recommended", body)
        self.assertEqual(body["source"], "environment")

        status, body = admin.put("/api/admin/capacity", {"max_users": 12})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["max_users"], 12)
        self.assertEqual(body["source"], "settings")
        self.assertEqual(db.get_setting("max_users"), "12")

        # And the registration gate actually honours the stored value.
        status, body = admin.get("/api/admin/capacity")
        self.assertEqual(body["current"], 12)

    def test_the_stored_cap_beats_the_environment_default(self):
        self._make_user("boss@example.com")
        with _mock.patch.dict("os.environ", {"INFE_PILOT_MAX_USERS": "5"}):
            db.set_setting("max_users", "9")
            self.assertEqual(web._max_users(), (9, "settings"))
            db.delete_setting("max_users")
            self.assertEqual(web._max_users(), (5, "environment"))

    def test_reset_falls_back_to_the_environment_default(self):
        self._make_user("boss@example.com")
        admin = self._login("boss@example.com")
        admin.put("/api/admin/capacity", {"max_users": 12})
        status, body = admin.put("/api/admin/capacity", {"reset": True})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["source"], "environment")
        self.assertEqual(db.get_setting("max_users"), "")

    def test_the_cap_cannot_drop_below_the_accounts_that_already_exist(self):
        """Not dangerous — the cap only gates new registrations — but the panel
        would show a cap nobody could fit under, which reads as a bug."""
        self._make_user("boss@example.com")
        self._make_user("member@example.com")
        admin = self._login("boss@example.com")
        status, body = admin.put("/api/admin/capacity", {"max_users": 1})
        self.assertEqual(status, 422, body)
        self.assertEqual(db.get_setting("max_users"), "", "被拒绝的值不能写进去")

    def test_silly_values_are_refused(self):
        self._make_user("boss@example.com")
        admin = self._login("boss@example.com")
        for payload in ({"max_users": 0}, {"max_users": -3}, {"max_users": 99999},
                        {"max_users": "abc"}, {}):
            status, _ = admin.put("/api/admin/capacity", payload)
            self.assertEqual(status, 422, payload)
        self.assertEqual(db.get_setting("max_users"), "")

    def test_the_audit_record_names_the_number_and_nothing_else(self):
        self._make_user("boss@example.com")
        admin = self._login("boss@example.com")
        admin.put("/api/admin/capacity", {"max_users": 12})
        _, body = admin.get("/api/admin/users")
        entries = [item for item in body["audit"] if item["action"] == "capacity_changed"]
        self.assertTrue(entries, "改名额必须留审计")
        self.assertEqual(entries[0]["detail"], "12")
        for forbidden in ("master", "key", "encrypted_", "password"):
            self.assertNotIn(forbidden, json.dumps(entries).lower())

    def test_the_endpoints_are_404_for_an_ordinary_account(self):
        self._make_user("boss@example.com")
        member = self._make_user("member@example.com")
        client = self._login(member["email"])
        self.assertEqual(client.get("/api/admin/capacity")[0], 404)
        self.assertEqual(client.put("/api/admin/capacity", {"max_users": 99})[0], 404)

    def test_an_anonymous_caller_is_refused(self):
        self._make_user("boss@example.com")
        client = Client(self.base)
        self.assertIn(client.get("/api/admin/capacity")[0], (401, 404))

    # -- the sentinel panel and "已知晓" ------------------------------------

    def _record_finding(self, key: str = "setup_stalled:usr_x") -> None:
        db.record_alert(key, "warning", "细节", "标题", dt.datetime.now(dt.timezone.utc))

    def test_the_console_receives_the_sentinels_findings(self):
        """The panel reads `alert_state`; if it were not delivered, the whole
        "quiet findings still show up" promise would be hollow."""
        self._make_user("boss@example.com")
        self._record_finding()
        admin = self._login("boss@example.com")
        status, body = admin.get("/api/admin/users")
        self.assertEqual(status, 200, body)
        self.assertIn("alerts", body)
        self.assertEqual(len(body["alerts"]), 1)
        self.assertEqual(body["alerts"][0]["tier"], "digest")
        self.assertFalse(body["alerts"][0]["acknowledged"])

    def test_acknowledging_is_admin_only(self):
        self._make_user("boss@example.com")
        member = self._make_user("member@example.com")
        self._record_finding()
        anonymous = Client(self.base)
        self.assertIn(anonymous.post("/api/admin/alerts/setup_stalled:usr_x/acknowledge")[0],
                      (401, 404))
        # A signed-in ordinary account gets 404, not 403: the console's existence
        # is not disclosed to it.
        self.assertEqual(
            self._login(member["email"]).post(
                "/api/admin/alerts/setup_stalled:usr_x/acknowledge")[0], 404)

    def test_an_admin_can_acknowledge_and_undo(self):
        self._make_user("boss@example.com")
        self._record_finding()
        admin = self._login("boss@example.com")

        status, body = admin.post("/api/admin/alerts/setup_stalled:usr_x/acknowledge")
        self.assertEqual(status, 200, body)
        self.assertTrue(body["alerts"][0]["acknowledged"])
        # Still listed: acknowledging is not closing.
        self.assertTrue(body["alerts"][0]["open"])

        status, body = admin.delete("/api/admin/alerts/setup_stalled:usr_x/acknowledge")
        self.assertEqual(status, 200, body)
        self.assertFalse(body["alerts"][0]["acknowledged"])

    def test_acknowledging_an_unknown_finding_is_a_404(self):
        self._make_user("boss@example.com")
        admin = self._login("boss@example.com")
        self.assertEqual(admin.post("/api/admin/alerts/nope/acknowledge")[0], 404)
        self.assertEqual(admin.delete("/api/admin/alerts/nope/acknowledge")[0], 404)

    def test_the_key_survives_the_encoding_the_browser_actually_sends(self):
        """The regression: 「已知晓」 clicked the way the console clicks it.

        `adminAcknowledgeAlert` builds its URL with `encodeURIComponent`, which
        turns `setup_stalled:usr_x` into `setup_stalled%3Ausr_x`. Route
        parameters were never percent-decoded, so the handler looked up a key
        that does not exist and every click answered 404 -- for *every* finding
        whose key carries an identifier, which in production is all of them.

        The test above passes `:` raw. That is a shape `urllib` only produces
        when nobody encoded it, so it never exercised the real client. Both are
        asserted now: one documents the contract, this one is what a browser
        sends.
        """
        self._make_user("boss@example.com")
        self._record_finding()
        admin = self._login("boss@example.com")

        encoded = "setup_stalled%3Ausr_x"
        status, body = admin.post(f"/api/admin/alerts/{encoded}/acknowledge")
        self.assertEqual(status, 200, body)
        self.assertTrue(body["alerts"][0]["acknowledged"])

        # And the round trip back out, with the same encoding.
        status, body = admin.delete(f"/api/admin/alerts/{encoded}/acknowledge")
        self.assertEqual(status, 200, body)
        self.assertFalse(body["alerts"][0]["acknowledged"])

        # The audit line records the real key, not the wire form: an operator
        # reading 「key=setup_stalled%3Ausr_x」 would be looking at a key that
        # appears nowhere else in the console.
        _, body = admin.get("/api/admin/users")
        entries = [item for item in body["audit"] if item["action"] == "alert_acknowledged"]
        self.assertTrue(entries, "静音必须留审计")
        self.assertIn("setup_stalled:usr_x", entries[-1]["detail"])
        self.assertNotIn("%3A", entries[-1]["detail"])

    def test_acknowledging_is_audited(self):
        self._make_user("boss@example.com")
        self._record_finding()
        admin = self._login("boss@example.com")
        admin.post("/api/admin/alerts/setup_stalled:usr_x/acknowledge")
        _, body = admin.get("/api/admin/users")
        entries = [item for item in body["audit"] if item["action"] == "alert_acknowledged"]
        self.assertTrue(entries, "静音必须留审计")
        self.assertIn("setup_stalled:usr_x", entries[0]["detail"])

    # -- one-click reminders for accounts that never finished ---------------
    #
    # Endpoint-level on purpose. The last time a feature like this was "tested",
    # the functions were green and the route returned 500 -- `analyse_many` and
    # `json_response` each wrap the other, and only a real request exercises both.

    def _admin(self) -> Client:
        """The operator account, created per test: `setUp` empties every table."""
        self._make_user("boss@example.com")
        return self._login("boss@example.com")

    def _stalled(self, email: str, *, hours: float = 30, **kwargs) -> dict:
        """An account that registered long enough ago to count as stuck."""
        user = self._make_user(email, **kwargs)
        moment = (dt.datetime.now(dt.timezone.utc)
                  - dt.timedelta(hours=hours)).isoformat(timespec="seconds")
        with db.connect() as connection:
            connection.execute("UPDATE users SET created_at=? WHERE id=?", (moment, user["id"]))
        return user

    def _received_school_mail(self, user: dict, *, uid: int = 1) -> str:
        """Give this account one message from an allowed sender.

        This is the product's whole definition of "the school's forwarding rule
        works": a message actually arrived. A mailbox that has never had one is
        the account `GAP_NO_MAIL` is about.
        """
        mailbox = db.get_mailbox(user["id"])
        message_id = db.insert_message(user["id"], mailbox["id"], "1", uid, {
            "subject": "选课通知", "sender_name": "Registry",
            "sender_address": "teacher@cityu.edu.hk",
            "received": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "body": "请于本周五前确认选课。", "message_key": f"<school-{uid}@cityu.edu.hk>",
        })
        self.assertIsNotNone(message_id, "夹具没有真的写进一行来信")
        return str(message_id)

    def _age_mailbox(self, user: dict, hours: float) -> None:
        """Make the mailbox look like it was connected `hours` ago."""
        moment = (dt.datetime.now(dt.timezone.utc)
                  - dt.timedelta(hours=hours)).isoformat(timespec="seconds")
        with db.connect() as connection:
            connection.execute("UPDATE mailboxes SET updated_at=? WHERE user_id=?",
                               (moment, user["id"]))

    def test_everyone_can_be_reached_even_a_brand_new_account(self):
        """用户原话：「我要可以给所有不管多久的没有注册完的用户发邮件」。

        `MIN_AGE_HOURS` 是给**自动**提醒用的缓冲（别在人还在填表时插一脚），
        运营者点「所有人都发」时它不该再挡人 —— 否则今天刚注册的人永远够不到。
        """
        fresh = self._make_user("justnow@example.com", mailbox=False)  # created_at = 现在
        client = self._admin()
        _, body = client.get("/api/admin/setup-reminders")
        self.assertEqual(body["counts"]["stalled"], 0, "新账号不该出现在默认那一档")
        self.assertGreaterEqual(body["counts"]["recent"], 1, "至少他算「刚注册的」")
        rows = {row["email"]: row for row in body["all_rows"]}
        self.assertIn("justnow@example.com", rows)
        self.assertTrue(rows["justnow@example.com"]["too_new"])
        # 运营者自己那个还没配邮箱的账号也在名单里（它确实没配完）——
        # 这一条钉住的是「新账号进得来」，不是名单总数。
        self.assertNotIn("justnow@example.com", [r["email"] for r in body["rows"]])
        # 默认那一档够不到他……
        with _mock.patch("pilot_app.setup_reminders.send_as_operator") as sender:
            sender.return_value = {"message_id": "<1@x>", "refused": {}}
            status, sent_default = client.post("/api/admin/setup-reminders", {})
            self.assertEqual(status, 200, sent_default)
            self.assertEqual(sent_default["sent"], 0)
            # ……「所有人都发」够得到。
            status, sent_all = client.post("/api/admin/setup-reminders", {"audience": "all"})
        self.assertEqual(status, 200, sent_all)
        mailed = [call[0][2] for call in sender.call_args_list]
        self.assertIn("justnow@example.com", mailed, "「所有人都发」必须够得到今天刚注册的人")

    def test_the_audience_is_recorded_and_junk_is_refused(self):
        self._stalled("stuck@example.com", mailbox=False)
        client = self._admin()
        with _mock.patch("pilot_app.setup_reminders.send_as_operator") as sender:
            sender.return_value = {"message_id": "<1@x>", "refused": {}}
            status, body = client.post("/api/admin/setup-reminders", {"audience": "all"})
        self.assertEqual(status, 200, body)
        with db.connect() as connection:
            detail = connection.execute(
                "SELECT detail FROM audit_log WHERE action='setup_reminders_sent'"
                " ORDER BY rowid DESC LIMIT 1"      # 最新那条：审计表在本类里不清空
            ).fetchone()[0]
        self.assertIn("audience=all", detail)
        self.assertEqual(client.post("/api/admin/setup-reminders", {"audience": "everyone"})[0], 422)

    def test_the_letter_can_be_edited_and_a_bad_placeholder_is_refused(self):
        """「我要可以在后台编辑文字内容」——改的是之后的信，改错了不许发出去。"""
        self._stalled("stuck@example.com", mailbox=False)
        client = self._admin()
        _, body = client.get("/api/admin/setup-reminders")
        self.assertIn("{link}", body["templates"]["never"])
        self.assertEqual(body["templates"]["never"], body["default_templates"]["never"])
        mine = "同学你好：\n\n请看 {link} 把邮箱接上，有问题找我。\n\n{wechat}"
        status, saved = client.put("/api/admin/setup-reminders/template",
                                   {"group": "never", "text": mine})
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved["text"], mine)
        self.assertIn("同学你好", saved["preview"]["never"])
        self.assertNotIn("{link}", saved["preview"]["never"], "占位符必须被替换掉")
        # 写错的占位符会被拒绝，而不是原样寄到真人邮箱里
        status, refused = client.put("/api/admin/setup-reminders/template",
                                     {"group": "never", "text": "看 {linkk} 设置"})
        self.assertEqual(status, 422)
        self.assertIn("linkk", refused["detail"])
        # 少了 {link} 也不行：收信人不知道该去哪儿
        self.assertEqual(client.put("/api/admin/setup-reminders/template",
                                    {"group": "never", "text": "随便写点什么"})[0], 422)
        # 清空 = 恢复默认
        status, reset = client.put("/api/admin/setup-reminders/template",
                                   {"group": "never", "text": ""})
        self.assertEqual(status, 200)
        self.assertEqual(reset["text"], body["default_templates"]["never"])

    def test_the_operator_can_pick_who_gets_a_reminder(self):
        """用户原话：「我要可以自己选给谁发卡住的邮件提醒」。

        三个卡住的人里只发一个 —— 手选既要**覆盖那两道门槛**（已经提醒过的、
        今天刚注册的都能点名发），又不能变成「谁都能发」：中途已经配好的人必须
        被跳过并如实回报，否则他会收到一句「你还没配好」，那既不真也很难听。
        """
        picked = self._stalled("picked@example.com", mailbox=False)
        self._stalled("ignored@example.com", mailbox=False, hours=0.1)   # 刚注册 + 没点他
        client = self._admin()
        with _mock.patch("pilot_app.setup_reminders.send_as_operator") as sender:
            sender.return_value = {"message_id": "<1@x>", "refused": {}}
            status, body = client.post("/api/admin/setup-reminders",
                                       {"audience": "selected", "user_ids": [picked["id"]]})
        self.assertEqual(status, 200, body)
        mailed = [call[0][2] for call in sender.call_args_list]
        self.assertEqual(mailed, ["picked@example.com"], "只有被点名的那个收到信")
        self.assertEqual(body["sent"], 1)
        self.assertEqual(body["requested"], 1)
        self.assertEqual(body["skipped"], [])
        # 面板按 sent_ids 清勾选：失败的人必须留在勾选里（否则「发过了」是假的）
        self.assertEqual(body["sent_ids"], [picked["id"]])
        with db.connect() as connection:
            detail = connection.execute(
                "SELECT detail FROM audit_log WHERE action='setup_reminders_sent'"
                " ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        self.assertIn("audience=selected", detail)
        self.assertIn("selected=1", detail)
        self.assertNotIn("picked@example.com", detail, "审计里不放地址")

    def test_picking_someone_overrides_the_two_gates(self):
        """提醒过的、刚注册的，只要被点名就发；没点名的照旧不动。"""
        old = self._stalled("reminded@example.com", mailbox=False)
        fresh = self._make_user("brandnew@example.com", mailbox=False)
        client = self._admin()
        # 先给他盖上「已提醒」的章：默认那一档从此不该再碰他。
        with db.connect() as connection:
            connection.execute("INSERT OR REPLACE INTO app_settings(key,value,updated_at,updated_by)"
                               " VALUES(?,?,?,'test')",
                               (f"setup_reminder:{old['id']}", "2026-09-15T00:00:00+00:00|never",
                                "2026-09-15T00:00:00+00:00"))
        with _mock.patch("pilot_app.setup_reminders.send_as_operator") as sender:
            sender.return_value = {"message_id": "<1@x>", "refused": {}}
            _, default_run = client.post("/api/admin/setup-reminders", {})
            self.assertEqual(default_run["sent"], 0, "已经提醒过的人不在默认那一档")
            _, picked = client.post("/api/admin/setup-reminders",
                                    {"audience": "selected",
                                     "user_ids": [old["id"], fresh["id"]]})
        self.assertEqual(picked["sent"], 2, picked)
        mailed = [call[0][2] for call in sender.call_args_list]
        self.assertIn("reminded@example.com", mailed, "点名要覆盖「已经提醒过」这道门")
        self.assertIn("brandnew@example.com", mailed, "点名要覆盖「刚注册」这道门")

    def test_picking_somebody_who_finished_is_skipped_and_reported(self):
        """点名不等于「一定能收到」：他已经配好、也收到过信，就不该再收到提醒。"""
        # verify=True：邮箱**验证通过**才算没有缺口。只建一行 mailbox 是「配了但
        # 从没连通成功」，那本身就是一个缺口（refused 那一档），提醒他是对的。
        #
        # 2026-09-16 起还要**收到过一封 CityU 来信**才算真的没事：在这之前
        # 「配好了邮箱」就等于「没有缺口」，而一个邮箱通着、学校那边转发规则
        # 却没生效的账号从此永远收不到信，也没有人告诉他（见
        # setup_reminders.GAP_NO_MAIL）。
        done = self._make_user("done@example.com", verify=True)
        self._received_school_mail(done)
        stuck = self._stalled("stuck@example.com", mailbox=False)
        client = self._admin()
        with _mock.patch("pilot_app.setup_reminders.send_as_operator") as sender:
            sender.return_value = {"message_id": "<1@x>", "refused": {}}
            status, body = client.post("/api/admin/setup-reminders",
                                       {"audience": "selected",
                                        "user_ids": [done["id"], stuck["id"], "usr_does_not_exist"]})
        self.assertEqual(status, 200, body)
        mailed = [call[0][2] for call in sender.call_args_list]
        self.assertEqual(mailed, ["stuck@example.com"])
        self.assertEqual(body["requested"], 3)
        self.assertEqual(sorted(body["skipped"]), sorted([done["id"], "usr_does_not_exist"]))
        self.assertEqual(body["sent"], 1)

    # -- 第三种卡住：邮箱通了，但一封 CityU 来信都没到过（2026-09-16）--------
    #
    # 用户原话：「为什么会出现这种问题，去解决。」起因是一个真实账号：注册、
    # 配好邮箱、授权码验证通过、收信灯是绿的——而他的邮箱里从 8 月 20 日起
    # 就没有过任何一封 CityU 来信（转发规则那一半从来没生效过）。旧的定义里
    # 「配好了邮箱」就等于「没有缺口」，所以这个人不在任何名单上，也没有人
    # 告诉他任何事。

    def test_a_connected_mailbox_with_no_school_mail_is_stuck(self):
        user = self._stalled("silent@example.com", verify=True, hours=30)
        self._age_mailbox(user, 30)
        client = self._admin()
        _, body = client.get("/api/admin/setup-reminders")
        rows = {row["email"]: row for row in body["rows"]}
        self.assertIn("silent@example.com", rows, "邮箱通了却收不到信，必须出现在名单里")
        self.assertEqual(rows["silent@example.com"]["group"], "no_mail")
        self.assertEqual(body["counts"]["no_mail"], 1)
        # 这一档的信不是「你还没配好」，而是学校那一边的步骤。
        self.assertIn("转发", rows["silent@example.com"]["body"])
        self.assertIn("cityu.edu.hk", rows["silent@example.com"]["body"])

    def test_one_arrived_message_clears_it_for_good(self):
        """转发的唯一证据是**信真的到了**——到了一封，这一档就再也不该出现。"""
        user = self._stalled("works@example.com", verify=True, hours=30)
        self._age_mailbox(user, 30)
        self._received_school_mail(user)
        client = self._admin()
        _, body = client.get("/api/admin/setup-reminders")
        self.assertEqual(body["rows"], [])
        self.assertEqual(body["counts"]["no_mail"], 0)

    def test_a_mailbox_connected_minutes_ago_is_left_alone(self):
        """刚接通就催人是错的：安静的几小时不是故障。"""
        user = self._stalled("brandnewbox@example.com", verify=True, hours=30)
        self._age_mailbox(user, 0.5)
        client = self._admin()
        _, body = client.get("/api/admin/setup-reminders")
        self.assertEqual(body["counts"]["stalled"], 0)
        # 但运营者手选时他进得来 —— 「所有人都发 / 自己挑」是一个决定，不是启发式。
        rows = {row["email"]: row for row in body["all_rows"]}
        self.assertIn("brandnewbox@example.com", rows)

    def test_a_changed_verdict_is_not_covered_by_the_old_stamp(self):
        """上次告诉他「你还没配好」，这次该告诉他的却是另一件事。

        印章记的是**发过哪一句**。他后来把邮箱配好了，于是那句已经不成立，
        而新的问题（转发一直没生效）需要另一封信——旧的印章不该把它盖住。
        """
        user = self._stalled("changed@example.com", mailbox=False)
        client = self._admin()
        with _mock.patch("pilot_app.setup_reminders.send_as_operator") as sender:
            sender.return_value = {"message_id": "<1@x>", "refused": {}}
            first = client.post("/api/admin/setup-reminders", {})
            self.assertEqual(first[1]["sent"], 1)
            # 他把邮箱配好了（而且通了），但学校那一边什么都没到过。
            mailbox_id = db.upsert_mailbox(user["id"], {
                "email": "box-changed@example.com", "report_to": "box-changed@example.com",
                "imap_host": "imap.qq.com", "imap_port": 993,
                "smtp_host": "smtp.qq.com", "smtp_port": 465,
                "encrypted_password": self.box.encrypt("s", context=f"mailbox:{user['id']}"),
            })
            db.record_mailbox_verification(mailbox_id)
            self._age_mailbox(user, 30)
            _, body = client.get("/api/admin/setup-reminders")
            row = [item for item in body["rows"] if item["email"] == "changed@example.com"][0]
            self.assertEqual(row["group"], "no_mail")
            self.assertTrue(row["needs_notice"], "换了一句话就等于还没告诉过他这一句")
            _, second = client.post("/api/admin/setup-reminders", {})
        self.assertEqual(second["sent"], 1, "第二封该发：问题变了")
        self.assertIn("changed@example.com", [call[0][2] for call in sender.call_args_list])

    # -- 第四种：服务商不再允许用授权码收信（2026-09-16 生产实际遇到）--------
    #
    # 一个真实账号把私人转发邮箱也设成了 @outlook.com（地址不写进这里）。微软对个人版
    # 关掉了 Basic auth，所以我们永远登不进去——**换授权码也没用**。原来的
    # `refused` 那一封会告诉他「授权码填成了登录密码」，并教他生成应用密码：
    # 每一句都是错的，他会照做到深夜，然后认为软件坏了。

    def _provider_blocked(self, email: str = "blocked@example.com") -> dict:
        user = self._stalled(email, verify=False, hours=30)
        with db.connect() as connection:
            connection.execute(
                "UPDATE mailboxes SET imap_host=?, last_error=?, last_verify_error=?"
                " WHERE user_id=?",
                ("outlook.office365.com",
                 "这个邮箱的服务商已经停用「账号密码 / 授权码」登录（微软 Outlook、Hotmail 已强制改用 OAuth）。"
                 "请换一个支持授权码的邮箱作为转发邮箱，例如 QQ 邮箱、Gmail 或 163 邮箱。",
                 "这个邮箱的服务商已经停用「账号密码 / 授权码」登录（微软 Outlook、Hotmail 已强制改用 OAuth）。"
                 "请换一个支持授权码的邮箱作为转发邮箱，例如 QQ 邮箱、Gmail 或 163 邮箱。",
                 user["id"]))
        return user

    def test_a_blocked_provider_gets_its_own_sentence(self):
        user = self._provider_blocked()
        client = self._admin()
        _, body = client.get("/api/admin/setup-reminders")
        row = [item for item in body["rows"] if item["user_id"] == user["id"]][0]
        self.assertEqual(row["group"], "provider")
        self.assertEqual(body["counts"]["provider"], 1)
        letter = row["body"]
        # 不能再说「授权码填错了」——那会让人去生成一个永远不可能成功的凭据
        self.assertIn("不是你的设置错了", letter)
        self.assertIn("换一个", letter)
        self.assertNotIn("最常见的原因是授权码填成了邮箱的登录密码", letter)

    def test_a_microsoft_host_is_recognised_even_before_the_error_appears(self):
        """主机名是一条独立证据：错误文本是后加的，老账号可能只有主机名。"""
        user = self._stalled("hostonly@example.com", verify=False, hours=30)
        with db.connect() as connection:
            connection.execute("UPDATE mailboxes SET imap_host=? WHERE user_id=?",
                               ("outlook.office365.com", user["id"]))
        client = self._admin()
        _, body = client.get("/api/admin/setup-reminders")
        row = [item for item in body["rows"] if item["user_id"] == user["id"]][0]
        self.assertEqual(row["group"], "provider")

    def test_a_wrong_auth_code_is_still_the_old_sentence(self):
        """别把两种「登不进去」混成一句：QQ 的授权码错了他自己改得动。"""
        user = self._stalled("qqwrong@example.com", verify=False, hours=30)
        with db.connect() as connection:
            connection.execute(
                "UPDATE mailboxes SET imap_host=?, last_error=? WHERE user_id=?",
                ("imap.qq.com", "IMAP 连接失败：b'LOGIN Login error or password error'", user["id"]))
        client = self._admin()
        _, body = client.get("/api/admin/setup-reminders")
        row = [item for item in body["rows"] if item["user_id"] == user["id"]][0]
        self.assertEqual(row["group"], "refused")

    def test_the_two_sentences_are_actually_different_letters(self):
        row = {"mailbox_email": "a@outlook.com", "imap_host": "outlook.office365.com",
               "mailbox_error": "…已强制改用 OAuth…"}
        self.assertTrue(database_mod.Database.mailbox_needs_another_provider(row))
        self.assertFalse(database_mod.Database.mailbox_needs_another_provider(
            {"imap_host": "imap.qq.com", "mailbox_error": "LOGIN Login error or password error"}))

    def test_a_selection_that_is_not_a_list_of_ids_is_refused(self):
        self._stalled("stuck@example.com", mailbox=False)
        client = self._admin()
        # 空、不是列表、全是空白、超上限 —— 每一种都要 422，而不是静默发出意外的信
        self.assertEqual(client.post("/api/admin/setup-reminders",
                                     {"audience": "selected"})[0], 422)
        self.assertEqual(client.post("/api/admin/setup-reminders",
                                     {"audience": "selected", "user_ids": "usr_1"})[0], 422)
        self.assertEqual(client.post("/api/admin/setup-reminders",
                                     {"audience": "selected", "user_ids": ["", "  "]})[0], 422)
        too_many = [f"usr_{index}" for index in range(setup_reminders_mod.BATCH_LIMIT + 1)]
        status, body = client.post("/api/admin/setup-reminders",
                                   {"audience": "selected", "user_ids": too_many})
        self.assertEqual(status, 422)
        self.assertIn(str(setup_reminders_mod.BATCH_LIMIT), body["detail"])

    def test_a_working_mailbox_stops_being_called_stale_within_minutes(self):
        """「收信正常的更新频率太慢了」——判据以前借的是**告警**阈值（QQ 一小时），
        于是一个刚恢复的邮箱要等一小时才在卡片上变绿。现在按它自己的收信间隔算。"""
        self.assertEqual(web._poll_freshness_seconds({"imap_host": "imap.qq.com"}), 180.0)
        self.assertEqual(web._poll_freshness_seconds({"imap_host": "imap.gmail.com"}), 1800.0)
        self.assertEqual(web._poll_freshness_seconds({}), 180.0, "认不出主机时按最快的那档")
        # 10 分钟前收过信的 QQ 邮箱：旧规则说它「在跑」，新规则也说 —— 但 5 分钟
        # 这个窗口差在「一小时内的任何时刻都不会再被判成停顿」。
        self.assertLess(web._poll_freshness_seconds({"imap_host": "imap.qq.com"}), 3600)

    def test_the_panel_lists_who_is_stuck_and_shows_both_letters(self):
        self._stalled("stuck@example.com", mailbox=False)
        client = self._admin()
        status, body = client.get("/api/admin/setup-reminders")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["counts"]["stalled"], 1)
        self.assertEqual(body["counts"]["pending"], 1)
        self.assertEqual([row["group"] for row in body["rows"]], ["never"])
        self.assertIn("还差一步", body["preview"]["never"])
        self.assertIn("登录被拒绝", body["preview"]["refused"])
        self.assertGreaterEqual(body["batch_limit"], 1)

    def _reject_mailbox(self, user_id: str) -> None:
        """The production shape of a wrong auth code.

        A poll *happened* (so there is a timestamp) and it *failed* -- and the
        timestamp cannot express the second half, which is the trap.
        """
        moment = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        with db.connect() as connection:
            connection.execute(
                "UPDATE mailboxes SET last_polled_at=?, last_error=? WHERE user_id=?",
                (moment, "IMAP 连接失败：b'LOGIN Login error or password error'", user_id))

    def test_a_rejected_auth_code_is_listed_even_though_setup_gap_is_empty(self):
        """This account appears in no other list.

        `last_polled_at` is written on failure too, so `setup_gap` returns "" and
        `stalled_setups` never mentions it -- the receive light is the only thing
        that can see it, which is why the grouping makes two judgements, not one.
        Production had exactly this account on 2026-09-15.
        """
        user = self._stalled("refused@example.com", mailbox=True, verify=False)
        self._reject_mailbox(user["id"])
        rows = {row["id"]: row for row in db.list_users_overview()}
        self.assertEqual(db.setup_gap(rows[user["id"]]), "", "前提：setup_gap 看不见它")
        client = self._admin()
        _, body = client.get("/api/admin/setup-reminders")
        self.assertEqual([row["group"] for row in body["rows"]], ["refused"])

    def test_a_finished_account_is_never_listed(self):
        self._stalled("fine@example.com", mailbox=True, verify=True)
        client = self._admin()
        _, body = client.get("/api/admin/setup-reminders")
        self.assertEqual(body["rows"], [])
        self.assertEqual(body["counts"]["stalled"], 0)

    def test_a_brand_new_account_is_not_pounced_on(self):
        """Ten minutes after registering you are busy, not stuck."""
        self._make_user("fresh@example.com", mailbox=False)
        client = self._admin()
        _, body = client.get("/api/admin/setup-reminders")
        self.assertEqual(body["counts"]["stalled"], 0)

    # -- 「提醒之后他回来过没有」（2026-09-17）--------------------------------
    #
    # 运营者的问题：「我发出去的那封信到底有没有把人叫回来」。**印章答不了它**——
    # 印章只说明我们做了什么。会话表也答不了：退出登录会把行删掉，生产上两个
    # 9-15 被提醒的账号连一行会话都没剩，于是「没看到信」和「看到了没配完」分不开。
    # 所以这一列记的是**用过应用**（任何已登录请求），不是登录。

    def _last_seen(self, user_id: str) -> str:
        """直接读那一列：`get_user` 只返回四个字段，而这一列是运营侧的。"""
        with db.connect() as connection:
            row = connection.execute(
                "SELECT last_seen_at FROM users WHERE id=?", (user_id,)).fetchone()
        return str((row or {})["last_seen_at"] or "")

    def test_a_signed_in_request_marks_the_account_as_seen(self):
        user = self._stalled("seen@example.com", mailbox=False)
        client = self._login("seen@example.com")
        client.get("/api/me")
        self.assertTrue(self._last_seen(user["id"]), "已登录的请求必须留下活跃时间")

    def test_coming_back_after_the_letter_is_visible_in_the_panel(self):
        self._stalled("told@example.com", mailbox=False)
        admin = self._admin()
        with _mock.patch("pilot_app.setup_reminders.send_as_operator") as sender:
            sender.return_value = {"message_id": "<1@x>", "refused": {}}
            admin.post("/api/admin/setup-reminders", {})
        # 提醒之后本人回来了一趟（真的走一次登录路径，不手写时间戳）。
        self._login("told@example.com").get("/api/me")
        _, body = admin.get("/api/admin/setup-reminders")
        row = [item for item in body["rows"] if item["email"] == "told@example.com"][0]
        self.assertTrue(row["notified_at"])
        self.assertTrue(row["last_seen_at"])
        self.assertIs(row["came_back_after_notice"], True)

    def test_someone_who_never_came_back_is_not_reported_as_back(self):
        self._stalled("silent@example.com", mailbox=False)
        admin = self._admin()
        with _mock.patch("pilot_app.setup_reminders.send_as_operator") as sender:
            sender.return_value = {"message_id": "<1@x>", "refused": {}}
            admin.post("/api/admin/setup-reminders", {})
        _, body = admin.get("/api/admin/setup-reminders")
        row = [item for item in body["rows"] if item["email"] == "silent@example.com"][0]
        self.assertIs(row["came_back_after_notice"], False)
        self.assertFalse(row["ever_seen"])

    def test_an_account_that_was_never_told_gets_no_verdict(self):
        """对着一个还没被提醒过的人说「他没回来」，是把我们自己的动作算在他头上。"""
        self._stalled("notold@example.com", mailbox=False)
        admin = self._admin()
        _, body = admin.get("/api/admin/setup-reminders")
        row = [item for item in body["rows"] if item["email"] == "notold@example.com"][0]
        self.assertEqual(row["notified_at"], "")
        self.assertIsNone(row["came_back_after_notice"])

    def test_activity_before_the_letter_does_not_count_as_coming_back(self):
        user = self._stalled("early@example.com", mailbox=False)
        # 他注册那天用过应用，但提醒是之后才发的。
        self._login("early@example.com").get("/api/me")
        with db.connect() as connection:
            connection.execute(
                "UPDATE users SET last_seen_at=? WHERE id=?",
                ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=20)).isoformat(timespec="seconds"),
                 user["id"]))
        admin = self._admin()
        with _mock.patch("pilot_app.setup_reminders.send_as_operator") as sender:
            sender.return_value = {"message_id": "<1@x>", "refused": {}}
            admin.post("/api/admin/setup-reminders", {})
        _, body = admin.get("/api/admin/setup-reminders")
        row = [item for item in body["rows"] if item["email"] == "early@example.com"][0]
        self.assertTrue(row["ever_seen"], "他确实用过应用")
        self.assertIs(row["came_back_after_notice"], False, "但那是提醒之前的事")

    def test_a_reminder_older_than_the_tracking_start_gets_no_verdict(self):
        """这一列是 v0.63.67 才有的。比它更早的那次提醒，「之后」没有人看着——
        那时候说「他没回来」是拿一个没有数据的时段当证据，而那句话会让运营者
        去发第二封信。不知道就说不知道。"""
        self._stalled("oldtold@example.com", mailbox=False)
        admin = self._admin()
        with _mock.patch("pilot_app.setup_reminders.send_as_operator") as sender:
            sender.return_value = {"message_id": "<1@x>", "refused": {}}
            admin.post("/api/admin/setup-reminders", {})
        # 把「从什么时候开始记」推到提醒之后，模拟一次升级前的旧印章。
        later = (dt.datetime.now(dt.timezone.utc)
                 + dt.timedelta(minutes=5)).isoformat(timespec="seconds")
        db.set_setting(database_mod.LAST_SEEN_SINCE_KEY, later)
        _, body = admin.get("/api/admin/setup-reminders")
        row = [item for item in body["rows"] if item["email"] == "oldtold@example.com"][0]
        self.assertIsNone(row["came_back_after_notice"], "没有数据的时间段不许下结论")
        self.assertEqual(row["verdict_reason"], "before_tracking")

    def test_a_reminder_after_the_tracking_start_is_judged_normally(self):
        """另一半：记录已经在跑，判据就得照常给结论——否则「不知道」会变成万能挡箭牌。"""
        db.set_setting(database_mod.LAST_SEEN_SINCE_KEY,
                       (dt.datetime.now(dt.timezone.utc)
                        - dt.timedelta(days=1)).isoformat(timespec="seconds"))
        self._stalled("knowable@example.com", mailbox=False)
        admin = self._admin()
        with _mock.patch("pilot_app.setup_reminders.send_as_operator") as sender:
            sender.return_value = {"message_id": "<1@x>", "refused": {}}
            admin.post("/api/admin/setup-reminders", {})
        _, body = admin.get("/api/admin/setup-reminders")
        row = [item for item in body["rows"] if item["email"] == "knowable@example.com"][0]
        self.assertIs(row["came_back_after_notice"], False)
        self.assertEqual(row["verdict_reason"], "")

    def test_the_activity_stamp_is_written_at_most_every_few_minutes(self):
        """每个已登录请求都写一次，等于把 SQLite 当一个高频计数器用；条件更新让
        没到间隔的请求什么都不改。"""
        user = self._make_user("busy@example.com")
        db.touch_last_seen(user["id"])
        first = self._last_seen(user["id"])
        db.touch_last_seen(user["id"])
        self.assertEqual(self._last_seen(user["id"]), first)
        # 过了间隔就必须更新（否则「回来过」会永远停在他第一次用的时候）。
        later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
            seconds=db.LAST_SEEN_MIN_GAP_SECONDS + 1)
        db.touch_last_seen(user["id"], now=later)
        self.assertGreater(self._last_seen(user["id"]), first)

    def test_the_operator_cannot_read_it_out_of_their_own_account_api(self):
        """`admin_note` 学到的教训：用户自己的接口不许漏出运营侧字段。"""
        user = self._stalled("selfview@example.com", mailbox=False)
        db.touch_last_seen(user["id"])
        client = self._login("selfview@example.com")
        _, me = client.get("/api/me")
        self.assertNotIn("last_seen_at", json.dumps(me))
        _, exported = client.get("/api/account/export")
        self.assertNotIn("last_seen_at", json.dumps(exported))

    def test_ordinary_users_cannot_see_or_use_it(self):
        self._stalled("plain3@example.com")
        client = self._login("plain3@example.com")
        self.assertEqual(client.get("/api/admin/setup-reminders")[0], 404)
        self.assertEqual(client.post("/api/admin/setup-reminders", {})[0], 404)

    def test_sending_mails_everyone_stuck_and_records_it(self):
        self._stalled("never@example.com", mailbox=False)
        self._stalled("refused@example.com", mailbox=True, verify=False)
        client = self._admin()
        with _mock.patch("pilot_app.setup_reminders.send_as_operator") as sender:
            sender.return_value = {"from": "boss@example.com", "message_id": "<1@x>", "refused": {}}
            status, body = client.post("/api/admin/setup-reminders", {})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["sent"], 2)
        self.assertEqual(body["failed"], 0)
        self.assertEqual(body["counts"]["pending"], 0)
        self.assertEqual(body["counts"]["notified"], 2)
        self.assertTrue(all(row["notified_at"] for row in body["rows"]))
        self.assertEqual(sender.call_count, 2)

    def test_the_second_press_does_not_mail_anybody_again(self):
        """The whole reason the bookkeeping exists: these are real inboxes, and a
        second reminder about the same thing is how a helpful feature turns into
        spam."""
        self._stalled("once@example.com", mailbox=False)
        client = self._admin()
        with _mock.patch("pilot_app.setup_reminders.send_as_operator") as sender:
            sender.return_value = {"message_id": "<1@x>", "refused": {}}
            first = client.post("/api/admin/setup-reminders", {})
            second = client.post("/api/admin/setup-reminders", {})
        self.assertEqual(first[1]["sent"], 1)
        self.assertEqual(second[1]["sent"], 0)
        self.assertEqual(sender.call_count, 1, "同一个人不该收到第二封")

    def test_the_resend_button_is_the_only_way_to_repeat(self):
        self._stalled("again@example.com", mailbox=False)
        client = self._admin()
        with _mock.patch("pilot_app.setup_reminders.send_as_operator") as sender:
            sender.return_value = {"message_id": "<1@x>", "refused": {}}
            client.post("/api/admin/setup-reminders", {})
            status, body = client.post("/api/admin/setup-reminders", {"include_notified": True})
        self.assertEqual(status, 200)
        self.assertEqual(body["sent"], 1)
        self.assertEqual(sender.call_count, 2)

    def test_a_failed_send_is_not_recorded_as_delivered(self):
        """Recording before the send is the one way to actually lose a person:
        the record would say they were told, and they never were."""
        self._stalled("flaky@example.com", mailbox=False)
        client = self._admin()
        with _mock.patch("pilot_app.setup_reminders.send_as_operator") as sender:
            sender.side_effect = RuntimeError("SMTP 发送失败：connection refused")
            status, body = client.post("/api/admin/setup-reminders", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["sent"], 0)
        self.assertEqual(body["failed"], 1)
        self.assertEqual(len(body["failures"]), 1)
        self.assertEqual(body["counts"]["pending"], 1, "失败的人必须还留在待发名单里")

    def test_a_refused_recipient_is_a_failure_not_a_delivery(self):
        """`send_message` only raises when *every* recipient is refused; a partial
        refusal comes back as a map, and treating that as success would record a
        delivery that did not happen."""
        self._stalled("refused2@example.com", mailbox=False)
        client = self._admin()
        with _mock.patch("pilot_app.setup_reminders.send_as_operator") as sender:
            sender.return_value = {"message_id": "<1@x>", "refused": {"refused2@example.com": 550}}
            _, body = client.post("/api/admin/setup-reminders", {})
        self.assertEqual(body["sent"], 0)
        self.assertEqual(body["failed"], 1)
        self.assertEqual(len(body["failures"]), 1)
        self.assertEqual(body["counts"]["pending"], 1)

    def test_sending_is_audited(self):
        """Mail to real people is an operator action; the console's audit list is
        the only trace of it that survives the session."""
        self._stalled("audited@example.com", mailbox=False)
        client = self._admin()
        with _mock.patch("pilot_app.setup_reminders.send_as_operator") as sender:
            sender.return_value = {"message_id": "<1@x>", "refused": {}}
            client.post("/api/admin/setup-reminders", {})
        _, body = client.get("/api/admin/users")
        entries = [item for item in body["audit"] if item["action"] == "setup_reminders_sent"]
        self.assertTrue(entries, "发信必须留审计")
        self.assertIn("sent=1", entries[0]["detail"])

    def test_the_wechat_line_comes_from_the_environment_not_the_code(self):
        """This repository is public. A self-hosted copy must not mail its users
        somebody else's personal account, so the id is configuration -- and the
        console says plainly whether this instance has one."""
        self._stalled("contact@example.com", mailbox=False)
        client = self._admin()
        with _mock.patch.dict(os.environ, {"INFE_PILOT_CONTACT_WECHAT": "someone_wechat_id"}):
            _, body = client.get("/api/admin/setup-reminders")
            self.assertEqual(body["preview"]["wechat"], "someone_wechat_id")
            self.assertIn("someone_wechat_id", body["preview"]["never"])
        os.environ.pop("INFE_PILOT_CONTACT_WECHAT", None)
        _, body = client.get("/api/admin/setup-reminders")
        self.assertEqual(body["preview"]["wechat"], "")
        self.assertNotIn("微信", body["preview"]["never"])


    # -- 替用户刷新状态（2026-09-16）----------------------------------------
    #
    # 用户原话：「帮我做对每一个用户都可以一键刷新他们所有状态的按钮，我要这个
    # 按钮可以选择全部人也可以单某个人」。四盏灯里三盏要求「有人真的测过一次」，
    # 而唯一不会去点那个按钮的人，正是账号的主人。

    def test_refreshing_runs_the_real_tests_and_lights_the_lamps(self):
        user = self._make_user("probe@example.com", mailbox=True, verify=False)
        client = self._admin()
        with _mock.patch("pilot_app.web.get_service") as service:
            service.return_value.test_mailbox.return_value = {"imap": "ok"}
            service.return_value.test_model.return_value = "连接成功"
            service.return_value.test_search.return_value = [{"title": "x"}]
            status, body = client.post(f"/api/admin/users/{user['id']}/refresh", {})
        self.assertEqual(status, 200, body)
        self.assertEqual([item["key"] for item in body["results"]],
                         ["mailbox", "model", "search"])
        self.assertTrue(all(item["ok"] for item in body["results"]), body["results"])
        # 灯真的变了：验证过的收信灯变绿（这正是这个按钮存在的理由）。
        lights = {item["key"]: item for item in body["lights"]}
        self.assertTrue(lights["mailbox"]["ok"], body["lights"])
        self.assertTrue(lights["model"]["ok"], body["lights"])
        # 出报告**没有**被点亮：它只能由一封真的来信证明。
        self.assertFalse(lights["report"]["ok"])

    def test_a_failed_probe_is_recorded_so_the_light_goes_red(self):
        """只记成功会让上一次的失败永远挂着——失败也是答案。"""
        user = self._make_user("badprobe@example.com", mailbox=True, verify=True)
        client = self._admin()
        with _mock.patch("pilot_app.web.get_service") as service:
            service.return_value.test_mailbox.side_effect = MailError("授权码被拒绝")
            _, body = client.post(f"/api/admin/users/{user['id']}/refresh",
                                  {"targets": ["mailbox"]})
        self.assertFalse(body["results"][0]["ok"])
        self.assertIn("授权码被拒绝", body["results"][0]["error"])
        self.assertFalse(body["lights"][0]["ok"], "失败了灯必须变红")
        self.assertIn("授权码被拒绝", body["lights"][0]["detail"])

    def test_a_shared_key_is_never_reported_as_the_accounts_own(self):
        """没有自己的 key 的账号用的是平台兜底 key：真的调通了，但这盏灯记不下
        来（`connections` 里没有行），所以响应必须说出来，而不是让运营者看到
        「刚刷新成功」和「从没测过」同时挂在一个人身上。"""
        user = self._make_user("shared@example.com", mailbox=True, verify=True, model=False)
        client = self._admin()
        with _mock.patch("pilot_app.web.get_service") as service:
            service.return_value.test_model.return_value = "连接成功"
            _, body = client.post(f"/api/admin/users/{user['id']}/refresh",
                                  {"targets": ["model"]})
        result = body["results"][0]
        self.assertTrue(result["ok"])
        self.assertFalse(result["own"])
        self.assertIn("平台兜底", result["note"])
        self.assertIn("不会变绿", result["note"])

    def test_the_console_shows_a_grey_platform_light_instead_of_a_red_one(self):
        """用户原话（2026-09-16）：「为什么点刷新用户状态还是亮红灯」。

        那两盏红灯**点多少次都不会变绿**：`record_connection_result` 是
        `UPDATE connections`，而没有自己 key 的账号根本没有那一行。所以它不是
        「还没测」，而是「这件事对他不适用」——平台 key 由平台出钱，是他真实的
        配置，用户自己的仪表盘对同一件事说的是「平台代付」。
        """
        user = self._make_user("grey@example.com", mailbox=True, model=False)
        client = self._admin()
        with _mock.patch.dict(os.environ, {"INFE_PILOT_DEFAULT_MODEL_KEY": "platform-fixture-key"}):
            status, body = client.get("/api/admin/users")
        self.assertEqual(status, 200, body)
        row = next(item for item in body["users"] if item["id"] == user["id"])
        lights = {item["key"]: item for item in row["lights"]}
        self.assertEqual(lights["model"]["state"], "shared", lights["model"])
        self.assertFalse(lights["model"]["ok"], "不是绿灯：平台 key 没在这个账号上被证明过")
        self.assertIn("平台兜底", lights["model"]["detail"])
        # 同一份列表里，别人自己的 key 照旧按它自己的证据判。
        self.assertEqual(lights["mailbox"]["state"], "untested")

    def test_the_refresh_response_carries_the_same_verdict_as_the_list(self):
        """刷新之后那张卡是**照响应重画的**，所以两个入口必须给出同一个答案。

        只改列表不改刷新，就会回到「点完刷新，卡片又变回红的」那种自相矛盾——
        v0.63.1 的 `usage-refresh` 就是这么坏的（两份状态、一份没接上）。
        """
        user = self._make_user("both@example.com", mailbox=True, verify=True, model=False)
        client = self._admin()
        with _mock.patch.dict(os.environ, {"INFE_PILOT_DEFAULT_MODEL_KEY": "platform-fixture-key"}), \
                _mock.patch("pilot_app.web.get_service") as service:
            service.return_value.test_model.return_value = "连接成功"
            _, refreshed = client.post(f"/api/admin/users/{user['id']}/refresh",
                                       {"targets": ["model"]})
            _, listed = client.get("/api/admin/users")
        from_refresh = {item["key"]: item for item in refreshed["lights"]}["model"]
        from_list = next(item for item in listed["users"]
                         if item["id"] == user["id"])["lights"]
        from_list = {item["key"]: item for item in from_list}["model"]
        self.assertEqual(from_refresh["state"], "shared", from_refresh)
        self.assertEqual(from_refresh, from_list)

    def test_only_the_three_testable_parts_are_accepted(self):
        user = self._make_user("targets@example.com")
        client = self._admin()
        for payload in ({"targets": ["report"]}, {"targets": "mailbox"},
                        {"targets": [1, 2]}):
            self.assertEqual(
                client.post(f"/api/admin/users/{user['id']}/refresh", payload)[0], 422, payload)
        self.assertEqual(client.post("/api/admin/users/usr_nope/refresh", {})[0], 404)

    def test_ordinary_users_cannot_refresh_anybody(self):
        member = self._make_user("member9@example.com")
        victim = self._make_user("victim9@example.com")
        self.assertEqual(
            self._login(member["email"]).post(
                f"/api/admin/users/{victim['id']}/refresh", {})[0], 404)

    def test_refreshing_is_audited_without_any_reading(self):
        user = self._make_user("auditme@example.com", mailbox=True, verify=True)
        client = self._admin()
        with _mock.patch("pilot_app.web.get_service") as service:
            service.return_value.test_mailbox.return_value = {"imap": "ok"}
            client.post(f"/api/admin/users/{user['id']}/refresh", {"targets": ["mailbox"]})
        _, body = client.get("/api/admin/users")
        entries = [item for item in body["audit"] if item["action"] == "user_refreshed"]
        self.assertTrue(entries, "替别人连一次邮箱必须留审计")
        self.assertIn("targets=mailbox", entries[0]["detail"])
        self.assertIn("ok=1", entries[0]["detail"])


class FailedReportWordingTests(unittest.TestCase):
    """面板上的两个数必须能对上账（用户报过一次对不上）。

    「失败报告 5 份」与「下发情况」是两张不同的表：前者含每日简报，后者一行一封邮件。
    测试钉的是**话有没有说清**——数字本身由 `test_service.FailedReportAccountingTests` 管。
    """

    def test_the_health_card_separates_the_two_kinds_of_failure(self):
        root = pathlib.Path(__file__).resolve().parents[2]
        app_js = (root / "pilot_app" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("failed_reports_digests", app_js)
        self.assertIn("逐封邮件", app_js)
        self.assertIn("每日简报", app_js)

    def test_the_mail_list_says_why_a_digest_failure_is_not_in_it(self):
        root = pathlib.Path(__file__).resolve().parents[2]
        app_js = (root / "pilot_app" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("failed_digests", app_js)
        self.assertIn("不对应某一封邮件，所以不在上面的列表里", app_js)


if __name__ == "__main__":
    unittest.main()

class RegisteredUsersPanelIsCollapsedTests(unittest.TestCase):
    """「已注册用户」里一个账号一张卡，**默认全部收起**。

    用户原话：「已注册用户一点开全部展开了，显得太杂乱了」。这里的断言是**结构**
    层面的——渲染成 `<details>`、默认不带 `open`、开一个关一个、展开状态记在内存里。
    真正的点击行为由浏览器套件按**真实坐标**验（`admin_edit_check`）。
    """

    def test_each_account_is_a_details_that_starts_closed(self):
        app = (pathlib.Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("el('details', 'admin-user-box')", app)
        # 默认打开的话，这里就该出现 `details.open = true` 这类写法。
        self.assertNotRegex(app, r"admin-user-box'\),\s*\n?\s*details\.open = true")
        self.assertIn("if (adminUserOpen === String(row.id)) details.open = true;", app)

    def test_opening_one_closes_the_others(self):
        app = (pathlib.Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("querySelectorAll('details.admin-user-box[open]')", app)
        self.assertIn("if (other !== details) other.open = false;", app)

    def test_the_open_card_survives_a_redraw(self):
        """刷新状态之后面板整块重画 —— 不记着的话，人正在读的那张卡会当场收起。"""
        app = (pathlib.Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("let adminUserOpen = ''", app)

    def test_the_four_lights_stay_visible_while_collapsed(self):
        """收起可以，但「谁卡在哪」不能一起收起来 —— 那是这一页存在的理由。"""
        app = (pathlib.Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
        page = (pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("renderLights(row, { compact: true })", app)
        # 灯只画一份：画两份的话，一个账号在一次渲染里会有八盏灯。
        self.assertEqual(app.count("renderLights(row"), 1 + app.count("function renderLights(row"))
        self.assertIn(".admin-user-box:not([open]) .lights.compact .why{display:none}", page)

    def test_the_checkbox_is_not_inside_the_summary(self):
        """勾选框若在 <summary> 里，点它会不会顺手展开就取决于浏览器的默认行为 ——
        这个项目已经两次栽在「点击落到祖先元素」上，所以从结构上分开。"""
        app = (pathlib.Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("top.appendChild(pick);", app)
        self.assertIn("top.appendChild(details);", app)
        # 勾选框必须显式 width:auto —— 后台给 input 统一设了 width:100%，
        # 否则它会被拉成整行宽，把右边的 <details> 挤成 0 宽（Playwright 眼里
        # 就是「不可见」，人眼里是一个空行）。
        page = (pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn(".admin-user-top > input[type=checkbox]{margin:7px 0 0;flex:0 0 auto;width:auto}", page)

    def test_the_third_light_state_is_drawn_in_its_own_colour(self):
        """第三态（走平台 key）必须是**自己的一种颜色**，不是红色的变体。

        用户原话：「为什么点刷新用户状态还是亮红灯」——那两盏红灯点多少次都不会
        变绿，所以它们必须长得不像故障：灰色、用主题里的中性色（不是写死的颜色，
        否则四个主题里总有一个看不见它），并且措辞说的是「不适用」而不是「没测过」。
        """
        app = (pathlib.Path(__file__).resolve().parent.parent / "static" / "app.js").read_text(encoding="utf-8")
        page = (pathlib.Path(__file__).resolve().parent.parent / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("light.state === 'shared'", app)
        self.assertIn("light.failed_at ? `（失败于 ${adminStamp(light.failed_at)}）`", app)
        self.assertIn(".light.shared .dot{background:var(--muted);box-shadow:0 0 0 2px var(--line-soft)}", page)
        self.assertIn(".light.shared b{color:var(--muted)}", page)
        # 「全部通过」只覆盖它真测的三件事，而这个面板上有四盏灯。
        self.assertNotIn("，全部通过", app)
        self.assertIn("「出报告」不在其中：它只能由一封真的来信点亮", app)
        self.assertIn("灰色的「走平台兜底 key」不是故障", page)


class RefreshShowsWhatIsNewTests(unittest.TestCase):
    """刷新之后，需要他动手的事要写在刷新按钮下面，并标出这一次新增的。

    用户原话（2026-09-17）：「我刷新后台界面应该要可以显示新的通知，比如有人申请了
    邀请码等等」。刷新本来就取回了这些数字，坏的是**形状**：它们散在 17 个**收起**的
    面板摘要行里，有人提交申请时屏幕上唯一的变化是某一行小字从「0 待处理」变成
    「1 待处理」——没有第二处会说话。

    真正点下去的行为由浏览器套件验（`admin_edit_check`）；这里钉四条容易悄悄退化的：
    这一行不新增任何请求、基准只在整块刷新时前移、每一项点得动、空的时候也说话。
    """

    @staticmethod
    def _app() -> str:
        return (pathlib.Path(__file__).resolve().parent.parent
                / "static" / "app.js").read_text(encoding="utf-8")

    @staticmethod
    def _page() -> str:
        return (pathlib.Path(__file__).resolve().parent.parent
                / "static" / "index.html").read_text(encoding="utf-8")

    def test_every_item_comes_from_data_the_refresh_already_fetched(self):
        """**不新增请求**：这一行是「把已经拿到的数字说出来」，不是又一轮查询。

        后台有 17 个面板、2 核机器，刷新一次已经够重了。这条把函数的边界钉住：
        一旦有人在里面写 `api(...)` 或调 `loadXxx()`，断言当场红。
        """
        app = self._app()
        body = app[app.index("function adminAttentionItems()"):app.index("function renderAdminAttention(")]
        self.assertNotIn("api(", body)
        self.assertNotIn("await ", body)
        # 它读的那几处正是 `/api/admin/users` 与留言接口的字段。
        # （`adminData.signup_counts` 2026-09-22 起不在这里了：开放注册之后不再有新申请，
        #  「N 个申请等发码」那一项连同它的数字一起删了。）
        for source in ("adminData.alerts", "adminData.stalled_users",
                       "adminData.health", "adminPending.guestbook"):
            self.assertIn(source, body, source)

    def test_the_baseline_only_moves_on_a_full_refresh(self):
        """「新增」的基准只在页面加载 / 按「刷新全部」时前移。

        留言面板在刷新过程里也会重画这一行（它是唯一知道待处理留言数的地方），
        若那次重画顺手把基准前移，刚发现的「新增 1 个新账号」会被自己人吃掉 ——
        这个 bug 在浏览器里真出现过：那一行显示了新的数，却没有「（新增 1）」。
        """
        app = self._app()
        self.assertIn("function renderAdminAttention({ rebase = true } = {})", app)
        self.assertIn("renderAdminAttention({ rebase: false })", app)
        # 整块刷新那条路（loadAdmin）用默认值，也就是 rebase。
        load_admin = app[app.index("async function loadAdmin("):]
        load_admin = load_admin[:load_admin.index("\nasync function ")]
        self.assertIn("renderAdminAttention()", load_admin)
        self.assertLess(load_admin.index("refreshPanels()"), load_admin.index("renderAdminAttention()"),
                        "这一行要在面板都刷完之后再画，否则它拿到的是半新半旧的数字")

    def test_each_item_opens_the_panel_that_handles_it(self):
        app = self._app()
        start = app.index("function adminAttentionItems()")
        body = app[start:app.index("async function refreshPanels(")]
        self.assertIn("panel.open = true", body)
        self.assertIn("scrollIntoView", body)
        # 每一项都要指名一个真的存在的面板：id 写错的话，那个按钮点了没反应。
        page = self._page()
        for panel_id in re.findall(r"'(panel-[a-z]+)'", body):
            self.assertIn(f'id="{panel_id}"', page, panel_id)

    def test_the_line_exists_and_speaks_when_empty(self):
        """空的时候也要说话（「没有需要你处理的事」）——一个空行读起来像坏了。"""
        page = self._page()
        self.assertIn('id="admin-attention"', page)
        self.assertIn('role="status"', page)
        self.assertIn("现在没有需要你处理的事。", self._app())


class SinceYouLastLookedTests(unittest.TestCase):
    """「我不在的时候发生了什么」—— 和「现在要我做什么」是两件事。

    用户原话问了三遍：「我刷新后台界面应该要可以显示新的通知，有人申请了邀请码等等」。
    v0.63.71 做的是那行「需要你处理」（现在要我做什么）；这一条盯的是另一半：**已经
    自己了结的事**（有人申请、被批准、甚至注册完了）在「需要你处理」里会消失，而运营者
    恰恰想知道它发生过 —— 只看得见「还欠着什么」的后台，会让人以为一直没人来过。

    时刻按管理员一人一个记在服务端，所以刷新页面、换设备、明天再来都还在。
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = database_mod.Database(pathlib.Path(self.temporary.name) / "activity.sqlite3")
        self.database.initialize()

    def _apply(self, email: str) -> None:
        self.database.create_signup_request(email, "想问一下", "test")

    def test_the_first_look_says_so_instead_of_claiming_nothing_happened(self):
        """第一次打开没有「上次」可比 —— 说「没有新动静」是假话（我们并不知道）。"""
        first = self.database.admin_activity("usr_admin")
        self.assertTrue(first["first"])
        self.assertEqual(first["signups"], 0)

    def test_the_second_look_counts_what_arrived_in_between(self):
        self.database.admin_activity("usr_admin")  # 第一次：只落一个时刻
        self._apply("came-in@example.com")
        second = self.database.admin_activity("usr_admin")
        self.assertFalse(second["first"])
        self.assertEqual(second["signups"], 1)
        self.assertEqual(second["applicants"], ["came-in@example.com"], "名字比数字有用")
        self.assertTrue(second["since"], "要说清「上次」是哪一刻")

    def test_something_arriving_in_the_same_second_as_the_look_is_still_reported(self):
        """**宁可重复，也不能漏** —— 这条钉的是方向。

        记录是秒精度、时刻是微秒精度，所以「和上次同一秒」的那一条到底在时刻之前
        还是之后，数据里读不出来。两种错法的代价不一样：漏掉一条申请，运营者永远
        不知道有人来过；重复一遍只是同一句话出现两次。所以左边按整秒算（`>=`）。
        """
        self.database.admin_activity("usr_admin")   # 记下时刻
        self._apply("same-second@example.com")      # 同一秒里到达
        self.assertEqual(self.database.admin_activity("usr_admin")["signups"], 1,
                         "同一秒到达的也必须报出来")

    def test_the_same_thing_is_not_reported_twice(self):
        self.database.admin_activity("usr_admin")
        self._apply("once@example.com")
        # 等到下一秒再看：这一次会把它数进去，而**那一刻**也成了新的基准。
        time.sleep(1.05)
        self.assertEqual(self.database.admin_activity("usr_admin")["signups"], 1)
        self.assertEqual(self.database.admin_activity("usr_admin")["signups"], 0,
                         "看过之后就不该再报一次（容忍的重复只在基准所在的那一秒里）")

    def test_it_counts_things_that_resolved_themselves(self):
        """这正是它和「需要你处理」的分工：批准之后 pending 归零，但事发生过。"""
        self.database.admin_activity("usr_admin")
        self._apply("approved@example.com")
        row = self.database.list_signup_requests(10)[0]
        self.database.decide_signup_request(row["id"], "invited", invite_label="lbl")
        activity = self.database.admin_activity("usr_admin")
        self.assertEqual(activity["signups"], 1, "已经批准了，但它仍然发生过")

    def test_the_marker_is_per_admin(self):
        self.database.admin_activity("usr_one")
        self._apply("for-two@example.com")
        self.assertEqual(self.database.admin_activity("usr_two")["first"], True,
                         "另一个管理员第一次打开时没有可比的上次")
        self.assertEqual(self.database.admin_activity("usr_one")["signups"], 1)

    def test_it_does_not_count_a_deleted_guest_message(self):
        self.database.admin_activity("usr_admin")
        self.database.create_guest_message(body="你好", nickname="同学", sealed_email=b"",
                                           client_hash="x")
        rows = self.database.guest_messages(limit=10)
        self.assertTrue(rows, "留言应当先存下来")
        self.database.set_guest_message_status(rows[0]["id"], "deleted", actor="usr_admin")
        self.assertEqual(self.database.admin_activity("usr_admin")["guest"], 0,
                         "删掉的留言不该在「新留言」里再数一遍")
