# -*- coding: utf-8 -*-
"""`manage check-mailboxes`：分档要准，输出里不能有秘密。

这个文件存在的理由，是 2026-09-18 到 09-20 那三天里的**三次「授权码用不了」**，
而它们是**三件不同的事**：

① **一个 126 的账号** —— 我们把它的地址填成了 `imap.163.com`（网易一个域名一台机器）。
   服务器回的是通用的「密码错误」，我们却翻译成「授权码不对」，于是用户一遍遍重新生成
   一串**本来就是对的**授权码。已修（v0.63.99）。真实地址不进公开树，测试里用夹具地址。
② **两个 163 的账号** —— 主机是对的，**163 真的拒了那两串码**（同一份夹具地址）。
③ 微软系 —— **永远不可能**用授权码。

所以下面钉三件事：

* **分档的顺序**：主机不一致必须**先于**登录结果判定。少了这条，① 会再次被报成
  「授权码被拒」，而那是这个命令唯一存在的意义。
* **探针是只读的**：只发 `ID → LOGIN → EXAMINE → LOGOUT`，绝不 `SELECT`、绝不
  `UID FETCH`、绝不 `STORE`/`DELETE`。假服务器把每一条命令都记下来，这里逐条断言。
* **输出里没有秘密、也没有完整地址**：包括一台**故意把授权码回显在错误里**的
  假服务器——「服务器不会回显密码」不是一条能被验证的性质，所以擦洗要能兜住它。
"""

from __future__ import annotations

import ast
import base64
import contextlib
import datetime as dt
import imaplib
import pathlib
import io
import os
import ssl
import tempfile
import unittest
from unittest import mock

from pilot_app import mailboxcheck, manage
from pilot_app.database import Database
from pilot_app.security import SecretBox, hash_password, token_hash

_TEST_KEY = base64.urlsafe_b64encode(b"\x00" * 32).decode()

# 真机上抓下来的原话（不是编的例子）：163 用 126 的服务器拒 126 的账号、163 拒真的
# 坏码、Gmail 的 AUTHENTICATIONFAILED、微软的 LOGON denied。分类器照着它们判。
SERVER_WORDS = {
    "netease_password_error": "LOGIN Login error or password error",
    "gmail_auth_failed": "[AUTHENTICATIONFAILED] Invalid credentials (Failure)",
    "qq_wrong_kind": "Login fail. Please enter your authorization code to login.",
    # QQ 真正回的那一句很长，而且把「频率限制」也列在可能原因里。它是**真机**教出来的：
    # 先看限流词就会把 QQ 最常见的失败报成「非授权码」。
    "qq_generic": ("Login fail. Account is abnormal, service is not open, "
                   "password is incorrect, login frequency limited, or system is busy. "
                   "More information at https://help.mail.qq.com/detail/108/1023"),
    "microsoft": "LOGON is denied.",
    "microsoft_disabled": "Basic authentication is disabled.",
    "throttled": "Login frequency limited, please try again later.",
    "unsafe_login": "[EXAMINE Unsafe Login. Please contact kefu@188.com for help]",
}


# ---------------------------------------------------------------------------
# 一枚假 IMAP 服务器：按主机名决定这一台怎么答，并把每条命令记下来
# ---------------------------------------------------------------------------


