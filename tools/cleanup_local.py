#!/usr/bin/env python3
"""Remove the local leftovers listed as item 20 of the open-items ledger.

    .secrets/           the QQ app password handed over for the 2026-09-13
                        diagnostics. Nothing in the repo reads it -- only
                        publish_export.py mentions it, as an exclusion.
    ~/pilot-local/      a local sandbox: its own env file, a dev database and
                        four small scripts. Nothing references it.
    /tmp/probe_gen.py   a one-off latency probe written for the server.

Deleting is irreversible, so this refuses more than it deletes.

**The precondition is not a checkbox.** The ledger says not to touch these
until the master key has an offline copy, and the honest form of that question
is not "do you have one?" -- it is "which copy?" So this asks for the
fingerprint of the production key, read off the copy in your hand, and then
does the thing a promise cannot do: it **searches these leftovers for that
exact key** and stops if it finds it. Supplying the fingerprint is also the
evidence that you hold the copy, which is why it is required even though the
script never stores or transmits it.

    # see what would go, without deleting anything (also the default)
    python tools/cleanup_local.py

    # actually delete
    PILOT_MASTER_KEY_FINGERPRINT=XXXX-XXXX-XXXX \
    PILOT_CLEANUP_CONFIRM=yes python tools/cleanup_local.py

The fingerprint comes from `python -m pilot_app.manage restore-drill` on the
server (printed first, always, for exactly this purpose) or from whatever you
wrote down next to the offline copy. See docs/master-key-2026-09-14.md.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pilot_app.security import key_fingerprint  # noqa: E402

TARGETS = (
    Path(__file__).resolve().parent.parent / ".secrets",
    Path.home() / "pilot-local",
    Path("/tmp/probe_gen.py"),
)

_KEY_LINE = re.compile(r"^\s*(?:export\s+)?INFE_PILOT_MASTER_KEY\s*=\s*(.+?)\s*$", re.M)


def _interesting_files(root: Path) -> list[Path]:
    """Files worth looking inside: text-ish, small, and not a database."""
    if root.is_file():
        return [root]
    found = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if path.name.endswith(("-shm", "-wal")) or path.suffix in {".sqlite3", ".db"}:
            continue
        if path.stat().st_size > 1_000_000:
            continue
        found.append(path)
    return found


def keys_in(paths: tuple[Path, ...] | list[Path]) -> list[tuple[Path, str]]:
    """Every master key found in these trees, as (file, fingerprint)."""
    found: list[tuple[Path, str]] = []
    for root in paths:
        if not root.exists():
            continue
        for path in _interesting_files(root):
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for match in _KEY_LINE.finditer(text):
                value = match.group(1).strip().strip('"').strip("'")
                if value:
                    found.append((path, key_fingerprint(value)))
    return found


def decide(paths: tuple[Path, ...] | list[Path], expected: str) -> tuple[bool, list[str]]:
    """May we delete these? And what should the operator be told?

    Separate from `main` so the rule can be tested without deleting anything.
    It refuses when the key it was told to protect is *inside* the target --
    that is the one outcome that would turn a tidy-up into a catastrophe, and
    it is checkable rather than a matter of trust.
    """
    if not expected:
        return False, [
            "没给 PILOT_MASTER_KEY_FINGERPRINT。",
            "  请照着你手上那份离线副本（或服务器上 `manage restore-drill` 打出的那一行）",
            "  填上生产主密钥的指纹，例如：",
            "    PILOT_MASTER_KEY_FINGERPRINT=<主密钥指纹见运营者离线副本> "
            "PILOT_CLEANUP_CONFIRM=yes python tools/cleanup_local.py",
        ]
    matches = [(path, fp) for path, fp in keys_in(paths) if fp == expected]
    if matches:
        return False, [
            "拒绝执行：这些东西里有一把 key，指纹与你给的生产主密钥**一模一样**。",
            *[f"    {path}" for path, _ in matches],
            "  删掉它就会删掉生产主密钥的一份副本。先把副本放到离线介质上，再回来。",
        ]
    found = keys_in(paths)
    lines = []
    if found:
        lines = ["  顺带说明：这些遗留物里有别的主密钥，但不是生产那把：",
                 *[f"    {path} → {fp}" for path, fp in found]]
    else:
        lines = ["  遗留物里没有任何主密钥。"]

    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        lines.append(f"  本来就不存在（跳过）：{'、'.join(missing)}")
    return True, lines


def main() -> int:
    expected = (os.environ.get("PILOT_MASTER_KEY_FINGERPRINT") or "").strip().upper()
    confirmed = os.environ.get("PILOT_CLEANUP_CONFIRM") == "yes"

    print("清单第 20 条 · 本机遗留物")
    for path in TARGETS:
        if not path.exists():
            print(f"  （不存在）{path}")
            continue
        # Every file, not just the ones worth scanning for a key: the first
        # version reported the scanned bytes, which said "7 KB" for a directory
        # holding a 160 KB database. A size that only counts part of the thing
        # is the kind of number this project keeps having to correct.
        if path.is_dir():
            total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
            kind = "目录"
        else:
            total = path.stat().st_size
            kind = "文件"
        print(f"  {kind}  {path}  {total} 字节")

    allowed, notes = decide(TARGETS, expected)
    for line in notes:
        print(line)

    if not allowed:
        print("\n什么都没删。")
        return 1

    if not confirmed:
        print("\n这是预演（没有设 PILOT_CLEANUP_CONFIRM=yes）。要真的删，加那个变量重跑。")
        return 0

    for path in TARGETS:
        if not path.exists():
            continue
        shutil.rmtree(path) if path.is_dir() else path.unlink()
        print(f"  已删除 {path}")
    print("\n清完了。这份清单上的三处都不再存在。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
