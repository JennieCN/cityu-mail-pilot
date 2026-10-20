"""Safe helpers for moving the legacy single-user IMAP cursor into the pilot."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


LEGACY_UID = re.compile(r"imap:([1-9][0-9]*)")


def read_legacy_processed_uids(path: str | Path) -> list[int]:
    """Read the old ``imap-state.json`` and fail closed on an unknown shape."""
    source = Path(path)
    try:
        payload: Any = json.loads(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"无法读取旧状态文件：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"旧状态文件不是有效 JSON：第 {exc.lineno} 行") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("processed_uids"), list):
        raise ValueError("旧状态文件缺少 processed_uids 数组。")
    parsed: list[int] = []
    invalid: list[str] = []
    for value in payload["processed_uids"]:
        match = LEGACY_UID.fullmatch(str(value))
        if match:
            parsed.append(int(match.group(1)))
        else:
            invalid.append(repr(value)[:80])
    if invalid:
        raise ValueError("旧状态包含未知 UID 格式，已停止迁移：" + ", ".join(invalid[:5]))
    result = sorted(set(parsed))
    if not result:
        raise ValueError("旧状态中没有已处理的 IMAP UID。")
    return result
