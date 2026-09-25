"""Standard-library onboarding and dashboard API for the 3-5 user pilot.

Replaces the previous FastAPI/uvicorn layer: the audit found the pinned
Starlette branch carried public advisories and no compatible fixed release was
available, so the HTTP surface is implemented on ``http.server`` instead. Only
``cryptography`` remains as a runtime dependency.

The public API is unchanged:

* ``db`` / ``service`` module attributes for tests and admin tooling
* identical routes, JSON shapes, cookie name and ``{"detail": ...}`` errors
* same-origin Origin fence, security headers, login throttling and body limits
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import html
import json
import logging
import os
import re
import secrets
import signal
import sys
import threading
import time
import traceback
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, quote, unquote, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import __version__ as VERSION
from . import agent as agent_mod
from . import alerting
from . import analytics as analytics_mod
from . import i18n
from . import imageguard
from . import invites as invites_mod
from . import signup_notice
from . import mailio as mailio_mod
from . import metrics as metrics_mod
from . import service as service_mod
from . import pricing as pricing_mod
from . import providers
from . import reports as reports_mod
from . import snooze
from . import taskexport
from . import setup_reminders
from . import worker as worker_mod
from .database import Database, utc_now
from . import mailpresets
from .mailpresets import public_mailbox_help
from . import appearance
from . import database as database_mod
from . import demo
from . import digest_synthesis
from .providers import (MODEL_PRESETS, SEARCH_PRESETS, normalized_model_config,
                        public_catalog, supports_native_search)
from .security import (
    SecretBox,
    SecurityError,
    generate_temporary_password,
    hash_password,
    new_token,
    spend_verification_time,
    token_hash,
    validate_public_host,
    verify_password,
)
from .service import PilotService

SESSION_COOKIE = "cityu_mail_session"
SESSION_DAYS = 14
APP_ROOT = Path(__file__).resolve().parent

MAX_BODY_BYTES = 64 * 1024
# Background photos are the one request that is not JSON, so they get their own
# ceiling instead of raising the shared one. Raising MAX_BODY_BYTES would hand
# every JSON endpoint (login, signup, profile) a 1.5 MB read budget it has no use
# for, which is a denial-of-service surface bought for nothing.
MAX_BACKGROUND_BYTES = 1_500_000
MAX_JSON_DEPTH_ITEMS = 200

STATIC_ROOT = (APP_ROOT / "static").resolve()
STATIC_FILES: dict[str, tuple[str, str]] = {
    # The root is the marketing page a stranger lands on; the application lives
    # at /app. Keeping them apart is what lets one be indexable prose and the
    # other a single-page app, without either compromising for the other.
    "/": ("landing.html", "text/html; charset=utf-8"),
    "/app": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    # 设计系统预览：给维护者看的说明书（noindex、不在主导航，页脚有小入口）。
    "/design-system": ("design-system.html", "text/html; charset=utf-8"),
    # Crawlers: `/` is the page worth indexing, `/app` is a login shell.
    "/robots.txt": ("robots.txt", "text/plain; charset=utf-8"),
    "/landing.js": ("landing.js", "application/javascript; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/theme-boot.js": ("theme-boot.js", "application/javascript; charset=utf-8"),
    # 语言切换器的自动提交 + JS 里的 `t()`。介绍页与应用外壳都加载它。
    "/i18n.js": ("i18n.js", "application/javascript; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/icon-192.png": ("icon-192.png", "image/png"),
    "/icon-512.png": ("icon-512.png", "image/png"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
    # The legacy name iOS probes during Add-to-Home-Screen, seen 404ing in the
    # access log on 2026-09-14. Same bytes as the line above; the copies are
    # pinned identical by a test.
    "/apple-touch-icon-precomposed.png": ("apple-touch-icon-precomposed.png", "image/png"),
    # Deliberately NOT listed: /apple-touch-icon-120x120.png and its
    # "-precomposed" twin. iOS probes them too, but 120px is below the 180px an
    # iPhone home screen actually wants, and the probe order puts them *before*
    # apple-touch-icon.png -- answering 200 would win that race and replace a
    # sharp icon with an upscaled one. Only the two 180 names are served; the
    # reasoning and the log evidence are in docs/phone-install-2026-09-14.md.
    "/bg-paper.png": ("bg-paper.png", "image/png"),
    "/bg-dusk.png": ("bg-dusk.png", "image/png"),
    "/bg-harbour.png": ("bg-harbour.png", "image/png"),
    "/bg-night.png": ("bg-night.png", "image/png"),
    # Screenshots of the running app, shown on the landing page. Real ones: the
    # single strongest signal that a page is describing a product that exists is
    # being able to see it, and a marketing page that never shows the thing is
    # the shape every generated page has. Regenerate with tools/site_shots.js.
    "/app-tasks.png": ("app-tasks.png", "image/png"),
    "/app-tasks-phone.png": ("app-tasks-phone.png", "image/png"),
    # 首屏右边那张收件箱截图。它**不是真机截图**，是设计稿（PR #6）里画的那张，
    # 我们从 `mail-pilot.html` 内嵌的 base64 原样取出来（834×622）。用户 2026-09-23
    # 明确要「照稿子放那张图」，所以它取代了原来那块「活的行」——
    # 代价写在 `docs/landing-pr6-adoption-2026-09-23.md`：图里的日期是画上去的，
    # 而且它只有中文版；卡片底下那两行小字照旧写明「演示数据」。
    "/hero-preview.png": ("hero-preview.png", "image/png"),
    # The forwarding tutorial for desktop Outlook, which has no 「转发」 switch --
    # it has rules. Real screenshots of the operator's own machine, cropped and
    # with the account line excluded and the address painted over; the only way
    # to produce them is tools/forward_shots.js, and test_forward_shots.py pins
    # their bytes so a regeneration is looked at by a human.
    "/forward-rule-1-add.png": ("forward-rule-1-add.png", "image/png"),
    "/forward-rule-2-condition.png": ("forward-rule-2-condition.png", "image/png"),
    "/forward-rule-3-action.png": ("forward-rule-3-action.png", "image/png"),
    "/forward-rule-4-done.png": ("forward-rule-4-done.png", "image/png"),
    # The install diagrams on the landing page. **Diagrams, not screenshots** --
    # there is no Android device in this project, and what the reader needs is
    # which control to tap, not what the screen looks like. Drawn by
    # tools/install_shots.js; test_install_shots.py pins their bytes.
    "/install-android-apk.png": ("install-android-apk.png", "image/png"),
    "/install-android-unknown.png": ("install-android-unknown.png", "image/png"),
    "/install-android-install-anyway.png": ("install-android-install-anyway.png", "image/png"),
    "/install-android-chrome.png": ("install-android-chrome.png", "image/png"),
    "/install-ios-share.png": ("install-ios-share.png", "image/png"),
    "/install-ios-add.png": ("install-ios-add.png", "image/png"),
    "/install-standalone.png": ("install-standalone.png", "image/png"),
    # 「授权码」那一步的示意图。**画的，不是搜来的截图** —— 第三方教程图没有可再分发的
    # 许可，而且我无法确认它还是不是今天的界面；「永远最新」那部分交给各家官网的链接。
    # 由 tools/appcode_shots.js 产出，test_appcode_shots.py 钉住字节。
    "/appcode-two-passwords.png": ("appcode-two-passwords.png", "image/png"),
    "/appcode-code-once.png": ("appcode-code-once.png", "image/png"),
    "/appcode-where.png": ("appcode-where.png", "image/png"),
    # 客服群二维码（运营者自己的群）。**这一行漏过一次，是真机上抓到的**：2026-09-23
    # 上线 v1.2.0 后介绍页那一节渲染了 `<img src="/wechat-group.png">`，而这张图不在
    # 上面这张表里 → 访客看到的是一张**裂图**，单测全绿（它们只断言 URL 出现在 HTML 里，
    # 没断言这个 URL 服务得出来）。现在 `test_landing` 有一条通用断言：页面引用的每一个
    # 本地图片都必须在 `STATIC_FILES` 里、且在 `static/` 下真的存在。
    "/wechat-group.png": ("wechat-group.png", "image/png"),
    "/privacy": ("privacy.html", "text/html; charset=utf-8"),
    "/terms": ("terms.html", "text/html; charset=utf-8"),
}

# 首页**不再**有 `{{PILOT_COUNT}}`（2026-09-24 随 PR #10 那句「N 个账号接好了邮箱」
# 一起从页面删掉；代码里的注入点也撤了，见 `render_landing_page` 的 docstring）。
# The legal pages carry {{CONTACT_LINK}}: the contact address is an operator
# setting, so a self-hoster must not inherit ours (and we must not publish theirs
# by accident). Every other static file is still served byte-for-byte.
#
# `/app` 在 2026-09-23 加进来：登录/注册那一屏现在也要跟着界面语言走，而它是一张
# 静态 HTML。**翻译是在服务端做的**（`i18n.translate_file`），所以英文用户拿到的
# 第一份 HTML 就是英文的——没有「先闪一下中文再被脚本换掉」。登录之后的界面带着
# `data-i18n-skip`，第二轮再翻。
TEMPLATED_STATIC = frozenset({"/", "/privacy", "/terms", "/app"})

# Shown instead of an address when the operator configured no contact channel.
# A privacy policy without a contact route is not a usable policy, so the gap is
# stated out loud rather than rendered as a dead mailto: link.
#
# **这句在 `render_legal_page` 里是照着字面量再写一遍的**，不是引用这个常量：
# 抽取器是照着源码里的字面量找待译句子的，写成模块常量它就看不见——第一版就是
# 这样，英文页上一直印着这句中文，覆盖率却报 100%。渲染出来才发现，所以
# `test_i18n_pages` 那条「英文页上不许有中文」才是判据。改这句时两处一起改，
# 忘了改常量，`test_compliance` 会红。
NO_CONTACT_NOTICE = "本实例的运营者（尚未配置联系邮箱）"

# Appearance is a per-user preference stored on the profile, so the same choice
# follows the account to another browser. The lists below are the only accepted
# values: anything else is rejected instead of being stored and later injected
# into a class name or a url().
THEMES: tuple[str, ...] = ("classic", "paper", "dusk", "harbour", "night")
BACKGROUNDS: tuple[str, ...] = ("", "paper", "dusk", "harbour", "night", "none", "custom")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    # img-src carries blob: because the background-photo preview is an object URL
    # built from the re-encoded image the browser just produced. That adds no
    # real reach: a blob URL can only be created by script, and script-src is
    # locked to 'self', so anything able to mint one is already running our code.
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self'; "
        "img-src 'self' data: blob:; connect-src 'self'"
    ),
}

AUTHENTICATED_METHODS = {"GET", "HEAD", "OPTIONS"}


# --------------------------------------------------------------------------
# errors and responses
# --------------------------------------------------------------------------


class ApiError(Exception):
    """An error that maps to a JSON ``{"detail": ...}`` response."""

    def __init__(self, status: int, detail: str,
                 params: Optional[dict[str, Any]] = None) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        # 带参数的文案（``字段 {name} 太长。`` 这一类）。译文里的 ``{name}`` 由
        # `dispatch` 在翻译时替换掉：**翻译要用模板本身当 key**，而抛出点手上
        # 已经拼好的那句（`字段 foo 太长。`）永远匹配不上任何一条译文。
        self.params = params or {}


class Response:
    __slots__ = ("status", "body", "content_type", "headers", "cookies")

    def __init__(
        self,
        status: int = 200,
        body: bytes = b"",
        content_type: str = "application/json; charset=utf-8",
        headers: Optional[dict[str, str]] = None,
        cookies: Optional[list[str]] = None,
    ) -> None:
        self.status = status
        self.body = body
        self.content_type = content_type
        self.headers = headers or {}
        self.cookies = cookies or []


def json_response(payload: Any, status: int = 200, *, cookies: Optional[list[str]] = None) -> Response:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return Response(status=status, body=body, cookies=cookies)


def error_response(status: int, detail: str, *, cookies: Optional[list[str]] = None) -> Response:
    return json_response({"detail": detail}, status=status, cookies=cookies)


def _etag_matches(request: "Request", etag: str) -> bool:
    """Does the client already hold exactly these bytes?

    Only ``If-None-Match`` is consulted when it is present, which is what
    RFC 9110 requires (a stale ``If-Modified-Since`` must not override a fresh
    ETag comparison). ``W/`` prefixes are stripped before comparing: our tags are
    strong, and answering a weak comparison with a 304 is safe because the bytes
    are identical by construction.
    """
    header = str((request.headers.get("If-None-Match") or "")).strip()
    if not header:
        return False
    candidates = {tag.strip().removeprefix("W/") for tag in header.split(",")}
    return "*" in candidates or etag in candidates


def _file_validators(target: Path) -> dict[str, str]:
    """Validators for a file served off disk, so a repeat visit costs a 304.

    ``Cache-Control: no-cache`` stays: these URLs carry no version, so a phone
    **must** revalidate — otherwise a deploy would leave it running last week's
    JavaScript. What changed (2026-09-24, the operator: 「为什么我打开视频和软件
    很慢」) is that revalidating used to mean re-downloading: the app sent no
    validator at all, so every open of the app pulled the whole 320 KB
    `app.js` over a mobile network. mtime+size is the right validator here --
    the bytes change exactly when a deploy replaces the file, and hashing a
    320 KB file on every request would cost more than the 304 saves.
    """
    info = target.stat()
    stamp = dt.datetime.fromtimestamp(info.st_mtime, dt.timezone.utc)
    return {
        "ETag": '"%x-%x"' % (info.st_mtime_ns, info.st_size),
        "Last-Modified": stamp.strftime("%a, %d %b %Y %H:%M:%S GMT"),
    }


def file_response(target: Path, content_type: str, *, download_name: str = "",
                  request: Optional["Request"] = None) -> Response:
    headers = {"Cache-Control": "no-cache"}
    if download_name:
        # `attachment` so no browser ever tries to render the bytes, and the
        # quotes are stripped because this value came from a file name: a stray
        # `"` would end the header early and let the rest be read as a new one.
        headers["Content-Disposition"] = (
            'attachment; filename="' + download_name.replace('"', "").replace("\\", "") + '"')
    try:
        headers.update(_file_validators(target))
    except OSError:  # pragma: no cover - a race with a deploy must not 500
        pass
    if request is not None and "ETag" in headers and _etag_matches(request, headers["ETag"]):
        # No body at all: `_respond` announces `len(body)`, so an empty body is
        # an honest `Content-Length: 0` on a 304.
        return Response(status=304, body=b"", content_type=content_type, headers=headers)
    data = target.read_bytes()
    return Response(status=200, body=data, content_type=content_type, headers=headers)


def contact_email() -> str:
    """The address published on the legal pages, or "" when none is set.

    ``INFE_PILOT_CONTACT_EMAIL`` exists so an operator can publish a dedicated
    address without making it their admin login (the admin list grants rights;
    the contact address is merely printed on a public page). Falling back to the
    first admin keeps single-operator installs working with no extra config.
    """
    configured = os.environ.get("INFE_PILOT_CONTACT_EMAIL", "").strip()
    if configured:
        return configured
    admins = sorted(alerting.admin_emails())
    return admins[0] if admins else ""


def render_legal_page(target: Path, locale: str = i18n.DEFAULT_LOCALE) -> bytes:
    """Fill the contact placeholder in a legal page.

    The address is escaped before it reaches the attribute, because the value
    comes from an environment variable and a quote in it would otherwise break
    out of the href.

    ``locale`` 默认中文：**不带参数调用就等于改造前**，别处的调用与测试照旧。
    """
    address = contact_email()
    if address:
        link = '<a href="mailto:%s">%s</a>' % (html.escape(address, quote=True), html.escape(address))
    else:
        # 字面量、不是 `NO_CONTACT_NOTICE`（见那一行上面的注释）：抽取器只认源码里
        # 的字面量。
        link = "<code>%s</code>" % _say("本实例的运营者（尚未配置联系邮箱）", locale)
    text = i18n.translate_file(target, locale)
    # 翻译**先做、替换后做**：`{{CONTACT_LINK}}` 是注入点，先注入的话这句中文就再也
    # 匹配不上它的译文了（key 是中文原文）。下面每一处都是这个顺序。
    text = text.replace("{{CONTACT_LINK}}", link)
    return _finish_page(text, target.name, locale).encode("utf-8")


def _say(message: str, locale: str = i18n.DEFAULT_LOCALE, **params: Any) -> str:
    """当场翻译并转义，给拼 HTML 的那些 render_* 用。

    **不要在函数里再定义一个局部别名**（第一版就是这么写的：`def say(...)`）。
    抽取器是照着源码里的 `translate_text(` 找待译句子的，别名一出现，那几句就
    从 `keys.json` 里消失了——而覆盖率仍然报 100%，因为**分母也一起消失了**。
    2026-09-23 实测漏掉 8 句（`找到我们`、`这张码 {when}有效` 那些），
    是「英文页上不许有中文」那条端到端测试把它们揪出来的。

    转义在翻译**之后**：词典是我们自己写的，但 `<` 一旦漏进去就是一次注入口。
    """
    return html.escape(translate_text(message, locale, **params))


def translate_text(message: str, locale: str = i18n.DEFAULT_LOCALE, **params: Any) -> str:
    """服务端文案翻译的**唯一入口**（web 层内部用）。

    单独包一层而不是到处写 ``i18n.t(...)``，是为了让抽取工具一眼分得清
    「这是给人看的文案」和「这是 dispatch 里统一翻译 API 报错的那次调用」。
    """
    return i18n.t(message, locale, **params)


def language_cookie(locale: str) -> str:
    """记住「这个人选了哪种语言」。

    不是 ``HttpOnly``：这一条要在客户端读得到，``i18n.js`` 靠它决定切换器上选中
    哪一项。它不含任何身份信息，被脚本改掉最多是让页面换成另一种语言——
    与 ``cityu_mail_session`` 完全不是一个量级的东西，所以不需要 HttpOnly。
    """
    return f"{i18n.LANG_COOKIE}={locale}; Path=/; Max-Age=31536000; SameSite=Lax{_cookie_flags()}"


def render_language_switch(locale: str) -> str:
    """切换器：一个 ``<select>`` 加一个 ``<noscript>`` 里的按钮。

    为什么是表单而不是几个 ``<a>``：地址栏里那个 ``?lang=`` 是**可以贴给别人的**
    （「你看这页英文版」），而链接要每个语言手写一份 URL。

    为什么不用 ``onchange="this.form.submit()"``：CSP 是 ``script-src 'self'``，
    内联事件处理器会被浏览器**静默拦掉**——页面看着有切换器，选了没反应。
    自动提交放在外部文件 ``i18n.js`` 里；禁用脚本的人靠 ``<noscript>`` 里那个按钮
    （脚本一开，``<noscript>`` 整块不渲染，所以按钮不会多出来）。
    """
    options = []
    # `offered()` 而不是 `locales()`：词典还没写的语言不出现（见那个函数的说明）。
    for item in i18n.offered():
        selected = " selected" if item["code"] == locale else ""
        # 未校对的语言明说自己是初译：并排放着而不加标记，等于替它担保。
        suffix = "" if item["reviewed"] else translate_text("（初译）", locale)
        options.append('<option value="%s"%s>%s%s</option>'
                       % (html.escape(item["code"], quote=True), selected,
                          html.escape(item["label"]), html.escape(suffix)))
    return (
        '<form class="lang-switch" method="get" action="" id="lang-switch-form">'
        '<label class="lang-switch-label" for="lang-switch">%s</label>'
        '<select id="lang-switch" name="%s">%s</select>'
        '<noscript><button type="submit">%s</button></noscript>'
        '</form>'
    ) % (html.escape(translate_text("语言", locale)), i18n.LANG_PARAM, "".join(options),
         html.escape(translate_text("切换", locale)))


def render_hreflang(path: str) -> str:
    """``hreflang`` 替代链接。

    没有它，搜索引擎只会看到其中一个语言版本——而「香港的同学搜到的是中文、
    交换生搜到的是英文」这件事，恰恰是这一页最该被搜到的两种样子。
    """
    return "\n".join(
        '<link rel="alternate" hreflang="%s" href="%s">'
        % (html.escape(item["hreflang"], quote=True), html.escape(item["href"], quote=True))
        for item in i18n.alternates(path)
    )


def _finish_page(text: str, path: str, locale: str) -> str:
    """每张公开页都要做的两件事：说清自己是什么语言、给出切换器。"""
    # `lang` 属性是屏幕阅读器选发音、浏览器选断行规则的依据。它是复制的，
    # 不是装饰：写错这一处，英文页面会按中文断行，读屏软件会读出怪音。
    text = re.sub(r'<html lang="[^"]*"', '<html lang="%s"' % locale, text, count=1)
    text = text.replace("{{LANG_SWITCH}}", render_language_switch(locale))
    text = text.replace("{{HREFLANG}}", render_hreflang(path))
    return text


def _with_language(request: Request, response: Response) -> Response:
    """地址栏里明确带了 ``?lang=`` 时，把这个选择记进 cookie。

    只认 GET/HEAD 上的显式选择：一个 POST 后面跟着的 ``?lang=`` 不是「用户选了
    语言」，而是某个表单碰巧带了这个字段——那样写 cookie，会让一次 API 调用
    悄悄改掉整个界面的语言。
    """
    explicit = (request.query.get(i18n.LANG_PARAM) or [""])[0]
    if explicit and i18n.is_supported(explicit) and request.method in {"GET", "HEAD"}:
        response.cookies.append(language_cookie(explicit))
    return response


#: 列表型参数在译文里的分隔符。中文/日文用顿号，英韩用逗号加空格。
#: 这**不是一条文案**（顿号在英文里就是错的），所以它是语言属性，不放进词典。
LIST_SEPARATORS = {"zh-Hans": "、", "zh-Hant": "、", "ja": "、"}


def _render_param(value: Any, locale: str) -> str:
    """把一个报错参数渲染成目标语言。

    字符串原样（调用方已经拼好了）；**列表表示「这些元素各自要过词典」**——
    典型是「身份只能是：本科生、研究生、其他」，那三个中文既是显示文字也是存库取值。
    """
    if isinstance(value, (list, tuple)):
        joiner = LIST_SEPARATORS.get(locale, ", ")
        return joiner.join(i18n.t(str(item), locale) for item in value)
    return str(value)


def fail(request: Request, status: int, message: str) -> Response:
    """dispatch 自己发出的那几个错误（404/405/…），同样按语言翻。"""
    return _with_language(request, error_response(status, i18n.t(message, page_locale(request))))


def page_locale(request: Request) -> str:
    """这次请求该用哪种语言。优先级见 :func:`pilot_app.i18n.negotiate`。

    ``?lang=`` 排在最前面，因为它是**这一次点击**的意思表示：一个人在英文页面上
    点了「简体中文」，不该因为我们从他的账号里读到别的偏好就把他按回去。
    """
    explicit = (request.query.get(i18n.LANG_PARAM) or [""])[0]
    if explicit and i18n.is_supported(explicit):
        return explicit
    return i18n.negotiate(
        request.header("Accept-Language"),
        request.cookie(i18n.LANG_COOKIE) or "",
        str(_visit_identity(request).get("ui_locale") or ""),
    )


def render_landing_page(target: Path, locale: str = i18n.DEFAULT_LOCALE) -> bytes:
    """Fill the landing page's live numbers.

    **这里曾经注入过一句「现在有 N 个账号接好了邮箱」。** 2026-09-24 随朋友那一版
    改版（PR #10）从页面上删掉了：官网不再公布账号数。所以那次查库与
    `{{PILOT_COUNT}}` 替换也一起撤掉 —— 留着它们是**每次渲染首页白查一次库**，
    而这正是我们清理布告栏残留时同一个毛病（占位符没了、代码还在）。

    `Database.landing_user_count` **保留着**（它的定义与那几条测试都还在）：那是
    「一个账号算不算用起来了」这件事的**唯一定义**，哪天要把数字放回页面（或放进
    应用里）就直接用它。哪天真的再放回官网，**记得把那条测试也带回来** ——
    `test_the_sentence_does_not_claim_mail_is_flowing`（2026-09-15 的事故：页面写
    「4 个在收信」而实际只有 3 个）当时随这句话一起删掉了，它守的规矩没变：
    **句子不许比数字说得更满**。
    """
    # **翻译模板在前、注入片段在后**。反过来的话，那些片段里的中文会被当成模板
    # 的一部分，而它们带标签，匹配不上任何一条译文（key 是中文原文）。
    text = i18n.translate_file(target, locale)
    text = text.replace("{{SOURCE_LINK}}", render_source_link(locale))
    # The nav entry and the section are decided by the same condition as the
    # footer link, so a copy of this software without a repository configured
    # renders neither.
    text = text.replace("{{SOURCE_NAV}}", render_source_nav(locale))
    text = text.replace("{{SOURCE_SECTION}}", render_source_section(locale))
    # 客服群那张码（可选；见 `render_wechat_section`）。它插在申请那一节之后、
    # 留言板之前——「找到我们」的两条路挨着放。
    text = text.replace("{{WECHAT_GROUP}}", render_wechat_section(locale=locale))
    # The install instructions are prose and live in the template; only the
    # button is live, because whether this server has an APK at all is a fact
    # about the machine rather than something the page can assert.
    text = text.replace("{{APK_BUTTON}}", render_apk_button(locale))
    text = text.replace("{{GUESTBOOK}}", render_guestbook(get_db().published_guest_messages(20), locale))
    return _finish_page(text, target.name, locale).encode("utf-8")


def public_stamp(value: str | None) -> str:
    """A stored UTC timestamp with the local offset spelled out."""
    local = reports_mod.to_local(value, None)
    if local is None:
        return ""
    offset = local.utcoffset() or dt.timedelta(hours=8)
    total = int(offset.total_seconds())
    hours, minutes = divmod(abs(total) // 60, 60)
    marker = f"GMT{'+' if total >= 0 else '-'}{hours}"
    if minutes:
        marker += f":{minutes:02d}"
    return f"{local.month}月{local.day}日 {local:%H:%M} ({marker})"


def render_guestbook(rows: list[dict[str, Any]], locale: str = i18n.DEFAULT_LOCALE) -> str:
    """The published messages on the landing page, or a line saying there are none.

    Unlike the bulletin board this section always renders, because the form under
    it is the point: a visitor who is about to write something should be able to
    see that the board exists and is read. An empty board says so in words rather
    than showing a heading over nothing.

    Every field is escaped here and only here -- the template receives finished
    markup. A message is untrusted text from a stranger, and this is the one path
    where it reaches HTML, so there is no second place to get it wrong.

    **译文只加在我们自己写的句子上**：留言正文和昵称是用户写的，一个字都不改
    （「留言不等于注册」是隐私政策里的承诺，替用户改口供比不翻译严重得多）。
    """
    parts = ['<ul class="guestlist">']
    if not rows:
        parts.append('<li class="guest-empty">%s</li>' % html.escape(translate_text(
            "还没有公开的留言。你写的那条会先给运营者看，通过后才会匿名刊登在这里。", locale)))
    for row in rows:
        name = str(row.get("nickname") or "").strip() or translate_text("一位同学", locale)
        stamp = public_stamp(row.get("decided_at") or row.get("created_at"))
        parts.append('<li class="guest-item">')
        parts.append(f'<p class="guest-body">{html.escape(str(row.get("body") or ""))}</p>')
        parts.append(f'<p class="guest-meta">{html.escape(name)}'
                     + (f' · {html.escape(stamp)}' if stamp else "") + "</p>")
        parts.append("</li>")
    parts.append("</ul>")
    return "\n".join(parts)


# The published source repository. Optional, because most copies of this software
# are somebody's own installation, and a footer pointing at a repository its owner
# does not control would be a link to a stranger's code.
SOURCE_URL_ENV = "INFE_PILOT_SOURCE_URL"


def source_url() -> str:
    """The operator's public source repository, or "" when unset.

    AGPL-3.0 section 13 is why this exists at all: running modified software as a
    network service obliges the operator to offer *that* source to the people
    using it. A footer link is the cheapest honest way to do that -- and it also
    satisfies the licence for the unmodified case, where the obligation is easy to
    forget precisely because nothing was changed.
    """
    raw = (os.environ.get(SOURCE_URL_ENV) or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    # Only http(s). A `javascript:` value here would turn an operator's typo into
    # a link that runs code in every visitor's browser, on the public page.
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        logging.warning("%s 不是 http(s) 地址，已忽略", SOURCE_URL_ENV)
        return ""
    return raw[:300]


def render_source_link(locale: str = i18n.DEFAULT_LOCALE) -> str:
    """The footer link, or nothing at all when no repository is configured."""
    url = source_url()
    if not url:
        return ""
    return (f'<a href="{html.escape(url, quote=True)}" target="_blank" '
            f'rel="noopener">{html.escape(translate_text("源代码（AGPL-3.0）", locale))}</a> · ')


def render_source_nav(locale: str = i18n.DEFAULT_LOCALE) -> str:
    """The landing page's nav entry, or nothing. Jumps to the section below."""
    if not source_url():
        return ""
    return '<a href="#source">%s</a>' % html.escape(translate_text("开源", locale))


#: 客服群二维码（运营者上传）。**两个都要配**才渲染：图片路径 + 有效日期。
#: 为什么要日期：微信群的码**只有 7 天**，一张过期的码挂在公开页面上是一次静默失败——
#: 访客扫了没反应，我们这边一点动静都没有。所以到期当天起，这一节只留一句人话。
WECHAT_IMG_ENV = "INFE_PILOT_WECHAT_GROUP_IMG"
WECHAT_UNTIL_ENV = "INFE_PILOT_WECHAT_GROUP_UNTIL"


