"""Read nginx ``access.log`` files so earlier visits can be imported.

The app counts visits as they happen; this module exists for the history from
before it started counting. nginx has been writing a ``combined``-format line for
every request all along, so the past is sitting in ``/var/log/nginx/access.log``
and its rotated, gzipped ancestors, and it can be replayed into the visitor
statistics instead of being lost.

Four decisions shape everything below.

**One bad line must not cost the other 99,999.** A log is not a data file we
control. A proxy writes an HTML error page into it, logrotate hands us a file
that is still being appended to, an encoding gets mangled, a clock is wrong. The
import is therefore a filter, not a validator: a line that cannot be read is
counted and stepped over, and the caller decides what to say about the count.

**Time is converted here, once.** The bracketed field is the server's *local*
time with its offset (``+0800`` on our box). Records leave as UTC ISO-8601 with
an explicit offset -- the same shape the rest of the app stores -- so nothing
downstream has to remember which zone a log was written in. Getting this wrong
shifts every imported visit by eight hours, which is the class of bug this
project has been bitten by before, and it would still look like a successful
import.

**The month comes from a table, never from ``strptime("%b")``.** ``%b`` resolves
through the process locale, so on a box that is not set to C the same line either
fails or resolves to another month -- a wrong date on every row rather than an
exception. :data:`MONTHS` is the C-locale table nginx itself writes.

**Nothing here writes, opens a socket, or keeps state.** It is a parser:
:func:`parse_line` for one line, :func:`iter_records` for a set of files.
"""

from __future__ import annotations

import datetime as dt
import gzip
import io
import os
import re
import zlib
from typing import Any, Iterator

# The month abbreviations nginx writes, which are the C locale's. Spelled out
# because strptime("%b") resolves through LC_TIME; see the module docstring.
MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

# A rotated log is recognised by its content, not by its name: logrotate renames
# and compresses independently, and a file that lost its ".gz" while being moved
# should still import.
GZIP_MAGIC = b"\x1f\x8b"

# One quoted field of a combined line. nginx escapes an embedded quote as \" (and
# a non-printable byte as \xXX), so a field ends at the first quote that is not
# escaped -- without this, one bot with a quotation mark in its user agent would
# turn a whole line into "unparseable".
_FIELD = r'(?:[^"\\]|\\.)*'

# The default "combined" log format, field by field:
#   192.0.2.44 - - [16/Sep/2026:00:09:35 +0800] "GET / HTTP/1.1" 200 24130 "-" "curl/8"
# The two identity fields after the address are matched and dropped: they are "-"
# in every log this app produces and nothing here uses them. The pattern is
# anchored at the start and deliberately open at the end, so a vhost that appends
# extra fields ($request_time and friends) still imports.
LINE_RE = re.compile(
    rf"^(?P<ip>\S+) \S+ \S+ "
    rf"\[(?P<stamp>[^\]]+)\] "
    rf'"(?P<request>{_FIELD})" '
    rf"(?P<status>\S+) (?P<bytes>\S+) "
    rf'"(?P<referrer>{_FIELD})" '
    rf'"(?P<user_agent>{_FIELD})"'
)

# 16/Sep/2026:00:09:35 +0800. The day may be zero-padded or space-padded
# depending on the server, and the offset is required: the default format always
# writes it, and without it the instant is ambiguous, so such a line is better
# skipped than guessed at.
STAMP_RE = re.compile(
    r"^(?P<day>\d{1,2})/(?P<month>[A-Za-z]{3})/(?P<year>\d{4}):"
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2}) "
    r"(?P<offset>[+-]\d{4})$"
)


def _int(value: str) -> int:
    """A numeric field as an int; anything else -- nginx's ``-`` -- counts as 0.

    A byte count of ``-`` is legal output (a 304 has no body), and a status that
    is not a number is a broken line whose visit we would still rather count than
    discard along with the line.
    """
    try:
        return int(value)
    except ValueError:
        return 0


def _offset(text: str) -> dt.tzinfo:
    """``+0800`` as a tzinfo. The sign applies to the whole offset, not the hours."""
    sign = -1 if text.startswith("-") else 1
    return dt.timezone(sign * dt.timedelta(hours=int(text[1:3]), minutes=int(text[3:5])))


