"""The admin note, and the per-account "跑通了没有" lights.

Two features, one theme: the console must not tell the operator something it
cannot back up.

* **The note is the operator's, not the user's.** It is the only field on that
  panel that is *about* the account rather than *for* it, so the tests drive the
  endpoints the account itself can reach and assert the text is not in them. A
  note that leaks into `/api/me` or the data export would be read back to the
  person it is about, which is the one thing an operator writing in it is
  assuming cannot happen.

* **A light is green only when something actually succeeded.** The trap is real
  and it is in this schema: `record_connection_result` writes `last_test_at`
  *unconditionally*, and `record_mailbox_verification` / `update_mailbox_poll`
  write their timestamps the same way. The *error* column carries the outcome. So
  "there is a timestamp" means "somebody tried", and a light built on that would
  glow green for a key whose test just failed. Several tests below fail if the
  implementation ever switches to the timestamp alone.
"""

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

_TMP = tempfile.mkdtemp()
os.environ["INFE_PILOT_DB"] = _TMP + "/admin-note.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
os.environ["INFE_PILOT_MAX_USERS"] = "50"
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import web  # noqa: E402
from pilot_app.database import Database  # noqa: E402
from pilot_app.security import hash_password, token_hash  # noqa: E402
from pilot_app.web import db  # noqa: E402

ADMIN = "note-admin@example.com"
NOTE = "授权码填错过一次，10 月 3 日帮他改好了"
PASSWORD = "a-long-enough-password"


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
                raw = response.read().decode() or "{}"
                return response.status, json.loads(raw) if raw[0] in "{[" else raw
        except urllib.error.HTTPError as error:
            raw = error.read().decode()
            try:
                return error.code, json.loads(raw or "{}")
            except json.JSONDecodeError:
                return error.code, {"detail": raw}

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, payload=None):
        return self.request("POST", path, payload)

    def put(self, path, payload=None):
        return self.request("PUT", path, payload)


