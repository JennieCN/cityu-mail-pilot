"""Prove the upgrade path works on a database the size of a real install.

A migration that takes 40 seconds on a test fixture can take 20 minutes on a
1 GiB file, and the only honest way to know is to run it against one. This takes
an *existing* database (built by `tools/sqlite_bench.py --keep`), records its
schema version and query plans, runs `Database.initialize()` on it in place, and
reports what changed: how long the migration took, which indexes appeared, and
whether the plans changed from scans to seeks.

Two properties matter beyond speed:

* **It is idempotent.** Running it twice must be a no-op the second time -- that
  is the `SCHEMA_VERSION` gate, and an install that restarts must not pay for the
  migration again.
* **It does not lose rows.** The `messages` CHECK-constraint rebuild in
  `_relax_message_status_check` copies a table; this counts before and after.

Usage:
    python tools/sqlite_migration_check.py <path-to-pilot.sqlite3>
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pilot_app.database import (  # noqa: E402
    RETIRED_INDEXES, SCHEMA_VERSION, SCHEMA_VERSION_KEY, Database)

WATCHED = ("messages", "reports", "page_views", "users", "token_usage", "task_states")

PLANS = {
    "count_analysed_messages": "SELECT COUNT(*) FROM messages WHERE user_id=? AND status!='skipped'",
    "today_reports": ("SELECT r.id FROM reports r JOIN messages m ON m.id=r.message_id "
                      "WHERE r.user_id=? AND r.kind='immediate' AND m.received_at>=? AND m.received_at<? "
                      "ORDER BY m.received_at DESC"),
    "list_messages_overview": ("SELECT m.id FROM messages m JOIN users u ON u.id=m.user_id "
                               "ORDER BY m.received_at DESC LIMIT 50"),
}


def counts(path: Path) -> dict:
    connection = sqlite3.connect(path, timeout=30)
    try:
        out = {}
        for table in WATCHED:
            try:
                out[table] = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except sqlite3.OperationalError:
                out[table] = None
        return out
    finally:
        connection.close()


def schema_version(path: Path) -> int:
    connection = sqlite3.connect(path, timeout=30)
    try:
        row = connection.execute(
            "SELECT value FROM app_settings WHERE key=?", (SCHEMA_VERSION_KEY,)).fetchone()
        return int(str(row[0])) if row else 0
    except (sqlite3.OperationalError, TypeError, ValueError):
        return 0
    finally:
        connection.close()


def indexes(path: Path) -> set[str]:
    connection = sqlite3.connect(path, timeout=30)
    try:
        return {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        connection.close()


def plans(path: Path, user_id: str) -> dict:
    connection = sqlite3.connect(path, timeout=30)
    try:
        out = {}
        for name, sql in PLANS.items():
            params = {"count_analysed_messages": (user_id,),
                      "today_reports": (user_id, "2020-01-01T00:00:00+00:00",
                                        "2099-01-01T00:00:00+00:00"),
                      "list_messages_overview": ()}[name]
            out[name] = [str(row[-1]) for row in
                         connection.execute("EXPLAIN QUERY PLAN " + sql, params)]
        return out
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database")
    parser.add_argument("--user", default="usr_bench00000")
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    path = Path(args.database)
    if not path.exists():
        print(f"no such database: {path}")
        return 2
    print(f"{path}  {path.stat().st_size / 1024 / 1024:.1f} MiB")

    before = {"size_bytes": path.stat().st_size, "version": schema_version(path),
              "counts": counts(path), "indexes": sorted(indexes(path)),
              "plans": plans(path, args.user)}

    db = Database(path)
    start = time.perf_counter()
    db.initialize()
    migrated = time.perf_counter() - start

    after_first = {"size_bytes": path.stat().st_size, "version": schema_version(path),
                   "counts": counts(path), "indexes": sorted(indexes(path)),
                   "plans": plans(path, args.user)}

    start = time.perf_counter()
    db.initialize()
    second = time.perf_counter() - start

    added = sorted(set(after_first["indexes"]) - set(before["indexes"]))
    lost = sorted(set(before["indexes"]) - set(after_first["indexes"]))
    # A deliberately retired index disappearing is the migration working, not a
    # problem: `RETIRED_INDEXES` is the list of indexes that were measured, found
    # to buy nothing, and are dropped so existing installs stop paying for them.
    unexpectedly_lost = [name for name in lost if name not in RETIRED_INDEXES]
    retired = [name for name in lost if name in RETIRED_INDEXES]
    row_delta = {table: (before["counts"][table], after_first["counts"][table])
                 for table in WATCHED if before["counts"][table] != after_first["counts"][table]}

    report = {
        "database": str(path),
        "before": before,
        "after_first_run": after_first,
        "migration_seconds": round(migrated, 2),
        "second_run_seconds": round(second, 3),
        "indexes_added": added,
        "indexes_lost": lost,
        "indexes_retired": retired,
        "indexes_lost_unexpectedly": unexpectedly_lost,
        "row_delta": row_delta,
        "size_delta_mb": round((after_first["size_bytes"] - before["size_bytes"]) / 1024 / 1024, 2),
        "expected_version": SCHEMA_VERSION,
    }

    print(f"\nmigrated in {migrated:.1f}s   second run {second:.3f}s   "
          f"version {before['version']} -> {after_first['version']} "
          f"(expected {SCHEMA_VERSION})")
    print(f"size {before['size_bytes'] / 1024 / 1024:.1f} -> "
          f"{after_first['size_bytes'] / 1024 / 1024:.1f} MiB "
          f"({report['size_delta_mb']:+.1f})")
    print(f"indexes added: {', '.join(added) if added else '(none)'}")
    print(f"indexes lost : {', '.join(lost) if lost else '(none)'}"
          + (f"  (deliberately retired: {', '.join(retired)})" if retired else ""))
    print(f"row changes  : {row_delta if row_delta else '(none)'}")
    print("\nquery plans:")
    for name in PLANS:
        print(f"  {name}")
        print(f"    before: {' | '.join(before['plans'][name])}")
        print(f"    after : {' | '.join(after_first['plans'][name])}")

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, ensure_ascii=False),
                                   encoding="utf-8")
    problems = []
    if after_first["version"] != SCHEMA_VERSION:
        problems.append("version was not stamped")
    if unexpectedly_lost:
        problems.append(f"indexes disappeared: {unexpectedly_lost}")
    if row_delta:
        problems.append(f"row counts changed: {row_delta}")
    if second > max(1.0, migrated / 2):
        problems.append(f"second run cost {second:.2f}s, so the version gate is not skipping")
    for problem in problems:
        print(f"\nPROBLEM: {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
