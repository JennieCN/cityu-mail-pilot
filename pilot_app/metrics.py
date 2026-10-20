"""Server and application metrics for the admin console.

Standard library only, like the rest of the runtime. On Linux (which is what the
pilot runs on) the numbers come straight from ``/proc``; anywhere else the parts
that need it degrade to ``None`` instead of failing, so the panel still renders
in local development on macOS.

Two design choices worth knowing:

* **CPU and network are rates, not readings.** ``/proc`` only exposes monotonic
  counters, so a percentage needs two samples. The first call takes its own
  second sample ~60 ms later; every call after that diffs against the previous
  one, which means the browser's poll interval is also the averaging window
  (3 s of load, not "since boot").
* **Nothing here spawns a process.** No ``psutil``, no ``systemctl``, no
  ``df``/``free`` — a metrics panel must not be able to hang or fail because an
  external command is missing, and the web service runs as an unprivileged user.
"""

from __future__ import annotations

import datetime as dt
import math
import os
import platform
import shutil
import sys
import threading
import time
from typing import Any, Optional

PROC = "/proc"
MODULE_START = time.monotonic()

_LOCK = threading.Lock()
_PREVIOUS: dict[str, Any] = {}


# ---------------------------------------------------------------------------
# /proc readers
# ---------------------------------------------------------------------------
def _read(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _safe(default, reader, *args, **kwargs):
    """Run one reader; an unforeseen failure degrades one reading, not the panel.

    The parsers below already skip lines they cannot read, and ``_read`` turns a
    missing file into ``None``. This is the last layer: ``/proc`` is a kernel
    interface that varies by version, architecture and container, and the lesson
    from ``/var/backups`` applies here too -- one unreadable source must not blank
    the whole panel, because a panel of dashes cannot be told apart from a server
    with nothing wrong with it.
    """
    try:
        return reader(*args, **kwargs)
    except Exception:  # pragma: no cover - defensive, exercised by a fixture test
        return default


def _cpu_times() -> Optional[tuple[int, int]]:
    """(total_jiffies, idle_jiffies) from /proc/stat, or None off Linux.

    A line this process cannot parse is skipped rather than raised on: the kernel
    has added fields to ``cpu`` before and will again, and a metrics panel is not
    the place to discover that.
    """
    text = _read(f"{PROC}/stat")
    if not text:
        return None
    for line in text.splitlines():
        if not line.startswith("cpu "):
            continue
        try:
            fields = [int(value) for value in line.split()[1:]]
        except ValueError:
            continue
        if len(fields) < 4:  # user, nice, system, idle are the only guaranteed ones
            continue
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)  # idle + iowait
        return sum(fields), idle
    return None


def _network_bytes() -> Optional[tuple[int, int]]:
    """(received, transmitted) summed over real interfaces, loopback excluded."""
    text = _read(f"{PROC}/net/dev")
    if not text:
        return None
    received = transmitted = 0
    for line in text.splitlines()[2:]:
        name, _, values = line.partition(":")
        name = name.strip()
        if not name or name == "lo" or name.startswith(("veth", "br-", "docker", "virbr")):
            continue
        columns = values.split()
        if len(columns) < 9:
            continue
        try:
            received += int(columns[0])
            transmitted += int(columns[8])
        except ValueError:
            continue
    return received, transmitted


def _memory() -> dict[str, Optional[float]]:
    text = _read(f"{PROC}/meminfo")
    if not text:
        return {"total_mb": None, "used_mb": None, "available_mb": None, "percent": None,
                "swap_total_mb": None, "swap_used_mb": None, "swap_percent": None}
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if not rest:
            continue
        try:
            values[key.strip()] = int(rest.split()[0])  # kB
        except (ValueError, IndexError):
            continue
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", values.get("MemFree", 0))
    used = max(0, total - available)
    swap_total = values.get("SwapTotal", 0)
    swap_used = max(0, swap_total - values.get("SwapFree", 0))
    to_mb = lambda kb: round(kb / 1024, 1)  # noqa: E731 - short and local
    return {
        "total_mb": to_mb(total),
        "used_mb": to_mb(used),
        "available_mb": to_mb(available),
        "percent": round(used / total * 100, 1) if total else None,
        "swap_total_mb": to_mb(swap_total),
        "swap_used_mb": to_mb(swap_used),
        "swap_percent": round(swap_used / swap_total * 100, 1) if swap_total else None,
    }


def _uptime() -> Optional[float]:
    text = _read(f"{PROC}/uptime")
    if not text:
        return None
    try:
        return round(float(text.split()[0]), 1)
    except (ValueError, IndexError):
        return None


