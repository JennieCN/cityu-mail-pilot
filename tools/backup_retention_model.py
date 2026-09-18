"""How many backup copies actually accumulate, and how much disk that costs.

"14 days of copies" is not the same as 14 copies: `prune` deletes by *age* with a
floor (never fewer than `BACKUP_KEEP_MIN`) and a ceiling (`BACKUP_KEEP_MAX`), and
the count that settles out depends on how often copies are made. Daily is the timer;
**each upgrade also runs the backup**, so a week of deploys adds a week of copies.

This simulates the retention rule for real calendar days -- same loop, same order of
checks as `prune` -- for several backup cadences, then multiplies by the database
size, which *grows* as the pilot runs. The growth is the part that is easy to miss:
the newest copies are the biggest ones, and they are the ones that survive.

Usage:
    python tools/backup_retention_model.py [--years 2] [--start-mb 60]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pilot_app import backup  # noqa: E402

MIB = 1024 * 1024


def simulate(copies_per_day: float, years: float, start_mb: float, mb_per_day: float) -> dict:
    """One simulated timeline. Returns the peak number of copies and peak bytes.

    `prune`'s exact rule, applied per simulated day:
      * sort newest first, drop anything at index >= BACKUP_KEEP_MAX,
      * keep the first BACKUP_KEEP_MIN unconditionally,
      * otherwise drop it once `(now - made).days > BACKUP_KEEP_DAYS`.
    """
    keep_days = backup.BACKUP_KEEP_DAYS
    keep_min = backup.BACKUP_KEEP_MIN
    keep_max = backup.BACKUP_KEEP_MAX
    days = int(years * 365)
    step = 1.0 / copies_per_day if copies_per_day else 1.0

    pending: float = 0.0  # fractional copies carried between days
    copies: list[tuple[int, float]] = []  # (day made, size MiB)
    peak_count = 0
    peak_bytes = 0.0
    samples: list[tuple[int, int, float]] = []

    for day in range(days):
        pending += copies_per_day
        made_now = int(pending)
        pending -= made_now
        size_mb = start_mb + mb_per_day * day
        for _ in range(made_now):
            copies.append((day, size_mb))

        # `prune`: newest first by name; the name is a timestamp, so day order.
        copies.sort(key=lambda item: item[0], reverse=True)
        kept: list[tuple[int, float]] = []
        for index, (made_day, size) in enumerate(copies):
            if index >= keep_max:
                continue
            if index < keep_min:
                kept.append((made_day, size))
                continue
            if (day - made_day) > keep_days:
                continue
            kept.append((made_day, size))
        copies = kept

        total = sum(size for _day, size in copies)
        peak_count = max(peak_count, len(copies))
        peak_bytes = max(peak_bytes, total)
        if day % 30 == 0 or day == days - 1:
            samples.append((day, len(copies), total))

    return {
        "copies_per_day": copies_per_day,
        "peak_copies": peak_count,
        "peak_backup_gib": round(peak_bytes / 1024, 1),
        "final_copies": len(copies),
        "final_backup_gib": round(sum(size for _d, size in copies) / 1024, 1),
        "samples": samples,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=float, default=2.0)
    parser.add_argument("--start-mb", type=float, default=60.0,
                        help="database size when the pilot starts")
    parser.add_argument("--mb-per-day", type=float, default=0.0,
                        help="growth per day; 100 users x 3 mails/day x 34 KiB is about 10")
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    print(f"retention rule: keep {backup.BACKUP_KEEP_DAYS} days, "
          f"at least {backup.BACKUP_KEEP_MIN}, at most {backup.BACKUP_KEEP_MAX}")
    print(f"simulating {args.years:.0f} years from {args.start_mb:.0f} MiB, "
          f"growing {args.mb_per_day:.1f} MiB/day\n")
    print(f"  {'copies/day':>11} {'peak copies':>12} {'peak backups':>14} "
          f"{'live db':>10} {'total disk':>12}")

    for rate, label in ((1.0, "daily timer only"),
                        (1.5, "daily + deploy every other day"),
                        (2.0, "daily + one deploy a day"),
                        (4.0, "daily + three deploys a day")):
        result = simulate(rate, args.years, args.start_mb, args.mb_per_day)
        live = args.start_mb / 1024 + args.mb_per_day * args.years * 365 / 1024
        total = live + result["peak_backup_gib"]
        print(f"  {rate:>11.1f} {result['peak_copies']:>12} "
              f"{result['peak_backup_gib']:>11.1f} GiB {live:>8.1f} GiB {total:>10.1f} GiB"
              f"   ({label})")
    print()
    print("  读法：'peak backups' 是备份目录稳定后的最大占用，'total disk' 再加上线上库。")
    print("  每天多跑一次升级备份，就是多一整份完整副本 —— 这是最容易低估的一项。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
