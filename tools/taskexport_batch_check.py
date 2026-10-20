#!/usr/bin/env python3
"""100 条批量校验案例：日历待办纯规则美化（2026-09-18）。

跑法（仓库根目录）：
    python3 tools/taskexport_batch_check.py

与 pilot_app/tests/test_taskexport.py 的差别：那 44 条是钉住回归的单元测试；
这里的 100 条是**规则面的穷举案例表**——每条案例是（输入, 断言）对，覆盖
类型推断的全部词表与优先级、日期解析的每种写法、定时/全天事件的每个分支、
标题拼装的每个规则、以及注入与批量导出的边界。全部离线，不联网、不写库。
"""

from __future__ import annotations

import datetime as dt
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pilot_app import taskexport as tx  # noqa: E402

TODAY = dt.date(2026, 9, 16)
HK = "Asia/Hong_Kong"


def base_task(**overrides):
    base = {
        "task_key": uuid.uuid4().hex,
        "task_day": "2026-09-16",
        "action": "做一件事",
        "deadline": "",
        "priority": "medium",
        "user_priority": "",
        "subject": "通知",
        "sender": "老师",
    }
    base.update(overrides)
    return base


def unfold(raw: str) -> list[str]:
    lines: list[str] = []
    for piece in raw.split(tx.CRLF):
        if piece.startswith(" ") and lines:
            lines[-1] += piece[1:]
        elif piece:
            lines.append(piece)
    return lines


def summary_of(raw: str) -> str:
    return [line for line in unfold(raw) if line.startswith("SUMMARY:")][0]


def dtstart_of(raw: str) -> str:
    """The VEVENT's DTSTART (the VTIMEZONE block has one too -- skip it)."""
    inside_event = raw.split("BEGIN:VEVENT", 1)[1]
    return [line for line in unfold(inside_event) if line.startswith("DTSTART")][0]


CASES: list[tuple[str, callable]] = []


def case(name):
    def register(fn):
        CASES.append((name, fn))
        return fn
    return register


# --- 1. 类型推断（30 条：全部词表 + 边界） ---------------------------------
# 组1 作业 / 组2 考试 / 组3 注册 / 组4 缴费 / 组5 图书 / 组6 求职 / 组7 活动
# / 组8 申请 / 组9 确认，每组「中文命中」+「英文命中」各一条 = 18 条；
# 另加 12 条边界：顺序优先级 4 条、action-only 4 条、subject 干扰 4 条。

_KIND_CASES = [
    ("assignment-中文", "提交作业到 Canvas", "assignment"),
    ("assignment-英文", "Submit the homework on Canvas", "assignment"),
    ("exam-中文", "准备周五的考试", "exam"),
    ("exam-英文", "Midterm quiz covers chapters 1-5", "exam"),
    ("registration-中文", "在选课系统确认三门课", "registration"),
    ("registration-英文", "Complete your enrolment online", "registration"),
    ("payment-中文", "请在 9 月底前缴纳学费", "payment"),
    ("payment-英文", "Pay the tuition fee by Sept 30", "payment"),
    ("library-中文", "归还《计算机网络》", "library"),
    ("library-英文", "Return the overdue library book", "library"),
    ("career-中文", "参加下周的宣讲会并投简历", "career"),
    ("career-英文", "The internship recruit fair opens", "career"),
    ("event-中文", "参加周五的工作坊", "event"),
    ("event-英文", "Attend the seminar on Friday", "event"),
    ("form-中文", "填写问卷调查", "form"),
    ("form-英文", "Please fill in this survey form", "form"),
    ("reply-中文", "回复导师的邮件", "reply"),
    ("reply-英文", "Confirm your attendance by reply", "reply"),
]

for _name, _action, _kind in _KIND_CASES:
    def _make(action=_action, kind=_kind, name=_name):
        @case(f"kind/{name}")
        def _check():
            assert tx.task_kind(base_task(action=action)) == kind, \
                f"{action!r} 应为 {kind}，得到 {tx.task_kind(base_task(action=action))}"
        return _check
    _make()

@case("kind/顺序-作业优先于确认")
def _():
    # 「提交作业并回复确认」同时命中 assignment 与 reply，表序在前的 assignment 赢
    assert tx.task_kind(base_task(action="提交作业并回复确认")) == "assignment"

