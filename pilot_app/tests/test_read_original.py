# -*- coding: utf-8 -*-
"""「看原信」：当场从邮箱取回一封，读完就丢。

这个功能的**功能**部分很容易写对，容易写错的是它的两条性质，所以本文件大半在测它们：

1. **它不许把正文留下来。** 隐私政策写着「报告发出后正文立即清空」，而这个功能正好是
   唯一一个能把正文再拉回服务器的地方——顺手缓存一下、或者把正文写进 messages 行，
   功能照常工作，承诺当场失效。所以每次读完都要回头看数据库里那一行还是不是空的。
2. **它不许猜。** 邮箱被重建过（UIDVALIDITY 变了）时，同一串 UID 指的是另一封信；
   别人的 message_id 更是什么都不该发生。取不到就说取不到。
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
import uuid
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["INFE_PILOT_DB"] = _TMP + "/read-original.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
os.environ["INFE_PILOT_MAX_USERS"] = "50"
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import mailio  # noqa: E402
from pilot_app import service as service_mod  # noqa: E402
from pilot_app import web  # noqa: E402
from pilot_app.security import token_hash  # noqa: E402
from pilot_app.web import db, service  # noqa: E402


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


class FakeImap:
    """和 test_mailio 里那个同源：只实现「看原信」会用到的那几个动作。"""

    def __init__(self, *, uid_rows=None, uid_validity=b"1", require_id=False):
        self.uid_rows = uid_rows
        self.uid_validity = uid_validity
        self.require_id = require_id
        self.identified = False
        self.commands = []
        self.uid_calls = []

    def _simple_command(self, name, *args):
        self.commands.append((name, args))
        if name == "ID":
            self.identified = True
        return ("OK", [b""])

    def login(self, user, password):
        self.commands.append(("LOGIN", (user,)))
        return ("OK", [b"LOGIN completed"])

    def select(self, mailbox="INBOX", readonly=False):
        self.commands.append(("EXAMINE" if readonly else "SELECT", (mailbox,)))
        return ("OK", [b"24"])

    def response(self, key):
        return ("UIDVALIDITY", [self.uid_validity])

    def uid(self, verb, *args):
        self.uid_calls.append((verb, args))
        return ("OK", [self.uid_rows])

    def close(self): pass

    def logout(self): pass


def raw_message(subject: str = "作业截止", body: str = "请在周五 23:59 前提交作业。") -> bytes:
    from email.message import EmailMessage
    message = EmailMessage()
    message["From"] = "老师 <student@my.cityu.edu.hk>"
    message["Subject"] = subject
    message["Date"] = "Mon, 14 Sep 2026 04:00:00 +0000"
    message.set_content(body)
    return message.as_bytes()


class OriginalRouteTests(unittest.TestCase):
    """一个账号走天下。

    **这套测试整进程共用一个数据库，试点名额上限是 50**（`test_appearance` 里也写着
    这一条）。第一版每个测试都注册一个新账号——24 个测试就是 24 个账号，直接把名额顶爆，
    然后别处的测试开始莫名其妙地 403：`test_signup` 报「当前试点名额已满」，单跑却过。
    所以账号与夹具都放在 `setUpClass`，只有**确实需要隔离**的测试（限流、别人的信）
    才另开账号，并且一共只开两三个。
    """

    @classmethod
    def setUpClass(cls):
        cls.server = web.create_server("127.0.0.1", 0)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.stamp = dt.datetime.now().timestamp()
        cls.client = Client(cls.base)
        cls.user = cls._register(cls.client, "a")
        cls._connect_model(cls.client)
        cls.message_id, cls.mailbox_id = cls._seed(cls.user, cls.stamp)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        # 用类级的那一份：同一个账号、同一封信、同一个会话。
        self.stamp = type(self).stamp
        self.client = type(self).client
        self.user = type(self).user
        self.message_id = type(self).message_id
        self.mailbox_id = type(self).mailbox_id

    # --- fixtures -----------------------------------------------------------

    @staticmethod
    def _connect_model(client: Client) -> None:
        """给账号配一个**假的**模型连接。

        翻译/总结这条路测的是「取信 → 调模型 → 只回给这一次请求」，
        不是「谁来付钱」——所以这里给一把假 key，真正出网的那一步由 `_fake_model` 挡掉。
        没有它，服务端会（正确地）说「还没有配置 AI 模型」。
        """
        status, body, _ = client.request("PUT", "/api/connections/model", payload={
            "provider": "deepseek", "api_key": "sk-fixture-not-used", "model": "deepseek-flash"})
        assert status == 200, body

    @classmethod
    def _register(cls, client: Client, tag: str, stamp: float | None = None) -> dict:
        stamp = stamp if stamp is not None else cls.stamp
        # 邀请码加一段随机尾巴：同一个 tag 在一次运行里可能被开两次（两处「别人的信」），
        # 而 `invites.code_hash` 是唯一的——撞了就是 IntegrityError，看起来像产品坏了。
        suffix = uuid.uuid4().hex[:8]
        code = f"read-{tag}-{stamp}-{suffix}"
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with db.connect() as connection:
            connection.execute("INSERT INTO invites(code_hash,expires_at) VALUES(?,?)",
                               (token_hash(code), expiry))
        web.reset_signup_rate_limit()  # 见 web.reset_signup_rate_limit：限速按 IP，单测得自己清
        status, user, _ = client.post("/api/auth/register", {
            "email": f"read-{tag}-{stamp}-{suffix}@example.com", "password": "a-long-enough-password",
            "invite_code": code, "accepted_terms": True,
        })
        assert status == 200, user
        return user

    @staticmethod
    def _seed(user: dict, stamp: float) -> tuple[str, str]:
        """一封**已经出过报告**的信：正文按真实流程被清空了。

        邮箱 id 跟着 user 走（`mailboxes.user_id` 是唯一的，一个账号一个邮箱），
        所以同一个函数开多少套夹具都不会撞。
        """
        mailbox_id = f"mbx_{user['id'][-10:]}"
        with db.connect() as connection:
            connection.execute(
                """INSERT INTO mailboxes(id,user_id,email,report_to,imap_host,imap_port,
                   smtp_host,smtp_port,encrypted_password,uid_validity,updated_at)
                   VALUES(?,?,?,?,'imap.example.com',993,'smtp.example.com',465,?,?,?)""",
                (mailbox_id, user["id"], f"box-{stamp}@qq.com", f"box-{stamp}@qq.com",
                 service.secrets.encrypt("授权码", context=f"mailbox:{user['id']}"), "1",
                 "2026-09-14T00:00:00+00:00"),
            )
        message_id = db.insert_message(
            user["id"], mailbox_id, "1", 7,
            {"subject": "作业截止", "sender_name": "老师", "sender_address": "student@my.cityu.edu.hk",
             "received": "2026-09-14T04:00:00+00:00", "importance": "normal",
             "body": service.secrets.encrypt("请提交作业。", context=f"message:{user['id']}")},
        )
        db.finish_message(message_id)          # ← 真实流程：正文在这里被清空
        return message_id, mailbox_id

    def _other_user(self, tag: str) -> Client:
        """另开一个账号（**只在需要「别人的信」时用**），返回它自己的客户端。"""
        client = Client(self.base)
        self._register(client, tag, self.stamp + len(tag))
        return client

    def _own_fixture(self, tag: str) -> tuple[Client, str]:
        """给这个测试**开一整套独立夹具**：新账号 + 自己的邮箱与那封信。

        限流是按人算的，所以测限流的用例必须有自己的预算——否则它会把同一个账号的
        额度吃光，后面的用例全变成 429（这正是第一版的样子）。
        """
        client = Client(self.base)
        user = self._register(client, tag, self.stamp + len(tag) + 1)
        self._connect_model(client)
        message_id, _ = self._seed(user, self.stamp + len(tag) + 1)
        return client, message_id

    def _stored_body(self) -> bytes | str:
        """那一行现在存着的正文。

        类型不固定（`finish_message` 写的是空串，插入时是加密 BLOB），所以断言只看
        「空不空」与「有没有那封信的内容」——**不是**「等于某个具体类型的空值」。
        """
        with db.connect() as connection:
            row = connection.execute("SELECT body FROM messages WHERE id=?", (self.message_id,)).fetchone()
        return row["body"] if row else b"<row gone>"

    def _get(self, message_id: str | None = None, client: Client | None = None):
        target = message_id or self.message_id
        return (client or self.client).get(f"/api/messages/{target}/original")

    # --- the two properties ------------------------------------------------

    def test_reading_a_letter_leaves_nothing_behind(self):
        """读完一遍之后，那一行的正文必须**还是空的**。

        这是整个功能里最容易被「顺手优化掉」的一条：把取回来的正文写回 messages 行，
        或者放进某个缓存，功能一模一样地工作，而隐私政策那句话当场变成假话。
        """
        fake = FakeImap(uid_rows=(b'1 (BODY[] {123}', raw_message()))
        with mock.patch.object(mailio.imaplib, "IMAP4_SSL", return_value=fake):
            status, body, _ = self._get()
        self.assertEqual(status, 200, body)
        self.assertIn("请在周五", body["body"])
        stored = self._stored_body()
        self.assertFalse(stored, "正文不许被写回数据库")
        self.assertNotIn("请在周五", str(stored), "取回来的那封信一个字都不许落库")
        # 取信本身必须是只读的：EXAMINE + BODY.PEEK[]。P1（GPT 审计第二条）之后
        # 前面多了一句 `RFC822.SIZE` 预检、正文取法在问不出大小时还会带上限
        # （`<0.N>`），所以这里钉**性质**——PEEK、正文只取一次——而不是整串字面。
        self.assertIn(("EXAMINE", ("INBOX",)), fake.commands)
        self.assertIn(("fetch", ("7", "(RFC822.SIZE)")), fake.uid_calls, "取正文前先问大小")
        specs = [args[1] for verb, args in fake.uid_calls
                 if verb == "fetch" and "RFC822.SIZE" not in args[1]]
        self.assertEqual(len(specs), 1, f"正文只该取一次：{fake.uid_calls}")
        self.assertIn("BODY.PEEK[]", specs[0])
        self.assertNotIn("BODY[]", specs[0], "不许用会把信标成已读的 BODY[]")

    def test_the_response_says_it_was_read_live_and_where_else_to_look(self):
        fake = FakeImap(uid_rows=(b'1 (BODY[] {123}', raw_message()))
        with mock.patch.object(mailio.imaplib, "IMAP4_SSL", return_value=fake):
            status, body, _ = self._get()
        self.assertEqual(status, 200)
        self.assertTrue(body["live"], "界面靠这个字段写「实时读取、服务器不留存」")
        self.assertEqual(body["subject"], "作业截止")
        self.assertIn("student@my.cityu.edu.hk", body["sender_address"])
        look = body["look_here"]
        self.assertTrue(look, "至少要给出「去哪儿还能看到这封信」")
        # 转发邮箱那一头是 QQ：只能到收件箱，**不该**出现任何「精确到这一封」的说法。
        home = [item for item in look if item["url"] == web.webmail_home(f"box-{self.stamp}@qq.com")]
        self.assertEqual(len(home), 1)
        self.assertIn("收件箱", home[0]["label"])

    # --- it must not guess --------------------------------------------------

    def test_somebody_elses_message_is_a_404(self):
        other = self._other_user("b")
        status, body, _ = self._get(client=other)
        self.assertEqual(status, 404, body)
        self.assertNotIn("作业", json.dumps(body, ensure_ascii=False),
                         "连主题都不该漏给不是主人的人")

    def test_a_rebuilt_mailbox_is_reported_not_guessed(self):
        """UIDVALIDITY 变了 ⇒ 同一串 UID 是另一封信 ⇒ 宁可说取不到。"""
        fake = FakeImap(uid_rows=(b'1 (BODY[] {123}', raw_message(subject="别人的信")),
                         uid_validity=b"99")
        with mock.patch.object(mailio.imaplib, "IMAP4_SSL", return_value=fake):
            status, body, _ = self._get()
        self.assertEqual(status, 410, body)
        self.assertEqual(fake.uid_calls, [], "认不出是哪一封时连取都不取")
        self.assertIn("重建", body["detail"])

    def test_a_letter_that_is_no_longer_there(self):
        fake = FakeImap(uid_rows=None)
        with mock.patch.object(mailio.imaplib, "IMAP4_SSL", return_value=fake):
            status, body, _ = self._get()
        self.assertEqual(status, 404, body)
        self.assertIn("不在你的邮箱里", body["detail"])

    def test_a_mailbox_that_refuses_us_says_why(self):
        class Refusing(FakeImap):
            def select(self, mailbox="INBOX", readonly=False):
                return ("NO", [b"EXAMINE Unsafe Login. Please contact kefu@188.com for help"])

        with mock.patch.object(mailio.imaplib, "IMAP4_SSL", return_value=Refusing()):
            status, body, _ = self._get()
        self.assertEqual(status, 400, body)
        self.assertIn("安全验证", body["detail"], "要把「下一步怎么做」给出来")
        self.assertFalse(self._stored_body(), "失败路径也不许留下正文")

    # --- access control -----------------------------------------------------

    def test_anonymous_callers_are_refused(self):
        status, body, _ = self._get(client=Client(self.base))
        self.assertEqual(status, 401, body)

    def test_an_unknown_id_is_the_same_404_as_somebody_elses(self):
        status, body, _ = self._get(message_id="msg_does_not_exist")
        self.assertEqual(status, 404, body)

    def test_clicking_too_often_is_throttled(self):
        """每次点击都是真开一次 IMAP。把人家的邮箱敲到被限流，比功能坏掉更糟。"""
        client, message_id = self._own_fixture("throttle")     # 自己的预算，别吃别人的
        fake = FakeImap(uid_rows=(b'1 (BODY[] {123}', raw_message()))
        with mock.patch.object(mailio.imaplib, "IMAP4_SSL", return_value=fake):
            for _ in range(web.ORIGINAL_RATE_LIMIT):
                self.assertEqual(self._get(message_id, client)[0], 200)
            status, body, _ = self._get(message_id, client)
        self.assertEqual(status, 429, body)
        self.assertIn("20 次", body["detail"], "限流要说清是多少次，不然用户不知道等多久")

    # --- 翻译 / AI 总结 ------------------------------------------------------

    def _assist(self, kind: str = "translate", *, client: Client | None = None,
                message_id: str | None = None):
        target = message_id or self.message_id
        return (client or self.client).request(
            "POST", f"/api/messages/{target}/assist", payload={"kind": kind})

    def _fake_model(self, text: str = "这是译文。"):
        """假的模型调用：返回一段文本，用量也照给（用量要进 token_usage）。"""
        from pilot_app import providers

        def fake_generate(**_kwargs):
            return providers.Generation(text, [], "none",
                                        {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18})
        return mock.patch("pilot_app.service.providers.generate", side_effect=fake_generate)

    def test_translate_returns_text_and_keeps_nothing(self):
        """翻一遍之后：库里的正文还是空的，而且用量确实记了一笔。"""
        fake = FakeImap(uid_rows=(b'1 (BODY[] {123}', raw_message()))
        with mock.patch.object(mailio.imaplib, "IMAP4_SSL", return_value=fake), self._fake_model("请在周五前交作业。"):
            status, body, _ = self._assist("translate")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["text"], "请在周五前交作业。")
        self.assertIn("/", body["model"])
        self.assertFalse(self._stored_body(), "翻译也不许把正文写回库里")
        with db.connect() as connection:
            rows = connection.execute(
                "SELECT message_id FROM token_usage WHERE user_id=? AND kind='assist-translate'",
                (self.user["id"],)).fetchall()
        self.assertTrue(rows, "翻译/总结同样是模型调用，用量要记（否则「我用了多少」会说假话）")
        self.assertIn(self.message_id, [row["message_id"] for row in rows])

    def test_both_kinds_are_offered(self):
        for kind in ("translate", "summary"):
            fake = FakeImap(uid_rows=(b'1 (BODY[] {123}', raw_message()))
            with mock.patch.object(mailio.imaplib, "IMAP4_SSL", return_value=fake), self._fake_model():
                status, body, _ = self._assist(kind)
            self.assertEqual(status, 200, body)
            self.assertEqual(body["kind"], kind)

    def test_a_kind_we_do_not_offer_is_refused(self):
        """不认识的 kind 当场拦下——不能让它在服务端走到「随便编个提示词」那一步。"""
        status, body, _ = self._assist("translate-into-klingon")
        self.assertEqual(status, 422, body)

    # --- 「模型把原文抄回来了」这一条真机上学到的 -----------------------------

    ENGLISH_LETTER = ("Dear student,\n\nThe deadline is 5 pm on Friday.\n\nRegards,\nRegistry\n\n"
                      "Please contact the office if you have any questions about the arrangement.")

    def _fake_model_calls(self, texts, *, finish: str = ""):
        """假模型，返回一串预设答复，并把每次收到的提示词记下来。"""
        from pilot_app import providers

        replies = list(texts)
        calls: list[str] = []

        def fake_generate(**kwargs):
            calls.append(str(kwargs.get("prompt") or ""))
            answer = replies.pop(0) if replies else texts[-1]
            return providers.Generation(answer, [], "none",
                                        {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
                                        finish)

        patcher = mock.patch("pilot_app.service.providers.generate", side_effect=fake_generate)
        return patcher, calls

    def test_a_model_that_copies_the_letter_back_is_never_shown_as_a_translation(self):
        """真机抓到的样子：英文信进去，**英文原文**出来，而界面显示得跟成功一样。

        这里钉住的是「宁可说没翻出来」：返回的文本必须是空的，note 必须说清这次没成，
        而正文照样不许落库。**不许**把原文当译文递给用户——那是用成功的样子骗人。
        """
        english = self.ENGLISH_LETTER
        fake = FakeImap(uid_rows=(b'1 (BODY[] {123}', raw_message(body=english)))
        patcher, calls = self._fake_model_calls([english, english, english, english])
        with mock.patch.object(mailio.imaplib, "IMAP4_SSL", return_value=fake), patcher:
            status, body, _ = self._assist("translate")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["state"], "unanswered", body)
        self.assertEqual(body["text"], "", "把原文抄回来时一个字都不许当译文显示")
        self.assertIn("没翻出来", body["note"])
        self.assertGreaterEqual(len(calls), 2, "第一次没翻出来时该换一种说法再问一次，而不是直接放弃")
        self.assertFalse(self._stored_body(), "失败了也不许把正文写回库里")

    def test_it_asks_again_in_another_wording_before_giving_up(self):
        """第二次真的换了说法（提示词不一样），而且第二次的回答会被采用。"""
        english = self.ENGLISH_LETTER
        fake = FakeImap(uid_rows=(b'1 (BODY[] {123}', raw_message(body=english)))
        patcher, calls = self._fake_model_calls([english, "亲爱的同学：请在周五下午 5 点前完成。"])
        with mock.patch.object(mailio.imaplib, "IMAP4_SSL", return_value=fake), patcher:
            status, body, _ = self._assist("translate")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["state"], "ok", body)
        self.assertIn("周五", body["text"])
        self.assertEqual(len(calls), 2)
        self.assertNotEqual(calls[0], calls[1], "换说法才算再问一次，重发同一段提示词不算")

    def test_a_translation_cut_off_at_the_output_cap_says_so(self):
        """译文被砍在半路时（finish=length），界面要说「可能被截断」。

        用户看到一段戛然而止的中文，会以为信就写到这里——那是个安静的谎。
        """
        fake = FakeImap(uid_rows=(b'1 (BODY[] {123}', raw_message(body=self.ENGLISH_LETTER)))
        patcher, _ = self._fake_model_calls(["亲爱的同学：请在周五前完成作业。"], finish="length")
        with mock.patch.object(mailio.imaplib, "IMAP4_SSL", return_value=fake), patcher:
            status, body, _ = self._assist("translate")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["state"], "partial", body)
        self.assertIn("截断", body["note"])
        self.assertIn("周五", body["text"], "截断也要把已有的部分给用户")

    def test_a_letter_that_is_already_chinese_is_not_judged(self):
        """原文本来就是中文时，抄回来是对的——不许把这种情形判成「没翻出来」。"""
        chinese = "各位同学：请于本周五前提交作业。"
        fake = FakeImap(uid_rows=(b'1 (BODY[] {123}', raw_message(body=chinese)))
        patcher, calls = self._fake_model_calls([chinese])
        with mock.patch.object(mailio.imaplib, "IMAP4_SSL", return_value=fake), patcher:
            status, body, _ = self._assist("translate")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["state"], "ok", body)
        self.assertEqual(len(calls), 1, "中文原文一次就该通过，不必再问一遍")

    def test_somebody_elses_message_cannot_be_translated(self):
        other = self._other_user("b")
        status, body, _ = self._assist(client=other)
        self.assertEqual(status, 404, body)

    def test_an_anonymous_caller_cannot_spend_our_model_budget(self):
        status, body, _ = self._assist(client=Client(self.base))
        self.assertEqual(status, 401, body)

    def test_a_letter_that_is_gone_says_so_here_too(self):
        fake = FakeImap(uid_rows=None)
        with mock.patch.object(mailio.imaplib, "IMAP4_SSL", return_value=fake), self._fake_model():
            status, body, _ = self._assist()
        self.assertEqual(status, 404, body)
        self.assertIn("不在你的邮箱里", body["detail"])

    def test_it_is_throttled_tighter_than_reading(self):
        """翻译/总结**每次都在花钱**，所以限得比「看原信」紧。"""
        client, message_id = self._own_fixture("budget")       # 同上：自己的预算
        fake = FakeImap(uid_rows=(b'1 (BODY[] {123}', raw_message()))
        with mock.patch.object(mailio.imaplib, "IMAP4_SSL", return_value=fake), self._fake_model():
            for _ in range(web.ASSIST_RATE_LIMIT):
                self.assertEqual(self._assist(client=client, message_id=message_id)[0], 200)
            status, body, _ = self._assist(client=client, message_id=message_id)
        self.assertEqual(status, 429, body)
        self.assertIn("20 次", body["detail"])


class AssistJudgementTests(unittest.TestCase):
    """判「模型有没有真回答」的那两个纯函数，以及分段切法。

    它们决定的是**界面会不会把英文原文当中文译文递出去**，所以单独钉一遍：
    比起走一遍 HTTP，这里能把边界一条条列清楚。
    """

    def _unanswered(self, body: str, answer: str) -> bool:
        return service._assist_unanswered(body, answer)

    def test_an_english_letter_answered_with_english_is_not_an_answer(self):
        self.assertTrue(self._unanswered("Dear student, the deadline is Friday.", "Dear student, the deadline is Friday."))
        self.assertTrue(self._unanswered("Dear student, the deadline is Friday.", "The deadline is on Friday."))

    def test_an_english_letter_answered_in_chinese_is_an_answer(self):
        self.assertFalse(self._unanswered("Dear student, the deadline is Friday.",
                                          "亲爱的同学：截止时间是周五。"))

    def test_a_chinese_letter_is_never_judged(self):
        """中文原文抄回来是对的——判据不能把「不需要翻译」当成失败。"""
        self.assertFalse(self._unanswered("各位同学：请周五前提交作业。", "各位同学：请周五前提交作业。"))

    def test_a_link_heavy_letter_may_pass_on_being_clearly_rewritten(self):
        """原文大半是链接/编号时，中文占比低也算答了——只要它明显不再等于原文。"""
        body = "https://a.example/1 https://a.example/2 https://a.example/3 https://a.example/4"
        answer = "1. https://a.example/1\n2. 第一个链接\n3. https://a.example/3\n4. 第四个链接"
        self.assertFalse(self._unanswered(body, answer))

    def test_an_empty_answer_is_never_an_answer(self):
        self.assertTrue(self._unanswered("Dear student.", ""))

    def test_chunks_keep_every_character_and_respect_paragraphs(self):
        blocks = [f"Paragraph {n}: " + ("word " * 60) for n in range(1, 8)]
        body = "\n\n".join(blocks)
        pieces = service._assist_chunks(body)
        self.assertGreater(len(pieces), 1)
        # 一个字都不许丢：分段只是把它拆开，不是摘要。
        self.assertEqual("".join(pieces).replace("\n", "").replace(" ", ""),
                         body.replace("\n", "").replace(" ", ""))
        for piece in pieces[:-1]:
            self.assertLessEqual(len(piece), service_mod.ASSIST_CHUNK_CHARS * 2)

    def test_a_single_giant_paragraph_is_still_cut(self):
        pieces = service._assist_chunks("x" * 5000)
        self.assertGreater(len(pieces), 1)
        self.assertEqual(sum(len(piece) for piece in pieces), 5000)

    def test_the_translation_budget_grows_with_the_letter(self):
        """1500 的固定预算实测会把译文砍断（finish=length），所以按原文字数给。"""
        short = service._assist_budget("translate", "x" * 200)
        long = service._assist_budget("translate", "x" * 6000)
        self.assertEqual(short, service_mod.ASSIST_MIN_TOKENS)
        self.assertEqual(long, 6000)
        self.assertLessEqual(service._assist_budget("translate", "x" * 90000),
                             service_mod.ASSIST_MAX_TOKENS)
        # 总结本来就是短的，不需要跟着原文涨。
        self.assertEqual(service._assist_budget("summary", "x" * 6000),
                         service_mod.ASSIST_MIN_TOKENS)


class OriginalLinksTests(unittest.TestCase):
    """「还能去哪儿看」：给得出去处，但**每条都要说清精确到什么程度**。"""

    def test_the_school_mailbox_is_a_destination_of_its_own(self):
        links = web.original_links("me@qq.com", "student@my.cityu.edu.hk", "<abc@x>")
        school = [item for item in links if "学校邮箱" in item["label"]]
        self.assertEqual(len(school), 1)
        self.assertEqual(school[0]["url"], web.SCHOOL_WEBMAIL)
        self.assertIn("收件箱", school[0]["detail"], "学校邮箱只能到收件箱，别暗示能直达那一封")

    def test_school_mail_gets_the_link_even_before_he_fills_in_his_school_address(self):
        """邮件本身就是证据（v0.63.85）。

        用户反馈「在看原件的地方能不能直接跳到 outlook 的学校邮箱」——他看不到那一格，
        因为第一版把它挂在"填过学校邮箱吗"上。**每一封我们能读到的信都是从学校转来的**，
        所以这条入口不该再向用户要一遍他已经用行动证明过的东西。
        """
        links = web.original_links("me@qq.com", "", "", school_mail=True)
        self.assertEqual([item for item in links if "学校邮箱" in item["label"]][0]["url"],
                         web.SCHOOL_WEBMAIL)

    def test_no_school_mail_and_no_school_address_means_no_school_link(self):
        """两样都没有时不许凭空给一格：做不到的事不暗示做得到。"""
        links = web.original_links("me@qq.com", "", "")
        self.assertFalse([item for item in links if "学校邮箱" in item["label"]])

    def test_only_gmail_gets_an_exact_link(self):
        # Gmail：有 Message-ID 就能精确定位（#search/rfc822msgid:）
        links = web.original_links("me@gmail.com", "", "<abc.123@mail.example>")
        exact = [item for item in links if "打开这一封" in item["label"]]
        self.assertEqual(len(exact), 1)
        self.assertIn("rfc822msgid", exact[0]["url"])
        # QQ：没有这种办法，就不给「精确」那条
        qq = web.original_links("me@qq.com", "", "<abc.123@mail.example>")
        self.assertFalse([item for item in qq if "打开这一封" in item["label"]])

    def test_a_missing_message_id_means_no_exact_link(self):
        links = web.original_links("me@gmail.com", "", "")
        self.assertFalse([item for item in links if "打开这一封" in item["label"]])

    def test_the_message_id_is_url_encoded(self):
        links = web.original_links("me@gmail.com", "", "<a b+c/@x>")
        exact = [item for item in links if "打开这一封" in item["label"]][0]
        self.assertNotIn(" ", exact["url"])
        self.assertNotIn("<", exact["url"])


class WebmailHomeTests(unittest.TestCase):
    """兜底链接只到收件箱——做不到「精确到那一封」就别在界面上暗示做得到。"""

    def test_known_providers(self):
        self.assertEqual(web.webmail_home("me@qq.com"), "https://mail.qq.com/")
        self.assertEqual(web.webmail_home("me@163.com"), "https://mail.163.com/")
        self.assertEqual(web.webmail_home("me@gmail.com"), "https://mail.google.com/")
        self.assertEqual(web.webmail_home("student@my.cityu.edu.hk"), "https://outlook.office.com/mail/")

    def test_an_unknown_provider_gets_no_link(self):
        self.assertEqual(web.webmail_home("me@some-uni.example"), "")
        self.assertEqual(web.webmail_home(""), "")

    def test_a_lookalike_domain_is_not_matched(self):
        """`notqq.com` 不是 QQ 邮箱；后缀匹配必须按域名边界来。"""
        self.assertEqual(web.webmail_home("me@notqq.com"), "")
