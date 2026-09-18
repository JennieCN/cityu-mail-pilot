"""Handing the user's own task list to the phone they already carry.

Two shapes, one source of truth (``line_for``), because the two platforms really
do differ and pretending otherwise would ship a button that works for half the
people who press it:

* **An iCalendar file** (``text/calendar``) that iOS and Android hand to the
  system calendar app.
* **A plain-text checklist** that can be pasted into iOS 提醒事项 / Google Tasks,
  for the people who want a to-do list rather than a calendar.

What the platform research actually says (checked 2026-09-16)
-------------------------------------------------------------
* iOS registers ``text/calendar`` and ``.ics`` against **Calendar**, and Safari
  only hands the file over when the server really sends that content type --
  serving it as ``application/octet-stream`` is the documented way to get a file
  that "cannot be opened". So the route must set the header, not just the name.
* **Reminders has no file import.** A file containing only ``VTODO`` components
  "opens with nothing to show" on iOS, and Google Tasks has no import at all.
  Every item here is therefore a ``VEVENT``, and the to-do path is text, not a
  file.
* ``UID`` is the identity Apple de-duplicates on: a stable UID means exporting
  the same task twice **updates** the existing entry instead of adding a second
  copy. Ours is derived from ``task_key``, which is itself a content
  fingerprint, so a rewritten action becomes a new entry (correctly) while an
  unchanged one does not pile up.
* All-day events dodge the timezone minefield entirely -- a floating time is
  read in whatever zone the phone happens to be in, and a ``TZID`` without a
  matching ``VTIMEZONE`` is undefined. ``DTEND`` for a date-valued event is
  **exclusive**: a one-day event on the 18th must end on the 19th, and getting
  that wrong shows every task a day short.
* Timed events (2026-09-18) need the zone spelled out anyway: a floating
  ``DTSTART:20260918T235900`` is read in the *phone's* zone, and a student
  studying in Hong Kong with a phone set to another zone would see a deadline
  an hour (or more) off. A ``TZID`` is only defined when a matching
  ``VTIMEZONE`` exists in the same file, so every timed event ships with one.

The date we put on an event
---------------------------
``deadline`` is a *display* string ("9/18/2026 23:59", "9月18日", "明天",
"以邮件为准"): it was built to be read, not parsed. So this module parses back
only the shapes it can prove -- an absolute date, with or without a year, and
the two relative words that are unambiguous -- and otherwise puts the event on
the day the mail arrived. The exact deadline text is kept in the title either
way, because a date we guessed must never replace what the mail actually said.
"""

from __future__ import annotations

import datetime as dt
import re
import zoneinfo
from typing import Any, Iterable, Mapping, Sequence

# RFC 5545 wants CRLF, and some clients are strict about it.
CRLF = "\r\n"
# The calendar's own name where the client shows one (iOS/Google read X-WR-CALNAME).
CALENDAR_NAME = "CityU Mail Pilot 待办"
PRODID = "-//CityU Mail Pilot//Tasks//CN"
# Highest first, as RFC 5545 defines them (1 = highest, 9 = lowest).
_ICS_PRIORITY = {"high": "1", "medium": "5", "low": "9"}
_TITLE_PREFIX = {"high": "【急】", "medium": "【中】", "low": "【缓】"}

