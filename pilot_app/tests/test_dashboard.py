"""Tests for the "clear action" dashboard status API."""

import datetime as dt
import os
import unittest
from unittest import mock

_TMP = os.environ.get("INFE_PILOT_DB", "/tmp/pilot-dashboard.sqlite3")
os.environ["INFE_PILOT_DB"] = _TMP
os.environ.setdefault("INFE_PILOT_MASTER_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"

from pilot_app import reports as reports_mod  # noqa: E402
from pilot_app import web  # noqa: E402
from pilot_app.security import SecretBox, hash_password, token_hash  # noqa: E402


REPORT = """## 1. 重要程度与一句话结论
- 等级：高
- 结论：本周五 23:59 前必须提交作业。

## 2. 必须采取的行动与截止时间
- 周五 23:59 前提交（截止：2026-09-18 23:59）

## 3. 邮件内容总结
- 老师更新了提交时间。

## 5. 联网搜索后的建议与来源
来源：Canvas 指南 https://community.canvaslms.com/x
"""


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.db = web.db
        self.db.initialize()
        with self.db.connect() as connection:
            connection.execute("DELETE FROM feedback")
            connection.execute("DELETE FROM reports")
            connection.execute("DELETE FROM messages")
            connection.execute("DELETE FROM mailboxes")
            connection.execute("DELETE FROM connections")
            connection.execute("DELETE FROM sessions")
            connection.execute("DELETE FROM profiles")
            connection.execute("DELETE FROM users")
        self.box = SecretBox.from_environment()
        stamp = dt.datetime.now().timestamp()
        invite = f"dash-invite-{stamp}"
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat(timespec="seconds")
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO invites(code_hash,expires_at) VALUES(?,?)", (token_hash(invite), expiry))
        self.user = self.db.create_user(
            f"dash-{stamp}@example.com",
            hash_password("a-long-enough-password"), token_hash(invite),
        )

    def _user_row(self):
        return self.db.get_user(self.user["id"])

    def _complete_profile(self, school_email="student@my.cityu.edu.hk"):
        self.db.upsert_profile(self.user["id"], {
            "school_email": school_email, "major": "通信工程", "year_of_study": "大二",
            "courses": ["密码学"], "interests": [], "career_goals": [], "focus_topics": [],
            "less_interested": [], "custom_instructions": "", "language": "bilingual",
            "timezone": "Asia/Hong_Kong", "immediate_enabled": True, "daily_enabled": True,
            "daily_time": "22:00",
        })

    def _add_mailbox(self, **overrides):
        values = {
            "email": "pilot@qq.com", "report_to": "pilot@qq.com", "imap_host": "imap.qq.com", "imap_port": 993,
            "smtp_host": "smtp.qq.com", "smtp_port": 465,
            "encrypted_password": self.box.encrypt("pw", context=f"mailbox:{self.user['id']}"),
        }
        values.update(overrides)
        return self.db.upsert_mailbox(self.user["id"], values)

    def _add_model(self, provider="deepseek", last_error=""):
        return self.db.upsert_connection(self.user["id"], {
            "kind": "model", "provider": provider, "model": "m", "base_url": "",
            "encrypted_api_key": self.box.encrypt("key", context=f"connection:{self.user['id']}:model"),
            "config_json": "{}", "enabled": True, "last_error": last_error,
        })

    def test_dashboard_start_asks_for_profile_first(self):
        body = web.build_dashboard(self._user_row())
        self.assertEqual(body["next_step"]["kind"], "profile")
        self.assertEqual(body["channels"]["mailbox"]["state"], "missing")
        self.assertEqual(body["today"]["messages"], 0)

    def test_dashboard_verification_state_reflects_recorded_result(self):
        self._complete_profile()
        mailbox_id = self._add_mailbox()
        body = web.build_dashboard(self._user_row())
        self.assertEqual(body["next_step"]["kind"], "verify")
        self.assertEqual(body["channels"]["mailbox"]["state"], "unknown")

        self.db.record_mailbox_verification(mailbox_id)
        body = web.build_dashboard(self._user_row())
        self.assertEqual(body["channels"]["mailbox"]["state"], "ok")
        self.assertIn("检查成功", body["channels"]["mailbox"]["detail"])

        self.db.record_mailbox_verification(mailbox_id, error="授权码不正确")
        body = web.build_dashboard(self._user_row())
        self.assertEqual(body["channels"]["mailbox"]["state"], "error")
        self.assertIn("授权码不正确", body["channels"]["mailbox"]["detail"])

    # -- "nothing has arrived yet" ------------------------------------------

    def _ready(self, hours_ago: float = 5.0) -> None:
        """Profile + verified mailbox + model, configured `hours_ago` hours ago.

        Two clocks, backdated differently on purpose. 「转发生效没有」 counts from
        when the mailbox was **configured** (`mailboxes.updated_at`) — that is
        when the school's rule started having somewhere to deliver to. The
        mailbox check itself is fresh, because a stale check takes over the
        dashboard with 「确认邮箱可以收信」 and would hide the question this file
        is about (see `test_no_warning_before_the_forwarding_clock_runs_out`).
        """
        self._complete_profile()
        mailbox_id = self._add_mailbox()
        self.db.record_mailbox_verification(mailbox_id)
        self._add_model()
        with self.db.connect() as connection:
            stamp = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_ago)).isoformat(timespec="seconds")
            connection.execute("UPDATE users SET created_at=? WHERE id=?", (stamp, self.user["id"]))
            connection.execute("UPDATE mailboxes SET updated_at=? WHERE user_id=?", (stamp, self.user["id"]))

    def _add_message(self, status: str, skip_reason: str = "") -> None:
        mailbox = self.db.get_mailbox(self.user["id"])
        self.db.insert_message(self.user["id"], mailbox["id"], "1", 500, {
            "subject": "s", "sender_name": "t", "sender_address": "teacher@cityu.edu.hk",
            "received": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "body": b"body", "skip_reason": skip_reason,
        })
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE messages SET status=?, skip_reason=? WHERE user_id=?",
                (status, skip_reason, self.user["id"]))

    def test_setup_done_but_nothing_received_asks_to_check_forwarding(self):
        """The one step we cannot verify from our side is the school's forwarding
        rule, so a silent inbox must not be reported as "everything is ready"."""
        self._ready(hours_ago=30)
        body = web.build_dashboard(self._user_row())
        self.assertEqual(body["next_step"]["kind"], "mailbox")
        self.assertEqual(body["next_step"].get("tone"), "warn")
        self.assertIn("CityU 邮件", body["next_step"]["title"])
        # 这句话和设置向导第 2 步那一格是同一句（一个定义，一处门槛）：两处各写
        # 一份的话，同一个事实会有两种说法，而用户没法判断哪个是真的。
        self.assertEqual(body["next_step"]["detail"],
                         self.db.setup_progress(self.user["id"])["forwarding"]["detail"])

    def test_no_warning_before_the_forwarding_clock_runs_out(self):
        """接通才两小时就报「转发没生效」是错的：安静的几小时不是故障。"""
        self._ready(hours_ago=0.2)
        body = web.build_dashboard(self._user_row())
        self.assertEqual(body["next_step"]["kind"], "done")
        self.assertEqual(body["setup"]["forwarding"]["state"], "todo")

    def test_a_single_cityu_mail_clears_the_warning(self):
        self._ready()
        self._add_message("sent")
        body = web.build_dashboard(self._user_row())
        self.assertNotEqual(body["next_step"]["kind"], "mailbox")
        self.assertNotEqual(body["next_step"].get("tone"), "warn")

    def test_skipped_mail_does_not_count_as_a_working_forward(self):
        """Someone else's newsletter landing in the same inbox proves nothing."""
        self._ready(hours_ago=30)
        self._add_message("skipped", "发件人不在允许名单内")
        self.assertEqual(self.db.count_analysed_messages(self.user["id"]), 0)
        body = web.build_dashboard(self._user_row())
        self.assertEqual(body["next_step"].get("tone"), "warn")

    def test_switching_report_mail_off_does_not_hide_a_broken_forward(self):
        """关掉报告邮件**不等于**我们不再读他的邮箱（v0.63.85 拆开了这两件事）。

        改之前这条测试写的是「即时摘要关了就不轮询，所以空邮箱正常」——那正是那个 bug
        的化石：它把"不发邮件"和"不收信"当成一件事，于是关掉开关的人连"你的转发从来没
        生效过"这句提醒都看不到。现在信照收，所以这句话照说。
        """
        self._ready(hours_ago=30)
        self.db.upsert_profile(self.user["id"], {"immediate_enabled": False})
        body = web.build_dashboard(self._user_row())
        self.assertEqual(body["next_step"].get("tone"), "warn")

    def test_the_dashboard_says_out_loud_that_report_mail_is_off(self):
        """关掉之后，"邮箱里什么都没有"和"坏了"长得一样——所以首页必须自己说出来。"""
        self._ready(hours_ago=30)
        self.db.upsert_profile(self.user["id"], {"immediate_enabled": False, "daily_enabled": False})
        body = web.build_dashboard(self._user_row())
        self.assertEqual(body["channels"]["report_mail"]["state"], "optional")
        self.assertIn("App 里", body["channels"]["report_mail"]["detail"])
        self.assertFalse(body["today"]["immediate_enabled"])
        # 只关一半时也要说清是哪一半。
        self.db.upsert_profile(self.user["id"], {"immediate_enabled": False, "daily_enabled": True})
        body = web.build_dashboard(self._user_row())
        self.assertEqual(body["channels"]["report_mail"]["state"], "ok")
        self.assertIn("只发每日简报", body["channels"]["report_mail"]["detail"])

    def test_verification_does_not_touch_the_uid_cursor(self):
        self._complete_profile()
        mailbox_id = self._add_mailbox()
        self.db.update_mailbox_poll(mailbox_id, last_uid=500, uid_validity="123")
        self.db.record_mailbox_verification(mailbox_id)
        with self.db.connect() as connection:
            row = connection.execute("SELECT last_uid,uid_validity FROM mailboxes WHERE id=?", (mailbox_id,)).fetchone()
        self.assertEqual(row["last_uid"], 500)
        self.assertEqual(row["uid_validity"], "123")

    def test_stale_verification_asks_to_recheck(self):
        self._complete_profile()
        mailbox_id = self._add_mailbox()
        old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3)).isoformat(timespec="seconds")
        with self.db.connect() as connection:
            connection.execute("UPDATE mailboxes SET last_verified_at=? WHERE id=?", (old, mailbox_id))
        body = web.build_dashboard(self._user_row())
        self.assertEqual(body["channels"]["mailbox"]["state"], "stale")
        self.assertEqual(body["next_step"]["kind"], "verify")

    def test_today_tasks_are_extracted_with_deadlines(self):
        self._complete_profile()
        mailbox_id = self._add_mailbox()
        self.db.record_mailbox_verification(mailbox_id)
        self._add_model()
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        message_id = self.db.insert_message(
            self.user["id"], mailbox_id, "1", 1,
            {"subject": "作业截止", "sender_name": "老师", "sender_address": "t@x.hk",
             "received": now, "importance": "normal",
             "body": self.box.encrypt("body", context=f"message:{self.user['id']}")},
        )
        with self.db.connect() as connection:
            connection.execute("UPDATE messages SET received_at=?,status='sent' WHERE id=?", (now, message_id))
        self.db.create_report(
            user_id=self.user["id"], message_id=message_id, kind="immediate", subject="【AI邮件摘要】作业截止",
            body=self.box.encrypt(REPORT, context=f"report:{self.user['id']}"), sent_to="pilot@qq.com",
        )
        body = web.build_dashboard(self._user_row())
        self.assertEqual(body["next_step"]["kind"], "task")
        self.assertGreaterEqual(len(body["tasks"]), 1)
        self.assertEqual(body["tasks"][0]["deadline"], "2026/9/18 23:59")
        self.assertEqual(body["today"]["tasks"], len(body["tasks"]))
        self.assertIn("作业截止", body["recent"][0]["subject"])
        self.assertEqual(body["channels"]["model"]["state"], "ok")

    def test_native_search_marks_step_four_skippable(self):
        self._complete_profile()
        self._add_mailbox()
        self._add_model(provider="openai")
        body = web.build_dashboard(self._user_row())
        self.assertTrue(body["channels"]["search"]["native"])
        self.assertEqual(body["channels"]["search"]["state"], "ok")

    def test_search_error_is_surfaced_not_hidden(self):
        self._complete_profile()
        self._add_mailbox()
        self._add_model(provider="deepseek")
        self.db.upsert_connection(self.user["id"], {
            "kind": "search", "provider": "tavily", "model": "", "base_url": "",
            "encrypted_api_key": self.box.encrypt("k", context=f"connection:{self.user['id']}:search"),
            "config_json": "{}", "enabled": True,
        })
        self.db.record_connection_result(self.user["id"], "search", error="quota exhausted")
        body = web.build_dashboard(self._user_row())
        self.assertEqual(body["channels"]["search"]["state"], "error")
        self.assertIn("quota exhausted", body["channels"]["search"]["detail"])

    def test_model_error_is_surfaced_not_hidden(self):
        self._complete_profile()
        self._add_mailbox()
        self._add_model(provider="deepseek")
        self.db.record_connection_result(self.user["id"], "model", error="401 unauthorized")
        body = web.build_dashboard(self._user_row())
        self.assertEqual(body["channels"]["model"]["state"], "error")
        self.assertIn("401 unauthorized", body["channels"]["model"]["detail"])

    def test_setup_checklist_starts_with_everything_missing(self):
        """The four things a new user must do, and the honest starting state.

        Four of the seven production accounts stalled here and the setup page had
        no way to say what was missing -- so the checklist is the fix, and its
        *empty* state is the one that matters most.
        """
        setup = self.db.setup_progress(self.user["id"])
        self.assertEqual([key for key in setup],
                         ["emails", "mailbox", "forwarding", "report"])
        self.assertFalse(setup["emails"]["ok"])
        self.assertIn("还没填写", setup["emails"]["detail"])
        self.assertFalse(setup["forwarding"]["ok"])
        # `verification_lights` states both reds differently on purpose:
        # "never tried" and "tried and failed" need different next actions.
        self.assertEqual(setup["mailbox"]["state"], "untested")
        self.assertEqual(setup["report"]["state"], "untested")

    def test_setup_checklist_names_the_one_missing_field(self):
        """Saying "还差私人转发邮箱" is the whole point; a generic "未完成" is not."""
        self.db.upsert_mailbox(self.user["id"], {
            "email": "me@qq.com", "report_to": "me@qq.com",
            "imap_host": "imap.qq.com", "imap_port": 993,
            "smtp_host": "smtp.qq.com", "smtp_port": 465,
            "encrypted_password": b"x"})
        setup = self.db.setup_progress(self.user["id"])
        self.assertFalse(setup["emails"]["ok"])
        self.assertIn("CityU 学校邮箱", setup["emails"]["detail"])

    def test_a_wrong_password_shows_the_error_not_a_tick(self):
        """Asked for by the real case: an account that pasted the QQ login
        password got a red light, and the message has to say what it was."""
        mailbox_id = self.db.upsert_mailbox(self.user["id"], {
            "email": "me@qq.com", "report_to": "me@qq.com",
            "imap_host": "imap.qq.com", "imap_port": 993,
            "smtp_host": "smtp.qq.com", "smtp_port": 465,
            "encrypted_password": b"x"})
        self.db.update_mailbox_poll(mailbox_id, last_uid=1, uid_validity="1",
                                    error="IMAP 连接失败：b'LOGIN Login error or password error'")
        setup = self.db.setup_progress(self.user["id"])
        self.assertFalse(setup["mailbox"]["ok"])
        self.assertEqual(setup["mailbox"]["state"], "failed")
        self.assertIn("LOGIN", setup["mailbox"]["detail"])

    def test_a_successful_poll_is_proof_even_without_pressing_the_button(self):
        """The worker polls every minute; a user should not have to press
        「只读连接测试」 to be told their mailbox works."""
        mailbox_id = self.db.upsert_mailbox(self.user["id"], {
            "email": "me@qq.com", "report_to": "me@qq.com",
            "imap_host": "imap.qq.com", "imap_port": 993,
            "smtp_host": "smtp.qq.com", "smtp_port": 465,
            "encrypted_password": b"x"})
        self.db.update_mailbox_poll(mailbox_id, last_uid=5, uid_validity="1", error="")
        setup = self.db.setup_progress(self.user["id"])
        self.assertTrue(setup["mailbox"]["ok"], setup["mailbox"])

    def test_forwarding_is_proven_only_by_mail_actually_arriving(self):
        """The one step we cannot perform and cannot test from our side. A
        configured mailbox is not evidence that the school rule exists."""
        mailbox_id = self.db.upsert_mailbox(self.user["id"], {
            "email": "me@qq.com", "report_to": "me@qq.com",
            "imap_host": "imap.qq.com", "imap_port": 993,
            "smtp_host": "smtp.qq.com", "smtp_port": 465,
            "encrypted_password": b"x"})
        self.db.update_mailbox_poll(mailbox_id, last_uid=1, uid_validity="1", error="")
        self.assertFalse(self.db.setup_progress(self.user["id"])["forwarding"]["ok"])
        # A skipped message is somebody else's newsletter: it proves the mailbox
        # is reachable, not that CityU is forwarding.
        self.db.insert_message(self.user["id"], mailbox_id, "1", 2,
                               {"subject": "newsletter", "sender_address": "a@b.example.com"})
        skipped = self.db.due_messages()[0]["id"]
        self.db.mark_message_skipped(skipped, "非本校发件域")
        self.assertFalse(self.db.setup_progress(self.user["id"])["forwarding"]["ok"])

    def test_the_forwarding_step_says_since_when_and_what_counts(self):
        """这一格最容易被读成「转发没生效」，所以它必须写清楚两件读者能自己核对
        的事：从什么时候起算、算的是哪一类邮件。含糊的措辞在这件事上没有第二种
        解释方式——用户只会得出「你们的程序坏了」。"""
        self._ready(hours_ago=30)
        step = self.db.setup_progress(self.user["id"])["forwarding"]
        self.assertEqual(step["state"], "warn")
        self.assertFalse(step["ok"])
        self.assertRegex(step["detail"], r"接通已经 1\.[23] 天")
        self.assertIn("发件人是 CityU", step["detail"])
        # 到了一封就算数，这一格从此不再是警告。
        self._add_message("sent")
        cleared = self.db.setup_progress(self.user["id"])["forwarding"]
        self.assertTrue(cleared["ok"])
        self.assertIn("已经处理过 1 封", cleared["detail"])

    def test_the_forwarding_step_is_patient_for_the_first_day(self):
        self._ready(hours_ago=3)
        step = self.db.setup_progress(self.user["id"])["forwarding"]
        self.assertEqual(step["state"], "todo")
        self.assertIn("如果第 2 步还没做", step["detail"])

    def test_the_checklist_travels_in_the_dashboard(self):
        payload = web.build_dashboard(self._user_row())
        self.assertIn("setup", payload)
        self.assertEqual(set(payload["setup"]),
                         {"emails", "mailbox", "forwarding", "report"})

    def test_verify_endpoint_requires_a_saved_mailbox(self):
        import http.cookiejar
        import json
        import threading
        import urllib.error
        import urllib.request
        from pilot_app.security import new_token, token_hash

        session = new_token()
        expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat(timespec="seconds")
        self.db.create_session(self.user["id"], token_hash(session), expires)

        server = web.create_server("127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:%d" % server.server_address[1]
            jar = http.cookiejar.CookieJar()
            jar.set_cookie(http.cookiejar.Cookie(
                version=0, name="cityu_mail_session", value=session, port=None, port_specified=False,
                domain="127.0.0.1", domain_specified=True, domain_initial_dot=False, path="/",
                path_specified=True, secure=False, expires=None, discard=False, comment=None,
                comment_url=None, rest={},
            ))
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

            def call(method, path, payload=None):
                data = None if payload is None else json.dumps(payload).encode()
                request = urllib.request.Request(base + path, data=data, method=method)
                request.add_header("Content-Type", "application/json")
                try:
                    with opener.open(request, timeout=20) as response:
                        return response.status, json.loads(response.read().decode())
                except urllib.error.HTTPError as error:
                    return error.code, json.loads(error.read().decode())

            status, body = call("GET", "/api/dashboard")
            self.assertEqual(status, 200, body)
            self.assertIn("next_step", body)
            self.assertEqual(body["next_step"]["kind"], "profile")
            status, body = call("POST", "/api/mailbox/verify")
            self.assertEqual(status, 422)
            self.assertIn("私人转发邮箱", body["detail"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()


class DashboardLivenessTests(unittest.TestCase):
    """首页顶部要**跟着变**，而不是等下一次重开软件。

    用户原话：「软件首页点已经完成后最上面的待办要重新进软件才会刷新，我要变成实时的」。
    顶部那张卡（「你的下一步」）**是服务端算出来的** —— 今天还剩几件事、下一件是什么，
    规则在 `build_dashboard` 里只有一份（六种情形：资料 / 邮箱 / 连接 / 模型 / 转发没生效 /
    有待办）。点掉一条待办之后清单能就地重画，这张卡不会，于是它继续写着「今天有 3 件事
    要处理」，要重进软件才变。

    这里钉的是**接线**：点完要补一次只针对顶部的对齐、切回前台也要对齐，而且那份内容
    **只能来自服务端** —— 在浏览器里自己推一遍就等于把「下一步是什么」写成第二份规则，
    两份迟早会各说各话。真实行为由浏览器套件按真实坐标验（`tools/tasks_check.js`：点掉
    一条之后顶部从「9 件」变成「8 件」，以及「在界面背后处理掉一条再切回前台」）。
    """

    @staticmethod
    def _app() -> str:
        import pathlib
        return (pathlib.Path(web.__file__).resolve().parent / "static" / "app.js").read_text(
            encoding="utf-8")

    def test_ticking_a_task_resyncs_the_top_card(self):
        app = self._app()
        mark = app[app.index("async function markTask("):]
        mark = mark[:mark.index("\n}\n")]
        # **整行**比对，不看子串：第一版写成 `assertIn("syncDashboardTop()", mark)`，
        # 于是把调用注释掉之后它照样绿（注释里也有这串字符）——反向验证当场抓到。
        lines = [line.strip() for line in mark.splitlines()
                 if line.strip() and not line.strip().startswith("//")]
        self.assertIn("syncDashboardTop();", lines,
                      "点完待办之后顶部那张卡没人管了（用户看到的正是这一条）")

    def test_the_sync_asks_the_server_rather_than_guessing(self):
        """「下一步是什么」只有一个定义，在服务端。"""
        app = self._app()
        body = app[app.index("async function syncDashboardTop("):]
        body = body[:body.index("\n}\n")]
        self.assertIn("api('/api/dashboard')", body)
        self.assertIn("renderHero()", body)
        self.assertIn("renderTaskSummary()", body, "四个数字也要跟着")
        # 不许在浏览器里自己拼 next_step：那是第二份规则。
        self.assertNotIn("next_step =", body)
        self.assertNotIn("today.tasks", body.replace("renderTaskSummary()", ""))

    def test_two_quick_ticks_cannot_show_the_older_answer(self):
        """连着点两条会有两次请求在飞，先发的可能后到 —— 晚到的旧结果要丢掉。"""
        app = self._app()
        self.assertIn("let topSyncToken = 0;", app)
        body = app[app.index("async function syncDashboardTop("):]
        body = body[:body.index("\n}\n")]
        self.assertIn("token !== topSyncToken", body)

    def test_coming_back_to_the_app_refreshes_the_dashboard(self):
        """手机上「重新进软件」就是切走再切回来；这一下以前什么都不做。"""
        app = self._app()
        handler = app[app.index("document.addEventListener('visibilitychange'"):]
        handler = handler[:handler.index("});")]
        self.assertIn("activeSection === 'dashboard'", handler)
        self.assertIn("refreshDashboard()", handler)
        # **安静地刷**：他没点任何东西，不能弹「状态已刷新」。
        self.assertNotIn("notify: true", handler)
