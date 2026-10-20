"""Measure the SQLite access pattern this app actually uses.

Not a general SQLite benchmark: every timing here corresponds to a call the
running program makes on a real request or worker pass -- ``Database.connect``
per operation, ``PRAGMA journal_mode=WAL`` on every one of them, the dashboard's
handful of reads, the worker's queue poll, the page-view insert on every
uncached request.

Usage:
    python tools/sqlite_bench.py [--users 100] [--scale 1.0] [--json out.json]
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import statistics
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pilot_app.database import Database, utc_now  # noqa: E402

import sqlite3  # noqa: E402

# What one page of the app costs in connections. Counted from web.py: session
# lookup + last-seen stamp on every authenticated request, then the dashboard
# handler's own reads, then analytics' insert.
CONNECTIONS_PER_REQUEST = 15
# Reports are the bulk of the bytes: a markdown digest per analysed mail.
REPORT_BYTES = 30_000
# The stored mail body is capped at 20 000 characters by insert_message.
BODY_BYTES = 20_000


def _payload(size: int) -> bytes:
    """`size` bytes that behave like what the real database holds: **ciphertext**.

    This was `b"m" * size` and `b"b" * size` for a long time, and that one detail
    invalidated every size measurement taken from it -- a 30 000-byte run of one
    repeated byte gzips to 65 bytes, so a "990 MiB database" compressed to 12 MiB
    and the backup numbers derived from it were nonsense
    (`docs/sqlite-performance-2026-09-18.md` §6 carries a correction).

    Real `reports.body_markdown` and `messages.body` are AES-GCM envelopes from
    `service.encrypt_report` / `SecretBox.encrypt`, which are incompressible
    because what they hold is mostly base64 of ciphertext plus a nonce and a tag.
    Random bytes are the honest stand-in: they have the same incompressibility and
    the same size, which is what the page count and the disk maths depend on.
    """
    return os.urandom(size)


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
    return ordered[index]


def summarise(name: str, values: list[float]) -> dict:
    return {
        "name": name,
        "n": len(values),
        "mean_ms": round(statistics.fmean(values) * 1000, 3) if values else 0.0,
        "p50_ms": round(percentile(values, 0.50) * 1000, 3),
        "p95_ms": round(percentile(values, 0.95) * 1000, 3),
        "p99_ms": round(percentile(values, 0.99) * 1000, 3),
        "max_ms": round(max(values) * 1000, 3) if values else 0.0,
    }


def timed(callable_, *args, **kwargs):
    start = time.perf_counter()
    result = callable_(*args, **kwargs)
    return result, time.perf_counter() - start


# ---------------------------------------------------------------------------
# seeding
# ---------------------------------------------------------------------------


def _stamp(days_ago: float) -> str:
    """A second-precision UTC timestamp `days_ago` in the past.

    Every row used to carry ``utc_now()``, which quietly made the whole benchmark
    optimistic: a range scan of ``created_at >= (now - 14 days)`` then matched
    *nothing*, and the operator panels looked fast for a reason that does not hold
    on a real install.
    """
    moment = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago)
    return moment.isoformat(timespec="seconds")


def seed(path: Path, users: int, messages_per_user: int, reports_per_user: int,
         page_views: int, *, history_days: int = 180, realistic: bool = True) -> dict:
    """Fill a database that looks like ``users`` accounts after ``history_days``.

    ``realistic=True`` (the default) is the shape production actually has, and the
    difference is not cosmetic:

    * a mail that was analysed and delivered has its **body cleared** (the privacy
      page promises this in as many words, and ``service`` never keeps one), so
      ``sent``/``skipped`` rows are small;
    * only a handful of rows are ever ``pending``/``failed`` -- a queue with
      thousands of rows is a broken install, not a busy one.

    Seeding every row with a 20 KiB body and a one-in-six chance of ``pending``
    produced a 1.5 GiB ``messages`` table with 10 000 due rows, which made
    ``due_messages`` look like a 300 ms query and hid what actually is slow.
    ``realistic=False`` reproduces the pessimistic shape on purpose, for the
    "what if the queue is backed up" question.
    """
    db = Database(path)
    db.initialize()
    now = _stamp(0)
    rng = __import__("random").Random(20260917)
    pending_per_user = 2 if realistic else max(1, messages_per_user // 6)

    with db.connect() as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        for index in range(users):
            user_id = f"usr_bench{index:05d}"
            registered = _stamp(history_days + index % 30)
            connection.execute(
                "INSERT INTO users(id,email,password_hash,created_at,last_seen_at) VALUES(?,?,?,?,?)",
                (user_id, f"bench{index}@example.com", "x" * 60, registered, now),
            )
            connection.execute(
                "INSERT INTO profiles(user_id,updated_at) VALUES(?,?)", (user_id, now))
            mailbox_id = f"mbx_bench{index:05d}"
            connection.execute(
                """INSERT INTO mailboxes(id,user_id,email,report_to,imap_host,imap_port,
                       smtp_host,smtp_port,encrypted_password,uid_validity,last_uid,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (mailbox_id, user_id, f"bench{index}@example.com", f"bench{index}@example.com",
                 "imap.example.com", 993, "smtp.example.com", 465, b"x" * 80, "1", 10_000, now),
            )
            connection.execute(
                """INSERT INTO connections(id,user_id,kind,provider,model,encrypted_api_key,updated_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (f"conn_bench{index}", user_id, "model", "deepseek", "deepseek-chat",
                 b"y" * 60, now),
            )
            connection.execute(
                """INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)""",
                (uuid.uuid4().hex * 2, user_id, "2099-01-01T00:00:00+00:00", now),
            )
            messages = []
            # Newest first: the pending tail is the most recent mail, which is what
            # a live install looks like between two queue passes.
            for number in range(messages_per_user):
                uid = 1000 + number
                days_ago = history_days * (number + 1) / (messages_per_user + 1)
                received = _stamp(days_ago)
                if number < pending_per_user:
                    status = "failed" if number % 3 == 2 else "pending"
                else:
                    status = "skipped" if number % 8 == 7 else "sent"
                # What the database actually holds for each status.
                if status in ("sent", "skipped"):
                    body = b""
                else:
                    body = _payload(BODY_BYTES)
                messages.append((
                    f"msg_{index}_{number}", user_id, mailbox_id, "1", uid,
                    f"<{index}.{number}@mail.example.com>",
                    "CityU 教务通知：选课与考试安排 " + str(number),
                    "教务处", "registry@cityu.edu.hk", received,
                    rng.choice(["normal", "high"]), body, status,
                    "sender_filter" if status == "skipped" else "",
                    1 if status == "sent" else 0, received,
                ))
            connection.executemany(
                """INSERT INTO messages(id,user_id,mailbox_id,uid_validity,imap_uid,message_key,
                       subject,sender_name,sender_address,received_at,importance,body,status,
                       skip_reason,attempts,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                messages,
            )
            connection.executemany(
                """INSERT INTO reports(id,user_id,message_id,kind,subject,body_markdown,status,
                       sent_to,report_date,created_at,sent_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                [(f"rep_{index}_{number}", user_id, f"msg_{index}_{number}", "immediate",
                  "CityU 邮件摘要", _payload(REPORT_BYTES), "sent",
                  f"bench{index}@example.com", _stamp(0)[:10], _stamp(0), _stamp(0))
                 for number in range(reports_per_user)],
            )
            connection.executemany(
                """INSERT INTO token_usage(id,user_id,message_id,kind,provider,model,
                       input_tokens,output_tokens,total_tokens,cost,currency,on_platform,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(f"tok_{index}_{number}", user_id, f"msg_{index}_{number}", "immediate",
                  "deepseek", "deepseek-chat", 4000, 900, 4900, 0.0012, "USD", 1,
                  _stamp(history_days * (number + 1) / (reports_per_user + 1)))
                 for number in range(reports_per_user)],
            )
            connection.executemany(
                """INSERT INTO task_states(user_id,task_key,state,task_day,subject,action,
                       deadline,priority,sender,message_id,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                [(user_id, f"task{index}{number}", "done",
                  _stamp(history_days * number / 20)[:10], "作业", "交作业",
                  "", "normal", "registry@cityu.edu.hk", f"msg_{index}_{number}",
                  _stamp(history_days * number / 20))
                 for number in range(10)],
            )
        imported = max(0, page_views - users)
        chunk = 2000
        for start in range(0, page_views, chunk):
            connection.executemany(
                """INSERT INTO page_views(id,created_at,path,status,referrer,client_hash,
                       country,country_name,city,continent,bot,member,admin,source)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(f"pv_{index:08d}",
                  _stamp(180 * (index % 200_000) / max(1, page_views)),
                  rng.choice(["/", "/app", "/api/me", "/landing"]),
                  200, "", uuid.uuid4().hex[:32], "HK", "Hong Kong", "Hong Kong", "AS",
                  1 if index % 17 == 0 else 0, 1 if index % 3 == 0 else 0, 0, "nginx")
                 for index in range(start, min(start + chunk, page_views))],
            )
        connection.execute("PRAGMA foreign_keys=ON")
    checkpoint = sqlite3.connect(path, timeout=20)
    try:
        checkpoint.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        checkpoint.close()
    return {"users": users, "messages": users * messages_per_user,
            "reports": users * reports_per_user, "page_views": page_views,
            "size_bytes": path.stat().st_size,
            "wal_bytes": Path(str(path) + "-wal").stat().st_size
            if Path(str(path) + "-wal").exists() else 0}