@case("kind/顺序-考试优先于活动")
def _():
    assert tx.task_kind(base_task(action="考试相关讲座安排")) == "exam"

@case("kind/顺序-缴费优先于确认")
def _():
    assert tx.task_kind(base_task(action="缴费后请确认")) == "payment"

@case("kind/顺序-图书优先于申请")
def _():
    assert tx.task_kind(base_task(action="归还图书的申请已通过")) == "library"

@case("kind/边界-无关键词是other")
def _():
    assert tx.task_kind(base_task(action="阅读第六章并整理笔记")) == "other"

@case("kind/边界-空action是other")
def _():
    assert tx.task_kind(base_task(action="")) == "other"

@case("kind/边界-纯数字action是other")
def _():
    assert tx.task_kind(base_task(action="12345")) == "other"

@case("kind/边界-其他不戴帽子（✅ 读起来像已完成）")
def _():
    assert tx._KIND_EMOJI["other"] == ""
    assert not tx.pretty_title(base_task(action="随便写点什么", deadline="")).startswith("✅")

@case("kind/subject不参与-图书邮件里的阅读任务")
def _():
    assert tx.task_kind(base_task(action="阅读第 2 章", subject="图书馆逾期通知")) == "other"

@case("kind/subject不参与-作业邮件里的其他任务")
def _():
    assert tx.task_kind(base_task(action="查看宿舍通知", subject="作业截止提醒")) == "other"

@case("kind/subject不参与-考试邮件里的求职任务按action算")
def _():
    assert tx.task_kind(base_task(action="参加招聘宣讲", subject="考试安排")) == "career"

@case("kind/subject不参与-活动邮件里的确认任务按action算")
def _():
    assert tx.task_kind(base_task(action="请回复是否参加", subject="活动通知")) == "reply"


# --- 2. 日期解析 → 事件落在哪天（16 条） ------------------------------------
@case("date/9/18/2026")
def _():
    assert tx.event_day(base_task(deadline="9/18/2026 23:59"), today=TODAY) == dt.date(2026, 9, 18)

@case("date/2026/9/18")
def _():
    assert tx.event_day(base_task(deadline="2026/9/18"), today=TODAY) == dt.date(2026, 9, 18)

@case("date/2026年9月18日")
def _():
    assert tx.event_day(base_task(deadline="2026年9月18日"), today=TODAY) == dt.date(2026, 9, 18)

@case("date/9月18日（当年）")
def _():
    assert tx.event_day(base_task(deadline="9月18日"), today=TODAY) == dt.date(2026, 9, 18)

@case("date/明天")
def _():
    assert tx.event_day(base_task(deadline="明天"), today=TODAY) == dt.date(2026, 9, 17)

@case("date/明天-tomorrow")
def _():
    assert tx.event_day(base_task(deadline="tomorrow"), today=TODAY) == dt.date(2026, 9, 17)

@case("date/今天")
def _():
    assert tx.event_day(base_task(deadline="今天"), today=TODAY) == dt.date(2026, 9, 16)

@case("date/后天")
def _():
    assert tx.event_day(base_task(deadline="后天"), today=TODAY) == dt.date(2026, 9, 18)

@case("date/跨年-12月的1月5日")
def _():
    assert tx.event_day(base_task(deadline="1月5日", task_day="2026-12-20"),
                        today=dt.date(2026, 12, 20)) == dt.date(2027, 1, 5)

@case("date/宽限-刚过去两天仍是今年")
def _():
    assert tx.event_day(base_task(deadline="9月14日"), today=TODAY) == dt.date(2026, 9, 14)

@case("date/无法解析落到到达日-以邮件为准")
def _():
    assert tx.event_day(base_task(deadline="以邮件为准"), today=TODAY) == dt.date(2026, 9, 16)

@case("date/无法解析落到到达日-本周")
def _():
    assert tx.event_day(base_task(deadline="本周"), today=TODAY) == dt.date(2026, 9, 16)

@case("date/无法解析落到到达日-周五（星期词不算日期）")
def _():
    assert tx.event_day(base_task(deadline="周五"), today=TODAY) == dt.date(2026, 9, 16)

@case("date/无法解析落到到达日-空")
def _():
    assert tx.event_day(base_task(deadline=""), today=TODAY) == dt.date(2026, 9, 16)

