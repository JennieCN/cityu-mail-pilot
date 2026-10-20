# -*- coding: utf-8 -*-
"""「要不要收到报告邮件」：关掉的是**投递**，不是处理。

这一轮修的是一条真陷阱：`active_mailboxes()` 曾经拿 `immediate_enabled` 过滤，于是
「收到新邮件后立即发送摘要」这个勾选一关，worker 连这个邮箱都不再读了——待办、简报的
证据、「看原信」全部一起消失，而设置页上写的是"要不要发邮件"。

所以这个文件测的不是"有没有这个开关"，而是两条容易被改回去的性质：

1. **关掉之后照样读邮箱、照样生成报告**（`mailio.send_report` 一次都不许被调用，
   但报告行必须存在、待办必须还在、正文必须照样清空）；
2. **没发就是没发**：那些信在库里是 `held`，不是 `sent`（假话）、不是 `failed`（会报警）、
   不是 `skipped`（那是"不是本校来信"，会进简报的"同类"清单）。
"""

from __future__ import annotations

import datetime as dt
import http.cookiejar
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["INFE_PILOT_DB"] = _TMP + "/report-delivery.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
os.environ["INFE_PILOT_MAX_USERS"] = "50"
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import database as database_module  # noqa: E402
from pilot_app import mailio  # noqa: E402
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

    def put(self, path, payload=None):
        return self.request("PUT", path, payload=payload)


def _register(client: Client, stamp: float) -> dict:
    """注册一个账号（邀请码与邮箱都带随机尾巴：同一次运行里可以开好几个）。"""
    suffix = uuid.uuid4().hex[:8]
    code = f"delivery-{stamp}-{suffix}"
    expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
    with db.connect() as connection:
        connection.execute("INSERT INTO invites(code_hash,expires_at) VALUES(?,?)",
                           (token_hash(code), expiry))
    web.reset_signup_rate_limit()  # 见 web.reset_signup_rate_limit：限速按 IP，单测得自己清
    status, user, _ = client.request("POST", "/api/auth/register", {
        "email": f"delivery-{stamp}-{suffix}@example.com",
        "password": "a-long-enough-password", "invite_code": code, "accepted_terms": True,
    })
    assert status == 200, user
    return user


class DeliverySwitchTests(unittest.TestCase):
    """一个账号走天下（同 `test_read_original`：整进程共用一个库，名额上限 50）。"""

    @classmethod
    def setUpClass(cls):
        cls.server = web.create_server("127.0.0.1", 0)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.stamp = dt.datetime.now().timestamp()
        cls.client = Client(cls.base)
        cls.user = cls._register(cls.client)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        # 每个测试都从"两种都发"开始：开关是这一轮的主角，残留状态会让断言说不清。
        self.client.put("/api/reports/delivery", {"immediate": True, "daily": True})

    @classmethod
    def _register(cls, client: Client) -> dict:
        return _register(client, cls.stamp)

    # --- 端点本身 -----------------------------------------------------------

    def test_both_options_must_be_given(self):
        """一次点击 = 一次完整写入：缺一个字段就 422，不产生"半开"的状态。"""
        status, body, _ = self.client.put("/api/reports/delivery", {"immediate": False})
        self.assertEqual(status, 422, body)
        status, body, _ = self.client.put("/api/reports/delivery", {"immediate": True, "daily": "yes"})
        self.assertEqual(status, 422, body)

    def test_it_writes_both_fields_and_reads_back(self):
        status, body, _ = self.client.put("/api/reports/delivery", {"immediate": False, "daily": True})
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"ok": True, "immediate": False, "daily": True})
        profile = db.get_profile(self.user["id"])
        self.assertFalse(profile["immediate_enabled"])
        self.assertTrue(profile["daily_enabled"])
        # 默认值不许变：新开关不许在升级当天改变任何人的邮件。
        self.assertEqual(db.report_delivery(self.user["id"]), {"immediate": False, "daily": True})

    def test_an_anonymous_caller_cannot_change_anybody(self):
        status, body, _ = Client(self.base).put("/api/reports/delivery",
                                                {"immediate": False, "daily": False})
        self.assertEqual(status, 401, body)
        self.assertEqual(db.report_delivery(self.user["id"]), {"immediate": True, "daily": True})

    def test_changing_one_account_does_not_touch_another(self):
        other = Client(self.base)
        other_user = self._register(other)
        other.put("/api/reports/delivery", {"immediate": False, "daily": False})
        self.assertEqual(db.report_delivery(self.user["id"]), {"immediate": True, "daily": True})
        self.assertEqual(db.report_delivery(other_user["id"]), {"immediate": False, "daily": False})

    def test_the_default_for_a_fresh_account_is_still_send(self):
        fresh = Client(self.base)
        user = self._register(fresh)
        self.assertEqual(db.report_delivery(user["id"]), {"immediate": True, "daily": True})

    def test_a_missing_profile_still_defaults_to_sending(self):
        """没有 profile 行（迁移中途/老账号）时不许把人的邮件静音。"""
        self.assertEqual(db.report_delivery("usr_does_not_exist"),
                         {"immediate": True, "daily": True})