# ---------------------------------------------------------------------------
# micro-measurements
# ---------------------------------------------------------------------------


def bench_connect_components(path: Path, rounds: int) -> dict:
    """Split one ``Database.connect()`` into the pieces it is made of."""
    out: dict[str, list[float]] = {"open": [], "foreign_keys": [], "wal": [],
                                   "query": [], "commit_close": []}
    for _ in range(rounds):
        start = time.perf_counter()
        connection = sqlite3.connect(path, timeout=20)
        out["open"].append(time.perf_counter() - start)

        start = time.perf_counter()
        connection.execute("PRAGMA foreign_keys = ON")
        out["foreign_keys"].append(time.perf_counter() - start)

        start = time.perf_counter()
        connection.execute("PRAGMA journal_mode = WAL")
        out["wal"].append(time.perf_counter() - start)

        connection.row_factory = sqlite3.Row
        start = time.perf_counter()
        connection.execute("SELECT * FROM profiles WHERE user_id=?", ("usr_bench00001",)).fetchone()
        out["query"].append(time.perf_counter() - start)

        start = time.perf_counter()
        connection.commit()
        connection.close()
        out["commit_close"].append(time.perf_counter() - start)
    return {name: summarise(name, values) for name, values in out.items()}


def bench_database_connect(path: Path, rounds: int) -> dict:
    db = Database(path)
    values = []
    for _ in range(rounds):
        start = time.perf_counter()
        with db.connect() as connection:
            connection.execute("SELECT * FROM profiles WHERE user_id=?", ("usr_bench00001",)).fetchone()
        values.append(time.perf_counter() - start)
    return summarise("Database.connect() + tiny read", values)


