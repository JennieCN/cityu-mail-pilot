"""Tests for the admin server-metrics panel.

Two things need proving here. First, the numbers: the collectors read Linux
``/proc``, so the tests point them at a written-out fixture tree and check the
arithmetic (a CPU percentage from two counter samples, a network rate, memory
totals) rather than just "it returned a dict". Second, the boundary: this is the
most detailed view of the server in the product, so it must be admin-only and
must not start leaking secrets or per-user mail content.
"""

from __future__ import annotations


import datetime as dt
import http.cookiejar
import json
import os
import pathlib
import sqlite3
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

_TMP = tempfile.mkdtemp()
os.environ["INFE_PILOT_DB"] = _TMP + "/metrics.sqlite3"
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
os.environ["INFE_PILOT_COOKIE_SECURE"] = "0"
os.environ["INFE_PILOT_MAX_USERS"] = "50"
os.environ["INFE_PILOT_ADMIN_EMAILS"] = "boss@example.com"
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import manage as manage_mod  # noqa: E402
from pilot_app import metrics as metrics_mod  # noqa: E402
from pilot_app import web  # noqa: E402
from pilot_app.security import hash_password, token_hash  # noqa: E402
from pilot_app.tests import admin_fixture  # noqa: E402
from pilot_app.web import db  # noqa: E402


class Client:
    def __init__(self, base: str) -> None:
        self.base = base
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), 
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, method: str, path: str, payload=None):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(request, timeout=20) as response:
                return response.status, json.loads(response.read().decode() or "{}")
        except urllib.error.HTTPError as error:
            raw = error.read().decode()
            try:
                return error.code, json.loads(raw or "{}")
            except json.JSONDecodeError:
                return error.code, {"detail": raw}

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, payload=None):
        return self.request("POST", path, payload)


