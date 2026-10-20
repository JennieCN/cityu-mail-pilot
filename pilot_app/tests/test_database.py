import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pilot_app.database import LAST_SEEN_SINCE_KEY, Database
from pilot_app.security import hash_password, token_hash


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temporary.name) / "pilot.sqlite3")
        self.db.initialize()

    def tearDown(self):
        self.temporary.cleanup()

    def user(self, email: str, code: str) -> dict:
        expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with self.db.connect() as connection:
            connection.execute("INSERT INTO invites(code_hash,expires_at) VALUES(?,?)", (token_hash(code), expires))
        return self.db.create_user(email, hash_password("long-enough-password"), token_hash(code))

    def test_the_migration_adds_the_activity_column_to_an_existing_database(self):
        """生产库比这一列老；升级必须自己把列和「从什么时候开始记」一起补上。

        少了列 → `SELECT u.last_seen_at` 当场 no such column（整个管理后台 500）；
        少了那个时间点 → 面板会对升级前发出的提醒说「他没回来」，
        而那段时间根本没人看着。
        """
        path = Path(self.temporary.name) / "old-users.sqlite3"
        connection = sqlite3.connect(path)
        connection.execute(
            """CREATE TABLE users (
                   id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                   password_hash TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'active'
                       CHECK(status IN ('active','paused','deleted')),
                   created_at TEXT NOT NULL)""")
        connection.execute(
            "INSERT INTO users(id,email,password_hash,status,created_at)"
            " VALUES('usr_old','old@example.com','x','active','2026-01-01T00:00:00+00:00')")
        connection.commit()
        connection.close()
        fresh = Database(path)
        fresh.initialize()
        columns = {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(users)")}
        self.assertIn("last_seen_at", columns)
        self.assertTrue(fresh.get_setting(LAST_SEEN_SINCE_KEY, ""), "开始记录的时间也要落库")
        # 老账号是空字符串，不是 NULL：面板据此说「从没用过」。
        with fresh.connect() as conn:
            row = conn.execute("SELECT last_seen_at FROM users WHERE id='usr_old'").fetchone()
        self.assertEqual(row["last_seen_at"], "")
        # 幂等：再跑一次不许报错，也不许把开始时间刷新成现在。
        first = fresh.get_setting(LAST_SEEN_SINCE_KEY, "")
        fresh.initialize()
        self.assertEqual(fresh.get_setting(LAST_SEEN_SINCE_KEY, ""), first)

    def test_invite_is_single_use(self):
        self.user("one@example.com", "invite")
        with self.assertRaises(ValueError):
            self.db.create_user("two@example.com", hash_password("long-enough-password"), token_hash("invite"))

    def test_reports_are_isolated_by_user(self):
        one = self.user("one@example.com", "one")
        two = self.user("two@example.com", "two")
        self.db.create_report(user_id=one["id"], message_id=None, kind="test", subject="private one", body=b"cipher-a", sent_to="one@example.com")
        self.db.create_report(user_id=two["id"], message_id=None, kind="test", subject="private two", body=b"cipher-b", sent_to="two@example.com")
        self.assertEqual([row["subject"] for row in self.db.list_reports(one["id"])], ["private one"])
        self.assertEqual([row["subject"] for row in self.db.list_reports(two["id"])], ["private two"])

    def test_profile_lists_round_trip(self):
        user = self.user("one@example.com", "one")
        self.db.upsert_profile(user["id"], {"school_email": "student@my.cityu.edu.hk", "major": "通信工程", "courses_json": json.dumps(["C++", "密码学"]), "updated_at": "ignored"})
        profile = self.db.get_profile(user["id"])
        self.assertEqual(profile["major"], "通信工程")
        self.assertEqual(profile["school_email"], "student@my.cityu.edu.hk")
        self.assertEqual(profile["courses"], ["C++", "密码学"])

    def test_changing_mailbox_identity_resets_uid_state_but_keeps_reports(self):
        user = self.user("owner@example.com", "mailbox-change")
        common = {
            "report_to": "owner@example.com", "imap_host": "imap.qq.com", "imap_port": 993,
            "smtp_host": "smtp.qq.com", "smtp_port": 465,
            "encrypted_password": b"placeholder", "enabled": True,
        }
        mailbox_id = self.db.upsert_mailbox(user["id"], {**common, "email": "old@qq.com"})
        self.db.update_mailbox_poll(mailbox_id, last_uid=88, uid_validity="123")
        message_id = self.db.insert_message(user["id"], mailbox_id, "123", 88, {
            "subject": "old message", "received": "2026-09-13T00:00:00+00:00", "body": "old",
        })
        report_id = self.db.create_report(
            user_id=user["id"], message_id=message_id, kind="immediate", subject="kept report",
            body="kept", sent_to="owner@example.com",
        )

        # Updating only credentials/config keeps the ingestion cursor.
        self.db.upsert_mailbox(user["id"], {**common, "email": "old@qq.com", "encrypted_password": b"new"})
        self.assertEqual(self.db.get_mailbox(user["id"])["last_uid"], 88)

        # Connecting another inbox resets UID identity and detaches, but does
        # not erase, the report already delivered to the user.
        self.db.upsert_mailbox(user["id"], {**common, "email": "new@qq.com"})
        mailbox = self.db.get_mailbox(user["id"])
        self.assertEqual(mailbox["last_uid"], 0)
        self.assertEqual(mailbox["uid_validity"], "")
        with self.db.connect() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM messages WHERE mailbox_id=?", (mailbox_id,)
            ).fetchone()[0], 0)
            report = connection.execute("SELECT message_id FROM reports WHERE id=?", (report_id,)).fetchone()
        self.assertIsNone(report["message_id"])

    def test_second_copy_of_the_same_mail_is_not_stored_again(self):
        """Two forwarding rules deliver one mail twice with different UIDs.

        IMAP UIDs differ, so only the RFC 5322 Message-ID can tell the copies
        apart from genuinely new mail; without this the user gets two reports.
        """
        user = self.user("dup@example.com", "dup")
        mailbox_id = self.db.upsert_mailbox(user["id"], {
            "email": "me@qq.com", "report_to": "me@qq.com", "imap_host": "imap.qq.com", "imap_port": 993,
            "smtp_host": "smtp.qq.com", "smtp_port": 465, "encrypted_password": b"cipher",
        })
        first = self.db.insert_message(user["id"], mailbox_id, "v1", 100, {
            "subject": "Course deadline", "received": "2026-09-13T00:00:00+00:00", "body": "cipher-a",
            "message_key": "<20260913091828.5982EBAE32@smtp82.ad.cityu.edu.hk>",
        })
        second = self.db.insert_message(user["id"], mailbox_id, "v1", 101, {
            "subject": "Course deadline", "received": "2026-09-13T00:00:04+00:00", "body": "cipher-a",
            "message_key": "<20260913091828.5982EBAE32@smtp82.ad.cityu.edu.hk>",
        })
        self.assertIsInstance(first, str)
        self.assertIsNone(second)
        with self.db.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE user_id=?", (user["id"],)
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_different_mails_without_message_id_are_both_kept(self):
        """A missing Message-ID must never drop a real, distinct email."""
        user = self.user("nokey@example.com", "nokey")
        mailbox_id = self.db.upsert_mailbox(user["id"], {
            "email": "me@qq.com", "report_to": "me@qq.com", "imap_host": "imap.qq.com", "imap_port": 993,
            "smtp_host": "smtp.qq.com", "smtp_port": 465, "encrypted_password": b"cipher",
        })
        for uid in (200, 201):
            self.db.insert_message(user["id"], mailbox_id, "v1", uid, {
                "subject": f"Notice {uid}", "received": "2026-09-13T00:00:00+00:00", "body": "cipher",
            })
        with self.db.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE user_id=?", (user["id"],)
            ).fetchone()[0]
        self.assertEqual(count, 2)

    def test_same_message_id_from_another_user_is_kept(self):
        """Per-user isolation: one user's dedup key must not hide another's mail."""
        one = self.user("dup-one@example.com", "dup-one")
        two = self.user("dup-two@example.com", "dup-two")
        key = "<shared-forward@smtp82.ad.cityu.edu.hk>"
        stored = []
        for user, email in ((one, "one@qq.com"), (two, "two@qq.com")):
            mailbox_id = self.db.upsert_mailbox(user["id"], {
                "email": email, "report_to": email, "imap_host": "imap.qq.com", "imap_port": 993,
                "smtp_host": "smtp.qq.com", "smtp_port": 465, "encrypted_password": b"cipher",
            })
            stored.append(self.db.insert_message(user["id"], mailbox_id, "v1", 300, {
                "subject": "Shared notice", "received": "2026-09-13T00:00:00+00:00", "body": "cipher",
                "message_key": key,
            }))
        self.assertTrue(all(isinstance(value, str) for value in stored))

    def test_initialize_migrates_a_premessagekey_database(self):
        """Regression: the schema index on messages.message_key must not run
        before the column exists, or every existing deployment fails to start."""
        import sqlite3
        legacy = Path(self.temporary.name) / "legacy.sqlite3"
        connection = sqlite3.connect(legacy)
        connection.executescript(
            """
            CREATE TABLE users(id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL);
            CREATE TABLE mailboxes(id TEXT PRIMARY KEY, user_id TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL, report_to TEXT NOT NULL, imap_host TEXT NOT NULL,
                imap_port INTEGER NOT NULL, smtp_host TEXT NOT NULL, smtp_port INTEGER NOT NULL,
                encrypted_password BLOB NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                uid_validity TEXT NOT NULL DEFAULT '', last_uid INTEGER NOT NULL DEFAULT 0,
                last_polled_at TEXT, last_error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
            CREATE TABLE messages(id TEXT PRIMARY KEY, user_id TEXT NOT NULL, mailbox_id TEXT NOT NULL,
                uid_validity TEXT NOT NULL DEFAULT '', imap_uid INTEGER NOT NULL,
                subject TEXT NOT NULL, sender_name TEXT NOT NULL DEFAULT '',
                sender_address TEXT NOT NULL DEFAULT '', received_at TEXT NOT NULL,
                importance TEXT NOT NULL DEFAULT 'normal', body BLOB NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT, last_error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                UNIQUE(mailbox_id, uid_validity, imap_uid));
            INSERT INTO users VALUES('usr','legacy@example.com','hash','active','2026-09-01T00:00:00+00:00');
            INSERT INTO mailboxes VALUES('mbx','usr','me@qq.com','me@qq.com','imap.qq.com',993,
                'smtp.qq.com',465,X'00',1,'',0,NULL,'','2026-09-01T00:00:00+00:00');
            INSERT INTO messages VALUES('msg','usr','mbx','',7,'Old mail','','','2026-09-01T00:00:00+00:00',
                'normal',X'00','sent',0,NULL,'','2026-09-01T00:00:00+00:00');
            """
        )
        connection.commit()
        connection.close()

        migrated = Database(legacy)
        migrated.initialize()
        migrated.initialize()  # must be idempotent
        with migrated.connect() as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
            mailbox_columns = {row[1] for row in connection.execute("PRAGMA table_info(mailboxes)")}
            kept = connection.execute("SELECT subject FROM messages").fetchone()[0]
        self.assertIn("message_key", columns)
        self.assertIn("last_verified_at", mailbox_columns)
        self.assertIn("last_verify_error", mailbox_columns)
        self.assertEqual(kept, "Old mail")

    def test_initialize_migrates_a_preschoolemail_database(self):
        import sqlite3
        legacy = Path(self.temporary.name) / "legacy-profile.sqlite3"
        connection = sqlite3.connect(legacy)
        connection.executescript(
            """
            CREATE TABLE users(id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL);
            CREATE TABLE profiles(user_id TEXT PRIMARY KEY, major TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL);
            """
        )
        connection.commit()
        connection.close()
        migrated = Database(legacy)
        migrated.initialize()
        with migrated.connect() as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(profiles)")}
        self.assertIn("school_email", columns)

    def test_initialize_relaxes_an_old_status_check_and_keeps_data(self):
        """Regression: an older DB only allowed 5 statuses, so writing the new
        'skipped' status failed. SQLite cannot ALTER a CHECK, so initialize()
        rebuilds the table; it must stay idempotent and lose nothing."""
        import sqlite3
        legacy = Path(self.temporary.name) / "legacy-status.sqlite3"
        connection = sqlite3.connect(legacy)
        connection.executescript(
            """
            CREATE TABLE users(id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL);
            CREATE TABLE mailboxes(id TEXT PRIMARY KEY, user_id TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL, report_to TEXT NOT NULL, imap_host TEXT NOT NULL,
                imap_port INTEGER NOT NULL, smtp_host TEXT NOT NULL, smtp_port INTEGER NOT NULL,
                encrypted_password BLOB NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                uid_validity TEXT NOT NULL DEFAULT '', last_uid INTEGER NOT NULL DEFAULT 0,
                last_polled_at TEXT, last_error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
            CREATE TABLE messages(id TEXT PRIMARY KEY, user_id TEXT NOT NULL, mailbox_id TEXT NOT NULL,
                uid_validity TEXT NOT NULL DEFAULT '', imap_uid INTEGER NOT NULL,
                subject TEXT NOT NULL, sender_name TEXT NOT NULL DEFAULT '',
                sender_address TEXT NOT NULL DEFAULT '', received_at TEXT NOT NULL,
                importance TEXT NOT NULL DEFAULT 'normal', body BLOB NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','processing','sent','failed')),
                attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT,
                last_error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                UNIQUE(mailbox_id, uid_validity, imap_uid));
            CREATE TABLE reports(id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                message_id TEXT REFERENCES messages(id) ON DELETE SET NULL, kind TEXT NOT NULL,
                subject TEXT NOT NULL, body_markdown BLOB NOT NULL,
                status TEXT NOT NULL DEFAULT 'generated', sent_to TEXT NOT NULL DEFAULT '',
                report_date TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, sent_at TEXT,
                UNIQUE(user_id, kind, report_date, message_id));
            INSERT INTO users VALUES('usr','old@example.com','hash','active','2026-09-01T00:00:00+00:00');
            INSERT INTO mailboxes VALUES('mbx','usr','me@qq.com','me@qq.com','imap.qq.com',993,
                'smtp.qq.com',465,X'00',1,'',0,NULL,'','2026-09-01T00:00:00+00:00');
            INSERT INTO messages VALUES('msg1','usr','mbx','',7,'CityU notice','','','2026-09-01T00:00:00+00:00',
                'normal',X'00','sent',0,NULL,'','2026-09-01T00:00:00+00:00');
            INSERT INTO reports VALUES('rpt1','usr','msg1','immediate','subj',X'00','sent','me@qq.com','','',
                '2026-09-01T00:00:00+00:00',NULL);
            """
        )
        connection.commit()
        connection.close()

        migrated = Database(legacy)
        migrated.initialize()
        migrated.initialize()  # idempotent

        with migrated.connect() as conn:
            definition = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='messages'").fetchone()[0]
            kept = conn.execute("SELECT status,subject FROM messages").fetchone()
            linked = conn.execute("SELECT COUNT(*) FROM reports WHERE message_id IS NOT NULL").fetchone()[0]
            integrity = conn.execute("PRAGMA foreign_key_check").fetchall()
        self.assertIn("skipped", definition, "约束必须已放宽")
        self.assertEqual(kept["subject"], "CityU notice")
        self.assertEqual(linked, 1, "reports 与 messages 的关联必须保留")
        self.assertEqual(integrity, [])
        # The new status must now be writable.
        migrated.mark_message_skipped_by_uid("mbx", "", 7, "测试原因")
        with migrated.connect() as conn:
            row = conn.execute("SELECT status,skip_reason FROM messages WHERE id='msg1'").fetchone()
        self.assertEqual(row["status"], "skipped")
        self.assertEqual(row["skip_reason"], "测试原因")