def render_wechat_section(*, now: Optional[dt.datetime] = None,
                          locale: str = i18n.DEFAULT_LOCALE) -> str:
    """「扫码进群」那一节，或者一句「码过期了」。

    * **没配图片 → 整节不出现**（自建的人不该把我们的群挂到他的站上，与
      `render_source_section` 同一条规矩）。
    * **配了图片、日期还没到 →** 出二维码 + 一句「几天内有效」。
    * **日期过了（或日期读不出来）→ 不出图**，改出一句指路的话（留言板 + 联系邮箱）。
      读不出日期时按「过期」处理而不是按「永久」：猜错的方向只能是让访客去留言，
      不能是让他扫一个可能已经作废的码。

    译文里的 ``{link}`` / ``{contact}`` 是**结构占位符**：链接与收件地址不进译文
    （译者不该、也不该被要求去维护一个邮件地址），翻译整句、再把它们换回来。
    """
    image = (os.environ.get(WECHAT_IMG_ENV) or "").strip()
    if not image:
        return ""
    today = (now or dt.datetime.now(dt.timezone.utc)).astimezone(
        dt.timezone(dt.timedelta(hours=8))).date()
    try:
        until = dt.date.fromisoformat((os.environ.get(WECHAT_UNTIL_ENV) or "").strip())
    except ValueError:
        until = None
    contact = contact_email()
    board = '<a href="#guestbook">%s</a>' % _say("留言", locale)
    if contact:
        body = _say("客服群的二维码到期了（微信的群码只有 7 天，我们每 7 天换一张）。"
                    "想找我们，在下面{link}，或写信到 {contact}。", locale)
        body = body.replace("{contact}", '<a href="mailto:%s">%s</a>'
                            % (html.escape(contact), html.escape(contact)))
    else:
        body = _say("客服群的二维码到期了（微信的群码只有 7 天，我们每 7 天换一张）。"
                    "想找我们，在下面{link}。", locale)
    fallback = '<p class="note">%s</p>' % body.replace("{link}", board)
    # 这一节的标题**要包一层 `<div>`**：新版设计稿的 `.section-head` 是两栏网格
    # （`1.1fr .9fr`，第一格放标题、第二格放引导句）。原来 kicker 与 h2 是并列的两个
    # 子元素，于是 h2 被放进第二格、跑到右边去，标题看起来被拆成两半
    # （2026-09-23 在线上截图里看到）。包起来之后与页面里其它各节同一个形状。
    if until is None or today > until:
        return ('<hr class="rule">\n\n'
                '<section class="block reveal" id="wechat" aria-labelledby="wechat-title">\n'
                '  <div class="wrap">\n'
                '    <div class="shell lift">\n'
                '      <div class="core wechat-core is-expired">\n'
                '        <div class="wechat-copy">\n'
                '          <p class="kicker">%s</p>\n'
                '          <h2 id="wechat-title">%s</h2>\n'
                '          %s\n'
                '        </div>\n'
                '      </div>\n'
                '    </div>\n'
                '  </div>\n'
                '</section>\n') % (
                    _say("找到我们", locale), _say("扫码进群", locale), fallback)
    days = (until - today).days
    when = (translate_text("{month} 月 {day} 日前", locale, month=until.month, day=until.day)
            if days else translate_text("今天之内", locale))
    return (
        '<hr class="rule">\n\n'
        '<section class="block reveal" id="wechat" aria-labelledby="wechat-title">\n'
        '  <div class="wrap">\n'
        '    <div class="shell lift">\n'
        '      <div class="core wechat-core">\n'
        '        <div class="wechat-copy">\n'
        '          <p class="kicker">%s</p>\n'
        '          <h2 id="wechat-title">%s</h2>\n'
        '          <p class="note">%s<b>%s</b>%s</p>\n'
        '        </div>\n'
        '        <div class="wechat-code">\n'
        # 尺寸写成**这张图自己的比例**（966×1482 的那张群卡缩到 280 宽就是 430 高）：
        # 原来这里写死 280×300，是给更方的那张旧码留的框；图一换，浏览器预留的
        # 位置就比真图矮一截，图片落下来时那一节会跳一下。CSS 里是 `height:auto`，
        # 所以这两个属性只影响「图到之前占多高」。
        '          <img class="group-qr" src="%s" width="280" height="430"\n'
        '               alt="%s" loading="lazy">\n'
        '        </div>\n'
        '      </div>\n'
        '    </div>\n'
        '  </div>\n'
        '</section>\n') % (
            _say("找到我们", locale), _say("扫码进群", locale),
            _say("用微信扫一下进客服群，随时问。", locale),
            _say("这张码 {when}有效", locale, when=when),
            _say("（微信的群码只有 7 天），过期了就用下面的留言板。", locale),
            html.escape(image, quote=True),
            html.escape("CityU Mail Pilot " + translate_text("客服群二维码", locale)))


def render_source_section(locale: str = i18n.DEFAULT_LOCALE) -> str:
    """「源代码公开」那一整节，或者什么都没有。

    A footer link was not enough: the operator asked for the fact to be *on the
    page*, and a line of muted text at the bottom is where facts go to be
    unread. It is also the honest place for the AGPL-3.0 argument -- a reader
    who is about to hand us their mail password deserves to be told, in the body
    of the page rather than in a footer, that the code doing it can be read.

    **All of it disappears when no repository is configured.** A self-hosted
    copy must not point its visitors at somebody else's source, and that is the
    same rule the footer link and the app shell already follow.
    """
    url = source_url()
    if not url:
        return ""
    safe = html.escape(url, quote=True)

    # 2026-09-22 收下 PR #5 的 ③：他那一版更短，而且把「你可以自己核对」写在了
    # 按钮上。**emoji 去掉了** —— 首页此前刻意不用 emoji（`landing_check` 与
    # 文案评审都按「没有 emoji」看），只保留他那句话本身。
    #
    # `{repo}` 是**结构占位符**：链接地址不进译文。
    return (
        '<section id="source">\n'
        '  <h2>%s</h2>\n'
        '  <p>%s</p>\n'
        '  <p class="repo"><a class="cta" href="%s" target="_blank" '
        'rel="noopener noreferrer">%s</a></p>\n'
        '  <p>%s<code>%s</code></p>\n'
        '</section>\n\n  '
    ) % (
        _say("开源与信任", locale),
        _say("项目采用 {license} 许可证，源代码公开在 GitHub：", locale).replace(
            "{license}", "<b>AGPL-3.0</b>"),
        safe,
        _say("担心代码偷窥隐私？我们的代码是公开开源的，你可以自己检查", locale),
        _say("也可以直接访问源码地址：", locale),
        html.escape(url),
    )


# --------------------------------------------------------------------------
# The Android package (a sideloadable APK), and the proof that it is ours
# --------------------------------------------------------------------------

# The APK is a *build artifact*, so it does not live in `static/`. Everything
# under `pilot_app/` ends up in the release tarball, the offline source snapshot
# and the publication export, and a signed multi-megabyte binary has no business
# in any of the three: it is not source, it is rebuilt without the code changing,
# and the export tool would have to grow yet another exclusion to keep a blob out
# of the public tree. Beside the database it is outside all three by construction.
DOWNLOAD_DIR_ENV = "INFE_PILOT_DOWNLOAD_DIR"
DEFAULT_DOWNLOAD_DIR = Path("/var/lib/cityu-mail-pilot/download")
APK_FILENAME = "cityu-mail-pilot.apk"
APK_ROUTE = "/download/" + APK_FILENAME
APK_MEDIA_TYPE = "application/vnd.android.package-archive"

# Chrome only opens an installed package without its address bar if the site
# proves it owns that package, by serving a Digital Asset Links statement from
# this exact well-known path. Unverified, the app still runs but shows a URL bar,
# which is indistinguishable from a browser shortcut -- so the page tells the
# reader how to check rather than promising a chrome-free window it may not get.
ASSETLINKS_PATH = "/.well-known/assetlinks.json"
ANDROID_PACKAGE_ENV = "INFE_PILOT_ANDROID_PACKAGE"
ANDROID_FINGERPRINT_ENV = "INFE_PILOT_ANDROID_FINGERPRINT"

# `com.example.app` shape. Only used to reject nonsense early: the value goes
# into a JSON document this server publishes about somebody else's app, and an
# operator typo there is a claim about a package that is not ours.
_PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$")


def download_dir() -> Path:
    raw = (os.environ.get(DOWNLOAD_DIR_ENV) or "").strip()
    return Path(raw) if raw else DEFAULT_DOWNLOAD_DIR


def apk_path() -> Optional[Path]:
    """The published APK when the operator has put one there, else None.

    Existence *is* the switch, and that is the point: a page offering a download
    that 404s is worse than a page offering none, and most copies of this
    software -- anything self-hosted -- will never have an APK at all, because
    the package is bound to one domain and one signing key.
    """
    try:
        target = (download_dir() / APK_FILENAME).resolve()
    except OSError:  # pragma: no cover - a path the OS refuses to resolve
        return None
    return target if target.is_file() else None


def android_fingerprint() -> str:
    """The signing certificate's SHA-256 as Asset Links wants it, or "".

    `keytool -list -v` prints it colon-separated and `gradle signingReport` does
    not, in either case, so both spellings are accepted and normalised rather
    than making the operator reformat a 95-character string by hand.
    """
    raw = (os.environ.get(ANDROID_FINGERPRINT_ENV) or "").strip()
    if not raw:
        return ""
    hexed = re.sub(r"[^0-9A-Fa-f]", "", raw).upper()
    if len(hexed) != 64:
        logging.warning("%s 不是 SHA-256（需要 64 位十六进制），已忽略", ANDROID_FINGERPRINT_ENV)
        return ""
    return ":".join(hexed[index:index + 2] for index in range(0, 64, 2))


def assetlinks_document() -> Optional[bytes]:
    """The Digital Asset Links statement, or None when there is nothing to claim.

    Absent configuration means *no document*, not an empty one. This file asserts
    that a named Android package is this site, and a copy of the software that
    has not built its own APK must not make that assertion about an app it does
    not control -- so a self-hoster who sets nothing publishes nothing.
    """
    fingerprint = android_fingerprint()
    package = (os.environ.get(ANDROID_PACKAGE_ENV) or "").strip()
    if not fingerprint or not package:
        return None
    if not _PACKAGE_RE.match(package):
        logging.warning("%s 不是合法的安卓包名，已忽略", ANDROID_PACKAGE_ENV)
        return None
    return json.dumps([{
        "relation": ["delegate_permission/common.handle_all_urls"],
        "target": {
            "namespace": "android_app",
            "package_name": package,
            "sha256_cert_fingerprints": [fingerprint],
        },
    }], ensure_ascii=False, indent=2).encode("utf-8")


def _human_size(count: int) -> str:
    """A file size the way a download page should print it."""
    if count >= 1024 * 1024:
        return f"{count / (1024 * 1024):.1f} MB"
    return f"{max(1, round(count / 1024))} KB"


def render_apk_button(locale: str = i18n.DEFAULT_LOCALE) -> str:
    """The Android download button, or a sentence saying there is not one.

    Both states are true ones. The empty state is not an error: a self-hosted
    copy has no APK by definition, so the page falls back to describing the
    browser route -- which works on every Android phone -- instead of leaving a
    dead button behind.
    """
    target = apk_path()
    if target is None:
        return '<p class="note">%s</p>' % _say("这台服务器上没有准备好安卓安装包，用下面的「添加到主屏幕」一样能装。", locale)
    try:
        size = _human_size(target.stat().st_size)
    except OSError:  # pragma: no cover - removed between the check and the stat
        size = ""
    label = (_say("下载安卓安装包（{size}）", locale, size=size) if size
             else _say("下载安卓安装包", locale))
    return (f'<div class="dl"><a class="btn" href="{APK_ROUTE}" download '
            f'id="apk-download">{label}</a></div>')


# --------------------------------------------------------------------------
# request
# --------------------------------------------------------------------------


class Request:
    def __init__(
        self,
        *,
        method: str,
        path: str,
        query: dict[str, list[str]],
        headers: Any,
        body: bytes,
        client: str,
    ) -> None:
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body
        self.client = client
        self.user: Optional[dict[str, Any]] = None

    @property
    def origin(self) -> str:
        return (self.headers.get("Origin") or "").rstrip("/")

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name) or default

    def cookie(self, name: str) -> Optional[str]:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            jar = SimpleCookie()
            jar.load(raw)
        except Exception:
            return None
        morsel = jar.get(name)
        return morsel.value if morsel else None

    def json_object(self) -> dict[str, Any]:
        if not self.body:
            raise ApiError(422, i18n.mark("请求缺少 JSON 内容。"))
        try:
            payload = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(422, i18n.mark("请求内容不是合法的 JSON。")) from exc
        if not isinstance(payload, dict):
            raise ApiError(422, i18n.mark("请求内容必须是 JSON 对象。"))
        return payload

    def query_int(self, name: str, default: int) -> int:
        values = self.query.get(name)
        if not values:
            return default
        try:
            return int(values[0])
        except (TypeError, ValueError):
            return default


# --------------------------------------------------------------------------
# validated input helpers (stand-ins for the previous pydantic models)
# --------------------------------------------------------------------------


def _field(payload: dict[str, Any], name: str, default: Any = None) -> Any:
    value = payload.get(name, default)
    return default if value is None and default is not None else value


def _string(
    payload: dict[str, Any],
    name: str,
    *,
    default: Optional[str] = None,
    minimum: int = 0,
    maximum: int = 1000,
    required: bool = True,
) -> str:
    value = payload.get(name, default)
    if value is None:
        if required:
            raise ApiError(422, i18n.mark("缺少字段 {name}。"), {"name": name})
        return ""
    if not isinstance(value, str):
        raise ApiError(422, i18n.mark("字段 {name} 必须是文字。"), {"name": name})
    if len(value) < minimum:
        raise ApiError(422, i18n.mark("字段 {name} 太短。"), {"name": name})
    if len(value) > maximum:
        raise ApiError(422, i18n.mark("字段 {name} 过长。"), {"name": name})
    return value


def _boolean(payload: dict[str, Any], name: str, default: bool) -> bool:
    value = payload.get(name, default)
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    raise ApiError(422, i18n.mark("字段 {name} 必须是布尔值。"), {"name": name})


def _port(payload: dict[str, Any], name: str, default: Optional[int] = None) -> int:
    value = payload.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(422, i18n.mark("字段 {name} 必须是端口号。"), {"name": name})
    if not 1 <= value <= 65535:
        raise ApiError(422, i18n.mark("字段 {name} 必须在 1-65535 之间。"), {"name": name})
    return value


def _string_list(payload: dict[str, Any], name: str, *, maximum_items: int, item_maximum: int = 200) -> list[str]:
    value = payload.get(name, [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise ApiError(422, i18n.mark("字段 {name} 必须是列表。"), {"name": name})
    if len(value) > maximum_items:
        raise ApiError(422, i18n.mark("字段 {name} 的条目过多。"), {"name": name})
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ApiError(422, i18n.mark("字段 {name} 只能包含文字。"), {"name": name})
        if len(item) > item_maximum:
            raise ApiError(422, i18n.mark("字段 {name} 的单个条目过长。"), {"name": name})
        cleaned = item.strip()
        if cleaned:
            result.append(cleaned)
    return result


def _config(payload: dict[str, Any], name: str) -> dict[str, Any]:
    value = payload.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ApiError(422, i18n.mark("字段 {name} 必须是对象。"), {"name": name})
    if len(value) > MAX_JSON_DEPTH_ITEMS:
        raise ApiError(422, i18n.mark("字段 {name} 的条目过多。"), {"name": name})
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, (str, int, float, bool)):
            raise ApiError(422, i18n.mark("字段 {name} 只支持简单的键值对。"), {"name": name})
    return dict(value)


def _email(value: str) -> str:
    result = value.strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", result):
        raise ApiError(422, i18n.mark("邮箱地址格式不正确。"))
    return result


def _cityu_email(value: str) -> str:
    """Validate an optional CityU school mailbox identity."""
    value = value.strip()
    if not value:
        return ""
    result = _email(value)
    domain = result.rsplit("@", 1)[1]
    if not (domain == "cityu.edu.hk" or domain.endswith(".cityu.edu.hk")):
        raise ApiError(422, "学校邮箱必须使用 CityU 域名（例如 @my.cityu.edu.hk）。")
    return result


# --------------------------------------------------------------------------
# process-wide state
# --------------------------------------------------------------------------

_db_singleton: Optional[Database] = None
_service_singleton: Optional[PilotService] = None
_state_lock = threading.Lock()
_login_attempts: dict[str, list[float]] = {}
_attempt_lock = threading.Lock()


def get_db() -> Database:
    global _db_singleton
    with _state_lock:
        if _db_singleton is None:
            database = Database(os.environ.get("INFE_PILOT_DB", "/var/lib/cityu-mail-pilot/pilot.sqlite3"))
            database.initialize()
            _db_singleton = database
        return _db_singleton


def get_service() -> PilotService:
    global _service_singleton
    database = get_db()  # taken before the lock: get_db() locks the same mutex
    with _state_lock:
        if _service_singleton is None:
            _service_singleton = PilotService(database, SecretBox.from_environment())
        return _service_singleton


def _visit_identity(request: Request) -> dict[str, Any]:
    """这份请求背后是谁（如果登录了的话）。

    必须在**记录访问时**自己解一次会话：`request.user` 只有那些要求登录的处理器
    才会填，而一个人打开首页（`/`）时它还是 None——于是「登录用户」和「运营者」
    两个标记在最重要的一条路径上永远是假的（这也是为什么运营者自己的浏览被算成
    了访客）。这里按 cookie 查一次，答不上来就按匿名处理。
    """
    if request.user:
        return request.user
    token = request.cookie(SESSION_COOKIE)
    if not token:
        return {}
    try:
        return get_db().session_user(token_hash(token)) or {}
    except Exception:  # pragma: no cover - 统计数据不该让页面出错
        return {}


def _record_visit(request: Request, response: Response) -> None:
    """Count one page view. Never raises, and never slows a page down much.

    Called for every response, and returns immediately for the ones that are not
    page views (the API, static files, health probes, 404s). Everything it can
    fail at -- an unwritable database, a missing country database -- is inside
    ``analytics.record``, because this runs in the request a real person is
    waiting for and statistics must never be able to break a page.

    ``DNT``/``Sec-GPC`` are honoured here rather than inside ``record`` so that
    the decision is visible at the point where the request context is available.
    """
    try:
        if analytics_mod.wants_no_tracking(request.headers):
            return
        visitor = _visit_identity(request)
        analytics_mod.record(
            get_db(),
            get_service().secrets,
            ip=request.client or "",
            path=request.path,
            status=int(getattr(response, "status", 0) or 0),
            method=request.method,
            referrer=request.header("Referer"),
            user_agent=request.header("User-Agent"),
            member=bool(visitor),
            admin=bool(_is_admin(visitor)) if visitor else False,
            own_host=request.header("Host"),
        )
    except Exception:  # pragma: no cover - defensive
        logging.debug("visit not recorded", exc_info=True)


def _client_label(request: Request) -> str:
    """What may be written down about who made a request: a keyed digest.

    Every persisted client value goes through here -- audit rows, pilot
    applications -- for the same reason the message board does it: recognising
    the same client again never requires the address itself, and a bare hash of
    an IPv4 address is not anonymous because the whole space is enumerable. The
    address is still available where it is genuinely needed (rate limiting, the
    live visitor view) because those never leave memory.

    Nothing is lost for an operator investigating an incident: nginx keeps its
    own access log with full addresses, and that log is not copied into the
    database or into the daily off-site backups.
    """
    raw = str(getattr(request, "client", "") or "")
    if not raw:
        return ""
    try:
        return get_service().secrets.anonymized(raw)
    except Exception:  # pragma: no cover - a request must not fail over a label
        return ""


def __getattr__(name: str) -> Any:  # pragma: no cover - module attribute plumbing
    """Keep ``from pilot_app.web import db, service`` working lazily."""
    if name == "db":
        return get_db()
    if name == "service":
        return get_service()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _rate_limit(key: str, *, failed: bool = False) -> None:
    """Small single-worker login throttle; the reverse proxy adds the IP limit."""
    now = time.monotonic()
    with _attempt_lock:
        recent = [value for value in _login_attempts.get(key, []) if now - value < 900]
        if len(recent) >= 8:
            raise ApiError(429, i18n.mark("登录尝试过多，请 15 分钟后再试。"))
        if failed:
            recent.append(now)
        _login_attempts[key] = recent


# The public application form needs its own budget: the login throttle is
# sized for a person mistyping a password, not for a stranger filling in a form.
#
# 2026-09-22: **同一个计数器现在也管注册**（`register()`）。开放注册之后限速是唯一的
# 防批量建号闸门，而复用它比另写一套好：同一 IP 每小时 5 次，与当年的申请书同一个预算。
# 见 `docs/open-registration-2026-09-22.md` 决定 4。
_signup_attempts: dict[str, list[float]] = {}


def _signup_rate_limit(client: str) -> None:
    now = time.monotonic()
    key = f"signup:{client}"
    with _attempt_lock:
        recent = [value for value in _signup_attempts.get(key, []) if now - value < 3600]
        if len(recent) >= 5:
            raise ApiError(429, i18n.mark("提交过于频繁，请一小时后再试。"))
        recent.append(now)
        _signup_attempts[key] = recent


def reset_signup_rate_limit() -> None:
    """忘掉内存里所有的注册/申请计数。

    **这是给测试与预演用的，没有任何路由能碰到它。** 单测在一个进程里从一个地址
    （127.0.0.1）注册的账号远多于任何真实客户端 —— 一个套件跑十个用例就会撞上
    第六次，而那条限速本身是对的。所以套件在注册前自己清一次，限速的判据由
    `test_signup.SignupRateLimitTests` 专门验（它**不清**）。
    """
    with _attempt_lock:
        _signup_attempts.clear()


# The public message board is a second unauthenticated write, so it gets its own
# budget rather than sharing the application form's. Same shape as the signup
# throttle on purpose: one person writing three messages is normal, a script
# writing thirty is not.
_guestbook_attempts: dict[str, list[float]] = {}
GUESTBOOK_RATE_LIMIT = 5
GUESTBOOK_MIN_SECONDS = 3

# 「看原信」每次点击都要**真开一次 IMAP 连接**。它不写任何东西，所以没有数据风险，
# 但 2 核 2G 的机器上它是最贵的一次点击，而且连的是用户自己的邮箱——把人家的邮箱
# 敲到被服务商限流，比这个功能本身坏掉更糟。所以按人限：10 分钟 20 次。
_original_attempts: dict[str, list[float]] = {}
ORIGINAL_RATE_LIMIT = 20
ORIGINAL_WINDOW_SECONDS = 600


# --------------------------------------------------------------------------- #
# 连接测试：每一次点击都是一次真实的供应商调用
# --------------------------------------------------------------------------- #
#
# 走平台兜底 key 时这笔钱是**运营者出的**，而这颗按钮就摆在设置页上：连点它能烧钱，
# 在别人正跑着的时候点它能挤占线程。这个文件里已经有七个限流器，这里不造第八套——
# 同一把 `_attempt_lock`、同一个滑动窗口，再加两道并发闸门：
#
# ① 每用户每分钟 `TEST_RATE_LIMIT` 次；
# ② 同一个用户同时只允许一次（上一次没出结果就再点，多半是卡住了在乱点）；
# ③ 全局同时最多 `TEST_MAX_INFLIGHT` 次（每个测试都占一条出站连接与一份额度）。
#
# 三个数字是**建议初值**（2026-09-22 那份审查报告提的就是这几个），要改改这里，
# 改之前先想清楚刷它的成本：平台 key 是运营者付钱的。
_test_attempts: dict[str, list[float]] = {}
TEST_RATE_LIMIT = 3
TEST_WINDOW_SECONDS = 60
TEST_MAX_INFLIGHT = 2
_test_inflight: set[str] = set()


@contextlib.contextmanager
def _connection_test_slot(user_id: str):
    """占一个连接测试的名额；`finally` 一定放行，否则一次失败会永久占住名额。"""
    now = time.monotonic()
    with _attempt_lock:
        recent = [value for value in _test_attempts.get(user_id, [])
                  if now - value < TEST_WINDOW_SECONDS]
        if len(recent) >= TEST_RATE_LIMIT:
            raise ApiError(429, f"连接测试太频繁了——每分钟最多 {TEST_RATE_LIMIT} 次，稍等一下再试。")
        if user_id in _test_inflight:
            raise ApiError(429, "上一次连接测试还没出结果，等它回来再点。")
        if len(_test_inflight) >= TEST_MAX_INFLIGHT:
            raise ApiError(429, "现在有别的连接测试在跑，几秒后再试。")
        recent.append(now)
        _test_attempts[user_id] = recent
        _test_inflight.add(user_id)
    try:
        yield
    finally:
        with _attempt_lock:
            _test_inflight.discard(user_id)


def _original_rate_limit(user_id: str) -> None:
    now = time.monotonic()
    with _attempt_lock:
        recent = [value for value in _original_attempts.get(user_id, [])
                  if now - value < ORIGINAL_WINDOW_SECONDS]
        if len(recent) >= ORIGINAL_RATE_LIMIT:
            raise ApiError(429, "看原信看得很勤——歇一会儿再点（十分钟内最多 20 次）。")
        recent.append(now)
        _original_attempts[user_id] = recent


# 翻译 / 总结：**每一次点击都是一次真实的模型调用**（走平台 key 时是运营者出钱），
# 所以限得比「看原信」紧：一小时 20 次。一小时二十次够一个人读完今天的信了；
# 脚本刷它会在半个小时里烧掉一笔钱，而那时用户自己还不知道。
_assist_attempts: dict[str, list[float]] = {}
ASSIST_RATE_LIMIT = 20
ASSIST_WINDOW_SECONDS = 3600


def _assist_rate_limit(user_id: str) -> None:
    now = time.monotonic()
    with _attempt_lock:
        recent = [value for value in _assist_attempts.get(user_id, [])
                  if now - value < ASSIST_WINDOW_SECONDS]
        if len(recent) >= ASSIST_RATE_LIMIT:
            raise ApiError(429, "翻译/总结用得有点密——歇一会儿再点（一小时最多 20 次）。")
        recent.append(now)
        _assist_attempts[user_id] = recent


# 「去邮箱里看」的兜底链接。**它只到收件箱，精确不到某一封**：QQ/163 的网页版没有
# 稳定的单封地址，硬拼一个只会把用户送到登录页或者空白页。所以这里只谈「哪儿能看信」，
# 不谈「就是这一封」——做不到的事不要在界面上暗示做得到。
WEBMAIL_HOMES = (
    ("qq.com", "https://mail.qq.com/"),
    ("foxmail.com", "https://mail.qq.com/"),
    ("163.com", "https://mail.163.com/"),
    ("126.com", "https://mail.126.com/"),
    ("gmail.com", "https://mail.google.com/"),
    ("googlemail.com", "https://mail.google.com/"),
    ("outlook.com", "https://outlook.live.com/mail/"),
    ("hotmail.com", "https://outlook.live.com/mail/"),
    ("live.com", "https://outlook.live.com/mail/"),
    ("cityu.edu.hk", "https://outlook.office.com/mail/"),
)


def webmail_home(email: str) -> str:
    """Where this person's mailbox lives on the web, or ``""`` if we don't know."""
    address = (email or "").strip().lower()
    domain = address.rsplit("@", 1)[-1] if "@" in address else ""
    for suffix, url in WEBMAIL_HOMES:
        if domain == suffix or domain.endswith("." + suffix):
            return url
    return ""


# 学校邮箱（CityU 是 Microsoft 365）。同一个地址在设置向导第 2 步也用了，
# 所以它只写这一处。
SCHOOL_WEBMAIL = "https://outlook.office.com/mail/"


def gmail_message_url(email: str, message_key: str) -> str:
    """Gmail 里**那一封**的直接地址，做不到就返回空串。

    只有 Gmail 有这种办法：它支持按 RFC 5322 的 `Message-ID` 搜一封
    （`#search/rfc822msgid:<id>`），而我们正好留着这个值（`messages.message_key`，
    本来就是拿它去重的）。QQ/163 没有稳定的单封地址；Outlook 网页版连「复制邮件链接」
    都不是每个租户都有——微软自己的问答里，提问者就回帖说他的租户里根本没有那个选项。
    所以**只有这一家**给深链，其余给收件箱。
    """
    key = (message_key or "").strip()
    if not key or "gmail" not in (email or "").lower():
        return ""
    return "https://mail.google.com/mail/u/0/#search/rfc822msgid%3A" + quote(key, safe="")


def original_links(mailbox_email: str, school_email: str, message_key: str,
                   *, school_mail: bool = False) -> list[dict[str, str]]:
    """「这封信还能去哪儿看」——**一处定义**，每条都说清能精确到什么程度。

    界面不该暗示做不到的事：这里是「到收件箱」还是「到那一封」，`detail` 里逐条写明。

    ``school_mail`` = 我们**知道**这封信是从学校邮箱转过来的（发件域是 CityU）。
    学校那一格以前只按「用户填过学校邮箱吗」决定，于是没填资料的人根本看不到它——
    而早期反馈里那位用户要的正是这一格（原话「能不能在看原件的地方直接跳到 outlook
    的学校邮箱」）。邮件本身就是证据，不该再要求他先填一遍。
    """
    links: list[dict[str, str]] = []
    if (school_email or "").strip() or school_mail:
        links.append({"label": "学校邮箱（Outlook 网页版）", "url": SCHOOL_WEBMAIL,
                      "detail": "原件在学校邮箱里；打开后到收件箱，用下面「复制主题」粘进搜索框"})
    home = webmail_home(mailbox_email)
    if home:
        links.append({"label": "转发邮箱的收件箱", "url": home,
                      "detail": "转过来的那一封在这里"})
    exact = gmail_message_url(mailbox_email, message_key)
    if exact:
        links.append({"label": "在 Gmail 里打开这一封", "url": exact,
                      "detail": "按邮件 ID 直接定位，不用自己翻"})
    return links


def _guestbook_rate_limit(client: str) -> None:
    now = time.monotonic()
    key = f"guestbook:{client}"
    with _attempt_lock:
        recent = [value for value in _guestbook_attempts.get(key, []) if now - value < 3600]
        if len(recent) >= GUESTBOOK_RATE_LIMIT:
            raise ApiError(429, i18n.mark("留言提交过于频繁，请一小时后再试。"))
        recent.append(now)
        _guestbook_attempts[key] = recent


# 2026-09-22：**「我没收到邀请码」那个未认证写入已下线**（注册完全开放之后，自助重发
# 没有意义了）。连同它的按 IP 计数器（`_resend_rate_limit`）与那份恒定的回执一起删掉 ——
# 端点不在了，回执自然也没有存在的理由。**worker 那一半留着**（`pilot_app/invites.py`
# 的 `process_resend_queue`）：队列里可能还有历史行，而且它不会给任何人发新码。
# 见 `docs/open-registration-2026-09-22.md`。


def _clear_attempts(key: str) -> None:
    with _attempt_lock:
        _login_attempts.pop(key, None)


_verify_lock = threading.Lock()
_verify_recent: dict[str, float] = {}
_admin_actions: dict[str, list[float]] = {}


def _verification_allowed(user_id: str) -> bool:
    """Throttle explicit IMAP checks so a user cannot hammer a mail provider.

    Opening a mailbox is a real outbound connection; without this a stuck page
    could lock the account out of its own provider.
    """
    now = time.monotonic()
    with _verify_lock:
        last = _verify_recent.get(user_id, 0.0)
        if now - last < 60:
            return False
        _verify_recent[user_id] = now
        if len(_verify_recent) > 500:
            cutoff = now - 600
            for key in [key for key, value in _verify_recent.items() if value < cutoff]:
                _verify_recent.pop(key, None)
        return True


def _cookie_flags() -> str:
    secure = os.environ.get("INFE_PILOT_COOKIE_SECURE", "1") != "0"
    return "; Secure" if secure else ""


def _session_cookie(user_id: str) -> str:
    token = new_token()
    expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=SESSION_DAYS)
    get_db().create_session(user_id, token_hash(token), expires.isoformat(timespec="seconds"))
    return (
        f"{SESSION_COOKIE}={token}; Path=/; Max-Age={SESSION_DAYS * 86400}; "
        f"HttpOnly; SameSite=Lax{_cookie_flags()}"
    )


