"""Tests for the in-app month calendar on the home page.

Two claims hold this feature up and both are easy to get subtly wrong:

1. it is a **view**, not a store, so a task that just came out of analysis is
   on the page with nothing to import; and
2. a task lands on the day its **deadline** names -- the same day the .ics
   export and the phone feed use, so the app's calendar and the phone's cannot
   show one task on two different days.

The second is the one a user would never be able to report clearly ("my
calendar says Friday but the app said Thursday"), so it is cross-checked here
against the export rather than asserted on its own.
"""

# The dev box runs Python 3.9, the server 3.14; PEP 604 annotations are only
# evaluated lazily with this import, so keep it first.
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
from unittest.mock import patch

_TMP = tempfile.mkdtemp()
os.environ["INFE_PILOT_DB"] = _TMP + "/task-calendar.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
os.environ["INFE_PILOT_MAX_USERS"] = "50"

from pilot_app import web  # noqa: E402
from pilot_app.security import token_hash  # noqa: E402
from pilot_app.web import db, service  # noqa: E402

# 读静态文件必须写明编码：这台机器的默认编码是 GBK，而 index.html 是 UTF-8。
STATIC = pathlib.Path(__file__).resolve().parents[1] / "static"


class Client:
    def __init__(self, base: str) -> None:
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(self.jar))

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