@case("date/task_day优先于today做锚点")
def _():
    # task_day 是邮件到达日：12月1日的邮件说「明天」→ 12月2日，而不是 today+1
    assert tx.event_day(base_task(deadline="明天", task_day="2026-12-01"),
                        today=TODAY) == dt.date(2026, 12, 2)

@case("date/非法月日不炸-13月40日")
def _():
    assert tx.event_day(base_task(deadline="13月40日"), today=TODAY) == dt.date(2026, 9, 16)

@case("date/英文月名-Oct 8")
def _():
    assert tx.event_day(base_task(deadline="Oct 8"), today=TODAY) == dt.date(2026, 10, 8)

@case("date/英文月名-October 8, 2026")
def _():
    assert tx.event_day(base_task(deadline="October 8, 2026"), today=TODAY) == dt.date(2026, 10, 8)

@case("date/英文日在前-8 October 2026")
def _():
    assert tx.event_day(base_task(deadline="8 October 2026"), today=TODAY) == dt.date(2026, 10, 8)

@case("date/英文缩写带点-Sep. 15")
def _():
    assert tx.event_day(base_task(deadline="Sep. 15"), today=TODAY) == dt.date(2026, 9, 15)

@case("date/只有月份没有日号不算日期-May 2026")
def _():
    assert tx.event_day(base_task(deadline="May 2026"), today=TODAY) == dt.date(2026, 9, 16)

@case("date/英文动作里的日期由reports认出来")
def _():
    from pilot_app import reports

    action = "核对 CB3410 Homework 1 (PDF 格式) 是否已提交，截止 Oct 8 05:00。"
    assert reports.deadline_of(action) == "10月8日 05:00", reports.deadline_of(action)


# --- 3. 定时事件 vs 全天事件（16 条） ---------------------------------------
@case("clock/带时刻+时区→定时DTSTART")
def _():
    raw = tx.build_ics([base_task(deadline="9/18/2026 23:59")], today=TODAY, timezone=HK)
    assert dtstart_of(raw) == "DTSTART;TZID=Asia/Hong_Kong:20260918T235900"

@case("clock/定时事件持续1小时")
def _():
    raw = tx.build_ics([base_task(deadline="9/18/2026 23:59")], today=TODAY, timezone=HK)
    assert "DTEND;TZID=Asia/Hong_Kong:20260919T005900" in unfold(raw)

@case("clock/跨午夜DTEND落到次日")
def _():
    raw = tx.build_ics([base_task(deadline="9/18/2026 23:59")], today=TODAY, timezone=HK)
    end = [line for line in unfold(raw) if line.startswith("DTEND")][0]
    assert "20260919" in end

@case("clock/带时刻无时区→全天降级")
def _():
    raw = tx.build_ics([base_task(deadline="9/18/2026 23:59")], today=TODAY)
    assert dtstart_of(raw) == "DTSTART;VALUE=DATE:20260918"

@case("clock/不带时刻有时区→全天")
def _():
    raw = tx.build_ics([base_task(deadline="9月18日")], today=TODAY, timezone=HK)
    assert dtstart_of(raw) == "DTSTART;VALUE=DATE:20260918"

@case("clock/只有时刻没有日期→全天（不造约会）")
def _():
    """2026-09-23 运营者手机截图那条：日期丢了只剩 05:00，日历上多出当天 05:00 的日程。"""
    raw = tx.build_ics([base_task(deadline="05:00", task_day="2026-09-19")],
                       today=dt.date(2026, 9, 19), timezone=HK)
    assert dtstart_of(raw) == "DTSTART;VALUE=DATE:20260919", dtstart_of(raw)
    assert "BEGIN:VTIMEZONE" not in unfold(raw)

@case("clock/英文截止落在它自己那天-Oct 8 05:00")
def _():
    raw = tx.build_ics([base_task(action="核对 CB3410 Homework 1 (PDF 格式) 是否已提交，截止 Oct 8 05:00。",
                                  deadline="10月8日 05:00", task_day="2026-09-19")],
                       today=dt.date(2026, 9, 19), timezone=HK)
    assert dtstart_of(raw) == "DTSTART;TZID=Asia/Hong_Kong:20261008T050000", dtstart_of(raw)
    summary = [line for line in unfold(raw) if line.startswith("SUMMARY:")][0]
    assert summary.count("Oct 8") == 1, summary