def _utc_time(stamp: str) -> str | None:
    """UTC ISO-8601 for one ``16/Sep/2026:00:09:35 +0800`` stamp, or None.

    The month is looked up in :data:`MONTHS` instead of being parsed by
    ``strptime("%b")``, which would consult the process locale. An abbreviation
    nobody recognises, and a date the calendar does not have (31 February, hour
    25), both make the line unreadable rather than raise: one wrong clock entry in
    a 100k-line log must not stop the import.
    """
    parts = STAMP_RE.match(stamp.strip())
    if parts is None:
        return None
    month = MONTHS.get(parts.group("month"))
    if month is None:
        return None
    try:
        moment = dt.datetime(
            int(parts.group("year")), month, int(parts.group("day")),
            int(parts.group("hour")), int(parts.group("minute")), int(parts.group("second")),
            tzinfo=_offset(parts.group("offset")),
        )
    except ValueError:
        return None
    # The one conversion that matters: local time + offset -> the UTC instant the
    # rest of the app stores. isoformat() writes the "+00:00" explicitly, so a
    # reader can never mistake it for a naive local stamp.
    return moment.astimezone(dt.timezone.utc).isoformat()


def parse_line(line: str) -> dict | None:
    """Parse one combined-format line.

    Returns {"ip": str, "time": str, "method": str, "path": str, "status": int,
             "bytes": int, "referrer": str, "user_agent": str}
    where "time" is UTC ISO-8601 with an explicit +00:00 offset, or None when the
    line cannot be parsed. Never raises.

    Absent data uses nginx's own convention: a ``-`` referrer, user agent or byte
    count becomes ``""``, ``""`` and ``0``, and a ``-`` request line leaves both
    ``method`` and ``path`` empty. The request keeps its query string. Fields come
    back as nginx wrote them, still escaped -- undoing ``\\"`` and ``\\xXX`` would
    be a second parser to get wrong, and nothing here needs the original bytes.
    """
    if not isinstance(line, str):
        # Text is the caller's business: only the caller knows which encoding its
        # files use, and a parser that promises not to raise must not raise here.
        return None
    match = LINE_RE.match(line.strip())
    if match is None:
        return None
    when = _utc_time(match.group("stamp"))
    if when is None:
        return None

    request = match.group("request")
    method = path = ""
    if request and request != "-":
        words = request.split()
        method = words[0] if words else ""
        path = words[1] if len(words) > 1 else ""

    return {
        "ip": match.group("ip"),
        "time": when,
        "method": method,
        "path": path,
        "status": _int(match.group("status")),
        "bytes": _int(match.group("bytes")),
        "referrer": "" if match.group("referrer") == "-" else match.group("referrer"),
        "user_agent": "" if match.group("user_agent") == "-" else match.group("user_agent"),
    }


def _path_order(path) -> tuple:
    """Sort key that reads a logrotate set oldest first.

    logrotate numbers a log as it ages: ``access.log.2.gz`` is older than
    ``access.log.1``, which is older than the live ``access.log``. A plain
    ``sorted()`` puts ``access.log`` first, which would invert the documented
    "oldest first" -- and with ``limit`` set that means importing the newest
    records while claiming to import the oldest. Anything without a rotation
    number (``other.log``, say) keeps plain lexicographic order, so an arbitrary
    set of paths is still read in a deterministic order.

    Everything is compared as text: a caller may hand over ``str`` and
    ``pathlib.Path`` in the same list, and a sort that reached a mixed pair would
    raise -- which is the one thing this module promises not to do.
    """
    text = str(path)
    name = os.path.basename(text)
    if name.endswith(".gz"):
        name = name[:-3]
    stem, _, rotation = name.rpartition(".")
    if stem and rotation.isdigit():
        # A bigger rotation number is further in the past, hence the negative.
        return (stem, 0, -int(rotation), text)
    return (name, 1, 0, text)