def _expired_cookie() -> str:
    return f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax{_cookie_flags()}"


MANIFEST_PATH = "/manifest.webmanifest"


def render_manifest(request: Request) -> str:
    """The install metadata, coloured for whoever is asking.

    The manifest is fetched by the browser (and, for the Android splash, kept by
    the OS at install time), and the theme is a per-account setting that lives on
    the server -- so the only way the splash can match the app is if this is
    rendered per request. Chromium will not send the session cookie for a
    manifest unless the <link> carries ``crossorigin="use-credentials"``; that is
    why index.html has it, and ``tools/shell_check.js`` asserts the manifest the
    browser actually parses.

    Signed out is the normal case for a first visit, and it gets the default
    theme -- exactly what the old static file said, so nothing regresses for a
    visitor who has no account yet.
    """
    theme = appearance.DEFAULT_THEME
    token = request.cookie(SESSION_COOKIE)
    if token:
        try:
            user = get_db().session_user(token_hash(token))
        except Exception:  # noqa: BLE001 - a broken session must not break install
            user = None
        if user:
            profile = get_db().get_profile(user["id"]) or {}
            theme = str(profile.get("theme") or appearance.DEFAULT_THEME)
    return appearance.manifest_json(theme)


def _require_user(request: Request) -> dict[str, Any]:
    token = request.cookie(SESSION_COOKIE)
    if not token:
        raise ApiError(401, "请先登录。")
    user = get_db().session_user(token_hash(token))
    if not user:
        raise ApiError(401, "登录已过期。")
    request.user = user
    # 「他回来了没有」——运营者问的是这个，而会话表答不了（退出登录就把行删了）。
    # 一个**已登录的请求**就是「回来过」，所以记在这里：这是所有要求登录的接口
    # 唯一的入口。写失败绝不能让人用不了应用（它只是一条证据），所以吞掉异常并
    # 留下日志——数据库真坏了，别的地方会叫得比这声响。
    try:
        get_db().touch_last_seen(str(user["id"]))
    except Exception:  # noqa: BLE001 - 见上：这不是可以中断请求的失败
        logging.warning("记录最后活跃时间失败（不影响这次请求）", exc_info=True)
    return user


def _admin_emails() -> set[str]:
    """Operator identities named by the server environment.

    These are the instance owner's own accounts, and they are the floor: an
    operator added from the console can always be removed again, and the accounts
    named here cannot be, so a wrong click in the console can never lock the
    owner out of their own installation.

    Delegates to :mod:`pilot_app.alerting` so the HTTP surface and the alert
    mailer cannot disagree about who counts as an operator.
    """
    return alerting.admin_emails()


def _is_admin(user: dict[str, Any]) -> bool:
    """Owner-named accounts, plus anyone granted rights from the console.

    Until v0.34.0 this was the environment variable alone, and that is still the
    half that cannot be taken away: the stored grants are additive. A session
    still cannot grant itself anything -- every grant goes through an endpoint
    that requires an existing operator and their password.
    """
    if str(user.get("email", "")).strip().lower() in _admin_emails():
        return True
    return bool(user.get("is_admin"))


def _admin_source(user: dict[str, Any]) -> str:
    """Where this account's rights come from, for the console to display."""
    if str(user.get("email", "")).strip().lower() in _admin_emails():
        return "env"
    return "database" if user.get("is_admin") else ""


def _int_query(request: Request, name: str, *, default: int, minimum: int, maximum: int) -> int:
    """A bounded integer from the query string. Junk means "use the default".

    Forgiving on purpose: this only ever feeds a time window, and a 422 over
    ``?days=abc`` would be a worse answer than showing the usual fortnight.
    """
    raw = str((request.query.get(name) or [""])[0] or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def _require_admin(request: Request) -> dict[str, Any]:
    user = _require_user(request)
    if not _is_admin(user):
        # Deliberately identical to a missing resource: an ordinary user should
        # not learn that an admin surface exists.
        raise ApiError(404, "资源不存在。")
    return user


def _confirm_operator(request: Request, admin: dict[str, Any]) -> None:
    """Ask an operator to re-enter their password before changing operator rights.

    Granting operator rights is the most powerful thing this console can do: it
    is the one action whose effect outlives the session that performed it. An
    unattended browser is the realistic threat -- a laptop left open, a shared
    machine -- and the password is the only thing an attacker sitting at that
    browser does not have. The precedent is the account-deletion flow, which
    already makes the operator retype their own address for the same reason.
    """
    payload = request.json_object()
    password = str(payload.get("password") or "")
    if not password:
        raise ApiError(422, "请重新输入你的登录密码。")
    record = get_db().find_user_for_login(admin["email"])
    if not record or not verify_password(password, record["password_hash"]):
        raise ApiError(403, "密码不正确。")


def _admin_rate_limit(user_id: str) -> None:
    """Operators act rarely; this only stops a runaway click or a stuck script."""
    now = time.monotonic()
    with _verify_lock:
        recent = [value for value in _admin_actions.get(user_id, []) if now - value < 60]
        if len(recent) >= 30:
            raise ApiError(429, "管理操作过于频繁，请稍后再试。")
        recent.append(now)
        _admin_actions[user_id] = recent
        if len(_admin_actions) > 200:
            cutoff = now - 600
            for key in [key for key, values in _admin_actions.items() if not values or values[-1] < cutoff]:
                _admin_actions.pop(key, None)


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

Route = tuple[re.Pattern[str], Callable[[Request], Response]]

ROUTES: dict[str, list[Route]] = {}


def route(method: str, pattern: str) -> Callable[[Callable[[Request], Response]], Callable[[Request], Response]]:
    compiled = re.compile("^" + pattern + "$")

    def decorator(function: Callable[[Request], Response]) -> Callable[[Request], Response]:
        ROUTES.setdefault(method, []).append((compiled, function))
        return function

    return decorator


@route("GET", "/api/locale")
def locale_info(request: Request) -> Response:
    """这次请求在用哪种语言、有哪几种可选、各自译了多少。

    **不需要登录**：语言切换器就长在介绍页和登录屏上，而看这两块的人正是还没
    登录的人。这里也没有一条信息是私密的（语言清单是公开文件，「译了多少」是
    覆盖率），所以它和 `/api/catalog` 一样是公开读。
    """
    payload = i18n.registry()
    payload["current"] = page_locale(request)
    return _with_language(request, json_response(payload))


@route("POST", "/api/locale")
def set_locale(request: Request) -> Response:
    """换一种界面语言。写 cookie；**登录了的话同时写进账号**。

    另开一个端点而不是塞进 `/api/profile`：那一个是「整份覆盖」，而只改一项的
    接口必须另开（不变量 4）——否则前端每换一次语言都得先把整份资料读出来再写
    回去，中间任何一次并发保存都会被这次写入抹掉。
    """
    payload = request.json_object()
    wanted = _string(payload, "locale", maximum=40).strip()
    if not i18n.is_supported(wanted):
        raise ApiError(422, i18n.mark("不支持这种语言。"))
    user = request.user or _visit_identity(request)
    if user:
        get_db().set_ui_locale(user["id"], wanted)
    body = i18n.registry()
    body["current"] = wanted
    return json_response(body, cookies=[language_cookie(wanted)])


@route("GET", "/i18n/(?P<name>[A-Za-z0-9-]+)\\.json")
def locale_catalog(request: Request, name: str) -> Response:
    """给 ``i18n.js`` 用的词典（JS 里拼出来的那几句动态文案）。

    页面本身的文字是**服务端**翻好的，这里只是给「表单提示 / 报错回显 / 按钮
    忙碌态」那几句用的。所以它是**按需**取的，不是每次开页都取——一份 en.json
    有 100 KB，首屏一个字都用不到它。

    语言代码先对着清单验一遍再落到文件路径上：`name` 来自 URL，直接拼路径就是
    一次目录穿越。
    """
    if not i18n.is_supported(name):
        return fail(request, 404, "资源不存在。")
    path = i18n.I18N_ROOT / ("%s.json" % name)
    headers = {"Cache-Control": "public, max-age=300"}
    if not path.is_file():
        # 中文没有词典文件（它是原文），所以这里对一个合法但还没译的语言返回空表
        # ——JS 那边查不到就原样显示中文，与其它语言的缺译行为一致。
        return Response(status=200, body=b"{}", content_type="application/json; charset=utf-8",
                        headers=headers)
    return Response(status=200, body=path.read_bytes(),
                    content_type="application/json; charset=utf-8", headers=headers)


@route("GET", "/health")
def health(request: Request) -> Response:
    return json_response({"status": "ok", "version": VERSION})


@route("GET", "/api/catalog")
def catalog(request: Request) -> Response:
    payload = public_catalog()
    payload["mailbox"] = public_mailbox_help()
    return json_response(payload)


#: 「你的身份」这个下拉框允许的值（v1.0.1）。只收固定几个值而不是任意文字：
#: 这一栏是给运营者看的分类，不是自由文本——收自由文本只会让面板里出现
#: 「本科生」「本科」「Undergrad」三种写法，而它一个字都不会更准。
SIGNUP_IDENTITIES = ("本科生", "研究生", "其他")
#: 「最想先解决什么」那组多选（设计稿里的四项）。同理：固定集合才能统计。
SIGNUP_GOALS = ("错过截止时间", "通知太多", "分不清轻重", "找不到要办的事")


def _signup_extras(payload: dict[str, Any]) -> dict[str, str]:
    """The three **optional** things the signup/register form asks for.

    One reader for two forms on purpose: the landing page's application form and
    (since 2026-09-23) the register form in `/app` ask the same three questions,
    and a second copy of these whitelists is exactly how the two would drift --
    a value accepted at one door and refused at the other. The rules:

    * **all three are optional** -- nothing here may ever block a registration
      (that is the point of open registration);
    * an unknown ``identity``/``goals`` value is **refused, not dropped**: a
      silent drop lets the writer believe we stored it;
    * the strings are trimmed, and the maximum lengths are the ones the columns
      and the panel can actually show.
    """
    nickname = _string(payload, "nickname", default="", required=False, maximum=40).strip()
    identity = _string(payload, "identity", default="", required=False, maximum=20).strip()
    if identity and identity not in SIGNUP_IDENTITIES:
        # 选项传**元组**而不是拼好的字符串：它们既是存进库的取值（所以中文字面量必须
        # 留在代码里），又要能跟着界面语言走 —— 拼好的字符串在译文里没人翻，英文页上
        # 就成了「one of: 本科生、研究生、其他」。`dispatch` 认得列表型参数。
        raise ApiError(422, i18n.mark("身份只能是：{options}。"),
                       {"options": SIGNUP_IDENTITIES})
    goals = _string_list(payload, "goals", maximum_items=4, item_maximum=20)
    unknown = [item for item in goals if item not in SIGNUP_GOALS]
    if unknown:
        # 拒绝而不是「过滤掉不认识的」：静默丢弃会让填的人以为我们收到了，
        # 而面板上什么都没有——这与留言板那条「超长拒绝不截断」是同一条规矩。
        raise ApiError(422, i18n.mark("「最想先解决什么」里有不认识的选项。"))
    return {"nickname": nickname, "identity": identity, "goals": "、".join(goals)}


@route("POST", "/api/signup")
def public_signup(request: Request) -> Response:
    """Accept a pilot application from the public landing page.

    This is the only unauthenticated write in the API, so it is deliberately
    narrow: it stores an address and a note, and nothing else. It cannot create
    an account, cannot mint an invite, and cannot read anything back -- approval
    stays a separate, operator-only action. A flood here annoys the operator;
    it cannot grant anybody access.

    Throttled per client, and the reply never confirms whether an address is
    already registered with the pilot.
    """
    client = request.client or "unknown"
    _signup_rate_limit(client)
    payload = request.json_object()
    email = _email(_string(payload, "email", maximum=254))
    note = _string(payload, "note", default="", required=False, maximum=500)
    # 三个选填项（v1.0.1，设计稿里那三栏）。**全是选填**：一个字都不填照样能申请，
    # 这一点有测试盯着——申请表不是把陌生人挡在外面的地方。解析与白名单只有一处
    # （`_signup_extras`），注册那条路读的是同一份，所以两个门不会一个收一个拒。
    extras = _signup_extras(payload)
    try:
        row, already = get_db().create_signup_request(
            email, note, _client_label(request),
            nickname=extras["nickname"], identity=extras["identity"], goals=extras["goals"])
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    if not already:
        _notify_new_signup(row)
    return json_response({"ok": True, "already": already})


@route("POST", "/api/guestbook")
def public_guest_message(request: Request) -> Response:
    """Accept a message from anyone, signed in or not.

    This is the **second** unauthenticated write in the API (the application form
    is the first), so it is held to the same narrow contract: it can store one
    message and nothing else. It cannot create an account, cannot mint an invite,
    and cannot read anything back -- not even to confirm what was stored. A flood
    here annoys the operator; it cannot grant anybody access.

    The five anti-abuse measures are deliberately dependency-free -- no CAPTCHA
    service, because that would add a runtime dependency and hand every visitor's
    address to a third party:

    * per-client rate limit (above),
    * a honeypot field no person can fill in,
    * a minimum time between page load and submit,
    * a hard length limit that refuses rather than truncates,
    * at most two links, because a message board is not a place to post links.

    And everything lands as ``pending``: nothing reaches the public page until a
    person has looked at it.
    """
    client = request.client or "unknown"
    _guestbook_rate_limit(client)
    payload = request.json_object()

    # The honeypot: hidden by CSS, so a person never sees it, and a form-filling
    # robot cannot tell it is not part of the form. Answering "ok" rather than an
    # error is deliberate -- a robot that gets a rejection learns which field to
    # skip next time.
    if _string(payload, "website", default="", required=False, maximum=200).strip():
        logging.info("guestbook honeypot tripped from %s", client)
        return json_response({"ok": True})

    # Load-to-submit time. The client reports it, so this is a soft signal: it
    # raises the cost of the naive case (POST the endpoint the moment it is
    # discovered) and claims nothing more than that.
    try:
        elapsed_ms = int(payload.get("elapsed_ms") or 0)
    except (TypeError, ValueError):
        elapsed_ms = 0
    if 0 < elapsed_ms < GUESTBOOK_MIN_SECONDS * 1000:
        logging.info("guestbook submitted in %s ms from %s", elapsed_ms, client)
        raise ApiError(422, i18n.mark("提交得太快了，请确认你是本人操作。"))

    body = _string(payload, "body", maximum=database_mod.GUEST_BODY_LIMIT)
    if not body.strip():
        raise ApiError(422, i18n.mark("请先写点什么。"))
    if _count_links(body) > database_mod.GUEST_LINK_LIMIT:
        raise ApiError(422, i18n.mark("留言里最多 {count} 个链接。"),
                        {"count": database_mod.GUEST_LINK_LIMIT})
    nickname = _string(payload, "nickname", default="", required=False,
                       maximum=database_mod.GUEST_NICKNAME_LIMIT)
    address = _string(payload, "email", default="", required=False, maximum=254).strip()
    if address:
        address = _email(address)

    secrets = get_service().secrets
    try:
        get_db().create_guest_message(
            body=body, nickname=nickname,
            # Encrypted, not hashed: the operator may want to answer, and never
            # publishes it. The client address is the other way round -- hashed,
            # because nothing ever needs to read it back.
            sealed_email=secrets.encrypt(address, context="guestbook") if address else b"",
            client_hash=secrets.anonymized(client),
        )
    except ValueError as exc:
        raise ApiError(422, str(exc)) from exc
    _notify_new_guest_message()
    return json_response({"ok": True})


def _count_links(text: str) -> int:
    """How many links a message is trying to publish.

    Deliberately counts a bare ``www.`` too: the point is to stop a message that
    is mostly an advertisement, not to parse URLs correctly, and a scheme-less
    link is exactly the shape a spammer reaches for.
    """
    return len(re.findall(r"(?:https?://|www\.)\S+", text, flags=re.IGNORECASE))


def _notify_new_guest_message() -> None:
    """One line to the operator so a waiting message is not forgotten.

    The board is `pending` by default, which means a message nobody is told about
    is a message nobody reads -- the same silent-failure shape as an
    unacknowledged alert. The body is *not* in the mail: the operator opens the
    console to read it, and mail is the one place this project does not put
    visitor text. Failure here never fails the request; the message is stored.
    """
    try:
        alerting.send_admin_mail(
            get_db(), get_service().secrets,
            subject="[CityU Mail Pilot] 官网有一条新留言",
            text_body=(
                "有人在官网上留了言，正在等你看一眼。\n\n"
                "正文不会发到邮件里——到管理后台的「留言板」面板读，"
                "在那边决定刊登、驳回还是删除。\n"
            ),
        )
    except Exception:  # noqa: BLE001 - the message is already stored
        logging.warning("could not notify the operator about a guest message", exc_info=True)


@route("GET", "/api/admin/analytics")
def admin_analytics(request: Request) -> Response:
    """Visitor statistics for the console: how many, from where, and how many robots.

    Three deliberate properties:

    * The addresses in ``recent`` come from an in-memory buffer and are **not**
      stored. Restarting web empties it; the database holds only keyed digests.
      This is the shape the operator chose: they can watch traffic arrive, and
      nothing about a visitor's address reaches the daily backup.
    * Robots are reported next to the human numbers, never folded into them.
      This box is on a public address, so scanner traffic is a large and
      uninteresting share of the total; a combined figure would be a number
      nobody can act on.
    * The window is counted in the *reader's* local days (their profile
      timezone), so "今天" means today where they are, matching every other
      timestamp in the console.
    """
    admin = _require_admin(request)
    database = get_db()
    days = _int_query(request, "days", default=7, minimum=1, maximum=90)
    try:
        profile = database.get_profile(admin["id"])
        zone = str((profile or {}).get("timezone") or "Asia/Shanghai")
    except Exception:  # pragma: no cover - a missing profile is not fatal here
        zone = "Asia/Shanghai"
    offset = analytics_mod.day_modifier(zone)
    today = database.page_view_totals(offset=offset, days=1)
    return json_response({
        "days": days,
        "timezone": zone,
        "today": today,
        "totals": database.page_view_totals(offset=offset, days=days),
        "daily": database.page_view_daily(offset=offset, days=max(days, 14)),
        "paths": database.page_view_breakdown("path", offset=offset, days=days),
        "referrers": database.page_view_breakdown("referrer", offset=offset, days=days),
        "countries": database.page_view_breakdown("country", offset=offset, days=days),
        "cities": database.page_view_breakdown("city", offset=offset, days=days, limit=8),
        "recent": analytics_mod.recent(40),
        "robots": database.page_view_breakdown(
            "path", offset=offset, days=days, humans_only=False, limit=8),
        "geo": {
            "available": geoip_available(),
            "retention_days": analytics_mod.retention_days(),
        },
    })


def geoip_available() -> bool:
    """Whether the offline country database has been built on this machine."""
    try:
        from . import geoip as geoip_mod

        return geoip_mod.available()
    except Exception:  # pragma: no cover
        return False


@route("POST", "/api/admin/analytics/purge")
def admin_purge_analytics(request: Request) -> Response:
    """删掉运营者自己的访问记录（他看自己的站不算访客）。

    删两类：打过 `admin` 标记的，以及**来自他现在这个 IP 摘要**的——历史行没有标记，
    但那把摘要认得出「这个地址」。别人的记录一行都不碰，删了几条如实回报。
    """
    admin = _require_admin(request)
    database = get_db()
    digest = get_service().secrets.anonymized(request.client or "")
    removed = database.purge_operator_page_views(digest)
    database.record_audit(action="analytics_operator_purged", actor_user_id=admin["id"],
                          actor_email=admin["email"], detail=f"removed={removed}")
    logging.info("admin %s purged %s of their own page views", admin["id"], removed)
    return json_response({"ok": True, "removed": removed,
                          "recent": analytics_mod.recent(40)})


@route("GET", "/api/admin/guestbook")
def admin_guest_messages(request: Request) -> Response:
    """Every message, newest first, for the console's moderation panel."""
    _require_admin(request)
    database = get_db()
    rows = database.guest_messages()
    secrets = get_service().secrets
    return json_response({
        "messages": [_guest_row(row, secrets=secrets) for row in rows],
        "counts": _guest_counts(rows),
    })


@route("PUT", r"/api/admin/guestbook/(?P<message_id>[^/]+)")
def admin_set_guest_message(request: Request, message_id: str) -> Response:
    """Publish, un-publish, reject or delete one message.

    ``delete`` removes the row; the other three are status changes, so an
    operator's decision is visible in the console rather than making the message
    disappear without trace.
    """
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    database = get_db()
    payload = request.json_object()
    status = _string(payload, "status", maximum=20)
    if status not in {"pending", "published", "rejected", "deleted"}:
        raise ApiError(422, "未知的状态。")
    try:
        if status == "deleted":
            database.delete_guest_message(message_id)
        else:
            database.set_guest_message_status(message_id, status, actor=admin["email"])
    except KeyError as exc:
        raise ApiError(404, "没有这条留言。") from exc
    # The audit line records the decision, never the text: the console is where
    # visitor words live, and the audit log is exported and read in other places.
    database.record_audit(action=f"guest_message_{status}", actor_user_id=admin["id"],
                          actor_email=admin["email"], detail=f"id={message_id}",
                          client=_client_label(request))
    rows = database.guest_messages()
    return json_response({
        "ok": True, "id": message_id,
        "messages": [_guest_row(row, secrets=get_service().secrets) for row in rows],
        "counts": _guest_counts(rows),
    })


def _guest_row(row: dict[str, Any], *, secrets: Any) -> dict[str, Any]:
    """One message as the console sees it.

    The optional address is decrypted **only** here, and only for an
    administrator: the public page never asks for it, so it cannot leak by
    accident. Empty means the visitor chose not to leave one.
    """
    address = ""
    if row.get("email"):
        try:
            address = secrets.decrypt(row["email"].encode("utf-8") if isinstance(row["email"], str) else row["email"],
                                      context="guestbook")
        except Exception:  # noqa: BLE001 - a key rotation must not break the panel
            address = "（无法解密）"
    return {
        "id": row["id"], "body": row["body"], "nickname": row["nickname"],
        "email": address, "status": row["status"], "created_at": row["created_at"],
        "decided_at": row.get("decided_at") or "", "decided_by": row.get("decided_by") or "",
    }


def _guest_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"pending": 0, "published": 0, "rejected": 0, "deleted": 0}
    for row in rows:
        status = str(row.get("status") or "")
        if status in counts:
            counts[status] += 1
    counts["total"] = len(rows)
    return counts


def _notify_new_signup(row: dict[str, Any]) -> None:
    """Tell the operators a signup row arrived. Never fails the request.

    **2026-09-22 起这是一条历史通道**：首页那张申请表与「发邀请码」按钮一起下线，
    注册完全开放，所以这封信只可能来自直接调用 `POST /api/signup` 的客户端。它记的
    仍是「有人来过」，但**不需要谁去发码**（信里明说这一点，否则运营者会去找一个
    已经不在界面上的按钮）。

    The applicant cannot be e-mailed directly: every send in this project goes
    out through a user's own SMTP credentials, and there is no system mailbox.
    A notification failure must not lose the row, which is why this swallows
    errors after logging them.

    Who counts as an operator is the installer's own address (always) plus any
    admin the installer ticked in the console -- ``signup_notice`` owns that
    rule, including the part where a revoked admin silently stops receiving.
    """
    try:
        database = get_db()
        service = get_service()
        alerting.send_admin_mail(
            database, service.secrets,
            subject="[CityU Mail Pilot] 新的注册申请（历史通道）",
            text_body=(
                f"有人从网站申请了一个名额。\n\n"
                f"邮箱：{row.get('email', '')}\n"
                f"称呼：{row.get('nickname') or '（没填）'}\n"
                f"身份：{row.get('identity') or '（没填）'}\n"
                f"最想先解决：{row.get('goals') or '（没填）'}\n"
                f"留言：{row.get('note') or '（没有留言）'}\n"
                f"时间：{row.get('created_at', '')}\n"
                f"来源：{row.get('client', '')}\n\n"
                f"注意：注册已经**完全开放**（首页不再有申请表单，也没有「发邀请码」这一步），"
                f"所以这一条只可能来自直接调用接口的客户端 —— **不需要发码**，"
                f"它只是记在管理后台的「注册申请（历史）」面板里供回看。"
            ),
            also=signup_notice.extra_recipients(database),
        )
    except Exception:  # noqa: BLE001 - the application is already stored
        logging.warning("could not notify the operator about a new signup", exc_info=True)


@route("POST", "/api/auth/register")
def register(request: Request) -> Response:
    payload = request.json_object()
    email = _email(_string(payload, "email", maximum=254))
    password = _string(payload, "password", minimum=1, maximum=400)
    # **邀请码 2026-09-22 取消了**：注册是开放的，这里不再要求它。字段仍然收下——老客户端
    # 与老书签里可能还带着——带了一张还有效的码就照旧认领（数据层保留了这个能力），不带也直接建号。
    # 见 `docs/open-registration-2026-09-22.md`。
    invite_code = _string(payload, "invite_code", default="", required=False, maximum=200)
    # 2026-09-23：申请表上那三栏**挪到了注册表单**（用户拍板；申请制取消后它们本来
    # 会随表单一起消失）。同样是**全选填**：一个字都不填照样能建号 —— 这是开放注册的
    # 底线，有测试盯着。白名单与解析复用申请书那一份（`_signup_extras`）。
    extras = _signup_extras(payload)
    # Consent is enforced here, not only in the browser. A checkbox that the
    # server never checks is decoration, and the disclosure that matters most --
    # that mail bodies go to a third-party model -- is exactly the one a user
    # cannot discover after the fact.
    if not _boolean(payload, "accepted_terms", False):
        raise ApiError(400, i18n.mark("请先阅读并同意《隐私政策》与《服务条款》。"))
    # 开放注册之后，**限速就是唯一一道防批量注册的闸**（名额上限管的是总量，不管速度）：
    # 和申请书共用同一个计数器 —— 同一 IP 每小时 5 次。它是内存里的，不落盘（见 `_client_label`）。
    _signup_rate_limit(request.client or "unknown")
    database = get_db()
    limit, _source = _max_users()
    if database.count_users() >= limit:
        raise ApiError(403, i18n.mark("当前名额已满。"))
    code = invite_code.strip()
    try:
        user = database.create_user(email, hash_password(password), token_hash(code) if code else "",
                                    signup_extras=extras, max_users=limit)
    except database_mod.CapacityFull as exc:
        # 上面那次 `count_users()` 只是快路径；真正守住的是数据层同一个事务里的复核。
        raise ApiError(403, i18n.mark("当前名额已满。")) from exc
    except (ValueError, SecurityError) as exc:
        raise ApiError(400, str(exc)) from exc
    return json_response(user, cookies=[_session_cookie(user["id"])])


@route("POST", "/api/auth/login")
def login(request: Request) -> Response:
    payload = request.json_object()
    email = _email(_string(payload, "email", maximum=254))
    password = _string(payload, "password", minimum=1, maximum=400)
    attempt_key = token_hash(email)
    _rate_limit(attempt_key)
    user = get_db().find_user_for_login(email)
    if not user:
        # **不是** `not user or not verify_password(...)`：那样会短路，账号不存在时一次
        # PBKDF2 都不跑，于是两条失败路径的耗时差一个数量级——文案恒定挡不住计时这一路。
        # 现在两条路都恰好跑一次（见 `security.spend_verification_time` 的注释）。
        spend_verification_time(password)
        _rate_limit(attempt_key, failed=True)
        raise ApiError(401, i18n.mark("邮箱或密码错误。"))
    if not verify_password(password, user["password_hash"]):
        _rate_limit(attempt_key, failed=True)
        raise ApiError(401, i18n.mark("邮箱或密码错误。"))
    _clear_attempts(attempt_key)
    return json_response(
        {key: user[key] for key in ("id", "email", "status", "created_at")},
        cookies=[_session_cookie(user["id"])],
    )


