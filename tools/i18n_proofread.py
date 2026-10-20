#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""词典体检：把**机器能判的**那些错挑出来，剩下的交给人读。

为什么要有它：翻译最容易烂的方式不是「没翻」，而是**翻了但结构坏了**——
译文里丢了 `<code>`、少了一个 `</b>`、把 `{count}` 漏掉、或者整条没翻（还是中文）。
这些在页面上表现为「那一段突然变成中文」「标签从中间断掉」「数字不见了」，
而**任何一条都不会让测试变红**——因为 `test_i18n` 判的是覆盖率（缺没缺），不是内容对不对。

判据（每条都能说出「为什么这是错的」，不是风格偏好）：

* `tokens`  ：HTML 标签、`{}` 占位符、链接地址必须与原文**逐一同形**（数量+顺序）。
* `urls`    ：译文里的 URL 集合必须与原文一致。
* `cjk`     ：英文译文里不该出现汉字（专有名词白名单除外）。
* `hant`    ：繁体译文里不该出现**只有简体才有的字**（"这/个/为/发/说/时"…）。
* `same`    ：译文与原文逐字相同（专有名词、日期、纯数字白名单除外）。
* `ratio`   ：长度比例超出**本词典实测带**（上限按语言分开，见 `RATIO_MAX`）——多半是
  整条塞了别的段落。**只对原文长于 12 字的键生效**：`留言`→`Leave a message` 这种
  短键的比例天然可以到 8 倍，拿比例去判它只会制造假红。
* `glossary`：高频术语在同一语言里必须只有一种译法（表见下）。

用法::

    .venv-pilot/bin/python tools/i18n_proofread.py            # 报告
    .venv-pilot/bin/python tools/i18n_proofread.py --json x.json

只读，不改任何文件。
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
I18N = ROOT / "pilot_app" / "static" / "i18n"
LANGUAGES = ("en", "zh-Hant", "ja", "ko")

# ---------------------------------------------------------------- 结构 token
TAG = re.compile(r"</?[a-zA-Z][^>]*>")
PLACEHOLDER = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}|\{\{[A-Z_]+\}\}|%[sd]")
URL = re.compile(r'https?://[^\s"\'<>)]+|(?:href|src)="([^"]*)"')

#: 英文译文里允许出现的汉字（专有名词、署名、示例地址的域名部分）。
CJK_ALLOWED_IN_EN = ("余剑篪", "城大", "香港城市大學")

#: 只在简体里出现的字。**不是手打的**（手打一定会混进「把/作/找/才」这种两体同形的字，
#: 第一版就是这么错的）：这份表 = 我们自己的语料里出现过的汉字（1316 个）∩ OpenCC
#: 「简→繁会变」的那些（330 个）。生成方式见 `docs/i18n-2026-09-23.md` §词典体检；
#: 想重新生成：`python -c "import zhconv,json;…"`（一行的事，别抄错）。
SIMPLIFIED_ONLY = set(
    "与专业东丢两个临为么义习书争于产仅从仓们价优会传伪体余侧偿储儿关内册写决况准几凭"
    "击则刚创删别剑办务动区协单卖占却压参双发变台号后吗启员响团园围国图场坏块声处备复"
    "够头奖学宁实审对导将尔尝尽届属岁师带帮并广库应开异弃张弯归当录态总惯户托执扫扰护"
    "报担择损换据携摄数断无旧时昵显暂术机权条来构标栏样检楼槛欧残毕汇没泄测浏滚滞满滤"
    "点状独现琐电画监盖盘着码础确离种称窥笔筛签简类紧约级纪纯线练组细终经结绕给络绝统"
    "继绩续维综绿编缩缴网罚联脚节苹范获营补装见规视览觉触计认让训议记讲许论设访证识诉"
    "词译试诚话询该详语误说请诺读课谁调负责败账质贴费资赔赖跃踪转软轻载辖辞边达过运还"
    "这进远违连迟适选遗遥邮采里钟钥钮钱链销错键长门闭问间阅队阶际陈险随隐隶雇静页顶项"
    "顺须频题颜额风馆马验"
)