def _lines(handle: Any) -> Iterator[str]:
    """Decode the lines of an open binary log handle, gzipped or not.

    The magic bytes decide, not the file name (see :data:`GZIP_MAGIC`).
    Undecodable bytes become U+FFFD instead of raising: a user agent carries
    whatever the client sent, and one bad byte must not end an import.
    """
    head = handle.read(2)
    handle.seek(0)
    stream = gzip.GzipFile(fileobj=handle) if head == GZIP_MAGIC else handle
    with io.TextIOWrapper(stream, encoding="utf-8", errors="replace") as text:
        for line in text:
            yield line


def iter_records(
    paths,
    *,
    since: str = "",
    until: str = "",
    limit: int = 0,
    stats: dict | None = None,
) -> Iterator[dict]:
    """Yield parsed records from the given files, oldest first.

    Accepts plain files and gzipped ones (logrotate writes .gz). ``since``/``until``
    are 'YYYY-MM-DD' (UTC) inclusive bounds compared against the record's UTC
    date; ``limit`` caps the number of yielded records (0 = no cap). When ``stats``
    is a dict it is filled in with {"files": n, "lines": n, "parsed": n,
    "skipped": n, "oldest": iso, "newest": iso} for the caller to report.

    What the caller can rely on, in full:

    * ``lines == parsed + skipped``. ``parsed`` counts every line that produced a
      record, including records the date window then dropped (they parsed fine;
      the window is not an error); ``skipped`` counts lines that produced nothing.
      How many records were actually yielded is the caller's own count.
    * ``oldest``/``newest`` describe the yielded records and are ``""`` when there
      were none. ``files`` counts the files that could be opened.
    * The file order is :func:`_path_order` -- a logrotate set is read oldest
      first -- and the lines of a file stay in file order, which for a log that is
      only ever appended to is time order. A ``limit`` therefore keeps the *old*
      end, and nothing beyond it is read: the next file is not even opened.
    * A path that cannot be opened is skipped rather than raised on. logrotate can
      move a file between the caller's glob and this read, and losing the whole
      import to that race would be worse than losing one file.
    * ``paths`` is any iterable of ``str`` or ``pathlib.Path`` (a ``Path.glob``
      result needs no conversion), and one path on its own also works.
    * ``stats`` is filled in as the generator runs (this module is lazy), so read
      it after the loop, not after the call.
    """
    counters: dict[str, Any] = stats if stats is not None else {}
    counters["files"] = 0
    counters["lines"] = 0
    counters["parsed"] = 0
    counters["skipped"] = 0
    counters["oldest"] = ""
    counters["newest"] = ""

    if isinstance(paths, (str, bytes, os.PathLike)):
        # One path instead of a list of them is a common enough slip, and
        # ``sorted("access.log")`` would quietly walk its characters, find no such
        # files, and report a successful import of nothing.
        paths = [paths]

    # Only the day part takes part in the comparison, so a caller who passes a
    # full ISO timestamp gets the same day rather than an empty import.
    since_day = str(since or "")[:10]
    until_day = str(until or "")[:10]
    cap = int(limit or 0)
    yielded = 0

    for path in sorted(paths, key=_path_order):
        if cap > 0 and yielded >= cap:
            return
        try:
            handle = open(path, "rb")
        except OSError:
            continue
        counters["files"] += 1
        try:
            with handle:
                for line in _lines(handle):
                    if cap > 0 and yielded >= cap:
                        # Checked inside the loop as well, because the cap is
                        # normally reached in the middle of a file.
                        return
                    counters["lines"] += 1
                    record = parse_line(line)
                    if record is None:
                        counters["skipped"] += 1
                        continue
                    counters["parsed"] += 1
                    day = record["time"][:10]
                    if since_day and day < since_day:
                        continue
                    if until_day and day > until_day:
                        continue
                    stamp = record["time"]
                    if not counters["oldest"] or stamp < counters["oldest"]:
                        counters["oldest"] = stamp
                    if stamp > counters["newest"]:
                        counters["newest"] = stamp
                    yielded += 1
                    yield record
        except (OSError, EOFError, zlib.error):
            # A rotated file that is being written while we read it, or one whose
            # middle was clobbered: gzip reports that as BadGzipFile (an OSError),
            # EOFError or a raw zlib.error depending on where it broke, and all
            # three mean the same thing -- this file ends here, the import does not.
            continue
