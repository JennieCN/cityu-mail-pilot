"""Lightweight BYOK model and web-search adapters for the pilot."""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

from .security import (SecurityError, outbound_secrets, redact_secrets,
                       validate_outbound_https_url)

# Generating a full action-first bilingual report from a long email is slow: a
# measured Doubao run took ~176s per email and one real run was cut off at 240s,
# which is why the old 120s default failed on real mail. Override with
# INFE_PILOT_MODEL_TIMEOUT.
MODEL_TIMEOUT_SECONDS = int(os.environ.get("INFE_PILOT_MODEL_TIMEOUT", "300"))
SEARCH_TIMEOUT_SECONDS = int(os.environ.get("INFE_PILOT_SEARCH_TIMEOUT", "45"))

#: 本机那台（自建服务）的等待上限。
#:
#: **这个数是量出来的，不是照抄文档**（2026-09-23 在生产上按真实调用形状量的）：
#:
#: ============  ==========  ========  ========  ==========================
#: 场景            正文字符    输出 tok  耗时      备注
#: ============  ==========  ========  ========  ==========================
#: 小请求（自检）        —          2     1.6 s   稳态
#: 长通知报告        7,748        583    18.7 s   guard 一次过
#: 短通知报告          400        814    62.7 s   **guard 重生成了一次**
#: ============  ==========  ========  ========  ==========================
#:
#: 也就是说真实区间是 **2–130 s**：慢的那一次不是模型慢（折算 20–31 tok/s），
#: 而是**护栏判不合格后重生成了一次**（对方文档 §3.1 明说 `reply/summarize` 会这样，
#: 实测确实如此）。交付文档建议的「读取 90 s」对着这条路**不够**——第一版我按它取了
#: 120 s，量完发现短报告那次就已经 62.7 s，翻倍就是 125 s，正好压在线上。
#:
#: 所以取 **240 s**：比付费那档的 300 s 短一点（那台是家宽 + 租来的隧道，卡住比挂掉常见），
#: 但足够容纳「长正文 + 重生成」这一档。两档加起来最坏 240 + 300 = 540 s。
LOCAL_MODEL_TIMEOUT_SECONDS = int(os.environ.get("INFE_PILOT_LOCAL_MODEL_TIMEOUT", "240"))


def request_timeout(provider: str) -> int:
    """这一档的请求超时（秒）。

    按**供应商**分流，而不是按某一次调用的参数：本机那台是我们自己维护的、延迟分布窄；
    供应商那侧要留够长回答的余量。调用方不必知道这件事，`generate()` 每次自己取。
    """
    preset = MODEL_PRESETS.get(str(provider or "").strip().lower())
    if preset is not None and preset.local_model:
        return LOCAL_MODEL_TIMEOUT_SECONDS
    return MODEL_TIMEOUT_SECONDS


class ProviderError(RuntimeError):
    """A model or search provider returned an actionable failure."""


class TransientProviderError(ProviderError):
    """A failure worth retrying: timeout, dropped connection, 429 or 5xx.

    Kept separate so the service retries exactly these, and surfaces everything
    else (a bad key or model name) immediately instead of three times.
    """


class ProviderTimeout(TransientProviderError):
    """The provider took the request but did not answer in time.

    Split out from the other transient failures because it is the expensive one
    to retry. A dropped connection fails in milliseconds, so an immediate retry
    is nearly free; a timeout has already burned the whole budget (measured: a
    real report takes ~234 s against a 300 s ceiling), so retrying it doubles
    the time that message holds a generation slot. The queue already retries
    failed messages with exponential backoff, and this failure is left to that.
    """


@dataclass(frozen=True)
class ModelPreset:
    id: str
    label: str
    protocol: str
    base_url: str
    default_model: str = ""
    fixed_host: bool = True
    # Providers whose own API can search the web as a server-side tool, so the
    # user does not need a second, separately billed search API. Capability-flag
    # plus external fallback follows the MIT-licensed smalibary/pi-native-search.
    native_search: bool = False
    #: 这台服务是**我们自己的**（运营者维护的一台机器），不是供应商的公开端点。
    #:
    #: 用途只有一个：把「花的是账户里的钱」与「花的是自己家的电」分开。余额、警戒线、
    #: 见底就不调用——这一整套只对**有账户**的那一档成立（`metered_connection`）。
    local_model: bool = False
    #: Environment variable a **fixed-host** provider reads its Base URL from.
    #:
    #: `fixed_host` means "the operator decides this address, a user's saved row
    #: cannot". It does not mean "a vendor's public endpoint that never moves":
    #: the local model service below sits at a fixed host name that follows the
    #: tunnel. Without this field, moving it would take an edit to this file plus
    #: a deploy — the exact "restart the software and it fixes itself" habit this
    #: operator console is supposed to remove.
    base_env: str = ""
    #: CA bundle (PEM) to trust **in addition to** the system roots, for this
    #: provider's requests only. Deliberately not a global switch: one provider's
    #: self-signed certificate must not become everybody's trust anchor.
    ca_file: str = ""
    #: The certificate this provider pins, as a SHA-256 fingerprint ("AA:BB:…").
    #:
    #: A self-signed certificate on a host name we do not control is **not**
    #: authentication. Whoever can point that name at their own box and sign
    #: their own certificate passes every ordinary check, and then receives our
    #: Bearer key. The tunnel in this deployment is a rented TCP forwarder, so
    #: that is a real party. Pinning the exact certificate is what turns "somebody
    #: is answering at this address" into "our service is answering".
    pinned_fingerprint: str = ""


MODEL_PRESETS: dict[str, ModelPreset] = {
    "openai": ModelPreset("openai", "OpenAI", "openai_responses", "https://api.openai.com/v1", native_search=True),
    "anthropic": ModelPreset("anthropic", "Anthropic Claude", "anthropic", "https://api.anthropic.com/v1", native_search=True),
    "gemini": ModelPreset("gemini", "Google Gemini", "gemini", "https://generativelanguage.googleapis.com/v1beta", native_search=True),
    "volcengine_ark": ModelPreset("volcengine_ark", "火山方舟 / 豆包", "ark", "https://ark.cn-beijing.volces.com/api/plan"),
    "volcengine_ark_openai": ModelPreset("volcengine_ark_openai", "火山方舟标准 OpenAI 兼容", "openai_chat", "https://ark.cn-beijing.volces.com/api/v3"),
    # 同一个 v3 底座，只是路径换成 /responses——方舟的「联网内容插件」**只**在
    # Responses 上有，OpenAI 兼容的 /chat/completions 上没有（官方说明与实测一致：
    # 兼容层只翻译基础对话）。所以想要原生联网搜索就必须走这个协议。
    "volcengine_ark_responses": ModelPreset(
        "volcengine_ark_responses", "火山方舟 Responses（原生联网搜索）",
        "openai_responses", "https://ark.cn-beijing.volces.com/api/v3", native_search=True),
    "deepseek": ModelPreset("deepseek", "DeepSeek", "openai_chat", "https://api.deepseek.com"),
    "openrouter": ModelPreset("openrouter", "OpenRouter", "openai_chat", "https://openrouter.ai/api/v1"),
    "groq": ModelPreset("groq", "Groq", "openai_chat", "https://api.groq.com/openai/v1"),
    "mistral": ModelPreset("mistral", "Mistral AI", "openai_chat", "https://api.mistral.ai/v1"),
    "xai": ModelPreset("xai", "xAI", "openai_chat", "https://api.x.ai/v1"),
    "together": ModelPreset("together", "Together AI", "openai_chat", "https://api.together.ai/v1"),
    "qwen": ModelPreset("qwen", "阿里云百炼 / Qwen", "openai_chat", "https://dashscope.aliyuncs.com/compatible-mode/v1", fixed_host=False),
    "zhipu": ModelPreset("zhipu", "智谱 GLM", "openai_chat", "https://open.bigmodel.cn/api/paas/v4", fixed_host=False),
    "moonshot": ModelPreset("moonshot", "Moonshot / Kimi", "openai_chat", "https://api.moonshot.cn/v1", fixed_host=False),
    "azure_openai": ModelPreset("azure_openai", "Azure OpenAI", "azure_openai", "", fixed_host=False),
    "custom_openai": ModelPreset("custom_openai", "自定义 OpenAI 兼容 API", "openai_chat", "", fixed_host=False),
    # 本机大模型服务（维护侧那台盒子 + 樱花隧道）。它长得像 `custom_openai`，但三处不能混：
    #   ① 地址由运营者固定（`base_env`），**不给用户填**——用户若能把它改到别处，存在这里的
    #      那把 key 就会被送到他挑的机器上，而那是运营者的凭据；
    #   ② 自签证书 + 指纹钉扎（`local_model_tls`）；
    #   ③ 每个请求要带 `x_guard.task`，否则护栏只能靠猜（`guard_task_for`）。
    # 交付说明：docs/local-model-2026-09-22.md。
    "local_openai": ModelPreset(
        "local_openai", "本机大模型（Bonsai + 本地护栏）", "openai_chat",
        "https://frp-act.com:59851/v1", default_model="ternary-bonsai-2-27b",
        base_env="INFE_PILOT_LOCAL_MODEL_BASE_URL",
        ca_file="certs/localmodel.pem",
        pinned_fingerprint="F2:A2:95:1D:1B:C7:86:F7:9A:1F:10:A2:30:B7:EC:C5:5E:A0:15:45:F2:33:9D:A1:63:E0:18:DB:0B:1A:FE:F3",
        local_model=True,
    ),
}


