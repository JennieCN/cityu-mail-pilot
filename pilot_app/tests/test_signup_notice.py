"""Tests for telling *more* admins when somebody applies for the pilot.

The feature looks like a checkbox list, but the interesting part is what it must
never become. The application notice contains a stranger's address, and the mail
goes out through the operator's own mailbox, so an endpoint that accepted an
arbitrary recipient would be a way to send mail to anyone from the installer's
account. The seven properties below are the ones the handover named, in its
order:

1. with nothing selected the behaviour is **byte-for-byte** today's;
2. ticking a console admin adds him;
3. ticking an installer address does **not** duplicate the letter;
4. a non-admin address is refused (``ValueError`` from the module, 422 over HTTP);
5. revoking an admin removes him **immediately**, without editing a list;
6. the HTTP surface is admin-only (404, not 403), validates its payload, audits;
7. end to end: both letters go out, and no third one does.
"""

from __future__ import annotations

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
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["INFE_PILOT_DB"] = _TMP + "/signup_notice.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
os.environ["INFE_PILOT_MAX_USERS"] = "50"
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import alerting, signup_notice, web  # noqa: E402
from pilot_app.database import Database  # noqa: E402
from pilot_app.security import SecretBox, hash_password, token_hash  # noqa: E402
from pilot_app.tests import admin_fixture  # noqa: E402
from pilot_app.web import db as web_db  # noqa: E402

OWNER = "owner@example.com"
PASSWORD = "a-long-enough-password"
SECRETS = SecretBox.from_environment()


def _set_module_db(database) -> None:
    """Rebind the module-level ``web_db`` (and, at teardown, put the old one back)."""
    global web_db
    web_db = database


def _restore_db_path(saved) -> None:
    if saved is None:
        os.environ.pop("INFE_PILOT_DB", None)
    else:
        os.environ["INFE_PILOT_DB"] = saved


def setUpModule() -> None:
    """Run this whole module against a database of its own.

    Same reason as `test_signup.py` (see the longer note there): the HTTP layer
    reads `web.get_db()`, a process-wide singleton whose file is fixed by the
    first module that calls it, so `SignupNoticeApiTests.setUpClass` used to
    register its two accounts into whatever the suite before it had left behind
    -- and once that file crossed ``INFE_PILOT_MAX_USERS`` the registration came
    back 403 「当前名额已满。」, which is not what a single `skip`-worthy note
    should ever be able to do.

    照 `test_password_reset.py` 的先例：本模块自己建一个库，把 `web.get_db` 指过去，
    跑完全部还原（`INFE_PILOT_DB` 同进程里别的套件也会读，改了必须放回去）。
    """
    database = Database(os.path.join(_TMP, "signup_notice.sqlite3"))
    database.initialize()
    # 注册、登录、后台面板都从 `get_db()` 取库。
    patcher = mock.patch.object(web, "get_db", return_value=database)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    # 服务对象里也攥着一个库（发通知邮件那条路要用它）。先放掉，让它按上面那个
    # `get_db()` 重建；跑完再原样放回 —— 否则万一它是在本模块里第一次建出来的，
    # 就会带着本模块的库活到后面的套件里去。
    saved_service = web._service_singleton
    web._service_singleton = None
    unittest.addModuleCleanup(setattr, web, "_service_singleton", saved_service)
    # 测试自己插邀请码、读写设置走的是模块级的 `web_db`：`from pilot_app.web import db`
    # 在导入那一刻就把当时的那个库绑死了，所以这里必须一起换掉。
    saved_db = web_db
    _set_module_db(database)
    unittest.addModuleCleanup(_set_module_db, saved_db)
    saved_path = os.environ.get("INFE_PILOT_DB")
    os.environ["INFE_PILOT_DB"] = database.path
    unittest.addModuleCleanup(_restore_db_path, saved_path)