class FakeProcTests(unittest.TestCase):
    """The Linux path, driven from a fixture tree instead of the real machine."""

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.real_proc = metrics_mod.PROC
        metrics_mod.PROC = str(self.root)
        metrics_mod._PREVIOUS.clear()
        self.write("stat", "cpu  100 0 100 800 0 0 0 0 0 0\ncpu0 100 0 100 800 0 0 0 0 0 0\n")
        self.write("meminfo", "\n".join([
            "MemTotal:        2000000 kB",
            "MemFree:          200000 kB",
            "MemAvailable:     500000 kB",
            "SwapTotal:        400000 kB",
            "SwapFree:         100000 kB",
        ]) + "\n")
        self.write("uptime", "123456.78 999.0\n")
        self.write("net/dev", "\n".join([
            "Inter-|   Receive                                                |  Transmit",
            " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed",
            "    lo: 9999999 0 0 0 0 0 0 0 9999999 0 0 0 0 0 0 0",
            "  eth0: 1000000 0 0 0 0 0 0 0 2000000 0 0 0 0 0 0 0",
        ]) + "\n")
        self.write("self/statm", "1000 250 50 1 0 100 0\n")
        self.write("self/status", "Name:\tpython\nThreads:\t7\n")

    def tearDown(self):
        metrics_mod.PROC = self.real_proc
        metrics_mod._PREVIOUS.clear()

    def write(self, relative: str, text: str) -> None:
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def test_cpu_percent_comes_from_the_counter_delta(self):
        # Pretend a sample was taken one second ago: 1000 total jiffies of which
        # 800 were idle, versus 1200/900 now. The delta is 200 total with 100
        # idle, i.e. half the time was busy.
        # 1200 total / 900 idle one second ago, 1400 / 1000 now: 200 more jiffies
        # of which 100 were idle, so the CPU was busy half of that interval.
        metrics_mod._PREVIOUS["cpu"] = (time.monotonic() - 1.0, (1200, 900))
        self.write("stat", "cpu  200 0 200 1000 0 0 0 0 0 0\n")
        result = metrics_mod.host_metrics()
        self.assertAlmostEqual(result["cpu_percent"], 50.0, delta=1.0)

    def test_first_ever_sample_still_produces_a_number(self):
        # /proc only holds counters, so the very first poll has nothing to diff
        # against; it takes its own second reading rather than showing an empty
        # panel. This is also the branch that must not divide by zero.
        readings = [(1000, 800), (1100, 850)]
        calls = {"n": 0}

        def sampler():
            value = readings[min(calls["n"], len(readings) - 1)]
            calls["n"] += 1
            return value

        rate = metrics_mod._rate("first", sampler, sampler())
        self.assertIsNotNone(rate)
        total_per_second, idle_per_second = rate
        self.assertGreater(total_per_second, 0)
        self.assertAlmostEqual(idle_per_second / total_per_second, 0.5, delta=0.1)
        metrics_mod._PREVIOUS.clear()

    def test_memory_totals_and_swap(self):
        memory = metrics_mod.host_metrics()["memory"]
        self.assertEqual(memory["total_mb"], 1953.1)
        self.assertEqual(memory["available_mb"], 488.3)
        self.assertEqual(memory["used_mb"], round((2_000_000 - 500_000) / 1024, 1))
        self.assertAlmostEqual(memory["percent"], 75.0, delta=0.2)
        self.assertAlmostEqual(memory["swap_percent"], 75.0, delta=0.2)

    def test_network_rate_ignores_loopback(self):
        # Pretend the previous sample is a second old, otherwise the min-window
        # guard (correctly) refuses to divide a tiny delta by a tiny interval.
        # The CPU baseline is set too: without it host_metrics would spend its
        # first-sample window sleeping and stretch the network interval.
        metrics_mod._PREVIOUS["net"] = (time.monotonic() - 1.0, (1_000_000, 2_000_000))
        metrics_mod._PREVIOUS["cpu"] = (time.monotonic() - 1.0, (1000, 800))
        self.write("net/dev", "\n".join([
            "Inter-|   Receive                                                |  Transmit",
            " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed",
            "    lo: 9999999 0 0 0 0 0 0 0 9999999 0 0 0 0 0 0 0",
            "  eth0: 2024000 0 0 0 0 0 0 0 4048000 0 0 0 0 0 0 0",
        ]) + "\n")
        result = metrics_mod.host_metrics()
        self.assertIsNotNone(result["network"]["rx_kbps"])
        self.assertAlmostEqual(result["network"]["rx_kbps"], (2_024_000 - 1_000_000) / 1024, delta=60)
        self.assertNotIn("9999999", json.dumps(result))
        metrics_mod._PREVIOUS.clear()

    def test_two_calls_in_quick_succession_do_not_invent_a_number(self):
        # Hitting the endpoint twice within milliseconds would otherwise divide a
        # near-zero counter delta by a near-zero interval and report nonsense.
        metrics_mod._PREVIOUS["cpu"] = (time.monotonic(), (1000, 800))
        self.write("stat", "cpu  300 0 300 1300 0 0 0 0 0 0\n")
        self.assertIsNone(metrics_mod.host_metrics()["cpu_percent"])
        # ...and the old baseline is kept, so the next spaced-out call is fine.
        stamp, counters = metrics_mod._PREVIOUS["cpu"]
        self.assertEqual(counters, (1000, 800))
        metrics_mod._PREVIOUS.clear()

    def test_an_idle_machine_reports_zero_not_null(self):
        metrics_mod._PREVIOUS["cpu"] = (time.monotonic() - 1.0, (1000, 800))
        self.write("stat", "cpu  0 0 0 0 0 0 0 0 0 0\n")
        self.assertEqual(metrics_mod.host_metrics()["cpu_percent"], 0.0)
        metrics_mod._PREVIOUS.clear()

    def test_uptime_and_process_readings(self):
        host = metrics_mod.host_metrics()
        self.assertEqual(host["uptime_seconds"], 123456.8)
        process = metrics_mod.collect()["process"]
        self.assertEqual(process["threads"], 7)
        self.assertIsInstance(process["rss_mb"], float)

    def test_a_missing_proc_tree_yields_nulls_instead_of_raising(self):
        metrics_mod.PROC = str(self.root / "nope")
        host = metrics_mod.host_metrics()
        self.assertIsNone(host["cpu_percent"])
        self.assertIsNone(host["memory"]["total_mb"])
        self.assertIsNone(host["uptime_seconds"])
        self.assertIsNone(host["network"]["rx_kbps"])
        self.assertEqual(host["disk"]["total_gb"] > 0, True)  # shutil still works

    def test_one_unparseable_line_does_not_blank_the_whole_panel(self):
        """A single odd line used to take out CPU, disk and uptime with it.

        Found by asking what the panel does with input nobody has seen yet: a
        kernel that grows a new /proc/meminfo field, or a container that mounts
        something odd there. The old code raised out of ``host_metrics``, the
        caller stored ``{"error": ...}``, and the operator got a host panel with
        *one* error in it instead of five readings and one dash -- which cannot
        be told apart from "the server is fine, there is just nothing to show".
        """
        self.write("meminfo", "\n".join([
            "MemTotal:        2000000 kB",
            "SomethingNew:    not-a-number kB",
            "MemAvailable:     500000 kB",
        ]) + "\n")
        host = metrics_mod.host_metrics()
        self.assertEqual(host["memory"]["total_mb"], 1953.1)   # parsed anyway
        self.assertEqual(host["memory"]["available_mb"], 488.3)
        self.assertEqual(host["uptime_seconds"], 123456.8)      # unrelated, intact
        self.assertGreater(host["disk"]["total_gb"], 0)
        self.assertNotIn("error", metrics_mod.collect()["host"])

    def test_a_cpu_line_the_kernel_grew_a_field_into_is_skipped(self):
        self.write("stat", "\n".join([
            "cpu  who knows",
            "cpu  200 0 200 1000 0 0 0 0 0 0",
        ]) + "\n")
        self.assertEqual(metrics_mod._cpu_times(), (1400, 1000))
        # Fewer than the four guaranteed fields is not a reading either.
        self.write("stat", "cpu  100 0 100\n")
        self.assertIsNone(metrics_mod._cpu_times())

    def test_a_network_line_with_a_non_numeric_column_is_skipped(self):
        self.write("net/dev", "\n".join([
            "Inter-|   Receive                                                |  Transmit",
            " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed",
            "  eth0: n/a n/a n/a n/a n/a n/a n/a n/a n/a n/a n/a n/a n/a n/a n/a n/a",
            "  eth1: 1000000 0 0 0 0 0 0 0 2000000 0 0 0 0 0 0 0",
        ]) + "\n")
        self.assertEqual(metrics_mod._network_bytes(), (1_000_000, 2_000_000))

    def test_one_broken_reading_does_not_blank_the_others(self):
        """The last layer: an unforeseeable failure costs one field, not the panel."""
        def explode():
            raise RuntimeError("this /proc layout has never been seen")

        original = metrics_mod._memory
        metrics_mod._memory = explode
        try:
            host = metrics_mod.host_metrics()
        finally:
            metrics_mod._memory = original
        self.assertIsNone(host["memory"]["total_mb"])          # the broken one
        self.assertEqual(host["uptime_seconds"], 123456.8)      # the rest are fine
        self.assertGreater(host["disk"]["total_gb"], 0)
        self.assertEqual(host["cpu_count"], metrics_mod.os.cpu_count())


