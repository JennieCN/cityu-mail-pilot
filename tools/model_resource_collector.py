"""Read-only numeric resource collector for the model box (standard library only).

This module is the *collector* half of the admin console's second stage: the TLS
proxy on the model box calls ``collect(upstream_key=UPSTREAM_KEY)`` once per
``GET /monitor/resources`` and forwards the returned dict verbatim.  It answers
with the frozen protocol (``schema = 1``) and nothing else:

    {"schema": 1,
     "collected_at": "2026-10-01T12:00:00+00:00",
     "cpu":    {"state": "ok"|"unknown", "utilization_percent": number|null},
     "memory": {"state": "ok"|"unknown", "total_bytes": int|null,
                "available_bytes": int|null},
     "disk":   {"state": "ok"|"unknown", "total_bytes": int|null,
                "used_bytes": int|null, "free_bytes": int|null},
     "gpu":    {"state": "ok"|"unknown",
                "devices": [{"index": int, "utilization_percent": number|null,
                             "memory_used_mib": number|null,
                             "memory_total_mib": number|null,
                             "temperature_c": number|null}]},
     "slots":  {"state": "ok"|"unknown", "total": int|null,
                "busy": int|null, "idle": int|null}}

Design boundaries (they are the point of the module, not decoration):

* **Fixed sources.** ``/proc/stat``, ``/proc/meminfo``, ``shutil.disk_usage("/")``,
  one ``nvidia-smi`` query with fixed arguments, and one fixed
  ``http://127.0.0.1:8080/slots`` request.  No user-supplied path, host or
  command; no shell; no process listing; nothing installed.
* **CPU is a diff.** A percentage can only come from two monotonic counter
  samples; load average is never treated as a CPU percentage.  The ``guest`` and
  ``guest_nice`` columns are dropped because they are already counted inside
  ``user``/``nice``.  The first call takes a second sample no more than 0.1 s
  later; when even that window cannot produce a delta the section is ``unknown``.
* **Each section fails alone.** A missing ``/proc`` file, an absent or timing-out
  ``nvidia-smi``, an oversized 401, a malformed slots body and a read timeout
  each degrade only their own section to ``{"state": "unknown", ... nulls}``.
  Nothing is ever reported as ``0`` to look like success.
* **Slots credential stays in memory.** The key is used for the
  ``Authorization`` header of one request; it is never logged, never persisted,
  never echoed in the result.  Only the slot *counts* are derived from the body:
  no params, prompt, cache or raw payload crosses the boundary.
* **One bounded shared cache.** A process-wide lock coalesces concurrent readers
  and a <= 15 s cache stops a poll burst from repeating the heavy queries.  The
  cache is keyed by a hash of the credential, so a changed key cannot reuse an
  old reading.  No logging and no persistence of any kind.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import math
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

SCHEMA = 1
CACHE_TTL_SECONDS = 15.0
COMMAND_TIMEOUT_SECONDS = 2
SLOTS_URL = "http://127.0.0.1:8080/slots"
SLOTS_MAX_BYTES = 65536
SLOTS_TIMEOUT_SECONDS = 2
SLOTS_MAX_TOTAL = 64
MAX_GPU_DEVICES = 8
GPU_MAX_MIB = 2 ** 30
PROC_STAT = "/proc/stat"
PROC_MEMINFO = "/proc/meminfo"
ROOT_PATH = "/"
FIRST_SAMPLE_WINDOW_SECONDS = 0.05  # deliberately <= 0.1 s
NVIDIA_SMI_ARGS = (
    "nvidia-smi",
    "--query-gpu=index,utilization.gpu,memory.used,memory.total,temperature.gpu",
    "--format=csv,noheader,nounits",
)

_LOCK = threading.Lock()
_CACHE: dict[str, Any] = {"identity": None, "at": 0.0, "value": None}
_CPU_PREVIOUS: Optional[tuple[float, int, int]] = None  # (monotonic, total, idle)


# ---------------------------------------------------------------------------
# Protocol-shaped unknown sections (fresh dicts: callers may keep them)
# ---------------------------------------------------------------------------
def _unknown_cpu() -> dict[str, Any]:
    return {"state": "unknown", "utilization_percent": None}


def _unknown_memory() -> dict[str, Any]:
    return {"state": "unknown", "total_bytes": None, "available_bytes": None}


def _unknown_disk() -> dict[str, Any]:
    return {"state": "unknown", "total_bytes": None, "used_bytes": None, "free_bytes": None}


def _unknown_gpu() -> dict[str, Any]:
    return {"state": "unknown", "devices": []}


def _unknown_slots() -> dict[str, Any]:
    return {"state": "unknown", "total": None, "busy": None, "idle": None}


# ---------------------------------------------------------------------------
# Small validation helpers: bool is never a number, non-finite is never a reading
# ---------------------------------------------------------------------------
def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def _bounded_number(value: Any, low: float, high: float, digits: int = 1) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < low or number > high:
        return None
    return round(number, digits)


def _bounded_int(value: Any, low: int, high: int) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if low <= value <= high else None


def _csv_int(text: str) -> Optional[int]:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


def _utc_stamp() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# /proc: CPU (diff) and memory
# ---------------------------------------------------------------------------
def _cpu_times(text: Optional[str]) -> Optional[tuple[int, int]]:
    """(total_jiffies, idle_jiffies) excluding guest/guest_nice double counting."""
    if not text:
        return None
    for line in text.splitlines():
        if not line.startswith("cpu "):
            continue
        try:
            fields = [int(value) for value in line.split()[1:]]
        except ValueError:
            return None
        if len(fields) < 4:  # user, nice, system, idle are the only guaranteed ones
            return None
        # fields[8] and fields[9] are guest and guest_nice, already inside
        # user/nice; adding them again would count that time twice.
        total = sum(fields[:8])
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)  # idle + iowait
        return total, idle
    return None


def _cpu_percent(total_delta: int, idle_delta: int) -> dict[str, Any]:
    if total_delta <= 0 or idle_delta < 0 or idle_delta > total_delta:
        return _unknown_cpu()
    percent = (1.0 - (idle_delta / total_delta)) * 100.0
    return {"state": "ok", "utilization_percent": round(min(100.0, max(0.0, percent)), 1)}


def _cpu_reading() -> dict[str, Any]:
    global _CPU_PREVIOUS
    first = _cpu_times(_read_text(PROC_STAT))
    if first is None:
        return _unknown_cpu()
    previous = _CPU_PREVIOUS
    if previous is None:
        # First call: one short second sample so the panel still shows a number
        # immediately.  The window is capped well below 0.1 s; if it yields no
        # usable delta the section stays unknown (never a fabricated 0).
        time.sleep(FIRST_SAMPLE_WINDOW_SECONDS)
        again = _cpu_times(_read_text(PROC_STAT))
        if again is None:
            return _unknown_cpu()
        _CPU_PREVIOUS = (time.monotonic(), again[0], again[1])
        return _cpu_percent(again[0] - first[0], again[1] - first[1])
    _, total, idle = previous
    _CPU_PREVIOUS = (time.monotonic(), first[0], first[1])
    return _cpu_percent(first[0] - total, first[1] - idle)


def _memory_reading() -> dict[str, Any]:
    text = _read_text(PROC_MEMINFO)
    if not text:
        return _unknown_memory()
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if not rest:
            continue
        try:
            values[key.strip()] = int(rest.split()[0])  # kB
        except (ValueError, IndexError):
            continue
    total_kb = values.get("MemTotal")
    available_kb = values.get("MemAvailable")
    if total_kb is None or available_kb is None:
        return _unknown_memory()
    if total_kb <= 0 or available_kb < 0 or available_kb > total_kb:
        return _unknown_memory()
    return {"state": "ok", "total_bytes": total_kb * 1024, "available_bytes": available_kb * 1024}


# ---------------------------------------------------------------------------
# Disk and GPU
# ---------------------------------------------------------------------------
def _disk_usage() -> tuple:
    """Single seam for the fixed root filesystem reading (tests patch this)."""
    return shutil.disk_usage(ROOT_PATH)


def _disk_reading() -> dict[str, Any]:
    total, used, free = _disk_usage()
    total, used, free = int(total), int(used), int(free)
    if total <= 0 or used < 0 or free < 0:
        return _unknown_disk()
    # used + free can be smaller than total (reserved blocks) but never larger.
    if used > total or free > total or used + free > total:
        return _unknown_disk()
    return {"state": "ok", "total_bytes": total, "used_bytes": used, "free_bytes": free}


def _nvidia_smi() -> Any:
    """One fixed query; no user input, no shell, no process listing."""
    return subprocess.run(
        list(NVIDIA_SMI_ARGS),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=COMMAND_TIMEOUT_SECONDS,
        check=False,
        stdin=subprocess.DEVNULL,
    )


def _gpu_reading() -> dict[str, Any]:
    try:
        completed = _nvidia_smi()
    except (OSError, subprocess.SubprocessError):
        return _unknown_gpu()  # missing binary, timeout, spawn failure: unknown
    if completed.returncode != 0:
        return _unknown_gpu()
    devices: list[dict[str, Any]] = []
    seen: set[int] = set()
    for line in (completed.stdout or "").splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 5:
            continue
        index = _bounded_int(_csv_int(parts[0]), 0, MAX_GPU_DEVICES - 1)
        if index is None or index in seen:
            continue  # keep the real index; never renumber a device
        seen.add(index)
        device = {
            "index": index,
            "utilization_percent": _bounded_number(_csv_number(parts[1]), 0.0, 100.0),
            "memory_used_mib": _bounded_number(_csv_number(parts[2]), 0.0, GPU_MAX_MIB),
            "memory_total_mib": _bounded_number(_csv_number(parts[3]), 0.0, GPU_MAX_MIB),
            "temperature_c": _bounded_number(_csv_number(parts[4]), 0.0, 150.0),
        }
        used, total = device["memory_used_mib"], device["memory_total_mib"]
        if total == 0 or (used is not None and total is not None and used > total):
            # Contradictory capacity: drop the pair, keep the other readings.
            device["memory_used_mib"] = None
            device["memory_total_mib"] = None
        devices.append(device)
        if len(devices) >= MAX_GPU_DEVICES:
            break  # at most 8 devices, first eight by the driver's own index order
    return {"state": "ok", "devices": devices} if devices else _unknown_gpu()


def _csv_number(text: str) -> Optional[float]:
    """nvidia-smi prints ``[N/A]``/``[Not Supported]`` for absent sensors."""
    try:
        number = float(str(text).strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


# ---------------------------------------------------------------------------
# Slots: fixed loopback URL, no redirects, bounded read, memory-only credential
# ---------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the Bearer credential can never be forwarded."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


_SLOTS_OPENER = urllib.request.build_opener(_NoRedirect)


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


def _slots_reading(upstream_key: Any) -> dict[str, Any]:
    key = upstream_key if isinstance(upstream_key, str) else ""
    if not key or "\r" in key or "\n" in key:
        return _unknown_slots()  # no credential: do not send an unauthenticated probe
    request = urllib.request.Request(
        SLOTS_URL, method="GET",
        headers={"Accept": "application/json", "Authorization": "Bearer " + key})
    try:
        deadline = time.monotonic() + SLOTS_TIMEOUT_SECONDS
        with _SLOTS_OPENER.open(request, timeout=SLOTS_TIMEOUT_SECONDS) as response:
            raw = _read_bounded(response, SLOTS_MAX_BYTES, max(0.0, deadline - time.monotonic()))
    except Exception:  # noqa: BLE001 - closed to a section state, never to text
        return _unknown_slots()
    if raw is None:
        return _unknown_slots()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return _unknown_slots()
    if not isinstance(payload, list) or not payload or len(payload) > SLOTS_MAX_TOTAL:
        return _unknown_slots()
    busy = 0
    for item in payload:
        # Every entry must carry the documented boolean; a shape we do not
        # recognise is unknown, never "0 busy".
        if not isinstance(item, dict) or type(item.get("is_processing")) is not bool:
            return _unknown_slots()
        if item["is_processing"]:
            busy += 1
    total = len(payload)
    return {"state": "ok", "total": total, "busy": busy, "idle": total - busy}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def _identity(upstream_key: Any) -> str:
    key = upstream_key if isinstance(upstream_key, str) else ""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _safe(reader: Callable[[], dict[str, Any]], fallback: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    try:
        value = reader()
    except Exception:  # noqa: BLE001 - one section must not blank the rest
        return fallback()
    return value if isinstance(value, dict) else fallback()


def _collect_uncached(upstream_key: Any) -> dict[str, Any]:
    result = {
        "schema": SCHEMA,
        "cpu": _safe(_cpu_reading, _unknown_cpu),
        "memory": _safe(_memory_reading, _unknown_memory),
        "disk": _safe(_disk_reading, _unknown_disk),
        "gpu": _safe(_gpu_reading, _unknown_gpu),
        "slots": _safe(lambda: _slots_reading(upstream_key), _unknown_slots),
    }
    result["collected_at"] = _utc_stamp()
    return result


def collect(*, upstream_key: Optional[str] = None) -> dict[str, Any]:
    """One protocol-shaped reading; concurrent callers share one refresh."""
    identity = _identity(upstream_key)
    with _LOCK:
        now = time.monotonic()
        cached = _CACHE["value"]
        if cached is not None and _CACHE["identity"] == identity and now - _CACHE["at"] < CACHE_TTL_SECONDS:
            return copy.deepcopy(cached)
        value = _collect_uncached(upstream_key)
        _CACHE["identity"] = identity
        _CACHE["at"] = time.monotonic()
        _CACHE["value"] = value
        return copy.deepcopy(value)
