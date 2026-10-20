"""Tests for "registered but never finished" — the silent failure mode.

Three of the first six pilot accounts stalled between registering and having a
working mailbox. Nothing about that is visible from inside the product: no mail
arrives, nothing fails, nothing queues. The account simply produces no reports,
and the only way to notice was for the operator to open the console and read
every row.

Two consumers share one definition here — the admin console and the sentinel —
because two places disagreeing about who is "unfinished" would be worse than
either one being wrong.
"""

import datetime as dt
import os
import tempfile
import unittest

_TMP = tempfile.mkdtemp()
os.environ["INFE_PILOT_DB"] = _TMP + "/stalled.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import alerting  # noqa: E402
from pilot_app.database import Database, utc_now  # noqa: E402
from pilot_app.security import token_hash  # noqa: E402
from pilot_app.web import db  # noqa: E402


def hours_ago(hours: float) -> str:
    moment = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    return moment.isoformat(timespec="seconds")


class SetupGapTests(unittest.TestCase):
    """The definition, which is the part the other two places depend on."""

    def row(self, **overrides):
        base = {"status": "active", "mailbox_email": "someone@example.com",
                "mailbox_enabled": 1, "last_verified_at": utc_now(), "last_polled_at": None}
        return {**base, **overrides}

    def test_a_finished_account_has_no_gap(self):
        self.assertEqual(Database.setup_gap(self.row()), "")

    def test_a_verified_mailbox_is_finished_even_without_a_poll(self):
        self.assertEqual(Database.setup_gap(self.row(last_polled_at=None)), "")

    def test_a_poll_that_worked_counts_even_without_a_manual_check(self):
        """The direct check is optional; the poller reaching the mailbox is proof."""
        self.assertEqual(
            Database.setup_gap(self.row(last_verified_at=None, last_polled_at=utc_now())), "")

    def test_no_mailbox_is_the_first_gap(self):
        self.assertEqual(
            Database.setup_gap(self.row(mailbox_email=None, mailbox_enabled=None)), "no_mailbox")

    def test_a_disabled_mailbox_is_the_same_gap_as_none(self):
        """A mailbox switched off delivers nothing, which is the same outcome."""
        self.assertEqual(Database.setup_gap(self.row(mailbox_enabled=0)), "no_mailbox")

    def test_a_mailbox_never_reached_reports_its_own_gap(self):
        self.assertEqual(
            Database.setup_gap(self.row(last_verified_at=None, last_polled_at=None)), "unreachable")

    def test_a_deleted_account_is_not_counted(self):
        self.assertEqual(Database.setup_gap(self.row(status="deleted")), "")

    def test_a_missing_key_is_not_a_gap(self):
        """The instance pays for the keys now, so a personal key is optional.

        Counting it here would have reported three working accounts as stuck on
        the day the platform credentials were configured.
        """
        row = self.row()
        row.pop("model_provider", None)
        self.assertEqual(Database.setup_gap(row), "")

    def test_every_gap_has_a_label(self):
        """The e-mail and the console both print this; a missing one shows a code."""
        for gap in ("no_mailbox", "unreachable"):
            self.assertIn(gap, Database.SETUP_GAP_LABELS)
            self.assertTrue(Database.SETUP_GAP_LABELS[gap])


