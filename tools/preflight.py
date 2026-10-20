#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""上线前那一遍：全量单测 + 19 套浏览器检查 → 一句说得清的 GO / NO-GO。

    .venv-pilot/bin/python tools/preflight.py                   # 跑「上线前那一遍」（默认就是这个）
    .venv-pilot/bin/python tools/preflight.py --pr 3            # PR #3 的补丁打进临时副本，在副本里跑同一遍
    .venv-pilot/bin/python tools/preflight.py --patch x.diff    # 同上，补丁来自本地文件
    .venv-pilot/bin/python tools/preflight.py --serve           # 起预发站点（自己的库、自己的端口、假数据）
    .venv-pilot/bin/python tools/preflight.py --serve --status  # 预发站点还开着吗
    .venv-pilot/bin/python tools/preflight.py --serve --stop    # 关掉
    .venv-pilot/bin/python tools/preflight.py --open            # 【在 Mac 上跑】开隧道 + 打开浏览器

为什么是这一层、而不是又一个测试运行器：单测与 19 套浏览器检查各自都有专门的运行器
（`unittest discover` 与 `tools/run_browser_checks.sh`），它们已经知道那些坑（每个套件一个
干净库与端口、失败套件的日志在哪儿、CI 注解怎么发）。这一层只做三件事：**跑**、**读结果**、
**给一句人能转述的结论**。「绿了几条」不是结论，「GO / NO-GO + 红的是哪几条」才是。

三条底线（改这个文件时不许破）：

1. **绝不碰生产**：只用临时库、临时端口、假主密钥；不部署、不碰 /etc、不碰生产库、不发信。
   预发站点**只起 web 进程** —— 会发信的是 worker，而 worker 不在这里起。
2. **秘密不进文件、不进日志**：预发站点用的是仓库里本来就公开的假 key（`AAAA…=`）和
   `tools/seed_preview.py` 造的假账号；这个脚本不读、不打印任何真 key。
3. **不改工作区**：`--pr` / `--patch` 只在临时副本里动手，跑完还核对工作区一个字节没变。

退出码：**0 = GO**；**1 = NO-GO**（点得出名字）；**3 = 这一遍根本没法开始**（补丁打不上、
Playwright 没装、端口占着…）；**4 = 没跑全**（`--no-browser` / `--no-unit` / `--only`），它**不算 GO**。
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import pathlib
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]

# 临时东西全在这里：运行记录、PR 副本、预发站点的库与 pid。**换个地方只要设 PREFLIGHT_HOME**。
# 默认 /tmp 是因为它本来就是一次性的 —— 预检的产物没有任何一件值得留到重启之后。
PREFLIGHT_HOME = pathlib.Path(os.environ.get("PREFLIGHT_HOME") or "/tmp/preflight")

# 假 key / 假账号：和 tools/run_browser_checks.sh、tools/seed_preview.py 用的是同一套，
# 也就是说这份文件里没有、也不许出现任何真凭据。主密钥是 32 个 0 字节的 base64。
FAKE_MASTER = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
ADMIN_EMAIL = "boss@example.com"
ADMIN_PASSWORD = "a-long-enough-password"
DEFAULT_STAGING_PORT = 8788
STAGING_SMOKE = ("shell_check", "browser_check")
# 运行器用来框住每一套与最后那个汇总块的分隔线。解析汇总时靠它把「运行器的结论」
# 与「套件自己的输出」分开。
SEPARATOR = "═" * 10

# 预发站点**只**需要这些环境变量。生产那套（真 key、真 SMTP、真库）一个都不进来。
STAGING_ENV = {
    "INFE_PILOT_MASTER_KEY": FAKE_MASTER,
    "INFE_PILOT_COOKIE_SECURE": "0",          # 预发走 http://127.0.0.1，Secure cookie 会登不上
    "INFE_PILOT_ADMIN_EMAILS": ADMIN_EMAIL,
    "INFE_PILOT_ALERTS": "0",                 # 哨兵/告警一律不跑：这个进程只服务网页
    "INFE_PILOT_SOURCE_URL": "",              # 不开源链接那一块的开关，预发不需要
}

# 不能进临时副本的东西。`.secrets/` 与 `publish-private.json` 是**本机秘密**，一步都不许走；
# dist/handoff 的**大块生成物**、`.venv-pilot` 与 `node_modules` 见下面两行与符号链接。
COPY_EXCLUDE = {
    ".git", ".e2e", ".secrets", ".venv", ".venv-pilot", "node_modules",
    "dist", "work", "preview", "__pycache__", ".mypy_cache", ".pytest_cache",
    # 本机视频生成工具链：**65 GB**（ComfyUI 源码 + 约 17 GB 模型权重），
    # 与邮件助手无关、已 gitignore（AGENTS.md §7）。不挡的话 `--pr/--patch` 会试着把
    # 它整个拷进副本，然后死在「磁盘满」上 —— 那是工装故障，看起来却像补丁打不上。
    "videogen",
}
# 按后缀/文件名挡：几百 MB 的发布包与快照、本机环境文件。**不要把整个 `handoff/` 挡掉** ——
# 单测要读 `handoff/HANDOFF.md` 那三个小文件，整目录一挡，副本里的单测就红
# （2026-09-20 试打补丁那一轮就是这么被抓出来的：`test_handoff` 说少带了三个文件）。
COPY_EXCLUDE_SUFFIXES = (".pyc", ".sqlite3", ".sqlite3-shm", ".sqlite3-wal",
                         ".tar.gz", ".sha256", ".zip", ".log")
COPY_EXCLUDE_FILES = {".env"}
# 前缀，不是精确名：**给秘密文件做备份/改名**（`publish-private.json.bak-20260923`）是
# 2026-09-23 那台机器真干过的事，精确名挡不住 —— 副本同样一个字都不该被复制出去。
COPY_EXCLUDE_PREFIXES = ("publish-private.json",)