class FakeIMAP:
    """最小的 IMAP 服务器替身——只够回答这一条探针，但**每条命令都记账**。

    行为按 `host` 或按**地址**给：同一个 `imap.163.com` 上既有能用的账号、也有被拒的
    账号，而假服务器必须让它们答得不一样（真实世界里本来就是这样）。
    """

    #: host -> {"connect": "refuse"/"timeout", ...}；连接层只有主机，没有地址。
    plan: dict[str, dict] = {}
    #: 地址 -> {"login": "ok"/"reject"/"explode", "words": ..., "examine": ...}
    by_address: dict[str, dict] = {}
    log: list[tuple] = []

    def __init__(self, host, port, timeout=None, ssl_context=None):
        # 2026-09-24：`IMAP4_SSL` 现在**必须**收到一个校验证书的上下文（见 `test_imap_tls.py`）。
        # 这个替身顺手当一道守卫：真调用点漏传就当场红。
        if ssl_context is None or getattr(ssl_context, "verify_mode", None) != ssl.CERT_REQUIRED:
            raise AssertionError("IMAP4_SSL 没有收到校验证书的 ssl_context")
        self.host, self.port, self.timeout = host, port, timeout
        self.address = ""
        self.settings = type(self).plan.get(host, {})
        if self.settings.get("connect") == "refuse":
            type(self).log.append((host, "connect-refused"))
            raise OSError("connection refused by the fake server")
        if self.settings.get("connect") == "timeout":
            type(self).log.append((host, "connect-timeout"))
            raise TimeoutError("timed out")
        # `identify_client` 只看 CAPABILITY 里有没有 ID；真机上 163/126 有。
        self.capabilities = ("IMAP4rev1", "ID", "AUTH=PLAIN")
        type(self).log.append((host, "connect"))

    def _settings(self) -> dict:
        return type(self).by_address.get(self.address) or type(self).plan.get(self.host, {})

    def __getattr__(self, name):
        """**假服务器只提供真客户端也有的东西。**

        2026-09-20 生产上撞出来的那一课：第一版探针写的是 `client.examine("INBOX")`，
        而 `imaplib.IMAP4` 根本没有这个方法（`__getattr__` 只认 `Commands` 里的大写
        动词），生产上第一次真跑就 `AttributeError`。**单测全绿**——因为这个假服务器
        自己定义了 `examine`，探针在假货上跑得通。这和 `mailio.identify_client` 当年
        踩的是同一个坑（假服务器实现了 `_simple_command`，于是 `KeyError` 永远不出现）。

        所以这里反过来：真客户端没有的属性，这里也**必须**没有。
        """
        if not hasattr(imaplib.IMAP4, name):
            raise AttributeError(f"假服务器照真客户端办事：imaplib.IMAP4 没有 {name!r}")
        raise AttributeError(name)

    def _simple_command(self, name, *args):
        # RFC 2971 的 ID：imaplib 不认这个动词，所以 `identify_client` 直接调它。
        type(self).log.append((self.host, name, args))
        return "OK", [b"ID completed"]

    def login(self, address, password):
        self.address = address
        type(self).log.append((self.host, "login", address, password))
        mode = self._settings().get("login", "ok")
        if mode == "explode":
            raise OSError("connection reset by peer")
        if mode == "reject":
            words = self._settings().get("words", SERVER_WORDS["netease_password_error"])
            if self._settings().get("echo_password"):
                words += f" (the password was {password})"
            raise imaplib.IMAP4.error(words)
        return "OK", [b"LOGIN completed"]

    def select(self, folder="INBOX", readonly=False):
        """`select(..., readonly=True)` 就是 imaplib 的 `EXAMINE`（见 `ProbeTests`）。"""
        type(self).log.append((self.host, "select", folder, bool(readonly)))
        mode = self._settings().get("open", "ok")
        if mode == "explode":
            raise OSError("connection reset while opening the mailbox")
        if mode == "no":
            return "NO", [self._settings().get("words", SERVER_WORDS["unsafe_login"]).encode()]
        return "OK", [b"[READ-ONLY] Examine completed"]

    def uid(self, *args, **kwargs):        # pragma: no cover - 探针不该用它
        type(self).log.append((self.host, "uid", args))
        return "OK", [b""]

    def store(self, *args, **kwargs):      # pragma: no cover - 探针不该用它
        type(self).log.append((self.host, "store", args))
        return "OK", [b""]

    def logout(self):
        type(self).log.append((self.host, "logout"))
        return "BYE", [b"bye"]


@contextlib.contextmanager
def fake_servers(plan: dict[str, dict], by_address: dict[str, dict] | None = None):
    FakeIMAP.plan, FakeIMAP.by_address, FakeIMAP.log = plan, dict(by_address or {}), []
    with mock.patch("imaplib.IMAP4_SSL", FakeIMAP):
        yield FakeIMAP.log


@contextlib.contextmanager
def master_key():
    with mock.patch.dict(os.environ, {"INFE_PILOT_MASTER_KEY": _TEST_KEY}):
        yield SecretBox.from_base64(_TEST_KEY)


# ---------------------------------------------------------------------------
# 纯函数：决策表
# ---------------------------------------------------------------------------