def _process() -> dict[str, Any]:
    info: dict[str, Any] = {
        "pid": os.getpid(),
        "uptime_seconds": round(time.monotonic() - MODULE_START, 1),
        "rss_mb": None,
        "threads": None,
        "open_files": None,
    }
    text = _read(f"{PROC}/self/statm")
    if text:
        try:
            pages = int(text.split()[1])  # resident pages
            info["rss_mb"] = round(pages * os.sysconf("SC_PAGE_SIZE") / 1024 / 1024, 1)
        except (ValueError, IndexError, OSError):
            # sysconf is unavailable on some platforms and raises ValueError for
            # an unknown name; the resident-page count alone is not worth a 500.
            pass
    status = _read(f"{PROC}/self/status")
    if status:
        for line in status.splitlines():
            if line.startswith("Threads:"):
                try:
                    info["threads"] = int(line.split()[1])
                except (ValueError, IndexError):
                    pass
                break
    try:
        info["open_files"] = len(os.listdir(f"{PROC}/self/fd"))
    except OSError:
        pass
    return info


# ---------------------------------------------------------------------------
# rate helpers
# ---------------------------------------------------------------------------
MIN_WINDOW_SECONDS = 0.2
FIRST_SAMPLE_WINDOW_SECONDS = 0.3


def _rate(name: str, sampler, reading: Optional[tuple[int, int]]) -> Optional[tuple[float, float]]:
    """Per-second (first, second) between the previous sample and this one."""
    if reading is None:
        return None
    taken = time.monotonic()
    previous = _PREVIOUS.get(name)
    if not previous:
        # First sample: take our own baseline a moment later so the very first
        # poll still shows a number instead of an empty panel. The pair used for
        # the diff is (reading, again), not (reading, reading). The window is
        # deliberately long enough (300 ms = ~60 jiffies on a 2-core box) that an
        # idle machine still accumulates a measurable number of ticks.
        time.sleep(FIRST_SAMPLE_WINDOW_SECONDS)
        again = sampler()
        if again is None:
            return None
        later = time.monotonic()
        _PREVIOUS[name] = (later, again)
        elapsed = later - taken
        if elapsed <= 0:
            return None
        return ((again[0] - reading[0]) / elapsed, (again[1] - reading[1]) / elapsed)

    stamp, before = previous
    elapsed = taken - stamp
    if elapsed < MIN_WINDOW_SECONDS:
        # Two calls in quick succession (a script hammering the endpoint, or a
        # double click) would divide a tiny counter delta by a tiny interval and
        # produce a wild number. Keep the older baseline so the next, properly
        # spaced sample gets a real window.
        return None
    _PREVIOUS[name] = (taken, reading)
    return ((reading[0] - before[0]) / elapsed, (reading[1] - before[1]) / elapsed)


def host_metrics() -> dict[str, Any]:
    """CPU, memory, disk, load, uptime and network for the machine we run on.

    Each reading is taken independently: an unforeseen failure in one of them
    (a /proc layout we have never seen, a container that hides a file) leaves the
    rest of the panel intact instead of replacing the whole host block with an
    error string. The operator's next question is always "which part is broken",
    and a single error for all six cannot answer it.
    """
    with _LOCK:
        cpu = _safe(None, _rate, "cpu", _cpu_times, _safe(None, _cpu_times))
        network = _safe(None, _rate, "net", _network_bytes, _safe(None, _network_bytes))

    cpu_percent = None
    if cpu:
        total_per_second, idle_per_second = cpu
        if total_per_second > 0:
            cpu_percent = round(max(0.0, min(100.0, (1 - idle_per_second / total_per_second) * 100)), 1)
        else:
            # Not one jiffy of CPU time elapsed across the whole window: the
            # machine really is idle, and 0 % is the honest reading. Leaving it
            # null made an idle server look like a broken panel.
            cpu_percent = 0.0

    try:
        load = [round(value, 2) for value in os.getloadavg()]
    except OSError:
        load = None

    disk: dict[str, Any] = {"total_gb": None, "used_gb": None, "free_gb": None, "percent": None}
    try:
        usage = shutil.disk_usage("/")
        disk = {
            "total_gb": round(usage.total / 1024 ** 3, 1),
            "used_gb": round(usage.used / 1024 ** 3, 1),
            "free_gb": round(usage.free / 1024 ** 3, 1),
            "percent": round(usage.used / usage.total * 100, 1) if usage.total else None,
        }
    except OSError:
        pass

    return {
        "cpu_percent": cpu_percent,
        "cpu_count": os.cpu_count(),
        "load": load,
        "memory": _safe({
            "total_mb": None, "used_mb": None, "available_mb": None, "percent": None,
            "swap_total_mb": None, "swap_used_mb": None, "swap_percent": None,
        }, _memory),
        "disk": disk,
        "uptime_seconds": _safe(None, _uptime),
        "network": {
            "rx_kbps": round(network[0] / 1024, 1) if network else None,
            "tx_kbps": round(network[1] / 1024, 1) if network else None,
        },
        "platform": f"{platform.system()} {platform.release()}",
        "python": sys.version.split()[0],
    }