class DeliveryBehaviourTests(unittest.TestCase):
    """关掉之后真正发生的事：**照样处理、不发信、记 held**。

    注册要一个真会话，所以这一组自己起一个服务端（每个套件一个独立库是
    `run_browser_checks.sh` 的规矩，这里只是要一个能建账号的 HTTP 面）。
    """

    @classmethod
    def setUpClass(cls):
        cls.db = db
        cls.stamp = dt.datetime.now().timestamp()
        cls.server = web.create_server("127.0.0.1", 0)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.client = Client("http://127.0.0.1:%d" % cls.server.server_address[1])
        cls.user = _register(cls.client, cls.stamp)
        # 假的模型连接：这条路测的是"发不发"，不是"谁来付钱"。真出网那一步由
        # 下面 `_run` 里的假 `providers.generate` 挡掉。
        status, body, _ = cls.client.request("PUT", "/api/connections/model", payload={
            "provider": "deepseek", "api_key": "sk-fixture-not-used", "model": "deepseek-flash"})
        assert status == 200, body
        cls.service = web.get_service()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    _seq = 0

    @classmethod
    def _next_uid(cls) -> int:
        """每封夹具信一个自己的 IMAP UID：撞了会被 UNIQUE 忽略，回来的是上一行。"""
        cls._seq += 1
        return 1000 + cls._seq

    def _message(self, stamp: str) -> dict:
        """一封本校来信，正文已加密入库（和真实 ingest 一样）。"""
        mailbox_id = f"mbx_{self.user['id'][-10:]}"
        with self.db.connect() as connection:
            exists = connection.execute("SELECT 1 FROM mailboxes WHERE id=?", (mailbox_id,)).fetchone()
            if not exists:
                connection.execute(
                    """INSERT INTO mailboxes(id,user_id,email,report_to,imap_host,imap_port,
                       smtp_host,smtp_port,encrypted_password,uid_validity,updated_at)
                       VALUES(?,?,?,?,'imap.example.com',993,'smtp.example.com',465,?,?,?)""",
                    (mailbox_id, self.user["id"], f"box-{stamp}@qq.com", f"box-{stamp}@qq.com",
                     self.service.secrets.encrypt("授权码", context=f"mailbox:{self.user['id']}"),
                     "1", "2026-09-18T00:00:00+00:00"),
                )
        message_id = self.db.insert_message(
            self.user["id"], mailbox_id, "1", self._next_uid(),
            {"subject": "作业截止提醒", "sender_name": "老师",
             "sender_address": "student@my.cityu.edu.hk",
             "received": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
             "importance": "normal",
             "body": self.service.secrets.encrypt("请在周五前提交作业。",
                                                  context=f"message:{self.user['id']}")},
        )
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
        return dict(row)

    def _run(self, *, deliver: bool):
        """跑一遍真实处理路径，返回（是否成功, 发送调用次数, 邮件行）。"""
        message = self._message(str(self.stamp + (1 if deliver else 2)))
        self.db.upsert_profile(self.user["id"], {"immediate_enabled": 1 if deliver else 0,
                                                "daily_enabled": 1 if deliver else 0})
        sent: list[tuple] = []
        from pilot_app import providers

        def fake_generate(**_kwargs):
            return providers.Generation("## 1. 重要程度与一句话结论\n- 等级：高\n- 结论：交作业\n",
                                        [], "none", {"total_tokens": 12})

        with mock.patch("pilot_app.service.providers.generate", side_effect=fake_generate), \
             mock.patch.object(mailio, "send_report", side_effect=lambda *a, **k: sent.append(a)):
            ok = self.service.process_message(message)
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM messages WHERE id=?", (message["id"],)).fetchone()
            report = connection.execute("SELECT * FROM reports WHERE message_id=?",
                                        (message["id"],)).fetchone()
        return ok, len(sent), dict(row), dict(report) if report else {}

    def test_a_held_mail_is_processed_and_keeps_its_task(self):
        ok, sends, row, report = self._run(deliver=False)
        self.assertTrue(ok, "关掉报告邮件不等于处理失败")
        self.assertEqual(sends, 0, "关掉之后一次邮件都不许发")
        self.assertEqual(row["status"], "held")
        # 类型不固定（清空写的是空串，插入时是加密 BLOB），所以只看"空不空"。
        self.assertFalse(row["body"], "不发邮件也不是留下正文的理由")
        self.assertEqual(report.get("status"), "generated", "报告还是要生成（App 里的待办靠它）")
        self.assertFalse(report.get("sent_at"))

    def test_with_the_switch_on_it_still_sends_and_says_sent(self):
        ok, sends, row, report = self._run(deliver=True)
        self.assertTrue(ok)
        # 可能不止一封：开启「来信即时提醒」时是一封提醒 + 一封报告。这里只要求"真的发了"，
        # 具体几封由别的测试管；关掉时必须是 0（下面那条）。
        self.assertGreaterEqual(sends, 1, "默认行为不许变：开关开着照发")
        self.assertEqual(row["status"], "sent")
        self.assertEqual(report.get("status"), "sent")
        self.assertTrue(report.get("sent_at"))

    def test_polling_no_longer_depends_on_the_switch(self):
        """这一条是整轮的核心：关掉报告邮件之后，邮箱**照样**是"该读的"。"""
        self.db.upsert_profile(self.user["id"], {"immediate_enabled": 0})
        with self.db.connect() as connection:
            connection.execute("UPDATE mailboxes SET enabled=1 WHERE user_id=?", (self.user["id"],))
        ids = {row["user_id"] for row in self.db.active_mailboxes()}
        self.assertIn(self.user["id"], ids)
        # 「暂停收信」是另一个开关，它才是真的不看。
        with self.db.connect() as connection:
            connection.execute("UPDATE mailboxes SET enabled=0 WHERE user_id=?", (self.user["id"],))
        ids = {row["user_id"] for row in self.db.active_mailboxes()}
        self.assertNotIn(self.user["id"], ids)
        with self.db.connect() as connection:
            connection.execute("UPDATE mailboxes SET enabled=1 WHERE user_id=?", (self.user["id"],))

    def test_held_mail_is_not_counted_as_undelivered(self):
        """运营者面板上的「未送达」是故障清单，held 不是故障。"""
        self._run(deliver=False)
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE reports SET status='generated',sent_at=NULL WHERE user_id=?", (self.user["id"],))
        with self.db.connect() as connection:
            held_ids = {row["id"] for row in connection.execute(
                "SELECT id FROM messages WHERE user_id=? AND status='held'", (self.user["id"],))}
        self.assertTrue(held_ids)
        page = self.db.list_messages_overview(limit=200, status="undelivered", user_id=self.user["id"])
        self.assertFalse(held_ids & {row["id"] for row in page["messages"]},
                         "held 不许出现在未送达里")
        held = self.db.list_messages_overview(limit=200, status="held", user_id=self.user["id"])
        self.assertTrue(held["messages"], "held 要能单独筛出来")
        # 屏幕上那一个字同样要认它。
        self.assertEqual(web._delivery_state({"status": "held"}), "held")

    def test_it_still_counts_as_evidence_that_forwarding_works(self):
        """「学校那封信真的到了吗」的证据按 `status != 'skipped'` 算——held 算数。"""
        before = self.db.count_analysed_messages(self.user["id"])
        self._run(deliver=False)
        self.assertEqual(self.db.count_analysed_messages(self.user["id"]), before + 1)