class VolumeWindowTests(unittest.TestCase):
    """端到端样本要分两个窗口返回：整个窗口，以及**最近几天**那一小段。

    为什么值得单独钉：`capacity.advise` 用最近的这一小段回答「这一台现在多快」。
    换主服务会把整个延迟分布搬走——2026-09-23 切到本机那台时，14 天窗口里的 p50
    还是上一任供应商的 7 秒，而当天本机那档已经是 10.5 秒。窗口切错了，容量建议
    就会一直描述一个已经不在干活的供应商。
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temporary.name) / "pilot.sqlite3")
        self.db.initialize()
        self.uid = 0
        expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with self.db.connect() as connection:
            connection.execute("INSERT INTO invites(code_hash,expires_at) VALUES(?,?)",
                               (token_hash("code"), expires))
        self.user = self.db.create_user("volume@example.com",
                                        hash_password("long-enough-password"),
                                        token_hash("code"))
        self.mailbox_id = self.db.upsert_mailbox(self.user["id"], {
            "email": "box@example.com", "report_to": "box@example.com",
            "imap_host": "imap.qq.com", "imap_port": 993,
            "smtp_host": "smtp.qq.com", "smtp_port": 465,
            "encrypted_password": b"\x00",
        })

    def tearDown(self):
        self.temporary.cleanup()

    def _report(self, days_ago: float, seconds: float) -> None:
        """一封信 + 它的报告，两者相隔 `seconds` 秒，报告落在 `days_ago` 天前。

        **时间戳必须带微秒**（2026-09-23 修的一条随机红）。`recent_volume` 的合理区间是
        `1 <= delta < 3600`，而这两个时间戳原先按**秒**截断：0.2 秒的间隔被压成 0 或 1 秒，
        压成 1 时正好落在闭下界上——用例于是随机变红（实测 6 次红 2 次，CI 上同样会碰）。
        产品那边的过滤是对的（注释写着「一秒以内是同一批写入」），错的是夹具把要测的
        那个间隔做丢了；所以修的是夹具，`database.py` 一行都不动。
        """
        self.uid += 1
        now = dt.datetime.now(dt.timezone.utc)
        produced = now - dt.timedelta(days=days_ago)
        arrived = produced - dt.timedelta(seconds=seconds)
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO messages(id,user_id,mailbox_id,imap_uid,subject,body,status,"
                "received_at,created_at) VALUES(?,?,?,?,?,?, 'sent',?,?)",
                (f"msg_{days_ago}_{seconds}", self.user["id"], self.mailbox_id, self.uid, "s", b"b",
                 arrived.isoformat(timespec="microseconds"),
                 arrived.isoformat(timespec="microseconds")))
            connection.execute(
                "INSERT INTO reports(id,user_id,message_id,kind,subject,body_markdown,"
                "status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (f"rep_{days_ago}_{seconds}", self.user["id"], f"msg_{days_ago}_{seconds}",
                 "immediate", "s", b"b", "sent", produced.isoformat(timespec="microseconds")))

    def test_the_recent_window_is_a_subset_of_the_full_one(self):
        self._report(0.5, 3)      # 最近
        self._report(0.5, 4)      # 最近
        self._report(5, 30)       # 更早（在 14 天窗口里，不在最近窗口里）
        volume = self.db.recent_volume(14)
        full = volume["report_seconds_end_to_end"]
        recent = volume["report_seconds_recent"]
        self.assertEqual(len(full), 3)
        self.assertEqual(len(recent), 2)
        self.assertEqual(sorted(recent), [3, 4])
        for secs in recent:
            self.assertIn(secs, full, "最近窗口必须是整个窗口的子集")

    def test_a_sample_older_than_the_recent_window_is_not_counted_as_recent(self):
        self._report(9, 8)
        volume = self.db.recent_volume(14)
        self.assertEqual(volume["report_seconds_end_to_end"], [8])
        self.assertEqual(volume["report_seconds_recent"], [])

    def test_the_plausible_range_filter_applies_to_both_windows(self):
        """一秒以内是同一批写入，一小时以上多半是排队/重试/时钟问题——两窗口都要滤掉。"""
        self._report(0.5, 0.2)
        self._report(0.5, 4000)
        volume = self.db.recent_volume(14)
        self.assertEqual(volume["report_seconds_end_to_end"], [])
        self.assertEqual(volume["report_seconds_recent"], [])


class KeyCircuitTests(unittest.TestCase):
    """The breaker that stops a rejected model key from eating the queue.

    Item 8 of the open-items list. The property that matters most is not "it
    trips" but "**it never loses mail**": a suspended account's messages must
    stay `pending` and become due again on their own. A breaker that dropped or
    failed them would turn a recoverable credential problem into data loss.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temporary.name) / "pilot.sqlite3")
        self.db.initialize()
        expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with self.db.connect() as connection:
            connection.execute("INSERT INTO invites(code_hash,expires_at) VALUES(?,?)",
                               (token_hash("code"), expires))
        self.user = self.db.create_user("key@example.com", hash_password("long-enough-password"),
                                        token_hash("code"))
        self.mailbox = self.db.upsert_mailbox(self.user["id"], {
            "email": "key@example.com", "report_to": "key@example.com",
            "imap_host": "imap.example.com", "imap_port": 993,
            "smtp_host": "smtp.example.com", "smtp_port": 465,
            "encrypted_password": b"placeholder", "enabled": True,
        })

    def tearDown(self):
        self.temporary.cleanup()

    def queue(self, uid: int = 1) -> str:
        return self.db.insert_message(self.user["id"], self.mailbox, "1", uid, {
            "subject": "Course", "received": "2026-09-15T00:00:00+00:00", "body": "hello",
        })

    def trip(self) -> None:
        for _ in range(3):
            self.db.record_key_failure(self.user["id"], "model", "HTTP 401：invalid api key")

    def test_the_first_two_failures_do_not_suspend_anything(self):
        """One bad answer can be a provider hiccup; two still get retried."""
        self.queue()
        first = self.db.record_key_failure(self.user["id"], "model", "HTTP 401")
        second = self.db.record_key_failure(self.user["id"], "model", "HTTP 401")
        self.assertIsNone(first["open_until"])
        self.assertIsNone(second["open_until"])
        self.assertFalse(self.db.key_circuit_open(self.user["id"]))
        self.assertEqual(len(self.db.due_messages()), 1)

    def test_the_third_failure_suspends_the_account(self):
        self.queue()
        self.trip()
        self.assertTrue(self.db.key_circuit_open(self.user["id"]))
        self.assertEqual(self.db.open_key_circuits("model")[0]["failures"], 3)

    def test_a_suspended_account_still_has_its_messages(self):
        """The whole point: defer, never drop.

        Asserted on the row itself and not merely on "the queue is empty" --
        "not in the queue" is also what a lost message looks like.
        """
        message = self.queue()
        self.trip()
        self.assertEqual(self.db.due_messages(), [])
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT status, attempts, last_error FROM messages WHERE id=?", (message,)
            ).fetchone()
        self.assertEqual(row["status"], "pending", "熔断期间邮件必须还是 pending")
        self.assertEqual(int(row["attempts"]), 0, "没有真的尝试过，就不该记一次尝试")
        self.assertEqual(str(row["last_error"]), "")

    def test_the_window_expiring_puts_it_back_in_the_queue(self):
        """That is the half-open probe: after the window the account is due again."""
        self.queue()
        self.trip()
        self.assertEqual(self.db.due_messages(), [])
        # Move the window into the past, the way the passage of time would.
        past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=1)).isoformat(timespec="seconds")
        with self.db.connect() as connection:
            connection.execute("UPDATE key_circuits SET open_until=? WHERE user_id=?", (past, self.user["id"]))
        self.assertFalse(self.db.key_circuit_open(self.user["id"]))
        self.assertEqual(len(self.db.due_messages()), 1)
        self.assertEqual(self.db.open_key_circuits("model"), [])

    def test_one_success_clears_everything(self):
        self.queue()
        self.trip()
        self.db.clear_key_failures(self.user["id"], "model")
        self.assertFalse(self.db.key_circuit_open(self.user["id"]))
        self.assertEqual(len(self.db.due_messages()), 1)

    def test_replacing_the_credential_clears_the_breaker(self):
        """A user who just pasted a new key must not have to wait out the window.

        Asserted at `upsert_connection` rather than at either HTTP handler,
        because that is the one method both save paths go through.
        """
        self.trip()
        self.assertTrue(self.db.key_circuit_open(self.user["id"]))
        self.db.upsert_connection(self.user["id"], {
            "kind": "model", "provider": "deepseek", "model": "deepseek-chat", "base_url": "",
            "encrypted_api_key": b"new-cipher", "config_json": "{}", "enabled": True,
        })
        self.assertFalse(self.db.key_circuit_open(self.user["id"]),
                         "换了 key 就该立刻恢复，不该等窗口过去")

    def test_the_console_list_names_the_account_and_what_to_tell_the_operator(self):
        self.trip()
        rows = self.db.open_key_circuits("model")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["email"], "key@example.com")
        self.assertEqual(rows[0]["failures"], 3)
        self.assertTrue(rows[0]["open_until"])
        self.assertIn("401", rows[0]["reason"])

    def test_search_failures_do_not_suspend_generation(self):
        """The two credentials are separate; only the model one gates the queue."""
        self.queue()
        for _ in range(3):
            self.db.record_key_failure(self.user["id"], "search", "HTTP 401")
        self.assertFalse(self.db.key_circuit_open(self.user["id"], "model"))
        self.assertEqual(len(self.db.due_messages()), 1)


