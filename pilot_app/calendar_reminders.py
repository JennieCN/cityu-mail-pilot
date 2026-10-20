"""Pure helpers for the persistent calendar reminder domain."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any, Mapping

from . import calendar_events, reports, taskexport
from .security import SecretBox

PARSER_VERSION = 1
REMINDER_WINDOW_SECONDS = 900


def parse_utc(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def iso_utc(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def expires_at(due_at_utc: str) -> str:
    return iso_utc(parse_utc(due_at_utc) + dt.timedelta(seconds=REMINDER_WINDOW_SECONDS))


def due_for_rule(source: Mapping[str, Any], *, rule_kind: str, rule_value: int,
                 now: str | None = None) -> tuple[str, str]:
    """Derive and validate a UTC due/expiry pair from encrypted-source fields."""
    value = int(rule_value)
    if rule_kind == "minutes_before":
        if not source.get("starts_at_utc") or not 0 <= value <= 10080:
            raise ValueError("minutes_before 只适用于有唯一时刻且范围有效的事项。")
        due = parse_utc(str(source["starts_at_utc"])) - dt.timedelta(minutes=value)
    elif rule_kind == "day_at":
        if not source.get("all_day") or not 0 <= value <= 1439:
            raise ValueError("day_at 只适用于全天事项且范围有效。")
        clock = f"{value // 60:02d}:{value % 60:02d}"
        validated = calendar_events.validate({
            "title": "Reminder", "notes": "", "date": str(source["local_date"]),
            "time": clock, "timezone": str(source["timezone"]), "all_day": False,
            "fold": None, "state": "open",
        })
        due = parse_utc(validated["starts_at_utc"])
    else:
        raise ValueError("未知的提醒规则。")
    due_text = iso_utc(due)
    expiry = expires_at(due_text)
    if now is not None and parse_utc(expiry) <= parse_utc(now):
        raise ValueError("提醒时间已经过期。")
    return due_text, expiry


def canonical_schedule(report_id: str, task_key: str, *, explicit_deadline: int,
                       local_date: str, local_time: str, timezone: str,
                       all_day: int, starts_at_utc: str) -> str:
    return json.dumps([report_id, task_key, int(explicit_deadline), local_date,
                       local_time, timezone, int(all_day), starts_at_utc],
                      ensure_ascii=False, separators=(",", ":"))


def build_source_fields(secrets: SecretBox, user_id: str, report_id: str,
                        task: Mapping[str, Any], *, timezone: str,
                        fallback_day: str) -> dict[str, Any]:
    task_key = str(task.get("task_key") or "")
    task_day = str(task.get("task_day") or fallback_day)
    anchor = dt.date.fromisoformat(task_day)
    due_day, clock = taskexport.deadline_day_and_clock(task, today=anchor)
    explicit = int(bool(str(task.get("deadline") or "").strip()))
    local_time = f"{clock[0]:02d}:{clock[1]:02d}" if clock else ""
    all_day = int(clock is None)
    starts_at_utc = ""
    if clock is not None:
        validated = calendar_events.validate({
            "title": str(task.get("action") or "Reminder")[:200] or "Reminder",
            "notes": "", "date": due_day.isoformat(), "time": local_time,
            "timezone": timezone, "all_day": False,
            "fold": task.get("fold"), "state": "open",
        })
        starts_at_utc = iso_utc(parse_utc(validated["starts_at_utc"]))
    canonical = canonical_schedule(report_id, task_key, explicit_deadline=explicit,
                                    local_date=due_day.isoformat(), local_time=local_time,
                                    timezone=timezone, all_day=all_day,
                                    starts_at_utc=starts_at_utc)
    return {
        "task_key": task_key,
        "parser_version": PARSER_VERSION,
        "source_fingerprint": secrets.anonymized(canonical),
        "explicit_deadline": explicit,
        "encrypted_schedule": secrets.encrypt(canonical, context=f"calendar-mail-source:{user_id}:{task_key}"),
        "local_date": due_day.isoformat(),
        "local_time": local_time,
        "timezone": timezone,
        "all_day": all_day,
        "starts_at_utc": starts_at_utc,
    }


def report_tasks(secrets: SecretBox, user_id: str, report: Mapping[str, Any], body: str,
                 timezone: str, *, fallback_day: str) -> list[dict[str, Any]]:
    message_id = str(report.get("message_id") or "")
    message = {
        "id": message_id,
        "subject": report.get("message_subject") or report.get("subject") or "",
        "sender_name": report.get("sender_name") or "",
        "sender_address": report.get("sender_address") or "",
        "received": report.get("received_at") or report.get("created_at") or "",
        "importance": report.get("importance") or "normal",
    }
    tasks = reports.today_tasks([(str(report["id"]), body, message_id)], [message], timezone=timezone)
    return [build_source_fields(secrets, user_id, str(report["id"]), task,
                                timezone=timezone, fallback_day=fallback_day) for task in tasks]
