"""Encrypted, rebuildable task-date projection for the in-app calendar."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import reports, taskexport
from .security import SecretBox

INDEX_VERSION = 1


def project_report(secrets: SecretBox, user_id: str, report: dict[str, Any],
                   body: str, timezone: str, *, fallback_day: str) -> list[tuple[str, str, bytes]]:
    """Derive one report's indexed tasks without persisting task text in plaintext.

    The keyed day token supports equality lookup for a calendar month; all task
    details stay encrypted and can be regenerated from the encrypted report.
    """
    message_id = str(report.get("message_id") or "")
    received = str(report.get("report_received_at") or report.get("received_at")
                   or report.get("created_at") or "")
    message = {
        "id": message_id,
        "subject": report.get("message_subject") or report.get("subject") or "",
        "sender_name": report.get("sender_name") or "",
        "sender_address": report.get("sender_address") or "",
        "received": received,
        "importance": report.get("importance") or "normal",
    }
    derived = reports.today_tasks(
        [(report["id"], body, message_id)], [message], timezone=timezone,
    )
    result: list[tuple[str, str, bytes]] = []
    for task in derived:
        task_day = str(task.get("task_day") or "")
        if not task_day:
            try:
                zone = ZoneInfo(timezone)
            except (ZoneInfoNotFoundError, ValueError, KeyError):
                zone = ZoneInfo("Asia/Hong_Kong")
            try:
                stamp = dt.datetime.fromisoformat(received.replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=dt.timezone.utc)
                task_day = stamp.astimezone(zone).date().isoformat()
            except (ValueError, TypeError):
                task_day = fallback_day
            task["task_day"] = task_day
        try:
            anchor = dt.date.fromisoformat(task_day)
        except ValueError:
            anchor = dt.date.fromisoformat(fallback_day)
            task["task_day"] = anchor.isoformat()
        due_day, clock = taskexport.deadline_day_and_clock(task, today=anchor)
        task["clock"] = f"{clock[0]:02d}:{clock[1]:02d}" if clock else ""
        encrypted = secrets.encrypt(
            json.dumps(task, ensure_ascii=False, separators=(",", ":")),
            context=f"calendar-index:{user_id}:{task['task_key']}",
        )
        token = secrets.anonymized(
            f"calendar-day:v1:{user_id}:{due_day.isoformat()}"
        )
        result.append((task["task_key"], token, encrypted))
    return result
