"""Tests for the public landing page and the pilot application flow.

The landing page is new surface area of a specific kind: **the only
unauthenticated write in the API**. So most of what follows is about the
boundary -- an application must be storable and reviewable without ever being
able to grant access, and the public endpoint must not become a way to probe
which addresses are already registered or to hammer the operator.
"""

import datetime as dt
import http.cookiejar
import json
import os
import re
import pathlib
import tempfile
import threading
import unittest
from unittest import mock
import urllib.error
import urllib.request

_TMP = tempfile.mkdtemp()
os.environ["INFE_PILOT_DB"] = _TMP + "/signup.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
os.environ["INFE_PILOT_MAX_USERS"] = "50"
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import database as database_mod  # noqa: E402
from pilot_app import invites  # noqa: E402
from pilot_app import web  # noqa: E402
from pilot_app.security import token_hash  # noqa: E402
from pilot_app.tests import admin_fixture  # noqa: E402
from pilot_app.web import db  # noqa: E402


def _set_module_db(database) -> None:
    """Rebind the module-level ``db`` (and, at teardown, put the old one back)."""
    global db
    db = database


def _restore_db_path(saved) -> None:
    if saved is None:
        os.environ.pop("INFE_PILOT_DB", None)
    else:
        os.environ["INFE_PILOT_DB"] = saved


def setUpModule() -> None:
    """Run this whole module against a database of its own.

    ``web.get_db()`` is a process-wide singleton, so under ``discover`` the file
    behind it is whichever module *called* it first (``test_admin``) and every
    module after that shares it. Nothing about that is visible until the shared
    file crosses ``INFE_PILOT_MAX_USERS``: then registering here answers
    「当前名额已满。」 and this module fails for a reason that has nothing to do
    with signup. 2026-09-22 was that day -- registration no longer requires an
    invite code, so the shared file fills up faster than it used to.

    照 `test_password_reset.py` 的先例：本模块自己建一个库，把 `web.get_db` 指过去，
    跑完全部还原（`INFE_PILOT_DB` 同进程里别的套件也会读，改了必须放回去）。
    """
    database = database_mod.Database(os.path.join(_TMP, "signup-web.sqlite3"))
    database.initialize()
    # 注册、登录、申请、后台面板都从 `get_db()` 取库。
    patcher = mock.patch.object(web, "get_db", return_value=database)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)
    # 服务对象里也攥着一个库（批准申请时用它找发件邮箱）。先放掉，让它按上面那个
    # `get_db()` 重建；跑完再原样放回 —— 否则万一它是在本模块里第一次建出来的，
    # 就会带着本模块的库活到后面的套件里去。
    saved_service = web._service_singleton
    web._service_singleton = None
    unittest.addModuleCleanup(setattr, web, "_service_singleton", saved_service)
    # 测试自己插邀请码、读申请走的是模块级的 `db`：`from pilot_app.web import db` 在
    # 导入那一刻就把当时的那个库绑死了，所以这里必须一起换掉。
    saved_db = db
    _set_module_db(database)
    unittest.addModuleCleanup(_set_module_db, saved_db)
    saved_path = os.environ.get("INFE_PILOT_DB")
    os.environ["INFE_PILOT_DB"] = database.path
    unittest.addModuleCleanup(_restore_db_path, saved_path)


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


def _decode(raw: bytes):
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw.decode("utf-8", "replace")


