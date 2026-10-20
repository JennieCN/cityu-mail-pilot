"""Tests for noticing an invite e-mail that never went out.

The applicant is the only party who cannot see this failure: no code arrived, so
nothing happened on their side, so they will never write in to ask. Silence is the
entire symptom -- which is why it needs a sentinel rather than a careful reader.

The other half of the definition matters as much: "no `invite_sent_at`" is *not* a
failure. The operator may deliberately skip the e-mail and hand the code over
themselves, and that path (v0.27.0) leaves exactly the same empty timestamp. Only a
recorded error means something went wrong.
"""

import datetime as dt
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ["INFE_PILOT_DB"] = _TMP + "/invites.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import alerting, manage  # noqa: E402
from pilot_app.database import Database, utc_now  # noqa: E402
from pilot_app.web import db  # noqa: E402


class InviteSendFailedTests(unittest.TestCase):
    """The predicate, which both consumers read."""

    def row(self, **overrides):
        base = {"invite_label": "signup-abc", "invite_sent_at": utc_now(),
                "invite_send_error": ""}
        return {**base, **overrides}

    def test_a_delivered_invite_is_not_a_failure(self):
        self.assertFalse(Database.invite_send_failed(self.row()))

    def test_a_recorded_error_is_a_failure(self):
        self.assertTrue(Database.invite_send_failed(
            self.row(invite_sent_at="", invite_send_error="SMTP 550 被拒绝")))

    def test_skipping_the_email_is_not_a_failure(self):
        """The operator handing the code over by hand leaves no timestamp either."""
        self.assertFalse(Database.invite_send_failed(
            self.row(invite_sent_at="", invite_send_error="")))

    def test_a_pending_application_with_no_code_is_not_a_failure(self):
        self.assertFalse(Database.invite_send_failed(
            {"invite_label": "", "invite_sent_at": "", "invite_send_error": ""}))

    def test_an_error_without_a_code_is_not_a_failure(self):
        """Nothing was issued, so there is no code that failed to reach anyone."""
        self.assertFalse(Database.invite_send_failed(
            {"invite_label": "", "invite_sent_at": "", "invite_send_error": "未知"}))