class ConcludeTests(unittest.TestCase):
    """决策表本身。**顺序**是这里最重要的东西。"""

    def test_a_126_address_on_163s_server_is_our_mistake_not_a_bad_code(self):
        """① 的回归测试：**主机不一致排在登录结果之前**。

        少了这条，`imap.163.com` 对 126 账号回的通用「密码错误」会再一次被报成
        「授权码被拒」——于是用户又去重新生成一串本来就是对的授权码。
        """
        tier, reason = mailboxcheck.conclude(
            address="someone@126.com", stored_host="imap.163.com", stored_port=993,
            probe_state=mailboxcheck.AUTH_REJECTED)
        self.assertEqual(tier, mailboxcheck.HOST_MISMATCH)
        self.assertIn("imap.126.com", reason)
        self.assertIn("我们的配置问题", reason)

    def test_a_wrong_port_is_also_our_configuration(self):
        tier, _ = mailboxcheck.conclude(
            address="someone@163.com", stored_host="imap.163.com", stored_port=143,
            probe_state=mailboxcheck.OK)
        self.assertEqual(tier, mailboxcheck.HOST_MISMATCH)

    def test_a_correct_host_with_a_refused_code_is_the_code(self):
        """②：主机对、服务器明确拒绝——这一档才是要用户重新生成授权码的。"""
        tier, _ = mailboxcheck.conclude(
            address="refused@163.com", stored_host="imap.163.com", stored_port=993,
            probe_state=mailboxcheck.AUTH_REJECTED)
        self.assertEqual(tier, mailboxcheck.AUTH_REJECTED)

    def test_microsoft_is_its_own_tier_whatever_the_probe_said(self):
        """③：这条路根本不存在，所以重试、重新生成都没有意义——要劝换邮箱。"""
        for state in (mailboxcheck.AUTH_REJECTED, mailboxcheck.NETWORK, mailboxcheck.OK):
            tier, reason = mailboxcheck.conclude(
                address="someone@outlook.com", stored_host="outlook.office365.com",
                stored_port=993, probe_state=state)
            self.assertEqual(tier, mailboxcheck.PROVIDER_BLOCKED, state)
            self.assertIn("OAuth", reason)

    def test_a_server_that_says_oauth_only_is_a_dead_end_for_that_provider(self):
        """没有第二家今天会这样答，但判据不能写成「只有微软会」。"""
        tier, reason = mailboxcheck.conclude(
            address="someone@qq.com", stored_host="imap.qq.com", stored_port=993,
            probe_state=mailboxcheck.PROVIDER_BLOCKED)
        self.assertEqual(tier, mailboxcheck.PROVIDER_BLOCKED)
        self.assertIn("OAuth", reason)
        self.assertNotIn("微软", reason)

    def test_an_old_microsoft_host_is_recognised_without_the_domain(self):
        tier, _ = mailboxcheck.conclude(
            address="someone@corp.example.com", stored_host="imap-mail.outlook.com",
            stored_port=993, probe_state=mailboxcheck.NETWORK)
        self.assertEqual(tier, mailboxcheck.PROVIDER_BLOCKED)

    def test_a_known_domain_pointed_at_a_dead_microsoft_host_is_a_host_mismatch(self):
        """A gmail address aimed at outlook.office365.com is a config bug, not a
        dead end -- calling it 'this provider is blocked' would hide a fixable
        problem behind a dead end."""
        tier, _ = mailboxcheck.conclude(
            address="someone@gmail.com", stored_host="outlook.office365.com",
            stored_port=993, probe_state=mailboxcheck.AUTH_REJECTED)
        self.assertEqual(tier, mailboxcheck.HOST_MISMATCH)

    def test_an_unknown_domain_is_its_own_tier(self):
        tier, reason = mailboxcheck.conclude(
            address="someone@corp.example.com", stored_host="mail.corp.example.com",
            stored_port=993, probe_state=mailboxcheck.NETWORK)
        self.assertEqual(tier, mailboxcheck.UNKNOWN_DOMAIN)
        self.assertIn("人工", reason)

    def test_a_working_unknown_domain_is_still_usable(self):
        """域名不认识 **但登录成功** = 这个邮箱真的能用；不能报成故障。

        它仍然会出现在「域名不在预置里」那一档的点名清单里（见输出测试），
        但结论是「能用」——一个跑得通的邮箱不是问题。
        """
        tier, _ = mailboxcheck.conclude(
            address="someone@corp.example.com", stored_host="mail.corp.example.com",
            stored_port=993, probe_state=mailboxcheck.OK)
        self.assertEqual(tier, mailboxcheck.OK)

    def test_unreachable_is_network_and_never_a_verdict(self):
        tier, reason = mailboxcheck.conclude(
            address="someone@qq.com", stored_host="imap.qq.com", stored_port=993,
            probe_state=mailboxcheck.NETWORK)
        self.assertEqual(tier, mailboxcheck.NETWORK)
        self.assertIn("再跑", reason)

    def test_a_row_without_an_address_is_reported_as_unchecked(self):
        tier, reason = mailboxcheck.conclude(
            address="", stored_host="imap.qq.com", stored_port=993, probe_state=mailboxcheck.OK)
        self.assertEqual(tier, mailboxcheck.CHECK_FAILED)
        self.assertTrue(reason)