class SignupNoticeTestCase(unittest.TestCase):
    """A database of its own: these are pure selection rules, and sharing the
    process-wide web database would make them depend on which other suite ran
    first."""

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.work.name) / "pilot.sqlite3")
        self.db.initialize()
        # The environment list is the floor, and each test sets it explicitly:
        # the whole suite shares one process, so a module-level assignment is
        # whatever the last imported module happened to write.
        self.env = mock.patch.dict("os.environ", {"INFE_PILOT_ADMIN_EMAILS": OWNER}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.work.cleanup)

    def account(self, email: str, *, admin: bool = False, mailbox: bool = True) -> dict:
        invite = self.db.create_invite(f"notice-{email}", 1)
        user = self.db.create_user(email, hash_password(PASSWORD), token_hash(invite))
        if admin:
            self.db.grant_admin(email)
        if mailbox:
            self.db.upsert_mailbox(user["id"], {
                "email": f"box-{email}", "report_to": email,
                "imap_host": "imap.qq.com", "imap_port": 993,
                "smtp_host": "smtp.qq.com", "smtp_port": 465,
                "enabled": True,
                "encrypted_password": SECRETS.encrypt("pw", context=f"mailbox:{user['id']}"),
            })
        return user

    # -- 1. the default ---------------------------------------------------

    def test_nothing_selected_means_only_the_environment_list(self):
        self.account(OWNER)
        self.assertEqual(signup_notice.selected(self.db), [])
        self.assertEqual(signup_notice.extra_recipients(self.db), [])

    def test_the_letter_is_byte_identical_when_nothing_is_selected(self):
        """The claim is not "the same recipients" but "the same send": the
        no-selection path has to produce the same call arguments as the code
        that existed before this feature."""
        self.account(OWNER)
        calls: list = []

        def fake_send(config, password, subject, markdown, **kwargs):
            calls.append((dict(config), password, subject, markdown, kwargs))
            return ["sent"]

        with mock.patch.object(alerting.mailio, "send_report", fake_send):
            delivered = alerting.send_admin_mail(self.db, SECRETS, "主题", "正文")
            before = list(calls)
            calls.clear()
            delivered_after = alerting.send_admin_mail(
                self.db, SECRETS, "主题", "正文",
                also=signup_notice.extra_recipients(self.db))
        self.assertEqual(delivered, [OWNER])
        self.assertEqual(delivered_after, delivered)
        self.assertEqual(before, calls, "没勾人时这次发送必须和以前逐字节相同")

    # -- 2. adding a console admin ---------------------------------------

    def test_a_ticked_console_admin_gets_the_letter_too(self):
        self.account(OWNER)
        self.account("deputy@example.com", admin=True)
        signup_notice.set_selected(self.db, ["deputy@example.com"], actor=OWNER)
        self.assertEqual(signup_notice.selected(self.db), ["deputy@example.com"])
        self.assertEqual(signup_notice.extra_recipients(self.db), ["deputy@example.com"])

    def test_selection_is_normalised_and_deduplicated(self):
        self.account(OWNER)
        self.account("deputy@example.com", admin=True)
        signup_notice.set_selected(self.db, [" Deputy@Example.com ", "deputy@example.com"], actor=OWNER)
        self.assertEqual(signup_notice.extra_recipients(self.db), ["deputy@example.com"])

    # -- 3. the installer is never doubled --------------------------------

    def test_ticking_an_installer_does_not_duplicate_the_letter(self):
        self.account(OWNER)
        signup_notice.set_selected(self.db, [OWNER], actor=OWNER)
        self.assertEqual(signup_notice.selected(self.db), [OWNER], "存下来是可以的")
        self.assertEqual(signup_notice.extra_recipients(self.db), [],
                         "环境里那份已经在收件人里了，不能再加一遍")

    # -- 4. only admins may be named --------------------------------------

    def test_a_stranger_cannot_be_selected(self):
        self.account(OWNER)
        with self.assertRaises(ValueError) as caught:
            signup_notice.set_selected(self.db, ["stranger@example.com"], actor=OWNER)
        self.assertIn("stranger@example.com", str(caught.exception))
        self.assertEqual(self.db.get_setting(signup_notice.SETTING_KEY, ""), "",
                         "被拒绝时不该留下半份名单")

    def test_an_ordinary_account_cannot_be_selected(self):
        """Registered is not the same as admin -- that is the whole boundary."""
        self.account(OWNER)
        self.account("member@example.com")
        with self.assertRaises(ValueError):
            signup_notice.set_selected(self.db, ["member@example.com"], actor=OWNER)

    def test_a_paused_admin_cannot_be_selected(self):
        self.account(OWNER)
        user = self.account("deputy@example.com", admin=True)
        with self.db.connect() as connection:
            connection.execute("UPDATE users SET status='paused' WHERE id=?", (user["id"],))
        with self.assertRaises(ValueError):
            signup_notice.set_selected(self.db, ["deputy@example.com"], actor=OWNER)

    # -- 5. revocation takes effect at read time --------------------------

    def test_revoking_an_admin_stops_his_letter_immediately(self):
        self.account(OWNER)
        user = self.account("deputy@example.com", admin=True)
        signup_notice.set_selected(self.db, ["deputy@example.com"], actor=OWNER)
        self.assertEqual(signup_notice.extra_recipients(self.db), ["deputy@example.com"])

        self.db.revoke_admin(user["id"])
        self.assertEqual(signup_notice.selected(self.db), [],
                         "撤权之后立刻不该再收到，不需要谁记得回来改名单")
        self.assertEqual(signup_notice.extra_recipients(self.db), [])
        self.assertIn("deputy@example.com",
                      self.db.get_setting(signup_notice.SETTING_KEY, ""),
                      "名单本身还在：再授权一次就该自动恢复，不用重勾")

        self.db.grant_admin("deputy@example.com")
        self.assertEqual(signup_notice.extra_recipients(self.db), ["deputy@example.com"])

    def test_a_paused_admin_stops_receiving_too(self):
        self.account(OWNER)
        user = self.account("deputy@example.com", admin=True)
        signup_notice.set_selected(self.db, ["deputy@example.com"], actor=OWNER)
        with self.db.connect() as connection:
            connection.execute("UPDATE users SET status='paused' WHERE id=?", (user["id"],))
        self.assertEqual(signup_notice.selected(self.db), [])

    # -- the list the console draws ---------------------------------------

    def test_candidates_say_who_could_actually_receive(self):
        """``can_receive`` is the difference between "tick me" and a letter that
        never arrives: every send borrows the recipient's own mailbox."""
        owner = self.account(OWNER)
        self.account("deputy@example.com", admin=True)
        self.account("nomailbox@example.com", admin=True, mailbox=False)
        rows = {row["email"]: row for row in signup_notice.candidates(self.db, alerting.admin_emails())}
        self.assertEqual(rows[OWNER]["source"], "env")
        self.assertTrue(rows[OWNER]["can_receive"])
        self.assertEqual(rows["deputy@example.com"]["source"], "database")
        self.assertTrue(rows["deputy@example.com"]["can_receive"])
        self.assertFalse(rows["nomailbox@example.com"]["can_receive"],
                         "没配好邮箱的管理员勾了也收不到，界面必须先说")
        self.assertEqual(sorted(rows), sorted([OWNER, "deputy@example.com", "nomailbox@example.com"]))
        self.assertEqual(owner["email"], OWNER)
        self.assertNotIn("nomailbox@example.com", signup_notice.selected(self.db))