def bench_session_lookup(path: Path, digests: list[str], rounds: int) -> dict:
    """The read on *every* authenticated request (web.py `session_user`)."""
    db = Database(path)
    values = []
    for index in range(rounds):
        digest = digests[index % len(digests)]
        start = time.perf_counter()
        db.session_user(digest)
        values.append(time.perf_counter() - start)
    return summarise("session_user()", values)


def bench_dashboard(path: Path, user_ids: list[str], rounds: int) -> dict:
    """The real /api/me shape, connection for connection.

    Mirrors the handler: profile, mailbox, model connection, search connection,
    today's reports, task states, analysed count, announcement, announced image,
    pending count, setup progress, verification lights.
    """
    db = Database(path)
    values = []
    connections: list[int] = []
    for index in range(rounds):
        user_id = user_ids[index % len(user_ids)]
        counter = {"n": 0}
        real_connect = db.connect

        @contextlib.contextmanager
        def counting_connect():
            counter["n"] += 1
            with real_connect() as connection:
                yield connection

        db.connect = counting_connect  # type: ignore[method-assign]
        start = time.perf_counter()
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
        values.append(time.perf_counter() - start)
        connections.append(counter["n"])
        db.connect = real_connect  # type: ignore[method-assign]
    result = summarise("/api/me dashboard reads", values)
    result["connections_per_request"] = round(statistics.fmean(connections), 1)
    return result


def bench_worker_poll(path: Path, rounds: int) -> dict:
    """What one worker pass touches: queue poll, recovery, active mailboxes."""
    db = Database(path)
    values = []
    for _ in range(rounds):
        start = time.perf_counter()
        db.due_messages(200)
        db.active_mailboxes()
        db.recover_inflight()
        values.append(time.perf_counter() - start)
    return summarise("worker queue pass (3 connections)", values)