class LoginErrorTests(unittest.TestCase):
    """服务器拒绝登录时，话是哪一档的。判据只有措辞，不猜。"""

    def test_the_real_163_words_are_a_refused_code(self):
        self.assertEqual(
            mailboxcheck.classify_login_error(imaplib.IMAP4.error(SERVER_WORDS["netease_password_error"])),
            mailboxcheck.AUTH_REJECTED)
        self.assertEqual(
            mailboxcheck.classify_login_error(imaplib.IMAP4.error(SERVER_WORDS["gmail_auth_failed"])),
            mailboxcheck.AUTH_REJECTED)
        self.assertEqual(
            mailboxcheck.classify_login_error(imaplib.IMAP4.error(SERVER_WORDS["qq_wrong_kind"])),
            mailboxcheck.AUTH_REJECTED)

    def test_the_microsoft_words_are_a_dead_end_not_a_bad_code(self):
        for key in ("microsoft", "microsoft_disabled"):
            self.assertEqual(
                mailboxcheck.classify_login_error(imaplib.IMAP4.error(SERVER_WORDS[key])),
                mailboxcheck.PROVIDER_BLOCKED, key)

    def test_throttling_is_not_a_bad_code(self):
        """「Too many login failures」里也有 login fail，可它不是码错了。"""
        self.assertEqual(
            mailboxcheck.classify_login_error(imaplib.IMAP4.error(SERVER_WORDS["throttled"])),
            mailboxcheck.LOGIN_REFUSED)
        self.assertEqual(
            mailboxcheck.classify_login_error(
                imaplib.IMAP4.error("Too many login failures, try again later")),
            mailboxcheck.LOGIN_REFUSED)

    def test_qqs_long_message_is_still_a_refused_code(self):
        """真机教出来的一条：QQ 把「频率限制」列在**同一句**的原因清单里。

        先看限流词就会把 QQ 最常见的失败报成「不是授权码」——那正是这个命令要消灭的
        那类误诊（把用户指去错的地方）。判据按词的明确程度排，不按整句。
        """
        self.assertEqual(
            mailboxcheck.classify_login_error(imaplib.IMAP4.error(SERVER_WORDS["qq_generic"])),
            mailboxcheck.AUTH_REJECTED)

    def test_a_service_that_is_not_open_is_still_a_login_failure(self):
        """QQ 的另一句：「service is not open」——要用户去开 IMAP，不是等一会儿。"""
        self.assertEqual(
            mailboxcheck.classify_login_error(
                imaplib.IMAP4.error("Login fail. service is not open")),
            mailboxcheck.AUTH_REJECTED)

    def test_an_unrecognised_refusal_is_not_blamed_on_the_code(self):
        self.assertEqual(
            mailboxcheck.classify_login_error(imaplib.IMAP4.error("NO something odd")),
            mailboxcheck.LOGIN_REFUSED)


# ---------------------------------------------------------------------------
# 探针：只读，而且先发 ID
# ---------------------------------------------------------------------------


