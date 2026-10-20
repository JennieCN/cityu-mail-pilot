"""Tests for the nginx access-log reader.

The two lines everything else builds on are real, copied out of the production
server's access log: a scanner and an iPhone hitting the landing page. The rest
are the shapes a rotated log actually contains -- a query string, nginx's ``-``
for "there was nothing here", an IPv6 client, an error page a proxy wrote into
the file, a truncated line, a gzip file.

Most of this is about one thing: the timestamp. The bracket holds local time with
an offset, and the module's job at that point is to emit the UTC instant. An
eight-hour error would move every imported visit and still look like a working
import, which is why the conversion is asserted to the second.
"""

from __future__ import annotations

import gzip
import pathlib
import tempfile
import unittest

from pilot_app import nginxlog

# Copied from production, byte for byte.
LINE_SCANNER = (
    '192.0.2.44 - - [16/Sep/2026:00:09:35 +0800] "GET / HTTP/1.1" 200 24130 '
    '"-" "odin-scanner/0.4"'
)
LINE_IPHONE = (
    '198.51.100.9 - - [16/Sep/2026:00:10:04 +0800] "GET / HTTP/1.1" 400 264 '
    '"-" "Mozilla/5.0 (iPhone; CPU iPhone OS 13_2_3 like Mac OS X) AppleWebKit/605.1.15 '
    '(KHTML, like Gecko) Version/13.0.3 Mobile/15E148 Safari/604.1"'
)

# What a proxy writes into the log when it cannot reach the app.
HTML_ERROR_PAGE = "\n".join([
    "<!DOCTYPE html>",
    "<html><head><title>502 Bad Gateway</title></head>",
    "<body><center><h1>502 Bad Gateway</h1></center><hr><center>nginx</center></body>",
    "</html>",
])

# Everything here has to come back as None rather than as a record or an error.
UNPARSEABLE = [
    "",
    "   ",
    "<!DOCTYPE html>",
    '192.0.2.44 - - [16/Sep/2026:00:09:35 +0800] "GET / HT',
    '192.0.2.44 - - [16/Sep/2026:00:09:35 +0800] "GET / HTTP/1.1" 200',
    '192.0.2.44 - - [16/Sep/2026:00:09:35 +0800] "GET / HTTP/1.1" 200 5 "-"',
    '192.0.2.44 - - [16/Foo/2026:00:09:35 +0800] "GET / HTTP/1.1" 200 5 "-" "-"',
    '192.0.2.44 - - [31/Feb/2026:00:09:35 +0800] "GET / HTTP/1.1" 200 5 "-" "-"',
    '192.0.2.44 - - [16/Sep/2026:24:09:35 +0800] "GET / HTTP/1.1" 200 5 "-" "-"',
    "GET / HTTP/1.1",
]


def log_line(
    *,
    ip: str = "203.0.113.9",
    when: str = "16/Sep/2026:00:00:00 +0000",
    request: str = "GET / HTTP/1.1",
    status: str = "200",
    size: str = "512",
    referrer: str = "-",
    agent: str = "curl/8.0",
) -> str:
    """Build one combined-format line. The defaults are the boring case."""
    return f'{ip} - - [{when}] "{request}" {status} {size} "{referrer}" "{agent}"'


