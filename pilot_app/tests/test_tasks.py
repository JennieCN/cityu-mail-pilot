"""Tests for the daily task list: hide one, and find it again later.

The feature is small but it has one property that is easy to break and hard to
notice: hiding must not destroy anything. Tasks are re-derived from report text
on every request, so a "done" mark is a separate row keyed by a hash of the
content — never a deletion, and never a position in a list. Most of what follows
exists to pin that down, because the failure mode (a task quietly gone, with the
report it came from edited or dropped) would look like nothing at all.
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
import unittest.mock as mock
import urllib.error
import urllib.request

_TMP = tempfile.mkdtemp()
os.environ["INFE_PILOT_DB"] = _TMP + "/tasks.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
os.environ["INFE_PILOT_MAX_USERS"] = "50"
os.environ.pop("INFE_PILOT_ORIGIN", None)
# NOTE: do NOT touch INFE_PILOT_ADMIN_EMAILS or INFE_PILOT_CONTACT_EMAIL here.
# Every test module shares one process, and popping a variable at import time
# strips it from suites that already set it (test_admin and test_metrics both
# depend on theirs). Per-test changes belong in setUp/tearDown with a restore.

from pilot_app import database as database_mod  # noqa: E402
from pilot_app import reports as reports_mod  # noqa: E402
from pilot_app import service as service_mod  # noqa: E402
from pilot_app import snooze  # noqa: E402
from pilot_app import web  # noqa: E402
from pilot_app.security import token_hash  # noqa: E402
from pilot_app.web import db, service  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _route_source(start: str, end: str) -> str:
    """``web.py`` 里从 ``start`` 到 ``end`` 的那一段（结构断言的取景框）。

    「这个接口不发邮件」这种事，跑一遍看不见（没人调它就什么都不发生），
    所以按源码验：那一段里不许出现发送邮件的名字。
    """
    text = (ROOT / "pilot_app" / "web.py").read_text(encoding="utf-8")
    return text[text.index(start):text.index(end)]

REPORT = """## 1. 重要程度与一句话结论
- 等级：高
- 结论：本周五 23:59 前必须提交作业。

## 2. 必须采取的行动与截止时间
- 提交作业到 Canvas（截止：2026-09-18 23:59）
- 预习第六章

## 3. 邮件内容总结
- 老师布置了作业。
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


# ---------------------------------------------------------------------------
# the fingerprint itself
# ---------------------------------------------------------------------------


class TaskKeyTests(unittest.TestCase):
    def test_the_same_task_always_hashes_the_same(self):
        self.assertEqual(reports_mod.task_key("rep_1", "提交作业"),
                         reports_mod.task_key("rep_1", "提交作业"))

    def test_whitespace_differences_do_not_change_the_key(self):
        """The model's line breaks vary between runs; the identity must not."""
        self.assertEqual(reports_mod.task_key("rep_1", "提交作业  到 Canvas"),
                         reports_mod.task_key("rep_1", "提交作业\n到 Canvas"))

    def test_a_rewritten_action_becomes_a_new_task(self):
        """A changed wording is a new question and must resurface.

        Carrying a "handled" mark onto text the user never read would hide an
        item behind an answer to a different one. This is the rule static
        analysis suppressions use: the fingerprint covers the claim.
        """
        self.assertNotEqual(reports_mod.task_key("rep_1", "提交作业"),
                            reports_mod.task_key("rep_1", "提交报告"))

    def test_two_reports_can_share_an_action_without_colliding(self):
        self.assertNotEqual(reports_mod.task_key("rep_1", "提交作业"),
                            reports_mod.task_key("rep_2", "提交作业"))

    def test_the_key_is_url_and_sql_safe(self):
        key = reports_mod.task_key("rep_1", "提交作业 <b>&amp;</b> 100%")
        self.assertRegex(key, r"^[0-9a-f]{32}$")

    def test_tasks_carry_a_key_and_a_day(self):
        tasks = reports_mod.today_tasks(
            [("rep_1", REPORT, "msg_1")],
            [{"id": "msg_1", "subject": "作业", "sender_name": "老师", "sender_address": "t@x.hk",
              "received": "2026-09-14T04:00:00+00:00", "importance": "normal"}],
            timezone="Asia/Hong_Kong",
        )
        self.assertEqual(len(tasks), 2)
        for task in tasks:
            self.assertRegex(task["task_key"], r"^[0-9a-f]{32}$")
        self.assertEqual(tasks[0]["task_day"], "2026-09-14")
        self.assertEqual(len({task["task_key"] for task in tasks}), 2, "同一封邮件的两个动作要有不同的键")


# ---------------------------------------------------------------------------
# hiding and finding again, through the real HTTP surface
# ---------------------------------------------------------------------------


