"""Tests for the restore drill.

The claim being tested is the one that matters on the day it matters: a backup
is only a backup if it can be read back. Structure is not enough — a copy taken
under a master key that no longer exists passes ``PRAGMA integrity_check`` and
is still worthless, which is exactly the failure a "we have backups" check
cannot see.
"""

from __future__ import annotations

import contextlib
import io
import pathlib
import tempfile
import unittest
from unittest import mock

from pilot_app import backup as backup_mod
from pilot_app import manage
from pilot_app.database import Database
from pilot_app.security import SecretBox, hash_password, token_hash

KEY_ONE = "A" * 43 + "="
KEY_TWO = "B" * 43 + "="


class RestoreDrillTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.root = pathlib.Path(self.work.name)
        self.db_path = self.root / "pilot.sqlite3"
        self.backup_dir = self.root / "backups"
        self.backup_dir.mkdir()
        self.env = {
            "INFE_PILOT_DB": str(self.db_path),
            "INFE_PILOT_BACKUP_DIR": str(self.backup_dir),
            "INFE_PILOT_MASTER_KEY": KEY_ONE,
        }
        self._patch = mock.patch.dict("os.environ", self.env, clear=False)
        self._patch.start()
        self.addCleanup(self._patch.stop)

        self.db = Database(self.db_path)
        self.db.initialize()
        invite = self.db.create_invite("u1", 1)
        user = self.db.create_user("u@example.com", hash_password("a-long-enough-password"),
                                   token_hash(invite))
        self.user_id = user["id"]
        box = SecretBox.from_environment()
        self.db.upsert_mailbox(user["id"], {
            "email": "box@qq.com", "report_to": "u@example.com",
            "imap_host": "imap.qq.com", "imap_port": 993,
            "smtp_host": "smtp.qq.com", "smtp_port": 465,
            "encrypted_password": box.encrypt("the-real-app-password",
                                              context=f"mailbox:{user['id']}"),
        })

    def _run(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = manage.restore_drill(self.db, *args)
        return code, out.getvalue()

    def _backup(self) -> pathlib.Path:
        # `[]` rather than nothing: `main` now parses arguments, and with None it
        # would read the *test runner's* argv and exit(2) on an unknown flag.
        backup_mod.main([])
        return sorted(self.backup_dir.glob("pilot-*.sqlite3"))[-1]

    def test_a_good_backup_passes_and_reports_its_counts(self):
        self._backup()
        code, text = self._run()
        self.assertEqual(code, 0, text)
        self.assertIn("完整性检查：ok", text)
        self.assertIn("成功解密 1 个邮箱授权码", text)

    def test_a_backup_from_another_master_key_is_refused(self):
        """The whole reason this drill exists: structure is not enough."""
        self._backup()
        with mock.patch.dict("os.environ", {"INFE_PILOT_MASTER_KEY": KEY_TWO}):
            code, text = self._run()
        self.assertEqual(code, 1, text)
        # The structural verdict still passes — that is the point.
        self.assertIn("完整性检查：ok", text)
        self.assertIn("解不开", text)

    def test_it_never_prints_the_credential(self):
        self._backup()
        _, text = self._run()
        self.assertNotIn("the-real-app-password", text)
        self.assertNotIn(KEY_ONE, text)

    def test_a_missing_backup_is_reported_rather_than_crashing(self):
        code, text = self._run()
        self.assertEqual(code, 1, text)
        self.assertIn("找不到", text)

    def test_an_explicit_path_is_honoured(self):
        path = self._backup()
        other = self.backup_dir / "pilot-19990101T000000Z.sqlite3"
        other.write_bytes(path.read_bytes())
        code, text = self._run(str(other))
        self.assertEqual(code, 0, text)
        self.assertIn(other.name, text)

    def test_a_corrupt_file_fails_integrity(self):
        path = self._backup()
        broken = self.backup_dir / "pilot-20200101T000000Z.sqlite3"
        broken.write_bytes(path.read_bytes()[:2048] + b"\x00" * 512)
        code, text = self._run(str(broken))
        self.assertEqual(code, 1, text)

    def test_production_data_is_not_touched(self):
        """The drill must be safe to run against a live install."""
        before = self.db_path.read_bytes()
        self._backup()
        self._run()
        self.assertEqual(self.db_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