class SchemaMigrationTests(unittest.TestCase):
    """老库要能加上 `held`：SQLite 改不了 CHECK，只能重建，而且必须幂等、不丢数据。"""

    OLD_SCHEMA = (
        "CREATE TABLE messages("
        " id TEXT PRIMARY KEY, user_id TEXT NOT NULL, mailbox_id TEXT NOT NULL,"
        " uid_validity TEXT NOT NULL DEFAULT '', imap_uid INTEGER NOT NULL,"
        " subject TEXT NOT NULL, sender_name TEXT NOT NULL DEFAULT '',"
        " sender_address TEXT NOT NULL DEFAULT '', received_at TEXT NOT NULL,"
        " importance TEXT NOT NULL DEFAULT 'normal', message_key TEXT, body BLOB NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'pending'"
        "   CHECK(status IN ('pending','processing','sent','failed','skipped')),"
        " skip_reason TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,"
        " next_attempt_at TEXT, last_error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,"
        " UNIQUE(mailbox_id, uid_validity, imap_uid))"
        ";CREATE INDEX idx_messages_due ON messages(status, next_attempt_at)"
    )

    def _old_database(self, path: str) -> None:
        connection = sqlite3.connect(path)
        connection.executescript(self.OLD_SCHEMA)
        connection.execute(
            "INSERT INTO messages(id,user_id,mailbox_id,uid_validity,imap_uid,subject,"
            "sender_address,received_at,body,status,created_at)"
            " VALUES('msg_old','usr_x','mbx_x','1',7,'旧信','student@my.cityu.edu.hk',"
            "'2026-09-01T00:00:00+00:00',X'00','sent','2026-09-01T00:00:00+00:00')")
        connection.commit()
        connection.close()

    def test_an_old_database_gains_the_held_status_without_losing_rows(self):
        path = _TMP + "/old-schema.sqlite3"
        self._old_database(path)
        database = database_module.Database(path)
        database.initialize()
        with database.connect() as connection:
            definition = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='messages'").fetchone()[0]
            self.assertIn("held", definition)
            connection.execute(
                "UPDATE messages SET status='held' WHERE id='msg_old'")
            row = connection.execute("SELECT subject,status FROM messages WHERE id='msg_old'").fetchone()
        self.assertEqual((row["subject"], row["status"]), ("旧信", "held"))

    def test_running_it_twice_changes_nothing(self):
        path = _TMP + "/old-schema-twice.sqlite3"
        self._old_database(path)
        database = database_module.Database(path)
        database.initialize()
        database.initialize()
        with database.connect() as connection:
            rows = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        self.assertEqual(rows, 1)