class StalledSetupTests(unittest.TestCase):
    """Who gets listed, and who must not."""

    def setUp(self):
        self.db = db
        with self.db.connect() as connection:
            connection.execute("DELETE FROM mailboxes")
            connection.execute("DELETE FROM users")

    def account(self, email, *, age_hours, gap="none", status="active"):
        user_id = f"usr_{email.split('@')[0]}"
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO users(id,email,password_hash,status,created_at) VALUES(?,?,?,?,?)",
                (user_id, email, "x", status, hours_ago(age_hours)))
            if gap != "none":
                connection.execute(
                    """INSERT INTO mailboxes(id,user_id,email,report_to,imap_host,imap_port,
                           smtp_host,smtp_port,encrypted_password,enabled,last_verified_at,
                           last_polled_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (f"mbx_{user_id}", user_id, "private@example.com",
                     "private@example.com", "imap.example.com", 993,
                     "smtp.example.com", 465, b"cipher", 1 if gap == "unreachable" else 0,
                     None, None, utc_now()))
            else:
                connection.execute(
                    """INSERT INTO mailboxes(id,user_id,email,report_to,imap_host,imap_port,
                           smtp_host,smtp_port,encrypted_password,enabled,last_verified_at,
                           updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (f"mbx_{user_id}", user_id, "private@example.com", "private@example.com",
                     "imap.example.com", 993, "smtp.example.com", 465, b"cipher", 1,
                     utc_now(), utc_now()))
        return user_id

    def emails(self, **kwargs):
        # Deliberately NOT sorted: one of these tests is about the order, and a
        # helper that quietly re-sorted made that test assert its own helper.
        return [row["email"] for row in self.db.stalled_setups(**kwargs)]

    def test_an_old_unfinished_account_is_listed(self):
        self.account("stuck@example.com", age_hours=20, gap="no_mailbox")
        self.assertIn("stuck@example.com", self.emails())

    def test_a_recent_unfinished_account_is_not_listed_yet(self):
        """Somebody who registered ten minutes ago has not stalled; they are busy."""
        self.account("fresh@example.com", age_hours=0.2, gap="no_mailbox")
        self.assertEqual(self.emails(), [])

    def test_a_finished_account_is_never_listed(self):
        self.account("done@example.com", age_hours=40, gap="none")
        self.assertEqual(self.emails(), [])

    def test_the_threshold_is_a_parameter(self):
        self.account("two-hours@example.com", age_hours=2, gap="no_mailbox")
        self.assertEqual(self.emails(hours=12), [])
        self.assertIn("two-hours@example.com", self.emails(hours=1))

    def test_an_unreachable_mailbox_is_reported_as_its_own_gap(self):
        self.account("badcode@example.com", age_hours=30, gap="unreachable")
        rows = self.db.stalled_setups(hours=12)
        self.assertEqual(rows[0]["setup_gap"], "unreachable")

    def test_a_paused_account_is_still_listed(self):
        """Pausing is the operator's reaction to the stall, not a reason to hide it."""
        self.account("paused@example.com", age_hours=30, gap="no_mailbox", status="paused")
        self.assertIn("paused@example.com", self.emails())

    def test_results_are_oldest_first(self):
        self.account("newer@example.com", age_hours=20, gap="no_mailbox")
        self.account("older@example.com", age_hours=40, gap="no_mailbox")
        self.assertEqual(self.emails(), ["older@example.com", "newer@example.com"])


class StalledSetupAlertTests(unittest.TestCase):
    """The sentinel that turns the silent case into one e-mail a day."""

    def setUp(self):
        with db.connect() as connection:
            connection.execute("DELETE FROM mailboxes")
            connection.execute("DELETE FROM users")
        self.user_id = "usr_stalled_alert"
        with db.connect() as connection:
            connection.execute(
                "INSERT INTO users(id,email,password_hash,status,created_at) VALUES(?,?,?,?,?)",
                (self.user_id, "quiet@example.com", "x", "active", hours_ago(30)))

    def findings(self):
        return alerting.evaluate(db, disk_percent=1.0, certificate_days=365)

    def test_an_account_that_never_finished_produces_a_finding(self):
        keys = [item["key"] for item in self.findings()]
        self.assertIn(f"setup_stalled:{self.user_id}", keys)

    def test_the_finding_says_what_is_missing_and_that_nothing_errored(self):
        item = [f for f in self.findings() if f["key"].startswith("setup_stalled")][0]
        self.assertIn("私人转发邮箱", item["detail"])
        self.assertIn("不会产生任何错误", item["detail"])
        self.assertEqual(item["severity"], "warning")

    def test_it_is_not_repeated_every_six_hours_like_an_outage(self):
        """A stalled signup is not an incident; one reminder a day is enough."""
        self.assertEqual(alerting._repeat_for(f"setup_stalled:{self.user_id}"),
                         alerting.ALERT_SETUP_REPEAT_SECONDS)
        self.assertGreaterEqual(alerting.ALERT_SETUP_REPEAT_SECONDS, 24 * 3600)
        # Everything else keeps the six-hour default.
        self.assertIsNone(alerting._repeat_for("queue_backlog"))

    def test_a_finished_account_disappears_from_the_findings(self):
        with db.connect() as connection:
            connection.execute(
                """INSERT INTO mailboxes(id,user_id,email,report_to,imap_host,imap_port,
                       smtp_host,smtp_port,encrypted_password,enabled,last_verified_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("mbx_stalled_alert", self.user_id, "private@example.com", "private@example.com",
                 "imap.example.com", 993, "smtp.example.com", 465, b"cipher", 1,
                 utc_now(), utc_now()))
        keys = [item["key"] for item in self.findings()]
        self.assertNotIn(f"setup_stalled:{self.user_id}", keys)

    def test_there_is_one_finding_per_account(self):
        with db.connect() as connection:
            connection.execute(
                "INSERT INTO users(id,email,password_hash,status,created_at) VALUES(?,?,?,?,?)",
                ("usr_second", "quiet2@example.com", "x", "active", hours_ago(30)))
        keys = [item["key"] for item in self.findings() if item["key"].startswith("setup_stalled")]
        self.assertEqual(len(keys), 2, "每个账号一条，才能各自独立地报「已配完」")


if __name__ == "__main__":
    unittest.main()