def bench_page_view_insert(path: Path, threads: int, per_thread: int) -> dict:
    """The write that competes with readers: one connection per page view."""
    db = Database(path)
    values: list[float] = []
    errors: list[str] = []
    lock = threading.Lock()

    def worker(worker_id: int) -> None:
        local = []
        for index in range(per_thread):
            start = time.perf_counter()
            try:
                db.record_page_view(created_at=utc_now(), path="/app", client_hash="a" * 32,
                                    source="live", member=True)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(f"{type(exc).__name__}: {exc}")
                continue
            local.append(time.perf_counter() - start)
        with lock:
            values.extend(local)

    threads_list = [threading.Thread(target=worker, args=(index,)) for index in range(threads)]
    start = time.perf_counter()
    for thread in threads_list:
        thread.start()
    for thread in threads_list:
        thread.join()
    elapsed = time.perf_counter() - start
    result = summarise(f"record_page_view x{threads} threads", values)
    result["throughput_per_second"] = round(len(values) / elapsed, 1)
    result["errors"] = errors[:5]
    result["error_count"] = len(errors)
    return result


def bench_mixed(path: Path, readers: int, writer_ops: int, read_ops: int) -> dict:
    """Readers and a writer at once: the case a busy pilot actually is."""
    db = Database(path)
    user_ids = [f"usr_bench{index:05d}" for index in range(min(20, 100))]
    read_values: list[float] = []
    write_values: list[float] = []
    errors: list[str] = []
    lock = threading.Lock()
    stop = threading.Event()

    def reader(worker_id: int) -> None:
        local = []
        index = 0
        while not stop.is_set() and index < read_ops:
            user_id = user_ids[(worker_id + index) % len(user_ids)]
            start = time.perf_counter()
            try:
                db.get_profile(user_id)
                db.get_mailbox(user_id)
                db.today_reports(user_id, "2020-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00")
                db.task_states(user_id)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(f"read {type(exc).__name__}: {exc}")
            local.append(time.perf_counter() - start)
            index += 1
        with lock:
            read_values.extend(local)

    def writer() -> None:
        local = []
        for _ in range(writer_ops):
            start = time.perf_counter()
            try:
                db.record_page_view(created_at=utc_now(), path="/app", client_hash="b" * 32,
                                    source="live")
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(f"write {type(exc).__name__}: {exc}")
            local.append(time.perf_counter() - start)
        with lock:
            write_values.extend(local)

    threads_list = [threading.Thread(target=reader, args=(index,)) for index in range(readers)]
    writer_thread = threading.Thread(target=writer)
    for thread in threads_list:
        thread.start()
    writer_thread.start()
    writer_thread.join()
    stop.set()
    for thread in threads_list:
        thread.join()
    result = {
        "readers": summarise(f"{readers} readers x4 reads", read_values),
        "writer": summarise("1 writer (page view)", write_values),
        "error_count": len(errors),
        "errors": errors[:5],
    }
    return result


def bench_initialize(path: Path) -> dict:
    """``Database.initialize()`` -- it runs once per process, at startup."""
    db = Database(path)
    _, first = timed(db.initialize)
    values = []
    for _ in range(3):
        _, elapsed = timed(db.initialize)
        values.append(elapsed)
    return {"first_ms": round(first * 1000, 1),
            "warm_ms": round(statistics.fmean(values) * 1000, 1)}


def bench_backup(path: Path, destination: Path) -> dict:
    """The nightly online backup, which holds a read lock the whole way."""
    start = time.perf_counter()
    with sqlite3.connect(path) as original, sqlite3.connect(destination) as backup:
        original.backup(backup)
    elapsed = time.perf_counter() - start
    return {"seconds": round(elapsed, 2),
            "source_bytes": path.stat().st_size,
            "copy_bytes": destination.stat().st_size}


def bench_pragmas(path: Path) -> dict:
    with sqlite3.connect(path) as connection:
        return {
            "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
            "synchronous": connection.execute("PRAGMA synchronous").fetchone()[0],
            "cache_size": connection.execute("PRAGMA cache_size").fetchone()[0],
            "mmap_size": connection.execute("PRAGMA mmap_size").fetchone()[0],
            "page_size": connection.execute("PRAGMA page_size").fetchone()[0],
            "page_count": connection.execute("PRAGMA page_count").fetchone()[0],
            "freelist_count": connection.execute("PRAGMA freelist_count").fetchone()[0],
            "wal_autocheckpoint": connection.execute("PRAGMA wal_autocheckpoint").fetchone()[0],
        }


