"""Push API and transport tests. All network tests use loopback TLS only."""
import datetime as dt
import json
import os
import socket
import ssl
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from pilot_app import push,web
from pilot_app.service import PilotService
from pilot_app.push_transport import PinnedSession,PinnedHTTPSConnection
from pilot_app.reminder_worker import response_outcome,run_once
from pilot_app.security import token_hash
from pilot_app.tests.test_notification_devices import NotificationFixture


class WebPushAPITests(NotificationFixture,unittest.TestCase):
    def call(self,method,path,payload=None,owner="a",origin="https://pilot.example.test"):
        cookie="synthetic-cookie-"+owner
        with self.db.connect() as c:
            c.execute("INSERT OR IGNORE INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                (token_hash(cookie),owner,"2100-01-01T00:00:00Z",self.now))
        headers={"Content-Type":"application/json","Cookie":web.SESSION_COOKIE+"="+cookie}
        if origin is not None:
            headers["Origin"]=origin
        req=web.Request(method=method,path=path,query={},headers=headers,
            body=json.dumps(payload).encode() if payload is not None else b"",client="127.0.0.1")
        with patch.object(web,"get_db",return_value=self.db),patch.object(web,"get_service",return_value=SimpleNamespace(secrets=self.box)),patch.dict(os.environ,{"INFE_PILOT_ORIGIN":"https://pilot.example.test"}):
            response=web.dispatch(req)
        return response.status,json.loads(response.body),response.headers

    def test_rules_owner_origin_revision_and_forbidden_due(self):
        event,reminder=self.ready()
        path="/api/calendar/reminders/manual/"+event["id"]
        status,value,headers=self.call("GET",path)
        self.assertEqual(status,200);self.assertEqual(headers["Cache-Control"],"no-store")
        self.assertEqual(self.call("GET",path,owner="b")[0],404)
        data=dict(expected_source_schedule_revision=event["schedule_revision"],
            expected_reminder_revision=value["reminder"]["revision"],rule_kind="minutes_before",rule_value=30,enabled=True)
        self.assertEqual(self.call("PUT",path,data,origin=None)[0],403)
        self.assertEqual(self.call("PUT",path,dict(data,due_at_utc=self.now))[0],422)
        self.assertEqual(self.call("PUT",path,data)[0],200)
        self.assertEqual(self.call("PUT",path,data)[0],409)

    def test_preferences_validate_without_overwriting_profile(self):
        payload=dict(mail_default_enabled=True,timed_offset_minutes=60,all_day_minute=540)
        self.assertEqual(self.call("PUT","/api/calendar/preferences",payload)[0],200)
        self.assertEqual(self.call("GET","/api/calendar/preferences")[1]["timed_offset_minutes"],60)
        self.assertEqual(self.call("PUT","/api/calendar/preferences",dict(payload,timed_offset_minutes=-1))[0],422)

    def test_application_rule_to_fake_sender_and_authorized_target(self):
        self.bind();event,_=self.ready();messages=[]
        def provider(message):
            messages.append(json.loads(message["payload"]));return ("provider_accepted",201,0)
        self.assertEqual(run_once(self.db,self.box,provider,now=self.now,slots=2),1)
        self.assertEqual(len(messages),1)
        target="/api/notifications/deliveries/"+messages[0]["delivery_id"]+"/target"
        self.assertEqual(self.call("GET",target)[1]["event_id"],event["id"])
        self.assertEqual(self.call("GET",target,owner="b")[0],404)

    def test_provider_classification_retry_after_and_auth(self):
        for code,expected in ((201,"provider_accepted"),(410,"invalid"),(403,"auth_failed"),(429,"retry"),(503,"retry"),(302,"permanent_failed")):
            self.assertEqual(response_outcome(code)[0],expected)
        self.assertEqual(response_outcome(429,"120")[2],120)
        self.assertEqual(response_outcome(503,"Tue, 20 Oct 2099 11:07:00 GMT",self.now)[2],120)

    def test_sender_key_mismatch_stops_before_delivery_without_migration(self):
        from pilot_app.reminder_worker import main
        from cryptography.hazmat.primitives.asymmetric import ec
        key=ec.derive_private_key(2,ec.SECP256R1()).public_key()
        provider=SimpleNamespace(vapid=SimpleNamespace(public_key=key))
        env={"INFE_PILOT_PUSH_ENABLED":"1","INFE_PILOT_DB":str(self.db.path),
             "INFE_PILOT_VAPID_PRIVATE_KEY_FILE":"synthetic-only.pem",
             "INFE_PILOT_VAPID_SUBJECT":"mailto:synthetic@example.test",
             "INFE_PILOT_VAPID_PUBLIC_KEY":"mismatched"}
        with patch.dict(os.environ,env),patch("pilot_app.reminder_worker.WebPushProvider",return_value=provider),patch("pilot_app.reminder_worker.run_once") as run,patch("pilot_app.database.Database.initialize") as initialize:
            with self.assertRaisesRegex(SystemExit,"configuration unavailable"):
                main(["--once"])
            run.assert_not_called();initialize.assert_not_called()

    def test_completed_mail_target_recovers_owned_task_without_calendar_projection(self):
        self.bind()
        push.preferences(self.db,"a",dict(mail_default_enabled=True,timed_offset_minutes=60,all_day_minute=540),now="2000-01-01T00:00:00Z")
        with self.db.connect() as c:
            c.execute("UPDATE profiles SET timezone='UTC' WHERE user_id='a'")
        service=PilotService(self.db,self.box)
        body="## 2. 必须采取的行动与截止时间\n- 提交合成作业（截止：2099-10-20 12:00）\n"
        report=self.db.create_report(user_id="a",message_id=None,kind="immediate",subject="Synthetic mail target",body=service.encrypt_report(body,"a"),sent_to="a@example.test")
        service.reconcile_calendar_once()
        with self.db.connect() as c:
            source=c.execute("SELECT * FROM calendar_mail_sources WHERE report_id=?",(report,)).fetchone()
            c.execute("UPDATE calendar_reminders SET status='ready' WHERE mail_source_id=?",(source["id"],))
            c.execute("UPDATE calendar_recovery_state SET status='complete'")
        push.expand_ready(self.db,self.now)
        claim=push.claim_delivery(self.db,self.now)
        self.db.set_task_state("a",source["task_key"],"done")
        with patch.object(web,"_calendar_task_snapshot",return_value=None):
            cookie="synthetic-cookie-a"
            self.call("GET","/api/calendar/preferences")
            req=web.Request(method="GET",path="/api/notifications/deliveries/"+claim["id"]+"/target",query={},headers={"Cookie":web.SESSION_COOKIE+"="+cookie},body=b"",client="127.0.0.1")
            with patch.object(web,"get_db",return_value=self.db),patch.object(web,"get_service",return_value=service):
                response=web.dispatch(req)
        self.assertEqual(response.status,200)
        value=json.loads(response.body)
        self.assertEqual(value["task"]["task_key"],source["task_key"])
        self.assertEqual(value["task"]["subject"],"Synthetic mail target")
        self.assertFalse(value["task"].get("message_id"))
        self.assertEqual(self.call("GET",req.path,owner="b")[0],404)


class PushTransportTests(NotificationFixture,unittest.TestCase):
    def setUp(self):
        super().setUp()
        key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
        name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,"fcm.googleapis.com")])
        now=dt.datetime.now(dt.timezone.utc)
        cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
              .serial_number(x509.random_serial_number()).not_valid_before(now-dt.timedelta(days=1))
              .not_valid_after(now+dt.timedelta(days=1))
              .add_extension(x509.SubjectAlternativeName([x509.DNSName("fcm.googleapis.com")]),critical=False)
              .sign(key,hashes.SHA256()))
        cert_path=Path(self.tmp.name)/"server.crt";key_path=Path(self.tmp.name)/"server.key"
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
        self.requests=[];self.response_code=201;self.slow=False
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body=self.rfile.read(int(self.headers.get("Content-Length",0)))
                owner.requests.append((self.path,self.headers.get("Host"),body))
                if owner.slow:
                    time.sleep(1)
                try:
                    self.send_response(owner.response_code)
                    self.send_header("Content-Length","10000000")
                    self.send_header("Location","https://fcm.googleapis.com/never-follow")
                    self.end_headers()
                except OSError:
                    pass
            def log_message(self,*args):
                pass
        self.server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
        context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(str(cert_path),str(key_path))
        self.server.socket=context.wrap_socket(self.server.socket,server_side=True)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.client_context=ssl.create_default_context(cafile=str(cert_path))
        self.resolutions=[];self.connections=[]
        def resolver(host,port,**kwargs):
            self.resolutions.append(host);return self.resolver(host,port,**kwargs)
        def connect(host,address,timeout):
            self.connections.append((host,address))
            # Explicit test injection only. Production cannot enable private IPs.
            return PinnedHTTPSConnection(host,"127.0.0.1",port=self.server.server_port,timeout=timeout,context=self.client_context)
        self.transport=PinnedSession(resolver=resolver,connection_factory=connect)

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)
        super().tearDown()

    def test_actual_tls_host_pinning_zero_body_reads_no_proxy_or_redirect(self):
        self.response_code=302
        with patch.dict(os.environ,{"HTTPS_PROXY":"http://127.0.0.1:1","ALL_PROXY":"http://127.0.0.1:1"}):
            result=self.transport.post(self.subscription["endpoint"],data=b"synthetic encrypted data",headers={"TTL":"30"})
        self.assertEqual(result.status_code,302);self.assertEqual(result.text,"")
        self.assertEqual(self.resolutions,["fcm.googleapis.com"])
        self.assertEqual(self.connections,[("fcm.googleapis.com","8.8.8.8")])
        self.assertEqual(len(self.requests),1);self.assertEqual(self.requests[0][1],"fcm.googleapis.com")

    def test_deadline_closes_the_actual_request(self):
        self.slow=True;self.transport.deadline=0.15;start=time.monotonic()
        with self.assertRaises((OSError,TimeoutError,http_client_error())):
            self.transport.post(self.subscription["endpoint"],data=b"x",headers={})
        self.assertLess(time.monotonic()-start,0.8)
        self.assertEqual(len(self.requests),1)

    def test_wrong_hostname_is_rejected_by_tls(self):
        connection=PinnedHTTPSConnection("wrong.example.test","127.0.0.1",port=self.server.server_port,context=self.client_context)
        try:
            with self.assertRaises(ssl.SSLCertVerificationError):
                connection.request("POST","/",body=b"x")
        finally:
            connection.close()
        self.assertEqual(self.requests,[])

    def test_dns_finishing_after_budget_never_starts_a_connection(self):
        def slow_resolver(*args,**kwargs):
            time.sleep(0.12)
            return self.resolver(*args,**kwargs)
        self.transport.resolver=slow_resolver;self.transport.deadline=0.1
        with self.assertRaises(TimeoutError):
            self.transport.post(self.subscription["endpoint"],data=b"x",headers={})
        self.assertEqual(self.connections,[])
        self.assertEqual(self.requests,[])

    def test_socket_closed_at_request_boundary_cannot_reconnect(self):
        owner=self
        class ClosingConnection(PinnedHTTPSConnection):
            def request(connection,*args,**kwargs):
                connection.close()
                return super(ClosingConnection,connection).request(*args,**kwargs)
        self.transport.connection_factory=lambda host,address,timeout: ClosingConnection(
            host,"127.0.0.1",port=owner.server.server_port,timeout=timeout,context=owner.client_context)
        with self.assertRaises(http_client_error()):
            self.transport.post(self.subscription["endpoint"],data=b"x",headers={})
        self.assertEqual(self.requests,[])

    def test_installed_sdk_encrypts_through_real_local_tls_transport(self):
        try:
            from py_vapid import Vapid
            import http_ece
        except ImportError:
            self.skipTest("Optional sender dependencies are verified in the isolated sender venv")
        from pilot_app.reminder_worker import WebPushProvider
        from cryptography.hazmat.primitives.asymmetric import ec
        self.bind();self.ready()
        vapid=Vapid();vapid.generate_keys()
        key_path=Path(self.tmp.name)/"synthetic-vapid.pem";vapid.save_key(str(key_path))
        provider=WebPushProvider(str(key_path),"mailto:synthetic@example.test",session=self.transport)
        self.assertEqual(run_once(self.db,self.box,provider,now=self.now,slots=1),1)
        self.assertEqual(len(self.requests),1)
        plaintext=http_ece.decrypt(self.requests[0][2],private_key=ec.derive_private_key(1,ec.SECP256R1()),
                                  auth_secret=bytes(range(16)),version="aes128gcm")
        payload=json.loads(plaintext)
        self.assertEqual(payload["message"],"calendar_reminder")
        self.assertNotIn("Private event title",plaintext.decode())
        with self.db.connect() as c:
            self.assertEqual(c.execute("SELECT status FROM reminder_deliveries").fetchone()[0],"provider_accepted")


def http_client_error():
    import http.client
    return http.client.HTTPException


if __name__=="__main__":
    unittest.main()
