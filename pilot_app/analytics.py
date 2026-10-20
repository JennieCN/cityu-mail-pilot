"""Who visited, and from where — counted, not identified.

The question this answers is "有多少人浏览了我的网页", and the operator also
asked to see *where* visitors come from. Both halves are doable without turning
the site into a surveillance tool, but only if the two halves are stored
differently:

* **What is kept** is the countable part: which page, which day, which referring
  site, whether the visitor was a signed-in member, whether a robot fetched it,
  and the country/city resolved from the address at the moment of the visit.
* **What is not kept** is the address itself. The database gets
  ``SecretBox.anonymized(ip)`` — the same keyed digest the message board uses —
  so the same visitor can be recognised again for a unique-visitor count and
  nothing else. The raw address lives in a small in-memory ring buffer that is
  never written to disk, which is what lets the operator watch live traffic
  without the address ending up in the daily backup.

Why the digest and not a plain SHA-256: the whole IPv4 space is 2^32 and
enumerable in minutes, so a bare hash of an address *is* the address. The same
reasoning is written out in ``security.SecretBox.anonymized``; this module uses
that function rather than inventing a second definition of "anonymised".

Two more deliberate choices, both about honesty of the numbers:

* Robots are counted **separately**, never folded into the human total. This box
  sits on a public address, so scanners find it within hours; a "visitors" number
  that mixes ``odin-scanner/0.4`` with a real reader is not a number anybody can
  act on.
* ``DNT: 1`` and ``Sec-GPC: 1`` are honoured — those requests are not recorded at
  all, and the panel says how many were skipped for that reason, so the total
  never quietly claims to be everybody.
"""

from __future__ import annotations

import os
import re
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from . import geoip

# ---------------------------------------------------------------------------
# What counts as a visit

# Not visits: the app's own API, its static files, health probes and the PWA
# plumbing. Counting those would make "N 次浏览" a measure of how chatty the
# front-end is rather than of how many people looked at a page.
SKIP_PREFIXES = ("/api/", "/static/", "/.well-known/")
SKIP_EXACT = {
    "/health",
    "/favicon.ico",
    "/apple-touch-icon.png",
    "/apple-touch-icon-precomposed.png",
    "/manifest.webmanifest",
    "/demo-data.js",
    "/robots.txt",
    "/sitemap.xml",
}

# Only these can be a page view. A HEAD is a browser or a monitor checking
# whether something changed; it has no body, so it is not somebody reading.
RECORDED_METHODS = {"GET", "HEAD"}
RECORDED_STATUSES = {200, 304}


def is_page_view(method: str, path: str, status: int) -> bool:
    """Whether this request is a page somebody could have read."""
    if method not in RECORDED_METHODS or status not in RECORDED_STATUSES:
        return False
    if path in SKIP_EXACT or path.startswith(SKIP_PREFIXES):
        return False
    return True


# ---------------------------------------------------------------------------
# The client address

# Hostnames and bracketed IPv6 literals; anything else is not a referrer.
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$|^\[[0-9a-f:]+\]$")

_LOOPBACK = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}


def _peer_is_loopback(peer: str) -> bool:
    return str(peer or "").strip() in _LOOPBACK or str(peer or "").startswith("127.")


def client_ip(peer: str, header: Any) -> str:
    """The visitor's address, or the socket peer when it cannot be trusted.

    ``X-Real-IP`` is only believed when the connection itself came from
    loopback — that is our own nginx (see ``nginx-cityu-mail-pilot.conf.example``,
    which always sets it to ``$remote_addr``) and nobody else. Believing the
    header from an arbitrary peer would let any visitor choose their own
    address, which would poison the visitor count and the rate limits built on
    it. ``X-Forwarded-For`` is deliberately *not* consulted: it is a list, the
    client controls its left-hand entries, and one trusted header beats parsing
    an untrusted one.

    This exists because the address was previously taken straight from
    ``self.client_address[0]``, which behind nginx is always ``127.0.0.1`` — so
    every visitor in the database looked like the same person, and the message
    board's "5 per hour" limit was one shared budget for the whole internet.
    """
    peer = str(peer or "").strip()
    if not _peer_is_loopback(peer):
        return peer
    try:
        forwarded = str(header.get("X-Real-IP") or "").strip()
    except Exception:  # pragma: no cover - headers are a mapping in practice
        return peer
    return forwarded or peer


# ---------------------------------------------------------------------------
# Robots

# Substrings that appear in crawler, scanner and command-line client strings.
# This is a coarse allowlist of shapes, not a UA database: the panel shows what
# was classified how, so a misclassification is visible rather than silent.
_BOT_MARKERS = (
    "bot", "spider", "crawl", "scanner", "scan/", "slurp", "facebookexternalhit",
    "curl/", "wget", "python-requests", "python-urllib", "aiohttp", "httpx",
    "go-http-client", "java/", "libwww", "okhttp", "axios", "node-fetch",
    "nmap", "masscan", "zgrab", "nuclei", "nikto", "sqlmap", "dirbuster",
    "gobuster", "feroxbuster", "headlesschrome", "phantomjs", "monitor",
    "uptime", "pingdom", "statuscake", "censys", "shodan", "internetmeasurement",
    "expanse", "netsystemsresearch", "odin-scanner", "leakix", "paloalto",
)

