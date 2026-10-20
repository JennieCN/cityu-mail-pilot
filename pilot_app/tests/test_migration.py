import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from pilot_app.database import Database
from pilot_app.migration import read_legacy_processed_uids
from pilot_app.security import hash_password, token_hash


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.db = Database(self.root / "pilot.sqlite3")
        self.db.initialize()
        expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO invites(code_hash,expires_at) VALUES(?,?)",
                (token_hash("migration-invite"), expires),
            )
        self.user = self.db.create_user(
            "owner@example.com", hash_password("long-enough-password"), token_hash("migration-invite")
        )
        self.mailbox_id = self.db.upsert_mailbox(
            self.user["id"],
            {
                "email": "owner@qq.com",
                "report_to": "owner@qq.com",
                "imap_host": "imap.qq.com",
                "imap_port": 993,
                "smtp_host": "smtp.qq.com",
                "smtp_port": 465,
                "encrypted_password": b"ciphertext-placeholder",
                "enabled": True,
            },
        )

    def tearDown(self):
        self.temporary.cleanup()

    def state(self, payload) -> Path:
        path = self.root / "imap-state.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_reader_preserves_sparse_membership_and_deduplicates(self):
        path = self.state({"processed_uids": ["imap:9", "imap:2", "imap:9", "imap:6"]})
        self.assertEqual(read_legacy_processed_uids(path), [2, 6, 9])

    def test_reader_rejects_unknown_or_empty_state(self):
        for payload in ({}, {"processed_uids": []}, {"processed_uids": ["graph:2"]}, []):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                read_legacy_processed_uids(self.state(payload))

    def test_import_requires_paused_user(self):
        with self.assertRaisesRegex(ValueError, "暂停"):
            self.db.seed_legacy_processed_uids(self.user["id"], "12345", [2, 6, 9], verification="server")

    def test_exact_import_is_idempotent_and_keeps_safe_rescan_cursor(self):
        self.db.set_user_status(self.user["id"], "paused")
        first = self.db.seed_legacy_processed_uids(self.user["id"], "12345", [9, 2, 6, 9], verification="server")
        second = self.db.seed_legacy_processed_uids(self.user["id"], "12345", [2, 6, 9], verification="server")
        self.assertEqual(first, {"seen": 3, "inserted": 3, "already_present": 0})
        self.assertEqual(second, {"seen": 3, "inserted": 0, "already_present": 3})
        mailbox = self.db.get_mailbox(self.user["id"])
        self.assertEqual(mailbox["uid_validity"], "12345")
        self.assertEqual(mailbox["last_uid"], 0)
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT imap_uid,status FROM messages WHERE mailbox_id=? ORDER BY imap_uid",
                (self.mailbox_id,),
            ).fetchall()
        self.assertEqual([(row["imap_uid"], row["status"]) for row in rows], [(2, "sent"), (6, "sent"), (9, "sent")])

    def test_import_rejects_uidvalidity_change(self):
        self.db.set_user_status(self.user["id"], "paused")
        self.db.seed_legacy_processed_uids(self.user["id"], "12345", [2], verification="server")
        with self.assertRaisesRegex(ValueError, "不一致"):
            self.db.seed_legacy_processed_uids(self.user["id"], "54321", [3], verification="server")


if __name__ == "__main__":
    unittest.main()
