"""逐个问一个**已配置的转发邮箱**：你这个授权码现在到底还能不能用？

## 为什么要有这个文件

2026-09-18 到 09-20 这三天里出现了三次「授权码用不了」，而它们是**三件不同的事**：

① **一个 126 的账号**：**我们的预置把 126 的地址填成了 `imap.163.com`**（网易一个
   域名一台机器）。服务器回的是通用的「密码错误」，我们却把它翻译成「授权码不对」，
   于是用户一遍遍重新生成一串**本来就是对的**授权码。已修（v0.63.99）。
② **两个 163 的账号**：主机是对的，**163 真的拒了那两串授权码**
   —— 这一档只能由用户自己到网页版重新生成。
③ 微软系（Outlook / Hotmail / Live）：**永远不可能**用授权码（只给 OAuth）——这一档
   要劝用户换一个邮箱，而不是让他重试。

把这三件事混成一句「授权码用不了」，就会一直修不好：① 被当成 ②，用户白折腾；
③ 被当成 ②，用户死在一条本来就没有出口的路上。所以这个模块只做一件事：**分档**，
而且每一档都说得清「下一步该谁做什么」。

它和 `providercheck.py` 是一对，问的不是同一个问题：

* `providercheck` 问**服务商**「你还让不让人用授权码」——不发凭据、按家问；
* `mailboxcheck` 问**一个具体的邮箱**「你存着的这串授权码现在还能不能进去」——
  用真的凭据、按人问。

## 探针只读

`ID`（RFC 2971）→ `LOGIN` → `EXAMINE INBOX` → `LOGOUT`。`EXAMINE` 在 IMAP 里本身就是
**只读**打开（区别于 `SELECT`），全程**不取任何一封信、不 STORE、不 DELETE**。

`ID` 必须发在 `LOGIN` **之前**：163/126 不给客户端发 `ID` 就直接拒开箱
（`NO [EXAMINE Unsafe Login. Please contact kefu@188.com for help]`），这条路
2026-09-18 已经踩过一次（见 `docs/imap-id-163-2026-09-18.md`）。

## 打码与秘密

本模块**只处理**地址、主机、判定和服务器原话；它把授权码当作一个只进不出的参数。
返回的每一行里都没有授权码（`test_mailbox_check` 有一条测试专门盯着这一点），
真正的打印擦洗在 `manage.check_mailboxes` 里再做一遍。
"""

from __future__ import annotations

import imaplib
import ssl
from typing import Any, Callable, Iterable

from . import mailio, mailpresets

#: 一次探测最多等多久。163/126 在跨境线路上偶尔要几秒才回问候语。
PROBE_TIMEOUT = 15

# ---------------------------------------------------------------------------
# 分档：每档一个常量，档名直接印在表上
# ---------------------------------------------------------------------------

#: 登录成功、收件箱也以只读方式打开了。**只有这一档是「没问题」。**
OK = "ok"
#: 库里存的主机（或端口）与「这个域名应该用的那台」不一致。**这是我们的配置问题**，
#: 单独一档就是为了不让它掉进「授权码被拒」——那正是 ① 让用户白折腾的原因。
HOST_MISMATCH = "host_mismatch"
#: 主机对、服务器明确拒绝了这串授权码。要用户自己去网页版重新生成。
AUTH_REJECTED = "auth_rejected"
#: 这家服务商**根本不给**用授权码（微软个人版只给 OAuth）。重试多少次都一样。
PROVIDER_BLOCKED = "provider_blocked"
#: 连不上：超时、DNS、TLS、被防火墙拦。**不算结论**，换个时间再跑。
NETWORK = "network"
#: 登录成功了，但服务器不允许打开收件箱（网易的「不安全登录」就是这一档）。
INBOX_REFUSED = "inbox_refused"
#: 登录被拒，但服务器的话**不像**「授权码不对」（限流、账号被锁、要 OAuth 之外的东西）。
LOGIN_REFUSED = "login_refused"
#: 地址的域名不在任何预置里——用户自己填的主机，我们判断不了对错。
UNKNOWN_DOMAIN = "unknown_domain"
#: 这一行连探都没探成（授权码解不开、库里没地址）。
CHECK_FAILED = "check_failed"