#: 「简繁同形、但 OpenCC 在某些词里会换掉」的字 —— 它们在繁体里**是正字**，
#: 出现在译文里不算漏网。第一版没有这张表，于是把「後台」「這台」「余劍篪」报成了简体字。
CONTEXTUAL_OK = set("台余里于干后只系表制面划冲准别卜谷采松")

#: 长度比例的**每语言**上限。只留上限，**不留下限** —— 这一版的键里有不少是
#: 「中文 / English」双语标题（报告语言那条链加的），译成英文会掉一半，拿同一个下限量
#: 会把它们全报成假红。上限抓的是「短键被译成一大段」这种塞私货。
#:
#: **这些数字是量出来的，不是拍的**（2026-09-23 实测 714 条、其中 428 条长于 12 字）：
#:
#: | 语言 | 中位数 | p95 | p99 | 最大 |
#: |---|---|---|---|---|
#: | en | 2.75 | 4.12 | 4.57 | 5.60（「本条款适用香港特别行政区法律。」，法条套话） |
#: | zh-Hant | 1.00 | 1.07 | 1.14 | 1.59 |
#: | ja | 1.48 | 2.00 | 2.36 | 2.80 |
#: | ko | 1.52 | 2.03 | 2.25 | 2.62 |
#:
#: 第一版对 ja/ko 用的是 2.0 —— 那**正好压在 p95 上**，于是每 20 行就报 1 行，
#: 40 行假红里没有一行是错的（日语要加假名与助词、韩语要加助词，中文一句话译过去
#: 天然长 1.5–2.8 倍）。上限取 **p99 的约 1.3 倍**：既在合法译文的自然带之外，
#: 又追得上「一句换一整段」这种真错（那会是 2–5 倍于自然带）。**今天这四项都是 0 行，
#: 这是校准的结果**：这条是**回归探测器**，不是给现有译文打分的。
RATIO_MAX = {"en": 6.0, "zh-Hant": 2.0, "ja": 3.2, "ko": 3.0}

#: 术语表：同一语言里只许一种译法（第一条是「应当采用的」）。
GLOSSARY = {
    "en": {
        "转发": ("forward",),
        "私人邮箱": ("personal inbox", "personal mail"),
        "授权码": ("app password",),
        "日报/简报": ("daily digest", "daily brief"),
        "待办": ("to-do", "task"),
        "已跳过": ("Skipped",),
    },
    "zh-Hant": {
        "轉寄/電郵": ("電郵",),
        "私隱": ("私隱",),
        "登入": ("登入",),
        "帳戶": ("帳戶",),
        "設定": ("設定",),
        "簡報/摘要": ("簡報",),
    },
}


def tokens(text: str) -> list[str]:
    return [t for t in TAG.findall(text)]


#: 日文里**本来就是这个词**的汉字（2026-09-23 逐条看过那 11 处候选，全是正字，没有一处是
#: 混进来的简体）。为什么按**词**而不是按**字**豁免：豁免一个 `与`，就等于从此看不见
#: 任何含「与」的简体串；豁免 `与える`/`付与`，混进来的「与」只要不在这些词里就照样报。
#: `即时` 那种真错（2026-09-23 抓到的唯一一处）不在这张表里，仍然报。
GLYPH_OK_WORDS = {
    "ja": ("延滞", "触れ", "占め", "旧", "提携", "名称", "雇用", "与え", "付与", "尽く", "紛争"),
}


def _is_real_word(lang: str, char: str, value: str) -> bool:
    """这个可疑字在译文里是不是落在**一个真正的日文词**里。"""
    return any(char in word and word in value for word in GLYPH_OK_WORDS.get(lang, ()))


