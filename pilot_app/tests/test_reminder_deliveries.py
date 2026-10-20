"""Claims, cancellations, unknown results and explicit confirmation."""
import unittest
from concurrent.futures import ThreadPoolExecutor

from pilot_app import calendar_events,push
from pilot_app.tests.test_notification_devices import NotificationFixture
from pilot_app.service import PilotService


class ReminderDeliveryTests(NotificationFixture,unittest.TestCase):
    def prepare(self):
        self.bind();event,reminder=self.ready()
        self.assertEqual(push.expand_ready(self.db,self.now),1)
        return event,reminder

    def claim(self):
        return push.claim_delivery(self.db,self.now)

    def test_real_rule_due_claim_payload_and_authorized_target(self):
        event,_=self.prepare()
        self.assertEqual(push.expand_ready(self.db,self.now),0)
        claim=self.claim();message=push.start_attempt(self.db,self.box,claim,self.now)
        self.assertNotIn("Private event title",message["payload"])
        self.assertGreater(message["ttl"],0)
        self.assertTrue(push.finish_attempt(self.db,claim,"provider_accepted",201,now=self.now))
        self.assertIsNone(self.claim())
        self.assertEqual(push.delivery_target(self.db,"a",claim["id"])["event_id"],event["id"])
        with self.assertRaises(KeyError):
            push.delivery_target(self.db,"b",claim["id"])

    def test_two_connections_claim_once(self):
        self.prepare()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.claim(),range(2)))
        self.assertEqual(sum(x is not None for x in results),1)

    def test_logout_after_claim_prevents_start(self):
        self.prepare();claim=self.claim()
        self.db.delete_session("session-a")
        self.assertIsNone(push.start_attempt(self.db,self.box,claim,self.now))

    def test_accepted_result_is_retained_after_cancellation(self):
        self.prepare();claim=self.claim()
        push.start_attempt(self.db,self.box,claim,self.now)
        self.db.delete_session("session-a")
        self.assertTrue(push.finish_attempt(self.db,claim,"provider_accepted",201,now=self.now))
        with self.db.connect() as c:
            row=c.execute("SELECT * FROM reminder_deliveries").fetchone()
            self.assertEqual(row["status"],"cancelled")
            self.assertEqual(row["last_outcome"],"provider_accepted")
            self.assertEqual(c.execute("SELECT outcome FROM reminder_delivery_attempts").fetchone()[0],"provider_accepted")

    def test_started_lease_becomes_unknown_with_one_bounded_retry(self):
        self.prepare();claim=self.claim()
        push.start_attempt(self.db,self.box,claim,self.now)
        self.assertIsNone(push.claim_delivery(self.db,"2099-10-20T11:05:31Z"))
        retry=push.claim_delivery(self.db,"2099-10-20T11:06:02Z")
        self.assertEqual(retry["unknown_retries"],1)
        push.start_attempt(self.db,self.box,retry,"2099-10-20T11:06:02Z")
        push.finish_attempt(self.db,retry,"unknown",now="2099-10-20T11:06:03Z")
        self.assertIsNone(push.claim_delivery(self.db,"2099-10-20T11:07:00Z"))
        # A known, late acceptance is still recorded without issuing another delivery.
        push.finish_attempt(self.db,claim,"provider_accepted",201,now="2099-10-20T11:07:01Z")
        self.assertIsNone(push.claim_delivery(self.db,"2099-10-20T11:07:02Z"))

    def test_natural_session_expiry_blocks_without_cleanup(self):
        self.prepare()
        with self.db.connect() as c:
            c.execute("UPDATE sessions SET expires_at=?",(self.now,))
        self.assertIsNone(self.claim())

    def test_explicit_confirmation_is_owner_and_version_fenced(self):
        event,reminder=self.prepare()
        moved=calendar_events.validate({"title":"T","date":"2099-10-21","time":"12:00","timezone":"UTC","all_day":False})
        event=self.db.update_calendar_event("a",event["id"],event["revision"],moved,self.box)
        view=push.reminder_view(self.db,self.box,"a","manual",event["id"])
        kwargs=dict(expected_source_revision=event["schedule_revision"],
            expected_reminder_revision=view["reminder"]["revision"],rule_kind="minutes_before",rule_value=60,now=self.now)
        saved=push.save_rule(self.db,self.box,"a","manual",event["id"],**kwargs)
        self.assertEqual(saved["confirmation_required"],1)
        with self.assertRaises(push.NotificationConflict):
            push.save_rule(self.db,self.box,"a","manual",event["id"],confirm=True,**kwargs)
        kwargs["expected_reminder_revision"]=saved["revision"]
        with self.assertRaises(KeyError):
            push.save_rule(self.db,self.box,"b","manual",event["id"],confirm=True,**kwargs)
        confirmed=push.save_rule(self.db,self.box,"a","manual",event["id"],confirm=True,**kwargs)
        self.assertEqual(confirmed["confirmation_required"],0)
        self.assertEqual(confirmed["dispatch_generation"],reminder["dispatch_generation"]+1)

    def test_real_mail_report_outbox_default_reminder_and_target(self):
        self.bind()
        push.preferences(self.db,"a",dict(mail_default_enabled=True,timed_offset_minutes=60,all_day_minute=540),now="2000-01-01T00:00:00Z")
        with self.db.connect() as c:
            c.execute("UPDATE profiles SET timezone='UTC' WHERE user_id='a'")
        body="## 1. 重要程度与一句话结论\n- 等级：高\n\n## 2. 必须采取的行动与截止时间\n- 提交作业到 Canvas（截止：2099-10-20 12:00）\n\n## 3. 邮件内容总结\n- 合成作业。\n"
        service=PilotService(self.db,self.box)
        report_id=self.db.create_report(user_id="a",message_id=None,kind="immediate",subject="Synthetic deadline",body=service.encrypt_report(body,"a"),sent_to="a@example.test")
        self.assertEqual(service.reconcile_calendar_once()["done"],1)
        with self.db.connect() as c:
            source=c.execute("SELECT * FROM calendar_mail_sources WHERE report_id=?",(report_id,)).fetchone()
        self.assertIsNotNone(source)
        view=push.reminder_view(self.db,self.box,"a","mail",source["id"])
        self.assertEqual(view["reminder"]["due_at_utc"],"2099-10-20T11:00:00Z")
        self.db.require_calendar_recovery("p2_reupgrade")
        for _ in range(10):
            self.db.calendar_recovery_batch()
            service.reconcile_calendar_once()
            if self.db.complete_calendar_recovery():
                break
        self.assertEqual(self.db.advance_due_calendar_reminders(self.now)["ready"],1)
        self.assertEqual(push.expand_ready(self.db,self.now),1)
        claim=self.claim();push.start_attempt(self.db,self.box,claim,self.now)
        push.finish_attempt(self.db,claim,"provider_accepted",201,now=self.now)
        self.assertEqual(push.delivery_target(self.db,"a",claim["id"])["task_key"],source["task_key"])
        with self.db.connect() as c:
            c.execute("UPDATE reports SET body_markdown=? WHERE id=?",(service.encrypt_report(body.replace("2099-10-20","2099-10-21"),"a"),report_id))
        with self.assertRaises(push.NotificationConflict):
            push.reminder_view(self.db,self.box,"a","mail",source["id"])
        with self.db.connect() as c:
            c.execute("UPDATE reminder_deliveries SET status='pending',attempts=0,claim_token=NULL,call_started_at=NULL")
        stale=self.claim()
        self.assertIsNotNone(stale)
        self.assertIsNone(push.start_attempt(self.db,self.box,stale,self.now))
        with self.db.connect() as c:
            self.assertEqual(c.execute("SELECT status FROM reminder_deliveries").fetchone()[0],"cancelled")

    def test_done_reopen_does_not_duplicate_an_accepted_delivery(self):
        event,_=self.prepare();claim=self.claim()
        push.start_attempt(self.db,self.box,claim,self.now)
        push.finish_attempt(self.db,claim,"provider_accepted",201,now=self.now)
        for state in ("done","open"):
            current=self.db.calendar_event("a",event["id"])
            values=calendar_events.validate(dict(title="T",date="2099-10-20",time="12:00",timezone="UTC",all_day=False,state=state))
            self.db.update_calendar_event("a",event["id"],current["revision"],values,self.box)
        self.db.advance_due_calendar_reminders(self.now)
        self.assertEqual(push.expand_ready(self.db,self.now),0)
        self.assertIsNone(self.claim())

    def test_retry_response_is_bounded_to_three_attempts(self):
        self.prepare();claim=self.claim()
        for attempt in range(3):
            now=push.after(self.now,attempt*31)
            if attempt:
                claim=push.claim_delivery(self.db,now)
            self.assertIsNotNone(claim)
            push.start_attempt(self.db,self.box,claim,now)
            push.finish_attempt(self.db,claim,"retry",503,30,now)
        self.assertIsNone(push.claim_delivery(self.db,push.after(self.now,120)))
        with self.db.connect() as c:
            row=c.execute("SELECT status,attempts FROM reminder_deliveries").fetchone()
        self.assertEqual(tuple(row),("permanent_failed",3))

    def test_gone_revokes_only_the_current_device(self):
        first=self.bind()
        self.installation="synthetic-installation-two"
        other=self.bind(subscription=dict(self.subscription,endpoint=self.subscription["endpoint"]+"-two"))
        self.ready();push.expand_ready(self.db,self.now)
        claim=self.claim();push.start_attempt(self.db,self.box,claim,self.now)
        push.finish_attempt(self.db,claim,"invalid",410,now=self.now)
        devices={row["id"]:row for row in push.list_devices(self.db,"a")}
        self.assertEqual(devices[claim["device_id"]]["state"],"invalid")
        unaffected=other if claim["device_id"]==first["id"] else first
        self.assertEqual(devices[unaffected["id"]]["state"],"active")
        self.assertEqual(self.claim()["device_id"],unaffected["id"])

    def test_auth_failure_pauses_channel_without_revoking_devices(self):
        self.prepare();claim=self.claim()
        push.start_attempt(self.db,self.box,claim,self.now)
        push.finish_attempt(self.db,claim,"auth_failed",403,now=self.now)
        self.assertEqual(self.db.get_setting("notification_channel_paused"),"1")
        self.assertEqual(push.list_devices(self.db,"a")[0]["state"],"active")
        self.assertIsNone(self.claim())

    def test_recovery_gate_waits_without_discarding_a_retry(self):
        self.prepare();claim=self.claim()
        push.start_attempt(self.db,self.box,claim,self.now)
        push.finish_attempt(self.db,claim,"retry",503,30,self.now)
        self.db.require_calendar_recovery("p2_reupgrade")
        self.assertIsNone(push.claim_delivery(self.db,push.after(self.now,31)))
        with self.db.connect() as c:
            self.assertEqual(c.execute("SELECT status FROM reminder_deliveries").fetchone()[0],"retry")
            c.execute("UPDATE calendar_recovery_state SET status='complete'")
        retry=push.claim_delivery(self.db,push.after(self.now,31))
        self.assertEqual(retry["id"],claim["id"])
        self.assertEqual(retry["attempts"],1)

    def test_recovered_pending_reminder_waits_for_due_transition(self):
        self.prepare();claim=self.claim()
        push.start_attempt(self.db,self.box,claim,self.now)
        push.finish_attempt(self.db,claim,"retry",503,30,self.now)
        with self.db.connect() as c:
            c.execute("UPDATE calendar_reminders SET status='pending'")
            push.cancel_ineligible(c,user="a",now=self.now)
        self.assertIsNone(push.claim_delivery(self.db,push.after(self.now,31)))
        self.db.advance_due_calendar_reminders(push.after(self.now,32))
        self.assertEqual(push.claim_delivery(self.db,push.after(self.now,32))["id"],claim["id"])

    def test_stale_acceptance_records_fact_without_overwriting_new_claim(self):
        self.prepare();old=self.claim();push.start_attempt(self.db,self.box,old,self.now)
        push.claim_delivery(self.db,push.after(self.now,31))
        current=push.claim_delivery(self.db,push.after(self.now,62))
        self.assertFalse(push.finish_attempt(self.db,old,"provider_accepted",201,now=push.after(self.now,63)))
        with self.db.connect() as c:
            row=c.execute("SELECT * FROM reminder_deliveries").fetchone()
            self.assertEqual(row["claim_token"],current["claim_token"])
            self.assertEqual(row["status"],"claimed")
            self.assertEqual(c.execute("SELECT outcome FROM reminder_delivery_attempts WHERE claim_token=?",(old["claim_token"],)).fetchone()[0],"provider_accepted")
        self.assertIsNone(push.start_attempt(self.db,self.box,current,push.after(self.now,64)))

    def test_lease_without_network_start_is_safe_to_reclaim(self):
        self.prepare();first=self.claim()
        second=push.claim_delivery(self.db,push.after(self.now,31))
        self.assertNotEqual(first["claim_token"],second["claim_token"])
        self.assertEqual(second["attempts"],0)
        self.assertEqual(second["unknown_retries"],0)
        self.assertIsNone(push.start_attempt(self.db,self.box,first,push.after(self.now,31)))

    def test_previously_unsent_done_open_reuses_delivery_identity(self):
        event,_=self.prepare()
        with self.db.connect() as c:
            original=c.execute("SELECT id FROM reminder_deliveries").fetchone()[0]
        for state in ("done","open"):
            current=self.db.calendar_event("a",event["id"])
            values=calendar_events.validate(dict(title="T",date="2099-10-20",time="12:00",timezone="UTC",all_day=False,state=state))
            self.db.update_calendar_event("a",event["id"],current["revision"],values,self.box)
        self.db.advance_due_calendar_reminders(self.now)
        push.expand_ready(self.db,self.now)
        self.assertEqual(self.claim()["id"],original)


if __name__=="__main__":
    unittest.main()