class TaskFlowTests(unittest.TestCase):
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
        self.client = Client(self.base)
        self.seen = {
            name: os.environ.get(name) for name in ("INFE_PILOT_CONTACT_EMAIL", "INFE_PILOT_ADMIN_EMAILS")
        }
        self._register()

    def tearDown(self):
        for name, value in self.seen.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _register(self):
        code = f"tasks-invite-{self.stamp}"
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with db.connect() as connection:
            connection.execute("INSERT INTO invites(code_hash,expires_at) VALUES(?,?)",
                               (token_hash(code), expiry))
        web.reset_signup_rate_limit()  # 见 web.reset_signup_rate_limit：限速按 IP，单测得自己清
        status, user, _ = self.client.post("/api/auth/register", {
            "email": f"tasks-{self.stamp}@example.com", "password": "a-long-enough-password",
            "invite_code": code, "accepted_terms": True,
        })
        self.assertEqual(status, 200, user)
        self.user = user

    # Hong Kong has no DST, so a fixed offset is exact -- and it avoids depending
    # on the host having tzdata, which macOS does not by default.
    HK = dt.timezone(dt.timedelta(hours=8))

    def _local_moment(self, days_ago: int = 0) -> str:
        """A UTC timestamp that lands in the middle of a local day N days back.

        The app buckets reports by the *profile timezone's* date. Seeding from a
        UTC timestamp and then querying with the UTC date string works for most of
        the day and breaks for the eight hours when Hong Kong is already on the
        next date -- which is exactly what happened at 00:00 HKT on 2026-09-14.
        """
        local = dt.datetime.now(self.HK) - dt.timedelta(days=days_ago)
        moment = local.replace(hour=10, minute=0, second=0, microsecond=0)
        return moment.astimezone(dt.timezone.utc).isoformat(timespec="seconds")

    def _local_day(self, days_ago: int = 0) -> str:
        return (dt.datetime.now(self.HK) - dt.timedelta(days=days_ago)).strftime("%Y-%m-%d")

    def _seed_report(self, *, received: str, report: str = REPORT, key: str | None = None,
                     mailbox_suffix: str = ""):
        """One immediate report for one message, and return (message_id, report_id)."""
        mailbox_id = f"mbx_{key or 'x'}{mailbox_suffix}"
        with db.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO mailboxes(id,user_id,email,report_to,imap_host,imap_port,
                   smtp_host,smtp_port,encrypted_password,updated_at)
                   VALUES(?,?,?,?,'h',993,'h',465,?,?)""",
                (mailbox_id, self.user["id"], f"{mailbox_id}@example.com", f"{mailbox_id}@example.com",
                 b"\x00", "2026-09-14T00:00:00+00:00"),
            )
        message_id = db.insert_message(
            self.user["id"], mailbox_id, "1", int(dt.datetime.now().timestamp() * 1000) % 100000,
            {"subject": "作业截止", "sender_name": "老师", "sender_address": "t@cityu.edu.hk",
             "received": received, "importance": "normal",
             "body": service.secrets.encrypt("body", context=f"message:{self.user['id']}")},
        )
        with db.connect() as connection:
            connection.execute("UPDATE messages SET received_at=?,status='sent' WHERE id=?",
                               (received, message_id))
        report_id = db.create_report(
            user_id=self.user["id"], message_id=message_id, kind="immediate", subject="【AI邮件摘要】作业截止",
            body=service.secrets.encrypt(report, context=f"report:{self.user['id']}"), sent_to="pilot@qq.com",
        )
        return message_id, report_id

    @staticmethod
    def _today_iso(hours_ago: int = 0) -> str:
        return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_ago)).isoformat(timespec="seconds")

    def _today_tasks(self):
        status, body, _ = self.client.get("/api/tasks")
        self.assertEqual(status, 200, body)
        return body

    # -- 轻重缓急（用户自己定）-----------------------------------------------

    def test_the_view_ships_both_priorities_and_says_which_to_show(self):
        self._seed_report(received=self._today_iso())
        task = self._today_tasks()["tasks"][0]
        self.assertIn("user_priority", task)
        self.assertEqual(task["user_priority"], "", "默认是空的：没设过就不该假装设过")
        self.assertEqual(task["effective_priority"], task["priority"])
        self.assertIn("export_title", task)

    def test_the_view_ships_the_one_line_conclusion(self):
        """每行任务都带一句「这封信在讲什么」（v0.65.0 起印在清单上）。

        模型那句话在报告里本来就有，`parse_report` 也一直在算——只是从来没进过给浏览器的
        那份数据，所以界面上只有一行 action，用户看不出这件事的来龙去脉。
        """
        self._seed_report(received=self._today_iso())
        for task in self._today_tasks()["tasks"]:
            self.assertEqual(task["conclusion"], "本周五 23:59 前必须提交作业。")

    def test_a_report_with_no_conclusion_ships_an_empty_one(self):
        """提炼不出结论时送**空串**，不送「关于「X」的摘要」那句兜底。

        报告里总得有个标题，所以 `conclusion_of` 有兜底句；但那句话不是模型说的，
        印在清单上就是界面在替模型编话。空串 = 界面据此整行不渲染。
        """
        self._seed_report(received=self._today_iso(),
                          report="## 2. 必须采取的行动与截止时间\n- 提交作业到 Canvas\n")
        task = self._today_tasks()["tasks"][0]
        self.assertEqual(task["conclusion"], "")
        self.assertNotIn("的摘要", task["conclusion"])

    def test_the_view_ships_a_machine_readable_received_time(self):
        """`received` 是 UTC ISO 原值，给「按时间」排序用；给人看的那份是
        `received_display`。两个都送，是因为拿显示字符串排序会排错（`momentText()`
        渲染出来的东西不是能比大小的值）。"""
        received = self._today_iso()
        self._seed_report(received=received)
        task = self._today_tasks()["tasks"][0]
        self.assertEqual(task["received"], received)
        self.assertNotEqual(task["received"], task["received_display"])

    def test_the_clipboard_title_stays_plain_text(self):
        """`export_title` 是**剪贴板**那一行（粘进 iOS 提醒事项），不是日历标题。

        2026-09-19 审这份日历时发现的：`export_title` 曾被换成美化过的标题，于是
        emoji 与 ⏰ 会跟着「复制成清单」粘进别人的提醒事项——而那份设计文档自己
        写着「emoji 不进清单」。日历里的漂亮标题在 `build_ics` 里生成，不经过这里。
        """
        from pilot_app import taskexport

        self._seed_report(received=self._today_iso())
        task = self._today_tasks()["tasks"][0]
        self.assertEqual(task["export_title"], taskexport.line_for(task))
        self.assertNotIn("⏰", task["export_title"])

    def test_setting_a_priority_is_remembered_and_shown(self):
        self._seed_report(received=self._today_iso())
        key = self._today_tasks()["tasks"][0]["task_key"]
        status, body, _ = self.client.put(f"/api/tasks/{key}/priority",
                                          {"priority": "high", "day": self._local_day()})
        self.assertEqual(status, 200, body)
        mine = [task for task in body["tasks"] if task["task_key"] == key][0]
        self.assertEqual(mine["user_priority"], "high")
        self.assertEqual(mine["effective_priority"], "high")
        # 重新取一次也还在（真的落了库，不是只在这次响应里）
        again = [task for task in self._today_tasks()["tasks"] if task["task_key"] == key][0]
        self.assertEqual(again["user_priority"], "high")

    def test_the_users_ranking_decides_the_order(self):
        """把第二件提到「急」，它就要排到第一件前面去。"""
        _, report_id = self._seed_report(received=self._today_iso())
        before = self._today_tasks()["tasks"]
        self.assertGreaterEqual(len(before), 2, "夹具要有两个动作")
        first, last = before[0], before[-1]
        # 同一封信里的动作**继承同一个 priority**（报告级判断），所以要把顺序测出来，
        # 就得制造真正的差别：一个降到「缓」、另一个提到「急」。
        self.client.put(f"/api/tasks/{first['task_key']}/priority",
                        {"priority": "low", "day": self._local_day()})
        self.client.put(f"/api/tasks/{last['task_key']}/priority",
                        {"priority": "high", "day": self._local_day()})
        after = self._today_tasks()["tasks"]
        self.assertEqual(after[0]["task_key"], last["task_key"],
                         "自己定的「急」必须排到最前，而不是只换一个颜色")
        self.assertEqual(after[-1]["task_key"], first["task_key"],
                         "自己定的「缓」要沉到底部")

    def test_clearing_it_goes_back_to_the_mail_s_own_reading(self):
        self._seed_report(received=self._today_iso())
        task = self._today_tasks()["tasks"][0]
        key, derived = task["task_key"], task["priority"]
        self.client.put(f"/api/tasks/{key}/priority", {"priority": "low", "day": self._local_day()})
        _, body, _ = self.client.put(f"/api/tasks/{key}/priority",
                                     {"priority": "", "day": self._local_day()})
        mine = [item for item in body["tasks"] if item["task_key"] == key][0]
        self.assertEqual(mine["user_priority"], "")
        self.assertEqual(mine["effective_priority"], derived)

    def test_an_unknown_priority_is_refused(self):
        self._seed_report(received=self._today_iso())
        key = self._today_tasks()["tasks"][0]["task_key"]
        for value in ("urgent", "HIGH", "1", None):
            with self.subTest(value=value):
                status, _, _ = self.client.put(f"/api/tasks/{key}/priority",
                                               {"priority": value, "day": self._local_day()})
                self.assertEqual(status, 422, f"{value!r} 不该被接受")
        # 少写这个字段 = 请求发坏了，不是「清空」：静默清掉用户自己定的排序，
        # 和保存成功长得一模一样。
        status, _, _ = self.client.put(f"/api/tasks/{key}/priority", {"day": self._local_day()})
        self.assertEqual(status, 422, "缺 priority 字段要 422，不能当成清空")

    def test_an_unknown_task_is_a_404(self):
        status, _, _ = self.client.put(f"/api/tasks/{'f' * 32}/priority", {"priority": "high"})
        self.assertEqual(status, 404)

    def test_ranking_a_task_does_not_un_handle_it(self):
        """两个决定是两件事：重新排序不该把「已处理」翻回来。"""
        self._seed_report(received=self._today_iso())
        key = self._today_tasks()["tasks"][0]["task_key"]
        self.client.put(f"/api/tasks/{key}", {"state": "done", "day": self._local_day()})
        _, body, _ = self.client.put(f"/api/tasks/{key}/priority",
                                     {"priority": "high", "day": self._local_day()})
        self.assertNotIn(key, [item["task_key"] for item in body["tasks"]],
                         "重新排序之后它仍然是「已处理」，不该跳回待处理列表")
        handled = [item for item in body["done"] if item["task_key"] == key][0]
        self.assertEqual(handled["user_priority"], "high")

    # -- 「稍后提醒」 ---------------------------------------------------------
    #
    # 规格是 `docs/snooze-2026-09-23.md`。这一组盯四件事：三个预设算出来的时刻、
    # 越界夹取、**到点自己回来**（读的时候判断，没有定时任务）、以及越权与
    # 日报那一行。任何一条错了，用户看到的是「按钮坏了」或「我的待办不见了」。

    def _snooze(self, key, until, day=""):
        return self.client.put("/api/tasks/snooze",
                               {"task_key": key, "until": until,
                                "day": day or self._local_day()})

    def test_the_three_presets_resolve_to_the_moments_they_promise(self):
        """预设说的是**用户本地**的钟点，所以拿一个固定的钟来问。

        「今晚 21:00 已经过了」这一支是最容易写错的：把它算成「过去的那一刻」，
        这条待办会立刻回来 —— 界面上看起来就是按钮没反应。
        """
        hk = dt.timezone(dt.timedelta(hours=8))
        noon = dt.datetime(2026, 9, 24, 12, 0, tzinfo=hk)

        def when(preset, now):
            return snooze.when_text(snooze.resolve(preset, now=now, timezone="Asia/Hong_Kong"),
                                    now=now, timezone="Asia/Hong_Kong")

        self.assertEqual(when("1h", noon), "13:00")
        self.assertEqual(when("tonight", noon), "21:00")
        self.assertEqual(when("tomorrow", noon), "明天 09:00")
        # 22:30 已经过了今晚 21:00 ⇒ 明天 21:00
        late = dt.datetime(2026, 9, 24, 22, 30, tzinfo=hk)
        self.assertEqual(when("tonight", late), "明天 21:00")
        # 21:00 整点这一刻算「已经过了」：留在今天会让它一秒后自己回来。
        edge = dt.datetime(2026, 9, 24, 21, 0, 0, tzinfo=hk)
        self.assertEqual(when("tonight", edge), "明天 21:00")
        # 认不出的写法要报错，不许猜一个时刻。
        with self.assertRaises(snooze.InvalidSnooze):
            snooze.resolve("later", now=noon, timezone="Asia/Hong_Kong")

    def test_out_of_range_moments_are_clamped_not_refused(self):
        """太短没意义、太长等于删除 —— 服务端夹在 5 分钟 ~ 30 天。"""
        now = dt.datetime.now(dt.timezone.utc)

        def seconds_until(value):
            moment = snooze.parse_iso(snooze.resolve(value, now=now, timezone="Asia/Hong_Kong"))
            return (moment - now).total_seconds()

        self.assertAlmostEqual(seconds_until((now + dt.timedelta(minutes=1)).isoformat()),
                               snooze.MIN_SECONDS, delta=2)
        self.assertAlmostEqual(seconds_until((now + dt.timedelta(days=90)).isoformat()),
                               snooze.MAX_SECONDS, delta=2)
        # 已经过去的时刻同样夹到现在之后：存一个过去的时刻 = 它立刻回来，
        # 而用户看到的是「保存成功」，两件事长得一模一样。
        self.assertGreater(seconds_until("2020-01-01T00:00:00+00:00"), 0)
        self.assertAlmostEqual(seconds_until("1h"), 3600, delta=2)

    def test_snoozing_takes_it_out_of_the_list_and_cancelling_puts_it_back(self):
        self._seed_report(received=self._today_iso())
        key = self._today_tasks()["tasks"][0]["task_key"]

        status, body, _ = self._snooze(key, "1h")
        self.assertEqual(status, 200, body)
        self.assertNotIn(key, [task["task_key"] for task in body["tasks"]],
                         "稍后提醒过的待办不许留在主列表里")
        self.assertIn(key, [task["task_key"] for task in body["snoozed"]])
        row = [task for task in body["snoozed"] if task["task_key"] == key][0]
        self.assertTrue(row["snoozed_until"], "那一栏要给出「什么时候回来」")
        # 重新读一次也还在那一栏（真的落了库，不是只在这次响应里）
        again = self._today_tasks()
        self.assertNotIn(key, [task["task_key"] for task in again["tasks"]])
        self.assertEqual(again["counts"]["total"], body["counts"]["total"],
                         "它只是挪开了，当天的总数不该变")
        # `state` 一个字节都不许动：稍后提醒**不是**第二种完成状态。
        with db.connect() as connection:
            state = connection.execute(
                "SELECT state FROM task_states WHERE user_id=? AND task_key=?",
                (self.user["id"], key)).fetchone()[0]
        self.assertEqual(state, "open")

        # 取消：立刻回主列表
        status, body, _ = self._snooze(key, "")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["snoozed"], [])
        self.assertEqual(body["snoozed_until"], "")
        self.assertIn(key, [task["task_key"] for task in body["tasks"]])

    def test_it_comes_back_by_itself_once_the_moment_has_passed(self):
        """到点是**读列表**时判断的 —— 所以这里没有定时任务可等，改小时间再读一次就对。"""
        self._seed_report(received=self._today_iso())
        key = self._today_tasks()["tasks"][0]["task_key"]
        self._snooze(key, "1h")
        # 接口会把时刻夹到 5 分钟以后，所以直接改库来模拟「时间到了」这件事本身。
        with db.connect() as connection:
            connection.execute(
                "UPDATE task_states SET snoozed_until=? WHERE user_id=? AND task_key=?",
                (self._today_iso(hours_ago=1), self.user["id"], key))
        view = self._today_tasks()
        self.assertIn(key, [task["task_key"] for task in view["tasks"]],
                      "时刻一过它就该自己回到主列表")
        self.assertEqual(view["snoozed"], [])

    def test_another_users_task_key_is_a_404_and_leaks_nothing(self):
        """别人的 key → 404，而且与「根本没有这个 key」**逐字相同**。

        两句不一样的话（「不是你的」vs「不存在」）会让这个接口变成一台探测别人
        清单里有什么的机器 —— 与 `/api/admin/*` 那条边界同一个理由。
        """
        self._seed_report(received=self._today_iso())
        key = self._today_tasks()["tasks"][0]["task_key"]

        other = Client(self.base)
        stamp = dt.datetime.now().timestamp()
        code = f"tasks-snooze-other-{stamp}"
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with db.connect() as connection:
            connection.execute("INSERT INTO invites(code_hash,expires_at) VALUES(?,?)",
                               (token_hash(code), expiry))
        web.reset_signup_rate_limit()
        status, other_user, _ = other.post("/api/auth/register", {
            "email": f"tasks-snooze-other-{stamp}@example.com",
            "password": "a-long-enough-password", "invite_code": code, "accepted_terms": True,
        })
        self.assertEqual(status, 200, other_user)

        mine = other.put("/api/tasks/snooze", {"task_key": key, "until": "1h"})
        absent = other.put("/api/tasks/snooze", {"task_key": "f" * 32, "until": "1h"})
        self.assertEqual(mine[0], 404, mine[1])
        self.assertEqual(absent[0], 404, absent[1])
        self.assertEqual(mine[1], absent[1], "两种 404 必须一模一样，否则就是存在性探针")
        # 我这边一点没变：他的调用不许碰到我的行。
        self.assertIn(key, [task["task_key"] for task in self._today_tasks()["tasks"]])

    def test_a_malformed_key_or_an_unknown_moment_is_refused(self):
        self._seed_report(received=self._today_iso())
        key = self._today_tasks()["tasks"][0]["task_key"]
        for payload in ({"task_key": "not-a-key", "until": "1h"},
                        {"task_key": key[:31], "until": "1h"},
                        {"task_key": key, "until": "later"},
                        {"task_key": key, "until": None},
                        {"task_key": key}):
            with self.subTest(payload=payload):
                status, _, _ = self.client.put("/api/tasks/snooze", payload)
                self.assertEqual(status, 422, payload)
        # 少写 until 不等于「取消」：静默把一条挪开的待办叫回来，和保存成功
        # 长得一模一样 —— 那条待办会自己冒出来，用户不知道为什么。
        status, _, _ = self.client.put("/api/tasks/snooze", {"task_key": key})
        self.assertEqual(status, 422)
        self.assertIn(key, [task["task_key"] for task in self._today_tasks()["tasks"]])

    def test_the_daily_brief_carries_the_line_and_sends_no_extra_mail(self):
        """那一行在**确定性清单**里、不在模型写的综览里，而且不多发一封邮件。"""
        self._seed_report(received=self._today_iso())
        key = self._today_tasks()["tasks"][0]["task_key"]
        self._snooze(key, "1h")
        # 自己造一个**属于本账号**的邮箱：`_seed_report` 那份的 id 是固定的，
        # 而整个类共用一个库，`INSERT OR IGNORE` 会让第二个测试起就没有自己的邮箱。
        with db.connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO mailboxes(id,user_id,email,report_to,imap_host,imap_port,
                   smtp_host,smtp_port,encrypted_password,updated_at)
                   VALUES(?,?,?,?,'h',993,'h',465,?,?)""",
                (f"mbx_snooze_{int(dt.datetime.now().timestamp() * 1000)}", self.user["id"],
                 "pilot@example.com", "pilot@example.com",
                 service.secrets.encrypt("pw", context=f"mailbox:{self.user['id']}"),
                 "2026-09-14T00:00:00+00:00"))

        captured = {}

        def fake_send(mailbox, password, subject, markdown, *, html_body, text_body):
            captured.update(markdown=markdown, html=html_body, text=text_body)

        user = {**self.user, "timezone": "Asia/Hong_Kong", "report_to": "pilot@example.com"}
        with mock.patch.object(service_mod.mailio, "send_report",
                               side_effect=fake_send) as sender:
            self.assertTrue(service.send_daily(user, self._local_day()))

        self.assertEqual(sender.call_count, 1,
                         "稍后提醒**不额外发邮件**：整封简报还是一次发送")
        line = "你让它稍后提醒的 1 件"
        self.assertIn(line, captured["markdown"], "它属于确定性清单")
        self.assertIn(line, captured["text"], "纯文本那一半同样要有")
        self.assertIn(line, captured["html"], "HTML 那一半同样要有")
        # 「不在综览里」：综览是引用块（`>`），清单是 `- ` 开头的那几条。
        quoted = "\n".join(row for row in captured["markdown"].splitlines()
                           if row.startswith(">"))
        self.assertNotIn(line, quoted, "这一行是事实，不许混进模型写的那段话里")

    def test_the_snooze_path_has_no_mail_and_no_timer_in_it(self):
        """规格里两条「不做」按**结构**验，而不是靠跑一遍看看有没有信。

        没有邮件发送调用、没有后台定时任务 —— 到点回来是读列表时判断的，
        所以服务器重启也不会漏。加一个 cron/定时器进去，这条立刻红。
        """
        source = (ROOT / "pilot_app" / "snooze.py").read_text(encoding="utf-8")
        for banned in ("mailio", "send_report", "smtplib", "threading", "Timer",
                       "time.sleep", "schedule"):
            self.assertNotIn(banned, source, f"snooze.py 里不该出现 {banned}")
        body = _route_source("def snooze_task", "def export_tasks_ics")
        for banned in ("mailio", "send_report"):
            self.assertNotIn(banned, body, "「稍后提醒」接口不许发邮件")

    # -- 导出到手机日历 ------------------------------------------------------

    def test_the_export_is_a_calendar_file_with_the_right_content_type(self):
        """iOS 只在这个响应头正确时才把文件交给「日历」——给成 octet-stream 就是那个
        「下载了但打不开」的老问题，所以这条断言盯的是头，不只是内容。"""
        self._seed_report(received=self._today_iso())
        key = self._today_tasks()["tasks"][0]["task_key"]
        status, body, headers = self.client.get(
            f"/api/tasks/export.ics?keys={key}&day={self._local_day()}")
        self.assertEqual(status, 200, body)
        self.assertTrue(headers.get("Content-Type", "").startswith("text/calendar"),
                        headers.get("Content-Type"))
        self.assertIn("attachment", headers.get("Content-Disposition", ""))
        self.assertIn(".ics", headers.get("Content-Disposition", ""))
        self.assertIn("BEGIN:VCALENDAR", body)
        self.assertIn(f"UID:{key}@cityu-mail-pilot", body)

    def test_the_export_only_carries_the_selected_tasks(self):
        self._seed_report(received=self._today_iso())
        tasks = self._today_tasks()["tasks"]
        self.assertGreaterEqual(len(tasks), 2)
        status, body, _ = self.client.get(
            f"/api/tasks/export.ics?keys={tasks[0]['task_key']}&day={self._local_day()}")
        self.assertEqual(status, 200)
        self.assertEqual(body.count("BEGIN:VEVENT"), 1)
        self.assertIn(tasks[0]["task_key"][:8], body)
        self.assertNotIn(tasks[1]["task_key"][:8], body, "没勾的不该出现在文件里")

    def test_exporting_nothing_is_refused_rather_than_returning_an_empty_calendar(self):
        self._seed_report(received=self._today_iso())
        status, body, _ = self.client.get(f"/api/tasks/export.ics?day={self._local_day()}")
        self.assertEqual(status, 422, body)
        status, _, _ = self.client.get(
            f"/api/tasks/export.ics?keys={'f' * 32}&day={self._local_day()}")
        self.assertEqual(status, 422, "别人的/不存在的 id 也导不出东西")

    def test_another_users_task_key_exports_nothing(self):
        """越权面：导出是按「我这一天的清单」过滤的，不是按 id 直接查库。"""
        self._seed_report(received=self._today_iso())
        mine = self._today_tasks()["tasks"][0]["task_key"]
        other = Client(self.base)
        code = f"tasks-other-{self.stamp}"
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with db.connect() as connection:
            connection.execute("INSERT INTO invites(code_hash,expires_at) VALUES(?,?)",
                               (token_hash(code), expiry))
        web.reset_signup_rate_limit()  # 见 web.reset_signup_rate_limit：限速按 IP，单测得自己清
        status, _, _ = other.post("/api/auth/register", {
            "email": f"tasks-other-{self.stamp}@example.com", "password": "a-long-enough-password",
            "invite_code": code, "accepted_terms": True,
        })
        self.assertEqual(status, 200)
        status, body, _ = other.get(
            f"/api/tasks/export.ics?keys={mine}&day={self._local_day()}")
        self.assertEqual(status, 422, body)

    def test_anonymous_cannot_export_or_re_rank(self):
        self._seed_report(received=self._today_iso())
        key = self._today_tasks()["tasks"][0]["task_key"]
        stranger = Client(self.base)
        self.assertIn(stranger.get(f"/api/tasks/export.ics?keys={key}")[0], (401, 404))
        self.assertIn(stranger.put(f"/api/tasks/{key}/priority", {"priority": "high"})[0],
                      (401, 404))

    # -- the happy path ----------------------------------------------------

    def test_a_task_can_be_hidden_and_comes_back_on_request(self):
        self._seed_report(received=self._today_iso())
        before = self._today_tasks()
        self.assertEqual(len(before["tasks"]), 2, before)
        key = before["tasks"][0]["task_key"]
        original = before["tasks"][0]["action"]

        status, body, _ = self.client.put(f"/api/tasks/{key}", {"state": "done"})
        self.assertEqual(status, 200, body)
        self.assertEqual([t["task_key"] for t in body["tasks"]], [t["task_key"] for t in before["tasks"] if t["task_key"] != key])
        self.assertEqual([t["task_key"] for t in body["done"]], [key])
        self.assertEqual(body["counts"], {"total": 2, "open": 1, "done": 1})
        self.assertEqual(body["done"][0]["action"], original, "找回来时要还是原文")

        status, body, _ = self.client.put(f"/api/tasks/{key}", {"state": "open"})
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["tasks"]), 2, "恢复后要回到待处理")
        self.assertEqual(body["done"], [])
        self.assertEqual(body["tasks"][0]["action"], original)

    def test_hiding_deletes_nothing(self):
        """The whole point: a hidden task is a decision, not a deletion."""
        message_id, report_id = self._seed_report(received=self._today_iso())
        key = self._today_tasks()["tasks"][0]["task_key"]
        self.client.put(f"/api/tasks/{key}", {"state": "done"})
        with db.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM messages WHERE id=?", (message_id,)).fetchone()[0], 1)
            row = connection.execute("SELECT body_markdown FROM reports WHERE id=?", (report_id,)).fetchone()
            self.assertIsNotNone(row, "报告不得被删除")
            self.assertEqual(
                service.decrypt_report(row["body_markdown"], self.user["id"]), REPORT,
                "报告正文必须原样保留——任务是从它推导出来的")
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM task_states WHERE user_id=? AND state='done'",
                                   (self.user["id"],)).fetchone()[0], 1)

    def test_the_dashboard_stops_counting_a_hidden_task(self):
        self._seed_report(received=self._today_iso())
        key = self._today_tasks()["tasks"][0]["task_key"]
        status, dash, _ = self.client.get("/api/dashboard")
        self.assertEqual(status, 200)
        self.assertEqual(dash["today"]["tasks"], 2, dash["today"])
        self.client.put(f"/api/tasks/{key}", {"state": "done"})
        _, dash, _ = self.client.get("/api/dashboard")
        self.assertEqual(dash["today"]["tasks"], 1, "「需要行动」必须只算还没处理的")
        self.assertEqual(dash["today"]["tasks_done"], 1)
        # The nudge must not still headline a task the user just ticked off.
        self.assertNotIn(key, [t["task_key"] for t in dash["tasks"]])

    def test_reopening_keeps_the_day_summary_honest(self):
        """The archive is a record, so changing your mind must update it, not
        erase the row and pretend the day never had a decision."""
        self._seed_report(received=self._today_iso())
        key = self._today_tasks()["tasks"][0]["task_key"]
        self.client.put(f"/api/tasks/{key}", {"state": "done"})
        _, body, _ = self.client.put(f"/api/tasks/{key}", {"state": "open"})
        today = body["day"]
        row = [d for d in body["days"] if d["day"] == today]
        self.assertEqual(len(row), 1, body["days"])
        self.assertEqual(row[0]["total"], 1)
        self.assertEqual(row[0]["done"], 0, "恢复之后当天就不该再算作已处理")

    # -- daily grouping ----------------------------------------------------

    def test_an_earlier_day_can_be_viewed_again(self):
        self._seed_report(received=self._local_moment(days_ago=1))
        today = self._today_tasks()
        self.assertEqual(today["tasks"], [], "昨天的事不该出现在今天")
        self.assertTrue(today["is_today"])

        day = self._local_day(days_ago=1)
        status, body, _ = self.client.get(f"/api/tasks/day/{day}")
        self.assertEqual(status, 200, body)
        self.assertFalse(body["is_today"])
        self.assertEqual(len(body["tasks"]), 2, body)

    def test_an_earlier_day_can_still_be_acted_on(self):
        """The history view is not read-only: what was hidden can be restored."""
        yesterday = self._local_moment(days_ago=1)
        self._seed_report(received=yesterday)
        day = self._local_day(days_ago=1)
        _, view, _ = self.client.get(f"/api/tasks/day/{day}")
        key = view["tasks"][0]["task_key"]
        status, body, _ = self.client.put(f"/api/tasks/{key}", {"state": "done", "day": day})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["day"], day, "必须回的是用户正在看的那一天")
        self.assertEqual(body["counts"]["done"], 1)

    def test_the_day_list_only_shows_days_with_decisions(self):
        self._seed_report(received=self._today_iso())
        key = self._today_tasks()["tasks"][0]["task_key"]
        self.client.put(f"/api/tasks/{key}", {"state": "done"})
        body = self._today_tasks()
        self.assertEqual([d["day"] for d in body["days"]], [body["day"]])

    def test_a_bad_day_falls_back_to_today_instead_of_erroring(self):
        """A stale bookmark or a typo must not turn a read view into a 500."""
        self._seed_report(received=self._today_iso())
        # Anything shaped like a date reaches the handler; an impossible one is
        # corrected to today rather than rejected, so a mistyped URL still shows
        # something useful.
        for day in ("2026-13-45", "2026-02-30", "0000-00-00"):
            status, body, _ = self.client.get(f"/api/tasks/day/{day}")
            self.assertEqual(status, 200, (day, body))
            self.assertTrue(body["is_today"], day)
        # A shape that is not a date at all never matches the route.
        for junk in ("yesterday", "2026-9-4", "../etc/passwd"):
            status, _, _ = self.client.get(f"/api/tasks/day/{junk}")
            self.assertEqual(status, 404, junk)

    # -- a purged source mail ---------------------------------------------

    def test_a_handled_task_survives_its_mail_being_purged(self):
        """Reconnecting a mailbox deletes messages and with them the join the
        live list needs. The decision must still be visible, or the history
        would quietly lose exactly what it exists to prove."""
        message_id, _ = self._seed_report(received=self._today_iso())
        before = self._today_tasks()
        key = before["tasks"][0]["task_key"]
        original = before["tasks"][0]["action"]
        self.client.put(f"/api/tasks/{key}", {"state": "done"})
        with db.connect() as connection:
            connection.execute("DELETE FROM reports WHERE message_id=?", (message_id,))
            connection.execute("DELETE FROM messages WHERE id=?", (message_id,))
        body = self._today_tasks()
        self.assertEqual([t["task_key"] for t in body["done"]], [key], "记录要按快照留住")
        self.assertTrue(body["done"][0]["archived"])
        self.assertEqual(body["done"][0]["action"], original, "快照要保留原文")
        # …and it can still be reopened. Only the task the user actually made a
        # decision about is preserved; the report's other action was never
        # recorded anywhere and goes with the mail. That is the honest outcome:
        # we promise to keep decisions, not to keep mail we were told to drop.
        status, reopened, _ = self.client.put(f"/api/tasks/{key}", {"state": "open"})
        self.assertEqual(status, 200, reopened)
        self.assertEqual([t["task_key"] for t in reopened["tasks"]], [key])
        self.assertEqual(reopened["counts"]["open"], 1)

    # -- boundaries --------------------------------------------------------

    def test_an_unknown_key_is_refused_not_silently_stored(self):
        body = self._today_tasks()
        status, response, _ = self.client.put("/api/tasks/" + "0" * 32, {"state": "done"})
        self.assertEqual(status, 404, response)
        self.assertEqual(self._today_tasks()["counts"]["total"], body["counts"]["total"])

    def test_a_malformed_key_never_reaches_the_database(self):
        status, _, _ = self.client.put("/api/tasks/not-a-key", {"state": "done"})
        self.assertEqual(status, 404)

    def test_an_unknown_state_is_refused(self):
        self._seed_report(received=self._today_iso())
        key = self._today_tasks()["tasks"][0]["task_key"]
        status, body, _ = self.client.put(f"/api/tasks/{key}", {"state": "deleted"})
        self.assertEqual(status, 422, body)
        self.assertEqual(self._today_tasks()["counts"]["done"], 0)

    def test_the_router_does_not_accept_a_key_of_the_wrong_shape(self):
        """Only the 32-hex key form is routable, so junk cannot even be tried."""
        for bad in ("../../etc/passwd", "ABC", "0" * 31, "0" * 33, "g" * 32):
            status, _, _ = self.client.put(f"/api/tasks/{bad}", {"state": "done"})
            self.assertIn(status, (404, 405), f"{bad} 不该匹配路由")

    def test_tasks_require_a_session(self):
        anonymous = Client(self.base)
        for path in ("/api/tasks", "/api/tasks/day/2026-09-14"):
            status, _, _ = anonymous.get(path)
            self.assertEqual(status, 401, path)

    def test_one_users_decision_does_not_touch_anothers(self):
        """Two accounts whose reports happen to read the same must stay separate."""
        self._seed_report(received=self._today_iso())
        key = self._today_tasks()["tasks"][0]["task_key"]

        other = Client(self.base)
        stamp = dt.datetime.now().timestamp()
        code = f"tasks-other-{stamp}"
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with db.connect() as connection:
            connection.execute("INSERT INTO invites(code_hash,expires_at) VALUES(?,?)",
                               (token_hash(code), expiry))
        web.reset_signup_rate_limit()  # 见 web.reset_signup_rate_limit：限速按 IP，单测得自己清
        status, other_user, _ = other.post("/api/auth/register", {
            "email": f"tasks-other-{stamp}@example.com", "password": "a-long-enough-password",
            "invite_code": code, "accepted_terms": True,
        })
        self.assertEqual(status, 200, other_user)
        status, theirs, _ = other.get("/api/tasks")
        self.assertEqual(status, 200, theirs)
        self.assertEqual(theirs["tasks"], [], "新账户不该看到别人的任务")

        self.client.put(f"/api/tasks/{key}", {"state": "done"})
        _, theirs_again, _ = other.get("/api/tasks")
        self.assertEqual(theirs_again["tasks"], [])

    def test_export_includes_the_task_decisions(self):
        self._seed_report(received=self._today_iso())
        key = self._today_tasks()["tasks"][0]["task_key"]
        self.client.put(f"/api/tasks/{key}", {"state": "done"})
        status, body, _ = self.client.get("/api/account/export")
        self.assertEqual(status, 200, body)
        self.assertEqual([row["task_key"] for row in body["task_states"]], [key])
        self.assertEqual(body["task_states"][0]["state"], "done")

    def test_deleting_the_account_removes_the_decisions(self):
        self._seed_report(received=self._today_iso())
        key = self._today_tasks()["tasks"][0]["task_key"]
        self.client.put(f"/api/tasks/{key}", {"state": "done"})
        status, body, _ = self.client.put("/api/account/status/deleted")
        self.assertEqual(status, 200, body)
        with db.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM task_states WHERE user_id=?",
                                   (self.user["id"],)).fetchone()[0], 0,
                "删号必须连同任务决定一起清掉")