_BROWSERS = (
    ("Edg/", "Edge"), ("OPR/", "Opera"), ("YaBrowser", "Yandex"),
    ("Firefox/", "Firefox"), ("Chrome/", "Chrome"), ("Safari/", "Safari"),
)

_SYSTEMS = (
    ("iPhone", "iOS"), ("iPad", "iPadOS"), ("iPod", "iOS"), ("Android", "Android"),
    ("Windows", "Windows"), ("Macintosh", "macOS"), ("CrOS", "ChromeOS"),
    ("Linux", "Linux"),
)


def looks_like_bot(user_agent: str) -> bool:
    """Whether this user agent is a machine rather than a person.

    An empty user agent counts as a machine: every browser sends one, and the
    things that do not are scripts.
    """
    text = str(user_agent or "").strip().lower()
    if not text:
        return True
    return any(marker in text for marker in _BOT_MARKERS)


def classify(user_agent: str) -> dict[str, Any]:
    """Coarse browser/system labels for the panel. Never used for decisions."""
    text = str(user_agent or "")
    browser = ""
    for marker, label in _BROWSERS:
        if marker.lower() in text.lower():
            browser = label
            break
    system = ""
    for marker, label in _SYSTEMS:
        if marker.lower() in text.lower():
            system = label
            break
    return {"bot": looks_like_bot(text), "browser": browser, "system": system}


# ---------------------------------------------------------------------------
# Referrers

def referrer_host(value: str, *, own_host: str = "") -> str:
    """The referring *site*, reduced to its host name.

    The full URL is not kept: a referring URL can carry a search query, and
    those queries are about the person who typed them. The host answers the
    question the operator actually has ("where do visitors come from").
    """
    raw = str(value or "").strip()
    if not raw or raw == "-":
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    try:
        from urllib.parse import urlsplit

        host = (urlsplit(raw).hostname or "").strip().lower()
    except Exception:
        return ""
    if not host or _HOST_RE.match(host) is None:
        # urlsplit() is happy to call "not a url at all" a hostname. A
        # referrer column full of prose would be worse than an empty one.
        return ""
    own = str(own_host or "").split(":")[0].strip().lower()
    if own and (host == own or host.endswith("." + own)):
        return ""  # an internal link is not a source of visitors
    if len(host) > 120:
        return host[:120]
    return host


# ---------------------------------------------------------------------------
# Opting out

def wants_no_tracking(header: Any) -> bool:
    """``DNT: 1`` / ``Sec-GPC: 1``.

    Honouring these costs a little accuracy and buys a claim the panel can make
    without an asterisk: the people who asked not to be counted are not counted.
    """
    try:
        dnt = str(header.get("DNT") or "").strip()
        gpc = str(header.get("Sec-GPC") or "").strip()
    except Exception:  # pragma: no cover
        return False
    return dnt == "1" or gpc == "1"


# ---------------------------------------------------------------------------
# Retention

DEFAULT_RETENTION_DAYS = 180


def retention_days() -> int:
    """How long visit rows are kept. Regenerable, so this can be short."""
    raw = os.environ.get("INFE_PILOT_ANALYTICS_DAYS", "")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS
    if value <= 0:
        return DEFAULT_RETENTION_DAYS
    return min(value, 3650)


# ---------------------------------------------------------------------------
# The live buffer: the only place a raw address exists

RECENT_MAX = 200
_recent: deque[dict[str, Any]] = deque(maxlen=RECENT_MAX)
_recent_lock = threading.Lock()


def remember(entry: dict[str, Any]) -> None:
    """Add one visit to the live buffer, unless the operator made it himself.

    His own page loads are the one kind of traffic this list cannot inform him
    about -- he already knows he just opened the page -- and they push real
    visitors out of a 200-slot buffer. He asked for his own records to be gone
    ("把我自己的记录删了"), and "gone" has to include the live list, or the
    panel he just cleaned goes on showing him his own address.
    """
    if entry.get("admin"):
        return
    with _recent_lock:
        _recent.appendleft(entry)


def recent(limit: int = 50) -> list[dict[str, Any]]:
    """The last few visits, newest first, **including their raw addresses**.

    In memory only: a restart empties it, and it is never read from or written
    to the database. This is the compromise behind "show me the addresses but
    don't store them" — the operator can watch traffic arrive, and the daily
    backup cannot leak what was never written down. Operator visits are not in
    here at all; see :func:`remember`.
    """
    with _recent_lock:
        return [dict(item) for item in list(_recent)[: max(1, min(int(limit), RECENT_MAX))]]


