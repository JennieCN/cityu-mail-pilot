"""Deterministic mail triage — no model call, so it is instant.

Pattern source (MIT, design only, no code copied):
``danieleschmidt/crewai-email-triage`` — a three-stage pipeline of
classify → summarise → rank built purely from keyword, sender-reputation and
recency rules, explicitly so that triage needs no LLM. Adopted ideas:

* classify by ordered rule cascade (spam/low-value → action → urgent → default),
  because ordering is what keeps a marketing mail out of "urgent";
* rank with a small additive score (category base + recency + keyword intensity
  + sender authority) instead of a second model call;
* keep it explainable: every decision returns human-readable reasons.

Adapted to this project: CityU student mail, Chinese + English keywords, the
existing dashboard categories, and a subject-alert format that does not depend
on the seven-section report. Kept as pure stdlib.
"""

from __future__ import annotations

import re
from typing import Any

# --- category vocabulary ----------------------------------------------------

URGENT = "urgent"
ACADEMIC = "academic"
OPPORTUNITY = "opportunity"
ADMINISTRATIVE = "administrative"
LOW = "low"

CATEGORY_LABELS = {
    URGENT: "紧急 / Urgent",
    ACADEMIC: "学业相关 / Academic",
    OPPORTUNITY: "机会与活动 / Opportunity",
    ADMINISTRATIVE: "行政通知 / Administrative",
    LOW: "低优先级 / Low priority",
}

# Ordered cascade: the first group that matches decides the category.
SPAM_OR_LOW_KEYWORDS = (
    "unsubscribe", "退订", "newsletter", "订阅", "promotion", "促销", "优惠", "折扣",
    "discount", "% off", "coupon", "优惠券", "限时", "sale", "marketing", "推广",
    "会员", "返现", "免费领取", "抽奖", "中奖",
)
SAFETY_KEYWORDS = (
    "fraud", "scam", "phishing", "caution", "warning", "security alert", "safety",
    "防诈", "诈骗", "钓鱼", "安全提示", "警示", "警惕",
)
URGENT_KEYWORDS = (
    "urgent", "asap", "immediately", "critical", "deadline today", "expires today",
    "last call", "action required", "final reminder", "overdue",
    "缴费", "付款", "到期",
    "紧急", "尽快", "立即", "今天截止", "今日截止", "最后提醒", "逾期", "务必",
)
ACADEMIC_KEYWORDS = (
    "assignment", "course", "lecture", "tutorial", "exam", "quiz", "grade", "gpa",
    "semester", "registration", "supervisor", "thesis", "credits", "canvas",
    "作业", "课程", "讲座", "辅导", "考试", "测验", "成绩", "学分", "选课", "论文", "导师",
)
OPPORTUNITY_KEYWORDS = (
    "internship", "career", "recruit", "job", "competition", "hackathon",
    "scholarship", "workshop", "seminar", "webinar", "event", "career centre",
    "实习", "招聘", "就业", "比赛", "竞赛", "奖学金", "工作坊", "讲座活动", "宣讲",
)
ADMIN_KEYWORDS = (
    "tuition", "fee", "payment", "housing", "library", "portal", "maintenance",
    "notice", "reminder", "visa", "student card", "insurance", "enrolment",
    "announcement", "posting", "digest", "caution", "fraud", "scam", "safety",
    "公告", "通告", "防诈", "诈骗", "安全提示",
    "缴费", "学费", "宿舍", "图书馆", "系统维护", "通知", "提醒", "签证", "学生证", "注册",
)
ACTION_KEYWORDS = (
    "please review", "please confirm", "please respond", "please submit",
    "your approval", "sign off", "need your", "awaiting your", "reply",
    "register", "apply", "submit", "rsvp", "due", "deadline", "assignment",
    "homework", "expires", "expiring", "complete", "action required",
    "报名", "申请", "提交", "回复", "确认",
    "请提交", "请确认", "请回复", "请填写", "请报名", "请完成", "报名截止", "提交截止",
    "截止", "请尽快", "务必", "需要你", "需要您",
)
# Sender patterns that raise urgency/seriousness without any model.
AUTHORITY_SENDER_PATTERNS = (
    r"(^|[.@_-])(registrar|examination|exams|registry|academic|department|faculty|dean|"
    r"advising|finance|bursar|housing|library|security|ithelp|helpdesk)@",
    r"@(cityu\.edu\.hk|my\.cityu\.edu\.hk)$",
)