class SignupTests(unittest.TestCase):
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
        # Every test here talks to the server from 127.0.0.1, and the endpoint
        # is throttled per client -- so without this the sixth test onwards
        # would see 429 and the suite would be testing the throttle rather than
        # the feature. Each test gets a clean budget; the throttle itself is
        # covered by its own test.
        web._signup_attempts.clear()
        self.stamp = dt.datetime.now().timestamp()
        self.client = Client(self.base)

    def _apply(self, email: str, note: str = ""):
        return self.client.post("/api/signup", {"email": email, "note": note})

    def _as_admin(self) -> Client:
        """一个管理员会话，**不经过开放注册**（见 admin_fixture）。

        以前这里先把 `INFE_PILOT_ADMIN_EMAILS` 指到一个新地址、再用那个地址注册 ——
        那正是 2026-09-26 那条 P1 攻击路径（注册不验证邮箱归属，谁先注册谁是管理员），
        所以注册端点现在对保留地址一律 403。改成建号 + 授权：权限同样是真的，
        但走的是后台「授权」那条路。
        """
        client = Client(self.base)
        return admin_fixture.admin_session(db, client, f"signup-boss-{self.stamp}@example.com")

    def tearDown(self):
        os.environ.pop("INFE_PILOT_ADMIN_EMAILS", None)

    # -- the landing page --------------------------------------------------

    def test_the_landing_page_is_served_at_the_root(self):
        status, body, headers = self.client.get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn("CityU Mail Pilot", body)
        self.assertIn('id="apply"', body)

    def test_the_landing_page_is_indexable(self):
        """A page nobody can find is a page nobody reads."""
        _, body, _ = self.client.get("/")
        self.assertIn("<title>", body)
        self.assertIn('name="description"', body)
        self.assertIn('property="og:title"', body)

    def test_the_landing_page_needs_no_javascript_to_be_read(self):
        """The copy is server-rendered; the script only submits the form."""
        _, body, _ = self.client.get("/")
        self.assertIn("<h1>", body)
        self.assertIn("只读", body)
        self.assertNotIn("<script>", body)

    def test_the_landing_script_is_a_separate_file(self):
        """CSP is script-src 'self'; an inline block would vanish silently."""
        _, body, _ = self.client.get("/")
        self.assertIn('<script src="/landing.js" defer></script>', body)
        status, script, headers = self.client.get("/landing.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers.get("Content-Type", ""))
        self.assertIn("standalone", script, "安装态直通应用的逻辑不见了")

    def test_the_web_process_logs_at_info_level(self):
        """Otherwise the record of a send is written and thrown away.

        Only worker.py configured logging, so every logging.info() in the web
        process -- including "invite emailed to ..." -- went to a logger still at
        WARNING and vanished. The send could have worked perfectly and left no
        evidence, which is indistinguishable from never having run.
        """
        import logging

        root = logging.getLogger()
        saved_level, saved_handlers = root.level, list(root.handlers)
        try:
            root.handlers.clear()
            root.setLevel(logging.WARNING)
            with mock.patch.dict(os.environ, {"LOG_LEVEL": "INFO"}):
                web.configure_logging()
            self.assertLessEqual(root.level, logging.INFO,
                                 "web 进程必须让 INFO 日志真的写出来")
        finally:
            root.setLevel(saved_level)
            root.handlers[:] = saved_handlers

    def test_the_approval_path_names_the_invite_in_its_log(self):
        """The line an operator greps for when asking whether a code went out.

        v0.63.72 moved the send itself into `pilot_app/invites.py` (the worker's
        plan-B paths put the same message on the wire), so the strings live there
        now. What this test is about has not changed: whichever module sends an
        invite has to say so in the journal, in words somebody would search for --
        and the approval path must still reach it through that one place.
        """
        sender = pathlib.Path(invites.__file__).read_text(encoding="utf-8")
        self.assertIn("invite emailed to", sender)
        self.assertIn("could not email the invite", sender)
        approval = pathlib.Path(web.__file__).read_text(encoding="utf-8")
        self.assertIn("invites_mod.issue_and_send(", approval,
                      "批准那条路必须走共用的那一份（否则三条路会各自漂）")

    def test_an_invite_approval_records_the_send_on_the_application(self):
        """The durable record, which survives a log rotation and is queryable."""
        admin = self._as_admin()
        email = f"recorded-{self.stamp}@example.com"
        self._apply(email)
        request_id = self._first_pending_id(email)
        with mock.patch.object(web.alerting, "send_as_operator",
                               return_value={"from": "operator@example.com",
                                             "message_id": "<recorded@example.com>", "refused": {}}):
            status, body, _ = admin.post(f"/api/admin/signups/{request_id}", {"status": "invited"})
        self.assertEqual(status, 200, body)
        row = next(item for item in db.list_signup_requests(200) if item["email"] == email)
        self.assertTrue(row["invite_sent_at"], "发送成功必须落库")
        self.assertEqual(row["invite_message_id"], "<recorded@example.com>")
        self.assertEqual(row["invite_send_error"], "")

    def test_a_refused_recipient_is_recorded_as_a_failure(self):
        """smtplib only raises when *every* recipient is refused; a partial
        refusal comes back as a map, and treating that as success would record a
        delivery that did not happen."""
        admin = self._as_admin()
        email = f"refused-{self.stamp}@example.com"
        self._apply(email)
        request_id = self._first_pending_id(email)
        with mock.patch.object(web.alerting, "send_as_operator",
                               return_value={"from": "operator@example.com",
                                             "message_id": "<x@example.com>",
                                             "refused": {email: (550, b"mailbox unavailable")}}):
            status, body, _ = admin.post(f"/api/admin/signups/{request_id}", {"status": "invited"})
        self.assertEqual(status, 200, body)
        self.assertFalse(body["emailed"], "被拒收不能算发送成功")
        row = next(item for item in db.list_signup_requests(200) if item["email"] == email)
        self.assertEqual(row["invite_sent_at"], "")
        self.assertIn("收件人被拒绝", row["invite_send_error"])

    def test_the_app_moved_to_app_and_the_root_is_not_it(self):
        status, app, _ = self.client.get("/app")
        self.assertEqual(status, 200)
        self.assertIn('id="dashboard"', app)
        _, landing, _ = self.client.get("/")
        self.assertNotIn('id="dashboard"', landing)

    def test_the_manifest_launches_the_app_not_the_marketing_page(self):
        status, body, _ = self.client.get("/manifest.webmanifest")
        self.assertEqual(status, 200)
        self.assertEqual(body["start_url"], "/app")

    # -- the number on the landing page ------------------------------------

    def _add_user(self, uid: str, *, status: str = "active", mailbox: bool = True,
                  enabled: int = 1) -> None:
        """One account, optionally with a mailbox, for the count tests.

        Deltas are compared rather than absolute totals: this class shares one
        database with every other test in the module, so the baseline is not a
        number this test gets to choose.
        """
        email = f"{uid}-{self.stamp}@example.com"
        with db.connect() as connection:
            connection.execute(
                """INSERT INTO users(id,email,password_hash,status,created_at)
                   VALUES(?,?, 'h',?,'2026-09-14T00:00:00+00:00')""", (uid, email, status))
            if mailbox:
                connection.execute(
                    """INSERT INTO mailboxes(id,user_id,email,report_to,imap_host,imap_port,
                           smtp_host,smtp_port,encrypted_password,enabled,updated_at)
                       VALUES(?,?,?,?, 'h',993,'h',465,X'00',?,'2026-09-14T00:00:00+00:00')""",
                    (f"mbx-{uid}", uid, email, email, enabled))

    def test_the_landing_page_never_shows_a_placeholder(self):
        """If the substitution ever stops happening, the page must fail a test
        rather than quietly publish `{{SOURCE_LINK}}` to strangers.

        （举的例子以前是 `{{PILOT_COUNT}}`；那句账号数 2026-09-24 随 PR #10 从页面上
        删掉了，占位符与注入点一起撤了。这里换成还在用的那个，判据一个字没改。）"""
        _, body, _ = self.client.get("/")
        self.assertNotIn("{{", body)
        # 找的是**没被替换掉的占位符**，而不是任意两个花括号：介绍页的样式是内联的，
        # `@media (min-width:900px){ .hero{…} }` 这种嵌套规则天然带 `}}`（2026-09-22
        # 那次外观重做把它撞出来了）。`{{NAME}}` 这种形状才是要拦的东西。
        leftovers = re.findall(r"\{\{\s*[A-Za-z_][A-Za-z0-9_]*\s*\}\}", body)
        self.assertEqual(leftovers, [], f"页面里有没替换掉的占位符：{leftovers[:5]}")

    def test_the_landing_page_no_longer_publishes_an_account_count(self):
        """2026-09-24：浅色创建账号卡删除后，账号数量行也不再公开显示。"""
        _, before, _ = self.client.get("/")
        self._add_user(f"counted{int(self.stamp)}")
        _, after, _ = self.client.get("/")
        self.assertEqual(before, after, "删掉数量行后，加账号不应再改变官网正文")
        self.assertNotIn("个账号接好了邮箱", after)

    def test_a_registered_account_that_never_set_up_a_mailbox_is_not_counted(self):
        """Registering is a few seconds of work that commits nobody."""
        baseline = db.landing_user_count()
        self._add_user(f"bare{int(self.stamp)}", mailbox=False)
        self.assertEqual(db.landing_user_count(), baseline)

    def test_a_paused_account_is_not_counted(self):
        baseline = db.landing_user_count()
        self._add_user(f"paused{int(self.stamp)}", status="paused")
        self.assertEqual(db.landing_user_count(), baseline)

    def test_an_account_that_turned_its_mailbox_off_is_not_counted(self):
        baseline = db.landing_user_count()
        self._add_user(f"off{int(self.stamp)}", enabled=0)
        self.assertEqual(db.landing_user_count(), baseline)

    def test_a_fully_set_up_account_does_count(self):
        """The other half of the rule: an over-eager definition would read as
        modesty but is the same thing as an untrue number."""
        baseline = db.landing_user_count()
        self._add_user(f"ready{int(self.stamp)}")
        self.assertEqual(db.landing_user_count(), baseline + 1)

    # -- applying ----------------------------------------------------------

    def test_anyone_can_apply(self):
        status, body, _ = self._apply(f"apply-{self.stamp}@example.com", "想试试")
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        self.assertFalse(body["already"])

    def test_applying_twice_says_so_instead_of_failing(self):
        email = f"twice-{self.stamp}@example.com"
        self._apply(email)
        status, body, _ = self._apply(email)
        self.assertEqual(status, 200, body)
        self.assertTrue(body["already"], "重复申请应当被识别，而不是报错")

    def test_the_optional_questions_are_stored_and_shown_to_the_operator(self):
        """设计稿里那三栏（怎么称呼你 / 身份 / 最想先解决什么）是**选填**的补充信息。

        它们唯一的用途是让运营者在面板上多知道一点，所以这一条钉两件事：**存下来了**、
        而且**空着也照收**——申请表不是把陌生人挡在外面的地方。
        """
        email = f"asked-{self.stamp}@example.com"
        status, body, _ = self.client.post("/api/signup", {
            "email": email, "note": "选课总漏",
            "nickname": "  小明  ", "identity": "本科生",
            "goals": ["错过截止时间", "找不到要办的事"]})
        self.assertEqual(status, 200, body)
        with db.connect() as connection:
            row = connection.execute(
                "SELECT nickname,identity,goals FROM signup_requests WHERE email=?",
                (email,)).fetchone()
        self.assertEqual(row["nickname"], "小明", "两头的空格要去掉")
        self.assertEqual(row["identity"], "本科生")
        self.assertEqual(row["goals"], "错过截止时间、找不到要办的事")

        bare = f"bare-{self.stamp}@example.com"
        status, body, _ = self.client.post("/api/signup", {"email": bare})
        self.assertEqual(status, 200, body)
        with db.connect() as connection:
            row = connection.execute(
                "SELECT nickname,identity,goals FROM signup_requests WHERE email=?",
                (bare,)).fetchone()
        self.assertEqual((row["nickname"], row["identity"], row["goals"]), ("", "", ""),
                         "一个字都不填也要能申请")

    def test_an_unknown_identity_or_goal_is_refused_rather_than_dropped(self):
        """固定集合才能统计。不认识的选项**拒绝**，而不是静默丢掉——
        静默丢掉会让填的人以为我们收到了，而面板上什么都没有。"""
        email = f"junk-{self.stamp}@example.com"
        status, body, _ = self.client.post("/api/signup", {"email": email, "identity": "本科生 "})
        self.assertEqual(status, 200, f"两边空格而已，应当照收：{body}")
        status, body, _ = self.client.post(
            "/api/signup", {"email": email, "identity": "旁听生"})
        self.assertEqual(status, 422, body)
        status, body, _ = self.client.post(
            "/api/signup", {"email": email, "goals": ["错过截止时间", "别的东西"]})
        self.assertEqual(status, 422, body)
        status, body, _ = self.client.post(
            "/api/signup", {"email": email,
                            "goals": ["错过截止时间", "通知太多", "分不清轻重",
                                      "找不到要办的事", "第五个"]})
        self.assertEqual(status, 422, f"超过四个选项也要拒：{body}")

    def test_the_landing_page_no_longer_embeds_a_form(self):
        """2026-09-22：首页那一节只剩一张卡 + 一个按钮，表单与自助重发一起下线。

        接口本身留着（`POST /api/signup` 仍然是合法的未认证写入，历史工具与这套
        测试都在用），所以这里钉的是**界面**：两个表单都不在页面上，按钮在，而且
        指向应用而不是接口。见 `docs/open-registration-2026-09-22.md`。
        """
        _, page, _ = self.client.get("/")
        for gone in ('id="signup-form"', 'id="signup-nickname"', 'id="resend-form"',
                     'action="/api/signup"', 'action="/api/invite/resend"'):
            self.assertNotIn(gone, page, f"首页又嵌回了 {gone}")
        section = page[page.index('id="apply"'):page.index('id="download"')]
        self.assertIn('href="/app"', section)
        self.assertIn('class="btn"', section)

    def test_an_application_creates_no_account_and_no_invite(self):
        """The whole safety property: applying is a request, not access."""
        with db.connect() as connection:
            before_users = connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            before_invites = connection.execute("SELECT COUNT(*) FROM invites").fetchone()[0]
        email = f"nobody-{self.stamp}@example.com"
        self._apply(email, "试试看能不能白拿一个号")
        with db.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM users").fetchone()[0], before_users)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM invites").fetchone()[0], before_invites)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM users WHERE email=?", (email,)).fetchone()[0], 0)

    def test_a_bad_address_is_refused(self):
        # 422 is this API's code for a field that failed validation.
        for bad in ("not-an-email", "@example.com", "a@", ""):
            status, _, _ = self._apply(bad)
            self.assertEqual(status, 422, bad)

    def test_the_response_does_not_leak_whether_an_account_exists(self):
        """A public endpoint must not become an address-enumeration oracle."""
        status, body, _ = self._apply(f"unknown-{self.stamp}@example.com")
        self.assertEqual(status, 200)
        self.assertEqual(set(body.keys()), {"ok", "already"},
                         "回复里多带了字段，可能泄露账号是否存在")

    def test_an_absurdly_long_note_is_refused_with_a_reason(self):
        """Refusing beats silently truncating: the sender can see what happened,
        and the store keeps its own cap as a second line of defence."""
        status, body, _ = self._apply(f"longnote-{self.stamp}@example.com", "x" * 5000)
        self.assertEqual(status, 422, body)
        with db.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM signup_requests WHERE note LIKE 'xxx%'").fetchone()[0], 0)

    def test_the_public_form_is_rate_limited(self):
        """It is the only unauthenticated write, so it is the one that needs a
        ceiling. Five an hour per client is plenty for a person filling a form."""
        seen = []
        for index in range(8):
            status, _, _ = self._apply(f"flood-{self.stamp}-{index}@example.com")
            seen.append(status)
        self.assertIn(429, seen, f"连续提交没有被限流：{seen}")

    def test_an_application_cannot_be_read_back_anonymously(self):
        """Applications are only visible through the operator overview, and
        there is deliberately no standalone GET route to list them."""
        self._apply(f"private-{self.stamp}@example.com")
        status, body, _ = self.client.get("/api/admin/users")
        self.assertEqual(status, 401)
        self.assertNotIn("private-", json.dumps(body, ensure_ascii=False))
        status, _, _ = self.client.get("/api/admin/signups")
        self.assertEqual(status, 404, "不该存在一个列出申请的 GET 路由")

    # -- reviewing ---------------------------------------------------------

    def test_only_an_operator_can_decide(self):
        status, body, _ = self._apply(f"decide-{self.stamp}@example.com")
        request_id = self._first_pending_id(f"decide-{self.stamp}@example.com")
        anonymous = Client(self.base)
        status, _, _ = anonymous.post(f"/api/admin/signups/{request_id}", {"status": "invited"})
        self.assertEqual(status, 401)

    def test_approving_mints_a_working_invite_exactly_once(self):
        admin = self._as_admin()
        email = f"approved-{self.stamp}@example.com"
        self._apply(email)
        request_id = self._first_pending_id(email)

        status, body, _ = admin.post(f"/api/admin/signups/{request_id}", {"status": "invited"})
        self.assertEqual(status, 200, body)
        code = body["code"]
        self.assertTrue(code, "批准之后必须给出邀请码")
        self.assertEqual(body["signup"]["status"], "invited")

        # The code actually works.
        fresh = Client(self.base)
        status, user, _ = fresh.post("/api/auth/register", {
            "email": email, "password": "a-long-enough-password",
            "invite_code": code, "accepted_terms": True,
        })
        self.assertEqual(status, 200, user)

        # And only once: it is single-use like every other invite.
        second = Client(self.base)
        status, _, _ = second.post("/api/auth/register", {
            "email": f"again-{self.stamp}@example.com", "password": "a-long-enough-password",
            "invite_code": code, "accepted_terms": True,
        })
        self.assertEqual(status, 400, "邀请码只能用一次")

    def test_approving_emails_the_code_to_the_applicant(self):
        """The operator clicks once; the applicant gets the code in their inbox.

        Without this the operator had to copy the code into their own mail
        client, and the whole point of the application form was to remove a
        manual step from the operator's day.
        """
        admin = self._as_admin()
        email = f"mailed-{self.stamp}@example.com"
        self._apply(email)
        request_id = self._first_pending_id(email)
        sent = []
        with mock.patch.object(web.alerting, "send_as_operator",
                               side_effect=lambda db, secrets, to, subject, body, **kw:
                               sent.append((to, subject, body)) or {
                                   "from": "operator@example.com",
                                   "message_id": "<fixed-for-test@example.com>", "refused": {}}):
            status, body, _ = admin.post(f"/api/admin/signups/{request_id}", {"status": "invited"})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["emailed"], body)
        self.assertEqual(len(sent), 1, "应当只发一封")
        to, subject, text = sent[0]
        self.assertEqual(to, email)
        self.assertIn("邀请码", subject)
        self.assertIn(body["code"], text, "邮件里必须带上邀请码本身")
        self.assertIn("/app", text, "要告诉对方去哪里注册")

    def test_the_invite_email_states_who_pays_and_where_the_mail_goes(self):
        """The applicant may never open the site again, so the two facts that
        matter have to be in the message itself."""
        admin = self._as_admin()
        email = f"disclose-{self.stamp}@example.com"
        self._apply(email)
        request_id = self._first_pending_id(email)
        sent = []
        with mock.patch.object(web.alerting, "send_as_operator",
                               side_effect=lambda db, secrets, to, subject, body, **kw:
                               sent.append(body) or {
                                   "from": "operator@example.com",
                                   "message_id": "<fixed-for-test@example.com>", "refused": {}}):
            admin.post(f"/api/admin/signups/{request_id}", {"status": "invited"})
        text = sent[0]
        self.assertIn("管理员", text, "必须说明在另行通知前谁付费")
        self.assertIn("自己的 key", text, "必须说明可以换成自己的 key")
        self.assertIn("以原始邮件为准", text, "必须说明 AI 会出错")
        self.assertIn("立即清空", text, "必须说明正文不长期保存")

    def test_a_failed_send_still_gives_the_operator_the_code(self):
        """Losing a freshly minted single-use code to an SMTP hiccup would be
        worse than losing the e-mail."""
        admin = self._as_admin()
        email = f"sendfail-{self.stamp}@example.com"
        self._apply(email)
        request_id = self._first_pending_id(email)
        with mock.patch.object(web.alerting, "send_as_operator",
                               side_effect=RuntimeError("smtp 挂了")):
            status, body, _ = admin.post(f"/api/admin/signups/{request_id}", {"status": "invited"})
        self.assertEqual(status, 200, body)
        self.assertFalse(body["emailed"])
        self.assertIn("smtp", body["email_error"])
        self.assertTrue(body["code"], "发信失败也必须把码交给运营者")

    def test_the_operator_can_skip_the_email(self):
        admin = self._as_admin()
        email = f"noemail-{self.stamp}@example.com"
        self._apply(email)
        request_id = self._first_pending_id(email)
        with mock.patch.object(web.alerting, "send_as_operator") as sender:
            status, body, _ = admin.post(f"/api/admin/signups/{request_id}",
                                         {"status": "invited", "email": False})
        self.assertEqual(status, 200, body)
        self.assertFalse(body["emailed"])
        sender.assert_not_called()
        self.assertTrue(body["code"])

    def test_declining_never_sends_mail(self):
        admin = self._as_admin()
        email = f"nosend-{self.stamp}@example.com"
        self._apply(email)
        request_id = self._first_pending_id(email)
        with mock.patch.object(web.alerting, "send_as_operator") as sender:
            admin.post(f"/api/admin/signups/{request_id}", {"status": "declined"})
        sender.assert_not_called()

    def test_declining_hands_out_nothing(self):
        admin = self._as_admin()
        email = f"declined-{self.stamp}@example.com"
        self._apply(email)
        request_id = self._first_pending_id(email)
        status, body, _ = admin.post(f"/api/admin/signups/{request_id}", {"status": "declined"})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["code"], "", "婉拒不该产生邀请码")
        self.assertEqual(body["signup"]["status"], "declined")

    def test_a_declined_address_may_apply_again(self):
        """Otherwise a mis-click locks someone out permanently."""
        admin = self._as_admin()
        email = f"retry-{self.stamp}@example.com"
        self._apply(email)
        request_id = self._first_pending_id(email)
        admin.post(f"/api/admin/signups/{request_id}", {"status": "declined"})
        status, body, _ = self._apply(email)
        self.assertEqual(status, 200, body)
        self.assertFalse(body["already"], "被婉拒之后应该能重新申请")

    def test_an_unknown_request_is_a_404(self):
        admin = self._as_admin()
        status, _, _ = admin.post("/api/admin/signups/sgn_does_not_exist", {"status": "invited"})
        self.assertEqual(status, 404)

    def test_a_bad_status_is_refused(self):
        admin = self._as_admin()
        email = f"badstatus-{self.stamp}@example.com"
        self._apply(email)
        request_id = self._first_pending_id(email)
        status, _, _ = admin.post(f"/api/admin/signups/{request_id}", {"status": "approved"})
        self.assertEqual(status, 422)

    def test_the_operator_overview_carries_the_applications(self):
        admin = self._as_admin()
        email = f"visible-{self.stamp}@example.com"
        self._apply(email)
        status, body, _ = admin.get("/api/admin/users")
        self.assertEqual(status, 200, body)
        self.assertIn("signups", body)
        self.assertIn("signup_counts", body)
        self.assertIn(email, [row["email"] for row in body["signups"]])

    def test_the_decision_is_audited(self):
        admin = self._as_admin()
        email = f"audited-{self.stamp}@example.com"
        self._apply(email)
        request_id = self._first_pending_id(email)
        admin.post(f"/api/admin/signups/{request_id}", {"status": "declined"})
        status, body, _ = admin.get("/api/admin/users")
        actions = [row["action"] for row in body.get("audit", [])]
        self.assertIn("signup_declined", actions)

    def _first_pending_id(self, email: str) -> str:
        with db.connect() as connection:
            row = connection.execute(
                "SELECT id FROM signup_requests WHERE email=? AND status='pending'", (email,)
            ).fetchone()
        assert row is not None, f"没有找到 {email} 的申请"
        return row["id"]


