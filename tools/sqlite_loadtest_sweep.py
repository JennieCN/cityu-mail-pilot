"""Where does this install stop being fast? Sweep the request rate and find out.

A single number ("p95 = 700 ms") says nothing useful on its own: it depends
entirely on how many people were asking at once. This runs the same workload at a
series of arrival rates against **one** database, so the only thing changing is
how hard it is being pushed, and reports the curve. The answer the operator needs
is not "how fast is it" but "how many people can use it before it hurts".

The benchmark holds the number of concurrent clients fixed and raises the rate
each client is asked for, which is the honest way to find the knee: real traffic
arrives as people, not as a rate.

Usage:
    python tools/sqlite_loadtest_sweep.py <seeded.sqlite3> [--rates 0.25,1,2,5,10]
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))


def run_point(database: Path, rate: float, clients: int, seconds: float) -> dict:
    """One sweep point, through the real load test rather than a copy of it."""
    handle = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
    handle.close()
    out = Path(handle.name)
    command = [
        sys.executable, str(HERE / "sqlite_loadtest.py"),
        "--database", str(database),
        "--clients", str(clients),
        "--rps", str(rate),
        "--seconds", str(seconds),
        "--json", str(out),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    try:
        report = json.loads(out.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        report = {"error": f"load test produced nothing usable (exit {result.returncode})",
                  "stderr": result.stderr[-2000:], "stdout": result.stdout[-2000:]}
    finally:
        with contextlib.suppress(OSError):
            out.unlink()
    report["requested_rps"] = rate
    report["clients"] = clients
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("database")
    parser.add_argument("--rates", default="0.25,1,2,5,10")
    parser.add_argument("--rps-per-client", type=float, default=0.25,
                        help="concurrent clients = ceil(rate / this); the default models "
                             "one page load every four seconds per active person")
    parser.add_argument("--min-clients", type=int, default=4)
    parser.add_argument("--seconds", type=float, default=15.0)
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    database = Path(args.database)
    rates = [float(item) for item in args.rates.split(",") if item.strip()]
    points = []
    for rate in rates:
        clients = max(args.min_clients, round(rate / args.rps_per_client))
        report = run_point(database, rate, clients, args.seconds)
        metrics = report.get("metrics", {})
        render = metrics.get("render", {})
        point = {
            "rate": rate,
            "clients": clients,
            "renders": report.get("throughput", {}).get("renders", 0),
            "achieved_rps": report.get("throughput", {}).get("achieved_rps"),
            "p50_ms": render.get("p50_ms"),
            "p95_ms": render.get("p95_ms"),
            "p99_ms": render.get("p99_ms"),
            "max_ms": render.get("max_ms"),
            "worker_pass_p95_ms": metrics.get("worker_pass", {}).get("p95_ms"),
            "errors": report.get("error_count", 0),
            "error_detail": report.get("error", ""),
        }
        points.append(point)
        print(f"rate {rate:>6.2f}/s  clients {clients:>3}  "
              f"renders {point['renders']:>4}  "
              f"p50 {point['p50_ms'] if point['p50_ms'] is not None else float('nan'):8.1f}  "
              f"p95 {point['p95_ms'] if point['p95_ms'] is not None else float('nan'):8.1f}  "
              f"max {point['max_ms'] if point['max_ms'] is not None else float('nan'):8.1f} ms  "
              f"errors {point['errors']}", flush=True)

    clean = [point for point in points if point["p95_ms"] is not None]
    knee = None
    for point in clean:
        if point["p95_ms"] > 1000:
            knee = point["rate"]
            break
    summary = {
        "database": str(database),
        "size_mb": round(database.stat().st_size / 1024 / 1024, 1),
        "points": points,
        "first_rate_above_1s_p95": knee,
        "worst_p95_under_1s": max(
            (point["p95_ms"] for point in clean if point["p95_ms"] <= 1000), default=None),
    }
    print()
    if knee is not None:
        print(f"p95 crosses one second at {knee}/s of page renders.")
    else:
        print("p95 stayed under one second at every rate tested.")
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=2, ensure_ascii=False),
                                   encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