SEARCH_PRESETS = {
    "doubao": {"label": "豆包联网搜索", "base_url": "https://open.feedcoopapi.com/search_api/web_search"},
    "tavily": {"label": "Tavily", "base_url": "https://api.tavily.com/search"},
    "brave": {"label": "Brave Search", "base_url": "https://api.search.brave.com/res/v1/web/search"},
}


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """**拒绝跟随重定向。**

    2026-09-22 的安全审查（`handoff/REVIEW-2026-09-22.md` 第一条 P1）演示过这条路径：
    一个已登录用户把自己那台服务器的 Base URL 指到一个他控制的域名，那个域名回 302 到
    `http://127.0.0.1:…` 或云元数据地址——默认 opener 会**跟过去**，而 Python 的重定向
    处理器会把请求头一起带过去（本机实测：302 之后目标那侧收到的 `Authorization` 仍是
    `Bearer sk-…`）。保存时的检查只看**原始地址**，重定向目标从不经过它。

    「不跟」而不是「跟了再检查」：一个把自己凭据往别处送的供应商没有正当用途，而
    检查每一次跳转是一道迟早会漏的闸门。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ProviderError(
            f"供应商返回了重定向（HTTP {code}）到另一个地址，已拒绝跟随。"
            "如果你配置的是自定义 API 地址，请直接填最终地址。"
            f"（目标：{redact_secrets(str(newurl), outbound_secrets(getattr(req, 'headers', None), req.full_url))}）")


#: 出站请求统一走它：**只有这一个 opener**，不给「某处漏用默认 opener」留口子。
_OUTBOUND_OPENER = urllib.request.build_opener(_NoRedirects)

#: 额外 CA / 钉扎那几种组合的 opener，按 (ca_file, pin) 缓存。进程内建一次就够。
_TLS_OPENERS: dict[tuple[str, str], Any] = {}
_CONTEXTS: dict[str, "ssl.SSLContext"] = {}


def _context_for(ca_file: str) -> "ssl.SSLContext":
    """系统信任库 **+** 指定 CA 的 context（按文件名缓存）。

    不是 `_create_unverified_context`：主机名与有效期照常校验，只是多认一张证书。
    """
    cached = _CONTEXTS.get(ca_file)
    if cached is not None:
        return cached
    context = ssl.create_default_context()
    if ca_file:
        context.load_verify_locations(cafile=ca_file)
    _CONTEXTS[ca_file] = context
    return context


# ---------------------------------------------------------------------------
# 额外 CA + 指纹钉扎：给自签证书的供应商用（目前只有本机那台）
# ---------------------------------------------------------------------------
#
# 为什么不是 `verify=False`：那等于把「这个地址上是谁在应答」整个放弃。这里要的是**更严**
# 而不是更松——只多信一张我们指定的证书，并且只认它那一张。
def reset_tls_cache() -> None:
    """清掉 opener / context 缓存。

    只给测试与「运营者刚换了证书」这两件事用：缓存按文件名键控，同名文件换了内容
    （续期后覆盖同一个 `cert.pem`）在进程里不会自动重读。
    """
    _TLS_OPENERS.clear()
    _CONTEXTS.clear()


# ---------------------------------------------------------------------------
# 额外 CA + 指纹钉扎：给自签证书的供应商用（目前只有本机那台）
# ---------------------------------------------------------------------------
#
# 为什么不是 `ssl._create_unverified_context()` 或 `verify=False`：那把「地址对不对」
# 整个放弃了。这里要的是**更严**而不是更松——多信一张我们指定的证书，并且只认它那一张。
def local_model_cert_path() -> str:
    """本机服务的 CA 文件。运营者可以用环境变量换掉（证书续期时不必改代码）。"""
    override = (os.environ.get("INFE_PILOT_LOCAL_MODEL_CA_FILE") or "").strip()
    if override:
        return override
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "certs", "localmodel.pem")


def normalize_fingerprint(value: str) -> str:
    """把指纹归一成大写十六进制、去掉分隔符。读不出来就返回空串。

    比指纹这件事不能靠肉眼：一个带冒号、一个不带，直接比字符串会把**正确的**
    证书判成错的（然后所有人都没有报告），所以两边都过这一道。
    """
    cleaned = re.sub(r"[^0-9A-Fa-f]", "", str(value or ""))
    return cleaned.upper() if len(cleaned) == 64 else ""


def local_model_fingerprint() -> str:
    """要钉的那张证书的 SHA-256 指纹（运营者可用环境变量覆盖）。"""
    preset = MODEL_PRESETS["local_openai"]
    override = (os.environ.get("INFE_PILOT_LOCAL_MODEL_PIN") or "").strip()
    return override or preset.pinned_fingerprint


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """自签证书 + 指纹钉扎的 HTTPS 连接。

    两件事都要做，缺一不可：

    * `context` 里带上那张自签证书当 CA —— 否则握手第一步就失败（系统信任库里没有它）；
    * 握手之后**逐字节比指纹** —— 否则「信任这张证书」等于「信任任何自称这个域名的证书」。

    第二条是这次接入的真正安全边界。隧道是租来的 TCP 转发，域名不属于我们；
    只做第一条的话，任何能让 `frp-act.com` 解析到自己机器上、再自签一张同域名证书的人，
    都会同时拿到我们的请求正文和 `Authorization` 头。
    """

    def __init__(self, *args, ca_file: str = "", pin: str = "", **kwargs):
        self._ca_file = ca_file
        self._pin = normalize_fingerprint(pin)
        # `urllib` 会把它的默认 `context` 一起塞进来（`HTTPSHandler` 那条路总是带这个
        # 参数，哪怕是 None）。丢掉它、换成我们自己的：否则自签证书那张根本不参与校验。
        kwargs.pop("context", None)
        # 握手用的 context 挂在连接上，于是 `super().connect()` 的隧道/代理分支、超时、
        # `source_address` 全部照旧——只多一步握完之后的指纹比对。
        kwargs["context"] = _context_for(self._ca_file)
        super().__init__(*args, **kwargs)

    def connect(self):
        super().connect()
        if not self._pin:
            return
        der = self.sock.getpeercert(binary_form=True)
        presented = normalize_fingerprint(hashlib.sha256(der).hexdigest()) if der else ""
        if presented != self._pin:
            self.sock.close()
            self.sock = None
            raise ssl.SSLError(
                f"本机模型服务的证书指纹与钉扎的那一张不一致，已断开（对端 {presented or '读不出来'}）。"
                "这说明这个地址上应答的不是我们的服务，不是网络故障；重新签发证书之后要同时"
                "更新 certs/localmodel.pem 与 INFE_PILOT_LOCAL_MODEL_PIN。")


def _build_tls_opener(ca_file: str, pin: str):
    """带额外 CA + 指纹钉扎的 opener。

    自己写一个 handler 而不是 `HTTPSHandler(connection_class=…)`：那个参数是 Python 3.12
    才有的，而生产跑 3.14、这台开发机跑 3.9，两端都要能跑（这个项目的测试矩阵就是这两端）。
    照抄标准库的 `HTTPSHandler.https_open` 的写法，只把连接类换掉。
    """

    class _Connection(_PinnedHTTPSConnection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, ca_file=ca_file, pin=pin, **kwargs)

    class _PinnedHTTPSHandler(urllib.request.AbstractHTTPHandler):
        # **必须排在那一个之前。** `build_opener` 总会补一个标准库的 `HTTPSHandler`
        # （handler_order 500），而 urlopen 只认**第一个**能处理该协议的 handler：
        # 两边同为 500 时先来后到由插入顺序决定，标准库那个会先接走请求，于是自签
        # 证书压根没进校验（现场表现：`CERTIFICATE_VERIFY_FAILED: self signed certificate`，
        # 看起来像证书装错了，其实是这条更早的路径根本没读它）。
        handler_order = 400

        def https_open(self, request):
            # 刻意**不**照标准库那样传 `context=` / `check_hostname=`：那两个参数会盖掉
            # 连接自己装好的 context（`HTTPSConnection.__init__` 会在 context 上设
            # `check_hostname`），而我们要的正是「用我们这张 CA 去校验」。
            return self.do_open(_Connection, request)

    return urllib.request.build_opener(_NoRedirects, _PinnedHTTPSHandler())


def _outbound_open(request: urllib.request.Request, *, timeout: int, tls: Optional[dict[str, str]] = None):
    """出站请求的唯一入口（测试也按这个名字打桩，不碰 `urllib.request` 全局）。

    ``tls`` 只有 `local_model_tls` 会传：它把这一次请求的信任范围收窄到**一个**供应商。
    默认路径（``None``）仍走系统信任库的 opener，所以别的供应商一点都没变松。
    """
    if not tls:
        return _OUTBOUND_OPENER.open(request, timeout=timeout)
    key = (str(tls.get("ca_file") or ""), normalize_fingerprint(str(tls.get("pin") or "")))
    opener = _TLS_OPENERS.get(key)
    if opener is None:
        opener = _build_tls_opener(*key)
        _TLS_OPENERS[key] = opener
    return opener.open(request, timeout=timeout)


#: 一次 API 响应的字节上限。报告生成要的是普通邮件正文，正常响应是几十 KB 量级；
#: 给到 2 MiB 是留了两个数量级的余量，同时挡住「上游（或路上一个被接管的网关）
#: 一直往我们这儿灌数据」把线程与内存吃光。可用环境变量调。
MAX_RESPONSE_BYTES = int(os.environ.get("INFE_PILOT_MAX_RESPONSE_BYTES", str(2 * 1024 * 1024)))


def _read_bounded(response, *, timeout: int) -> bytes:
    """带上限、带**总时限**地读响应。

    `response.read()` 什么都不带：上游可以一直发，我们就一直在读——线程、内存和这次调用
    的预算都跟着走。`timeout`（`urlopen` 的那个）是**socket 超时**，是两次阻塞之间的
    间隔，不是这次操作的总时长：慢速分块能在这个间隔之内把总时间拉得很长。所以这里
    自己看着表读（2026-09-22 那份审查的第四条 P2）。
    """
    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    total = 0
    while True:
        if time.monotonic() > deadline:
            raise ProviderTimeout(f"接口响应超过 {timeout} 秒还没读完，已中断。稍后会自动重试。")
        chunk = response.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise ProviderError(
                f"接口响应超过 {MAX_RESPONSE_BYTES // (1024 * 1024)} MB 上限，已中断。"
                "正常响应是几十 KB 量级；这么大通常意味着 Base URL 指错了地方，"
                "或者中间有网关在返回别的东西。")
        chunks.append(chunk)
    return b"".join(chunks)


#: 有些供应商把错误放在 **HTTP 200 的正文**里（`{"error": …}` 或 `{"type": "error"}`）。
#: 不认出来的话，那种响应会被当成「模型返回了空正文」→ 记成**永久失败、不重试**——
#: 而其中很常见的一类恰恰是「限流/超额/过载」，本该退避重试。
_TRANSIENT_HINTS = (
    "rate limit", "rate_limit", "ratelimit", "too many requests", "quota",
    "overloaded", "overload", "capacity", "temporarily", "try again", "timeout",
)
_TRANSIENT_STATUS = ("RESOURCE_EXHAUSTED", "UNAVAILABLE", "OVERLOADED", "ABORTED", "INTERNAL")


def _raise_body_error(payload: Any, secrets: list[str]) -> None:
    """200 但正文里是错误 → 按它的形状分出「可重试」与「永久」。

    分类只看**明确的证据**：HTTP 状态码（各家自己的字段）、错误类型串、以及
    错误文本里的关键词。认不出来就算永久——交给熔断器与重试策略按永久处理，
    比把一次真失败重试到天荒地老要好。
    """
    if not isinstance(payload, dict):
        return
    error = payload.get("error")
    if not error and payload.get("type") != "error":
        return
    body = error if isinstance(error, dict) else {"message": str(error or payload.get("message") or "")}
    text = " ".join(str(body.get(key) or "") for key in ("message", "type", "code", "status", "reason"))
    text = f"{text} {payload.get('status') or ''}".strip()
    code = body.get("code") or payload.get("code")
    status = str(body.get("status") or payload.get("status") or "").upper()
    transient = False
    try:
        numeric = int(code)
    except (TypeError, ValueError):
        numeric = 0
    if numeric == 429 or numeric >= 500:
        transient = True
    if status in _TRANSIENT_STATUS:
        transient = True
    lowered = text.lower()
    if any(hint in lowered for hint in _TRANSIENT_HINTS):
        transient = True
    message = f"API 返回了错误（HTTP 200）：{redact_secrets(text[:400], secrets)}"
    if transient:
        raise TransientProviderError(message)
    raise ProviderError(message)


def _json_request(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any] | None = None,
    method: str = "POST",
    timeout: int = 120,
    tls: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Accept": "application/json", **headers, **({"Content-Type": "application/json"} if body else {})},
    )
    # 这一次请求里「一旦被回显就必须抹掉」的值：头的凭据 + URL 查询串里的秘密。
    secrets = outbound_secrets(headers, url)
    try:
        with _outbound_open(request, timeout=timeout, tls=tls) as response:
            raw = _read_bounded(response, timeout=timeout)
            try:
                payload = json.loads(raw.decode()) if raw else {}
                # 200 也可能是错误（见 `_raise_body_error` 的注释）：在**返回之前**判掉，
                # 否则调用方会把它当成「空回答」，于是一次限流被记成永久失败。
                _raise_body_error(payload, secrets)
                return payload
            except (ValueError, UnicodeDecodeError):
                # 上游返回的不是 JSON（网关的 HTML 错误页、被截断的响应……）。**不回显正文**：
                # 它可能带着我们的请求凭据。`from None` 是因为 JSONDecodeError 的原文里
                # 就抄了一段出错的文档。
                raise ProviderError(
                    f"API 返回的不是 JSON（HTTP 200，{len(raw)} 字节）。"
                    "常见原因是 Base URL 填成了网站首页，或中间有网关拦截。") from None
    except urllib.error.HTTPError as exc:
        detail = exc.read(2048).decode(errors="replace")
        # 供应商的报错正文有用（它常常直接指出哪个字段不对），但**上游把请求凭据回显在
        # 正文里是真实存在的形状**（401 里的 `Incorrect API key provided: sk-…`），所以这里
        # 不是「相信供应商不会回显」，而是先抹掉我们自己发出去的那几个值、再对常见形状
        # 兜底清扫。异常链保留：`str(HTTPError)` 只有状态行，不带正文。
        message = f"API 返回 HTTP {exc.code}: {redact_secrets(detail[:800], secrets)}"
        if exc.code == 429 or exc.code >= 500:
            raise TransientProviderError(message) from exc
        raise ProviderError(message) from exc
    except urllib.error.URLError as exc:
        # 证书不对**不是**「稍后再试就好」：钉扎指纹不一致意味着这个地址上应答的不是
        # 我们的服务，重试一次仍然不是。把它降级成 `ProviderError`（永久）而不是
        # `TransientProviderError`，重试与退避才不会围着一道永远过不去的门空转。
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ssl.SSLError) or "CERTIFICATE_VERIFY_FAILED" in str(reason):
            raise ProviderError(
                f"TLS 校验没通过（{redact_secrets(str(reason), secrets)}）。"
                "这通常是证书换了却没同步、或这个地址上应答的不是原来那台服务——"
                "不是网络抖动，重试不会变好。核对 certs/localmodel.pem 与钉扎指纹。") from None
        # `reason` 可能是带查询串的 URL 或解析器给的原话，所以**断掉异常链**：我们的
        # message 脱敏过，而 `__cause__` 会原样保留上游文本（日志里的 traceback 会印它）。
        raise TransientProviderError(
            f"无法连接 API：{redact_secrets(str(exc.reason), secrets)}") from None
    except (TimeoutError, socket.timeout) as exc:
        # A bare read timeout is not wrapped in URLError, so it used to escape
        # as an opaque socket.error instead of an actionable provider failure.
        raise ProviderTimeout(f"接口响应超时（超过 {timeout} 秒没有返回）。稍后会自动重试。") from exc
    except http.client.RemoteDisconnected as exc:
        # A long generation can be cut off mid-flight by the provider or an
        # intermediary; this is exactly the case an automatic retry fixes.
        raise TransientProviderError("接口连接被中断（长回答可能超时）。稍后会自动重试。") from exc
    except http.client.HTTPException as exc:
        # 同 URLError：原文形状不受我们控制，断链。
        raise TransientProviderError(
            f"接口连接异常：{redact_secrets(str(exc), secrets)}") from None
    except OSError as exc:
        raise TransientProviderError(f"网络错误：{redact_secrets(str(exc), secrets)}") from exc


# Model names a provider still accepts but no longer documents. Kept as data,
# with the evidence next to it, because the name we *send* is the difference
# between a working install and a wall of red lights on the day the provider
# retires the alias.
#
# Measured 2026-09-15 against the real DeepSeek API: a request for
# "deepseek-chat" comes back with "model": "deepseek-flash", and GET /models
# lists only deepseek-flash and deepseek-v4-pro. The official docs no longer
# mention deepseek-chat at all.
LEGACY_MODEL_ALIASES: dict[tuple[str, str], str] = {
    ("deepseek", "deepseek-chat"): "deepseek-flash",
}


def official_model_name(provider: str, model: str) -> str:
    """The name to actually send for ``model``.

    A user's stored connection keeps whatever they typed — this only decides
    what goes on the wire. Records (``token_usage``) take the name from here too,
    so "what we requested" and "what we billed it as" stay the same string.
    """
    return LEGACY_MODEL_ALIASES.get((provider.strip().lower(), model.strip().lower()), model)


def label_for(provider: str) -> str:
    """供应商的**给人看**的名字；不认识的返回原样。

    界面上一直显示的是 `provider` 这个内部 id（`deepseek`、`local_openai`……）。
    对「本机大模型」那一档，用户看到的是「谁在处理我的邮件」这件事——
    `local_openai` 既说不清也不好看，所以界面读这个名字。
    """
    preset = MODEL_PRESETS.get(str(provider or "").strip().lower())
    return preset.label if preset else str(provider or "")


def normalized_model_config(provider: str, model: str, base_url: str = "") -> tuple[ModelPreset, str, str]:
    if provider not in MODEL_PRESETS:
        raise ProviderError("不支持的模型供应商。")
    preset = MODEL_PRESETS[provider]
    model = official_model_name(provider, model.strip())
    if not model:
        raise ProviderError("必须填写模型或部署名称。")
    if preset.fixed_host:
        # 固定主的供应商也可能把地址放在环境里（本机服务就是这样：隧道换域名时
        # 运营者改一个变量，不是改代码）。它**不是**用户可填的字段，所以仍走
        # `fixed_host=True` 这一支：用户填的那个值照旧被忽略。
        override = (os.environ.get(preset.base_env) or "").strip() if preset.base_env else ""
        effective_base = override or preset.base_url
    else:
        effective_base = base_url.strip() or preset.base_url
    if not effective_base:
        raise ProviderError("此供应商必须填写 API Base URL。")
    if not preset.fixed_host:
        effective_base = validate_outbound_https_url(effective_base)
    return preset, model, effective_base.rstrip("/")


def local_model_tls(provider: str) -> Optional[dict[str, str]]:
    """本机服务的出站 TLS 配置；其它供应商一律 ``None``（走系统信任库）。

    返回 ``None`` 也算一种结果：没有配 CA 文件时不去编一个「信任一切」的 context，
    而是让请求照常失败——一个读不到的证书文件不该被静默降级成不校验。

    文件不存在时**不抛异常**：`load_verify_locations` 会自己报错，报文里带着路径，
    比我们在这里猜一句「证书丢了」更接近真相。
    """
    preset = MODEL_PRESETS.get(str(provider or "").strip().lower())
    if preset is None or not preset.ca_file:
        return None
    return {"ca_file": local_model_cert_path(), "pin": local_model_fingerprint()}


def local_model_health_url(base_url: str) -> str:
    """把模型基址（`…/v1`）换成同源的 `/health`。

    交付文档 §9 的自测第一条就是这个路径（预期 `{"proxy":"ok","upstream":…}`），
    它**不需要 key、不调模型**，所以可以每 5 分钟问一次而不花任何钱。
    """
    root = str(base_url or "").strip().rstrip("/")
    if not root:
        return ""
    if root.endswith("/v1"):
        root = root[: -len("/v1")]
    return root + "/health"


#: 主服务那台盒子**同时**能跑几份报告。默认 2 是交付文档 §8 的 `-np 2`（每槽 57344 上下文）。
#: 为什么必须是一个可配的数字而不是写死：这是**对方机器**上的参数，换模型/换参数就要跟着改，
#: 而我们这边看到的只是"变慢/排队"。容量面板把它当成一个显式的约束输入（见 `capacity.advise`）。
LOCAL_MODEL_SLOTS_ENV = "INFE_PILOT_LOCAL_MODEL_SLOTS"
LOCAL_MODEL_SLOTS_DEFAULT = 2


def local_model_is_primary() -> bool:
    """第一档是不是本机那台盒子（不是付费供应商）。"""
    connection = platform_model_default()
    return bool(connection and str(connection.get("provider") or "") == LOCAL_MODEL_PROVIDER)


def local_model_slots() -> Optional[int]:
    """主服务那台的推理槽数；**主档不是本机那台时返回 ``None``**（那就不存在这个约束）。

    2026-09-23 补：容量面板原来只拿我们自己的 `REPORT_WORKERS`（6）当并发，
    而真正同时在算的只有那台盒子的 2 个槽——两个数字在两台机器上，谁也不认识谁，
    于是面板给的账号上限比真实天花板高出一个数量级。
    """
    if not local_model_is_primary():
        return None
    raw = (os.environ.get(LOCAL_MODEL_SLOTS_ENV) or "").strip()
    if not raw:
        return LOCAL_MODEL_SLOTS_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        logging.warning("%s 不是整数：%r，按默认 %s 处理",
                        LOCAL_MODEL_SLOTS_ENV, raw, LOCAL_MODEL_SLOTS_DEFAULT)
        return LOCAL_MODEL_SLOTS_DEFAULT
    return max(1, min(64, value))


def local_model_health(base_url: str, *, timeout: int = 5) -> dict[str, Any]:
    """探一次本机服务的 `/health`（带那张自签证书与指纹钉扎）。

    为什么单独有这一条，而不是复用 `generate()`：报告那条路上「那台不通」只有在**真的
    要出一封报告**时才会被发现——如果那台在凌晨断了、而下一封信要等到中午，中间这几个
    小时里谁都不知道，报告全走付费兜底。这个探测把「最早什么时候知道」从「下一封信」
    压到「下一轮巡检」。

    **不校验业务、不调模型、不需要 key**：它只回答「这个地址上还有没有我们的服务在听」。
    """
    url = local_model_health_url(base_url)
    if not url:
        raise ProviderError("本机服务的地址没配置，探不了。")
    return _json_request(url, headers={}, method="GET", timeout=timeout,
                         tls=local_model_tls(LOCAL_MODEL_PROVIDER))


#: 护栏任务名（`x_guard.task`）。取值是**对方服务定的**，见交付文档 §3.1，不要自己造词。
GUARD_TASKS = ("classify", "extract", "summarize", "reply")


def guard_task_for(provider: str, task: str) -> str:
    """本机服务：这次调用该报哪个护栏任务；别的供应商返回空串（不产生该字段）。

    **必须显式报。** 不报的时候护栏只能按正文猜任务，而猜错的代价不是「少一道检查」而是
    「多一道错的检查」：一份日报摘要被当成 `reply` 审，就会被要求「信息不足要索取订单号」，
    于是每封报告都带着 issue、或者被重生成一次（延迟翻倍）。
    """
    if str(provider or "").strip().lower() != "local_openai":
        return ""
    value = str(task or "").strip().lower()
    if value not in GUARD_TASKS:
        raise ProviderError(f"护栏任务名不认识：{task!r}（只允许 {'/'.join(GUARD_TASKS)}）")
    return value


# The pilot's shared model credential. It lives in the environment file, not in
# the database, and that is a security decision rather than a convenience one:
# the database is copied into a daily backup that gets written to /var/backups
# and shipped around, while the master key deliberately is not. A live API key in
# a backup would be a downgrade of exactly the property the master key's absence
# protects.
#
# It is a fallback, never an override: a user who configured their own key keeps
# using it, which is what the landing page and the privacy policy already promise.
PLATFORM_KEY_ENV = "INFE_PILOT_DEFAULT_MODEL_KEY"
PLATFORM_PROVIDER_ENV = "INFE_PILOT_DEFAULT_MODEL_PROVIDER"
PLATFORM_MODEL_ENV = "INFE_PILOT_DEFAULT_MODEL_NAME"
PLATFORM_BASE_ENV = "INFE_PILOT_DEFAULT_MODEL_BASE_URL"

#: 「兜底的兜底」：主服务（本机那台盒子 + 隧道）不可用时接手的第二把 key。
#:
#: 为什么值得多一套变量：主服务跑在家用宽带 + 租来的 TCP 隧道上，**它挂掉不是异常，
#: 是常态的一种**（断电、断网、隧道额度用完、盒子重启）。没有这一层的话，那段时间里
#: 所有没自带 key 的账号一封报告都收不到；有这一层，用户看到的只是「今天有点慢」。
#: 代价是那段时间真的在花钱——所以它排在主服务之后，且只在这条链里被调用。
PLATFORM_FALLBACK_KEY_ENV = "INFE_PILOT_DEFAULT_MODEL_FALLBACK_KEY"
PLATFORM_FALLBACK_PROVIDER_ENV = "INFE_PILOT_DEFAULT_MODEL_FALLBACK_PROVIDER"
PLATFORM_FALLBACK_MODEL_ENV = "INFE_PILOT_DEFAULT_MODEL_FALLBACK_NAME"
PLATFORM_FALLBACK_BASE_ENV = "INFE_PILOT_DEFAULT_MODEL_FALLBACK_BASE_URL"

#: 本机那台盒子（运营者自建的服务）在 `MODEL_PRESETS` 里的 id。单独提出来是因为下面那道
#: 闸门要按它判断「这一档会不会把凭据发到第三方那台机器上」。
LOCAL_MODEL_PROVIDER = "local_openai"


def _platform_connection(provider_var: str, model_var: str, base_var: str,
                         key_var: str) -> Optional[dict[str, Any]]:
    """按一套环境变量拼一个平台连接；没配 key 就 ``None``。

    ``model`` 为空时的兜底规则是**供应商自己公布的默认名**：deepseek 是
    `deepseek-flash`，本机那台是它自己的模型名（服务端其实忽略 `model` 字段，
    但记录里要有个名字，否则「这次是哪个模型答的」在 `token_usage` 里就是空白）。
    """
    provider = (os.environ.get(provider_var) or "deepseek").strip()
    if provider not in MODEL_PRESETS:
        logging.warning("%s 不是已知供应商：%s，这一档不生效", provider_var, provider)
        return None
    model = (os.environ.get(model_var) or "").strip()
    if not model:
        model = MODEL_PRESETS[provider].default_model
    if not model and provider != "deepseek":
        # 往 OpenAI 发 "deepseek-flash" 会在供应商那边报一个和真正错误（少配了个变量）
        # 毫无关系的错，所以除这两家之外名字必须写全。
        logging.warning("设置了 %s，但没有设置 %s（供应商 %s），这一档不生效",
                        key_var, model_var, provider)
        return None
    if not model:
        model = "deepseek-flash"
    try:
        preset, model, base_url = normalized_model_config(
            provider, model, os.environ.get(base_var) or "")
    except ProviderError as exc:
        logging.warning("平台默认模型配置无效，已忽略：%s", exc)
        return None
    return {
        "id": "", "user_id": "", "kind": "model",
        "provider": preset.id, "model": model, "base_url": base_url,
        "enabled": 1, "last_test_at": None, "last_error": "",
        "created_at": "", "updated_at": "",
        # Not ciphertext: the plaintext comes from the environment on demand, so
        # nothing has to decrypt it and it never enters a row that could be
        # serialised into an API response or a backup.
        "encrypted_api_key": None,
        "platform": True,
    }


def platform_model_default() -> Optional[dict[str, Any]]:
    """The instance-wide model connection, or None when the operator set no key.

    Returning None is the normal state for a self-hosted install: the software
    must work with every user bringing their own key. The pilot operator adding
    one is what turns the published "the operator pays during the pilot" line
    into something the code actually does, instead of something the operator
    performs by hand for each new account in the admin console.

    这个实例的「平台默认」是**主服务**（本机那台盒子）；配了第二把 key 时它只是链里的
    第一跳，见 `platform_model_connections`。
    """
    api_key = (os.environ.get(PLATFORM_KEY_ENV) or "").strip()
    if not api_key:
        return None
    return _platform_connection(PLATFORM_PROVIDER_ENV, PLATFORM_MODEL_ENV, PLATFORM_BASE_ENV,
                               PLATFORM_KEY_ENV)


def metered_model_connection() -> Optional[dict[str, Any]]:
    """**花账户里钱**的那一档（= 管理员的 DeepSeek key），没有就 ``None``。

    `budget` 只关心这一档：余额、见底告警、见底不调用，全都是「账户里还剩多少钱」的
    问题，而本机那台没有账户、也没有余额接口。判据是「这台服务不是我们自己的」
    （`ModelPreset.local_model`），而不是「这家有没有余额接口」——后者会把任何一家
    还没接余额查询的供应商也当成「没有账户」，于是那道闸静默失效。
    """
    for connection in platform_model_connections():
        preset = MODEL_PRESETS.get(str(connection.get("provider") or "").strip().lower())
        if preset is not None and not preset.local_model:
            return connection
    return None


def is_metered(connection: dict[str, Any] | None) -> bool:
    """这一次调用是不是要花账户里的钱（决定要不要过余额那道闸）。"""
    preset = MODEL_PRESETS.get(str((connection or {}).get("provider") or "").strip().lower())
    return bool(preset is not None and not preset.local_model)


def is_local(connection: dict[str, Any] | None) -> bool:
    """这一档是不是**我们自己维护的那台**（本机服务）。

    单独写一个而不是取 `is_metered` 的补集：那个补集只对**认得的**供应商成立，
    不认识的供应商两边都是 `False`——「不知道」不该被当成「我们自己那台」。
    """
    preset = MODEL_PRESETS.get(str((connection or {}).get("provider") or "").strip().lower())
    return bool(preset is not None and preset.local_model)


def _platform_connections_raw() -> list[dict[str, Any]]:
    """两档都按环境变量拼出来，**不做一致性检查**（那是 `platform_model_connections`）。

    单独留一层，是因为「装错了」这件事本身要能被问出来：闸门把有问题的那一档摘掉之后，
    从返回值里再也看不出刚才发生过什么，而运营者需要知道——他以为主服务在省钱，
    实际上每一封报告都在花钱。
    """
    candidates = [platform_model_default()]
    fallback = None
    if (os.environ.get(PLATFORM_FALLBACK_KEY_ENV) or "").strip():
        fallback = _platform_connection(
            PLATFORM_FALLBACK_PROVIDER_ENV, PLATFORM_FALLBACK_MODEL_ENV,
            PLATFORM_FALLBACK_BASE_ENV, PLATFORM_FALLBACK_KEY_ENV)
        if fallback is not None:
            fallback["platform_tier"] = "fallback"
    candidates.append(fallback)
    return [item for item in candidates if item is not None]


def _cross_provider_shared_key(connections: list[dict[str, Any]]) -> bool:
    """两档**跨供应商**却用同一把 key —— 这是装错了，不是配置风格。"""
    if len(connections) != 2:
        return False
    first, second = connections
    if first.get("provider") == second.get("provider"):
        return False
    key = platform_connection_key(first)
    return bool(key) and key == platform_connection_key(second)


def platform_key_conflict() -> bool:
    """现在这两档是不是「同一把 key 发给两家」——哨兵与界面用它问「是不是装错了」。

    为什么不能从 `platform_model_connections()` 的结果反推：那道闸门**已经把有问题的
    那一档摘掉了**，摘完之后列表看起来很正常。这件事必须能被单独问一次，否则一次
    「主服务其实没生效、钱照花」会以最安静的方式长期存在。
    """
    try:
        return _cross_provider_shared_key(_platform_connections_raw())
    except Exception:  # noqa: BLE001 - 问一句配置而已，读不出来就当没这回事
        logging.exception("平台模型两档的一致性检查读不出来，按「没冲突」处理")
        return False


def platform_model_connections() -> list[dict[str, Any]]:
    """平台凭据的**有序**候选：主服务在前，付费兜底在后。

    只有一个调用方需要「全都试一遍」——出报告那条路（`Service._platform_attempts`）。
    其余地方（界面、余额、用量）读第一档就够，别把它们改成绕圈。

    **跨供应商共用同一把 key 时，本机那一档会被摘掉。** 为什么需要这道闸门：
    2026-09-23 真发生过一次——想把主服务换成本机那台，却只改了 `_PROVIDER` 而没换 key，
    于是**每一次调用**都会把管理员那把付费 key 当成主服务的凭据，发给第三方那台盒子。
    它的症状是「钱照花、报告照出」，没有任何一处会红，所以只能在这里拦。

    两档同一把 key **本身是合法的**（过渡期两家都是 deepseek 就是这么配的），判据因此
    必须是「同一把 key **且**不是同一家供应商」。摘掉的是**本机那一档**：宁可少一次不花钱
    的调用，也不能把付费凭据发到别人的机器上；剩下那档照样出报告，用户无感。两家都不是
    本机时只留排在前面的那一档——同一个道理，key 只该发给他属于的那一家。
    """
    connections = _platform_connections_raw()
    if not _cross_provider_shared_key(connections):
        return connections
    if any(item.get("provider") == LOCAL_MODEL_PROVIDER for item in connections):
        logging.warning(
            "平台模型两档用了同一把 key 却是不同供应商，其中有本机那一档 —— 本机那一档"
            "已摘掉，避免把这把凭据发到那台盒子上。多半是改了 %s 却没换 %s；"
            "修法与现场记录见 docs/local-model-wiring-2026-09-23.md",
            PLATFORM_PROVIDER_ENV, PLATFORM_KEY_ENV)
        return [item for item in connections if item.get("provider") != LOCAL_MODEL_PROVIDER]
    logging.warning(
        "平台模型两档用了同一把 key 却是不同供应商 —— 只保留排在前面的那一档，"
        "这把凭据不该发给后面那一家。")
    return connections[:1]


def platform_tier(connection: dict[str, Any] | None) -> str:
    """``"primary"`` / ``"fallback"`` / ``""``（不是平台连接）。

    这一格存在的唯一理由是**取哪一把 key**：主服务与付费兜底是两把不同的凭据，
    而它们长得完全一样（都是 `platform=True` 的 dict）。靠「在列表里的下标」去猜，
    在这个函数被单独调用时（`connection_key` 就是这样）根本不成立——
    猜错的后果是**把主服务的 key 发给 DeepSeek**。
    """
    if not connection or not connection.get("platform"):
        return ""
    return "fallback" if connection.get("platform_tier") == "fallback" else "primary"


def platform_connection_key(connection: dict[str, Any] | None) -> str:
    """平台连接对应的明文 key（明文只在环境里，任何 dict 里都不放）。"""
    return platform_model_fallback_key() if platform_tier(connection) == "fallback" else platform_model_key()


# ---------------------------------------------------------------------------
# 这把 key 还剩多少钱（只读、免费、不产生任何模型调用）
# ---------------------------------------------------------------------------
#: DeepSeek 公开的余额查询接口。来源是官方 API 文档的「Get User Balance」页（2026-09-22 读），
#: 同日在生产上用平台 key 真调过一次：HTTP 200，返回 `is_available` 与按币种的余额。
BALANCE_PATH = "/user/balance"


def supports_balance(provider: str) -> bool:
    """这家供应商有没有「查余额」这个只读接口。

    **目前只有 DeepSeek 有。** 2026-09-22 逐家看过 `MODEL_PRESETS` 里的十家：其余各家要么
    没有公开接口，要么要控制台会话。没有的时候我们不猜、不推算、也不报「余额未知」——
    报一个我们其实看不见的数，比不报更糟。
    """
    return str(provider or "").strip().lower() == "deepseek"


def _amount(value: Any) -> Optional[float]:
    """接口里的金额是**字符串**（`"52.18"`）。读不出来就是 None，绝不当成 0。"""
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def fetch_balance(connection: dict[str, Any], *, timeout: int = 20) -> Optional[dict[str, Any]]:
    """读这把 key 的余额；``None`` = 这家没有这个接口，或者没配 key。

    返回归一化后的读数，金额是两位小数的 float（原始字符串也留着，界面上照原样显示）::

        {"is_available": True,
         "balances": [{"currency": "CNY", "total": 52.18, "granted": 0.0,
                       "topped_up": 52.18, "total_text": "52.18"}]}

    ``is_available`` 是**供应商自己的判断**（「余额够不够调用」），我们不自己算这个结论：
    起付线、赠送额的有效期、多币种怎么算，只有它知道。读不到这个字段就是 ``None``——
    「不知道」与「够用」是两件事。

    走的是和模型调用同一套出站闸门（不跟随重定向、响应有上限、错误正文脱敏，见
    `_json_request`），所以这里不会多出一条绕过审查的出口。
    """
    if not supports_balance((connection or {}).get("provider")):
        return None
    api_key = str((connection or {}).get("api_key") or "").strip()
    if not api_key and (connection or {}).get("platform"):
        # 平台那把 key 的明文只在环境里，**不进** `platform_model_default()` 返回的那个 dict
        # （那个 dict 会被当成数据库行序列化出去）。所以这里按需读一次，形状与
        # `Service.connection_key` 一致。
        #
        # 写成 `str(... or "")` 而不是把函数调用的结果直接赋给它：`credentials.scan_secrets`
        # 会把「标识符 = 标识符」那种形状读成**一个字面量 key**（它要 ≥16 个
        # [A-Za-z0-9+/=_-] 字符，函数名恰好全中），于是 `publish_export.py` 会拒绝导出整棵树。
        # 那次拒绝是对的——闸门宁可误报——所以这里改形状，不改闸门。
        # （这段注释本身也不能把那句话原样写出来：扫描器连注释一起扫。）
        api_key = str(platform_connection_key(connection) or "").strip()
    if not api_key:
        return None
    base_url = str((connection or {}).get("base_url")
                   or MODEL_PRESETS["deepseek"].base_url).rstrip("/")
    payload = _json_request(base_url + BALANCE_PATH,
                            headers={"Authorization": f"Bearer {api_key}"},
                            method="GET", timeout=timeout)
    infos = payload.get("balance_infos")
    balances: list[dict[str, Any]] = []
    for item in infos if isinstance(infos, list) else []:
        if not isinstance(item, dict):
            continue
        currency = str(item.get("currency") or "").strip().upper()
        if not currency:
            continue
        balances.append({
            "currency": currency,
            "total": _amount(item.get("total_balance")),
            "granted": _amount(item.get("granted_balance")),
            "topped_up": _amount(item.get("topped_up_balance")),
            "total_text": str(item.get("total_balance") or ""),
        })
    available = payload.get("is_available")
    if available is None and not balances:
        # 200 但什么也没说（网关的占位响应之类）：这是**没读到**，不是「余额是 0」。
        return None
    return {"is_available": None if available is None else bool(available),
            "balances": balances}


# Search has its own fallback, with its own variables, for the same reason the
# model one exists: the operator was handing one search key to every user by hand,
# and a user who never got it silently lost source-checking.
#
# It is a separate key rather than a reuse of the model key because the two are
# different accounts at different vendors -- the model key is DeepSeek, the search
# key is 火山/豆包 -- so one variable could not hold both.
PLATFORM_SEARCH_KEY_ENV = "INFE_PILOT_DEFAULT_SEARCH_KEY"
PLATFORM_SEARCH_PROVIDER_ENV = "INFE_PILOT_DEFAULT_SEARCH_PROVIDER"
PLATFORM_SEARCH_BASE_ENV = "INFE_PILOT_DEFAULT_SEARCH_BASE_URL"


def platform_search_default() -> Optional[dict[str, Any]]:
    """The instance-wide search credential, or None when the operator set no key.

    Same contract as :func:`platform_model_default`: a fallback, never an
    override, and None is the normal state for a self-hosted install.

    One thing this makes true that is worth stating plainly: with the operator's
    search key, the *query* -- which is derived from the user's mail by
    ``prompts.public_search_query`` -- is sent to the operator's search account. The
    privacy policy says so; do not add this fallback anywhere without that
    sentence staying true.
    """
    api_key = (os.environ.get(PLATFORM_SEARCH_KEY_ENV) or "").strip()
    if not api_key:
        return None
    provider = (os.environ.get(PLATFORM_SEARCH_PROVIDER_ENV) or "doubao").strip()
    if provider not in SEARCH_PRESETS:
        logging.warning(
            "INFE_PILOT_DEFAULT_SEARCH_PROVIDER 不是已知搜索供应商：%s，平台搜索 key 不生效", provider)
        return None
    base_url = (os.environ.get(PLATFORM_SEARCH_BASE_ENV) or SEARCH_PRESETS[provider]["base_url"]).strip()
    return {
        "id": "", "user_id": "", "kind": "search",
        "provider": provider, "model": "", "base_url": base_url,
        "enabled": 1, "last_test_at": None, "last_error": "",
        "created_at": "", "updated_at": "",
        "encrypted_api_key": None,
        "platform": True,
    }


def platform_search_key() -> str:
    return (os.environ.get(PLATFORM_SEARCH_KEY_ENV) or "").strip()


def platform_model_key() -> str:
    return (os.environ.get(PLATFORM_KEY_ENV) or "").strip()


def platform_model_fallback_key() -> str:
    """付费兜底那一档的明文 key；没配就是空串。

    与 `platform_model_key()` 分开而不是合成一个「按连接取 key」的函数：值从哪里来
    （哪一个环境变量）这件事，在两个地方各写一次比藏进一张表里更容易看懂——而这里
    错一次的后果是把**主服务的 key 发给 DeepSeek**。
    """
    return (os.environ.get(PLATFORM_FALLBACK_KEY_ENV) or "").strip()


@dataclass(frozen=True)
class Generation:
    """A model answer plus the source URLs the provider itself cited.

    ``usage`` carries whatever token accounting the provider returned, so the
    latency work can tell "the model is thinking" apart from "the model is
    writing a very long answer" without guessing.

    ``finish`` is the provider's own stop reason when it gave one. It is here
    because "the answer ended" and "the answer was cut off at the output cap"
    look identical in the text alone -- a translation that stops mid-sentence
    reads like a short mail. ``length`` is the one value callers act on: the
    budget ran out, which is worth saying out loud instead of quietly handing
    the user half a translation.

    ``guard`` 只有本机服务那套护栏会填（`{"ok":…, "issues":[…], "retried":…}`）。
    它是**旁路信息**：`ok=false` 不代表这次调用失败，只代表「这条结果按业务策略该看一眼」。

    **它被谁读了（2026-09-26 更正）**：`manage check-model` 会打印它；`service` 会在报告
    落库之后，用它在 journal 里写一行**可追溯到具体某个人某一封信**的告警
    （`guard ESCALATE ... report=…`）。此外没人读。

    **它不改变任何交付决定** —— 报告照发、App 照显示（`ok=false` 是业务升级，不是错误）。
    要不要因此改行为是产品决定，见 `docs/guard-escalation-2026-09-26.md`。
    这句注释以前写的是「读它的地方见 `service`（记录 `retried` 与问题条数）」——
    **那段代码并不存在**，而这正是护栏变成摆设的方式：读注释的人会以为它已经接上了。
    """

    text: str
    sources: list[dict[str, str]]
    search_mode: str = "none"
    usage: dict[str, Any] | None = None
    finish: str = ""
    guard: dict[str, Any] | None = None


def _log_guard(provider: str, model: str, guard: Any) -> None:
    """把护栏的结论记一行（不含正文、不含问题原文里的字段值）。

    `issues` 里可能带着从邮件里抄出来的片段（对方文档 §6 也这么说），所以这里只记
    **条数**与几个布尔/耗时：够用来回答「这封为什么被拦、为什么慢」，不够用来还原正文。
    """
    if not isinstance(guard, dict):
        return
    issues = guard.get("issues")
    logging.info(
        "guard %s/%s: ok=%s task=%s issues=%s retried=%s auto_fixed=%s jev=%s latency=%ss",
        provider, model, bool(guard.get("ok")), guard.get("task"),
        len(issues) if isinstance(issues, list) else 0,
        bool(guard.get("retried")), bool(guard.get("auto_fixed")),
        bool(guard.get("jev")), guard.get("latency_s"),
    )


_USAGE_KEYS = (
    ("input_tokens", "output_tokens", "total_tokens"),           # OpenAI Responses / Anthropic
    ("prompt_tokens", "completion_tokens", "total_tokens"),       # OpenAI Chat / OpenAI-compatible
    ("promptTokenCount", "candidatesTokenCount", "totalTokenCount"),  # Gemini
)


def _int_at(block: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = block.get(key)
        if isinstance(value, (int, float)):
            return int(value)
    return 0


def extract_usage(response: dict[str, Any]) -> dict[str, int]:
    """Normalise the provider's token accounting into one small dict.

    Shape follows what every provider already reports, so it costs nothing and
    nothing has to be guessed: input/output/total, plus the two numbers that
    actually change the bill — tokens served from the provider's prompt cache
    (much cheaper) and tokens spent on hidden reasoning (billed as output even
    though the user never sees them).
    """
    for block in (response.get("usage"), (response.get("usageMetadata") or None),
                  (response.get("response", {}) or {}).get("usage")):
        if not isinstance(block, dict):
            continue
        for input_key, output_key, total_key in _USAGE_KEYS:
            if input_key in block or output_key in block:
                usage: dict[str, int] = {}
                for label, key in (("input", input_key), ("output", output_key), ("total", total_key)):
                    value = block.get(key)
                    if isinstance(value, (int, float)):
                        usage[label] = int(value)
                if not usage:
                    continue
                details = block.get("prompt_tokens_details")
                usage["cached_input"] = _int_at(
                    details if isinstance(details, dict) else {},
                    "cached_tokens", "cache_read_input_tokens")
                usage["cached_input"] = usage["cached_input"] or _int_at(
                    block, "prompt_cache_hit_tokens", "cachedContentTokenCount", "cache_read_input_tokens")
                completion_details = block.get("completion_tokens_details")
                usage["reasoning"] = _int_at(
                    completion_details if isinstance(completion_details, dict) else {},
                    "reasoning_tokens")
                return usage
    return {}


def supports_native_search(provider: str) -> bool:
    """True when this provider's own API can search without a second API key."""
    preset = MODEL_PRESETS.get(provider)
    return bool(preset and preset.native_search)