_ABSOLUTE_DATE = re.compile(r"(?:(20\d{2})/)?(\d{1,2})/(\d{1,2})(?:/(20\d{2}))?")
_MONTH_DAY = re.compile(r"(?:(20\d{2})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_RELATIVE_DAYS = {"今天": 0, "today": 0, "明天": 1, "tomorrow": 1, "后天": 2}
_CLOCK_TIME = re.compile(r"(?<!\d)([01]?\d|2[0-3])[:：]([0-5]\d)(?!\d)")

# --- task kinds -------------------------------------------------------------
# One keyword each, matched against the action text. Order is the tie-break:
# the first kind whose keyword appears wins, so the most specific kinds come
# first. These are heuristics for a *label*, not a classification guarantee --
# the title still carries the action text itself, so a wrong label costs an
# emoji, never information.
_KIND_DEFS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("assignment", "📝", ("作业", "assignment", "homework", "problem set", "习题")),
    ("exam", "📑", ("考试", "exam", "quiz", "测验", "midterm", "final")),
    ("registration", "🧭", ("选课", "注册", "登记", "registration", "enrol", "add/drop")),
    ("payment", "💰", ("缴费", "付款", "学费", "payment", "fee", "tuition")),
    ("library", "📚", ("图书", "归还", "图书馆", "library", "return", "overdue", "逾期")),
    ("career", "💼", ("实习", "招聘", "宣讲", "career", "internship", "job", "recruit")),
    ("event", "🎪", ("讲座", "工作坊", "活动", "seminar", "workshop", "event", "webinar")),
    ("form", "🖋️", ("问卷", "表格", "申请", "survey", "form", "apply")),
    ("reply", "↩️", ("回复", "确认", "reply", "confirm", "rsvp")),
)
_KIND_LABELS = {
    "assignment": "作业", "exam": "考试", "registration": "注册", "payment": "缴费",
    "library": "图书", "career": "求职", "event": "活动", "form": "申请",
    "reply": "确认", "other": "待办",
}
_KIND_EMOJI = {kind: emoji for kind, emoji, _ in _KIND_DEFS}
# 「其他」**不加符号**，而不是给一个 ✅（2026-09-19 改）。
# 生产实测：最近 7 天 304 条任务里 **52% 落到 other**，而 ✅ 挂在一条**还没做完**的
# 待办前面，读起来是「已完成」——那正是这一屏最不该说错的一句话。认得出类型才戴帽子。
_KIND_EMOJI["other"] = ""


def _one_line(value: Any) -> str:
    """Whatever the mail said, flattened to something a calendar can hold.

    Carriage returns and control characters are **removed rather than escaped**:
    they are how a crafted subject line would try to end our property and start
    one of its own, and no legitimate deadline or action needs them.
    """
    text = str(value or "")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _escape(value: Any) -> str:
    """RFC 5545 TEXT escaping. Order matters: the backslash goes first."""
    text = _one_line(value)
    text = text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
    return text


def _fold(line: str) -> str:
    """Fold one content line at 75 octets, continuing with a single space.

    Counted in **octets, not characters**: this project is full of Chinese, where
    three bytes per character reaches the limit four times sooner than an ASCII
    line would. Folding in the middle of a multi-byte character produces a file
    that some parsers reject outright.
    """
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    pieces: list[str] = []
    current = ""
    width = 0
    limit = 75
    for character in line:
        size = len(character.encode("utf-8"))
        if width + size > limit:
            pieces.append(current)
            current = character
            width = size
            # Continuation lines carry a leading space, which itself counts.
            limit = 74
        else:
            current += character
            width += size
    pieces.append(current)
    return (CRLF + " ").join(pieces)


def _as_date(value: Any) -> dt.date | None:
    text = str(value or "").strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        try:
            return dt.date.fromisoformat(text[:10])
        except ValueError:
            return None
    return None


