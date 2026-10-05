"""Structured, action-first report rendering (plain text + conservative HTML email).

Two layouts are produced here:

* **Immediate report** — "A / 清晰行动版": importance and the one-line conclusion
  first, then what to do and by when, then the email summary, personal relevance,
  web-search recommendations with clickable sources, risks/inferences, and a
  short English brief.
* **Daily digest** — "C / 学生简报版": what must be handled first, then grouped
  sections (urgent / academic / opportunities / admin / low-value), counts,
  deadlines, failures, and one traceable row per email. No email is ever
  dropped: anything the classifier cannot place still gets a row.

Email HTML is deliberately old-fashioned: tables, inline styles, `width`
attributes, and `mso-` conditional comments only. No JavaScript, no `<style>`
block, no flex/grid, no `<details>`, no remote fonts, no background images.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html
import re
from typing import Any, Iterable, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import snooze
from .i18n import DEFAULT_LOCALE, mark, t

HONG_KONG = "Asia/Hong_Kong"

SECTION_ORDER = (1, 2, 3, 4, 5, 6, 7)
DAILY_SECTION_ORDER = (1, 2, 3, 4, 5, 6, 7)

CONTENT_DISCLAIMER = "AI 生成内容可能出错；邮件事实、联网来源与推测已在报告中分开标注。"

# The optional model-written paragraph in the daily digest. The wording says who
# wrote it and what it is worth, because everything else around it is derived
# from the messages themselves and must not be read as the same kind of thing.
SYNTHESIS_HEADING = "一段综览（模型写的，仅供参考）"

# A URL must stop at whitespace, brackets, and CJK/full-width characters: models
# write Chinese prose directly after a link ("https://x.com/ 以及..."), and those
# runs must never be treated as part of the href. Kept ASCII-only so a Unicode
# regex never splits a percent-encoded URL such as %E4%B8%AD.
URL_PATTERN = re.compile(
    r"https://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]*"
    r"[A-Za-z0-9\-_~:/#\[\]@$&*+=%]"
)
_URL_TRAILING = ".,;:!?)]}，。；：！？、）】》"

# Canonical section identifiers for the action-first immediate report.
S_PRIORITY = 1
S_ACTIONS = 2
S_SUMMARY = 3
S_RELEVANCE = 4
S_RECOMMENDATIONS = 5
S_RISKS = 6
S_ENGLISH = 7

_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("importance", ("importance", "priority", "重要程度", "重要吗", "结论")),
    ("actions", ("needs me", "actions for me", "what i need", "需要我做什么", "必须采取的行动", "行动")),
    ("summary", ("email content summary", "content summary", "邮件内容", "内容总结")),
    ("relevance", ("personal relevance", "relationship with", "关系", "相关性")),
    ("recommendations", ("web search", "recommendation", "联网", "建议")),
    ("risks", ("risk", "inference", "风险", "推测")),
    ("english", ("english", "英文")),
]

PRIORITY_HIGH = "high"
PRIORITY_MEDIUM = "medium"
PRIORITY_LOW = "low"
PRIORITY_UNKNOWN = "unknown"

_PRIORITY_LABELS = {
    PRIORITY_HIGH: ("重要 · 需要尽快处理", "Important"),
    PRIORITY_MEDIUM: ("一般 · 建议今天看", "Normal"),
    PRIORITY_LOW: ("低优先级 · 可以稍后", "Low priority"),
    PRIORITY_UNKNOWN: ("未能判定 · 请自行判断", "Unclear"),
}

_PRIORITY_COLORS = {
    PRIORITY_HIGH: ("#b42318", "#fff1f0", "#f5c4c0"),
    PRIORITY_MEDIUM: ("#8a6100", "#fff8e8", "#f0d9a8"),
    PRIORITY_LOW: ("#475467", "#f2f4f7", "#e4e7ec"),
    PRIORITY_UNKNOWN: ("#475467", "#f2f4f7", "#e4e7ec"),
}

NO_ACTION_PATTERNS = (
    "无需行动", "不需要行动", "无需采取行动", "无行动", "无待办", "没有待办", "没有行动",
    "无。", "无 ", "不需要", "无需", "no action", "nothing to do", "no further action",
    "none required", "n/a",
)

_LOW_VALUE_HINTS = (
    "广告", "优惠", "促销", "营销", "折扣", "推广", "订阅", "newsletter", "促销活动",
    "unsubscribe", "限时", "会员", "返现", "优惠券", "推广邮件",
)

_SEARCH_FAILED_PATTERNS = (
    "未完成联网核实", "未取得可验证来源", "no live web verification",
    "未取得可核实的来源", "没有可验证来源", "未找到可验证来源",
)

_DATE_PATTERNS = (
    (re.compile(r"(?<!\d)(20\d{2})[-/](\d{1,2})[-/](\d{1,2})"), "ymd"),
    (re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"), "ymd"),
    # 美式写法（Canvas / Outlook 的英文通知）：**必须有年份**。没有年份的 `10/8`
    # 故意不认 —— 「第 6/8 周」这种比例在邮件里真的会出现，把它读成 6 月 8 日
    # 就是我们要修的那类错日期，只是换了个来源。
    (re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})/(20\d{2})(?!\d)"), "mdy"),
    (re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日"), "md"),
)

# 英文邮件里的日期（Canvas / Outlook 的通知几乎都长这样）。
#
# 没有这张表的时候，「…截止 Oct 8 05:00」里的**日期被整个丢掉、只剩时钟**：任务行显示成
# 「截止 05:00」，导出日历时再把 05:00 挂到「收到那封信的那一天」上 —— 运营者 2026-09-23
# 手机截图里那条 「截止 Oct 8 05:00」 变成 9 月 19 日 05:00 的日程，就是这么来的。
_MONTH_NUMBERS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
# 长名在前不是风格问题：`jun(?:e)?` 这类写法要让 `\b` 卡在词尾，
# 否则 "January" 会被 "jan" 匹配掉半截。
_MONTH_WORD = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?"
               r"|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
# 日号可以带序数后缀（8th），年份可以没有；`20\d{2}` 这个前缀也让
# 「May 2026」（只说月份、没有日号）**匹配不上**，不会把整个年份当日子。
_EN_MONTH_FIRST = re.compile(
    rf"\b(?P<month>{_MONTH_WORD})\.?(?:\s+|\s*[-–]\s*)(?P<day>\d{{1,2}})(?:st|nd|rd|th)?\b"
    rf"(?:\s*,?\s*(?P<year>20\d{{2}}))?", re.I)
_EN_DAY_FIRST = re.compile(
    rf"\b(?P<day>\d{{1,2}})(?:st|nd|rd|th)?(?:\s+|\s*[-–]\s*)(?P<month>{_MONTH_WORD})\.?\b"
    rf"(?:\s*,?\s*(?P<year>20\d{{2}}))?", re.I)

_DEADLINE_MARKERS = (
    "截止", "之前", "以前", "deadline", "due", "by ", "中午", "上午", "下午", "晚上",
    "今天", "明天", "后天", "本周", "这周", "下周", "周一", "周二", "周三", "周四",
    "周五", "周六", "周日", "星期", "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
)

_ACTION_MARKERS = (
    "提交", "完成", "回复", "回信", "确认", "报名", "登记", "注册", "缴费", "付", "预约",
    "参加", "出席", "下载", "填写", "上传", "联系", "检查", "查看", "准备", "打印",
    "submit", "complete", "reply", "confirm", "register", "apply", "pay", "book",
    "attend", "download", "fill", "upload", "contact", "review", "check", "prepare",
)


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def _clean(text: Any) -> str:
    return str(text or "").replace("\r", "").strip()


def _collapse(text: Any) -> str:
    return re.sub(r"\s+", " ", _clean(text)).strip()


def _inline(text: str) -> str:
    """Conservative inline HTML for one line of model output.

    Only ``https://`` links become anchors: a model-supplied ``http://`` or
    ``javascript:`` target must never reach a mail client as a clickable link.

    **2026-09-24 修的一条真 bug**：屏蔽那条正则原先匹配任意 ``scheme://``，**连
    ``https://`` 自己一起吃掉**（它先跑，把 scheme 换成「[已屏蔽非 https 链接] 」），
    于是下面把 https 变成 ``<a>`` 的那一步永远找不到东西 —— 报告正文里每一个正常
    链接都变成「[已屏蔽非 https 链接] 域名/路径」：**既不可点，又被诬成坏源**。
    结构化「来源」那一节不走这里，所以一直是好的，测试也只钉了那里（`_sources_html`），
    这就是它能藏这么久的原因。

    两处判据：① 否定前瞻 `(?!https://)` 放过 https；② 前面那个 `\\b` 保证不会从
    `https://` 的中间（`ttps://`）开始匹配 —— 少了它，前瞻只挡得住第一个位置，
    链接会被切成 `h[已屏蔽…]`。
    """
    safe = html.escape(_clean(text))
    safe = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", safe)
    safe = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<em>\1</em>", safe)
    safe = re.sub(r"`([^`]+)`", r"<strong>\1</strong>", safe)
    plain = re.sub(r"\b(?!https://)[a-zA-Z][a-zA-Z0-9+.-]*://", "[已屏蔽非 https 链接] ", safe)

    def link(match: re.Match[str]) -> str:
        url = match.group(0)
        return f'<a href="{url}" style="color:#1769aa;word-break:break-all">{url}</a>'

    return re.sub(URL_PATTERN, link, plain)


def _strip_inline(text: str) -> str:
    value = re.sub(r"\*\*(.+?)\*\*", r"\1", _clean(text))
    value = re.sub(r"`([^`]+)`", r"\1", value)
    return re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"\1", value)


def _lines(text: str) -> list[str]:
    return [_clean(line) for line in _clean(text).splitlines()]


def _line_body(line: str) -> tuple[str, str]:
    """Split one report line into (indent, body) and drop model sub-headings.

    Models routinely emit ``### 基础信息`` inside a section; that is a label, not
    content, and must never be presented as the conclusion or an action.
    """
    indent = line[: len(line) - len(line.lstrip())]
    stripped = re.sub(r"^\s*#{1,6}\s*", "", line).strip()
    return indent, stripped


_LIST_MARKER = re.compile(r"^\s*(?:[-*•·]|\d+[.)、]|□|✓)\s*(.+)$")
_LABEL_END = ("：", ":") 


def _iter_list_entries(text: str) -> list[str]:
    """Flatten a section into top-level list entries.

    Indented continuations are joined into their parent entry, and a bare label
    such as ``若确认非本人操作：`` is merged with the bullets that follow it, so a
    single decision never explodes into six pseudo-actions.
    """
    entries: list[str] = []
    pending = ""
    for raw in _lines(text):
        indent, body = _line_body(raw)
        if not body:
            continue
        match = _LIST_MARKER.match(raw)
        if match:
            item = _strip_inline(match.group(1)).strip()
            if not item:
                continue
            if indent and entries:
                entries[-1] = f"{entries[-1]} {item}".strip()
                continue
            if item.endswith(_LABEL_END):
                pending = item
                continue
            if pending:
                if entries:
                    entries[-1] = f"{entries[-1]} {pending}".strip()
                else:
                    entries.append(pending)
                pending = ""
            entries.append(item)
            continue
        if indent and entries:
            entries[-1] = f"{entries[-1]} {body}".strip()
            continue
        if body.endswith(_LABEL_END):
            pending = body
            continue
        entries.append(body)
    if pending:
        entries.append(pending)
    return [entry for entry in entries if entry]


def _bullets(text: str) -> list[str]:
    """Extract list items, falling back to sentences when the model used prose."""
    items = _iter_list_entries(text)
    if items:
        return items
    flat = _collapse(_strip_inline(text))
    if flat:
        return [part.strip() for part in re.split(r"(?<=[。；;.!?])\s*", flat) if part.strip()]
    return []


def _paragraphs(text: str) -> list[str]:
    return _iter_list_entries(text)


def _match_key(heading: str) -> str:
    needle = heading.lower()
    for key, keywords in _KEYWORDS:
        for keyword in keywords:
            if keyword in needle:
                return key
    return ""


def _resolve_timezone(name: str | None) -> dt.tzinfo:
    try:
        return ZoneInfo(name or HONG_KONG)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        try:
            return ZoneInfo(HONG_KONG)
        except (ZoneInfoNotFoundError, ValueError, KeyError):  # pragma: no cover - no tzdata
            return dt.timezone(dt.timedelta(hours=8))


def local_day_offset_hours(timezone: str | None, at: dt.datetime | None = None) -> int:
    """The user's UTC offset in whole hours, for bucketing days in SQL.

    SQLite cannot be handed a tz database name, so a day bucket has to be a fixed
    offset. Whole hours cover every zone this pilot has seen; being off by half an
    hour would only misplace usage inside a 30-minute window around local
    midnight. The point is that it is computed from the same timezone the rest of
    the page renders in, so the console cannot show a day that disagrees with the
    timestamps next to it.
    """
    moment = at or dt.datetime.now(dt.timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    offset = moment.astimezone(_resolve_timezone(timezone)).utcoffset() or dt.timedelta(0)
    return int(offset.total_seconds() // 3600)


def to_local(value: str | None, timezone: str | None = None) -> dt.datetime | None:
    """Parse an ISO timestamp (UTC from IMAP) into the user's timezone."""
    text = _clean(value)
    if not text:
        return None
    try:
        moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(_resolve_timezone(timezone))