class SignupNoticeApiTests(unittest.TestCase):
    """The HTTP surface, against a real server -- 404/422 are the product here."""

    @classmethod
    def setUpClass(cls):
        cls.server = web.create_server("127.0.0.1", 0)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        # Set here rather than at import: the whole suite shares one process, so
        # a module-level assignment is whatever the last imported module wrote.
        cls.saved_admin_emails = os.environ.get("INFE_PILOT_ADMIN_EMAILS")
        os.environ["INFE_PILOT_ADMIN_EMAILS"] = OWNER
        cls.owner = admin_client(cls.base, OWNER)
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
        with web_db.connect() as connection:
            connection.execute("UPDATE users SET is_admin=0")
        web_db.delete_setting(signup_notice.SETTING_KEY)

    def member_email(self) -> str:
        with web_db.connect() as connection:
            row = connection.execute("SELECT email FROM users WHERE id=?",
                                     (self.member.user_id,)).fetchone()
        return row["email"]

    # -- who may reach it --------------------------------------------------

    def test_a_non_operator_cannot_reach_it(self):
        status, body = self.member.request("PUT", "/api/admin/signup-notice",
                                           {"admins": [self.member_email()]})
        self.assertEqual(status, 404, body)
        self.assertNotIn("signup", json.dumps(body), "普通用户不该知道这个界面存在")

    def test_an_anonymous_visitor_cannot_reach_it(self):
        stranger = Client(self.base)
        self.assertEqual(stranger.request("PUT", "/api/admin/signup-notice",
                                          {"admins": [OWNER]})[0], 401)

    # -- payload validation ------------------------------------------------

    def test_a_non_admin_address_is_refused_with_422(self):
        status, body = self.owner.request("PUT", "/api/admin/signup-notice",
                                          {"admins": ["stranger@example.com"]})
        self.assertEqual(status, 422, body)
        self.assertIn("stranger@example.com", body["detail"])
        self.assertEqual(web_db.get_setting(signup_notice.SETTING_KEY, ""), "")

    def test_admins_must_be_a_list_of_strings(self):
        for payload in ({"admins": "deputy@example.com"},
                        {"admins": [123]},
                        {"admins": None},
                        {}):
            status, body = self.owner.request("PUT", "/api/admin/signup-notice", payload)
            self.assertEqual(status, 422, (payload, status, body))

    # -- saving, auditing, and reading back --------------------------------

    def test_saving_is_audited_and_read_back(self):
        web_db.grant_admin(self.member_email())
        status, body = self.owner.request("PUT", "/api/admin/signup-notice",
                                          {"admins": [self.member_email()]})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["selected"], [self.member_email()])
        self.assertIn(self.member_email(),
                      [row["email"] for row in body["candidates"]])

        status, panel = self.owner.get("/api/admin/users")
        self.assertEqual(status, 200)
        self.assertEqual(panel["signup_notification"]["selected"], [self.member_email()])
        self.assertIn(OWNER, panel["signup_notification"]["installers"])
        entry = next(row for row in panel["audit"] if row["action"] == "signup_notice_updated")
        self.assertEqual(str(entry["actor_email"]).lower(), OWNER)
        self.assertIn(self.member_email(), str(entry["detail"]))

    def test_clearing_the_list_keeps_the_installer(self):
        """The endpoint only ever adds: an empty list means "nobody extra", and
        the environment list keeps receiving -- it cannot be revoked here."""
        web_db.grant_admin(self.member_email())
        self.owner.request("PUT", "/api/admin/signup-notice", {"admins": [self.member_email()]})
        status, body = self.owner.request("PUT", "/api/admin/signup-notice", {"admins": []})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["selected"], [])
        _, panel = self.owner.get("/api/admin/users")
        self.assertIn(OWNER, panel["signup_notification"]["installers"],
                      "装机器的人永远收得到，清空名单也撤不掉他")

    def test_a_revoked_admin_disappears_from_the_read_back_too(self):
        web_db.grant_admin(self.member_email())
        self.owner.request("PUT", "/api/admin/signup-notice", {"admins": [self.member_email()]})
        with web_db.connect() as connection:
            connection.execute("UPDATE users SET is_admin=0 WHERE id=?", (self.member.user_id,))
        _, panel = self.owner.get("/api/admin/users")
        self.assertEqual(panel["signup_notification"]["selected"], [])
        self.assertNotIn(self.member_email(),
                         [row["email"] for row in panel["signup_notification"]["candidates"]])

    # -- 7. end to end -----------------------------------------------------

    def test_both_letters_go_out_and_no_third_one(self):
        """The whole point, at the seam where it is easy to get wrong: one tick
        must produce exactly one more letter -- not a replacement and not a
        duplicate."""
        web_db.grant_admin(self.member_email())
        self._give_mailbox(OWNER)
        self._give_mailbox(self.member_email())
        status, body = self.owner.request("PUT", "/api/admin/signup-notice",
                                          {"admins": [self.member_email()]})
        self.assertEqual(status, 200, body)

        sent: list = []

        def fake_send(config, password, subject, markdown, **kwargs):
            sent.append(config["report_to"])
            return ["sent"]

        row = {"email": f"applicant-{dt.datetime.now().timestamp()}@example.com",
               "note": "", "created_at": "2026-09-20T00:00:00+00:00", "ip": "203.0.113.9",
               "client": "pytest"}
        with mock.patch.object(alerting.mailio, "send_report", fake_send):
            web._notify_new_signup(row)

        self.assertEqual(sorted(sent), sorted([OWNER, self.member_email()]),
                         "环境里的运营者与勾上的管理员各收到一封")
        self.assertEqual(len(sent), 2, "没有第三封")

    def _give_mailbox(self, email: str) -> None:
        user = web_db.find_user_for_login(email)
        if not user:
            raise AssertionError(f"测试账号不存在：{email}")
        web_db.upsert_mailbox(user["id"], {
            "email": f"box-{email}", "report_to": email,
            "imap_host": "imap.qq.com", "imap_port": 993,
            "smtp_host": "smtp.qq.com", "smtp_port": 465, "enabled": True,
            "encrypted_password": SECRETS.encrypt("pw", context=f"mailbox:{user['id']}"),
        })