class ProbeTests(unittest.TestCase):
    def test_the_command_order_is_id_then_login_then_examine_then_logout(self):
        """RFC 2971 的 ID 必须**先于** LOGIN：163/126 不发它就直接拒开箱。"""
        with fake_servers({"imap.163.com": {}}) as log:
            state, _ = mailboxcheck.probe_mailbox("imap.163.com", 993,
                                                  "someone@163.com", "an-app-password")
        self.assertEqual(state, mailboxcheck.OK)
        self.assertEqual([entry[1] for entry in log],
                         ["connect", "ID", "login", "select", "logout"])

    def test_the_readonly_branch_uses_a_verb_imaplib_actually_has(self):
        """契约问 imaplib 本身，不搭一个"像 imaplib"的假货去问。

        2026-09-20 的 bug：探针写的是 `client.examine("INBOX")`，而 `imaplib.IMAP4`
        **没有** `examine` 这个方法（`__getattr__` 只认 `Commands` 里注册过的大写动词），
        生产上第一次真跑就是 `AttributeError: Unknown IMAP4 command: 'examine'`——
        而假服务器自己定义了 `examine`，所以 44 条单测全绿。

        第一版这条测试是**造一个没连过网的 IMAP4 实例**去截 `_simple_command`，好读出发出去
        的动词是 `EXAMINE` 还是 `SELECT`。它在 3.9 与宿舍机的 3.14 上都过，却在 CI 的
        **3.14.7** 上炸：没走过 `__init__` 的实例缺 `_encoding`，3.14 的 `__getattr__`
        把它当成命令名去查，抛 `Unknown IMAP4 command: '_encoding'`。
        ——**一个测试不该靠"内部属性恰好够用"活着**，所以改成问两件版本无关的事：
        ① 真客户端**没有** `examine`；② 我们要用的那个动词真的在它的 `Commands` 表里。
        发出去的动词到底是 EXAMINE 还是 SELECT，由下面那条假客户端断言（它记调用）。
        """
        self.assertFalse(hasattr(imaplib.IMAP4, "examine"),
                         "imaplib 现在有 examine 了？那这条探针的写法可以重选一次")
        # 只读那一支走 `select(..., readonly=True)`，imaplib 内部就是发这个动词。
        self.assertIn("EXAMINE", imaplib.Commands,
                      "只读开箱靠的就是它注册的 EXAMINE；没有它这条只读路就不成立")
        self.assertIn("SELECT", imaplib.Commands)
        # 静态那一半走 AST：注释里当然会提到那个动词（它就是这段历史），
        # 要抓的是**真的去点它**的代码。
        with open(pathlib.Path(mailboxcheck.__file__), encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        touched = [node.attr for node in ast.walk(tree)
                   if isinstance(node, ast.Attribute) and node.attr == "examine"]
        self.assertEqual(touched, [],
                         "别再调用 imaplib 没有的那个动词——真机上它会当场抛")

    def test_the_fake_server_refuses_what_the_real_client_does_not_have(self):
        """假服务器照真客户端办事——否则「调了不存在的方法」在单测里是绿的。"""
        with fake_servers({"imap.qq.com": {}}):
            # 直接构造（不经生产调用）时也要给一个真上下文：替身现在会检查这一点。
            server = FakeIMAP("imap.qq.com", 993, ssl_context=ssl.create_default_context())
            with self.assertRaises(AttributeError):
                server.examine("INBOX")

    def test_only_the_read_only_verbs_are_ever_sent(self):
        """铁律 2：只读。假服务器认得 select/uid/store，所以它们出现就会被记下来。"""
        with fake_servers({"imap.qq.com": {}}) as log:
            mailboxcheck.probe_mailbox("imap.qq.com", 993, "someone@qq.com", "pw")
        verbs = {entry[1] for entry in log}
        self.assertEqual(verbs, {"connect", "ID", "login", "select", "logout"})
        for forbidden in ("uid", "store", "delete", "append", "copy", "expunge"):
            self.assertNotIn(forbidden, verbs)

    def test_the_mailbox_is_opened_read_only(self):
        with fake_servers({"imap.qq.com": {}}) as log:
            mailboxcheck.probe_mailbox("imap.qq.com", 993, "someone@qq.com", "pw")
        self.assertIn(("imap.qq.com", "select", "INBOX", True), log)
        selects = [entry for entry in log if entry[1] == "select"]
        self.assertEqual(selects, [("imap.qq.com", "select", "INBOX", True)])

    def test_the_connection_is_dropped_even_when_login_fails(self):
        """A leaked connection keeps the mailbox locked for everyone else."""
        with fake_servers({"imap.qq.com": {"login": "reject"}}) as log:
            mailboxcheck.probe_mailbox("imap.qq.com", 993, "someone@qq.com", "pw")
        self.assertEqual(log[-1][1], "logout")

    def test_a_connect_failure_is_reported_as_the_network_not_as_a_verdict(self):
        with fake_servers({"imap.qq.com": {"connect": "refuse"}}):
            state, words = mailboxcheck.probe_mailbox("imap.qq.com", 993, "a@qq.com", "pw")
        self.assertEqual(state, mailboxcheck.NETWORK)
        self.assertIn("refused", words)

    def test_a_login_that_blows_up_midway_is_the_network(self):
        with fake_servers({"imap.qq.com": {"login": "explode"}}):
            state, _ = mailboxcheck.probe_mailbox("imap.qq.com", 993, "a@qq.com", "pw")
        self.assertEqual(state, mailboxcheck.NETWORK)

    def test_a_refused_examine_is_not_a_refused_code(self):
        """登录过了、箱子打不开：这正是网易「不安全登录」的形状，两档不能混。"""
        with fake_servers({"imap.163.com": {"open": "no"}}):
            state, words = mailboxcheck.probe_mailbox("imap.163.com", 993, "a@163.com", "pw")
        self.assertEqual(state, mailboxcheck.INBOX_REFUSED)
        self.assertIn("Unsafe Login", words)

    def test_the_server_words_lose_the_bytes_wrapper(self):
        with fake_servers({"imap.163.com": {"login": "reject"}}):
            _, words = mailboxcheck.probe_mailbox("imap.163.com", 993, "a@163.com", "pw")
        self.assertNotIn("b'", words)
        self.assertTrue(words.startswith("LOGIN"))


# ---------------------------------------------------------------------------
# 逐行检查的结果里没有凭据
# ---------------------------------------------------------------------------


class CheckAllTests(unittest.TestCase):
    def test_no_result_row_carries_the_password(self):
        with fake_servers({"imap.qq.com": {}, "imap.163.com": {"login": "reject"}}):
            results = mailboxcheck.check_all([
                {"email": "a@qq.com", "imap_host": "imap.qq.com", "imap_port": 993,
                 "password": "secret-one"},
                {"email": "b@163.com", "imap_host": "imap.163.com", "imap_port": 993,
                 "password": "secret-two"},
            ])
        self.assertEqual([item["tier"] for item in results],
                         [mailboxcheck.OK, mailboxcheck.AUTH_REJECTED])
        for item in results:
            for value in item.values():
                self.assertNotIn("secret-", str(value))

    def test_a_row_that_could_not_be_prepared_is_not_probed_at_all(self):
        with fake_servers({"imap.qq.com": {}}) as log:
            results = mailboxcheck.check_all([
                {"email": "a@qq.com", "imap_host": "imap.qq.com", "imap_port": 993,
                 "password": "", "problem": "存着的授权码解不开。"},
            ])
        self.assertEqual(results[0]["tier"], mailboxcheck.CHECK_FAILED)
        self.assertEqual(log, [])


class SummarizeTests(unittest.TestCase):
    def _row(self, tier, probe_state=None):
        return {"tier": tier, "probe_state": probe_state or tier}

    def test_a_clean_run_says_so_in_one_sentence(self):
        text = mailboxcheck.summarize([self._row(mailboxcheck.OK)] * 3)
        self.assertIn("3 个", text)
        self.assertIn("全部能用", text)

    def test_a_mixed_run_names_every_tier_and_the_next_step(self):
        text = mailboxcheck.summarize([
            self._row(mailboxcheck.OK), self._row(mailboxcheck.AUTH_REJECTED),
            self._row(mailboxcheck.HOST_MISMATCH)])
        self.assertIn("1 个能用", text)
        self.assertIn("授权码被拒 1 个", text)
        self.assertIn("主机填错 1 个", text)
        self.assertIn("重新生成", text)
        self.assertIn("配置问题", text)

    def test_an_empty_database_is_not_reported_as_success(self):
        self.assertIn("没有任何已配置", mailboxcheck.summarize([]))


# ---------------------------------------------------------------------------
# 整条命令：真库（临时）+ 假 IMAP
# ---------------------------------------------------------------------------


class CommandHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = Database(os.path.join(self.tmp, "check-mailboxes.sqlite3"))
        self.db.initialize()
        self.addCleanup(FakeIMAP.plan.clear)
        self.addCleanup(FakeIMAP.by_address.clear)

    def add_mailbox(self, email, password, host, port=993, enabled=True, **settings):
        code = f"code-{email}"
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO invites(code_hash,expires_at) VALUES(?,?)",
                (token_hash(code),
                 (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()))
        user = self.db.create_user(email, hash_password("a-long-enough-password"), token_hash(code))
        box = SecretBox.from_base64(_TEST_KEY)
        self.db.upsert_mailbox(user["id"], {
            "email": email, "report_to": email, "imap_host": host, "imap_port": port,
            "smtp_host": host.replace("imap.", "smtp."), "smtp_port": 465,
            "encrypted_password": box.encrypt(password, context=f"mailbox:{user['id']}"),
        })
        if not enabled:
            # 「停止读我的邮箱」是 `mailboxes.enabled`，不是账号状态——两个开关
            # 分开正是当年的一个坑（见 `Database.active_mailboxes` 的说明）。
            with self.db.connect() as connection:
                connection.execute("UPDATE mailboxes SET enabled=0 WHERE email=?", (email,))
        if settings:
            # 「连不上」只有主机、没有地址（连都连不上，哪来的登录名），所以它按主机
            # 记；登录/开箱的回答按**地址**记——同一台服务器上不同账号本来就答得不一样。
            connect = settings.pop("connect", None)
            if connect:
                FakeIMAP.plan.setdefault(host, {})["connect"] = connect
            FakeIMAP.by_address[email] = settings
        return user["id"]

    def run_command(self, *args):
        """跑真正的 `check_mailboxes`，但**一个字节都不出本机**。

        假服务器在这里也必须装上：第一版忘了装，于是这个套件真的连去了
        `imap.qq.com`/`imap.163.com`/`outlook.office365.com`（用假授权码登录、被真
        服务器拒了）。测试碰真实服务商既有隐私问题，也会让我们自己的 IP 被限流——
        所以网络这一层**永远**是假的，和 `test_providercheck` 一样。
        """
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("imaplib.IMAP4_SSL", FakeIMAP), master_key(), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = manage.check_mailboxes(self.db, *args)
        return code, out.getvalue(), err.getvalue()


class CommandOutputTests(CommandHarness):
    def test_the_table_has_one_row_per_person_and_classifies_each_tier(self):
        self.add_mailbox("good@163.com", "pw-good", "imap.163.com")
        self.add_mailbox("refused@163.com", "pw-bad", "imap.163.com",
                         login="reject", words=SERVER_WORDS["netease_password_error"])
        self.add_mailbox("wronghost@126.com", "pw-126", "imap.163.com", login="reject")
        self.add_mailbox("old@outlook.com", "pw-ms", "outlook.office365.com", login="reject",
                         words=SERVER_WORDS["microsoft_disabled"])
        self.add_mailbox("down@qq.com", "pw-net", "imap.qq.com", connect="refuse")
        code, out, _ = self.run_command()
        self.assertEqual(code, 1)
        self.assertIn("能用", out)
        self.assertIn("授权码被拒", out)
        self.assertIn("主机填错", out)
        self.assertIn("这家不支持授权码", out)
        self.assertIn("网络问题", out)
        # 一行一个人：表格里每个打码地址恰好出现一次（明细块会再提一次不属于
        # 「能用」的那些人，那是解释，不是第二行）。
        table = out.split("合计")[0]
        for masked in ("go***@163.com", "re***@163.com", "wr***@126.com",
                       "ol***@outlook.com", "do***@qq.com"):
            self.assertEqual(table.count(masked), 1, f"{masked} 在表格里出现了 "
                             f"{table.count(masked)} 次：\n{table}")

    def test_the_126_row_is_never_reported_as_a_bad_code(self):
        """本轮的核心：主机填错那一行**不能**掉进「授权码被拒」。"""
        self.add_mailbox("wronghost@126.com", "pw-126", "imap.163.com", login="reject")
        code, out, _ = self.run_command()
        self.assertEqual(code, 1)
        row = [line for line in out.splitlines() if "wr***@126.com" in line][0]
        self.assertIn("主机填错", row)
        self.assertNotIn("授权码被拒", row)
        self.assertIn("imap.126.com", out)

    def test_a_clean_run_exits_zero(self):
        self.add_mailbox("good@163.com", "pw", "imap.163.com")
        self.add_mailbox("also@qq.com", "pw2", "imap.qq.com")
        code, out, _ = self.run_command()
        self.assertEqual(code, 0, out)
        self.assertIn("全部能用", out)

    def test_an_unknown_domain_is_named_even_when_it_works(self):
        self.add_mailbox("someone@corp.example.com", "pw", "mail.corp.example.com")
        code, out, _ = self.run_command()
        self.assertEqual(code, 0, out)
        self.assertIn("域名不在任何预置里", out)
        self.assertIn("人工看一眼", out)
        self.assertIn("so***@corp.example.com", out)

    def test_an_unknown_domain_that_fails_is_not_guessed_at(self):
        self.add_mailbox("someone@corp.example.com", "pw", "mail.corp.example.com",
                         connect="refuse")
        code, out, _ = self.run_command()
        self.assertEqual(code, 1)
        self.assertIn("域名不在预置里", out)
        self.assertNotIn("授权码被拒", out)

    def test_a_paused_mailbox_is_still_checked_and_marked(self):
        self.add_mailbox("good@163.com", "pw", "imap.163.com")
        self.add_mailbox("paused@qq.com", "pw2", "imap.qq.com", enabled=False)
        code, out, _ = self.run_command()
        self.assertEqual(code, 0, out)
        self.assertIn("已暂停", out)


class CommandSecrecyTests(CommandHarness):
    """输出里没有授权码、没有密文、没有完整地址、没有主密钥。"""

    def test_neither_the_password_nor_the_ciphertext_nor_the_key_is_printed(self):
        user_id = self.add_mailbox("good@163.com", "a-very-secret-app-password", "imap.163.com")
        self.add_mailbox("refused@163.com", "another-secret-code", "imap.163.com", login="reject")
        code, self.out, _ = self.run_command()
        self.assertEqual(code, 1)
        for secret in ("a-very-secret-app-password", "another-secret-code", _TEST_KEY):
            self.assertNotIn(secret, self.out)
        ciphertext = self.db.get_mailbox(user_id)["encrypted_password"]
        self.assertNotIn(base64.urlsafe_b64encode(ciphertext).decode(), self.out)
        self.assertNotIn("v1:", self.out)

    def test_a_server_that_echoes_the_password_cannot_leak_it(self):
        """「服务器不会回显密码」不是一条能被验证的性质——所以擦洗要兜得住。

        163 的通用错误里**确实**没有任何凭据，但一台被中间人换掉的、或者只是写得
        很烂的服务器完全可能把收到的字符串原样写回错误里。这一条喂的就是那种服务器。
        """
        self.add_mailbox("refused@163.com", "leak-me-please", "imap.163.com",
                         login="reject", echo_password=True)
        code, self.out, _ = self.run_command()
        self.assertEqual(code, 1)
        self.assertNotIn("leak-me-please", self.out)
        self.assertIn("***", self.out)

    def test_the_whole_address_is_never_printed(self):
        self.add_mailbox("zhangsan@163.com", "pw", "imap.163.com", login="reject",
                         echo_password=True)
        _, self.out, _ = self.run_command()
        self.assertNotIn("zhangsan@163.com", self.out)
        self.assertIn("zh***@163.com", self.out)

    def test_a_server_echoing_the_whole_address_cannot_leak_it(self):
        """同一个道理：原话里出现完整地址时，也要按 `_mask` 打码。"""
        self.add_mailbox("zhangsan@163.com", "pw", "imap.163.com", login="reject",
                         words="LOGIN failed for zhangsan@163.com (password error)")
        _, self.out, _ = self.run_command()
        self.assertNotIn("zhangsan@163.com", self.out)
        self.assertIn("zh***@163.com", self.out)


class CommandFailureTests(CommandHarness):
    def test_a_missing_master_key_says_so_without_printing_a_stack(self):
        self.add_mailbox("good@163.com", "pw", "imap.163.com")
        out = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INFE_PILOT_MASTER_KEY", None)
            with contextlib.redirect_stdout(out):
                code = manage.check_mailboxes(self.db)
        self.assertEqual(code, 2)
        self.assertIn("主密钥", out.getvalue())

    def test_an_undecryptable_row_does_not_stop_the_others(self):
        self.add_mailbox("good@163.com", "pw", "imap.163.com")
        user = self.add_mailbox("broken@qq.com", "pw2", "imap.qq.com")
        with self.db.connect() as connection:
            connection.execute("UPDATE mailboxes SET encrypted_password=? WHERE user_id=?",
                               (b"v1:not-really-a-ciphertext", user))
        code, out, _ = self.run_command()
        self.assertEqual(code, 1)
        self.assertIn("没能检查", out)
        self.assertIn("能用", out)

    def test_an_empty_database_is_reported_not_treated_as_a_pass(self):
        code, out, _ = self.run_command()
        self.assertEqual(code, 0)
        self.assertIn("没有任何已配置", out)


class ReadOnlyDatabaseTests(CommandHarness):
    def test_the_command_writes_nothing(self):
        """只读：跑两次，库里每一行的内容与 `updated_at` 都不许变。"""
        self.add_mailbox("good@163.com", "pw", "imap.163.com")
        before = self.db.all_mailboxes()
        self.run_command()
        self.run_command()
        after = self.db.all_mailboxes()
        self.assertEqual(before, after)