#: 日期怎么写，按语言分。**只有两种形状**：月份在后（中日韩）与月份在前（英）。
#: 韩语的月/日各带一个自己的量词（월 / 일），而且**月与日之间要空格**（`9월 13일`）——
#: 中文与日文都不加（`9月13日`）。
_MONTH_FIRST_LOCALES = ("en",)
_MONTH_DAY_FORMATS = {"ko": "{month}월 {day}일 {time}"}
_MONTH_DAY_DEFAULT = "{month}月{day}日 {time}"


def format_moment(value: str | None, timezone: str | None = None, *, with_date: bool = True,
                  locale: str = DEFAULT_LOCALE) -> str:
    """把 UTC ISO 渲染成「给人看的时刻」，**按界面语言**。

    2026-09-23 之前这里只有中文一种写法（`9月16日 09:25`），而英文/日文/韩文界面
    也照样显示它 —— 一句中文日期混在一屏英文里。现在按 locale 分支：

    * 中文（简/繁）与日文：`9月16日 09:25`
    * 韩文：`9월 16일 09:25`
    * 英文：`Sep 16, 09:25`

    **只影响显示，不影响库里存的东西**：`parse_report` / `build_digest` 仍然把中文那版
    写进 `received_display`（那一步还不知道读的人选哪种语言），渲染时改用
    `display_when()` 从 ISO 原值现算。
    """
    local = to_local(value, timezone)
    if not local:
        return t("时间未提供", locale)
    if not with_date:
        return f"{local:%H:%M}"
    if locale in _MONTH_FIRST_LOCALES:
        return f"{local:%b} {local.day}, {local:%H:%M}"
    template = _MONTH_DAY_FORMATS.get(locale, _MONTH_DAY_DEFAULT)
    return template.format(month=local.month, day=local.day, time=f"{local:%H:%M}")


def display_when(entry: dict[str, Any], *, locale: str = DEFAULT_LOCALE,
                 timezone: str | None = None) -> str:
    """一条记录（报告/日报条目/App 列表行）的「收件时刻」，按语言现算。

    存进库里的 `received_display` 是**中文**的：落库那一刻还不知道读的人选哪种语言，
    所以它不能当译文用（`t("收件时间：{when}")` 只翻信封、翻不到里面那个日期）。
    这里优先拿 ISO 原值 `received` / `received_at` 现算；只有连 ISO 都没有时，
    才退回那句存下来的中文 —— 那时候宁可给一个中文时刻，也不要空着。
    """
    iso = entry.get("received") or entry.get("received_at")
    if iso:
        return format_moment(iso, timezone or entry.get("timezone"), locale=locale)
    return entry.get("received_display") or ""


def weekday_label(value: dt.datetime) -> str:
    names = ("一", "二", "三", "四", "五", "六", "日")
    return f"星期{names[value.weekday()]}"


def greeting_for(value: dt.datetime) -> str:
    if value.hour < 6:
        return "夜深了"
    if value.hour < 12:
        return "早上好"
    if value.hour < 18:
        return "下午好"
    return "晚上好"


def derive_priority(text: str, importance: str = "normal") -> str:
    """Read the model's importance section; fall back to the mail's own header.

    Ordering matters: an explicit ``重要性：低`` must win, so low-priority markers
    are checked before the generic "高" substring.
    """
    haystack = _clean(text).lower()
    if haystack:
        for token in ("低优先级", "优先级：低", "等级：低", "重要程度：低", "重要性：低", "不重要",
                      "可以忽略", "仅供参考", "low priority", "priority: low", "not urgent"):
            if token in haystack:
                return PRIORITY_LOW
        for token in ("高优先级", "优先级：高", "等级：高", "重要程度：高", "重要性：高", "重要",
                      "紧急", "urgent", "critical", "high priority", "priority: high",
                      "action required"):
            if token in haystack:
                return PRIORITY_HIGH
        for token in ("优先级：中", "等级：中", "重要程度：中", "重要性：中", "一般",
                      "medium", "normal priority", "priority: medium"):
            if token in haystack:
                return PRIORITY_MEDIUM
        if re.search(r"(?<![a-zA-Z])high(?![a-zA-Z])", haystack):
            return PRIORITY_HIGH
        if re.search(r"(?<![a-zA-Z])low(?![a-zA-Z])", haystack):
            return PRIORITY_LOW
        if re.search(r"(?<![a-zA-Z])(?:mid|moderate)(?![a-zA-Z])", haystack):
            return PRIORITY_MEDIUM
    if importance == "high":
        return PRIORITY_HIGH
    if importance == "low":
        return PRIORITY_LOW
    return PRIORITY_UNKNOWN


def priority_label(priority: str) -> str:
    return _PRIORITY_LABELS.get(priority, _PRIORITY_LABELS[PRIORITY_UNKNOWN])[0]


def priority_label_en(priority: str) -> str:
    return _PRIORITY_LABELS.get(priority, _PRIORITY_LABELS[PRIORITY_UNKNOWN])[1]


def priority_rank(priority: str) -> int:
    """Sort key for a priority: highest first, anything unknown last.

    Public because two other places order by it now -- the task list (where the
    user's own ranking decides the order) and nothing else should have to know
    that "high" happens to sort before "low" alphabetically backwards.
    """
    return {PRIORITY_HIGH: 0, PRIORITY_MEDIUM: 1, PRIORITY_LOW: 2, PRIORITY_UNKNOWN: 3}.get(priority, 3)


# Kept as the private spelling: this module used it before the task list did.
_priority_rank = priority_rank


def deadline_note(action: str) -> str:
    """A short "截止：…" note, or "" when the action already states the deadline."""
    deadline = deadline_of(action)
    if not deadline:
        return ""
    if deadline in action or "截止" in action:
        return ""
    return deadline


def is_actionable_line(text: str) -> bool:
    clean = _collapse(_strip_inline(text)).strip("。.；;，, ")
    if not clean:
        return False
    lowered = clean.lower()
    if lowered in {"- 无。", "无", "none", "n/a", "no"}:
        return False
    for pattern in NO_ACTION_PATTERNS:
        if lowered.startswith(pattern) and len(clean) <= len(pattern) + 6:
            return False
    return True


_DEADLINE_LEAD = re.compile(
    r"截止|之前|以前|不晚于|deadline|due\s*(?:by|on|date)|by\s",
    re.I,
)


def month_number(word: Any) -> int:
    """``"Oct"`` / ``"October"`` → 10；不是月名就是 0。"""
    return _MONTH_NUMBERS.get(str(word or "").strip().lower()[:3], 0)


def date_candidates(text: str) -> list[tuple[int, int, int, int]]:
    """文中提到的每一个日期，``(位置, 年, 月, 日)``，按出现顺序。

    **日期形状只有这一处知道**：显示（``deadline_of``）、排序（``deadline_sort_key``）、
    导出层的「动作里是不是已经写过这个截止」（``taskexport``）都从这里取，
    所以加一种写法不会出现「一边认得、一边不认得」。年份缺失时是 0（"9月18日"、"Oct 8"）。

    中文与数字写法大小写不敏感不分先后；同一个位置被两种英文写法同时匹配到时只留一个。
    """
    found: list[tuple[int, int, int, int]] = []
    for pattern, kind in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            if kind == "md" and re.search(r"20\d{2}\s*年\s*$", text[:match.start()]):
                # Do not turn a dated Chinese source into no-year evidence too.
                continue
            if kind == "ymd":
                year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
            elif kind == "mdy":
                year, month, day = int(match.group(3)), int(match.group(1)), int(match.group(2))
            else:
                year, month, day = 0, int(match.group(1)), int(match.group(2))
            found.append((match.start(), year, month, day))
    for pattern in (_EN_MONTH_FIRST, _EN_DAY_FIRST):
        for match in pattern.finditer(text):
            found.append((match.start(), int(match.group("year") or 0),
                          month_number(match.group("month")), int(match.group("day"))))
    found.sort()
    unique: list[tuple[int, int, int, int]] = []
    for item in found:
        try:
            dt.date(item[1] or 2000, item[2], item[3])
        except ValueError:
            continue
        if unique and unique[-1][0] == item[0]:
            continue
        unique.append(item)
    return unique


def date_label(candidate: tuple[int, int, int, int]) -> str:
    """一个候选日期给人看的样子：有年份 ``2026/10/8``，没有就 ``10月8日``。"""
    _start, year, month, day = candidate
    return f"{year}/{month}/{day}" if year else f"{month}月{day}日"


def months_to_numbers(text: str) -> str:
    """``"Oct 8"`` → ``"10 8"``，专给「动作里是不是已经写过这个截止」的数字比对用。

    只替换**带日号**的月名（``Oct 8`` / ``8 Oct`` 两种写法），所以
    「may submit」这种没有日号的月份不会被变成数字；年份一并去掉，因为
    「动作写 Oct 8、我们写 2026/10/8」不该被算成两个不同的截止时间。
    """

    def month_first(match: re.Match[str]) -> str:
        return f"{month_number(match.group('month'))} {int(match.group('day'))}"

    def day_first(match: re.Match[str]) -> str:
        return f"{int(match.group('day'))} {month_number(match.group('month'))}"

    return _EN_DAY_FIRST.sub(day_first, _EN_MONTH_FIRST.sub(month_first, text))


def clock_range(text: str) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """An explicit same-day time interval; unrelated clocks remain deadlines."""
    match = re.search(r"(?<!\d)([01]?\d|2[0-3])[:：]([0-5]\d)\s*"
                      r"(?:[-–—]|至|到|\bto\b)\s*"
                      r"([01]?\d|2[0-3])[:：]([0-5]\d)(?!\d)", text, re.I)
    if not match:
        return None
    start = (int(match[1]), int(match[2]))
    end = (int(match[3]), int(match[4]))
    return (start, end) if end > start else None