@case("clock/全角冒号也算时刻")
def _():
    raw = tx.build_ics([base_task(deadline="9/18/2026 23：59")], today=TODAY, timezone=HK)
    assert "DTSTART;TZID=Asia/Hong_Kong:20260918T235900" in unfold(raw)

@case("clock/单数字小时9:00")
def _():
    raw = tx.build_ics([base_task(deadline="9/18/2026 9:00")], today=TODAY, timezone=HK)
    assert "DTSTART;TZID=Asia/Hong_Kong:20260918T090000" in unfold(raw)

@case("clock/上午下午词不算时刻→全天")
def _():
    raw = tx.build_ics([base_task(deadline="9月18日 下午")], today=TODAY, timezone=HK)
    assert dtstart_of(raw) == "DTSTART;VALUE=DATE:20260918"

@case("clock/混合-一个带时刻一个不带")
def _():
    raw = tx.build_ics([base_task(deadline="9/18/2026 23:59"),
                        base_task(deadline="9月19日")], today=TODAY, timezone=HK)
    lines = unfold(raw)
    assert lines.count("BEGIN:VEVENT") == 2
    assert "DTSTART;TZID=Asia/Hong_Kong:20260918T235900" in lines
    assert "DTSTART;VALUE=DATE:20260919" in lines

@case("clock/VTIMEZONE只在有定时事件时出现")
def _():
    raw = tx.build_ics([base_task(deadline="9月18日")], today=TODAY, timezone=HK)
    assert "BEGIN:VTIMEZONE" not in unfold(raw)

@case("clock/VTIMEZONE在定时事件时出现且只一份")
def _():
    raw = tx.build_ics([base_task(deadline="9/18/2026 23:59"),
                        base_task(deadline="9/19/2026 8:00")], today=TODAY, timezone=HK)
    assert unfold(raw).count("BEGIN:VTIMEZONE") == 1

@case("clock/坏时区Not/AZone降级全天不炸")
def _():
    raw = tx.build_ics([base_task(deadline="9/18/2026 23:59")], today=TODAY,
                       timezone="Not/AZone")
    assert dtstart_of(raw) == "DTSTART;VALUE=DATE:20260918"

@case("clock/敌意时区注入不产生第二个VEVENT")
def _():
    hostile = "X\r\nEND:VTIMEZONE\r\nBEGIN:VEVENT\r\nSUMMARY:evil"
    raw = tx.build_ics([base_task(deadline="9/18/2026 23:59")], today=TODAY,
                       timezone=hostile)
    lines = unfold(raw)
    assert lines.count("BEGIN:VEVENT") == 1
    assert "SUMMARY:evil" not in lines

@case("clock/上海时区+0800正确")
def _():
    raw = tx.build_ics([base_task(deadline="9/18/2026 23:59")], today=TODAY,
                       timezone="Asia/Shanghai")
    assert "TZID:Asia/Shanghai" in unfold(raw)
    assert "DTSTART;TZID=Asia/Shanghai:20260918T235900" in unfold(raw)

@case("clock/伦敦时区（有夏令时）降级全天，不写假偏移")
def _():
    # 2026-09-19 改：Europe/London 一年里偏移会变，一个固定 STANDARD 的
    # VTIMEZONE 会在半年里差一小时。宁可退回全天事件，也不写一句假话。
    raw = tx.build_ics([base_task(deadline="9/18/2026 23:59")], today=TODAY,
                       timezone="Europe/London")
    lines = unfold(raw)
    assert "BEGIN:VTIMEZONE" not in lines
    assert "DTSTART;VALUE=DATE:20260918" in lines
    assert not any(line.startswith("DTSTART;TZID=") for line in lines)

@case("clock/全天事件DTEND排他")
def _():
    raw = tx.build_ics([base_task(deadline="9月18日")], today=TODAY)
    assert "DTEND;VALUE=DATE:20260919" in unfold(raw)


# --- 4. 美化标题（22 条） ----------------------------------------------------
@case("title/emoji+基础文案+短日期")
def _():
    title = tx.pretty_title(base_task(action="提交作业到 Canvas", deadline="9/18/2026 23:59"))
    assert title == "📝 【中】提交作业到 Canvas ⏰ 9/18 23:59", title

@case("title/年份省略")
def _():
    title = tx.pretty_title(base_task(deadline="9/18/2026 23:59"))
    assert "2026" not in title