class SchoolMailLinkTests(unittest.TestCase):
    """需求二：看原信里要能直接跳学校 Outlook——**不必先填过学校邮箱**。"""

    def test_school_mail_gets_the_school_link_without_a_saved_school_address(self):
        links = web.original_links("me@qq.com", "", "", school_mail=True)
        school = [item for item in links if "学校邮箱" in item["label"]]
        self.assertEqual(len(school), 1)
        self.assertEqual(school[0]["url"], web.SCHOOL_WEBMAIL)
        self.assertIn("复制主题", school[0]["detail"], "到不了那一封就要说清怎么自己找")

    def test_a_forwarding_mailbox_we_do_not_know_is_just_the_inbox(self):
        """非 Gmail 的转发邮箱没有单封链接——不给假深链。"""
        links = web.original_links("me@163.com", "", "<abc@x>", school_mail=True)
        self.assertFalse([item for item in links if "Gmail" in item["label"]])

    def test_gmail_can_still_point_at_the_exact_message(self):
        links = web.original_links("me@gmail.com", "", "<abc@x>", school_mail=True)
        self.assertTrue([item for item in links if "Gmail" in item["label"]])

    def test_no_school_mail_and_no_saved_address_means_no_school_entry(self):
        """没填过、又不是学校来信时不要凭空给一格——做不到的事不暗示做得到。"""
        links = web.original_links("me@qq.com", "", "")
        self.assertFalse([item for item in links if "学校邮箱" in item["label"]])


if __name__ == "__main__":
    unittest.main()