class ParseLineTests(unittest.TestCase):
    def test_the_scanner_line_converts_plus_0800_to_utc(self):
        """00:09:35 on the 16th at +0800 is 16:09:35 on the 15th UTC.

        Every other field is a copy; this one is a conversion, and it is the one
        that would silently move every imported visit if it were done as a string
        edit instead of arithmetic on the offset.
        """
        self.assertEqual(nginxlog.parse_line(LINE_SCANNER), {
            "ip": "192.0.2.44",
            "time": "2026-09-15T16:09:35+00:00",
            "method": "GET",
            "path": "/",
            "status": 200,
            "bytes": 24130,
            "referrer": "",
            "user_agent": "odin-scanner/0.4",
        })

    def test_the_iphone_line_parses_field_by_field(self):
        record = nginxlog.parse_line(LINE_IPHONE)
        self.assertIsNotNone(record)
        self.assertEqual(record["ip"], "198.51.100.9")
        self.assertEqual(record["time"], "2026-09-15T16:10:04+00:00")
        self.assertEqual(record["method"], "GET")
        self.assertEqual(record["path"], "/")
        self.assertEqual(record["status"], 400)
        self.assertEqual(record["bytes"], 264)
        self.assertEqual(record["referrer"], "")
        # The user agent has spaces, commas and parentheses in it, so this also
        # proves the field ends at the closing quote and not at the first space.
        self.assertEqual(
            record["user_agent"],
            "Mozilla/5.0 (iPhone; CPU iPhone OS 13_2_3 like Mac OS X) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/13.0.3 Mobile/15E148 Safari/604.1")

    def test_offsets_on_the_other_side_and_with_minutes(self):
        self.assertEqual(
            nginxlog.parse_line(log_line(when="16/Sep/2026:00:09:35 -0700"))["time"],
            "2026-09-16T07:09:35+00:00", "a negative offset moves the instant forward")
        self.assertEqual(
            nginxlog.parse_line(log_line(when="16/Sep/2026:00:09:35 +0530"))["time"],
            "2026-09-15T18:39:35+00:00", "the minutes of the offset count too")

    def test_a_query_string_stays_in_the_path(self):
        record = nginxlog.parse_line(log_line(request="GET /sub/page?utm_source=qq&n=2 HTTP/1.1"))
        self.assertEqual(record["method"], "GET")
        self.assertEqual(record["path"], "/sub/page?utm_source=qq&n=2",
                         "the query string is part of what was requested")
        self.assertEqual(record["status"], 200)

    def test_the_referrer_and_user_agent_are_kept_as_written(self):
        record = nginxlog.parse_line(
            log_line(referrer="https://www.cityu.edu.hk/", agent="Mozilla/5.0 (Macintosh)"))
        self.assertEqual(record["referrer"], "https://www.cityu.edu.hk/")
        self.assertEqual(record["user_agent"], "Mozilla/5.0 (Macintosh)")

    def test_dashes_mean_absent_and_a_dash_byte_count_is_zero(self):
        record = nginxlog.parse_line(log_line(referrer="-", agent="-", size="-"))
        self.assertEqual(record["referrer"], "", "a dash referrer is no referrer")
        self.assertEqual(record["user_agent"], "", "a dash user agent is no user agent")
        self.assertEqual(record["bytes"], 0, "a dash byte count (a 304) is zero bytes")

    def test_an_ipv6_client_parses(self):
        record = nginxlog.parse_line(log_line(ip="2001:db8:85a3::8a2e:370:7334"))
        self.assertEqual(record["ip"], "2001:db8:85a3::8a2e:370:7334")
        mapped = nginxlog.parse_line(log_line(ip="::ffff:192.0.2.44"))
        self.assertEqual(mapped["ip"], "::ffff:192.0.2.44",
                         "an IPv4-mapped address is still just the client field")

    def test_a_malformed_request_line_leaves_the_request_empty(self):
        # nginx writes "-" when a request was too broken to log at all.
        record = nginxlog.parse_line(log_line(request="-"))
        self.assertEqual(record["method"], "")
        self.assertEqual(record["path"], "")
        # A request with no protocol (HTTP/0.9) is still a request.
        record = nginxlog.parse_line(log_line(request="GET /old"))
        self.assertEqual(record["method"], "GET")
        self.assertEqual(record["path"], "/old")

    def test_an_escaped_quote_does_not_end_a_field(self):
        # nginx writes an embedded quote as \", so a bot with a quotation mark in
        # its user agent must not turn the whole line into "unparseable".
        record = nginxlog.parse_line(
            log_line(request='GET /a\\"b?x=1 HTTP/1.1', agent='Mozilla \\"quoted\\"'))
        self.assertIsNotNone(record)
        self.assertEqual(record["path"], '/a\\"b?x=1',
                         "fields come back exactly as nginx wrote them, escapes included")
        self.assertEqual(record["status"], 200)

    def test_garbage_lines_are_none_instead_of_raising(self):
        for line in UNPARSEABLE:
            with self.subTest(line=line):
                self.assertIsNone(nginxlog.parse_line(line), f"unparseable: {line!r}")
        # The same lines, read out of a file the way iter_records reads them.
        for line in UNPARSEABLE:
            with self.subTest(line=line, source="file line"):
                self.assertIsNone(nginxlog.parse_line(line + "\n"))
                self.assertIsNone(nginxlog.parse_line(line + "\r\n"))

    def test_bytes_are_not_a_line(self):
        # The contract says str; "never raises" has to survive a caller that hands
        # over a slice of a file it decoded some other way.
        self.assertIsNone(nginxlog.parse_line(b'192.0.2.44 - - [16/Sep/2026:00:09:35 +0800]'))
        self.assertIsNone(nginxlog.parse_line(None))


