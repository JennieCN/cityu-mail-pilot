#!/usr/bin/env python3
"""Email the accounts that registered but never finished setting up a mailbox.

    python tools/notify_stalled.py                 # dry run: who, and the exact text
    PILOT_NOTIFY_CONFIRM=yes python tools/notify_stalled.py --send

The wording, the classification of who is stuck, the provider steps and the
"never twice" bookkeeping all live in :mod:`pilot_app.setup_reminders`, because
the admin console's one-click button does exactly the same thing and two copies
of "which sentence does this person need" would drift apart. This file is only
the command line around it.

Why the console has it too
--------------------------
`setup_gap` and the sentinel already make a stalled account visible *to the
operator*. Visible is not fixed: the one person who cannot see the problem is
the person it belongs to -- they receive nothing and never learn that anything
was expected of them.

Safety
------
* **Dry run by default.** Sending needs `--send` *and* `PILOT_NOTIFY_CONFIRM=yes`,
  because these are real people's inboxes and there is no unsend.
* **Idempotent.** Each delivery is recorded under ``setup_reminder:<user_id>``;
  a second run skips anyone already written to unless ``--repeat`` is passed.
  Without that, re-running after a partial failure would mail the people who
  already got one.
* **Nothing personal is printed.** Addresses are masked in the output -- the
  operator does not need the full list in a terminal scrollback to decide
  whether to send, and the tool's output is often pasted somewhere.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# The package this tool talks to must be the *installed* one. `python /tmp/x.py`
# puts the script's own directory first on `sys.path`, so a stale unpacked copy
# left in /tmp silently shadows the real code -- which is exactly what happened
# the first time this ran on the server: it imported a v0.48.0 `pilot_app` from a
# forgotten deploy directory and the permission error was the only reason it was
# noticed. Run it from the installed tree, and say out loud where it came from.
ROOT = Path(os.environ.get("INFE_PILOT_APP_DIR") or Path(__file__).resolve().parent.parent)
sys.path.insert(0, str(ROOT))

import pilot_app  # noqa: E402
from pilot_app import setup_reminders  # noqa: E402
from pilot_app.database import Database  # noqa: E402
from pilot_app.security import SecretBox  # noqa: E402


def mask(address: str) -> str:
    address = str(address or "")
    if "@" not in address:
        return "(none)"
    local, _, domain = address.partition("@")
    return f"{local[:2]}…@{domain}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--send", action="store_true",
                        help="真的发信；还需要 PILOT_NOTIFY_CONFIRM=yes")
    parser.add_argument("--repeat", action="store_true",
                        help="给已经收到过提醒的账号再发一次")
    parser.add_argument("--limit", type=int, default=0,
                        help="最多发几封（0 = 不限）")
    args = parser.parse_args(argv)

    db = Database(os.environ.get("INFE_PILOT_DB", "/var/lib/cityu-mail-pilot/pilot.sqlite3"))
    rows = setup_reminders.collect(db)

    if args.send and os.environ.get("PILOT_NOTIFY_CONFIRM") != "yes":
        print("要真发信，请设置 PILOT_NOTIFY_CONFIRM=yes（先不加 --send 看一遍 dry run）。",
              file=sys.stderr)
        return 2

    todo = [row for row in rows if args.repeat or not row["notified_at"]]
    if args.limit:
        todo = todo[:args.limit]

    counts = setup_reminders.whats_left(db)
    print(f"共 {counts['stalled']} 个账号卡住（{counts['never']} 个没配邮箱 · "
          f"{counts['refused']} 个授权码被拒），其中 {len(todo)} 个这次要发。")
    print(f"（用的是 {Path(pilot_app.__file__).parent}）")
    wechat = setup_reminders.contact_wechat()
    print(f"（微信联系方式：{wechat}）" if wechat else "（没有配置 INFE_PILOT_CONTACT_WECHAT，邮件里不会出现微信那行）")
    print()

    for row in todo:
        subject, body = setup_reminders.message_for(row)
        print(f"--- {row['id'][:12]}  {mask(row.get('email'))}  "
              f"group={row['group']}  age={row['age_hours']:.1f}h"
              + ("  （已经提醒过）" if row["notified_at"] else ""))
        print(f"    主题   {subject}")
        print("    " + body.replace("\n", "\n    "))
        print()

    if not args.send:
        print(f"这是 dry run。要发这 {len(todo)} 封：PILOT_NOTIFY_CONFIRM=yes … --send")
        return 0

    result = setup_reminders.send_pending(
        db, SecretBox.from_environment(), include_notified=args.repeat, limit=args.limit,
        actor="notify_stalled")
    for item in result["sent"]:
        print(f"  已发 {item['user_id'][:12]} {mask(item['email'])} id={item['message_id']}")
    for item in result["failed"]:
        print(f"  失败 {item['user_id'][:12]} {mask(item['email'])}: {item['error']}")
    print()
    print(f"发出 {len(result['sent'])} 封，失败 {len(result['failed'])} 封。")
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