class RegisterWithATakenEmailTests(unittest.TestCase):
    """用**已经注册过的邮箱**再点一次注册：必须是 400 + 一句人话，不能是 500。

    2026-09-19 生产上真的报了这个：「用户注册显示服务器内部错误」。根因是
    `create_user` 直接 INSERT，撞上 `users.email` 的唯一约束抛
    `sqlite3.IntegrityError`，而 `web.register` 只把 `ValueError` 翻成 400 ——
    于是用户看到的是「服务器内部错误」，而且**没有任何提示告诉他该去登录**。
    这里钉四件事：状态码、句子、邀请码没被这次失败吃掉、大小写不敏感。
    """

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
        web._signup_attempts.clear()
        self.stamp = dt.datetime.now().timestamp()
        self.client = Client(self.base)

    def _invite(self, label: str) -> str:
        code = f"taken-{label}-{self.stamp}"
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with db.connect() as connection:
            connection.execute("INSERT INTO invites(code_hash,expires_at) VALUES(?,?)",
                               (token_hash(code), expiry))
        return code

    def _register(self, email: str, code: str):
        return self.client.post("/api/auth/register", {
            "email": email, "password": "a-long-enough-password",
            "invite_code": code, "accepted_terms": True,
        })

    def test_a_taken_email_gets_a_sentence_not_a_500(self):
        email = f"taken-{self.stamp}@example.com"
        status, body, _ = self._register(email, self._invite("first"))
        self.assertEqual(status, 200, body)

        status, body, _ = self._register(email, self._invite("second"))
        self.assertEqual(status, 400, f"必须是 400，不是 {status}：{body}")
        self.assertIn("已经注册过", body["detail"])
        self.assertIn("登录", body["detail"], "要告诉他下一步该做什么")
        self.assertNotIn("服务器内部错误", body["detail"])

    def test_the_collision_is_case_insensitive(self):
        """`users.email` 是 UNIQUE COLLATE NOCASE，注册这条路也必须当同一个邮箱。"""
        status, body, _ = self._register(f"Case-{self.stamp}@Example.com", self._invite("case1"))
        self.assertEqual(status, 200, body)
        status, body, _ = self._register(f"case-{self.stamp}@example.com", self._invite("case2"))
        self.assertEqual(status, 400, body)
        self.assertIn("已经注册过", body["detail"])

    def test_a_refused_attempt_does_not_eat_the_invite(self):
        """失败那一次必须整笔回滚：申请人的码不能被一次手滑吃掉。"""
        email = f"rollback-{self.stamp}@example.com"
        self.assertEqual(self._register(email, self._invite("keep1"))[0], 200)
        code = self._invite("keep2")
        self.assertEqual(self._register(email, code)[0], 400)
        with db.connect() as connection:
            row = connection.execute("SELECT used_by FROM invites WHERE code_hash=?",
                                     (token_hash(code),)).fetchone()
        self.assertIsNone(row["used_by"], "被拒绝的那次不该消耗邀请码")
        # 换一个邮箱，同一张码照样能用 —— 这才叫「没被吃掉」。
        status, body, _ = self._register(f"other-{self.stamp}@example.com", code)
        self.assertEqual(status, 200, body)

    def test_a_paused_account_says_so(self):
        email = f"paused-{self.stamp}@example.com"
        status, user, _ = self._register(email, self._invite("paused"))
        self.assertEqual(status, 200, user)
        db.set_user_status(user["id"], "paused")
        status, body, _ = self._register(email, self._invite("paused2"))
        self.assertEqual(status, 400, body)
        self.assertIn("暂停", body["detail"])

    def test_the_same_address_can_come_back_after_deleting(self):
        """注销是真删行（v0.63.x），所以那个邮箱必须能重新注册 —— 别把它永久烧掉。"""
        email = f"comeback-{self.stamp}@example.com"
        status, user, _ = self._register(email, self._invite("back1"))
        self.assertEqual(status, 200, user)
        db.set_user_status(user["id"], "deleted")
        status, body, _ = self._register(email, self._invite("back2"))
        self.assertEqual(status, 200, body)


