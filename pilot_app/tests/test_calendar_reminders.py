"""P3 durable reminder and recovery-gate tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from pilot_app import calendar_events
from pilot_app.database import Database
from pilot_app.security import SecretBox
from pilot_app import worker
from pilot_app.calendar_reminders import build_source_fields, due_for_rule


class CalendarReminderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "calendar.sqlite3")
        self.db.initialize()
        with self.db.connect() as c:
            c.execute("INSERT INTO users(id,email,password_hash,created_at) VALUES(?,?,?,?)",
                      ("u", "u@example.test", "test", "2026-01-01T00:00:00+00:00"))
            c.execute("""INSERT INTO calendar_events(id,user_id,encrypted_payload,local_date,local_time,timezone,all_day,starts_at_utc,fold,state,revision,schedule_revision,created_at,updated_at,deleted_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", ("e", "u", b"x", "2026-10-20", "12:00", "UTC", 0,
                    "2026-10-20T12:00:00Z", 0, "open", 1, 1, "2026-01-01", "2026-01-01", None))

    def tearDown(self):
        self.tmp.cleanup()

    def _open_gate(self):
        self.db.require_calendar_recovery("p3_release")
        self.db.begin_calendar_recovery()
        self.db.calendar_recovery_batch()
        self.db.calendar_recovery_batch()
        self.assertTrue(self.db.complete_calendar_recovery())

    def test_new_schema_starts_closed_and_opening_is_persistent(self):
        self.assertEqual(self.db.calendar_recovery_state()["status"], "required")
        self._open_gate()
        self.assertEqual(self.db.calendar_recovery_state()["status"], "complete")
        self.assertEqual(self.db.advance_due_calendar_reminders()["gate_closed"], False)

    def test_due_transition_is_single_and_expiry_wins(self):
        self._open_gate()
        self.db.upsert_calendar_reminder(user_id="u", source_kind="manual", source_id="e",
            source_schedule_revision=1, rule_kind="minutes_before", rule_value=0,
            due_at_utc="2026-10-20T11:00:00Z", expires_at="2026-10-20T11:15:00Z")
        self.assertEqual(self.db.advance_due_calendar_reminders("2026-10-20T11:05:00Z")["ready"], 1)
        self.assertEqual(self.db.advance_due_calendar_reminders("2026-10-20T11:05:00Z")["ready"], 0)
        self.assertEqual(self.db.advance_due_calendar_reminders("2026-10-20T11:15:00Z")["expired"], 1)

    def test_manual_done_cancels_reminder_in_same_transaction(self):
        self._open_gate()
        self.db.upsert_calendar_reminder(user_id="u", source_kind="manual", source_id="e",
            source_schedule_revision=1, rule_kind="minutes_before", rule_value=0,
            due_at_utc="2026-10-20T11:00:00Z", expires_at="2026-10-20T11:15:00Z")
        row = self.db.calendar_event("u", "e")
        values = calendar_events.validate({"title": "T", "notes": "", "date": "2026-10-20", "time": "12:00",
            "timezone": "UTC", "all_day": False, "fold": 0, "state": "done"})
        self.db.update_calendar_event("u", "e", row["revision"], values, SecretBox(bytes(range(32))))
        self.assertEqual(self.db.calendar_reminders_for_user("u")[0]["status"], "cancelled_done")

    def test_report_outbox_is_atomic_and_claim_fence_rejects_expired_claim(self):
        rid = self.db.create_report(user_id="u", message_id=None, kind="immediate", subject="s", body="b", sent_to="u@example.test")
        claim = self.db.claim_calendar_reconcile(lease_seconds=1)
        self.assertIsNotNone(claim)
        self.assertFalse(self.db.finish_calendar_reconcile(claim, {"tasks": []}, now="2999-01-01T00:00:00Z"))

    def test_generation_supersede_does_not_apply_old_plan(self):
        rid = self.db.create_report(user_id="u", message_id=None, kind="immediate", subject="s", body="b", sent_to="u@example.test")
        claim = self.db.claim_calendar_reconcile()
        self.db.enqueue_calendar_reconcile(rid, "u", "invalidate")
        self.assertFalse(self.db.finish_calendar_reconcile(claim, {"tasks": []}))
        with self.db.connect() as c:
            row = c.execute("SELECT status,requested_generation,attempts FROM calendar_reconcile_outbox WHERE report_id=?", (rid,)).fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["requested_generation"], 2)
        self.assertEqual(row["attempts"], 0)

    def test_default_enabled_report_creates_rule_derived_reminder(self):
        with self.db.connect() as c:
            c.execute("INSERT INTO calendar_preferences(user_id,mail_default_enabled,timed_offset_minutes,enabled_at,created_at,updated_at) VALUES('u',1,60,'2000-01-01','test','test')")
        rid = self.db.create_report(user_id="u", message_id=None, kind="immediate", subject="s", body="b", sent_to="u@example.test")
        claim = self.db.claim_calendar_reconcile()
        task = {"task_key": "default", "parser_version": 1, "source_fingerprint": "fp",
                "explicit_deadline": 1, "encrypted_schedule": b"x", "starts_at_utc": "2099-10-20T12:00:00Z",
                "local_date": "2099-10-20", "local_time": "12:00", "timezone": "UTC", "all_day": 0}
        self.assertTrue(self.db.finish_calendar_reconcile(claim, {"tasks": [task]}))
        row = self.db.calendar_reminders_for_user("u")[0]
        self.assertEqual(row["rule_kind"], "minutes_before")
        self.assertEqual(row["due_at_utc"], "2099-10-20T11:00:00Z")

    def test_recovery_does_not_skip_scan_or_task_done(self):
        rid = self.db.create_report(user_id="u", message_id=None, kind="immediate", subject="s", body="b", sent_to="u@example.test")
        claim = self.db.claim_calendar_reconcile()
        task = {"task_key": "done-task", "parser_version": 1, "source_fingerprint": "fp",
                "explicit_deadline": 1, "encrypted_schedule": b"x", "starts_at_utc": "2099-10-20T12:00:00Z",
                "local_date": "2099-10-20", "local_time": "12:00", "timezone": "UTC", "all_day": 0}
        self.assertTrue(self.db.finish_calendar_reconcile(claim, {"tasks": [task]}))
        with self.db.connect() as c:
            source_id = c.execute("SELECT id FROM calendar_mail_sources WHERE report_id=?", (rid,)).fetchone()[0]
            c.execute("INSERT INTO task_states(user_id,task_key,state,updated_at) VALUES('u','done-task','done','test')")
        self.db.upsert_calendar_reminder(user_id="u", source_kind="mail", source_id=source_id,
            source_schedule_revision=1, rule_kind="minutes_before", rule_value=0,
            due_at_utc="2099-10-20T11:00:00Z", expires_at="2099-10-20T11:15:00Z")
        self.db.require_calendar_recovery("p2_reupgrade")
        self.assertFalse(self.db.complete_calendar_recovery())
        self.db.calendar_recovery_batch("2099-10-20T10:00:00Z")
        self.db.calendar_recovery_batch("2099-10-20T10:00:00Z")
        self.db.complete_calendar_recovery()
        self.assertEqual(self.db.advance_due_calendar_reminders("2099-10-20T11:05:00Z")["ready"], 0)

    def test_mixed_utc_expiry_is_expired_at_same_instant(self):
        self._open_gate()
        self.db.upsert_calendar_reminder(user_id="u", source_kind="manual", source_id="e",
            source_schedule_revision=1, rule_kind="minutes_before", rule_value=0,
            due_at_utc="2099-10-20T11:00:00Z", expires_at="2099-10-20T11:15:00Z")
        result = self.db.advance_due_calendar_reminders("2099-10-20T11:15:00+00:00")
        self.assertEqual(result["ready"], 0)
        self.assertEqual(result["expired"], 1)

    def test_permanent_failure_dead_and_blocks_old_source(self):
        rid = self.db.create_report(user_id="u", message_id=None, kind="immediate", subject="s", body="b", sent_to="u@example.test")
        claim = self.db.claim_calendar_reconcile()
        self.assertTrue(self.db.fail_calendar_reconcile(claim, "SecurityError", permanent=True))
        with self.db.connect() as c:
            row = c.execute("SELECT status,last_error FROM calendar_reconcile_outbox WHERE report_id=?", (rid,)).fetchone()
        self.assertEqual(row["status"], "dead")
        self.assertEqual(row["last_error"], "SecurityError")

    def test_all_day_rule_and_dst_gap_are_validated(self):
        due, expires = due_for_rule({"all_day": 1, "local_date": "2099-10-20", "timezone": "UTC"},
                                    rule_kind="day_at", rule_value=540, now="2099-10-19T00:00:00Z")
        self.assertEqual(due, "2099-10-20T09:00:00Z")
        with self.assertRaises(ValueError):
            due_for_rule({"all_day": 1, "local_date": "2026-03-08", "timezone": "America/New_York"},
                         rule_kind="day_at", rule_value=150, now="2026-03-01T00:00:00Z")

    def test_needs_confirmation_survives_recovery(self):
        self._open_gate()
        self.db.upsert_calendar_reminder(user_id="u", source_kind="manual", source_id="e",
            source_schedule_revision=1, rule_kind="minutes_before", rule_value=0,
            due_at_utc="2099-10-20T11:00:00Z", expires_at="2099-10-20T11:15:00Z",
            status="needs_confirmation")
        with self.db.connect() as c:
            c.execute("INSERT INTO task_states(user_id,task_key,state,updated_at) VALUES('u','x','open','test')")
        self.db.require_calendar_recovery("p2_reupgrade")
        self.db.calendar_recovery_batch("2099-10-19T00:00:00Z")
        self.db.calendar_recovery_batch("2099-10-19T00:00:00Z")
        self.assertEqual(self.db.calendar_reminders_for_user("u")[0]["status"], "needs_confirmation")

    def test_manual_timing_confirmation_survives_done_reopen(self):
        self._open_gate()
        self.db.upsert_calendar_reminder(user_id="u", source_kind="manual", source_id="e",
            source_schedule_revision=1, rule_kind="minutes_before", rule_value=0,
            due_at_utc="2099-10-20T11:00:00Z", expires_at="2099-10-20T11:15:00Z")
        row = self.db.calendar_event("u", "e")
        moved = calendar_events.validate({"title": "T", "notes": "", "date": "2099-10-21", "time": "12:00",
            "timezone": "UTC", "all_day": False, "fold": 0, "state": "open"})
        self.db.update_calendar_event("u", "e", row["revision"], moved, SecretBox(bytes(range(32))))
        row = self.db.calendar_event("u", "e")
        done = dict(moved, state="done")
        self.db.update_calendar_event("u", "e", row["revision"], calendar_events.validate(done), SecretBox(bytes(range(32))))
        row = self.db.calendar_event("u", "e")
        reopened = self.db.update_calendar_event("u", "e", row["revision"], moved, SecretBox(bytes(range(32)))
        )
        self.assertEqual(self.db.calendar_reminders_for_user("u")[0]["status"], "needs_confirmation")

    def test_mail_timing_confirmation_survives_done_reopen(self):
        rid = self.db.create_report(user_id="u", message_id=None, kind="immediate", subject="s", body="b", sent_to="u@example.test")
        claim = self.db.claim_calendar_reconcile()
        task = {"task_key": "mail-confirm", "parser_version": 1, "source_fingerprint": "fp1",
                "explicit_deadline": 1, "encrypted_schedule": b"x", "starts_at_utc": "2099-10-20T12:00:00Z",
                "local_date": "2099-10-20", "local_time": "12:00", "timezone": "UTC", "all_day": 0}
        self.assertTrue(self.db.finish_calendar_reconcile(claim, {"tasks": [task]}))
        with self.db.connect() as c:
            source_id = c.execute("SELECT id FROM calendar_mail_sources WHERE report_id=?", (rid,)).fetchone()[0]
        self.db.upsert_calendar_reminder(user_id="u", source_kind="mail", source_id=source_id,
            source_schedule_revision=1, rule_kind="minutes_before", rule_value=0,
            due_at_utc="2099-10-20T11:00:00Z", expires_at="2099-10-20T11:15:00Z")
        changed = dict(task, source_fingerprint="fp2")
        self.db.enqueue_calendar_reconcile(rid, "u", "deadline_changed")
        claim = self.db.claim_calendar_reconcile()
        self.assertTrue(self.db.finish_calendar_reconcile(claim, {"tasks": [changed]}))
        self.assertEqual(self.db.calendar_reminders_for_user("u")[0]["status"], "needs_confirmation")
        self.db.set_task_state("u", "mail-confirm", "done", {"task_day": "2099-10-20"})
        self.db.set_task_state("u", "mail-confirm", "open", {"task_day": "2099-10-20"})
        self.assertEqual(self.db.calendar_reminders_for_user("u")[0]["status"], "needs_confirmation")

    def test_reopen_and_recovery_due_respects_confirmation(self):
        secret = SecretBox(bytes(range(32)))
        now = "2099-10-19T00:00:00Z"
        for kind in ("manual", "mail"):
            for changed in (False, True):
                with self.subTest(kind=kind, changed=changed):
                    db = Database(Path(self.tmp.name) / f"{kind}-{changed}.sqlite3")
                    db.initialize()
                    with db.connect() as c:
                        c.execute("INSERT INTO users(id,email,password_hash,created_at) VALUES('u','u@example.test','test',?)", (now,))
                    values = calendar_events.validate({"title": "T", "notes": "",
                        "date": "2099-10-20", "time": "12:00", "timezone": "UTC",
                        "all_day": False, "fold": 0, "state": "open"})
                    task = {"task_key": "t", "parser_version": 1,
                        "source_fingerprint": "fp1", "explicit_deadline": 1,
                        "encrypted_schedule": b"x", "starts_at_utc": "2099-10-20T12:00:00Z",
                        "local_date": "2099-10-20", "local_time": "12:00", "timezone": "UTC", "all_day": 0}
                    if kind == "manual":
                        event = db.create_calendar_event("u", "create", "digest", values, secret)
                        source_id = event["id"]
                    else:
                        report_id = db.create_report(user_id="u", message_id=None, kind="immediate",
                            subject="s", body="b", sent_to="u@example.test")
                        claim = db.claim_calendar_reconcile(now=now)
                        # Reports are created at the real clock, before this synthetic future.
                        self.assertTrue(db.finish_calendar_reconcile(claim, {"tasks": [task]}, now=now))
                        with db.connect() as c:
                            source_id = c.execute("SELECT id FROM calendar_mail_sources").fetchone()[0]
                    db.upsert_calendar_reminder(user_id="u", source_kind=kind, source_id=source_id,
                        source_schedule_revision=1, rule_kind="minutes_before", rule_value=60,
                        due_at_utc="2099-10-20T11:00:00Z", expires_at="2099-10-20T11:15:00Z")
                    if changed:
                        values = calendar_events.validate(dict(values, date="2099-10-21"))
                        task = dict(task, source_fingerprint="fp2", local_date="2099-10-21",
                                    starts_at_utc="2099-10-21T12:00:00Z")
                        if kind == "manual":
                            db.update_calendar_event("u", source_id, event["revision"], values, secret)
                        else:
                            db.enqueue_calendar_reconcile(report_id, "u", "deadline_changed")
                            claim = db.claim_calendar_reconcile(now=now)
                            self.assertTrue(db.finish_calendar_reconcile(claim, {"tasks": [task]}, now=now))
                        self.assertEqual(db.calendar_reminders_for_user("u")[0]["status"], "needs_confirmation")
                    for state in ("done", "open"):
                        if kind == "manual":
                            event = db.calendar_event("u", source_id)
                            db.update_calendar_event("u", source_id, event["revision"], dict(values, state=state), secret)
                        else:
                            db.set_task_state("u", "t", state, {"task_day": task["local_date"]})
                    db.set_user_status("u", "paused")
                    db.require_calendar_recovery("p2_reupgrade")
                    db.calendar_recovery_batch(now)
                    if kind == "mail":
                        db.enqueue_calendar_reconcile(report_id, "u", "paused_exact_rebuild")
                        claim = db.claim_calendar_reconcile(now=now)
                        self.assertTrue(db.finish_calendar_reconcile(claim, {"tasks": [task]}, now=now))
                    self.assertEqual(db.calendar_reminders_for_user("u")[0]["confirmation_required"], int(changed))
                    db.set_user_status("u", "active")
                    # Recovery drains an exact mail reconcile before the gate can open.
                    db.require_calendar_recovery("p2_reupgrade")
                    for _ in range(12):
                        db.calendar_recovery_batch(now)
                        claim = db.claim_calendar_reconcile(now=now)
                        if claim:
                            self.assertTrue(db.finish_calendar_reconcile(claim, {"tasks": [task]}, now=now))
                        if db.complete_calendar_recovery(now):
                            break
                    self.assertEqual(db.calendar_recovery_state()["status"], "complete")
                    reminder = db.calendar_reminders_for_user("u")[0]
                    self.assertEqual(reminder["confirmation_required"], int(changed))
                    self.assertEqual(reminder["status"], "needs_confirmation" if changed else "pending")
                    due_now = "2099-10-21T11:05:00Z" if changed else "2099-10-20T11:05:00Z"
                    result = db.advance_due_calendar_reminders(due_now)
                    self.assertFalse(result["gate_closed"])
                    self.assertEqual(result["ready"], 0 if changed else 1)

    def test_mail_wall_time_preserves_selected_dst_fold(self):
        base = {"task_key": "fold", "task_day": "2026-10-20",
                "deadline": "2026-11-01 01:30", "action": "T"}
        results = [build_source_fields(SecretBox(bytes(range(32))), "u", "r",
            dict(base, fold=fold), timezone="America/New_York", fallback_day="2026-10-20")
            for fold in (0, 1)]
        self.assertEqual([row["starts_at_utc"] for row in results],
                         ["2026-11-01T05:30:00Z", "2026-11-01T06:30:00Z"])

    def test_index_replace_enqueues_and_open_gate_runs_normal_outbox(self):
        rid = self.db.create_report(user_id="u", message_id=None, kind="immediate", subject="s", body="b", sent_to="u@example.test")
        self.db.replace_calendar_report_index(rid, "u", timezone="UTC", parser_version=1,
            key_fingerprint="k", status="indexed", tasks=[])
        with self.db.connect() as c:
            self.assertEqual(c.execute("SELECT requested_generation FROM calendar_reconcile_outbox WHERE report_id=?", (rid,)).fetchone()[0], 2)
        claim = self.db.claim_calendar_reconcile()
        self.db.finish_calendar_reconcile(claim, {"tasks": []})
        self._open_gate()
        service = Mock()
        service.db = self.db
        service.reconcile_calendar_once.return_value = {"done": 1, "failed": 0}
        result = worker.process_calendar_reminders(service)
        self.assertEqual(service.reconcile_calendar_once.call_count, 1)
        self.assertEqual(result["gate_opened"], 1)


if __name__ == "__main__":
    unittest.main()
