"""The optional model-written paragraph on top of the daily digest.

The daily brief is deliberately assembled **without** a model: every processed
message gets exactly one row, and that is what makes the promise "no mail is ever
silently dropped" true. Asking a model to summarise the day would break it.

This module adds the other thing a model is good at -- noticing that three
courses all have homework due -- *without* touching that guarantee. The
synthesis is an extra paragraph; the deterministic list underneath it is still
the report. If the model is unavailable, slow, or wrong, the digest goes out
exactly as it does today.

Two consequences of that ordering, both deliberate:

* **Off unless the operator turns it on.** Absent setting and absent environment
  variable both mean off, so a downloaded copy of this software does not start
  spending its owner's model budget because a feature exists.
* **A failure is a missing paragraph, never a missing mail.** Every error path
  here returns ``""``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

from . import providers

# The console setting wins; this is only the installation default.
SETTING_KEY = "digest_synthesis"
ENABLED_ENV = "INFE_PILOT_DIGEST_SYNTHESIS"

# What the model is asked for. Short on purpose: the reader already has the list,
# and a long synthesis is the thing that would compete with it for attention.
MAX_OUTPUT_TOKENS = 400
HEADING = "一段综览（模型写的，仅供参考）"


def enabled_from_environment() -> bool:
    return (os.environ.get(ENABLED_ENV) or "0").strip() not in {"", "0", "false", "False", "no"}


def enabled(db: Any) -> bool:
    """Console setting first, installation default second."""
    stored = db.get_setting(SETTING_KEY, "")
    if stored == "":
        return enabled_from_environment()
    return stored == "1"


def set_enabled(db: Any, value: bool, *, actor: str = "") -> bool:
    db.set_setting(SETTING_KEY, "1" if value else "0", actor=actor)
    return bool(value)


def build_prompt(digest: dict[str, Any]) -> str:
    """Ask for a few sentences about the *shape* of the day.

    The input is the digest's own items, which is text this program produced and
    which the user is about to receive anyway -- no mail body is added here, so
    turning this on does not widen what leaves the machine.
    """
    lines: list[str] = []
    for entry in (digest.get("items") or [])[:30]:
        bits = [str(entry.get("subject") or "")]
        if entry.get("sender"):
            bits.append(f"发件人：{entry['sender']}")
        if entry.get("deadline"):
            bits.append(f"截止：{entry['deadline']}")
        lines.append("- " + " · ".join(bit for bit in bits if bit))
    metrics = digest.get("metrics") or {}
    return (
        "下面是一个人今天收到的学校邮件清单（已经由程序整理好，逐条都准确）。\n"
        "请用中文写 2–3 句话的综览，帮他看出**跨邮件的规律**，例如「三门课都有作业」"
        "「有两件事都在本周五截止」。\n\n"
        "要求：\n"
        "1. 只写清单里有的信息，不要补充、不要推测、不要给建议。\n"
        "2. 不要重复罗列清单，也不要写标题或 Markdown。\n"
        "3. 找不到规律就直说「今天没有明显的集中事项」。\n\n"
        f"今天共收到 {metrics.get('total', 0)} 封，其中需要行动 {metrics.get('actionable', 0)} 项。\n"
        "清单：\n" + ("\n".join(lines) or "- （今天没有邮件）")
    )


def clean(text: str) -> str:
    """Keep the paragraph to what the renderer expects.

    A model that ignores "no Markdown" must not be able to inject headings into a
    numbered report, and one that rambles must not push the list off the first
    screen. So: strip markers, collapse blank lines, cap the length.
    """
    value = str(text or "").strip()
    if not value:
        return ""
    cleaned: list[str] = []
    for line in value.splitlines():
        line = line.strip().lstrip("#>-*").strip()
        if line:
            cleaned.append(line)
    return "\n".join(cleaned)[:600]


def synthesize(service: Any, user: dict[str, Any],
               digest: dict[str, Any]) -> tuple[str, dict[str, Any], Optional[dict[str, Any]]]:
    """One optional model call. Never raises.

    Returns ``(text, usage, connection)`` so the caller records the spend itself
    -- this module orchestrates a prompt and a parser, it does not own the
    service's bookkeeping.

    The circuit breaker is honoured (a credential the provider has already
    rejected is not poked again), but there is **no retry**: a digest is not
    worth two model round-trips, and the list underneath is already the answer.
    The breaker is deliberately not *fed* either -- a synthesis failure says
    nothing about whether the user's key can generate reports, and counting it
    would suspend an account over an optional paragraph.
    """
    try:
        if service.db.key_circuit_open(user["id"]) is True:
            logging.info("skipping digest synthesis for %s: credential circuit is open", user["id"])
            return "", {}, None
        connection = service.model_connection(user["id"])
        if not connection:
            return "", {}, None
        # 与出报告、连接测试同一条钱闸：走平台那把 key 时，账上没钱就不花。
        service.require_budget_for(connection)
        result = providers.generate(
            provider=connection["provider"], model=connection["model"],
            base_url=connection["base_url"], api_key=service.connection_key(connection),
            prompt=build_prompt(digest), config=_config(connection),
            max_output_tokens=MAX_OUTPUT_TOKENS,
        )
        return clean(getattr(result, "text", "") or ""), dict(getattr(result, "usage", None) or {}), connection
    except Exception:  # noqa: BLE001 - the digest must go out regardless
        logging.warning("digest synthesis failed for %s; sending the list alone",
                        user.get("id"), exc_info=True)
        return "", {}, None


def _config(connection: dict[str, Any]) -> dict[str, Any]:
    import json

    try:
        return json.loads(connection.get("config_json") or "{}")
    except (TypeError, ValueError):
        return {}


def attach(digest: dict[str, Any], text: str) -> dict[str, Any]:
    """Put the paragraph on the digest where the three renderers look for it."""
    if text:
        digest["synthesis"] = text
    return digest