def deadline_of(text: str) -> str:
    """Extract an explicit deadline (date and/or clock time) from one action line.

    Models write either ``截止：2026-09-18 23:59`` or prose like
    ``今天阅读要求，周五 23:59 前提交``. A date that follows a deadline marker is
    preferred; otherwise the last non-cancellation date in the line is used, because the deadline
    is almost always the final date mentioned. English spellings (``Oct 8``,
    ``8 October 2026``) are the same date as their Chinese counterparts — a mail
    written in English must not lose its date and keep only its clock.
    """
    raw = _strip_inline(text)
    clean = _collapse(raw)
    if not clean:
        return ""
    # A cancellation clause is not the appointment that the user must attend.
    # Only drop clearly separated clauses; do not guess semantics across dates.
    clauses = re.split(r"[;；。\n]|\.(?=\s+[A-Z])|,(?=\s*(?:lecture|class)\b)", raw)
    active = [part for part in clauses if not re.search(
        r"\b(?:cancelled|canceled)\b|取消", part, re.I)
        or re.search(r"\b(?:not|never)\s+(?:cancelled|canceled)\b|不(?:会)?取消|未取消", part, re.I)]
    if active and any(date_candidates(part) for part in active):
        clean = _collapse("; ".join(active))
    marker = _DEADLINE_LEAD.search(clean)
    candidates: list[tuple[int, str]] = []
    for item in date_candidates(clean):
        candidates.append((item[0], date_label(item)))
    candidates.sort()
    pieces: list[str] = []
    if candidates:
        after_marker = [item for item in candidates if marker and item[0] >= marker.start()]
        chosen = (after_marker or candidates)[-1]
        pieces.append(chosen[1])
    clock_matches = list(re.finditer(r"(?<!\d)([01]?\d|2[0-3])[:：]([0-5]\d)(?!\d)", clean))
    if clock_matches:
        interval = clock_range(clean)
        if interval and len(clock_matches) == 2:
            start, end = interval
            pieces.append(f"{start[0]:02d}:{start[1]:02d}–{end[0]:02d}:{end[1]:02d}")
        else:
            clock = clock_matches[-1]
            pieces.append(f"{int(clock.group(1)):02d}:{clock.group(2)}")
    for marker_word in ("今天", "明天", "后天", "本周", "这周", "下周", "周一", "周二", "周三",
                        "周四", "周五", "周六", "周日", "星期", "tonight", "today", "tomorrow",
                        "this week", "next week"):
        if marker_word not in clean:
            continue
        if candidates:
            # An absolute date (with or without a clock time) already answers
            # "by when"; also printing "周五" reads as a second, contradicting
            # deadline, so the relative marker is only a last resort.
            break
        if not any(marker_word in piece for piece in pieces):
            pieces.append(marker_word)
        break
    if not pieces:
        for marker_word in _DEADLINE_MARKERS:
            if marker_word in clean.lower() or marker_word in clean:
                pieces.append("以邮件为准")
                break
    return " ".join(pieces)


def has_explicit_deadline(text: str) -> bool:
    return bool(deadline_of(text))


_RELATIVE_DAYS = {"今天": 0, "明天": 1, "后天": 2, "today": 0, "tomorrow": 1}


def deadline_sort_key(text: str, report_date: str = "") -> tuple[int, str]:
    """Order deadlines chronologically; unparseable ones go last, never absent.

    ``deadline_of`` 已经决定了这一行**显示**哪个日期（有截止标记时取标记之后
    的最后一个，否则取最后一个）——排序取同一个，否则列表按 A 排、行里印着 B。
    """
    deadline = deadline_of(text)
    if not deadline:
        return (9, "")
    base = None
    try:
        if report_date:
            base = dt.date.fromisoformat(report_date)
    except ValueError:
        base = None
    candidates = date_candidates(deadline)
    if candidates:
        _start, year, month, day = candidates[-1]
        try:
            if year:
                return (0, dt.date(year, month, day).isoformat())
            if base:
                return (0, base.replace(month=month, day=day).isoformat())
            return (1, deadline)
        except ValueError:
            return (1, deadline)
    for word, offset in _RELATIVE_DAYS.items():
        if word in deadline and base:
            return (0, (base + dt.timedelta(days=offset)).isoformat())
    if any(word in deadline for word in _RELATIVE_DAYS):
        return (0, (base or dt.date(1970, 1, 1)).isoformat())
    return (1, deadline)


def urls_in(text: str) -> list[str]:
    found = URL_PATTERN.findall(_clean(text))
    return [url.rstrip(_URL_TRAILING) for url in found]


_SOURCE_MARKERS = ("来源", "参考", "出处", "链接", "source", "reference", "link")


_SOURCE_SPLIT = re.compile(
    r"(?:来源|参考|出处|链接|参考文献|引用|source|reference|link)\s*[0-9一二三四五六七八九十]*\s*[:：]",
    re.I,
)


def _source_label(segment: str, url: str) -> str:
    """Best-effort human label for one URL, always falling back to the URL itself."""
    before = segment.split(url, 1)[0]
    explicit = False
    for marker in _SOURCE_MARKERS:
        index = before.lower().rfind(marker.lower())
        if index >= 0:
            before = before[index + len(marker):]
            explicit = True
            break
    if not explicit:
        # Generic prose ("详见 <link> 以及 <link2>") must not turn a whole
        # sentence into a source name; keep only the closest clause.
        before = re.split(r"[；;。！!？?\n]|以及|并且|\band\b", before)[-1]
    candidate = _collapse(before).strip(" -–—:：,，。;；[【(（)】]")[:90]
    if 2 <= len(candidate) <= 90:
        return candidate
    return ""


def dedupe_sources(text: str) -> list[dict[str, str]]:
    """Return labelled, de-duplicated https sources mentioned in a section.

    Models write sources either as ``来源：标题 URL`` rows or inline inside a
    paragraph; only the part that actually belongs to the link becomes a label so
    a whole sentence is never presented as a source name.
    """
    sources: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in _paragraphs(text) or _bullets(text):
        for segment in _SOURCE_SPLIT.split(line):
            for url in urls_in(segment):
                if url in seen:
                    continue
                seen.add(url)
                label = _source_label(segment, url)
                sources.append({"url": url, "label": f"{label}（{url}）" if label else url})
    return sources


def search_was_unavailable(text: str) -> bool:
    lowered = _collapse(text).lower()
    return any(pattern.lower() in lowered for pattern in _SEARCH_FAILED_PATTERNS) or not urls_in(text)


def digest_key(subject: str, sender: str) -> str:
    """Merge repeated copies of the same notice without losing any of them."""
    clean = re.sub(r"(?i)^\s*(?:re|fw|fwd|转发|回复)\s*[:：]\s*", "", _clean(subject))
    clean = re.sub(r"[\s\W_]+", "", clean).lower()
    if len(clean) >= 6:
        return "s:" + clean[:120]
    fallback = re.sub(r"[\s\W_]+", "", _clean(sender)).lower()
    return "x:" + (fallback[:60] or hashlib.sha1(_clean(subject).encode("utf-8")).hexdigest()[:16])


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #

def parse_sections(markdown: str) -> dict[str, str]:
    """Split a stored report into canonical sections, tolerating old layouts."""
    text = _clean(markdown)
    matches = list(re.finditer(r"(?m)^\s*#{1,4}\s*([1-7])\s*[.)、]?\s*(.*)$", text))
    if not matches and text:
        return {"summary": text}
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        title = match.group(2)
        key = _match_key(title) or _positional_key(int(match.group(1)))
        content = text[match.end():end].strip()
        if key and content and key not in sections:
            sections[key] = content
    return sections


def _positional_key(number: int) -> str:
    """Fallback mapping for reports written before the action-first layout."""
    return {
        1: "summary", 2: "recommendations", 3: "actions",
        4: "relevance", 5: "risks", 6: "english", 7: "english",
    }.get(number, "")


def _is_metadata_line(text: str) -> bool:
    """Sender/subject/date header lines are traceability metadata, not a conclusion."""
    lowered = _collapse(text).lower()
    return any(marker in lowered for marker in (
        "发件人", "发件邮箱", "显示发件", "sender", "from:", "主题", "subject:", "邮件接收时间",
        "收件时间", "邮件日期",
    ))


def conclusion_of(sections: dict[str, str], subject: str = "") -> str:
    for key in ("importance", "summary", "english"):
        chunk = sections.get(key, "")
        if not _clean(chunk):
            continue
        fallback = ""
        for item in _bullets(chunk) or _paragraphs(chunk):
            clean = _strip_inline(re.sub(r"^\s*#+\s*", "", item)).strip()
            # Strip the model's own label before judging the line, otherwise
            # "结论：xxx" is skipped as a label and the gist falls through to a
            # later bullet.
            clean = re.sub(
                r"^(?:等级|优先级|重要性|重要程度|结论|一句话结论|Priority|Level)\s*[:：]\s*",
                "", clean,
            ).strip()
            if clean.endswith(_LABEL_END) or len(clean) < 8:
                continue
            if _is_metadata_line(clean):
                fallback = fallback or clean
                continue
            return clean[:240]
        if fallback:
            return fallback[:240]
    return _no_conclusion(subject)


def _no_conclusion(subject: str) -> str:
    """`conclusion_of` 的兜底句。

    报告里总得有个标题，但**任务行不能把它当成模型说过的话印出来**——
    「关于「XX」的摘要」不是结论，是「这封邮件没提炼出东西」。兜底长什么样、以及
    「这句是不是兜底」都只写在这里，两处不会漂。
    """
    return f"关于「{_collapse(subject)[:80]}」的摘要" if subject else "未能提炼一句话结论。"


def usable_conclusion(parsed: dict[str, Any]) -> str:
    """模型的一句话结论；是兜底句时返回空串（界面据此整行不渲染）。"""
    conclusion = _collapse(parsed.get("conclusion"))
    return "" if conclusion == _no_conclusion(_collapse(parsed.get("subject"))) else conclusion


def actions_of(sections: dict[str, str]) -> list[str]:
    found: list[str] = []
    for item in _bullets(sections.get("actions", "")):
        if is_actionable_line(item) and item not in found:
            found.append(item)
    return found


def parse_report(markdown: str, *, subject: str = "", message: dict[str, Any] | None = None,
                 timezone: str | None = None, kind: str = "immediate") -> dict[str, Any]:
    sections = parse_sections(markdown)
    message = message or {}
    priority = derive_priority(sections.get("importance", ""), str(message.get("importance") or "normal"))
    actions = actions_of(sections)
    return {
        "kind": kind,
        "sections": sections,
        "subject": _collapse(message.get("subject") or subject)[:300],
        "sender_name": _collapse(message.get("sender_name"))[:200],
        "sender_address": _collapse(message.get("sender_address"))[:320],
        "received": _collapse(message.get("received") or message.get("received_at")),
        "received_display": format_moment(message.get("received") or message.get("received_at"), timezone),
        "importance": _collapse(message.get("importance") or "normal"),
        "priority": priority,
        "priority_label": priority_label(priority),
        "conclusion": conclusion_of(sections, message.get("subject") or subject),
        "actions": actions,
        "deadline": next((deadline_of(item) for item in actions if has_explicit_deadline(item)), ""),
        "deadlines": [deadline_of(item) for item in actions if has_explicit_deadline(item)],
        "relevance": sections.get("relevance", ""),
        "summary": sections.get("summary", ""),
        "recommendations": sections.get("recommendations", ""),
        "risks": sections.get("risks", ""),
        "english": sections.get("english", ""),
        "sources": dedupe_sources(sections.get("recommendations", "")),
        "search_unavailable": search_was_unavailable(sections.get("recommendations", "")),
        "message_id": _collapse(message.get("id")),
    }


def task_key(report_id: str, action: str) -> str:
    """A stable identity for one action item, derived from its own content.

    Tasks are not stored: they are re-derived from the report text on every
    request. So "handled" cannot be a row id or a position in a list — an index
    would point at a *different* task the moment a report gains or loses an
    action, and the user would see an unrelated item disappear.

    Hashing the report id together with the action's text is stable across
    recomputation. It is deliberately sensitive to the text: if the model
    rewrites an action, the key changes, the old "handled" mark stops matching,
    and the new wording surfaces for a fresh decision instead of being silently
    hidden behind a stale one. This is the rule static-analysis suppressions
    use — fingerprint the content, never a scan-local id.
    """
    payload = f"{_collapse(report_id)}\x1f{_collapse(action)}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:32]


