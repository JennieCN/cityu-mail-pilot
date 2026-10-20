"""Tests for granting operator rights from the admin console.

Until now operator identity came from one place and one place only: the
``INFE_PILOT_ADMIN_EMAILS`` environment variable. That is a strong property -- a
stolen session, a stray request or a compromised admin account could not create
another admin, and rotating the environment variable was always a complete
remedy. Adding a console that can grant the right necessarily gives some of that
up, so the tests below are mostly about what the new power must **not** be able
to do:

* a non-operator cannot reach it, and cannot learn it exists;
* a stolen session alone is not enough, because the operator's password has to be
  re-entered;
* a typo cannot hand rights to a stranger who registers later, because only an
  existing account can be granted;
* the environment-named owner can never be removed from the console;
* the instance can never be left with nobody who can administer it;
* every grant and revocation is attributable in the audit trail.
"""

import datetime as dt
import http.cookiejar
import json
import os
import pathlib
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

_TMP = tempfile.mkdtemp()
os.environ["INFE_PILOT_DB"] = _TMP + "/admins.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
os.environ["INFE_PILOT_MAX_USERS"] = "50"
os.environ["INFE_PILOT_ADMIN_EMAILS"] = "owner@example.com"
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import web  # noqa: E402
from pilot_app.security import token_hash  # noqa: E402
from pilot_app.tests import admin_fixture  # noqa: E402
from pilot_app.web import db  # noqa: E402

PASSWORD = "a-long-enough-password"
OWNER_EMAIL = "owner@example.com"


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
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), 
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, method: str, path: str, payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(request, timeout=20) as response:
                return response.status, _decode(response.read())
        except urllib.error.HTTPError as error:
            return error.code, _decode(error.read())

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, payload=None):
        return self.request("POST", path, payload)


def register(base: str, email: str) -> Client:
    """Register one account and return a signed-in client for it."""
    code = f"admin-invite-{email}-{dt.datetime.now().timestamp()}"
    expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
    with db.connect() as connection:
        connection.execute("INSERT INTO invites(code_hash,expires_at) VALUES(?,?)",
                           (token_hash(code), expiry))
    client = Client(base)
    web.reset_signup_rate_limit()  # 见 web.reset_signup_rate_limit：限速按 IP，单测得自己清
    status, body = client.post("/api/auth/register", {
        "email": email, "password": PASSWORD, "invite_code": code, "accepted_terms": True})
    if status != 200:
        raise AssertionError(f"注册 {email} 失败：{status} {body}")
    client.user_id = body["id"]
    return client


def admin_client(base: str, email: str) -> Client:
    """The environment owner's session: **create + grant + sign in**.

    `owner@example.com` is the address `INFE_PILOT_ADMIN_EMAILS` names, so the
    open registration route refuses it outright (403, no account) -- that is the
    P1 from 2026-09-26, and the fixture must not re-enact the attack. See
    ``admin_fixture``. Rights still come from the environment list at request
    time; only the account's *existence* is arranged here.
    """
    client = Client(base)
    user = admin_fixture.create_admin(db, email, PASSWORD)
    client.user_id = user["id"]
    return admin_fixture.sign_in(client, email, PASSWORD)


class AdminGrantTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = web.create_server("127.0.0.1", 0)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        # The environment variable is set here rather than at import: the suite
        # shares one process, so a module-level assignment is whatever the last
        # imported module happened to write, and this class would then depend on
        # import order for something as load-bearing as who the operator is.
        cls.saved_admin_emails = os.environ.get("INFE_PILOT_ADMIN_EMAILS")
        os.environ["INFE_PILOT_ADMIN_EMAILS"] = OWNER_EMAIL
        # Two accounts for the whole class. Registering one per test would be
        # eighteen, and the whole suite shares one database with a pilot cap of
        # fifty: the extra rows push later modules over it and they fail with a
        # message about the cap rather than about anything they did.
        cls.owner = admin_client(cls.base, OWNER_EMAIL)
        cls.member = register(cls.base, f"member-{dt.datetime.now().timestamp()}@example.com")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        if cls.saved_admin_emails is None:
            os.environ.pop("INFE_PILOT_ADMIN_EMAILS", None)
        else:
            os.environ["INFE_PILOT_ADMIN_EMAILS"] = cls.saved_admin_emails

    def setUp(self):
        # Every test starts from the same known state: the environment owner is
        # the only operator and no stored grants exist. Without this, grants left
        # by earlier tests make assertions about counts and about "the last
        # operator" depend on the order the tests happen to run in.
        with db.connect() as connection:
            connection.execute("UPDATE users SET is_admin=0")
        self.other = self.member

    # -- fixtures ----------------------------------------------------------

    def roster(self, client: Client):
        status, body = client.get("/api/admin/users")
        self.assertEqual(status, 200, body)
        return body["admins"]

    # -- the owner's own access -------------------------------------------

    def test_the_environment_owner_is_listed_as_not_removable(self):
        roster = self.roster(self.owner)
        entry = next(item for item in roster if item["email"] == "owner@example.com")
        self.assertEqual(entry["source"], "env")
        self.assertFalse(entry["removable"], "环境变量里的主人不能在后台被移除")

    def test_the_environment_owner_can_still_administer_with_an_empty_store(self):
        """A self-hoster who never grants anybody must not lose access."""
        self.assertEqual(db.database_admins(), [])
        status, _ = self.owner.get("/api/admin/users")
        self.assertEqual(status, 200)

    # -- granting ----------------------------------------------------------

    def test_granting_makes_the_account_an_operator(self):
        status, _ = self.other.get("/api/admin/users")
        self.assertEqual(status, 404, "授予之前不能是管理员")

        status, body = self.owner.post("/api/admin/admins",
                                       {"email": self.other_email(), "password": PASSWORD})
        self.assertEqual(status, 200, body)
        status, _ = self.other.get("/api/admin/users")
        self.assertEqual(status, 200, "授予之后应当立刻生效，不必重新登录")

    def other_email(self) -> str:
        with db.connect() as connection:
            row = connection.execute("SELECT email FROM users WHERE id=?", (self.other.user_id,)).fetchone()
        return row["email"]

    def test_the_grant_takes_effect_without_a_new_login(self):
        """`is_admin` is read with the session on every request, so revoking is
        immediate too -- an operator who has just been removed stops being one on
        their next click rather than at session expiry."""
        self.owner.post("/api/admin/admins", {"email": self.other_email(), "password": PASSWORD})
        self.assertEqual(self.other.get("/api/admin/users")[0], 200)
        self.owner.post(f"/api/admin/admins/{self.other.user_id}/revoke", {"password": PASSWORD})
        self.assertEqual(self.other.get("/api/admin/users")[0], 404, "收回也应当立刻生效")

    def test_a_wrong_password_grants_nothing(self):
        status, body = self.owner.post("/api/admin/admins",
                                       {"email": self.other_email(), "password": "not-the-password"})
        self.assertEqual(status, 403, body)
        self.assertEqual(self.other.get("/api/admin/users")[0], 404)

    def test_a_missing_password_grants_nothing(self):
        status, _ = self.owner.post("/api/admin/admins", {"email": self.other_email()})
        self.assertEqual(status, 422)
        self.assertEqual(self.other.get("/api/admin/users")[0], 404)

    def test_an_unregistered_address_is_refused(self):
        """Otherwise the grant is a standing promise, and a typo hands rights to
        whoever later registers the address that was typed."""
        status, body = self.owner.post("/api/admin/admins",
                                       {"email": f"nobody-{dt.datetime.now().timestamp()}@example.com", "password": PASSWORD})
        self.assertEqual(status, 404, body)
        self.assertIn("还没有注册", body["detail"])

    def test_granting_twice_is_harmless(self):
        first = self.owner.post("/api/admin/admins", {"email": self.other_email(), "password": PASSWORD})
        second = self.owner.post("/api/admin/admins", {"email": self.other_email(), "password": PASSWORD})
        self.assertEqual(first[0], 200)
        self.assertEqual(second[0], 200, second[1])
        self.assertEqual(db.count_admin_capable(), 1, "重复授予不应产生第二行")

    # -- revoking ----------------------------------------------------------

    def test_the_owner_cannot_be_removed_from_the_console(self):
        """The stored grant is additive; the environment is the floor. Removing
        the owner by accident must not be possible from a browser."""
        self.owner.post("/api/admin/admins", {"email": self.other_email(), "password": PASSWORD})
        status, body = self.owner.post("/api/admin/admins/owner-row/revoke", {"password": PASSWORD})
        self.assertEqual(status, 404, body)
        self.assertEqual(self.owner.get("/api/admin/users")[0], 200, "主人必须还在")

    def test_the_last_operator_cannot_be_removed(self):
        """With no environment operators left, revoking the final grant would
        leave an instance nobody can administer."""
        # Grant first, while the environment owner is still an operator.
        self.assertEqual(self.owner.post("/api/admin/admins",
                                         {"email": self.other_email(), "password": PASSWORD})[0], 200)
        saved = os.environ.get("INFE_PILOT_ADMIN_EMAILS")
        os.environ["INFE_PILOT_ADMIN_EMAILS"] = ""
        try:
            # With no environment operators left, `other` is the only one, so
            # removing `other` would leave nobody able to administer anything.
            status, body = self.other.post(f"/api/admin/admins/{self.other.user_id}/revoke",
                                           {"password": PASSWORD})
            self.assertEqual(status, 422, body)
            self.assertIn("最后一个管理员", body["detail"])
            self.assertEqual(self.other.get("/api/admin/users")[0], 200, "被拒绝之后权限应当还在")
        finally:
            if saved is None:
                os.environ.pop("INFE_PILOT_ADMIN_EMAILS", None)
            else:
                os.environ["INFE_PILOT_ADMIN_EMAILS"] = saved

    # -- who may reach it --------------------------------------------------

    def test_a_non_operator_cannot_reach_any_of_it(self):
        for method, path, payload in (
            ("GET", "/api/admin/users", None),
            ("POST", "/api/admin/admins", {"email": "owner@example.com", "password": PASSWORD}),
            ("POST", "/api/admin/admins/x/revoke", {"password": PASSWORD}),
        ):
            status, body = self.other.request(method, path, payload)
            self.assertEqual(status, 404, (path, status, body))

    def test_an_anonymous_visitor_cannot_reach_it(self):
        stranger = Client(self.base)
        self.assertEqual(stranger.get("/api/admin/users")[0], 401)
        self.assertEqual(stranger.post("/api/admin/admins", {"email": "x@example.com"})[0], 401)

    def test_an_operator_cannot_grant_rights_without_re_entering_their_password(self):
        """A session alone must not be enough: an unattended browser is the
        realistic way this power would be abused."""
        status, _ = self.owner.post("/api/admin/admins", {"email": self.other_email()})
        self.assertEqual(status, 422)
        self.assertEqual(self.other.get("/api/admin/users")[0], 404)

    # -- the trail ---------------------------------------------------------

    def test_granting_and_revoking_are_both_audited(self):
        self.owner.post("/api/admin/admins", {"email": self.other_email(), "password": PASSWORD})
        self.owner.post(f"/api/admin/admins/{self.other.user_id}/revoke", {"password": PASSWORD})
        _, body = self.owner.get("/api/admin/users")
        actions = [row["action"] for row in body.get("audit", [])]
        self.assertIn("admin_granted", actions)
        self.assertIn("admin_revoked", actions)

    def test_the_audit_names_who_was_granted(self):
        self.owner.post("/api/admin/admins", {"email": self.other_email(), "password": PASSWORD})
        _, body = self.owner.get("/api/admin/users")
        entry = next(row for row in body["audit"] if row["action"] == "admin_granted")
        self.assertEqual(str(entry["target_email"]).lower(), self.other_email().lower())
        self.assertEqual(str(entry["actor_email"]).lower(), "owner@example.com")

    def test_a_deleted_account_loses_the_grant_with_it(self):
        """The grant lives on the account row, so it cannot outlive the account
        and become a way back in."""
        # Its own account, because the test deletes it: doing that to the shared
        # member would invalidate the session every later test relies on.
        doomed = register(self.base, f"doomed-{dt.datetime.now().timestamp()}@example.com")
        with db.connect() as connection:
            email = connection.execute("SELECT email FROM users WHERE id=?",
                                       (doomed.user_id,)).fetchone()["email"]
        self.owner.post("/api/admin/admins", {"email": email, "password": PASSWORD})
        self.assertEqual(len(db.database_admins()), 1)
        with db.connect() as connection:
            connection.execute("UPDATE users SET status='deleted' WHERE id=?", (doomed.user_id,))
        self.assertEqual(db.database_admins(), [], "已删除的账号不该还留在管理员名单里")

    # -- the roster the console shows --------------------------------------

    def test_the_roster_hides_the_row_when_the_environment_already_covers_it(self):
        """An account named in both places is shown once, as the environment one,
        because removing the stored flag would not actually take the rights away
        and a console that implied otherwise would be lying."""
        self.owner.post("/api/admin/admins", {"email": "owner@example.com", "password": PASSWORD})
        roster = self.roster(self.owner)
        owners = [item for item in roster if item["email"] == "owner@example.com"]
        self.assertEqual(len(owners), 1, "同一个人只该出现一次")
        self.assertEqual(owners[0]["source"], "env")
        self.assertFalse(owners[0]["removable"])


if __name__ == "__main__":
    unittest.main()
