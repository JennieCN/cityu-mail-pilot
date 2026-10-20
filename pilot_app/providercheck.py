"""问服务商一句：**你还让人用授权码登录吗？**

## 为什么要有这个文件

2026-09-16 的「outlook 那件事」：微软个人版把基础认证关了，用户按向导拿到授权码、
填进来、永远是登录被拒 —— 而**我们这边一切正常**，灯是红的、信也发了，可谁都说不清
「到底是他的码不对，还是这条路已经没了」。事后我们按主机名与错误文本判定了它，并在
设置向导里给了一条出路（换成 QQ/163/Gmail）。

那是**用户先撞上、我们后知道**。这个模块把顺序倒过来：定期去问每一家
「你现在还提供密码登录吗」，**在用户之前**发现门关了。

## 怎么问：不发凭据，只看服务器自己怎么说

连上 IMAP 的 993，读它握手时的 `CAPABILITY`，以及登录失败时的措辞。被判定的只有三种：

``password_ok``
    服务器**提供**用密码（授权码）登录这条路 —— 广播了 ``AUTH=PLAIN``/``AUTH=LOGIN``，
    或者至少**没有**把 ``LOGINDISABLED`` 挂出来。
``oauth_only``
    明确不给：``LOGINDISABLED`` 且只广播 OAuth 机制。**outlook.office365.com 现在就是这样**，
    这条判定直接由真机复现（见 tests/test_providercheck.py 里那串原始 CAPABILITY）。
``unreachable``
    连不上、超时、证书坏了。**不算「门关了」** —— 网络抖动与政策变更必须分开，
    否则一次网络故障就会让人去改一个没坏的东西。

**「没广播 AUTH=PLAIN」不等于不能用密码**：163 的 CAPABILITY 里就没有 PLAIN，
可真机实测用假凭据登录，它照样进入「验密码」这一步（返回 `Login error or password error`）。
所以判定看的是 ``LOGINDISABLED`` 这个**明确的拒绝**，而不是「没提到」——
把「没提到」当拒绝，会把一家好好的服务商误判成死路。

## 判定与期望分开

``EXPECTED`` 说每种预设**应该**是什么样：被封禁的那一家应当是 ``oauth_only``，
其余应当是 ``password_ok``。两边一比就是**漂移**：

* 没封禁的变成了 ``oauth_only`` → **下一次「outlook 的事」**，要马上告诉运营者；
* 封禁的又变回 ``password_ok`` → 微软把门打开了，我们可以把那条封禁撤掉；
* 连不上 → 先记着，不据此下任何结论。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import socket
import ssl
from typing import Any, Callable

from . import mailpresets

PASSWORD_OK = "password_ok"
OAUTH_ONLY = "oauth_only"
UNREACHABLE = "unreachable"

PROBE_TIMEOUT = 12
STAMP_KEY = "provider_check"          # app_settings 里那一行（含时间戳与每家结果）
STAMP_MAX_AGE = dt.timedelta(hours=36)   # 超过它就说明「检查本身没在跑」
REFRESH_AFTER = dt.timedelta(hours=24)   # 多久重跑一次真探测


def classify(capability: str) -> str:
    """从一行 CAPABILITY / 打招呼文本判定「还给不给用密码登录」。

    纯函数：测试把真机抓下来的那几行原样喂进来。
    """
    text = str(capability or "").upper()
    if "LOGINDISABLED" in text:
        return OAUTH_ONLY
    if "AUTH=PLAIN" in text or "AUTH=LOGIN" in text or "AUTH=PLAIN-CLIENTTOKEN" in text:
        return PASSWORD_OK
    # 既没明说禁止、也没广播任何密码机制：**不猜**。163 就是这种形状
    # （CAPABILITY 里只有 AUTH=XOAUTH2，可真机用密码登录它是正常受理的）。
    return PASSWORD_OK


def probe_imap(host: str, port: int = 993, timeout: int = PROBE_TIMEOUT) -> tuple[str, str]:
    """连一次 IMAP，读握手与 CAPABILITY。**不发任何凭据。**

    返回 ``(判定, 原始那一行)`` —— 原始那行要留着，运营者据此判断我们是不是误判。
    """
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as raw:
            with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as tls:
                greeting = tls.recv(4096).decode("utf-8", "replace")
                tls.sendall(b"a1 CAPABILITY\r\n")
                reply = tls.recv(8192).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - 任何失败都只是「这次没问到」
        return UNREACHABLE, f"{type(exc).__name__}: {exc}"[:200]
    combined = " ".join(line.strip() for line in (greeting + reply).splitlines())
    return classify(combined), combined[:400]


def _targets() -> list[dict[str, Any]]:
    """每种预设探测一次。被封禁的也要探 —— 「它是不是真的还不给用」本身就是信息。"""
    seen: list[dict[str, Any]] = []
    # 走公开的那一份（`public_mailbox_help`）而不是内部表：字段与前端看到的一致，
    # 也就不会出现「面板上说支持、探测器看的却是另一份数据」。
    for item in mailpresets.public_mailbox_help()["presets"]:
        host = str(item.get("imap_host") or "").strip()
        if not host:
            continue          # 「其它邮箱」没有固定主机，探不了
        seen.append({"id": item["id"], "label": item.get("short_label") or item["label"],
                     "host": host, "port": int(item.get("imap_port") or 993),
                     "blocked": bool(item.get("blocked_reason"))})
    return seen


def expected_state(blocked: bool) -> str:
    """被封禁的**应该**是 ``oauth_only``；其余**应该**还能用密码。"""
    return OAUTH_ONLY if blocked else PASSWORD_OK


def check_all(probe: Callable[[str, int], tuple[str, str]] = probe_imap) -> list[dict[str, Any]]:
    """逐家探一遍。``probe`` 可注入，测试因此不碰网络。"""
    results = []
    for target in _targets():
        state, raw = probe(target["host"], target["port"])
        results.append({**target, "state": state, "raw": raw,
                        "expected": expected_state(target["blocked"])})
    return results


def drift(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只看**该报警**的差别（连不上不算，网络抖动与政策变更要分开）。"""
    out = []
    for item in results:
        if item["state"] == UNREACHABLE:
            continue
        if item["state"] != item["expected"]:
            out.append(item)
    return out


