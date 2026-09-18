"""How long does a database of a given size last on a disk of a given size?

The question this answers is not "how much does it use" but **"when does it fill
up, and what is the cheapest knob"**. Three things are true at once and only the
third is usually obvious:

* the live database grows and **never shrinks** (there is no retention for
  `messages`/`reports` -- see §7 of the performance doc);
* the backup directory is roughly `copies x live size`, so it grows at the *same
  rate* and is larger than the live database by the copy count;
* **and the backups cannot be compressed away.** Measured on a 915 MiB database
  whose payload is real AES-GCM ciphertext: gzip gets 1.0x (874 MiB out). An
  earlier version of this model assumed a good ratio and was wrong by 47x, because
  the benchmark had seeded repeated bytes instead of ciphertext.

So the knobs are: how many copies, how big each one is, and how many accounts.

Usage:
    python tools/disk_budget.py [--disk-gb 40] [--users 100] [--mails-per-user-day 3]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pilot_app import backup  # noqa: E402

# Per-mail resident cost, and the reason this file has a correction notice in the
# performance doc. Two different numbers were measured at different times:
#
#   31.2 KiB  a 915 MiB database holding 30 000 mails whose `reports.body_markdown`
#             rows were seeded at REPORT_BYTES = 30 000 -- i.e. the *cap*
#             (`service.REPORT_MAX_TOKENS=4000`), not what a report actually is.
#    8.4 KiB  what a real report costs. `service.py` records the measurement:
#             reports land around 2,200-2,800 characters, which is 6.5-8.3 KiB
#             once wrapped in the AES-GCM envelope (1.34x for base64) plus ~1.2 KiB
#             of mail metadata.
#
# The default is the *real* number, because a budget tool that overstates the cost
# by 3.7x produces a "you will fill the disk in 24 months" that is not true and
# hides the fact that a 40 GB disk is comfortable. Override with `--kib-per-mail`.
KIB_PER_MAIL = 8.4

# What the install already occupies before any data: OS, nginx, the venv, the
# released code, and the geoip database. Rough, and deliberately generous.
BASE_OVERHEAD_GIB = 4.0
# Never plan to fill a disk to the brim: logs, package updates and the SQLite WAL
# need room, and a full disk takes the web service down with it.
HEADROOM_FRACTION = 0.15


def retention_copies(keep_days: int, copies_per_day: float) -> int:
    """How many copies settle out, applying `prune`'s three rules."""
    keep_min = backup.BACKUP_KEEP_MIN
    keep_max = backup.BACKUP_KEEP_MAX
    # Distinct days held = keep_days + 1 (today plus `keep_days` back).
    days = keep_days + 1
    count = max(keep_min, int(round(days * copies_per_day)))
    return min(keep_max, count)


def simulate(users: int, mails_per_user_day: float, keep_days: int,
             copies_per_day: float, budget_gib: float, months: int = 60,
             kib_per_mail: float = KIB_PER_MAIL) -> dict:
    """Growth of live database + backups, month by month, until the budget is gone."""
    mib_per_day = users * mails_per_user_day * kib_per_mail / 1024
    gib_per_month = mib_per_day * 30 / 1024
    copies = retention_copies(keep_days, copies_per_day)

    # The backups hold the recent copies, so their footprint tracks the *current*
    # database size, not the average.
    start_gib = 0.06  # a fresh install starts near-empty
    full_at = None
    samples = []
    for month in range(months + 1):
        live = start_gib + gib_per_month * month
        total = BASE_OVERHEAD_GIB + live * (1 + copies)
        samples.append({"month": month, "live_gib": round(live, 2),
                        "total_gib": round(total, 1), "copies": copies})
        if full_at is None and total >= budget_gib:
            full_at = month
    return {"copies": copies, "gib_per_month_live": round(gib_per_month, 2),
            "gib_per_month_total": round(gib_per_month * (1 + copies), 2),
            "months_until_full": full_at, "samples": samples,
            "first_total_gib": samples[0]["total_gib"]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--disk-gb", type=float, default=40.0)
    parser.add_argument("--users", type=int, default=100)
    parser.add_argument("--mails-per-user-day", type=float, default=3.0)
    parser.add_argument("--kib-per-mail", type=float, default=KIB_PER_MAIL,
                        help="resident cost of one mail; the default is the measured "
                             "real-report figure (see the constant's comment)")
    parser.add_argument("--json", default="")
    args = parser.parse_args()

    budget = args.disk_gb * (1 - HEADROOM_FRACTION)
    print(f"disk {args.disk_gb:.0f} GB, keeping {HEADROOM_FRACTION:.0%} free "
          f"=> usable budget {budget:.1f} GiB")
    print(f"payload cost {KIB_PER_MAIL} KiB/mail (measured, ciphertext), "
          f"base overhead {BASE_OVERHEAD_GIB} GiB\n")

    print(f"  {'KEEP_DAYS':>10} {'copies':>7} {'+GiB/month':>12} {'full in':>9}   note")
    print("  " + "-" * 66)
    options = []
    for keep_days, note in ((14, "the old default (before 2026-09-18)"), (7, "the default"),
                            (3, "three days"), (2, "two days"), (1, "yesterday only")):
        result = simulate(args.users, args.mails_per_user_day, keep_days, 1.0, budget, kib_per_mail=args.kib_per_mail)
        options.append({"keep_days": keep_days, **{k: v for k, v in result.items()
                                                   if k != "samples"}})
        months = result["months_until_full"]
        when = "never in 5y" if months is None else (f"{months} mo" if months > 0
                                                     else "already over")
        print(f"  {keep_days:>10} {result['copies']:>7} "
              f"{result['gib_per_month_total']:>11.2f} {when:>9}   {note}")

    print(f"\n  same, but with a daily deploy (部署也写一份备份):")
    for keep_days in (14, 7, 3, 2):
        result = simulate(args.users, args.mails_per_user_day, keep_days, 2.0, budget, kib_per_mail=args.kib_per_mail)
        months = result["months_until_full"]
        when = "never in 5y" if months is None else (f"{months} mo" if months > 0
                                                     else "already over")
        print(f"  {keep_days:>10} {result['copies']:>7} "
              f"{result['gib_per_month_total']:>11.2f} {when:>9}")

    # How many accounts fit, at a chosen retention, over two years?
    print(f"\n  用 KEEP_DAYS=3 的话，两年不爆盘能装多少人（每天 3 封）：")
    fitted = []
    for users in (50, 100, 150, 200, 300):
        result = simulate(users, args.mails_per_user_day, 3, 1.0, budget, months=24, kib_per_mail=args.kib_per_mail)
        ok = result["months_until_full"] is None
        fitted.append((users, ok, result["gib_per_month_total"]))
        print(f"    {users:>4} users: {'可以用满两年' if ok else '会在 24 个月内写满'}"
              f"   (+{result['gib_per_month_total']:.2f} GiB/month)")

    if args.json:
        Path(args.json).write_text(
            json.dumps({"budget_gib": budget, "options": options,
                        "fitted": fitted}, indent=2, ensure_ascii=False),
            encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
