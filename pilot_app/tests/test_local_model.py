"""本机大模型服务（主服务）接入的测试。

这一套盯的是**接入的形状**，不是对方服务本身：预设长什么样、地址从哪儿取、
自签证书与指纹怎么办、护栏任务名有没有带上、以及「主服务挂了谁接手」。

对方那台机器不在 CI 里，也永远不会在——所以这里一条网络断言都没有：
「服务活着吗」由 `manage check-localmodel` 在真机上回答（见 docs/local-model-2026-09-22.md）。
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import ssl
import tempfile
import threading
import unittest
from unittest import mock

from pilot_app import providers


def _make_self_signed(directory: str, common_name: str = "127.0.0.1") -> tuple[str, str, str]:
    """造一张自签证书，返回 (cert.pem, key.pem, SHA-256 指纹)。

    只在装了 `cryptography` 的环境里可用（requirements.txt 里就有，但它是为了别的用途）。
    没有它就跳过这一组——**不**用仓库里那张真证书来跑测试：那张属于生产接入，
    拿它当夹具会让「指纹钉扎」这件事在证书续期那天变成一堆红。
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import datetime as dt
    import ipaddress

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName([
            x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            x509.DNSName("localhost"),
        ]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path = os.path.join(directory, "cert.pem")
    key_path = os.path.join(directory, "key.pem")
    with open(cert_path, "wb") as handle:
        handle.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as handle:
        handle.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption()))
    fingerprint = cert.fingerprint(hashes.SHA256()).hex().upper()
    return cert_path, key_path, fingerprint


