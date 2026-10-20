"""⑥：供应商的错误形状与限流差异，**逐家显式分类**。

审查报告（`handoff/REVIEW-2026-09-22.md`）的下一轮范围里有一条「所有供应商错误形状与限流差异」。
当时的现状是：分类只有一行——`429 或 >=500` 算可重试。那对**按状态码报错**的家是对的
（Anthropic 的 529 过载、Gemini 的 503 都被 `>=500` 盖住），但漏了一类：

**HTTP 200、正文里却是错误**（`{"error": …}` 或 `{"type": "error"}`）。不认出来，
调用方会把它当成「模型返回了空正文」，于是**一次限流被记成永久失败、不重试**——
方向刚好反了。这个文件把各家的真实形状摆成一张表，一条一条钉住。
"""

import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest import mock

_TMP = tempfile.mkdtemp()
os.environ.setdefault("INFE_PILOT_MASTER_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
os.environ.setdefault("INFE_PILOT_COOKIE_SECURE", "0")

from pilot_app import providers  # noqa: E402


class _Body:
    """一个只回一段正文的假响应（_read_bounded 会分块读）。"""

    def __init__(self, payload: dict, status: int = 200):
        self.raw = json.dumps(payload).encode()
        self.status = status
        self._sent = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size: int = -1) -> bytes:
        if self._sent:
            return b""
        self._sent = True
        return self.raw


def _call(payload: dict):
    with mock.patch.object(providers, "_outbound_open", return_value=_Body(payload)):
        return providers._json_request("https://api.example.com/v1/chat", headers={}, payload={"a": 1})


class BodyErrorClassificationTests(unittest.TestCase):
    """**200 里的错误**：形状各不相同，分类要一致。"""

    TRANSIENT = {
        "OpenAI 限流": {"error": {"message": "Rate limit reached for gpt-4o", "type": "rate_limit_exceeded"}},
        "OpenAI 配额": {"error": {"message": "You exceeded your current quota", "code": "insufficient_quota"}},
        "Anthropic 过载（200 里报）": {"type": "error", "error": {"type": "overloaded_error",
                                                                "message": "Overloaded"}},
        "Gemini 资源耗尽": {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                      "message": "Quota exceeded"}},
        "Gemini 服务不可用": {"error": {"code": 503, "status": "UNAVAILABLE", "message": "try again later"}},
        "通用 500": {"error": {"code": 500, "message": "internal"}},
        "只说稍后再试": {"error": {"message": "Server is temporarily unavailable, please try again"}},
    }

    PERMANENT = {
        "OpenAI 认证失败": {"error": {"message": "Incorrect API key provided", "type": "invalid_request_error"}},
        "OpenAI 模型名不存在": {"error": {"message": "The model `gpt-9` does not exist", "code": "model_not_found"}},
        "Gemini 请求非法": {"error": {"code": 400, "status": "INVALID_ARGUMENT", "message": "bad request"}},
        "Anthropic 参数错": {"type": "error", "error": {"type": "invalid_request_error",
                                                        "message": "max_tokens: must be >= 1"}},
    }

    def test_transient_shapes_are_retryable(self):
        for label, payload in self.TRANSIENT.items():
            with self.subTest(label):
                with self.assertRaises(providers.TransientProviderError) as caught:
                    _call(payload)
                self.assertIn("HTTP 200", str(caught.exception))

    def test_permanent_shapes_are_not_retried(self):
        for label, payload in self.PERMANENT.items():
            with self.subTest(label):
                with self.assertRaises(providers.ProviderError) as caught:
                    _call(payload)
                self.assertNotIsInstance(caught.exception, providers.TransientProviderError,
                                         f"{label} 不该被当成可重试")

    def test_a_successful_body_is_untouched(self):
        """别把正常响应误判成错误——上面那两条的新写法最容易犯的就是这个错。"""
        payload = {"choices": [{"message": {"content": "ok"}}], "usage": {"total_tokens": 3}}
        self.assertEqual(_call(payload), payload)
        self.assertEqual(_call({"output_text": "ok"}), {"output_text": "ok"})

    def test_the_key_is_scrubbed_from_these_messages_too(self):
        """这条路上的错误文本同样会落库，所以它也要过脱敏。"""
        fake = "sk-fixture-bodyerr0000"
        with self.assertRaises(providers.ProviderError) as caught:
            _call({"error": {"message": f"Incorrect API key provided: {fake}"}})
        self.assertNotIn(fake, str(caught.exception))


class StatusCodeClassificationTests(unittest.TestCase):
    """按状态码报错的家：这一层是原来的行为，别在改上面那条时弄坏。"""

    def _http_error(self, code: int, body: bytes = b'{"error": {"message": "boom"}}'):
        return urllib.error.HTTPError("https://api.example.com/v1/chat", code, "err", {}, io.BytesIO(body))

    def test_429_and_5xx_are_transient(self):
        for code in (429, 500, 502, 503, 529):        # 529 = Anthropic 的过载
            with self.subTest(code):
                with mock.patch.object(providers, "_outbound_open", side_effect=self._http_error(code)):
                    with self.assertRaises(providers.TransientProviderError):
                        providers._json_request("https://api.example.com/v1/chat", headers={}, payload={"a": 1})

    def test_4xx_is_permanent(self):
        for code in (400, 401, 403, 404, 422):
            with self.subTest(code):
                with mock.patch.object(providers, "_outbound_open", side_effect=self._http_error(code)):
                    with self.assertRaises(providers.ProviderError) as caught:
                        providers._json_request("https://api.example.com/v1/chat", headers={}, payload={"a": 1})
                    self.assertNotIsInstance(caught.exception, providers.TransientProviderError)
