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
import datetime as dt
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
from . import imageguard
from . import invites as invites_mod
from . import mailio as mailio_mod
from . import metrics as metrics_mod
from . import service as service_mod
from . import pricing as pricing_mod
from . import providers
from . import reports as reports_mod
from . import taskexport
from . import setup_reminders
from .database import Database, utc_now
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
    hash_password,
    new_token,
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
    # Crawlers: `/` is the page worth indexing, `/app` is a login shell.
    "/robots.txt": ("robots.txt", "text/plain; charset=utf-8"),
    "/landing.js": ("landing.js", "application/javascript; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/theme-boot.js": ("theme-boot.js", "application/javascript; charset=utf-8"),
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
    "/privacy": ("privacy.html", "text/html; charset=utf-8"),
    "/terms": ("terms.html", "text/html; charset=utf-8"),
}

# The landing page carries a {{PILOT_COUNT}} placeholder so the "N accounts in
# use" sentence is read from the database at request time rather than typed into
# the file, where it would go stale the moment somebody else signed up.
# The legal pages carry {{CONTACT_LINK}}: the contact address is an operator
# setting, so a self-hoster must not inherit ours (and we must not publish theirs
# by accident). Every other static file is still served byte-for-byte.
TEMPLATED_STATIC = frozenset({"/", "/privacy", "/terms"})

# Shown instead of an address when the operator configured no contact channel.
# A privacy policy without a contact route is not a usable policy, so the gap is
# stated out loud rather than rendered as a dead mailto: link.
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

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


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


def file_response(target: Path, content_type: str, *, download_name: str = "") -> Response:
    data = target.read_bytes()
    headers = {"Cache-Control": "no-cache"}
    if download_name:
        # `attachment` so no browser ever tries to render the bytes, and the
        # quotes are stripped because this value came from a file name: a stray
        # `"` would end the header early and let the rest be read as a new one.
        headers["Content-Disposition"] = (
            'attachment; filename="' + download_name.replace('"', "").replace("\\", "") + '"')
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


def render_legal_page(target: Path) -> bytes:
    """Fill the contact placeholder in a legal page.

    The address is escaped before it reaches the attribute, because the value
    comes from an environment variable and a quote in it would otherwise break
    out of the href.
    """
    address = contact_email()
    if address:
        link = '<a href="mailto:%s">%s</a>' % (html.escape(address, quote=True), html.escape(address))
    else:
        link = "<code>%s</code>" % html.escape(NO_CONTACT_NOTICE)
    text = target.read_text(encoding="utf-8")
    return text.replace("{{CONTACT_LINK}}", link).encode("utf-8")


def render_landing_page(target: Path) -> bytes:
    """Fill the landing page's live numbers and its bulletin board.

    The page used to state how many accounts were in use as a written-down
    number. That is the same mistake as a hard-coded count anywhere else: true on
    the day it was typed and quietly false afterwards, on the one page whose claim
    is that it tells the truth about a small pilot. So the sentence is rendered
    from the database instead.

    "In use" means *an active account with an enabled mailbox*, not a count of
    rows in `users`. Registering is one click away from doing nothing, and
    counting those would put a number on the page the product cannot back up. The
    definition lives in `Database.landing_user_count` and a test pins it, because
    a number that means whatever is convenient is worse than no number.

    The sentence says *接好了邮箱* rather than *在收信* on purpose: an account
    that enabled a mailbox with a wrong auth code is counted here (it did the
    work) but is not receiving anything, and on 2026-09-15 production had exactly
    one such account -- so the older wording claimed 4 accounts were receiving
    mail when 3 were. The number was right and the sentence was wrong; changing
    the sentence keeps both the pinned definition and the claim true.
    """
    count = get_db().landing_user_count()
    if count <= 0:
        phrase = "现在还在内测的最早期，还没有人开始用。"
    elif count == 1:
        phrase = "现在有 1 个账号接好了邮箱，那个是我自己。"
    else:
        phrase = f"现在有 {count} 个账号接好了邮箱，其中一个是我自己。"
    text = target.read_text(encoding="utf-8")
    text = text.replace("{{PILOT_COUNT}}", html.escape(phrase))
    text = text.replace("{{SOURCE_LINK}}", render_source_link())
    # The nav entry and the section are decided by the same condition as the
    # footer link, so a copy of this software without a repository configured
    # renders neither.
    text = text.replace("{{SOURCE_NAV}}", render_source_nav())
    text = text.replace("{{SOURCE_SECTION}}", render_source_section())
    # The install instructions are prose and live in the template; only the
    # button is live, because whether this server has an APK at all is a fact
    # about the machine rather than something the page can assert.
    text = text.replace("{{APK_BUTTON}}", render_apk_button())
    text = text.replace("{{GUESTBOOK}}", render_guestbook(get_db().published_guest_messages(20)))
    return text.replace("{{BULLETIN}}", render_bulletin(get_db().public_announcements(3))).encode("utf-8")


