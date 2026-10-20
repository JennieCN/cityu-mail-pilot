"""多用户越权矩阵：**另一个用户的 id 永远不该打开他的东西**。

审查报告的 C 段把「双用户越权矩阵」列成扩容前要补的证据。`test_multiuser_scope` 管的是
**worker 那半边**（每个用户都被轮询、发件人过滤对谁都生效、两份报告不串），这里管
**HTTP 那半边**：拿到另一个人的 id（task_key / message_id / report_id），能不能读、能不能改。

判据是三条一起看，缺一条这个矩阵就会「全绿但是坏的」：

1. **别人的 id → 404**（不是 403：连"这个 id 存在"都不该泄露——和 `/api/admin/*` 同一条规矩）；
2. **数据一个字都没变**（只看状态码会被「404 但其实已经改了」骗过去）；
3. **自己的 id 仍然能用**（正向对照：否则「所有请求都 404」也能全绿——
   那测的是「接口坏了」，不是「越权被挡住了」）。
"""

import datetime as dt
import http.cookiejar
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

_TMP = tempfile.mkdtemp()
os.environ["INFE_PILOT_DB"] = _TMP + "/authz.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
os.environ["INFE_PILOT_MAX_USERS"] = "50"
os.environ.pop("INFE_PILOT_ORIGIN", None)
os.environ.pop("INFE_PILOT_ADMIN_EMAILS", None)

from pilot_app import database as database_mod  # noqa: E402
from pilot_app import mailio, reports, service as service_mod, web  # noqa: E402
from pilot_app.security import SecretBox, hash_password, token_hash  # noqa: E402

PASSWORD = "a-long-enough-password"
REPORT_BODY = ("## 1. 重要程度与一句话结论\n- 等级：高\n- 结论：周五前交作业。\n"
               "## 2. 必须采取的行动与截止时间\n- 提交作业到 Canvas（截止：2026-09-25 23:59）\n")


class Client:
    def __init__(self, base: str) -> None:
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                  urllib.request.HTTPCookieProcessor(self.jar))

    def request(self, method: str, path: str, payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as error:
            raw = error.read()
            try:
                return error.code, (json.loads(raw) if raw else {})
            except ValueError:
                return error.code, {"raw": raw[:120].decode("utf-8", "replace")}

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, payload=None):
        return self.request("POST", path, payload=payload)

    def put(self, path, payload=None):
        return self.request("PUT", path, payload=payload)


class AuthorizationMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database = database_mod.Database(os.path.join(_TMP, "authz.sqlite3"))
        cls.database.initialize()
        # **必须与 web 层同一把密钥**：夹具里的邮箱授权码/正文/报告都是加密的，
        # 而 HTTP 那条路上的 `get_service()` 用 `SecretBox.from_environment()` 解开它们。
        # 第一版这里自己 `SecretBox(os.urandom(32))`，于是每个需要解密的接口都 500——
        # 症状看起来像"接口坏了"，其实是夹具与产品用了两把钥匙。
        cls.box = SecretBox.from_environment()
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
        from unittest import mock
        db_patch = mock.patch.object(web, "get_db", return_value=self.database)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        saved = web._service_singleton
        web._service_singleton = None
        self.addCleanup(lambda: setattr(web, "_service_singleton", saved))
        web._login_attempts.clear()
        tag = os.urandom(4).hex()
        self.victim = self._user(f"victim-{tag}@example.com", tag)
        self.attacker = self._user(f"attacker-{tag}@example.com", tag + "b")
        self.attacker_client = Client(self.base)
        self._login(self.attacker_client, self.attacker["user"]["email"])
        self.anon = Client(self.base)

    # -- 夹具 -------------------------------------------------------------

    def _user(self, email: str, tag: str) -> dict:
        code = f"authz-{tag}"
        expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with self.database.connect() as connection:
            connection.execute("INSERT INTO invites(code_hash,expires_at) VALUES(?,?)",
                               (token_hash(code), expiry))
        user = self.database.create_user(email, hash_password(PASSWORD), token_hash(code))
        mailbox_id = self.database.upsert_mailbox(user["id"], {
            "email": f"box-{tag}@qq.com", "report_to": email, "imap_host": "imap.example.com",
            "imap_port": 993, "smtp_host": "smtp.example.com", "smtp_port": 465,
            "encrypted_password": self.box.encrypt("authcode-16chars",
                                                   context=f"mailbox:{user['id']}")})
        message_id = self.database.insert_message(
            user["id"], mailbox_id, "1", 11,
            {"subject": "作业截止", "sender_name": "老师", "sender_address": "student@my.cityu.edu.hk",
             "received": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
             "importance": "normal",
             "body": self.box.encrypt("请提交作业。", context=f"message:{user['id']}")})
        report_id = self.database.create_report(
            user_id=user["id"], message_id=message_id, kind="immediate", subject="【AI邮件摘要】作业截止",
            body=self.box.encrypt(REPORT_BODY, context=f"report:{user['id']}"), sent_to=email)
        tasks = reports.today_tasks(
            [(report_id, REPORT_BODY, message_id)],
            [{"id": message_id, "subject": "作业截止", "sender_name": "老师",
              "sender_address": "student@my.cityu.edu.hk",
              "received": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
              "importance": "normal"}],
            timezone="Asia/Hong_Kong")
        return {"user": user, "mailbox_id": mailbox_id, "message_id": message_id,
                "report_id": report_id, "task_key": tasks[0]["task_key"]}

    def _login(self, client, email: str) -> None:
        status, body = client.post("/api/auth/login", {"email": email, "password": PASSWORD})
        self.assertEqual(status, 200, body)

    # -- 矩阵 -------------------------------------------------------------

    def _victim_snapshot(self) -> dict:
        """受害者那一侧**所有可能被改动的行**。只看状态码会被「404 但其实已经改了」骗过去。"""
        with self.database.connect() as connection:
            report = connection.execute("SELECT status FROM reports WHERE id=?",
                                        (self.victim["report_id"],)).fetchone()
            message = connection.execute("SELECT status FROM messages WHERE id=?",
                                         (self.victim["message_id"],)).fetchone()
            states = connection.execute("SELECT COUNT(*) FROM task_states WHERE user_id=?",
                                        (self.victim["user"]["id"],)).fetchone()[0]
            feedback = connection.execute("SELECT COUNT(*) FROM feedback WHERE user_id=?",
                                          (self.victim["user"]["id"],)).fetchone()[0]
        return {"report": tuple(report), "message": message[0],
                "task_states": states, "feedback": feedback}

    def test_another_users_task_cannot_be_touched(self):
        before = self._victim_snapshot()
        key = self.victim["task_key"]
        status, body = self.attacker_client.put(f"/api/tasks/{key}", {"state": "done"})
        self.assertEqual(status, 404, f"别人的任务该 404，实际 {status} {body}")
        status, body = self.attacker_client.put(f"/api/tasks/{key}/priority", {"priority": "low"})
        self.assertEqual(status, 404, f"别人的任务优先级该 404，实际 {status} {body}")
        self.assertEqual(self._victim_snapshot(), before, "被拒的请求不许改动数据")

    def test_another_users_message_cannot_be_read(self):
        before = self._victim_snapshot()
        status, body = self.attacker_client.get(f"/api/messages/{self.victim['message_id']}/original")
        self.assertEqual(status, 404, f"别人的原信该 404，实际 {status} {body}")
        self.assertEqual(self._victim_snapshot(), before)

    def test_another_users_report_feedback_cannot_be_written(self):
        before = self._victim_snapshot()
        status, body = self.attacker_client.put(f"/api/reports/{self.victim['report_id']}/feedback",
                                                {"rating": "useful", "note": "越权写入"})
        self.assertEqual(status, 404, f"别人的报告反馈该 404，实际 {status} {body}")
        self.assertEqual(self._victim_snapshot(), before, "被拒的反馈不许落到别人的报告上")

    def test_anonymous_is_rejected_before_ownership_is_even_asked(self):
        """没登录是 401（不是 404）：矩阵里"没身份"与"别人的东西"必须分得开。"""
        for method, path, payload in (
                ("PUT", f"/api/tasks/{self.victim['task_key']}", {"state": "done"}),
                ("GET", f"/api/messages/{self.victim['message_id']}/original", None),
                ("PUT", f"/api/reports/{self.victim['report_id']}/feedback", {"rating": "useful"})):
            status, body = self.anon.request(method, path, payload)
            self.assertEqual(status, 401, f"{method} {path} 匿名该 401，实际 {status} {body}")

    def test_the_attacker_can_still_use_their_own(self):
        """正向对照：不然「全都 404」也能让上面三条全绿。"""
        key = self.attacker["task_key"]
        status, body = self.attacker_client.put(f"/api/tasks/{key}", {"state": "done"})
        self.assertEqual(status, 200, f"自己的任务该能标记，实际 {status} {body}")
        status, body = self.attacker_client.put(f"/api/tasks/{key}/priority", {"priority": "high"})
        self.assertEqual(status, 200, f"自己的优先级该能改，实际 {status} {body}")
        status, body = self.attacker_client.put(f"/api/reports/{self.attacker['report_id']}/feedback",
                                                {"rating": "useful", "note": "自己的"})
        self.assertEqual(status, 200, f"自己的报告该能反馈，实际 {status} {body}")
        # 匿名那侧也要有对照：**自己的**东西匿名同样 401（这不是越权，是没登录）
        status, _ = self.anon.get(f"/api/messages/{self.attacker['message_id']}/original")
        self.assertEqual(status, 401)

    def test_the_list_endpoints_only_carry_your_own_rows(self):
        """列表接口：别人的东西不该出现在我的响应里（哪怕 id 猜不到）。"""
        status, body = self.attacker_client.get("/api/tasks")
        self.assertEqual(status, 200)
        mine = {task["task_key"] for task in body.get("tasks", [])}
        self.assertNotIn(self.victim["task_key"], mine)
        status, body = self.attacker_client.get("/api/reports")
        self.assertEqual(status, 200)
        rows = body if isinstance(body, list) else body.get("reports", [])
        ids = {row["id"] for row in rows}
        self.assertNotIn(self.victim["report_id"], ids, "报告列表里出现了别人的报告")
        self.assertIn(self.attacker["report_id"], ids, "自己的报告要能看到（正向对照）")

    def test_the_admin_surface_is_not_merely_forbidden_it_is_invisible(self):
        """非管理员的 `/api/admin/*` 一律 404（不暴露后台存在）——矩阵里最外面那一圈。"""
        for method, path, payload in (
                ("GET", "/api/admin/users", None),
                ("GET", "/api/admin/usage", None),
                ("PUT", f"/api/admin/users/{self.victim['user']['id']}/settings", {"note": "x"}),
                ("PUT", f"/api/admin/users/{self.victim['user']['id']}/status/paused", None)):
            status, body = self.attacker_client.request(method, path, payload)
            self.assertEqual(status, 404, f"{method} {path} 该 404，实际 {status} {body}")