def plan_queries(path: Path, user_id: str) -> dict:
    """EXPLAIN QUERY PLAN for the queries that run on a hot path."""
    queries = {
        "session_user": ("SELECT s.token_hash,s.expires_at,u.id,u.email,u.status "
                         "FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?",
                         ("x",)),
        "due_messages": ("""SELECT * FROM messages WHERE status IN ('pending','failed')
                            AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                            AND NOT EXISTS (SELECT 1 FROM key_circuits c
                              WHERE c.user_id=messages.user_id AND c.kind='model'
                                AND c.open_until IS NOT NULL AND c.open_until>?)
                            ORDER BY created_at LIMIT ?""", ("2099", "2020", 200)),
        "today_reports": ("""SELECT * FROM reports WHERE user_id=? AND created_at>=? AND created_at<?""",
                          (user_id, "2020", "2099")),
        "task_states": ("SELECT * FROM task_states WHERE user_id=?", (user_id,)),
        "count_analysed": ("SELECT COUNT(*) FROM messages WHERE user_id=? AND status!='skipped'",
                           (user_id,)),
        "page_view_totals": ("""SELECT COUNT(*),COUNT(DISTINCT client_hash) FROM page_views
                                WHERE created_at>=? AND admin=0""", ("2020",)),
        "usage_user": ("SELECT * FROM token_usage WHERE user_id=? ORDER BY created_at DESC LIMIT 50",
                       (user_id,)),
        "insert_message_lookup": ("SELECT id FROM messages WHERE user_id=? AND message_key=?",
                                  (user_id, "x")),
        "announcement_active": ("SELECT * FROM announcements WHERE active=1 ORDER BY created_at DESC",
                                ()),
    }
    plans = {}
    with sqlite3.connect(path) as connection:
        for name, (sql, params) in queries.items():
            rows = connection.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
            plans[name] = [str(row[-1]) for row in rows]
    return plans


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--users", type=int, default=100)
    parser.add_argument("--messages-per-user", type=int, default=300)
    parser.add_argument("--reports-per-user", type=int, default=300)
    parser.add_argument("--page-views", type=int, default=200_000)
    parser.add_argument("--rounds", type=int, default=200)
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--json", default="")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    workdir = Path(tempfile.mkdtemp(prefix="pilot-bench-"))
    path = workdir / "pilot.sqlite3"
    print(f"building {path} ...", flush=True)
    started = time.perf_counter()
    shape = seed(path, args.users, args.messages_per_user, args.reports_per_user,
                 args.page_views)
    shape["seed_seconds"] = round(time.perf_counter() - started, 1)
    shape["size_mb"] = round(shape["size_bytes"] / 1024 / 1024, 1)
    print(f"seeded in {shape['seed_seconds']}s, {shape['size_mb']} MiB", flush=True)

    db = Database(path)
    with db.connect() as connection:
        digests = [row[0] for row in connection.execute(
            "SELECT token_hash FROM sessions LIMIT 50").fetchall()]
    user_ids = [f"usr_bench{index:05d}" for index in range(min(50, args.users))]

    report = {"shape": shape, "pragmas": bench_pragmas(path)}
    report["connect_components"] = bench_connect_components(path, args.rounds)
    report["database_connect"] = bench_database_connect(path, args.rounds)
    report["session_lookup"] = bench_session_lookup(path, digests, args.rounds)
    report["dashboard"] = bench_dashboard(path, user_ids, max(20, args.rounds // 4))
    report["worker_poll"] = bench_worker_poll(path, max(20, args.rounds // 4))
    report["page_view_write"] = bench_page_view_insert(path, args.threads, 40)
    report["mixed"] = bench_mixed(path, args.threads, 200, 100)
    report["initialize"] = bench_initialize(path)
    report["backup"] = bench_backup(path, workdir / "backup.sqlite3")
    report["query_plans"] = plan_queries(path, user_ids[0])

    print(json.dumps(report, indent=2, ensure_ascii=False))
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2, ensure_ascii=False),
                                   encoding="utf-8")
    if not args.keep:
        import shutil
        shutil.rmtree(workdir, ignore_errors=True)
    else:
        print(f"kept {workdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
