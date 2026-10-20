"""What 100 people using the app at once does to this one SQLite file.

Every other script here runs one call at a time in a loop, which measures *cost*
but not *contention* -- and contention is the failure mode that matters on a
single-file database: one writer makes every reader wait, and a checkpoint makes
every writer wait. This one drives a realistic mix from many threads at once, for
a realistic arrival rate, and reports what a person would experience: the p95 of a
page render, and whether anything failed.

The load model, stated so it can be argued with:

* 100 accounts, and the pilot's honest assumption is that a tenth of them open
  the app in any given hour (`--active-fraction 0.1`).
* A session is ~6 page views over ~90 seconds (`--views 6`).
* So ~10 people x 6 views / 5400 s is a floor of ~0.011 requests/s, and this
  script runs at `--rps` (default 2.0) -- roughly 180x that floor, i.e. the whole
  fleet opening the app inside the same minute, twice as fast again. If this
  passes, the ordinary case has a large margin.

Each page render performs the same reads `web.py`'s `/api/me` handler does, and
each request also pays the two things *every* request pays -- a session lookup and
a visit insert -- because those are where write contention enters the read path.

Usage:
    python tools/sqlite_loadtest.py [--users 100] [--rps 2] [--seconds 20]
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import random
import shutil
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pilot_app.database import Database, utc_now  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "sqlite_bench", Path(__file__).resolve().parent / "sqlite_bench.py")
_bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bench)  # type: ignore[union-attr]
seed, summarise = _bench.seed, _bench.summarise


class Recorder:
    """Latencies grouped by which operation paid them."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.samples: dict[str, list[float]] = {}
        self.errors: list[str] = []

    def add(self, kind: str, seconds: float) -> None:
        with self.lock:
            self.samples.setdefault(kind, []).append(seconds)

    def fail(self, kind: str, exc: BaseException) -> None:
        with self.lock:
            self.errors.append(f"{kind}: {type(exc).__name__}: {exc}")


def render_page(db: Database, api: object, user_id: str) -> None:
    """The read set of one `/api/me`, plus the two writes every request pays."""
    # 1. session lookup (every authenticated request)
    db.session_user("x" * 64)
    # 2. the dashboard handler's reads, in handler order
    db.get_profile(user_id)
    db.get_mailbox(user_id)
    db.get_connection(user_id, "model")
    db.get_connection(user_id, "search")
    db.today_reports(user_id, "2020-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00")
    db.task_states(user_id)
    db.count_analysed_messages(user_id)
    db.active_announcement_for(user_id)
    db.count_pending_announcements(user_id)
    db.setup_progress(user_id)
    # 3. the two writes on the request path: "last seen" and the visit row
    db.touch_last_seen(user_id)
    db.record_page_view(created_at=utc_now(), path="/app", client_hash="e" * 32,
                        source="live", member=True)


def worker(db: Database, recorder: Recorder, api: object, user_ids: list[str],
           rps: float, stop: threading.Event, seed_value: int) -> None:
    rng = random.Random(seed_value)
    interval = 1.0 / rps
    next_at = time.perf_counter()
    while not stop.is_set():
        next_at += interval
        delay = next_at - time.perf_counter()
        if delay > 0:
            stop.wait(delay)
        else:
            next_at = time.perf_counter()
        user_id = rng.choice(user_ids)
        start = time.perf_counter()
        try:
            render_page(db, api, user_id)
        except Exception as exc:  # noqa: BLE001
            recorder.fail("render", exc)
            continue
        recorder.add("render", time.perf_counter() - start)