def forget_recent() -> None:
    with _recent_lock:
        _recent.clear()


# ---------------------------------------------------------------------------
# Recording

_last_purge = 0.0
_PURGE_EVERY_SECONDS = 3600


def record(
    database: Any,
    secrets: Any,
    *,
    ip: str,
    path: str,
    status: int,
    method: str = "GET",
    referrer: str = "",
    user_agent: str = "",
    member: bool = False,
    admin: bool = False,
    own_host: str = "",
    geo_path: Optional[str] = None,
    now: Optional[datetime] = None,
) -> bool:
    """Store one visit. Returns whether it was stored.

    Every failure path here is "store nothing and carry on": this runs inside
    the request that a real person is waiting for, so a statistics table must
    never be able to break a page.
    """
    if not is_page_view(method, path, status):
        return False
    moment = now or datetime.now(timezone.utc)
    ip = str(ip or "").strip()
    geo = geoip.lookup(ip, geo_path) if ip else {}
    bot = looks_like_bot(user_agent)
    entry = {
        "time": moment.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "ip": ip,
        "path": path,
        "status": int(status),
        "referrer": referrer_host(referrer, own_host=own_host),
        "country": geo.get("country", ""),
        "country_name": geo.get("country_name", ""),
        "city": geo.get("city", ""),
        "bot": bot,
        "member": bool(member),
        "admin": bool(admin),
    }
    remember(entry)
    try:
        database.record_page_view(
            created_at=entry["time"],
            path=path,
            status=int(status),
            referrer=entry["referrer"],
            client_hash=secrets.anonymized(ip) if ip else "",
            country=entry["country"],
            country_name=entry["country_name"],
            city=entry["city"],
            country_continent=geo.get("continent", ""),
            bot=bot,
            member=bool(member),
            admin=bool(admin),
            source="live",
        )
    except Exception:  # pragma: no cover - a full disk must not break a page
        return False
    _maybe_purge(database, moment)
    return True


def import_row(
    secrets: Any,
    *,
    ip: str,
    path: str,
    status: int,
    method: str = "GET",
    referrer: str = "",
    user_agent: str = "",
    created_at: str,
    source: str = "nginx",
    geo_path: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Turn one log line into a row, or ``None`` when it is not a page view.

    Split out from the insert so a whole import can go in as one transaction --
    the per-row version lost most of a 1,919-row import on the live box, because
    every row was its own connection racing the running web process.
    """
    if not is_page_view(method, path, status):
        return None
    address = str(ip or "").strip()
    geo = geoip.lookup(address, geo_path) if address else {}
    return {
        "id": f"pv_{os.urandom(8).hex()}",
        "created_at": str(created_at),
        "path": str(path)[:300],
        "status": int(status or 0),
        "referrer": referrer_host(referrer)[:120],
        "client_hash": secrets.anonymized(address) if address else "",
        "country": str(geo.get("country", ""))[:8],
        "country_name": str(geo.get("country_name", ""))[:60],
        "city": str(geo.get("city", ""))[:80],
        "continent": str(geo.get("continent", ""))[:8],
        "bot": 1 if looks_like_bot(user_agent) else 0,
        "member": 0,
        "admin": 0,
        "source": str(source)[:16],
    }


def _maybe_purge(database: Any, now: datetime) -> None:
    """Delete what is past its retention window, at most once an hour.

    Piggy-backing on a request keeps this out of the worker's hot loop and out
    of any timer; the visit table is regenerable data, so the cost of a restart
    losing the "last purge" stamp is one extra delete.
    """
    global _last_purge
    stamp = time.monotonic()
    if stamp - _last_purge < _PURGE_EVERY_SECONDS:
        return
    _last_purge = stamp
    try:
        cutoff = (now - timedelta(days=retention_days())).isoformat(timespec="seconds")
        database.purge_page_views(cutoff)
    except Exception:  # pragma: no cover
        pass


# ---------------------------------------------------------------------------
# Presenting

def day_modifier(timezone_name: str, *, now: Optional[datetime] = None) -> str:
    """A SQLite modifier that shifts UTC stamps onto the reader's own day.

    The operator asked "how many people visited today", and "today" means their
    day, not UTC's: grouping by UTC would cut the day at 08:00 in Hong Kong and
    the panel would disagree with the clock on the wall. The offset is taken at
    the current moment, which is exactly right for a zone without DST (this
    instance runs Asia/Shanghai) and off by an hour twice a year elsewhere —
    said out loud here rather than discovered later.
    """
    name = str(timezone_name or "").strip() or "Asia/Shanghai"
    try:
        from zoneinfo import ZoneInfo

        offset = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(name)).utcoffset()
    except Exception:
        offset = timedelta(hours=8)
    if offset is None:
        offset = timedelta(hours=8)
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    return f"{sign}{abs(total_minutes)} minutes"