LABELS: dict[str, str] = {
    OK: "能用",
    HOST_MISMATCH: "主机填错",
    AUTH_REJECTED: "授权码被拒",
    PROVIDER_BLOCKED: "这家不支持授权码",
    NETWORK: "网络问题",
    INBOX_REFUSED: "打不开收件箱",
    LOGIN_REFUSED: "登录被拒（非授权码）",
    UNKNOWN_DOMAIN: "域名不在预置里",
    CHECK_FAILED: "没能检查",
}

#: 每一档「下一步该谁做什么」。一句话，能被运营者直接转述给用户。
NEXT_STEPS: dict[str, str] = {
    OK: "不需要处理",
    HOST_MISMATCH: "我们的配置问题：把库里的收信服务器改成这个域名该用的那台；"
                   "不要去让用户重新生成授权码",
    AUTH_REJECTED: "要用户到邮箱网页版重新生成一个授权码（QQ/163 叫「授权码」，"
                   "Gmail 叫「应用专用密码」，都不是登录密码）；"
                   "顺手确认那里的 IMAP/SMTP 服务是开着的",
    PROVIDER_BLOCKED: "劝用户换一个支持授权码的邮箱（QQ 邮箱、Gmail、163 都可以），"
                      "城市大学的邮件照样转得过去",
    NETWORK: "先别下结论：换个时间再跑一次这条命令",
    INBOX_REFUSED: "让用户用网页版登录一次完成安全验证；还不行就照服务器原话联系客服",
    LOGIN_REFUSED: "先看服务器原话，别急着让用户重新生成授权码",
    UNKNOWN_DOMAIN: "域名不在预置里，人工看一眼主机填得对不对",
    CHECK_FAILED: "看原因（多半是主密钥或数据问题），必要时人工查",
}

# 「服务器的话不像授权码不对」的那些词，**必须先于**授权码词判断：
# 「Too many login failures」里也有 login fail，可它不是"码错了"，等一会儿就好。
_NOT_THE_CODE_WORDS = (
    "frequency", "too many", "rate limit", "temporarily", "try again later",
    "suspended", "locked", "disabled for", "unavailable", "server busy",
)
# 「服务器明确说凭据不对」——**先于**限流词判断，因为这些词本身不会被用作限流措辞，
# 而 QQ 的限流词是混在一串可能原因里的。这一条是真机教出来的：QQ 失败时回的是
#
#   Login fail. Account is abnormal, service is not open, password is incorrect,
#   login frequency limited, or system is busy.
#
# 那一串里有 "frequency"——先看限流词就会把**最常见的 QQ 失败**报成「非授权码」，
# 也就是把「你的码不对」说成「别的原因」，让用户在错的地方找。判据按词的**明确程度**
# 排序，而不是按整句。
_CLEAR_CREDENTIAL_WORDS = (
    "authenticationfailed", "authentication failed", "authenticationfailure",
    "invalid credentials", "password error", "password is incorrect",
    "incorrect password", "wrong password", "bad credentials",
    "not authenticated", "auth failed", "username and password not accepted",
)
# 「登录失败」的泛泛说法：已经排除限流之后才轮到它们。
_LOGIN_FAILURE_WORDS = (
    "login fail", "login failed", "invalid login", "authorization code",
)
# 「这家根本不给用授权码」——微软个人版的原话（真机抓的，见 test_providercheck）。
_OAUTH_ONLY_WORDS = (
    "basic authentication is disabled", "logon is denied", "login is denied",
    "logindisabled", "oauth",
)

# ---------------------------------------------------------------------------
# 纯函数：判定与预置的对照
# ---------------------------------------------------------------------------


def expected_hosts(address: str) -> dict[str, Any]:
    """这个地址**应该**用哪台服务器（空字典 = 域名不在任何预置里）。

    只转发给 `mailpresets.hosts_for_email`：域名与服务器的对应关系只有那一处定义，
    这里再写一份就会漂，而漂的方向是「检查说对、保存说错」。
    """
    return mailpresets.hosts_for_email(address)