# ---------------------------------------------------------------------------
# storage-level guarantees
# ---------------------------------------------------------------------------


class TaskStorageTests(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".sqlite3")
        self.db = database_mod.Database(self.path)
        self.db.initialize()

    def _user(self, user_id: str = "u1") -> None:
        with self.db.connect() as connection:
            connection.execute(
                """INSERT INTO users(id,email,password_hash,status,created_at)
                   VALUES(?,?,?, 'active','2026-09-14T00:00:00+00:00')""",
                (user_id, f"{user_id}@example.com", "h"))

    def test_state_is_upserted_rather_than_duplicated(self):
        self._user()
        for state in ("done", "open", "done"):
            self.db.set_task_state("u1", "k" * 32, state, {"task_day": "2026-09-14", "action": "a"})
        with self.db.connect() as connection:
            rows = connection.execute("SELECT state FROM task_states WHERE user_id='u1'").fetchall()
        self.assertEqual(len(rows), 1, "同一任务只该有一行")
        self.assertEqual(rows[0]["state"], "done")

    def test_an_empty_snapshot_does_not_erase_a_stored_one(self):
        """Reopening from the archive sends no text; the text must survive."""
        self._user()
        self.db.set_task_state("u1", "k" * 32, "done",
                               {"task_day": "2026-09-14", "action": "原文", "subject": "作业"})
        self.db.set_task_state("u1", "k" * 32, "open", {})
        row = self.db.task_states("u1")["k" * 32]
        self.assertEqual(row["action"], "原文")
        self.assertEqual(row["subject"], "作业")
        self.assertEqual(row["task_day"], "2026-09-14")
        self.assertIsNone(row["done_at"])

    def test_an_invalid_state_is_rejected_by_the_store_too(self):
        self._user()
        with self.assertRaises(ValueError):
            self.db.set_task_state("u1", "k" * 32, "archived")

    def test_a_very_long_action_is_truncated_not_rejected(self):
        self._user()
        self.db.set_task_state("u1", "k" * 32, "done", {"task_day": "2026-09-14", "action": "x" * 50_000})
        self.assertEqual(len(self.db.task_states("u1")["k" * 32]["action"]), 2000)

    def test_decisions_are_scoped_to_their_owner(self):
        self._user("u1")
        self._user("u2")
        self.db.set_task_state("u1", "k" * 32, "done", {"task_day": "2026-09-14"})
        self.assertEqual(self.db.task_states("u2"), {})
        self.assertEqual(self.db.task_states("u2", day="2026-09-14"), {})

    def test_the_schema_is_created_for_an_existing_database(self):
        """Upgrading must add the table without touching existing rows."""
        reopened = database_mod.Database(self.path)
        reopened.initialize()
        self._user("u3")
        reopened.set_task_state("u3", "k" * 32, "done", {"task_day": "2026-09-14"})
        self.assertEqual(len(reopened.task_states("u3")), 1)


if __name__ == "__main__":
    unittest.main()