class MonthTableTests(unittest.TestCase):
    """The month must come from the table, not from the process locale."""

    def test_the_table_holds_the_twelve_c_locale_abbreviations(self):
        self.assertEqual(nginxlog.MONTHS["Sep"], 9)
        self.assertEqual(sorted(nginxlog.MONTHS),
                         ["Apr", "Aug", "Dec", "Feb", "Jan", "Jul", "Jun",
                          "Mar", "May", "Nov", "Oct", "Sep"])
        self.assertEqual(sorted(nginxlog.MONTHS.values()), list(range(1, 13)))

    def test_every_abbreviation_is_what_parsing_uses(self):
        # strptime("%b") would resolve "Sep" through LC_TIME, so the test walks the
        # table instead of the calendar: every entry has to come back as that month.
        for name, number in nginxlog.MONTHS.items():
            with self.subTest(month=name):
                record = nginxlog.parse_line(log_line(when=f"05/{name}/2026:12:00:00 +0000"))
                self.assertIsNotNone(record, f"{name} must parse")
                self.assertEqual(record["time"][:7], f"2026-{number:02d}",
                                 f"{name} is month {number}")

    def test_the_table_is_the_only_month_source(self):
        """Rename September in the table and the parse has to follow it.

        A month read through ``strptime("%b")`` would ignore this edit; the parse
        following it is what "the table is what we read" means. Nothing here
        changes the process locale -- not depending on it is the whole point.
        """
        original = dict(nginxlog.MONTHS)
        try:
            nginxlog.MONTHS["Sep"] = 12
            record = nginxlog.parse_line(LINE_SCANNER)
        finally:
            nginxlog.MONTHS.clear()
            nginxlog.MONTHS.update(original)
        self.assertIsNotNone(record)
        self.assertEqual(record["time"][:7], "2026-12")