def domain_of(address: str) -> str:
    return str(address or "").strip().lower().rsplit("@", 1)[-1].rstrip(".")


def _blocked_domains() -> frozenset[str]:
    return frozenset(
        domain
        for item in mailpresets.MAILBOX_PRESETS if item.get("blocked_reason")
        for domain in item["domains"]
    )


def _blocked_hosts() -> frozenset[str]:
    return frozenset(
        host
        for item in mailpresets.MAILBOX_PRESETS if item.get("blocked_reason")
        for host in (item["imap_host"], *item.get("blocked_hosts", ()))
    )


def blocked_provider(address: str, stored_host: str = "") -> bool:
    """这家服务商是不是我们**已经知道**用不了授权码的那一家（微软个人版）。

    域名是主判据。存下来的主机名只在**域名不认识**时才作数：一个 gmail.com 的地址
    把服务器填成了 outlook.office365.com，那是「主机填错」（我们的配置问题），
    而把它说成「这家不支持授权码」会把一个能修的问题说成死路。
    """
    domain = domain_of(address)
    if domain and domain in _blocked_domains():
        return True
    if domain and expected_hosts(address):
        return False
    return str(stored_host or "").strip().lower() in _blocked_hosts()


def classify_login_error(exc: BaseException) -> str:
    """服务器拒绝登录时，这到底是哪一档？**只看明确的措辞，不猜。**

    顺序是「明确 → 模糊」，不是「先想到哪个查哪个」：一句里出现 "password is incorrect"
    就是凭据问题，哪怕同一句里还列着 "login frequency limited"（QQ 的原话就是那样）；
    只有**通篇**都像限流（"Too many login failures"）才算「不是码的问题」。
    """
    text = str(exc).lower()
    if any(word in text for word in _OAUTH_ONLY_WORDS):
        return PROVIDER_BLOCKED
    if any(word in text for word in _CLEAR_CREDENTIAL_WORDS):
        return AUTH_REJECTED
    if any(word in text for word in _NOT_THE_CODE_WORDS):
        return LOGIN_REFUSED
    if any(word in text for word in _LOGIN_FAILURE_WORDS):
        return AUTH_REJECTED
    return LOGIN_REFUSED


#: 探针本身的结论各自怎么说。主机那一层的问题由 `conclude` 另行判。
_PROBE_REASONS = {
    OK: "",
    AUTH_REJECTED: "邮箱服务商明确拒绝了这串授权码（服务器原话见下）。",
    NETWORK: "这次没连上（超时、DNS、TLS 或端口被拦）——这不是结论，换个时间再跑一次。",
    INBOX_REFUSED: "登录成功了，但服务器不允许打开收件箱（服务器原话见下）。",
    LOGIN_REFUSED: "登录被拒，但服务器的话不像「授权码不对」（服务器原话见下）。",
    CHECK_FAILED: "这一行没有探成。",
}


