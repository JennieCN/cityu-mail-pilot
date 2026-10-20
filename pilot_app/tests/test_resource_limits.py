"""A4/B1：资源上限——响应不能无限读、一轮不能无限取、大邮件不能整个拉进内存。

审查报告（`handoff/REVIEW-2026-09-22.md` 第四条 P2）指的三处，这里一处一条：

* **API 响应**：`response.read()` 不带上限，`urlopen(timeout=…)` 是 socket 超时、
  不是这次操作的总时限——慢速分块可以把总时长拉得很长；
* **取信**：`UID SEARCH` 把所有 UID 一次性给出来，然后逐封 `BODY.PEEK[]`——
  一个积压邮箱能在**一次**轮询里把线程和这一轮的时间占住；
* **单封大小**：先取整封再截正文，附件也一起进了内存。

三条的判据都不是「跑得快」，而是**可证明的界**：读了多少字节、取了几封、游标推到哪、
以及「过大」这件事有没有留下可见的记录（静默丢掉是最坏的选择）。
"""

from __future__ import annotations

import email.message
import os
import re
import tempfile
import time
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ.setdefault("INFE_PILOT_MASTER_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
os.environ.setdefault("INFE_PILOT_COOKIE_SECURE", "0")
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import mailio, providers  # noqa: E402


class FakeResponse:
    """一个只会一直吐字节的响应（模拟「上游一直发」）。"""

    def __init__(self, chunk: bytes = b"x" * 65536, limit: int | None = None):
        self.chunk = chunk
        self.limit = limit
        self.reads = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size: int = -1) -> bytes:
        self.reads += 1
        if self.limit is not None and self.reads > self.limit:
            return b""
        return self.chunk