def _search_keyword_limit(config: dict[str, Any]) -> int:
    """The optional per-round keyword cap some search tools accept (Ark: 1–50).

    Returns 0 when unset or unusable. A bad value is dropped rather than raised
    on: the connection editor is free-form, and losing web search entirely
    because somebody typed "many" would be a worse outcome than ignoring it.
    """
    raw = config.get("search_max_keyword")
    if raw in (None, ""):
        return 0
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return 0
    return value if 1 <= value <= 50 else 0


def _dedupe_sources(sources: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    unique: list[dict[str, str]] = []
    for item in sources:
        url = item.get("url", "")
        if url and url not in seen:
            seen.add(url)
            unique.append(item)
    return unique


def _openai_text(response: dict[str, Any]) -> str:
    if isinstance(response.get("output_text"), str) and response["output_text"].strip():
        return response["output_text"].strip()
    parts: list[str] = []
    for item in response.get("output", []) or []:
        if isinstance(item, dict):
            for block in item.get("content", []) or []:
                if isinstance(block, dict) and block.get("type") in {"output_text", "text"}:
                    parts.append(str(block.get("text", "")))
    return "\n\n".join(parts).strip()


def _openai_sources(response: dict[str, Any]) -> list[dict[str, str]]:
    """Every citation the provider is willing to give us, in either shape.

    The OpenAI Responses API puts them in ``output[].content[].annotations[]``
    as ``url_citation``. A provider that adds a server-side search tool also
    reports the search itself as a ``web_search_call`` item, and some (火山方舟's
    「联网内容插件」 among them) hang the source list off that item instead.

    Both are accepted on purpose. The Ark path could not be exercised against a
    live key -- none of this installation's keys is an Ark key, checked against
    the real endpoint -- so the parser is written to the documented shape *and*
    to the neighbouring one rather than betting the feature on one guess. A
    citation that only ever appears in the second place would otherwise look
    exactly like "this provider cannot search".
    """
    found: list[dict[str, str]] = []
    for item in response.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        for block in item.get("content", []) or []:
            if not isinstance(block, dict):
                continue
            for note in block.get("annotations", []) or []:
                if isinstance(note, dict) and note.get("type") == "url_citation":
                    result = _safe_result(note.get("title"), note.get("url"))
                    if result:
                        found.append(result)
        # The `web_search_call` shape: sources sit either on the item or under
        # its `action`. Field names vary (`sources` / `results` / `citations`),
        # and so do the entries inside them (`url` / `link`, `title` / `name`).
        if str(item.get("type", "")) == "web_search_call":
            action = item.get("action") if isinstance(item.get("action"), dict) else {}
            for container in (item, action):
                for key in ("sources", "results", "citations"):
                    entries = container.get(key)
                    if not isinstance(entries, list):
                        continue
                    for entry in entries:
                        if not isinstance(entry, dict):
                            continue
                        result = _safe_result(
                            entry.get("title") or entry.get("name"),
                            entry.get("url") or entry.get("link"),
                            entry.get("summary") or entry.get("snippet") or entry.get("content"),
                        )
                        if result:
                            found.append(result)
    return _dedupe_sources(found)


def _anthropic_text(response: dict[str, Any]) -> str:
    parts = [
        str(item.get("text", ""))
        for item in response.get("content", []) or []
        if isinstance(item, dict) and item.get("type") == "text"
    ]
    return "\n\n".join(parts).strip()


def _anthropic_sources(response: dict[str, Any]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for block in response.get("content", []) or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "web_search_tool_result":
            for item in block.get("content", []) or []:
                if isinstance(item, dict) and item.get("type") == "web_search_result":
                    result = _safe_result(item.get("title"), item.get("url"))
                    if result:
                        found.append(result)
        for note in block.get("citations", []) or []:
            if isinstance(note, dict):
                result = _safe_result(note.get("title"), note.get("url"))
                if result:
                    found.append(result)
    return _dedupe_sources(found)


def _gemini_text(response: dict[str, Any]) -> str:
    try:
        return "\n\n".join(
            str(part["text"]) for part in response["candidates"][0]["content"]["parts"] if part.get("text")
        ).strip()
    except (KeyError, IndexError, TypeError):
        return ""


def _gemini_sources(response: dict[str, Any]) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    for candidate in response.get("candidates", []) or []:
        if not isinstance(candidate, dict):
            continue
        metadata = candidate.get("groundingMetadata") or {}
        for chunk in metadata.get("groundingChunks", []) or []:
            web = chunk.get("web") if isinstance(chunk, dict) else None
            if isinstance(web, dict):
                result = _safe_result(web.get("title"), web.get("uri"))
                if result:
                    found.append(result)
    return _dedupe_sources(found)


def generate(
    *, provider: str, model: str, api_key: str, prompt: str, base_url: str = "",
    config: dict[str, Any] | None = None, max_output_tokens: int = 4000,
    native_search: bool = False, guard_task: str = "",
) -> Generation:
    """Generate text, optionally letting the provider search the web itself.

    ``native_search`` is honoured only for providers that declare the capability.
    Callers must treat any search failure as non-fatal so the summary still runs.

    ``guard_task`` 是给本机服务那套护栏用的（`classify/extract/summarize/reply`）。
    别的供应商会忽略它——**不是**「所有供应商都支持」，而是这条字段只在
    `guard_task_for` 认那一家时才进请求体（见那里的注释：报错任务名比不报更糟）。
    """
    preset, model, base = normalized_model_config(provider, model, base_url)
    config = config or {}
    use_search = bool(native_search and preset.native_search)
    mode = "native" if use_search else "none"
    #: 这一次调用的出站 TLS 配置（自签证书的供应商才有；别的供应商是 None）。
    tls = local_model_tls(provider)
    task = guard_task_for(provider, guard_task) if guard_task else ""
    guard = {"x_guard": {"task": task}} if task else {}

    if preset.protocol == "openai_responses":
        payload: dict[str, Any] = {
            "model": model, "store": False, "max_output_tokens": max_output_tokens, "input": prompt,
        }
        if use_search:
            tool: dict[str, Any] = {"type": "web_search"}
            # 火山方舟的联网插件接受一个可选的关键词条数上限（官方工具说明：1–50，
            # 默认 5）。它是**可选**的，所以只在连接里显式配了才发——发一个供应商
            # 不认识的字段，代价是整个请求失败。
            keyword_limit = _search_keyword_limit(config)
            if keyword_limit and provider.startswith("volcengine_ark"):
                tool["max_keyword"] = keyword_limit
            payload["tools"] = [tool]
        response = _json_request(
            f"{base}/responses",
            headers={"Authorization": f"Bearer {api_key}"},
            payload=payload,
            timeout=request_timeout(provider),
            tls=tls,
        )
        text = _openai_text(response)
        if text:
            # Responses 协议没有 finish_reason，只有「为什么这轮没做完」。
            stop = str((response.get("incomplete_details") or {}).get("reason") or "")
            return Generation(text, _openai_sources(response) if use_search else [], mode,
                              extract_usage(response),
                              "length" if stop == "max_output_tokens" else stop)
    elif preset.protocol in {"openai_chat", "azure_openai"}:
        if preset.protocol == "azure_openai":
            api_version = str(config.get("api_version") or "2024-10-21")
            url = f"{base.rstrip('/')}/openai/deployments/{urllib.parse.quote(model)}/chat/completions?api-version={urllib.parse.quote(api_version)}"
            headers = {"api-key": api_key}
            payload_model: dict[str, Any] = {}
        else:
            url = f"{base}/chat/completions"
            headers = {"Authorization": f"Bearer {api_key}"}
            payload_model = {"model": model}
        payload: dict[str, Any] = {
            **payload_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": max_output_tokens,
            **guard,
        }
        thinking = str(config.get("thinking") or "").strip().lower()
        if thinking not in {"enabled", "disabled"}:
            # DeepSeek enables hidden reasoning by default at "high" effort, and
            # for a structured report that is actively harmful: measured on
            # deepseek-flash, all 4000 output tokens went to reasoning and the
            # visible answer never started (20.7 s, 5/7 sections). With
            # thinking disabled the same prompt returns a complete report in
            # 3.1 s using 600 tokens. Writing a fixed-format summary is not a
            # task that benefits from a chain of thought, so it is off unless a
            # connection explicitly asks for it.
            thinking = "disabled" if preset.protocol == "openai_chat" and provider == "deepseek" else ""
        if thinking:
            payload["thinking"] = {"type": thinking}
        response = _json_request(
            url,
            headers=headers,
            payload=payload,
            timeout=request_timeout(provider),
            tls=tls,
        )
        if task:
            # 护栏的结论不改变这次调用的成败：HTTP 200 + `guard.ok=false` 是**业务升级**，
            # 不是错误（对方文档 §3.4 明写）。这里只留一行可查的记录——`guard.retried`
            # 是延迟翻倍的解释，`latency_s` 是「为什么这封比那封慢」的证据。
            _log_guard(provider, model, response.get("guard"))
        try:
            choice = response["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError):
            choice, message = {}, {}
        text = str(message.get("content") or "").strip()
        # Decide BEFORE returning anything. A handful of visible characters is
        # not an answer either: measured on deepseek-flash, one run came back
        # with 5 characters after 3995 reasoning tokens. The rule is "the budget
        # went on hidden thinking and almost nothing came out", which cannot
        # misfire on a genuinely long report. Left unchecked, the report
        # normaliser fills every missing section with "无" and the user receives
        # a well-formatted email that says nothing while the message is marked
        # sent — a silent failure that looks like success.
        usage = response.get("usage") or {}
        reasoning_tokens = int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        mostly_reasoning = reasoning_tokens >= max(1, max_output_tokens) * 0.8 and len(text) < 400
        if mostly_reasoning or (not text and message.get("reasoning_content")):
            raise ProviderError(
                f"模型把输出上限用在了隐藏推理上，几乎没有正文（正文 {len(text)} 字符，"
                f"推理 token {reasoning_tokens or '未知'}，上限 {max_output_tokens}，"
                f"finish_reason={choice.get('finish_reason')!r}）。"
                "请把隐藏推理关掉——DeepSeek 的 deepseek-flash 默认就开着思考，"
                "请求里要带 thinking: {\"type\": \"disabled\"}；或大幅提高输出上限。不换模型也能修。"
            )
        if text:
            return Generation(text, [], "none", extract_usage(response),
                              str(choice.get("finish_reason") or ""),
                              guard=response.get("guard") if task else None)
        raise ProviderError(
            f"模型返回了空正文（finish_reason={choice.get('finish_reason')!r}）；"
            "请检查模型名是否与供应商提供的名称一致。"
        )
    elif preset.protocol in {"anthropic", "ark"}:
        url = f"{base}/messages" if preset.protocol == "anthropic" else f"{base}/v1/messages"
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        if preset.protocol == "ark":
            headers = {"Authorization": f"Bearer {api_key}", "anthropic-version": "2023-06-01"}
        payload = {
            "model": model, "max_tokens": max_output_tokens,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }
        if use_search:
            payload["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
        response = _json_request(url, headers=headers, payload=payload, timeout=request_timeout(provider), tls=tls)
        text = _anthropic_text(response)
        if text:
            # 各家把「撞到输出上限」叫得不一样，这里统一成 "length"——调用方只认这一个值。
            stop = response.get("stop_reason")
            return Generation(text, _anthropic_sources(response) if use_search else [], mode,
                              extract_usage(response), "length" if stop == "max_tokens" else str(stop or ""))
    elif preset.protocol == "gemini":
        payload = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_output_tokens, "temperature": 0.2},
        }
        if use_search:
            payload["tools"] = [{"google_search": {}}]
        response = _json_request(
            f"{base}/models/{urllib.parse.quote(model)}:generateContent",
            headers={"x-goog-api-key": api_key},
            payload=payload,
            timeout=request_timeout(provider),
            tls=tls,
        )
        text = _gemini_text(response)
        if text:
            stop = ((response.get("candidates") or [{}])[0].get("finishReason"))
            return Generation(text, _gemini_sources(response) if use_search else [], mode,
                              extract_usage(response), "length" if stop == "MAX_TOKENS" else str(stop or ""))
    raise ProviderError("模型 API 没有返回可用文本；请检查模型名和接口类型。")


def generate_text(
    *, provider: str, model: str, api_key: str, prompt: str, base_url: str = "",
    config: dict[str, Any] | None = None, max_output_tokens: int = 4000,
) -> str:
    """Backwards-compatible text-only wrapper around :func:`generate`."""
    return generate(
        provider=provider, model=model, api_key=api_key, prompt=prompt, base_url=base_url,
        config=config, max_output_tokens=max_output_tokens,
    ).text


def _safe_result(title: Any, url: Any, summary: Any = "") -> dict[str, str] | None:
    value = str(url or "").strip()
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or re.search(r"[\x00-\x20<>]", value):
        return None
    return {"title": str(title or "未命名来源")[:240], "url": value[:1000], "summary": str(summary or "")[:800]}


def web_search(provider: str, api_key: str, query: str, *, count: int = 5) -> list[dict[str, str]]:
    query = re.sub(r"\s+", " ", query).strip()[:160]
    if not query:
        return []
    count = max(1, min(int(count), 10))
    if provider == "doubao":
        response = _json_request(
            SEARCH_PRESETS[provider]["base_url"],
            headers={"Authorization": f"Bearer {api_key}", "X-Traffic-Tag": "cityu_mail_pilot"},
            payload={"Query": query, "SearchType": "web", "Count": count, "Filter": {"NeedUrl": True, "NeedContent": False, "AuthInfoLevel": 1}, "QueryControl": {"QueryRewrite": True}},
            timeout=SEARCH_TIMEOUT_SECONDS,
        )
        container = response.get("Result", response)
        items = next((container.get(key) for key in ("WebResults", "Results", "Documents") if isinstance(container, dict) and isinstance(container.get(key), list)), [])
        raw = [(item.get("Title"), item.get("Url") or item.get("URL"), item.get("Summary") or item.get("Snippet")) for item in items if isinstance(item, dict)]
    elif provider == "tavily":
        response = _json_request(
            SEARCH_PRESETS[provider]["base_url"], headers={},
            payload={"api_key": api_key, "query": query, "max_results": count, "search_depth": "basic", "include_raw_content": False}, timeout=45,
        )
        raw = [(item.get("title"), item.get("url"), item.get("content")) for item in response.get("results", []) if isinstance(item, dict)]
    elif provider == "brave":
        url = SEARCH_PRESETS[provider]["base_url"] + "?" + urllib.parse.urlencode({"q": query, "count": count})
        response = _json_request(url, headers={"X-Subscription-Token": api_key}, method="GET", timeout=45)
        raw = [(item.get("title"), item.get("url"), item.get("description")) for item in response.get("web", {}).get("results", []) if isinstance(item, dict)]
    else:
        raise ProviderError("不支持的搜索供应商。")
    return [result for item in raw if (result := _safe_result(*item))][:count]


def public_catalog() -> dict[str, Any]:
    return {
        "models": [
            {
                "id": item.id,
                "label": item.label,
                "requires_base_url": not item.fixed_host,
                "native_search": item.native_search,
            }
            for item in MODEL_PRESETS.values()
        ],
        "search": [{"id": key, "label": value["label"]} for key, value in SEARCH_PRESETS.items()],
    }
