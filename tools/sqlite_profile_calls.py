"""Where one dashboard request actually spends its time, call by call.

`sqlite_bench.py` says `/api/me` costs ~477 ms at 100-user scale and that ten
connections are opened for it. Ten connections at the measured ~0.77 ms are ~8 ms,
so 469 ms is somewhere else -- and the only honest way to find it is to time every
call the handler makes. This script does that, and reports both the per-call cost
and the SQL each call runs underneath.

Usage:
    python tools/sqlite_profile_calls.py [--users 100] [--rounds 50]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pilot_app.database import Database  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "sqlite_bench", Path(__file__).resolve().parent / "sqlite_bench.py")
_bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bench)  # type: ignore[union-attr]

# The read set web.py's /api/me handler performs, in handler order. Names, not
# lambdas, so the report is readable.
DASHBOARD_CALLS = [
    ("get_profile", lambda db, uid: db.get_profile(uid)),
    ("get_mailbox", lambda db, uid: db.get_mailbox(uid)),
    ("get_connection(model)", lambda db, uid: db.get_connection(uid, "model")),
    ("get_connection(search)", lambda db, uid: db.get_connection(uid, "search")),
    ("today_reports", lambda db, uid: db.today_reports(
        uid, "2020-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00")),
    ("task_states", lambda db, uid: db.task_states(uid)),
    ("count_analysed_messages", lambda db, uid: db.count_analysed_messages(uid)),
    ("active_announcement_for", lambda db, uid: db.active_announcement_for(uid)),
    ("count_pending_announcements", lambda db, uid: db.count_pending_announcements(uid)),
    ("setup_progress", lambda db, uid: db.setup_progress(uid)),
    ("session_user", lambda db, uid: db.session_user("x" * 64)),
    ("list_reports(50)", lambda db, uid: db.list_reports(uid, 50)),
    ("usage_for_user(30)", lambda db, uid: db.usage_for_user(uid, days=30)),
    ("task_day_summaries", lambda db, uid: db.task_day_summaries(uid, 30)),
]

WORKER_CALLS = [
    ("due_messages(200)", lambda db, uid: db.due_messages(200)),
    ("active_mailboxes", lambda db, uid: db.active_mailboxes()),
    ("recover_inflight", lambda db, uid: db.recover_inflight()),
    ("daily_users", lambda db, uid: db.daily_users()),
    ("key_circuit_open", lambda db, uid: db.key_circuit_open(uid)),
    ("pending_announcement_deliveries", lambda db, uid: db.pending_announcement_deliveries(20)),
    ("open_invite_resends", lambda db, uid: db.open_invite_resends(20)),
]

ADMIN_CALLS = [
    ("list_users_overview", lambda db, uid: db.list_users_overview()),
    ("list_messages_overview", lambda db, uid: db.list_messages_overview(limit=50)),
    ("usage_overview", lambda db, uid: db.usage_overview(30)),
    ("recent_volume", lambda db, uid: db.recent_volume(14)),
    ("list_audit", lambda db, uid: db.list_audit(50)),
    ("page_view_totals", lambda db, uid: db.page_view_totals()),
    ("page_view_daily", lambda db, uid: db.page_view_daily()),
]


def profile(db: Database, calls, user_ids: list[str], rounds: int) -> list[dict]:
    out = []
    for name, call in calls:
        values = []
        for index in range(rounds):
            user_id = user_ids[index % len(user_ids)]
            start = time.perf_counter()
            try:
                call(db, user_id)
            except Exception as exc:  # noqa: BLE001
                out.append({"name": name, "error": f"{type(exc).__name__}: {exc}"})
                break
            values.append(time.perf_counter() - start)
        else:
            out.append({
                "name": name,
                "calls": len(values),
                "mean_ms": round(statistics.fmean(values) * 1000, 3),
                "p50_ms": round(_bench.percentile(values, 0.50) * 1000, 3),
                "p95_ms": round(_bench.percentile(values, 0.95) * 1000, 3),
                "max_ms": round(max(values) * 1000, 3),
            })
    return out


def query_plans(db: Database, user_id: str) -> dict:
    """The plan for the queries the profile just showed to be slow."""
    statements = {
        "count_analysed_messages": (
            "SELECT COUNT(*) FROM messages WHERE user_id=? AND status!='skipped'", (user_id,)),
        "today_reports": (
            """SELECT r.id FROM reports r JOIN messages m ON m.id=r.message_id
                WHERE r.user_id=? AND r.kind='immediate' AND m.received_at>=? AND m.received_at<?
                ORDER BY m.received_at DESC""",
            (user_id, "2020-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00")),
        "list_reports": (
            "SELECT * FROM reports WHERE user_id=? ORDER BY created_at DESC LIMIT 50", (user_id,)),
        "setup_progress_subquery": (
            "SELECT COUNT(*) FROM messages WHERE user_id=? AND status!='skipped'", (user_id,)),
        "due_messages": (
            """SELECT * FROM messages WHERE status IN ('pending','failed')
                 AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                 AND NOT EXISTS (SELECT 1 FROM key_circuits c WHERE c.user_id=messages.user_id
                   AND c.kind='model' AND c.open_until IS NOT NULL AND c.open_until>?)
               ORDER BY created_at LIMIT ?""", ("2099", "2020", 200)),
        "list_messages_overview": (
            """SELECT * FROM messages m LEFT JOIN users u ON u.id=m.user_id
               ORDER BY m.received_at DESC LIMIT 50 OFFSET 0""", ()),
        "page_view_totals": (
            """SELECT COUNT(*),COUNT(DISTINCT client_hash) FROM page_views
               WHERE created_at>=? AND admin=0""", ("2020",)),
    }
    plans = {}
    import sqlite3
    connection = sqlite3.connect(db.path)
    try:
        for name, (sql, params) in statements.items():
            rows = connection.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
            plans[name] = [str(row[-1]) for row in rows]
    finally:
        connection.close()
    return plans


def index_inventory(db: Database) -> dict:
    import sqlite3
    connection = sqlite3.connect(db.path)
    inventory: dict[str, list[str]] = {}
    try:
        rows = connection.execute(
            """SELECT tbl_name, name, COALESCE(sql,'(implicit)') FROM sqlite_master
               WHERE type='index' ORDER BY tbl_name, name""").fetchall()
        for table, name, sql in rows:
            inventory.setdefault(table, []).append(f"{name} :: {' '.join(sql.split())}")
    finally:
        connection.close()
    return inventory


def table_sizes(db: Database) -> list[dict]:
    """Bytes per table, via dbstat -- the answer to 'what makes it 1.5 GB'."""
    import sqlite3
    connection = sqlite3.connect(db.path)
    try:
        connection.execute("CREATE VIRTUAL TABLE IF NOT EXISTS temp.dbstat USING dbstat")
        rows = connection.execute(
            """SELECT name, SUM(pgsize) AS bytes, COUNT(*) AS pages FROM temp.dbstat
               WHERE aggregate=1 GROUP BY name ORDER BY bytes DESC""").fetchall()
        return [{"table": row[0], "mb": round(row[1] / 1024 / 1024, 1), "pages": row[2]}
                for row in rows]
    except sqlite3.OperationalError as exc:
        return [{"error": str(exc)}]
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", type=int, default=100)
    parser.add_argument("--messages-per-user", type=int, default=300)
    parser.add_argument("--reports-per-user", type=int, default=300)
    parser.add_argument("--page-views", type=int, default=200_000)
    parser.add_argument("--rounds", type=int, default=30)
    parser.add_argument("--drop-late-indexes", action="store_true",
                        help="drop the indexes this change adds and measure again, on the "
                             "same bytes -- this is the A/B that answers 'how much did the "
                             "indexes actually buy'")
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="pilot-profile-"))
    path = workdir / "pilot.sqlite3"
    print(f"seeding {path} ...", flush=True)
    shape = _bench.seed(path, args.users, args.messages_per_user, args.reports_per_user,
                        args.page_views)
    shape["size_mb"] = round(shape["size_bytes"] / 1024 / 1024, 1)
    print(f"seeded {shape['size_mb']} MiB", flush=True)

    db = Database(path)
    user_ids = [f"usr_bench{index:05d}" for index in range(min(50, args.users))]
    # The "before" state, if this is the A/B: `seed()` called `initialize()`, which
    # created the late indexes. Dropping exactly those and nothing else leaves the
    # two runs differing only by the indexes.
    if args.drop_late_indexes:
        import sqlite3 as _sqlite3

        from pilot_app.database import LATE_INDEXES

        connection = _sqlite3.connect(path, timeout=60)
        try:
            for name, _target in LATE_INDEXES:
                connection.execute(f"DROP INDEX IF EXISTS {name}")
            connection.execute("ANALYZE")
            connection.commit()
        finally:
            connection.close()
        print(f"dropped {len(LATE_INDEXES)} late indexes: this is the 'before' state",
              flush=True)

    report = {
        "shape": shape,
        "dashboard": profile(db, DASHBOARD_CALLS, user_ids, args.rounds),
        "worker": profile(db, WORKER_CALLS, user_ids, args.rounds),
        "admin": profile(db, ADMIN_CALLS, user_ids, max(5, args.rounds // 4)),
        "query_plans": query_plans(db, user_ids[0]),
        "indexes": index_inventory(db),
        "table_sizes": table_sizes(db),
    }
    for section in ("dashboard", "worker", "admin"):
        print(f"\n== {section} ==")
        for entry in report[section]:
            if "error" in entry:
                print(f"  {entry['name']:<32} ERROR {entry['error']}")
            else:
                print(f"  {entry['name']:<32} mean {entry['mean_ms']:8.3f} ms  "
                      f"p95 {entry['p95_ms']:8.3f} ms  max {entry['max_ms']:8.3f} ms")
    print("\n== query plans ==")
    for name, plan in report["query_plans"].items():
        print(f"  {name}: {' | '.join(plan)}")
    print("\n== table sizes (MiB) ==")
    for entry in report["table_sizes"]:
        print(f"  {entry}")
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, ensure_ascii=False),
                                   encoding="utf-8")
    shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