@case("title/远期年份保留-2031")
def _():
    title = tx.pretty_title(base_task(deadline="2031/9/18"))
    assert "2031/9/18" in title

@case("title/远期年份保留-2020")
def _():
    title = tx.pretty_title(base_task(deadline="2020/9/18"))
    assert "2020/9/18" in title

@case("title/中文日期缩短为M/D")
def _():
    title = tx.pretty_title(base_task(deadline="9月18日"))
    assert "9/18" in title and "月" not in title

@case("title/无日期无⏰标签")
def _():
    title = tx.pretty_title(base_task(deadline=""))
    assert "⏰" not in title

@case("title/无法解析日期保留原文-以邮件为准")
def _():
    title = tx.pretty_title(base_task(deadline="以邮件为准"))
    assert "⏰ 以邮件为准" in title

@case("title/无法解析日期保留原文-本周")
def _():
    title = tx.pretty_title(base_task(deadline="本周"))
    assert "⏰ 本周" in title

@case("title/action自带截止时不重复-同日两种写法")
def _():
    title = tx.pretty_title(base_task(action="阅读第 6 章（截止：2026-10-06 23:59）",
                                      deadline="2026/10/6 23:59"))
    assert "⏰" not in title
    assert title.count("截止") == 1

@case("title/action日期与deadline不同则都出现")
def _():
    title = tx.pretty_title(base_task(action="阅读第 6 章（截止：2026-10-06 23:59）",
                                      deadline="10/9/2026 23:59"))
    assert "⏰ 10/9 23:59" in title

@case("title/优先级-急")
def _():
    assert "【急】" in tx.pretty_title(base_task(priority="high"))

@case("title/优先级-缓")
def _():
    assert "【缓】" in tx.pretty_title(base_task(priority="low"))

@case("title/用户优先级压过模型优先级")
def _():
    title = tx.pretty_title(base_task(priority="low", user_priority="high"))
    assert "【急】" in title and "【缓】" not in title, title

@case("title/unknown优先级无前缀")
def _():
    title = tx.pretty_title(base_task(priority="unknown"))
    assert "【" not in title

@case("title/类型emoji-缴费")
def _():
    assert tx.pretty_title(base_task(action="缴纳学费", deadline="9月20日")).startswith("💰")

@case("title/类型emoji-图书")
def _():
    assert tx.pretty_title(base_task(action="归还图书", deadline="9月20日")).startswith("📚")

@case("title/类型emoji-考试")
def _():
    assert tx.pretty_title(base_task(action="准备考试", deadline="9月20日")).startswith("📑")

@case("title/标题里的逗号分号在ICS层被转义")
def _():
    raw = tx.build_ics([base_task(action="提交作业, 附上;清单", deadline="9/18/2026 23:59")],
                       today=TODAY)
    summary = summary_of(raw)
    assert "\\," in summary and "\\;" in summary

@case("title/长标题按75字节折行且能展开")
def _():
    action = "整理课程笔记" * 30
    raw = tx.build_ics([base_task(action=action)], today=TODAY)
    for line in raw.split(tx.CRLF):
        assert len(line.encode("utf-8")) <= 75, line[:40]
    summary = summary_of(raw)
    assert summary.count("整理课程笔记") == 30

@case("title/CRLF注入被拆除")
def _():
    hostile = "看通知\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nUID:evil"
    raw = tx.build_ics([base_task(action=hostile)], today=TODAY)
    lines = unfold(raw)
    assert lines.count("BEGIN:VEVENT") == 1
    assert "UID:evil" not in lines

@case("title/控制字符被丢弃")
def _():
    assert "\x00" not in tx.pretty_title(base_task(action="正常\x00文本"))

@case("title/空action给占位符")
def _():
    assert "（无描述）" in tx.pretty_title(base_task(action=""))


# --- 5. line_for 语义未变（8 条） --------------------------------------------
@case("line/仍是优先级+行动+全文截止")
def _():
    line = tx.line_for(base_task(deadline="9/18/2026 23:59"))
    assert line == "【中】做一件事（截止 9/18/2026 23:59）", line

@case("line/无截止不加后缀")
def _():
    assert "截止" not in tx.line_for(base_task(deadline=""))

@case("line/action自带截止不重复")
def _():
    line = tx.line_for(base_task(action="阅读第 6 章（截止：2026-10-06 23:59）",
                                 deadline="2026/10/6 23:59"))
    assert line.count("截止") == 1

