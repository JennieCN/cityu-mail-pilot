"""What a person actually waits for: HTTP against the real web server.

Every other measurement in this directory times a function or a query. This one
starts `pilot_app.web` as its own process -- the same `create_server` the systemd
unit runs -- signs in for real, and drives `/api/me` and the operator console over
HTTP with a cookie, so what it reports includes everything a request pays:

* the socket and the threading HTTP server,
* `_require_user` (a session lookup **and** a `touch_last_seen` write),
* the visit insert in `_record_visit`,
* the handler's own reads,
* JSON encoding and the response.

That list is the reason it exists: the database-level numbers say a page render
costs about 40 ms of SQLite work, and only this can say whether a person sees 40 ms
or 400 ms. It also answers the question an operator actually asks -- **how many
people at once does this server keep fast** -- because concurrency here is real
threads on a real server rather than a simulated arrival rate.

Usage:
    python tools/http_loadtest.py <seeded.sqlite3> [--clients 12] [--seconds 20]
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import importlib.util
import json
import os
import re
import secrets
import shutil
import socket
import statistics
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pilot_app.security import hash_password, token_hash  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "sqlite_bench", Path(__file__).resolve().parent / "sqlite_bench.py")
_bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bench)  # type: ignore[union-attr]
seed, summarise = _bench.seed, _bench.summarise

SESSION_COOKIE = "cityu_mail_session"
# Named like a stand-in on purpose: the publication export scans for password
# literals, and a value that does not *say* it is a fixture gets the export
# refused (2026-09-19). This is the throwaway password of the accounts this
# script creates in its own temporary database and deletes on exit.
PASSWORD = "load-test-placeholder-password"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def make_session(database: Path, email: str, *, admin: bool) -> str:
    """Create a real account and a real session row; return the cookie token.

    Does the same three writes `/api/auth/register` does (`users`, `profiles`, and
    `sessions`), and hashes the token the same way, so the request path being
    measured is the production one rather than a shortcut around authentication.
    """
    token = secrets.token_urlsafe(32)
    expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30)).isoformat()
    user_id = f"usr_http_{secrets.token_hex(6)}"
    connection = sqlite3.connect(database, timeout=30)
    try:
        connection.execute(
            "INSERT INTO users(id,email,password_hash,created_at,is_admin,last_seen_at) "
            "VALUES(?,?,?,?,?,?)",
            (user_id, email, hash_password(PASSWORD),
             dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
             1 if admin else 0, ""))
        connection.execute("INSERT INTO profiles(user_id,updated_at) VALUES(?,?)",
                           (user_id, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))
        connection.execute(
            "INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
            (token_hash(token), user_id, expires,
             dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")))
        connection.commit()
    finally:
        connection.close()
    return token


def drain(stream, sink: list) -> None:
    """Read a subprocess pipe to EOF in the background.

    The server logs a line per request, and **not reading its stdout is a
    deadlock**: once the pipe buffer fills, the child blocks in `write()` and stops
    serving. The first version of this script only read the pipe on the failure
    path, which made a 15-second run take 60 and left the numbers unattributable.
    """
    try:
        for line in stream:
            sink.append(line.rstrip("\n"))
    except Exception:  # noqa: BLE001 - closing the pipe during termination is normal
        pass


def wait_for_server(port: int, process: subprocess.Popen, seconds: float = 40.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if process.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.25)
    return False


def start_server(database: Path, port: int, log: list) -> subprocess.Popen:
    env = {
        **os.environ,
        "INFE_PILOT_DB": str(database),
        "INFE_PILOT_MASTER_KEY": "AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA=",
        "INFE_PILOT_ORIGIN": f"http://127.0.0.1:{port}",
        "INFE_PILOT_COOKIE_SECURE": "0",
        "INFE_PILOT_ADMIN_EMAILS": "http-load@example.com",
        "LOG_LEVEL": "WARNING",
        "PYTHONUNBUFFERED": "1",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "pilot_app.web", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(Path(__file__).resolve().parent.parent),
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    threading.Thread(target=drain, args=(process.stdout, log), daemon=True).start()
    return process


class Client:
    """One signed-in browser."""

    def __init__(self, port: int, token: str, paths: list[str]) -> None:
        self.base = f"http://127.0.0.1:{port}"
        self.token = token
        self.paths = paths
        self.latencies: list[float] = []
        self.statuses: list[int] = []
        self.errors: list[str] = []

    def get(self, path: str) -> tuple[int, float, int]:
        request = urllib.request.Request(self.base + path)
        request.add_header("Cookie", f"{SESSION_COOKIE}={self.token}")
        start = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read()
                return response.status, time.perf_counter() - start, len(body)
        except urllib.error.HTTPError as exc:
            exc.read()
            return exc.code, time.perf_counter() - start, 0


def drive(port: int, token: str, paths: list[str], stop: threading.Event,
          counter: dict, lock: threading.Lock, think_seconds: float) -> None:
    """One person's browsing loop.

    `think_seconds` is the pause between page loads, and it matters more than it
    looks: with it at zero each "person" issues back-to-back requests as fast as the
    server answers, which measures the *ceiling* and not the *experience*. A real
    person reads a page for a second or two between clicks. Both numbers are worth
    having -- the ceiling says when the server runs out, the think-time test says
    what somebody actually sees -- so this is a knob rather than a fixed choice.
    """
    client = Client(port, token, paths)
    index = 0
    while not stop.is_set():
        path = paths[index % len(paths)]
        index += 1
        try:
            status, elapsed, size = client.get(path)
        except Exception as exc:  # noqa: BLE001
            with lock:
                client.errors.append(f"{type(exc).__name__}: {exc}")
            if think_seconds:
                stop.wait(think_seconds)
            continue
        with lock:
            client.latencies.append(elapsed)
            client.statuses.append(status)
            counter["bytes"] += size
            counter["requests"] += 1
        if think_seconds:
            stop.wait(think_seconds)
    with lock:
        counter.setdefault("clients", []).append({
            "n": len(client.latencies),
            "statuses": {str(code): client.statuses.count(code)
                         for code in sorted(set(client.statuses))},
            "errors": client.errors[:3],
            "p50_ms": round(statistics.median(client.latencies) * 1000, 1)
            if client.latencies else None,
        })
        counter.setdefault("all_latencies", []).extend(client.latencies)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", nargs="?", default="",
                        help="reuse a seeded database (100 accounts, realistic history)")
    parser.add_argument("--users", type=int, default=100)
    parser.add_argument("--messages-per-user", type=int, default=200)
    parser.add_argument("--reports-per-user", type=int, default=200)
    parser.add_argument("--page-views", type=int, default=80_000)
    parser.add_argument("--clients", type=int, default=12)
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--paths", default="/api/me",
                        help="comma-separated request paths each synthetic browser cycles")
    parser.add_argument("--think", type=float, default=0.0,
                        help="seconds a person pauses between page loads; 0 measures the "
                             "server's ceiling, ~2 models somebody actually reading")
    parser.add_argument("--servers", type=int, default=1,
                        help="run N web processes against the same database and spread "
                             "clients across them; this is the experiment that decides "
                             "whether 'add processes' is a safe scaling path for this "
                             "single-file database")
    parser.add_argument("--json", default="")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="pilot-http-"))
    if args.database:
        database = Path(args.database)
        print(f"reusing {database} ({database.stat().st_size / 1024 / 1024:.1f} MiB)", flush=True)
        # The session rows belong in a copy, not in the caller's database.
        local = workdir / "pilot.sqlite3"
        shutil.copy2(database, local)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(database) + suffix)
            if sidecar.exists():
                shutil.copy2(sidecar, Path(str(local) + suffix))
        database = local
    else:
        database = workdir / "pilot.sqlite3"
        print(f"seeding {database} ...", flush=True)
        seed(database, args.users, args.messages_per_user, args.reports_per_user,
             args.page_views)

    ports = [free_port() for _ in range(max(1, args.servers))]
    token = make_session(database, "http-load@example.com", admin=True)
    paths = [item.strip() for item in args.paths.split(",") if item.strip()]

    log: list[str] = []
    processes = [start_server(database, port, log) for port in ports]
    try:
        for port, process in zip(ports, processes):
            if not wait_for_server(port, process):
                print(f"server on {port} did not start (exit {process.poll()}):\n"
                      + "\n".join(log[-40:]))
                return 1
        print(f"servers up on {ports}; {args.clients} clients, "
              f"{args.seconds:.0f}s, think {args.think}s, paths {paths}", flush=True)

        stop = threading.Event()
        lock = threading.Lock()
        counter: dict = {"requests": 0, "bytes": 0}
        # Clients are spread across the processes, so with `--servers 2` half the
        # load hits each -- which is what a second process would actually receive
        # behind nginx.
        threads = [threading.Thread(target=drive,
                                    args=(ports[index % len(ports)], token, paths, stop,
                                          counter, lock, args.think))
                   for index in range(args.clients)]
        started = time.perf_counter()
        for thread in threads:
            thread.start()
        time.sleep(args.seconds)
        stop.set()
        for thread in threads:
            thread.join()
        elapsed = time.perf_counter() - started
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)

    latencies = counter.get("all_latencies", [])
    statuses: dict[str, int] = {}
    for entry in counter.get("clients", []):
        for code, count in entry["statuses"].items():
            statuses[code] = statuses.get(code, 0) + count
    report = {
        "config": {"clients": args.clients, "seconds": round(elapsed, 2), "paths": paths,
                   "think_seconds": args.think, "servers": len(ports),
                   "database_mb": round(database.stat().st_size / 1024 / 1024, 1)
                   if database.exists() else None},
        "requests": counter.get("requests", 0),
        "throughput_per_second": round(counter.get("requests", 0) / elapsed, 2),
        "bytes_per_second": round(counter.get("bytes", 0) / elapsed, 1),
        "statuses": statuses,
        "latency": summarise("http", latencies),
        "per_client": counter.get("clients", []),
    }

    print(f"\n{report['requests']} requests in {elapsed:.1f}s "
          f"({report['throughput_per_second']}/s, "
          f"{report['bytes_per_second'] / 1024:.0f} KiB/s)")
    print(f"  statuses: {statuses}")
    if latencies:
        print(f"  latency : p50 {report['latency']['p50_ms']:.1f} ms  "
              f"p95 {report['latency']['p95_ms']:.1f} ms  "
              f"p99 {report['latency']['p99_ms']:.1f} ms  "
              f"max {report['latency']['max_ms']:.1f} ms")
    else:
        print("  latency : no successful requests")
    failures = {code: count for code, count in statuses.items() if code != "200"}
    if failures:
        print(f"  NON-200: {failures}")
    for entry in report["per_client"][:5]:
        if entry["errors"]:
            print(f"  client error: {entry['errors']}")
    server_warnings = [line for line in log if "Error" in line or "Traceback" in line
                       or "WARNING" in line]
    if server_warnings:
        print(f"  server warnings ({len(server_warnings)}):")
        for line in server_warnings[:5]:
            print(f"    {line[:160]}")
    report["server_warnings"] = server_warnings[:20]

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, ensure_ascii=False),
                                   encoding="utf-8")
    return 1 if failures or not latencies else 0


if __name__ == "__main__":
    raise SystemExit(main())