@route("POST", "/api/auth/logout")
def logout(request: Request) -> Response:
    token = request.cookie(SESSION_COOKIE)
    if token:
        get_db().delete_session(token_hash(token))
    return json_response({"ok": True}, cookies=[_expired_cookie()])


@route("GET", "/api/me")
def me(request: Request) -> Response:
    user = _require_user(request)
    database = get_db()
    mailbox = database.get_mailbox(user["id"])
    connections: dict[str, Any] = {}
    for kind in ("model", "search"):
        item = database.get_connection(user["id"], kind)
        if item:
            connections[kind] = {
                key: item[key]
                for key in ("provider", "model", "base_url", "enabled", "last_test_at", "last_error")
            }
            # 给人看的供应商名字（`local_openai` → 「本机大模型（Bonsai + 本地护栏）」）。
            # 与 `provider` 并存而不是替换：程序按 id 判断，界面读 label。
            connections[kind]["label"] = providers.label_for(item["provider"])
    # The screen has to distinguish "nothing configured" from "running on the
    # pilot's key", or a user whose reports and citations work is told to go
    # configure something that is not broken. Only the fact that a platform key
    # exists is exposed; the key itself is never in this payload, nor in any other
    # response (see `test_platform_key`).
    for kind, fallback in (("model", providers.platform_model_default()),
                           ("search", providers.platform_search_default())):
        if kind in connections or not fallback:
            continue
        connections[kind] = {
            key: fallback[key]
            for key in ("provider", "model", "base_url", "enabled", "last_test_at", "last_error")
        }
        connections[kind]["platform"] = True
        connections[kind]["label"] = providers.label_for(fallback["provider"])
    safe_mailbox = None
    if mailbox:
        safe_mailbox = {
            key: mailbox[key]
            for key in ("email", "report_to", "imap_host", "imap_port", "smtp_host", "smtp_port", "enabled",
                        "last_polled_at", "last_error")
        }
        # 「这个邮箱的服务商已经不给用授权码了，得换一个」——判断只有一处
        # （`Database.mailbox_needs_another_provider`），客户端只认这一个布尔量，
        # 不自己去匹配错误文字。两个错误列都要看：轮询写 `last_error`、手动测试写
        # `last_verify_error`，而用户看到的红字可能来自任何一边。
        safe_mailbox["needs_another_provider"] = database.mailbox_needs_another_provider({
            "mailbox_error": " ".join(
                str(mailbox.get(key) or "") for key in ("last_error", "last_verify_error")),
            "imap_host": mailbox.get("imap_host") or "",
        })
    return json_response(
        # `is_admin` in the identity block is stripped on purpose. The stored
        # column means "granted from the console" and the top-level field means
        # "can actually administer this instance", which is the environment list
        # OR the column. Shipping both under one name would leave the next reader
        # to guess which one the client is checking.
        {"user": {key: value for key, value in user.items() if key != "is_admin"},
         "profile": database.get_profile(user["id"]), "mailbox": safe_mailbox,
         # What somebody who has not chosen gets. The panel needs it to say
         # "follow the instance (which is currently: brief)" -- "follow" with no
         # noun is not a choice anybody can make deliberately.
         "report_mode_default": service_mod.instance_report_mode(),
         "connections": connections, "is_admin": _is_admin(user),
         # The application shell is a static file, so the footer link to the
         # source cannot be templated into it. It rides here instead, and the
         # shell fills the footer in after login. See `source_url` for why the
         # link exists at all (AGPL-3.0 section 13).
         "source_url": source_url(),
         # The photo itself is fetched from its own route so this response stays
         # small; what the picker needs is whether one exists and which revision
         # to put in the URL.
         "background_image": database.background_summary(user["id"])}
    )


@route("PUT", "/api/profile")
def save_profile(request: Request) -> Response:
    user = _require_user(request)
    payload = request.json_object()
    daily_time = _string(payload, "daily_time", default="22:00", maximum=5)
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", daily_time):
        raise ApiError(422, "每日发送时间必须是 HH:MM。")
    data: dict[str, Any] = {
        "school_email": _cityu_email(_string(payload, "school_email", default="", required=False, maximum=254)),
        "major": _string(payload, "major", default="", maximum=200),
        "year_of_study": _string(payload, "year_of_study", default="", maximum=80),
        "custom_instructions": _string(payload, "custom_instructions", default="", maximum=1000),
        "language": _string(payload, "language", default="bilingual", maximum=40),
        "timezone": _string(payload, "timezone", default="Asia/Hong_Kong", maximum=64),
        "immediate_enabled": _boolean(payload, "immediate_enabled", True),
        "daily_enabled": _boolean(payload, "daily_enabled", True),
        "daily_time": daily_time,
        "courses_json": json.dumps(_string_list(payload, "courses", maximum_items=20), ensure_ascii=False),
        "interests_json": json.dumps(_string_list(payload, "interests", maximum_items=20), ensure_ascii=False),
        "career_goals_json": json.dumps(_string_list(payload, "career_goals", maximum_items=10), ensure_ascii=False),
        "focus_topics_json": json.dumps(_string_list(payload, "focus_topics", maximum_items=20), ensure_ascii=False),
        "less_interested_json": json.dumps(_string_list(payload, "less_interested", maximum_items=20), ensure_ascii=False),
    }
    get_db().upsert_profile(user["id"], data)
    return json_response({"ok": True})


@route("PUT", "/api/appearance")
def save_appearance(request: Request) -> Response:
    """Save only the look of the interface.

    Deliberately separate from ``PUT /api/profile``: that endpoint writes every
    profile field from the request body and defaults anything missing, so saving
    a theme through it would silently wipe the user's courses and instructions.
    """
    user = _require_user(request)
    payload = request.json_object()
    theme = _string(payload, "theme", default="paper", maximum=20)
    if theme not in THEMES:
        raise ApiError(422, "未知的主题。")
    background = _string(payload, "background", default="", required=False, maximum=20)
    if background not in BACKGROUNDS:
        raise ApiError(422, "未知的背景图。")
    if background == "custom" and not get_db().background_summary(user["id"])["present"]:
        # Otherwise the picker offers a blank page: 'custom' with nothing stored
        # falls back to the theme, so the user selects their photo and sees no
        # change at all.
        raise ApiError(422, "还没有上传自定义背景图。")
    get_db().upsert_profile(user["id"], {"theme": theme, "background": background})
    return json_response({"ok": True, "theme": theme, "background": background})


REPORT_MODE_PATH = "/api/reports/mode"


@route("PUT", REPORT_MODE_PATH)
def save_report_mode(request: Request) -> Response:
    """Choose how detailed each per-message report is.

    Separate from ``PUT /api/profile`` for the usual reason: that endpoint writes
    every profile field from the request body and defaults whatever is missing,
    so changing one preference through it would silently wipe the user's courses
    and instructions.

    ``''`` means "follow this instance" and is the default for everybody, which
    is what lets this ship without changing one word of any existing user's mail.
    The two explicit values mean exactly one e-mail per message: the two-stage
    "brief then full" mode is an instance-level experiment and is deliberately
    not offered here -- two e-mails for every message is noise, not a preference.

    The cost difference is small and worth stating plainly to the user: measured
    on this instance, the full seven-section report costs about 1.45x the brief
    one (roughly $0.0002 more per message), because the prompt -- the e-mail plus
    search results -- is the same either way and dominates. So this is a reading
    preference, not a way to save money.
    """
    user = _require_user(request)
    payload = request.json_object()
    mode = _string(payload, "mode", default="", maximum=10)
    if mode not in {"", "brief", "full"}:
        raise ApiError(422, "未知的报告详细程度。")
    get_db().upsert_profile(user["id"], {"report_mode": mode})
    return json_response({"ok": True, "mode": mode})


REPORT_DELIVERY_PATH = "/api/reports/delivery"


@route("PUT", REPORT_DELIVERY_PATH)
def save_report_delivery(request: Request) -> Response:
    """要不要收我们的邮件——即时摘要与每日简报，两个字段一次写完。

    单独一个端点（不并进 `PUT /api/profile`）：那个接口按请求体写全字段、缺的走默认值，
    用它改一个偏好会顺手抹掉用户的课程与要求。**两个字段都要给**：总开关是一次点击，
    半个状态（只关了一半）不该由一次点击产生。

    关掉的是**投递**，不是处理——我们照样读邮箱、照样生成报告（App 里的待办、按天回看、
    看原信、翻译总结全靠它），只是不发邮件；那些信在库里收尾成 `held`。
    界面上必须同时说清三件事：学校转来的原信还是会到他的私人邮箱（那是他自己的转发规则）、
    服务公告与账号故障通知不受影响、待办与提醒一条不少。
    """
    user = _require_user(request)
    payload = request.json_object()
    wanted = {}
    for key in ("immediate", "daily"):
        if key not in payload:
            raise ApiError(422, "两个选项都要给（immediate 与 daily）。")
        value = payload.get(key)
        if not isinstance(value, bool):
            raise ApiError(422, "这两个选项只能是 true 或 false。")
        wanted[key] = value
    get_db().upsert_profile(user["id"], {
        "immediate_enabled": 1 if wanted["immediate"] else 0,
        "daily_enabled": 1 if wanted["daily"] else 0,
    })
    return json_response({"ok": True, **wanted})


BACKGROUND_PATH = "/api/appearance/background"


@route("PUT", BACKGROUND_PATH)
def upload_background(request: Request) -> Response:
    """Store a user-uploaded background photo.

    The body is the image itself, not multipart. A multipart parser is a large
    piece of code whose failure modes land squarely on attacker-controlled input,
    and there is exactly one field here, so the raw body plus a Content-Type
    header carries the same information with far less to get wrong.

    Validation lives in ``imageguard``; this function's job is the parts that are
    about storage rather than about images.
    """
    user = _require_user(request)
    if not request.body:
        raise ApiError(422, "没有收到图片内容。")
    try:
        media_type, width, height = imageguard.validate(
            request.body, request.header("Content-Type")
        )
    except imageguard.ImageRejected as exc:
        detail = f"（{exc.detail}）" if exc.detail else ""
        raise ApiError(422, exc.reason + detail) from exc

    revision = get_db().set_background_image(user["id"], media_type, request.body)
    # Choosing the photo and selecting it are one action from the user's side, so
    # the server does both; leaving them separate produces an upload that appears
    # to have done nothing.
    get_db().upsert_profile(user["id"], {"background": "custom"})
    return json_response({
        "ok": True, "background": "custom", "rev": revision,
        "media_type": media_type, "width": width, "height": height,
        "size": len(request.body),
    })


@route("DELETE", BACKGROUND_PATH)
def delete_background(request: Request) -> Response:
    user = _require_user(request)
    get_db().clear_background_image(user["id"])
    profile = get_db().get_profile(user["id"])
    if profile.get("background") == "custom":
        get_db().upsert_profile(user["id"], {"background": ""})
    return json_response({"ok": True, "background": ""})


@route("GET", BACKGROUND_PATH)
def serve_background(request: Request) -> Response:
    """Serve the caller's own photo, and nobody else's.

    Two headers are load-bearing rather than decorative. ``nosniff`` is already
    global, and the Content-Type comes from our own allowlist rather than from
    anything the uploader supplied, so the bytes can never be interpreted as
    HTML or script. ``private`` keeps the photo out of any shared cache: it is
    personal data belonging to one account.
    """
    user = _require_user(request)
    stored = get_db().get_background_image(user["id"])
    if not stored:
        raise ApiError(404, "还没有自定义背景图。")
    if stored["media_type"] not in (imageguard.JPEG, imageguard.PNG):
        # Defence in depth: a value that somehow predates the allowlist must not
        # become a content-type header we would not choose today.
        raise ApiError(404, "背景图格式不受支持。")
    return Response(
        status=200,
        body=stored["bytes"],
        content_type=stored["media_type"],
        headers={
            "Cache-Control": "private, max-age=604800",
            "ETag": f'"{stored["rev"]}"',
            "Content-Disposition": "inline",
        },
    )



@route("PUT", "/api/mailbox")
def save_mailbox(request: Request) -> Response:
    user = _require_user(request)
    payload = request.json_object()
    # The authorization gate: a user may only start reading their mail once they
    # have asserted that they are entitled to forward and process it (terms §3).
    # Same reasoning as `accepted_terms` in `register` -- a checkbox the server
    # never checks is decoration, and this is the assertion the whole service
    # rests on. It is required only on the transition from "no mailbox" to
    # "mailbox" so that an existing account editing its host or port is not
    # asked to re-assert something it already asserted.
    database = get_db()
    is_first_setup = database.get_mailbox(user["id"]) is None
    if is_first_setup and not _boolean(payload, "accepted_terms", False):
        raise ApiError(
            400,
            "请先确认你有权把这部分邮件转发给本服务并允许处理"
            "（《服务条款》第 3 条），再保存邮箱设置。",
        )
    mailbox_email = _email(_string(payload, "email", maximum=254))
    # The form promises "leave blank = send back to the same mailbox", so
    # honour that here instead of running "" through email validation.
    report_to_raw = _string(payload, "report_to", default="", required=False, maximum=254).strip()
    data: dict[str, Any] = {
        "email": mailbox_email,
        "report_to": _email(report_to_raw) if report_to_raw else mailbox_email,
        "imap_port": _port(payload, "imap_port"),
        "smtp_port": _port(payload, "smtp_port"),
        "enabled": _boolean(payload, "enabled", True),
    }
    app_password = _string(payload, "app_password", minimum=1, maximum=1000)
    try:
        data["imap_host"] = validate_public_host(_string(payload, "imap_host", maximum=253))
        data["smtp_host"] = validate_public_host(_string(payload, "smtp_host", maximum=253))
    except SecurityError as exc:
        raise ApiError(422, str(exc)) from exc
    # 有些供应商**一个域名一台服务器**（网易五台，实测过），把 A 域名的账号指到 B 域名
    # 的服务器上会被拒登录，而服务器回的是「Login error or password error」——用户看到
    # 的是「授权码不对」，于是一直去重新生成一个本来就是对的授权码。这里当场说清楚。
    wanted = mailpresets.hosts_for_email(mailbox_email)
    if wanted and data["imap_host"] != wanted["imap_host"]:
        known = set(mailpresets.PRESETS_BY_ID.get(
            mailpresets.preset_id_for_email(mailbox_email), {}).get("hosts_by_domain", {}).values())
        if data["imap_host"] in {host for pair in known for host in pair}:
            raise ApiError(422, f"这个地址的收信服务器是 {wanted['imap_host']}，"
                                f"不是 {data['imap_host']}——同一家的不同域名是不同服务器，"
                                "填错了服务器会回「密码错误」，看起来像授权码不对。")
    data["encrypted_password"] = get_service().secrets.encrypt(app_password, context=f"mailbox:{user['id']}")
    get_db().upsert_mailbox(user["id"], data)
    return json_response({"ok": True})


@route("PUT", r"/api/connections/(?P<kind>[A-Za-z]+)")
def save_connection(request: Request, kind: str) -> Response:
    user = _require_user(request)
    if kind not in {"model", "search"}:
        raise ApiError(404, "未知的连接类型。")
    payload = request.json_object()
    provider = _string(payload, "provider", maximum=64)
    api_key = _string(payload, "api_key", default="", required=False, maximum=4000).strip()
    if not api_key:
        raise ApiError(422, "请填写 API key。")
    model = _string(payload, "model", default="", required=False, maximum=200).strip()
    base_url_input = _string(payload, "base_url", default="", required=False, maximum=500)
    try:
        if kind == "model":
            _, _, base_url = normalized_model_config(provider, model, base_url_input)
        else:
            if provider not in SEARCH_PRESETS:
                raise ValueError("不支持的搜索供应商。")
            base_url = SEARCH_PRESETS[provider]["base_url"]
    except (ValueError, SecurityError) as exc:
        raise ApiError(422, str(exc)) from exc
    data = {
        "kind": kind,
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "encrypted_api_key": get_service().secrets.encrypt(api_key, context=f"connection:{user['id']}:{kind}"),
        "config_json": json.dumps(_config(payload, "config")),
        "enabled": _boolean(payload, "enabled", True),
    }
    get_db().upsert_connection(user["id"], data)
    return json_response({"ok": True})


@route("POST", r"/api/test/(?P<target>[A-Za-z]+)")
def test_connection(request: Request, target: str) -> Response:
    user = _require_user(request)
    service = get_service()
    # 名额在**调用之前**占：限流与并发闸门都要挡住真正的出站请求，而不是事后记账。
    with _connection_test_slot(user["id"]):
        try:
            if target == "model":
                result = service.test_model(user["id"])
                get_db().record_connection_result(user["id"], "model")
                return json_response({"ok": True, "result": result})
            if target == "search":
                results = service.test_search(user["id"])
                get_db().record_connection_result(user["id"], "search")
                return json_response({"ok": True, "results": results})
            if target == "mailbox":
                result = service.test_mailbox(user["id"])
                mailbox = get_db().get_mailbox(user["id"])
                if mailbox:
                    get_db().record_mailbox_verification(mailbox["id"])
                return json_response({"ok": True, **result})
        except ApiError:
            raise
        except Exception as exc:
            if target == "mailbox":
                mailbox = get_db().get_mailbox(user["id"])
                if mailbox:
                    get_db().record_mailbox_verification(mailbox["id"], error=str(exc))
            elif target in {"model", "search"}:
                get_db().record_connection_result(user["id"], target, error=str(exc))
            raise ApiError(400, str(exc)) from exc
    raise ApiError(404, "未知的测试目标。")


