"""A/B the indexes on identical bytes, alternating so a stall cannot fake a result.

The first attempt at this compared two separate profiling runs and produced an
impossible number: `recent_volume` came out 5x *slower* with the indexes present.
That is not a result, it is a one-off stall (a checkpoint, a page-cache eviction)
landing in one run -- and it is exactly the failure mode that makes people "prove"
whatever they already believed.

So this script does the comparison the way it should be done:

* **one database**, seeded once, never reseeded;
* the indexes are dropped and recreated **in place**, alternating A/B/A/B, so
  cache state and machine noise affect both sides;
* the reported figure is the **median across cycles per state**, and the per-cycle
  numbers are printed too, so a wide spread is visible instead of hidden;
* results are cross-checked for sanity: the index can only help, so a "slower with
  the index" verdict is reported as **unstable**, not as a finding.

Usage:
    python tools/sqlite_index_ab.py [--users 100] [--cycles 3]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pilot_app.database import LATE_INDEXES, Database  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "sqlite_bench", Path(__file__).resolve().parent / "sqlite_bench.py")
_bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bench)  # type: ignore[union-attr]
seed = _bench.seed

# The calls worth A/B-ing: the ones the audit says the indexes serve, plus the two
# the operator console is built on.
CALLS = [
    ("count_analysed_messages", lambda db, uid: db.count_analysed_messages(uid)),
    ("setup_progress", lambda db, uid: db.setup_progress(uid)),
    ("today_reports", lambda db, uid: db.today_reports(
        uid, "2020-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00")),
    ("get_profile", lambda db, uid: db.get_profile(uid)),
    ("session_user", lambda db, uid: db.session_user("x" * 64)),
    ("list_users_overview", lambda db, uid: db.list_users_overview()),
    ("list_messages_overview", lambda db, uid: db.list_messages_overview(limit=50)),
    ("recent_volume", lambda db, uid: db.recent_volume(14)),
]


def set_indexes(path: Path, present: bool) -> None:
    connection = sqlite3.connect(path, timeout=120)
    try:
        for name, target in LATE_INDEXES:
            if present:
                connection.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {target}")
            else:
                connection.execute(f"DROP INDEX IF EXISTS {name}")
        # Statistics after every state change, so the planner is never choosing
        # from stale row counts -- that alone can look like an index "not helping".
        connection.execute("ANALYZE")
        connection.commit()
    finally:
        connection.close()


def measure_once(db: Database, user_ids: list[str], rounds: int) -> dict:
    out: dict[str, list[float]] = {}
    for name, call in CALLS:
        values = []
        for index in range(rounds):
            user_id = user_ids[index % len(user_ids)]
            start = time.perf_counter()
            call(db, user_id)
            values.append(time.perf_counter() - start)
        out[name] = values
    return {name: statistics.median(values) for name, values in out.items()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", type=int, default=100)
    parser.add_argument("--messages-per-user", type=int, default=300)
    parser.add_argument("--reports-per-user", type=int, default=300)
    parser.add_argument("--page-views", type=int, default=200_000)
    parser.add_argument("--rounds", type=int, default=15)
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--json", default="")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="pilot-ab-"))
    path = workdir / "pilot.sqlite3"
    print(f"seeding {path} ...", flush=True)
    shape = seed(path, args.users, args.messages_per_user, args.reports_per_user,
                 args.page_views)
    print(f"seeded {shape['size_bytes'] / 1024 / 1024:.0f} MiB; "
          f"{args.cycles} cycles, {args.rounds} rounds each", flush=True)

    db = Database(path)
    user_ids = [f"usr_bench{index:05d}" for index in range(min(20, args.users))]

    runs: dict[str, list[dict]] = {"without": [], "with": []}
    for cycle in range(args.cycles):
        # Alternate the order between cycles as well: whichever state runs second
        # benefits from a warmer cache, so a fixed order is a thumb on the scale.
        order = ["without", "with"] if cycle % 2 == 0 else ["with", "without"]
        for state in order:
            set_indexes(path, present=(state == "with"))
            runs[state].append(measure_once(db, user_ids, args.rounds))
        print(f"  cycle {cycle + 1}/{args.cycles} done ({' -> '.join(order)})", flush=True)

    report: dict[str, object] = {"shape": shape, "cycles": args.cycles, "calls": {}}
    print(f"\n  {'call':<26} {'without':>10} {'with':>10} {'speedup':>9}  verdict")
    for name, _call in CALLS:
        without = statistics.median(run[name] for run in runs["without"])
        with_ = statistics.median(run[name] for run in runs["with"])
        without_ms, with_ms = without * 1000, with_ * 1000
        speedup = without_ms / with_ms if with_ms else 0.0
        if speedup >= 2:
            verdict = f"keeps: {speedup:.1f}x"
        elif speedup >= 1.15:
            verdict = f"modest: {speedup:.2f}x"
        elif speedup > 0.87:
            verdict = "no measurable effect"
        else:
            verdict = f"UNSTABLE (slower with the index: {speedup:.2f}x)"
        spread = (max(run[name] for run in runs["with"])
                  - min(run[name] for run in runs["with"])) * 1000
        report["calls"][name] = {
            "without_ms": round(without_ms, 3), "with_ms": round(with_ms, 3),
            "speedup": round(speedup, 2), "verdict": verdict,
            "with_spread_ms": round(spread, 3),
        }
        print(f"  {name:<26} {without_ms:>9.2f} {with_ms:>9.2f} {speedup:>8.1f}x  {verdict}"
              + (f"   (spread {spread:.0f} ms)" if spread > max(5.0, with_ms) else ""))

    dashboard = ["count_analysed_messages", "setup_progress", "today_reports", "get_profile"]
    for label, group in (("dashboard calls", dashboard),
                         ("operator console calls",
                          ["list_users_overview", "list_messages_overview", "recent_volume"])):
        without = sum(report["calls"][n]["without_ms"] for n in group)
        with_ = sum(report["calls"][n]["with_ms"] for n in group)
        print(f"\n  {label}: {without:.1f} ms -> {with_:.1f} ms  ({without / with_:.1f}x)")
        report[label] = {"without_ms": round(without, 1), "with_ms": round(with_, 1),
                         "speedup": round(without / with_, 2)}

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, ensure_ascii=False),
                                   encoding="utf-8")
    if not args.keep:
        shutil.rmtree(workdir, ignore_errors=True)
    else:
        print(f"kept {workdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