def _same_is_correct(lang: str, key: str) -> bool:
    """译文与原文逐字相同，但是**本来就该相同**。

    两类，各有判据，都不写死清单：

    1. **繁体**：键里若没有任何「只有简体才有」的字，那繁体译文与原文逐字相同是**对的**
       —— OpenCC 也不会改它（`重要`/`截止 2026/9/24 23:59`/`如果出了事`）。判据与
       `hant`/`glyph` 那两条同源：`SIMPLIFIED_ONLY` 减 `CONTEXTUAL_OK`。
       词汇级差异（`郵箱`→`信箱`、`設置`→`設定`）由 `GLOSSARY` 那一条管，不靠这里。
    2. **日文/韩文里的短标签**：`重要`/`低` 这类汉字词在日语里就是同一个词（≤4 字）。
       句子级的整条没翻仍然会报 —— 那才是这条要抓的。
    """
    stripped = TAG.sub("", key).strip()
    if lang == "zh-Hant":
        return not any(c in SIMPLIFIED_ONLY and c not in CONTEXTUAL_OK for c in stripped)
    if lang in ("ja", "ko"):
        return len(stripped) <= 4
    return False


def placeholders(text: str) -> list[str]:
    return PLACEHOLDER.findall(text)


def urls(text: str) -> set[str]:
    """译文与原文里的链接必须指向同一个地方。

    只认两种：完整的 `http(s)://…`，以及 `href="…"` / `src="…"` 的属性值。
    **不要**拿 `/[a-z]…` 这种「看起来像路径」的正则去扫全文 —— 它会把 `</strong>`
    认成 `/strong`（第一版就是这么报了 6 处假红）。"""
    out: set[str] = set()
    for item in URL.findall(text):
        if isinstance(item, tuple):          # 带分组的那一支
            item = item[0]
        if item:
            out.add(item)
    return out


def check() -> dict:
    document = json.loads((I18N / "keys.json").read_text(encoding="utf-8"))
    required = [str(item) for item in document["keys"]]
    # **兜底碎片（`i18n.fallback_keys()`）也要一起体检。** 它们真的会出现在页面上
    # （整块没译时按碎片取——安装步骤那 8 句就是这么走的），而覆盖率那条链只把
    # `keys` 算作「必须翻译」、把碎片只算进「孤儿」的已知集合，所以**除了这里
    # 没有第二双眼睛看它们**。2026-09-23 之前这个工具只读 `keys`：
    # 722 条里有 8 条一条都没被检查过，而那 8 条恰好是重建词典时曾经丢过的那一批。
    keys = required + [k for k in (document.get("fallback") or []) if k not in required]
    char_counts: dict[str, dict[str, int]] = {}
    report: dict[str, list[str]] = {name: [] for name in
                                    ("tokens", "urls", "cjk", "hant", "glyph", "same", "ratio")}
    gloss: dict[str, dict[str, list[str]]] = {lang: {} for lang in LANGUAGES}
    for lang in LANGUAGES:
        table = json.loads((I18N / f"{lang}.json").read_text(encoding="utf-8"))
        counts: dict[str, int] = {}
        for value in table.values():
            for c in set(value):                 # 按「出现过的条目数」计数，不按次数
                counts[c] = counts.get(c, 0) + 1
        char_counts[lang] = counts
        for key in keys:
            value = table.get(key)
            if value is None or value == "":
                continue
            short = key.replace("\n", " ")[:46]
            if tokens(key) != tokens(value):
                report["tokens"].append(
                    f"[{lang}] {short}\n      原文标签 {tokens(key)}\n      译文标签 {tokens(value)}")
            if placeholders(key) != placeholders(value):
                report["tokens"].append(
                    f"[{lang}] {short}\n      占位符 {placeholders(key)} → {placeholders(value)}")
            if urls(key) != urls(value):
                report["urls"].append(f"[{lang}] {short}\n      {sorted(urls(key))} → {sorted(urls(value))}")
            if lang == "en":
                bad = [c for c in value if "\u4e00" <= c <= "\u9fff"]
                if bad and not any(a in value for a in CJK_ALLOWED_IN_EN):
                    report["cjk"].append(f"[en] {short} → 译文里有汉字：{''.join(bad)[:12]}｜{value[:60]}")
            if lang == "zh-Hant":
                bad = sorted({c for c in value if c in SIMPLIFIED_ONLY and c not in CONTEXTUAL_OK})
                if bad:
                    report["hant"].append(f"[zh-Hant] {short} → 简体字 {''.join(bad)}｜{value[:60]}")
            if lang in ("ja", "ko"):
                # **简体字混进日文/韩文**：抽取器只看「有没有译文」，所以「即时」这种
                # 简体字留在日文里，覆盖率照样报 0 缺 —— 这一条是宿舍机 2026-09-23
                # 独立复核时点出来的（它当场抓到一处 `即时`）。判据：日文用新字体、
                # 韩文基本不用汉字，所以**凡是 OpenCC 会改的简体字**出现在这两门里都可疑；
                # 再按「本词典里只出现一两次」收窄，避免把日文新字体（体/国/会…）误报。
                if any("\u4e00" <= c <= "\u9fff" for c in value):
                    # **只报「罕见」的那些**：日文新字体里本来就有大量与简体同形的字
                    # （学/国/会/体/当/数…），拿简体表去扫会把整份日文点成红。
                    # 判据用频次：在我们自己的译文里出现 ≤2 次的，才可能是混进来的简体字
                    # （2026-09-23 抓到的那处 `即时` 的「时」全篇只出现 1 次）。
                    counts = char_counts[lang]
                    rare = sorted({c for c in value
                                   if c in SIMPLIFIED_ONLY and c not in CONTEXTUAL_OK
                                   and counts.get(c, 0) <= 2
                                   and not _is_real_word(lang, c, value)})
                    if rare:
                        report["glyph"].append(
                            f"[{lang}] {short} → 译文里有简体字 {''.join(rare)}｜{value[:60]}")
            if value == key and len(key) > 8 and not re.fullmatch(r"[\d\s:/.\-年月日]+", key) \
                    and not _same_is_correct(lang, key):
                report["same"].append(f"[{lang}] {short}")
            if len(key) > 12 and len(value) > len(key) * RATIO_MAX[lang]:
                report["ratio"].append(
                    f"[{lang}] {short}｜原文 {len(key)} 字 → 译文 {len(value)} 字"
                    f"（{len(value) / len(key):.2f} 倍，上限 {RATIO_MAX[lang]}）｜{value[:60]}")
        for term, variants in GLOSSARY.get(lang, {}).items():
            used: dict[str, list[str]] = {}
            for key, value in table.items():
                for variant in re.findall(r"[A-Za-z\u4e00-\u9fff]+", value):
                    if variant in variants:
                        used.setdefault(variant, []).append(key[:26])
            if len(used) > 1:
                gloss[lang][term] = {v: ks[:3] for v, ks in used.items()}
    return {"report": report, "glossary": gloss, "keys": len(keys),
            "required": len(required)}