class SignupStorageTests(unittest.TestCase):
    """The store's own guarantees, without the HTTP layer."""

    def setUp(self):
        self.path = tempfile.mktemp(suffix=".sqlite3")
        self.db = database_mod.Database(self.path)
        self.db.initialize()

    def test_a_duplicate_pending_request_is_reported_not_raised(self):
        _, first = self.db.create_signup_request("a@example.com")
        row, second = self.db.create_signup_request("a@example.com")
        self.assertFalse(first)
        self.assertTrue(second)
        self.assertEqual(row["email"], "a@example.com")

    def test_addresses_are_normalised(self):
        self.db.create_signup_request("  Mixed@Example.COM ")
        row, already = self.db.create_signup_request("mixed@example.com")
        self.assertTrue(already, "大小写和空格不同的同一个地址应视为重复")

    def test_counts_track_the_decision(self):
        row, _ = self.db.create_signup_request("count@example.com")
        self.assertEqual(self.db.signup_request_counts()["pending"], 1)
        self.db.decide_signup_request(row["id"], "invited", "label")
        counts = self.db.signup_request_counts()
        self.assertEqual((counts["pending"], counts["invited"]), (0, 1))

    def test_an_unknown_status_is_rejected(self):
        with self.assertRaises(ValueError):
            self.db.decide_signup_request("sgn_x", "approved")

    def test_deciding_something_that_does_not_exist_raises(self):
        with self.assertRaises(KeyError):
            self.db.decide_signup_request("sgn_missing", "declined")

    def test_pending_requests_sort_to_the_top(self):
        first, _ = self.db.create_signup_request("old@example.com")
        self.db.create_signup_request("new@example.com")
        self.db.decide_signup_request(first["id"], "declined")
        rows = self.db.list_signup_requests()
        self.assertEqual(rows[0]["email"], "new@example.com", "待处理的应排在最前")

    def test_an_empty_address_is_refused(self):
        with self.assertRaises(ValueError):
            self.db.create_signup_request("   ")

    def test_deleting_a_user_does_not_touch_applications(self):
        """An application is not user data; it must survive unrelated deletions."""
        self.db.create_signup_request("keep@example.com")
        with self.db.connect() as connection:
            connection.execute(
                """INSERT INTO users(id,email,password_hash,status,created_at)
                   VALUES('u1','gone@example.com','h','active','2026-09-14T00:00:00+00:00')""")
        self.db.set_user_status("u1", "deleted")
        self.assertEqual(len(self.db.list_signup_requests()), 1)