def render_guestbook(rows: list[dict[str, Any]]) -> str:
    """The published messages on the landing page, or a line saying there are none.

    Unlike the bulletin board this section always renders, because the form under
    it is the point: a visitor who is about to write something should be able to
    see that the board exists and is read. An empty board says so in words rather
    than showing a heading over nothing.

    Every field is escaped here and only here -- the template receives finished
    markup. A message is untrusted text from a stranger, and this is the one path
    where it reaches HTML, so there is no second place to get it wrong.
    """
    parts = ['<ul class="guestlist">']
    if not rows:
        parts.append('<li class="guest-empty">还没有公开的留言。你写的那条会先给运营者看，通过后才会匿名刊登在这里。</li>')
    for row in rows:
        name = str(row.get("nickname") or "").strip() or "一位同学"
        stamp = bulletin_stamp(row.get("decided_at") or row.get("created_at"))
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


def render_source_link() -> str:
    """The footer link, or nothing at all when no repository is configured."""
    url = source_url()
    if not url:
        return ""
    return (f'<a href="{html.escape(url, quote=True)}" target="_blank" '
            f'rel="noopener">源代码（AGPL-3.0）</a> · ')


def render_source_nav() -> str:
    """The landing page's nav entry, or nothing. Jumps to the section below."""
    return '<a href="#source">开源</a>' if source_url() else ""