def _split_tasks(tasks: list[dict[str, Any]], states: dict[str, dict[str, Any]],
                 day: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split freshly derived tasks into (still open, already handled).

    ``states`` is the user's own record of what they ticked off. A task whose
    wording changed since then simply has no matching key, so it comes back --
    which is the point of keying on content: a rewritten action is a new
    question, and hiding it behind an answer to the old one would be wrong.
    """
    open_tasks: list[dict[str, Any]] = []
    done_tasks: list[dict[str, Any]] = []
    for task in tasks:
        state = states.get(task["task_key"])
        if state and state["state"] == "done":
            done_tasks.append({**task, "done_at": state["done_at"]})
        else:
            open_tasks.append(task)
    return open_tasks, done_tasks


def _archived_tasks(states: dict[str, dict[str, Any]], day: str,
                    seen: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Tasks recorded for a day that can no longer be rebuilt from the reports.

    Reconnecting a mailbox purges its messages, and a report whose message is
    gone drops out of the day's join. Without this the archive would lose
    exactly the rows it exists to prove -- "I handled this" -- so the stored
    snapshot is replayed instead. Marked ``archived`` so the UI can say the
    original mail is no longer available.
    """
    open_tasks: list[dict[str, Any]] = []
    done_tasks: list[dict[str, Any]] = []
    for key, state in states.items():
        if key in seen or state["task_day"] != day:
            continue
        entry = {
            "task_key": key, "task_day": state["task_day"], "subject": state["subject"],
            "action": state["action"], "deadline": state["deadline"], "priority": state["priority"],
            "sender": state["sender"], "received_display": "", "message_id": state["message_id"],
            "done_at": state["done_at"], "archived": True,
        }
        (done_tasks if state["state"] == "done" else open_tasks).append(entry)
    return open_tasks, done_tasks


def _local_window(timezone: str, now: dt.datetime | None = None,
                  day: str = "") -> tuple[str, str, dt.datetime, str]:
    """(start_utc, end_utc, local_now, local_date) for one user's local day.

    ``day`` (``YYYY-MM-DD``) picks an earlier local day; anything unparseable
    falls back to today rather than erroring, so a stale bookmark or a typo in a
    URL cannot turn a read-only page into a 500.
    """
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        zone = ZoneInfo("Asia/Hong_Kong")
    local_now = (now or dt.datetime.now(dt.timezone.utc)).astimezone(zone)
    start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    if day:
        try:
            start = start.replace(year=int(day[0:4]), month=int(day[5:7]), day=int(day[8:10]))
        except (ValueError, IndexError):
            start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(days=1)
    return (
        start.astimezone(dt.timezone.utc).isoformat(timespec="seconds"),
        end.astimezone(dt.timezone.utc).isoformat(timespec="seconds"),
        local_now,
        start.date().isoformat(),
    )


def _next_daily_run(daily_time: str, timezone: str, local_now: dt.datetime) -> dt.datetime:
    try:
        hour, minute = [int(part) for part in str(daily_time or "22:00").split(":", 1)]
    except (TypeError, ValueError):
        hour, minute = 22, 0
    candidate = local_now.replace(hour=min(max(hour, 0), 23), minute=min(max(minute, 0), 59),
                                  second=0, microsecond=0)
    if candidate <= local_now:
        candidate += dt.timedelta(days=1)
    return candidate


def build_dashboard(user: dict[str, Any]) -> dict[str, Any]:
    """Everything the "clear action" home screen needs, in one response.

    No network calls are made here: a per-request IMAP or model probe would make
    the dashboard slow and would hammer providers. Real checks happen only when
    the user presses a test button, whose result is stored and reported as a
    verified-at timestamp instead of being implied.
    """
    db = get_db()
    profile = db.get_profile(user["id"]) or {}
    mailbox = db.get_mailbox(user["id"])
    own_model = db.get_connection(user["id"], "model")
    # Falling back here as well as in the worker matters: if the dashboard said
    # "还没有配置 AI 模型" while reports were being generated with the pilot key,
    # the screen would be telling the user to fix something that is not broken.
    model = own_model or providers.platform_model_default()
    # Same reason as the model fallback just above: with the pilot's search key the
    # dashboard must not say "未配置" while citations are working.
    search = db.get_connection(user["id"], "search") or providers.platform_search_default()
    timezone = profile.get("timezone") or "Asia/Hong_Kong"
    start_utc, end_utc, local_now, local_date = _local_window(timezone, None)

    rows = db.today_reports(user["id"], start_utc, end_utc)
    service = get_service()
    tasks = reports_mod.today_tasks(
        [(row["id"], service.decrypt_report(row["body_markdown"], user["id"]), row["message_id"]) for row in rows],
        [{"id": row["message_id"], "subject": row["message_subject"], "sender_name": row["sender_name"],
          "sender_address": row["sender_address"], "received": row["received_at"],
          "importance": row["importance"]} for row in rows],
        timezone=timezone,
    )
    states = db.task_states(user["id"])
    tasks_open, tasks_done = _split_tasks(tasks, states, local_date)
    # Everything downstream ("today has N things to do", the next-step nudge)
    # counts only what is still open. A task the user just ticked off must stop
    # being the headline immediately, otherwise the dashboard argues with them.
    tasks = tasks_open
    recent = [
        {
            "subject": row["message_subject"],
            "sender": row["sender_name"] or row["sender_address"],
            "priority": reports_mod.derive_priority(
                reports_mod.parse_sections(service.decrypt_report(row["body_markdown"], user["id"])).get("importance", ""),
                row["importance"],
            ),
            "received": row["received_at"],
            "received_display": reports_mod.format_moment(row["received_at"], timezone),
            "status": row["status"],
        }
        for row in rows[:5]
    ]

    verified_at = mailbox.get("last_verified_at") if mailbox else None
    verify_error = (mailbox or {}).get("last_verify_error") or ""
    send_error = (mailbox or {}).get("last_error") or ""
    fresh = False
    moment = reports_mod.to_local(verified_at, "UTC")
    if moment and (dt.datetime.now(dt.timezone.utc) - moment).total_seconds() < 24 * 3600:
        fresh = True
    if not mailbox:
        mailbox_state, mailbox_detail = "missing", "还没有填写私人转发邮箱。"
    elif verify_error:
        mailbox_state, mailbox_detail = "error", verify_error
    elif fresh:
        mailbox_state, mailbox_detail = "ok", f"最近一次直连检查成功（{reports_mod.format_moment(verified_at, timezone)}）。"
    elif verified_at:
        mailbox_state, mailbox_detail = "stale", "上次检查已超过 24 小时，建议重新检查一次。"
    else:
        mailbox_state, mailbox_detail = "unknown", "还没有检查过，点下面的按钮确认一次。"

    native_search = bool(model and supports_native_search(model["provider"]))
    if model and model.get("platform"):
        # Say whose key it is and who is paying, because that is the sentence the
        # landing page and the privacy policy already promised the user would see.
        # 供应商读的是**给人看的名字**（`providers.label_for`）：这一档 2026-09-22 起是
        # 本机那台盒子，而「谁在处理我的邮件」正是隐私政策让用户有权知道的事——
        # `local_openai` 这种内部 id 摆在这里等于没说。
        model_state = "ok"
        model_detail = (f"{providers.label_for(model['provider'])} · {model['model']}，"
                        f"在另行通知前用管理员提供的 key，你不花钱。想换成自己的，在下面填一次即可覆盖。")
    elif model:
        model_state = "error" if model.get("last_error") else "ok"
        model_detail = (f"已配置 {model['provider']}" + (f" · {model['model']}" if model["model"] else "")
                        + (f"　上次出错：{model['last_error']}" if model.get("last_error") else ""))
    else:
        model_state, model_detail = "missing", "还没有配置 AI 模型。"
    if native_search:
        search_state = "ok"
        search_detail = f"{model['provider']} 自带联网搜索，第 4 步可以跳过。"
    elif search and search.get("platform"):
        search_state = "ok"
        search_detail = (f"{search['provider']}，在另行通知前用管理员提供的搜索 key，你不花钱。"
                         "想换成自己的，在下面填一次即可覆盖。")
    elif search:
        search_state = "error" if search.get("last_error") else "ok"
        search_detail = f"已配置 {search['provider']}" + (f"　上次出错：{search['last_error']}" if search.get("last_error") else "")
    else:
        search_state, search_detail = "optional", "未配置：报告仍会生成，只是没有联网核实来源。"

    immediate_enabled = bool(profile.get("immediate_enabled", 1))
    daily_enabled = bool(profile.get("daily_enabled", 1))

    analysed_any = db.count_analysed_messages(user["id"]) > 0
    # 「一封 CityU 来信都没到过」这句话只有一个出处（`Database.forwarding_step`）：
    # 门槛、算多久、算哪些邮件，全项目一份。这里以前自己写了一个两小时的冷启动
    # 判断，于是同一个事实在设置向导和首页上有两种说法、两个门槛 —— 而这一格恰恰
    # 是唯一无法从我们这边验证的一步，说法不一致时没人能判断哪个是真的。
    forwarding = db.forwarding_step(
        {"mailbox_updated_at": (mailbox or {}).get("updated_at")}, 0)
    next_run = _next_daily_run(profile.get("daily_time") or "22:00", timezone, local_now)

    if not profile.get("school_email") or not profile.get("major"):
        next_step = {"kind": "profile", "title": "先补充个人资料", "detail": "填写 CityU 学校邮箱和专业，报告才能判断相关性。", "action": "去填写"}
    elif not mailbox:
        next_step = {"kind": "mailbox", "title": "设置私人转发邮箱", "detail": "在 CityU Outlook 里把邮件转发到你的私人邮箱，再把授权码填到这里。", "action": "去设置"}
    elif not fresh:
        next_step = {"kind": "verify", "title": "确认邮箱可以收信", "detail": "点一次只读连接检查；不会删除或改动你的邮件。", "action": "立即检查"}
    elif not model:
        next_step = {"kind": "model", "title": "配置 AI 模型 API", "detail": "填入你自己的模型 key，之后每封新邮件都会生成摘要。", "action": "去配置"}
    elif not analysed_any and forwarding["state"] == "warn":
        # Setup is complete and the mailbox answers, but not one allowed-sender
        # mail has ever arrived *and* it has had long enough to arrive. Note the
        # condition no longer asks whether report mail is switched on: turning
        # delivery off does not stop us reading the mailbox (that was the whole
        # point of v0.63.85), so "your forwarding has never worked" is still the
        # most useful thing to say. Saying
        # "一切就绪" here is the one thing that would leave a new user stuck
        # without knowing it: the forwarding rule is the only step we cannot
        # verify from our side. The detail is the shared sentence (see
        # `Database.forwarding_step`); the title and the action are this card's.
        next_step = {
            "kind": "mailbox",
            "title": "还没有收到过 CityU 邮件",
            "detail": forwarding["detail"],
            "action": "检查转发设置",
            "tone": "warn",
        }
    elif tasks:
        first = tasks[0]
        next_step = {"kind": "task", "title": f"今天有 {len(tasks)} 件事要处理", "detail": first["action"]
                     + (f"（截止：{first['deadline']}）" if first["deadline"] else ""),
                     "action": "查看待办"}
    else:
        next_step = {"kind": "done", "title": "一切就绪，没有待处理事项",
                     "detail": f"下一封新邮件到达会自动生成摘要；每日简报下次在 {next_run:%H:%M} 发出。",
                     "action": "查看报告记录"}

    announcement = db.active_announcement_for(user["id"])
    return {
        # 「这封信还能去哪儿看」的兜底去处（学校邮箱 + 转发邮箱）。放在首页响应里，是因为
        # **取不到原信时没有别的响应体能带它**——那时客户端就得自己拼域名，那就是第二份
        # 规则。真正的接口会在同一条规则上再加一条「Gmail 精确到那一封」（它要 Message-ID，
        # 首页这份没有）。两处都调 `original_links`，判据只有一处。
        "look_here": original_links(str((mailbox or {}).get("email") or ""),
                                    str(profile.get("school_email") or ""), "",
                                    school_mail=True),
        # The broadcast rides on the dashboard response so it is on screen the
        # moment a user opens the app — no second request, no flicker.
        "announcement": (
            {"id": announcement["id"], "title": announcement["title"], "body": announcement["body"],
             "tone": announcement["tone"], "created_at": announcement["created_at"],
             # 配图（如果有）。只给 URL，不给字节：对话框用 <img src> 取，浏览器自己缓存，
             # 也不会让每一次 /api/dashboard 都背上几百 KB 的 base64。
             # 用**图片自己的 id**（和布告栏那条路一致）：同一个资源两种地址，
             # 迟早有一处按另一种写法去比对而查不到。
             "image_url": (f"/announcement-image/{db.announcement_image(announcement['id'])['id']}"
                           if db.announcement_image(announcement["id"]) else ""),
             "created_display": reports_mod.format_moment(announcement["created_at"], timezone)}
            if announcement else None
        ),
        # How many are still waiting. The modal shows one at a time, so without
        # this the card appearing right after a confirmation looks exactly like
        # the one just dismissed -- users reported that as "点确认没有反应"
        # (2026-09-16). With the count the button can say how many are left.
        "announcement_pending": db.count_pending_announcements(user["id"]),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "local_date": local_date,
        "local_display": f"{local_now.month}月{local_now.day}日 {reports_mod.weekday_label(local_now)}",
        "greeting": reports_mod.greeting_for(local_now),
        "next_run": next_run.isoformat(timespec="seconds"),
        "next_run_display": f"{next_run.month}月{next_run.day}日 {next_run:%H:%M}",
        "next_step": next_step,
        "channels": {
            "mailbox": {"state": mailbox_state, "detail": mailbox_detail,
                        "verified_at": verified_at, "label": "邮箱收信"},
            "model": {"state": model_state, "detail": model_detail, "label": "AI 摘要"},
            "search": {"state": search_state, "detail": search_detail, "label": "联网搜索",
                       "native": native_search},
            "digest": {
                "state": "ok" if daily_enabled else "optional",
                "detail": (f"下次自动发出：{next_run.month}月{next_run.day}日 {next_run:%H:%M}（{timezone}）。"
                           if daily_enabled else
                           "每日简报已关闭——报告仍然照常生成，在「报告」里看。"),
                "label": "每日简报",
            },
            # 「报告邮件」这一格是给**忘了自己关过**的人看的：关掉之后我们不再发任何
            # 报告邮件，而"邮箱里什么都没有"和"坏了"长得一模一样。所以它必须出现在
            # 首页的通道栏里，并且明说报告还在 App 里。
            "report_mail": {
                "state": "ok" if (immediate_enabled or daily_enabled) else "optional",
                "detail": ("即时摘要与每日简报都会发到你的邮箱。"
                           if (immediate_enabled and daily_enabled) else
                           ("只发每日简报，即时摘要已关闭。" if daily_enabled else
                            ("只发即时摘要，每日简报已关闭。" if immediate_enabled else
                             "已关闭：报告照常生成，只在 App 里看，不发邮件。"))),
                "label": "报告邮件",
            },
        },
        # Which of the four setup steps are actually done, so the setup page can
        # say what is still missing instead of only what is wrong. Four of the
        # seven production accounts stalled before configuring a mailbox at all,
        # and nothing on that page could tell them so.
        "setup": db.setup_progress(user["id"]),
        "today": {
            "messages": len(rows),
            "tasks": len(tasks_open),
            "tasks_done": len(tasks_done),
            "failed": sum(1 for row in rows if row["status"] == "failed"),
            "sent": sum(1 for row in rows if row["status"] == "sent"),
            "immediate_enabled": immediate_enabled,
            "daily_enabled": daily_enabled,
        },
        "tasks": tasks_open[:8],
        "tasks_done": [dict(task) for task in tasks_done[:20]],
        "recent": recent,
        "send_error": send_error,
    }


@route("GET", "/api/dashboard")
def dashboard(request: Request) -> Response:
    user = _require_user(request)
    return json_response(build_dashboard(user))


# --------------------------------------------------------------------------
# daily tasks: hide one, find it again later
# --------------------------------------------------------------------------


def task_day_view(user: dict[str, Any], day: str = "") -> dict[str, Any]:
    """One local day's action items, split into open and handled.

    Rebuilt from the reports every time; ``task_states`` only decides which of
    them the user has hidden. Nothing the model wrote is ever destroyed, which
    is what makes hiding safe and reversible.
    """
    database = get_db()
    profile = database.get_profile(user["id"])
    timezone = profile.get("timezone") or "Asia/Hong_Kong"
    start_utc, end_utc, _local_now, local_date = _local_window(timezone, day=day)
    rows = database.today_reports(user["id"], start_utc, end_utc)
    service = get_service()
    derived = reports_mod.today_tasks(
        [(row["id"], service.decrypt_report(row["body_markdown"], user["id"]), row["message_id"])
         for row in rows],
        [{"id": row["message_id"], "subject": row["message_subject"], "sender_name": row["sender_name"],
          "sender_address": row["sender_address"], "received": row["received_at"],
          "importance": row["importance"]} for row in rows],
        timezone=timezone,
    )
    states = database.task_states(user["id"])
    open_tasks, done_tasks = _split_tasks(derived, states, local_date)
    seen = {task["task_key"] for task in derived}
    archived_open, archived_done = _archived_tasks(states, local_date, seen)
    open_tasks.extend(archived_open)
    done_tasks.extend(archived_done)
    for task in open_tasks + done_tasks:
        # Two different facts, kept apart: `priority` is what the report said,
        # `user_priority` is what the owner decided. The view ships both plus
        # the one that should be shown, so the browser never re-derives a rule
        # that the archive and the export also depend on.
        state = states.get(task["task_key"]) or {}
        task["user_priority"] = str(state.get("user_priority") or "")
        task["snoozed_until"] = str(state.get("snoozed_until") or "")
        task["effective_priority"] = taskexport.effective_priority(task)
        # `export_title` is the **clipboard** line, not the calendar's: the browser
        # pastes it into iOS 提醒事项 / Google Tasks (`app.js` 「复制成清单」), and a
        # checklist there wants the plain sentence. The calendar's prettier title
        # (emoji + ⏰) is produced inside `build_ics` and never travels through
        # here -- one field, one consumer, or the emoji quietly ends up pasted
        # into somebody's Reminders (which is exactly what happened in this
        # feature's first cut, caught in review on 2026-09-19).
        task["export_title"] = taskexport.line_for(task)
    # 「稍后提醒」：还没到点的离开主列表，到点的**自己回来**。
    #
    # 判断放在**读**的时候，所以这里没有、也不该有任何定时任务：服务器重启、
    # worker 停摆、备份还原都不会漏掉那一刻。`snoozed_until <= now` 与空值走同一条
    # 路（都留在主列表），所以一个读不出来的时刻不会让一条待办凭空消失。
    now = dt.datetime.now(dt.timezone.utc)
    main_tasks: list[dict[str, Any]] = []
    snoozed_tasks: list[dict[str, Any]] = []
    for task in open_tasks:
        (snoozed_tasks if snooze.is_asleep(task, now=now) else main_tasks).append(task)
    open_tasks = main_tasks
    snoozed_tasks.sort(key=lambda item: snooze.parse_iso(item.get("snoozed_until")) or now)
    # The user's own ranking is the strongest signal there is, so it decides the
    # order of the open list; `sort` is stable, so tasks they have not touched
    # keep the report's ordering (importance, then deadline, then arrival).
    open_tasks.sort(key=lambda item: (reports_mod.priority_rank(item["effective_priority"]),
                                      0 if item["deadline"] else 1))
    return {
        "day": local_date,
        "is_today": local_date == _local_window(timezone)[3],
        "tasks": open_tasks,
        "snoozed": snoozed_tasks,
        "done": done_tasks,
        # `total` counts the ones that are only away for a while: they are still
        # part of that day's list, and the day's numbers must add up again when
        # they walk back in. The per-bucket counts keep their old shape (`open`
        # is the main list, so it is what the badge and 「需要行动」 read) -- the
        # snoozed rows travel in their own array.
        "counts": {"total": len(open_tasks) + len(snoozed_tasks) + len(done_tasks),
                   "open": len(open_tasks), "done": len(done_tasks)},
        "days": database.task_day_summaries(user["id"]),
        "priorities": [{"value": "", "label": "跟随来信判断"},
                       {"value": reports_mod.PRIORITY_HIGH, "label": "急"},
                       {"value": reports_mod.PRIORITY_MEDIUM, "label": "中"},
                       {"value": reports_mod.PRIORITY_LOW, "label": "缓"}],
    }


@route("GET", "/api/tasks")
def tasks_for_today(request: Request) -> Response:
    user = _require_user(request)
    return json_response(task_day_view(user))


@route("GET", r"/api/tasks/day/(?P<day>[0-9]{4}-[0-9]{2}-[0-9]{2})")
def tasks_for_day(request: Request, day: str) -> Response:
    user = _require_user(request)
    return json_response(task_day_view(user, day=day))


@route("PUT", r"/api/tasks/(?P<task_key>[0-9a-f]{32})")
def set_task(request: Request, task_key: str) -> Response:
    """Hide one task ("handled") or bring it back.

    The stored row is built from the server's own derived task, not from the
    request body: the client sends only the decision, so a tampered payload
    cannot plant arbitrary text in the user's archive. If the task can no longer
    be derived (its mail was purged) the previously stored snapshot is reused,
    which is what lets an old entry still be reopened.
    """
    user = _require_user(request)
    payload = request.json_object()
    state = _string(payload, "state", minimum=1, maximum=20)
    if state not in {"done", "open"}:
        raise ApiError(422, "无效的任务状态。")
    day = _string(payload, "day", default="", required=False, maximum=20)
    database = get_db()
    # A previously stored decision already carries the snapshot, which is the
    # only way to act on a task whose source mail has since been purged.
    snapshot: dict[str, Any] | None = database.task_states(user["id"]).get(task_key)
    view = task_day_view(user, day=day or (snapshot or {}).get("task_day", "") or "")
    for task in view["tasks"] + view["done"]:
        if task["task_key"] == task_key:
            snapshot = task
            break
    if snapshot is None:
        raise ApiError(404, "找不到这个任务。")
    database.set_task_state(user["id"], task_key, state, snapshot)
    view = task_day_view(user, day=day or snapshot.get("task_day", "") or "")
    return json_response({**view, "changed": task_key, "state": state})


@route("PUT", r"/api/tasks/(?P<task_key>[0-9a-f]{32})/priority")
def set_task_priority(request: Request, task_key: str) -> Response:
    """Set (or clear) the user's own 轻重缓急 for one task.

    A route of its own rather than one more key on the state endpoint: handling
    a task and re-ranking it are different decisions, and folding them together
    is how a re-rank would silently un-handle something.

    Only the ranking is taken from the request body -- the stored snapshot comes
    from the server's own derived task, for the reason that endpoint explains.
    """
    user = _require_user(request)
    payload = request.json_object()
    # The key is required even though "" is a legitimate value (it means "go back
    # to the mail's own reading"). A body that merely forgot it must not silently
    # erase a ranking the user set on purpose -- the same rule the admin-note
    # endpoint spells out, for the same reason: that loss is indistinguishable
    # from a successful save.
    if not isinstance(payload.get("priority"), str):
        # Covers both "the key is missing" and "it is null": `""` is the one and
        # only way to say "clear it", which keeps a client bug from quietly
        # erasing a ranking somebody set by hand.
        raise ApiError(422, "priority 必须是 high / medium / low / 空字符串。")
    priority = _string(payload, "priority", default="", required=False, maximum=20)
    if priority not in {"", reports_mod.PRIORITY_HIGH, reports_mod.PRIORITY_MEDIUM,
                        reports_mod.PRIORITY_LOW}:
        raise ApiError(422, "无效的优先级。")
    day = _string(payload, "day", default="", required=False, maximum=20)
    database = get_db()
    snapshot: dict[str, Any] | None = database.task_states(user["id"]).get(task_key)
    view = task_day_view(user, day=day or (snapshot or {}).get("task_day", "") or "")
    for task in view["tasks"] + view["done"]:
        if task["task_key"] == task_key:
            snapshot = task
            break
    if snapshot is None:
        raise ApiError(404, "找不到这个任务。")
    database.set_task_priority(user["id"], task_key, priority, snapshot)
    view = task_day_view(user, day=day or snapshot.get("task_day", "") or "")
    return json_response({**view, "changed": task_key, "user_priority": priority})


@route("PUT", "/api/tasks/snooze")
def snooze_task(request: Request) -> Response:
    """「稍后提醒」：把一条待办送走一阵子，到点它自己回来。

    Its own endpoint, for the two reasons the neighbours already spell out:

    * **not** one more key on ``PUT /api/profile`` -- that route overwrites every
      field with defaults (铁律 4), so saving a snooze through it would silently
      reset the rest of the profile;
    * **not** one more key on ``PUT /api/tasks/<key>`` -- that one's ``state`` is
      done/open, and a third value there would quietly change the meaning of every
      query and of the daily brief.

    ``until`` takes the three presets (``1h`` / ``tonight`` / ``tomorrow``), an ISO
    moment, or ``""`` to call the task back now. The result is clamped server-side
    to 5 minutes .. 30 days: a client clock can be wrong, and a moment in the past
    would look like "the button does nothing" while one three years out would look
    like the task was deleted.

    Only the user's own ``task_key`` is accepted; somebody else's is a 404, the
    same boundary ``/api/admin/*`` uses (an unknown key and a foreign one must be
    indistinguishable, or the endpoint becomes an oracle for what other people
    have in their list).
    """
    user = _require_user(request)
    payload = request.json_object()
    task_key = _string(payload, "task_key", minimum=1, maximum=64)
    if not re.fullmatch(r"[0-9a-f]{32}", task_key):
        # The path route refuses a malformed key by not matching it at all; here
        # the key arrives in the body, so the shape has to be checked by hand.
        raise ApiError(422, i18n.mark("无效的任务编号。"))
    if not isinstance(payload.get("until"), str):
        # Covers "missing" and "null" together. `""` is the one and only way to
        # say "cancel", so a client that merely forgot the field must not look
        # like a successful cancel -- that loss is invisible afterwards.
        raise ApiError(422, i18n.mark("until 必须是字符串：1h / tonight / tomorrow / ISO 时间 / 空字符串。"))
    profile = get_db().get_profile(user["id"]) or {}
    timezone = str(profile.get("timezone") or "Asia/Hong_Kong")
    try:
        until = snooze.resolve(payload.get("until"), now=dt.datetime.now(dt.timezone.utc),
                               timezone=timezone)
    except snooze.InvalidSnooze:
        raise ApiError(422, i18n.mark("无效的稍后提醒时间。"))
    day = _string(payload, "day", default="", required=False, maximum=20)
    database = get_db()
    snapshot: dict[str, Any] | None = database.task_states(user["id"]).get(task_key)
    view = task_day_view(user, day=day or (snapshot or {}).get("task_day", "") or "")
    # The snapshot comes from the server's own derived task (never the request
    # body), exactly like the two endpoints above: a tampered payload cannot
    # plant text in the archive.
    for task in view["tasks"] + view["snoozed"] + view["done"]:
        if task["task_key"] == task_key:
            snapshot = task
            break
    if snapshot is None:
        raise ApiError(404, "找不到这个任务。")
    database.set_task_snooze(user["id"], task_key, until, snapshot)
    view = task_day_view(user, day=day or snapshot.get("task_day", "") or "")
    return json_response({**view, "changed": task_key, "snoozed_until": until})


@route("GET", "/api/tasks/export.ics")
def export_tasks_ics(request: Request) -> Response:
    """The selected tasks as an iCalendar file, for the phone's calendar app.

    A real download with ``text/calendar`` on it: iOS only hands the file to
    Calendar when the server says what it is (serving it as
    ``application/octet-stream`` is the documented way to produce a file that
    "cannot be opened"), and the extension alone is not enough.

    Reads only; the selection travels in the query string because the browser
    has to navigate to it to get a download at all.
    """
    user = _require_user(request)
    day = request.query.get("day", [""])[0]
    view = task_day_view(user, day=day)
    wanted = {value for value in ",".join(request.query.get("keys", [])).split(",") if value}
    everything = view["tasks"] + view["done"]
    chosen = [task for task in everything if task["task_key"] in wanted] if wanted else []
    if not chosen:
        # An empty calendar is a valid file that silently does nothing, and
        # "I pressed export and no task appeared" is the worst outcome here.
        raise ApiError(422, "没有选中任何任务。")
    # The user's own zone, the same one that decided which day "today" is:
    # a timed deadline (23:59 之类) must land on the wall-clock the user means.
    profile = get_db().get_profile(user["id"]) or {}
    timezone = str(profile.get("timezone") or "Asia/Hong_Kong")
    body = taskexport.build_ics(
        chosen, origin=os.environ.get("INFE_PILOT_ORIGIN", "").rstrip("/"),
        now=dt.datetime.now(dt.timezone.utc),
        today=dt.date.fromisoformat(view["day"]),
        timezone=timezone,
    ).encode("utf-8")
    return Response(
        status=200,
        body=body,
        content_type="text/calendar; charset=utf-8",
        headers={
            # No caching: the file is a snapshot of a list that changes, and a
            # stale copy in the phone's download list is a task the user thinks
            # they exported but did not.
            "Cache-Control": "no-store",
            "Content-Disposition": (
                'attachment; filename="' + taskexport.filename(view["day"]).replace('"', "") + '"'),
        },
    )


@route("POST", "/api/mailbox/verify")
def verify_mailbox(request: Request) -> Response:
    """Explicit, user-triggered read-only IMAP check.

    Read-only by construction (``fetch_new_messages`` opens the mailbox with
    ``readonly=True`` and never marks or deletes), and it never moves the UID
    cursor, so pressing this button cannot cause a duplicated or skipped report.
    """
    user = _require_user(request)
    db = get_db()
    mailbox = db.get_mailbox(user["id"])
    if not mailbox:
        raise ApiError(422, "请先保存私人转发邮箱，再检查连接。")
    if not _verification_allowed(user["id"]):
        raise ApiError(429, "刚刚检查过了，请一分钟后再试。")
    db.record_mailbox_verification(mailbox["id"])
    try:
        result = get_service().test_mailbox(user["id"])
    except Exception as exc:
        message = str(exc)
        db.record_mailbox_verification(mailbox["id"], error=message)
        raise ApiError(400, message) from exc
    return json_response({"ok": True, **result, "dashboard": build_dashboard(user)})


@route("GET", r"/api/messages/(?P<message_id>[^/]+)/original")
def message_original(request: Request, message_id: str) -> Response:
    """One original mail, read live from the mailbox and stored nowhere.

    The raw body is deleted the moment the report is delivered — that is a
    promise in the privacy policy, not an oversight — so this cannot be answered
    from our own tables. We kept `uid_validity` + `imap_uid`, which is enough to
    find that one message again in the mailbox the user already has.

    Two consequences the UI states plainly rather than hiding: it takes a second
    or two (a real IMAP round trip), and it can honestly fail — the mail may no
    longer be in the mailbox, or the mailbox may have been rebuilt.
    """
    user = _require_user(request)
    _original_rate_limit(user["id"])
    try:
        result = get_service().read_original(user["id"], message_id)
    except KeyError as exc:
        raise ApiError(404, "找不到这封邮件。") from exc
    except mailio_mod.MailError as exc:
        raise ApiError(400, str(exc)) from exc
    except Exception as exc:                     # 解密失败、磁盘、想不到的东西
        # 与 `verify_mailbox` 同一个口径：**照实报，别变成 500**。用户点了「看原信」，
        # 得到的应该是一句能读的话；500 只会让人以为整个软件坏了。
        raise ApiError(400, f"取这一封时出错了：{exc}") from exc
    state = result.get("state")
    if state == mailio_mod.ORIGINAL_GONE:
        raise ApiError(404, "这封信已经不在你的邮箱里了（可能被删掉或移到别的文件夹）。")
    if state == mailio_mod.ORIGINAL_MOVED:
        raise ApiError(410, "这个邮箱重建过，我们已经无法确定哪一封是它了——请直接在邮箱里查看。")
    message = result.get("message") or {}
    db = get_db()
    mailbox = db.get_mailbox(user["id"]) or {}
    profile = db.get_profile(user["id"]) or {}
    # 「还能去哪儿看」。学校邮箱那条只有填过学校邮箱才给；Gmail 那条要 Message-ID。
    row = db.message_for_user(user["id"], message_id) or {}
    return json_response({
        "ok": True,
        "live": True,           # 界面据此写「实时读取、服务器不留存」
        "subject": message.get("subject", ""),
        "sender_name": message.get("sender_name", ""),
        "sender_address": message.get("sender_address", ""),
        "received": message.get("received", ""),
        "body": message.get("body", ""),
        "truncated": bool(result.get("truncated")),
        # `school_mail`：这封信的发件域在允许名单里，也就是说它**就是从学校邮箱转过来的**
        # （我们能读到的每一封信都是）。有这条证据就不必再要求用户先填过学校邮箱——
        # 早期反馈里那位用户看不到这一格，正是因为第一版把它挂在了"填过资料吗"上。
        "look_here": original_links(str(mailbox.get("email") or ""),
                                    str(profile.get("school_email") or ""),
                                    str(row.get("message_key") or ""),
                                    school_mail=service_mod.is_allowed_sender(
                                        str(message.get("sender_address") or ""))),
    })


@route("POST", r"/api/messages/(?P<message_id>[^/]+)/assist")
def message_assist(request: Request, message_id: str) -> Response:
    """翻译 / 总结**这一封原信**（按需、不保存）。

    它和「看原信」是同一件事的两半：先把那一封只读取回来，再把正文交给模型。
    所以取不到的三种情形说一样的话；区别在于这一步**会花钱**、而且**正文会离开
    我们的服务器**（隐私政策里「正文会发给模型服务商」那一段同样适用），因此：
    用户点一次才发生一次、按人限流、结果只回给这一次请求、用量照记。
    """
    user = _require_user(request)
    payload = request.json_object()
    kind = _string(payload, "kind", minimum=1, maximum=20)
    if kind not in service_mod.PilotService.ASSIST_KINDS:
        raise ApiError(422, "不支持的助手动作。")
    _assist_rate_limit(user["id"])
    try:
        result = get_service().assist(user["id"], message_id, kind)
    except KeyError as exc:
        raise ApiError(404, "找不到这封邮件。") from exc
    except providers.ProviderError as exc:
        raise ApiError(400, str(exc)) from exc
    except mailio_mod.MailError as exc:
        raise ApiError(400, str(exc)) from exc
    except Exception as exc:
        raise ApiError(400, f"这一步没做成：{exc}") from exc
    state = result.get("state")
    if state == mailio_mod.ORIGINAL_GONE:
        raise ApiError(404, "这封信已经不在你的邮箱里了（可能被删掉或移到别的文件夹）。")
    if state == mailio_mod.ORIGINAL_MOVED:
        raise ApiError(410, "这个邮箱重建过，我们已经无法确定哪一封是它了——请直接在邮箱里查看。")
    return json_response({
        "ok": True,
        "live": True,
        "kind": result.get("kind", kind),
        "text": result.get("text", ""),
        # `state`/`note` 是**如实报告**那一半：译文被截断、或者这次根本没翻出来
        # （模型把英文原文抄了回来），都要让用户看见，而不是显示成一次成功。
        "state": result.get("state", "ok"),
        "note": result.get("note", ""),
        "model": result.get("model", ""),
    })


@route("GET", "/api/account/export")
def account_export(request: Request) -> Response:
    """Download everything we hold about the caller, as one JSON file.

    Reading your own data is a right the privacy policy advertises, so it has to
    be a button that works rather than a promise to email us. Report bodies are
    decrypted here because the export is for the user, not for us; credentials
    are excluded by ``export_user_data``.
    """
    user = _require_user(request)
    service = get_service()
    data = get_db().export_user_data(user["id"])
    for row in data["reports"]:
        row["body_markdown"] = service.decrypt_report(row["body_markdown"], user["id"])
    data["exported_at"] = utc_now()
    data["format"] = "cityu-mail-pilot-export/1"
    body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
    return Response(
        status=200,
        body=body,
        content_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="cityu-mail-pilot-{stamp}.json"',
            "Cache-Control": "no-store",
        },
    )


@route("GET", "/api/usage")
def my_usage(request: Request) -> Response:
    """What this account's own model calls cost, and who paid for them.

    The account's own data and nothing else: `usage_for_user` is keyed on the
    session user and there is no id parameter, so there is no shape of this
    request that can read somebody else's usage. The admin view stays where it
    was (`/api/admin/usage`), which is the one that may look at everyone.

    **The split is the whole point.** During the pilot the operator pays for any
    account using the instance key, so one "you spent $0.42" line would be false
    for most of them and "you spent $0" would be false for the ones who brought
    their own key. Three buckets, plus "not recorded" for calls made before the
    column existed -- see `Database.record_usage`.
    """
    user = _require_user(request)
    database = get_db()
    days = request.query_int("days", 30)
    profile = database.get_profile(user["id"]) or {}
    # The user reads dates in their own timezone, and a UTC bucket would move an
    # evening's usage into the next date on their screen.
    offset = reports_mod.local_day_offset_hours(profile.get("timezone"))
    page = database.usage_for_user(user["id"], days=days, timezone_offset_hours=offset)
    page["currency_note"] = "费用是按服务商公开价目表估算的，不是账单；实际以服务商的结算为准。"
    page["payer_labels"] = {
        "platform": "平台代付（在另行通知前由管理员承担）",
        "own": "你自己的 key",
        "unknown": "早期记录（没有区分是谁付的）",
    }
    return json_response(page)


@route("GET", "/api/reports")
def reports(request: Request) -> Response:
    user = _require_user(request)
    rows = get_db().list_reports(user["id"], request.query_int("limit", 30))
    service = get_service()
    for row in rows:
        row["body_markdown"] = service.decrypt_report(row["body_markdown"], user["id"])
    return json_response(rows)


@route("PUT", r"/api/reports/(?P<report_id>[^/]+)/feedback")
def feedback(request: Request, report_id: str) -> Response:
    user = _require_user(request)
    payload = request.json_object()
    rating = _string(payload, "rating", minimum=1, maximum=40)
    note = _string(payload, "note", default="", required=False, maximum=1000)
    try:
        get_db().upsert_feedback(user["id"], report_id, rating, note)
    except (KeyError, ValueError) as exc:
        raise ApiError(404, str(exc)) from exc
    return json_response({"ok": True})


@route("PUT", r"/api/account/status/(?P<status>[A-Za-z]+)")
def account_status(request: Request, status: str) -> Response:
    user = _require_user(request)
    if status not in {"active", "paused", "deleted"}:
        raise ApiError(422, "无效状态。")
    get_db().set_user_status(user["id"], status)
    cookies = [_expired_cookie()] if status == "deleted" else None
    return json_response({"ok": True, "status": status}, cookies=cookies)


# --------------------------------------------------------------------------
# account security: password change and session revocation
# --------------------------------------------------------------------------


def _current_digest(request: Request) -> str | None:
    token = request.cookie(SESSION_COOKIE)
    return token_hash(token) if token else None


@route("GET", "/api/account/security")
def account_security(request: Request) -> Response:
    user = _require_user(request)
    return json_response({
        "email": user["email"],
        "session_days": SESSION_DAYS,
        "active_sessions": get_db().count_sessions(user["id"]),
    })


@route("PUT", "/api/account/password")
def change_password(request: Request) -> Response:
    """Change the password and revoke every other session.

    A password change is exactly the moment a borrowed or stolen device must
    lose access, so this is not optional: all sessions except the caller's are
    deleted, and the caller keeps working with a freshly minted cookie.
    """
    user = _require_user(request)
    _admin_rate_limit(f"password:{user['id']}")
    payload = request.json_object()
    current = _string(payload, "current_password", minimum=1, maximum=400)
    new_password = _string(payload, "new_password", minimum=12, maximum=400)
    database = get_db()
    record = database.find_user_for_login(user["email"])
    if not record or not verify_password(current, record["password_hash"]):
        raise ApiError(400, "当前密码不正确。")
    if verify_password(new_password, record["password_hash"]):
        raise ApiError(422, "新密码不能与当前密码相同。")
    database.set_password(user["id"], hash_password(new_password))
    removed = database.revoke_sessions(user["id"])
    database.record_audit(action="password_changed", actor_user_id=user["id"],
                          actor_email=user["email"], target_user_id=user["id"],
                          target_email=user["email"], detail=f"revoked={removed}",
                          client=_client_label(request))
    logging.info("password changed for user %s; %s other session(s) revoked", user["id"], removed)
    # The caller's own session was revoked too, so hand them a new cookie.
    return json_response({"ok": True, "revoked": removed}, cookies=[_session_cookie(user["id"])])


@route("POST", "/api/account/sessions/revoke")
def revoke_own_sessions(request: Request) -> Response:
    """Sign out every device, including this one (a new cookie is issued)."""
    user = _require_user(request)
    _admin_rate_limit(f"revoke:{user['id']}")
    removed = get_db().revoke_sessions(user["id"])
    get_db().record_audit(action="signed_out_all_devices", actor_user_id=user["id"],
                          actor_email=user["email"], target_user_id=user["id"],
                          target_email=user["email"], detail=f"revoked={removed}",
                          client=_client_label(request))
    return json_response({"ok": True, "revoked": removed}, cookies=[_session_cookie(user["id"])])


# --------------------------------------------------------------------------
# admin console
#
# Everything below requires an email listed in INFE_PILOT_ADMIN_EMAILS. The
# check is server-side only, operators cannot be created from the web, and no
# response ever contains an encrypted password, API key or invite hash.
# --------------------------------------------------------------------------


MAX_USERS_SETTING = "max_users"


def _max_users() -> tuple[int, str]:
    """The effective pilot cap, and where it came from.

    The stored setting wins over the environment. ``pilot.env`` is 0600 root and
    is read once at process start, so an operator who wants to raise the cap
    while the service runs has nowhere else to put it; the environment value
    stays as the install-time default. The source is reported so the admin panel
    can say which one is in force instead of leaving the operator guessing why
    an edit to pilot.env appeared to do nothing.
    """
    default = max(1, int(os.environ.get("INFE_PILOT_MAX_USERS", "5")))
    stored = get_db().get_setting(MAX_USERS_SETTING, "").strip()
    if stored:
        try:
            return max(1, min(1000, int(stored))), "settings"
        except ValueError:
            pass
    return default, "environment"


