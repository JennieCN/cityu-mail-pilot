"""Manual calendar facts: validation, encrypted content and month presentation."""
from __future__ import annotations

import datetime as dt
import json
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class InvalidEvent(ValueError):
    pass


class EventConflict(ValueError):
    pass


def validate(payload: dict) -> dict:
    """Derive UTC on the server; ambiguous wall times require an explicit fold."""
    result = {}
    for name, limit in (("title", 200), ("notes", 2000)):
        value = payload.get(name, "")
        if not isinstance(value, str) or len(value) > limit:
            raise InvalidEvent(f"{name} 必须是最多 {limit} 字的文字。")
        result[name] = value.strip()
    if not result["title"]:
        raise InvalidEvent("请填写事项标题。")
    date = payload.get("date")
    if not isinstance(date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise InvalidEvent("请选择有效日期。")
    try:
        day = dt.date.fromisoformat(date)
        if not 1970 <= day.year <= 2100:
            raise ValueError()
    except ValueError:
        raise InvalidEvent("日期须在 1970 至 2100 年之间。") from None
    zone_name = payload.get("timezone")
    if not isinstance(zone_name, str) or len(zone_name) > 64:
        raise InvalidEvent("请选择有效时区。")
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise InvalidEvent("请选择有效时区。") from None
    all_day = payload.get("all_day")
    if type(all_day) is not bool:
        raise InvalidEvent("请选择全天或具体时刻。")
    time = payload.get("time", "")
    fold = payload.get("fold")
    if fold is not None and (type(fold) is not int or fold not in (0, 1)):
        raise InvalidEvent("重复时刻的偏移选择无效。")
    starts = None
    if all_day:
        if time not in ("", None) or fold is not None:
            raise InvalidEvent("全天事项不应同时填写具体时刻。")
        time = ""
    else:
        if not isinstance(time, str) or not re.fullmatch(r"\d{2}:\d{2}", time):
            raise InvalidEvent("请填写具体时刻。")
        try:
            wall = dt.datetime.combine(day, dt.time.fromisoformat(time))
        except ValueError:
            raise InvalidEvent("具体时刻无效。") from None
        candidates = {}
        for candidate in (0, 1):
            aware = wall.replace(tzinfo=zone, fold=candidate)
            utc = aware.astimezone(dt.timezone.utc)
            if utc.astimezone(zone).replace(tzinfo=None) == wall:
                candidates[candidate] = utc
        if not candidates:
            raise InvalidEvent("夏令时切换使这个时刻不存在，请选择其他时刻。")
        if len(set(candidates.values())) > 1 and fold is None:
            raise InvalidEvent("这个时刻因夏令时出现两次，请选择第一次或第二次。")
        chosen = 0 if fold is None else fold
        if chosen not in candidates:
            raise InvalidEvent("这个时刻的偏移选择无效。")
        starts = candidates[chosen].isoformat(timespec="seconds")
        fold = chosen
    state = payload.get("state", "open")
    if state not in ("open", "done"):
        raise InvalidEvent("无效的事项状态。")
    result.update(date=date, time=time, timezone=zone_name, all_day=all_day,
                  starts_at_utc=starts, fold=fold, state=state)
    return result


def content(secrets, user_id: str, event_id: str, values: dict) -> bytes:
    return secrets.encrypt(json.dumps({key: values[key] for key in ("title", "notes")},
                                     ensure_ascii=False),
                           context=f"calendar-event:{user_id}:{event_id}")


def present(secrets, row: dict) -> dict:
    base = {key: row[key] for key in ("id", "state", "revision", "schedule_revision")}
    base["source_kind"] = "manual"
    if row["state"] == "deleted":
        return base
    text = json.loads(secrets.decrypt(row["encrypted_payload"],
                                     context=f"calendar-event:{row['user_id']}:{row['id']}"))
    base.update(text, date=row["local_date"], time=row["local_time"],
                timezone=row["timezone"], all_day=bool(row["all_day"]),
                starts_at_utc=row["starts_at_utc"], fold=row["fold"],
                created_at=row["created_at"], updated_at=row["updated_at"])
    return base


def month_bounds(year: int, month: int, timezone: str) -> tuple[str, str]:
    zone = ZoneInfo(timezone)
    start = dt.datetime(year, month, 1, tzinfo=zone)
    end = dt.datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=zone)
    return tuple(value.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
                 for value in (start, end))


def merge_month(database, secrets, user_id: str, timezone: str, data: dict) -> dict:
    """Manual facts remain usable while the independent mail index rebuilds."""
    year, month = map(int, data["month"].split("-"))
    start, end = month_bounds(year, month, timezone)
    cells = {cell["day"]: cell for cell in data["cells"]}
    for row in database.calendar_events_for_month(user_id, data["month"], start, end):
        item = present(secrets, row)
        if item["all_day"]:
            day = item["date"]
        else:
            day = dt.datetime.fromisoformat(item["starts_at_utc"]).astimezone(
                ZoneInfo(timezone)).date().isoformat()
        cell = cells.setdefault(day, {"day": day, "open": 0, "done": 0,
                                      "urgent": False, "items": []})
        item.update(event_id=item["id"], text=item["title"], task_day=day,
                    dated=True, subject="自己添加", priority="medium")
        if item["state"] == "done":
            cell["done"] += 1
            cell.setdefault("manual_done", []).append(item)
        else:
            cell["open"] += 1
            cell["items"].append(item)
    for cell in cells.values():
        for item in cell["items"]:
            item.setdefault("source_kind", "mail")
    data["cells"] = [cells[key] for key in sorted(cells)]
    data["counts"] = {key: sum(cell[key] for cell in cells.values()) for key in ("open", "done")}
    return data
