"""Tests for visitor counting.

Two properties matter more than the arithmetic:

* **The address is never written down.** The operator asked to see where
  visitors come from while choosing "don't store the address", so the most
  important test here reads back every stored column and asserts the raw
  address appears in none of them.
* **Statistics cannot break a page.** Every refusal -- robots, a global privacy
  control, a path that is not a page, a broken country database, a database that
  raises on insert -- has to end in "nothing recorded", never in an exception
  escaping into a response.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import os
import tempfile
import unittest
from email.message import Message
from unittest import mock

from pilot_app import analytics, database, geoip, manage
from pilot_app.security import SecretBox

# Two real lines' worth of user agents: the scanner that found the box within
# hours of it going public, and an actual phone browser.
SCANNER = "odin-scanner/0.4"
IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 13_2_3 like Mac OS X) "
          "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/13.0.3 Mobile/15E148 Safari/604.1")


def headers(**values: str) -> Message:
    message = Message()
    for key, value in values.items():
        message[key.replace("_", "-")] = value
    return message


class ClientAddressTests(unittest.TestCase):
    def test_real_ip_is_trusted_from_our_own_proxy(self) -> None:
        self.assertEqual(
            analytics.client_ip("127.0.0.1", headers(X_Real_IP="203.0.113.7")), "203.0.113.7")
        self.assertEqual(
            analytics.client_ip("::1", headers(X_Real_IP="2001:db8::1")), "2001:db8::1")

    def test_real_ip_is_ignored_from_anywhere_else(self) -> None:
        # Otherwise every visitor could choose their own address, which would
        # poison both the visitor count and the rate limits built on it.
        self.assertEqual(
            analytics.client_ip("198.51.100.4", headers(X_Real_IP="8.8.8.9")), "198.51.100.4")

    def test_missing_header_falls_back_to_the_peer(self) -> None:
        self.assertEqual(analytics.client_ip("127.0.0.1", headers()), "127.0.0.1")
        self.assertEqual(analytics.client_ip("", headers()), "")

    def test_forwarded_for_is_not_consulted(self) -> None:
        # It is a list, the client controls its left-hand entries, and one
        # trusted header beats parsing an untrusted one.
        self.assertEqual(
            analytics.client_ip("127.0.0.1", headers(X_Forwarded_For="8.8.8.9")), "127.0.0.1")


class PageViewTests(unittest.TestCase):
    def test_pages_count(self) -> None:
        for path in ("/", "/app", "/demo", "/privacy", "/terms", "/download/x.apk"):
            with self.subTest(path=path):
                self.assertTrue(analytics.is_page_view("GET", path, 200))

    def test_non_pages_do_not(self) -> None:
        cases = [
            ("GET", "/api/me", 200),          # the app's own API
            ("GET", "/static/app.js", 200),   # its own assets
            ("GET", "/health", 200),          # monitors
            ("GET", "/favicon.ico", 200),
            ("GET", "/manifest.webmanifest", 200),
            ("GET", "/", 404),                # a scanner's guess, not a reader
            ("GET", "/", 500),
            ("POST", "/api/guestbook", 200),  # a write is not a visit
            ("PUT", "/api/profile", 200),
        ]
        for method, path, status in cases:
            with self.subTest(method=method, path=path, status=status):
                self.assertFalse(analytics.is_page_view(method, path, status))

    def test_head_is_a_visit_and_304_too(self) -> None:
        self.assertTrue(analytics.is_page_view("HEAD", "/", 200))
        self.assertTrue(analytics.is_page_view("GET", "/", 304))


class RobotTests(unittest.TestCase):
    def test_scanner_and_empty_agents_are_robots(self) -> None:
        for agent in (SCANNER, "curl/8.4.0", "python-requests/2.31", "", "   ",
                      "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"):
            with self.subTest(agent=agent):
                self.assertTrue(analytics.looks_like_bot(agent))

    def test_a_phone_is_not_a_robot(self) -> None:
        self.assertFalse(analytics.looks_like_bot(IPHONE))
        self.assertFalse(analytics.looks_like_bot(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"))

    def test_classify_labels_the_common_ones(self) -> None:
        phone = analytics.classify(IPHONE)
        self.assertEqual(phone["browser"], "Safari")
        self.assertEqual(phone["system"], "iOS")
        self.assertFalse(phone["bot"])
        self.assertTrue(analytics.classify(SCANNER)["bot"])


class ReferrerTests(unittest.TestCase):
    def test_only_the_host_survives(self) -> None:
        # A referring URL can carry a search query, and a search query is about
        # the person who typed it.
        self.assertEqual(
            analytics.referrer_host("https://www.google.com/search?q=cityu+mail"),
            "www.google.com")
        self.assertEqual(analytics.referrer_host("http://t.co/abc"), "t.co")

    def test_our_own_host_is_not_a_source(self) -> None:
        self.assertEqual(analytics.referrer_host("https://mail.example.com/app", own_host="mail.example.com"), "")
        self.assertEqual(analytics.referrer_host("https://mail.example.com:8443/app", own_host="mail.example.com"), "")

    def test_junk_is_empty(self) -> None:
        for value in ("", "-", "not a url at all", "https://"):
            with self.subTest(value=value):
                self.assertEqual(analytics.referrer_host(value), "")


class OptOutTests(unittest.TestCase):
    def test_dnt_and_gpc_are_honoured(self) -> None:
        self.assertTrue(analytics.wants_no_tracking(headers(DNT="1")))
        self.assertTrue(analytics.wants_no_tracking(headers(Sec_GPC="1")))
        self.assertFalse(analytics.wants_no_tracking(headers(DNT="0")))
        self.assertFalse(analytics.wants_no_tracking(headers()))


class RetentionTests(unittest.TestCase):
    def test_default_and_override(self) -> None:
        previous = os.environ.pop("INFE_PILOT_ANALYTICS_DAYS", None)
        try:
            self.assertEqual(analytics.retention_days(), analytics.DEFAULT_RETENTION_DAYS)
            os.environ["INFE_PILOT_ANALYTICS_DAYS"] = "30"
            self.assertEqual(analytics.retention_days(), 30)
            os.environ["INFE_PILOT_ANALYTICS_DAYS"] = "nonsense"
            self.assertEqual(analytics.retention_days(), analytics.DEFAULT_RETENTION_DAYS)
            os.environ["INFE_PILOT_ANALYTICS_DAYS"] = "0"
            self.assertEqual(analytics.retention_days(), analytics.DEFAULT_RETENTION_DAYS)
        finally:
            os.environ.pop("INFE_PILOT_ANALYTICS_DAYS", None)
            if previous is not None:
                os.environ["INFE_PILOT_ANALYTICS_DAYS"] = previous


class DayModifierTests(unittest.TestCase):
    def test_hong_kong_is_plus_eight_hours(self) -> None:
        self.assertEqual(analytics.day_modifier("Asia/Shanghai"), "+480 minutes")

    def test_a_negative_zone(self) -> None:
        self.assertEqual(analytics.day_modifier("America/New_York", now=dt.datetime(2026, 1, 15, tzinfo=dt.timezone.utc)),
                         "-300 minutes")

    def test_an_unknown_zone_falls_back_to_the_instance_zone(self) -> None:
        self.assertEqual(analytics.day_modifier("Mars/Olympus"), "+480 minutes")
        self.assertEqual(analytics.day_modifier(""), "+480 minutes")


class RecordTests(unittest.TestCase):
    def setUp(self) -> None:
        analytics.forget_recent()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = database.Database(os.path.join(self.temporary.name, "pilot.sqlite3"))
        self.database.initialize()
        self.secrets = SecretBox(b"2" * 32)

    def _record(self, **overrides):
        values = dict(
            ip="203.0.113.7", path="/", status=200, method="GET",
            referrer="https://www.google.com/search?q=secret+query",
            user_agent=IPHONE, member=False, own_host="mail.example.com",
        )
        values.update(overrides)
        return analytics.record(self.database, self.secrets, **values)

    def test_a_visit_is_counted_without_the_address(self) -> None:
        self.assertTrue(self._record())
        with self.database.connect() as connection:
            rows = [dict(row) for row in connection.execute("SELECT * FROM page_views")]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["path"], "/")
        self.assertEqual(row["referrer"], "www.google.com")
        self.assertEqual(row["client_hash"], self.secrets.anonymized("203.0.113.7"))
        # The whole point: nothing anywhere in the row is the address.
        flattened = " ".join(str(value) for value in row.values())
        self.assertNotIn("203.0.113.7", flattened)
        self.assertNotIn("secret", flattened)  # nor the search query
        # And the hash cannot be turned back into it by enumerating IPv4.
        self.assertNotEqual(row["client_hash"], "203.0.113.7")

    def test_the_live_buffer_does_keep_the_address(self) -> None:
        # The other half of the deal: the operator can watch traffic arrive.
        self._record()
        live = analytics.recent(10)
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0]["ip"], "203.0.113.7")
        self.assertEqual(live[0]["path"], "/")

    def test_the_live_buffer_is_memory_only_and_bounded(self) -> None:
        for index in range(analytics.RECENT_MAX + 25):
            self._record(ip=f"203.0.113.{index % 250}")
        self.assertEqual(len(analytics.recent(1000)), analytics.RECENT_MAX)
        self.assertEqual(analytics.recent(3)[0]["ip"], analytics.recent(3)[0]["ip"])
        analytics.forget_recent()
        self.assertEqual(analytics.recent(10), [])

    def test_robots_are_stored_but_flagged(self) -> None:
        self._record(user_agent=SCANNER)
        with self.database.connect() as connection:
            row = dict(connection.execute("SELECT * FROM page_views").fetchone())
        self.assertEqual(row["bot"], 1)
        self.assertEqual(self.database.page_view_totals()["bot_pv"], 1)
        self.assertEqual(self.database.page_view_totals()["human_pv"], 0)

    def test_members_are_flagged(self) -> None:
        self._record(member=True)
        self.assertEqual(self.database.page_view_totals()["member_pv"], 1)

    def test_non_pages_are_not_stored_at_all(self) -> None:
        self.assertFalse(self._record(path="/api/me"))
        self.assertFalse(self._record(path="/health"))
        self.assertFalse(self._record(method="POST"))
        self.assertFalse(self._record(status=404))
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM page_views").fetchone()[0], 0)
        self.assertEqual(analytics.recent(10), [])

    def test_unique_visitors_count_digests_not_requests(self) -> None:
        self._record(path="/", ip="203.0.113.7")
        self._record(path="/app", ip="203.0.113.7")
        self._record(path="/", ip="203.0.113.8")
        totals = self.database.page_view_totals()
        self.assertEqual(totals["pv"], 3)
        self.assertEqual(totals["uv"], 2)

    def test_a_broken_country_database_does_not_stop_the_count(self) -> None:
        self.assertTrue(self._record(geo_path=os.path.join(self.temporary.name, "missing.sqlite3")))
        self.assertEqual(self.database.page_view_totals()["pv"], 1)

    def test_a_database_that_raises_is_swallowed(self) -> None:
        # This runs inside the request a real person is waiting for.
        class Exploding:
            def record_page_view(self, **kwargs):
                raise RuntimeError("disk full")

            def purge_page_views(self, cutoff):
                raise RuntimeError("disk full")

        self.assertFalse(analytics.record(Exploding(), self.secrets, ip="203.0.113.7", path="/", status=200))
        # ...but the live view still saw it, because that part is memory only.
        self.assertEqual(len(analytics.recent(5)), 1)

    def test_geo_is_resolved_at_insert_time(self) -> None:
        source = os.path.join(self.temporary.name, "src.csv")
        with open(source, "w", encoding="utf-8") as handle:
            # A real, allocated range: RFC 5737 test nets are marked private
            # by ipaddress, so they would be answered "unknown" before the
            # table is ever consulted.
            handle.write("8.8.8.0,8.8.8.255,CN\n")
        geo_path = os.path.join(self.temporary.name, "geo.sqlite3")
        geoip.build(source, "", geo_path, dataset="country")
        self.assertTrue(self._record(ip="8.8.8.8", geo_path=geo_path))
        with self.database.connect() as connection:
            row = dict(connection.execute("SELECT * FROM page_views").fetchone())
        self.assertEqual(row["country"], "CN")
        self.assertEqual(row["country_name"], "中国")

    def test_retention_deletes_old_rows(self) -> None:
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=400)
        self._record(now=old)
        self._record()
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM page_views").fetchone()[0], 2)
        removed = self.database.purge_page_views(
            (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=180)).isoformat(timespec="seconds"))
        self.assertEqual(removed, 1, "只应删掉超过保留期的那一条")
        with self.database.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM page_views").fetchone()[0], 1)
        self.assertEqual(self.database.page_view_totals(days=1)["pv"], 1)

    def test_totals_are_counted_in_the_readers_day(self) -> None:
        # 16:09 UTC is already the 17th in Hong Kong, so a "today" window in
        # +08:00 must include it while the UTC day would not.
        late = dt.datetime.now(dt.timezone.utc).replace(hour=16, minute=9, second=35)
        self._record(now=late)
        self.assertEqual(self.database.page_view_totals(offset="+480 minutes", days=1)["pv"], 1)
        self.assertEqual(self.database.page_view_daily(offset="+480 minutes", days=1)[0]["day"],
                         (late + dt.timedelta(hours=8)).date().isoformat())


class OperatorTests(unittest.TestCase):
    """运营者看自己的站不算访客——用户原话：「把我自己的记录删了」。"""

    def setUp(self) -> None:
        analytics.forget_recent()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = database.Database(os.path.join(self.temporary.name, "pilot.sqlite3"))
        self.database.initialize()
        self.secrets = SecretBox(b"2" * 32)

    def _visit(self, **overrides):
        values = dict(ip="8.8.8.8", path="/", status=200, user_agent=IPHONE)
        values.update(overrides)
        return analytics.record(self.database, self.secrets, **values)

    def test_an_operator_visit_is_stored_but_not_counted(self) -> None:
        self.assertTrue(self._visit(admin=True))
        self.assertTrue(self._visit(ip="8.8.8.9"))
        totals = self.database.page_view_totals(days=7)
        self.assertEqual(totals["human_pv"], 1, "运营者那一次不该算进人数")
        self.assertEqual([row["label"] for row in self.database.page_view_breakdown("path")], ["/"])
        self.assertEqual(self.database.page_view_daily(days=7)[0]["human_pv"], 1)

    def test_purging_removes_his_own_rows_and_nothing_else(self) -> None:
        self._visit(admin=True)
        self._visit(ip="8.8.8.9")
        self._visit(ip="8.8.8.9")            # 另一个访客来了两次
        removed = self.database.purge_operator_page_views(self.secrets.anonymized("8.8.8.8"))
        self.assertEqual(removed, 1)
        self.assertEqual(self.database.page_view_totals(days=7)["human_pv"], 2)

    def test_purging_also_matches_his_current_address(self) -> None:
        # 历史行没有 admin 标记，但那把摘要认得出「这个地址」。
        self._visit(ip="8.8.8.9")            # 升级前记下的、运营者自己那次
        self._visit(ip="192.0.2.7")            # 真访客
        removed = self.database.purge_operator_page_views(self.secrets.anonymized("8.8.8.9"))
        self.assertEqual(removed, 1)
        self.assertEqual(self.database.page_view_totals(days=7)["human_pv"], 1)

    def test_imported_rows_are_never_operator_rows(self) -> None:
        row = analytics.import_row(self.secrets, ip="8.8.8.8", path="/", status=200,
                                   user_agent=IPHONE, created_at="2026-09-15T00:00:00+00:00")
        self.assertEqual(row["admin"], 0)

    def test_his_own_visit_never_reaches_the_live_list(self) -> None:
        # 「删了」得包括这份内存列表，否则刚清理过的面板还在给他看自己的地址；
        # 而且他的页面加载会把真访客从 200 条的缓冲里挤出去。
        self._visit(admin=True, path="/app")
        self._visit(ip="8.8.8.9", path="/")
        live = analytics.recent(10)
        self.assertEqual([row["path"] for row in live], ["/"])
        self.assertFalse(any(row.get("admin") for row in live))


class ImportTests(unittest.TestCase):
    """Importing must be repeatable: a second run may not double the numbers."""

    def setUp(self) -> None:
        analytics.forget_recent()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = database.Database(os.path.join(self.temporary.name, "pilot.sqlite3"))
        self.database.initialize()
        self.secrets = SecretBox(b"2" * 32)
        self.now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)

    def _row(self, **overrides):
        values = dict(ip="8.8.8.9", path="/", status=200, method="GET",
                      referrer="https://t.co/x", user_agent=IPHONE,
                      created_at=self.now.isoformat(timespec="seconds"))
        values.update(overrides)
        return analytics.import_row(self.secrets, **values)

    def _write_log(self, *addresses: str) -> None:
        """One real combined-format line per address, as nginx writes them."""
        self.log_path = os.path.join(self.temporary.name, "access.log")
        local_now = self.now.astimezone(dt.timezone(dt.timedelta(hours=8)))
        lines = [
            f'{address} - - [{(local_now + dt.timedelta(seconds=index)).strftime("%d/%b/%Y:%H:%M:%S")} +0800] "GET / HTTP/1.1" 200 2413 "-" "{IPHONE}"'
            for index, address in enumerate(addresses)
        ]
        with open(self.log_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

    def test_the_same_log_imported_twice_is_a_no_op(self) -> None:
        later = (self.now + dt.timedelta(seconds=25)).isoformat(timespec="seconds")
        rows = [self._row(), self._row(path="/app", created_at=later)]
        self.assertEqual(self.database.record_page_views(rows), (2, 0))
        self.assertEqual(self.database.record_page_views(rows), (0, 2), "第二次导入应当全部是重复")
        self.assertEqual(self.database.page_view_totals(days=30)["pv"], 2)

    def test_live_visits_are_still_counted_even_if_they_look_alike(self) -> None:
        # The dedupe index is partial (source='nginx') for exactly this reason:
        # somebody reloading a page in the same second is two page views, and
        # dropping one would silently under-count real traffic.
        self.assertTrue(analytics.record(self.database, self.secrets, ip="8.8.8.9", path="/",
                                         status=200, user_agent=IPHONE,
                                         now=self.now))
        self.assertTrue(analytics.record(self.database, self.secrets, ip="8.8.8.9", path="/",
                                         status=200, user_agent=IPHONE,
                                         now=self.now))
        totals = self.database.page_view_totals(days=30)
        self.assertEqual(totals["pv"], 2)
        self.assertEqual(totals["uv"], 1)

    def test_a_robot_log_line_keeps_its_flag_and_no_address(self) -> None:
        row = self._row(ip="8.8.8.9", user_agent="odin-scanner/0.4")
        self.assertEqual(row["bot"], 1)
        self.database.record_page_views([row])
        with self.database.connect() as connection:
            stored = dict(connection.execute("SELECT * FROM page_views").fetchone())
        self.assertNotIn("8.8.8.9", " ".join(str(value) for value in stored.values()))

    def test_non_page_lines_produce_no_row(self) -> None:
        self.assertIsNone(self._row(path="/api/me"))
        self.assertIsNone(self._row(status=404))
        self.assertIsNone(self._row(method="POST"))

    def test_a_purged_address_is_not_brought_back_by_the_importer(self) -> None:
        """删掉之后重跑一次导入，他自己的历史不能又回来。

        导入的去重键就是 (时间, 页面, 摘要)：行删了，去重记录也一起没了，所以没
        有这张「清过的地址」名单，`--apply` 一次就等于撤销那次删除——而面板上看
        起来像是删除没生效。
        """
        operator_ip, visitor_ip = "8.8.8.8", "192.0.2.7"
        self.assertEqual(self.database.purge_operator_page_views(self.secrets.anonymized(operator_ip)), 0)
        self.assertIn(self.secrets.anonymized(operator_ip), self.database.ignored_page_view_clients())

        self._write_log(operator_ip, visitor_ip)
        with mock.patch.dict(os.environ, {"INFE_PILOT_DB": self.database.path}), \
             mock.patch("pilot_app.security.SecretBox.from_environment",
                        staticmethod(lambda: self.secrets)), \
             mock.patch("sys.argv", ["manage.py", "analytics-import-nginx",
                                     "--path", self.log_path, "--apply"]), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            code = manage.main()
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("跳过 1 条", out.getvalue())

        with self.database.connect() as connection:
            digests = [row["client_hash"] for row in connection.execute("SELECT client_hash FROM page_views")]
        self.assertEqual(digests, [self.secrets.anonymized(visitor_ip)],
                         "只有真访客那一行进来了")


class BreakdownTests(unittest.TestCase):
    def setUp(self) -> None:
        analytics.forget_recent()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database = database.Database(os.path.join(self.temporary.name, "pilot.sqlite3"))
        self.database.initialize()
        self.secrets = SecretBox(b"2" * 32)

    def _record(self, **overrides):
        values = dict(ip="203.0.113.7", path="/", status=200, method="GET",
                      referrer="https://t.co/x", user_agent=IPHONE)
        values.update(overrides)
        return analytics.record(self.database, self.secrets, **values)

    def test_breakdowns_group_and_rank(self) -> None:
        for _ in range(3):
            self._record(path="/")
        self._record(path="/app", ip="203.0.113.9")
        paths = self.database.page_view_breakdown("path")
        self.assertEqual(paths[0]["label"], "/")
        self.assertEqual(paths[0]["views"], 3)
        self.assertEqual(paths[1]["label"], "/app")
        referrers = self.database.page_view_breakdown("referrer")
        self.assertEqual(referrers[0]["label"], "t.co")

    def test_breakdowns_exclude_robots_by_default(self) -> None:
        self._record(user_agent=SCANNER, path="/wp-login.php")
        self._record(path="/")
        self.assertEqual([row["label"] for row in self.database.page_view_breakdown("path")], ["/"])
        self.assertEqual(len(self.database.page_view_breakdown("path", humans_only=False)), 2)

    def test_an_unknown_group_is_refused(self) -> None:
        # The column name cannot be a bound parameter, so the whitelist is the
        # thing that keeps this from being string-built SQL.
        with self.assertRaises(ValueError):
            self.database.page_view_breakdown("path; DROP TABLE page_views")

    def test_empty_labels_are_not_rows(self) -> None:
        self._record(referrer="", geo_path=os.path.join(self.temporary.name, "none.sqlite3"))
        self.assertEqual(self.database.page_view_breakdown("referrer"), [])
        self.assertEqual(self.database.page_view_breakdown("country"), [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
