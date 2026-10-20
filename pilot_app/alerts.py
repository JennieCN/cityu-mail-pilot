"""Arrival alert: an instant, model-free heads-up before the full report.

Why this exists
---------------
The report model is a heavy reasoner: one report costs ~200 s of *hidden*
reasoning tokens before the first visible character, and the plan key in use
only allows that one model. Measured evidence is in HANDOVER section 15. So the
honest options are (a) get a faster model, (b) send nothing until it finishes,
or (c) tell the user immediately that the mail arrived and what it is, then send
the full report when it is ready.

(c) is the alert-then-report pattern used by monitoring systems, and it is the
only one that improves perceived latency without changing the agreed report
contract: the alert is **not** a second copy of the report — it carries no
model output at all, so the user still receives exactly one original mail (from
CityU), one instant heads-up, and one full report.

Everything here is deterministic and stdlib-only; see ``triage.py`` for the
rule provenance (MIT project, design reference only).
"""

from __future__ import annotations

import html
from typing import Any

from . import triage


ALERT_LABEL = "已收到"

_VAGUE_GISTS = ("", "（无主题）", "no subject", "untitled")


def _clean(value: Any, limit: int = 200) -> str:
    return " ".join(str(value or "").split())[:limit]


def should_alert(triage_result: dict[str, Any], *, urgent_only: bool = False) -> bool:
    """Whether this message deserves an instant heads-up.

    ``urgent_only`` (INFE_PILOT_ALERT_URGENT_ONLY=1) keeps quiet about routine
    mail, so the mailbox is not twice as busy for a newsletter.
    """
    if urgent_only:
        return bool(triage_result.get("urgent"))
    return True


def build_alert(message: dict[str, Any], *, mailbox_email: str = "",
                full_follows: bool = True) -> dict[str, str]:
    """Compose the instant alert from rules alone (no model, no network).

    ``full_follows`` says whether the full report will actually be sent after
    this heads-up. 站上关掉完整版（`INFE_PILOT_FULL_REPORT=0`）时它是 False，
    提醒里那句「稍后单独发送」就必须换掉 —— 否则这条提醒在承诺一封永远不会到的
    邮件（2026-09-23 实测：站上正是这个配置，而句子是写死的）。
    """
    verdict = triage.triage(message)
    subject = _clean(message.get("subject"), 120) or "（无主题）"
    sender = _clean(message.get("sender_name"), 60) or _clean(message.get("sender_address"), 80) or "未知发件人"
    title = f"【{ALERT_LABEL}】{subject}"

    starred = "★ " if verdict["urgent"] else ""
    lines = [
        f"{starred}{verdict['effective_label']} · 紧急度 {verdict['urgency']}/10",
        "",
        f"发件人：{sender}",
        f"主题：{subject}",
    ]
    gist = _clean(verdict["gist"], 160)
    if gist and gist.lower() not in _VAGUE_GISTS:
        lines += ["", f"内容提要：{gist}"]
    if verdict["reasons"]:
        lines += ["", "判定依据：" + "；".join(verdict["reasons"])[:180]]
    lines += [
        "",
        "—— 这是规则引擎在邮件到达后立刻发出的提醒，不含 AI 分析。",
        ("完整的中英双语报告（含行动项、联网核实来源与风险提示）正在生成，稍后单独发送。"
         if full_follows else
         "本站当前只发送提醒与精简摘要，不会再发完整版报告。"),
    ]
    text = "\n".join(lines)

    body = "".join(
        f'<p style="margin:6px 0">{html.escape(line)}</p>' if line else "<br>"
        for line in lines
    )
    label_color = "#b42318" if verdict["urgent"] else "#123b63"
    html_body = (
        '<!doctype html><html lang="zh-Hans"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
        '<body style="margin:0;padding:16px;background-color:#f3f6f9;'
        'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,Helvetica,Arial,sans-serif;'
        'color:#1d2939;word-break:break-word;overflow-wrap:anywhere">'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        'style="width:100%;max-width:600px;margin:0 auto;border-collapse:collapse;table-layout:fixed">'
        f'<tr><td style="background-color:{label_color};padding:14px 18px;color:#ffffff">'
        '<div style="font-size:11px;letter-spacing:.08em;color:#dbe9f5">CITYU MAIL PILOT</div>'
        f'<div style="font-size:16px;font-weight:700;margin-top:4px">{html.escape(ALERT_LABEL)}</div>'
        '</td></tr>'
        '<tr><td style="background-color:#ffffff;border:1px solid #dbe3eb;border-top:0;padding:16px 18px">'
        f'{body}'
        '</td></tr></table></body></html>'
    )
    return {
        "subject": title,
        "text": text,
        "html": html_body,
        "urgent": verdict["urgent"],
        "category": verdict["effective_category"],
        "urgency": verdict["urgency"],
    }
