"""Every index this project adds must justify itself. This is how.

An index is not free: every `INSERT` maintains it, every backup carries it, and a
wrong one can even make the planner choose worse. One index in this change was
added on a plausible theory, measured, and turned out to be worth nothing (see
`RETIRED_INDEXES`); this script is the check that caught it, generalised to all of
them.

For each index it runs the query the index exists for in three states, against one
database:

1. as shipped (whatever SQLite wants to do),
2. with that index **dropped**, so the difference is the index's entire effect,
3. with `ANALYZE` statistics refreshed first, so the planner is not choosing on
   guesswork -- a plan chosen without statistics is evidence about the statistics,
   not about the index.

The index is restored afterwards. `ANALYZE` writes an `sqlite_stat1` table, which
is itself a schema change; it is dropped again at the end so the database is left
as it was found.

Usage:
    python tools/sqlite_index_audit.py <seeded.sqlite3> [--users 20] [--rounds 40]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pilot_app.database import LATE_INDEXES, REJECTED_INDEXES  # noqa: E402

# The query each index exists to serve. `None` params means "does not need any".
CASES = {
    "idx_messages_user_status": {
        "why": "`count_analysed_messages` / `setup_progress`, on every dashboard load, "
               "and the console's queue depth",
        "sql": "SELECT COUNT(*) FROM messages WHERE user_id=? AND status!='skipped'",
        "params": "user",
        "aggregate": True,
    },
    "idx_messages_user_received": {
        "why": "`messages_between` / the daily digest's window read",
        "sql": ("SELECT id,subject FROM messages WHERE user_id=? "
                "AND received_at>=? AND received_at<? ORDER BY received_at ASC"),
        "params": "user_window",
        "aggregate": True,
    },
    "idx_messages_received": {
        "why": "`list_messages_overview` ordering the whole fleet by arrival",
        "sql": ("SELECT m.id FROM messages m JOIN users u ON u.id=m.user_id "
                "ORDER BY m.received_at DESC LIMIT 50"),
        "params": "none",
    },
    "idx_reports_message_kind": {
        "why": "`report_for_message` -- 'is there already a report for this mail'",
        "sql": "SELECT * FROM reports WHERE message_id=? AND kind='immediate'",
        "params": "message",
    },
}


def params_for(kind: str, user_id: str, window: tuple[str, str], message_id: str) -> tuple:
    if kind == "user":
        return (user_id,)
    if kind == "user_window":
        return (user_id, window[0], window[1])
    if kind == "message":
        return (message_id,)
    return ()


def measure(path: Path, sql: str, params: tuple, rounds: int, per_round: int,
            users: list[str] = ()) -> dict:
    """Time `sql`.

    When `users` is given, the statement is run once per user (with `params[0]`
    replaced by that user) so the timing is the **aggregate** a page render or a
    console load actually pays -- a single user's row set is too small for the
    difference between a scan and a seek to show up, which is how a full table scan
    hid on the dashboard for so long.
    """
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        plan = [str(row[-1]) for row in connection.execute(
            "EXPLAIN QUERY PLAN " + sql, params).fetchall()]
        values = []
        for _ in range(rounds):
            start = time.perf_counter()
            for count in range(per_round):
                if users:
                    connection.execute(sql, (users[count % len(users)],) + params[1:]).fetchall()
                else:
                    connection.execute(sql, params).fetchall()
            values.append((time.perf_counter() - start) / per_round)
        return {"mean_ms": round(statistics.fmean(values) * 1000, 3),
                "p50_ms": round(statistics.median(values) * 1000, 3),
                "plan": plan}
    finally:
        connection.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database")
    parser.add_argument("--users", type=int, default=20)
    parser.add_argument("--rounds", type=int, default=30)
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    path = Path(args.database)
    connection = sqlite3.connect(path, timeout=60)
    connection.row_factory = sqlite3.Row
    try:
        user_ids = [str(row[0]) for row in connection.execute(
            "SELECT id FROM users LIMIT ?", (args.users,)).fetchall()]
        message_id = connection.execute(
            "SELECT id FROM messages LIMIT 1").fetchone()[0]
        window = ("2020-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00")
        existing = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
    finally:
        connection.close()

    per_round = max(1, len(user_ids))
    report = {"database": str(path), "users": len(user_ids), "indexes": {}}

    # Both lists are audited: the shipped ones must keep earning their place, and
    # the rejected ones are re-checked so a schema or query change cannot quietly
    # make a rejection obsolete. The rejected ones are created here (and dropped
    # again at the end) because they are not in the database.
    definitions = dict(LATE_INDEXES)
    targets = list(LATE_INDEXES) + [(name, definitions[name]) for name in REJECTED_INDEXES
                                    if name in definitions]
    rejected = set(REJECTED_INDEXES)

    print(f"{path}\n  auditing {len(targets)} indexes over {len(user_ids)} users\n")
    print(f"  {'index':>28} {'as shipped':>12} {'index dropped':>14} {'+ANALYZE':>10}  verdict")

    for name, target in targets:
        case = CASES.get(name)
        if case is None:
            print(f"  {name:>28}  (no query case defined -- add one before shipping)")
            continue
        params = params_for(case["params"], user_ids[0], window, message_id)
        # Per-user statements are run across every sampled user, so the number is
        # the aggregate a page or console load pays -- and so a full table scan
        # cannot hide behind one user's small row set.
        aggregate = user_ids if case.get("aggregate") else []

        if name in rejected:
            # Measure the rejected candidate *with* the index present, by creating
            # it, so the comparison is about the index rather than about its
            # absence from this database.
            connection = sqlite3.connect(path, timeout=60)
            try:
                connection.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {target}")
                connection.commit()
            finally:
                connection.close()

        shipped = measure(path, case["sql"], params, args.rounds, per_round, aggregate)

        connection = sqlite3.connect(path, timeout=60)
        try:
            connection.execute(f"DROP INDEX IF EXISTS {name}")
            connection.commit()
        finally:
            connection.close()
        without = measure(path, case["sql"], params, args.rounds, per_round, aggregate)

        connection = sqlite3.connect(path, timeout=60)
        try:
            connection.execute("ANALYZE")
            connection.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {target}")
            connection.execute("ANALYZE")
            connection.commit()
        finally:
            connection.close()
        analysed = measure(path, case["sql"], params, args.rounds, per_round, aggregate)

        connection = sqlite3.connect(path, timeout=60)
        try:
            if name in rejected:
                connection.execute(f"DROP INDEX IF EXISTS {name}")
            else:
                connection.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {target}")
            connection.execute("DROP TABLE IF EXISTS sqlite_stat1")
            connection.commit()
        finally:
            connection.close()

        gain = without["mean_ms"] / max(0.001, shipped["mean_ms"])
        rescued = without["mean_ms"] / max(0.001, analysed["mean_ms"])
        if gain >= 1.5:
            verdict = f"keeps: {gain:.1f}x faster than without"
        elif rescued >= 1.5:
            verdict = f"keeps, but only with ANALYZE: {rescued:.1f}x"
        else:
            verdict = f"reject: only {gain:.2f}x, not worth its write cost"
        if name in rejected:
            verdict = ("rejection still holds: " if gain < 1.5 else
                       f"RECONSIDER: now worth {gain:.1f}x") + f" ({gain:.2f}x)"
        report["indexes"][name] = {
            "why": case["why"], "shipped": shipped, "without": without,
            "analysed": analysed, "gain": round(gain, 2), "verdict": verdict,
            "status": "rejected" if name in rejected else "shipped",
        }
        print(f"  {name:>28} {shipped['mean_ms']:>11.3f} {without['mean_ms']:>13.3f} "
              f"{analysed['mean_ms']:>9.3f}  {verdict}")

    print()
    for name, entry in report["indexes"].items():
        print(f"  {name} -- {entry['why']}")
        print(f"      shipped : {' | '.join(entry['shipped']['plan'])}")
        print(f"      dropped : {' | '.join(entry['without']['plan'])}")

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, ensure_ascii=False),
                                   encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
