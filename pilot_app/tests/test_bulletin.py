"""The public landing page has no bulletin board.

The announcement feature still exists for signed-in users. What was removed is
the **public** half of it: the rendering path on `/`, the console switch, the
``PUT .../board`` endpoint, and the anonymous image access that only existed to
serve that page. Operator-written text and operator-uploaded pictures must never
reach someone who is not signed in.

``is_public`` / ``public_at`` survive as **storage only** -- the rows that were
once on the board are the only record that they ever were, and dropping a column
means rewriting the table. Nothing reads or writes them any more, which is what
the last two tests here pin down.
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
os.environ["INFE_PILOT_DB"] = _TMP + "/bulletin.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
os.environ["INFE_PILOT_MAX_USERS"] = "50"
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import database as database_mod  # noqa: E402
from pilot_app import web  # noqa: E402
from pilot_app.security import hash_password, token_hash  # noqa: E402
from pilot_app.web import db  # noqa: E402


class Client:
    def __init__(self, base: str) -> None:
        self.base = base
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self.jar),
        )

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


def _decode(raw: bytes):
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw.decode("utf-8", "replace")


class LandingHasNoBulletinTests(unittest.TestCase):
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
        self.client = Client(self.base)
        with db.connect() as connection:
            connection.execute("DELETE FROM announcements")
            connection.execute("DELETE FROM announcement_dismissals")

    def _publish(self, *, title="系统维护", body="周六 22:00 起维护两小时。", tone="info"):
        return db.create_announcement(
            title=title,
            body=body,
            tone=tone,
            deliver_email=False,
            created_by="ops@example.com",
        )

    def _landing(self) -> str:
        status, body, _ = self.client.get("/")
        self.assertEqual(status, 200)
        return body

    def test_an_empty_page_has_no_board(self):
        page = self._landing()
        self.assertNotIn('id="board"', page)
        self.assertNotIn("布告栏", page)
        self.assertNotIn("{{BULLETIN}}", page)

    def test_a_public_notice_never_reaches_the_landing_page(self):
        self._publish(title="公开公告也不显示", body="这段文字只属于站内广播。")
        page = self._landing()
        self.assertNotIn('id="board"', page)
        self.assertNotIn("布告栏", page)
        self.assertNotIn("公开公告也不显示", page)
        self.assertNotIn("这段文字只属于站内广播。", page)
        self.assertNotIn("{{BULLETIN}}", page)

    def test_operator_text_is_never_rendered_on_the_public_page(self):
        self._publish(title='<script>alert(1)</script>', body="a<b & c")
        page = self._landing()
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertNotIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertNotIn("a<b & c", page)

    def test_a_legacy_public_row_is_readable_but_unreachable(self):
        """老库那 6 条「曾经贴过布告栏」的行：**读得出来，但再也写不进去。**

        这条以前叫 `test_the_storage_field_remains_backward_compatible`，测的是
        「撤下的公告不能再贴到布告栏」—— 那条接口随布告栏一起去掉了。所以改成钉住
        留下来的两件事：列与旧值都还在（那是唯一记录「它当时是公开的」的地方），
        而**写入路径已经没有了**：还传 `is_public` 会当场 TypeError。
        """
        path = os.path.join(_TMP, "legacy-public.sqlite3")
        fresh = database_mod.Database(path)
        fresh.initialize()
        with fresh.connect() as connection:
            connection.execute(
                """INSERT INTO announcements(id,title,body,tone,deliver_email,active,is_public,
                                             public_at,created_by,created_at)
                   VALUES('ann_legacy','老公告','正文','info',0,1,1,?,?,?)""",
                ("2026-09-14T14:23:06+00:00", "ops@example.com", "2026-09-14T14:00:00+00:00"))
        rows = {row["id"]: row for row in fresh.list_announcements(20)}
        self.assertIn("ann_legacy", rows, "老行照旧读得出来（列还在）")
        with self.assertRaises(TypeError):
            fresh.create_announcement(title="新公告", body="正文", tone="info",
                                      deliver_email=False, created_by="ops@example.com",
                                      is_public=True)

    def test_the_migration_adds_the_legacy_columns(self):
        path = os.path.join(_TMP, "old-announcements.sqlite3")
        connection = sqlite3.connect(path)
        connection.execute(
            """CREATE TABLE announcements (
                   id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
                   body TEXT NOT NULL DEFAULT '', tone TEXT NOT NULL DEFAULT 'info',
                   deliver_email INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1,
                   created_by TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                   withdrawn_at TEXT)"""
        )
        connection.commit()
        connection.close()
        fresh = database_mod.Database(path)
        fresh.initialize()
        columns = {
            row[1]
            for row in sqlite3.connect(path).execute(
                "PRAGMA table_info(announcements)"
            )
        }
        self.assertIn("is_public", columns)
        self.assertIn("public_at", columns)


class PendingCountTests(unittest.TestCase):
    """The in-app confirmation count still has to be correct."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db = database_mod.Database(os.path.join(self.temporary.name, "pilot.sqlite3"))
        self.db.initialize()
        invite = self.db.create_invite("pending", 1)
        self.user = self.db.create_user(
            "reader@example.com",
            hash_password("a-long-enough-password"),
            token_hash(invite),
        )
        with self.db.connect() as connection:
            connection.execute("DELETE FROM announcements")
            connection.execute("DELETE FROM announcement_dismissals")

    def _publish(self, title: str) -> str:
        return self.db.create_announcement(
            title=title,
            body="正文",
            tone="info",
            deliver_email=False,
            created_by="ops@example.com",
        )

    def test_no_announcements_means_zero(self):
        self.assertEqual(self.db.count_pending_announcements(self.user["id"]), 0)

    def test_every_active_announcement_counts_until_this_user_confirms_it(self):
        first = self._publish("第一条")
        second = self._publish("第二条")
        self.assertEqual(self.db.count_pending_announcements(self.user["id"]), 2)
        self.db.dismiss_announcement(first, self.user["id"])
        self.assertEqual(self.db.count_pending_announcements(self.user["id"]), 1)
        self.db.dismiss_announcement(second, self.user["id"])
        self.assertEqual(self.db.count_pending_announcements(self.user["id"]), 0)

    def test_another_users_confirmation_does_not_count_as_mine(self):
        announcement = self._publish("只给别人确认过")
        invite = self.db.create_invite("pending2", 1)
        other = self.db.create_user(
            "other@example.com",
            hash_password("a-long-enough-password"),
            token_hash(invite),
        )
        self.db.dismiss_announcement(announcement, other["id"])
        self.assertEqual(self.db.count_pending_announcements(self.user["id"]), 1)

    def test_a_withdrawn_announcement_stops_counting(self):
        alive = self._publish("还在的")
        gone = self._publish("要撤下的")
        self.assertEqual(self.db.count_pending_announcements(self.user["id"]), 2)
        self.db.withdraw_announcement(gone)
        self.assertEqual(self.db.count_pending_announcements(self.user["id"]), 1)
        self.db.dismiss_announcement(alive, self.user["id"])
        self.assertEqual(self.db.count_pending_announcements(self.user["id"]), 0)


if __name__ == "__main__":
    unittest.main()