class _Handler(http.server.BaseHTTPRequestHandler):
    """一个只会答 200 + 一行 JSON 的替身服务。"""

    def do_POST(self):  # noqa: N802 - stdlib 的命名
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        body = json.dumps({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # 静音
        pass


class PinnedTlsTests(unittest.TestCase):
    """自签证书 + 指纹钉扎：这是这次接入真正的安全边界。"""

    @classmethod
    def setUpClass(cls):
        if shutil.which("openssl") is None:
            raise unittest.SkipTest("需要 openssl 或 cryptography")
        try:
            import cryptography  # noqa: F401
        except ImportError:  # pragma: no cover - 生产依赖里有，CI 也有
            raise unittest.SkipTest("没有 cryptography，跳过自签证书这一组")
        cls.tmp = tempfile.mkdtemp(prefix="localmodel-tls-")
        cls.cert, cls.key, cls.fingerprint = _make_self_signed(cls.tmp)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cls.cert, cls.key)
        cls.server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        providers.reset_tls_cache()

    def _connect(self, *, pin: str, port: int | None = None):
        connection = providers._PinnedHTTPSConnection(
            "127.0.0.1", port or self.port, ca_file=self.cert, pin=pin, timeout=5)
        connection.connect()
        try:
            connection.request("POST", "/v1/chat/completions", body=b"{}")
            return connection.getresponse().status
        finally:
            connection.close()

    def test_a_matching_pin_is_accepted(self):
        """指纹对得上就该通——否则「钉扎」变成「谁都用不了」。"""
        self.assertEqual(self._connect(pin=self.fingerprint), 200)

    def test_the_pin_is_compared_regardless_of_formatting(self):
        """冒号/大小写不该让人重新签一张证书。"""
        spaced = ":".join(self.fingerprint[i:i + 2] for i in range(0, len(self.fingerprint), 2))
        self.assertEqual(self._connect(pin=spaced.lower()), 200)

    def test_a_different_certificate_is_refused(self):
        """**反向验证**：把钉扎换成另一张证书的指纹，必须连不上。

        这一条是这次接入存在的理由：隧道是租来的，域名不是我们的。只信任「一张自签
        证书」而不管它是哪一张，等于把请求正文和 `Authorization` 交给任何能让那个域名
        解析到自己机器上的人。
        """
        other = "AA" * 32
        with self.assertRaises(ssl.SSLError):
            self._connect(pin=other)

    def test_json_request_carries_the_pin_through(self):
        """`_json_request(tls=…)` 这一路必须真的走到钉扎连接上。

        走通了（200）算通过：如果 `tls` 在传递途中被丢掉，请求会退回系统信任库，
        而这张自签证书不在里面——结果是握手失败，不是「悄悄不校验」。
        """
        payload = providers._json_request(
            f"https://127.0.0.1:{self.port}/v1/chat/completions",
            headers={"Authorization": "Bearer test"},
            payload={"messages": []},
            tls={"ca_file": self.cert, "pin": self.fingerprint},
            timeout=5,
        )
        self.assertEqual(payload["choices"][0]["message"]["content"], "ok")

    def test_a_wrong_pin_is_not_treated_as_a_network_hiccup(self):
        """指纹不对必须是**永久失败**，不能是「稍后重试」。

        `TransientProviderError` 会让 `_generate_with_retry` 换下一档重试、并让队列退避
        重试——围着一道永远过不去的门空转，还把「这个地址上应答的不是我们的服务」
        说成「网络不好」。
        """
        with self.assertRaises(providers.ProviderError) as caught:
            providers._json_request(
                f"https://127.0.0.1:{self.port}/v1/chat/completions",
                headers={"Authorization": "Bearer test"},
                payload={"messages": []},
                tls={"ca_file": self.cert, "pin": "AA" * 32},
                timeout=5,
            )
        self.assertNotIsInstance(caught.exception, providers.TransientProviderError)
        self.assertIn("TLS", str(caught.exception))

    def test_the_default_path_does_not_trust_it(self):
        """不给 `tls` 时必须失败：这张自签证书**不是**系统信任库的一部分。

        没有这一条，一个「顺手把额外 CA 塞进全局 context」的改动会让所有供应商都
        多信一张家宽机器上的自签证书，而测试全绿。
        """
        with self.assertRaises(providers.ProviderError):
            providers._json_request(
                f"https://127.0.0.1:{self.port}/v1/chat/completions",
                headers={"Authorization": "Bearer test"},
                payload={"messages": []},
                timeout=5,
            )


class PresetTests(unittest.TestCase):
    def test_the_local_provider_is_a_fixed_host_with_its_own_tls(self):
        preset = providers.MODEL_PRESETS["local_openai"]
        self.assertTrue(preset.fixed_host, "用户不该能改本机服务的地址")
        self.assertEqual(preset.ca_file, "certs/localmodel.pem")
        self.assertEqual(preset.protocol, "openai_chat")
        self.assertTrue(preset.base_env, "地址要能从环境变量改，否则换隧道就得改代码")
        self.assertEqual(len(providers.normalize_fingerprint(preset.pinned_fingerprint)), 64)

    def test_the_bundled_certificate_matches_the_pinned_fingerprint(self):
        """仓库里那张证书必须就是钉扎的那一张。

        两处分开存（文件给人看、指纹给代码用）时最容易出现「换了文件忘了改指纹」，
        而那一天的现场是**所有人一封报告都收不到**、原因写着「对端不是我们的服务」。
        这里当场把它算一遍。
        """
        import hashlib

        path = providers.local_model_cert_path()
        with open(path, "rb") as handle:
            pem = handle.read().decode()
        der = ssl.PEM_cert_to_DER_cert(pem)
        digest = hashlib.sha256(der).hexdigest().upper()
        self.assertEqual(
            providers.normalize_fingerprint(digest),
            providers.normalize_fingerprint(providers.MODEL_PRESETS["local_openai"].pinned_fingerprint))

    def test_the_base_url_can_be_moved_by_the_operator(self):
        with mock.patch.dict(os.environ, {"INFE_PILOT_LOCAL_MODEL_BASE_URL": "https://box.example:8443/v1"}):
            self.assertEqual(
                providers.normalized_model_config("local_openai", "ternary-bonsai-2-27b")[2],
                "https://box.example:8443/v1")

    def test_a_user_supplied_base_url_is_still_ignored(self):
        """固定主就是固定主：连接行里填的地址对这家不生效。"""
        self.assertEqual(
            providers.normalized_model_config("local_openai", "ternary-bonsai-2-27b", "https://evil.example/v1")[2],
            providers.MODEL_PRESETS["local_openai"].base_url)

    def test_only_the_local_provider_gets_the_extra_trust(self):
        self.assertIsNone(providers.local_model_tls("deepseek"))
        self.assertIsNotNone(providers.local_model_tls("local_openai"))

    def test_a_bare_fingerprint_is_not_mistaken_for_a_pin(self):
        """"短/长不对的指纹"要报成「读不出来」，不能当成一张真指纹去比。"""
        self.assertEqual(providers.normalize_fingerprint("AABB"), "")
        self.assertEqual(providers.normalize_fingerprint("not a fingerprint"), "")


class GuardTaskTests(unittest.TestCase):
    def test_the_guard_task_only_rides_the_local_provider(self):
        self.assertEqual(providers.guard_task_for("local_openai", "summarize"), "summarize")
        self.assertEqual(providers.guard_task_for("deepseek", "summarize"), "")

    def test_an_unknown_task_is_refused_loudly(self):
        with self.assertRaises(providers.ProviderError):
            providers.guard_task_for("local_openai", "translate")

    def test_the_payload_carries_x_guard_for_the_local_service(self):
        captured: dict = {}

        def fake(url, **kwargs):
            captured.update(kwargs)
            return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                    "guard": {"ok": True, "task": "summarize", "issues": [], "retried": False},
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

        with mock.patch.object(providers, "_json_request", fake):
            result = providers.generate(
                provider="local_openai", model="ternary-bonsai-2-27b", api_key="k",
                prompt="写一份周报", guard_task="summarize")
        self.assertEqual(captured["payload"]["x_guard"], {"task": "summarize"})
        self.assertEqual(captured["payload"]["grammar"], providers.LOCAL_MODEL_TEXT_GRAMMAR)
        self.assertEqual(captured["tls"], providers.local_model_tls("local_openai"))
        self.assertNotIn("stream", captured["payload"])
        self.assertEqual(result.guard["task"], "summarize")

    def test_a_plain_provider_payload_has_no_guard_field(self):
        captured: dict = {}

        def fake(url, **kwargs):
            captured.update(kwargs)
            return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

        with mock.patch.object(providers, "_json_request", fake):
            result = providers.generate(
                provider="deepseek", model="deepseek-flash", api_key="k",
                prompt="写一份周报", guard_task="summarize")
        self.assertNotIn("x_guard", captured["payload"])
        self.assertNotIn("grammar", captured["payload"])
        self.assertIsNone(captured["tls"])
        self.assertIsNone(result.guard)

    def test_local_plain_text_constraint_preserves_request_and_response(self):
        """All local tasks use Unicode text, without forcing JSON or streaming."""
        prompt = '## 合成 / Synthetic\n中文 日本語 한국어 😀\t{"actions":[]}'
        for task in ("", "classify", "extract", "summarize", "reply"):
            with self.subTest(task=task):
                response = {"choices": [{"message": {"content": prompt}, "finish_reason": "stop"}],
                            "usage": {"prompt_tokens": 3, "completion_tokens": 5}}
                with mock.patch.object(providers, "_json_request", return_value=response) as request:
                    result = providers.generate(provider="local_openai", model="ternary-bonsai-2-27b",
                        api_key="fixture-only", prompt=prompt, guard_task=task, max_output_tokens=2345)
                payload = request.call_args.kwargs["payload"]
                self.assertEqual(payload["messages"], [{"role": "user", "content": prompt}])
                self.assertEqual(payload["grammar"], r"root ::= [\t\n\r\x20-\uD7FF\uE000-\U0010FFFF]*")
                self.assertEqual(payload["max_tokens"], 2345)
                self.assertEqual(payload["temperature"], 0.2)
                self.assertNotIn("stream", payload)
                self.assertNotIn("response_format", payload)
                self.assertEqual(result.text, prompt)
                self.assertEqual(result.finish, "stop")
                if task:
                    self.assertEqual(payload["x_guard"], {"task": task})
                else:
                    self.assertNotIn("x_guard", payload)

    def test_grammar_does_not_leak_to_other_chat_providers(self):
        for provider in ("deepseek", "azure_openai"):
            with self.subTest(provider=provider):
                response = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
                with mock.patch.object(providers, "_json_request", return_value=response) as request:
                    providers.generate(provider=provider, model="fixture-model", api_key="fixture-only",
                        base_url="https://8.8.8.8/v1", prompt="Synthetic")
                self.assertNotIn("grammar", request.call_args.kwargs["payload"])


class PlatformChainTests(unittest.TestCase):
    """主服务在前、付费兜底在后——顺序就是「谁是主服务」的唯一答案。"""

    def setUp(self):
        self.saved = {key: os.environ.get(key) for key in (
            providers.PLATFORM_KEY_ENV, providers.PLATFORM_PROVIDER_ENV,
            providers.PLATFORM_MODEL_ENV, providers.PLATFORM_BASE_ENV,
            providers.PLATFORM_FALLBACK_KEY_ENV, providers.PLATFORM_FALLBACK_PROVIDER_ENV,
            providers.PLATFORM_FALLBACK_MODEL_ENV, providers.PLATFORM_FALLBACK_BASE_ENV,
        )}
        for key in self.saved:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _configure(self):
        os.environ.update({
            providers.PLATFORM_KEY_ENV: "local-service-key",
            providers.PLATFORM_PROVIDER_ENV: "local_openai",
            providers.PLATFORM_FALLBACK_KEY_ENV: "sk-paid",
            providers.PLATFORM_FALLBACK_PROVIDER_ENV: "deepseek",
        })

    def test_the_local_service_is_the_primary(self):
        self._configure()
        chain = providers.platform_model_connections()
        self.assertEqual([item["provider"] for item in chain], ["local_openai", "deepseek"])
        self.assertEqual(providers.platform_model_default()["provider"], "local_openai")

    def test_each_tier_reads_its_own_variable(self):
        """两把 key 不许串门：主服务那把发给 DeepSeek 是一次凭据泄漏。"""
        self._configure()
        primary, fallback = providers.platform_model_connections()
        self.assertEqual(providers.platform_connection_key(primary), "local-service-key")
        self.assertEqual(providers.platform_connection_key(fallback), "sk-paid")
        self.assertEqual(providers.platform_tier(primary), "primary")
        self.assertEqual(providers.platform_tier(fallback), "fallback")
        self.assertEqual(providers.platform_tier(None), "")
        self.assertEqual(providers.platform_tier({"user_id": "u"}), "")
        # 没标 tier 的平台连接按主服务算：`platform_model_default()` 从来不带那个字段。
        self.assertEqual(providers.platform_tier({"platform": True, "user_id": "u"}), "primary")

    def test_the_balance_gate_looks_at_the_paid_tier(self):
        """本机那台没有账户：余额这件事只对付费兜底成立。

        这一条盯的是一个**静默失效**：`budget` 照旧读「平台默认」时，读到的是本机服务
        （`supports_balance` 为假），于是「余额见底就不再调用」那道闸无声无息地没了。
        """
        self._configure()
        self.assertEqual(providers.metered_model_connection()["provider"], "deepseek")

    def test_without_a_fallback_key_the_chain_has_one_link(self):
        os.environ.update({
            providers.PLATFORM_KEY_ENV: "local-service-key",
            providers.PLATFORM_PROVIDER_ENV: "local_openai",
        })
        self.assertEqual([item["provider"] for item in providers.platform_model_connections()],
                         ["local_openai"])
        self.assertIsNone(providers.metered_model_connection())

    def test_a_fallback_with_no_key_never_becomes_a_candidate(self):
        os.environ.update({
            providers.PLATFORM_KEY_ENV: "local-service-key",
            providers.PLATFORM_PROVIDER_ENV: "local_openai",
            providers.PLATFORM_FALLBACK_PROVIDER_ENV: "deepseek",
        })
        self.assertEqual(len(providers.platform_model_connections()), 1)

    def test_an_unknown_provider_still_kills_that_tier_only(self):
        os.environ.update({
            providers.PLATFORM_KEY_ENV: "local-service-key",
            providers.PLATFORM_PROVIDER_ENV: "local_openai",
            providers.PLATFORM_FALLBACK_KEY_ENV: "sk-paid",
            providers.PLATFORM_FALLBACK_PROVIDER_ENV: "not-a-provider",
        })
        self.assertEqual([item["provider"] for item in providers.platform_model_connections()],
                         ["local_openai"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