# 健康卡现在以「信有没有到」为主，而不是「我们登进去了几个」。窗口固定 24 小时：
# 学校工作日发信、周末安静，一天是既能看见问题又不会把周末当故障的长度。
DELIVERY_WINDOW_HOURS = 24
# 哪些状态排在最前面：要人动手的在上，已暂停的沉底。
DELIVERY_ORDER = {"broken": 0, "stale": 1, "no_mail": 2, "ok": 3, "paused": 4}


def _poll_freshness_seconds(row: dict[str, Any]) -> float:
    """How long a mailbox may go without a poll before the card calls it stale.

    Derived from the interval that mailbox is **actually polled at**
    (`worker.poll_interval_for`: the configured `INFE_PILOT_POLL_SECONDS`, or the
    provider's own slower floor for Gmail), doubled to allow for one slow round,
    with a floor so a fast mailbox is not called stale between two heartbeats.

    It used to reuse ``alerting.stale_after_for`` -- the *alert* threshold, an
    hour for QQ -- and the operator reported the consequence in plain words:
    「收信正常的更新频率太慢了，一直只有 2 个」. The number was not wrong, it was
    answering a different question: "should this wake somebody up" instead of
    "is this mailbox being collected right now". A dashboard that only moves
    once an hour is a dashboard nobody trusts.

    **And then it happened again, the other way round** (2026-09-24, the
    operator: 「为什么显示只有 2 个正常收信」). The second version derived the
    window from the *provider floor* with a 60-second fallback -- correct while
    the site polled every 60 s, and wrong the moment that day's earlier change
    moved the poll interval to 300 s: the window stayed 180 s, so for 120 of
    every 300 seconds **every** QQ/163/126 mailbox looked "stopped" and dropped
    out of the count. The only survivors were the two Gmail mailboxes (their
    floor is 900 s, so their window was 1800 s) -- which is exactly the "2" he
    saw. Two lessons, both now structural: the window must come from the
    interval the poller *uses* (never from a constant read off one provider's
    documentation), and a changed poll interval has to be followed by whoever
    renders freshness. `test_admin.FreshnessWindowTests` pins both.
    """
    try:
        interval = float(worker_mod.poll_interval_for({"imap_host": row.get("imap_host")}))
    except Exception:  # pragma: no cover - a malformed row must not break the card
        interval = 60.0
    return max(180.0, interval * 2.0)


def _mailbox_delivery_rows(database, boxes: list[dict[str, Any]], now: dt.datetime,
                           ) -> dict[str, Any]:
    """What each mailbox can *prove*, and the totals across them.

    The operator's complaint that produced this: 「收信正常那里一直显示 4，为什么每次
    都会这样，我要换一个方式来确定正常情况」. He was right twice over.

    **The number was wrong.** It was `considered - stale - broken` computed as two
    subtractions, so a mailbox that is both (which is exactly what a wrong
    authorisation code produces: it stops polling *and* it has an error) was
    subtracted twice and the count came out one too low. It also counted
    mailboxes whose **owner is paused** -- we stop polling those on purpose, so
    they are neither healthy nor stale, and the sentinel has always excluded them.

    **And it answered the wrong question.** "How many mailboxes did we manage to
    log in to" is a fact about *us*; it does not move when everything is fine, so
    a constant 4 could equally mean "four are fine" or "four have been stuck for
    a week". What the operator wanted was a way to *confirm* that mail is
    actually flowing -- and the only evidence of that is **school mail arriving**:
    we can see our own poll succeed, we cannot see the forwarding rule the user
    set inside CityU's webmail. So each mailbox now carries its own verdict plus
    the dates behind it, and the card leads with arrivals instead of logins.
    """
    window = DELIVERY_WINDOW_HOURS
    since_day = (now - dt.timedelta(hours=window)).isoformat(timespec="seconds")
    since_week = (now - dt.timedelta(days=7)).isoformat(timespec="seconds")
    ever = database.school_mail_evidence()
    day = database.school_mail_evidence(since=since_day)
    week = database.school_mail_evidence(since=since_week)

    rows: list[dict[str, Any]] = []
    for row in boxes:
        if not row.get("mailbox_email"):
            continue
        mailbox_id = str(row.get("mailbox_id") or "")
        evidence = ever.get(mailbox_id) or {}
        last_at = evidence.get("last_at")
        last_seen = reports_mod.to_local(last_at, "UTC") if last_at else None
        polled = reports_mod.to_local(row.get("last_polled_at"), "UTC")
        poll_age = round((now - polled).total_seconds(), 1) if polled else None
        mail_age = round((now - last_seen).total_seconds(), 1) if last_seen else None
        error = str(row.get("mailbox_error") or "").strip()
        paused = str(row.get("status") or "") == "paused"
        if paused:
            state = "paused"
            detail = "账号已暂停，我们按你的意思没有轮询它——不算故障，也不计进下面的比例。"
        elif error:
            state = "broken"
            detail = f"登不进去：{error[:160]}"
        elif poll_age is None or poll_age > _poll_freshness_seconds(row):
            state = "stale"
            detail = ("从没轮询过——邮箱配好了，但一次都没试过。" if poll_age is None
                      else "轮询停了：超过这个邮箱该有的收信间隔（时间在下面那一列）。")
        elif mail_age is None:
            # Polling works, nothing from the school has ever arrived. This is the
            # state the whole product exists to detect, and the fix is on the
            # *school* side (the forwarding rule), so the sentence has to say so.
            state = "no_mail"
            detail = "取信正常，但从没收到过任何本校来信——转发规则可能没生效（要改的是学校那一边）。"
        else:
            state = "ok"
            detail = "取信正常，也在收到本校来信。"
        rows.append({
            "mailbox": str(row.get("mailbox_email") or ""),
            "imap_host": str(row.get("imap_host") or ""),
            "user_status": str(row.get("status") or ""),
            "state": state, "detail": detail,
            "polled_at": row.get("last_polled_at") or "",
            "poll_age_seconds": poll_age,
            "last_mail_at": last_at or "",
            "mail_age_seconds": mail_age,
            "school_mail_24h": int((day.get(mailbox_id) or {}).get("count") or 0),
            "school_mail_7d": int((week.get(mailbox_id) or {}).get("count") or 0),
            "school_mail_total": int(evidence.get("count") or 0),
        })
    rows.sort(key=lambda item: (DELIVERY_ORDER.get(item["state"], 9), item["mailbox"]))
    arrived = [row for row in rows if row["last_mail_at"]]
    newest = max(arrived, key=lambda item: item["last_mail_at"]) if arrived else None
    return {
        "delivery_window_hours": window,
        "delivery": rows,
        "school_mail_24h": sum(row["school_mail_24h"] for row in rows),
        "school_mail_7d": sum(row["school_mail_7d"] for row in rows),
        "mailboxes_with_school_mail_24h": sum(1 for row in rows if row["school_mail_24h"]),
        "last_school_mail_at": (newest or {}).get("last_mail_at", ""),
        "last_school_mail_mailbox": (newest or {}).get("mailbox", ""),
        "quiet_mailboxes": [row["mailbox"] for row in rows if row["state"] == "no_mail"],
    }


def _working_counts(rows: list[dict[str, Any]], boxes: list[dict[str, Any]]) -> dict[str, int]:
    """「多少人是正常的」——运营者只要这一个数（2026-09-24 用户原话：

        「管理后台显示太多东西什么轮询正常，什么取信正常，简化一下，我就想知道多少人是正常的」

    所以这个数必须**有判据**，不能是前端把几个格子加起来。

    「正常」只认一种状态：**邮箱登得进去，而且真的收到过本校来信**（`state == "ok"`）。
    这是这一屏上唯一配得上「他在正常用」四个字的状态，其余各有各的说法：

      * `broken`   邮箱登不进去（授权码失效）——要找的是**用户**
      * `no_mail`  登得进去，但从没有过本校来信——要找的是**学校那一边的转发规则**
      * `stale`    轮询停了——要找的是**我们**
      * `paused`   用户自己暂停了——**谁都不用找**，它按设计就不轮询

    **这四种绝不合并成「不正常」**：合成一个数就把「谁该动手」这件事抹掉了，
    而运营者看这一屏就是为了知道该找谁。

    分母只算**我们本来该在轮询的邮箱**（已启用 + 账号未暂停）。把还没配完邮箱的人
    算进分母，比例会显得像系统坏了，而实际上那一步还没走到——「没配完」单独给一个数。
    """
    counts = {"ok": 0, "broken": 0, "no_mail": 0, "stale": 0, "paused": 0}
    for row in rows:
        state = str(row.get("state") or "")
        if state in counts:
            counts[state] += 1
    # 分母用「不是 paused」而不是「四个数相加」：将来多一个状态时，相加会**静默少算**，
    # 而运营者看到的分母会莫名其妙变小——那正是这类数字最容易骗人的地方。
    counts["configured"] = sum(1 for row in rows if str(row.get("state") or "") != "paused")
    # 注册了但还没接好邮箱的人：他们收不到任何报告，也**不会产生任何错误**，
    # 是这一屏上唯一会静默消失的一类（2026-09-24 生产上 64 个账号里 48 个是这种）。
    counts["without_mailbox"] = sum(
        1 for row in boxes
        if str(row.get("status") or "") == "active"
        and not (row.get("mailbox_email") and row.get("mailbox_enabled")))
    return counts


def _service_health() -> dict[str, Any]:
    database = get_db()
    now = dt.datetime.now(dt.timezone.utc)
    boxes = database.list_users_overview()
    with_mailbox = [row for row in boxes if row.get("mailbox_email") and row.get("mailbox_enabled")]
    # A paused account is *meant* to stop polling: the operator paused it, so its
    # mailbox is neither healthy nor stale and counting it in either direction
    # was how this card ended up showing a permanent, meaningless number.
    considered = [row for row in with_mailbox if str(row.get("status")) != "paused"]
    paused = [row for row in with_mailbox if str(row.get("status")) == "paused"]
    # "Stale" must mean the same thing here as it does in the alert sentinel, and
    # both must follow the interval the mailbox actually gets. Gmail is polled
    # every 15 minutes on Google's own advice, so the flat five-minute threshold
    # this used to carry reported a perfectly healthy mailbox as broken — which
    # is exactly how a real problem would go unnoticed: an operator who has
    # learned the warning is noise stops reading it.
    stale = []
    broken = []
    for row in considered:
        seen = reports_mod.to_local(row.get("last_polled_at"), "UTC")
        if not seen or (now - seen).total_seconds() > _poll_freshness_seconds(row):
            stale.append(row)
        # A poll *attempt* is not a working mailbox. `update_mailbox_poll` writes
        # `last_polled_at` whether the login succeeded or failed, so an account
        # whose authorisation code is wrong is re-stamped every few minutes and
        # reads as perfectly healthy. `verification_lights` already encodes the
        # correct rule ("a timestamp AND an empty error column"); this is the one
        # place that had not been taught it.
        if str(row.get("mailbox_error") or "").strip():
            broken.append(row)
    # How long ago the poller last stamped *any* mailbox, so the card can show a
    # number that moves every minute instead of a verdict that only changes once
    # an hour. "收信正常 2 / 4" alone cannot tell the operator whether the poller
    # is alive and the two are broken, or the poller itself stopped.
    ages = []
    for row in considered:
        seen = reports_mod.to_local(row.get("last_polled_at"), "UTC")
        if seen:
            ages.append(max(0.0, (now - seen).total_seconds()))
    circuits = database.open_key_circuits("model")
    # One set, not two subtractions: a mailbox that is both stale and broken is
    # still *one* mailbox, and subtracting it twice is how this number came out
    # one too low on the day the operator asked about it.
    unhealthy = {str(row.get("mailbox_id") or "") for row in stale} | \
                {str(row.get("mailbox_id") or "") for row in broken}
    delivery = _mailbox_delivery_rows(database, with_mailbox, now)
    return {
        "checked_at": now.isoformat(timespec="seconds"),
        "users": len(boxes),
        "active_users": sum(1 for row in boxes if row["status"] == "active"),
        "paused_users": sum(1 for row in boxes if row["status"] == "paused"),
        "mailboxes": len(considered),
        "mailboxes_paused": len(paused),
        "mailboxes_polled_recently": len(considered) - len(stale),
        "stale_mailboxes": len(stale),
        "freshness_note": "按每个邮箱自己的收信间隔 ×2 判断（Gmail 15 分钟、其它 1 分钟），"
                          "所以刚跑完一轮就会跟着变。",
        "newest_poll_seconds": round(min(ages), 1) if ages else None,
        "oldest_poll_seconds": round(max(ages), 1) if ages else None,
        # Deliberately *not* `considered - stale`: that number cannot fall when a
        # mailbox is being polled into a wall, which is the failure an operator
        # most needs to see in a single glance.
        "healthy_mailboxes": len(considered) - len(unhealthy),
        "broken_mailboxes": len(broken),
        "broken_mailbox_emails": [str(row.get("mailbox_email") or "") for row in broken],
        # Named, so the warning can point at the mailbox instead of making the
        # operator open every user to find it.
        "stale_mailbox_emails": [str(row.get("mailbox_email") or "") for row in stale],
        "paused_mailbox_emails": [str(row.get("mailbox_email") or "") for row in paused],
        **delivery,
        # 「多少人是正常的」：一个数，判据在 `_working_counts` 里（2026-09-24 用户要求简化）。
        "working": _working_counts(delivery["delivery"], boxes),
        "pending_messages": sum(int(row.get("queue_depth") or 0) for row in boxes),
        "failed_reports": sum(int(row.get("failed_reports") or 0) for row in boxes),
        # 同一个数字的两种东西：逐封邮件的失败，与每日简报的失败。后者不可能出现在
        # 「下发情况」那张表里（简报没有 message_id），所以必须分开说——
        # 否则运营者看到「5 份失败」而列表是空的（2026-09-18 用户就是这么报上来的）。
        **_failed_report_split(database),
        # Accounts we deliberately stopped generating for, because their model
        # credential kept being rejected. Reported here rather than only in the
        # log, because the symptom on the user's side is silence -- their mail
        # sits queued and no report arrives -- and the account's owner is the one
        # person who cannot see why. Nothing is dropped: those messages stay
        # pending and run as soon as the credential works or the window lapses.
        "suspended_accounts": len(circuits),
        "suspended_detail": [
            {
                "email": str(row.get("email") or ""),
                "failures": int(row.get("failures") or 0),
                "until": str(row.get("open_until") or ""),
                "reason": str(row.get("reason") or "")[:200],
            }
            for row in circuits
        ],
        "max_users": _max_users()[0],
        "max_users_source": _max_users()[1],
        "version": VERSION,
    }


@route("GET", "/api/admin/metrics")
def admin_metrics(request: Request) -> Response:
    """Live host and mail-pipeline numbers for the operator.

    Admin-only like the rest of ``/api/admin/*``; the host section reads /proc,
    which means it describes the machine the web service runs on. Values that
    the platform cannot provide come back as null so the panel can print “—”
    instead of pretending a missing reading is zero.
    """
    _require_admin(request)
    database = get_db()
    with database.connect() as connection:
        snapshot = metrics_mod.collect(connection)
    snapshot["service"] = _service_health()
    return json_response(snapshot)


def _admin_user_rows() -> list[dict[str, Any]]:
    """User rows with the derived fields the console sorts and labels on.

    Attached in exactly one place because ``list_users_overview`` returns raw
    columns, and every endpoint that hands rows to the console has to decorate
    them identically. Two of them did not: the status and settings endpoints
    returned bare rows, so pausing an account or saving a setting made its
    "未配完" badge disappear until the panel was reloaded -- the same class of bug
    as the audit list that updated its summary but not its rows. Adding a second
    derived field is precisely when that drift gets worse, so both now come from
    here.
    """
    database = get_db()
    users = _decorate_light_rows(database.list_users_overview())
    for row in users:
        row["setup_gap"] = database.setup_gap(row)
        # "What counts as proven" lives in `Database.verification_lights`, never
        # in app.js: a browser deriving its own lights from the same timestamps
        # would produce the false green that docstring describes, and it would do
        # so only after somebody else edited the frontend.
        row["lights"] = database.verification_lights(row)
    return users


def _platform_available() -> dict[str, bool]:
    """Which of the two per-account tests this instance can serve from its own key.

    Read from the environment on every call rather than cached: it is two
    ``os.environ`` lookups, and a cached copy would keep saying "no platform key"
    for the rest of the process after the operator installed one -- which is the
    same "restart the software and it fixes itself" habit this console is
    supposed to remove.
    """
    return {"model": providers.platform_model_default() is not None,
            "search": providers.platform_search_default() is not None}


