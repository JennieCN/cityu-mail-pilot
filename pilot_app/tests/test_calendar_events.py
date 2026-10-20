"""Tests for manual calendar facts, time-zone rules and additive storage."""
import datetime as dt
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pilot_app import calendar_events
from pilot_app.database import Database
from pilot_app.security import SecretBox


class CalendarEventFactsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temporary.name) / "calendar.sqlite3")
        self.db.initialize()
        self.secrets = SecretBox(bytes(range(32)))
        self.user_id = self._user("one")
        self.other_id = self._user("two")

    def tearDown(self):
        self.temporary.cleanup()

    def _user(self, suffix):
        user_id = "usr_calendar_" + suffix
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO users(id,email,password_hash,created_at) VALUES(?,?,?,?)",
                (user_id, user_id + "@example.test", "test-only", "2026-01-01T00:00:00+00:00"),
            )
        return user_id

    def _values(self, **changes):
        values = {
            "title": "Submit assignment", "notes": "Course portal", "date": "2026-03-01",
            "time": "00:30", "timezone": "Asia/Tokyo", "all_day": False,
            "fold": None, "state": "open",
        }
        values.update(changes)
        return calendar_events.validate(values)

    def _create(self, user_id=None, key="create-one", values=None):
        return self.db.create_calendar_event(
            user_id or self.user_id, key, "digest-" + key,
            values or self._values(), self.secrets,
        )

    def test_dst_gap_requires_valid_wall_time_and_fold_is_explicit(self):
        base = {"title": "DST", "notes": "", "timezone": "America/New_York",
                "all_day": False, "state": "open"}
        with self.assertRaises(calendar_events.InvalidEvent):
            calendar_events.validate({**base, "date": "2026-03-08", "time": "02:30"})
        with self.assertRaises(calendar_events.InvalidEvent):
            calendar_events.validate({**base, "date": "2026-11-01", "time": "01:30"})
        first = calendar_events.validate({**base, "date": "2026-11-01", "time": "01:30", "fold": 0})
        second = calendar_events.validate({**base, "date": "2026-11-01", "time": "01:30", "fold": 1})
        delta = dt.datetime.fromisoformat(second["starts_at_utc"]) - dt.datetime.fromisoformat(first["starts_at_utc"])
        self.assertEqual(delta, dt.timedelta(hours=1))

    def test_midnight_all_day_and_schedule_revision_rules(self):
        midnight = self._values(time="00:00")
        self.assertEqual(midnight["starts_at_utc"], "2026-02-28T15:00:00+00:00")
        all_day = self._values(all_day=True, time="", fold=None)
        self.assertIsNone(all_day["starts_at_utc"])
        with self.assertRaises(calendar_events.InvalidEvent):
            self._values(all_day=True)

        row = self._create(values=midnight)
        original_schedule = row["schedule_revision"]
        values = calendar_events.validate({**calendar_events.present(self.secrets, row), "notes": "updated"})
        changed = self.db.update_calendar_event(self.user_id, row["id"], row["revision"], values, self.secrets)
        self.assertEqual(changed["revision"], row["revision"] + 1)
        self.assertEqual(changed["schedule_revision"], original_schedule)

    def test_create_replay_is_idempotent_and_delete_never_restores_text(self):
        values = self._values()
        row = self._create(key="stable-key", values=values)
        replay = self._create(key="stable-key", values=values)
        self.assertEqual(replay["id"], row["id"])
        self.assertNotIn(b"Submit assignment", row["encrypted_payload"])
        with self.assertRaises(calendar_events.EventConflict):
            self.db.create_calendar_event(self.user_id, "stable-key", "different", values, self.secrets)

        deleted = self.db.update_calendar_event(self.user_id, row["id"], row["revision"], None, self.secrets)
        self.assertEqual(deleted["encrypted_payload"], b"")
        self.assertEqual(deleted["local_date"], "")
        self.assertEqual(deleted["local_time"], "")
        self.assertEqual(deleted["timezone"], "")
        self.assertEqual(deleted["all_day"], 0)
        self.assertIsNone(deleted["starts_at_utc"])
        self.assertIsNone(deleted["fold"])
        self.assertEqual(deleted["created_at"], "")
        self.assertNotIn("title", calendar_events.present(self.secrets, deleted))
        repeated = self.db.update_calendar_event(
            self.user_id, row["id"], row["revision"], None, self.secrets)
        self.assertEqual(repeated["id"], row["id"])
        self.assertEqual(repeated["revision"], deleted["revision"])
        self.assertEqual(repeated["encrypted_payload"], b"")
        replay = self._create(key="stable-key", values=values)
        self.assertEqual(replay["state"], "deleted")
        self.assertEqual(replay["encrypted_payload"], b"")

    def test_expired_keys_for_idle_accounts_are_swept_globally_and_on_restart(self):
        owner = self._create(key="idle-owner")
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE calendar_event_requests SET created_at='2000-01-01T00:00:00+00:00'"
                " WHERE user_id=? AND create_key='idle-owner'", (self.user_id,))
        self._create(user_id=self.other_id, key="other-account")
        with self.db.connect() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM calendar_event_requests"
                " WHERE user_id=? AND create_key='idle-owner'", (self.user_id,)
            ).fetchone()[0], 0)
            connection.execute(
                "UPDATE calendar_event_requests SET created_at='2000-01-01T00:00:00+00:00'"
                " WHERE user_id=? AND create_key='other-account'", (self.other_id,))

        self.db.initialize()
        self.db.initialize()
        with self.db.connect() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM calendar_event_requests WHERE created_at<'2001-01-01'"
            ).fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM calendar_events WHERE id=?", (owner["id"],)
            ).fetchone()[0], 1)

    def test_two_connections_racing_the_same_create_key_create_one_event(self):
        barrier = threading.Barrier(2)
        values = self._values()

        def create():
            barrier.wait()
            return self.db.create_calendar_event(
                self.user_id, "racing-key", "same-digest", values, self.secrets,
            )

        with ThreadPoolExecutor(max_workers=2) as workers:
            first, second = list(workers.map(lambda _index: create(), range(2)))
        self.assertEqual(first["id"], second["id"])
        with self.db.connect() as connection:
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM calendar_events WHERE user_id=?", (self.user_id,)
            ).fetchone()[0], 1)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM calendar_event_requests WHERE user_id=?", (self.user_id,)
            ).fetchone()[0], 1)

    def test_ownership_revision_and_completed_management(self):
        row = self._create()
        self.assertIsNone(self.db.calendar_event(self.other_id, row["id"]))
        values = calendar_events.validate({**calendar_events.present(self.secrets, row), "state": "done"})
        done = self.db.update_calendar_event(self.user_id, row["id"], row["revision"], values, self.secrets)
        self.assertEqual(done["state"], "done")
        with self.assertRaises(calendar_events.EventConflict):
            self.db.update_calendar_event(self.user_id, row["id"], row["revision"], values, self.secrets)
        visible = self.db.calendar_event(self.user_id, row["id"])
        reopened_values = calendar_events.validate({**calendar_events.present(self.secrets, visible), "state": "open"})
        reopened = self.db.update_calendar_event(self.user_id, row["id"], done["revision"], reopened_values, self.secrets)
        self.assertEqual(reopened["state"], "open")

    def test_timed_query_uses_display_zone_and_all_day_keeps_event_date(self):
        self._create(key="timed", values=self._values())
        self._create(key="all-day", values=self._values(
            title="Holiday", time="", all_day=True, fold=None,
        ))
        feb_start, feb_end = calendar_events.month_bounds(2026, 2, "America/New_York")
        feb = self.db.calendar_events_for_month(self.user_id, "2026-02", feb_start, feb_end)
        self.assertEqual([row["local_date"] for row in feb], ["2026-03-01"])
        feb_data = {"month": "2026-02", "cells": [], "counts": {"open": 0, "done": 0}}
        merged_feb = calendar_events.merge_month(self.db, self.secrets, self.user_id, "America/New_York", feb_data)
        self.assertEqual(merged_feb["cells"][0]["day"], "2026-02-28")

        mar_start, mar_end = calendar_events.month_bounds(2026, 3, "America/New_York")
        mar = self.db.calendar_events_for_month(self.user_id, "2026-03", mar_start, mar_end)
        self.assertEqual([(row["local_date"], row["all_day"]) for row in mar], [("2026-03-01", 1)])

    def test_migration_is_additive_idempotent_and_rolls_back_with_marker_failure(self):
        event = self._create()
        self.db.initialize()
        self.db.migrate()
        self.assertEqual(self.db.calendar_event(self.user_id, event["id"])["id"], event["id"])
        self.assertEqual(self.db.get_setting("calendar_schema_version"), "3")

        path = Path(self.temporary.name) / "migration-fails.sqlite3"
        failed = Database(path)
        failed.migrate()
        with failed.connect() as connection:
            connection.execute("""CREATE TRIGGER block_calendar_marker BEFORE INSERT ON app_settings
                WHEN NEW.key='calendar_schema_version' BEGIN SELECT RAISE(ABORT,'forced'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            failed._migrate_calendar_events()
        with failed.connect() as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("calendar_events", tables)
        self.assertNotIn("calendar_event_requests", tables)

    def test_account_deletion_cascades_events_and_create_keys(self):
        self._create(key="delete-account")
        with self.db.connect() as connection:
            connection.execute("DELETE FROM users WHERE id=?", (self.user_id,))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM calendar_events WHERE user_id=?", (self.user_id,)).fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM calendar_event_requests WHERE user_id=?", (self.user_id,)).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