_TIME_SIGNALS = ("today", "tomorrow", "tonight", "this week", "next week",
                 "today", "by friday", "by monday", "by tuesday", "by wednesday",
                 "by thursday", "by saturday", "by sunday",
                 "今天", "明天", "本周", "这周", "下周", "周一", "周二", "周三", "周四",
                 "周五", "周六", "周日", "星期")


def _text(message: dict[str, Any]) -> str:
    subject = str(message.get("subject") or "")
    body = str(message.get("body") or "")
    return f"{subject} {body}".lower()


def _sender(message: dict[str, Any]) -> str:
    return str(message.get("sender_address") or "").lower()


def sender_is_authority(address: str) -> bool:
    lowered = str(address or "").lower()
    return any(re.search(pattern, lowered) for pattern in AUTHORITY_SENDER_PATTERNS)


def classify(message: dict[str, Any]) -> tuple[str, list[str]]:
    """Return (category, reasons) using base signal + modifiers.

    Deliberately NOT a first-match cascade. A cascade mis-classified two real
    mails: a legitimate CityU announcement dropped to "low" because its footer
    says "unsubscribe", and a lecture schedule was raised to "urgent" because
    it mentioned a weekday. So each family of signals votes, the strongest
    structural signal sets the base, and marketing flourishes only demote when
    nothing structural is present.
    """
    text = _text(message)
    sender = _sender(message)
    reasons: list[str] = []

    low_hits = [word for word in SPAM_OR_LOW_KEYWORDS if word in text]
    urgent_hits = [word for word in URGENT_KEYWORDS if word in text]
    action_hits = [word for word in ACTION_KEYWORDS if word in text]
    academic_hits = [word for word in ACADEMIC_KEYWORDS if word in text]
    opportunity_hits = [word for word in OPPORTUNITY_KEYWORDS if word in text]
    admin_hits = [word for word in ADMIN_KEYWORDS if word in text]
    authority = sender_is_authority(sender)

    # Base: the strongest structural subject-matter signal wins.
    safety_hits = [word for word in SAFETY_KEYWORDS if word in text]
    ranked = [
        (len(urgent_hits) * 3 + (1 if action_hits else 0), URGENT),
        (len(safety_hits) * 3, ADMINISTRATIVE),   # 防诈/安全公告优先于"招聘"等词
        (len(academic_hits) * 2, ACADEMIC),
        (len(admin_hits) * 2 + (1 if authority else 0), ADMINISTRATIVE),
        (len(opportunity_hits) * 2, OPPORTUNITY),
    ]
    ranked.sort(key=lambda item: item[0], reverse=True)
    best_score, category = ranked[0]
    if best_score:
        label = {"urgent": "紧急", "academic": "学业", "administrative": "行政", "opportunity": "机会/活动"}[category]
        hits = urgent_hits or academic_hits or admin_hits or opportunity_hits
        reasons.append(f"{label}关键词：" + "、".join(hits[:3]))
    else:
        category = LOW
        reasons.append("没有可识别的结构信号")

    # A real deadline needs an action signal plus a time reference, or an
    # explicit urgency word: "辅导课自下周开始" is a schedule, while
    # "周五 23:59 前提交" is a deadline.
    has_deadline = bool(action_hits) and any(signal in text for signal in _TIME_SIGNALS)
    if has_deadline:
        reasons.append("需要动作且含时间要求：" + "、".join(action_hits[:2]))

    # Marketing flourishes only demote mail that has no substance at all.
    if low_hits and category == LOW:
        reasons.append("低价值/营销信号：" + "、".join(low_hits[:3]))
    elif low_hits:
        reasons.append("（含 unsubscribe/推广字样，但有实质内容，故不降级）")

    if authority and category in {LOW, OPPORTUNITY}:
        category = ADMINISTRATIVE
        reasons.append("来自学校官方地址，提升为行政通知")

    return category, reasons


def urgent_flag(message: dict[str, Any]) -> tuple[bool, list[str]]:
    """Whether this mail should alert now, independent of its category.

    Kept separate from ``classify`` so the daily digest still groups by subject
    matter while the instant alert can still fire for "assignment due Friday".
    """
    text = _text(message)
    urgent_hits = [word for word in URGENT_KEYWORDS if word in text]
    action_hits = [word for word in ACTION_KEYWORDS if word in text]
    reasons: list[str] = []
    if urgent_hits:
        reasons.append("紧急信号：" + "、".join(urgent_hits[:3]))
        return True, reasons
    if action_hits and any(signal in text for signal in _TIME_SIGNALS):
        reasons.append("需要动作且含时间要求：" + "、".join(action_hits[:2]))
        return True, reasons
    return False, reasons


    if authority and category in {LOW, OPPORTUNITY}:
        category = ADMINISTRATIVE
        reasons.append("来自学校官方地址，提升为行政通知")

    return category, reasons


