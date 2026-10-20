"""Tests for the offline country lookup.

The lookup sits on the request path, so most of what needs proving is what it
does when it *cannot* answer: a private address, a gap in the data, a database
that was never built, a file that is not a database at all. All of those must be
"unknown", because the alternative is a 500 on a page a real person asked for.

The rest is the build: it must stream 700k rows without holding them in memory,
skip a broken line instead of dying, and never leave a half-written file where a
lookup could read wrong answers out of it.
"""

from __future__ import annotations

import gzip
import os
import sqlite3
import tempfile
import unittest

from pilot_app import geoip


def _open_descriptors() -> int | None:
    """How many descriptors this process holds, or None where that is unreadable."""
    for root in ("/proc/self/fd", "/dev/fd"):
        try:
            return len(os.listdir(root))
        except OSError:
            continue
    return None


class BuildAndLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = self.temporary.name

    def _write(self, name: str, text: str, *, gzipped: bool = False) -> str:
        path = os.path.join(self.root, name)
        if gzipped:
            with gzip.open(path, "wt", encoding="utf-8") as handle:
                handle.write(text)
        else:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(text)
        return path

    def _build(self, text: str, *, dataset: str = "country", gzipped: bool = False) -> str:
        source = self._write("src.csv", text, gzipped=gzipped)
        target = os.path.join(self.root, "geoip.sqlite3")
        geoip.build(source, "", target, dataset=dataset)
        return target

    def test_country_lookup_hits_both_families_and_boundaries(self) -> None:
        target = self._build(
            "8.8.8.0,8.8.8.127,AU\n"
            "8.8.8.128,8.8.8.191,CN\n"
            "2000::,2000:ffff:ffff:ffff:ffff:ffff:ffff:ffff,CH\n"
        )
        self.assertEqual(geoip.lookup("8.8.8.0", target)["country"], "AU")     # first address
        self.assertEqual(geoip.lookup("8.8.8.127", target)["country"], "AU")   # last address
        self.assertEqual(geoip.lookup("8.8.8.150", target)["country"], "CN")     # inside the second
        self.assertEqual(geoip.lookup("2000::1", target)["country"], "CH")
        self.assertEqual(geoip.lookup("2000::1", target)["country_name"], "瑞士")

    def test_gap_between_ranges_is_unknown(self) -> None:
        # The closest start is 8.8.8.0 but its range ends at 8.8.8.63, so an
        # address in the hole must not inherit the country below it. This is the
        # failure mode the "verify end >= address in Python" step exists for.
        target = self._build("8.8.8.0,8.8.8.63,AU\n8.8.8.192,8.8.8.255,CN\n")
        self.assertEqual(geoip.lookup("8.8.8.100", target), {})
        self.assertEqual(geoip.lookup("8.8.8.63", target)["country"], "AU")
        self.assertEqual(geoip.lookup("8.8.8.192", target)["country"], "CN")

    def test_private_reserved_and_junk_are_unknown(self) -> None:
        target = self._build("8.8.8.0,8.8.8.255,ZZ\n")
        for address in ("10.0.0.1", "192.168.1.1", "127.0.0.1", "::1", "192.0.2.1"):
            with self.subTest(address=address):
                self.assertEqual(geoip.lookup(address, target), {})
        for address in ("not-an-ip", "", "8.8.8.8.9"):
            with self.subTest(address=address):
                self.assertEqual(geoip.lookup(address, target), {})

    def test_missing_database_is_not_an_error(self) -> None:
        missing = os.path.join(self.root, "nope.sqlite3")
        self.assertFalse(geoip.available(missing))
        self.assertEqual(geoip.lookup("8.8.8.8", missing), {})

    def test_corrupt_database_is_not_an_error(self) -> None:
        # A truncated download or an unrelated file must degrade to "unknown".
        # Raising here would take out the page the visitor asked for.
        broken = self._write("broken.sqlite3", "this is not a database at all")
        self.assertFalse(geoip.available(broken))
        self.assertEqual(geoip.lookup("8.8.8.8", broken), {})

    def test_zz_becomes_a_word_not_a_country_code(self) -> None:
        # An ordinary range that DB-IP could not place. (a reserved block is
        # reserved, so it never reaches the table at all -- the private
        # filter answers first, which is the same answer.)
        target = self._build("8.8.8.0,8.8.8.127,ZZ\n")
        self.assertEqual(geoip.lookup("8.8.8.5", target)["country_name"], "未知")

    def test_country_name_falls_back_to_the_code(self) -> None:
        self.assertEqual(geoip.country_name("CN"), "中国")
        self.assertEqual(geoip.country_name("cn"), "中国")
        self.assertEqual(geoip.country_name("ZZ"), "未知")
        self.assertEqual(geoip.country_name(""), "未知")
        # A code the table does not know is shown as-is: an unfamiliar two
        # letter code beats an invented country name.
        self.assertEqual(geoip.country_name("QQ"), "QQ")

    def test_city_dataset_keeps_region_and_city(self) -> None:
        target = self._build(
            '8.8.8.0,8.8.8.127,OC,AU,Queensland,"South Brisbane",-27.4767,153.017\n',
            dataset="city",
        )
        row = geoip.lookup("8.8.8.5", target)
        self.assertEqual(row["country"], "AU")
        self.assertEqual(row["city"], "Queensland · South Brisbane")
        self.assertEqual(row["continent"], "OC")

    def test_gzipped_input_is_read(self) -> None:
        target = self._build("8.8.8.0,8.8.8.127,AU\n", gzipped=True)
        self.assertEqual(geoip.lookup("8.8.8.5", target)["country"], "AU")

    def test_broken_rows_are_skipped_not_fatal(self) -> None:
        target = self._build(
            "8.8.8.0,8.8.8.63,AU\n"
            "not-an-ip,8.8.8.255,CN\n"
            "8.8.8.97\n"
            "\n"
            "8.8.8.192,8.8.8.223,NZ\n"
        )
        self.assertEqual(geoip.lookup("8.8.8.5", target)["country"], "AU")
        self.assertEqual(geoip.lookup("8.8.8.200", target)["country"], "NZ")
        # Only the malformed line claimed .255, so nothing covers it.
        self.assertEqual(geoip.lookup("8.8.8.255", target), {})

    def test_build_reports_counts_and_leaves_no_temp_file(self) -> None:
        source = self._write("src.csv", "8.8.8.0,8.8.8.127,AU\n2000::,2000::ff,CH\n")
        target = os.path.join(self.root, "built.sqlite3")
        counts = geoip.build(source, "", target, dataset="country")
        self.assertEqual(counts["rows"], 2)
        self.assertEqual(counts["v4"], 1)
        self.assertEqual(counts["v6"], 1)
        self.assertTrue(os.path.isfile(target))
        self.assertFalse(os.path.exists(target + ".building"))

    def test_failed_build_leaves_no_database_behind(self) -> None:
        # The caller can only trust "no file" as "the build did not happen" if a
        # failure really removes what it wrote.
        source = self._write("src.csv", "8.8.8.0,8.8.8.127,AU\n")
        target = os.path.join(self.root, "nested", "missing", "built.sqlite3")
        with self.assertRaises(Exception):
            geoip.build(source, "", target)
        self.assertFalse(os.path.exists(target + ".building"))

    def test_unknown_dataset_is_refused(self) -> None:
        source = self._write("src.csv", "8.8.8.0,8.8.8.127,AU\n")
        with self.assertRaises(ValueError):
            geoip.build(source, "", os.path.join(self.root, "x.sqlite3"), dataset="planet")

    def test_fetch_url_shape(self) -> None:
        self.assertEqual(
            geoip.fetch_url("country", "2026-09"),
            "https://download.db-ip.com/free/dbip-country-lite-2026-09.csv.gz",
        )
        self.assertEqual(
            geoip.fetch_url("city", "2026-01"),
            "https://download.db-ip.com/free/dbip-city-lite-2026-01.csv.gz",
        )
        with self.assertRaises(ValueError):
            geoip.fetch_url("planet", "2026-09")
        with self.assertRaises(ValueError):
            geoip.fetch_url("country", "September")

    def test_default_path_prefers_the_environment(self) -> None:
        previous = os.environ.get(geoip.DEFAULT_PATH_ENV)
        os.environ[geoip.DEFAULT_PATH_ENV] = "/tmp/custom-geoip.sqlite3"
        try:
            self.assertEqual(geoip.default_path(), "/tmp/custom-geoip.sqlite3")
        finally:
            if previous is None:
                os.environ.pop(geoip.DEFAULT_PATH_ENV, None)
            else:
                os.environ[geoip.DEFAULT_PATH_ENV] = previous
        self.assertEqual(geoip.default_path(), geoip.DEFAULT_PATH)

    def test_lookup_does_not_create_the_database(self) -> None:
        # Opening a missing SQLite path in write mode creates an empty file,
        # which would then look "available" forever. Read-only mode is what
        # stops a lookup from having that side effect.
        missing = os.path.join(self.root, "never-built.sqlite3")
        geoip.lookup("8.8.8.8", missing)
        self.assertFalse(os.path.exists(missing))

    def test_every_lookup_closes_its_connection(self) -> None:
        """The deterministic version of the leak test below.

        Counting descriptors is not enough on its own: on CPython a leaked
        connection is usually collected by refcounting a moment later, so this
        machine showed a growth of *one* descriptor for 400 leaking calls while
        the production box -- Python 3.14, pinned at 1024 -- ran out after about
        two thousand. Holding a strong reference to every connection the module
        opens removes refcounting from the picture: if the code relied on the
        garbage collector, these are still open and the assertion fails here,
        on every platform, instead of on the server a day later.
        """
        target = self._build("8.8.8.0,8.8.8.127,AU\n")
        recorded: list = []
        real_connect = geoip.sqlite3.connect

        def spy(*args, **kwargs):
            connection = real_connect(*args, **kwargs)
            recorded.append(connection)
            return connection

        geoip.sqlite3.connect = spy
        try:
            for _ in range(5):
                self.assertEqual(geoip.lookup("8.8.8.8", target)["country"], "AU")
        finally:
            geoip.sqlite3.connect = real_connect
        self.assertTrue(recorded, "查询应当真的打开过连接")
        for index, connection in enumerate(recorded):
            with self.assertRaises(sqlite3.ProgrammingError,
                                   msg=f"第 {index} 个连接没有关（不是靠 gc 收的）"):
                connection.execute("SELECT 1")

    def test_lookups_do_not_leak_file_descriptors(self) -> None:
        """The bug this pins nearly took the web process down.

        `with sqlite3.connect(...)` commits, it does not close, so the first
        version of lookup() held a descriptor open per call. Two thousand
        lookups exhausted the default limit of 1024 and every later database
        open failed -- on the request path, that is a web process that stops
        answering after about a thousand page views. It surfaced as a 1,919-row
        log import that stored only 586 rows.
        """
        target = self._build("8.8.8.0,8.8.8.127,AU\n")
        before = _open_descriptors()
        if before is None:  # pragma: no cover - no /proc and no /dev/fd
            self.skipTest("这个平台数不了打开的文件描述符")
        for _ in range(400):
            geoip.lookup("8.8.8.8", target)
        after = _open_descriptors()
        self.assertLess(after - before, 20,
                        f"400 次查询后文件描述符从 {before} 涨到 {after}（连接没有关）")

    def test_built_database_has_an_index_on_start(self) -> None:
        # Without it every page view scans 700k rows on a 2-core box.
        target = self._build("8.8.8.0,8.8.8.127,AU\n")
        with sqlite3.connect(target) as connection:
            names = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='ranges'")]
        self.assertTrue(any("start" in name for name in names), names)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