# --------------------------------------------------------------------------
# helpers, copied in shape from test_admin_grant.py -- one process, one server
# --------------------------------------------------------------------------


def _decode(raw: bytes):
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw.decode("utf-8", "replace")


def tearDownModule() -> None:
    """这个函数体是空的，但**它必须存在**。

    CPython 3.9 的 `unittest.suite.TestSuite._handleModuleTearDown` 把
    `doModuleCleanups()` 写在了 `if tearDownModule is not None:` **里面**，所以一个模块
    只要没定义 `tearDownModule`，`addModuleCleanup()` 注册的清理就**一次都不会跑**。
    上面的 `setUpModule` 正是用 `addModuleCleanup` 把 `web.get_db` 换回原样的 ——
    清理不跑，那个 mock 就活到整轮结束：后面的套件（`test_tasks` / `test_web`）从
    `get_db()` 拿到的是**本模块的库**，而它们自己的 `db` 还是进程里那个单例，
    于是出现「邀请码无效」/「名额已满」这种与服务端逻辑毫无关系的红。
    2026-09-23 在 `python -m unittest discover` 上实测：去掉这个函数，套件必红；
    加上它就绿。（3.10+ 已把 `doModuleCleanups()` 挪到 `if` 外面，多这一个空函数无害。）
    """


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