def today_tasks(reports: Sequence[tuple[str, str, str]], messages: Sequence[dict[str, Any]],
                timezone: str | None = None) -> list[dict[str, Any]]:
    """Build the "what must I do" list shown on the dashboard and in the digest.

    ``reports`` items are ``(report_id, markdown, message_id)``. Each returned
    task carries a ``task_key`` and the local ``task_day`` it belongs to, which
    together are what let a user hide one item and find it again later.
    """
    by_id = {_collapse(item.get("id")): item for item in messages}
    tasks: list[dict[str, Any]] = []
    for report_id, markdown, message_id in reports:
        message = by_id.get(_collapse(message_id), {})
        parsed = parse_report(markdown, message=message, timezone=timezone)
        local = to_local(message.get("received"), timezone)
        day = local.date().isoformat() if local else ""
        for action in parsed["actions"]:
            tasks.append({
                "task_key": task_key(report_id, action),
                "task_day": day,
                "subject": parsed["subject"],
                "action": action,
                "deadline": deadline_of(action),
                "priority": parsed["priority"],
                "sender": parsed["sender_name"] or parsed["sender_address"],
                "received_display": parsed["received_display"],
                # 界面用这两个字段，别处不用：
                # `conclusion` 是「这封信在说什么」——只印 action 那一行时，用户看不出
                # 这件事的来龙去脉；兜底句已经被 `usable_conclusion` 排掉了，空串就是
                # 「没有可印的结论」，界面据此整行不渲染。
                # `received` 是 UTC ISO 原值（**不是**给人看的那个 `received_display`）：
                # 「按时间」排序要一个能比大小的值，而显示仍然只能走 `momentText()`。
                "conclusion": usable_conclusion(parsed),
                "received": parsed["received"],
                "message_id": parsed["message_id"],
            })
    tasks.sort(key=lambda item: (_priority_rank(item["priority"]), 0 if item["deadline"] else 1,
                                 item["received_display"]))
    return tasks


# --------------------------------------------------------------------------- #
# classification for the daily digest
# --------------------------------------------------------------------------- #

def classify(parsed: dict[str, Any]) -> str:
    subject = _collapse(parsed.get("subject"))
    corpus = " ".join([subject, _collapse(parsed.get("conclusion")),
                       _collapse(parsed.get("summary")), _collapse(parsed.get("relevance"))]).lower()
    lowered_subject = subject.lower()

    if any(hint in lowered_subject for hint in _LOW_VALUE_HINTS) or any(
        hint in corpus for hint in ("营销邮件", "促销邮件", "广告邮件", "newsletter", "unsubscribe")
    ):
        return "low"
    if parsed.get("actions"):
        return "urgent"
    if parsed.get("priority") == PRIORITY_HIGH:
        return "urgent"
    academic = ("课程", "作业", "考试", "成绩", "学分", "讲座", "教授", "导师", "论文", "选课",
                "tutorial", "lecture", "course", "assignment", "exam", "grade", "gpa", "semester")
    if any(word in corpus for word in academic):
        return "academic"
    opportunity = ("实习", "招聘", "比赛", "竞赛", "活动", "奖学金", "工作坊", "宣讲", "招新",
                   "internship", "career", "job", "competition", "hackathon", "scholarship",
                   "workshop", "seminar", "event")
    if any(word in corpus for word in opportunity):
        return "opportunity"
    administrative = ("缴费", "注册", "宿舍", "图书馆", "系统", "维护", "行政", "通知", "表格",
                      "截止", "tuition", "housing", "library", "portal", "notice", "reminder",
                      "administration", "fee", "enrol")
    if any(word in corpus for word in administrative):
        return "administrative"
    return "low"


CATEGORY_TITLES = {
    "urgent": "紧急待办 / Urgent",
    "academic": "学业相关 / Academic",
    "opportunity": "机会与活动 / Opportunities",
    "administrative": "行政通知 / Administrative",
    "low": "低优先级与营销 / Low priority",
    "failed": "处理失败 · 需要关注 / Failed",
}

CATEGORY_ORDER = ("urgent", "academic", "opportunity", "administrative", "low", "failed")


def _delivery_status(message: dict[str, Any]) -> str:
    """Truthful delivery state for a message that already has a report.

    ``report_status`` wins when present: a report is created before delivery and
    marked 'sent' after, while the message row is only advanced by the worker.
    Without this the 22:00 brief could report a successfully delivered report as
    "failed" simply because the worker had not finished bookkeeping yet.
    """
    report_status = _collapse(message.get("report_status"))
    if report_status in {"sent", "failed"}:
        return report_status
    message_status = _collapse(message.get("status"))
    if message_status == "failed":
        return "failed"
    return report_status or message_status or "sent"