def _parse_deadline(deadline: Any, anchor: dt.date) -> dt.date | None:
    """The date a deadline string names, or ``None`` when it cannot be proven.

    ``anchor`` is the day the mail arrived (or today), used for two things: the
    year of a "9月18日" (which is almost always the next occurrence, not the
    current one) and the meaning of 今天/明天/后天.
    """
    text = _one_line(deadline)
    if not text:
        return None
    match = _ABSOLUTE_DATE.search(text)
    if match:
        year = int(match.group(1) or match.group(4) or 0)
        month, day = int(match.group(2)), int(match.group(3))
        if not 1 <= month <= 12 or not 1 <= day <= 31:
            return None
        try:
            return dt.date(year or anchor.year, month, day)
        except ValueError:
            return None
    match = _MONTH_DAY.search(text)
    if match:
        month, day = int(match.group(2)), int(match.group(3))
        if not 1 <= month <= 12 or not 1 <= day <= 31:
            return None
        year = int(match.group(1) or 0)
        if year:
            try:
                return dt.date(year, month, day)
            except ValueError:
                return None
        for candidate_year in (anchor.year, anchor.year + 1):
            try:
                candidate = dt.date(candidate_year, month, day)
            except ValueError:
                return None
            # A date more than a month behind the anchor belongs to next year:
            # a December mail about "1月5日" means January, not last January.
            if (candidate - anchor).days > -31:
                return candidate
        return None
    for word, offset in _RELATIVE_DAYS.items():
        if word in text.lower() or word in text:
            return anchor + dt.timedelta(days=offset)
    return None


def effective_priority(task: Mapping[str, Any]) -> str:
    """What the user should see: their own ranking wins over the model's."""
    from .reports import (PRIORITY_HIGH, PRIORITY_LOW, PRIORITY_MEDIUM,
                          _priority_rank)  # local import: keeps reports optional

    chosen = str(task.get("user_priority") or "")
    if chosen in {PRIORITY_HIGH, PRIORITY_MEDIUM, PRIORITY_LOW}:
        return chosen
    return str(task.get("priority") or "")
    # (_priority_rank is imported for the caller's convenience below.)


def task_kind(task: Mapping[str, Any]) -> str:
    """A coarse kind label for the emoji and CATEGORIES, or ``"other"``.

    Deliberately cheap: one pass over a small keyword table, against the
    **action text only**. The subject is not consulted -- an action often has
    nothing to do with the mail's subject line ("图书馆逾期通知" can carry the
    action "阅读第 2 章"), and a label that misleads is worse than a plain
    ✅. This is the layer a future Jev decision call would replace with a real
    ``choice`` answer -- the rest of the file already consumes the label, so
    that swap touches exactly this function.
    """
    action = _one_line(task.get("action"))
    for kind, _emoji, keywords in _KIND_DEFS:
        if any(keyword in action for keyword in keywords):
            return kind
    return "other"


def _numbers(value: Any) -> list[int]:
    return [int(piece) for piece in re.findall(r"\d+", _one_line(value))]


def _already_states(action: str, deadline: str) -> bool:
    """Whether the action text already carries this deadline's numbers.

    Compared as numbers rather than as text because the two spellings really do
    differ: the report writes ``2026-10-06 23:59`` and our label is
    ``2026/10/6 23:59``. A substring test would call those different and print
    both, and a task line with two deadlines that look like two dates is worse
    than one with none.
    """
    wanted = _numbers(deadline)
    if not wanted:
        return False
    remaining = iter(_numbers(action))
    return all(any(token == value for token in remaining) for value in wanted)


def _base_title(task: Mapping[str, Any]) -> str:
    """Priority prefix + action, without any deadline dressing."""
    action = _one_line(task.get("action")) or "（无描述）"
    prefix = _TITLE_PREFIX.get(effective_priority(task), "")
    return f"{prefix}{action}"


def line_for(task: Mapping[str, Any]) -> str:
    """One task as one line -- the ONLY place the exported wording is decided.

    Both the calendar entry's description and the pasted checklist come from
    here, so the two can never drift into describing the same task
    differently.
    """
    action = _one_line(task.get("action")) or "（无描述）"
    deadline = _one_line(task.get("deadline"))
    if deadline and _already_states(action, deadline):
        deadline = ""
    return _base_title(task) + (f"（截止 {deadline}）" if deadline else "")