class InviteDeliveryRecordTests(unittest.TestCase):
    """Whether an applicant was actually mailed, answerable after the fact.

    The operator's question is "did this person get their code?", and only part
    of it is knowable from here. These pin the knowable part down so it stays
    knowable later, and pin the difference between "we never tried" and "we tried
    and it failed" -- two facts that need different responses and used to be
    indistinguishable once the browser tab was closed.
    """

    def setUp(self):
        self.stamp = dt.datetime.now().timestamp()
        # A database of its own per test. Reusing the shared one looks fine when
        # the module runs alone and breaks under `discover`, because the env path
        # is whichever test module happened to be imported last.
        self.db = database_mod.Database(tempfile.mktemp(suffix=".sqlite3"))
        self.db.initialize()

    def _invited(self, email: str, label_suffix: str = "x") -> str:
        row, _ = self.db.create_signup_request(email)
        label = f"signup-{email[:40]}-{label_suffix}"
        self.db.create_invite(label, days=14)
        self.db.decide_signup_request(row["id"], "invited", invite_label=label)
        return row["id"]

    def _row_for(self, email: str) -> dict:
        return next(item for item in self.db.list_signup_requests() if item["email"] == email)

    def test_a_successful_send_is_recorded_with_its_message_id(self):
        email = f"sent-{self.stamp}@example.com"
        request_id = self._invited(email)
        self.db.record_invite_email(request_id, sent=True, message_id="<m1@example.com>")
        row = self._row_for(email)
        self.assertTrue(row["invite_sent_at"])
        self.assertEqual(row["invite_message_id"], "<m1@example.com>")
        self.assertEqual(row["invite_send_error"], "")

    def test_a_failed_send_is_recorded_as_a_failure_not_as_silence(self):
        email = f"failed-{self.stamp}@example.com"
        request_id = self._invited(email)
        self.db.record_invite_email(request_id, sent=False, error="SMTP 发送失败：超时")
        row = self._row_for(email)
        self.assertEqual(row["invite_sent_at"], "")
        self.assertIn("SMTP", row["invite_send_error"])

    def test_redeeming_the_code_shows_up_next_to_the_application(self):
        """The strongest evidence this side can hold: a redeemed code was read."""
        email = f"redeemed-{self.stamp}@example.com"
        request_id = self._invited(email, "r")
        self.db.record_invite_email(request_id, sent=True, message_id="<m2@example.com>")
        row = self._row_for(email)
        self.assertIsNone(row["invite_used_by"], "还没人用之前必须是空的")

        with self.db.connect() as connection:
            connection.execute(
                """INSERT INTO users(id,email,password_hash,status,created_at)
                   VALUES('u9',?,'h','active','2026-09-14T00:00:00+00:00')""", (email,))
            connection.execute(
                "UPDATE invites SET used_by='u9', used_at='2026-09-14T01:00:00+00:00' WHERE label=?",
                (row["invite_label"],))
        row = self._row_for(email)
        self.assertEqual(row["invite_used_by"], "u9")
        self.assertEqual(row["redeemer_email"], email, "要能看出是哪个账号用的")

    def test_re_approving_does_not_merge_two_invites_into_one_row(self):
        """Approving twice is a resend, and each attempt gets its own label.

        With a shared label the join matches two invites, so the applicant either
        appears twice or -- worse -- the wrong attempt's result is shown against
        the application.
        """
        email = f"resend-{self.stamp}@example.com"
        request_id = self._invited(email, "first")
        self.db.record_invite_email(request_id, sent=True, message_id="<first@example.com>")

        label2 = f"signup-{email[:40]}-second"
        self.db.create_invite(label2, days=14)
        self.db.decide_signup_request(request_id, "invited", invite_label=label2)
        self.db.record_invite_email(request_id, sent=False, error="第二次也失败了")

        rows = [item for item in self.db.list_signup_requests() if item["email"] == email]
        self.assertEqual(len(rows), 1, "一次申请只该占一行")
        self.assertEqual(rows[0]["invite_label"], label2, "应当显示最近一次的码")
        self.assertIn("第二次", rows[0]["invite_send_error"])

    def test_an_application_without_a_code_reports_nothing_rather_than_zero(self):
        email = f"pending-{self.stamp}@example.com"
        self.db.create_signup_request(email)
        row = self._row_for(email)
        self.assertIsNone(row["invite_used_by"])
        self.assertEqual(row["invite_sent_at"], "")
        self.assertEqual(row["invite_message_id"], "")


