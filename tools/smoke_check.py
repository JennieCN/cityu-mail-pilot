"""End-to-end check after a change: the worker runs, the schema is right, a request is served.

The unit suite covers behaviour; this covers the thing a unit test cannot -- that
`initialize()` on a *fresh* file leaves a schema the running program can actually
use, that the worker's `--once` cycle completes against it, and that the web
process answers a real HTTP request. It is the check to run before deploying,
because every one of these has failed in this project's history while the tests
were green.

Usage:
    python tools/smoke_check.py
"""

from __future__ import annotations

import json
import os
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


def check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'ok ' if ok else 'FAIL'}] {label}{(' -- ' + detail) if detail else ''}")
    return ok


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="pilot-smoke-"))
    database = workdir / "pilot.sqlite3"
    os.environ["INFE_PILOT_DB"] = str(database)
    # A 32-byte base64 key: the shape `SecretBox.from_environment` requires. Not a
    # secret -- it only ever encrypts rows in this throwaway directory.
    os.environ["INFE_PILOT_MASTER_KEY"] = "AQIDBAUGBwgJCgsMDQ4PEBESExQVFhcYGRobHB0eHyA="
    os.environ.setdefault("INFE_PILOT_ORIGIN", "http://127.0.0.1:8799")
    os.environ.setdefault("INFE_PILOT_COOKIE_SECURE", "0")

    results = []
    from pilot_app.database import SCHEMA_VERSION, Database

    started = time.perf_counter()
    db = Database(database)
    db.initialize()
    first = time.perf_counter() - started
    started = time.perf_counter()
    db.initialize()
    second = time.perf_counter() - started
    print(f"initialize: {first * 1000:.1f} ms, then {second * 1000:.1f} ms")
    results.append(check("initialize() is idempotent and cheap the second time",
                         second < 0.5 and second <= max(0.05, first), f"{second:.3f}s"))

    connection = sqlite3.connect(database)
    try:
        version = connection.execute(
            "SELECT value FROM app_settings WHERE key='schema_version'").fetchone()
        journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        connection.close()

    results.append(check("schema version is stamped", 
                         version is not None and int(version[0]) == SCHEMA_VERSION,
                         f"{version[0] if version else None} == {SCHEMA_VERSION}"))
    results.append(check("journal_mode is wal", journal == "wal", journal))
    needed = {"users", "profiles", "mailboxes", "messages", "reports", "page_views",
              "token_usage", "task_states", "app_settings"}
    missing = needed - tables
    results.append(check("every table the code reads exists", not missing,
                         f"missing {sorted(missing)}" if missing else f"{len(tables)} tables"))
    # The expectations come from the lists the migration itself uses, so this
    # cannot drift: every index in LATE_INDEXES must exist, and nothing a previous
    # version added and measurement retired may still be there.
    from pilot_app.database import LATE_INDEXES, RETIRED_INDEXES

    absent = {name for name, _ in LATE_INDEXES} - indexes
    results.append(check("every index the migration adds exists", not absent,
                         f"missing {sorted(absent)}" if absent else
                         f"{len(LATE_INDEXES)} late indexes present"))
    lingering = set(RETIRED_INDEXES) & indexes
    results.append(check("retired indexes are gone", not lingering,
                         f"still present {sorted(lingering)}" if lingering else
                         f"{len(RETIRED_INDEXES)} retired, none present"))

    # A worker cycle, in a subprocess so it exercises the real entry point.
    completed = subprocess.run([sys.executable, "-m", "pilot_app.worker", "--once"],
                               capture_output=True, text=True, cwd=str(Path(__file__).parent.parent))
    results.append(check("worker --once exits 0", completed.returncode == 0,
                         (completed.stderr or "").strip()[-300:]))

    # A real HTTP request against a real server thread. `create_server` is the
    # same constructor `web.main` uses, so this exercises the production path.
    import pilot_app.web as web
    server = web.create_server("127.0.0.1", 8799)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1},
                              daemon=True)
    thread.start()
    body = ""
    status = 0
    try:
        with urllib.request.urlopen("http://127.0.0.1:8799/health", timeout=10) as response:
            status = response.status
            body = response.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        body = f"{exc}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    version_ok = False
    try:
        version_ok = bool(json.loads(body).get("version"))
    except json.JSONDecodeError:
        pass
    results.append(check("/health answers 200 with a version", status == 200 and version_ok,
                         f"status {status}, body {body[:120]}"))

    print()
    failed = results.count(False)
    print(f"{len(results) - failed}/{len(results)} checks passed")
    if failed:
        print(f"scratch directory kept for inspection: {workdir}")
        return 1
    import shutil
    shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