class TaskCalendarTests(unittest.TestCase):
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
        self.stamp = dt.datetime.now().timestamp()
        self.created: list[str] = []
        self.client = Client(self.base)
        self._register()

    def tearDown(self):
        # 整个测试进程共用一个库、一个 `INFE_PILOT_MAX_USERS`（50），所以「每个
        # 用例注册一个账号」花的是共享预算：这一批注册完，test_tasks 那一批就会
        # 当场收到 403「当前名额已满」而红掉（test_compliance 与 test_appearance
        # 里都记着同一件事）。注册了就在这里还回去。
        with db.connect() as connection:
            for user_id in self.created:
                connection.execute("DELETE FROM users WHERE id=?", (user_id,))

    # Hong Kong has no DST, so a fixed offset is exact and needs no tzdata.
    HK = dt.timezone(dt.timedelta(hours=8))

    def _local_today(self) -> dt.date:
        return dt.datetime.now(self.HK).date()

    def _local_day(self, days_from_today: int = 0) -> str:
        return (self._local_today() + dt.timedelta(days=days_from_today)).isoformat()

    def _local_moment(self, days_from_today: int = 0) -> str:
        local = dt.datetime.now(self.HK) + dt.timedelta(days=days_from_today)
        return local.replace(hour=10, minute=0, second=0, microsecond=0).astimezone(
            dt.timezone.utc).isoformat(timespec="seconds")

    def _register(self, suffix: str = ""):
        code = f"calendar-month-invite-{self.stamp}{suffix}"
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with db.connect() as connection:
            connection.execute("INSERT INTO invites(code_hash,expires_at) VALUES(?,?)",
                               (token_hash(code), expiry))
        web.reset_signup_rate_limit()
        status, user, _ = self.client.post("/api/auth/register", {
            "email": f"calendar-month-{self.stamp}{suffix}@example.com",
            "password": "a-long-enough-password", "invite_code": code, "accepted_terms": True,
        })
        self.assertEqual(status, 200, user)
        self.user = user
        self.created.append(user["id"])

    def _seed(self, *, deadline: str = "", arrived_days_ago: int = 0, level: str = "高",
              clock: str = "23:59"):
        """One analyzed mail, optionally naming a deadline. Returns its task_key."""
        action = f"提交作业到 Canvas（截止：{deadline} {clock}）" if deadline else "预习第六章"
        report = (f"## 1. 重要程度与一句话结论\n- 等级：{level}\n- 结论：这封要处理。\n\n"
                  f"## 2. 必须采取的行动与截止时间\n- {action}\n\n"
                  f"## 3. 邮件内容总结\n- 老师布置了作业。\n")
        received = self._local_moment(days_from_today=-arrived_days_ago)
        mailbox_id = f"mbx_cal{self.stamp}{arrived_days_ago}"
        with db.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO mailboxes(id,user_id,email,report_to,imap_host,imap_port,
                   smtp_host,smtp_port,encrypted_password,updated_at)
                   VALUES(?,?,?,?,'h',993,'h',465,?,?)""",
                (mailbox_id, self.user["id"], f"{mailbox_id}@example.com",
                 f"{mailbox_id}@example.com", b"\x00", "2026-09-14T00:00:00+00:00"),
            )
        message_id = db.insert_message(
            self.user["id"], mailbox_id, "1",
            int(dt.datetime.now().timestamp() * 1000) % 100000,
            {"subject": "作业截止", "sender_name": "老师", "sender_address": "t@cityu.edu.hk",
             "received": received, "importance": "normal",
             "body": service.secrets.encrypt("body", context=f"message:{self.user['id']}")},
        )
        self.last_message_id = message_id
        with db.connect() as connection:
            connection.execute("UPDATE messages SET received_at=?,status='sent' WHERE id=?",
                               (received, message_id))
        db.create_report(
            user_id=self.user["id"], message_id=message_id, kind="immediate",
            subject="【AI邮件摘要】作业截止",
            body=service.secrets.encrypt(report, context=f"report:{self.user['id']}"),
            sent_to="pilot@qq.com", received_at=received,
        )
        status, view, _ = self.client.get("/api/tasks")
        self.assertEqual(status, 200, view)
        return view["tasks"]

    def _calendar(self, month: str = ""):
        path = "/api/tasks/calendar" + (f"?month={month}" if month else "")
        status, body, _ = self.client.get(path)
        self.assertEqual(status, 200, body)
        return body

    def _seed_corrupt(self):
        """One analyzed mail whose stored body will not decrypt.

        Not hypothetical: restoring a database under a different master key
        makes *every* report do this, and the calendar reads sixty days of them.
        """
        received = self._local_moment()
        with db.connect() as connection:
            # A user has exactly one mailbox; `_seed` may already have made it.
            row = connection.execute("SELECT id FROM mailboxes WHERE user_id=? LIMIT 1",
                                     (self.user["id"],)).fetchone()
            if row:
                mailbox_id = row["id"]
            else:
                mailbox_id = f"mbx_bad{self.stamp}"
                connection.execute(
                    """INSERT INTO mailboxes(id,user_id,email,report_to,imap_host,imap_port,
                       smtp_host,smtp_port,encrypted_password,updated_at)
                       VALUES(?,?,?,?,'h',993,'h',465,?,?)""",
                    (mailbox_id, self.user["id"], f"{mailbox_id}@example.com",
                     f"{mailbox_id}@example.com", b"\x00", "2026-09-14T00:00:00+00:00"),
                )
        message_id = db.insert_message(
            self.user["id"], mailbox_id, "1",
            int(dt.datetime.now().timestamp() * 1000) % 100000,
            {"subject": "读不出来的那一封", "sender_name": "老师",
             "sender_address": "t@cityu.edu.hk", "received": received,
             "importance": "normal", "body": b"\x00"},
        )
        with db.connect() as connection:
            connection.execute("UPDATE messages SET received_at=?,status='sent' WHERE id=?",
                               (received, message_id))
        db.create_report(
            user_id=self.user["id"], message_id=message_id, kind="immediate",
            subject="【AI邮件摘要】读不出来的那一封",
            body=b"v1:not-actually-ciphertext", sent_to="pilot@qq.com",
        )
        return message_id

    def _cell(self, body: dict, day: str):
        for cell in body["cells"]:
            if cell["day"] == day:
                return cell
        return None

    # -- the shapes the browser needs --------------------------------------

    def test_the_grid_geometry_comes_from_the_server(self):
        """第一个是周几、这个月几天：会因地区而异的规则只留在服务端。

        （「上一个月是哪个月」不再由服务端给：连点 ‹ 时每一次都得从上一次**想去**
        的月份接着算，读服务端回的 prev 会让三下点击都算成同一个月。那条算术在
        `app.js` 的 `calShift` 里，与地区无关。）
        """
        body = self._calendar("2026-10")
        self.assertEqual(body["month"], "2026-10")
        self.assertEqual(body["days_in_month"], 31)
        self.assertEqual(body["first_weekday"], dt.date(2026, 10, 1).weekday(),
                         "0 必须是周一，否则每一列的星期标注都会错位")
        self.assertLessEqual(body["first_weekday"] + body["days_in_month"], 42,
                             "七列网格最多六行")
        self.assertNotIn("prev", body)
        self.assertNotIn("next", body)

    def test_a_month_with_nothing_in_it_is_still_a_month(self):
        body = self._calendar("2026-03")
        self.assertEqual(body["cells"], [])
        self.assertEqual(body["counts"], {"open": 0, "done": 0})

    def test_a_typo_or_a_stale_bookmark_falls_back_to_this_month(self):
        """A read-only page must not turn a bad query string into a 500."""
        for value in ("", "2026", "2026-13", "not-a-month", "2026-1", "2026-10-05"):
            body = self._calendar(value)
            self.assertEqual(body["today"], self._local_today().isoformat())
            if value:
                self.assertEqual(body["month"], self._local_today().strftime("%Y-%m"))

    def test_an_unknown_month_is_not_a_route_not_found(self):
        self.assertEqual(self.client.get("/api/tasks/calendar?month=2026-10")[0], 200)
        self.assertEqual(self.client.get("/api/tasks/calendar")[0], 200)

    def test_the_extreme_years_fall_back_instead_of_raising(self):
        """`9999-12` 找月末要算到公元 10000 年；`0001-01` 的回看窗口要退到公元 0 年。

        两个都是在 `datetime` 里**真的会抛**的算术，而且都能用一个查询串触发 ——
        只读页面被一个月份参数打出一个 500，是这里最不该发生的事。
        """
        this_month = self._local_today().strftime("%Y-%m")
        for value in ("9999-12", "0001-01", "0000-01", "1969-12", "2101-01", "9999-99"):
            body = self._calendar(value)
            self.assertEqual(body["month"], this_month, f"{value} 该退回本月")
            self.assertGreaterEqual(body["first_weekday"], 0)
            self.assertLessEqual(body["first_weekday"] + body["days_in_month"], 42)

    def test_every_month_of_the_allowed_range_can_be_drawn(self):
        """边界内的每一格都要能算出来：翻月按钮正好停在 1970-01 / 2100-12 时也一样。"""
        for value in ("1970-01", "1970-12", "2100-01", "2100-12"):
            body = self._calendar(value)
            self.assertEqual(body["month"], value)
            self.assertLessEqual(body["first_weekday"] + body["days_in_month"], 42)

    def test_a_report_that_will_not_decrypt_does_not_take_the_page_down(self):
        """窗口一放宽，一行坏数据就会出现在每次首页加载的路径上。

        跳过它是必须的（另一边是整页 500），但跳过和丢信在用户眼里一模一样，
        所以 payload 里必须如实报数。
        """
        deadline = self._local_day(2)
        tasks = self._seed(deadline=deadline)
        self._seed_corrupt()
        body = self._calendar(deadline[:7])
        self.assertGreaterEqual(body["unreadable"], 1, "读不出来的那封必须被报出来")
        self.assertTrue(body["index"]["ready"])
        again = self._calendar(deadline[:7])
        self.assertEqual(again["unreadable"], body["unreadable"])
        self.assertTrue(again["index"]["ready"], "真正损坏的源报告不能无限重试")
        cell = self._cell(body, deadline)
        self.assertIsNotNone(cell, "读不出来的那一封不该连累读得出来的")
        self.assertIn(tasks[0]["task_key"], [item["task_key"] for item in cell["items"]])

    def test_nothing_to_skip_means_the_count_is_zero_rather_than_missing(self):
        """界面读 `data.unreadable`；字段缺了会渲染成「undefined 封邮件读不出来」。"""
        body = self._calendar("2026-03")
        self.assertEqual(body["unreadable"], 0)

    # -- where a task lands ------------------------------------------------

    def test_a_task_sits_on_its_deadline_not_on_the_day_its_mail_arrived(self):
        deadline = self._local_day(10)
        self._seed(deadline=deadline, arrived_days_ago=2)
        body = self._calendar(deadline[:7])
        cell = self._cell(body, deadline)
        self.assertIsNotNone(cell, f"截止日 {deadline} 应该有格子：{body['cells']}")
        self.assertEqual(cell["open"], 1)
        self.assertEqual(cell["items"][0]["clock"], "23:59")
        self.assertTrue(cell["items"][0]["dated"])
        self.assertEqual(cell["items"][0]["message_id"], self.last_message_id)
        self.assertEqual(cell["items"][0]["task_day"], self._local_day(-2),
                         "来信那天要留着，用户在清单里看到的就是那一天")
        self.assertIsNone(self._cell(body, self._local_day(-2)),
                          "这一天只是来信日，不是截止日，不该在日历上冒出来")

    def test_a_task_with_no_parseable_deadline_keeps_the_day_it_arrived(self):
        self._seed(arrived_days_ago=1)
        body = self._calendar(self._local_day(-1)[:7])
        cell = self._cell(body, self._local_day(-1))
        self.assertIsNotNone(cell)
        self.assertFalse(cell["items"][0]["dated"],
                         "必须能说出「这是来信日，不是信里写的日子」")
        self.assertEqual(cell["items"][0]["clock"], "")

    def test_a_deadline_in_another_month_does_not_bleed_into_this_one(self):
        deadline = self._local_day(45)
        self._seed(deadline=deadline)
        this_month = self._calendar(self._local_today().strftime("%Y-%m"))
        self.assertEqual(this_month["counts"]["open"], 0,
                         "下个月的截止日期不该出现在这个月的格子里")
        that_month = self._calendar(deadline[:7])
        self.assertIsNotNone(self._cell(that_month, deadline))

    def test_old_mail_is_indexed_for_a_later_deadline_month(self):
        """A report older than the former 60-day window still reaches its deadline month."""
        deadline = self._local_day(30)
        self._seed(deadline=deadline, arrived_days_ago=90)
        body = self._calendar(deadline[:7])
        expected_keys = db.calendar_task_keys(self.user["id"])
        cell = self._cell(body, deadline)
        self.assertIsNotNone(cell)
        self.assertEqual({item["task_key"] for item in cell["items"]}, expected_keys)
        self.assertTrue(body["index"]["ready"])

    def test_rebuilding_after_mailbox_disconnect_keeps_original_arrival_day(self):
        """Reports retain arrival metadata after mailbox cleanup unlinks their messages."""
        deadline = self._local_day(30)
        self._seed(deadline=deadline, arrived_days_ago=90)
        with db.connect() as connection:
            connection.execute("DELETE FROM messages WHERE user_id=?", (self.user["id"],))
        body = self._calendar(deadline[:7])
        expected_keys = db.calendar_task_keys(self.user["id"])
        cell = self._cell(body, deadline)
        self.assertIsNotNone(cell)
        self.assertEqual({item["task_key"] for item in cell["items"]}, expected_keys)
        self.assertTrue(cell["items"][0]["archived"])
        self.assertFalse(cell["items"][0]["message_id"],
                         "源邮件已从邮箱记录移除后，不能显示一个无法使用的原信入口")

    def test_calendar_task_can_be_completed_after_indexing_then_mailbox_disconnects(self):
        deadline = self._local_day(10)
        tasks = self._seed(deadline=deadline)
        self.assertTrue(self._calendar(deadline[:7])["index"]["ready"])
        with db.connect() as connection:
            connection.execute("DELETE FROM messages WHERE user_id=?", (self.user["id"],))

        status, result, _ = self.client.put(
            f"/api/tasks/{tasks[0]['task_key']}", {"state": "done"})
        self.assertEqual(status, 200, result)
        cell = self._cell(self._calendar(deadline[:7]), deadline)
        self.assertEqual(cell["done"], 1)
        self.assertEqual(cell["open"], 0)

    def test_calendar_task_can_be_completed_after_disconnect_and_first_rebuild(self):
        deadline = self._local_day(10)
        self._seed(deadline=deadline, arrived_days_ago=90)
        with db.connect() as connection:
            connection.execute("DELETE FROM messages WHERE user_id=?", (self.user["id"],))
        body = self._calendar(deadline[:7])
        self.assertTrue(body["index"]["ready"])
        self.assertTrue(self._cell(body, deadline)["items"][0]["archived"])
        task_keys = db.calendar_task_keys(self.user["id"])
        self.assertEqual(len(task_keys), 1)

        status, result, _ = self.client.put(
            f"/api/tasks/{next(iter(task_keys))}", {"state": "done"})
        self.assertEqual(status, 200, result)
        cell = self._cell(self._calendar(deadline[:7]), deadline)
        self.assertEqual(cell["done"], 1)
        self.assertEqual(cell["open"], 0)

    def test_another_user_cannot_complete_a_disconnected_calendar_task(self):
        deadline = self._local_day(10)
        tasks = self._seed(deadline=deadline)
        self._calendar(deadline[:7])
        with db.connect() as connection:
            connection.execute("DELETE FROM messages WHERE user_id=?", (self.user["id"],))

        owner_client, owner_user = self.client, self.user
        self.client = Client(self.base)
        self._register(suffix="-other")
        status, result, _ = self.client.put(
            f"/api/tasks/{tasks[0]['task_key']}", {"state": "done"})
        self.assertEqual(status, 404, result)
        self.client, self.user = owner_client, owner_user

    def test_corrupt_calendar_projection_is_invalidated_and_rebuilt_from_report(self):
        deadline = self._local_day(10)
        self._seed(deadline=deadline)
        manual_status, manual, _ = self.client.post(
            "/api/calendar/events", self._manual_payload(deadline))
        self.assertEqual(manual_status, 200, manual)
        body = ("## 1. 重要程度与一句话结论\n- 等级：高\n- 结论：这封要处理。\n\n"
                "## 2. 必须采取的行动与截止时间\n"
                f"- 提交作业到 Canvas（截止：{deadline} 23:59）\n"
                f"- 复习第六章（截止：{deadline} 23:59）\n\n"
                "## 3. 邮件内容总结\n- 老师布置了作业。\n")
        report_body = service.secrets.encrypt(body, context=f"report:{self.user['id']}")
        with db.connect() as connection:
            report = connection.execute(
                "SELECT id FROM reports WHERE user_id=? AND kind='immediate' ORDER BY created_at DESC LIMIT 1",
                (self.user["id"],),
            ).fetchone()
            connection.execute("UPDATE reports SET body_markdown=? WHERE id=?",
                               (report_body, report["id"]))

        first = self._calendar(deadline[:7])
        self.assertEqual(self._cell(first, deadline)["open"], 3)
        with db.connect() as connection:
            damaged = connection.execute(
                "SELECT task_key FROM calendar_task_index WHERE report_id=? LIMIT 1",
                (report["id"],),
            ).fetchone()
            connection.execute(
                "UPDATE calendar_task_index SET encrypted_payload=? WHERE task_key=?",
                (b"broken", damaged["task_key"],),
            )

        repairing = self._calendar(deadline[:7])
        self.assertFalse(repairing["index"]["ready"])
        self.assertEqual(repairing["unreadable"], 0,
                         "源报告可解密时不能算成 unreadable")
        repairing_cell = self._cell(repairing, deadline)
        self.assertEqual(repairing_cell["open"], 1, "修复期间保留完整的手动事项")
        self.assertEqual(repairing_cell["items"][0]["id"], manual["id"])
        self.assertEqual(repairing_cell["items"][0]["source_kind"], "manual")

        rebuilt = self._calendar(deadline[:7])
        self.assertTrue(rebuilt["index"]["ready"])
        self.assertEqual(rebuilt["unreadable"], 0)
        self.assertEqual(self._cell(rebuilt, deadline)["open"], 3,
                         "同一报告中的全部任务都必须从源报告恢复")

    def test_put_rebuilds_corrupt_projection_without_a_calendar_refresh(self):
        corruptions = {
            "ciphertext": lambda key: b"broken",
            "invalid_json": lambda key: service.secrets.encrypt(
                "{", context=f"calendar-index:{self.user['id']}:{key}"),
            "wrong_key": lambda key: service.secrets.encrypt(
                json.dumps({"task_key": "0" * 32}),
                context=f"calendar-index:{self.user['id']}:{key}"),
        }
        for number, (kind, corrupt) in enumerate(corruptions.items()):
            if number:
                self.client = Client(self.base)
                self._register(suffix=f"-repair-{number}")
            deadline = self._local_day(10)
            tasks = self._seed(deadline=deadline)
            task_key = tasks[0]["task_key"]
            self._calendar(deadline[:7])
            with db.connect() as connection:
                connection.execute("DELETE FROM messages WHERE user_id=?", (self.user["id"],))
                connection.execute(
                    "UPDATE calendar_task_index SET encrypted_payload=? WHERE user_id=? AND task_key=?",
                    (corrupt(task_key), self.user["id"], task_key),
                )

            # The PUT itself must repair from the encrypted source report; no
            # intervening calendar GET is allowed to hide a broken recovery path.
            status, result, _ = self.client.put(
                f"/api/tasks/{task_key}", {"state": "done"})
            self.assertEqual(status, 200, f"{kind}: {result}")
            cell = self._cell(self._calendar(deadline[:7]), deadline)
            self.assertEqual(cell["done"], 1, kind)
            self.assertEqual(cell["open"], 0, kind)
            self.assertEqual(self._calendar(deadline[:7])["unreadable"], 0, kind)

    def test_large_history_is_indexed_in_bounded_batches_with_progress(self):
        deadline = self._local_day(10)
        received = self._local_moment()
        report = ("## 1. 重要程度与一句话结论\n- 等级：高\n- 结论：这封要处理。\n\n"
                  "## 2. 必须采取的行动与截止时间\n"
                  f"- 提交作业到 Canvas（截止：{deadline} 23:59）\n\n"
                  "## 3. 邮件内容总结\n- 老师布置了作业。\n")
        for number in range(101):
            db.create_report(
                user_id=self.user["id"], message_id=None, kind="immediate",
                subject=f"报告 {number}",
                body=service.secrets.encrypt(report, context=f"report:{self.user['id']}"),
                sent_to="pilot@qq.com", received_at=received,
            )

        first = self._calendar(deadline[:7])
        self.assertEqual(first["index"]["total"], 101)
        self.assertEqual(first["index"]["indexed"], 100)
        self.assertFalse(first["index"]["ready"])
        self.assertEqual(first["cells"], [], "准备完成前不展示部分日历")

        second = self._calendar(deadline[:7])
        self.assertEqual(second["index"]["indexed"], 101)
        self.assertTrue(second["index"]["ready"])
        self.assertEqual(self._cell(second, deadline)["open"], 101)

    def test_the_calendar_and_the_download_export_name_the_same_day(self):
        """The one a user could never report clearly: two places, one task, two days."""
        deadline = self._local_day(6)
        tasks = self._seed(deadline=deadline)
        key = tasks[0]["task_key"]
        calendar_cell = self._cell(self._calendar(deadline[:7]), deadline)
        self.assertEqual([item["task_key"] for item in calendar_cell["items"]], [key])
        status, ics, _ = self.client.get(
            f"/api/tasks/export.ics?keys={key}&day={self._local_day()}")
        self.assertEqual(status, 200, ics)
        # `DTSTART;` with a parameter, not the bare `DTSTART:` that also appears
        # inside the VTIMEZONE block (1970 is not a deadline).
        start = [line for line in ics.replace("\r\n", "\n").split("\n")
                 if line.startswith("DTSTART;")]
        self.assertTrue(start, ics)
        self.assertIn(deadline.replace("-", ""), start[0],
                      f"导出的日期 {start[0]} 与日历格子 {deadline} 不一致")

    def test_a_midnight_deadline_lands_on_the_day_before(self):
        """「10月17日 00:00 截止」是「16 号睡前」，日历必须放在 16 号那一格。

        用户原话：10.16 号晚上 12 点的 ddl 是我在 16 号睡前就该做完的，而不是归到
        10.17 的 0 点。判据在 `reports.deadline_of`（唯一生成截止时间字符串的地方），
        日历只是把它画出来 —— 所以这里验的是整条链路，而不是日历自己有没有算对。
        """
        target = self._local_day(5)
        expected = self._local_day(4)
        self._seed(deadline=target, clock="00:00")
        body = self._calendar(expected[:7])
        cell = self._cell(body, expected)
        self.assertIsNotNone(cell, f"应落在 {expected}：{body['cells']}")
        item = cell["items"][0]
        self.assertIn("24:00", item["deadline"], "写法也要跟着改，否则格子和文字互相矛盾")
        self.assertEqual(item["clock"], "", "24 点没有具体时刻，不该长出一个凌晨的约会")
        self.assertTrue(item["dated"])
        if target[:7] == expected[:7]:
            self.assertIsNone(self._cell(body, target), f"不该同时出现在 {target}")
        # 导出文件里的日子必须和格子里是同一天。
        status, ics, _ = self.client.get(
            f"/api/tasks/export.ics?keys={item['task_key']}&day={self._local_day()}")
        self.assertEqual(status, 200, ics)
        start = [line for line in ics.replace("\r\n", "\n").split("\n")
                 if line.startswith("DTSTART;")]
        self.assertIn(expected.replace("-", ""), start[0])

    # -- handled, snoozed, and other people's ------------------------------

    def test_a_handled_task_is_counted_but_no_longer_listed(self):
        deadline = self._local_day(5)
        tasks = self._seed(deadline=deadline)
        cell = self._cell(self._calendar(deadline[:7]), deadline)
        self.assertEqual((cell["open"], cell["done"]), (1, 0))
        self.client.put(f"/api/tasks/{tasks[0]['task_key']}", {"state": "done"})
        cell = self._cell(self._calendar(deadline[:7]), deadline)
        self.assertEqual((cell["open"], cell["done"]), (0, 1))
        self.assertEqual(cell["items"], [], "处理过的不该再出现在那天的待办里")

    def test_a_snoozed_task_is_still_a_deadline(self):
        """Snooze means 'not now', not 'not due on Friday'."""
        deadline = self._local_day(8)
        tasks = self._seed(deadline=deadline)
        key = tasks[0]["task_key"]
        self.client.put("/api/tasks/snooze", {"task_key": key, "until": "tomorrow",
                                              "day": self._local_day()})
        cell = self._cell(self._calendar(deadline[:7]), deadline)
        self.assertIsNotNone(cell, "挪走的是清单上的位置，不是这个截止日期")
        self.assertIn(key, [item["task_key"] for item in cell["items"]])

    def test_another_account_sees_none_of_it(self):
        deadline = self._local_day(4)
        self._seed(deadline=deadline)
        stranger = Client(self.base)
        status, _, _ = stranger.get("/api/tasks/calendar")
        self.assertEqual(status, 401)
        self.client = Client(self.base)
        self._register(suffix="-b")
        body = self._calendar(deadline[:7])
        self.assertEqual(body["counts"]["open"], 0, "别人的截止日期不能出现在我的日历上")

    def test_the_payload_carries_what_the_detail_list_needs(self):
        deadline = self._local_day(3)
        self._seed(deadline=deadline)
        item = self._cell(self._calendar(deadline[:7]), deadline)["items"][0]
        for field in ("task_key", "text", "deadline", "clock", "priority", "subject", "task_day"):
            self.assertIn(field, item)
        self.assertIn("提交作业到 Canvas", item["text"])

    # -- the red dot --------------------------------------------------------

    def test_a_day_with_something_urgent_is_flagged_urgent(self):
        """红点的判据是「这天有没有急事」，由服务端给（前端只负责画颜色）。"""
        deadline = self._local_day(4)
        self._seed(deadline=deadline, level="高")
        cell = self._cell(self._calendar(deadline[:7]), deadline)
        self.assertTrue(cell["urgent"])

    def test_a_day_of_ordinary_things_is_not_urgent(self):
        deadline = self._local_day(4)
        self._seed(deadline=deadline, level="中")
        cell = self._cell(self._calendar(deadline[:7]), deadline)
        self.assertEqual(cell["open"], 1)
        self.assertFalse(cell["urgent"], "「中」不是急事，红了就等于每天都是红的")

    def test_urgent_follows_the_users_own_ranking(self):
        """用户自己把一条降成「缓」之后，那一天的圆点不该还是红的。

        这正是 `effective_priority` 存在的理由：报告说「这封重要」和「我决定这件事
        重要」是两件事，而红点必须跟后面那个。
        """
        deadline = self._local_day(4)
        tasks = self._seed(deadline=deadline, level="高")
        self.assertTrue(self._cell(self._calendar(deadline[:7]), deadline)["urgent"])
        status, _, _ = self.client.put(
            f"/api/tasks/{tasks[0]['task_key']}/priority",
            {"priority": "low", "day": self._local_day()})
        self.assertEqual(status, 200)
        cell = self._cell(self._calendar(deadline[:7]), deadline)
        self.assertFalse(cell["urgent"], "用户说它不急，红点就得灭")
        self.assertEqual(cell["open"], 1, "只是不再标红，事情还在那儿")

    def test_a_day_whose_urgent_item_is_handled_is_not_urgent(self):
        """已经处理掉的急事不该继续标红：红点是"还要你做什么"的信号。"""
        deadline = self._local_day(4)
        tasks = self._seed(deadline=deadline, level="高")
        self.client.put(f"/api/tasks/{tasks[0]['task_key']}", {"state": "done"})
        cell = self._cell(self._calendar(deadline[:7]), deadline)
        self.assertEqual(cell["done"], 1)
        self.assertFalse(cell["urgent"])

    def test_the_urgent_mark_wears_the_app_own_red(self):
        """红框与红点必须是报告里「高」用的那个红（`--bad` = #b42318），不是另挑一个。

        `reports._PRIORITY_COLORS[PRIORITY_HIGH]` 的第一项就是这个值；两处同色
        是刻意的：同一天在清单里标红、在日历上标红，用户才会认为它们是同一件事。

        急事红框比普通日期的透明边框粗一档：只靠颜色的话，色觉障碍的人拿不到这条信息。
        """
        page = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertIn("--bad:#b42318", page)
        self.assertIn(".cal-dots .open.urgent{background:var(--bad)}", page)
        self.assertIn(".cal-cell.urgent{border-color:var(--bad);border-width:2px}", page)
        self.assertIn("border:1px solid transparent", page,
                      "普通日期不应再画出一整格边框")
        self.assertIn(".cal-cell.has{cursor:pointer}", page,
                      "普通待办日期用圆点标记，避免每个日期都套边框")
        self.assertIn(".cal-cell.current.urgent{outline-color:var(--blue)}", page,
                      "选中急事日仍保留红框，并用蓝圈表达选中")
        self.assertIn(".cal-item .cal-complete{flex:0 0 auto;min-height:44px;padding:8px 12px}", page)
        self.assertIn(".cal-item .cal-original{display:block;margin-top:4px;padding:8px 12px;min-height:44px;font-size:13px}", page)
        self.assertIn("点有标记的日期查看待办", page)
        script = (STATIC / "app.js").read_text(encoding="utf-8")
        self.assertIn("node.setAttribute('aria-current', 'date')", script)
        self.assertIn("node.setAttribute('aria-pressed', String(key === showing))", script)
        self.assertIn("const tick = el('button', 'secondary cal-complete', '完成')", script)
        self.assertIn("const when = item.dated ? '' : '无截止日期 · 按来信日期显示'", script)
        self.assertIn("function openOriginalUnavailable(task)", script)
        self.assertIn("body.appendChild(el('div', 'when cal-original-unavailable', '原信暂不可用'))", script)

    # -- user-authored events ---------------------------------------------

    def _manual_payload(self, day, **changes):
        payload = {"create_key": "manual-event-request-1", "title": "<b>Course review</b>",
                   "notes": "bring notes", "date": day, "time": "", "timezone": "Asia/Hong_Kong",
                   "all_day": True, "fold": None}
        payload.update(changes)
        return payload

    def test_manual_event_crud_idempotency_export_and_account_isolation(self):
        day = self._local_day(2)
        payload = self._manual_payload(day)
        status, event, _ = self.client.post("/api/calendar/events", payload)
        self.assertEqual(status, 200, event)
        self.assertEqual(event["source_kind"], "manual")
        self.assertEqual(event["revision"], 1)
        event_id = event["id"]

        status, replay, _ = self.client.post("/api/calendar/events", payload)
        self.assertEqual(status, 200, replay)
        self.assertEqual(replay["id"], event_id)
        self.assertEqual(self.client.post("/api/calendar/events", {**payload, "title": "different"})[0], 409)
        self.assertEqual(self.client.get(f"/api/calendar/events/{event_id}")[0], 200)

        cell = self._cell(self._calendar(day[:7]), day)
        self.assertEqual(cell["open"], 1)
        self.assertEqual(cell["items"][0]["source_kind"], "manual")
        self.assertNotIn("message_id", cell["items"][0])

        status, edited, _ = self.client.put(f"/api/calendar/events/{event_id}", {
            "expected_revision": 1, "title": "Revised title", "notes": "updated notes",
        })
        self.assertEqual(status, 200, edited)
        self.assertEqual(edited["revision"], 2)
        self.assertEqual(edited["schedule_revision"], event["schedule_revision"])
        self.assertEqual(self.client.put(f"/api/calendar/events/{event_id}", {
            "expected_revision": 1, "title": "stale overwrite",
        })[0], 409)

        exported = self.client.get("/api/account/export")[1]
        self.assertIn(event_id, [row["id"] for row in exported["calendar_events"]])
        owner_client = self.client
        self.client = Client(self.base)
        self._register(suffix="-manual-stranger")
        self.assertEqual(self.client.get(f"/api/calendar/events/{event_id}")[0], 404)
        self.assertEqual(self.client.put(f"/api/calendar/events/{event_id}", {
            "expected_revision": 2, "title": "foreign update",
        })[0], 404)
        self.assertEqual(self.client.request("DELETE", f"/api/calendar/events/{event_id}", {
            "expected_revision": 2,
        })[0], 404)
        self.assertEqual(self.client.get("/api/account/export")[1]["calendar_events"], [])
        self.client = owner_client

        status, done, _ = self.client.put(f"/api/calendar/events/{event_id}", {
            "expected_revision": 2, "state": "done",
        })
        self.assertEqual(status, 200, done)
        cell = self._cell(self._calendar(day[:7]), day)
        self.assertEqual(cell["manual_done"][0]["title"], "Revised title")
        self.assertEqual(cell["manual_done"][0]["source_kind"], "manual")

        status, reopened, _ = self.client.put(f"/api/calendar/events/{event_id}", {
            "expected_revision": done["revision"], "state": "open",
        })
        self.assertEqual(status, 200, reopened)
        status, deleted, _ = self.client.request("DELETE", f"/api/calendar/events/{event_id}", {
            "expected_revision": reopened["revision"],
        })
        self.assertEqual(status, 200, deleted)
        self.assertEqual(deleted["state"], "deleted")
        self.assertNotIn("title", deleted)
        again = self.client.post("/api/calendar/events", payload)[1]
        self.assertEqual(again["state"], "deleted")
        self.assertNotIn("title", again)
        self.assertEqual(self.client.get(f"/api/calendar/events/{event_id}")[0], 404)

    def test_manual_events_remain_visible_when_mail_index_is_not_ready(self):
        day = self._local_day(3)
        status, event, _ = self.client.post("/api/calendar/events", self._manual_payload(day))
        self.assertEqual(status, 200, event)
        with patch.object(db, "calendar_reports_to_index", return_value=[]), \
                patch.object(db, "calendar_index_progress", return_value={
                    "indexed": 0, "total": 1, "unreadable": 0,
                }):
            data = self._calendar(day[:7])
        self.assertFalse(data["index"]["ready"])
        cell = self._cell(data, day)
        self.assertEqual(cell["items"][0]["id"], event["id"])
        self.assertEqual(data["counts"]["open"], 1)

    def test_dst_ambiguous_api_write_requires_explicit_fold(self):
        payload = self._manual_payload("2026-11-01", title="DST review", time="01:30",
                                       timezone="America/New_York", all_day=False)
        self.assertEqual(self.client.post("/api/calendar/events", payload)[0], 422)
        first = self.client.post("/api/calendar/events", {**payload, "create_key": "fold-first", "fold": 0})
        second = self.client.post("/api/calendar/events", {**payload, "create_key": "fold-second", "fold": 1})
        self.assertEqual(first[0], 200, first[1])
        self.assertEqual(second[0], 200, second[1])
        self.assertNotEqual(first[1]["starts_at_utc"], second[1]["starts_at_utc"])
        gap = self.client.post("/api/calendar/events", {
            **payload, "create_key": "dst-gap", "date": "2026-03-08", "time": "02:30", "fold": 0,
        })
        self.assertEqual(gap[0], 422)

        unambiguous = self.client.post("/api/calendar/events", {
            **payload, "create_key": "fold-change", "date": "2026-10-25", "fold": None,
        })
        self.assertEqual(unambiguous[0], 200, unambiguous[1])
        event = unambiguous[1]
        changed = {"expected_revision": event["revision"], "date": "2026-11-01", "time": "01:30"}
        self.assertEqual(self.client.put(f"/api/calendar/events/{event['id']}", changed)[0], 422)
        selected = self.client.put(f"/api/calendar/events/{event['id']}", {**changed, "fold": 1})
        self.assertEqual(selected[0], 200, selected[1])
        self.assertEqual(selected[1]["schedule_revision"], event["schedule_revision"] + 1)


if __name__ == "__main__":
    unittest.main()
