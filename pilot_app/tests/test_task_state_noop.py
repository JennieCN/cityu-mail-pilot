"""Repeating an unchanged task decision must not cancel a retrying push.

`set_task_state` used to bump `calendar_mail_sources.schedule_revision` and call
`push.cancel_ineligible()` on **every** call, including the ones where the state
was already what the client asked for. A delivery row remembers the revision it
was created against, so the bump made a `retry` delivery look stale and
`cancel_ineligible` turned it into `cancelled` -- a state nothing revives. The
reminder then never fires, and nobody sees why.
"""
import unittest

from pilot_app import push
from pilot_app.service import PilotService
from pilot_app.tests.test_notification_devices import NotificationFixture

BODY = (
    "## 1. 重要程度与一句话结论\n- 等级：高\n\n"
    "## 2. 必须采取的行动与截止时间\n- 提交作业到 Canvas（截止：2099-10-20 12:00）\n\n"
    "## 3. 邮件内容总结\n- 合成作业。\n"
)


class TaskStateNoopTests(NotificationFixture, unittest.TestCase):
    def open_mail_task(self):
        """A mail-derived task with a live reminder and one delivery waiting to retry."""
        self.bind()
        push.preferences(self.db, "a", dict(mail_default_enabled=True,
                                            timed_offset_minutes=60,
                                            all_day_minute=540), now="2000-01-01T00:00:00Z")
        with self.db.connect() as c:
            c.execute("UPDATE profiles SET timezone='UTC' WHERE user_id='a'")
        service = PilotService(self.db, self.box)
        report_id = self.db.create_report(
            user_id="a", message_id=None, kind="immediate", subject="Synthetic deadline",
            body=service.encrypt_report(BODY, "a"), sent_to="a@example.test")
        self.assertEqual(service.reconcile_calendar_once()["done"], 1)
        with self.db.connect() as c:
            source = c.execute("SELECT * FROM calendar_mail_sources WHERE report_id=?",
                               (report_id,)).fetchone()
        self.assertIsNotNone(source)
        self.assertEqual(source["user_state"], "open")
        # The user has already recorded a decision for this task (the row exists
        # with state='open'), which is the ordinary state of affairs on the
        # dashboard: every task the account has ever touched has one.
        with self.db.connect() as c:
            c.execute("""INSERT INTO task_states(user_id,task_key,state,task_day,subject,updated_at)
                         VALUES(?,?,'open','2099-10-20','Canvas 作业',?)""",
                      ("a", source["task_key"], self.now))
        # The recovery window has to finish before a reminder may become ready.
        self.db.require_calendar_recovery("p2_reupgrade")
        for _ in range(10):
            self.db.calendar_recovery_batch()
            service.reconcile_calendar_once()
            if self.db.complete_calendar_recovery():
                break
        self.assertEqual(self.db.advance_due_calendar_reminders(self.now)["ready"], 1)
        self.assertEqual(push.expand_ready(self.db, self.now), 1)
        with self.db.connect() as c:
            # What a transient push failure leaves behind: a row waiting to retry.
            c.execute("""UPDATE reminder_deliveries SET status='retry',attempts=1,cancel_reason=''
                         WHERE user_id='a'""")
            delivery = c.execute("SELECT * FROM reminder_deliveries WHERE user_id='a'").fetchone()
            reminder = c.execute("SELECT * FROM calendar_reminders WHERE user_id='a'").fetchone()
        return dict(source), dict(reminder), dict(delivery)

    def state(self, task_key):
        with self.db.connect() as c:
            source = c.execute("SELECT * FROM calendar_mail_sources WHERE user_id='a' AND task_key=?",
                               (task_key,)).fetchone()
            reminder = c.execute("SELECT * FROM calendar_reminders WHERE user_id='a'").fetchone()
            delivery = c.execute("SELECT * FROM reminder_deliveries WHERE user_id='a'").fetchone()
            stored = c.execute("SELECT * FROM task_states WHERE user_id='a' AND task_key=?",
                               (task_key,)).fetchone()
        return dict(source), dict(reminder), dict(delivery), (dict(stored) if stored else None)

    def test_repeating_the_same_state_keeps_the_retrying_delivery(self):
        source, reminder, delivery = self.open_mail_task()
        self.db.set_task_state("a", source["task_key"], "open", {"subject": "同一件事，重新提交一次"})
        after_source, after_reminder, after_delivery, stored = self.state(source["task_key"])
        self.assertEqual(after_delivery["status"], "retry",
                         "状态没变的重提交不该把正在重试的推送取消掉")
        self.assertEqual(after_source["schedule_revision"], source["schedule_revision"])
        self.assertEqual(after_reminder["source_schedule_revision"], reminder["source_schedule_revision"])
        self.assertEqual(after_reminder["status"], reminder["status"])
        # ...but the snapshot still has to refresh: the row is what the archive shows.
        self.assertEqual(stored["subject"], "同一件事，重新提交一次")

    def test_a_real_change_still_cancels_the_delivery(self):
        source, _, _ = self.open_mail_task()
        self.db.set_task_state("a", source["task_key"], "done")
        after_source, after_reminder, after_delivery, _ = self.state(source["task_key"])
        self.assertEqual(after_delivery["status"], "cancelled",
                         "真的标成已处理，待发的推送就该取消")
        self.assertEqual(after_reminder["status"], "cancelled_done")
        self.assertEqual(after_source["schedule_revision"], source["schedule_revision"] + 1)


if __name__ == "__main__":
    unittest.main()