class FailedInviteQueryTests(unittest.TestCase):
    """The query, against the real schema -- the join is the fiddly part."""

    def setUp(self):
        with db.connect() as connection:
            connection.execute("DELETE FROM signup_requests")
            connection.execute("DELETE FROM invites")
        self.counter = 0

    def add(self, email, *, label, sent_at="", error="", status="invited"):
        self.counter += 1
        with db.connect() as connection:
            connection.execute(
                """INSERT INTO signup_requests(id,email,note,status,invite_label,
                       invite_sent_at,invite_send_error,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (f"req_{self.counter}", email, "", status, label, sent_at, error, utc_now()))
            if label:
                connection.execute(
                    "INSERT INTO invites(code_hash,label,expires_at,used_at) VALUES(?,?,?,?)",
                    (f"h{self.counter}", label, utc_now(), None))
        return f"req_{self.counter}"

    def test_the_failed_one_is_found(self):
        self.add("a@example.com", label="signup-a", error="SMTP 550 被拒绝")
        rows = db.failed_invite_sends()
        self.assertEqual([row["email"] for row in rows], ["a@example.com"])

    def test_the_delivered_one_is_not_found(self):
        self.add("b@example.com", label="signup-b", sent_at=utc_now())
        self.assertEqual(db.failed_invite_sends(), [])

    def test_the_deliberately_skipped_one_is_not_found(self):
        self.add("c@example.com", label="signup-c")
        self.assertEqual(db.failed_invite_sends(), [])

    def test_the_console_command_and_the_sentinel_agree(self):
        """Two consumers, one definition -- asserted rather than assumed."""
        self.add("failed@example.com", label="signup-f", error="连接超时")
        self.add("fine@example.com", label="signup-g", sent_at=utc_now())
        self.add("skipped@example.com", label="signup-h")

        database = mock.MagicMock()
        database.get_setting.return_value = ""
        database.list_users_overview.return_value = []
        database.stalled_setups.return_value = []
        database.failed_invite_sends.side_effect = db.failed_invite_sends
        findings = alerting.evaluate(database, disk_percent=1.0, certificate_days=365,
                                     backup_dir=_TMP)
        alerted = {item["key"] for item in findings if item["key"].startswith("invite_failed")}
        console = {f"invite_failed:{row['id']}" for row in db.failed_invite_sends()}
        self.assertEqual(alerted, console)
        self.assertEqual(alerted, {"invite_failed:req_1"})

    def test_the_command_exits_nonzero_while_an_invite_is_stuck(self):
        self.add("stuck@example.com", label="signup-s", error="SMTP 550 被拒绝")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = manage.invitations(db)
        self.assertEqual(code, 1)
        self.assertIn("失败", buffer.getvalue())

    def test_the_command_exits_zero_when_everything_went_out(self):
        self.add("ok@example.com", label="signup-ok", sent_at=utc_now())
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = manage.invitations(db)
        self.assertEqual(code, 0, buffer.getvalue())

    def test_skipping_the_email_does_not_make_the_command_fail(self):
        """Otherwise a deliberate choice would look like an incident every morning."""
        self.add("handed-over@example.com", label="signup-hand")
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = manage.invitations(db)
        self.assertEqual(code, 0, buffer.getvalue())


class InviteAlertTests(unittest.TestCase):
    """The sentinel entry itself."""

    def setUp(self):
        with db.connect() as connection:
            connection.execute("DELETE FROM signup_requests")
            connection.execute("DELETE FROM invites")
        with db.connect() as connection:
            connection.execute(
                """INSERT INTO signup_requests(id,email,note,status,invite_label,
                       invite_sent_at,invite_send_error,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                ("req_alert", "waiting@example.com", "", "invited", "signup-alert",
                 "", "SMTP 550 User has no permission", utc_now()))
            connection.execute(
                "INSERT INTO invites(code_hash,label,expires_at) VALUES(?,?,?)",
                ("h-alert", "signup-alert", utc_now()))

    def findings(self):
        database = mock.MagicMock()
        database.get_setting.return_value = ""
        database.list_users_overview.return_value = []
        database.stalled_setups.return_value = []
        database.failed_invite_sends.side_effect = db.failed_invite_sends
        return alerting.evaluate(database, disk_percent=1.0, certificate_days=365,
                                 backup_dir=_TMP)

    def test_it_is_reported_with_the_provider_error(self):
        item = [f for f in self.findings() if f["key"].startswith("invite_failed")][0]
        self.assertIn("waiting@example.com", item["title"])
        self.assertIn("550", item["detail"])
        self.assertEqual(item["severity"], "warning")

    def test_the_detail_says_why_nobody_will_report_it(self):
        """The point of the alert is the thing the operator cannot infer."""
        item = [f for f in self.findings() if f["key"].startswith("invite_failed")][0]
        self.assertIn("什么都不会发生", item["detail"])
        self.assertIn("手动", item["detail"])

    def test_it_repeats_at_most_daily(self):
        self.assertEqual(alerting._repeat_for("invite_failed:req_1"),
                         alerting.ALERT_INVITE_REPEAT_SECONDS)
        self.assertGreaterEqual(alerting.ALERT_INVITE_REPEAT_SECONDS, 24 * 3600)

    def test_it_stops_once_the_invite_is_resent(self):
        with db.connect() as connection:
            connection.execute(
                "UPDATE signup_requests SET invite_sent_at=?, invite_send_error='' "
                "WHERE id='req_alert'", (utc_now(),))
        keys = {item["key"] for item in self.findings()}
        self.assertNotIn("invite_failed:req_alert", keys)


if __name__ == "__main__":
    unittest.main()
