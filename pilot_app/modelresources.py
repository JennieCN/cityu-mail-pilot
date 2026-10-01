"""Read-only model-box resource readings for the admin console (schema = 1).

Second stage of the model-server panel: the collector lives on the model box
(``tools/model_resource_collector.py``), the TLS proxy exposes it at the fixed
same-origin path ``GET /monitor/resources``, and this module is the only website
consumer.  It fetches, re-validates and narrows that payload to the frozen
whitelist; the admin API returns exactly this object and nothing else.

Boundaries that matter:

* **The connection must be the one ``modelconsole._primary()`` returned.**  The
  caller passes it in; this module rechecks the validated primary catalog, never
  accepts a client-supplied host/URL/path, and never bypasses the shared
  cross-provider key fence that removes a local tier sharing a paid key.
* **Pinned TLS, fixed path.**  Requests go through ``providers._outbound_open``
  with ``providers.local_model_tls("local_openai")`` (the same CA file and
  fingerprint pinning every other local-model call uses) at the fixed
  same-origin ``/monitor/resources`` path.  The URL must be HTTPS with no
  userinfo, query or fragment; redirects are refused by the shared outbound
  opener, so the Bearer credential can never be forwarded.
* **Bounded and quiet.**  At most 32768 bytes and 5 seconds; the key lives only
  in the request header in memory.  Failures collapse to a closed state -- no
  ``str(exc)``, no raw body, no upstream text ever reaches the response.
* **Validated, never fabricated.**  Non-finite numbers, booleans, wrong types,
  unknown schema and contradictory capacities become ``unknown``/``null``; a
  genuine ``0`` stays ``0``.  A missing, naive, future (> 5 s) or older than
  60 s timestamp marks the whole snapshot stale, so the website can never show
  it as a current reading.
* **One 15-second cache including the connection/TLS/credential identity.**
  Concurrent tabs coalesce; a changed key, certificate or base URL never reuses
  an older reading.  Nothing is written to the database, to token usage, or to
  the production health stamp.

The connection's ``base_url`` is the operator-configured primary, not user
input, so the URL checks below are defence in depth around that config rather
than the only gate.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import ipaddress
import json
import math
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from . import providers, security

SCHEMA = 1
CACHE_TTL_SECONDS = 15.0
MAX_RESPONSE_BYTES = 32768
REQUEST_TIMEOUT_SECONDS = 5
STALE_AFTER_SECONDS = 60.0
FUTURE_SLACK_SECONDS = 5.0
MONITOR_PATH = "/monitor/resources"
MAX_GPU_DEVICES = 8
GPU_MAX_MIB = 2 ** 30
SLOTS_MAX_TOTAL = 64
MAX_BYTES_VALUE = 2 ** 50
MIN_TEMPERATURE_C = 0.0
MAX_TEMPERATURE_C = 150.0

_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"identity": None, "at": 0.0, "value": None}


# ---------------------------------------------------------------------------
# Closed empty shapes: every failure path returns one of these
# ---------------------------------------------------------------------------
def _empty(state: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "state": state,
        "stale": True,
        "collected_at": None,
        "cpu": {"state": "unknown", "utilization_percent": None},
        "memory": {"state": "unknown", "total_bytes": None, "available_bytes": None},
        "disk": {"state": "unknown", "total_bytes": None, "used_bytes": None, "free_bytes": None},
        "gpu": {"state": "unknown", "devices": []},
        "slots": {"state": "unknown", "total": None, "busy": None, "idle": None},
    }


# ---------------------------------------------------------------------------
# Validation helpers: bool is never a number, non-finite is never a reading
# ---------------------------------------------------------------------------
def _number(value: Any, low: float, high: float, digits: int = 1) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < low or number > high:
        return None
    return round(number, digits)


def _integer(value: Any, low: int, high: int) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if low <= value <= high else None


def _parse_stamp(value: Any) -> tuple[Optional[str], Optional[dt.datetime]]:
    """UTC-aware ISO seconds, or (None, None) for missing/naive/unparseable."""
    if not isinstance(value, str) or not value.strip():
        return None, None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None, None
    if parsed.tzinfo is None:
        return None, None  # naive time is not a UTC timestamp
    try:
        utc = parsed.astimezone(dt.timezone.utc)
    except (ValueError, OverflowError):
        return None, None
    return utc.isoformat(timespec="seconds"), utc


def _is_stale(at: Optional[dt.datetime]) -> bool:
    if at is None:
        return True
    delta = (dt.datetime.now(dt.timezone.utc) - at).total_seconds()
    return delta > STALE_AFTER_SECONDS or delta < -FUTURE_SLACK_SECONDS


# ---------------------------------------------------------------------------
# Section normalisation
# ---------------------------------------------------------------------------
def _cpu_section(raw: Any) -> dict[str, Any]:
    value = raw if isinstance(raw, dict) else {}
    percent = _number(value.get("utilization_percent"), 0.0, 100.0)
    if value.get("state") == "ok" and percent is not None:
        return {"state": "ok", "utilization_percent": percent}
    return {"state": "unknown", "utilization_percent": None}


def _memory_section(raw: Any) -> dict[str, Any]:
    value = raw if isinstance(raw, dict) else {}
    total = _integer(value.get("total_bytes"), 0, MAX_BYTES_VALUE)
    available = _integer(value.get("available_bytes"), 0, MAX_BYTES_VALUE)
    if value.get("state") == "ok" and total is not None and available is not None and total > 0 and available <= total:
        return {"state": "ok", "total_bytes": total, "available_bytes": available}
    return {"state": "unknown", "total_bytes": None, "available_bytes": None}


def _disk_section(raw: Any) -> dict[str, Any]:
    value = raw if isinstance(raw, dict) else {}
    total = _integer(value.get("total_bytes"), 0, MAX_BYTES_VALUE)
    used = _integer(value.get("used_bytes"), 0, MAX_BYTES_VALUE)
    free = _integer(value.get("free_bytes"), 0, MAX_BYTES_VALUE)
    if value.get("state") == "ok" and None not in (total, used, free) and total > 0:
        # Reserved blocks make used + free smaller than total; larger is a
        # contradiction and the whole section is refused.
        if used <= total and free <= total and used + free <= total:
            return {"state": "ok", "total_bytes": total, "used_bytes": used, "free_bytes": free}
    return {"state": "unknown", "total_bytes": None, "used_bytes": None, "free_bytes": None}


def _gpu_section(raw: Any) -> dict[str, Any]:
    value = raw if isinstance(raw, dict) else {}
    if value.get("state") != "ok" or not isinstance(value.get("devices"), list):
        return {"state": "unknown", "devices": []}
    devices: list[dict[str, Any]] = []
    seen: set[int] = set()
    for item in value["devices"]:
        if len(devices) >= MAX_GPU_DEVICES:
            break
        if not isinstance(item, dict):
            continue
        index = _integer(item.get("index"), 0, MAX_GPU_DEVICES - 1)
        if index is None or index in seen:
            continue  # keep the real index; duplicates are not silently renamed
        seen.add(index)
        used = _number(item.get("memory_used_mib"), 0.0, GPU_MAX_MIB)
        total = _number(item.get("memory_total_mib"), 0.0, GPU_MAX_MIB)
        if total == 0 or (used is not None and total is not None and used > total):
            used = total = None  # contradictory capacity: drop the pair
        devices.append({
            "index": index,
            "utilization_percent": _number(item.get("utilization_percent"), 0.0, 100.0),
            "memory_used_mib": used,
            "memory_total_mib": total,
            "temperature_c": _number(item.get("temperature_c"), MIN_TEMPERATURE_C, MAX_TEMPERATURE_C),
        })
    return {"state": "ok", "devices": devices} if devices else {"state": "unknown", "devices": []}


def _slots_section(raw: Any) -> dict[str, Any]:
    value = raw if isinstance(raw, dict) else {}
    total = _integer(value.get("total"), 1, SLOTS_MAX_TOTAL)
    busy = _integer(value.get("busy"), 0, SLOTS_MAX_TOTAL)
    idle = _integer(value.get("idle"), 0, SLOTS_MAX_TOTAL)
    if value.get("state") == "ok" and None not in (total, busy, idle):
        if busy <= total and idle <= total and busy + idle == total:
            return {"state": "ok", "total": total, "busy": busy, "idle": idle}
    return {"state": "unknown", "total": None, "busy": None, "idle": None}


def _normalize(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or type(payload.get("schema")) is not int or payload.get("schema") != SCHEMA:
        return _empty("unknown")
    collected_at, at = _parse_stamp(payload.get("collected_at"))
    stale = _is_stale(at)
    return {
        "schema": SCHEMA,
        "state": "stale" if stale else "ok",
        "stale": stale,
        "collected_at": collected_at,
        "cpu": _cpu_section(payload.get("cpu")),
        "memory": _memory_section(payload.get("memory")),
        "disk": _disk_section(payload.get("disk")),
        "gpu": _gpu_section(payload.get("gpu")),
        "slots": _slots_section(payload.get("slots")),
    }


# ---------------------------------------------------------------------------
# Outbound request
# ---------------------------------------------------------------------------
def _monitor_url(base_url: str, *, allow_loopback: bool = False) -> str:
    root = str(base_url or "").strip().rstrip("/")
    if any(ord(char) <= 32 or ord(char) == 127 for char in root):
        return ""  # urlsplit silently drops embedded TAB/CR/LF
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    if not root:
        return ""
    url = root + MONITOR_PATH
    try:
        parsed = urllib.parse.urlsplit(url)
        parsed.port  # raises ValueError on a malformed port
    except ValueError:
        return ""
    if parsed.scheme != "https" or not parsed.hostname:
        return ""
    if parsed.username is not None or parsed.password is not None:
        return ""
    if parsed.query or parsed.fragment:
        return ""
    if not parsed.path.endswith(MONITOR_PATH):
        return ""
    host = parsed.hostname
    if not host.isascii():
        return ""  # IDNA maps Unicode full stops into numeric-host aliases
    if "%" in host:
        return ""  # scoped/escaped host is not an approved numeric tunnel
    if host in ("127.0.0.1", "::1"):
        return url if allow_loopback else ""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        # inet_aton accepts short, integer, hex and octal IPv4 forms without
        # DNS. Reject them, including IDNA/trailing-dot aliases, rather than
        # treating them as a public hostname in the resolve_dns=False gate.
        try:
            socket.inet_aton(host.rstrip(".").encode("idna").decode("ascii"))
        except (OSError, UnicodeError):
            pass
        else:
            return ""
    try:
        # Same outbound gate the provider path uses; no DNS here because the
        # base URL is operator configuration, not request input.
        security.validate_outbound_https_url(url, resolve_dns=False)
    except security.SecurityError:
        return ""
    return url


def _approved_primary(connection: dict) -> bool:
    """No client/fallback or removed shared-key tier may use this consumer."""
    if (connection.get("provider") != providers.LOCAL_MODEL_PROVIDER
            or connection.get("platform") is not True
            or providers.platform_tier(connection) == "fallback"):
        return False
    try:
        connections = providers.platform_model_connections()
        return bool(connections and connection == connections[0])
    except Exception:  # noqa: BLE001 - configuration failure is closed
        return False


def _read_bounded(response: Any, limit: int, timeout: float) -> Optional[bytes]:
    """Read at most ``limit`` bytes within ``timeout`` seconds; None = oversize."""
    deadline = time.monotonic() + timeout
    # urllib HTTPResponse owns a socket through this fixed stdlib chain.
    # Interrupt even slow chunk headers/body streams; a per-socket inactivity
    # timeout alone does not cap a continuously dripping response.
    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
    def interrupt():
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
    timer = threading.Timer(max(0.0, timeout), interrupt)
    timer.daemon = True
    timer.start()
    chunks: list[bytes] = []
    total = 0
    read = getattr(response, "read1", response.read)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            if sock is not None and sock.fileno() >= 0:
                sock.settimeout(remaining)
            chunk = read(min(8192, limit + 1 - total))
            if time.monotonic() >= deadline:
                return None
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                return None
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        timer.cancel()


def _fetch(connection: dict) -> Optional[dict]:
    if not _approved_primary(connection):
        return None
    try:
        key = str(providers.platform_connection_key(connection) or "").strip()
    except Exception:  # noqa: BLE001 - a missing credential is just "unknown"
        return None
    if not key or "\r" in key or "\n" in key:
        return None
    url = _monitor_url(str(connection.get("base_url") or ""), allow_loopback=True)
    if not url:
        return None
    request = urllib.request.Request(
        url, method="GET",
        headers={"Accept": "application/json", "Authorization": "Bearer " + key})
    tls = providers.local_model_tls(providers.LOCAL_MODEL_PROVIDER)
    deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
    with providers._outbound_open(request, timeout=REQUEST_TIMEOUT_SECONDS, tls=tls) as response:
        raw = _read_bounded(response, MAX_RESPONSE_BYTES, max(0.0, deadline - time.monotonic()))
    if raw is None:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _identity(connection: dict) -> tuple:
    tls = providers.local_model_tls(providers.LOCAL_MODEL_PROVIDER) or {}
    try:
        key = str(providers.platform_connection_key(connection) or "")
    except Exception:  # noqa: BLE001 - identity is only a cache key
        key = ""
    return (
        str(connection.get("base_url") or ""),
        str(tls.get("ca_file") or ""),
        str(tls.get("pin") or ""),
        hashlib.sha256(key.encode("utf-8")).hexdigest(),
    )


def reading(connection: Optional[dict]) -> dict[str, Any]:
    """Normalised whitelist reading for the validated primary connection.

    ``connection`` must be the dict ``modelconsole._primary()`` returned (or
    ``None`` when there is no local primary).  Anything else is treated as
    "unknown"; this module rechecks the validated catalog before cache reuse.
    """
    if not isinstance(connection, dict):
        return _empty("not_configured")
    if not _approved_primary(connection):
        return _empty("unknown")
    identity = _identity(connection)
    with _LOCK:
        now = time.monotonic()
        cached = _CACHE["value"]
        if cached is not None and _CACHE["identity"] == identity and now - _CACHE["at"] < CACHE_TTL_SECONDS:
            if cached["state"] not in ("ok", "stale"):
                return copy.deepcopy(cached)
            return _normalize(cached)
        try:
            payload = _fetch(connection)
            value = _normalize(payload) if isinstance(payload, dict) else _empty("unknown")
        except Exception:  # noqa: BLE001 - closed state only, never str(exc)
            value = _empty("unknown")
        _CACHE["identity"] = identity
        _CACHE["at"] = time.monotonic()
        _CACHE["value"] = value
        return copy.deepcopy(value)
