"""Dev-only server that feeds the metrics panel realistic Linux-shaped numbers.

The panel is built for the Ubuntu box the pilot runs on, where ``/proc`` provides
CPU, memory and network rates. On a macOS development machine those sources do
not exist, so every gauge would read "—" and the layout could not be reviewed.

This script serves the real app with only the two collector functions replaced,
so the browser check exercises the genuine route, the genuine admin guard and the
genuine front-end; only the numbers are synthetic. It is never used in
production and nothing in ``pilot_app`` imports it.

    python tools/metrics_preview.py --port 8910
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from pilot_app import metrics as metrics_mod  # noqa: E402
from pilot_app import web  # noqa: E402

START = dt.datetime.now(dt.timezone.utc)
_tick = {"n": 0}


def fake_host() -> dict:
    _tick["n"] += 1
    n = _tick["n"]
    # A wave so the sparkline has a visible shape, plus one honest lull.
    cpu = round(18 + 12 * math.sin(n / 3.0), 1)
    memory = round(46 + 4 * math.sin(n / 7.0), 1)
    return {
        "cpu_percent": cpu,
        "cpu_count": 2,
        "load": [round(0.15 + cpu / 100, 2), 0.21, 0.18],
        "memory": {
            "total_mb": 1966.9, "used_mb": round(1966.9 * memory / 100, 1),
            "available_mb": round(1966.9 * (100 - memory) / 100, 1), "percent": memory,
            "swap_total_mb": 0.0, "swap_used_mb": 0.0, "swap_percent": None,
        },
        "disk": {"total_gb": 59.2, "used_gb": 14.6, "free_gb": 41.7, "percent": 24.7},
        "uptime_seconds": 3 * 86400 + 7 * 3600 + n,
        "network": {"rx_kbps": round(4 + abs(math.sin(n / 5.0)) * 9, 1),
                    "tx_kbps": round(2 + abs(math.cos(n / 4.0)) * 5, 1)},
        "platform": "Linux 6.8.0-1018-generic",
        "python": "3.14.0",
    }


def fake_application(connection) -> dict:
    real = _REAL_APPLICATION(connection)
    return {
        **real,
        "messages_1h": max(real.get("messages_1h") or 0, 2),
        "messages_24h": max(real.get("messages_24h") or 0, 9),
        "sent_24h": max(real.get("sent_24h") or 0, 8),
        "average_latency_seconds": real.get("average_latency_seconds") or 81034.8,
        "latest_latency_seconds": real.get("latest_latency_seconds") or 294.0,
        "median_latency_seconds": real.get("median_latency_seconds") or 275.0,
        "p90_latency_seconds": real.get("p90_latency_seconds") or 350.0,
        "latency_samples": real.get("latency_samples") or 8,
        "database_size_mb": real.get("database_size_mb") or 0.34,
        "wal_size_mb": real.get("wal_size_mb") or 0.11,
    }


def fake_process() -> dict:
    return {"pid": 4711, "uptime_seconds": 3600.5 + _tick["n"], "rss_mb": 38.4,
            "threads": 4, "open_files": 23}


_REAL_APPLICATION = metrics_mod.application_metrics
metrics_mod._process = fake_process
metrics_mod.host_metrics = fake_host
metrics_mod.application_metrics = fake_application


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8910)
    args = parser.parse_args()
    web.db.initialize()
    server = web.create_server(args.host, args.port)
    print(f"metrics preview on http://{args.host}:{args.port} (synthetic numbers)", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