@case("line/无数字deadline保留-本周")
def _():
    assert "（截止 本周）" in tx.line_for(base_task(action="交作业", deadline="本周"))

@case("line/无数字deadline保留-以邮件为准")
def _():
    assert "（截止 以邮件为准）" in tx.line_for(base_task(deadline="以邮件为准"))

@case("line/用户优先级压过模型")
def _():
    assert tx.line_for(base_task(priority="low", user_priority="high")).startswith("【急】")

@case("line/清单格式-每行一个复选框")
def _():
    text = tx.build_text([base_task(), base_task(action="另一件事")])
    lines = text.splitlines()
    assert len(lines) == 2 and all(line.startswith("- [ ] ") for line in lines)

@case("line/清单里不出现转义反斜杠")
def _():
    assert "\\," not in tx.build_text([base_task(action="提交作业, 并附上")]), \
        "粘进提醒事项的文本是人读的"


# --- 6. 文件结构与批量导出（8 条） --------------------------------------------
@case("ics/合法日历骨架")
def _():
    raw = tx.build_ics([base_task()], today=TODAY)
    lines = unfold(raw)
    assert lines[0] == "BEGIN:VCALENDAR" and lines[-1] == "END:VCALENDAR"
    assert "VERSION:2.0" in lines and lines.count("BEGIN:VEVENT") == 1

@case("ics/UID稳定可重复导出")
def _():
    key = uuid.uuid4().hex
    uid = f"UID:{key}@cityu-mail-pilot"
    first = [line for line in unfold(tx.build_ics([base_task(task_key=key)], today=TODAY))
             if line.startswith("UID:")]
    again = [line for line in unfold(tx.build_ics([base_task(task_key=key)], today=TODAY))
             if line.startswith("UID:")]
    assert first == [uid] and again == [uid], (first, again)

@case("ics/批量100条不炸不丢")
def _():
    tasks = [base_task(action=f"任务 {i}", deadline="9/18/2026 23:59" if i % 2 else "9月20日")
             for i in range(100)]
    raw = tx.build_ics(tasks, today=TODAY, timezone=HK)
    assert unfold(raw).count("BEGIN:VEVENT") == 100
    assert unfold(raw).count("END:VEVENT") == 100

@case("ics/批量UID无重复")
def _():
    tasks = [base_task() for _ in range(50)]
    uids = [line for line in unfold(tx.build_ics(tasks, today=TODAY)) if line.startswith("UID:")]
    assert len(uids) == len(set(uids))

@case("ics/DTSTAMP是UTC且格式正确")
def _():
    raw = tx.build_ics([base_task()], today=TODAY,
                       now=dt.datetime(2026, 9, 16, 15, 0, tzinfo=dt.timezone.utc))
    assert unfold(raw).count("DTSTAMP:20260916T150000Z") == 1

@case("ics/CATEGORIES同时含日历名与类型")
def _():
    raw = tx.build_ics([base_task(action="缴纳学费")], today=TODAY)
    cats = [line for line in unfold(raw) if line.startswith("CATEGORIES:")][0]
    assert tx.CALENDAR_NAME in cats and "缴费" in cats

@case("ics/PRIORITY映射-急1中5")
def _():
    raw = tx.build_ics([base_task(priority="high"), base_task(priority="medium")], today=TODAY)
    lines = unfold(raw)
    assert lines.count("PRIORITY:1") == 1 and lines.count("PRIORITY:5") == 1

@case("ics/文件名ASCII且以.ics结尾")
def _():
    name = tx.filename("2026-09-16")
    assert name == "cityu-tasks-2026-09-16.ics" and name.isascii()


def main() -> int:
    failures: list[str] = []
    for name, fn in CASES:
        try:
            fn()
        except AssertionError as exc:
            failures.append(f"✗ {name}: {exc}")
        except Exception as exc:  # noqa: BLE001 — 批量校验要跑完全部案例
            failures.append(f"✗ {name}: {type(exc).__name__}: {exc}")
    total = len(CASES)
    print(f"共 {total} 条案例")
    if failures:
        for line in failures:
            print(line)
        print(f"通过 {total - len(failures)}/{total}，失败 {len(failures)}")
        return 1
    print(f"通过 {total}/{total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
