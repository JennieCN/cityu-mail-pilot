"""Tests for the IMAP IDLE watcher.

The properties that matter:

* a push notification must actually trigger the ordinary fetch path — that is
  the entire point, and it must reuse ``poll_mailbox`` rather than reimplement
  it, so the "never skip a mail" logic stays in one tested place;
* the cursor must be re-read from the database before every poll, because a
  watcher keeps running while the poller advances it;
* when the interpreter has no ``IMAP4.idle`` (the macOS 3.9 dev box) nothing is
  started at all and the plain poller carries on unchanged;
* a server that does not advertise IDLE is left to the poller, without errors;
* a watcher that cannot connect backs off and retries and **never raises**, so
  it cannot take the worker down;
* the supervisor starts one watcher per active mailbox and stops the ones whose
  account disappeared.

No test here touches the network: the connection factory is injected.
"""

from __future__ import annotations

import contextlib
import imaplib
import logging
import os
import pathlib
import tempfile
import threading
import time
import unittest
from unittest import mock

from pilot_app import idle
from pilot_app.database import Database
from pilot_app.security import SecretBox, hash_password, token_hash


class _ProblemCollector(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records: list = []

    def emit(self, record):
        self.records.append(record)


@contextlib.contextmanager
def capture_problems():
    """Collect anything the watcher logs at WARNING or above.

    ``watch_mailbox`` is deliberately fail-soft: it catches *everything* and
    retries, so a plain bug in it — a missing import, a wrong variable name —
    looks exactly like a flaky provider and the tests would still pass. That is
    how two separate ``NameError``s hid in this module, each one turning into a
    silent 30-second reconnect stall. Asserting "the happy path logs nothing"
    is what makes those visible.

    Only records from the calling thread are kept: a supervisor thread left
    over from a previous test would otherwise be counted against this one.
    """
    collector = _ProblemCollector()
    collector.origin = threading.get_ident()
    root = logging.getLogger()
    root.addHandler(collector)
    try:
        yield collector
    finally:
        root.removeHandler(collector)


def _messages(collector) -> list:
    return [record.getMessage() for record in collector.records
            if getattr(record, "thread", None) == collector.origin]


class FakeIdler:
    """Stands in for ``IMAP4.idle()``.

    ``hold`` matters: the real context blocks until a notification or the
    ``duration`` timeout, and a fake that returns instantly turns the watcher's
    outer loop into a hot spin that floods the test with polls.
    """

    def __init__(self, responses, hold=0.02):
        self.responses = list(responses)
        self.hold = hold

    def __enter__(self):
        def generate():
            for item in self.responses:
                yield item
            time.sleep(self.hold)

        return generate()

    def __exit__(self, *args):
        return False


class FakeClient:
    def __init__(self, responses=(("EXISTS", [b"1"]),), capabilities=("IDLE", "IMAP4REV1"),
                 hold=0.02):
        self.capabilities = tuple(capabilities)
        self.responses = responses
        self.hold = hold
        self.idle_calls = 0
        self.logged_out = False

    def idle(self, duration=None):
        self.idle_calls += 1
        return FakeIdler(self.responses, hold=self.hold)

    def logout(self):
        self.logged_out = True


class FakeDatabase:
    """Only the lookups the watcher makes, keyed the way the real one is.

    ``Database.get_mailbox`` takes a **user id** (one mailbox per user), so the
    fake must too: getting this wrong is exactly the bug this suite caught.
    """

    def __init__(self, mailboxes):
        self.rows = {row["user_id"]: row for row in mailboxes}

    def get_mailbox(self, user_id):
        return self.rows.get(user_id)

    def active_mailboxes(self):
        return [row for row in self.rows.values() if row.get("enabled")]

    def remove(self, user_id):
        self.rows.pop(user_id, None)


class FakeService:
    def __init__(self, mailboxes, stop=None, *, new_messages=1):
        self.db = FakeDatabase(mailboxes)
        self.stop = stop
        self.new_messages = new_messages
        self.polls: list = []

    def mailbox_password(self, mailbox):
        return "password"

    def poll_mailbox(self, mailbox):
        self.polls.append(mailbox.get("last_uid"))
        if self.stop is not None and len(self.polls) == 1:
            self.stop.set()
        return self.new_messages


def _mailbox(mailbox_id="mbx_1", *, user_id="usr_1", enabled=True, last_uid=0):
    return {"id": mailbox_id, "email": "box@qq.com", "imap_host": "imap.qq.com",
            "imap_port": 993, "enabled": enabled, "user_id": user_id, "last_uid": last_uid}


class CapabilityTests(unittest.TestCase):
    def test_idle_support_tracks_the_interpreter(self):
        self.assertEqual(idle.idle_supported(), hasattr(imaplib.IMAP4, "idle"))

    def test_enabled_requires_both_the_switch_and_the_api(self):
        with mock.patch.object(idle, "IDLE_ENABLED", True), \
                mock.patch.object(idle, "idle_supported", lambda: True):
            self.assertTrue(idle.enabled())
        with mock.patch.object(idle, "IDLE_ENABLED", False), \
                mock.patch.object(idle, "idle_supported", lambda: True):
            self.assertFalse(idle.enabled())
        with mock.patch.object(idle, "IDLE_ENABLED", True), \
                mock.patch.object(idle, "idle_supported", lambda: False):
            self.assertFalse(idle.enabled())

    def test_idle_defaults_to_off(self):
        """A deliberate default, not an oversight.

        On QQ Mail a concurrent SELECT on the same mailbox permanently stops
        pushes reaching an IDLE connection, and our own poller selects every
        minute. Enabling it by default would therefore hold an extra long-lived
        connection per mailbox and deliver nothing. See the module docstring for
        the three measurements behind this.
        """
        import importlib

        with mock.patch.dict("os.environ", {}, clear=False):
            os.environ.pop("INFE_PILOT_IDLE", None)
            reloaded = importlib.reload(idle)
            try:
                self.assertFalse(reloaded.IDLE_ENABLED)
                self.assertFalse(reloaded.enabled())
            finally:
                importlib.reload(idle)

    def test_the_dev_box_python_3_9_has_no_idle_api(self):
        """Documents the reason the fallback branch exists at all. On a 3.14
        interpreter this asserts the opposite, which is also correct."""
        expected = hasattr(imaplib.IMAP4, "idle")
        self.assertEqual(idle.idle_supported(), expected)

    def test_capability_detection_is_case_insensitive(self):
        self.assertTrue(idle._advertises_idle(FakeClient(capabilities=("idle",))))
        self.assertFalse(idle._advertises_idle(FakeClient(capabilities=("IMAP4REV1",))))
        self.assertFalse(idle._advertises_idle(FakeClient(capabilities=())))


class WatchMailboxTests(unittest.TestCase):
    def test_a_notification_triggers_the_ordinary_poll(self):
        stop = threading.Event()
        service = FakeService([_mailbox()], stop)
        client = FakeClient()
        with capture_problems() as problems:
            idle.watch_mailbox(service, "usr_1", stop, connect=lambda *a, **k: client)
        self.assertEqual(len(service.polls), 1,
                         "an EXISTS notification must run the normal fetch path")
        self.assertTrue(client.logged_out, "the watcher must log out when it stops")
        self.assertEqual(_messages(problems), [], "a healthy watch cycle must log nothing")

    def test_a_notification_is_logged_so_idle_can_be_seen_working(self):
        """Without this line, a working push path and one that silently fell
        back to the 60-second poll look identical in the journal — which is
        exactly what made the first production acceptance run inconclusive."""
        stop = threading.Event()

        class Notifying(FakeService):
            def poll_mailbox(self, mailbox):
                self.polls.append(1)
                # Stop on the *second* poll: stopping on the first would return
                # before the watcher ever entered IDLE, so there would be no
                # notification to log.
                if len(self.polls) >= 2:
                    self.stop.set()
                return 1

        service = Notifying([_mailbox()], stop)
        with self.assertLogs(level="INFO") as captured:
            idle.watch_mailbox(service, "usr_1", stop, connect=lambda *a, **k: FakeClient())
        self.assertIn("idle notification for mailbox mbx_1", "\n".join(captured.output))

    def test_a_timeout_without_a_notification_is_not_reported_as_one(self):
        stop = threading.Event()

        class TimedOut(FakeService):
            def poll_mailbox(self, mailbox):
                self.polls.append(1)
                if len(self.polls) >= 2:
                    self.stop.set()
                return 0

        class QuietClient(FakeClient):
            def idle(self, duration=None):
                self.idle_calls += 1
                return FakeIdler([])

        service = TimedOut([_mailbox()], stop)
        with self.assertLogs(level="INFO") as captured:
            idle.watch_mailbox(service, "usr_1", stop, connect=lambda *a, **k: QuietClient())
        self.assertNotIn("idle notification", "\n".join(captured.output))

    def test_the_cursor_is_re_read_before_each_poll(self):
        """A stale snapshot would re-download every mail since the watcher
        started, because the poller advances last_uid behind its back."""
        seen = []

        class Recording(FakeService):
            def poll_mailbox(self, mailbox):
                seen.append(mailbox["last_uid"])
                if len(seen) >= 2:
                    self.stop.set()
                return 0

        stop = threading.Event()
        service = Recording([_mailbox(last_uid=0)], stop)
        service.db.rows["usr_1"]["last_uid"] = 42  # "the poller moved on"

        class PausingClient(FakeClient):
            def idle(self, duration=None):
                self.idle_calls += 1
                return FakeIdler([])  # timeout, no notification

        with capture_problems() as problems:
            idle.watch_mailbox(service, "usr_1", stop, connect=lambda *a, **k: PausingClient())
        self.assertEqual(seen, [42, 42])
        self.assertEqual(_messages(problems), [], "a healthy watch cycle must log nothing")

    def test_a_server_without_idle_is_left_to_the_poller(self):
        stop = threading.Event()
        service = FakeService([_mailbox()], stop)
        client = FakeClient(capabilities=("IMAP4REV1",))
        idle.watch_mailbox(service, "usr_1", stop, connect=lambda *a, **k: client)
        self.assertEqual(service.polls, [], "must not poll; the plain poller owns this mailbox")
        self.assertTrue(client.logged_out)

    def test_a_disabled_mailbox_is_not_watched(self):
        stop = threading.Event()
        service = FakeService([_mailbox(enabled=False)], stop)
        idle.watch_mailbox(service, "usr_1", stop, connect=lambda *a, **k: FakeClient())
        self.assertEqual(service.polls, [])

    def test_a_deleted_mailbox_is_not_watched(self):
        stop = threading.Event()
        service = FakeService([], stop)
        idle.watch_mailbox(service, "usr_1", stop, connect=lambda *a, **k: FakeClient())
        self.assertEqual(service.polls, [])

    def test_a_failing_connection_retries_and_never_raises(self):
        attempts = []

        def flaky(mailbox, password, **kwargs):
            attempts.append(mailbox["id"])
            if len(attempts) == 1:
                raise OSError("connection reset")
            return FakeClient()

        stop = threading.Event()
        service = FakeService([_mailbox()], stop)
        with mock.patch.object(idle, "RECONNECT_SECONDS", 0.01):
            with capture_problems() as problems:
                idle.watch_mailbox(service, "usr_1", stop, connect=flaky)
        self.assertGreaterEqual(len(attempts), 2, "a failed connect must be retried")
        self.assertEqual(len(service.polls), 1)
        # A refused connection is expected and must stay a warning; anything at
        # ERROR level would mean a bug, not a flaky network.
        errors = [r for r in problems.records
                  if r.levelno >= logging.ERROR and getattr(r, "thread", None) == problems.origin]
        self.assertEqual([r.getMessage() for r in errors], [])

    def test_it_gives_up_once_stopped(self):
        stop = threading.Event()
        stop.set()
        service = FakeService([_mailbox()], stop)
        idle.watch_mailbox(service, "usr_1", stop, connect=lambda *a, **k: FakeClient())
        self.assertEqual(service.polls, [])

    def test_a_poll_failure_does_not_kill_the_watcher(self):
        """The fetch path opens its own connection, so its failure says nothing
        about the IDLE link; the watcher must keep waiting."""
        stop = threading.Event()

        class Failing(FakeService):
            def poll_mailbox(self, mailbox):
                self.polls.append(1)
                if len(self.polls) >= 2:
                    self.stop.set()
                raise RuntimeError("fetch blew up")

        service = Failing([_mailbox()], stop)
        idle.watch_mailbox(service, "usr_1", stop, connect=lambda *a, **k: FakeClient())
        self.assertGreaterEqual(len(service.polls), 2)


class SupervisorTests(unittest.TestCase):
    def test_nothing_starts_when_idle_is_unavailable(self):
        stop = threading.Event()
        service = FakeService([_mailbox()], stop)
        with mock.patch.object(idle, "idle_supported", lambda: False):
            self.assertIsNone(idle.start_supervisor(service, stop))
        self.assertEqual(service.polls, [])

    def test_nothing_starts_when_idle_is_switched_off(self):
        stop = threading.Event()
        service = FakeService([_mailbox()], stop)
        with mock.patch.object(idle, "IDLE_ENABLED", False):
            self.assertIsNone(idle.start_supervisor(service, stop))
        self.assertEqual(service.polls, [])

    def test_one_watcher_per_active_mailbox_and_stopped_when_it_disappears(self):
        stop = threading.Event()
        service = FakeService([_mailbox("mbx_1", user_id="usr_1"),
                               _mailbox("mbx_2", user_id="usr_2")], None, new_messages=0)
        clients: dict = {}

        def connect(mailbox, password, **kwargs):
            client = FakeClient()
            clients[mailbox["id"]] = client
            return client

        with mock.patch.object(idle, "enabled", lambda: True), \
                mock.patch.object(idle, "_connect", connect), \
                mock.patch.object(idle, "RECONCILE_SECONDS", 0.02), \
                mock.patch.object(idle, "RECONNECT_SECONDS", 0.02):
            supervisor = idle.start_supervisor(service, stop)
            self.assertIsNotNone(supervisor)
            deadline = time.time() + 5
            while time.time() < deadline and len(clients) < 2:
                time.sleep(0.01)
            self.assertEqual(sorted(clients), ["mbx_1", "mbx_2"],
                             "each active mailbox gets its own watcher")

            # Removing an account must stop its watcher rather than leak it.
            service.db.remove("usr_2")
            deadline = time.time() + 5
            while time.time() < deadline and not clients["mbx_2"].logged_out:
                time.sleep(0.01)
            self.assertTrue(clients["mbx_2"].logged_out,
                            "a removed mailbox's watcher must be shut down")
            # Joined inside the patch: once RECONCILE_SECONDS is restored to its
            # real value the supervisor would sleep for that long before it
            # noticed the stop signal, and outlive this test.
            stop.set()
            supervisor.join(timeout=5)
            self.assertFalse(supervisor.is_alive())

    def test_shutting_the_supervisor_down_shuts_its_watchers_down(self):
        """The leak this pins down: the supervisor used to exit on the stop
        signal without releasing its watchers, so they kept running (and kept
        polling a database that was being torn down) after shutdown."""
        stop = threading.Event()
        service = FakeService([_mailbox("mbx_1", user_id="usr_1")], None, new_messages=0)
        clients: dict = {}

        def connect(mailbox, password, **kwargs):
            client = FakeClient(hold=0.05)
            clients[mailbox["id"]] = client
            return client

        with mock.patch.object(idle, "enabled", lambda: True), \
                mock.patch.object(idle, "_connect", connect), \
                mock.patch.object(idle, "RECONCILE_SECONDS", 0.02), \
                mock.patch.object(idle, "RECONNECT_SECONDS", 0.02):
            supervisor = idle.start_supervisor(service, stop)
            deadline = time.time() + 5
            while time.time() < deadline and not clients:
                time.sleep(0.01)
            self.assertTrue(clients, "the watcher should have connected")
            stop.set()
            supervisor.join(timeout=5)
            self.assertFalse(supervisor.is_alive())
            deadline = time.time() + 5
            while time.time() < deadline and not clients["mbx_1"].logged_out:
                time.sleep(0.01)
            self.assertTrue(clients["mbx_1"].logged_out,
                            "a watcher must not outlive the supervisor")

    def test_the_supervisor_survives_a_database_error(self):
        stop = threading.Event()

        class Broken:
            def active_mailboxes(self):
                raise RuntimeError("database is gone")

        service = FakeService([], stop)
        service.db = Broken()
        with mock.patch.object(idle, "enabled", lambda: True), \
                mock.patch.object(idle, "RECONCILE_SECONDS", 0.02):
            supervisor = idle.start_supervisor(service, stop)
            self.assertIsNotNone(supervisor)
            time.sleep(0.1)
            self.assertTrue(supervisor.is_alive(), "a listing failure must not kill the supervisor")
            stop.set()
            supervisor.join(timeout=5)
            self.assertFalse(supervisor.is_alive())


class RealDatabaseTests(unittest.TestCase):
    """The watcher against a real database row, so the column names it reads
    (``enabled``, ``last_uid``, ``imap_host``) are actually checked."""

    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.work.name) / "pilot.sqlite3")
        self.db.initialize()
        self.secrets = SecretBox(b"7" * 32)

    def tearDown(self):
        self.work.cleanup()

    def _service(self, stop):
        class Service:
            pass

        invite = self.db.create_invite("u", 1)
        user = self.db.create_user("u@example.com", hash_password("a-long-enough-password"),
                                   token_hash(invite))
        self.db.upsert_mailbox(user["id"], {
            "email": "box@qq.com", "report_to": "u@example.com",
            "imap_host": "imap.qq.com", "imap_port": 993,
            "smtp_host": "smtp.qq.com", "smtp_port": 465,
            "encrypted_password": self.secrets.encrypt("pw", context=f"mailbox:{user['id']}"),
        })
        service = Service()
        service.db = self.db
        service.secrets = self.secrets
        service.polls = []

        def poll_mailbox(mailbox):
            service.polls.append(mailbox["id"])
            stop.set()
            return 1

        service.poll_mailbox = poll_mailbox

        def mailbox_password(mailbox):
            return self.secrets.decrypt(mailbox["encrypted_password"],
                                        context=f"mailbox:{mailbox['user_id']}")

        service.mailbox_password = mailbox_password
        active = self.db.active_mailboxes()
        self.assertEqual(len(active), 1, "the fixture must produce exactly one watchable mailbox")
        return service, active[0]

    def test_watching_a_real_mailbox_row_polls_it(self):
        stop = threading.Event()
        service, mailbox = self._service(stop)
        user_id = mailbox["user_id"]
        mailbox_id = mailbox["id"]

        def connect(row, password, **kwargs):
            self.assertEqual(password, "pw", "the watcher must decrypt the real credential")
            self.assertEqual(row["imap_host"], "imap.qq.com")
            self.assertEqual(row["id"], mailbox_id)
            return FakeClient()

        with capture_problems() as problems:
            idle.watch_mailbox(service, user_id, stop, connect=connect)
        self.assertEqual(service.polls, [mailbox_id],
                         "the watcher must fetch through the ordinary poll path")
        self.assertEqual(_messages(problems), [], "a healthy watch cycle must log nothing")


if __name__ == "__main__":
    unittest.main()
