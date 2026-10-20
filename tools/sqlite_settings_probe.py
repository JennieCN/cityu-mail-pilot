"""Which connection setting actually costs what, measured rather than assumed.

`Database.connect()` runs ``PRAGMA journal_mode = WAL`` on **every** connection --
every read, every write, fifteen times per dashboard request. The question this
script answers is not "is WAL good" (it is) but "does asking for it again on every
connection cost anything, and does it cost more under concurrency".

Two things had to be fixed before the numbers meant anything, and both are worth
knowing:

* **WAL is persistent.** Once a database is in WAL mode, ``PRAGMA journal_mode =
  WAL`` on a later connection is a cheap no-op. An earlier version of this probe
  ran all variants against *one* database, so five of the six were measuring the
  same no-op. Every variant now gets its own pristine copy.
* **Order matters as much as the setting.** Variants are run in a fresh random
  order per repetition, repeated, and reported as a median across repetitions --
  a fixed order plus a warm page cache is how a benchmark invents a result.

Usage:
    python tools/sqlite_settings_probe.py [--users 100] [--repetitions 3] [--json out.json]
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import random
import shutil
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pilot_app.database import utc_now  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "sqlite_bench", Path(__file__).resolve().parent / "sqlite_bench.py")
_bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_bench)  # type: ignore[union-attr]
seed, summarise = _bench.seed, _bench.summarise

# 256 MiB of address space for the page cache the kernel already has, and a 32 MiB
# SQLite page cache instead of the 2 MiB default. Both are *per connection* by
# default, which is why they are only worth setting once threads stop opening one
# connection each.
MMAP_SIZE = 268_435_456
CACHE_KIB = -32_000


class Variant:
    """One candidate shape for ``Database.connect()``.

    ``statement`` is what every connection runs. ``once`` is applied to the
    database file through a dedicated connection before measurement, which is
    where a persistent setting (``journal_mode``) belongs.
    """

    def __init__(self, name: str, *, statement: tuple[str, ...] = (),
                 once: tuple[str, ...] = (), note: str = "") -> None:
        self.name = name
        self.statement = statement
        self.once = once
        self.note = note

    @contextlib.contextmanager
    def connect(self, path: Path):
        connection = sqlite3.connect(path, timeout=20)
        connection.row_factory = sqlite3.Row
        try:
            for sql in self.statement:
                connection.execute(sql)
            yield connection
            connection.commit()
        finally:
            connection.close()


VARIANTS = [
    Variant(
        "A current: FK + journal_mode=WAL on every connection",
        statement=("PRAGMA foreign_keys = ON", "PRAGMA journal_mode = WAL"),
        note="what ships today",
    ),
    Variant(
        "B journal_mode once at startup, FK per connection",
        statement=("PRAGMA foreign_keys = ON",),
        once=("PRAGMA journal_mode = WAL",),
        note="the change under test",
    ),
    Variant(
        "C B + synchronous=NORMAL per connection",
        statement=("PRAGMA foreign_keys = ON", "PRAGMA synchronous = NORMAL"),
        once=("PRAGMA journal_mode = WAL",),
        note="WAL's default for new databases, not for this one",
    ),
    Variant(
        "D C + mmap + 32 MiB cache per connection",
        statement=("PRAGMA foreign_keys = ON", "PRAGMA synchronous = NORMAL",
                   f"PRAGMA mmap_size = {MMAP_SIZE}", f"PRAGMA cache_size = {CACHE_KIB}"),
        once=("PRAGMA journal_mode = WAL",),
    ),
    Variant(
        "E B + mmap + 32 MiB cache per connection (durability untouched)",
        statement=("PRAGMA foreign_keys = ON", f"PRAGMA mmap_size = {MMAP_SIZE}",
                   f"PRAGMA cache_size = {CACHE_KIB}"),
        once=("PRAGMA journal_mode = WAL",),
        note="the conservative recommendation",
    ),
    Variant(
        "F rollback journal (journal_mode=DELETE), FK per connection",
        statement=("PRAGMA foreign_keys = ON",),
        once=("PRAGMA journal_mode = DELETE",),
        note="the counterfactual: what giving up WAL would cost",
    ),
]


def fresh_copy(master: Path, path: Path, variant: Variant) -> None:
    shutil.copy2(master, path)
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    for sql in variant.once:
        connection = sqlite3.connect(path, timeout=30)
        try:
            connection.execute(sql)
        finally:
            connection.close()


def measure_reads(path: Path, variant: Variant, user_ids: list[str], rounds: int) -> list[float]:
    values: list[float] = []
    for index in range(rounds):
        user_id = user_ids[index % len(user_ids)]
        start = time.perf_counter()
        with variant.connect(path) as connection:
            connection.execute("SELECT * FROM profiles WHERE user_id=?", (user_id,)).fetchone()
        values.append(time.perf_counter() - start)
    return values


def measure_writes(path: Path, variant: Variant, threads: int, per_thread: int) -> tuple[list[float], int]:
    values: list[float] = []
    errors = 0
    lock = threading.Lock()

    def worker() -> None:
        nonlocal errors
        local = []
        for _ in range(per_thread):
            start = time.perf_counter()
            try:
                with variant.connect(path) as connection:
                    connection.execute(
                        """INSERT INTO page_views(id,created_at,path,status,referrer,client_hash,
                               country,country_name,city,continent,bot,member,admin,source)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (f"pv_{time.perf_counter_ns()}", utc_now(), "/app", 200, "", "c" * 32,
                         "HK", "Hong Kong", "Hong Kong", "AS", 0, 1, 0, "live"))
            except Exception:  # noqa: BLE001
                with lock:
                    errors += 1
                continue
            local.append(time.perf_counter() - start)
        with lock:
            values.extend(local)

    threads_list = [threading.Thread(target=worker) for _ in range(threads)]
    for thread in threads_list:
        thread.start()
    for thread in threads_list:
        thread.join()
    return values, errors