class ManageInvitationsCommandTests(unittest.TestCase):
    """The report an operator runs when they want to stop guessing."""

    def setUp(self):
        self.db = database_mod.Database(tempfile.mktemp(suffix=".sqlite3"))
        self.db.initialize()

    def _capture(self) -> tuple[str, int]:
        import io
        from contextlib import redirect_stdout

        from pilot_app import manage

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = manage.invitations(self.db)
        return buffer.getvalue(), code

    def test_a_failed_send_makes_the_command_exit_nonzero(self):
        """So a script or a cron can treat it as a signal rather than text."""
        email = f"cli-{dt.datetime.now().timestamp()}@example.com"
        row, _ = self.db.create_signup_request(email)
        label = f"signup-{email[:40]}-cli"
        self.db.create_invite(label, days=14)
        self.db.decide_signup_request(row["id"], "invited", invite_label=label)
        self.db.record_invite_email(row["id"], sent=False, error="SMTP 发送失败：测试")

        output, code = self._capture()
        self.assertIn(email, output)
        self.assertIn("失败", output)
        self.assertEqual(code, 1, "有发送失败时应当非零退出")

    def test_it_states_what_it_cannot_prove(self):
        """The limit is the important half of the answer.

        A 250 from the relay means the provider accepted the message, not that a
        human saw it, and a report that blurs those two would be worse than no
        report at all.
        """
        self.db.create_signup_request(f"footer-{dt.datetime.now().timestamp()}@example.com")
        output, _ = self._capture()
        self.assertIn("不是「已送达」", output)
        self.assertIn("没有已读回执", output)
        self.assertIn("只有收件人本人能确认", output)

    def test_it_keeps_the_message_id_visible_for_quoting_to_the_provider(self):
        email = f"cli-id-{dt.datetime.now().timestamp()}@example.com"
        row, _ = self.db.create_signup_request(email)
        label = f"signup-{email[:40]}-cliid"
        self.db.create_invite(label, days=14)
        self.db.decide_signup_request(row["id"], "invited", invite_label=label)
        self.db.record_invite_email(row["id"], sent=True, message_id="<quote-me@example.com>")
        output, _ = self._capture()
        self.assertIn("<quote-me@example.com>", output)