_CATEGORY_BASE = {URGENT: 9.0, ACADEMIC: 6.0, ADMINISTRATIVE: 5.0, OPPORTUNITY: 4.0, LOW: 1.0}


def score(message: dict[str, Any], category: str) -> float:
    """Additive 0–10 urgency hint: category base + signals. Explanatory only."""
    if category == LOW:
        return round(_CATEGORY_BASE[LOW], 1)
    text = _text(message)
    bonus = 0.0
    bonus += min(len([w for w in URGENT_KEYWORDS if w in text]) * 0.3, 1.2)
    if any(signal in text for signal in _TIME_SIGNALS):
        bonus += 0.5
    if sender_is_authority(_sender(message)):
        bonus += 0.5
    subject = str(message.get("subject") or "")
    if subject.isupper() or subject.count("!") >= 2:
        bonus += 0.3
    return round(min(_CATEGORY_BASE.get(category, 3.0) + bonus, 10.0), 1)


_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;.])\s*")


def sentences(text: str, *, minimum: int = 12, limit: int = 6) -> list[str]:
    found = []
    for raw in _SENTENCE_SPLIT.split(str(text or "").replace("\r", "")):
        clean = re.sub(r"\s+", " ", raw).strip(" \t-*•")
        if len(clean) >= minimum:
            found.append(clean)
        if len(found) >= limit:
            break
    return found


def summarize(message: dict[str, Any], *, limit: int = 160) -> str:
    """A one-line, model-free gist: the most signal-dense sentence we can find."""
    candidates = sentences(str(message.get("body") or ""))
    if not candidates:
        return str(message.get("subject") or "").strip()[:limit]
    subject_words = {word for word in re.split(r"\W+", str(message.get("subject") or "").lower()) if len(word) > 3}
    signals = ("请", "截止", "务必", "需要", "报名", "提交", "please", "deadline", "submit",
               "register", "due", "action", "required")

    def rank(sentence: str) -> float:
        lowered = sentence.lower()
        value = 0.0
        value += 0.5 * sum(1 for word in subject_words if word in lowered)
        value += 0.4 * sum(1 for word in signals if word in lowered)
        length = len(sentence)
        if 15 <= length <= 90:
            value += 0.3
        return value

    best = max(candidates, key=rank)
    best = re.sub(r"\s+", " ", best).strip()
    return best[:limit] + ("…" if len(best) > limit else "")


def triage(message: dict[str, Any]) -> dict[str, Any]:
    """One call for the alert path: category, urgency, gist and reasons."""
    category, reasons = classify(message)
    urgent, urgent_reasons = urgent_flag(message)
    effective = URGENT if urgent else category
    return {
        "category": category,
        "category_label": CATEGORY_LABELS.get(category, category),
        "urgent": urgent,
        "effective_category": effective,
        "effective_label": CATEGORY_LABELS.get(effective, effective),
        "urgency": score(message, effective),
        "gist": summarize(message),
        "reasons": reasons + [item for item in urgent_reasons if item not in reasons],
        "authority_sender": sender_is_authority(_sender(message)),
    }


def prompt_hint(message: dict[str, Any]) -> str:
    """A compact hint block so the model does not have to rediscover this.

    Passing the deterministic verdict in saves the model from re-deriving the
    category from scratch, which is exactly the kind of pre-work that inflates
    a reasoning model's hidden token budget.
    """
    verdict = triage(message)
    lines = [
        "<TRIAGE_HINT>",
        "这是规则引擎给出的初步判断（不是事实来源，若与邮件内容冲突，以邮件为准）：",
        f"初步类别：{verdict['category_label']}",
        f"初步紧急度：{verdict['urgency']}/10（依据：{'；'.join(verdict['reasons'])}）",
        f"初步摘要：{verdict['gist']}",
        "请据此判断重要程度，但不要照抄这句话。",
        "</TRIAGE_HINT>",
    ]
    return "\n".join(lines)