class ApiResponseBoundTests(unittest.TestCase):
    def test_a_huge_response_is_cut_off_at_the_cap(self):
        response = FakeResponse(chunk=b"x" * 65536)
        with mock.patch.object(providers, "_outbound_open", return_value=response):
            with self.assertRaises(providers.ProviderError) as caught:
                providers._json_request("https://api.example.com/v1/chat", headers={}, payload={"a": 1})
        message = str(caught.exception)
        self.assertIn("上限", message)
        self.assertIn("Base URL", message, "要给出下一步，而不是只说超了")
        # 读的次数与上限同量级，不是「一直读到天荒地老」
        self.assertLessEqual(response.reads, providers.MAX_RESPONSE_BYTES // 65536 + 2)

    def test_a_normal_response_is_unaffected(self):
        body = b'{"ok": true}'
        response = FakeResponse(chunk=body, limit=1)
        with mock.patch.object(providers, "_outbound_open", return_value=response):
            self.assertEqual(providers._json_request("https://api.example.com/v1/chat",
                                                     headers={}, payload={"a": 1}), {"ok": True})

    def test_a_slow_drip_hits_the_total_deadline(self):
        """socket 超时是两次阻塞之间的间隔；总时限要自己看表。"""
        response = FakeResponse(chunk=b"x" * 1024)
        clock = iter([0.0] + [999.0] * 50)          # 第一次看表正常，之后一律「已经超时」
        with mock.patch.object(providers, "_outbound_open", return_value=response), \
                mock.patch.object(providers.time, "monotonic", side_effect=lambda: next(clock)):
            with self.assertRaises(providers.ProviderTimeout) as caught:
                providers._json_request("https://api.example.com/v1/chat", headers={}, payload={"a": 1},
                                        timeout=30)
        self.assertIn("30 秒", str(caught.exception))
        self.assertLessEqual(response.reads, 2, "超时之后不该还在读")


def _raw_message(message_id: str, body: str = "正文") -> bytes:
    message = email.message.EmailMessage()
    message["From"] = "老师 <student@my.cityu.edu.hk>"
    message["To"] = "me@qq.com"
    message["Subject"] = "作业截止"
    message["Date"] = "Mon, 22 Sep 2026 09:00:00 +0800"
    message["Message-ID"] = message_id
    message.set_content(body)
    return message.as_bytes()


class FakeIMAP:
    """一个记账用的 IMAP 替身：记下每一次 fetch 用了什么取法。"""

    def __init__(self, uids: list[int], sizes: dict[int, int] | None = None):
        self.uids = uids
        self.sizes = sizes or {}
        self.fetches: list[tuple[int, str]] = []
        self.logged_out = False
        self.closed = False

    # -- 协议表面 ---------------------------------------------------------
    def login(self, user, password):
        return "OK", [b"logged in"]

    def select(self, mailbox, readonly=False):
        return "OK", [b"1"]

    def response(self, name):
        return "UIDVALIDITY", [b"1"]

    def uid(self, command, *args):
        if command == "search":
            return "OK", [b" ".join(str(uid).encode() for uid in self.uids)]
        if command == "fetch":
            uid = int(args[0])
            spec = args[1]
            self.fetches.append((uid, spec))
            if "RFC822.SIZE" in spec:
                if uid in self.sizes:
                    return "OK", [f"1 (RFC822.SIZE {self.sizes[uid]})".encode()]
                return "NO", [b"no size"]
            if "HEADER" in spec:
                return "OK", [(b"1 (BODY[HEADER] {0}", _raw_message(f"<h{uid}@x>").split(b"\r\n\r\n")[0] + b"\r\n")]
            return "OK", [(b"1 (BODY[] {0}", _raw_message(f"<m{uid}@x>"))]
        return "NO", [b"unsupported"]

    def close(self):
        self.closed = True

    def logout(self):
        self.logged_out = True


class FetchBoundTests(unittest.TestCase):
    CONFIG = {"email": "me@qq.com", "report_to": "me@qq.com", "imap_host": "imap.example.com",
              "imap_port": 993, "last_uid": 0, "uid_validity": ""}

    def _run(self, client):
        with mock.patch.object(mailio.imaplib, "IMAP4_SSL", return_value=client):
            return mailio.fetch_new_messages(dict(self.CONFIG), "authcode-16chars")

    def test_only_the_batch_cap_is_fetched_and_the_cursor_stops_there(self):
        uids = list(range(1, mailio.MAX_MESSAGES_PER_POLL + 6))
        client = FakeIMAP(uids)
        _, found, highest = self._run(client)
        self.assertEqual(len(found), mailio.MAX_MESSAGES_PER_POLL)
        self.assertEqual(highest, mailio.MAX_MESSAGES_PER_POLL,
                         "游标只该推到**处理过的**那一封，剩下的下一轮接着来")
        body_fetches = [uid for uid, spec in client.fetches if "BODY.PEEK[]" in spec]
        self.assertEqual(body_fetches, uids[:mailio.MAX_MESSAGES_PER_POLL])
        self.assertTrue(client.logged_out, "无论取多少封，都要登出")

    def test_the_next_round_continues_where_it_stopped(self):
        client = FakeIMAP(list(range(1, 31)))
        _, _, highest = self._run(client)
        config = {**self.CONFIG, "last_uid": highest, "uid_validity": "1"}
        client2 = FakeIMAP(list(range(26, 31)))
        with mock.patch.object(mailio.imaplib, "IMAP4_SSL", return_value=client2):
            _, found, highest2 = mailio.fetch_new_messages(config, "authcode-16chars")
        self.assertEqual(highest2, 30)
        self.assertEqual(len(found), 5)

    def test_an_oversized_message_is_not_fetched_and_is_marked(self):
        client = FakeIMAP([1], sizes={1: mailio.MAX_MESSAGE_BYTES + 1})
        _, found, _ = self._run(client)
        specs = [spec for _, spec in client.fetches if "RFC822.SIZE" not in spec]
        self.assertEqual(specs, ["(BODY.PEEK[HEADER])"], f"超大邮件只该取报头，实际取了：{specs}")
        uid, message = found[0]
        self.assertTrue(message["oversized"])
        self.assertEqual(message["body"], "", "正文一个字节都不该留下来")
        self.assertEqual(message["size_bytes"], mailio.MAX_MESSAGE_BYTES + 1)
        self.assertIn("作业截止", message["subject"], "报头还是要解析出来，好让记录说得清是哪封信")

    def test_a_normal_message_still_fetches_the_body(self):
        client = FakeIMAP([1], sizes={1: 40000})
        _, found, _ = self._run(client)
        uid, message = found[0]
        self.assertNotIn("oversized", message)
        self.assertIn("正文", message["body"])

    def test_a_server_without_size_support_still_works(self):
        """问不出大小的服务器（`NO`）不能因此收不到信。"""
        client = FakeIMAP([1])          # sizes 为空 → RFC822.SIZE 回 NO
        _, found, _ = self._run(client)
        self.assertEqual(len(found), 1)
        self.assertIn("正文", found[0][1]["body"])

    def test_large_numbers_are_reported_in_plain_units(self):
        """记录里给的是人能读的数字（MB），不是字节数。"""
        size = 30 * 1024 * 1024
        client = FakeIMAP([1], sizes={1: size})
        _, found, _ = self._run(client)
        message = found[0][1]
        self.assertAlmostEqual(message["size_bytes"] / 1048576, 30.0, places=1)


class VisibleSkipTests(unittest.TestCase):
    """超大邮件要被记成一条**看得见**的跳过，不能静默消失。

    这一条**真的跑一遍 `poll_mailbox`**，而不是去源码里找字符串：落库的那一行才是
    用户与运营者会看到的东西（状态、原因、正文、有没有进队列）。
    """

    def setUp(self):
        import datetime as dt
        import secrets as secrets_mod
        from pilot_app import database as database_mod
        from pilot_app import service as service_mod
        from pilot_app.security import SecretBox, token_hash

        self.db = database_mod.Database(os.path.join(_TMP, "oversized.sqlite3"))
        self.db.initialize()
        self.box = SecretBox(secrets_mod.token_bytes(32))
        code = f"oversized-{os.urandom(4).hex()}"
        with self.db.connect() as connection:
            connection.execute("INSERT INTO invites(code_hash,expires_at) VALUES(?,?)",
                               (token_hash(code),
                                (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()))
        self.email = f"oversized-{os.urandom(4).hex()}@example.com"
        self.user = self.db.create_user(self.email, "x" * 60, token_hash(code))
        self.mailbox_id = f"mbx_{self.user['id'][-8:]}"
        with self.db.connect() as connection:
            connection.execute(
                """INSERT INTO mailboxes(id,user_id,email,report_to,imap_host,imap_port,
                   smtp_host,smtp_port,encrypted_password,updated_at)
                   VALUES(?,?,?,?,'imap.example.com',993,'smtp.example.com',465,?,?)""",
                (self.mailbox_id, self.user["id"], self.email, self.email,
                 self.box.encrypt("authcode-16chars", context=f"mailbox:{self.user['id']}"),
                 dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))
        self.service = service_mod.PilotService(self.db, self.box)

    def _oversized(self, uid: int, size: int = 30 * 1024 * 1024):
        return (uid, {"subject": "作业截止", "sender_name": "老师",
                      "sender_address": "student@my.cityu.edu.hk",
                      "received": "2026-09-22T01:00:00+00:00", "importance": "normal",
                      "body": "", "oversized": True, "size_bytes": size,
                      "message_key": f"<big{uid}@x>"})

    def test_it_lands_as_a_visible_skip_with_no_body_and_is_not_queued(self):
        mailbox = self.db.get_mailbox(self.user["id"])
        with mock.patch.object(mailio, "fetch_new_messages",
                               return_value=("1", [self._oversized(7)], 7)):
            self.service.poll_mailbox(mailbox)
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT status, skip_reason, body FROM messages WHERE user_id=? AND imap_uid=7",
                (self.user["id"],)).fetchone()
        self.assertIsNotNone(row, "过大的邮件也要留一行记录")
        self.assertEqual(row["status"], "skipped")
        self.assertIn("邮件过大", row["skip_reason"])
        self.assertIn("30.0 MB", row["skip_reason"])
        self.assertIn(f"上限 {mailio.MAX_MESSAGE_BYTES // 1048576} MB", row["skip_reason"])
        self.assertIn("删掉那封信", row["skip_reason"], "要说下一步该做什么")
        self.assertIn(row["body"], (b"", ""), "正文一个字节都不该留下来")
        queued = [item["id"] for item in self.db.due_messages(limit=50)]
        self.assertEqual(queued, [], "过大的邮件不该进队列（它没有正文可分析）")