class OpenRegistrationTests(unittest.TestCase):
    """**注册不再需要邀请码**（2026-09-22 拍板，见 `docs/open-registration-2026-09-22.md`）。

    这个类钉的是取消邀请码这件事本身，四条：

    * **不带码也能建号**，而且不碰 `invites` 表（历史码的账目不能被新注册搅乱）；
    * **带一张历史有效码仍然能建号**，并且照旧被原子认领 —— 老邮件里的码不作废；
    * **`accepted_terms` 仍然服务端校验**（铁律 10），与有没有码无关；
    * **同一客户端第 6 次注册 429**：开放注册之后，这是唯一一道防批量建号的闸门。

    `setUp` 里**清**限速计数（一个进程里从一个地址注册的账号远多于任何真实客户端），
    只有最后那条限速判据自己不清 —— 它要的就是「连着发六次」。
    """

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
        web.reset_signup_rate_limit()
        self.stamp = dt.datetime.now().timestamp()
        self.client = Client(self.base)

    def _invite(self, label: str) -> str:
        code = f"open-{label}-{self.stamp}"
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with db.connect() as connection:
            connection.execute("INSERT INTO invites(code_hash,expires_at) VALUES(?,?)",
                               (token_hash(code), expiry))
        return code

    def _register(self, email: str, **extra):
        payload = {"email": email, "password": "a-long-enough-password", "accepted_terms": True}
        payload.update(extra)
        return self.client.post("/api/auth/register", payload)

    def test_a_registration_without_any_code_creates_the_account(self):
        """开发册的意思就是这一条：填一个邮箱就能建号，没有别的门槛。"""
        email = f"open-{self.stamp}@example.com"
        with db.connect() as connection:
            before = connection.execute("SELECT COUNT(*) FROM invites").fetchone()[0]
        status, user, _ = self._register(email)
        self.assertEqual(status, 200, user)
        self.assertEqual(user["email"], email)
        with db.connect() as connection:
            after = connection.execute("SELECT COUNT(*) FROM invites").fetchone()[0]
        self.assertEqual(after, before, "不带码的注册不该在 invites 表里留下任何东西")
        # 建出来的号真的能用（不是只回了一句话）。
        me = Client(self.base)
        status, body, _ = me.post("/api/auth/login", {
            "email": email, "password": "a-long-enough-password"})
        self.assertEqual(status, 200, body)
        status, body, _ = me.get("/api/me")
        self.assertEqual(status, 200, body)

    def test_the_terms_checkbox_is_still_enforced_server_side(self):
        """铁律 10：同意必须由**服务端**校验 —— 浏览器里勾一下不算数。

        它与邀请码是两件事：码取消了，这条不取消。
        """
        status, body, _ = self.client.post("/api/auth/register", {
            "email": f"noconsent-{self.stamp}@example.com",
            "password": "a-long-enough-password"})
        self.assertEqual(status, 400, body)
        self.assertIn("同意", body["detail"])

    def test_a_legacy_code_still_works_and_is_claimed_atomically(self):
        """**向后兼容**：老邮件里那张码仍然能建号，而且仍然是一次性的。

        数据层保留这个能力是有意的（库里还有 31 张没用过的码）；界面不再提供填码的
        地方，所以这条判据只能从接口这一层验。
        """
        code = self._invite("legacy")
        email = f"legacy-{self.stamp}@example.com"
        status, user, _ = self._register(email, invite_code=code)
        self.assertEqual(status, 200, user)
        with db.connect() as connection:
            row = connection.execute(
                "SELECT used_by FROM invites WHERE code_hash=?", (token_hash(code),)).fetchone()
        self.assertEqual(row["used_by"], user["id"], "这张码必须记在刚建出来的账号名下")
        # 同一张码不能开第二个号。
        status, body, _ = self._register(f"legacy2-{self.stamp}@example.com", invite_code=code)
        self.assertEqual(status, 400, body)
        self.assertIn("邀请码", body["detail"])

    def test_an_unknown_code_is_still_refused_for_an_old_client(self):
        """老客户端带着一张假码来，仍然要 400 —— 静默忽略它等于假装认领成功。"""
        status, body, _ = self._register(f"badcode-{self.stamp}@example.com",
                                         invite_code=f"not-a-real-code-{self.stamp}")
        self.assertEqual(status, 400, body)

    def test_the_self_service_resend_endpoint_is_gone(self):
        """`POST /api/invite/resend` 跟着邀请码一起下线（2026-09-22）。

        开放注册之后自助重发没有意义；端点留着的话，它仍然是**一个能间接产生凭据的
        未认证写入**（让一张已批准的码再走一次邮件）。所以判据是 404，而不是
        「还能用但不推荐」。
        """
        status, _, _ = self.client.post("/api/invite/resend", {"email": "someone@example.com"})
        self.assertEqual(status, 404, "自助重发那个端点又回来了")

    def test_the_sixth_registration_from_one_client_is_refused(self):
        """**同一客户端每小时 5 次**（复用申请书那份预算，见 `web._signup_rate_limit`）。

        这是开放注册之后唯一的防批量闸门，所以判据要按「第 6 次」数，而不是
        「有没有出现过 429」。
        """
        web.reset_signup_rate_limit()          # 这一条要的就是「连着发六次」，自己先归零
        seen = []
        for index in range(6):
            status, _, _ = self._register(f"flood-{self.stamp}-{index}@example.com")
            seen.append(status)
        self.assertEqual(seen[:5], [200] * 5, f"前五次应当都能建号：{seen}")
        self.assertEqual(seen[5], 429, f"第六次必须被限速拦下：{seen}")