def copy_name_is_excluded(name: str) -> bool:
    """Whether one file name is kept out of the preflight copy.

    Split out so a test can exercise **these rules** instead of restating the
    pattern list and then checking its own restatement.
    """
    return (name in COPY_EXCLUDE_FILES
            or name.startswith(COPY_EXCLUDE_PREFIXES)
            or name.endswith(COPY_EXCLUDE_SUFFIXES))


_RUNNING: list[subprocess.Popen] = []   # 退出时要把还活着的子进程组收掉

say = print


def die(message: str, code: int = 3):
    print(f"\n[停] {message}", file=sys.stderr)
    raise SystemExit(code)


def rule(title: str = ""):
    say("═" * 66)
    if title:
        say(f"  {title}")


# ------------------------------------------------------------------ 小工具

def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def port_free(port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def http_ok(url: str, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= int(response.status) < 400
    except Exception:
        return False


def pass_env(extra: dict) -> dict:
    """子进程的环境：继承当前 shell，但**盖上**我们要的那几个。

    `INFE_PILOT_DB` 一定被指向一个临时文件：这一遍跑的东西不许碰到任何既有库。
    """
    env = dict(os.environ)
    for key in list(env):
        if key.startswith("INFE_PILOT_"):
            env.pop(key)
    env.update(extra)
    return env


def run_logged(cmd: list, cwd: pathlib.Path, env: dict, log_path: pathlib.Path,
               timeout: int) -> tuple:
    """跑一条命令，输出进日志文件；超时就把**整个进程组**收掉（不留孤儿 web 服务）。"""
    started = time.monotonic()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "wb") as handle:
        proc = subprocess.Popen(cmd, cwd=str(cwd), env=env, stdout=handle,
                                stderr=subprocess.STDOUT, start_new_session=True)
        _RUNNING.append(proc)
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
            proc.wait()
            rc = 124
        finally:
            if proc in _RUNNING:
                _RUNNING.remove(proc)
    return rc, time.monotonic() - started


def tail(path: pathlib.Path, lines: int = 6) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(text[-lines:])


def duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"


# ------------------------------------------------------------------ 台账：一次运行的记录

class Ledger:
    """一条运行的记录（谁、哪棵树、跑了什么、结论），落盘成 summary.txt / summary.json。

    留着它有两个用处：出问题时有一份带时刻的现场；`--pr` 的副本路径写在里面，
    想知道「那份补丁到底长什么样」不用重跑。
    """

    def __init__(self, run_dir: pathlib.Path):
        self.dir = run_dir
        self.data = {
            "started_at": now_iso(),
            "finished_at": None,
            "root": str(ROOT),
            "tree": str(ROOT),
            "kind": "workspace",
            "patch": None,
            "version": version_of(ROOT),
            "commit": git_line(["rev-parse", "--short", "HEAD"]) or "",
            "dirty_before": git_line(["status", "--porcelain"]) or "",
            "dirty_after": None,
            "phases": [],
            "verdict": None,
            "exit": None,
        }

    def phase(self, entry: dict) -> dict:
        self.data["phases"].append(entry)
        return entry

    def write(self, verdict: str, code: int) -> pathlib.Path:
        self.data["verdict"] = verdict
        self.data["exit"] = code
        self.data["finished_at"] = now_iso()
        (self.dir / "summary.json").write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        return self.dir


def version_of(tree: pathlib.Path) -> str:
    try:
        text = (tree / "pilot_app" / "__init__.py").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "?"
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    return match.group(1) if match else "?"


def git_line(args: list) -> str:
    try:
        result = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


# ------------------------------------------------------------------ ① 单测

UNIT_RAN = re.compile(r"^Ran (\d+) tests? in ", re.M)
UNIT_OK = re.compile(r"^OK", re.M)
UNIT_BAD = re.compile(r"^(FAIL|ERROR): (\S+).*$", re.M)


def run_units(tree: pathlib.Path, run_dir: pathlib.Path, timeout: int) -> dict:
    python = tree / ".venv-pilot" / "bin" / "python"
    if not python.exists():
        die(f"找不到 {python} —— 单测跑不起来。先照 AGENTS.md §5 建好 .venv-pilot。")
    log = run_dir / "unit.log"
    cmd = [str(python), "-m", "unittest", "discover", "-s", "pilot_app/tests", "-p", "test_*.py"]
    rc, seconds = run_logged(cmd, tree, pass_env(
        {"INFE_PILOT_DB": str(run_dir / "unit-scratch.sqlite3")}), log, timeout)
    result = parse_unit_log(log.read_text(encoding="utf-8", errors="replace"), rc)
    result.update({"seconds": seconds, "log": str(log)})
    return result


def parse_unit_log(text: str, rc: int) -> dict:
    """`unittest` 的输出 → 一句能进结论的话。判据是**退出码 + OK 行 + 真的跑了测试**，
    三者缺一都不算绿：只有退出码会漏掉「一条都没跑」，只看 OK 行会漏掉收集阶段的崩溃。"""
    ran = UNIT_RAN.search(text)
    count = int(ran.group(1)) if ran else 0
    failures = [f"{kind}: {name}" for kind, name in UNIT_BAD.findall(text)]
    ok = rc == 0 and UNIT_OK.search(text) is not None and count > 0
    return {"name": "单测", "ok": ok, "count": count, "failures": failures[:20],
            "failure_count": len(failures), "rc": rc, "timed_out": rc == 124}


# ------------------------------------------------------------------ ② 19 套浏览器检查

def expected_suites(tree: pathlib.Path) -> list:
    """套件名单**从运行器里读**，不在这里抄第二份。

    抄一份的代价是：有人加了第 21 个套件、这里还是 20，于是「20/20 全绿」在少跑一个套件时
    照样成立。名单只有一个来源，闸门才拦得住东西。
    """
    script = tree / "tools" / "run_browser_checks.sh"
    text = script.read_text(encoding="utf-8", errors="replace")
    block = re.search(r"declare -a NAMES=\((.*?)\n\)", text, re.S)
    if not block:
        die(f"{script} 里读不到 NAMES 名单 —— 运行器改过形状了，先修 preflight 再来跑。")
    names = re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*$", block.group(1), re.M)
    if not names:
        die(f"{script} 的 NAMES 名单是空的。")
    return names


def playwright_ok(tree: pathlib.Path) -> bool:
    return ((tree / "node_modules" / "playwright").is_dir()
            or pathlib.Path("/tmp/pw/node_modules/playwright").is_dir())


def run_browsers(tree: pathlib.Path, run_dir: pathlib.Path, timeout: int,
                 only: str = "") -> dict:
    logs = run_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log = run_dir / "browser.log"
    cmd = ["bash", "tools/run_browser_checks.sh"] + ([only] if only else [])
    rc, seconds = run_logged(cmd, tree, pass_env({}), log, timeout)
    text = log.read_text(encoding="utf-8", errors="replace")
    result = parse_runner_summary(text, expected_suites(tree), only=only, rc=rc)
    result.update({"seconds": seconds, "log": str(log), "suite_logs": str(logs)})

    # 每个套件的完整日志从 /tmp 抄进运行目录：套件日志本来只在 /tmp，
    # 而这个工具跑在后台/别的机器上时，/tmp 多半已经不是原来那个了。
    copied = []
    for name in result["planned"]:
        source = pathlib.Path(f"/tmp/check-{name}-suite.log")
        if source.exists():
            shutil.copy2(source, logs / f"{name}.log")
            copied.append(name)
    result["copied"] = copied
    return result


def _final_summary_block(text: str) -> str:
    """运行器最后那个汇总块（`passed:` / `FAILED:` 那两行）。

    它在**最后两条分隔线之间**。两个坑都踩过（2026-09-20）：只看「最后一条分隔线之后」
    读到的是收尾那几行说明，一条汇总也没有；按 `═` 子串找位置又会在一行里匹配到好几段
    （那一行有 60 多个字符）。所以按**整行**认。
    """
    lines = text.splitlines()
    marks = [index for index, line in enumerate(lines)
             if line.strip() and set(line.strip()) == {"═"}]
    if len(marks) >= 2:
        return "\n".join(lines[marks[-2]:marks[-1]])
    return text


def parse_runner_summary(text: str, expected: list, *, only: str = "", rc: int = 0) -> dict:
    """`tools/run_browser_checks.sh` 的输出 → 过了哪些、红了哪些、哪些根本没跑到。

    「没跑到」必须单独有一档：运行器被人改短、或者某一套在跑之前就崩了的时候，
    只看「passed 有几条、FAILED 有没有」会得出一个**比事实更绿**的结论。

    只在**最后那个汇总块**里找 `passed:` / `FAILED:`。第一次写的时候是全文搜第一个匹配，
    结果套件失败时运行器会把该套件输出的尾部贴出来 —— 尾部里正好有一行长得像
    `passed: …`，于是「1 套跑红」被读成了「3 套通过」（2026-09-20 用一份故意破坏
    `browser_check` 的补丁喂出来）。
    """
    summary = _final_summary_block(text)

    def listed(pattern: str) -> list:
        match = re.search(pattern, summary, re.M)
        return match.group(1).split() if match else []

    passed = listed(r"^[ \t]*passed:[ \t]*\d+[ \t]*(.*)$")
    failed = listed(r"^[ \t]*FAILED:[ \t]*\d+[ \t]*(.*)$")
    planned = [only] if only else list(expected)
    # 汇总行以外的名字不算数：它们只可能出现套件输出里，不是运行器的结论。
    passed = [name for name in passed if name in planned]
    failed = [name for name in failed if name in planned]
    missing = [name for name in planned if name not in passed and name not in failed]
    # 播种失败是个安静的红：套件可能照样过，但它跑的是「没有数据的界面」，结论不算数。
    # 名字从运行器那一行里取（`播种失败，先看 /tmp/seed-<名字>.log`）—— 别拿套件名去全文里搜，
    # 每个套件的标题行都在同一份输出里，那样一有播种失败就会把 19 个套件全点名。
    seed_failed = re.findall(r"播种失败，先看 /tmp/seed-([A-Za-z0-9_]+)\.log", text)
    return {"name": "浏览器检查",
            "ok": rc == 0 and not failed and not missing and not seed_failed,
            "passed": passed, "failed": failed, "missing": missing,
            "seed_failed": seed_failed, "expected": expected, "planned": planned,
            "rc": rc, "timed_out": rc == 124}


# ------------------------------------------------------------------ 结论

def decide(units: dict, browsers: dict, partial: bool) -> tuple:
    """把两个阶段的结果压成一句话 + 退出码。红的东西必须**点得出名字**。"""
    reds = []
    if not units["ok"]:
        if units["timed_out"]:
            reds.append("单测：超时（没跑完）")
        elif units["count"] == 0:
            reds.append("单测：一条都没跑到（日志尾部见下）")
        else:
            reds.append(f"单测：{units['failure_count']} 条红 —— " + "、".join(units["failures"][:5]))
    if not browsers["ok"]:
        if browsers["timed_out"]:
            reds.append("浏览器检查：超时（没跑完）")
        for name in browsers["failed"]:
            logs = browsers.get("suite_logs") or "（运行目录）"
            reds.append(f"浏览器检查：{name} 红（{logs}/{name}.log）")
        if browsers["missing"]:
            reds.append("浏览器检查：这些套件根本没跑到 —— " + "、".join(browsers["missing"]))
        if browsers["seed_failed"]:
            reds.append("浏览器检查：这些套件的播种失败了（跑的不是有数据的界面）—— "
                        + "、".join(browsers["seed_failed"]))
        if not browsers["failed"] and not browsers["missing"] and browsers["rc"] != 0:
            reds.append(f"浏览器检查：运行器退出码 {browsers['rc']}（见 {browsers['log']}）")
    if reds:
        return "NO-GO ❌", reds, 1
    if partial:
        return "未完成（没跑全）", ["这一遍跳过了部分检查；**它不是 GO**，只够用来快速看一眼。"], 4
    return "GO ✅", [], 0


def failure_tail(name: str, browsers: dict, lines: int = 5) -> list:
    """一个红套件最后那几行（套件自己的 `--- FAILURES ---` 就在那儿）。

    只报套件名等于让人再去开一次日志；那几行里写的才是「哪条断言红了」。
    """
    logs = browsers.get("suite_logs") or ""
    for candidate in (pathlib.Path(logs) / f"{name}.log", pathlib.Path(f"/tmp/check-{name}-suite.log")):
        try:
            if not candidate.exists():
                continue
            text = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        picked = [line.strip() for line in text if line.strip()]
        return picked[-lines:]
    return []


def print_report(ledger: Ledger, units: dict, browsers: dict, verdict: str, reds: list):
    data = ledger.data
    say("")
    rule()
    say(f"  上线前那一遍 · v{data['version']} @ {data['commit']} · {data['kind']}")
    say(f"  树：{data['tree']}")
    if data["patch"]:
        say(f"  补丁：{data['patch']['source']} · sha256 {data['patch']['sha256']}")
    say(f"  记录：{ledger.dir}")
    rule()
    say("")
    if units.get("skipped"):
        say("  ① 单测          跳过（--no-unit）")
    elif units["count"]:
        mark = "✅" if units["ok"] else "❌"
        say(f"  ① 单测          {units['count']} 个，{'全过' if units['ok'] else '有红'}   "
            f"{mark}  ({duration(units['seconds'])})")
    else:
        say(f"  ① 单测          没跑起来   ❌  ({duration(units['seconds'])})")
    for line in units["failures"][:8]:
        say(f"       {line}")
    total = len(browsers["expected"])
    mark = "✅" if browsers["ok"] else "❌"
    say(f"  ② 浏览器检查    {len(browsers['passed'])}/{len(browsers['planned'])} 套件"
        f"（脚本里一共 {total} 个）   {mark}  ({duration(browsers['seconds'])})")
    for name in browsers["failed"]:
        say(f"       ✘ {name}")
        for line in failure_tail(name, browsers):
            say(f"           {line[:120]}")
    for name in browsers["missing"]:
        say(f"       ? {name}（没跑到）")
    say("")
    rule()
    if verdict.startswith("GO"):
        say(f"  结论：{verdict}   —— 这棵树可以进打包 / 上线那一步")
    elif verdict.startswith("未完成"):
        say(f"  结论：{verdict}   —— 不构成上线依据")
    else:
        say(f"  结论：{verdict}   —— 先修上面点名的 {len(reds)} 条，别打包、别部署")
        for line in reds:
            say(f"    · {line}")
    rule()
    say(f"  日志：单测 {units['log']}")
    say(f"        浏览器 {browsers['log']}")
    if browsers["copied"]:
        say(f"        逐套件 {browsers['suite_logs']}/")
    say("")


# ------------------------------------------------------------------ 临时副本（--pr / --patch）

def copy_tree(src: pathlib.Path, dst: pathlib.Path):
    """把工作区拷成一份能跑的副本 —— 排除生成物与本机秘密，再把 .venv-pilot 与
    node_modules 用**符号链接**接过去（几 MB 的拷贝 vs 几百 MB 的复制）。"""
    home = PREFLIGHT_HOME.resolve()

    def ignore(directory, names):
        skipped = set()
        for name in names:
            path = (pathlib.Path(directory) / name).resolve()
            if name in COPY_EXCLUDE or copy_name_is_excluded(name):
                skipped.add(name)
            elif home == path or home in path.parents:
                skipped.add(name)     # 别把运行记录拷进副本里（会自己套自己）
        return skipped

    shutil.copytree(str(src), str(dst), ignore=ignore, symlinks=True)
    for name in (".venv-pilot", "node_modules"):
        origin = ROOT / name
        if origin.exists():
            os.symlink(str(origin), str(dst / name))


def prepare_copy(run_dir: pathlib.Path, *, number: int = 0, patch_file: str = "") -> tuple:
    """取补丁 → 试打 → 真打（全在副本里）。返回 (副本路径, 补丁档案)。"""
    sys.path.insert(0, str(ROOT / "tools"))
    import pr_triage  # 现成的积木：取补丁、试打、要害文件提示（它也是 --rehearse 那条路）

    if number:
        source = f"PR #{number}（{pr_triage.REPO}）"
        patch = pr_triage.fetch_patch(number)
    else:
        source = f"本地补丁 {patch_file}"
        patch = pathlib.Path(patch_file).read_text(encoding="utf-8", errors="replace")
    digest = hashlib.sha256(patch.encode("utf-8")).hexdigest()
    files = pr_triage.patch_files(patch)

    say(f"  补丁：{source}")
    say(f"        sha256 {digest[:16]}… · 动了 {len(files)} 个文件")
    for name in files[:20]:
        say(f"          {name}")
    if len(files) > 20:
        say(f"          …还有 {len(files) - 20} 个")
    hits = pr_triage.sensitive_hits(files)
    if hits:
        say(f"        ⚠ 碰到了要害文件：{', '.join(hits)}")
        say("          —— 这一遍会**执行补丁里的代码**。对没读过的补丁，先 `--diff N` 看原文。")

    copy = run_dir / "tree"
    say(f"  临时副本：{copy}（工作区不会被碰）")
    copy_tree(ROOT, copy)

    ok, output = pr_triage.preview_apply(patch, copy)
    if not ok:
        die("补丁**打不上**这份副本，停在这里。这通常是补丁基于的版本太旧，不是产品坏了。\n"
            f"     {output.strip()[:600]}\n"
            f"     （工作区一个字节没动；副本留在 {copy}）", 3)

    archive = run_dir / f"patch{number or ''}.diff"
    archive.write_text(patch, encoding="utf-8")
    applied = subprocess.run(["patch", "-p1", "--forward", "--input", str(archive)],
                             cwd=str(copy), capture_output=True, text=True)
    if applied.returncode != 0:
        die(f"补丁试打通过、真打却失败了：\n     {(applied.stdout + applied.stderr).strip()[:600]}", 3)
    say("  ✅ 补丁打上了（只在这份副本里）")
    return copy, {"source": source, "sha256": digest, "files": files,
                  "archive": str(archive), "sensitive": hits}


# ------------------------------------------------------------------ 一轮预检（默认动作）

def mode_run(args) -> int:
    PREFLIGHT_HOME.mkdir(parents=True, exist_ok=True)
    lock_path = PREFLIGHT_HOME / "lock"
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            die(f"已经有一轮预检在跑（{lock_path} 被锁着）。\n"
                "     两个运行器会抢 /tmp/check-*.sqlite3 那几个文件名，所以这里只许一个。", 3)

        stamp = dt.datetime.now().strftime("%Y-%m-%dT%H%M%S")
        label = f"-pr{args.pr}" if args.pr else ("-patch" if args.patch else "")
        run_dir = PREFLIGHT_HOME / "runs" / f"{stamp}{label}"
        run_dir.mkdir(parents=True)
        ledger = Ledger(run_dir)

        say("")
        rule("上线前那一遍")
        say(f"  工作区：{ROOT}  v{ledger.data['version']} @ {ledger.data['commit']}")
        say(f"  记录：{run_dir}")
        rule()

        tree = ROOT
        if args.pr or args.patch:
            tree, patch_info = prepare_copy(run_dir, number=args.pr, patch_file=args.patch or "")
            ledger.data["tree"] = str(tree)
            ledger.data["kind"] = "PR 副本" if args.pr else "本地补丁副本"
            ledger.data["patch"] = patch_info

        partial = bool(args.no_browser or args.no_unit or args.only)

        if args.no_unit:
            say("\n  ① 单测……（--no-unit：跳过）")
            units = {"name": "单测", "ok": True, "count": 0, "failures": [], "failure_count": 0,
                     "rc": 0, "seconds": 0.0, "log": "", "timed_out": False, "skipped": True}
        else:
            if args.no_browser:
                say("\n  ① 单测……（--no-browser：这一遍不跑浏览器检查）")
            else:
                say("\n  ① 单测……")
            units = run_units(tree, run_dir, args.timeout)
            say(f"     → {units['count']} 个测试，{'全过' if units['ok'] else '有红'}"
                f"（{duration(units['seconds'])}）")
        ledger.phase(units)

        if args.no_browser:
            browsers = {"name": "浏览器检查", "ok": True, "passed": [], "failed": [],
                        "missing": [], "seed_failed": [], "expected": expected_suites(tree),
                        "planned": [], "rc": 0, "seconds": 0.0, "log": "", "suite_logs": "",
                        "copied": [], "timed_out": False, "skipped": True}
        else:
            if not playwright_ok(tree):
                die("Playwright 没装（node_modules/playwright 与 /tmp/pw 都没有）。\n"
                    "     这不是产品的问题，是工装：\n"
                    "       npm install --no-save playwright@1.63.0 && npx playwright install chromium", 3)
            what = f"只跑 {args.only}" if args.only else "19 套全跑"
            say(f"\n  ② 浏览器检查……（{what}，每套一个干净库和端口）")
            browsers = run_browsers(tree, run_dir, args.timeout, only=args.only or "")
            ledger.phase(browsers)
            say(f"     → {len(browsers['passed'])} 过 / {len(browsers['failed'])} 红"
                f"（{duration(browsers['seconds'])}）")

        verdict, reds, code = decide(units, browsers, partial)

        # 「不改工作区」不是一句口号：跑完把工作区的状态再量一遍，和开工前逐字比。
        after = git_line(["status", "--porcelain"]) if ledger.data["dirty_before"] is not None else None
        ledger.data["dirty_after"] = after
        if ledger.data["kind"] != "workspace":
            same = (after == ledger.data["dirty_before"])
            say("")
            say(f"  工作区核对：{'✅ 一个字节没动' if same else '❌ 和开工前不一样了（这不该发生）'}"
                f"（未提交处 {len(after.splitlines()) if after else 0} 个）")
            if not same:
                reds.append("工作区在跑的过程中被改了 —— 这违反 --pr 的前提，别信这一轮的结论")
                verdict, code = "NO-GO ❌", 1

        print_report(ledger, units, browsers, verdict, reds)
        path = ledger.write(verdict, code)
        say(f"  台账：{path}/summary.json")
        return code


# ------------------------------------------------------------------ ③ 预发站点

def _assert_safe_home():
    """预发站点只许把库放在临时目录里。这一条是**底线 1** 的代码化：
    路径长得像生产（/etc、/opt、/var/lib…）就直接停，不给「手滑」留机会。"""
    # 两种写法都查：**原样**与 `resolve()` 之后。macOS 上 `/etc` 与 `/var` 是符号链接
    # （解析成 `/private/etc`、`/private/var`），只看解析后的路径会让 `/etc/…` 这种
    # **一眼就是放真东西的地方**漏过去——而它正是这条底线要挡的（本文件不许写出
    # 那种路径的字面量，它自己有一条测试盯着）。
    candidates = {str(PREFLIGHT_HOME), str(PREFLIGHT_HOME.resolve())}
    for bad in ("/etc", "/opt", "/var/lib", "/srv", "/root", "/usr"):
        for text in sorted(candidates):
            if text == bad or text.startswith(bad + "/"):
                die(f"PREFLIGHT_HOME 指到了 {text} —— 那是放真东西的地方，"
                    "预发站点不许用它。", 3)


def staging_paths() -> dict:
    base = PREFLIGHT_HOME / "staging"
    return {"base": base, "descriptor": PREFLIGHT_HOME / "staging.json",
            "pid": base / "server.pid", "log": base / "server.log"}


def staging_status() -> dict:
    paths = staging_paths()
    if not paths["descriptor"].exists():
        return {}
    try:
        data = json.loads(paths["descriptor"].read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    pid = int(data.get("pid") or 0)
    # 判「还开着」看的是**端口上的 /health**，不是 pid：pid 会被系统回收，
    # 一个过期的台账说「pid 12345 活着」，那个 12345 可能早就是别人的进程了。
    data["ours"] = pid_is_our_web(pid)
    data["healthy"] = http_ok(f"http://127.0.0.1:{data['port']}/health")
    data["alive"] = data["healthy"] and data["ours"]
    return data


def pid_is_our_web(pid: int) -> bool:
    """这个 pid 现在还是不是我们的 web 进程？

    有这一问是因为 `--stop` 要发信号：只凭 pidfile 里那个数字就 kill，pid 被回收之后
    杀的就是别人的进程了。cmdline 对不上（或读不到）就当它不是 —— 少杀一个，
    比杀错一个好。
    """
    if pid <= 0:
        return False
    try:
        raw = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        raw = b""
    if raw:
        return b"pilot_app.web" in raw
    try:
        result = subprocess.run(["ps", "-o", "command=", "-p", str(pid)],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "pilot_app.web" in result.stdout


def _staging_card(descriptor: dict, *, remote_home: str = "", host: str = "dorm"):
    port = descriptor["port"]
    say("")
    rule("预发站点开着")
    say(f"    网址（在那台上）  http://127.0.0.1:{port}/app")
    say(f"    账号              {descriptor['email']}")
    say(f"    密码              {descriptor['password']}")
    say(f"    库                {descriptor['db']}（{descriptor['reports']} 封报告"
        f"{'，含后台夹具' if descriptor.get('fixtures') else ''}）")
    smoke = descriptor.get("smoke") or {}
    if smoke:
        bits = []
        for name, ok in smoke.items():
            bits.append(f"{name} {'✅' if ok else '❌'}")
        say(f"    自检              {'  '.join(bits)}")
    say("")
    say("  在 Mac 上打开（一条命令；走已有的反向隧道做端口转发）。")
    say("  它自带一次自检：curl 拿不到 /health 就不会去开浏览器（隧道没通时你看得见）：")
    say(f"    ssh -f -N -L {port}:127.0.0.1:{port} {host} \\")
    say(f"      && curl -fsS http://127.0.0.1:{port}/health \\")
    say(f"      && open http://127.0.0.1:{port}/app")
    say(f"  （把本仓库在 Mac 上更新之后，也可以直接："
        f".venv-pilot/bin/python tools/preflight.py --open）")
    say(f"  关隧道：pkill -f 'ssh -f -N -L {port}:127.0.0.1:{port}'")
    say("")
    say("  这一台只跑 web 进程：**不发信、不部署、不碰生产库**（会发信的是 worker，没起）。")
    say("  关掉：.venv-pilot/bin/python tools/preflight.py --serve --stop")
    rule()


def _wait_health(base: str, seconds: float = 30.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if http_ok(f"{base}/health", timeout=1.0):
            return True
        time.sleep(0.25)
    return False


def _seed_staging(tree: pathlib.Path, db: pathlib.Path, base: str, reports: int,
                  fixtures: bool, run_dir: pathlib.Path) -> bool:
    python = tree / ".venv-pilot" / "bin" / "python"
    env = pass_env({"INFE_PILOT_PREVIEW": "1", "INFE_PILOT_MASTER_KEY": FAKE_MASTER})
    cmd = [str(python), "tools/seed_preview.py", str(db), "--base", base,
           "--reports", str(reports)]
    if fixtures:
        cmd.append("--admin-fixtures")
    rc, _ = run_logged(cmd, tree, env, run_dir / "seed.log", 300)
    if rc != 0:
        say("    ⚠ 播种失败，先看 " + str(run_dir / "seed.log"))
        say(tail(run_dir / "seed.log", 6))
        return False
    return True


def _smoke_staging(tree: pathlib.Path, base: str, run_dir: pathlib.Path) -> dict:
    """拿**现成的两套浏览器检查**当预发站点的自检：真的开浏览器、真的登录、真的点一遍。"""
    results = {}
    shots = run_dir / "shots"
    for name in STAGING_SMOKE:
        script = tree / "tools" / f"{name}.js"
        if not script.exists():
            results[name] = False
            continue
        rc, _ = run_logged(["node", str(script), base, str(shots / name)], tree,
                           pass_env({"PILOT_ADMIN": ADMIN_EMAIL, "PILOT_PASSWORD": ADMIN_PASSWORD}),
                           run_dir / f"smoke-{name}.log", 600)
        results[name] = (rc == 0)
        say(f"    自检 {name}: {'✅ 过' if rc == 0 else '❌ 红（' + str(run_dir / f'smoke-{name}.log') + '）'}")
    return results


def mode_serve(args) -> int:
    _assert_safe_home()
    paths = staging_paths()
    current = staging_status()

    if args.stop:
        if not current:
            say("预发站点没在跑（没有台账）。")
            return 0
        pid = int(current.get("pid") or 0)
        if current.get("ours"):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            for _ in range(40):
                if not http_ok(f"http://127.0.0.1:{current['port']}/health", timeout=0.5):
                    break
                time.sleep(0.25)
            say(f"预发站点已停（pid {pid}）。库还留着：{current['db']}")
        elif current.get("healthy"):
            say(f"端口 {current['port']} 上确实有服务，但台账里的 pid {pid} 不是我们的 web 进程"
                f"（pid 会被回收）—— **没有动它**。要停就自己认一下那个进程。")
        else:
            say(f"预发站点没在跑（台账里的 pid {pid} 已经不是我们的进程了）。")
        paths["pid"].unlink(missing_ok=True)
        paths["descriptor"].unlink(missing_ok=True)
        return 0

    if args.status:
        if not current:
            say("预发站点没在跑。起来：.venv-pilot/bin/python tools/preflight.py --serve")
            return 3
        state = "开着" if current.get("healthy") else "**/health 不通**"
        say(f"预发站点{state}：http://127.0.0.1:{current['port']}/app"
            f" · {current['email']} / {current['password']}")
        if current.get("healthy") and not current.get("ours"):
            say(f"  （台账里的 pid {current['pid']} 对不上这个服务 —— 它可能是上一次跑的，"
                f"不影响你用；--stop 不会去动它。）")
        return 0 if current.get("healthy") else 3

    if args.detach and not args._child:
        # 后台起来：父进程只等台账出现，然后把卡片打给人看。setsid 让它在 Mac 合盖/断线后还活着。
        paths["base"].mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, str(pathlib.Path(__file__).resolve()), "--serve", "--_child",
               "--port", str(args.port or DEFAULT_STAGING_PORT), "--reports", str(args.reports)]
        if args.keep:
            cmd.append("--keep")
        if args.plain:
            cmd.append("--plain")
        if args.no_smoke:
            cmd.append("--no-smoke")
        with open(paths["log"], "ab") as log:
            subprocess.Popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                             start_new_session=True, cwd=str(ROOT))
        for _ in range(240):
            time.sleep(0.5)
            data = staging_status()
            if data.get("healthy"):
                _staging_card(data, remote_home=str(PREFLIGHT_HOME))
                return 0
        die(f"预发站点没起来（等着台账出现超时）。日志：{paths['log']}\n     {tail(paths['log'], 8)}", 3)

    if current.get("healthy"):
        say(f"已经有一个预发站点在跑：http://127.0.0.1:{current['port']}/app"
            f"（先 --serve --stop，或直接用 --status 看）")
        return 3

    # 从这一行往下 = 真正把站点拉起来的那一半（前台；--detach 时跑在 setsid 出来的子进程里）。
    PREFLIGHT_HOME.mkdir(parents=True, exist_ok=True)
    paths["base"].mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y-%m-%dT%H%M%S")
    run_dir = PREFLIGHT_HOME / "staging" / f"run-{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    port = args.port or DEFAULT_STAGING_PORT
    if not port_free(port):
        fallback = free_port()
        say(f"  端口 {port} 被占着，改用 {fallback}")
        port = fallback
    base = f"http://127.0.0.1:{port}"
    db = paths["base"] / "preview.sqlite3"
    if args.keep and db.exists():
        say(f"  沿用上次的库：{db}（--keep）")
    else:
        for suffix in ("", "-shm", "-wal"):
            pathlib.Path(str(db) + suffix).unlink(missing_ok=True)

    python = ROOT / ".venv-pilot" / "bin" / "python"
    env = pass_env({**STAGING_ENV, "INFE_PILOT_DB": str(db)})
    server_log = open(paths["log"], "ab")
    say(f"  起 web：{base}（库 {db}）")
    server = subprocess.Popen([str(python), "-m", "pilot_app.web", "--host", "127.0.0.1",
                               "--port", str(port)], cwd=str(ROOT), env=env,
                              stdout=server_log, stderr=subprocess.STDOUT, start_new_session=True)
    _RUNNING.append(server)
    paths["pid"].write_text(str(server.pid), encoding="utf-8")

    try:
        if not _wait_health(base):
            die(f"web 起来了但 /health 不通，先看 {paths['log']}：\n     {tail(paths['log'], 8)}", 3)
        say("  ✅ /health 通了")
        seeded = _seed_staging(ROOT, db, base, args.reports, not args.plain, run_dir)
        smoke = {} if (args.no_smoke or not seeded) else _smoke_staging(ROOT, base, run_dir)

        descriptor = {"url": base, "app": f"{base}/app", "port": port,
                      "host": "127.0.0.1", "pid": server.pid, "db": str(db),
                      "email": ADMIN_EMAIL, "password": ADMIN_PASSWORD,
                      "reports": args.reports, "fixtures": not args.plain,
                      "started_at": now_iso(), "version": version_of(ROOT),
                      "commit": git_line(["rev-parse", "--short", "HEAD"]) or "",
                      "smoke": smoke, "log": str(paths["log"]), "run": str(run_dir)}
        paths["descriptor"].write_text(json.dumps(descriptor, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
        _staging_card(descriptor, remote_home=str(PREFLIGHT_HOME))
        if smoke and not all(smoke.values()):
            say("  ⚠ 自检有红：站点还能点，但那两套检查没过 —— 先看上面的日志再给人看。")
        if not args._child:
            say("  （前台运行：这个终端要一直开着。要它自己活下来就用 --detach。Ctrl-C 关掉。）")
            while True:
                time.sleep(1.0)
                if server.poll() is not None:
                    say(f"  web 进程退出了（rc={server.returncode}），日志：{paths['log']}")
                    return 3
        else:
            server.wait()
        return 0
    except KeyboardInterrupt:
        say("\n  收到 Ctrl-C，关掉 web 进程。")
        return 0
    finally:
        if server.poll() is None:
            try:
                os.killpg(server.pid, signal.SIGTERM)
            except OSError:
                pass
            server.wait(timeout=10)
        server_log.close()


# ------------------------------------------------------------------ 【Mac 侧】开隧道 + 打开浏览器

def mode_open(args) -> int:
    host = args.host or os.environ.get("DORM_HOST") or "dorm"
    remote_home = args.remote_home or os.environ.get("PREFLIGHT_HOME") or "/tmp/preflight"
    remote = f"{remote_home}/staging.json"

    say(f"  问 {host}：预发站点开着吗（{remote}）")
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host,
                             f"cat {shlex.quote(remote)}"],
                            capture_output=True, text=True)
    if result.returncode != 0:
        die(f"读不到那台的预发台账（ssh {host} 退出码 {result.returncode}）。\n"
            f"     要么隧道没开（在那台上跑 `dorm status` 看），要么预发站点没起：\n"
            f"       在宿舍机：.venv-pilot/bin/python tools/preflight.py --serve\n"
            f"     {(result.stderr or '').strip()[:200]}", 3)
    try:
        data = json.loads(result.stdout)
    except ValueError:
        die(f"{host}:{remote} 不是一份台账（站点可能正在起）。原文：{result.stdout[:200]}", 3)

    port = int(data["port"])
    local_port = args.local_port or port
    target = f"http://127.0.0.1:{local_port}/app"

    if http_ok(f"http://127.0.0.1:{local_port}/health", timeout=1.5):
        say(f"  隧道已经通了（本地 {local_port} → {host}:{port}）")
    else:
        if not port_free(local_port):
            local_port = free_port()
            target = f"http://127.0.0.1:{local_port}/app"
            say(f"  本地 {args.local_port or port} 被别的进程占着，改用 {local_port}")
        say(f"  开隧道：本地 {local_port} → {host}:127.0.0.1:{port}")
        tunnel = subprocess.run(["ssh", "-f", "-N", "-L",
                                 f"{local_port}:127.0.0.1:{port}", host],
                                capture_output=True, text=True)
        if tunnel.returncode != 0:
            die(f"隧道没开起来：{(tunnel.stderr or '').strip()[:300]}", 3)
        if not _wait_health(f"http://127.0.0.1:{local_port}", seconds=15):
            die("隧道开了，但那边 /health 不通 —— 预发站点的 web 进程可能已经退出。\n"
                "     在那台上看：tools/preflight.py --serve --status", 3)

    say("")
    rule("预发站点（隧道已通）")
    say(f"    {target}")
    say(f"    账号  {data['email']}    密码  {data['password']}")
    say(f"    （那台上的库：{data['db']}）")
    rule()
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    if shutil.which(opener):
        subprocess.run([opener, target], check=False)
        say(f"  已经让浏览器打开 {target}")
    else:
        say(f"  这个系统上没有 {opener}，自己打开：{target}")
    say(f"  关隧道：pkill -f 'ssh -f -N -L {local_port}:127.0.0.1:{port}'")
    return 0


# ------------------------------------------------------------------ CLI

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="上线前那一遍：全量单测 + 19 套浏览器检查 → GO / NO-GO；也能起预发站点。",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    parser.add_argument("--pr", type=int, metavar="N",
                        help="把第 N 条 PR 的补丁打进临时副本，在副本里跑（不动工作区）")
    parser.add_argument("--patch", metavar="FILE", help="同上，但补丁来自本地文件")
    parser.add_argument("--only", metavar="SUITE", help="只跑某一套浏览器检查（结果不算 GO）")
    parser.add_argument("--no-browser", action="store_true", help="跳过浏览器检查（结果不算 GO）")
    parser.add_argument("--no-unit", action="store_true", help="跳过单测（结果不算 GO）")
    parser.add_argument("--timeout", type=int, default=5400, help="每个阶段的秒数上限（默认 5400）")
    parser.add_argument("--serve", action="store_true", help="起预发站点（自己的库、自己的端口）")
    parser.add_argument("--detach", action="store_true", help="配 --serve：后台跑，本命令立刻返回")
    parser.add_argument("--stop", action="store_true", help="配 --serve：关掉预发站点")
    parser.add_argument("--status", action="store_true", help="配 --serve：看预发站点还在不在")
    parser.add_argument("--port", type=int, help=f"预发站点端口（默认 {DEFAULT_STAGING_PORT}）")
    parser.add_argument("--reports", type=int, default=8, help="预发站点造几封报告（默认 8）")
    parser.add_argument("--plain", action="store_true", help="预发站点不造后台夹具")
    parser.add_argument("--no-smoke", action="store_true", help="预发站点起来后不自检")
    parser.add_argument("--keep", action="store_true", help="预发站点沿用上次的库")
    parser.add_argument("--open", action="store_true", help="【在 Mac 上跑】开隧道并打开浏览器")
    parser.add_argument("--host", help="配 --open：ssh 到哪台（默认 dorm，或 DORM_HOST）")
    parser.add_argument("--remote-home", help="配 --open：那台的 PREFLIGHT_HOME（默认 /tmp/preflight）")
    parser.add_argument("--local-port", type=int, help="配 --open：本地监听哪个端口")
    parser.add_argument("--_child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    try:
        if args.open:
            return mode_open(args)
        if args.serve or args.stop or args.status:
            args.serve = True
            return mode_serve(args)
        # 这几条在动手之前就拦掉：跑了一半才发现参数自相矛盾，等于白等十分钟。
        if args.no_unit and args.no_browser:
            die("--no-unit 与 --no-browser 不能一起给 —— 两半都不跑就没有「预检」这回事了。", 3)
        if args.no_browser and args.only:
            die("--no-browser 与 --only 不能一起给（--only 说的就是跑哪一套浏览器检查）。", 3)
        return mode_run(args)
    except KeyboardInterrupt:
        say("\n  收到 Ctrl-C。")
        return 130
    finally:
        for proc in list(_RUNNING):
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
