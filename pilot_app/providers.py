"""Lightweight BYOK model and web-search adapters for the pilot."""

from __future__ import annotations

import http.client
import json
import logging
import os
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Optional

from .security import SecurityError, validate_outbound_https_url

# Generating a full action-first bilingual report from a long email is slow: a
# measured Doubao run took ~176s per email and one real run was cut off at 240s,
# which is why the old 120s default failed on real mail. Override with
# INFE_PILOT_MODEL_TIMEOUT.
MODEL_TIMEOUT_SECONDS = int(os.environ.get("INFE_PILOT_MODEL_TIMEOUT", "300"))
SEARCH_TIMEOUT_SECONDS = int(os.environ.get("INFE_PILOT_SEARCH_TIMEOUT", "45"))


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
}


SEARCH_PRESETS = {
    "doubao": {"label": "豆包联网搜索", "base_url": "https://open.feedcoopapi.com/search_api/web_search"},
    "tavily": {"label": "Tavily", "base_url": "https://api.tavily.com/search"},
    "brave": {"label": "Brave Search", "base_url": "https://api.search.brave.com/res/v1/web/search"},
}


def _json_request(
    url: str,
    *,
    headers: dict[str, str],
    payload: dict[str, Any] | None = None,
    method: str = "POST",
    timeout: int = 120,
) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    request = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Accept": "application/json", **headers, **({"Content-Type": "application/json"} if body else {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            return json.loads(raw.decode()) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read(2048).decode(errors="replace")
        # Provider messages can help, but must never include the submitted key.
        message = f"API 返回 HTTP {exc.code}: {detail[:800]}"
        if exc.code == 429 or exc.code >= 500:
            raise TransientProviderError(message) from exc
        raise ProviderError(message) from exc
    except urllib.error.URLError as exc:
        raise TransientProviderError(f"无法连接 API：{exc.reason}") from exc
    except (TimeoutError, socket.timeout) as exc:
        # A bare read timeout is not wrapped in URLError, so it used to escape
        # as an opaque socket.error instead of an actionable provider failure.
        raise ProviderTimeout(f"接口响应超时（超过 {timeout} 秒没有返回）。稍后会自动重试。") from exc
    except http.client.RemoteDisconnected as exc:
        # A long generation can be cut off mid-flight by the provider or an
        # intermediary; this is exactly the case an automatic retry fixes.
        raise TransientProviderError("接口连接被中断（长回答可能超时）。稍后会自动重试。") from exc
    except http.client.HTTPException as exc:
        raise TransientProviderError(f"接口连接异常：{exc}") from exc
    except OSError as exc:
        raise TransientProviderError(f"网络错误：{exc}") from exc


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


def normalized_model_config(provider: str, model: str, base_url: str = "") -> tuple[ModelPreset, str, str]:
    if provider not in MODEL_PRESETS:
        raise ProviderError("不支持的模型供应商。")
    preset = MODEL_PRESETS[provider]
    model = official_model_name(provider, model.strip())
    if not model:
        raise ProviderError("必须填写模型或部署名称。")
    effective_base = preset.base_url if preset.fixed_host else (base_url.strip() or preset.base_url)
    if not effective_base:
        raise ProviderError("此供应商必须填写 API Base URL。")
    if not preset.fixed_host:
        effective_base = validate_outbound_https_url(effective_base)
    return preset, model, effective_base.rstrip("/")


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


def platform_model_default() -> Optional[dict[str, Any]]:
    """The instance-wide model connection, or None when the operator set no key.

    Returning None is the normal state for a self-hosted install: the software
    must work with every user bringing their own key. The pilot operator adding
    one is what turns the published "the operator pays during the pilot" line
    into something the code actually does, instead of something the operator
    performs by hand for each new account in the admin console.
    """
    api_key = (os.environ.get(PLATFORM_KEY_ENV) or "").strip()
    if not api_key:
        return None
    provider = (os.environ.get(PLATFORM_PROVIDER_ENV) or "deepseek").strip()
    if provider not in MODEL_PRESETS:
        logging.warning("INFE_PILOT_DEFAULT_MODEL_PROVIDER 不是已知供应商：%s，平台 key 不生效", provider)
        return None
    # deepseek is the provider this project has documented from the start, so its
    # model name is a known default. Anywhere else the name has to be spelled out:
    # sending "deepseek-flash" to OpenAI fails at the provider with an error that
    # says nothing about the real mistake, which is a setting missing here.
    #
    # ``deepseek-flash`` is DeepSeek's official name (read 2026-09-15: the quick
    # start and pricing pages list only deepseek-flash and deepseek-v4-pro, and
    # GET /models on the real API returns exactly those two). The name this code
    # used to default to, ``deepseek-chat``, is a legacy alias: it is still
    # accepted, but the response body comes back with "model": "deepseek-flash"
    # and the model list no longer contains it. Defaulting to a name the provider
    # has stopped documenting is how a working install quietly turns into a
    # broken one.
    model = (os.environ.get(PLATFORM_MODEL_ENV) or "").strip()
    if not model and provider != "deepseek":
        logging.warning(
            "设置了平台模型 key，但没有设置 INFE_PILOT_DEFAULT_MODEL_NAME（供应商 %s），平台 key 不生效",
            provider)
        return None
    if not model:
        model = "deepseek-flash"
    try:
        preset, model, base_url = normalized_model_config(
            provider, model, os.environ.get(PLATFORM_BASE_ENV) or "")
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
    """

    text: str
    sources: list[dict[str, str]]
    search_mode: str = "none"
    usage: dict[str, Any] | None = None
    finish: str = ""


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
    native_search: bool = False,
) -> Generation:
    """Generate text, optionally letting the provider search the web itself.

    ``native_search`` is honoured only for providers that declare the capability.
    Callers must treat any search failure as non-fatal so the summary still runs.
    """
    preset, model, base = normalized_model_config(provider, model, base_url)
    config = config or {}
    use_search = bool(native_search and preset.native_search)
    mode = "native" if use_search else "none"

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
            timeout=MODEL_TIMEOUT_SECONDS,
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
            timeout=MODEL_TIMEOUT_SECONDS,
        )
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
            return Generation(text, [], "none", extract_usage(response), str(choice.get("finish_reason") or ""))
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
        response = _json_request(url, headers=headers, payload=payload, timeout=MODEL_TIMEOUT_SECONDS)
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
            timeout=MODEL_TIMEOUT_SECONDS,
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