def render_source_section() -> str:
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
    return (
        '<section id="source">\n'
        '  <h2>源代码是公开的</h2>\n'
        '  <p>这个项目已经开源，许可证是 <b>AGPL-3.0</b>，代码在 GitHub 上：'
        f'<code>{html.escape(url)}</code>。这不是宣传语——'
        '所以你可以自己读一遍它到底怎么处理你的邮件。</p>\n'
        f'  <p class="repo"><a class="cta" href="{safe}" target="_blank" '
        'rel="noopener noreferrer">在 GitHub 上查看源代码 →</a></p>\n'
        '  <ul>\n'
        '    <li><b>你可以自己部署一份。</b>代码、安装脚本、备份与恢复步骤都在仓库里，'
        '不依赖我们这台服务器。</li>\n'
        '    <li><b>你可以核对隐私那一节。</b>「只读取信」「跳过邮件的正文不入库」'
        '这些说法在代码里都有对应的一行，不是空口承诺。</li>\n'
        '    <li><b>AGPL 第 13 条：</b>把这份程序作为网络服务提供的人，'
        '必须向使用者提供对应源码——所以我们把链接放在这里。</li>\n'
        '  </ul>\n'
        '</section>\n\n  '
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


def render_apk_button() -> str:
    """The Android download button, or a sentence saying there is not one.

    Both states are true ones. The empty state is not an error: a self-hosted
    copy has no APK by definition, so the page falls back to describing the
    browser route -- which works on every Android phone -- instead of leaving a
    dead button behind.
    """
    target = apk_path()
    if target is None:
        return ('<p class="note">这台服务器上没有准备好安卓安装包，'
                '用下面的「添加到主屏幕」一样能装。</p>')
    try:
        size = _human_size(target.stat().st_size)
    except OSError:  # pragma: no cover - removed between the check and the stat
        size = ""
    label = f"下载安卓安装包（{size}）" if size else "下载安卓安装包"
    return (f'<div class="dl"><a class="btn" href="{APK_ROUTE}" download '
            f'id="apk-download">{label}</a></div>')


# How many notices the public board shows at once. Three fits above the fold
# without turning the page into a feed; older ones stay in the console, and the
# board is not an archive.
BULLETIN_LIMIT = 3

# The board is read by people who are not signed in and may be anywhere, so
# every notice carries an explicit offset rather than a bare clock time. The
# audience is the university, hence Hong Kong, and `reports.to_local` already
# knows how to fall back to a fixed +08:00 when the host has no tzdata.
BULLETIN_TONES = ("info", "warn", "critical")


def bulletin_stamp(value: str | None) -> str:
    """A stored UTC timestamp as the board prints it, offset spelled out.

    The offset is read off the resolved datetime instead of being written as a
    literal, so the marker cannot disagree with the time next to it if the
    timezone ever moves.
    """
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


def render_bulletin(notices: list[dict[str, Any]]) -> str:
    """The public board on the landing page, or nothing at all.

    Empty means *no markup*: a heading with an empty list under it reads as a
    page that is broken or abandoned, which is a worse first impression than a
    page that simply has no news. So the section, the rule above it and the
    anchor all appear together or not at all.

    Titles and bodies are operator-written plain text that ends up in our own
    origin's HTML, so they are escaped here and *only* here -- the template gets
    finished markup. No markdown, no links: a notice does not need them, and
    every added syntax is another way for text to become markup.
    """
    rows = list(notices)[:BULLETIN_LIMIT]
    if not rows:
        return ""
    # No rule above the heading: the template already has one between the hero
    # and this placeholder. The rule *below* is ours, because the separator
    # between the board and the screenshot after it only exists when the board
    # does -- emitting both would print two hairlines 40px apart.
    # No blurb under the heading. "布告栏" plus the notice title already says
    # everything a line of explanation would ("operator-written, visible without
    # logging in"), and the whole section is absent unless there is something to
    # read -- so the sentence was explaining a thing most visitors never see.
    parts = [
        '<section id="board" aria-labelledby="board-title">',
        '<h2 id="board-title">布告栏</h2>',
    ]
    for row in rows:
        tone = str(row.get("tone") or "info")
        if tone not in BULLETIN_TONES:
            tone = "info"
        stamp = bulletin_stamp(row.get("public_at") or row.get("created_at"))
        parts.append(f'<article class="notice notice-{tone}">')
        parts.append(f'<h3>{html.escape(str(row.get("title") or "（无标题）"))}</h3>')
        if stamp:
            parts.append(f'<p class="stamp">{html.escape(stamp)}</p>')
        parts.append(f'<p class="post">{html.escape(str(row.get("body") or ""))}</p>')
        if row.get("image_id"):
            # 配图。**和这条公告的可见性完全一致**：能在这里读到它，是因为
            # `public_announcements()` 只返回 active+public 的那些。
            parts.append(
                f'<img class="notice-photo" src="/announcement-image/'
                f'{html.escape(str(row["image_id"]), quote=True)}" alt="公告配图" loading="lazy">')
        parts.append("</article>")
    parts.append("</section>")
    parts.append('<hr class="rule">')
    return "\n".join(parts)


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
            raise ApiError(422, "请求缺少 JSON 内容。")
        try:
            payload = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(422, "请求内容不是合法的 JSON。") from exc
        if not isinstance(payload, dict):
            raise ApiError(422, "请求内容必须是 JSON 对象。")
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
            raise ApiError(422, f"缺少字段 {name}。")
        return ""
    if not isinstance(value, str):
        raise ApiError(422, f"字段 {name} 必须是文字。")
    if len(value) < minimum:
        raise ApiError(422, f"字段 {name} 太短。")
    if len(value) > maximum:
        raise ApiError(422, f"字段 {name} 过长。")
    return value


def _boolean(payload: dict[str, Any], name: str, default: bool) -> bool:
    value = payload.get(name, default)
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    raise ApiError(422, f"字段 {name} 必须是布尔值。")


def _port(payload: dict[str, Any], name: str, default: Optional[int] = None) -> int:
    value = payload.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(422, f"字段 {name} 必须是端口号。")
    if not 1 <= value <= 65535:
        raise ApiError(422, f"字段 {name} 必须在 1-65535 之间。")
    return value


def _string_list(payload: dict[str, Any], name: str, *, maximum_items: int, item_maximum: int = 200) -> list[str]:
    value = payload.get(name, [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise ApiError(422, f"字段 {name} 必须是列表。")
    if len(value) > maximum_items:
        raise ApiError(422, f"字段 {name} 的条目过多。")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ApiError(422, f"字段 {name} 只能包含文字。")
        if len(item) > item_maximum:
            raise ApiError(422, f"字段 {name} 的单个条目过长。")
        cleaned = item.strip()
        if cleaned:
            result.append(cleaned)
    return result


def _config(payload: dict[str, Any], name: str) -> dict[str, Any]:
    value = payload.get(name, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ApiError(422, f"字段 {name} 必须是对象。")
    if len(value) > MAX_JSON_DEPTH_ITEMS:
        raise ApiError(422, f"字段 {name} 的条目过多。")
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, (str, int, float, bool)):
            raise ApiError(422, f"字段 {name} 只支持简单的键值对。")
    return dict(value)


def _email(value: str) -> str:
    result = value.strip().lower()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", result):
        raise ApiError(422, "邮箱地址格式不正确。")
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
            raise ApiError(429, "登录尝试过多，请 15 分钟后再试。")
        if failed:
            recent.append(now)
        _login_attempts[key] = recent


# The public application form needs its own budget: the login throttle is
# sized for a person mistyping a password, not for a stranger filling in a form.
_signup_attempts: dict[str, list[float]] = {}


def _signup_rate_limit(client: str) -> None:
    now = time.monotonic()
    key = f"signup:{client}"
    with _attempt_lock:
        recent = [value for value in _signup_attempts.get(key, []) if now - value < 3600]
        if len(recent) >= 5:
            raise ApiError(429, "提交过于频繁，请一小时后再试。")
        recent.append(now)
        _signup_attempts[key] = recent


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
    而内测反馈里那位用户要的正是这一格（原话「能不能在看原件的地方直接跳到 outlook
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
            raise ApiError(429, "留言提交过于频繁，请一小时后再试。")
        recent.append(now)
        _guestbook_attempts[key] = recent


# 「我没收到邀请码」是第三个未认证写入，也是**唯一一个会间接产生凭据**的：它能让
# 一张**已经被人批准过**的邀请码再走一次邮件。所以它的预算比留言板更紧，而且与
# 留言板分开计数 —— 一个正常人在上面点两次是可能的（第一次没收到），点十次不是。
_resend_attempts: dict[str, list[float]] = {}
INVITE_RESEND_RATE_LIMIT = 3
#: 同一个邮箱 24 小时内最多被重发几次。按 IP 那条挡不住换设备/换浏览器的人。
INVITE_RESEND_PER_EMAIL = 3
#: 回执。三种情形**逐字节相同**（有测试直接比字节）—— 见 `public_invite_resend`。
INVITE_RESEND_ACK = {
    "ok": True,
    "detail": "如果你的申请已经通过了，邀请码会在这几分钟内发到那个邮箱。"
              "收件箱里没有的话，看一眼垃圾邮件，并把它标成「不是垃圾邮件」。",
}


def _resend_rate_limit(client: str) -> None:
    now = time.monotonic()
    key = f"resend:{client}"
    with _attempt_lock:
        recent = [value for value in _resend_attempts.get(key, []) if now - value < 3600]
        if len(recent) >= INVITE_RESEND_RATE_LIMIT:
            raise ApiError(429, "请求过于频繁，请一小时后再试。")
        recent.append(now)
        _resend_attempts[key] = recent


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


@route("GET", "/health")
def health(request: Request) -> Response:
    return json_response({"status": "ok", "version": VERSION})


@route("GET", "/api/catalog")
def catalog(request: Request) -> Response:
    payload = public_catalog()
    payload["mailbox"] = public_mailbox_help()
    return json_response(payload)


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
    try:
        row, already = get_db().create_signup_request(email, note, _client_label(request))
    except ValueError as exc:
        raise ApiError(400, str(exc)) from exc
    if not already:
        _notify_new_signup(row)
    return json_response({"ok": True, "already": already})


@route("POST", "/api/invite/resend")
def public_invite_resend(request: Request) -> Response:
    """「我没收到邀请码」—— 申请人自助重发（未认证写入 **第三个**，v0.63.72）。

    这是 B 计划的一半（另一半是 worker 的自动重试，见 `pilot_app/invites.py` 与
    `docs/invite-plan-b-2026-09-17.md`）。它只做一件事：**让一张已经由人批准过、
    而且这个人还没注册的邀请码，再走一次邮件**。

    **回执永远同一句话**，无论这个邮箱批准过、还在等、被婉拒，还是从没申请过。
    这不是客气话而是接口性质：回执一旦随情形变化，这个端点就成了「某个邮箱申请过
    没有 / 批准了没有」的查询接口，而这两个问题的答案我们承诺过不对外提供。

    三件刻意的事：

    * **只入队，不在这里发信。** 发一封要几秒，而这是未认证端点 —— 把 SMTP 挂在
      请求路径上，一个陌生人就能拖住 web 进程；而且「有这份申请」要一秒、「没有」
      只要几毫秒，**耗时本身会把回执刻意抹掉的区别说出去**。
    * **按 IP 与按邮箱各限一次**。前者在内存里（挡不住换设备/换浏览器的人），
      后者查库（`recent_invite_resends`），两条都要。
    * **不建号、不发码给没被批准的地址、不给已经注册过的人发** —— 这三条由
      `Database.invite_eligible_for_resend` 一处决定，测试逐条盯着。
    """
    client = request.client or "unknown"
    _resend_rate_limit(client)
    payload = request.json_object()

    # 蜜罐与「停留不足 3 秒」都复用留言板那一套：同一种机器人，同一批门槛。
    if _string(payload, "website", default="", required=False, maximum=200).strip():
        logging.info("invite resend honeypot tripped from %s", client)
        return json_response(INVITE_RESEND_ACK)
    try:
        elapsed_ms = int(payload.get("elapsed_ms") or 0)
    except (TypeError, ValueError):
        elapsed_ms = 0
    if 0 < elapsed_ms < GUESTBOOK_MIN_SECONDS * 1000:
        logging.info("invite resend submitted in %s ms from %s", elapsed_ms, client)
        raise ApiError(422, "提交得太快了，请确认你是本人操作。")

    address = _email(_string(payload, "email", maximum=254))
    database = get_db()
    row = database.invite_eligible_for_resend(address)
    # 不够格就什么都不做 —— 但仍然回同一句话。**注意这里也不写队列**：往队列里塞
    # 一堆注定被跳过的行，既浪费 worker 的每一次扫描，也让「有多少人在等重发」
    # 这个数字变成噪音。
    if row is not None and database.recent_invite_resends(address, hours=24) < INVITE_RESEND_PER_EMAIL:
        database.queue_invite_resend(request_id=row["id"], email=address,
                                     client_hash=get_service().secrets.anonymized(client))
        logging.info("invite resend queued for application %s from %s", row["id"], client)
    return json_response(INVITE_RESEND_ACK)


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
        raise ApiError(422, "提交得太快了，请确认你是本人操作。")

    body = _string(payload, "body", maximum=database_mod.GUEST_BODY_LIMIT)
    if not body.strip():
        raise ApiError(422, "请先写点什么。")
    if _count_links(body) > database_mod.GUEST_LINK_LIMIT:
        raise ApiError(422, f"留言里最多 {database_mod.GUEST_LINK_LIMIT} 个链接。")
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
    """Tell the operator an application arrived. Never fails the request.

    The applicant cannot be e-mailed directly: every send in this project goes
    out through a user's own SMTP credentials, and there is no system mailbox.
    So the operator is notified and sends the invite themselves by approving it
    in the console. A notification failure must not lose the application, which
    is why this swallows errors after logging them.
    """
    try:
        service = get_service()
        alerting.send_admin_mail(
            get_db(), service.secrets,
            subject="[CityU Mail Pilot] 新的内测申请",
            text_body=(
                f"有人从网站申请了内测名额。\n\n"
                f"邮箱：{row.get('email', '')}\n"
                f"留言：{row.get('note') or '（没有留言）'}\n"
                f"时间：{row.get('created_at', '')}\n"
                f"来源：{row.get('client', '')}\n\n"
                f"到管理后台的「内测申请」面板一键发邀请码。"
            ),
        )
    except Exception:  # noqa: BLE001 - the application is already stored
        logging.warning("could not notify the operator about a new signup", exc_info=True)


@route("POST", "/api/auth/register")
def register(request: Request) -> Response:
    payload = request.json_object()
    email = _email(_string(payload, "email", maximum=254))
    password = _string(payload, "password", minimum=1, maximum=400)
    invite_code = _string(payload, "invite_code", minimum=1, maximum=200)
    # Consent is enforced here, not only in the browser. A checkbox that the
    # server never checks is decoration, and the disclosure that matters most --
    # that mail bodies go to a third-party model -- is exactly the one a user
    # cannot discover after the fact.
    if not _boolean(payload, "accepted_terms", False):
        raise ApiError(400, "请先阅读并同意《隐私政策》与《服务条款》。")
    database = get_db()
    limit, _source = _max_users()
    if database.count_users() >= limit:
        raise ApiError(403, "当前试点名额已满。")
    try:
        user = database.create_user(email, hash_password(password), token_hash(invite_code.strip()))
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
    if not user or not verify_password(password, user["password_hash"]):
        _rate_limit(attempt_key, failed=True)
        raise ApiError(401, "邮箱或密码错误。")
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
        model_state = "ok"
        model_detail = (f"{model['provider']} · {model['model']}，"
                        f"内测期间用管理员提供的 key，你不花钱。想换成自己的，在下面填一次即可覆盖。")
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
        search_detail = (f"{search['provider']}，内测期间用管理员提供的搜索 key，你不花钱。"
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
        task["effective_priority"] = taskexport.effective_priority(task)
        # `export_title` is the **clipboard** line, not the calendar's: the browser
        # pastes it into iOS 提醒事项 / Google Tasks (`app.js` 「复制成清单」), and a
        # checklist there wants the plain sentence. The calendar's prettier title
        # (emoji + ⏰) is produced inside `build_ics` and never travels through
        # here -- one field, one consumer, or the emoji quietly ends up pasted
        # into somebody's Reminders (which is exactly what happened in this
        # feature's first cut, caught in review on 2026-09-19).
        task["export_title"] = taskexport.line_for(task)
    # The user's own ranking is the strongest signal there is, so it decides the
    # order of the open list; `sort` is stable, so tasks they have not touched
    # keep the report's ordering (importance, then deadline, then arrival).
    open_tasks.sort(key=lambda item: (reports_mod.priority_rank(item["effective_priority"]),
                                      0 if item["deadline"] else 1))
    return {
        "day": local_date,
        "is_today": local_date == _local_window(timezone)[3],
        "tasks": open_tasks,
        "done": done_tasks,
        "counts": {"total": len(open_tasks) + len(done_tasks), "open": len(open_tasks),
                   "done": len(done_tasks)},
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
        # 内测反馈里那位用户看不到这一格，正是因为第一版把它挂在了"填过资料吗"上。
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
        "platform": "平台代付（内测期间由管理员承担）",
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

    Derived from the interval that mailbox is actually polled at (Gmail is 15
    minutes by Google's own advice, everything else is a minute), doubled to
    allow for one slow round, with a floor so a fast mailbox is not called stale
    between two heartbeats.

    It used to reuse ``alerting.stale_after_for`` -- the *alert* threshold, an
    hour for QQ -- and the operator reported the consequence in plain words:
    「收信正常的更新频率太慢了，一直只有 2 个」. The number was not wrong, it was
    answering a different question: "should this wake somebody up" instead of
    "is this mailbox being collected right now". A dashboard that only moves
    once an hour is a dashboard nobody trusts.
    """
    try:
        # `minimum_poll_seconds` returns 0 for providers with no documented
        # floor, and 0 means "no floor", not "poll constantly".
        interval = float(mailio_mod.minimum_poll_seconds({"imap_host": row.get("imap_host")}) or 60)
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
        **_mailbox_delivery_rows(database, with_mailbox, now),
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
    """一张广播配图。可见性和它所配的那条公告**完全一致**：

    * 已经发布、且贴到了官网布告栏 → 任何人都能取（布告栏在 `/`，没登录的人也看得到）；
    * 已经发布、只在站内 → 要登录；
    * 还没发布（草稿）→ 只有管理员；
    * 公告已撤下 → 站内仍然看得到（读过那条广播的人手里还有链接），未登录取不到。

    「和公告一致」是这里唯一的规则：图比文字更惹眼，一张图的可见性比它所配的文字更宽
    或更窄，都是一种不该出现的泄漏或死链。
    """
    stored = get_db().announcement_image(image_id)
    if not stored:
        raise ApiError(404, "找不到这张图片。")
    linked = stored.get("announcement_id")
    if not linked:
        _require_admin(request)
    elif not stored.get("is_public"):
        _require_user(request)
    elif not stored.get("active"):
        # 撤下的公告：站内还看得到（信里/对话框里那一条已经发出去过），外面的取不到。
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
    is_public = _boolean(payload, "public", False)
    image_id = _string(payload, "image_id", default="", required=False, maximum=80)
    database = get_db()
    try:
        announcement_id = database.create_announcement(
            title=title, body=body, tone=tone, deliver_email=deliver_email,
            created_by=admin["email"], is_public=is_public, image_id=image_id)
    except ValueError as exc:
        raise ApiError(422, str(exc)) from exc
    database.record_audit(action="announcement_published", actor_user_id=admin["id"],
                          actor_email=admin["email"],
                          detail=f"id={announcement_id} email={int(deliver_email)} "
                                 f"board={int(is_public)} image={int(bool(image_id))}",
                          client=_client_label(request))
    logging.info("admin %s published announcement %s (email=%s board=%s)",
                 admin["id"], announcement_id, deliver_email, is_public)
    return json_response({"ok": True, "id": announcement_id,
                          "announcements": database.list_announcements(20)})


@route("PUT", r"/api/admin/announcements/(?P<announcement_id>[^/]+)/board")
def admin_set_announcement_board(request: Request, announcement_id: str) -> Response:
    """Put an announcement on the public board at `/`, or take it off.

    Separate from publishing because the two audiences are different: the banner
    goes to accounts that signed up, the board is world-readable and indexable.
    The console asks for it explicitly rather than inferring it from "the
    operator wrote something", which would put every internal note on the open
    web by default.
    """
    admin = _require_admin(request)
    _admin_rate_limit(admin["id"])
    payload = request.json_object()
    is_public = _boolean(payload, "public", False)
    database = get_db()
    try:
        row = database.set_announcement_public(announcement_id, is_public)
    except KeyError as exc:
        raise ApiError(404, str(exc)) from exc
    except ValueError as exc:
        raise ApiError(422, str(exc)) from exc
    database.record_audit(
        action="announcement_board_on" if is_public else "announcement_board_off",
        actor_user_id=admin["id"], actor_email=admin["email"], detail=f"id={announcement_id}",
        client=_client_label(request))
    logging.info("admin %s set announcement %s board=%s", admin["id"], announcement_id, is_public)
    return json_response({"ok": True, "id": announcement_id, "is_public": row["is_public"],
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
        f"你在 CityU Mail Pilot 网站上申请的内测名额通过了，邀请码是：\n\n"
        f"    {code}\n\n"
        f"（只能用一次，14 天内有效）\n\n"
        f"从这里注册：{app_url}\n\n"
        f"两件事先说清楚：\n"
        f"· 内测期间免费，模型调用默认用管理员提供的 key、由管理员付费；你也可以在「AI 模型」里\n"
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
            return error_response(403, "Origin rejected")
    # The two Android-distribution routes sit next to the static files rather
    # than in the `@route` table: both are "read a document off disk and send
    # it", which is what the block below does, and neither is part of the API.
    if request.path == ASSETLINKS_PATH and request.method in {"GET", "HEAD"}:
        document = assetlinks_document()
        if document is None:
            return error_response(404, "页面不存在。")
        # `application/json`, without the charset the API responses carry:
        # Android's verifier is strict about the media type of this document.
        return Response(status=200, body=document,
                        content_type="application/json",
                        headers={"Cache-Control": "public, max-age=300"})
    if request.path == APK_ROUTE and request.method in {"GET", "HEAD"}:
        target = apk_path()
        if target is None:
            return error_response(404, "安装包尚未提供。")
        return file_response(target, APK_MEDIA_TYPE, download_name=APK_FILENAME)
    if request.path in STATIC_FILES and request.method in {"GET", "HEAD"}:
        name, content_type = STATIC_FILES[request.path]
        target = (STATIC_ROOT / name).resolve()
        if STATIC_ROOT not in target.parents or not target.is_file():
            return error_response(404, "页面不存在。")
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
            body = (render_landing_page(target) if request.path == "/"
                    else render_legal_page(target))
            return Response(status=200, body=body,
                            content_type=content_type, headers={"Cache-Control": "no-cache"})
        return file_response(target, content_type)
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
            return handler(request, **groups)
        except ApiError as exc:
            return error_response(exc.status, exc.detail)
    for method, entries in ROUTES.items():
        if method != request.method and any(pattern.match(request.path) for pattern, _ in entries):
            path_matched = True
            break
    if path_matched:
        return error_response(405, "方法不被允许。")
    return error_response(404, "资源不存在。")


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
            raise ApiError(400, "Content-Length 无效。") from exc
        if length < 0:
            self.close_connection = True
            raise ApiError(400, "Content-Length 无效。")
        if length > limit:
            self._discard(length)
            self.close_connection = True
            raise ApiError(413, "请求内容过大。")
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