class ApplicationMetricsTests(unittest.TestCase):
    def setUp(self):
        self.path = pathlib.Path(tempfile.mkdtemp()) / "app.sqlite3"
        self.database = db.__class__(self.path)
        self.database.initialize()
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row

    def tearDown(self):
        self.connection.close()

    def _seed(self, *, received: str, sent: str | None, message_status="sent", report_status="sent"):
        self.connection.execute(
            """INSERT INTO users(id,email,password_hash,status,created_at)
               VALUES('u1','u@example.com','x','active','2026-01-01T00:00:00+00:00')""")
        self.connection.execute(
            """INSERT INTO mailboxes(id,user_id,email,report_to,imap_host,imap_port,smtp_host,smtp_port,
                                    encrypted_password,last_polled_at,updated_at)
               VALUES('m1','u1','b@q.com','b@q.com','imap.qq.com',993,'smtp.qq.com',465,X'00',?,
                      '2026-01-01T00:00:00+00:00')""", (received,))
        self.connection.execute(
            """INSERT INTO messages(id,user_id,mailbox_id,imap_uid,subject,received_at,body,status,created_at)
               VALUES('msg1','u1','m1',1,'subject',?,X'00',?,'2026-01-01T00:00:00+00:00')""",
            (received, message_status))
        if report_status:
            self.connection.execute(
                """INSERT INTO reports(id,user_id,message_id,kind,subject,body_markdown,status,created_at,sent_at)
                   VALUES('r1','u1','msg1','immediate','s',X'00',?,?,?)""",
                (report_status, received, sent))
        self.connection.commit()

    def test_pipeline_counts_and_latency(self):
        now = dt.datetime.now(dt.timezone.utc)
        received = (now - dt.timedelta(minutes=10)).isoformat(timespec="seconds")
        sent = (now - dt.timedelta(minutes=8)).isoformat(timespec="seconds")  # 120 s later
        self._seed(received=received, sent=sent)
        result = metrics_mod.application_metrics(self.connection)
        self.assertEqual(result["messages_1h"], 1)
        self.assertEqual(result["messages_24h"], 1)
        self.assertEqual(result["sent_24h"], 1)
        self.assertEqual(result["queue"], 0)
        self.assertEqual(result["latency_samples"], 1)
        self.assertAlmostEqual(result["average_latency_seconds"], 120.0, delta=2.0)
        self.assertEqual(result["last_poll_at"], received)
        self.assertIsNotNone(result["database_size_mb"])

    def test_old_mail_is_outside_the_windows(self):
        now = dt.datetime.now(dt.timezone.utc)
        old = (now - dt.timedelta(days=3)).isoformat(timespec="seconds")
        self._seed(received=old, sent=old)
        result = metrics_mod.application_metrics(self.connection)
        self.assertEqual(result["messages_1h"], 0)
        self.assertEqual(result["messages_24h"], 0)
        self.assertEqual(result["sent_24h"], 0)
        self.assertIsNone(result["average_latency_seconds"])

    def test_queue_and_failure_counts(self):
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        self._seed(received=now, sent=None, message_status="pending", report_status=None)
        result = metrics_mod.application_metrics(self.connection)
        self.assertEqual(result["queue"], 1)
        self.assertEqual(result["failed"], 0)

    def test_backlog_does_not_hide_the_typical_latency_in_the_mean(self):
        """Real data: eleven messages sat unprocessed for two days and were then
        sent within twenty minutes. The mean said "22 hours" (correct, useless);
        the median has to keep saying "a few minutes"."""
        for index, seconds in enumerate([255, 275, 284, 294, 300, 310, 320, 330, 340, 350]):
            received = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds + 60)).isoformat(timespec="seconds")
            sent = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=60)).isoformat(timespec="seconds")
            self.connection.execute(
                """INSERT INTO messages(id,user_id,mailbox_id,imap_uid,subject,received_at,body,status,created_at)
                   VALUES(?, 'u1','m1',?, 's', ?, X'00','sent','2026-01-01T00:00:00+00:00')""",
                (f"msg{index}", 100 + index, received))
            self.connection.execute(
                """INSERT INTO reports(id,user_id,message_id,kind,subject,body_markdown,status,created_at,sent_at)
                   VALUES(?, 'u1', ?, 'immediate','s',X'00','sent',?,?)""",
                (f"r{index}", f"msg{index}", received, sent))
        self.connection.execute(
            """INSERT INTO messages(id,user_id,mailbox_id,imap_uid,subject,received_at,body,status,created_at)
               VALUES('msgold','u1','m1',999,'backlog',?,X'00','sent','2026-01-01T00:00:00+00:00')""",
            ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=23)).isoformat(timespec="seconds"),))
        self.connection.execute(
            """INSERT INTO reports(id,user_id,message_id,kind,subject,body_markdown,status,created_at,sent_at)
               VALUES('rold','u1','msgold','immediate','s',X'00','sent',?,?)""",
            ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=23)).isoformat(timespec="seconds"),
             dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))
        self.connection.commit()

        result = metrics_mod.application_metrics(self.connection)
        self.assertEqual(result["latency_samples"], 11)
        self.assertGreater(result["average_latency_seconds"], 7000, "平均值会被积压拉高")
        self.assertLess(result["median_latency_seconds"], 400, "中位数必须反映常态")
        self.assertGreater(result["p90_latency_seconds"], result["median_latency_seconds"])


class AdminMetricsEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = web.create_server("127.0.0.1", 0)
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)

    def setUp(self):
        db.initialize()
        with db.connect() as connection:
            for table in ("announcement_deliveries", "announcement_dismissals", "announcements",
                          "feedback", "reports", "messages", "mailboxes", "connections",
                          "sessions", "invites", "profiles", "users"):
                connection.execute(f"DELETE FROM {table}")
        self.stamp = dt.datetime.now().timestamp()

    def _register(self, email: str) -> Client:
        invite = db.create_invite(f"metrics-{email}-{self.stamp}", 1)
        client = Client(self.base)
        web.reset_signup_rate_limit()  # 见 web.reset_signup_rate_limit：限速按 IP，单测得自己清
        status, body = client.post("/api/auth/register", {
            "email": email, "password": "a-long-enough-password", "invite_code": invite, "accepted_terms": True})
        self.assertEqual(status, 200, body)
        return client

    def _admin(self) -> Client:
        """保留地址走「建号 + 授权」（见 admin_fixture），不走开放注册。"""
        return admin_fixture.admin_session(db, Client(self.base), "boss@example.com")

    def test_operator_gets_the_snapshot(self):
        client = self._admin()
        status, body = client.get("/api/admin/metrics")
        self.assertEqual(status, 200, body)
        for key in ("collected_at", "host", "process", "application", "service"):
            self.assertIn(key, body)
        self.assertIn("cpu_percent", body["host"])
        self.assertIn("messages_24h", body["application"])
        self.assertIn("users", body["service"])

    def test_ordinary_user_and_anonymous_are_refused(self):
        self._register("member@example.com")
        member = Client(self.base)
        member.post("/api/auth/login", {"email": "member@example.com", "password": "a-long-enough-password"})
        status, _ = member.get("/api/admin/metrics")
        self.assertEqual(status, 404, "非管理员必须 404，不能暴露这个接口存在")
        status, _ = Client(self.base).get("/api/admin/metrics")
        self.assertEqual(status, 401)

    def test_snapshot_never_contains_secrets_or_mail_content(self):
        client = self._admin()
        status, body = client.get("/api/admin/metrics")
        self.assertEqual(status, 200)
        blob = json.dumps(body)
        for forbidden in ("encrypted_", "password", "api_key", "token", "cipher"):
            self.assertNotIn(forbidden, blob)
        # It reports the machine, not the mail: no subject/body fields at all.
        self.assertNotIn("subject", blob)
        self.assertNotIn("body_markdown", blob)


