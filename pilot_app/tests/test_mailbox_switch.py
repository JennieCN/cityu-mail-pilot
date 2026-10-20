"""Tests for turning "your provider stopped allowing auth codes" into a way out.

The failure this file is about happened in production on 2026-09-16. A user's
page said, in a red box, that Microsoft's personal mailboxes can no longer be
read with an authorisation code and that they should switch to another mailbox.
Every word of that is true, and **it was a dead end**: the page offered no
button, so the only thing a stuck user could do was retype the same address.
The operator's screenshot of that page is why this exists.

Three separate claims are pinned here, because each can be true while the whole
thing is still broken:

* **One definition of "this provider is blocked."** The list of hosts lives in
  `mailpresets.BLOCKED_PROVIDER_HOSTS` and `Database._PROVIDER_BLOCK_HOSTS` is
  *derived* from it. Copying the list would drift in the direction of "the page
  says this one cannot work / the server does not", which is exactly the state
  that leaves a user retyping forever.
* **The page and the letter recommend the same mailboxes.** A stuck user gets
  the advice twice -- on the page and, if the operator reminds them, by e-mail.
  Two lists of "mailboxes that still work" is one list too many.
* **The client is handed the fact, not the wording.** `/api/me` carries
  `needs_another_provider`; `app.js` must not re-implement the judgement by
  matching error text (asserted below by grepping the bundle for a host name).
"""

import json
import os
import tempfile
import threading
import unittest

