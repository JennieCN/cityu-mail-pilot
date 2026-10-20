"""Synthetic device credentials and real SQLite transaction regressions."""
import base64
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization

from pilot_app import calendar_events, push
from pilot_app.database import Database
from pilot_app.security import SecretBox


class NotificationFixture:
    now = "2099-10-20T11:05:00Z"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.tmp.name) / "notifications.sqlite3")
        self.db.initialize()
        self.box = SecretBox(bytes(range(32)))
        with self.db.connect() as c:
            for user in ("a", "b"):
                c.execute("INSERT INTO users(id,email,password_hash,created_at) VALUES(?,?,?,?)",
                          (user,user+"@example.test","synthetic",self.now))
                c.execute("INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                          ("session-"+user,user,"2100-01-01T00:00:00Z",self.now))
                c.execute("INSERT INTO profiles(user_id,updated_at) VALUES(?,?)",(user,self.now))
        self.installation = "synthetic-installation-one"
        self.secret = "synthetic-installation-secret-0123456789-abcdef"
        point = ec.derive_private_key(1,ec.SECP256R1()).public_key().public_bytes(
            serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)
        self.subscription = {"endpoint":"https://fcm.googleapis.com/fcm/send/synthetic",
                             "keys":{"p256dh":base64.urlsafe_b64encode(point).decode().rstrip("="),
                                     "auth":base64.urlsafe_b64encode(bytes(range(16))).decode().rstrip("=")}}

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def resolver(*args,**kwargs):
        return [(2,1,6,"",("8.8.8.8",443))]

    def challenge(self,user="a",generation=0):
        return push.create_challenge(self.db,user,"session-"+user,self.installation,
                                     self.secret,generation,self.now)

    def bind(self,user="a",generation=0,challenge=None,subscription=None):
        ch=challenge or self.challenge(user,generation)
        return push.bind_device(self.db,self.box,user,"session-"+user,challenge_id=ch["challenge_id"],
            nonce=ch["nonce"],installation_id=self.installation,secret=self.secret,
            expected_generation=generation,subscription=subscription or self.subscription,
            now=self.now,resolver=self.resolver)

    def ready(self):
        event=self.db.create_calendar_event("a","create","digest",calendar_events.validate({
            "title":"Private event title","date":"2099-10-20","time":"12:00",
            "timezone":"UTC","all_day":False,"state":"open"}),self.box)
        reminder=push.save_rule(self.db,self.box,"a","manual",event["id"],
            expected_source_revision=event["schedule_revision"],expected_reminder_revision=None,
            rule_kind="minutes_before",rule_value=60,now="2099-10-20T10:00:00Z")
        self.db.require_calendar_recovery("p2_reupgrade")
        for _ in range(5):
            self.db.calendar_recovery_batch("2099-10-20T10:00:00Z")
            if self.db.complete_calendar_recovery("2099-10-20T10:00:00Z"):
                break
        self.assertEqual(self.db.advance_due_calendar_reminders(self.now)["ready"],1)
        return event,reminder


class NotificationDeviceTests(NotificationFixture,unittest.TestCase):
    def test_binding_replay_is_content_bound_and_encrypted(self):
        ch=self.challenge()
        first=self.bind(challenge=ch)
        self.assertEqual(first,self.bind(challenge=ch))
        changed=dict(self.subscription,endpoint=self.subscription["endpoint"]+"-other")
        with self.assertRaises(push.NotificationConflict):
            self.bind(challenge=ch,subscription=changed)
        with self.db.connect() as c:
            row=c.execute("SELECT * FROM notification_devices").fetchone()
            self.assertNotIn(b"fcm.googleapis.com",row["encrypted_subscription"])
        self.assertNotIn("encrypted_subscription",push.list_devices(self.db,"a")[0])

    def test_challenge_cannot_cross_owner_or_session(self):
        ch=self.challenge()
        with self.assertRaises(PermissionError):
            self.bind(user="b",challenge=ch)
        self.db.delete_session("session-a")
        with self.assertRaises(PermissionError):
            self.bind(challenge=ch)

    def test_revocation_increments_generation_and_replay_cannot_resurrect(self):
        ch=self.challenge();device=self.bind(challenge=ch)
        with self.assertRaises(KeyError):
            push.revoke_device(self.db,"b",device["id"],1,self.now)
        result=push.revoke_device(self.db,"a",device["id"],1,self.now)
        self.assertEqual(result["binding_generation"],2)
        self.assertEqual(result,push.revoke_device(self.db,"a",device["id"],1,self.now))
        with self.assertRaises(push.NotificationConflict):
            self.bind(challenge=ch)

    def test_account_switch_needs_installation_secret_and_new_challenge(self):
        self.bind();self.secret="wrong-installation-secret-0123456789-abcdef"
        with self.assertRaises(PermissionError):
            self.challenge("b",1)
        self.secret="synthetic-installation-secret-0123456789-abcdef"
        switched=self.bind("b",1)
        self.assertEqual(switched["binding_generation"],2)
        self.assertEqual(push.list_devices(self.db,"a"),[])

    def test_logout_and_global_revocation_clear_subscription(self):
        self.bind();self.db.delete_session("session-a")
        with self.db.connect() as c:
            row=c.execute("SELECT * FROM notification_devices").fetchone()
            self.assertEqual(row["state"],"revoked")
            self.assertIsNone(row["encrypted_subscription"])
        self.db.revoke_sessions("a")

    def test_endpoint_private_address_and_suffix_confusion_are_rejected(self):
        for endpoint in ("http://fcm.googleapis.com/x","https://fcm.googleapis.com.evil.test/x",
                         "https://evilpush.apple.com/x","https://fcm.googleapis.com:8443/x"):
            with self.subTest(endpoint=endpoint),self.assertRaises(ValueError):
                push.validate_endpoint(endpoint,self.resolver)
        with self.assertRaises(ValueError):
            push.validate_endpoint(self.subscription["endpoint"],lambda *a,**k:[(2,1,6,"",("127.0.0.1",443))])
        for address in ("224.0.0.1","ff02::1","10.0.0.1"):
            with self.subTest(address=address),self.assertRaises(ValueError):
                push.validate_endpoint(self.subscription["endpoint"],lambda *a,**k:[(2,1,6,"",(address,443))])

    def test_migration_is_idempotent_and_new_tables_cascade(self):
        self.bind();self.db.initialize()
        self.assertEqual(self.db.get_setting("calendar_schema_version"),"3")
        self.db.set_user_status("a","deleted")
        with self.db.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM notification_devices").fetchone()[0],0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM notification_installations").fetchone()[0],0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM notification_binding_challenges").fetchone()[0],0)

    def test_two_challenges_cannot_bind_the_same_generation(self):
        first=self.challenge();second=self.challenge()
        self.bind(challenge=first)
        with self.assertRaises(push.NotificationConflict):
            self.bind(challenge=second)
        with self.db.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM notification_devices WHERE state='active'").fetchone()[0],1)

    def test_deletion_clears_unbound_challenge_installation(self):
        self.challenge()
        self.db.set_user_status("a","deleted")
        with self.db.connect() as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM notification_installations").fetchone()[0],0)

    def test_deleting_previous_owner_keeps_current_owner_device(self):
        self.bind();self.bind("b",1)
        self.db.set_user_status("a","deleted")
        self.assertEqual(push.list_devices(self.db,"b")[0]["state"],"active")

    def test_schema_three_failure_rolls_back_and_sender_never_migrates(self):
        from pilot_app.reminder_worker import main
        with self.db.connect() as c:
            for table in ("reminder_delivery_attempts","reminder_deliveries","notification_binding_challenges","notification_devices","notification_installations"):
                c.execute("DROP TABLE "+table)
            c.execute("UPDATE app_settings SET value='2' WHERE key='calendar_schema_version'")
        with patch.dict("os.environ",{"INFE_PILOT_PUSH_ENABLED":"1","INFE_PILOT_DB":str(self.db.path)}):
            with self.assertRaisesRegex(SystemExit,"schema 3 required"):
                main(["--once"])
        with self.assertRaisesRegex(RuntimeError,"synthetic migration failure"):
            with self.db.connect() as c:
                class FailingConnection:
                    def execute(self,sql,args=()):
                        if "CREATE TABLE IF NOT EXISTS reminder_deliveries" in sql:
                            raise RuntimeError("synthetic migration failure")
                        return c.execute(sql,args)
                push.migrate(FailingConnection())
        self.assertEqual(self.db.get_setting("calendar_schema_version"),"2")
        with self.db.connect() as c:
            self.assertIsNone(c.execute("SELECT name FROM sqlite_master WHERE name='notification_devices'").fetchone())
        self.db.initialize();self.db.initialize()
        self.assertEqual(self.db.get_setting("calendar_schema_version"),"3")


if __name__=="__main__":
    unittest.main()