class CheckMetricsCommandTests(unittest.TestCase):
    """``manage check-metrics`` is the Linux half of the metrics_check suite.

    The browser suite skips three assertions on macOS, so those readings have to
    be checked somewhere that has /proc. These tests pin the command's own
    contract: it says "fine" only when every required reading is present, and it
    says which ones are missing when they are not -- a green result that cannot
    go red is not evidence.
    """

    def setUp(self):
        self.root = pathlib.Path(tempfile.mkdtemp())
        self.real_proc = metrics_mod.PROC
        metrics_mod.PROC = str(self.root)
        metrics_mod._PREVIOUS.clear()
        self.write("stat", "cpu  100 0 100 800 0 0 0 0 0 0\n")
        self.write("meminfo", "MemTotal: 2000000 kB\nMemAvailable: 500000 kB\nSwapTotal: 0 kB\n")
        self.write("uptime", "4242.5 9.0\n")
        self.write("net/dev", "\n".join([
            "Inter-|   Receive                                                |  Transmit",
            " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed",
            "  eth0: 1000000 0 0 0 0 0 0 0 2000000 0 0 0 0 0 0 0",
        ]) + "\n")
        self.write("self/statm", "1000 250 50 1 0 100 0\n")
        self.write("self/status", "Name:\tpython\nThreads:\t5\n")
        self.db = db.__class__(pathlib.Path(tempfile.mkdtemp()) / "app.sqlite3")
        self.db.initialize()
        # A baseline one second old, so the rate branch reports a number instead
        # of spending its first-sample window measuring nothing.
        metrics_mod._PREVIOUS["cpu"] = (time.monotonic() - 1.0, (1000, 800))
        metrics_mod._PREVIOUS["net"] = (time.monotonic() - 1.0, (1_000_000, 2_000_000))

    def tearDown(self):
        metrics_mod.PROC = self.real_proc
        metrics_mod._PREVIOUS.clear()

    def write(self, relative: str, text: str) -> None:
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    def run_command(self):
        import contextlib
        import io
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = manage_mod.check_metrics(self.db)
        return code, buffer.getvalue()

    def test_a_complete_proc_tree_passes_and_prints_the_readings(self):
        metrics_mod._PREVIOUS["cpu"] = (time.monotonic() - 1.0, (1000, 800))
        code, output = self.run_command()
        self.assertEqual(code, 0, output)
        self.assertIn("✓", output)
        self.assertIn("1953.1", output)          # memory total, from the fixture
        self.assertIn("4242.5", output)          # uptime
        self.assertIn("CPU", output)

    def test_a_missing_proc_tree_fails_and_names_what_is_missing(self):
        metrics_mod.PROC = str(self.root / "nope")
        code, output = self.run_command()
        self.assertEqual(code, 1, output)
        self.assertIn("不合格", output)
        for field in ("host.cpu_percent", "host.memory.total_mb", "process.rss_mb"):
            self.assertIn(field, output)

    def test_an_unparseable_meminfo_line_is_not_reported_as_a_missing_reading(self):
        """The two failures are different and must read differently.

        "the kernel said something we could not parse" and "there is no /proc
        here" both end up as a dash in the panel, but only one of them is a bug
        on this host. The command has to be able to tell the operator which.
        """
        self.write("meminfo", "MemTotal: 2000000 kB\nSomethingNew: n/a kB\nMemAvailable: 500000 kB\n")
        code, output = self.run_command()
        self.assertEqual(code, 0, output)
        self.assertIn("1953.1", output)


if __name__ == "__main__":
    unittest.main()