def _decorate_light_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tell `verification_lights` whether an account *could* ride the platform key.

    The predicate lives in the database layer, but whether this instance has a
    fallback key at all is a fact about the process, not about the row. Passing
    it in keeps `verification_lights` pure (its unit tests build rows by hand) and
    keeps the answer to "is there a platform key" in one place for both callers:
    the user list and the single-account refresh.
    """
    available = _platform_available()
    for row in rows:
        row["platform_model"] = available["model"]
        row["platform_search"] = available["search"]
    return rows


def _failed_report_split(database) -> dict[str, int]:
    """失败报告的三个数：总数、逐封邮件的、每日简报的（定义只有一处）。"""
    summary = database.failed_reports_summary()
    return {"failed_reports_per_mail": summary["per_mail"],
            "failed_reports_digests": summary["digests"]}


@route("GET", "/api/admin/users")
def admin_users(request: Request) -> Response:
    admin = _require_admin(request)
    database = get_db()
    users = _admin_user_rows()
    return json_response({
        "users": users,
        "stalled_users": sum(1 for row in users if row["setup_gap"]),
        # 「上次打开后台之后有什么动静」（v0.63.72）。用户原话问了三遍：「我刷新后台
        # 界面应该要可以显示新的通知，有人申请了邀请码等等」。挂在这个响应里是因为
        # 后台每次打开/刷新本来就会取它 —— **零新增请求**。
        "activity": database.admin_activity(admin["id"]),
        "announcements": database.list_announcements(20),
        "invites": database.list_invites(100),
        "signups": database.list_signup_requests(100),
        "signup_counts": database.signup_request_counts(),
        "health": _service_health(),
        # The sentinel's own stored verdict, not a fresh evaluation: see
        # `alerting.panel_rows` for why the console does not re-check.
        "alerts": alerting.panel_rows(database.list_alert_states()),
        "admin_emails": sorted(_admin_emails()),
        "admins": _admin_roster(),
        # 「邀请申请到了，除了我还能告诉谁」（v0.63.93）。装机器的人永远收得到，
        # 这里只是**加**：控制台授权的管理员要一个一个勾，默认谁都不加。
        "signup_notification": {
            "selected": signup_notice.selected(database),
            "installers": sorted(alerting.admin_emails()),
            "candidates": signup_notice.candidates(database, alerting.admin_emails()),
        },
        "audit": database.list_audit(20),
    })


@route("PUT", r"/api/admin/users/(?P<user_id>[^/]+)/status/(?P<status>[A-Za-z]+)")
def admin_set_user_status(request: Request, user_id: str, status: str) -> Response:
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    if status not in {"active", "paused", "deleted"}:
        raise ApiError(422, "无效状态。")
    database = get_db()
    try:
        target = database.get_user(user_id)
    except KeyError as exc:
        raise ApiError(404, "用户不存在。") from exc
    if target["id"] == admin["id"] and status != "active":
        raise ApiError(422, "不能暂停或删除你自己正在使用的管理员账户。")
    if status == "deleted":
        # Deletion is the only irreversible operator action, so it is the only
        # one that demands a typed confirmation. Pausing is one click away from
        # being undone and does not need the extra friction (which is also
        # painful on a phone keyboard).
        confirmation = ""
        try:
            confirmation = str(request.json_object().get("confirm_email") or "").strip().lower()
        except ApiError:
            confirmation = ""
        if confirmation != str(target["email"]).strip().lower():
            raise ApiError(422, "删除是不可恢复操作：请输入该用户的完整邮箱以确认。")
    database.set_user_status(user_id, status)
    database.record_audit(action=f"user_status_{status}", actor_user_id=admin["id"],
                          actor_email=admin["email"], target_user_id=target["id"],
                          target_email=target["email"], client=_client_label(request))
    # Audit trail also goes to journalctl; deliberately omits every secret.
    logging.info("admin %s set user %s status=%s", admin["id"], target["id"], status)
    return json_response({"ok": True, "user_id": user_id, "status": status,
                          "users": _admin_user_rows()})


@route("POST", r"/api/admin/users/(?P<user_id>[A-Za-z0-9_]+)/password-reset")
def admin_reset_password(request: Request, user_id: str) -> Response:
    """Hand one locked-out user a fresh temporary password, from the console.

    用户原话（2026-09-19 深夜）：「我在哪里改用户密码」。命令行那条命令（`manage
    reset-password`）是对的，但每次都要找运营者敲一行 systemd-run 才算帮到人——
    于是他要在后台有一个按钮。这一条是那个按钮的服务端。

    What makes sharing this with the console acceptable, given that the same
    action from a shell was deliberate about *not* living here:

    * **Re-authentication** (``_confirm_operator``): the operator retypes their own
      password, so a stolen cookie on an unlocked laptop is not enough to take
      over somebody else's account.
    * **One-time reveal**: the plaintext is in this response and nowhere else --
      not in the audit row, not in any log, not stored. There is no endpoint that
      reads a password back.
    * **Sessions die with the old password**: every session of the target is
      revoked, so a reset cannot be used to leave a second door open.
    * **Attribution**: the audit row names the operator who did it, and the
      command-line path (same action name) names the shell.

    Everything the console does not need to know is left out: the response has no
    hash and no secrets of any kind beyond the password being handed over.
    """
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    payload = request.json_object()
    _confirm_operator(request, admin)
    database = get_db()
    try:
        target = database.get_user(user_id)
    except KeyError as exc:
        raise ApiError(404, "用户不存在。") from exc
    if target["id"] == admin["id"]:
        # 替自己重设会把**正在用的这个会话**也撤掉，于是这次成功的操作看起来像
        # 「突然被登出」。改自己的密码有专门的地方，那里会顺手换一张新凭据。
        raise ApiError(422, "这是你自己的账号：请到「更多 → 账户安全」改密码。")
    password = generate_temporary_password()
    database.set_password(target["id"], hash_password(password))
    removed = database.revoke_sessions(target["id"])
    database.record_audit(action="password_reset_by_operator", actor_user_id=admin["id"],
                          actor_email=admin["email"], target_user_id=target["id"],
                          target_email=target["email"],
                          detail=f"revoked={removed}；来源=后台",
                          client=_client_label(request))
    # journalctl 也是要给人看的：这里刻意只留「谁替谁换过」，不留任何凭据。
    logging.info("admin %s reset the password of user %s; %s session(s) revoked",
                 admin["id"], target["id"], removed)
    return json_response({"ok": True, "user_id": target["id"], "email": target["email"],
                          "password": password, "revoked": removed,
                          "audit": database.list_audit(60)})


# Settings an operator may change on somebody else's account. Everything here
# is reversible and audited; nothing here ever returns a stored secret. The API
# key and the mailbox app password are write-only: an operator can replace a
# broken one, but can never read the one that is there.
ADMIN_EDITABLE_PROFILE = ("school_email", "major", "year_of_study", "timezone",
                          "daily_time", "daily_enabled", "immediate_enabled")


@route("POST", r"/api/announcements/(?P<announcement_id>[^/]+)/dismiss")
def dismiss_announcement(request: Request, announcement_id: str) -> Response:
    """Hide one broadcast for this user only; everybody else still sees it."""
    user = _require_user(request)
    get_db().dismiss_announcement(announcement_id, user["id"])
    return json_response({"ok": True})


ANNOUNCEMENT_IMAGE_PATH = "/api/admin/announcement-image"
#: 一张配图最大多少字节。和背景图同一个量级、同一套理由：手机拍的原图在浏览器里
#: 先被重编码到 2048px / 1.4MB 以内（见 app.js 的 `reencodeImage`），这里只是**上限**，
#: 不是目标值。它同时是广播邮件的内嵌附件大小 —— 那是每个收件人都会下载的东西。
MAX_ANNOUNCEMENT_IMAGE_BYTES = 1_500_000


@route("POST", ANNOUNCEMENT_IMAGE_PATH)
def upload_announcement_image(request: Request) -> Response:
    """Store a picture the operator wants to send with a broadcast.

    Two-step on purpose: 上传 → 预览 → 决定发不发。绑定的那一步在
    `create_announcement` 里（同一个事务），所以不存在「公告已经在用户屏幕上、
    图还没到」的窗口。没发布的草稿六小时后被 `initialize()` 清掉。

    校验完全交给 `imageguard`（内容嗅探、拒绝 SVG、拒绝带 EXIF/XMP/IPTC 的文件），
    这里只做「存下来」这一半 —— 和背景图那条路用的是同一个判断，不抄第二份。
    """
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    if not request.body:
        raise ApiError(422, "没有收到图片内容。")
    try:
        media_type, width, height = imageguard.validate(
            request.body, request.header("Content-Type")
        )
    except imageguard.ImageRejected as exc:
        detail = f"（{exc.detail}）" if exc.detail else ""
        raise ApiError(422, exc.reason + detail) from exc
    image_id = get_db().create_announcement_image(media_type, request.body, width, height)
    logging.info("admin %s uploaded announcement image %s (%sx%s, %s bytes)",
                 admin["id"], image_id, width, height, len(request.body))
    return json_response({
        "ok": True, "id": image_id, "media_type": media_type,
        "width": width, "height": height, "size": len(request.body),
        "preview_url": f"/announcement-image/{image_id}",
    })


@route("DELETE", ANNOUNCEMENT_IMAGE_PATH)
def drop_announcement_image(request: Request) -> Response:
    """放弃一张还没发布的配图。已经挂到公告上的删不掉（那不是「取消」）。"""
    admin = _require_admin(request)
    image_id = (request.query.get("id") or [""])[0]
    if not get_db().drop_announcement_image(image_id):
        raise ApiError(404, "这张图片不存在，或者已经发出去了（发出去的公告只能撤下，不能换图）。")
    logging.info("admin %s dropped announcement image %s", admin["id"], image_id)
    return json_response({"ok": True})


@route("GET", r"/announcement-image/(?P<image_id>[^/]+)")
def serve_announcement_image(request: Request, image_id: str) -> Response:
    """一张广播配图。**没有任何一档对匿名开放。**

    * 还没发布（草稿）→ 只有管理员；
    * 已经发布（含已撤下）→ 要登录 —— 站内看得到；公告撤下之后，读过那条广播的人
      手里那条链接也还有效，但外面取不到。

    **2026-09-24 改**：以前还有一档「贴到官网布告栏的 → 任何人都能取」，布告栏下线后
    随之取消（那一档存在的唯一理由就是让首页那块板能显示配图）。留意这**不是「收紧
    权限」**：那些图本来就是运营者主动公开过的，现在只是不再留一个匿名入口 ——
    它在没有任何页面展示这张图之后还开着。老库里 6 条 `is_public=1` 的公告正是如此。
    """
    stored = get_db().announcement_image(image_id)
    if not stored:
        raise ApiError(404, "找不到这张图片。")
    linked = stored.get("announcement_id")
    if not linked:
        _require_admin(request)
    else:
        # 草稿之外一律要登录，**不再看 `is_public`**（布告栏下线，那一档没有了）。
        _require_user(request)
    if stored["media_type"] not in (imageguard.JPEG, imageguard.PNG):
        raise ApiError(404, "图片格式不受支持。")
    return Response(
        status=200,
        body=stored["bytes"],
        content_type=stored["media_type"],
        headers={
            # `private`：一张还没公开的配图不该被任何共享缓存留下。id 是随机的、
            # 内容永不改变，所以浏览器自己缓存一会儿是安全的。
            "Cache-Control": "private, max-age=600",
            "Content-Disposition": "inline",
        },
    )


@route("POST", "/api/admin/announcements")
def admin_create_announcement(request: Request) -> Response:
    """Publish a broadcast to every active account.

    ``deliver_email`` chooses between a banner only and a banner plus one email
    per user's private mailbox. The emails are *queued* for the worker rather
    than sent here: the console must not hang while N mailboxes are contacted,
    and the worker already owns retries and per-user error reporting.
    """
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    payload = request.json_object()
    title = _string(payload, "title", minimum=1, maximum=200)
    body = _string(payload, "body", minimum=1, maximum=4000)
    tone = _string(payload, "tone", default="info", required=False, maximum=20)
    if tone not in {"info", "warn", "critical"}:
        raise ApiError(422, "未知的公告类型。")
    deliver_email = _boolean(payload, "deliver_email", False)
    # **没有 `public` 这个字段了**（2026-09-24 布告栏下线）：公告只有站内广播一种去向。
    # 客户端要是还发它，这里会当没看见 —— 它已经不指向任何东西了。
    image_id = _string(payload, "image_id", default="", required=False, maximum=80)
    database = get_db()
    try:
        announcement_id = database.create_announcement(
            title=title, body=body, tone=tone, deliver_email=deliver_email,
            created_by=admin["email"], image_id=image_id)
    except ValueError as exc:
        raise ApiError(422, str(exc)) from exc
    database.record_audit(action="announcement_published", actor_user_id=admin["id"],
                          actor_email=admin["email"],
                          detail=f"id={announcement_id} email={int(deliver_email)} "
                                 f"image={int(bool(image_id))}",
                          client=_client_label(request))
    logging.info("admin %s published announcement %s (email=%s image=%s)",
                 admin["id"], announcement_id, deliver_email, bool(image_id))
    return json_response({"ok": True, "id": announcement_id,
                          "announcements": database.list_announcements(20)})


@route("PUT", r"/api/admin/announcements/(?P<announcement_id>[^/]+)/withdraw")
def admin_withdraw_announcement(request: Request, announcement_id: str) -> Response:
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    database = get_db()
    try:
        database.withdraw_announcement(announcement_id)
    except KeyError as exc:
        raise ApiError(404, str(exc)) from exc
    database.record_audit(action="announcement_withdrawn", actor_user_id=admin["id"],
                          actor_email=admin["email"], detail=f"id={announcement_id}",
                          client=_client_label(request))
    return json_response({"ok": True, "announcements": database.list_announcements(20)})


@route("PUT", r"/api/admin/users/(?P<user_id>[^/]+)/settings")
def admin_update_user_settings(request: Request, user_id: str) -> Response:
    """Change another account's settings.

    Deliberately a *selective* patch: only the keys present in the body change.
    That is the whole reason this does not reuse ``PUT /api/profile``, which
    writes every field and defaults whatever is missing — an operator fixing a
    model name that way would silently wipe the user's courses and notes.
    """
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    payload = request.json_object()
    database = get_db()
    try:
        target = database.get_user(user_id)
    except KeyError as exc:
        # Deleting an account removes the row outright (privacy deletion), so a
        # removed user simply does not exist here; there is no "deleted but
        # still editable" state to guard against.
        raise ApiError(404, "用户不存在。") from exc

    changed: list[str] = []

    # ---- profile fields -------------------------------------------------
    profile_update: dict[str, Any] = {}
    if "school_email" in payload:
        profile_update["school_email"] = _cityu_email(
            _string(payload, "school_email", default="", required=False, maximum=254))
    if "major" in payload:
        profile_update["major"] = _string(payload, "major", default="", required=False, maximum=200)
    if "year_of_study" in payload:
        profile_update["year_of_study"] = _string(payload, "year_of_study", default="", required=False, maximum=80)
    if "timezone" in payload:
        zone = _string(payload, "timezone", default="Asia/Hong_Kong", maximum=64)
        try:
            ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ApiError(422, "时区名称无效，例如 Asia/Hong_Kong。") from exc
        profile_update["timezone"] = zone
    if "daily_time" in payload:
        daily_time = _string(payload, "daily_time", default="22:00", maximum=5)
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", daily_time):
            raise ApiError(422, "每日发送时间必须是 HH:MM。")
        profile_update["daily_time"] = daily_time
    # 这两个字段仍然收：接口是老接口，别的调用方（后台代改、脚本）还在用。
    # **界面**只有一个写入点——「报告与账户」里那个开关走 `PUT /api/reports/delivery`，
    # 因为资料表单保存一次就会把这里的值一起写回去，两处写入迟早自相矛盾（v0.63.85）。
    if "daily_enabled" in payload:
        profile_update["daily_enabled"] = _boolean(payload, "daily_enabled", True)
    if "immediate_enabled" in payload:
        profile_update["immediate_enabled"] = _boolean(payload, "immediate_enabled", True)
    if profile_update:
        database.upsert_profile(user_id, profile_update)
        changed.extend(sorted(profile_update))

    # ---- model / search connections -------------------------------------
    for kind, provider_key, name_key, base_key, secret_key, catalog in (
        ("model", "model_provider", "model_name", "model_base_url", "model_api_key", MODEL_PRESETS),
        ("search", "search_provider", "search_name", "search_base_url", "search_api_key", SEARCH_PRESETS),
    ):
        touched = [key for key in (provider_key, name_key, base_key, secret_key) if key in payload]
        if not touched:
            continue
        existing = database.get_connection(user_id, kind)
        provider = _string(payload, provider_key, default=(existing or {}).get("provider") or "", maximum=60)
        if provider not in catalog:
            raise ApiError(422, f"未知的{'模型' if kind == 'model' else '搜索'}供应商。")
        model = _string(payload, name_key, default=(existing or {}).get("model") or "", required=False, maximum=120)
        base_url = _string(payload, base_key, default=(existing or {}).get("base_url") or "",
                           required=False, maximum=300)
        if kind == "model":
            # Validation only: what the operator typed is what gets stored (the
            # same rule the user-facing save follows). A retired alias is mapped
            # forward when the request goes out, not rewritten in their settings
            # behind their back -- two write paths that disagree about this is
            # how "the console says one thing, the bill says another" starts.
            _, _, base_url = normalized_model_config(provider, model, base_url)
        elif not base_url:
            base_url = SEARCH_PRESETS[provider].get("base_url", "")
        if secret_key in payload:
            secret = _string(payload, secret_key, minimum=1, maximum=400)
            encrypted = get_service().secrets.encrypt(secret, context=f"connection:{user_id}:{kind}")
        elif existing:
            encrypted = existing["encrypted_api_key"]      # keep the stored one untouched
        else:
            raise ApiError(422, "首次配置需要提供 API key。")
        database.upsert_connection(user_id, {
            "kind": kind, "provider": provider, "model": model, "base_url": base_url,
            "encrypted_api_key": encrypted,
            "config_json": (existing or {}).get("config_json") or "{}",
            "enabled": True,
        })
        # Only the field names are recorded, never the values.
        changed.extend(sorted(touched))

    # ---- mailbox --------------------------------------------------------
    mailbox = database.get_mailbox(user_id)
    mailbox_touched = [key for key in ("report_to", "mailbox_app_password") if key in payload]
    if mailbox_touched:
        if not mailbox:
            raise ApiError(422, "该用户还没有配置私人邮箱。")
        # One upsert call covers both fields; the mailbox identity (address and
        # IMAP host) is passed through unchanged, so the UID cursor is kept and
        # no already-processed mail can be replayed.
        encrypted = mailbox["encrypted_password"]
        if "mailbox_app_password" in payload:
            secret = _string(payload, "mailbox_app_password", minimum=1, maximum=400)
            encrypted = get_service().secrets.encrypt(secret, context=f"mailbox:{user_id}")
        database.upsert_mailbox(user_id, {
            "email": mailbox["email"],
            "report_to": (_email(_string(payload, "report_to", maximum=254))
                          if "report_to" in payload else mailbox["report_to"]),
            "imap_host": mailbox["imap_host"], "imap_port": mailbox["imap_port"],
            "smtp_host": mailbox["smtp_host"], "smtp_port": mailbox["smtp_port"],
            "encrypted_password": encrypted,
        })
        changed.extend(sorted(mailbox_touched))

    if not changed:
        raise ApiError(422, "没有需要修改的字段。")

    database.record_audit(action="admin_user_settings_changed", actor_user_id=admin["id"],
                          actor_email=admin["email"], target_user_id=target["id"],
                          target_email=target["email"], detail="fields=" + ",".join(changed),
                          client=_client_label(request))
    logging.info("admin %s changed %s for user %s", admin["id"], ",".join(changed), target["id"])
    overview = [row for row in _admin_user_rows() if row["id"] == target["id"]]
    return json_response({"ok": True, "user_id": user_id, "changed": changed,
                          "user": overview[0] if overview else None,
                          "users": _admin_user_rows(),
                          "audit": database.list_audit(20)})


# 运营者能替用户重测的三件事，以及它们在界面上的名字。定义在这里而不是散在
# 路由与前端各一份：加一项时最坏的结果是前端悄悄不认识它。
REFRESH_TARGETS = ("mailbox", "model", "search")
REFRESH_LABELS = {"mailbox": "收信", "model": "模型", "search": "搜索"}


@route("POST", r"/api/admin/users/(?P<user_id>[^/]+)/refresh")
def admin_refresh_user(request: Request, user_id: str) -> Response:
    """Re-test one account's testable parts **for** its owner (用户原话：

    「帮我做对每一个用户都可以一键刷新他们所有状态的按钮，我要这个按钮可以选择全部人
    也可以单某个人」）。

    Why this has to exist at all: three of the four lights come from
    `Database.verification_lights`, and a light only turns green when something
    actually succeeded *for this account*. Somebody has to press the test button
    -- and the one person who will not press it is the account holder, because
    nothing has gone wrong for them yet, or because they never opened the page.
    Until now the operator could see a red light and had no way to clear it.

    Three things it deliberately does not do:

    * **No 出报告 light.** That one can only be earned by a real message
      arriving, being read and being mailed out. A button that turns it green
      would turn the only end-to-end proof in this product into a decoration.
    * **No cursor movement, no mail.** The mailbox probe is the same read-only
      `BODY.PEEK` path the settings page uses; nothing is stored, flagged or
      deleted, so refreshing somebody else's account cannot cost them a message.
    * **No pretending a shared key is theirs.** An account with no connection of
      its own rides the instance key; the probe really runs, but there is no row
      to record it on, and the response says so instead of showing a light that
      will still read 「从没测过」 afterwards.

    One account per request on purpose: the console walks the list itself, so a
    slow mail host cannot hold one enormous response open past nginx's timeout,
    and the operator can stop halfway without losing what already ran.
    """
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    payload = request.json_object() if request.body else {}
    targets = payload.get("targets")
    if targets is None:
        wanted = list(REFRESH_TARGETS)
    elif isinstance(targets, list) and all(isinstance(item, str) for item in targets):
        wanted = [item for item in dict.fromkeys(targets) if item in REFRESH_TARGETS]
        if not wanted:
            raise ApiError(422, f"没有可测的目标；可选：{'、'.join(REFRESH_TARGETS)}。")
    else:
        raise ApiError(422, "targets 必须是字符串数组。")
    database = get_db()
    try:
        target = database.get_user(user_id)
    except KeyError as exc:
        raise ApiError(404, "用户不存在。") from exc
    service = get_service()
    results = []
    for kind in wanted:
        started = time.monotonic()
        own = bool(database.get_mailbox(target["id"])) if kind == "mailbox" \
            else bool(database.get_connection(target["id"], kind))
        error = ""
        try:
            if kind == "mailbox":
                service.test_mailbox(target["id"])
            elif kind == "model":
                service.test_model(target["id"])
            else:
                service.test_search(target["id"])
        except Exception as exc:  # noqa: BLE001 -- the failure *is* the answer here
            error = f"{exc}"[:300] or type(exc).__name__
        # 记下来，灯才会变。失败也要记：`verification_lights` 的绿灯要求
        # 「有时间戳**且**错误列为空」，只记成功会让上一次的失败永远挂着。
        record = database.get_mailbox(target["id"]) if kind == "mailbox" else None
        if kind == "mailbox":
            if record:
                database.record_mailbox_verification(record["id"], error=error)
        else:
            database.record_connection_result(target["id"], kind, error=error)
        results.append({
            "key": kind, "label": REFRESH_LABELS[kind], "ok": not error,
            "error": error, "own": own,
            # 「这次真的调通了」和「那盏灯没变绿」同时出现，正是运营者会读成
            # 「刷新不管用」的那一格（2026-09-16 用户原话：「为什么点刷新用户状态
            # 还是亮红灯」）。所以这半句必须自己把因果说完，而不是只留一个 ✓。
            "note": "" if own else "用的是平台兜底 key：这次真的调通了，但账号上没有自己的 key，"
                                   "所以「模型 / 搜索」那两盏灯不会变绿——它们证明的是"
                                   "「他自己配的 key 能不能用」。",
            "seconds": round(time.monotonic() - started, 1),
        })
    database.record_audit(
        action="user_refreshed", actor_user_id=admin["id"], actor_email=admin["email"],
        target_user_id=target["id"], target_email=str(target.get("email") or ""),
        # 计数与目标名，不含任何读数：审计是给以后排查的人看的，不是给谁的账号做画像。
        detail=(f"targets={','.join(wanted)} "
                f"ok={sum(1 for item in results if item['ok'])} "
                f"failed={sum(1 for item in results if not item['ok'])}"),
    )
    fresh = {row["id"]: row for row in _decorate_light_rows(database.list_users_overview())}.get(target["id"], {})
    return json_response({
        "ok": True, "user_id": target["id"], "email": target.get("email"),
        "results": results,
        "lights": database.verification_lights(fresh) if fresh else [],
    })


@route("PUT", r"/api/admin/users/(?P<user_id>[^/]+)/note")
def admin_set_user_note(request: Request, user_id: str) -> Response:
    """Store the operator's private memo about one account.

    A route of its own rather than one more key on the settings endpoint. That
    endpoint patches *the user's* configuration, and a note is not the user's
    anything: it is the operator's, it is never shown to the account, and it must
    never appear in that account's own export. Keeping it in its own URL is what
    makes that boundary visible instead of burying it among fields that do reach
    the user.
    """
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    database = get_db()
    try:
        target = database.get_user(user_id)
    except KeyError as exc:
        raise ApiError(404, "用户不存在。") from exc
    payload = request.json_object()
    # The key is required rather than defaulted. "" is a legitimate value -- it
    # is how an operator erases a note -- but a body that merely forgot the key
    # must not erase one by accident, because that data loss is indistinguishable
    # from a successful no-op save.
    if "note" not in payload:
        raise ApiError(422, "缺少 note 字段。")
    note = _string(payload, "note", default="", required=False,
                   maximum=database.ADMIN_NOTE_LIMIT)
    stored = database.set_admin_note(target["id"], note)
    # The note's *text* never goes into the audit detail: the audit log is
    # rendered to every admin and read by whoever debugs the instance, and
    # somebody jotting "the student who wrote to me about X" has not agreed to
    # that. What is recorded is that it changed and how long it is.
    database.record_audit(action="admin_note_changed", actor_user_id=admin["id"],
                          actor_email=admin["email"], target_user_id=target["id"],
                          target_email=target["email"], detail=f"length={len(stored)}",
                          client=_client_label(request))
    logging.info("admin %s set note (%d chars) on user %s", admin["id"], len(stored), target["id"])
    return json_response({"ok": True, "user_id": user_id, "admin_note": stored,
                          "users": _admin_user_rows()})


@route("GET", "/api/admin/setup-reminders")
def admin_setup_reminders(request: Request) -> Response:
    """Who is stuck, which sentence each of them needs, and the letters themselves.

    The preview is here rather than behind a second endpoint because pressing
    this button writes to real people's inboxes. Nobody should do that on the
    strength of a button label, and the operator is the one who has to live with
    the wording.
    """
    _require_admin(request)
    database = get_db()
    return json_response({
        "rows": setup_reminders.panel_rows(database),
        # "所有人" 那一档要连新账号一起列出来（见 setup_reminders.collect）。
        "all_rows": setup_reminders.panel_rows(database, include_recent=True),
        "counts": setup_reminders.whats_left(database),
        "preview": setup_reminders.preview(database),
        # 正文可编辑：当前用的那一份 + 原始默认，面板据此提供「恢复默认」。
        "templates": {group: setup_reminders.template_for(database, group)
                      for group in setup_reminders.TEMPLATE_KEYS},
        "default_templates": {group: setup_reminders.default_template(group)
                              for group in setup_reminders.TEMPLATE_KEYS},
        "placeholders": list(setup_reminders.PLACEHOLDERS),
        "batch_limit": setup_reminders.BATCH_LIMIT,
    })


@route("PUT", "/api/admin/setup-reminders/template")
def admin_save_reminder_template(request: Request) -> Response:
    """Save the wording of one reminder, or reset it to ours.

    The text is checked before it is stored: an unknown ``{placeholder}`` would
    otherwise be mailed literally, and the one person who cannot report that
    back is the person who received it.
    """
    admin = _require_admin(request)
    payload = request.json_object()
    group = str(payload.get("group") or "").strip()
    text = str(payload.get("text") or "")
    database = get_db()
    try:
        saved = setup_reminders.set_template(database, group, text, actor=admin["id"])
    except setup_reminders.TemplateError as exc:
        raise ApiError(422, str(exc)) from exc
    database.record_audit(action="reminder_template_saved", actor_user_id=admin["id"],
                          actor_email=admin["email"],
                          detail=f"group={group} reset={not text.strip()} chars={len(saved)}")
    return json_response({"ok": True, "group": group, "text": saved,
                          "preview": setup_reminders.preview(database)})


@route("POST", "/api/admin/setup-reminders")
def admin_send_setup_reminders(request: Request) -> Response:
    """Mail every account that registered but never finished setting up.

    The one-click answer to a failure mode that leaves no trace on our side:
    somebody registers, never configures a mailbox, and receives nothing --
    nothing breaks, nothing queues, and **the only person who cannot see the
    problem is the person it belongs to**. The console could already show the
    operator who these people are; this is the part where they find out.

    ``audience`` says who:

    * ``pending`` (default) -- the accounts that never got one;
    * ``notified`` -- everyone again, including people already reminded (the
      wording changed, or the first one clearly never arrived);
    * ``all`` -- everyone who has not finished setting up, **including accounts
      younger than the automatic threshold**. The threshold exists so the
      automatic nudge does not land while somebody is still typing; the operator
      asking for "everyone" means everyone, and without this the console could
      not reach somebody who registered this morning.
    * ``selected`` -- the ids in ``user_ids``, chosen one by one in the console
      (用户原话：「我要可以自己选给谁发卡住的邮件提醒」). Naming somebody overrides
      the two gates above, but **not** whether they still need the letter: an
      account that finished setting up in the meantime comes back in ``skipped``
      instead of being told "你还没配好".
    """
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    payload = request.json_object()
    audience = str(payload.get("audience") or "").strip() or (
        "notified" if payload.get("include_notified") is True else "pending")
    if audience not in ("pending", "notified", "all", "selected"):
        raise ApiError(422, "未知的发送对象。")
    include_notified = audience in ("notified", "all")
    include_recent = audience == "all"
    # 手选：运营者点名要给谁发。校验得比"大概是个列表"严一点 —— 这个请求的
    # 后果是给真人寄信，所以宁可用 422 说清楚，也不要静默发出意外的信。
    only_ids: list[str] | None = None
    if audience == "selected":
        raw = payload.get("user_ids")
        if not isinstance(raw, list):
            raise ApiError(422, "手选发送需要在 user_ids 里给出要发给谁。")
        only_ids = [str(value).strip() for value in raw]
        only_ids = [value for value in dict.fromkeys(only_ids) if value]
        if not only_ids:
            raise ApiError(422, "一个人都没选。")
        if len(only_ids) > setup_reminders.BATCH_LIMIT:
            # 同一封一封地发、每条都有 SMTP 超时，上限就是 nginx 那 330 秒的
            # 响应窗口（见 setup_reminders.BATCH_LIMIT）。说清楚而不是悄悄截断。
            raise ApiError(422, f"一次最多选 {setup_reminders.BATCH_LIMIT} 个人"
                                f"（你选了 {len(only_ids)} 个）——分两批，或者用「所有人都发」。")
    database = get_db()
    # Synchronous, bounded by `setup_reminders.BATCH_LIMIT`: nginx allows a 330s
    # response, and the cap is what keeps even a hanging SMTP host inside it.
    result = setup_reminders.send_pending(
        database, get_service().secrets, include_notified=include_notified,
        include_recent=include_recent, actor=admin["id"], only_ids=only_ids)
    database.record_audit(
        action="setup_reminders_sent", actor_user_id=admin["id"],
        actor_email=admin["email"],
        detail=(f"audience={audience} sent={len(result['sent'])} failed={len(result['failed'])} "
                f"remaining={result['remaining']}"
                # 手选时记「选了几个」，不记 id 也不记地址：审计里放地址的代价
                # 是它会被每日备份带走，而这里不需要靠它来还原发生了什么。
                + (f" selected={result.get('requested', len(only_ids or []))}"
                   if only_ids is not None else "")
                + (" include_notified" if include_notified else "")),
        client=_client_label(request))
    logging.info("admin %s sent %d setup reminders (%d failed, %d left)",
                 admin["id"], len(result["sent"]), len(result["failed"]), result["remaining"])
    return json_response({
        "ok": True,
        # Counts, not the record lists: the console prints these straight into a
        # sentence, and handing it a list to interpolate is how "发出 2 封" turns
        # into "发出 [object Object] 封" on a page that is about being truthful.
        "sent": len(result["sent"]), "failed": len(result["failed"]),
        "failures": result["failed"], "remaining": result["remaining"],
        # 点了名却没发出去的（中途已经配好了 / id 不认识）：如实说出来。
        "skipped": result.get("skipped", []),
        "requested": result.get("requested", 0),
        # 实际发出去的 id：面板要按它来清勾选 —— 失败的人留着勾才能再按一次。
        "sent_ids": [row["user_id"] for row in result["sent"]],
        "rows": setup_reminders.panel_rows(database),
        "counts": setup_reminders.whats_left(database),
    })


@route("POST", r"/api/admin/alerts/(?P<key>[^/]+)/acknowledge")
def admin_acknowledge_alert(request: Request, key: str) -> Response:
    """Stop mailing one finding the operator has already seen.

    Not a delete and not a close: the condition is still true and the console
    must keep showing it. This says one thing only -- stop reminding me -- and
    that is the difference between a known-issues list and a blindfold. The
    acknowledgment is cleared automatically when the condition itself clears, so
    the same key firing again later is news again and will reach the operator.
    """
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    database = get_db()
    try:
        database.acknowledge_alert(key, dt.datetime.now(dt.timezone.utc))
    except KeyError as exc:
        raise ApiError(404, "没有这条巡检记录。") from exc
    database.record_audit(action="alert_acknowledged", actor_user_id=admin["id"],
                          actor_email=admin["email"], detail=f"key={key}",
                          client=_client_label(request))
    logging.info("admin %s acknowledged alert %s", admin["id"], key)
    return json_response({"ok": True, "key": key,
                          "alerts": alerting.panel_rows(database.list_alert_states())})


@route("DELETE", r"/api/admin/alerts/(?P<key>[^/]+)/acknowledge")
def admin_unacknowledge_alert(request: Request, key: str) -> Response:
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    database = get_db()
    try:
        database.unacknowledge_alert(key)
    except KeyError as exc:
        raise ApiError(404, "没有这条巡检记录。") from exc
    database.record_audit(action="alert_unacknowledged", actor_user_id=admin["id"],
                          actor_email=admin["email"], detail=f"key={key}",
                          client=_client_label(request))
    return json_response({"ok": True, "key": key,
                          "alerts": alerting.panel_rows(database.list_alert_states())})


def _delivery_state(row: dict[str, Any]) -> str:
    """One word for "what happened to this mail", from the operator's view.

    ``messages.status`` is authoritative for delivery: the worker flips it to
    ``sent`` only after the mail went out, and the stored report row is a
    *detail* (when, where to, latency) that older, migrated messages do not
    have. Judging delivery by the report row instead marked 41 already-delivered
    migration rows as "never sent" — right in the database, wrong on screen.
    """
    if row.get("status") == "skipped":
        return "skipped"
    # 处理成功、报告已生成，但主人关掉了报告邮件：**不是"没送到"**，也不是失败。
    if row.get("status") == "held":
        return "held"
    if row.get("status") == "failed" or row.get("report_status") == "failed":
        return "failed"
    if row.get("status") == "sent" or row.get("report_status") == "sent":
        return "sent"
    if row.get("report_status") == "generated":
        return "generated"
    return "pending"


@route("GET", "/api/admin/messages")
def admin_messages(request: Request) -> Response:
    """Every incoming mail across all accounts, with its delivery outcome.

    Metadata only: subject, sender, times, statuses and errors. Message bodies
    are erased on delivery by design and are not exposed here either, so this
    panel answers "was it processed and delivered" without turning the operator
    console into a way to read other people's mail.
    """
    _require_admin(request)
    status = str(request.query.get("status", ["all"])[0] or "all")
    if status not in get_db().MESSAGE_FILTERS:
        raise ApiError(422, "未知的筛选条件。")
    try:
        limit = int(request.query.get("limit", ["50"])[0])
        offset = int(request.query.get("offset", ["0"])[0])
    except (TypeError, ValueError):
        raise ApiError(422, "分页参数必须是数字。")
    user_id = str(request.query.get("user_id", [""])[0] or "")

    database = get_db()
    page = database.list_messages_overview(limit=limit, offset=offset, status=status, user_id=user_id)
    for row in page["messages"]:
        row["delivery"] = _delivery_state(row)
        row["latency_seconds"] = None
        if row.get("sent_at") and row.get("received_at"):
            sent = reports_mod.to_local(row["sent_at"], "UTC")
            received = reports_mod.to_local(row["received_at"], "UTC")
            if sent and received:
                row["latency_seconds"] = round((sent - received).total_seconds(), 1)
        # The report *subject* (already selected) is enough to confirm the
        # generated report belongs to the right mail. The report body itself is
        # deliberately not decrypted here: this panel is about delivery, and the
        # operator console should not become a reader for other people's mail.
    page["status"] = status
    # 「下发情况」是一行一封邮件，而每日简报没有邮件行——所以它失败多少次，这张表都
    # 看不见。把简报那几行一并交出去，界面才能替这个数字给一个交代。
    page["failed_digests"] = database.failed_digests(10)
    page["filters"] = sorted(database.MESSAGE_FILTERS)
    page["users"] = [{"id": row["id"], "email": row["email"]} for row in database.list_users_overview()]
    return json_response(page)


@route("GET", "/api/admin/usage")
def admin_usage(request: Request) -> Response:
    """Per-user token consumption and what it cost.

    Cost is only shown where a verified price exists (see ``pricing``); calls by
    an unpriced model are counted and reported as "价格未配置" rather than costed
    at zero, because a total that is silently too low is worse than no total.
    """
    _require_admin(request)
    try:
        days = int(request.query.get("days", ["30"])[0])
    except (TypeError, ValueError):
        raise ApiError(422, "天数必须是数字。")
    database = get_db()
    page = database.usage_overview(days=days)
    overrides = database.list_model_prices()
    page["prices"] = overrides
    page["known_prices"] = [
        {"provider": provider, "model": model, **price}
        for (provider, model), price in sorted(pricing_mod.DEFAULT_PRICES.items())
    ]
    page["currency_note"] = "费用为按供应商公开价目表估算，仅供参考；实际以你的账单为准。"
    return json_response(page)


@route("GET", "/api/admin/capacity")
def admin_capacity(request: Request) -> Response:
    """The pilot cap, plus a recommendation derived from live measurements.

    Deliberately not folded into ``/api/admin/metrics``: that one is polled every
    three seconds and describes the machine right now, whereas this is about how
    many *accounts* to admit and changes on the scale of days.
    """
    _require_admin(request)
    database = get_db()
    current, source = _max_users()
    # Imported here rather than at module scope so the web layer does not pull in
    # the scheduler (and its signal handling) just to read one constant.
    from . import capacity as capacity_mod
    from . import worker as worker_mod
    return json_response(capacity_mod.advise(
        volume=database.recent_volume(14),
        host=metrics_mod.host_metrics(),
        workers=worker_mod.REPORT_WORKERS,
        # 主服务那台盒子的推理槽（主档不是本机那台时是 None）：报告槽有 6 个不等于
        # 有 6 份在算。见 `capacity.advise` 的 docstring。
        model_slots=providers.local_model_slots(),
        current=current,
        source=source,
    ))


@route("PUT", "/api/admin/capacity")
def admin_set_capacity(request: Request) -> Response:
    """Change how many accounts the pilot admits — from the panel, live.

    This is the one operator knob that used to require editing a 0600 root-owned
    file over SSH and restarting the service, which is why it now lives in the
    database. The environment value stays as the install-time default, and
    ``reset`` drops back to it.
    """
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    payload = request.json_object()
    database = get_db()

    if _boolean(payload, "reset", False):
        database.delete_setting(MAX_USERS_SETTING)
        database.record_audit(action="capacity_reset", actor_user_id=admin["id"],
                              actor_email=admin["email"], client=_client_label(request))
        current, source = _max_users()
        return json_response({"ok": True, "max_users": current, "source": source})

    try:
        value = int(payload.get("max_users"))
    except (TypeError, ValueError):
        raise ApiError(422, "名额必须是整数。")
    if value < 1 or value > 1000:
        raise ApiError(422, "名额需要在 1 到 1000 之间。")
    existing = database.count_users()
    if value < existing:
        # Not dangerous — the cap only gates new registrations — but the panel
        # would show a cap that nobody could fit under, which reads as a bug.
        raise ApiError(422, f"已经有 {existing} 个账号了，名额不能低于这个数。"
                            "要减少用户请到「已注册用户」里暂停或删除。")
    database.set_setting(MAX_USERS_SETTING, str(value), actor=admin["email"])
    database.record_audit(action="capacity_changed", actor_user_id=admin["id"],
                          actor_email=admin["email"], detail=str(value),
                          client=_client_label(request))
    current, source = _max_users()
    return json_response({"ok": True, "max_users": current, "source": source})


@route("GET", "/api/admin/agent")
def admin_agent(request: Request) -> Response:
    """The AI operations assistant: switch, budget, recent analyses.

    Opening the panel must not cost money, so nothing here calls a model. The
    paid path is the explicit POST below, and the automatic one is the sentinel.
    """
    _require_admin(request)
    database = get_db()
    budget = agent_mod.budget_state(database)
    return json_response({
        "enabled": agent_mod.enabled(database),
        "install_default": agent_mod.enabled_from_environment(),
        "has_model_key": providers.platform_model_default() is not None,
        "budget": budget,
        "limits": {
            "daily_calls": agent_mod.AGENT_DAILY_CALLS,
            "per_mail": agent_mod.AGENT_MAX_PER_MAIL,
            "cooldown_hours": round(agent_mod.AGENT_COOLDOWN_SECONDS / 3600, 1),
        },
        "reports": agent_mod.report_for_panel(database, get_service().secrets, limit=10),
        # What the assistant may suggest, and what has been confirmed so far.
        # The catalogue travels to the browser so a button can only ever be
        # labelled with something the server would actually accept.
        "actions": [{"key": key, **meta} for key, meta in sorted(agent_mod.ACTIONS.items())],
        # `limit=` not a bare 10: the method is keyword-only, and a bare number
        # here is a TypeError *inside the handler* -- which is how this shipped
        # a 500 on the whole panel once already. See the test that calls the
        # endpoint rather than the function.
        "action_log": database.list_agent_actions(limit=10),
    })


@route("POST", r"/api/admin/agent/reports/(?P<report_id>[A-Za-z0-9_]+)/act")
def admin_agent_act(request: Request, report_id: str) -> Response:
    """Confirm the action the assistant suggested on one analysis.

    Note what the request body does **not** contain: the action. It is read back
    off the report, so an operator confirms a proposal rather than naming one.
    That is what keeps `agent.ACTIONS` a closed set -- a caller who could pass a
    key would have turned one confirm button into a general-purpose remote
    control for the whole catalogue.

    Queueing is also not doing. This writes a row; the worker picks it up on its
    next pass, because the web process runs as an unprivileged user and could
    not carry any of these out even if it tried.
    """
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    database = get_db()
    try:
        queued = database.request_agent_action(
            report_id, requested_by=admin["email"], now=dt.datetime.now(dt.timezone.utc))
    except KeyError as exc:
        raise ApiError(404, "没有这条分析记录。") from exc
    except ValueError as exc:
        raise ApiError(422, str(exc)) from exc
    database.record_audit(action="agent_action_confirmed", actor_user_id=admin["id"],
                          actor_email=admin["email"], detail=f"{queued['action']} report={report_id}",
                          client=_client_label(request))
    logging.info("admin %s confirmed agent action %s on %s",
                 admin["id"], queued["action"], report_id)
    return json_response({"ok": True, **queued,
                          "action_log": database.list_agent_actions(limit=10)})


@route("PUT", "/api/admin/agent")
def admin_set_agent(request: Request) -> Response:
    """Turn the assistant on or off. Database, not pilot.env — no SSH needed."""
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    payload = request.json_object()
    if "enabled" not in payload:
        raise ApiError(422, "缺少 enabled 字段。")
    wanted = _boolean(payload, "enabled", False)
    database = get_db()
    agent_mod.set_enabled(database, wanted, actor=admin["email"])
    database.record_audit(action="agent_toggled", actor_user_id=admin["id"],
                          actor_email=admin["email"], detail="on" if wanted else "off",
                          client=_client_label(request))
    return json_response({"ok": True, "enabled": wanted})


DEMO_PATH = "/demo"
DEMO_DATA_PATH = "/demo-data.js"


def render_demo_page() -> bytes:
    """The real app shell, in demo mode.

    Same file as `/app` on purpose: the demo's whole value is that it is the
    actual interface rather than a mock-up, so a change to the shell shows up
    here for free. Only the data differs, and that arrives through
    ``/demo-data.js``.

    The data is a **separate script file rather than an inline `<script>`**
    because the CSP is `script-src 'self'`: an inline block would be blocked by
    the browser silently, and the demo would render a login screen that nobody
    can get past. That mistake has already been made once in this project.
    """
    text = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    anchor = '<script src="/app.js" defer></script>'
    if anchor not in text:  # pragma: no cover - the shell would be broken anyway
        raise ApiError(500, "应用外壳缺少 app.js。")
    # Before app.js, because app.js reads it during its own boot.
    text = text.replace(anchor, f'<script src="{DEMO_DATA_PATH}"></script>\n' + anchor)
    return text.encode("utf-8")


@route("GET", DEMO_DATA_PATH)
def demo_data(request: Request) -> Response:
    """The fixture, dated as of today, as a script the shell can read.

    `no-store` because the dates move: a cached copy would put the demo back to
    the day it was captured, and "今天要处理的事" dated last month reads as a
    broken product rather than as a demo.
    """
    body = f"window.PILOT_DEMO={json.dumps(demo.responses(), ensure_ascii=False)};"
    return Response(status=200, body=body.encode("utf-8"),
                    content_type="application/javascript; charset=utf-8",
                    headers={"Cache-Control": "no-store"})


@route("GET", DEMO_PATH)
def demo_page(request: Request) -> Response:
    return Response(status=200, body=render_demo_page(),
                    content_type="text/html; charset=utf-8",
                    headers={"Cache-Control": "no-cache"})


@route("GET", "/api/admin/digest")
def admin_digest_settings(request: Request) -> Response:
    """The daily brief's optional extras. A database row, not pilot.env."""
    _require_admin(request)
    return json_response({
        "synthesis": digest_synthesis.enabled(get_db()),
        "synthesis_from_install": digest_synthesis.enabled_from_environment(),
    })