def pretty_title(task: Mapping[str, Any], *, today: dt.date | None = None) -> str:
    """The calendar's display title: kind emoji, action, a compact deadline tag.

    This is where "美观" lives for the rule-only layer, and it is deliberately
    a *presenter* over :func:`line_for`, not a second wording engine: the
    action text is exactly what ``line_for`` decided it should be, only the
    dressing changes. The deadline appears **once**: when we can shorten it,
    the full-text "（截止 …）" suffix is replaced by the ⏰ tag (the full text
    still lives in the DESCRIPTION); when the action already states its own
    deadline, nothing is added at all -- a title never carries two dates.

    ``today`` comes from the caller for the same reason ``build_ics`` takes it:
    "today" is the *user's* local day and this module keeps exactly one source
    for it; reading the server clock here would make the year-dropping window
    depend on the machine's timezone instead of the reader's.
    """
    kind = task_kind(task)
    # 认得出类型才戴帽子：`other` 的 emoji 是空串（见 `_KIND_EMOJI` 那段注释）。
    emoji = _KIND_EMOJI.get(kind, "")
    hat = f"{emoji} " if emoji else ""
    action = _one_line(task.get("action")) or "（无描述）"
    deadline = _one_line(task.get("deadline"))
    short = (_short_deadline(deadline, today=today)
             if deadline and not _already_states(action, deadline) else "")
    if short:
        return f"{hat}{_base_title(task)} ⏰ {short}"
    return f"{hat}{line_for(task)}"


def _short_deadline(deadline: str, *, today: dt.date | None = None) -> str:
    """A compact date for the title: 9/18 instead of 9/18/2026, time kept.

    Both spellings the parser understands are shortened -- "9/18/2026" and
    "9月18日" alike, so a title never mixes calendar styles. The year is
    dropped inside a window around today (a calendar already shows the year,
    and three date-like numbers in one title read as two contradictory
    deadlines); outside it the year stays. Anything unparseable comes back
    unchanged, because a date we cannot prove is not a date we rewrite.
    """
    match = _ABSOLUTE_DATE.search(deadline)
    if not match:
        match = _MONTH_DAY.search(deadline)
    if match:
        groups = match.groups()
        if match.re is _MONTH_DAY:
            year = int(groups[0] or 0)
            month, day = int(groups[1]), int(groups[2])
        else:
            year = int(groups[0] or groups[3] or 0)
            month, day = int(groups[1]), int(groups[2])
        if 1 <= month <= 12 and 1 <= day <= 31:
            this_year = (today or dt.date.today()).year
            near = not year or abs(year - this_year) <= 1
            base = f"{month}/{day}" if near else f"{year}/{month}/{day}"
            clock = _CLOCK_TIME.search(deadline)
            return f"{base} {clock.group(1)}:{clock.group(2)}" if clock else base
    return deadline


def event_day(task: Mapping[str, Any], *, today: dt.date) -> dt.date:
    """The day this task's event lands on: its deadline, else the mail's day.

    "The day the mail arrived" is the honest fallback: the task genuinely
    belongs to that day, and inventing today's date for an old item would move
    something the user already knows about.
    """
    anchor = _as_date(task.get("task_day")) or today
    return _parse_deadline(task.get("deadline"), anchor) or anchor


def _deadline_clock(deadline: Any) -> tuple[int, int] | None:
    """The wall-clock time a deadline string names, or ``None``.

    Only an explicit ``HH:MM`` counts (23:59, 9:00, full-width colons too) --
    the same shapes ``reports.deadline_of`` treats as clock times. Words like
    "中午" name a part of a day, not a time we would stake an alarm on, so
    they stay all-day.

    **The last clock in the string wins, exactly as in ``deadline_of``.** A line
    can name two times ("12:00 前提交，最晚 23:59 截止"), and both the field the
    report shows and the alarm the calendar sets have to come off the same one --
    otherwise the app says 23:59 and the phone buzzes at noon. Today's data has
    no such line (measured over 304 tasks / 7 days on 2026-09-19), so this is
    insurance, not a fix for a live defect.
    """
    matches = _CLOCK_TIME.findall(_one_line(deadline))
    if not matches:
        return None
    hour, minute = matches[-1]
    return int(hour), int(minute)