def build_digest(messages: Sequence[dict[str, Any]], reports: dict[str, str],
                 timezone: str | None = None,
                 snoozed: Sequence[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Compose the daily student brief.

    Every processed message gets exactly one row. Messages that the sender
    filter deliberately skipped get their own auditable list instead of being
    counted as failures, so "we did not analyse it" and "it failed" stay
    distinguishable.

    ``snoozed`` is the user's ``task_states`` rows; the ones still asleep become
    one line of the deterministic list ("你让它稍后提醒的 N 件…"). It is a **fact**,
    so it is computed here and never handed to the model -- and it never sends a
    message of its own: the brief is already going out, this is one more line in
    it.
    """
    items: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for message in messages:
        if str(message.get("status")) == "skipped":
            skipped.append({
                "subject": _collapse(message.get("subject")) or "（无主题）",
                "sender": _collapse(message.get("sender_name")) or _collapse(message.get("sender_address")),
                "sender_address": _collapse(message.get("sender_address")),
                "received_display": format_moment(message.get("received_at"), timezone),
                "reason": _collapse(message.get("skip_reason")) or "未说明原因",
            })
            continue
        message_id = _collapse(message.get("id"))
        body = reports.get(message_id)
        if not body:
            items.append({
                "key": "m:" + message_id, "message_id": message_id,
                "subject": _collapse(message.get("subject")) or "（无主题）",
                "sender": _collapse(message.get("sender_name")) or _collapse(message.get("sender_address")),
                "sender_address": _collapse(message.get("sender_address")),
                "received": _collapse(message.get("received_at")),
                "received_display": format_moment(message.get("received_at"), timezone),
                "status": _collapse(message.get("status")) or "pending",
                "last_error": _collapse(message.get("last_error")),
                "priority": PRIORITY_UNKNOWN, "priority_label": priority_label(PRIORITY_UNKNOWN),
                "conclusion": "这封邮件还没有生成摘要，请稍后查看或检查错误信息。",
                "actions": [], "deadline": "", "sources": [], "relevance": "",
                "search_unavailable": True, "duplicates": 0,
                "category": "failed" if _collapse(message.get("status")) == "failed" else "urgent",
                "parsed": None,
            })
            continue
        parsed = parse_report(body, message=message, timezone=timezone)
        category = classify(parsed)
        items.append({
            "key": digest_key(parsed["subject"], parsed["sender_address"] or parsed["sender_name"]),
            "message_id": message_id,
            "subject": parsed["subject"] or "（无主题）",
            "sender": parsed["sender_name"] or parsed["sender_address"],
            "sender_address": parsed["sender_address"],
            "received": parsed["received"],
            "received_display": parsed["received_display"],
            # A stored report means the mail WAS analysed. The message row can
            # legitimately still be 'pending'/'processing' (the worker marks it
            # after delivery, and the daily digest may run in between), so the
            # report status is the truthful delivery state here. Treating the
            # message status as authoritative made successful reports show up as
            # "failed" in the brief.
            "status": _delivery_status(message),
            "last_error": _collapse(message.get("last_error")),
            "priority": parsed["priority"],
            "priority_label": parsed["priority_label"],
            "conclusion": parsed["conclusion"],
            "actions": parsed["actions"],
            "deadline": parsed["deadline"],
            "sources": parsed["sources"],
            "relevance": _collapse(parsed["relevance"])[:600],
            "search_unavailable": parsed["search_unavailable"],
            "duplicates": 0,
            "category": category,
            "parsed": parsed,
        })

    merged: list[dict[str, Any]] = []
    index: dict[str, int] = {}
    for item in items:
        existing = index.get(item["key"])
        if existing is None:
            index[item["key"]] = len(merged)
            merged.append(item)
            continue
        primary = merged[existing]
        primary["duplicates"] += 1
        if _priority_rank(item["priority"]) < _priority_rank(primary["priority"]):
            primary["priority"] = item["priority"]
            primary["priority_label"] = item["priority_label"]
        if item["received"] > primary["received"]:
            primary["received"] = item["received"]
            primary["received_display"] = item["received_display"]
        if not primary["actions"] and item["actions"]:
            primary["actions"] = item["actions"]
            primary["deadline"] = item["deadline"]
            primary["conclusion"] = item["conclusion"]
        if not primary["sources"] and item["sources"]:
            primary["sources"] = item["sources"]
            primary["search_unavailable"] = item["search_unavailable"]
        if primary["category"] == "low" and item["category"] != "low":
            primary["category"] = item["category"]

    sections: dict[str, list[dict[str, Any]]] = {name: [] for name in CATEGORY_ORDER}
    for item in merged:
        sections[item["category"]].append(item)
    for name, entries in sections.items():
        rank = {"failed": 0, "urgent": 1, "academic": 2, "opportunity": 3, "administrative": 4, "low": 5}[name]
        entries.sort(key=lambda entry: (_priority_rank(entry["priority"]), rank, entry["received"]))

    all_entries = [entry for name in CATEGORY_ORDER for entry in sections[name]]
    ordered_tasks = sorted(
        ((entry, action) for entry in all_entries for action in entry["actions"]),
        key=lambda pair: (_priority_rank(pair[0]["priority"]), deadline_sort_key(pair[1]),
                          pair[0]["received"]),
    )
    deadlines = sorted(
        {deadline_of(action) for _, action in ordered_tasks if deadline_of(action)},
        key=deadline_sort_key,
    )
    skipped_items = skipped
    failed = len(sections["failed"])
    unprocessed = sum(1 for entry in all_entries if entry["status"] not in {"sent"})
    low_count = sum(1 + entry["duplicates"] for entry in sections["low"])
    without_sources = sum(1 for entry in all_entries if entry["search_unavailable"] and entry["status"] == "sent")

    return {
        "date": "",
        "generated_at": "",
        "timezone": timezone or HONG_KONG,
        "items": all_entries,
        "merged": merged,
        "sections": sections,
        "tasks": [{"subject": entry["subject"], "action": action, "deadline": deadline_of(action),
                   "priority": entry["priority"], "sender": entry["sender"],
                   "received_display": entry["received_display"], "message_id": entry["message_id"]}
                  for entry, action in ordered_tasks],
        "deadlines": deadlines,
        "next_deadline": deadlines[0] if deadlines else "",
        "skipped": skipped_items,
        # 「稍后提醒」那一行（没有就是空串，整行不出现）。算在这里而不是渲染时：
        # 渲染函数因此不读时钟，同一份 digest 渲染两次逐字相同——发出去的邮件与
        # 存在库里的那份正文才不会各说各话。
        "snoozed_line": snooze.digest_line(list(snoozed or []),
                                           now=dt.datetime.now(dt.timezone.utc),
                                           timezone=timezone or HONG_KONG),
        "metrics": {
            "total": len(messages),
            "skipped": len(skipped_items),
            "merged_total": len(merged),
            "actionable": len(ordered_tasks),
            "failed": failed + max(0, unprocessed - failed),
            "unprocessed": unprocessed,
            "low_priority": low_count,
            "without_sources": without_sources,
            "duplicates": sum(entry["duplicates"] for entry in merged),
            "sections": {name: sum(1 + entry["duplicates"] for entry in entries)
                         for name, entries in sections.items()},
        },
    }


# --------------------------------------------------------------------------- #
# markdown for storage
# --------------------------------------------------------------------------- #

def _synthesis_markdown(digest: dict[str, Any], *, locale: str = DEFAULT_LOCALE) -> list[str]:
    """The optional model paragraph, as a block quote.

    A quote rather than a numbered section on purpose: every other heading in
    this document is generated from the messages themselves, and a model-written
    paragraph must not read as one more of them. The heading says who wrote it.
    """
    text = str(digest.get("synthesis") or "").strip()
    if not text:
        return []
    lines = [f"\n> **{t(SYNTHESIS_HEADING, locale)}**"]
    lines.extend(f"> {line}" for line in text.splitlines())
    return lines


def digest_markdown(digest: dict[str, Any], *, locale: str = DEFAULT_LOCALE) -> str:
    metrics = digest["metrics"]
    out: list[str] = []
    out.append(t("## 1. 今天最重要 / Most important today", locale))
    if digest["tasks"]:
        for task in digest["tasks"][:3]:
            suffix = (t("（截止：{when}）", locale, when=task["deadline"])
                      if task["deadline"] else "")
            out.append(t("- {action}{suffix} · 来自「{subject}」", locale, action=task["action"],
                         suffix=suffix, subject=task["subject"]))
    else:
        out.append(t("- 今天没有必须立刻处理的事项。", locale))
        out.append(t("- 下一封新邮件到达时会自动生成即时摘要。", locale))
    out.extend(_synthesis_markdown(digest, locale=locale))
    for number, name in enumerate(CATEGORY_ORDER, start=2):
        if name == "failed":
            break  # listed in section 7 below
        entries = digest["sections"][name]
        out.append(f"\n## {number}. {t(CATEGORY_TITLES[name], locale)}")
        if not entries:
            out.append(t("- 无。", locale))
            continue
        for entry in entries:
            repeat = (t("（另有 {n} 封同类邮件）", locale, n=entry["duplicates"])
                      if entry["duplicates"] else "")
            line = t("- **{subject}**{repeat} · 发件人：{who} · 收件：{when}", locale,
                     subject=entry["subject"], repeat=repeat,
                     who=entry["sender"] or t("未知", locale),
                     when=entry["received_display"])
            if entry["actions"]:
                line += t(" · 待办：{action}", locale, action=entry["actions"][0])
                if entry["deadline"]:
                    line += t("（截止 {when}）", locale, when=entry["deadline"])
            out.append(line)
            if entry["status"] != "sent":
                out.append(t("  - ⚠ 状态：{status} {error}", locale, status=entry["status"],
                             error=entry["last_error"][:200]))
    out.append("\n" + t("## 7. 处理失败 · 需要关注 / Failed", locale))
    failed = digest["sections"]["failed"]
    if failed:
        for entry in failed:
            out.append(t("- {subject} · 发件人：{who} · 收件：{when} · 状态：{status} {error}",
                         locale, subject=entry["subject"],
                         who=entry["sender"] or t("未知", locale),
                         when=entry["received_display"], status=entry["status"],
                         error=entry["last_error"][:200]))
    else:
        out.append(t("- 无失败记录；所有邮件都已生成摘要。", locale))
    out.append("\n" + t("## 8. 今天的数字与异常 / Today's numbers and exceptions", locale))
    out.append(t("- 收到邮件：{n} 封（合并同类后 {m} 条）", locale,
                 n=metrics["total"], m=metrics["merged_total"]))
    out.append(t("- 需要行动：{n} 项", locale, n=metrics["actionable"]))
    # 「稍后提醒」那一行紧跟在「需要行动」下面：它解释的正是**为什么有几件不在上面**。
    # 它属于确定性清单，不属于 `_synthesis_markdown` 那段模型写的话——事实与叙述分家
    # 的理由见 docs/snooze-2026-09-23.md。
    if digest.get("snoozed_line"):
        out.append(f"- {digest['snoozed_line']}")
    out.append(t("- 处理失败或未完成：{n} 封", locale, n=metrics["failed"]))
    out.append(t("- 低优先级/营销：{n} 封", locale, n=metrics["low_priority"]))
    out.append(t("- 最近截止时间：{when}", locale,
                 when=digest["next_deadline"] or t("无明确截止时间", locale)))
    if metrics.get("skipped"):
        out.append(t("- 另有 {n} 封邮件不属于允许的发件人范围，未做 AI 处理（不是为了丢弃，见下方清单）。",
                     locale, n=metrics["skipped"]))
    if metrics["without_sources"]:
        out.append(t("- 有 {n} 封邮件本次未取得可验证来源（已按邮件标注，未伪造引用）。",
                     locale, n=metrics["without_sources"]))
    if digest["metrics"]["duplicates"]:
        out.append(t("- 已合并 {n} 封同类重复邮件，数量仍计入总数。", locale,
                     n=digest["metrics"]["duplicates"]))
    if not digest["items"]:
        out.append(t("- 今天没有收到需要处理的新邮件。", locale))
    skipped = digest.get("skipped") or []
    if skipped:
        out.append(t("- 被跳过（非允许发件人，未做 AI 处理）：{n} 封", locale, n=len(skipped)))
        for entry in skipped[:10]:
            # Full address, not a display name: this list is the audit trail
            # proving which mail was deliberately not analysed.
            who = entry.get("sender_address") or entry.get("sender") or t("未知发件人", locale)
            out.append(f"  - {entry['subject'][:60]} · {who} · {entry.get('reason', '')[:60]}")
    return "\n".join(out)


def with_digest_header(digest: dict[str, Any], report_date: str, generated_at: str) -> dict[str, Any]:
    digest["date"] = report_date
    digest["generated_at"] = generated_at
    return digest


# --------------------------------------------------------------------------- #
# HTML: shared primitives
# --------------------------------------------------------------------------- #

FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def _table(rows: str, *, width: int = 600, padding: str = "0") -> str:
    """Outlook-safe fixed-width table that also shrinks on a 360px phone.

    ``width``/``max-width`` are written both as HTML attributes and inline CSS
    (the Word renderer trusts the attribute; browsers trust the CSS), and
    ``table-layout:fixed`` plus ``word-break`` stop an unbreakable token in an
    email from pushing the whole layout wider than the viewport.
    """
    return (f'<table role="presentation" cellpadding="0" cellspacing="0" border="0" '
            f'width="100%" style="width:100%;max-width:{width}px;margin:0 auto;'
            f'border-collapse:collapse;padding:{padding};table-layout:fixed;'
            f'word-break:break-word;overflow-wrap:anywhere">{rows}</table>')


def _inner_table(rows: str, *, extra: str = "") -> str:
    """A 100%-wide table inside the frame.

    ``table-layout:fixed`` plus per-cell ``word-break`` are what keep an
    unbreakable token (a 200-character URL in a model citation, or a long CJK
    run) from widening the whole email on a phone.
    """
    return ('<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
            'style="width:100%;max-width:100%;border-collapse:collapse;table-layout:fixed;'
            f'word-break:break-word;overflow-wrap:anywhere;{extra}">{rows}</table>')


def _badge(text: str, priority: str) -> str:
    color, background, border = _PRIORITY_COLORS.get(priority, _PRIORITY_COLORS[PRIORITY_UNKNOWN])
    return (f'<span style="display:inline-block;padding:4px 10px;border-radius:99px;'
            f'background:{background};color:{color};border:1px solid {border};'
            f'font-size:12px;font-weight:700">{html.escape(text)}</span>')


def _paragraph_html(text: str, *, size: int = 14, color: str = "#243447", muted: bool = False) -> str:
    if not _clean(text):
        return ""
    align = "left"
    return (f'<p style="margin:8px 0;font-size:{size}px;line-height:1.6;text-align:{align};'
            f'color:{"#667085" if muted else color}">{_inline(text)}</p>')


def _bullets_html(items: Iterable[str], *, ordered: bool = False, show_deadline: bool = False,
                  mark_inference: bool = False, locale: str = DEFAULT_LOCALE) -> str:
    entries = [item for item in items if _clean(item)]
    if not entries:
        return ""
    tag = "ol" if ordered else "ul"
    rows = []
    for item in entries:
        deadline = deadline_note(item) if show_deadline else ""
        suffix = (f'<br><span style="font-size:12px;color:#8a6100">'
                  f'{t("截止：{when}", locale, when=html.escape(deadline))}</span>'
                  if deadline else "")
        inference = ""
        if mark_inference and re.search(r"推测|推断|inference|assumption|uncertain|不确定", item, re.I):
            inference = ('<span style="font-size:11px;color:#8a6100;background:#fff8e8;'
                         'border:1px solid #f0d9a8;border-radius:99px;padding:1px 7px;margin-right:6px">'
                         + t("AI 推测", locale) + '</span>')
        rows.append(f'<li style="margin:7px 0;font-size:14px;line-height:1.6">'
                    f'{inference}{_inline(item)}{suffix}</li>')
    return (f'<{tag} style="margin:8px 0;padding-left:22px;color:#243447">{ "".join(rows) }</{tag}>')


def _section(title: str, body: str, *, accent: str = "#123b63",
             locale: str = DEFAULT_LOCALE) -> str:
    if not _clean(body):
        body = ('<p style="margin:8px 0;font-size:14px;color:#667085">'
                f'{t("本次报告未提供这一部分。", locale)}</p>')
    return (
        '<tr><td style="padding:18px 22px 0">'
        f'<div style="font-size:13px;font-weight:700;color:{accent};letter-spacing:.02em;'
        f'border-bottom:1px solid #e4eaf0;padding-bottom:6px">{html.escape(title)}</div>'
        f'<div style="padding-top:4px">{body}</div>'
        '</td></tr>'
    )


def _callout(label: str, body: str, *, background: str = "#eaf4fc", border: str = "#b9d7ee",
             color: str = "#123b63") -> str:
    return (
        f'<div style="background:{background};border:1px solid {border};border-radius:10px;'
        f'padding:14px 16px;margin:6px 0">'
        f'<div style="font-size:12px;color:{color};font-weight:700">{html.escape(label)}</div>'
        f'<div style="font-size:15px;line-height:1.6;color:#1d2939;margin-top:5px">{_inline(body)}</div>'
        '</div>'
    )


def _email_shell(title: str, subtitle: str, body_rows: str, *, footer: str = CONTENT_DISCLAIMER,
                 locale: str = DEFAULT_LOCALE) -> str:
    """Outlook-safe single-column shell: tables, inline styles, no JS, no <style>.

    Compatibility choices follow the reviewed open-source guidance (react-email
    styling rules, leemunroe's responsive template, caniemail): the 600px frame
    is a fixed table with the width duplicated as an attribute and as inline CSS,
    colours use ``bgcolor`` where it matters, every cell carries explicit
    padding, and nothing depends on a ``<style>`` block, ``@media``, flex, grid,
    remote fonts, images or JavaScript.
    """
    return (
        '<!doctype html>'
        f'<html lang="{html.escape(locale, quote=True)}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="x-apple-disable-message-reformatting">'
        '<meta name="format-detection" content="telephone=no">'
        '<title>' + html.escape(title) + '</title></head>'
        '<body style="margin:0;padding:0;background-color:#f3f6f9;'
        f'-webkit-text-size-adjust:100%;font-family:{FONT};color:#1d2939;'
        'word-break:break-word;overflow-wrap:anywhere">'
        '<div style="display:none;font-size:1px;color:#f3f6f9;max-height:0;overflow:hidden;'
        'mso-hide:all">' + html.escape(subtitle[:120]) + '</div>'
        '<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" '
        'bgcolor="#f3f6f9" style="width:100%;background-color:#f3f6f9;border-collapse:collapse">'
        '<tr><td align="center" style="padding:20px 10px">'
        '<!--[if mso]><table role="presentation" cellpadding="0" cellspacing="0" border="0" '
        'width="600"><tr><td width="600"><![endif]-->'
        + _table(
            '<tr><td bgcolor="#123b63" style="background-color:#123b63;padding:20px 22px;color:#ffffff">'
            '<div style="font-size:11px;letter-spacing:.10em;color:#bcd7ea">CITYU MAIL PILOT</div>'
            f'<div style="font-size:19px;font-weight:700;line-height:1.35;margin-top:6px">{html.escape(title)}</div>'
            f'<div style="font-size:12px;color:#cfe3f2;margin-top:6px">{_inline(subtitle)}</div>'
            '</td></tr>'
            '<tr><td bgcolor="#ffffff" style="background-color:#ffffff;border:1px solid #dbe3eb;'
            'border-top:0;padding:0 0 18px">'
            + body_rows +
            '</td></tr>'
            '<tr><td style="padding:12px 4px 0;font-size:11px;line-height:1.6;color:#98a2b3">'
            + html.escape(footer) + '</td></tr>',
        )
        + '<!--[if mso]></td></tr></table><![endif]-->'
        + '</td></tr></table></body></html>'
    )


def _meta_row(parsed: dict[str, Any], *, locale: str = DEFAULT_LOCALE) -> str:
    sender = parsed["sender_name"] or parsed["sender_address"] or t("未知发件人", locale)
    address = f' &lt;{html.escape(parsed["sender_address"])}&gt;' if parsed["sender_address"] else ""
    return (f'<div style="font-size:13px;color:#475467;margin-top:8px;line-height:1.6">'
            f'{t("发件人：{who}", locale, who=html.escape(sender))}{address}<br>'
            f'{t("收件时间：{when}", locale, when=html.escape(display_when(parsed, locale=locale)))}　·　'
            f'{t("邮件头优先级：{level}", locale, level=html.escape(parsed["importance"] or "normal"))}'
            f'</div>')


def _sources_html(parsed: dict[str, Any], *, locale: str = DEFAULT_LOCALE) -> str:
    if parsed["sources"]:
        rows = []
        for index, source in enumerate(parsed["sources"][:8], 1):
            rows.append(
                '<li style="margin:6px 0;font-size:13px;line-height:1.6;word-break:break-all">'
                f'<a href="{html.escape(source["url"], quote=True)}" '
                f'style="color:#1769aa;text-decoration:underline">{html.escape(source["label"])}</a></li>'
            )
        return ('<ul style="margin:8px 0;padding-left:20px">' + "".join(rows) + '</ul>')
    return _paragraph_html(t("本次未取得可验证来源 / No verifiable live source was retrieved for this message. "
                             "报告中的建议仅供参考，未做联网核实。", locale), muted=True)


# --------------------------------------------------------------------------- #
# HTML: immediate report (A — action first)
# --------------------------------------------------------------------------- #

def render_immediate_html(parsed: dict[str, Any], *, subject: str | None = None,
                          locale: str = DEFAULT_LOCALE) -> str:
    title = _clean(subject) or parsed["subject"] or t("邮件摘要", locale)
    critical = [
        (t("这封邮件重要吗", locale), t(parsed["priority_label"], locale)),
        (t("一句话结论", locale), parsed["conclusion"]),
    ]
    if parsed["actions"]:
        first = parsed["actions"][0]
        critical.append((t("你要做什么", locale), first))
        critical.append((t("什么时候之前", locale),
                         deadline_of(first) or t("邮件没有给出明确截止时间", locale)))
    else:
        critical.append((t("你要做什么", locale), t("这封邮件不需要你采取行动。", locale)))
        critical.append((t("什么时候之前", locale), t("无截止时间", locale)))
    cells = []
    for label, value in critical:
        cells.append(
            '<tr><td style="padding:9px 0;border-bottom:1px solid #eef2f6;vertical-align:top">'
            f'<div style="font-size:12px;color:#667085">{html.escape(label)}</div>'
            f'<div style="font-size:14px;line-height:1.6;color:#1d2939;margin-top:3px">{_inline(value)}</div>'
            '</td></tr>'
        )
    priority_badge = _badge(t(parsed["priority_label"], locale), parsed["priority"])
    rows = (
        '<tr><td style="padding:18px 22px 0">'
        f'<div>{priority_badge}</div>'
        f'<div style="font-size:13px;color:#475467;margin-top:10px">{html.escape(title)}</div>'
        + _meta_row(parsed, locale=locale) +
        '</td></tr>'
        '<tr><td style="padding:14px 22px 0">'
        + _inner_table("".join(cells)) + '</td></tr>'
        + _section(t("你应该做什么 / What to do", locale),
                   _bullets_html(parsed["actions"], ordered=True, show_deadline=True, locale=locale)
                   or _paragraph_html(t("无需行动 / No action required.", locale), muted=True),
                   locale=locale)
        + _section(t("邮件讲了什么 / What the email says", locale),
                   _bullets_html(_bullets(parsed["summary"]), locale=locale)
                   or _paragraph_html(parsed["summary"])
                   or _paragraph_html(t("邮件正文为空或无法解析。", locale), muted=True),
                   locale=locale)
        + _section(t("为什么与你有关 / Why it matters to you", locale),
                   _paragraph_html(parsed["relevance"]), locale=locale)
        + _section(t("联网搜索后的建议 / Suggestions from web search", locale),
                   _paragraph_html(parsed["recommendations"]) + _sources_html(parsed, locale=locale),
                   locale=locale)
        + _section(t("风险、未知与推测 / Risks, unknowns and inferences", locale),
                   _bullets_html(_bullets(parsed["risks"]), mark_inference=True, locale=locale)
                   or _paragraph_html(parsed["risks"]), locale=locale)
        + _section("English brief", _paragraph_html(parsed["english"]) or
                   _paragraph_html("English summary was not provided for this message.", muted=True),
                   accent="#475467")
    )
    subtitle = t("即时摘要 · {when} · {level}", locale,
                 when=display_when(parsed, locale=locale), level=priority_label_en(parsed["priority"]))
    return _email_shell(title, subtitle, rows, footer=t(CONTENT_DISCLAIMER, locale), locale=locale)


def render_immediate_text(parsed: dict[str, Any], *, subject: str | None = None,
                          locale: str = DEFAULT_LOCALE) -> str:
    title = _clean(subject) or parsed["subject"] or t("邮件摘要", locale)
    out = [title, "=" * min(len(title), 60),
           t("重要程度：{level}", locale, level=t(parsed["priority_label"], locale)),
           t("结论：{conclusion}", locale, conclusion=parsed["conclusion"]),
           t("发件人：{who}", locale,
             who=parsed["sender_name"] or parsed["sender_address"] or t("未知", locale)),
           t("收件时间：{when}", locale, when=display_when(parsed, locale=locale)), ""]
    out.append(t("【你应该做什么】", locale))
    if parsed["actions"]:
        for index, action in enumerate(parsed["actions"], 1):
            note = deadline_note(action)
            out.append(f"{index}. {action}"
                       + (t("（截止：{when}）", locale, when=note) if note else ""))
    else:
        out.append(t("无需行动。", locale))
    out.append("")
    out.append(t("【邮件讲了什么】", locale))
    out.extend(f"- {item}" for item in (_paragraphs(parsed["summary"]) or [t("未提供。", locale)]))
    out.append("")
    out.append(t("【为什么与你有关】", locale))
    out.extend(f"- {item}" for item in (_paragraphs(parsed["relevance"]) or [t("未提供。", locale)]))
    out.append("")
    out.append(t("【联网搜索后的建议】", locale))
    out.extend(f"- {item}" for item in
               (_paragraphs(parsed["recommendations"]) or [t("本次未取得可验证来源。", locale)]))
    for source in parsed["sources"][:8]:
        out.append(f"  · {source['label']}")
    out.append("")
    out.append(t("【风险、未知与推测】", locale))
    out.extend(f"- {item}" for item in (_paragraphs(parsed["risks"]) or [t("未提供。", locale)]))
    out.append("")
    out.append("【English brief】")
    out.extend(f"- {item}" for item in (_paragraphs(parsed["english"]) or ["Not provided."]))
    out.append("")
    out.append(t(CONTENT_DISCLAIMER, locale))
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# HTML: daily digest (C — student brief)
# --------------------------------------------------------------------------- #

def _digest_item_html(entry: dict[str, Any], *, locale: str = DEFAULT_LOCALE) -> str:
    repeat = (f'<span style="font-size:12px;color:#667085">'
              f'{t("（另有 {n} 封同类邮件）", locale, n=entry["duplicates"])}</span>'
              if entry["duplicates"] else "")
    status = ""
    if entry["status"] != "sent":
        warning = t("⚠ 状态：{status} {error}", locale, status=html.escape(entry["status"]),
                    error=html.escape(entry["last_error"][:200]))
        status = (f'<div style="font-size:12px;color:#b42318;margin-top:5px">{warning}</div>')
    actions = ""
    if entry["actions"]:
        rows = "".join(
            f'<li style="margin:4px 0;font-size:13px;line-height:1.6">{_inline(action)}'
            + (f'<span style="color:#8a6100">'
               f'{t("（截止 {when}）", locale, when=html.escape(deadline_note(action)))}</span>'
               if deadline_note(action) else "")
            + '</li>'
            for action in entry["actions"][:4]
        )
        actions = f'<ul style="margin:6px 0 0;padding-left:20px">{rows}</ul>'
    sources = ""
    if entry["sources"]:
        links = " · ".join(
            f'<a href="{html.escape(source["url"], quote=True)}" style="color:#1769aa;word-break:break-all">'
            f'{html.escape(source["label"][:70])}</a>' for source in entry["sources"][:3]
        )
        sources = (f'<div style="font-size:12px;margin-top:6px">'
                   f'{t("来源：{links}", locale, links=links)}</div>')
    elif entry["status"] == "sent":
        sources = (f'<div style="font-size:12px;color:#667085;margin-top:6px">'
                   f'{t("本次未取得可验证来源", locale)}</div>')
    # 这一行是「谁 · 什么时候 · 多要紧」三件事拼起来的**一句**（不是三段），
    # 所以整句进词典、三个值当参数 —— 日/韩的语序与中文不同，拼片段会拼出病句。
    who = html.escape(entry["sender"] or t("未知发件人", locale))
    level = html.escape(t(entry["priority_label"], locale))
    meta_line = t("{who} · {when} · {level}", locale, who=who,
                  when=html.escape(display_when(entry, locale=locale, timezone=entry.get("timezone"))), level=level)
    return (
        '<tr><td style="padding:14px 0;border-top:1px solid #eef2f6;word-break:break-word;overflow-wrap:anywhere">'
        f'<div style="font-size:14px;font-weight:700;line-height:1.5;color:#1d2939">'
        f'{html.escape(entry["subject"])} {repeat}</div>'
        f'<div style="font-size:12px;color:#667085;margin-top:4px">'
        f'{meta_line}</div>'
        f'<div style="font-size:13px;line-height:1.6;color:#344054;margin-top:6px">'
        f'{_inline(entry["conclusion"])}</div>'
        + actions + sources + status +
        '</td></tr>'
    )


def _digest_section_html(title: str, entries: list[dict[str, Any]], *, note: str = "",
                         locale: str = DEFAULT_LOCALE) -> str:
    if not entries:
        return ""
    rows = "".join(_digest_item_html(entry, locale=locale) for entry in entries)
    tables = _inner_table(rows)
    footer = (f'<div style="font-size:12px;color:#667085;margin-top:8px">{_inline(note)}</div>'
              if note else "")
    return (
        '<tr><td style="padding:18px 22px 0">'
        f'<div style="font-size:13px;font-weight:700;color:#123b63;letter-spacing:.02em;'
        f'border-bottom:1px solid #e4eaf0;padding-bottom:6px">{html.escape(title)}</div>'
        f'<div>{tables}{footer}</div></td></tr>'
    )


def _digest_metric_html(digest: dict[str, Any], *, locale: str = DEFAULT_LOCALE) -> str:
    metrics = digest["metrics"]
    cells = [
        (t("今日邮件", locale), t("{n} 封", locale, n=metrics["total"])),
        (t("需要行动", locale), t("{n} 项", locale, n=metrics["actionable"])),
        (t("最近截止", locale), digest["next_deadline"] or t("无明确截止", locale)),
        (t("异常/失败", locale), t("{n} 封", locale, n=metrics["failed"])),
    ]
    rendered = []
    for index, (label, value) in enumerate(cells):
        border = "border-left:1px solid #e4e7ec;" if index else ""
        rendered.append(
            f'<td width="25%" style="padding:10px 8px;word-break:break-word;overflow-wrap:anywhere;{border}vertical-align:top">'
            f'<div style="font-size:11px;color:#667085">{html.escape(label)}</div>'
            f'<div style="font-size:14px;font-weight:700;color:#1d2939;margin-top:3px;'
            f'word-break:break-word">{html.escape(value)}</div></td>'
        )
    return _inner_table(
        '<tr>' + "".join(rendered) + '</tr>',
        extra='background-color:#f8fafc;border:1px solid #e4e7ec',
    )


def render_digest_html(digest: dict[str, Any], *, subject: str | None = None,
                       locale: str = DEFAULT_LOCALE) -> str:
    date_label = digest.get("date") or ""
    headline = (f'{date_label} · ' if date_label else "") + \
        (t("今天有 {n} 件事需要处理", locale, n=digest["metrics"]["actionable"])
         if digest["metrics"]["actionable"] else t("今天没有必须立刻处理的事项", locale))
    title = _clean(subject) or t("每日简报 {date}", locale, date=date_label).strip()
    top = digest["tasks"][:3]
    if top:
        rows = "".join(
            f'<div style="padding:8px 0;border-bottom:1px solid #e6eef5">'
            f'<div style="font-size:14px;line-height:1.6;color:#ffffff">{_inline(task["action"])}</div>'
            f'<div style="font-size:12px;color:#cfe3f2;margin-top:3px">'
            f'{t("{when} · 来自「{subject}」", locale, when=html.escape(task["deadline"] or t("无明确截止时间", locale)), subject=html.escape(task["subject"][:60]))}</div>'
            '</div>'
            for task in top
        )
        now_box = (
            '<div style="background:#123b63;border-radius:10px;padding:16px;margin:6px 0">'
            f'<div style="font-size:12px;color:#bcd7ea">{t("现在就要处理 / Do this first", locale)}</div>'
            f'<div style="font-size:16px;font-weight:700;color:#ffffff;margin:6px 0">{_inline(headline)}</div>'
            + rows + '</div>'
        )
    else:
        now_box = _callout(t("现在就要处理 / Do this first", locale),
                           t("{headline}。下一封新邮件到达时会自动生成即时摘要。", locale,
                             headline=headline),
                           background="#eef8f2", border="#cbe8d9", color="#0a5c42")

    synthesis = str(digest.get("synthesis") or "").strip()
    body = (
        '<tr><td style="padding:18px 22px 0">'
        + now_box
        + (f'<div style="margin-top:12px">{_callout(t(SYNTHESIS_HEADING, locale), synthesis, background="#fbf7ee", border="#eadfc6", color="#4a3c1e")}</div>'
           if synthesis else "")
        + '<div style="margin-top:12px">' + _digest_metric_html(digest, locale=locale) + '</div>'
        # 「稍后提醒」：一行事实，紧跟在数字表后面（纯文本那一半里它在第 8 节的清单里，
        # 两边说的是同一句 `snoozed_line`）。
        + (f'<div style="margin-top:12px">{_callout(t("稍后提醒 / Snoozed", locale), digest["snoozed_line"], background="#f5f3ff", border="#ddd6fe", color="#4c1d95")}</div>'
           if digest.get("snoozed_line") else "")
        + '</td></tr>'
    )
    body += _digest_section_html(t(CATEGORY_TITLES["failed"], locale), digest["sections"]["failed"],
                                 note=t("这些邮件没有成功生成摘要；报告不会丢弃它们，worker 会按退避策略重试。",
                                        locale),
                                 locale=locale)
    body += _digest_section_html(t(CATEGORY_TITLES["urgent"], locale), digest["sections"]["urgent"],
                                 locale=locale)
    body += _digest_section_html(t(CATEGORY_TITLES["academic"], locale), digest["sections"]["academic"],
                                 locale=locale)
    body += _digest_section_html(t(CATEGORY_TITLES["opportunity"], locale),
                                 digest["sections"]["opportunity"], locale=locale)
    body += _digest_section_html(t(CATEGORY_TITLES["administrative"], locale),
                                 digest["sections"]["administrative"], locale=locale)
    body += _digest_section_html(t(CATEGORY_TITLES["low"], locale), digest["sections"]["low"],
                                 note=t("营销或低价值邮件统一放在这里；它们没有被丢弃，仍可在报告中追溯。",
                                        locale),
                                 locale=locale)
    exceptions = [t("有 {n} 封邮件本次未取得可验证来源，已按邮件标注。", locale,
                    n=digest["metrics"]["without_sources"])
                  ] if digest["metrics"]["without_sources"] else []
    if digest["metrics"]["duplicates"]:
        exceptions.append(t("已合并 {n} 封同类重复邮件，数量仍计入总数。", locale,
                            n=digest["metrics"]["duplicates"]))
    if not digest["items"]:
        exceptions.append(t("今天没有收到需要处理的新邮件。", locale))
    body += _section(t("异常与整体说明 / Exceptions and notes", locale),
                     _bullets_html(exceptions, locale=locale)
                     or _paragraph_html(t("无异常。", locale), muted=True),
                     locale=locale)
    subtitle = t("{n} 封邮件 · {m} 件待办 · 生成于 {when}", locale,
                 n=digest["metrics"]["total"], m=digest["metrics"]["actionable"],
                 when=format_moment(digest.get("generated_at"), digest.get("timezone"), locale=locale))
    return _email_shell(title, subtitle, body,
                        footer=t("{disclaimer} 每日简报在 22:00 生成。", locale,
                                 disclaimer=t(CONTENT_DISCLAIMER, locale)),
                        locale=locale)


ANNOUNCEMENT_TONES = {
    "info": ("通知", "#123b63", "#eaf4fc"),
    "warn": ("提醒", "#8a6100", "#fff8e8"),
    "critical": ("重要", "#b42318", "#fff1f0"),
}


def announcement_subject(title: str) -> str:
    return f"【CityU Mail Pilot 公告】{_clean(title) or '来自管理员的通知'}"


def render_announcement_text(title: str, body: str, tone: str = "info",
                            has_image: bool = False) -> str:
    """Plain-text part of a broadcast.

    Deliberately plain: an announcement is read, not skimmed like a report, and
    a wall of formatted text in a personal inbox reads like marketing.

    ``tone`` 以前被忽略（永远写「通知」）：一条标着「重要」的广播，HTML 版说重要、
    纯文本版说通知——两半不一致。现在两半用同一个标签。

    ``has_image`` 只说**有**一张图，不试图描述它：纯文本客户端看不到图，装作没有
    更糟；而「有一张图」至少让人知道去网页版看什么。
    """
    label = ANNOUNCEMENT_TONES.get(tone, ANNOUNCEMENT_TONES["info"])[0]
    lines = [f"【{label}】{_clean(title)}", ""]
    for paragraph in str(body or "").splitlines():
        lines.append(paragraph.rstrip())
    if has_image:
        lines += ["", "（这条广播带一张图片，网页版里能看到。）"]
    lines += ["", "——", "这条消息由试点管理员发给所有试点用户。",
              "你也可以随时登录网页版查看：https://mycampusmail.com/"]
    return "\n".join(lines).strip() + "\n"


def render_announcement_html(title: str, body: str, tone: str = "info",
                            image_cid: str = "") -> str:
    """HTML part of a broadcast, under the same strict email rules as reports.

    One 600px table, inline CSS duplicated as attributes, no JS, no <style>, no
    @media, no remote images — the constraint set is documented in
    ``docs/email-html-compatibility-2026-09-13.md``.

    ``image_cid`` 是**内嵌**图片的 Content-ID（不是网址）。选内嵌而不是远程图片有
    两个理由：① 「无外部图片」是这个项目对邮件的硬约束——收件人客户端默认会拦远程
    图片，一张指向我们服务器的图在很多人那里就是一个空白框；② 内嵌的那份不依赖
    服务器可达，信躺在收件箱里半年后再打开也还在。代价是每封信大几百 KB，
    所以配图在上传时就被重编码到 2048px / 1.4MB 以内，而且**只有广播**才带图。
    """
    label, ink, background = ANNOUNCEMENT_TONES.get(tone, ANNOUNCEMENT_TONES["info"])
    safe_title = html.escape(_clean(title) or "来自管理员的通知")
    paragraphs = []
    for paragraph in str(body or "").split("\n"):
        text = html.escape(paragraph.strip())
        if text:
            paragraphs.append(
                f'<p style="margin:0 0 12px;font-size:15px;line-height:1.65;color:#243447">{text}</p>')
    return "".join([
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="background:#f4f7fb;padding:24px 12px"><tr><td align="center">',
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" '
        'style="width:600px;max-width:600px;background:#ffffff;border:1px solid #d9e2ec;'
        'table-layout:fixed;word-break:break-word">',
        f'<tr><td bgcolor="{ink}" style="background:{ink};padding:20px 28px;color:#ffffff">',
        f'<div style="font-size:11px;letter-spacing:.10em">CITYU MAIL PILOT · {label}</div>',
        f'<div style="font-size:20px;font-weight:700;margin-top:6px">{safe_title}</div></td></tr>',
        f'<tr><td bgcolor="{background}" style="background:{background};padding:22px 28px">',
        # 图片在文字**上面**：广播多半是一张通知/海报的图，先看图再看说明。
        # width 属性是给忽略 CSS 的客户端留的（内容是 600-28*2=544px 宽）；
        # alt 是给「图片被拦」的客户端留的 —— 那正是内嵌也拦不住的少数情况。
        (f'<img src="cid:{html.escape(image_cid, quote=True)}" width="544" alt="公告配图" '
         'style="display:block;width:100%;max-width:544px;height:auto;border:0;'
         'border-radius:8px;margin:0 0 16px">' if image_cid else ""),
        "".join(paragraphs),
        '</td></tr>',
        '<tr><td style="padding:16px 28px;border-top:1px solid #d9e2ec;font-size:12px;color:#64748b">',
        '这条消息由试点管理员发给所有试点用户。你也可以随时登录网页版查看。',
        '</td></tr></table></td></tr></table>',
    ])


def render_digest_text(digest: dict[str, Any], *, subject: str | None = None,
                       locale: str = DEFAULT_LOCALE) -> str:
    title = _clean(subject) or t("每日简报 {date}", locale, date=digest.get("date", "")).strip()
    out = [title, "=" * min(len(title), 60),
           t("收到邮件 {n} 封 · 需要行动 {m} 项 · 异常/失败 {k} 封", locale,
             n=digest['metrics']['total'], m=digest['metrics']['actionable'],
             k=digest['metrics']['failed']),
           t("最近截止时间：{when}", locale,
             when=digest['next_deadline'] or t("无明确截止时间", locale)), ""]
    out.append(t("【今天/明天必须处理什么】", locale))
    if digest["tasks"]:
        for index, task in enumerate(digest["tasks"][:8], 1):
            suffix = (t("（截止：{when}）", locale, when=task["deadline"])
                      if task["deadline"] else "")
            out.append(t("{index}. {action}{suffix} · 来自「{subject}」", locale, index=index,
                         action=task["action"], suffix=suffix, subject=task["subject"]))
    else:
        out.append(t("今天没有必须立刻处理的事项。", locale))
    synthesis = str(digest.get("synthesis") or "").strip()
    if synthesis:
        out.append("")
        out.append(f"【{t(SYNTHESIS_HEADING, locale)}】")
        out.extend(synthesis.splitlines())
    for name in CATEGORY_ORDER:
        entries = digest["sections"][name]
        out.append("")
        out.append(f"【{t(CATEGORY_TITLES[name], locale)}】")
        if not entries:
            out.append(t("- 无。", locale))
            continue
        for entry in entries:
            repeat = (t("（另有 {n} 封同类）", locale, n=entry["duplicates"])
                      if entry["duplicates"] else "")
            out.append(t("- {subject}{repeat} | 发件人：{who} | 收件：{when} | {level}", locale,
                         subject=entry['subject'], repeat=repeat,
                         who=entry['sender'] or t("未知", locale),
                         when=display_when(entry, locale=locale, timezone=entry.get('timezone')),
                         level=t(entry['priority_label'], locale)))
            out.append(f"  {entry['conclusion']}")
            for action in entry["actions"][:4]:
                deadline = deadline_of(action)
                out.append(f"  · {t('待办：{action}', locale, action=action)}"
                           + (t("（截止 {when}）", locale, when=deadline) if deadline else ""))
            if entry["status"] != "sent":
                out.append(t("  · ⚠ 状态：{status} {error}", locale, status=entry['status'],
                             error=entry['last_error'][:200]))
    out.append("")
    out.append(t("【异常与整体说明】", locale))
    # 同一句 `snoozed_line`，三个正文（markdown / HTML / 纯文本）说同一件事：
    # 只有一处算它，所以三份不可能各说各话。
    if digest.get("snoozed_line"):
        out.append(f"- {digest['snoozed_line']}")
    if digest["metrics"]["without_sources"]:
        out.append(t("- 有 {n} 封邮件本次未取得可验证来源，未伪造引用。", locale,
                     n=digest["metrics"]["without_sources"]))
    if digest["metrics"]["duplicates"]:
        out.append(t("- 已合并 {n} 封同类重复邮件，数量仍计入总数。", locale,
                     n=digest["metrics"]["duplicates"]))
    if not digest["items"]:
        out.append(t("- 今天没有收到需要处理的新邮件。", locale))
    out.append("")
    out.append(t(CONTENT_DISCLAIMER, locale))
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# browser preview (used only for responsive checks; never sent as email)
# --------------------------------------------------------------------------- #

PREVIEW_CSS = """
  :root{color-scheme:light}
  *{box-sizing:border-box}
  body{margin:0;background:#e9eef3;font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
  .frame{max-width:680px;margin:18px auto;background:#fff;border-radius:10px;overflow:hidden;
         box-shadow:0 10px 30px #0f274518}
  .frame > *{max-width:100%}
  .frame table{max-width:100%!important}
  img{max-width:100%;height:auto}
  .caption{margin:14px auto 6px;max-width:680px;font-size:13px;color:#475467;padding:0 12px}
  .stack{display:flex;flex-wrap:wrap;gap:18px;justify-content:center;padding:10px}
  .device{background:#cbd5e1;border-radius:18px;padding:8px}
  .device.mobile{width:390px}
  @media(max-width:520px){.device.mobile{width:100%}}
"""


# --------------------------------------------------------------------------- #
# entry helpers used by the delivery layer
# --------------------------------------------------------------------------- #

def render_immediate(markdown: str, message: dict[str, Any], *, subject: str,
                     timezone: str | None = None,
                     locale: str = DEFAULT_LOCALE) -> dict[str, str]:
    parsed = parse_report(markdown, message=message, timezone=timezone, kind="immediate")
    return {
        "subject": subject,
        "html": render_immediate_html(parsed, subject=subject, locale=locale),
        "text": render_immediate_text(parsed, subject=subject, locale=locale),
        "priority": parsed["priority"],
        "deadline": parsed["deadline"],
    }


def render_digest(digest: dict[str, Any], *, subject: str,
                  locale: str = DEFAULT_LOCALE) -> dict[str, str]:
    return {
        "subject": subject,
        "html": render_digest_html(digest, subject=subject, locale=locale),
        "text": render_digest_text(digest, subject=subject, locale=locale),
    }


def digest_subject(digest: dict[str, Any], *, locale: str = DEFAULT_LOCALE) -> str:
    """A concrete, scannable subject line for the 22:00 brief."""
    date_label = digest.get("date") or ""
    metrics = digest.get("metrics", {})
    parts = [t("【CityU 每日简报】{date}", locale, date=date_label).strip()]
    if metrics.get("total"):
        parts.append(t("{n} 封邮件", locale, n=metrics["total"]))
    if metrics.get("actionable"):
        parts.append(t("{n} 件待办", locale, n=metrics["actionable"]))
    if metrics.get("failed"):
        parts.append(t("{n} 封失败", locale, n=metrics["failed"]))
    return " · ".join(parts)


def preview_document(blocks: Sequence[tuple[str, str]]) -> str:
    parts = [
        '<!doctype html><html lang="zh-Hans"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        '<title>CityU Mail Pilot — report preview</title>',
        f"<style>{PREVIEW_CSS}</style></head><body>",
    ]
    for caption, markup in blocks:
        parts.append(f'<div class="caption">{html.escape(caption)}</div>')
        parts.append(f'<div class="frame">{markup}</div>')
    parts.append("</body></html>")
    return "".join(parts)

BRIEF_TRAILER = (
    "这是精简即时摘要。完整的中英双语报告（含邮件内容总结、与你的相关性、"
    "联网核实来源、风险与 AI 推测标注、English summary）稍后单独发送。"
)
#: 站上关掉完整版（`INFE_PILOT_FULL_REPORT=0`）时用这句。
#
# 2026-09-23 实测发现：上面那句是**无条件打印**的，而站上开的正是「只发精简版」，
# 于是每一封精简报告都在承诺一封永远不会到的邮件 —— **对用户说了假话**，也让那句
# 「稍后单独发送」变成永远等不到的东西。承诺与否取决于配置，所以要由调用方告诉
# 渲染层「完整版还会不会来」（`full_follows`），不能写死在模板里。
BRIEF_TRAILER_ONLY = (
    "这是精简即时摘要。本站当前只发送这一份，不会再发完整版；"
    "需要看原文可以在 App 里点开这封邮件。"
)


def brief_trailer(*, full_follows: bool) -> str:
    return BRIEF_TRAILER if full_follows else BRIEF_TRAILER_ONLY


#: 抽取器只认**字面量实参**（`t("…")`）。下面这些文案在源码里从来不是 `t()` 的直接实参
#: —— 它们是模块常量、或按优先级/分类**查表**取出来的 —— 所以必须在这里原样登记一遍。
#:
#: **为什么不用 `mark(CONTENT_DISCLAIMER)` 那种写法**：那也过不了抽取器（实参不是字面量），
#: 2026-09-23 试过，结果是「覆盖率报 0 缺、而每封邮件的免责声明、优先级徽章、精简版尾句
#: 仍是中文」。重复一遍原文是这里唯一的办法，代价是常量改了这里会漂 ——
#: `test_report_language` 有一条断言把这张表与常量逐个对齐，漂了当场红。
#: `mark()` 什么都不做，只负责被看见。
_REGISTERED_EMAIL_LABELS = (
    mark("AI 生成内容可能出错；邮件事实、联网来源与推测已在报告中分开标注。"),
    mark("一段综览（模型写的，仅供参考）"),
    mark("这是精简即时摘要。完整的中英双语报告（含邮件内容总结、与你的相关性、"
         "联网核实来源、风险与 AI 推测标注、English summary）稍后单独发送。"),
    mark("这是精简即时摘要。本站当前只发送这一份，不会再发完整版；"
         "需要看原文可以在 App 里点开这封邮件。"),
    mark("重要 · 需要尽快处理"),
    mark("一般 · 建议今天看"),
    mark("低优先级 · 可以稍后"),
    mark("未能判定 · 请自行判断"),
    mark("紧急待办 / Urgent"),
    mark("学业相关 / Academic"),
    mark("机会与活动 / Opportunities"),
    mark("行政通知 / Administrative"),
    mark("低优先级与营销 / Low priority"),
    mark("处理失败 · 需要关注 / Failed"),
)


def is_brief(markdown: str) -> bool:
    """True when this report only carries the condensed three sections."""
    text = _clean(markdown)
    if "## 7." in text or "English summary" in text or "Personal relevance" in text \
            or "与我的学业" in text:
        return False
    return "## 3. 邮件内容要点" in text or "Key points" in text


def render_brief_html(markdown: str, message: dict[str, Any], *, subject: str,
                      timezone: str | None = None, full_follows: bool = True,
                      locale: str = DEFAULT_LOCALE) -> str:
    """Compact email for the condensed report: essentials only, no filler."""
    parsed = parse_report(markdown, message=message, timezone=timezone, kind="brief")
    title = _clean(subject) or parsed["subject"] or t("邮件摘要", locale)
    priority_badge = _badge(t(parsed["priority_label"], locale), parsed["priority"])
    deadline = deadline_note(parsed["actions"][0]) if parsed["actions"] else ""
    rows = (
        '<tr><td style="padding:16px 20px 0">'
        f'<div>{priority_badge}</div>'
        f'<div style="font-size:13px;color:#475467;margin-top:10px">{html.escape(title)}</div>'
        + _meta_row(parsed, locale=locale) +
        '</td></tr>'
        '<tr><td style="padding:12px 20px 0">'
        + _callout(t("一句话结论 / In one line", locale), parsed["conclusion"]) +
        '</td></tr>'
        '<tr><td style="padding:12px 20px 0">'
        + _callout(
            t("你要做什么 / What to do", locale),
            ("" if not parsed["actions"] else ""),
            background="#fff8e8", border="#f0d9a8", color="#8a6100",
        )
        + _bullets_html(parsed["actions"], ordered=True, show_deadline=True, locale=locale)
        + (f'<div style="font-size:12px;color:#8a6100;margin-top:4px">'
           f'{t("截止：{when}", locale, when=html.escape(deadline))}</div>'
           if deadline else "")
        + '</td></tr>'
        + _section(t("邮件内容要点 / Key points", locale),
                   _bullets_html(_bullets(parsed["summary"]), locale=locale)
                   or _paragraph_html(parsed["summary"], muted=True),
                   locale=locale)
        + '<tr><td style="padding:14px 20px 0">'
        + _paragraph_html(t(brief_trailer(full_follows=full_follows), locale), size=12, muted=True)
        + '</td></tr>'
    )
    subtitle = t("精简即时摘要 · {when}", locale, when=display_when(parsed, locale=locale))
    return _email_shell(title, subtitle, rows, footer=t(CONTENT_DISCLAIMER, locale), locale=locale)


def render_brief_text(markdown: str, message: dict[str, Any], *, subject: str,
                      timezone: str | None = None, full_follows: bool = True,
                      locale: str = DEFAULT_LOCALE) -> str:
    parsed = parse_report(markdown, message=message, timezone=timezone, kind="brief")
    title = _clean(subject) or parsed["subject"] or t("邮件摘要", locale)
    out = [title, "=" * min(len(title), 60),
           t("重要程度：{level}", locale, level=t(parsed["priority_label"], locale)),
           t("结论：{conclusion}", locale, conclusion=parsed["conclusion"]),
           t("发件人：{who}", locale,
             who=parsed["sender_name"] or parsed["sender_address"] or t("未知", locale)),
           t("收件时间：{when}", locale, when=parsed["received_display"]), "",
           t("【你要做什么】", locale)]
    if parsed["actions"]:
        for index, action in enumerate(parsed["actions"], 1):
            note = deadline_note(action)
            out.append(f"{index}. {action}"
                       + (t("（截止：{when}）", locale, when=note) if note else ""))
    else:
        out.append(t("无需行动。", locale))
    out += ["", t("【邮件内容要点】", locale)]
    out.extend(f"- {item}" for item in (_paragraphs(parsed["summary"]) or [t("未提供。", locale)]))
    out += ["", t(brief_trailer(full_follows=full_follows), locale), "",
            t(CONTENT_DISCLAIMER, locale)]
    return "\n".join(out)


def render_brief(markdown: str, message: dict[str, Any], *, subject: str,
                 timezone: str | None = None, full_follows: bool = True,
                 locale: str = DEFAULT_LOCALE) -> dict[str, str]:
    return {
        "subject": subject,
        "html": render_brief_html(markdown, message, subject=subject, timezone=timezone,
                                  full_follows=full_follows, locale=locale),
        "text": render_brief_text(markdown, message, subject=subject, timezone=timezone,
                                  full_follows=full_follows, locale=locale),
    }