# ---------------------------------------------------------------------------
# application metrics
# ---------------------------------------------------------------------------
def application_metrics(connection) -> dict[str, Any]:
    """Counts that say whether mail is actually flowing.

    Takes an open sqlite connection so the caller owns transactions; every
    query is read-only and bounded by a time window.
    """
    now = dt.datetime.now(dt.timezone.utc)
    day_ago = (now - dt.timedelta(hours=24)).isoformat(timespec="seconds")
    hour_ago = (now - dt.timedelta(hours=1)).isoformat(timespec="seconds")

    def scalar(sql: str, parameters: tuple = ()) -> Any:
        row = connection.execute(sql, parameters).fetchone()
        return row[0] if row else None

    def count(sql: str, parameters: tuple = ()) -> int:
        return int(scalar(sql, parameters) or 0)

    # Latency: report the median and p90, not just the mean. The real data has a
    # one-off backlog in it (eleven messages sat unprocessed for two days and
    # were all sent within twenty minutes of each other), and a single mean over
    # that window read "22 hours" — correct arithmetic, useless as an operations
    # number. The median answers "how fast is it normally", p90 answers "how bad
    # does it get".
    samples = [row[0] for row in connection.execute(
        """SELECT (julianday(r.sent_at) - julianday(m.received_at)) * 86400.0
           FROM reports r JOIN messages m ON m.id = r.message_id
           WHERE r.kind='immediate' AND r.status='sent' AND r.sent_at IS NOT NULL
             AND r.sent_at >= ?
           ORDER BY 1""", (day_ago,)).fetchall()]

    def percentile(values: list, fraction: float):
        if not values:
            return None
        index = min(len(values) - 1, max(0, math.ceil(fraction * len(values)) - 1))
        return round(values[index], 1)

    average_latency = round(sum(samples) / len(samples), 1) if samples else None

    # "How fast is it right now?" is a different question from "how fast was the
    # last 24 hours": right after a backlog the window is full of old mail, so
    # the single most recent send is reported as well.
    latest = connection.execute(
        """SELECT (julianday(r.sent_at) - julianday(m.received_at)) * 86400.0
           FROM reports r JOIN messages m ON m.id = r.message_id
           WHERE r.kind='immediate' AND r.status='sent' AND r.sent_at IS NOT NULL
           ORDER BY r.sent_at DESC LIMIT 1""").fetchone()
    latest_latency = round(latest[0], 1) if latest and latest[0] is not None else None

    sizes = {"database_size_mb": None, "wal_size_mb": None}
    try:
        path = connection.execute("PRAGMA database_list").fetchone()[2]
        if path:
            sizes["database_size_mb"] = round(os.path.getsize(path) / 1024 / 1024, 2)
            if os.path.exists(path + "-wal"):
                sizes["wal_size_mb"] = round(os.path.getsize(path + "-wal") / 1024 / 1024, 2)
    except (OSError, TypeError, IndexError, AttributeError):
        pass

    return {
        "messages_1h": count("SELECT COUNT(*) FROM messages WHERE received_at >= ?", (hour_ago,)),
        "messages_24h": count("SELECT COUNT(*) FROM messages WHERE received_at >= ?", (day_ago,)),
        "sent_1h": count("SELECT COUNT(*) FROM reports WHERE kind='immediate' AND status='sent' AND sent_at >= ?", (hour_ago,)),
        "sent_24h": count("SELECT COUNT(*) FROM reports WHERE kind='immediate' AND status='sent' AND sent_at >= ?", (day_ago,)),
        "failed": count("SELECT COUNT(*) FROM messages WHERE status='failed'"),
        "queue": count("SELECT COUNT(*) FROM messages WHERE status IN ('pending','processing')"),
        "skipped_24h": count("SELECT COUNT(*) FROM messages WHERE status='skipped' AND received_at >= ?", (day_ago,)),
        "average_latency_seconds": average_latency,
        "median_latency_seconds": percentile(samples, 0.5),
        "latest_latency_seconds": latest_latency,
        "p90_latency_seconds": percentile(samples, 0.9),
        "latency_samples": len(samples),
        "last_poll_at": scalar("SELECT MAX(last_polled_at) FROM mailboxes"),
        "oldest_poll_at": scalar("SELECT MIN(last_polled_at) FROM mailboxes"),
        **sizes,
    }


def collect(connection=None) -> dict[str, Any]:
    """One snapshot. Never raises: a broken source becomes a null, not a 500."""
    snapshot: dict[str, Any] = {
        "collected_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "monotonic": round(time.monotonic(), 3),
        "host": {},
        "process": {},
        "application": {},
    }
    try:
        snapshot["host"] = host_metrics()
    except Exception as error:  # pragma: no cover - defensive
        snapshot["host"] = {"error": str(error)}
    try:
        snapshot["process"] = _process()
    except Exception as error:  # pragma: no cover - defensive
        snapshot["process"] = {"error": str(error)}
    if connection is not None:
        try:
            snapshot["application"] = application_metrics(connection)
        except Exception as error:  # pragma: no cover - defensive
            snapshot["application"] = {"error": str(error)}
    return snapshot