_TMP = tempfile.mkdtemp()
os.environ.setdefault("INFE_PILOT_DB", _TMP + "/mailbox-switch.sqlite3")
os.environ.setdefault("INFE_PILOT_MASTER_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
os.environ.setdefault("INFE_PILOT_COOKIE_SECURE", "0")
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import database as database_mod  # noqa: E402
from pilot_app import mailpresets, setup_reminders, web  # noqa: E402
from pilot_app.security import hash_password, token_hash  # noqa: E402

STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


class DefinitionTests(unittest.TestCase):
    def test_the_blocked_hosts_are_declared_once(self):
        """The database's list must be the same object, not a copy of the text.

        Equality would not be enough: two literal tuples that happen to match
        today drift the first time somebody edits one of them.
        """
        self.assertIs(database_mod.Database._PROVIDER_BLOCK_HOSTS,
                      mailpresets.BLOCKED_PROVIDER_HOSTS)
        self.assertGreaterEqual(len(mailpresets.BLOCKED_PROVIDER_HOSTS), 5)

    def test_one_preset_never_mixes_domains_that_need_different_servers(self):
        """**一个预置里的域名必须共用同一对服务器**（除非逐个域名写清了覆盖）。

        2026-09-20 的教训：网易的预置把 `163.com` 与 `126.com` 放在一起、服务器只写了
        `imap.163.com`——于是 126 的账号被 163 的服务器拒登录，而我们把服务器的拒绝
        翻译成「你的授权码不对或已失效」，**让人去重新生成一个本来就是对的授权码**。
        实测：同一个码在 `imap.163.com` 上 `Login error or password error`，
        在 `imap.126.com` 上直接进去（两台问候语分别报 `163com` / `126com`）。
        """
        for item in mailpresets.MAILBOX_PRESETS:
            overrides = item.get("hosts_by_domain") or {}
            if not overrides:
                continue
            self.assertEqual(
                set(overrides), set(item["domains"]),
                f"{item['id']}：写了 hosts_by_domain 就必须覆盖它的每一个域名——"
                "漏掉的那个会静默退回默认服务器，正是这次的 bug 形状")
            for domain, pair in overrides.items():
                self.assertEqual(len(pair), 2, f"{item['id']}/{domain} 要给出 (imap, smtp)")

    def test_every_domain_maps_to_the_servers_that_domain_actually_uses(self):
        """填服务器这件事由**地址**决定，不是由预置的默认值决定。"""
        expected = {
            "163.com": ("imap.163.com", "smtp.163.com"),
            "126.com": ("imap.126.com", "smtp.126.com"),
            "yeah.net": ("imap.yeah.net", "smtp.yeah.net"),
            "vip.163.com": ("imap.vip.163.com", "smtp.vip.163.com"),
            "vip.126.com": ("imap.vip.126.com", "smtp.vip.126.com"),
        }
        for domain, pair in expected.items():
            got = mailpresets.hosts_for_email("someone@" + domain)
            self.assertEqual((got["imap_host"], got["smtp_host"]), pair, domain)
            self.assertEqual(got["imap_port"], 993)
        # 没有覆盖的供应商照旧用预置的默认值。
        qq = mailpresets.hosts_for_email("someone@qq.com")
        self.assertEqual(qq["imap_host"], "imap.qq.com")
        # 认不出来的域名：什么都不猜。
        self.assertEqual(mailpresets.hosts_for_email("someone@example.org"), {})

    def test_the_client_is_given_the_same_per_domain_table(self):
        """页面拿到的必须是同一份数据——两处各写一份迟早漂。"""
        presets = {item["id"]: item for item in mailpresets.public_mailbox_help()["presets"]}
        netease = presets["163"]
        self.assertEqual(netease["hosts_by_domain"]["126.com"], ["imap.126.com", "smtp.126.com"])
        with open(os.path.join(STATIC, "app.js"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("hosts_by_domain", text, "页面要按域名填服务器，不能只用预置默认值")
        self.assertIn("mailServersFor", text)

    def test_the_blocked_domains_are_the_blocked_preset(self):
        """Every domain we call blocked must belong to a preset we mark blocked.

        Otherwise a user typing `someone@that-domain` gets the predictive warning
        on the page while the server would classify the same mailbox as an
        ordinary failure -- two answers to one question.
        """
        blocked = {item for item in mailpresets.BLOCKED_PROVIDER_HOSTS}
        for item in mailpresets.MAILBOX_PRESETS:
            marked = bool(item.get("blocked_reason"))
            for domain in item["domains"]:
                self.assertEqual(
                    domain in blocked, marked,
                    f"{item['id']} 的域名 {domain}：清单说 blocked={domain in blocked}，"
                    f"预设说 blocked={marked}——两处必须同时改")
            if marked:
                self.assertIn(item["imap_host"], blocked,
                              f"{item['id']} 标了 blocked，它的 IMAP 主机却不在清单里")

    def test_every_blocked_domain_routes_to_the_preset_that_blocks_it(self):
        """Two presets claiming one domain would send users to two different guides."""
        for item in mailpresets.MAILBOX_PRESETS:
            if not item.get("blocked_reason"):
                continue
            for domain in item["domains"]:
                self.assertEqual(mailpresets.preset_id_for_email("x@" + domain),
                                 item["id"], domain)

    def test_the_whole_list_is_what_the_server_calls_a_provider_refusal(self):
        """Behavioural, not structural: every entry has to actually classify.

        This is the assertion that catches a list edited by hand -- a host added
        to the tuple but not to any preset would still classify here, and one
        added to a preset but forgotten in the tuple cannot exist any more
        because the tuple is derived.
        """
        for host in mailpresets.BLOCKED_PROVIDER_HOSTS:
            self.assertTrue(
                database_mod.Database.mailbox_needs_another_provider({"imap_host": host}),
                host)
        self.assertFalse(
            database_mod.Database.mailbox_needs_another_provider({"imap_host": "imap.qq.com"}))

    def test_the_alternatives_are_the_three_we_name_in_the_letter(self):
        """Page and letter must recommend the same mailboxes.

        The letter is written by hand (`_switch_mailbox_steps`), so it cannot
        import the list; the joint assertion is what keeps them aligned. The
        comparison drops the 「邮箱」 suffix because the letter writes 「163」.
        """
        offered = mailpresets.recommended_alternatives()
        self.assertEqual([item["id"] for item in offered], ["qq", "163", "gmail"])
        for item in offered:
            self.assertFalse(item.get("blocked_reason"), item["id"])
            self.assertTrue(item["short_label"], item["id"])
        letter = setup_reminders._switch_mailbox_steps()
        for item in offered:
            token = item["short_label"].replace("邮箱", "").strip()
            self.assertIn(token, letter, f"提醒信里没提「{item['short_label']}」")

    def test_the_switch_can_exclude_the_one_that_is_broken(self):
        ids = [item["id"] for item in mailpresets.recommended_alternatives(exclude="qq")]
        self.assertEqual(ids, ["163", "gmail"])

    def test_the_client_is_told_the_short_label(self):
        published = mailpresets.public_mailbox_help()
        for item in published["alternatives"]:
            self.assertEqual(set(item), {"id", "label", "short_label", "domains"})
            self.assertTrue(item["short_label"])
        flagged = [item for item in published["presets"] if item["blocked_reason"]]
        self.assertEqual(len(flagged), 1, "「一定连不上」的服务商应当只有一个")
        self.assertEqual(flagged[0]["id"], "outlook")


class DashboardFlagTests(unittest.TestCase):
    """`/api/me` has to say *whose* fault it is, or the client has to guess."""

    @classmethod
    def setUpClass(cls):
        cls.server = web.create_server("127.0.0.1", 0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def _client(self, email: str):
        import http.cookiejar
        import urllib.request
        database = web.get_db()
        # 走真实的注册路径（邀请码 → create_user），而不是手写一行 users：
        # 资料表、默认设置这些都是注册的一部分，少建一样 `/api/me` 就 500
        # —— 那正是这一版夹具第二次踩到的坑。
        code = database.create_invite("switch-test", days=1)
        user = database.create_user(email, hash_password("a-long-enough-password"),
                                    token_hash(code))
        user_id = user["id"]
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(jar))

        def call(method, path, payload=None):
            data = json.dumps(payload).encode() if payload is not None else None
            request = urllib.request.Request(self.base + path, data=data, method=method)
            request.add_header("Content-Type", "application/json")
            with opener.open(request, timeout=20) as response:
                return response.status, json.loads(response.read().decode() or "{}")

        call("POST", "/api/auth/login",
             {"email": email, "password": "a-long-enough-password"})
        return database, user_id, call

    def _mailbox(self, database, user_id, host: str, error: str, verify_error: str = ""):
        """Write through the real API, then set the two error columns.

        Hand-written INSERTs into `mailboxes` broke twice while this file was
        being written (a new NOT NULL column, then another) -- which is the
        argument for going through `upsert_mailbox`: a fixture that copies the
        schema is a fixture that has to be edited every time the schema grows.
        """
        database.upsert_mailbox(user_id, {
            "email": "someone@example.com", "report_to": "someone@example.com",
            "imap_host": host, "imap_port": 993,
            "smtp_host": "smtp.example.com", "smtp_port": 465,
            "encrypted_password": b"x", "enabled": True,
        })
        with database.connect() as connection:
            connection.execute(
                "UPDATE mailboxes SET last_error=?, last_verify_error=? WHERE user_id=?",
                (error, verify_error, user_id))

    def test_a_provider_refusal_is_reported_as_such(self):
        database, user_id, call = self._client("blocked@example.com")
        self._mailbox(database, user_id, "outlook.office365.com",
                      "这个邮箱的服务商已经停用「账号密码 / 授权码」登录（微软 Outlook、"
                      "Hotmail 已强制改用 OAuth）。")
        _, me = call("GET", "/api/me")
        self.assertTrue(me["mailbox"]["needs_another_provider"])

    def test_the_text_alone_is_enough_when_the_host_is_unknown(self):
        """A provider that closes the door later leaves the old host in place."""
        database, user_id, call = self._client("later@example.com")
        self._mailbox(database, user_id, "imap.some-new-provider.com",
                      "", "这个邮箱的服务商已经强制改用 OAuth，请换一个邮箱。")
        _, me = call("GET", "/api/me")
        self.assertTrue(me["mailbox"]["needs_another_provider"])

    def test_a_working_mailbox_is_not_flagged(self):
        database, user_id, call = self._client("healthy@example.com")
        self._mailbox(database, user_id, "imap.qq.com", "")
        _, me = call("GET", "/api/me")
        self.assertFalse(me["mailbox"]["needs_another_provider"])

    def test_an_ordinary_wrong_code_is_not_flagged(self):
        """The whole point of the distinction: this one is the user's to fix."""
        database, user_id, call = self._client("typo@example.com")
        self._mailbox(database, user_id, "imap.qq.com", "授权码错误，请重新生成。")
        _, me = call("GET", "/api/me")
        self.assertFalse(me["mailbox"]["needs_another_provider"])


class ClientWiringTests(unittest.TestCase):
    """The browser half, checked statically: it is markup plus one flag."""

    def _page(self) -> str:
        with open(os.path.join(STATIC, "index.html"), encoding="utf-8") as handle:
            return handle.read()

    def _script(self) -> str:
        with open(os.path.join(STATIC, "app.js"), encoding="utf-8") as handle:
            return handle.read()

    def test_the_page_has_somewhere_to_put_the_way_out(self):
        self.assertIn('id="mail-switch"', self._page())

    def test_the_client_reads_the_flag_and_not_the_wording(self):
        script = self._script()
        self.assertIn("needs_another_provider", script)
        # No copy of the host list, and no matching on the error text: both would
        # be a second definition of a judgement the server already made.
        for leak in ("outlook.office365.com", "hotmail.com", "已强制改用", "OAuth"):
            self.assertNotIn(leak, script,
                             f"app.js 里出现了服务商判据的一部分：{leak}")

    def test_switching_providers_offers_every_alternative(self):
        script = self._script()
        self.assertIn("mailAlternatives()", script)
        self.assertIn("data-provider", script)
        # The click handler has to be delegated: the buttons are re-created on
        # every render (v0.63.41 lost a day to a handler bound to a dead node).
        self.assertIn("closest('button[data-provider]')", script)

    def test_the_three_steps_name_the_forwarding_rule(self):
        """The step users skip: a new mailbox needs a new CityU forwarding rule."""
        script = self._script()
        start = script.index("function renderMailSwitch")
        block = script[start:start + 2000]
        self.assertIn("转发", block)
        self.assertIn("CityU", block)


if __name__ == "__main__":
    unittest.main()


class WrongNetEaseServerTests(unittest.TestCase):
    """「把 126 的账号指到 163 的服务器上」这一类错配：**是我们写错的，要我们改回来**。

    用户侧看到的是「授权码不对或已失效」，于是他一遍遍重新生成一个本来就是对的授权码。
    2026-09-20 在真账号上量到：同一个码在 `imap.163.com` 上 `Login error or password
    error`，在 `imap.126.com` 上直接登进去（真实地址不进公开树，这里用夹具地址）。
    """

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.db = database_mod.Database(os.path.join(self.work.name, "pilot.sqlite3"))
        self.db.initialize()

    def tearDown(self):
        self.work.cleanup()

    def _mailbox(self, email: str, imap_host: str, smtp_host: str = "") -> str:
        """直接写一行邮箱（跳过注册流程：这里考的是那一行本身）。"""
        with self.db.connect() as connection:
            invite = "invite-" + email
            connection.execute(
                "INSERT INTO invites(code_hash,expires_at) VALUES(?,?)",
                (token_hash(invite), "2099-01-01T00:00:00+00:00"))
            user_id = "usr_" + token_hash(email)[:24]
            connection.execute(
                "INSERT INTO users(id,email,password_hash,created_at) VALUES(?,?,?,?)",
                (user_id, email, hash_password("a-long-enough-password"),
                 "2026-09-20T00:00:00+00:00"))
        self.db.upsert_mailbox(user_id, {
            "email": email, "report_to": email, "imap_host": imap_host, "imap_port": 993,
            "smtp_host": smtp_host or imap_host.replace("imap.", "smtp."), "smtp_port": 465,
            "enabled": True, "encrypted_password": "x",
        })
        return user_id

    def _hosts(self, user_id: str) -> tuple[str, str]:
        box = self.db.get_mailbox(user_id)
        return box["imap_host"], box["smtp_host"]

    def test_a_126_mailbox_pointed_at_163_is_repaired(self):
        user_id = self._mailbox("someone@126.com", "imap.163.com")
        self.db.initialize()                     # 启动时修
        self.assertEqual(self._hosts(user_id), ("imap.126.com", "smtp.126.com"))

    def test_the_other_netease_domains_are_repaired_too(self):
        cases = {"a@yeah.net": "imap.yeah.net", "b@vip.163.com": "imap.vip.163.com",
                 "c@vip.126.com": "imap.vip.126.com"}
        users = {email: self._mailbox(email, "imap.163.com") for email in cases}
        self.db.initialize()
        for email, host in cases.items():
            self.assertEqual(self._hosts(users[email])[0], host, email)

    def test_it_is_idempotent_and_leaves_everything_else_alone(self):
        correct = self._mailbox("someone@163.com", "imap.163.com")
        custom_domain = self._mailbox("someone@example.org", "imap.example.org")
        custom_host = self._mailbox("someone@126.com", "imap.mail.126.example.net")
        self.db.initialize()
        self.db.initialize()
        self.assertEqual(self._hosts(correct)[0], "imap.163.com", "本来就对的别动")
        self.assertEqual(self._hosts(custom_domain)[0], "imap.example.org",
                         "认不出来的域名不猜")
        self.assertEqual(self._hosts(custom_host)[0], "imap.mail.126.example.net",
                         "用户自己填的主机名不许替他改（他可能故意指向别处）")