def conclude(*, address: str, stored_host: str, stored_port: int,
             probe_state: str) -> tuple[str, str]:
    """决策表。**纯函数**：不连网、不读库、不碰授权码，测试可以直接喂。

    顺序是有讲究的，而且这个顺序本身就是 ① 的修法：

    1. 这条路根本不存在（微软）→ `provider_blocked`；
    2. 域名不在预置里 → 人工看（我们判断不了主机对不对）——**除非它真的登进去了**，
       那它就是一个能用的邮箱，不是故障；
    3. 主机与预置不一致 → `host_mismatch`：**排在登录结果之前**，
       所以「服务器拒绝」永远不会被误报成「你的授权码不对」；
    4. 剩下才看探针本身的结果。
    """
    address = str(address or "").strip()
    if not address or "@" not in address:
        return CHECK_FAILED, "库里这一行没有可用的邮箱地址，探不了。"
    known_blocked = blocked_provider(address, stored_host)
    if probe_state == PROVIDER_BLOCKED or known_blocked:
        # 两路证据分开说：一路是我们**已经知道**这家不给用（微软），另一路是服务器
        # 这次自己说了只给 OAuth。把第二种说成微软，会让人以为换台服务器就能好。
        if known_blocked:
            reason = ("微软系的 Outlook / Hotmail / Live 个人邮箱只给 OAuth，"
                      "授权码永远不会被接受——重试和重新生成都没有用。")
        else:
            reason = (f"{domain_of(address)} 这次明确表示只给 OAuth（服务器原话见下）——"
                      f"授权码这条路在这家已经关了，不是这串码的问题。")
        return PROVIDER_BLOCKED, reason
    expected = expected_hosts(address)
    if not expected:
        if probe_state == OK:
            # 域名不认识，但真的只读打开收件箱了——它就是在正常工作。仍然会被
            # 单独点名（见 `manage.check_mailboxes`），但结论不该是一个故障。
            return OK, ""
        return UNKNOWN_DOMAIN, (
            f"{domain_of(address)} 不在任何预置里（这是用户自己填的主机 "
            f"{stored_host}:{stored_port}），我们判断不了它填得对不对，请人工看一眼。")
    want_host = str(expected["imap_host"])
    want_port = int(expected["imap_port"])
    if str(stored_host).strip().lower() != want_host.lower() or int(stored_port) != want_port:
        return HOST_MISMATCH, (
            f"库里存的是 {stored_host}:{stored_port}，这个域名应该用 {want_host}:{want_port}。"
            f"这是我们的配置问题，不是授权码的问题。")
    return probe_state, _PROBE_REASONS.get(probe_state, "")


# ---------------------------------------------------------------------------
# 探针：只读地看一眼，绝不开箱取信
# ---------------------------------------------------------------------------


def _words(value: Any) -> str:
    """服务器（或操作系统）自己那句话，去掉 `b'…'` 包装、压成一行、截到 200 字。"""
    if isinstance(value, BaseException):
        first = value.args[0] if value.args else ""
        value = first.decode("utf-8", "replace") if isinstance(first, bytes) else str(value)
    elif isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    elif isinstance(value, (list, tuple)):
        value = value[0] if value else ""
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
    return " ".join(str(value).split())[:200]


def probe_mailbox(host: str, port: int, address: str, password: str, *,
                  timeout: int = PROBE_TIMEOUT) -> tuple[str, str]:
    """连一次、只读看一眼，返回 ``(状态, 服务器原话)``。

    **授权码只作为参数进来**：不进日志、不进返回值。返回的「原话」由调用方
    （`manage.check_mailboxes`）在打印前再擦一遍，见那里的 `_scrub`。
    """
    try:
        client = imaplib.IMAP4_SSL(host, int(port), timeout=timeout,
                                   ssl_context=mailio.imap_ssl_context())
    except (ssl.SSLError, OSError, imaplib.IMAP4.error) as exc:
        return NETWORK, _words(exc)
    try:
        # 163/126 不发 ID 就不给开箱；这一步失败不算致命（别的服务商不认它）。
        mailio.identify_client(client)
        try:
            client.login(address, password)
        except imaplib.IMAP4.abort as exc:          # 连接被服务器掐了：不是码的问题
            return NETWORK, _words(exc)
        except imaplib.IMAP4.error as exc:
            return classify_login_error(exc), _words(exc)
        except (ssl.SSLError, OSError) as exc:
            return NETWORK, _words(exc)
        try:
            # **`EXAMINE INBOX`**：只读打开。imaplib **没有** `examine()` 这个方法
            # （`IMAP4.__getattr__` 只认 `Commands` 表里的大写动词），只读那一支就在
            # `select(..., readonly=True)` 里——它内部把动词换成 EXAMINE。
            # 2026-09-20 第一版写的是 `client.examine("INBOX")`，生产上第一次真跑就抛
            # `AttributeError: Unknown IMAP4 command: 'examine'`：假服务器自己定义了
            # `examine`，所以单测全绿。现在假服务器会**照着真客户端**拒绝不存在的方法，
            # 另有一条测试直接问 imaplib：只读那一支发出去的到底是哪个动词。
            status, data = client.select("INBOX", readonly=True)
        except imaplib.IMAP4.abort as exc:
            return NETWORK, _words(exc)
        except imaplib.IMAP4.error as exc:
            return INBOX_REFUSED, _words(exc)
        except (ssl.SSLError, OSError) as exc:
            return NETWORK, _words(exc)
        if str(status).upper() != "OK":
            return INBOX_REFUSED, _words(data)
        return OK, _words(data)
    finally:
        # 无论走哪条路都要挂断：留着一条半开的连接会把邮箱占住。
        try:
            client.logout()
        except Exception:  # noqa: BLE001 - 挂断失败不该盖住真正的结论
            pass