class LightsUnitTests(unittest.TestCase):
    """The predicate itself, with no server and no database in the way."""

    def lights(self, **row) -> dict:
        return {light["key"]: light for light in Database.verification_lights(row)}

    # -- the trap: a timestamp is not a success ---------------------------

    def test_a_failed_test_is_red_even_though_the_timestamp_is_set(self):
        """The whole reason this feature reads the error column.

        `record_connection_result` stamps `last_test_at` on failure too, so a
        light keyed on the timestamp alone would show green for a key that was
        just proven broken -- the exact lie the operator cannot catch by eye.
        """
        lights = self.lights(model_last_test_at="2026-09-15T01:00:00Z", model_error="401 Unauthorized")
        self.assertFalse(lights["model"]["ok"])
        self.assertEqual(lights["model"]["state"], "failed")
        self.assertIn("401", lights["model"]["detail"])

    def test_a_failed_mailbox_poll_is_red(self):
        lights = self.lights(last_polled_at="2026-09-15T01:00:00Z",
                             mailbox_error="[AUTHENTICATIONFAILED] Login error")
        self.assertFalse(lights["mailbox"]["ok"])
        self.assertEqual(lights["mailbox"]["state"], "failed")

    def test_success_is_green(self):
        lights = self.lights(last_polled_at="2026-09-15T01:00:00Z", mailbox_error="",
                             model_last_test_at="2026-09-15T01:00:00Z", model_error="",
                             search_last_test_at="2026-09-15T01:00:00Z", search_error="",
                             last_sent_at="2026-09-15T02:00:00Z")
        self.assertTrue(all(light["ok"] for light in lights.values()), lights)

    def test_never_tried_is_red_not_grey(self):
        """`untested` is still red -- but it must be *nameable* as its own red.

        "没测过" and "测了但失败" need different actions from the reader, so the
        states stay distinct even though both draw red.
        """
        lights = self.lights()
        for key in ("mailbox", "model", "search", "report"):
            self.assertFalse(lights[key]["ok"], key)
            self.assertEqual(lights[key]["state"], "untested", key)

    def test_a_stale_verify_error_cannot_keep_the_light_red(self):
        """Both mailbox writers share `last_error`, which always describes the
        *latest* attempt. OR-ing in the verify-only column would leave the light
        red after the mailbox started working -- and a red light that cannot be
        cleared is one the operator learns to ignore.
        """
        lights = self.lights(last_verified_at="2026-09-01T00:00:00Z",
                             last_verify_error="旧的登录错误",
                             last_polled_at="2026-09-15T00:00:00Z",
                             mailbox_error="")
        self.assertTrue(lights["mailbox"]["ok"])

    # -- the report light --------------------------------------------------

    def test_a_delivered_report_is_the_only_green_for_that_light(self):
        green = self.lights(last_sent_at="2026-09-15T02:00:00Z")
        self.assertTrue(green["report"]["ok"])

        # Generated but never handed to SMTP: not proof of anything.
        generated = self.lights(last_report_at="2026-09-15T02:00:00Z")
        self.assertFalse(generated["report"]["ok"])
        self.assertEqual(generated["report"]["state"], "failed")

        failed = self.lights(last_report_at="2026-09-15T02:00:00Z", failed_reports=3)
        self.assertFalse(failed["report"]["ok"])
        self.assertIn("3", failed["report"]["detail"])

    def test_a_daily_digest_is_not_proof_that_the_model_works(self):
        """2026-09-16: a real account showed 出报告 green while it had never
        received a single CityU mail. Its only sent report was the daily digest,
        which is built from the deterministic list and goes out with zero
        analysed messages -- so it proves 收信+发信, not the model.

        灯的设计前提是「绿灯不能没有它声称的那件事」：所以这里不是把灯改红
        （东西确实发出去了），而是让 detail 说清楚它证明了什么、没证明什么。
        """
        digest_only = self.lights(last_sent_at="2026-09-16T14:00:00Z", mailed_reports=0)
        self.assertTrue(digest_only["report"]["ok"], "简报确实发出去了，灯不该变红")
        self.assertIn("每日简报", digest_only["report"]["detail"])
        self.assertIn("还没有任何一封来信被分析过", digest_only["report"]["detail"])

        from_mail = self.lights(last_sent_at="2026-09-16T14:00:00Z", mailed_reports=2)
        self.assertEqual(from_mail["report"]["detail"], "报告真的发出去了")

    def test_every_light_is_always_present(self):
        """The console draws a fixed order; a missing key would silently vanish."""
        keys = [light["key"] for light in Database.verification_lights({})]
        self.assertEqual(keys, ["mailbox", "model", "search", "report"])

    def test_a_failed_light_never_carries_a_success_timestamp(self):
        for row in ({"model_last_test_at": "2026-09-15T01:00:00Z", "model_error": "boom"},
                    {"model_last_test_at": "2026-09-15T01:00:00Z", "model_error": ""}):
            light = self.lights(**row)["model"]
            self.assertEqual("at" in light, light["ok"])

    def test_a_red_light_says_when_it_failed(self):
        """红灯必须能说出它有多旧，否则「早就修好的旧账」和「现在还在坏」长得一样。

        2026-09-16 一个真实账号就卡在这里：它的每日简报在主人**换掉那个邮箱之前**
        失败过一次，之后一直没再发过报告，于是那盏灯一直红着，而卡片上没有任何东西
        能说明那是旧事。`at` 仍然是绿灯专用（它表示「什么时候通的」），红灯带的是
        另一个字段。
        """
        failed = self.lights(model_last_test_at="2026-09-15T01:00:00Z",
                             model_error="401 Unauthorized")["model"]
        self.assertEqual(failed["failed_at"], "2026-09-15T01:00:00Z")
        self.assertNotIn("at", failed)

        report = self.lights(failed_reports=1,
                             last_failed_at="2026-09-16T15:06:29+00:00")["report"]
        self.assertEqual(report["failed_at"], "2026-09-16T15:06:29+00:00")
        self.assertEqual(self.lights(failed_reports=1)["report"]["failed_at"], "")

    # -- 第三态：走平台 key 的账号 ------------------------------------------

    def test_an_account_on_the_platform_key_is_grey_not_red(self):
        """用户原话：「为什么点刷新用户状态还是亮红灯」。

        没有自己的 key 的账号，模型/搜索这两盏灯**点多少次刷新都不会变绿**：测试
        结果是写进 `connections` 那一行的，而他根本没有那一行。红灯点不亮，读的人
        就学会不看这个面板了——而这盏灯要证明的事（「他自己配的 key 能不能用」）
        对这个账号本来就不适用。
        """
        for kind in ("model", "search"):
            light = self.lights(**{f"platform_{kind}": True})[kind]
            self.assertEqual(light["state"], "shared", light)
            self.assertFalse(light["ok"], "它不是绿灯：平台 key 并没有在这个账号上被证明过")
            self.assertIn("平台兜底", light["detail"])
            self.assertIn("不会变绿", light["hint"])

    def test_an_account_with_its_own_key_is_judged_on_that_key(self):
        """自己的 key 永远压过平台兜底：有 key 就是红灯（而且点得亮）。"""
        light = self.lights(model_provider="deepseek", platform_model=True)["model"]
        self.assertEqual(light["state"], "untested")
        self.assertFalse(light["ok"])

        broken = self.lights(model_provider="deepseek", platform_model=True,
                             model_last_test_at="2026-09-15T01:00:00Z",
                             model_error="401 Unauthorized")["model"]
        self.assertEqual(broken["state"], "failed")

    def test_without_any_platform_key_the_red_is_actionable(self):
        """平台也没有兜底 key 时，红得对：这样谁都出不了报告，得有人去配一把。"""
        for kind in ("model", "search"):
            light = self.lights()[kind]
            self.assertEqual(light["state"], "untested", light)
            self.assertNotIn("hint", light)