class LogFileTestCase(unittest.TestCase):
    """A throwaway directory to write log files into."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = pathlib.Path(self.directory.name)

    def write(self, name: str, text: str, *, gz: bool = False) -> str:
        path = self.root / name
        if gz:
            with gzip.open(path, "wb") as handle:
                handle.write(text.encode("utf-8"))
        else:
            path.write_text(text, encoding="utf-8")
        return str(path)


class IterRecordsTests(LogFileTestCase):
    def test_a_bad_line_is_counted_and_the_good_ones_around_it_still_come(self):
        # One import has to survive all of these at once: an HTML error page in
        # the middle, an empty line, and a line cut off mid-write by logrotate.
        path = self.write("access.log", "\n".join(
            [LINE_SCANNER] + HTML_ERROR_PAGE.splitlines() + ["", LINE_IPHONE, LINE_IPHONE[:40]]) + "\n")
        stats: dict = {}
        records = list(nginxlog.iter_records([path], stats=stats))
        self.assertEqual([record["time"] for record in records],
                         ["2026-09-15T16:09:35+00:00", "2026-09-15T16:10:04+00:00"])
        self.assertEqual(stats["files"], 1)
        self.assertEqual(stats["lines"], 8)
        self.assertEqual(stats["parsed"], 2)
        self.assertEqual(stats["skipped"], 6)
        self.assertEqual(stats["lines"], stats["parsed"] + stats["skipped"])

    def test_a_gzipped_file_yields_the_same_records_as_the_plain_one(self):
        text = "\n".join([LINE_SCANNER, LINE_IPHONE]) + "\n"
        plain = self.write("access.log", text)
        packed = self.write("access.log.1.gz", text, gz=True)
        # Without this the test would also pass if the fixture had been written
        # uncompressed, which would prove nothing about gzip.
        self.assertEqual(pathlib.Path(packed).read_bytes()[:2], b"\x1f\x8b",
                         "the fixture has to really be gzip")
        self.assertEqual(list(nginxlog.iter_records([packed])),
                         list(nginxlog.iter_records([plain])))

    def test_the_content_decides_whether_a_file_is_gzipped(self):
        # Two halves of the same rule: logrotate can leave a compressed file under
        # a plain name, and a file named .gz may be plain (a hand-fixed log). The
        # magic bytes are what count.
        text = LINE_SCANNER + "\n"
        misnamed_plain = self.write("access.log.1.gz", text)
        misnamed_gzip = self.write("access.log.2", text, gz=True)
        for path in (misnamed_plain, misnamed_gzip):
            with self.subTest(path=path):
                records = list(nginxlog.iter_records([path]))
                self.assertEqual([record["time"] for record in records],
                                 ["2026-09-15T16:09:35+00:00"])

    def test_since_and_until_are_inclusive_bounds(self):
        path = self.write("access.log", "\n".join([
            log_line(when="14/Sep/2026:00:00:00 +0000"),
            log_line(when="15/Sep/2026:00:00:00 +0000"),
            log_line(when="16/Sep/2026:00:00:00 +0000"),
        ]) + "\n")

        def days(**window):
            return [record["time"][:10] for record in nginxlog.iter_records([path], **window)]

        self.assertEqual(days(), ["2026-09-14", "2026-09-15", "2026-09-16"])
        self.assertEqual(days(since="2026-09-15"), ["2026-09-15", "2026-09-16"])
        self.assertEqual(days(until="2026-09-15"), ["2026-09-14", "2026-09-15"])
        self.assertEqual(days(since="2026-09-15", until="2026-09-15"), ["2026-09-15"],
                         "both bounds include the day they name")
        self.assertEqual(days(since="2026-09-16", until="2026-09-14"), [])
        self.assertEqual(days(since="2026-10-01"), [])

    def test_the_window_is_applied_to_the_utc_date_not_the_log_date(self):
        """The production line is written on the 16th and happens on the 15th.

        Filtering on what the bracket says instead of on the converted instant is
        the same eight-hour mistake, moved from the stored time to the window, and
        the caller would see "no visits on the 15th" with the visits right there.
        """
        path = self.write("access.log", LINE_SCANNER + "\n")
        self.assertEqual(list(nginxlog.iter_records([path], since="2026-09-16")), [])
        self.assertEqual([record["time"] for record in
                          nginxlog.iter_records([path], since="2026-09-15")],
                         ["2026-09-15T16:09:35+00:00"])

    def test_a_filtered_out_record_still_counts_as_parsed(self):
        # Lines the window drops were read fine; counting them as "skipped" would
        # report a healthy log as full of garbage.
        path = self.write("access.log", "\n".join([
            log_line(when="14/Sep/2026:00:00:00 +0000"),
            log_line(when="16/Sep/2026:00:00:00 +0000"),
        ]) + "\n")
        stats: dict = {}
        records = list(nginxlog.iter_records([path], since="2026-09-16", stats=stats))
        self.assertEqual(len(records), 1)
        self.assertEqual(stats["parsed"], 2)
        self.assertEqual(stats["skipped"], 0)
        self.assertEqual(stats["oldest"], "2026-09-16T00:00:00+00:00",
                         "oldest/newest describe what was yielded, not what was read")

    def test_limit_keeps_the_oldest_records_and_stops_reading(self):
        first = self.write("access.log.2.gz", "\n".join([
            log_line(when="14/Sep/2026:00:00:00 +0000"),
            log_line(when="15/Sep/2026:00:00:00 +0000"),
        ]) + "\n", gz=True)
        second = self.write("access.log.1", "\n".join([
            log_line(when="16/Sep/2026:00:00:00 +0000"),
            log_line(when="17/Sep/2026:00:00:00 +0000"),
        ]) + "\n")
        stats: dict = {}
        records = list(nginxlog.iter_records([first, second], limit=2, stats=stats))
        self.assertEqual([record["time"][:10] for record in records],
                         ["2026-09-14", "2026-09-15"], "a limit keeps the old end")
        self.assertEqual(stats["files"], 1, "after the cap the next file is not opened")
        self.assertEqual(stats["lines"], 2, "no line past the cap is read")
        self.assertEqual(len(list(nginxlog.iter_records([first, second], limit=99))), 4,
                         "a limit larger than the log is not a limit")
        self.assertEqual(len(list(nginxlog.iter_records([first, second], limit=0))), 4,
                         "zero means no cap")

    def test_a_logrotate_set_is_read_oldest_first_whatever_order_it_arrives_in(self):
        """logrotate numbers a log as it ages, and lexicographic order disagrees.

        ``access.log`` sorts *before* ``access.log.1``, so a plain sort reads the
        live file first and inverts the documented "oldest first" -- with a limit
        that means importing the newest records while claiming to import the
        oldest. The paths are handed over in the wrong order on purpose here.
        """
        live = self.write("access.log", log_line(when="16/Sep/2026:00:00:00 +0000") + "\n")
        one = self.write("access.log.1", log_line(when="15/Sep/2026:00:00:00 +0000") + "\n")
        two = self.write("access.log.2.gz", log_line(when="14/Sep/2026:00:00:00 +0000") + "\n", gz=True)
        ten = self.write("access.log.10.gz", log_line(when="13/Sep/2026:00:00:00 +0000") + "\n", gz=True)
        stats: dict = {}
        records = list(nginxlog.iter_records([live, ten, two, one], stats=stats))
        self.assertEqual([record["time"][:10] for record in records],
                         ["2026-09-13", "2026-09-14", "2026-09-15", "2026-09-16"],
                         "10 is older than 2, and the live file is the newest")
        self.assertEqual(stats["files"], 4)

    def test_stats_report_the_files_lines_parsed_skipped_and_the_range(self):
        older = self.write("access.log.2.gz", "\n".join([
            log_line(when="14/Sep/2026:23:00:00 +0000"),
            log_line(when="15/Sep/2026:16:09:35 +0000"),
        ]) + "\n", gz=True)
        newer = self.write("access.log.1", "\n".join([
            LINE_SCANNER, "not a log line", LINE_IPHONE,
        ]) + "\n")
        stats: dict = {}
        records = list(nginxlog.iter_records([newer, older], stats=stats))
        self.assertEqual(len(records), 4)
        self.assertEqual(stats, {
            "files": 2,
            "lines": 5,
            "parsed": 4,
            "skipped": 1,
            "oldest": "2026-09-14T23:00:00+00:00",
            "newest": "2026-09-15T16:10:04+00:00",
        })

    def test_a_pathlib_glob_result_and_a_lone_path_both_work(self):
        # ``Path.glob`` hands over Path objects, and a caller with one file has
        # every reason to pass just that. ``sorted("access.log")`` would walk the
        # characters of the string and import nothing at all, quietly.
        self.write("access.log.1", LINE_SCANNER + "\n")
        self.write("access.log", LINE_IPHONE + "\n")
        globbed = sorted(self.root.glob("access.log*"))
        self.assertEqual([record["time"] for record in nginxlog.iter_records(globbed)],
                         ["2026-09-15T16:09:35+00:00", "2026-09-15T16:10:04+00:00"])
        self.assertEqual([record["ip"] for record in
                          nginxlog.iter_records(str(self.root / "access.log.1"))],
                         ["192.0.2.44"])
        self.assertEqual(len(list(nginxlog.iter_records(self.root / "access.log.1"))), 1)

    def test_a_path_that_cannot_be_read_is_skipped_not_raised_on(self):
        # logrotate can move a file between the caller's glob and this read, and a
        # directory can be globbed by mistake; either way the other files still import.
        good = self.write("access.log.1", LINE_SCANNER + "\n")
        missing = str(self.root / "access.log.2.gz")
        stats: dict = {}
        records = list(nginxlog.iter_records([missing, self.root, good], stats=stats))
        self.assertEqual([record["time"] for record in records],
                         ["2026-09-15T16:09:35+00:00"])
        self.assertEqual(stats["files"], 1, "a path that never opened is not a file read")

    def test_a_corrupt_gzip_file_ends_that_file_and_not_the_import(self):
        # A rotated file that was still being written, or whose middle was
        # clobbered, breaks in one of three ways (BadGzipFile, EOFError, a raw
        # zlib.error) depending on where it broke. All three mean the same thing.
        corrupt = self.root / "access.log.3.gz"
        corrupt.write_bytes(b"\x1f\x8b" + b"\xff" * 20)
        good = self.write("access.log.1", LINE_SCANNER + "\n")
        stats: dict = {}
        records = list(nginxlog.iter_records([str(corrupt), good], stats=stats))
        self.assertEqual([record["time"] for record in records],
                         ["2026-09-15T16:09:35+00:00"],
                         "the readable file after the corrupt one still imports")
        self.assertEqual(stats["files"], 2, "the corrupt file opened, then ended")
        self.assertEqual(stats["lines"], 1)


if __name__ == "__main__":
    unittest.main()