if __name__ == "__main__":
    unittest.main()


class SchemaVersionGateTests(unittest.TestCase):
    """`SCHEMA_VERSION` 是一道闸门，而闸门只有一个会**静默**犯的错。

    它挡住的是唯一那件昂贵的事（重写 `messages`）。挡对了省一次全表复制，
    挡错了就是「迁移没跑，而没人发现」——所以这里钉三件互不重叠的事：

    1. **当前值是几**（棘轮）：改了 `SCHEMA` 或 `RETIRED_INDEXES` 却忘了 +1，
       闸门就会一直放行一个过期的版本号。和 `AGENTS.md` 的字节预算同一个手法——
       想动它就必须在测试里明写一次。
    2. **版本号最后才盖**：迁移中途失败的文件不许自称已经是新版本。
    3. **盖过章的库仍然能补列**：这正是第一版闸门踩的坑（`token_usage.on_platform`
       永远补不上，一读就 `no such column`）。
    """

    #: 2026-10-19：日历索引及报告来信时间持久化加入 schema 5。
    RECORDED = 5

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name) / "pilot.sqlite3"
        self.db = Database(self.path)
        self.db.initialize()

    def tearDown(self):
        self.temporary.cleanup()

    def test_the_recorded_schema_version_is_the_one_this_build_writes(self):
        from pilot_app import database as database_module

        self.assertEqual(
            database_module.SCHEMA_VERSION, self.RECORDED,
            "SCHEMA_VERSION 变了：如果这是有意的，把 RECORDED 一起改；"
            "如果不是，说明改动 SCHEMA / RETIRED_INDEXES 时忘了 +1，闸门会放行过期迁移。")

    def test_a_stamped_database_records_it_in_app_settings(self):
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key='schema_version'").fetchone()
        self.assertIsNotNone(row, "迁移跑完必须盖章，否则每次启动都重跑")
        self.assertEqual(int(row[0]), self.RECORDED)

    def test_a_failed_migration_does_not_claim_the_new_version(self):
        """盖章必须在最后一步：中途炸掉的文件必须留下旧版本号，下次重试。"""
        from unittest import mock

        fresh = Database(Path(self.temporary.name) / "failed.sqlite3")
        with mock.patch.object(Database, "_relax_message_status_check",
                               side_effect=RuntimeError("迁移中途炸了")):
            with self.assertRaises(RuntimeError):
                fresh.initialize()
        with fresh.connect() as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key='schema_version'").fetchone()
        self.assertIsNone(row, "迁移没跑完就盖了章，那一步以后永远不会重试")

    def test_a_stamped_database_still_gains_a_missing_additive_column(self):
        """闸门只挡昂贵的那件事，**不挡补列**——第一版就是在这里破的升级路径。"""
        with self.db.connect() as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(token_usage)")}
            if "on_platform" not in columns:
                self.skipTest("这一版没有 on_platform 列")
            connection.execute("ALTER TABLE token_usage DROP COLUMN on_platform")
            connection.commit()
        self.db.initialize()
        with self.db.connect() as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(token_usage)")}
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key='schema_version'").fetchone()
        self.assertIn("on_platform", columns, "盖过章的库也必须能补上缺的列")
        self.assertEqual(int(row[0]), self.RECORDED)