@route("PUT", "/api/admin/digest")
def admin_set_digest(request: Request) -> Response:
    """Turn the model-written digest paragraph on or off.

    Off is a real answer and the default: the paragraph is an extra model call
    per user per day, and the deterministic list underneath it is the report
    either way.
    """
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    payload = request.json_object()
    if "synthesis" not in payload:
        raise ApiError(422, "缺少 synthesis 字段。")
    wanted = _boolean(payload, "synthesis", False)
    database = get_db()
    digest_synthesis.set_enabled(database, wanted, actor=admin["email"])
    database.record_audit(action="digest_synthesis_toggled", actor_user_id=admin["id"],
                          actor_email=admin["email"], detail="on" if wanted else "off",
                          client=_client_label(request))
    return json_response({"ok": True, "synthesis": wanted})


@route("PUT", "/api/admin/signup-notice")
def admin_set_signup_notice(request: Request) -> Response:
    """Choose which extra admins get the "someone applied" e-mail.

    The installer's own address is always notified and cannot be removed here --
    it comes from the environment, the console cannot revoke it, and a notice
    that quietly stopped reaching the person who owns the server would be a bug.
    So this endpoint only ever **adds**.

    Only current admins may be named: the message contains an applicant's
    address, and an endpoint that accepted an arbitrary address would be a way
    to send mail to strangers from the operator's own mailbox.
    """
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    payload = request.json_object()
    if "admins" not in payload:
        raise ApiError(422, "缺少 admins 字段。")
    wanted = payload["admins"]
    if not isinstance(wanted, list) or any(not isinstance(item, str) for item in wanted):
        raise ApiError(422, "admins 必须是邮箱地址的数组。")
    database = get_db()
    try:
        chosen = signup_notice.set_selected(database, wanted, actor=admin["email"])
    except ValueError as exc:
        raise ApiError(422, str(exc)) from exc
    database.record_audit(action="signup_notice_updated", actor_user_id=admin["id"],
                          actor_email=admin["email"], detail=",".join(chosen) or "（只有环境里的管理员）",
                          client=_client_label(request))
    return json_response({
        "ok": True,
        "selected": signup_notice.selected(database),
        "candidates": signup_notice.candidates(database, alerting.admin_emails()),
    })


@route("POST", "/api/admin/agent/analyze")
def admin_agent_analyze(request: Request) -> Response:
    """Explain what is wrong right now, on demand.

    Runs through the same budget gate and cooldown as the automatic path: the
    button is a convenience, not a way around the ceiling.
    """
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    database = get_db()
    try:
        findings = alerting.evaluate(database)
    except Exception as exc:
        raise ApiError(500, f"巡检判定失败：{exc}")
    if not findings:
        return json_response({"ok": True, "findings": 0, "analyses": [],
                              "note": "现在没有异常。"})
    results = agent_mod.analyse_many(database, findings, secrets=get_service().secrets)
    database.record_audit(action="agent_analyzed", actor_user_id=admin["id"],
                          actor_email=admin["email"],
                          detail=f"findings={len(findings)} analysed={len(results)}",
                          client=_client_label(request))
    return json_response({"ok": True, "findings": len(findings), "analyses": [
        {**item, "finding": item.get("finding")} for item in results]})


@route("PUT", "/api/admin/prices")
def admin_set_price(request: Request) -> Response:
    """Set or clear an operator price override for one provider+model."""
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    payload = request.json_object()
    provider = _string(payload, "provider", maximum=60)
    model = _string(payload, "model", maximum=120)
    database = get_db()
    if _boolean(payload, "remove", False):
        database.delete_model_price(provider, model)
        database.record_audit(action="price_removed", actor_user_id=admin["id"],
                              actor_email=admin["email"], detail=f"{provider}/{model}",
                              client=_client_label(request))
        return json_response({"ok": True, "removed": f"{provider}/{model}",
                              "prices": database.list_model_prices()})

    def rate(name: str) -> float:
        try:
            value = float(payload.get(name))
        except (TypeError, ValueError):
            raise ApiError(422, f"{name} 必须是数字（每 100 万 token 的价格）。")
        if value < 0 or value > 100000:
            raise ApiError(422, f"{name} 超出合理范围。")
        return value

    database.set_model_price(
        provider, model,
        input_cache_hit=rate("input_cache_hit"),
        input_cache_miss=rate("input_cache_miss"),
        output=rate("output"),
        peak_multiplier=float(payload.get("peak_multiplier") or 1.0),
        currency=_string(payload, "currency", default="USD", required=False, maximum=8),
    )
    database.record_audit(action="price_set", actor_user_id=admin["id"], actor_email=admin["email"],
                          detail=f"{provider}/{model}", client=_client_label(request))
    return json_response({"ok": True, "prices": database.list_model_prices()})


@route("POST", r"/api/admin/signups/(?P<request_id>[^/]+)")
def admin_decide_signup(request: Request, request_id: str) -> Response:
    """Approve or decline a pilot application.

    Approving mints a single-use invite and returns its code **exactly once** --
    the same rule as the invites panel, because only the hash is stored. The
    operator copies it into a reply; nothing here can hand out access twice.

    v0.63.72 moved the mint-and-send half into `invites.issue_and_send`, because
    the applicant's own 「没收到邀请码」 path and the worker's automatic retry end
    in exactly the same place. Three copies of "mint a code and mail it" is how
    they would eventually disagree about labels, expiry or what "sent" means.
    """
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    payload = request.json_object()
    status = _string(payload, "status", minimum=1, maximum=20)
    if status not in {"invited", "declined", "pending"}:
        raise ApiError(422, "无效的申请状态。")
    database = get_db()
    try:
        row = database.decide_signup_request(request_id, status)
    except KeyError as exc:
        raise ApiError(404, "申请不存在。") from exc
    except ValueError as exc:
        raise ApiError(422, str(exc)) from exc

    code = ""
    emailed = False
    email_error = ""
    if status == "invited":
        # Approving the same applicant twice is a resend, not a mistake to
        # refuse: the usual reason is that the first message never arrived. Each
        # issuance therefore gets its own label, so the record of what was sent
        # to whom stays one row per attempt instead of two invites sharing one
        # name and a join that cannot tell them apart.
        #
        # Sending is best-effort on purpose: the code is returned to the operator
        # either way, and losing a freshly minted single-use code because SMTP
        # hiccuped would be the worse failure.
        issued = invites_mod.issue_and_send(database, get_service(), request_id,
                                            send=payload.get("email", True) is not False,
                                            reason="operator")
        row, code = issued["row"], issued["code"]
        emailed, email_error = issued["emailed"], issued["email_error"]
    database.record_audit(action=f"signup_{status}", actor_user_id=admin["id"],
                          actor_email=admin["email"], target_email=row["email"],
                          detail=f"application {request_id}", client=_client_label(request))
    logging.info("admin %s set signup %s status=%s", admin["id"], request_id, status)
    return json_response({"ok": True, "signup": row, "code": code,
                          "emailed": emailed, "email_error": email_error,
                          "signups": database.list_signup_requests(100),
                          "signup_counts": database.signup_request_counts(),
                          "invites": database.list_invites(100)})


def invite_letter(code: str, origin: str | None = None) -> tuple[str, str]:
    """The invite message: subject and body, as text.

    Split out of `_email_invite` so that what the applicant receives can be
    tested without an SMTP server -- and, more importantly, so there is exactly
    one copy of it. A test that retypes the body goes on passing after somebody
    edits the real one, which is the same "two definitions" failure this project
    keeps finding elsewhere.

    The wording obeys a short list of rules, each of which is a spam signal when
    broken: two links at most, no shouting, a display name, a reply invitation,
    a sign-off, and -- the only fix that actually works -- a line telling the
    reader to look in the junk folder, which they will read *before* they go
    looking. See `docs/invite-deliverability-2026-09-16.md`.
    """
    origin = (origin if origin is not None
              else os.environ.get("INFE_PILOT_ORIGIN", "")).rstrip("/")
    app_url = f"{origin}/app" if origin else "/app"
    privacy_url = f"{origin}/privacy" if origin else "/privacy"
    subject = "你要的 CityU Mail Pilot 邀请码"
    body = (
        f"你好，\n\n"
        f"你在 CityU Mail Pilot 网站上申请的名额通过了，邀请码是：\n\n"
        f"    {code}\n\n"
        f"（只能用一次，14 天内有效）\n\n"
        f"从这里注册：{app_url}\n\n"
        f"两件事先说清楚：\n"
        f"· 目前免费：在另行通知前，模型调用默认用管理员提供的 key、由管理员付费；你也可以在「AI 模型」里\n"
        f"  换成自己的 key，那样费用和调用记录都归你自己。用管理员的 key 时，服务商把这次调用\n"
        f"  记在管理员账号下。\n"
        f"· 生成报告时邮件正文会发给大模型服务商；报告由 AI 生成、可能出错，请以原始邮件为准。\n"
        f"  我们只读你的邮箱、不删信不改动，报告发出后正文立即清空。完整说明：{privacy_url}\n\n"
        f"如果收件箱里没有这封信，它多半在垃圾邮件里——把它标成「不是垃圾邮件」，\n"
        f"以后每天的清单就不会再进那里。\n\n"
        f"配好邮箱之后卡住了，直接回这封信就行，我会看到。\n\n"
        f"{alerting.sender_name()}\n"
    )
    return subject, body


def _email_invite(row: dict[str, Any], code: str) -> tuple[bool, str, str]:
    """Mail one applicant their invite code. Never raises.

    **This message is the one that most often lands in a junk folder**, and the
    shape is why: a first-contact message from a personal mailbox, containing a
    short token and several links, is exactly what verification spam looks like.
    We cannot fix authentication or sender reputation -- the mail is sent through
    the operator's own consumer mailbox, so the SPF/DKIM verdicts belong to that
    provider, not to us. What we *can* do is make it read like a person wrote it,
    keep it short, keep the links down to two, and tell the reader where to look
    if it is not in the inbox.

    The message still states plainly that the pilot is free and whose model
    account the mail will pass through, because the person reading it has not
    opened the site again and this may be the only place they see either fact.

    Returns ``(sent, error, message_id)``. The message id is kept because it is
    the only handle a human has for correlating our send with the provider's log
    or with the headers of the message the applicant says never arrived.

    Since v0.63.72 the body lives in `invites.send_invite`, because the worker
    sends the same message on the two plan-B paths (the applicant's own request
    and the automatic retry). This wrapper stays so the operator's path keeps
    reading the way it did -- and so the tests that patch
    `web.alerting.send_as_operator` keep exercising exactly one send site.
    """
    return invites_mod.send_invite(get_service(), row, code)


@route("POST", "/api/admin/admins")
def admin_grant(request: Request) -> Response:
    """Give an existing account operator rights.

    Re-authentication is required (see ``_confirm_operator``), the target has to
    be an account that already exists, and the whole thing is audited. The three
    together are what keep "an operator can create operators" from being a
    privilege-escalation bug: a stolen session alone is not enough, a typo cannot
    hand rights to a stranger who registers later, and the grant is attributable.
    """
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    payload = request.json_object()
    _confirm_operator(request, admin)
    email = _email(_string(payload, "email", maximum=254))
    try:
        row = get_db().grant_admin(email)
    except KeyError as exc:
        raise ApiError(404, str(exc)) from exc
    except ValueError as exc:
        raise ApiError(422, str(exc)) from exc
    get_db().record_audit(action="admin_granted", actor_user_id=admin["id"],
                          actor_email=admin["email"], target_user_id=row["id"],
                          target_email=row["email"], client=_client_label(request))
    logging.info("admin %s granted operator rights to %s", admin["id"], row["email"])
    return json_response({"ok": True, "admins": _admin_roster(),
                          "audit": get_db().list_audit(60)})


@route("POST", r"/api/admin/admins/(?P<user_id>[A-Za-z0-9_]+)/revoke")
def admin_revoke(request: Request, user_id: str) -> Response:
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    _confirm_operator(request, admin)
    database = get_db()
    try:
        row = database.revoke_admin(user_id)
    except KeyError as exc:
        raise ApiError(404, str(exc)) from exc
    # Refuse to leave the instance with nobody who can administer it. Environment
    # operators count, which is why the check is here and not inside the store:
    # this is the only layer that knows about them.
    if not _admin_emails() and database.count_admin_capable() == 0:
        database.grant_admin(row["email"])
        raise ApiError(422, "这是最后一个管理员，不能移除；否则没人能再管理这个实例。")
    database.record_audit(action="admin_revoked", actor_user_id=admin["id"],
                          actor_email=admin["email"], target_user_id=row["id"],
                          target_email=row["email"], client=_client_label(request))
    logging.info("admin %s revoked operator rights from %s", admin["id"], row["email"])
    return json_response({"ok": True, "admins": _admin_roster(),
                          "audit": database.list_audit(60)})


def _admin_roster() -> list[dict[str, Any]]:
    """Everyone who can administer this instance, and where the right comes from.

    The environment-named accounts are listed even though they are not rows, so
    the console answers the question an operator actually has -- "who can do what
    I am doing?" -- rather than only the subset the console can edit.
    """
    roster = [
        {"id": "", "email": address, "source": "env", "removable": False, "status": "active"}
        for address in sorted(_admin_emails())
    ]
    stored = {row["email"].lower() for row in roster}
    for row in get_db().database_admins():
        if row["email"].lower() in stored:
            # Named in both places. The environment wins for display, because
            # removing the stored flag would not actually take the rights away
            # and a console that implied otherwise would be lying.
            continue
        roster.append({"id": row["id"], "email": row["email"], "source": "database",
                       "removable": True, "status": row["status"]})
    return roster


@route("POST", "/api/admin/invites")
def admin_create_invite(request: Request) -> Response:
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    payload = request.json_object()
    label = _string(payload, "label", default="pilot", required=False, maximum=100)
    try:
        days = int(payload.get("days", 7))
    except (TypeError, ValueError):
        raise ApiError(422, "有效天数必须是数字。")
    if not 1 <= days <= 90:
        raise ApiError(422, "有效天数需在 1–90 之间。")
    code = get_db().create_invite(label or "pilot", days)
    get_db().record_audit(action="invite_created", actor_user_id=admin["id"],
                          actor_email=admin["email"], detail=f"label={label or 'pilot'} days={days}",
                          client=_client_label(request))
    logging.info("admin %s created invite label=%s days=%s", admin["id"], label, days)
    # The plaintext code is returned exactly once and never stored.
    return json_response({"ok": True, "code": code, "label": label or "pilot", "days": days,
                          "invites": get_db().list_invites(100)})


@route("DELETE", r"/api/admin/invites/(?P<label>[^/]+)")
def admin_expire_invite(request: Request, label: str) -> Response:
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    retired = get_db().expire_invite(label)
    get_db().record_audit(action="invite_revoked", actor_user_id=admin["id"],
                          actor_email=admin["email"], detail=f"label={label} retired={retired}",
                          client=_client_label(request))
    logging.info("admin %s expired %s invite(s) labelled %s", admin["id"], retired, label)
    return json_response({"ok": True, "retired": retired, "invites": get_db().list_invites(100)})


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------


def dispatch(request: Request) -> Response:
    """Resolve a request to a response; never raises for expected failures."""
    if request.method not in AUTHENTICATED_METHODS:
        allowed_origin = os.environ.get("INFE_PILOT_ORIGIN", "").rstrip("/")
        if allowed_origin and request.origin and request.origin != allowed_origin:
            return fail(request, 403, "Origin rejected")
    # The two Android-distribution routes sit next to the static files rather
    # than in the `@route` table: both are "read a document off disk and send
    # it", which is what the block below does, and neither is part of the API.
    if request.path == ASSETLINKS_PATH and request.method in {"GET", "HEAD"}:
        document = assetlinks_document()
        if document is None:
            return fail(request, 404, "页面不存在。")
        # `application/json`, without the charset the API responses carry:
        # Android's verifier is strict about the media type of this document.
        return Response(status=200, body=document,
                        content_type="application/json",
                        headers={"Cache-Control": "public, max-age=300"})
    if request.path == APK_ROUTE and request.method in {"GET", "HEAD"}:
        target = apk_path()
        if target is None:
            return fail(request, 404, "安装包尚未提供。")
        return file_response(target, APK_MEDIA_TYPE, download_name=APK_FILENAME,
                             request=request)
    if request.path in STATIC_FILES and request.method in {"GET", "HEAD"}:
        name, content_type = STATIC_FILES[request.path]
        target = (STATIC_ROOT / name).resolve()
        if STATIC_ROOT not in target.parents or not target.is_file():
            return fail(request, 404, "页面不存在。")
        if request.path == MANIFEST_PATH:
            # Static files are matched before routes, so the manifest is
            # special-cased here rather than given a @route that would never
            # be reached. See render_manifest for why it cannot be a file.
            # bytes, not str: `_respond` announces `len(body)` as
            # Content-Length, and a str would be measured in characters and
            # then fail to write -- a 200 with an empty body and a length that
            # matches nothing (the browser then reports a manifest parse error,
            # i.e. the app quietly stops being installable).
            return Response(status=200, body=render_manifest(request).encode("utf-8"),
                            content_type=content_type, headers={"Cache-Control": "no-store"})
        if request.path in TEMPLATED_STATIC:
            locale = page_locale(request)
            if request.path == "/":
                body = render_landing_page(target, locale)
            elif request.path == "/app":
                # 应用外壳：整份按语言翻好再发出去，`#dashboard` 那一块带
                # `data-i18n-skip`，所以它保持中文（第二轮再翻）。
                body = _finish_page(i18n.translate_file(target, locale), target.name,
                                    locale).encode("utf-8")
            else:
                body = render_legal_page(target, locale)
            # `_with_language` 是必须的：**`?lang=en` 的意义就是「以后都用英文」**，
            # 只在这一次请求上生效等于没记住——换一页又变回中文。
            headers = {"Cache-Control": "no-cache",
                       # 让缓存/CDN 知道这一页是分语言的：同一个 URL 对不同
                       # `Accept-Language` 是不同的内容。
                       "Vary": "Accept-Language, Cookie",
                       "Content-Language": locale}
            # 页面是**当场渲染**的（按语言、按配置），所以验证器只能是正文的哈希：
            # 手机重复打开时这一步把 50 KB 变成一次 304（2026-09-24 用户报手机上慢）。
            etag = '"%s"' % hashlib.sha1(body).hexdigest()[:20]
            headers["ETag"] = etag
            if _etag_matches(request, etag):
                return Response(status=304, body=b"", content_type=content_type,
                                headers=headers)
            return _with_language(request, Response(
                status=200, body=body, content_type=content_type, headers=headers))
        return file_response(target, content_type, request=request)
    candidates = ROUTES.get(request.method, [])
    path_matched = False
    for pattern, handler in candidates:
        match = pattern.match(request.path)
        if not match:
            continue
        path_matched = True
        try:
            # Route parameters arrive percent-encoded, and they are decoded here,
            # once, for every route rather than in each handler. A URL path
            # segment cannot carry a raw ':' or space, so the console sends
            # `mailbox_error%3Ausr_abc`; the handler then looked up a key that
            # does not exist. Every 「已知晓」 click on a finding whose key carries
            # an identifier answered 404, while the colon-less keys (`disk`,
            # `backup_stale`) worked -- and the browser fixture happened to
            # acknowledge `disk`, the one shape that never hits this, so the
            # suite stayed green through all of it.
            groups = {key: unquote(value)
                      for key, value in match.groupdict().items() if value is not None}
            return _with_language(request, handler(request, **groups))
        except ApiError as exc:
            # **服务端报错在这里统一翻译**，而不是在每个 raise 的地方调 t()：
            # 报错是抛出来的，抛出点手上没有这次请求，拿不到语言。放在这个唯一的
            # 出口上，一处生效、也不会漏。词典里没有的句子原样返回中文，
            # 所以还没翻的那些接口与改造前逐字相同。
            locale = page_locale(request)
            return _with_language(request, error_response(
                exc.status, i18n.t(exc.detail, locale, **{
                    name: _render_param(value, locale) for name, value in exc.params.items()})))
    for method, entries in ROUTES.items():
        if method != request.method and any(pattern.match(request.path) for pattern, _ in entries):
            path_matched = True
            break
    if path_matched:
        return fail(request, 405, "方法不被允许。")
    return fail(request, 404, "资源不存在。")


class PilotHandler(BaseHTTPRequestHandler):
    server_version = "CityUMailPilot/" + VERSION
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        # Request-line logging for journalctl. The query string is stripped so a
        # ?token=... never reaches the logs, and bodies are never logged.
        message = re.sub(r"\?[^\s]*", "", format % args)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self.server.log.append((stamp, message))  # type: ignore[attr-defined]
        self.server.log = self.server.log[-500:]  # type: ignore[attr-defined]
        print(f"{stamp} {message}", file=sys.stderr, flush=True)

    def _discard(self, length: int) -> None:
        """Drain a bounded amount so a client can finish writing before we refuse.

        The bound has to be at least as large as the biggest body any route will
        announce, or "your file is too big" is delivered as a connection reset
        instead of a 413: we stop reading, the client is still writing, and the
        kernel answers with RST. The background-upload cap is 1.5 MB, so a 4 MB
        window covers every refusal we can actually issue and stays bounded for
        the ones we cannot.
        """
        remaining = min(length, 4_194_304)
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            remaining -= len(chunk)

    def _read_body(self, limit: int = MAX_BODY_BYTES) -> bytes:
        raw_length = self.headers.get("Content-Length")
        if not raw_length:
            return b""
        try:
            length = int(raw_length)
        except ValueError as exc:
            self.close_connection = True
            raise ApiError(400, i18n.mark("Content-Length 无效。")) from exc
        if length < 0:
            self.close_connection = True
            raise ApiError(400, i18n.mark("Content-Length 无效。"))
        if length > limit:
            self._discard(length)
            self.close_connection = True
            raise ApiError(413, i18n.mark("请求内容过大。"))
        return self.rfile.read(length) if length else b""

    def _respond(self, response: Response, *, head_only: bool = False) -> None:
        # `head_only` suppresses the *write*, not the body. The response is built
        # in full either way, so Content-Length is the real length -- emptying
        # the body at the call site instead (which is what four handlers here
        # used to do before the APK download needed an honest size) reports
        # `Content-Length: 0` for every HEAD, which nothing notices until a
        # client wants the size before committing to the bytes.
        self.send_response(response.status)
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        for key, value in response.headers.items():
            self.send_header(key, value)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        for cookie in response.cookies:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        if not head_only and response.body:
            self.wfile.write(response.body)

    def _handle(self, method: str) -> None:
        parsed = urlparse(self.path)
        body = b""
        # The body has to be read before the router runs, so the one route that
        # accepts more than JSON declares itself here as well as below. Keeping
        # the path in a constant is what stops the two from drifting apart.
        limit = (MAX_BACKGROUND_BYTES
                 if parsed.path in (BACKGROUND_PATH, ANNOUNCEMENT_IMAGE_PATH)
                 else MAX_BODY_BYTES)
        try:
            body = self._read_body(limit)
        except ApiError as exc:
            self._respond(error_response(exc.status, exc.detail))
            return
        request = Request(
            method=method,
            path=parsed.path,
            query=parse_qs(parsed.query, keep_blank_values=True),
            headers=self.headers,
            body=body,
            client=analytics_mod.client_ip(self.client_address[0], self.headers),
        )
        try:
            response = dispatch(request)
        except Exception as exc:  # pragma: no cover - defensive
            traceback.print_exc()
            # One greppable line naming the route and the exception type. The
            # traceback above already says what broke, but it does not say which
            # endpoint the operator actually clicked, in terms the access log can
            # be joined to -- diagnosing "点分析助手显示服务器内部错误" meant
            # reading raw stacks to work out that it was the analyze button and
            # not the panel load. `journalctl | grep unhandled` now answers that
            # in one line. Nothing user-supplied goes in it: the path and the
            # exception class only.
            logging.error("unhandled %s on %s %s", type(exc).__name__, method, parsed.path)
            # Says the failure was recorded, because the person who sees this is
            # usually the operator and their next question is "is there anything
            # I can look at". A bare "server error" leaves them nowhere to go;
            # the line above is where they go. The exception text itself still
            # never reaches the client.
            response = error_response(500, "服务器内部错误，已记入服务日志。")
        _record_visit(request, response)
        try:
            self._respond(response, head_only=method == "HEAD")
        except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - client went away
            pass

    def do_GET(self) -> None:
        self._handle("GET")

    def do_HEAD(self) -> None:
        self._handle("HEAD")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_DELETE(self) -> None:
        self._handle("DELETE")


class PilotServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, PilotHandler)
        self.log: list[tuple[str, str]] = []


def create_server(host: str = "127.0.0.1", port: int = 8787) -> PilotServer:
    """Build a ready-to-serve instance; ``port=0`` picks a free port."""
    return PilotServer((host, port))


def configure_logging() -> None:
    """Turn on INFO logging for this process.

    Only worker.py used to call logging.basicConfig, so every logging.info() in
    the web process went to a root logger still sitting at WARNING and was thrown
    away. That is how "invite emailed to ..." came to leave no trace anywhere: the
    message may well have gone out, and nothing recorded that it had. The audit
    table still holds admin decisions, but the log is what a person reads when
    they want to know what the process actually did, and it was silently empty.

    Split out of main() so a test can call it: the bug was invisible precisely
    because nothing exercised this line.
    """
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="CityU Mail Pilot web service")
    parser.add_argument("--host", default=os.environ.get("INFE_PILOT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("INFE_PILOT_PORT", "8787")))
    args = parser.parse_args(argv)
    configure_logging()
    server = create_server(args.host, args.port)
    host, port = server.server_address[0], server.server_address[1]
    print(f"CityU Mail Pilot {VERSION} listening on http://{host}:{port}", flush=True)

    def _stop(signum: int, frame: Any) -> None:  # pragma: no cover - signal path
        # shutdown() must run on another thread than serve_forever().
        threading.Thread(target=server.shutdown, daemon=True).start()

    for name in ("SIGTERM", "SIGINT"):
        handler = getattr(signal, name, None)
        if handler is not None:
            signal.signal(handler, _stop)

    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
