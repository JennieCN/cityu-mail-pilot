# -*- coding: utf-8 -*-
"""「稍后提醒」：让一条待办暂时离开今天的清单，到点**自己**回来。

纯逻辑：不碰数据库、不联网、不发邮件。三个预设与一个显式时刻都在这儿算，
因为这三件事各自有一个容易写错、又不容易看出来的地方：

* **「今晚」已经过了就是明天**，不是「过去的一个时刻」（那样它会立刻回来，
  用户看到的是按钮没反应）；
* **夹取**在 5 分钟 ~ 30 天之间：太短等于没按，太长等于删除；
* **到点回来是在读列表时判断的**（``active()``），所以这里没有、也不该有
  任何定时任务 —— 服务器重启、worker 停摆都不会漏掉那一刻。

日报那一行（:func:`digest_line`）也在这里：它是**事实**，不是叙述，所以它属于
确定性清单，永远不交给模型写（理由见 ``docs/snooze-2026-09-23.md``）。
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: 三个预设。**不做自定义时长输入框**：三个预设覆盖真实场景，少一个能填错的地方。
PRESETS: tuple[str, ...] = ("1h", "tonight", "tomorrow")

#: 服务端夹取的边界。太短没意义（还没离开就被叫回来），太长等于删除。
MIN_SECONDS = 5 * 60
MAX_SECONDS = 30 * 24 * 60 * 60

#: 「今晚」与「明天早上」的钟点。与每日简报的默认时刻（22:00）**不同**是有意的：
#: 简报是汇总，这是提醒，挤在同一个钟点会让人以为它们是同一件事。
TONIGHT_HOUR = 21
TOMORROW_HOUR = 9

HONG_KONG = "Asia/Hong_Kong"


class InvalidSnooze(ValueError):
    """认不出的 ``until``。调用方翻成 422，而不是猜一个时刻。"""


def zone_for(timezone: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone or HONG_KONG)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo(HONG_KONG)


def parse_iso(value: object) -> dt.datetime | None:
    """把一个存下来的时刻读成 aware datetime；读不出来返回 ``None``。

    ``None`` 与 ``''`` 是同一件事：**没被稍后提醒**。读不出来的值（手改过的库、
    旧格式）也按这个处理 —— 一条读不出时刻的行留在主列表里，比让它凭空消失好。
    """
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        moment = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment


def resolve(until: object, *, now: dt.datetime, timezone: str = HONG_KONG) -> str:
    """``until`` → 存下来的 UTC ISO；``""`` → ``""``（取消）。

    ``now`` 必须传进来（不在这里读时钟）：预设的正确性全靠「现在是几点」，
    而一个自己读时钟的函数没法在测试里问「今晚 21:00 已经过了会怎样」。
    """
    text = str(until or "").strip()
    if text == "":
        return ""
    zone = zone_for(timezone)
    local = now.astimezone(zone)
    if text == "1h":
        moment = now + dt.timedelta(seconds=3600)
    elif text == "tonight":
        moment = local.replace(hour=TONIGHT_HOUR, minute=0, second=0, microsecond=0)
        if moment <= local:
            # 已经过了今晚 21:00 ⇒ 明天 21:00。**不是**「过去的那一刻」：
            # 那样这条待办会立刻回来，看起来像按钮坏了。
            moment += dt.timedelta(days=1)
    elif text == "tomorrow":
        moment = (local + dt.timedelta(days=1)).replace(
            hour=TOMORROW_HOUR, minute=0, second=0, microsecond=0)
    else:
        parsed = parse_iso(text)
        if parsed is None:
            raise InvalidSnooze(text)
        if parsed.tzinfo is None:  # pragma: no cover - parse_iso 已经补过 UTC
            parsed = parsed.replace(tzinfo=zone)
        moment = parsed
    return clamp(moment, now=now).astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def clamp(moment: dt.datetime, *, now: dt.datetime) -> dt.datetime:
    """把时刻夹在 ``now+5min`` 与 ``now+30d`` 之间。

    夹取是**服务端**的事：浏览器时钟可以错，而一个过去的时刻会让这条待办
    立刻回来（看起来像没保存），一个三年后的时刻会让它从此消失（看起来像被删了）。
    """
    earliest = now + dt.timedelta(seconds=MIN_SECONDS)
    latest = now + dt.timedelta(seconds=MAX_SECONDS)
    return min(max(moment, earliest), latest)


def is_asleep(row: dict, *, now: dt.datetime) -> bool:
    """这一行此刻还在睡吗（``snoozed_until`` 还没到）。

    **只有这一个地方判断「到点没有」**：列表的分流与日报那一行都问它，两处各写
    一遍迟早会出现「列表里回来了、日报里还在睡」这种自相矛盾。
    """
    moment = parse_iso((row or {}).get("snoozed_until"))
    return moment is not None and moment > now


def active(rows: list[dict], *, now: dt.datetime) -> list[dict]:
    """此刻**还在睡**的那些行，最早醒来的排在最前。

    入参就是 ``task_states`` 的行（带 ``snoozed_until`` 与 ``state``）。
    ``state='done'`` 的**不算**：已经处理掉的事不该在日报里被说成「等你稍后回来」。
    ``snoozed_until <= now`` 的行也不在这里 —— 那就是「到点了」，它们回主列表，
    不需要任何后台任务。
    """
    out: list[dict] = []
    for row in rows or []:
        if str(row.get("state") or "open") != "open":
            continue
        if not is_asleep(row, now=now):
            continue
        out.append(row)
    out.sort(key=lambda row: parse_iso(row.get("snoozed_until")) or now)
    return out


def when_text(until: object, *, now: dt.datetime, timezone: str = HONG_KONG) -> str:
    """「最早 X 回来」里的那个 X，按用户自己的时区说。

    同一天只说钟点；跨天要说清是哪一天，否则「最早 09:00 回来」在晚上读到会
    被当成「今晚 9 点」——而它其实是明天早上。
    """
    moment = parse_iso(until)
    if moment is None:
        return ""
    local = moment.astimezone(zone_for(timezone))
    today = now.astimezone(zone_for(timezone)).date()
    clock = local.strftime("%H:%M")
    days = (local.date() - today).days
    if days <= 0:
        return clock
    if days == 1:
        return f"明天 {clock}"
    return f"{local.month}月{local.day}日 {clock}"


def digest_line(rows: list[dict], *, now: dt.datetime, timezone: str = HONG_KONG) -> str:
    """日报**确定性清单**里的那一行；没有在睡的待办就返回空串（整行不出现）。

    > 你让它稍后提醒的 N 件（最早 21:00 回来）

    这一行**不额外发一封邮件**：少发邮件是这个项目的取向，而「今日简报」本来
    就是每天那一封（与「失败 = 少一段话，简报照发」同源）。
    """
    pending = active(rows, now=now)
    if not pending:
        return ""
    earliest = pending[0].get("snoozed_until")
    return (f"你让它稍后提醒的 {len(pending)} 件"
            f"（最早 {when_text(earliest, now=now, timezone=timezone)} 回来）")
