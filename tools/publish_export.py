"""Build the tree that can be published, and refuse to build one that cannot.

    .venv-pilot/bin/python tools/publish_export.py --out /tmp/publish
    .venv-pilot/bin/python tools/publish_export.py --out /tmp/publish --report

Why a tool instead of "clone it and delete the private bits": this working
directory is where the project *lives*, and it contains things that must never
leave it -- the operator's mailbox address, real students' addresses in a design
mock, the production host, and a diary (`HANDOVER.md`, `AGENTS.md`) whose whole
value is that it is candid. A manual copy is a decision made once, by hand, at
the end of a long day. This is the same decision written down and re-runnable.

The policy is deliberately **default-deny**: everything published is named in
``INCLUDE`` below, so a new file in this directory is not published until someone
adds it here on purpose. Two independent gates then run over the result:

1. ``handoff.scan_secrets`` -- the project's existing credential scanner, reused
   verbatim so "what looks like a credential" has exactly one definition;
2. a *personal-data* scan (`_scan_private`) -- the failure this repository is
   actually exposed to. Nothing here is a leaked API key; what would hurt is the
   operator's address, a real student's address, the production host, or the
   master-key fingerprint.

Either gate finding anything means exit code 3 and no output directory, because
"publish anyway and clean up later" is not available: a git push cannot be
recalled. See ``docs/publishing-2026-09-15.md`` for the decision record.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pilot_app import credentials  # noqa: E402

# The marker that lets a file declare itself a fixture. Defined here rather than
# imported from tools/handoff.py, which is not published: the publication tool
# must be able to run from the tree it just produced.
SNAPSHOT_SCAN_EXEMPT = re.compile(r"^handoff-security-scan: fixtures\s*$", re.MULTILINE)

# ---------------------------------------------------------------------------
# what goes out
# ---------------------------------------------------------------------------
# Whole directories, minus the names in EXCLUDE_NAMES / EXCLUDE_SUFFIXES.
INCLUDE_DIRS = (
    "pilot_app",
    "tools",
    # CI lives here, and the reason it is on this list rather than left out is
    # the same reason the list exists: a file that is not published cannot run
    # on the published repository. `.github/workflows/` is not documentation --
    # it is the thing that tells a visitor (and us) whether the tests pass, and
    # a workflow that only exists on the operator's laptop is not CI.
    ".github",
)

# Single files.
INCLUDE_FILES = (
    "LICENSE",
    "README.md",
)

# Documentation is the part most likely to carry somebody's address, so it is an
# allowlist too -- and a short one. These are the pages that help a stranger run
# this thing; the rest are the project's diary.
INCLUDE_DOCS = (
    "docs/backup-2026-09-14.md",
    "docs/compliance-2026-09-14.md",
    "docs/platform-key-2026-09-14.md",
    "docs/restore-drill.md",
    "docs/agent-monitor-2026-09-15.md",
    "docs/agent-handoff-2026-09-14.md",
    "docs/email-html-compatibility-2026-09-13.md",
    "docs/open-source-recon-2026-09-14.md",
    # The README points here for "why not an app store?". It is self-contained
    # (it references only two published files) and a self-hoster faces the same
    # question -- whereas a README that links a page we deliberately withhold is
    # a dead link on the front door, which is the thing this allowlist exists to
    # avoid in the first place.
    "docs/app-distribution-decision-2026-09-14.md",
    "docs/selfhost-distribution-recon-2026-09-14.md",
    "docs/python-distribution-recon-2026-09-14.md",
    # 依赖审计的结论：运行时只有 4 个第三方包、锁定版本与 OSV 上 0 条公告、四个都是
    # 宽松许可（与 AGPL-3.0 兼容）。自建的人会问这个，而 `test_dependency_audit` 也要求
    # 这份文档在**它所在的那棵树**里（2026-09-23 CI 三条作业红，就是它没被公开）。
    "docs/dependency-audit-2026-09-23.md",
    # How a self-hoster's account stops burning generation slots on a key the
    # provider keeps refusing. Added together with the feature (v0.63.4), because
    # this allowlist deliberately does not publish new files by default -- and
    # this is the step that would otherwise be forgotten, leaving the code public
    # and the reasoning private.
    "docs/key-circuit-2026-09-15.md",
    # What each browser suite actually checks. It lives outside AGENTS.md
    # because that card has a hard byte cap; a stranger running the checks
    # needs the same list.
    "docs/browser-checks.md",
    # Why the report detail level is a per-user choice whose default is
    # "follow the instance" -- i.e. why shipping this feature changed nobody's
    # mail. Same rule as above: new files are not public by default, so the
    # code would be public while the reasoning stayed private.
    "docs/report-mode-2026-09-15.md",
    # The visitor counter, including what it deliberately does not store. A
    # self-hoster running this code collects the same data, so the reasoning
    # has to travel with it.
    "docs/visitor-analytics-2026-09-16.md",
    # The two halves of v0.63.43: why "mailbox works but nothing ever arrived"
    # needed a third kind of stuck, and why the operator can re-run the three
    # tests for somebody -- including what that button deliberately refuses to
    # do (it cannot light the 出报告 lamp). Same rule as every line above: a new
    # file is not public until it is listed here.
    "docs/never-forwarded-2026-09-16.md",
    "docs/user-refresh-2026-09-16.md",
    # Task priority + the phone export: why the two platforms need two different
    # answers, and the five things an .ics writer gets wrong silently. A
    # self-hoster pressing the same button deserves the same reasoning.
    "docs/task-export-2026-09-16.md",
    # The landing page rewrite, one annotated screenshot at a time: why a
    # sentence was deleted, and -- more useful to a stranger -- which sentences
    # are pinned by tests and therefore must not be "tidied up" later. Same
    # allowlist rule as every line above.
    "docs/landing-optimization-2026-09-16.md",
    # Why a dead end had to become a button, and why the list of "this provider
    # cannot work" lives in exactly one place. A self-hoster hits the same dead
    # end with the same providers.
    "docs/mailbox-switch-2026-09-16.md",
    # How to ship an update to your own instance, which is the one thing the
    # public README does not cover (it stops at the first install). It travels
    # with `tools/deploy_prod.sh`, whose --help points at it: publishing the
    # script while withholding the page would be a dead reference in the public
    # tree, which is exactly what this allowlist exists to prevent.
    "docs/deploy-runbook-2026-09-17.md",
    # Why a 163/126 mailbox could not be read at all until v0.63.77: the server
    # demands the optional RFC 2971 `ID` command, and `imaplib` never sends it.
    # A self-hoster pointing this code at 163 hits the identical wall, and the
    # two traps inside (imaplib's command table; a test double that hid it) are
    # worth more to them than the workaround alone. Addresses are redacted by
    # hand -- the export gate knows the operator's own addresses, not a user's.
    "docs/imap-id-163-2026-09-18.md",
    # 日历待办的纯规则美化（v0.63.89）：由一次**直接推到公开 main** 的贡献并入，
    # 评审后改了四处。文档里同时留下了生产实测（304 条任务里 52% 落到「其他」、
    # 12% 会变成定时事件）——那些数字就是「为什么这么做」的答案。跟着一起公开的
    # 还有 100 条案例表 `tools/taskexport_batch_check.py`：工具会出去，它的说明
    # 不出去就是公开树里的死引用。规矩不变：新文件默认不公开，要显式列在这里。
    "docs/calendar-task-beautify-2026-09-18.md",
    # 同一位贡献者留下的调研（Jev 决策层值不值）。它已经随那次直接推送公开了，
    # 内容里没有秘密（导出闸门逐字扫过：无生产 IP、无真实邮箱、无密钥）。与其在
    # 下一次发布时**悄悄删掉别人的文档**，不如显式承认它在这里。
    "docs/jev-decision-layer-cost-test-2026-09-17.md",
    # SQLite 那一轮（v0.63.90）的实测记录：两个索引**为什么留下**、三个候选**为什么
    # 退役**、`WAL` 为什么从每个连接搬走。理由和上面每一条一样：`tools/sqlite_*.py`
    # 是一整批会一起公开的测量脚本，结论不公开，它们就只是一堆没有答案的脚本。
    "docs/sqlite-performance-2026-09-18.md",
)

# Never published, whatever else says otherwise. Each line is a reason.
EXCLUDE_NAMES = {
    "__pycache__",
    ".DS_Store",
    ".secrets",          # QQ app password handed over for diagnostics
    "handoff",           # cross-agent ledger + full source snapshots
    "publish-private.json",  # the scrub rules themselves are the private data
    "dist",              # release tarballs (the reader builds their own)
    "work",
    "outputs",
    "cloud_deploy",
    "outlook_ai_assistant",
    ".venv-pilot",
    ".e2e",
    # 运营者上传的**客服群二维码**（`pilot_app/static/wechat-group.png`）：微信群码只有 7 天，
    # 它会随服务器上的文件一起换，但**不该进公开仓库**——那里面躺着一张必然过期的码，
    # 别人 clone 下来只会得到一个死链。与安卓安装包同一条规矩：构建/运营产物不进公开树。
    "wechat-group.png",
    ".tools",
    "preview",
    # Playwright is installed with npm, and npm installs where you run it. The
    # documented place is the repository root (outside every include dir), but
    # one `npm install` typed inside tools/ would otherwise be walked and
    # published -- tens of thousands of files, and none of them ours.
    "node_modules",
    # Tools that are about *this* installation rather than about the software.
    "handoff.py",              # reads AGENTS.md / HANDOVER.md, which are not published
    "test_handoff.py",         # tests that tool, so it cannot run without it
    "post_first_notice.py",    # hardcodes the operator's address
    "make_design_options.py",  # HTML mocks built from real pilot rows
    # 飞书命令台：**不是这个 app 的一部分**（用户 2026-09-18 原话）。它是「人给
    # agent 派活」的通道，连的是运营者自己的群，跟本产品无关。按**文件名**排除而
    # 不是按路径——放在树里哪个位置都不该跟着公开树出去。`test_ci` 盯着这份名单。
    "feishu_console.py",
    "test_feishu_console.py",
    "feishu-console",
    ".lark-console",           # 运行状态：真实群消息的收件箱与游标
}

EXCLUDE_SUFFIXES = (".pyc", ".sqlite3", ".sqlite3-shm", ".sqlite3-wal")

# The .gitignore that the published repository should have. Written by this tool
# rather than copied, because the local one knows about local-only paths.
PUBLIC_GITIGNORE = """\
# Local-only. Kept out of the repository on purpose: the master key never leaves
# the server's 0600 environment file, and a database with real mail in it does
# not belong in a public repository either.
.secrets/
pilot.env
*.env
# 本机导出规则（`publish-private.json`）**以及它的备份/改名副本**：2026-09-23 那台机器
# 给这份文件做过 `.bak-日期` 副本，精确名挡不住 —— 副本同样是要藏起来的东西。
# （这条与源码树的 `.gitignore` 同源，`test_preflight.PrivateFileBackupTests` 两棵树都盯。）
publish-private.json*
*.sqlite3
*.sqlite3-shm
*.sqlite3-wal
.venv/
.venv-pilot/
__pycache__/
*.pyc
dist/
preview/
# Scratch output from running the app locally (`run.ps1` writes a throwaway
# database here). Never committed — it is a working directory, not source, and
# the database in it is exactly the kind of file the header above is about.
local-run/
.DS_Store
"""

# ---------------------------------------------------------------------------
# scrubbing
# ---------------------------------------------------------------------------
# Applied in order, so a specific rule must come before a general one (the
# operator's address is also a QQ address).
#
# Note what is NOT here. The first version of this file listed the production IP
# and the operator's address as literals, which meant the deny-list itself was
# the leak: publishing the tool published exactly the two strings it exists to
# remove. Values specific to *this* installation therefore live in
# `publish-private.json`, which is never published, and only generic rules --
# shapes that are personal wherever they appear -- are written down here.
GENERIC_SCRUBS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"\b[A-Za-z0-9._%+-]+@my\.cityu\.edu\.hk\b"),
     "student@my.cityu.edu.hk", "真实用户邮箱（CityU）"),
)

PRIVATE_RULES_PATH = ROOT / "publish-private.json"


def load_forbidden() -> list[str]:
    """Identifiers that must not appear anywhere in a published file.

    The scrub rules handle *shapes* (an address at this domain, this host, this
    key name). This handles the case that got past them the first time: a bare
    account name with no address around it. The list lives in the unpublished
    private file for the obvious reason -- the names are the thing being hidden.
    """
    import json

    if not PRIVATE_RULES_PATH.is_file():
        return []
    try:
        payload = json.loads(PRIVATE_RULES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(item.get("value", "")) for item in payload.get("forbidden", [])
            if item.get("value")]


def load_private_rules() -> list[tuple[re.Pattern[str], str, str]]:
    """Installation-specific replacements, from a file that is never published.

    Missing file is not an error: the export still runs, with only the generic
    rules, and the verifier below is what decides whether the result is safe. A
    copy of this project that someone else downloads has no private rules and
    needs none.
    """
    import json

    if not PRIVATE_RULES_PATH.is_file():
        return []
    try:
        payload = json.loads(PRIVATE_RULES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print(f"警告：{PRIVATE_RULES_PATH.name} 读不出来（{error}），只用通用规则。", file=sys.stderr)
        return []
    rules = []
    for entry in payload.get("rules", []):
        try:
            rules.append((re.compile(entry["pattern"]), entry["replace"], entry["label"]))
        except (KeyError, re.error) as error:
            print(f"警告：跳过一条无效规则（{error}）", file=sys.stderr)
    return rules

# Publication gate for addresses: "provably made up", checked on two axes.
#
# The first run of this tool produced the complete inventory (76 distinct
# addresses, 195 occurrences); reviewing it showed every one is either a fixture
# this project's tests invented or a public corporate role address that its
# sample mail quotes. Rather than paste 76 lines, the two things that actually
# distinguish a fixture are written down: a *stand-in local part* and a *domain
# that is reserved, a test provider, or quoted sample mail*. Anything outside
# both lists fails the export, so a real address arriving in the tree stops the
# build until a person looks at it -- which is the property worth having.
FIXTURE_LOCAL_PARTS = frozenset("""
a b c d e p s t t1 u v x y me box box1 pilot report reports old other owner purge new
one two someone you student teacher library lib career fees finance reg registry gate hello
getstarted no-reply noreply noreply_cap275421 account-security-noreply attacker operator
mixed boss privacy promo secret app-pass-123 shared-forward demo-cityu-1 demo-personal-1
10000 cityu-mail-pilot-alert
paused refused wronghost zhangsan
also broken down good
user
""".split())
# `user`（2026-09-26 加）：`test_rag_offline.py` 的 URL 边界夹具里有一条
# `https://user@www.cityu.edu.hk/a` —— 那**不是邮箱**，是一个「带 userinfo 的 URL」，
# 测试要的正是「这种 URL 必须被拒绝」。域名的安全靠上面 SAFE_DOMAINS 那条
# `*.cityu.edu.hk`（学校的公开站点），局部名 `user` 与 `you`/`someone`/`me` 同类，
# 是通用占位词而不是谁的账号。
# `paused` / `refused` / `wronghost` / `zhangsan`（2026-09-20 加）：`test_mailbox_check.py`
# 里那几行夹具用的就是这些**描述这一行在测什么**的局部名（暂停的账号、被拒的码、
# 主机填错的账号、中文占位名张三，以及 good/also/broken/down 这几个按结果命名的）。域名仍是真实存在的服务商域名，所以它们和
# `gate@qq.com` 一样，靠「局部名是替身」通过闸门。
# `registry` 与 `gate`（v0.63.90 加）：
#   registry@cityu.edu.hk 是 tools/sqlite_bench.py 造 3 万封假邮件时用的**发件人**，
#   和已经在名单里的 library/career/fees 同类（学校的机构角色，不是一个信箱）；
#   gate@qq.com 是 test_compliance.MailAuthorizationGateTests 的夹具邮箱。
#   两个都只出现在测试/基准里，没有任何真实用户用过这两个地址。

# Domains that are proof on their own: nobody has a mailbox at any of these, so
# the local part may be anything ("member-a@example.com" is still made up).
RESERVED_DOMAINS = (
    r"^(?:[a-z0-9-]+\.)*(?:example(?:\.(?:com|org|net|edu|co))?|invalid|test|localhost)$",
    r"^[a-z]$|^[a-z]\.(?:com|hk|org|net)$",      # "a@b.com", "t@x.hk"
    r"^[a-z0-9.-]*\.service$",                    # a unit template, not a mailbox
    # GitHub 给不想暴露真实邮箱的人生成的地址。它本来就是匿名形式，而且这个仓库里
    # 出现的是**默认占位值**，不是任何人的邮箱。
    r"^users\.noreply\.github\.com$",
)

# Domains that exist in the real world, so the local part has to be a stand-in
# before the address counts as a fixture.
SAFE_DOMAINS = (
    r"^(?:qq|163|126|gmail|outlook|icloud|yahoo|yeah|foxmail|q)\.(?:com|com\.hk|net)$",
    # 网易的 VIP 域名（`vip.163.com` / `vip.126.com`）与 126.com 是真实存在的邮箱域，
    # 只是上面那条单标签的正则盖不住——本项目的预置表里就有它们（一个域名一台机器），
    # 测试必须能写出这些域名。局部名仍然要过 FIXTURE_LOCAL_PARTS 那一关，
    # 所以「真实用户的地址」照样会被拦下来（2026-09-20 就是它拦住的）。
    r"^vip\.(?:163|126)\.com$",
    r"^(?:[a-z0-9-]+\.)*cityu\.edu\.hk$",
    r"^smtp\d+\.ad\.cityu\.edu\.hk$",
    r"^notcityu\.edu\.hk$",                       # anti-spoofing test look-alikes
    # 同类：`webmail_home` 必须按域名边界匹配，`notqq.com` 就是拿来测这条边界的
    # 虚构域名（它**不是** QQ 邮箱）。测试在 test_read_original.WebmailHomeTests。
    r"^notqq\.com$",
    r"^[a-z0-9.-]*cityu\.edu\.hk\.evil\.com$",
    r"^(?:mail\.grammarly\.com|codefinity\.com|fairwood\.com\.hk|accountprotection\.microsoft\.com)$",
    r"^other\.edu$",
    # OSV（Google 的开源漏洞库）的公开 API 主机：它出现在 `deps.py` 的只读查询里，
    # 也出现在 `test_dependency_audit.py` 那条「出站闸门必须拒绝带凭据的 URL」的用例里
    # （`https://user:pw@api.osv.dev/v1/query`）——其中 `pw` 是 URL 的 userinfo，
    # **不是任何人的邮箱**。局部名照样要过 `FIXTURE_LOCAL_PARTS`/长度那一关，
    # 所以真实用户的地址仍然拦得住。
    r"^api\.osv\.dev$",
)

# Vendors' own published role addresses. These are not anybody's mailbox and they
# cannot be scrubbed, because they are quoted **inside the error text the server
# sends us** -- rewriting one would mean shipping a fabricated quote. Kept exact
# rather than as a domain rule: `188.com` is NetEase's public mailbox domain, so
# "anything @188.com is safe" would be false.
PUBLIC_ROLE_ADDRESSES = (
    # 网易（163/126）在 "Unsafe Login. Please contact kefu@188.com for help" 里让
    # 用户联系的客服邮箱，出现在服务器原话里。
    "kefu@188.com",
)

# "20260913091828.5982EBAE32@smtp82.ad.cityu.edu.hk" -- a fixture Message-ID, and
# the local part is a timestamp plus a hex run rather than anybody's name.
MESSAGE_ID_LOCAL = re.compile(r"^\d{10,}\.[0-9A-Fa-f]{6,}$")


def _address_is_safe(address: str) -> bool:
    local, _, domain = address.rpartition("@")
    if not local or not domain:
        return False
    # `git@github.com` 是 SSH 远程地址，不是邮箱：局部名 `git` 是全世界 VCS 都用
    # 的那个系统账号。正则分不出这两者，所以在这里排除。
    if local in {"git", "hg", "svn"}:
        return True
    # 厂商自己公布的客服地址（出现在服务器原话里，见 PUBLIC_ROLE_ADDRESSES）。
    if address.lower() in PUBLIC_ROLE_ADDRESSES:
        return True
    domain = domain.lower()
    if any(re.fullmatch(pattern, domain) for pattern in RESERVED_DOMAINS):
        return True
    if not any(re.fullmatch(pattern, domain) for pattern in SAFE_DOMAINS):
        return False
    if MESSAGE_ID_LOCAL.match(local):
        return True
    return local.lower() in FIXTURE_LOCAL_PARTS or len(local) <= 3
ADDRESS = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# Only *this* machine's home directory is a leak. `/home/zulip` in a research note
# is another project's documented path, and flagging it would teach the reader to
# skim past this check.
PRIVATE_HOME = re.compile(re.escape(str(Path.home().parent / Path.home().name)))

# Ranges that are safe to publish, with the reason each one is safe.
SAFE_IP_PREFIXES = (
    "127.0.0.1",       # loopback
    "192.0.2.",        # RFC 5737 documentation
    "198.51.100.",     # RFC 5737 documentation
    "203.0.113.",      # RFC 5737 documentation (also the placeholder above)
    "10.",             # RFC 1918, not routable
    "192.168.",        # RFC 1918
    # The offline-geolocation tests need addresses that Python's `ipaddress`
    # does *not* classify as private: RFC 5737 documentation ranges are
    # private, so a lookup for one answers "unknown" before the table is ever
    # consulted -- which is the behaviour those tests exist to check. They are
    # therefore written against 8.8.8.0/24, a public DNS resolver chosen
    # precisely because it identifies nobody: no visitor, no server of ours.
    "8.8.8.",
    # 169.254.0.0/16 是 RFC 3927 的 link-local，其中 169.254.169.254 是各家云的元数据
    # 端点：**公开常量，指不出我们的任何一台机器**。`test_rag_offline.py` 拿它当
    # 「必须被拒绝的私网地址」夹具（`public_addresses` 要拒掉它），所以它留在树里。
    "169.254.",
)
SAFE_IP_172 = re.compile(r"^172\.(1[6-9]|2\d|3[01])\.")


def _scrub(text: str, rules) -> tuple[str, list[str]]:
    """Return the text with private values replaced, and what was replaced."""
    hits: list[str] = []
    for pattern, replacement, label in rules:
        text, count = pattern.subn(replacement, text)
        if count:
            hits.extend([label] * count)
    return text, hits


def _scan_private(text: str) -> list[str]:
    """Personal data and infrastructure that survived scrubbing.

    Deliberately checks the *result*, not the input: a scrubber that quietly
    stops matching (because someone edits the source text) must fail loudly here
    rather than ship.
    """
    problems: list[str] = []
    for match in ADDRESS.finditer(text):
        if not _address_is_safe(match.group(0)):
            problems.append(f"未替换的邮箱地址：{match.group(0)}（若确属虚构夹具，"
                            f"加进 SAFE_ADDRESS_PATTERNS 并说明理由）")
    for match in IPV4.finditer(text):
        octets = [int(part) for part in match.group(0).split(".")]
        if any(part > 255 for part in octets):
            continue
        value = match.group(0)
        if value.startswith(SAFE_IP_PREFIXES) or SAFE_IP_172.match(value):
            continue
        problems.append(f"未替换的公网 IP：{value}")
    for match in PRIVATE_HOME.finditer(text):
        problems.append(f"本机绝对路径：{match.group(0)}")
    return problems


def source_stamp() -> str:
    """A hash of the *sources* this export was made from.

    Why it exists: a refused export leaves the previous tree in `dist/publish`
    untouched (by design -- ``build`` stages elsewhere and only moves a good tree
    into place). That tree is still self-consistent, so `shasum -c
    PUBLISH-MANIFEST.txt` passes and `publish_push.sh` would happily push a
    **stale** tree while the change that got refused is silently missing --
    which is exactly what happened on 2026-09-17 (a new fixture address was
    refused, the push reported success, and the round's work was not published).

    The stamp is written into the tree and re-checked before pushing, so
    "the sources changed after this export" and "the last export was refused"
    both stop the push with one sentence instead of a silent stale publish.

    It hashes the **source** bytes, not the exported ones, so it can be computed
    the same way by both the export and any later check.
    """
    digest = hashlib.sha256()
    for relative in sorted(str(item) for item in iter_files()):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256((ROOT / relative).read_bytes()).hexdigest().encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def iter_files() -> list[Path]:
    """Every file the policy selects, relative to the repository root."""
    chosen: list[Path] = []
    for name in INCLUDE_DIRS:
        base = ROOT / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT)
            if any(part in EXCLUDE_NAMES for part in relative.parts):
                continue
            if relative.name in EXCLUDE_NAMES:
                continue
            if path.name.endswith(EXCLUDE_SUFFIXES):
                continue
            chosen.append(relative)
    for name in INCLUDE_FILES + INCLUDE_DOCS:
        path = ROOT / name
        if path.is_file():
            chosen.append(Path(name))
    return sorted(set(chosen))


# 发布与保存是两件事。工作区里可能正躺着**另一个人写了一半的东西**（本仓库同时有两个
# agent 在改），而公开树是给外面的人看的承诺——它应该是「提交过的状态」，不是「此刻磁盘
# 上的样子」。2026-09-18 真的发生过一次：一次推送把另一个会话没写完的文档一起带了出去，
# 闸门（凭据/隐私）都过了，所以没有人会发现。
#
# 做法是**闸门而不是架构**：仍然按策略导出工作区的文件，但导出前先问一次 git——
# 「要公开的这些路径里，有没有和 HEAD 不一样的？」有就拒绝，并列出是哪些，让人自己决定
# （提交它，或者明确带 PILOT_PUBLISH_ALLOW_DIRTY=yes 表示「我知道，就要这样推」）。
DIRTY_OVERRIDE_ENV = "PILOT_PUBLISH_ALLOW_DIRTY"


def _git(*args: str) -> tuple[int, str]:
    try:
        result = subprocess.run(("git", *args), cwd=ROOT, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return result.returncode, result.stdout


def dirty_published_paths(files: list[pathlib.Path]) -> list[str]:
    """要公开的文件里，哪些与 HEAD 不一致（改动/新增/删除/重命名）。

    没有 git、或者根本不在仓库里时返回空表：这个闸门是**加分项**，不该让一个不带 git 的
    环境无法发布（公开仓库本身可以只是一个导出目录）。
    """
    code, _ = _git("rev-parse", "--is-inside-work-tree")
    if code != 0:
        return []
    code, porcelain = _git("status", "--porcelain", "--untracked-files=all", "--", ".")
    if code != 0:
        return []
    published = {str(item) for item in files}
    dirty: set[str] = set()
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        entry = line[3:].strip().strip('"')
        if " -> " in entry:                      # 重命名：两边都算
            for part in entry.split(" -> "):
                if part.strip().strip('"') in published:
                    dirty.add(part.strip().strip('"'))
            continue
        if entry in published:
            dirty.add(entry)
    return sorted(dirty)


def build(out_dir: Path, rules, forbidden: list[str] | None = None) -> int:
    files = iter_files()
    forbidden = forbidden or []
    if os.environ.get(DIRTY_OVERRIDE_ENV, "") != "yes":
        dirty = dirty_published_paths(files)
        if dirty:
            print("拒绝导出——这些**要公开**的文件和最后一次提交不一样：", file=sys.stderr)
            for name in dirty:
                print(f"  {name}", file=sys.stderr)
            print("\n公开树应当是「提交过的状态」：先 `git add` + `git commit` 再导出；"
                  f"确实要推未提交的内容，就带 {DIRTY_OVERRIDE_ENV}=yes 再跑一次。", file=sys.stderr)
            return 3
    scrubbed: dict[str, int] = {}
    problems: list[str] = []
    exempted: list[str] = []
    written: list[tuple[str, str]] = []

    for relative in files:
        source = ROOT / relative
        try:
            text = source.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            # Binary assets (the PNGs under static/) travel unchanged; they are
            # small, hand-generated by tools/make_icons.py, and carry no EXIF.
            blob = source.read_bytes()
            written.append((str(relative), hashlib.sha256(blob).hexdigest()))
            target = out_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
            continue

        cleaned, hits = _scrub(text, rules)
        for label in hits:
            scrubbed[label] = scrubbed.get(label, 0) + 1
        # Source mode: the same call the offline snapshot makes. It drops the
        # patterns that exist for prose and logs but match ordinary code
        # (`password = secrets.decrypt(...)`), and it refuses to let the docs
        # vouch for a value the docs themselves contain.
        if SNAPSHOT_SCAN_EXEMPT.search(cleaned):
            # The project's existing escape hatch, with the project's rule: the
            # declaration has to be on a line of its own, and the file is named
            # in the output so a reader can see what was waved through.
            exempted.append(str(relative))
        else:
            for problem in credentials.scan_secrets(cleaned, source=True):
                problems.append(f"{relative}: {problem}")
        for problem in _scan_private(cleaned):
            problems.append(f"{relative}: {problem}")
        for token in forbidden:
            if token in cleaned:
                problems.append(f"{relative}: 出现了禁止公开的标识符（{len(token)} 字符，"
                                f"见 publish-private.json）")

        target = out_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(cleaned, encoding="utf-8")
        written.append((str(relative), hashlib.sha256(cleaned.encode("utf-8")).hexdigest()))

    (out_dir / ".gitignore").write_text(PUBLIC_GITIGNORE, encoding="utf-8")
    written.append((".gitignore", hashlib.sha256(PUBLIC_GITIGNORE.encode("utf-8")).hexdigest()))

    # 这一棵树的**来源**指纹（见 `source_stamp`）。放进清单里，于是它自己也受
    # `shasum -c` 保护：谁把这份导出连同指纹一起改了，推送前那一关照样会红。
    stamp = source_stamp()
    (out_dir / "SOURCE-STAMP").write_text(stamp + "\n", encoding="utf-8")
    written.append(("SOURCE-STAMP", hashlib.sha256((stamp + "\n").encode("utf-8")).hexdigest()))

    # Pure hash lines, so `shasum -a 256 -c PUBLISH-MANIFEST.txt` is silent and
    # therefore actually useful: a tool that prints warnings every time is a tool
    # whose output nobody reads. The prose lives in its own file.
    manifest = [f"{digest}  {name}" for name, digest in sorted(written)]
    (out_dir / "PUBLISH-MANIFEST.txt").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    code, head = _git("rev-parse", "--short", "HEAD")
    commit_line = f"提交：{head.strip()}\n" if code == 0 and head.strip() else "提交：（这个环境里没有 git 信息）\n"
    (out_dir / "PUBLISH-NOTES.txt").write_text(
        "这个包由 tools/publish_export.py 生成。\n"
        f"文件数：{len(written)}\n"
        + commit_line + "\n"
        "自证完整性：shasum -a 256 -c PUBLISH-MANIFEST.txt\n"
        "私有信息（生产域名/IP、运营者与用户的邮箱、部署密钥名、主密钥指纹）\n"
        "在导出时已被替换成占位值；替换规则不在这个包里。\n", encoding="utf-8")

    if problems:
        print("拒绝导出——下面这些内容不该出现在公开仓库里：", file=sys.stderr)
        for problem in sorted(set(problems)):
            print(f"  {problem}", file=sys.stderr)
        print("\n修掉它们（或调整 SCRUBS / INCLUDE），再跑一次。", file=sys.stderr)
        return 3

    print(f"文件数：{len(written)}")
    if exempted:
        print("按文件内声明跳过了凭据扫描：")
        for name in sorted(exempted):
            print(f"  {name}")
    if scrubbed:
        print("替换掉的私有信息：")
        for label, count in sorted(scrubbed.items()):
            print(f"  {label}: {count} 处")
    else:
        print("没有需要替换的私有信息（这本身可能值得怀疑——核对一下）。")
    print("清单：PUBLISH-MANIFEST.txt（`shasum -a 256 -c` 可自证）")
    return 0


def report() -> int:
    """Print what the policy selects, without writing anything."""
    files = iter_files()
    print(f"会公开 {len(files)} 个文件：")
    for relative in files:
        print(f"  {relative}")
    for name, reason in sorted({name: "本地/私密" for name in EXCLUDE_NAMES}.items()):
        if (ROOT / name).exists():
            print(f"排除：{name}（{reason}）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成可以公开的源码树（默认拒绝）")
    parser.add_argument("--out", default="", help="输出目录；省略时只打印清单")
    parser.add_argument("--report", action="store_true", help="只打印会公开哪些文件")
    parser.add_argument("--force", action="store_true", help="输出目录已存在时也覆盖")
    parser.add_argument("--stamp", action="store_true",
                        help="只打印当前源码的来源指纹（推送前用它核对那棵树是不是旧的）")
    args = parser.parse_args(argv)

    if args.stamp:
        print(source_stamp())
        return 0
    if args.report or not args.out:
        return report()

    out_dir = Path(args.out).expanduser().resolve()
    if out_dir.exists() and not args.force:
        print(f"{out_dir} 已存在；加 --force 覆盖，或换一个目录。", file=sys.stderr)
        return 2

    # Build somewhere else and move it into place only on success. A refused
    # export must not leave a directory that looks ready to publish: the whole
    # point of the gate is that the easy next step is not available.
    staging = out_dir.parent / (out_dir.name + ".building")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        code = build(staging, GENERIC_SCRUBS + tuple(load_private_rules()), load_forbidden())
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if code != 0:
        shutil.rmtree(staging, ignore_errors=True)
        return code
    if out_dir.exists():
        shutil.rmtree(out_dir)
    staging.rename(out_dir)
    print(f"已生成 {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