def measure_mixed(path: Path, variant: Variant, user_ids: list[str], reads_each: int) -> tuple[list[float], list[float]]:
    """Reads and writes at the same time, which is the state a live pilot is in."""
    read_values: list[float] = []
    write_values: list[float] = []
    stop = threading.Event()
    lock = threading.Lock()

    def reader(worker_id: int) -> None:
        local = []
        for index in range(reads_each):
            user_id = user_ids[(worker_id + index) % len(user_ids)]
            start = time.perf_counter()
            with variant.connect(path) as connection:
                connection.execute("SELECT * FROM profiles WHERE user_id=?", (user_id,)).fetchone()
                connection.execute("SELECT * FROM mailboxes WHERE user_id=?", (user_id,)).fetchone()
                connection.execute("SELECT * FROM task_states WHERE user_id=?", (user_id,)).fetchall()
            local.append(time.perf_counter() - start)
        with lock:
            read_values.extend(local)

    def writer() -> None:
        while not stop.is_set():
            start = time.perf_counter()
            with variant.connect(path) as connection:
                connection.execute(
                    """INSERT INTO page_views(id,created_at,path,status,referrer,client_hash,
                           country,country_name,city,continent,bot,member,admin,source)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (f"pv_{time.perf_counter_ns()}", utc_now(), "/app", 200, "", "d" * 32,
                     "HK", "Hong Kong", "Hong Kong", "AS", 0, 1, 0, "live"))
            write_values.append(time.perf_counter() - start)

    readers = [threading.Thread(target=reader, args=(index,)) for index in range(8)]
    writer_thread = threading.Thread(target=writer)
    for thread in readers:
        thread.start()
    writer_thread.start()
    for thread in readers:
        thread.join()
    stop.set()
    writer_thread.join()
    return read_values, write_values


def run_variant(master: Path, workdir: Path, variant: Variant, user_ids: list[str],
                args: argparse.Namespace, tag: str) -> dict:
    path = workdir / f"variant-{tag}.sqlite3"
    fresh_copy(master, path, variant)
    reads = measure_reads(path, variant, user_ids, args.rounds)
    dashboard = measure_reads(path, variant, user_ids, args.rounds)  # same shape, warm
    writes, write_errors = measure_writes(path, variant, args.threads, 25)
    mixed_reads, mixed_writes = measure_mixed(path, variant, user_ids, 10)
    result = {
        "name": variant.name,
        "note": variant.note,
        "statement": list(variant.statement),
        "once": list(variant.once),
        "read": summarise("read", reads),
        "warm_read": summarise("warm read", dashboard),
        "write": summarise("write", writes),
        "write_errors": write_errors,
        "mixed_read": summarise("mixed read", mixed_reads),
        "mixed_write": summarise("mixed write", mixed_writes),
    }
    path.unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        Path(str(path) + suffix).unlink(missing_ok=True)
    return result


def median_of(results: list[dict]) -> dict:
    """The median of each metric across repetitions, so one lucky run cannot win."""
    fields = ["read", "warm_read", "write", "mixed_read", "mixed_write"]
    merged = {"name": results[0]["name"], "note": results[0]["note"],
              "statement": results[0]["statement"], "once": results[0]["once"],
              "repetitions": len(results),
              "write_errors": sum(item["write_errors"] for item in results)}
    for field in fields:
        merged[field] = {
            key: round(statistics.median([item[field][key] for item in results]), 3)
            for key in ("mean_ms", "p50_ms", "p95_ms", "p99_ms", "max_ms")
        }
    return merged


def open_cost(path: Path) -> dict:
    """What a persistent ``journal_mode`` change costs when done once.

    A stray open connection holding a read snapshot is what makes the next
    ``journal_mode`` change answer "database is locked", so every connection here
    is closed by hand -- ``with sqlite3.connect(...)`` is a *transaction* context
    manager and does not close.
    """
    def one(sql: str) -> tuple[str, float]:
        connection = sqlite3.connect(path, timeout=30)
        try:
            start = time.perf_counter()
            row = connection.execute(sql).fetchone()
            return (str(row[0]) if row else ""), time.perf_counter() - start
        finally:
            connection.close()

    mode, wal = one("PRAGMA journal_mode = WAL")
    _, wal_again = one("PRAGMA journal_mode = WAL")
    _, checkpoint = one("PRAGMA wal_checkpoint(TRUNCATE)")
    return {"wal_mode_ms": round(wal * 1000, 2),
            "wal_again_ms": round(wal_again * 1000, 2),
            "checkpoint_ms": round(checkpoint * 1000, 2), "mode": mode}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", type=int, default=100)
    parser.add_argument("--messages-per-user", type=int, default=300)
    parser.add_argument("--reports-per-user", type=int, default=300)
    parser.add_argument("--page-views", type=int, default=200_000)
    parser.add_argument("--rounds", type=int, default=150)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--pessimistic", action="store_true",
                        help="seed the never-in-production shape: big bodies on every row, "
                             "a large pending backlog")
    parser.add_argument("--json", default="")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="pilot-settings-"))
    master = workdir / "master.sqlite3"
    print(f"seeding {master} (realistic={not args.pessimistic}) ...", flush=True)
    shape = seed(master, args.users, args.messages_per_user, args.reports_per_user,
                 args.page_views, realistic=not args.pessimistic)
    shape["size_mb"] = round(shape["size_bytes"] / 1024 / 1024, 1)
    print(f"seeded {shape['size_mb']} MiB", flush=True)

    user_ids = [f"usr_bench{index:05d}" for index in range(min(50, args.users))]
    collected: dict[str, list[dict]] = {variant.name: [] for variant in VARIANTS}
    rng = random.Random(4242)
    for repetition in range(args.repetitions):
        order = list(VARIANTS)
        rng.shuffle(order)
        for variant in order:
            result = run_variant(master, workdir, variant, user_ids, args,
                                 tag=f"{repetition}-{VARIANTS.index(variant)}")
            collected[variant.name].append(result)
        print(f"repetition {repetition + 1}/{args.repetitions} done", flush=True)

    report = {"shape": shape, "open_cost": open_cost(master),
              "pessimistic": args.pessimistic,
              "variants": [median_of(collected[variant.name]) for variant in VARIANTS]}

    print("\n== medians across repetitions ==")
    for entry in report["variants"]:
        print(f"\n{entry['name']}")
        print(f"   read        p50 {entry['read']['p50_ms']:7.3f}  p99 {entry['read']['p99_ms']:8.3f} ms")
        print(f"   write       p50 {entry['write']['p50_ms']:7.3f}  p99 {entry['write']['p99_ms']:8.3f} ms"
              f"   errors {entry['write_errors']}")
        print(f"   mixed read  p50 {entry['mixed_read']['p50_ms']:7.3f}  "
              f"p95 {entry['mixed_read']['p95_ms']:8.3f}  max {entry['mixed_read']['max_ms']:8.3f} ms")
        print(f"   mixed write p50 {entry['mixed_write']['p50_ms']:7.3f}  "
              f"p99 {entry['mixed_write']['p99_ms']:8.3f} ms")
    print(f"\nopen_cost {report['open_cost']}")

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