# ---------------------------------------------------------------------------
# 逐行检查
# ---------------------------------------------------------------------------


def check_all(entries: Iterable[dict[str, Any]], *,
              probe: Callable[..., tuple[str, str]] = probe_mailbox,
              timeout: int = PROBE_TIMEOUT) -> list[dict[str, Any]]:
    """对每一行做一次探测并分档。``probe`` 可注入，测试因此不碰网络。

    每行 ``entry`` 要带 ``email`` / ``imap_host`` / ``imap_port`` / ``password``；
    ``problem`` 非空表示这一行连探都不用探（例如授权码解密失败）。
    """
    results: list[dict[str, Any]] = []
    for entry in entries:
        address = str(entry.get("email") or "").strip()
        host = str(entry.get("imap_host") or "").strip()
        try:
            port = int(entry.get("imap_port") or 993)
        except (TypeError, ValueError):
            port = 993
        problem = str(entry.get("problem") or "")
        if problem:
            state, words = CHECK_FAILED, problem
        else:
            try:
                state, words = probe(host, port, address, str(entry.get("password") or ""),
                                     timeout=timeout)
            except Exception as exc:  # noqa: BLE001
                # 一行的意外**不该让整条命令挂掉**：2026-09-20 第一版在生产上就是这么
                # 挂的（`examine` 不存在），13 个邮箱一个结论都没印出来。现在它会变成
                # 一行「没能检查」，其余的人照常出结论。
                state, words = CHECK_FAILED, f"{type(exc).__name__}: {exc}"
        tier, reason = conclude(address=address, stored_host=host, stored_port=port,
                                probe_state=state)
        expected = expected_hosts(address)
        results.append({
            "email": address,
            "domain": domain_of(address),
            "imap_host": host,
            "imap_port": port,
            # 主机是否与预置一致：True / False / None（域名不在预置里，无从对照）
            "host_ok": (None if not expected
                        else (str(expected["imap_host"]).lower() == host.lower()
                              and int(expected["imap_port"]) == port)),
            "probe_state": state,
            "tier": tier,
            "reason": reason,
            "words": words,
            "enabled": bool(entry.get("enabled", True)),
        })
    return results


def summarize(results: list[dict[str, Any]]) -> str:
    """一句话，能被运营者原样转述。**不含地址、不含任何凭据。**"""
    if not results:
        return "数据库里没有任何已配置的转发邮箱——没有可检查的东西。"
    counts: dict[str, int] = {}
    for item in results:
        counts[item["tier"]] = counts.get(item["tier"], 0) + 1
    total = len(results)
    if counts.get(OK, 0) == total:
        return f"{total} 个已配置的转发邮箱全部能用授权码登录，没有发现问题。"
    parts = "，".join(f"{LABELS[tier]} {count} 个"
                     for tier, count in sorted(counts.items(), key=lambda kv: -kv[1])
                     if tier != OK)
    sentences = [f"{total} 个已配置的转发邮箱里，{counts.get(OK, 0)} 个能用；其余：{parts}。"]
    for tier in sorted(counts, key=lambda name: -counts[name]):
        if tier != OK:
            sentences.append(f"「{LABELS[tier]}」→ {NEXT_STEPS[tier]}。")
    return "".join(sentences)
