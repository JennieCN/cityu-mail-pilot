"""多语言（i18n）：**中文原文当 key，其它语言是叠加层**。

为什么不是 ``nav.dashboard`` 这种短键
------------------------------------
短键要先把界面上每一句话从 HTML/JS 里抠出来换成 ``t('nav.dashboard')``，
于是「改一句中文」变成「改两处（键表 + 词典）」，而且**漏改一处界面上就变成
一个键名**——没人看得懂，测试也未必发现。这里的做法是 gettext 那一套：

* **中文原文就是 key**，界面上照旧写着中文，一个字都不用动；
* 中文没有词典（``catalog("zh-Hans")`` 是空表），所以**默认路径逐字节等于改造前**；
* 别的语言是一层覆盖：查得到就换，**查不到就原样留着中文**。

由此得到两条要紧的性质：

1. **可以分批铺**。这一轮只覆盖公开网页，剩下的界面继续是中文，不会半中半英
   报错，也不会因为词典缺条就渲染出 ``nav.dashboard``。
2. **现有测试不会因为这次改造变红**。两千多条测试里大量断言直接盯着中文原文
   （``assertIn("名额已满", body)``），原文留在代码里，它们就还是成立。

缺译是**看得见**的：``coverage()`` 会说出某个语言译了多少条，测试里有一条棘轮
盯着已校对的语言必须是 100%。宁可显示中文，也不显示一个猜的英文。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

APP_ROOT = Path(__file__).resolve().parent
I18N_ROOT = APP_ROOT / "static" / "i18n"
LOCALES_FILE = I18N_ROOT / "locales.json"
KEYS_FILE = I18N_ROOT / "keys.json"

#: 中文（简体）是**原文语言**，也是没有匹配时的兜底。它没有词典文件：查不到就是
#: 空表，于是改写器一个字都不换——这条路径必须与改造前逐字节一致。
DEFAULT_LOCALE = "zh-Hans"

#: 语言选择的三个入口，优先级见 :func:`negotiate`。
LANG_PARAM = "lang"
LANG_COOKIE = "cityu_mail_lang"

#: 这些属性里的文字也要跟着界面语言走。``placeholder``/``aria-label``/``alt`` 是
#: 无障碍与表单提示，漏掉它们，英文用户会看到一个中文占位符。
TRANSLATABLE_ATTRS = ("placeholder", "aria-label", "alt", "title")

#: ``<meta>`` 的 ``content`` 只在**这几种名字**下才翻：一个是搜索引擎摘要，
#: 其余是分享到微信 / Twitter 时的卡片文字。别的 meta content（``viewport``、
#: ``og:url``、``og:type``、``http-equiv``）是机器读的值，翻一个字就是坏掉。
#:
#: 注意下面第二行才是**要翻的属性名**。第一版把 ``description`` 当成属性名写进
#: 白名单，结果一条 meta 都匹配不上——``name="description"`` 里的 ``name`` 和
#: ``content`` 都不在白名单里。页面上还看不见：英文版的搜索摘要与分享卡片一直是
#: 中文，是译者回报「分享摘要还是中文」才发现的（2026-09-23）。
META_TEXT_PROPS = ("description", "og:description", "og:title", "twitter:description", "twitter:title")
_META_TEXT_ATTRS = ("content",)

#: 这些标签的内容不是给人读的句子（CSS/JS/纯文本域），**永远不当翻译单元**，
#: 也永远不往里走。少了这条，``<style>`` 里那段中文注释会被抽成一条待译原文，
#: 而整段 CSS 甚至会变成一条「兜底原文」。
_NEVER_UNIT = frozenset({"script", "style", "textarea"})

#: 这些标签的内容**不是 HTML**，解析时必须原样跳过，否则一个选择器里的 ``>``
#: 会被当成标签，整份文档从此错位。
_RAWTEXT_TAGS = frozenset({"script", "style", "textarea", "title"})

#: 元素里带这个属性 = 「这一块不要翻译」。用户自己写的内容（留言板、昵称）、
#: 代码样例、以及**这一轮还没接的界面**都靠它划出去。
SKIP_ATTR = "data-i18n-skip"

#: 在被划出去的子树里**单独开一个口子**：这个元素（含它的子树）照常翻。
#:
#: 存在的原因是 `#dashboard` 整块带着 `data-i18n-skip`（登录后的界面第二轮再翻），
#: 而「报告与账户」里那张「你的邮件走这条路」的卡片已经译齐了四门语言——
#: 为了它把整块 skip 摘掉，棘轮会立刻要求补齐剩下三百多句。
#: 语义是**继承式**的：祖先的 skip 会往下传，遇到这个属性就断掉（见 `_parse`）。
INCLUDE_ATTR = "data-i18n-include"

#: 内容为空、没有闭合标签的标签。
_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})

_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_WHITESPACE = re.compile(r"\s+")


def has_cjk(text: str) -> bool:
    """这段话里有没有汉字。**判断「要不要翻译」只看这个**，不看语言代码。"""
    return bool(_CJK.search(text or ""))


# --------------------------------------------------------------------------
# 语言清单
# --------------------------------------------------------------------------

_LOCALES_CACHE: list[tuple[float, list[dict[str, Any]]]] = []


def _fallback_locales() -> list[dict[str, Any]]:
    return [{"code": DEFAULT_LOCALE, "label": "简体中文", "reviewed": True}]


def locales() -> list[dict[str, Any]]:
    """清单里的每一种语言，顺序就是切换器里的顺序。

    读不出来（文件丢了、JSON 坏了）时**退化成只有中文**，而不是抛异常：
    一个有真实用户的站点不该因为一个词典文件坏掉就 500。
    """
    try:
        stamp = LOCALES_FILE.stat().st_mtime
    except OSError:
        return _fallback_locales()
    if _LOCALES_CACHE and _LOCALES_CACHE[0][0] == stamp:
        return _LOCALES_CACHE[0][1]
    try:
        raw = json.loads(LOCALES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logging.warning("语言清单读不出来（%s），按只有中文处理", exc)
        return _fallback_locales()
    items: list[dict[str, Any]] = []
    for entry in raw if isinstance(raw, list) else []:
        code = str(entry.get("code") or "").strip()
        if not code:
            continue
        items.append({
            "code": code,
            "label": str(entry.get("label") or code),
            # `reviewed` = 有人从头到尾看过这份译文。**没看过的语言在界面上要
            # 说自己是初译**，不能和校对过的并排放着让人以为一样可信。
            "reviewed": bool(entry.get("reviewed")),
        })
    if not any(item["code"] == DEFAULT_LOCALE for item in items):
        items.insert(0, {"code": DEFAULT_LOCALE, "label": "简体中文", "reviewed": True})
    _LOCALES_CACHE[:] = [(stamp, items)]
    return items


def offered() -> list[dict[str, Any]]:
    """**真正该出现在界面上**的语言。

    门槛是「**至少有一条译文**」（默认语言永远在）。理由是一次实测出来的缺陷：
    `locales.json` 里先加了 `ja` / `ko` 但词典还没写，于是切换器提供「日本語」，
    而选了之后拿到的是 `<html lang="ja">` + **中文正文**——那不只是没用，
    是对读屏软件说了假话（它按 `lang` 选发音）。

    宁可暂时不提供，也不要提供一门我们没有的语言；词典一写进去，它自己就出现了。
    """
    return [item for item in locales()
            if item["code"] == DEFAULT_LOCALE or bool(catalog(item["code"]))]


def locale_codes() -> list[str]:
    return [item["code"] for item in locales()]


def is_supported(code: str) -> bool:
    return (code or "").strip() in locale_codes()


def label_of(code: str) -> str:
    for item in locales():
        if item["code"] == code:
            return str(item["label"])
    return code


def reviewed(code: str) -> bool:
    for item in locales():
        if item["code"] == code:
            return bool(item["reviewed"])
    return False


# --------------------------------------------------------------------------
# 词典
# --------------------------------------------------------------------------


def _catalog_path(locale: str) -> Path:
    return I18N_ROOT / ("%s.json" % locale)


_CATALOG_CACHE: dict[str, tuple[float, dict[str, str]]] = {}


def catalog(locale: str) -> dict[str, str]:
    """某种语言的词典：``{中文原文: 译文}``。

    按文件的 mtime 缓存。译文是**运营期间随时会改**的东西，改完不该要求重启
    进程，所以每次比对 mtime，而不是像别的配置那样「改一次要重启」。
    """
    code = (locale or "").strip()
    if not code or code == DEFAULT_LOCALE:
        # 中文是原文，没有词典——查不到就一个字都不换。这不是「还没做」，是设计。
        return {}
    path = _catalog_path(code)
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return {}
    cached = _CATALOG_CACHE.get(code)
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logging.warning("%s 词典读不出来（%s），这一门按原样显示中文", code, exc)
        return {}
    table = {str(k): str(v) for k, v in raw.items() if isinstance(raw, dict) and str(v)}
    _CATALOG_CACHE[code] = (stamp, table)
    return table


def _keys_document() -> dict:
    try:
        raw = json.loads(KEYS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def keys() -> list[str]:
    """公开面上所有**应当**被翻译的原文（``tools/i18n_extract.py`` 产出）。

    它是覆盖率棘轮的基准：词典里少了哪一条，测试就点名说出哪一条，
    而不是靠人翻页去找一个还写着中文的地方。
    """
    raw = _keys_document()
    items = raw.get("keys") if raw else None
    return [str(item) for item in items or []]


def fallback_keys() -> list[str]:
    """被行内标签切碎、只当退路用的那批原文。

    它们**不算「必须翻译」**（整块译了就用不到），但确实会出现在页面上，
    所以算「孤儿」时要把它们一起算进去——否则一份译好的碎片会被误报成
    「源码里已经没有这一条」。
    """
    return [str(item) for item in (_keys_document().get("fallback") or [])]


def missing(locale: str) -> list[str]:
    """这门语言还差哪些条目（按抽取顺序，方便照着补）。"""
    table = catalog(locale)
    return [key for key in keys() if key not in table]


def coverage(locale: str) -> tuple[int, int]:
    """``(已译, 应译)``。切换器上那句「译了多少」用的就是这个数。"""
    known = keys()
    if not known:
        return (0, 0)
    table = catalog(locale)
    return (sum(1 for key in known if key in table), len(known))


def orphans(locale: str) -> list[str]:
    """词典里有、但公开面上已经找不到的条目。

    界面改了一句话，旧译文就永远匹配不上了。留着它不会出错，但会让人以为
    「这句译过了」——所以测试里把它报出来，改的人自己决定删还是改。
    """
    known = set(keys()) | set(fallback_keys())
    return sorted(key for key in catalog(locale) if key not in known)


# --------------------------------------------------------------------------
# 翻译
# --------------------------------------------------------------------------


def t(message: str, locale: str = DEFAULT_LOCALE, **params: Any) -> str:
    """把一句中文换成目标语言；**查不到就返回原文**（但占位符照样替换）。

    参数用 ``{name}`` 占位，用 ``str.replace`` 逐个替换，**不走 ``str.format``**：
    译文里可能出现 ``{``（CSS 片段、模板），``format`` 会当场抛异常，把一句文案
    变成一次 500。

    **「查不到」不是例外，而是多数访问者走的那条路**：中文是原文语，`catalog("zh")`
    按设计返回空词典。所以早退那条分支**必须先替换参数再返回**——2026-09-23 生产上
    真栽过：`{when}`（客服群那一节）与 `{size}`（安卓按钮）原样出现在页面上，
    两处都是「词典没有这条文案」的正常路径。
    """
    text = str(message)
    hit = catalog(locale).get(text)
    if hit is None:
        hit = text
    if params:
        for name, value in params.items():
            hit = hit.replace("{%s}" % name, str(value))
    return hit


# --------------------------------------------------------------------------
# 语言协商
# --------------------------------------------------------------------------


def mark(message: str) -> str:
    """标记一句「这是要翻译的文案」，**它什么都不做**，原样返回。

    服务端的报错是在 ``web.dispatch()`` 里统一翻译的——只有那个唯一出口知道这次
    请求是什么语言，而 ``raise ApiError(...)`` 的地方手上没有请求。所以 raise 的
    地方不能直接调 :func:`t`。

    这个函数存在的唯一理由，是让 ``tools/i18n_extract.py`` 能在源码里认出这些
    句子。没有它，一条服务端文案会**悄无声息地**永远不被翻译：界面看着好好的，
    只是那一条一直是中文。
    """
    return message


def match_locale(tag: str) -> str:
    """把一个 BCP-47 标签折成我们支持的语言之一，认不出就返回 ``""``。

    中文要按**文字**分，不能按国家猜：``zh-HK``/``zh-TW``/``zh-MO`` 是繁体，
    ``zh-CN``/``zh-SG``/光秃秃的 ``zh`` 是简体。这条判断写错，香港用户打开
    就是简体——而 CityU 在香港。
    """
    raw = (tag or "").strip()
    if not raw:
        return ""
    supported = locale_codes()
    lowered = raw.lower()
    for code in supported:
        if code.lower() == lowered:
            return code
    base = lowered.split("-")[0]
    if base == "zh":
        rest = lowered.split("-")[1:]
        if any(part in {"hant", "tw", "hk", "mo"} for part in rest):
            return "zh-Hant" if "zh-Hant" in supported else ""
        return "zh-Hans" if "zh-Hans" in supported else ""
    for code in supported:
        if code.lower() == base:
            return code
    return ""


def parse_accept_language(header: str) -> list[str]:
    """按 q 值从高到低排出浏览器偏好的顺序（``q=0`` 是「明确不要」）。"""
    items: list[tuple[float, int, str]] = []
    for index, chunk in enumerate((header or "").split(",")):
        piece = chunk.strip()
        if not piece:
            continue
        parts = piece.split(";")
        tag = parts[0].strip()
        if not tag:
            continue
        quality = 1.0
        for attr in parts[1:]:
            attr = attr.strip()
            if attr.startswith("q="):
                try:
                    quality = float(attr[2:])
                except ValueError:
                    quality = 0.0
        if quality <= 0:
            continue
        items.append((-quality, index, tag))
    items.sort()
    return [tag for _q, _i, tag in items]


def negotiate(accept_language: str = "", cookie: str = "", user_pref: str = "") -> str:
    """这次请求该用哪种语言。

    顺序是**从「最像用户本人说过的话」到「最像机器的猜测」**：

    1. 账号里存着的偏好（登录用户换过设备也记得）；
    2. cookie（还没登录时，在介绍页上选的那一次）；
    3. ``Accept-Language``（什么都没说过时的第一印象）；
    4. 中文。
    """
    for candidate in (user_pref, cookie):
        code = (candidate or "").strip()
        if code and is_supported(code):
            return code
    for tag in parse_accept_language(accept_language):
        code = match_locale(tag)
        if code:
            return code
    return DEFAULT_LOCALE


# --------------------------------------------------------------------------
# HTML 改写器
# --------------------------------------------------------------------------
#
# 只在**原文**上做区间替换，不重新序列化整份文档。
#
# 为什么不重建：``<style>`` 里一个选择器、属性里的引号风格、自闭合标签的写法，
# 任何一处序列化差异都是一次静默的样式崩坏。区间替换的爆炸半径是**一个区间**，
# 而且有一条可以直接验的性质：
#
#     词典为空时，translate_html(source, "zh-Hans") == source，逐字节相同。
#
# 这条就是 `test_i18n.LosslessTests` 钉着的东西。它同时也证明了「中文路径
# 与改造前完全一致」——那正是「不推倒重来」这句话在本模块里的可验证形式。

_TAG_RE = re.compile(
    r"(?P<comment><!--.*?-->)"
    r"|(?P<decl><![^>]*>)"
    r"|(?P<end></\s*(?P<endname>[A-Za-z][-A-Za-z0-9:]*)\s*>)"
    r"|(?P<start><\s*(?P<name>[A-Za-z][-A-Za-z0-9:]*)(?P<attrs>(?:[^>\"']|\"[^\"]*\"|'[^']*')*)>)"
    r"|(?P<text>[^<]+)"
    r"|(?P<stray><)",
    re.S,
)

_ATTR_RE = re.compile(
    r"(?P<name>[-A-Za-z_:][-A-Za-z0-9_:.]*)\s*(?:=\s*(?P<value>\"[^\"]*\"|'[^']*'|[^\s\"'>]+))?"
)


class _Node(dict):
    """一颗够用的元素树。只为一件事存在：知道**每个元素的内容区间在哪**。"""


def _parse(source: str) -> _Node:
    """把 HTML 切成树，每个元素都记着自己的**内容区间**。

    这**不是**一个合规的 HTML 解析器，也不打算是：它只处理我们自己手写的那些
    文件（没有隐式闭合、没有误嵌套）。遇到 ``<`` 后面跟不出标签时按普通文本
    处理，宁可少翻一句，也不把文档结构弄错。
    """
    root = _Node(kind="element", tag="", inner_start=0, inner_end=len(source), children=[], skip=False)
    stack = [root]
    position = 0
    length = len(source)
    while position < length:
        match = _TAG_RE.match(source, position)
        if match is None:  # pragma: no cover - 上面的正则连单个字符都能匹配
            break
        position = match.end()
        kind = match.lastgroup
        if kind == "comment" or kind == "decl":
            # 注释与 `<!doctype>` **不是文字**，单独记一种节点。第一版把它们当成
            # text 节点，于是「兜底清单」里混进 21 条注释（landing.html 的注释里
            # 全是中文说明），看上去像 21 句没译的界面文案——真正要译的 8 段淹没在
            # 里面。注释是写给维护者的，本来就不该翻。
            stack[-1]["children"].append(_Node(kind=kind, start=match.start(), end=match.end()))
            continue
        if kind == "text" or kind == "stray":
            stack[-1]["children"].append(_Node(kind="text", start=match.start(), end=match.end()))
            continue
        if kind == "end":
            name = (match.group("endname") or "").lower()
            for index in range(len(stack) - 1, 0, -1):
                if stack[index]["tag"] == name:
                    stack[index]["inner_end"] = match.start()
                    del stack[index:]
                    break
            continue
        name = (match.group("name") or "").lower()
        attrs = match.group("attrs") or ""
        self_closing = attrs.rstrip().endswith("/")
        node = _Node(
            kind="element", tag=name, start=match.start(), inner_start=match.end(),
            inner_end=length, children=[], attrs=attrs,
            # 开始标签形如 `<name` + attrs + `>`，所以属性文本的绝对起点是
            # 「标签结束位置往回退一个 `>` 再退掉 attrs 的长度」。
            attrs_start=match.end() - 1 - len(attrs),
            skip=_has_skip_attr(attrs),
            include=_has_attr(attrs, INCLUDE_ATTR),
        )
        stack[-1]["children"].append(node)
        if name in _RAWTEXT_TAGS:
            closer = re.compile(r"</\s*%s\s*>" % re.escape(name), re.I).search(source, position)
            end = closer.start() if closer else length
            inner = source[position:end]
            if inner:
                node["children"].append(_Node(kind="text", start=position, end=end))
            node["inner_end"] = end
            position = closer.end() if closer else length
            continue
        if name in _VOID_TAGS or self_closing:
            node["inner_end"] = node["inner_start"]
            continue
        stack.append(node)
    return root


def _has_attr(attrs: str, name: str) -> bool:
    for match in _ATTR_RE.finditer(attrs or ""):
        if (match.group("name") or "").lower() == name:
            return True
    return False


def _has_skip_attr(attrs: str) -> bool:
    return _has_attr(attrs, SKIP_ATTR)


def _child_skip(parent_skipped: bool, node: dict) -> bool:
    """这一块（连同子树）要不要跳过翻译？祖先说跳就一直跳，除非它自己开了口子。

    `data-i18n-include` 是**局部反向**：写它 = 「这一段我接好了」，于是它和它的
    子树回到可翻译状态，祖先的 `data-i18n-skip` 不再往下压。两条走法
    （`unit_keys` 与改写器 `_walk`）都用这一条判据，所以「抽得到」与「翻得了」
    不会各说各话。
    """
    if node.get("include"):
        return False
    return parent_skipped or bool(node.get("skip"))


def normalize_key(fragment: str) -> str:
    """一个翻译单元的 key：把连续空白折成一个空格再掐头去尾。

    源码里的缩进和换行不该进入 key——否则把一段话重新缩进就让它「失去译文」，
    而译文其实一个字都没错。
    """
    return _WHITESPACE.sub(" ", fragment or "").strip()


def _pad(original: str, replacement: str) -> str:
    """换掉一段文字时，把它两侧原有的空白原样留着（缩进不至于塌掉）。"""
    head = original[:len(original) - len(original.lstrip())]
    tail = original[len(original.rstrip()):]
    return head + replacement + tail


#: 块级标签。一个元素的**内容**里只要出现这些，它自己就不算一个翻译单元——
#: 否则 ``<body>`` 会成为「一个单元」，而它的「译文」就是整页 HTML：实测放宽
#: 这条规则后，整份介绍页只剩 **2 条** key，最长那条 19513 字节。反过来，
#: ``<p>来信<b>转发</b>到…</p>`` 里只有行内标签，整段就是一个单元，译文可以
#: 自由重排语序（这正是需要的）。
BLOCK_TAGS = frozenset({
    "address", "article", "aside", "blockquote", "body", "button", "caption",
    "dd", "details", "dialog", "div", "dl", "dt", "fieldset", "figcaption",
    "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header",
    "html", "label", "legend", "li", "main", "nav", "ol", "option", "p",
    "section", "select", "summary", "table", "tbody", "td", "textarea",
    "tfoot", "th", "thead", "tr", "ul",
})


def _has_block_descendant(node: _Node) -> bool:
    for child in node.get("children") or []:
        if child.get("kind") != "element":
            continue
        if child.get("tag") in BLOCK_TAGS:
            return True
        if _has_block_descendant(child):
            return True
    return False


def _has_direct_text(node: _Node, source: str) -> bool:
    """这个元素的**直接**内容里有中文（不是从子元素里继承来的）。

    这条把「一句话」和「一个容器」分开了，两者看着都像单元，但译文完全不同：

    * ``<p>来信<b>转发</b>到你的私人邮箱。</p>`` —— 自己就有中文，**是**一句话。
      整段一个 key，英文才能把语序整个倒过来。
    * ``<nav><a href="#how">它做什么</a><a href="#inbox">收件箱演示</a></nav>``
      —— 自己一个字没有，**是**容器。不拦的话这一条 key 会把 ``href`` 和
      ``style="--i:0"`` 一起吞进去：改一次导航顺序，译文就此失配。
    """
    for child in node.get("children") or []:
        if child.get("kind") != "text":
            continue
        if has_cjk(source[child["start"]:child["end"]]):
            return True
    return False


def unit_keys(source: str) -> tuple[list[str], list[str]]:
    """抽出一份 HTML 里所有该翻译的原文，返回 ``(必须, 兜底)``。

    * **必须**：每个「翻译单元」（含汉字、自己直接含文字、且内容里没有块级标签
      的最外层元素）的内容，外加可翻译属性的值。这一批译齐，页面上就不会再有中文。
    * **兜底**：被 ``<b>`` 之类切开的零散文字。整块译了就用不上它们；某一块
      还没译时，有它们至少不会整段留在中文里。

    切换器上那句「译了多少」按**必须**那一批算——那才是「这页能不能看」的分母。
    """
    root = _parse(source)
    required: list[str] = []
    fallback: list[str] = []

    def walk(node: _Node, skipped: bool = False) -> None:
        for child in node.get("children") or []:
            if child.get("kind") == "text":
                if skipped:
                    continue
                raw = source[child["start"]:child["end"]]
                key = normalize_key(raw)
                if key and has_cjk(key) and key not in fallback:
                    fallback.append(key)
                continue
            if child.get("kind") != "element":
                continue
            # `_NEVER_UNIT` 必须挡在**这里**，不能只写在下面那条单元判定里：
            # 只挡住「当单元」的话，`walk()` 还会往 `<style>` 里面走，整段 CSS 就
            # 变成一条「兜底原文」（而且它含中文注释，所以 has_cjk 为真）。
            if child.get("tag") in _NEVER_UNIT:
                continue
            if _child_skip(skipped, child):
                # 被划出去的子树**还要往下走**：里面可能有 `data-i18n-include`
                # 单独开口子的块（`#dashboard` 里的「你的邮件走这条路」就是）。
                walk(child, True)
                continue
            for inner, _quote, _start, _end in _translatable_attrs(child.get("tag") or "",
                                                                   child.get("attrs") or ""):
                key = normalize_key(inner)
                if key and key not in required:
                    required.append(key)
            inner = source[child["inner_start"]:child["inner_end"]]
            if (has_cjk(inner) and _has_direct_text(child, source)
                    and not _has_block_descendant(child)):
                key = normalize_key(inner)
                if key and key not in required:
                    required.append(key)
                continue
            walk(child)

    walk(root)
    must = set(required)
    return required, [key for key in fallback if key not in must]


def _translatable_attrs(tag: str, attrs: str) -> list[tuple[str, str, int, int]]:
    """标签里该翻译的属性，返回 ``(原文, 引号, 值起点, 值终点)``（相对 ``attrs``）。"""
    if not attrs or not has_cjk(attrs):
        return []
    wanted = set(TRANSLATABLE_ATTRS)
    if tag == "meta":
        # ``name="description"`` 或 ``property="og:description"`` 之类：名字对上
        # 哪一种，**它的 content** 才进白名单。
        for prop in META_TEXT_PROPS:
            if re.search(r'(?:name|property)\s*=\s*["\']?%s["\']?' % re.escape(prop),
                         attrs, re.I) is not None:
                wanted |= set(_META_TEXT_ATTRS)
                break
    out: list[tuple[str, str, int, int]] = []
    for match in _ATTR_RE.finditer(attrs):
        name = (match.group("name") or "").lower()
        value = match.group("value")
        if value is None or name not in wanted:
            continue
        quote = value[0] if value[:1] in {'"', "'"} else ""
        inner = value[1:-1] if quote and value.endswith(quote) else value
        if not inner or not has_cjk(inner):
            continue
        start = match.start("value") + (1 if quote else 0)
        out.append((inner, quote, start, start + len(inner)))
    return out


def _attribute_hits(node: _Node, source: str, table: dict[str, str]) -> list[tuple[int, int, str]]:
    """这个标签的属性里，有没有该翻译的（占位符、无障碍标签、分享描述）。"""
    base = node.get("attrs_start") or 0
    out: list[tuple[int, int, str]] = []
    for inner, quote, start, end in _translatable_attrs(node.get("tag") or "", node.get("attrs") or ""):
        hit = table.get(normalize_key(inner))
        if hit is None:
            continue
        # 只换引号**里面**那一段，但译文里若出现同一个引号字符，必须转义——
        # 否则一个英文撇号就能把标签提前关掉。
        if quote == '"':
            hit = hit.replace('"', "&quot;")
        elif quote == "'":
            hit = hit.replace("'", "&#39;")
        out.append((base + start, base + end, hit))
    return out


def _walk(node: _Node, source: str, table: dict[str, str],
          out: list[tuple[int, int, str]], skipped: bool = False) -> None:
    """遍历一个元素的内容，自上而下找翻译单元；命中就整块换掉、不再往下走。

    「整块」指元素的**内容**（innerHTML）而不是元素本身：这样命中的译文可以
    自由重排标记（中文里 ``来信<b>转发</b>到…`` 到英文可能整句语序都变了），
    而 ``<p class="…">`` 这些属性还在我们手上，不会被译文带跑。

    整块没命中就退到**逐段**：父元素里被 ``<b>`` 切开的那些文字各自去查一次。
    逐段翻译在语序不同的语言里会读着别扭，但它至少不会把整段话留在中文——
    而「读着别扭」和「整段没译」哪个更糟，是运营者能自己决定的事（补一条整块
    译文即可），不需要改代码。

    ``skipped`` 是**继承下来**的状态（祖先带 `data-i18n-skip`）；带
    `data-i18n-include` 的子元素会把它清掉，见 `_child_skip`。
    """
    for child in node.get("children") or []:
        if child.get("kind") == "text":
            if skipped:
                continue
            raw = source[child["start"]:child["end"]]
            if not has_cjk(raw):
                continue
            piece = table.get(normalize_key(raw))
            if piece is not None:
                out.append((child["start"], child["end"], _pad(raw, piece)))
            continue
        if child.get("kind") != "element":
            continue
        if child.get("tag") in _NEVER_UNIT:
            continue
        if _child_skip(skipped, child):
            # 划出去的子树还是要走一遍：`data-i18n-include` 可以在里面把某一块
            # 重新放回可翻译状态（与 `unit_keys` 的同一条规则）。
            _walk(child, source, table, out, True)
            continue
        inner = source[child["inner_start"]:child["inner_end"]]
        hit = table.get(normalize_key(inner)) if has_cjk(inner) else None
        if hit is not None:
            out.append((child["inner_start"], child["inner_end"], hit))
            continue
        out.extend(_attribute_hits(child, source, table))
        _walk(child, source, table, out)


def translate_html(source: str, locale: str, table: Optional[dict[str, str]] = None) -> str:
    """把一份 HTML 里的中文换成目标语言；**词典为空时逐字节返回原文**。

    只有 ``<script>``/``<style>`` 的内容、注释、以及带 ``data-i18n-skip`` 的
    子树是绝对不碰的——留言板里那句话是用户写的，翻译它等于替用户改口供。
    """
    code = (locale or "").strip() or DEFAULT_LOCALE
    mapping = catalog(code) if table is None else table
    if not mapping:
        # 快路径，也是**最重要**的一条：中文（以及任何还没译的语言）走这里，
        # 返回的就是传进来的那份字符串本身。
        return source
    root = _parse(source)
    replacements: list[tuple[int, int, str]] = []
    _walk(root, source, mapping, replacements)
    if not replacements:
        return source
    # 从后往前替换，这样前面的区间不会因为后面的长度变化而错位。
    ordered = sorted(set(replacements), key=lambda item: item[0], reverse=True)
    text = source
    for start, end, value in ordered:
        text = text[:start] + value + text[end:]
    return text


CACHE_LIMIT = 32
_HTML_CACHE: dict[tuple[str, str, int, int], str] = {}


def translate_file(path: Path, locale: str) -> str:
    """读一个模板文件并翻译，按 ``(路径, 语言, mtime, 大小)`` 缓存。

    介绍页有 140 KB，每次请求都重新解析一遍是白花的 CPU；但按 mtime 失效意味着
    **改完模板不用重启**（这条与仓库其它地方「改配置要重启」的习惯相反，是有意的：
    文案是随时会改的东西）。
    """
    stat = path.stat()
    key = (str(path), (locale or DEFAULT_LOCALE), stat.st_mtime_ns, stat.st_size)
    cached = _HTML_CACHE.get(key)
    if cached is not None:
        return cached
    text = translate_html(path.read_text(encoding="utf-8"), locale)
    if len(_HTML_CACHE) >= CACHE_LIMIT:
        _HTML_CACHE.clear()
    _HTML_CACHE[key] = text
    return text


def reset_cache() -> None:
    """测试用：把按 mtime 的缓存清掉（改了词典文件之后必须调）。"""
    _CATALOG_CACHE.clear()
    _HTML_CACHE.clear()
    _LOCALES_CACHE.clear()


def alternates(path: str = "/") -> list[dict[str, str]]:
    """``hreflang`` 替代链接：让搜索引擎知道同一页有哪几个语言版本。

    ``x-default`` 指向不带参数的地址（也就是中文）——它是「谁都没匹配上时
    看哪一版」，不是「英文版」。
    """
    out = [{"hreflang": "x-default", "href": path}]
    for item in offered():
        params = "" if item["code"] == DEFAULT_LOCALE else "?%s=%s" % (LANG_PARAM, item["code"])
        out.append({"hreflang": item["code"], "href": path + params})
    return out


def registry() -> dict[str, Any]:
    """切换器要的一份快照：每种语言叫什么、有没有校对过、译了多少。"""
    items = []
    total = len(keys())
    for item in offered():
        # 中文是**原文**，不是「译了 0 条」。第一版如实报 0，切换器上就成了
        # 「简体中文 0/526」——一句既难看又不对的话：它一个字都没缺。
        done = total if item["code"] == DEFAULT_LOCALE else coverage(item["code"])[0]
        items.append({
            "code": item["code"],
            "label": item["label"],
            "reviewed": item["reviewed"],
            "translated": done,
            "total": total,
        })
    return {"default": DEFAULT_LOCALE, "locales": items}


def parse_cookie(header: str, name: str = LANG_COOKIE) -> str:
    """从 ``Cookie:`` 头里取一个值。解析失败就当没有，**绝不抛异常**。"""
    for chunk in (header or "").split(";"):
        key, _, value = chunk.partition("=")
        if key.strip() == name:
            return value.strip()
    return ""