def background_worker(db: Database, recorder: Recorder, stop: threading.Event,
                      interval: float, seed_value: int) -> None:
    """The worker service, doing what it does every pass.

    Not a stand-in: `due_messages` + `active_mailboxes` + `recover_inflight` is
    exactly `worker.main`'s queue loop, and `analytics`'s purge runs hourly from a
    request thread. Both write, both are on the same file as the readers, and the
    point of running them here is to see whether either one is visible to a person
    waiting for a page.
    """
    while not stop.is_set():
        start = time.perf_counter()
        try:
            db.due_messages(200)
            db.active_mailboxes()
            db.recover_inflight()
            db.update_mailbox_poll(f"mbx_bench{seed_value:05d}", last_uid=1, uid_validity="1")
        except Exception as exc:  # noqa: BLE001
            recorder.fail("worker", exc)
        else:
            recorder.add("worker_pass", time.perf_counter() - start)
        stop.wait(interval)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", type=int, default=100)
    parser.add_argument("--messages-per-user", type=int, default=300)
    parser.add_argument("--reports-per-user", type=int, default=300)
    parser.add_argument("--page-views", type=int, default=200_000)
    parser.add_argument("--clients", type=int, default=24)
    parser.add_argument("--rps", type=float, default=2.0)
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--worker-interval", type=float, default=5.0)
    parser.add_argument("--database", default="",
                        help="reuse an existing seeded database instead of building one; "
                             "the migration is idempotent, so this is safe and makes it "
                             "possible to compare settings on identical bytes")
    parser.add_argument("--json", default="")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="pilot-load-"))
    if args.database:
        path = Path(args.database)
        if not path.exists():
            print(f"no such database: {path}")
            return 2
        shape = {"bytes": path.stat().st_size}
        print(f"reusing {path} ({path.stat().st_size / 1024 / 1024:.1f} MiB)", flush=True)
    else:
        path = workdir / "pilot.sqlite3"
        print(f"seeding {path} ...", flush=True)
        shape = seed(path, args.users, args.messages_per_user, args.reports_per_user,
                     args.page_views)
        shape["size_mb"] = round(shape["size_bytes"] / 1024 / 1024, 1)
        print(f"seeded {shape['size_mb']} MiB", flush=True)

    db = Database(path)
    db.initialize()
    user_ids = [f"usr_bench{index:05d}" for index in range(args.users)]
    recorder = Recorder()
    stop = threading.Event()
    # Each thread opens its own connections, so the thread count is the real
    # concurrency: `TasksMax=96` in the unit file is the ceiling this must stay
    # under, with room for the worker.
    per_client_rps = args.rps / args.clients
    clients = [threading.Thread(target=worker,
                                args=(db, recorder, None, user_ids, per_client_rps,
                                      stop, index))
               for index in range(args.clients)]
    housekeeping = threading.Thread(
        target=background_worker,
        args=(db, recorder, stop, args.worker_interval, 1))

    started = time.perf_counter()
    for thread in clients:
        thread.start()
    housekeeping.start()
    time.sleep(args.seconds)
    stop.set()
    for thread in clients:
        thread.join()
    housekeeping.join()
    elapsed = time.perf_counter() - started

    expected = args.rps * args.seconds
    report = {
        "shape": shape,
        "config": {"clients": args.clients, "target_rps": args.rps,
                   "seconds": round(elapsed, 2),
                   "user_requests_per_second": round(args.rps / args.users, 4)},
        "metrics": {kind: summarise(kind, values) for kind, values in recorder.samples.items()},
        "errors": recorder.errors[:10],
        "error_count": len(recorder.errors),
    }
    renders = report["metrics"].get("render", {})
    report["throughput"] = {
        "renders": renders.get("n", 0),
        "expected": round(expected),
        "achieved_rps": round(renders.get("n", 0) / elapsed, 2),
    }

    print(f"\nran {elapsed:.1f}s, {renders.get('n', 0)} page renders "
          f"(target {expected:.0f})")
    for kind, summary in report["metrics"].items():
        print(f"  {kind:<12} n={summary['n']:<6} mean {summary['mean_ms']:8.2f} ms  "
              f"p50 {summary['p50_ms']:8.2f}  p95 {summary['p95_ms']:8.2f}  "
              f"p99 {summary['p99_ms']:8.2f}  max {summary['max_ms']:8.2f}")
    print(f"  errors: {report['error_count']}")
    for error in report["errors"]:
        print(f"    {error}")

    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, ensure_ascii=False),
                                   encoding="utf-8")
    if not args.keep:
        shutil.rmtree(workdir, ignore_errors=True)
    else:
        print(f"kept {workdir}")
    return 1 if report["error_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