def _observes_dst(zone: str) -> bool:
    """True when this zone's UTC offset changes across the year.

    The ``VTIMEZONE`` we emit is a single fixed ``STANDARD`` component, which is
    the truth for Hong Kong and a **lie of one hour** for, say, America/New_York
    in summer. A fixed block is not a shortcut we are allowed to take blindly:
    the timezone field in the profile is a **free-text input**
    (`index.html` 「时区」), not a picker, so "only Asian zones are possible" was
    never true. When the zone moves, we would rather ship no timed event than an
    alarm that is an hour off.
    """
    try:
        info = zoneinfo.ZoneInfo(zone)
    except Exception:
        return True
    winter = dt.datetime(2026, 1, 15, tzinfo=dt.timezone.utc).astimezone(info).utcoffset()
    summer = dt.datetime(2026, 7, 15, tzinfo=dt.timezone.utc).astimezone(info).utcoffset()
    return winter != summer


def _zone_offset_minutes(zone: str) -> int:
    try:
        moment = dt.datetime.now(dt.timezone.utc).astimezone(zoneinfo.ZoneInfo(zone))
    except Exception:
        return 480  # Asia/Hong_Kong's offset, for a zone string that failed
    seconds = moment.utcoffset().total_seconds() if moment.utcoffset() else 0
    return int(seconds // 60)


def _safe_zone(timezone: str) -> str:
    """The user's timezone string, or ``""`` when it cannot carry a timed event.

    This string travels into ``TZID=...`` and into ``ZoneInfo()``, so a hostile
    or merely mistyped profile value must neither inject calendar structure nor
    raise. An empty result means "no timed events": the calendar degrades to
    all-day, which is what it looked like before timed events existed.

    A zone we cannot describe truthfully also returns ``""`` -- see
    :func:`_observes_dst` for why a DST zone is one of those.
    """
    zone = _one_line(timezone)
    if not zone:
        return ""
    try:
        zoneinfo.ZoneInfo(zone)
    except Exception:
        return ""
    if _observes_dst(zone):
        return ""
    return zone


def _vtimezone(zone: str) -> list[str]:
    """A minimal ``VTIMEZONE`` block so a ``TZID`` reference is defined.

    RFC 5545 makes a TZID reference undefined without a matching component in
    the same file. One fixed ``STANDARD`` sub-component is the honest spelling
    **for a zone that does not move**, which is why ``_safe_zone`` refuses the
    others instead of pretending: half-inventing DST transitions would put a
    real appointment an hour off, and nobody would see it in the file.
    """
    offset = _zone_offset_minutes(zone)
    sign = "+" if offset >= 0 else "-"
    hh, mm = divmod(abs(offset), 60)
    return [
        "BEGIN:VTIMEZONE",
        f"TZID:{zone}",
        "BEGIN:STANDARD",
        "DTSTART:19700101T000000",
        f"TZOFFSETFROM:{sign}{hh:02d}{mm:02d}",
        f"TZOFFSETTO:{sign}{hh:02d}{mm:02d}",
        "TZNAME:CST" if zone.startswith("Asia") else "TZNAME:LOCAL",
        "END:STANDARD",
        "END:VTIMEZONE",
    ]


def build_ics(tasks: Sequence[Mapping[str, Any]], *, origin: str = "",
              now: dt.datetime | None = None, today: dt.date | None = None,
              timezone: str = "") -> str:
    """A calendar holding one event per task.

    A deadline with an explicit clock time becomes a real one-hour timed event
    in ``timezone`` (with the VTIMEZONE that makes its TZID defined); any other
    deadline stays an all-day event on its day. Import-safe to repeat.
    """
    moment = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    stamp = moment.strftime("%Y%m%dT%H%M%SZ")
    base = today or moment.date()
    zone = _safe_zone(timezone)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        # PUBLISH, not REQUEST: this is the user's own copy, not an invitation
        # that expects a reply, and a REQUEST without an ATTENDEE is what makes
        # some clients show Accept/Decline buttons on your own to-do.
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(CALENDAR_NAME)}",
    ]
    if zone and any(_deadline_clock(task.get("deadline")) for task in tasks):
        lines.extend(_vtimezone(zone))
    for task in tasks:
        day = event_day(task, today=base)
        clock = _deadline_clock(task.get("deadline"))
        kind = task_kind(task)
        title = pretty_title(task, today=base)
        description_bits = [line_for(task)]
        subject = _one_line(task.get("subject"))
        sender = _one_line(task.get("sender"))
        if subject:
            description_bits.append(f"来自邮件：{subject}")
        if sender:
            description_bits.append(f"发件人：{sender}")
        description_bits.append("由 CityU Mail Pilot 导出；在应用里点「✓ 处理好了」不会同步回这里。")
        if origin:
            description_bits.append(origin)
        lines.append("BEGIN:VEVENT")
        # Stable per task, so re-exporting updates instead of duplicating.
        lines.append(f"UID:{_one_line(task.get('task_key'))}@cityu-mail-pilot")
        lines.append(f"DTSTAMP:{stamp}")
        if clock and zone:
            # The deadline instant itself, one hour long, in the user's zone.
            start = dt.datetime.combine(day, dt.time(*clock), tzinfo=zoneinfo.ZoneInfo(zone))
            lines.append(f"DTSTART;TZID={zone}:{start.strftime('%Y%m%dT%H%M%S')}")
            lines.append(f"DTEND;TZID={zone}:"
                         f"{(start + dt.timedelta(hours=1)).strftime('%Y%m%dT%H%M%S')}")
        else:
            # Date-valued and therefore timezone-free; DTEND is exclusive.
            lines.append(f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}")
            lines.append(f"DTEND;VALUE=DATE:{(day + dt.timedelta(days=1)).strftime('%Y%m%d')}")
        lines.append(f"SUMMARY:{_escape(title)}")
        lines.append(f"DESCRIPTION:{_escape(chr(10).join(description_bits))}")
        lines.append("TRANSP:TRANSPARENT")
        # CATEGORIES is a **list** property: its separator is a real comma, and a
        # comma *inside* a value must be escaped. Escaping the separator (the
        # first version did, by escaping the whole joined string) produces one
        # category literally named "CityU Mail Pilot 待办,作业", so a client's
        # 「按类型筛选」 quietly finds nothing -- and an `assertIn` test passes
        # either way, which is why the assertion below counts the values.
        lines.append("CATEGORIES:" + ",".join(
            _escape(item) for item in (CALENDAR_NAME, _KIND_LABELS.get(kind, "待办"))))
        priority = _ICS_PRIORITY.get(effective_priority(task))
        if priority:
            lines.append(f"PRIORITY:{priority}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return CRLF.join(_fold(line) for line in lines) + CRLF


def build_text(tasks: Sequence[Mapping[str, Any]]) -> str:
    """The paste-able checklist (iOS 提醒事项 / Google Tasks, one line each)."""
    return "\n".join(f"- [ ] {line_for(task)}" for task in tasks)


def filename(day: str = "") -> str:
    """An ASCII file name: the .ics extension is what iOS keys on."""
    stamp = _one_line(day) or dt.date.today().isoformat()
    stamp = re.sub(r"[^0-9-]", "", stamp) or dt.date.today().isoformat()
    return f"cityu-tasks-{stamp}.ics"


__all__ = ["CRLF", "CALENDAR_NAME", "PRODID", "build_ics", "build_text", "line_for",
           "pretty_title", "task_kind", "event_day", "effective_priority",
           "filename"]