def admin_client(base: str, email: str) -> Client:
    """管理员的会话：**建号 + 授权 + 登录**，不经过开放注册（见 admin_fixture）。

    `owner@example.com` 正是 `INFE_PILOT_ADMIN_EMAILS` 点名的保留地址，注册端点
    现在对它一律 403 —— 那正是 2026-09-26 那条 P1 堵掉的路。
    """
    client = Client(base)
    user = admin_fixture.create_admin(web_db, email, PASSWORD)
    client.user_id = user["id"]
    return admin_fixture.sign_in(client, email, PASSWORD)


def register(base: str, email: str) -> Client:
    code = f"signup-notice-{email}-{dt.datetime.now().timestamp()}"
    expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
    with web_db.connect() as connection:
        connection.execute("INSERT INTO invites(code_hash,expires_at) VALUES(?,?)",
                           (token_hash(code), expiry))
    client = Client(base)
    web.reset_signup_rate_limit()  # 见 web.reset_signup_rate_limit：限速按 IP，单测得自己清
    status, body = client.request("POST", "/api/auth/register", {
        "email": email, "password": PASSWORD, "invite_code": code, "accepted_terms": True})
    if status != 200:
        raise AssertionError(f"注册 {email} 失败：{status} {body}")
    client.user_id = body["id"]
    return client


if __name__ == "__main__":
    unittest.main()
