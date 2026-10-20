"""每个账号自己的收信排除名单：哪些信不用处理。

用户要的是设计稿里那一句「**可以在设置里按发件人或关键词排除**」（B1）。这个模块只做
**判定**：把那份名单解析成规则、再拿一封邮件的元数据去比。落到产品里的接线（存哪一列、
在哪一步调用、界面上怎么编辑）在接线那一步做，不在这里。

## 三条不许含糊的边界

1. **只用发件地址与主题，绝不用正文。** 两样都是报头，在「是不是本校来信」那道判断之前
   就拿得到；而正文在发件人判断之前**根本不入库**（隐私政策写的「非本校来信只留元数据」
   就是这条）。为了筛掉一封信去解密正文，等于把那条承诺反过来做。
   **代价说清楚**：关键词只能匹配**主题**，匹配不到正文里的字——这是有意的，不是缺陷。
2. **排除 ≠ 静默丢弃。** 命中的信照旧记一行 `skipped`、带上原因，照旧出现在日报的
   「已跳过」里。用户自己写的规则也要看得见结果，否则「它是不是坏了」无从回答。
3. **规则要能一眼看懂、写错了要当场说。** 一行一条，`发件人:` / `关键词:` 前缀，
   也接受裸的地址或域名；上限 50 条、每条 120 字；空规则、超长、认不出的前缀一律
   **报错并说清是哪一行**——一份「以为生效了其实被忽略」的名单比没有名单更糟。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

#: 一份名单最多几条、每条多长。上限存在的理由是它每次轮询都要比一遍：
#: 一个几百条的名单会让每封信都多花几毫秒，而我们跑在 2 核的机器上。
MAX_RULES = 50
MAX_PATTERN = 120
#: 关键词至少两个字。一个字的「的」「a」会把所有信都排掉，那不是用户想要的。
MIN_KEYWORD = 2

KIND_SENDER = "sender"
KIND_KEYWORD = "keyword"

#: `发件人:` / `关键词:` 的中英文写法都收（界面上也会这么提示）。
_PREFIXES = {
    "发件人": KIND_SENDER, "发件": KIND_SENDER, "sender": KIND_SENDER, "from": KIND_SENDER,
    "关键词": KIND_KEYWORD, "关键字": KIND_KEYWORD, "keyword": KIND_KEYWORD, "subject": KIND_KEYWORD,
}
#: 只认「像域名」的形状：有 @、有点、没有空格。裸的地址/域名也当发件人规则。
_DOMAINISH = re.compile(r"^@?[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


class RuleError(ValueError):
    """名单里有一条写错了。报文是**给人看的**（会出现在设置页那一栏下面）。"""


def parse_rules(text: str) -> list[dict[str, str]]:
    """把用户写的那段文字解析成规则。写错就抛 :class:`RuleError`，报文点到具体哪一行。"""
    rules: list[dict[str, str]] = []
    for number, raw in enumerate(str(text or "").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        kind = ""
        pattern = line
        # 只认**认识的前缀**：`发件人:` / `关键词:` / `sender:` 这些。
        # 认不出就把整行当规则——`Re: 期中` 这类主题里本来就有冒号，
        # 拿「有冒号」当语法会把一条正常的排除规则读成格式错误。
        for sep in (":", "："):
            if sep in line:
                head, tail = line.split(sep, 1)
                mapped = _PREFIXES.get(head.strip().lower())
                if mapped:
                    kind, pattern = mapped, tail.strip()
                break
        if not pattern:
            raise RuleError(f"第 {number} 行只有前缀、没有内容。")
        if len(pattern) > MAX_PATTERN:
            raise RuleError(f"第 {number} 行太长了（上限 {MAX_PATTERN} 字）。")
        if not kind:
            # 没有前缀时自己判断：像地址/域名的当发件人，其余当主题关键词。
            kind = KIND_SENDER if ("@" in pattern or _DOMAINISH.match(pattern)) else KIND_KEYWORD
        if kind == KIND_KEYWORD and len(pattern) < MIN_KEYWORD:
            raise RuleError(f"第 {number} 行的关键词太短（至少 {MIN_KEYWORD} 个字）。")
        rules.append({"kind": kind, "pattern": pattern.casefold()})
        if len(rules) > MAX_RULES:
            raise RuleError(f"名单最多 {MAX_RULES} 条。")
    return rules


def describe(text: str) -> str:
    """给人看的一句话（设置页那一栏下面用）。写错了就把错误原样说出来。"""
    try:
        rules = parse_rules(text)
    except RuleError as exc:
        return f"这份名单还不能用：{exc}"
    if not rules:
        return "还没有排除规则：所有来信都会照常处理。"
    senders = sum(1 for rule in rules if rule["kind"] == KIND_SENDER)
    keywords = len(rules) - senders
    return f"已生效 {len(rules)} 条：发件人 {senders} 条 · 主题关键词 {keywords} 条。"


def matches(message: dict[str, Any], rules: Iterable[dict[str, str]]) -> str:
    """这封信是否命中排除规则；命中返回**给用户看的原因**，没命中返回空串。

    只看 ``sender_address`` 与 ``subject`` 两个字段（都是报头）。正文一个字都不看——
    见模块开头第一条边界。
    """
    sender = str((message or {}).get("sender_address") or "").strip().casefold()
    subject = str((message or {}).get("subject") or "").casefold()
    for rule in rules or []:
        pattern = str(rule.get("pattern") or "")
        if not pattern:
            continue
        if rule.get("kind") == KIND_SENDER:
            if not sender:
                continue
            bare = pattern.lstrip("@")
            # 该地址本身 / 该域名下的一切 / 域名后缀（`qq.com` 也命中 `mail.qq.com`）。
            if sender == bare or sender.endswith("@" + bare) or sender.endswith("." + bare):
                return f"你排除了发件人「{bare}」"
        else:
            if subject and pattern in subject:
                return f"你排除了主题里含「{pattern}」的邮件"
    return ""