class RegisterProfileExtrasTests(unittest.TestCase):
    """注册表单上那三栏**选填**资料（2026-09-23 从首页申请表挪进 `/app`）。

    三条性质，缺一条这个改动就站不住：

    1. **一个字都不填照样能注册** —— 开放注册的底线；
    2. 填了就存下来（`profiles.signup_*`），而且**后续「只改一项」的保存不会把它们
       清零**（铁律 4：`PUT /api/profile` 会把允许清单里的字段全写成默认值，所以这
       三列刻意不在那份清单里）；
    3. 取值白名单与申请书**同一份**（`web.SIGNUP_IDENTITIES` / `SIGNUP_GOALS`），
       不认识的取值 422 —— 拒绝而不是静默丢掉。
    """

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
        web.reset_signup_rate_limit()
        self.stamp = dt.datetime.now().timestamp()
        self.client = Client(self.base)

    def _register(self, email: str, **extra):
        payload = {"email": email, "password": "a-long-enough-password", "accepted_terms": True}
        payload.update(extra)
        return self.client.post("/api/auth/register", payload)

    def _extras(self, email: str) -> tuple:
        with db.connect() as connection:
            row = connection.execute(
                """SELECT signup_nickname,signup_identity,signup_goals FROM profiles
                   WHERE user_id=(SELECT id FROM users WHERE email=?)""", (email,)).fetchone()
        return (row["signup_nickname"], row["signup_identity"], row["signup_goals"])

    def test_they_are_stored_when_filled_in(self):
        email = f"extras-{self.stamp}@example.com"
        status, user, _ = self._register(
            email, nickname="  小明  ", identity="本科生",
            goals=["错过截止时间", "找不到要办的事"])
        self.assertEqual(status, 200, user)
        self.assertEqual(self._extras(email),
                         ("小明", "本科生", "错过截止时间、找不到要办的事"))

    def test_none_of_them_is_required(self):
        """**一个字都不填也能建号** —— 这是这次改动最要紧的一条。"""
        email = f"bare-{self.stamp}@example.com"
        status, user, _ = self._register(email)
        self.assertEqual(status, 200, user)
        self.assertEqual(self._extras(email), ("", "", ""))

    def test_the_whitelist_is_the_same_one_the_application_form_uses(self):
        """不认识的取值 422，而且**白名单就是申请书那一份**（不是抄来的第二份）。"""
        email = f"junk-{self.stamp}@example.com"
        status, body, _ = self._register(email, identity="旁听生")
        self.assertEqual(status, 422, body)
        self.assertIn("本科生", body["detail"], "报错要把允许的取值说出来")
        status, body, _ = self._register(email, goals=["别的东西"])
        self.assertEqual(status, 422, body)
        status, body, _ = self._register(email, goals=["错过截止时间", "通知太多",
                                                       "分不清轻重", "找不到要办的事", "第五个"])
        self.assertEqual(status, 422, f"超过四个选项也要拒：{body}")
        with db.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM users WHERE email=?", (email,)).fetchone()[0],
                0, "被拒的注册不能建出账号")

    def test_a_later_profile_save_does_not_clear_them(self):
        """铁律 4：`PUT /api/profile` 会用默认值覆盖**它允许清单里**的每一列。

        这三列不在那份清单里，所以「只改一项」的保存不许把它们抹掉 —— 这条断言就是
        那次选择的判据（把列放进 `upsert_profile.allowed` 会当场让它变红）。
        """
        email = f"keep-{self.stamp}@example.com"
        status, user, _ = self._register(email, nickname="小明", identity="研究生",
                                         goals=["通知太多"])
        self.assertEqual(status, 200, user)
        session = Client(self.base)
        status, body, _ = session.post("/api/auth/login", {
            "email": email, "password": "a-long-enough-password"})
        self.assertEqual(status, 200, body)
        # 一份**只有一项**的资料保存（其余字段会按接口语义写成默认值）。
        status, body, _ = session.put("/api/profile", {"major": "通信工程"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self._extras(email), ("小明", "研究生", "通知太多"),
                         "一次资料保存把注册时填的三栏抹掉了")

    def test_the_operator_can_see_them_on_the_user_row(self):
        """后台「用户」那一行要能看到这三栏（原来是「邀请申请」那一行显示的）。"""
        email = f"visible-{self.stamp}@example.com"
        status, user, _ = self._register(email, nickname="小红", identity="本科生",
                                         goals=["分不清轻重"])
        self.assertEqual(status, 200, user)
        os.environ["INFE_PILOT_ADMIN_EMAILS"] = email
        try:
            admin = Client(self.base)
            status, body, _ = admin.post("/api/auth/login", {
                "email": email, "password": "a-long-enough-password"})
            self.assertEqual(status, 200, body)
            status, body, _ = admin.get("/api/admin/users")
            self.assertEqual(status, 200, body)
            row = [item for item in body["users"] if item["email"] == email][0]
            self.assertEqual(row["signup_nickname"], "小红")
            self.assertEqual(row["signup_identity"], "本科生")
            self.assertEqual(row["signup_goals"], "分不清轻重")
        finally:
            os.environ.pop("INFE_PILOT_ADMIN_EMAILS", None)


if __name__ == "__main__":
    unittest.main()