class AdminNoteTests(unittest.TestCase):
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
        # Saved and restored rather than popped: other modules set this at import
        # time and rely on it still being there when they run.
        self._saved_admin = os.environ.get("INFE_PILOT_ADMIN_EMAILS")
        os.environ["INFE_PILOT_ADMIN_EMAILS"] = ADMIN
        db.initialize()
        with db.connect() as connection:
            for table in ("audit_log", "sessions", "reports", "messages", "mailboxes",
                          "connections", "profiles", "invites", "users"):
                connection.execute(f"DELETE FROM {table}")
        self.stamp = dt.datetime.now().timestamp()
        self.admin = self._register(ADMIN, admin=True)
        self.member = self._register(f"note-member-{self.stamp}@example.com")

    def tearDown(self):
        if self._saved_admin is None:
            os.environ.pop("INFE_PILOT_ADMIN_EMAILS", None)
        else:
            os.environ["INFE_PILOT_ADMIN_EMAILS"] = self._saved_admin

    # -- helpers -----------------------------------------------------------

    def _register(self, email: str, *, admin: bool = False) -> dict:
        """Create an account with a known password, straight in the database."""
        invite = db.create_invite(f"note-{email}-{self.stamp}", 1)
        user = db.create_user(email, hash_password(PASSWORD), token_hash(invite))
        if admin:
            db.upsert_profile(user["id"], {"school_email": "", "major": "", "year_of_study": "",
                                           "language": "bilingual", "timezone": "Asia/Hong_Kong"})
        return user

    def _login(self, email: str) -> Client:
        client = Client(self.base)
        status, body = client.post("/api/auth/login", {"email": email, "password": PASSWORD})
        self.assertEqual(status, 200, body)
        return client

    def _note_of(self, client: Client, user_id: str) -> str:
        status, body = client.get("/api/admin/users")
        self.assertEqual(status, 200, body)
        row = next(item for item in body["users"] if item["id"] == user_id)
        return row["admin_note"]

    # -- who may touch it --------------------------------------------------

    def test_anonymous_cannot_write_a_note(self):
        status, _ = Client(self.base).put(f"/api/admin/users/{self.member['id']}/note",
                                          {"note": "x"})
        self.assertEqual(status, 401)

    def test_an_ordinary_user_gets_404_not_403(self):
        """The console's existence is not disclosed to a non-admin."""
        member = self._login(self.member["email"])
        status, _ = member.put(f"/api/admin/users/{self.member['id']}/note", {"note": "我自己写的"})
        self.assertEqual(status, 404)
        status, _ = member.get("/api/admin/users")
        self.assertEqual(status, 404)

    def test_an_admin_can_write_and_read_it_back(self):
        admin = self._login(ADMIN)
        status, body = admin.put(f"/api/admin/users/{self.member['id']}/note", {"note": NOTE})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["admin_note"], NOTE)
        self.assertEqual(self._note_of(admin, self.member["id"]), NOTE)

    def test_an_empty_note_erases_it(self):
        admin = self._login(ADMIN)
        admin.put(f"/api/admin/users/{self.member['id']}/note", {"note": NOTE})
        status, body = admin.put(f"/api/admin/users/{self.member['id']}/note", {"note": ""})
        self.assertEqual(status, 200, body)
        self.assertEqual(self._note_of(admin, self.member["id"]), "")

    def test_a_body_without_the_key_does_not_erase_the_note(self):
        """Erasing must be an explicit act.

        A caller that forgot the field would otherwise wipe the note and get a
        200 back -- data loss that is indistinguishable from a successful save.
        """
        admin = self._login(ADMIN)
        admin.put(f"/api/admin/users/{self.member['id']}/note", {"note": NOTE})
        status, _ = admin.put(f"/api/admin/users/{self.member['id']}/note", {})
        self.assertEqual(status, 422)
        self.assertEqual(self._note_of(admin, self.member["id"]), NOTE)

    def test_an_over_long_note_is_refused_rather_than_truncated(self):
        admin = self._login(ADMIN)
        status, _ = admin.put(f"/api/admin/users/{self.member['id']}/note",
                              {"note": "字" * (Database.ADMIN_NOTE_LIMIT + 1)})
        self.assertEqual(status, 422)

    def test_a_note_for_a_stranger_is_a_404(self):
        admin = self._login(ADMIN)
        status, _ = admin.put("/api/admin/users/usr_does_not_exist/note", {"note": "x"})
        self.assertEqual(status, 404)

    def test_the_note_is_trimmed(self):
        admin = self._login(ADMIN)
        _, body = admin.put(f"/api/admin/users/{self.member['id']}/note",
                            {"note": f"\n  {NOTE}  \n"})
        self.assertEqual(body["admin_note"], NOTE)

    # -- it never reaches the user ----------------------------------------

    def test_the_note_is_not_in_the_users_own_api_me(self):
        admin = self._login(ADMIN)
        admin.put(f"/api/admin/users/{self.member['id']}/note", {"note": NOTE})
        member = self._login(self.member["email"])
        status, body = member.get("/api/me")
        self.assertEqual(status, 200)
        self.assertNotIn(NOTE, json.dumps(body, ensure_ascii=False))
        self.assertNotIn("admin_note", json.dumps(body, ensure_ascii=False))

    def test_the_note_is_not_in_the_users_own_data_export(self):
        admin = self._login(ADMIN)
        admin.put(f"/api/admin/users/{self.member['id']}/note", {"note": NOTE})
        member = self._login(self.member["email"])
        status, body = member.get("/api/account/export")
        self.assertEqual(status, 200)
        self.assertNotIn(NOTE, json.dumps(body, ensure_ascii=False))

    def test_the_note_is_not_in_the_users_login_response(self):
        admin = self._login(ADMIN)
        admin.put(f"/api/admin/users/{self.member['id']}/note", {"note": NOTE})
        client = Client(self.base)
        status, body = client.post("/api/auth/login",
                                   {"email": self.member["email"], "password": PASSWORD})
        self.assertEqual(status, 200)
        self.assertNotIn(NOTE, json.dumps(body, ensure_ascii=False))

    def test_the_users_own_settings_endpoint_cannot_write_the_note(self):
        """The note has its own route; that one must not become a back door."""
        member = self._login(self.member["email"])
        member.put("/api/profile", {"admin_note": "我自己写的", "language": "bilingual"})
        admin = self._login(ADMIN)
        self.assertEqual(self._note_of(admin, self.member["id"]), "")

    # -- the audit trail ---------------------------------------------------

    def test_the_audit_records_the_change_but_not_the_text(self):
        """The audit log is rendered to every admin and read when debugging.

        Somebody jotting down who they spoke to has not agreed to that sentence
        living in a log, so only the fact and the size are recorded.
        """
        admin = self._login(ADMIN)
        admin.put(f"/api/admin/users/{self.member['id']}/note", {"note": NOTE})
        with db.connect() as connection:
            rows = connection.execute(
                "SELECT action, detail FROM audit_log WHERE action='admin_note_changed'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn("length=", rows[0]["detail"])
        self.assertNotIn(NOTE, rows[0]["detail"])


class LightsEndpointTests(unittest.TestCase):
    """The lights as the console actually receives them."""

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
        self._saved_admin = os.environ.get("INFE_PILOT_ADMIN_EMAILS")
        os.environ["INFE_PILOT_ADMIN_EMAILS"] = ADMIN
        db.initialize()
        with db.connect() as connection:
            for table in ("audit_log", "sessions", "reports", "messages", "mailboxes",
                          "connections", "profiles", "invites", "users"):
                connection.execute(f"DELETE FROM {table}")
        self.stamp = dt.datetime.now().timestamp()
        invite = db.create_invite(f"lights-{self.stamp}", 1)
        self.member = db.create_user("lights@example.com", hash_password(PASSWORD),
                                     token_hash(invite))
        db.upsert_profile(self.member["id"], {"language": "bilingual", "timezone": "Asia/Hong_Kong"})
        self.admin = Client(self.base)
        status, body = self.admin.post("/api/auth/login", {"email": ADMIN, "password": PASSWORD})
        if status != 200:
            inv = db.create_invite(f"lights-admin-{self.stamp}", 1)
            db.create_user(ADMIN, hash_password(PASSWORD), token_hash(inv))
            status, body = self.admin.post("/api/auth/login", {"email": ADMIN, "password": PASSWORD})
        self.assertEqual(status, 200, body)

    def tearDown(self):
        if self._saved_admin is None:
            os.environ.pop("INFE_PILOT_ADMIN_EMAILS", None)
        else:
            os.environ["INFE_PILOT_ADMIN_EMAILS"] = self._saved_admin

    def _row(self) -> dict:
        status, body = self.admin.get("/api/admin/users")
        self.assertEqual(status, 200, body)
        return next(item for item in body["users"] if item["id"] == self.member["id"])

    def test_a_broken_mailbox_shows_red_through_the_api(self):
        """End to end: the poll failed, so the light must not be green."""
        mailbox_id = db.upsert_mailbox(self.member["id"], {
            "email": "box@example.com", "report_to": "box@example.com",
            "imap_host": "imap.qq.com", "imap_port": 993,
            "smtp_host": "smtp.qq.com", "smtp_port": 465,
            "encrypted_password": b"x",
        })
        db.update_mailbox_poll(mailbox_id, last_uid=1, uid_validity="1",
                               error="[AUTHENTICATIONFAILED] Login error")
        light = {item["key"]: item for item in self._row()["lights"]}["mailbox"]
        self.assertFalse(light["ok"])
        self.assertEqual(light["state"], "failed")

    def test_a_working_mailbox_shows_green_through_the_api(self):
        mailbox_id = db.upsert_mailbox(self.member["id"], {
            "email": "box@example.com", "report_to": "box@example.com",
            "imap_host": "imap.qq.com", "imap_port": 993,
            "smtp_host": "smtp.qq.com", "smtp_port": 465,
            "encrypted_password": b"x",
        })
        db.update_mailbox_poll(mailbox_id, last_uid=1, uid_validity="1")
        light = {item["key"]: item for item in self._row()["lights"]}["mailbox"]
        self.assertTrue(light["ok"], light)

    def test_the_lights_survive_a_status_change(self):
        """Pausing returned bare rows, so the badges vanished until a reload.

        The same drift would silently drop the lights, which is worse: the panel
        would look like "nothing is configured" right after the operator's click.
        """
        status, body = self.admin.put(
            f"/api/admin/users/{self.member['id']}/status/paused", {})
        self.assertEqual(status, 200, body)
        row = next(item for item in body["users"] if item["id"] == self.member["id"])
        self.assertIn("lights", row)
        self.assertIn("setup_gap", row)
        self.assertEqual(len(row["lights"]), 4)

    def test_the_lights_survive_a_settings_save(self):
        status, body = self.admin.put(
            f"/api/admin/users/{self.member['id']}/settings", {"major": "计算机科学"})
        self.assertEqual(status, 200, body)
        row = next(item for item in body["users"] if item["id"] == self.member["id"])
        self.assertIn("lights", row)
        self.assertIn("setup_gap", row)
        self.assertEqual(len(body["user"]["lights"]), 4)

    def test_the_lights_survive_a_note_save(self):
        status, body = self.admin.put(
            f"/api/admin/users/{self.member['id']}/note", {"note": "随便记一笔"})
        self.assertEqual(status, 200, body)
        row = next(item for item in body["users"] if item["id"] == self.member["id"])
        self.assertIn("lights", row)


class MigrationTests(unittest.TestCase):
    """The column is added to an *existing* database, which is the real path.

    Every other test here starts from a fresh file, where `admin_note` arrives
    via `CREATE TABLE`. Production is the other branch: `CREATE TABLE IF NOT
    EXISTS` is a no-op because the table is already there, so the column only
    appears if the `ALTER TABLE` in `initialize()` runs. Miss it and every
    admin request dies on "no such column: admin_note" -- a failure that no
    amount of fresh-database testing would have caught.
    """

    # The users table as it existed before this feature: everything except the
    # note. Written out by hand rather than derived, so that a future edit to
    # SCHEMA cannot quietly make this stop being an "old" database.
    OLD_USERS = """
        CREATE TABLE users (
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','deleted')),
            created_at TEXT NOT NULL,
            is_admin INTEGER NOT NULL DEFAULT 0
        )
    """

    def test_an_existing_database_gains_the_column_without_losing_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "old.sqlite3")
            connection = sqlite3.connect(path)
            connection.execute(self.OLD_USERS)
            connection.execute(
                "INSERT INTO users(id,email,password_hash,status,created_at,is_admin)"
                " VALUES('usr_old','old@example.com','x','active','2026-09-01T00:00:00Z',0)")
            connection.commit()
            connection.close()

            database = Database(path)
            database.initialize()

            with database.connect() as connection:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
            self.assertIn("admin_note", columns)
            # The row that was already there survives, with an empty note.
            row = database.find_user_for_login("old@example.com")
            self.assertIsNotNone(row)
            self.assertEqual(row["admin_note"], "")

            # And the new column is writable on that pre-existing row.
            database.set_admin_note("usr_old", "迁移过来的老账号")
            self.assertEqual(database.find_user_for_login("old@example.com")["admin_note"],
                             "迁移过来的老账号")

    def test_initialize_is_idempotent_for_the_new_column(self):
        """`initialize` runs on every boot, so the ALTER must be a no-op twice."""
        with tempfile.TemporaryDirectory() as folder:
            database = Database(os.path.join(folder, "twice.sqlite3"))
            database.initialize()
            database.initialize()
            with database.connect() as connection:
                columns = [row[1] for row in connection.execute("PRAGMA table_info(users)")]
            self.assertEqual(columns.count("admin_note"), 1)


if __name__ == "__main__":
    unittest.main()