def main() -> int:
    data = check()
    labels = {
        "tokens": "结构 token 不一致（标签/占位符）——**页面上会断**",
        "urls": "链接地址与原文不一致",
        "cjk": "英文译文里出现汉字",
        "hant": "繁体译文里出现简体字",
        "glyph": "日文/韩文里的可疑字（**候选，不是判定的错**：日文新字体与简体同形的很多，人工看一眼；2026-09-23 就是这样抓到混进来的一处「即时」）",
        "same": "与原文逐字相同（可能没翻）",
        "ratio": "长度比例超出本词典实测带（疑似整条塞了别的段落）",
    }
    total = 0
    extra = data["keys"] - data["required"]
    print(f"词典体检：{data['required']} 条必译 + {extra} 条兜底碎片"
          f"（共 {data['keys']} 条）× {len(LANGUAGES)} 种语言\n")
    for name, label in labels.items():
        items = data["report"][name]
        total += len(items)
        print(f"── {label}：{len(items)} 处")
        for item in items[:40]:
            print("   " + item)
        if len(items) > 40:
            print(f"   …（还有 {len(items) - 40} 处）")
        print()
    if data["glossary"]:
        print("── 术语不一致：")
        for lang, terms in data["glossary"].items():
            for term, used in terms.items():
                print(f"   [{lang}] {term}: " + " / ".join(f"{v}×{len(ks)}" for v, ks in used.items()))
        print()
    print(f"合计 {total} 处。**硬错误只有「结构 token / 链接」两类**；"
          f"「可疑字（日韩）」是候选列表，其余按上下文判断。")
    print("「逐字相同」与「长度比例」两条 2026-09-23 已按本词典实测校准："
          "这两项**今天都是 0 行是正常值**，它们是回归探测器 —— 变了才说明新加的那条译文有问题。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