def summarize(results: list[dict[str, Any]]) -> str:
    """给面板/日志看的一行行摘要（不含任何凭据，也就没有可泄露的东西）。"""
    lines = []
    for item in results:
        mark = {PASSWORD_OK: "✓ 可用", OAUTH_ONLY: "✗ 只能用 OAuth",
                UNREACHABLE: "? 连不上"}.get(item["state"], item["state"])
        flag = "" if item["state"] in (UNREACHABLE, item["expected"]) else "  ← 和预期不一样"
        lines.append(f"{item['label']}（{item['host']}）：{mark}{flag}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 记录：哨兵每 5 分钟跑一次，但**不能**每 5 分钟去连五家邮箱
# ---------------------------------------------------------------------------
def save(db: Any, results: list[dict[str, Any]], *, when: dt.datetime | None = None) -> None:
    stamp = (when or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds")
    db.set_setting(STAMP_KEY, json.dumps({"at": stamp, "results": results}, ensure_ascii=False),
                   actor="provider-check")


def load(db: Any) -> dict[str, Any] | None:
    raw = db.get_setting(STAMP_KEY, "")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logging.warning("provider_check 那一行读不出来，当作没跑过")
        return None
    return data if isinstance(data, dict) else None


def age(db: Any, *, now: dt.datetime | None = None) -> dt.timedelta | None:
    data = load(db)
    if not data or not data.get("at"):
        return None
    try:
        when = dt.datetime.fromisoformat(str(data["at"]))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return (now or dt.datetime.now(dt.timezone.utc)) - when


def in_use(rows: list[dict[str, Any]] | None) -> bool:
    """这台服务器上**有没有人在用**：至少一个账号配好了私人邮箱。

    探针只有在「有人会被这件事影响」时才值得报。开发机、预览库、刚装好还没人注册的实例
    都不该在面板上挂一条「服务商检查没跑」——那是教人忽略告警。
    """
    return any(str((row or {}).get("mailbox_email") or "").strip() for row in (rows or []))


def findings(db: Any, *, now: dt.datetime | None = None,
             rows: list[dict[str, Any]] | None = None) -> list[dict[str, str]]:
    """哨兵用的纯读取版本：**只读我们上次存下来的结果**，不碰网络。

    这样 `alerting.evaluate()` 仍然是确定性的、可注入的（它的所有测试都依赖这一点）。
    ``rows`` 由调用方传进来（`evaluate` 本来就已经查过一遍账号列表），省一次查询也保证
    两处看的是同一份数据。
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    if rows is None:
        rows = db.list_users_overview()
    data = load(db)
    out: list[dict[str, str]] = []
    # 没有那条记录 = **从来没跑成过**，这本身要报（否则探针悄悄不跑了，谁都不知道）。
    # 全新安装上它不会误报：worker 启动后的第一次哨兵就会先探一次再评估（见
    # `worker` 里 `refresh_if_due` 与 `run_checks` 的先后）。
    for item in drift([r for r in (data or {}).get("results", []) if isinstance(r, dict)]):
        if item.get("state") == OAUTH_ONLY:
            out.append({"key": f"provider_password_auth:{item['id']}",
                        "severity": "warning",
                        "title": f"「{item.get('label')}」不再允许用授权码登录",
                        "detail": (f"{item.get('host')} 现在只提供 OAuth（LOGINDISABLED）。"
                                   f"按 /app 设置向导里那种说法，用户拿到的授权码永远不会被接受——"
                                   f"要么把它标成「用不了」（像 outlook 那样），要么找一条 OAuth 的路。")})
        else:
            out.append({"key": f"provider_password_auth_back:{item['id']}",
                        "severity": "info",
                        "title": f"「{item.get('label')}」又可以只用密码登录了",
                        "detail": (f"{item.get('host')} 重新广播了密码登录。"
                                   f"如果这条封禁是我们加的，可以撤掉。")})
    stamp_time = _stamp_time(data) if data else None
    stale_seconds = (now - stamp_time).total_seconds() if stamp_time else None
    # 只有在真的有人用时才报「检查没跑」：没人用的实例上它只是一句噪音。
    if in_use(rows) and (stale_seconds is None or stale_seconds > STAMP_MAX_AGE.total_seconds()):
        when = "从没跑过" if stale_seconds is None else f"上次是 {int(stale_seconds // 3600)} 小时前"
        out.append({"key": "provider_check_stale", "severity": "warning",
                    "title": "服务商授权码检查没在跑",
                    "detail": (f"{when}。这一项是拿来提前发现「某家邮箱不再支持授权码」的"
                               f"（outlook 那件事就是用户先撞上的）。在服务器上跑："
                               f"sudo systemd-run --uid=cityumail "
                               f"--property=EnvironmentFile=/etc/cityu-mail-pilot/pilot.env "
                               f"--working-directory=/opt/cityu-mail-pilot --pipe --wait --collect "
                               f"/opt/cityu-mail-pilot/.venv/bin/python -m pilot_app.manage check-providers")})
    return out


def _stamp_time(data: dict[str, Any]) -> dt.datetime | None:
    try:
        when = dt.datetime.fromisoformat(str(data.get("at")))
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=dt.timezone.utc)


def refresh_if_due(db: Any, *, now: dt.datetime | None = None,
                   probe: Callable[[str, int], tuple[str, str]] = probe_imap,
                   force: bool = False) -> dict[str, Any] | None:
    """超过一天就真探一次并存下来；否则什么都不做（返回 None）。

    由 worker 的哨兵循环顺手调用 —— 一天十来个连接，任何一家服务商都不会介意。
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    last = age(db, now=now)
    if not force and last is not None and last < REFRESH_AFTER:
        return None
    results = check_all(probe)
    save(db, results, when=now)
    bad = drift(results)
    if bad:
        logging.warning("服务商授权码检查发现 %d 处变化：%s", len(bad),
                        "；".join(f"{item['id']}={item['state']}" for item in bad))
    else:
        logging.info("服务商授权码检查通过：%d 家全部符合预期", len(results))
    return {"results": results, "drift": bad, "at": now.isoformat(timespec="seconds")}
