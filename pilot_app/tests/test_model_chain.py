"""两档凭据的**整条链子**：谁先谁后、谁拿到哪把 key、故障时谁接手。

`test_local_model.py` 验的是接入的形状（预设、证书、钉扎、护栏任务）；
`test_platform_budget.py` / `test_service.py` 验的是钱的闸与熔断的分类。
这一份验的是**它们合起来**的那条路——从「这个账号有哪些候选」一直到
「真正发出去的那个 HTTP 请求里带的是哪把 key、去的是哪个地址」。

为什么值得单独一条：这条路最容易出的错**不是崩溃，而是安静地走错**——
把本机服务的 key 发到 DeepSeek、或者反过来把管理员那把 DeepSeek key 发到
一个我们只靠自签证书认识的地址上。两种都不会报错，只会记账错、或者凭据外流。
所以这里的断言落在**请求本身**上：URL、`Authorization`、`x_guard`，一个都不省。

用真库、真环境变量、真连接对象；只把最后那一步的网络换成一个记录器
（`providers._outbound_open` 就是为此留的唯一出口）。
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
import unittest
import urllib.request
from unittest import mock

from pilot_app import providers
from pilot_app.database import Database
from pilot_app.security import SecretBox, hash_password, token_hash
from pilot_app.service import PilotService

LOCAL_KEY = "local-service-key"
PAID_KEY = "sk-paid-fallback"


class _Recorder:
    """替掉 `_outbound_open`：记下每一次出站请求，然后按脚本回话。"""

    def __init__(self, answers: list[object]):
        self.requests: list[dict] = []
        self._answers = list(answers)

    def __call__(self, request: urllib.request.Request, *, timeout: int = 0, tls=None):
        body = json.loads(request.data.decode()) if request.data else {}
        self.requests.append({
            "url": request.full_url,
            "authorization": request.get_header("Authorization"),
            "payload": body,
            "tls": tls,
        })
        answer = self._answers.pop(0) if self._answers else RuntimeError("没有更多预设回话了")
        if isinstance(answer, Exception):
            raise answer
        return answer

    @property
    def urls(self) -> list[str]:
        return [item["url"] for item in self.requests]


class _Response:
    """`_json_request` 要的最小响应：上下文管理器 + 有上限的 read()。"""

    def __init__(self, payload: dict):
        self._raw = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, size: int = -1) -> bytes:
        chunk, self._raw = self._raw[:size] if size and size > 0 else self._raw, b""
        return chunk


def _answer(text: str = "## 1. 重点\nok", *, guard: dict | None = None) -> _Response:
    payload = {
        "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop", "index": 0}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    if guard is not None:
        payload["guard"] = guard
    return _Response(payload)


class ChainTestCase(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.db = Database(pathlib.Path(self.work.name) / "pilot.sqlite3")
        self.db.initialize()
        self.secrets = SecretBox.from_base64("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
        self.service = PilotService(self.db, self.secrets)
        self.saved = {key: os.environ.get(key) for key in (
            providers.PLATFORM_KEY_ENV, providers.PLATFORM_PROVIDER_ENV,
            providers.PLATFORM_MODEL_ENV, providers.PLATFORM_BASE_ENV,
            providers.PLATFORM_FALLBACK_KEY_ENV, providers.PLATFORM_FALLBACK_PROVIDER_ENV,
            providers.PLATFORM_FALLBACK_MODEL_ENV, providers.PLATFORM_FALLBACK_BASE_ENV,
            "INFE_PILOT_LOCAL_MODEL_BASE_URL", "INFE_PILOT_LOCAL_MODEL_CA_FILE",
            "INFE_PILOT_MODEL_ATTEMPTS",
        )}
        self.addCleanup(self._restore_env)
        for key in self.saved:
            os.environ.pop(key, None)

    def _restore_env(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _two_tiers(self):
        """主服务（本机那台）+ 付费兜底，两边都用假地址，免得真出网。"""
        os.environ.update({
            providers.PLATFORM_KEY_ENV: LOCAL_KEY,
            providers.PLATFORM_PROVIDER_ENV: "local_openai",
            "INFE_PILOT_LOCAL_MODEL_BASE_URL": "https://local.example:59851/v1",
            providers.PLATFORM_FALLBACK_KEY_ENV: PAID_KEY,
            providers.PLATFORM_FALLBACK_PROVIDER_ENV: "deepseek",
            # 故意设一个假地址：**固定主的供应商不读它**（deepseek 的地址由预设钉死），
            # 所以下面的断言要的是 api.deepseek.com——那也正是生产上真正会去的地址。
            providers.PLATFORM_FALLBACK_BASE_ENV: "https://paid.example/v1",
            "INFE_PILOT_MODEL_ATTEMPTS": "1",   # 一档一次，请求条数才好数
        })

    def _own(self, provider: str = "deepseek", model: str = "deepseek-flash",
             key: str = "sk-own") -> dict:
        """一个「用户自己的」模型连接（用完量那条路时够用就行）。"""
        box = SecretBox.from_base64("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
        return {"user_id": "usr", "kind": "model", "provider": provider, "model": model,
                "base_url": "", "config_json": "{}", "enabled": 1,
                "encrypted_api_key": box.encrypt(key, context="connection:usr:model")}

    def _message(self, user_id: str) -> dict:
        """一封「已收到」的邮件（`assist` 与 `_analyse` 都只吃这个形状）。"""
        return {"id": "msg_chain", "user_id": user_id, "subject": "Tuition deadline",
                "sender_name": "Registry", "sender_address": "registry@cityu.edu.hk",
                "received": "2026-09-23T02:00:00+00:00", "importance": "normal",
                "body": "Your tuition is due on 30 Sep 2026. Please settle it before the deadline."}

    def _recorded_a_failure(self, user_id: str) -> bool:
        """`key_circuits` 里有没有这个账号这一档的行（`failures` 是连续失败计数）。"""
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT failures FROM key_circuits WHERE user_id=? AND kind='model'",
                (user_id,)).fetchone()
        return bool(row and int(row["failures"]) >= 1)

    def _account(self, email: str = "chain@example.com", *, own_key: bool = False) -> dict:
        code = self.db.create_invite("chain", 1)
        user = self.db.create_user(email, hash_password("a-long-enough-password"), token_hash(code))
        if own_key:
            self.db.upsert_connection(user["id"], {
                "kind": "model", "provider": "openai", "model": "gpt-4o-mini",
                "base_url": "https://own.example/v1",
                "encrypted_api_key": self.secrets.encrypt("sk-own", context=f"connection:{user['id']}:model"),
                "config_json": "{}", "enabled": 1,
            })
        return user


class CandidateOrderTests(ChainTestCase):
    def test_an_account_without_its_own_key_gets_both_platform_tiers(self):
        self._two_tiers()
        user = self._account()
        self.assertEqual([item["provider"] for item in self.service.model_attempts(user["id"])],
                         ["local_openai", "deepseek"])

    def test_an_account_with_its_own_key_gets_only_its_own(self):
        """自带 key 的账号**一个平台档都看不到**：这是首页/隐私政策那三处承诺的代码形状。"""
        self._two_tiers()
        user = self._account(own_key=True)
        attempts = self.service.model_attempts(user["id"])
        self.assertEqual([item["provider"] for item in attempts], ["openai"])
        self.assertFalse(any(item.get("platform") for item in attempts))

    def test_without_a_fallback_key_there_is_only_one_tier(self):
        self._two_tiers()
        os.environ.pop(providers.PLATFORM_FALLBACK_KEY_ENV, None)
        user = self._account()
        self.assertEqual([item["provider"] for item in self.service.model_attempts(user["id"])],
                         ["local_openai"])


class OutboundRequestTests(ChainTestCase):
    """真正发出去的那个请求：地址、key、护栏任务名。"""

    def test_the_local_tier_sends_its_own_key_to_the_local_address(self):
        self._two_tiers()
        user = self._account()
        recorder = _Recorder([_answer(guard={"ok": True, "task": "summarize", "issues": [],
                                            "retried": False, "jev": False, "latency_s": 2.1})])
        with mock.patch.object(providers, "_outbound_open", recorder):
            _, used = self.service._generate_with_retry(
                user["id"], attempts=self.service.model_attempts(user["id"]),
                prompt="写周报", guard_task="summarize")
        self.assertEqual(used["provider"], "local_openai")
        self.assertEqual(recorder.urls, ["https://local.example:59851/v1/chat/completions"])
        self.assertEqual(recorder.requests[0]["authorization"], f"Bearer {LOCAL_KEY}")
        self.assertEqual(recorder.requests[0]["payload"]["x_guard"], {"task": "summarize"})
        # 自签证书那一档必须带上这一次请求的 TLS 配置（CA + 钉扎指纹）。
        self.assertEqual(recorder.requests[0]["tls"], providers.local_model_tls("local_openai"))

    def test_the_paid_tier_gets_the_paid_key_and_no_extra_trust(self):
        self._two_tiers()
        user = self._account()
        recorder = _Recorder([_answer()])
        with mock.patch.object(providers, "_outbound_open", recorder):
            _, used = self.service._generate_with_retry(
                user["id"], attempts=self.service.model_attempts(user["id"])[1:], prompt="写周报")
        self.assertEqual(used["provider"], "deepseek")
        self.assertEqual(recorder.urls, ["https://api.deepseek.com/chat/completions"],
                         "固定主的供应商不读环境里的地址覆盖")
        self.assertEqual(recorder.requests[0]["authorization"], f"Bearer {PAID_KEY}")
        self.assertIsNone(recorder.requests[0]["tls"],
                          "付费那档绝不能带本机服务的额外信任（那是另一台机器的事）")
        self.assertNotIn("x_guard", recorder.requests[0]["payload"],
                         "别的供应商不认这个字段；发过去就是整个请求失败")

    def test_the_keys_never_swap_places(self):
        """**把两把 key 串门**是这条路最贵的一种错，所以单独钉一条。

        主服务那把发给 DeepSeek = 把自家盒子的凭据交出去；反过来更糟：管理员那把
        DeepSeek key 会被送到一个我们只靠自签证书认识的地址上。
        """
        self._two_tiers()
        user = self._account()
        # 先让主服务断一次，看接手的那一次请求带的是谁。
        recorder = _Recorder([providers.TransientProviderError("接口连接被中断"), _answer()])
        with mock.patch("pilot_app.service.time.sleep"), \
                mock.patch.object(providers, "_outbound_open", recorder):
            _, used = self.service._generate_with_retry(
                user["id"], attempts=self.service.model_attempts(user["id"]), prompt="写周报")
        self.assertEqual(used["provider"], "deepseek")
        sent = [(item["url"], item["authorization"]) for item in recorder.requests]
        self.assertEqual(sent, [
            ("https://local.example:59851/v1/chat/completions", f"Bearer {LOCAL_KEY}"),
            ("https://api.deepseek.com/chat/completions", f"Bearer {PAID_KEY}"),
        ])


class FallbackBehaviourTests(ChainTestCase):
    def test_a_local_outage_is_invisible_to_the_user(self):
        """主服务挂了 → 第二档答话 → 这次生成**成功**，用户那边只是慢了一点。"""
        self._two_tiers()
        user = self._account()
        recorder = _Recorder([providers.TransientProviderError("无法连接 API：连接被拒绝"), _answer("兜底答的")])
        with mock.patch("pilot_app.service.time.sleep"), \
                mock.patch.object(providers, "_outbound_open", recorder):
            result, used = self.service._generate_with_retry(
                user["id"], attempts=self.service.model_attempts(user["id"]), prompt="写周报")
        self.assertEqual(result.text, "兜底答的")
        self.assertEqual(used["provider"], "deepseek")
        self.assertEqual(len(recorder.requests), 2)

    def test_both_tiers_down_leaves_a_sentence_a_human_can_act_on(self):
        self._two_tiers()
        user = self._account()
        recorder = _Recorder([
            providers.TransientProviderError("无法连接 API：连接被拒绝"),
            providers.TransientProviderError("API 返回 HTTP 503: upstream busy"),
        ])
        with mock.patch("pilot_app.service.time.sleep"), \
                mock.patch.object(providers, "_outbound_open", recorder):
            with self.assertRaises(providers.ProviderError) as caught:
                self.service._generate_with_retry(
                    user["id"], attempts=self.service.model_attempts(user["id"]), prompt="写周报")
        self.assertIn("503", str(caught.exception))
        self.assertEqual(len(recorder.requests), 2, "两档都试过之后才放弃")

    def test_a_local_credential_rejection_hands_over_and_is_never_counted(self):
        """本机那档被拒（401，对方轮换了 key）**要换兜底**，而且不记在用户头上。

        两层性质各有一条理由：

        * **换挡**：这个错是「永久」的（401 不会自己好），而 2026-09-23 之前这里是一个裸
          raise，于是「对方轮换 key」的症状是**所有没自带 key 的账号一封报告都出不来**——
          兜底那把明明在手边。判据是「这一档是不是我们自己维护的那台」；
        * **不计数**：熔断器的含义是「这把 key 是坏的」，而平台那两档是运营者配的，
          记在用户账上会让一个无辜账号被停掉（他改不了那两档）。
        """
        self._two_tiers()
        user = self._account()
        recorder = _Recorder([
            providers.ProviderError('API 返回 HTTP 401: {"error": {"message": "invalid api key"}}'),
            _answer("兜底答的"),
        ])
        with mock.patch.object(providers, "_outbound_open", recorder):
            result, used = self.service._generate_with_retry(
                user["id"], attempts=self.service.model_attempts(user["id"]), prompt="写周报")
        self.assertEqual(result.text, "兜底答的", "主服务被拒之后兜底要接手")
        self.assertEqual(used["provider"], "deepseek")
        self.assertFalse(self.db.key_circuit_open(user["id"], "model"),
                         "平台档的 401 不该在用户账上记失败（他改不了那两档配置）")
        # 而且这件事**要看得见**：降级意味着正在花付费那把的钱。
        from pilot_app import tierhealth
        status = tierhealth.reading(self.db)
        self.assertIsNotNone(status, "降级必须留下痕迹，否则就是静默花钱")
        self.assertEqual(status["state"], "degraded")
        self.assertIn("401", status["reason"])
        # 主服务再答话一次，那条痕迹要消失（否则一次抖动挂一整天，人就学会忽略它）。
        tierhealth.note_success(self.db)
        self.assertEqual(tierhealth.reading(self.db)["state"], "ok")

    def test_a_users_own_key_is_still_counted_when_the_provider_rejects_it(self):
        """反向：**用户自己的** key 被拒仍然要计（那是他自己的配置问题）。"""
        self._two_tiers()
        user = self._account(own_key=True)
        recorder = _Recorder([providers.ProviderError('API 返回 HTTP 401: {"error": "invalid api key"}')])
        with mock.patch.object(providers, "_outbound_open", recorder):
            with self.assertRaises(providers.ProviderError):
                self.service._generate_with_retry(
                    user["id"], attempts=self.service.model_attempts(user["id"]), prompt="写周报")
        self.assertTrue(self._recorded_a_failure(user["id"]),
                        "用户自己的 key 被拒仍然要计一次")


class UsageAttributionTests(ChainTestCase):
    def test_the_usage_row_names_the_tier_that_actually_answered(self):
        """兜底接手之后，用量那一行必须写 DeepSeek——不是「平台默认」那个名字。

        写错的后果不是崩溃：是「谁出钱」那张表安静地说谎。
        """
        self._two_tiers()
        user = self._account()
        recorder = _Recorder([providers.TransientProviderError("接口连接被中断"), _answer()])
        with mock.patch("pilot_app.service.time.sleep"), \
                mock.patch.object(providers, "_outbound_open", recorder):
            result, used = self.service._generate_with_retry(
                user["id"], attempts=self.service.model_attempts(user["id"]), prompt="写周报")
        self.service._record_usage(user["id"], "immediate", used, result.usage)
        rows = self.db.usage_overview(days=1)["users"]
        row = next(item for item in rows if item["user_id"] == user["id"])
        # `provider`/`model` 在逐模型的明细里（`totals` 那一层只有合计），
        # 而那正是「谁出钱」那张表读的地方。
        self.assertEqual([item["provider"] for item in row["models"]], ["deepseek"])
        self.assertEqual(row["models"][0]["calls"], 1)
        # 「这一笔是谁付的」写在**调用那一刻**（`on_platform` 是写入时记的），
        # 所以它只能从库里核，不能从聚合出来的那张表上倒退。
        with self.db.connect() as connection:
            stored = connection.execute(
                "SELECT provider, on_platform FROM token_usage WHERE user_id=?",
                (user["id"],)).fetchone()
        self.assertEqual(stored["provider"], "deepseek")
        self.assertEqual(int(stored["on_platform"]), 1, "借用运营者的服务，两档都记 on_platform")


class WiringOrderTests(unittest.TestCase):
    """把「从生产 2026-09-23 那个形状走到两档分家」的**操作顺序**钉住。

    出事的不是某个判断写错了，而是**中间少做了一步**：只改 `_PROVIDER` 没换 key。
    装配层的判据在 `test_platform_key_guard.py`（另一轮加的，10 条）；这一组钉的是
    **过渡本身**，起点取生产当时的真实形状（只读核过：主槽与兜底槽同一把付费 key、
    没有 `_PROVIDER`）。用真环境变量与真解析，不打桩——这条路的全部风险都在
    「环境文件被写成了什么样」。
    """

    PAID = "fixture-paid-deepseek-key"
    LOCAL = "fixture-local-model-key"

    def setUp(self):
        self.saved = {key: os.environ.get(key) for key in (
            providers.PLATFORM_KEY_ENV, providers.PLATFORM_PROVIDER_ENV,
            providers.PLATFORM_MODEL_ENV, providers.PLATFORM_BASE_ENV,
            providers.PLATFORM_FALLBACK_KEY_ENV, providers.PLATFORM_FALLBACK_PROVIDER_ENV,
            providers.PLATFORM_FALLBACK_MODEL_ENV, providers.PLATFORM_FALLBACK_BASE_ENV,
        )}
        self.addCleanup(self._restore)
        for key in self.saved:
            os.environ.pop(key, None)

    def _restore(self):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _todays_production(self):
        """2026-09-23 生产 `pilot.env` 的真实形状：两档同一把付费 key、没有 `_PROVIDER`。

        付费那把已经被 `--fallback --provider deepseek` 装进兜底槽了，
        所以现在**只差主槽那把 key**。
        """
        os.environ[providers.PLATFORM_KEY_ENV] = self.PAID
        os.environ[providers.PLATFORM_FALLBACK_PROVIDER_ENV] = "deepseek"
        os.environ[providers.PLATFORM_FALLBACK_KEY_ENV] = self.PAID

    def test_todays_production_is_two_tiers_fed_by_one_key(self):
        """起点本身是合法的：同一家、同一把 key 的两档。不要把它当成故障修掉。"""
        self._todays_production()
        self.assertFalse(providers.platform_key_conflict())
        self.assertEqual(len(providers.platform_model_connections()), 2)

    def test_the_wrong_order_is_caught_instead_of_leaking_the_paid_key(self):
        """**只改 `_PROVIDER` 不换 key** —— 这正是 2026-09-23 出事的那个动作。

        期望的形状不是「安静地照旧花钱」，而是：本机那一档被摘掉、哨兵能问出冲突、
        留下来那一档用的仍是**它自己那把** key（付费凭据不会流到第三方那台盒子上）。
        """
        self._todays_production()
        os.environ[providers.PLATFORM_PROVIDER_ENV] = "local_openai"   # 只改了这一个
        self.assertTrue(providers.platform_key_conflict(), "哨兵必须能问出「装错了」")
        chain = providers.platform_model_connections()
        self.assertEqual([item["provider"] for item in chain], ["deepseek"],
                         "本机那一档要被摘掉，而不是拿着付费 key 去调用")
        self.assertEqual(providers.platform_connection_key(chain[0]), self.PAID)

    def test_the_ordered_fix_ends_in_a_clean_two_tier_shape(self):
        """按文档做完之后，装配是干净的。

        `set_platform_key.sh --provider local_openai` 一次写 `_KEY` 与 `_PROVIDER`，
        所以不存在「provider 改了、key 还是旧的」那个中间态；这里按 env 的最终形状复核。
        """
        self._todays_production()
        # ① 主槽清掉之后只剩兜底那一档 —— 还能出报告（只是走付费），不是停摆。
        os.environ.pop(providers.PLATFORM_KEY_ENV, None)
        self.assertFalse(providers.platform_key_conflict())
        self.assertEqual([item["provider"] for item in providers.platform_model_connections()],
                         ["deepseek"])
        # ② 装上本机那把：两档分家，主服务在前。
        os.environ[providers.PLATFORM_KEY_ENV] = self.LOCAL
        os.environ[providers.PLATFORM_PROVIDER_ENV] = "local_openai"
        self.assertFalse(providers.platform_key_conflict())
        chain = providers.platform_model_connections()
        self.assertEqual([item["provider"] for item in chain], ["local_openai", "deepseek"])
        self.assertEqual([providers.platform_connection_key(item) for item in chain],
                         [self.LOCAL, self.PAID], "两把 key 各回各家")
        # ③ 兜底那一步本来就做完了（`--fallback --provider deepseek`），复核它没被动过。
        self.assertEqual(providers.metered_model_connection()["provider"], "deepseek")


class TimeoutBudgetTests(ChainTestCase):
    """本机那台是**家宽 + 租来的隧道**：卡住比挂掉更常见，而卡住最贵。

    它占着生成位，队列里排着的其他人跟着等。所以两件事：给它的等待上限**比付费那档短**，
    以及它一旦超时**直接换兜底**而不是把整封报告丢给队列退避（兜底那把就在手边）。
    """

    def test_the_local_tier_waits_less_than_the_paid_one(self):
        self.assertEqual(providers.request_timeout("local_openai"),
                         providers.LOCAL_MODEL_TIMEOUT_SECONDS)
        self.assertEqual(providers.request_timeout("deepseek"), providers.MODEL_TIMEOUT_SECONDS)
        self.assertLess(providers.LOCAL_MODEL_TIMEOUT_SECONDS, providers.MODEL_TIMEOUT_SECONDS)
        self.assertGreater(providers.LOCAL_MODEL_TIMEOUT_SECONDS, 90,
                           "交付文档建议读取 90 s，总时限要留出建连与护栏重生成的余量")

    def test_an_unknown_provider_gets_the_long_budget(self):
        """不认识的供应商按「供应商」对待：宽的那一档，而不是窄的。"""
        self.assertEqual(providers.request_timeout("nope"), providers.MODEL_TIMEOUT_SECONDS)

    def test_only_the_local_tier_gets_the_short_budget(self):
        self.assertTrue(providers.is_local({"provider": "local_openai"}))
        self.assertFalse(providers.is_local({"provider": "deepseek"}))
        self.assertFalse(providers.is_local({"provider": "nope"}))
        self.assertFalse(providers.is_local(None))
        self.assertFalse(providers.MODEL_PRESETS["deepseek"].local_model)

    def test_a_local_timeout_hands_over_instead_of_losing_the_message(self):
        """**这条是本轮修的漏洞**：本机卡住 → 兜底答话 → 用户无感。

        修之前：`ProviderTimeout` 一律 `raise`（那条规矩是为**用户自己的 key** 定的：
        重试同一档会让生成位被占两倍时间）。但两档之后它顺带把「换人」也堵死了——
        本机一卡，整封报告去排队退避，而兜底那把 key 就在手边。
        """
        self._two_tiers()
        os.environ["INFE_PILOT_MODEL_ATTEMPTS"] = "2"
        user = self._account()
        seen = []

        def flaky(**kwargs):
            seen.append(kwargs["provider"])
            if kwargs["provider"] == "local_openai":
                raise providers.ProviderTimeout("接口响应超时")
            return providers.Generation("兜底答的", [], "none",
                                        {"input": 1, "output": 1, "total": 2}, "stop")

        with mock.patch("pilot_app.service.time.sleep"), \
                mock.patch("pilot_app.service.providers.generate", side_effect=flaky):
            result, used = self.service._generate_with_retry(
                user["id"], attempts=self.service.model_attempts(user["id"]), prompt="写周报")
        self.assertEqual(result.text, "兜底答的")
        self.assertEqual(used["provider"], "deepseek")
        self.assertEqual(seen, ["local_openai", "deepseek"],
                         "超时之后**不再重试本机那一档**（省下一次 120 s），直接换兜底")

    def test_a_timeout_on_the_last_tier_still_fails_the_message(self):
        """兜底也超时就没有下一个人了：仍然交给队列退避，不要假装成功。"""
        self._two_tiers()
        user = self._account()
        with mock.patch("pilot_app.service.time.sleep"), \
                mock.patch("pilot_app.service.providers.generate",
                           side_effect=providers.ProviderTimeout("接口响应超时")):
            with self.assertRaises(providers.ProviderTimeout):
                self.service._generate_with_retry(
                    user["id"], attempts=self.service.model_attempts(user["id"])[1:], prompt="写周报")

    def test_a_users_own_key_timeout_never_hands_over_to_the_paid_tier(self):
        """用户自己的 key 超时**不换档**：那会让管理员替他的坏 key 付钱。

        这条是那道「换人」逻辑的边界。用户自己的 key 慢/卡是他自己的账，
        我们报一句退避重试即可；平台的兜底 key 只服务**没配 key**的账号。
        """
        self._two_tiers()
        user = self._account(own_key=True)
        calls = {"n": 0}

        def only_own(**kwargs):
            calls["n"] += 1
            self.assertEqual(kwargs["provider"], "openai", "只许试他自己的那一档")
            raise providers.ProviderTimeout("接口响应超时")

        with mock.patch("pilot_app.service.providers.generate", side_effect=only_own):
            with self.assertRaises(providers.ProviderTimeout):
                self.service._generate_with_retry(
                    user["id"], attempts=self.service.model_attempts(user["id"]), prompt="写周报")
        self.assertEqual(calls["n"], 1)


class GuardTaskMappingTests(ChainTestCase):
    """每一次调用报的 `x_guard.task` 必须与**那个动作**对得上。

    护栏的四个任务是按**动作**分的（对方文档 §3.3），拿错一个去审，结局通常不是「少一道
    检查」而是「多一道错的检查」——最贵的一种是它判定不合格、于是**重生成一次**（延迟翻倍）。
    """

    def test_a_translation_is_not_audited_as_a_reply(self):
        """看原信的「翻译 / 总结」两个 kind 都是概括，不是回复。

        报 `reply` 会让护栏按「信息不足没索取订单号」那一条去审一份译文——那是一条
        给客服回信定的规矩，而用户只是想要个翻译。
        """
        self._two_tiers()
        user = self._account()
        message_id = self._message(user["id"])["id"]
        captured: list[dict] = []

        def fake(**kwargs):
            captured.append(kwargs)
            return providers.Generation("译文", [], "none", {"input": 1, "output": 1, "total": 2}, "stop")

        # `assist` 会先去取原信（只读 IMAP）。这里只关心「报的是哪个护栏任务」，
        # 所以取信那一步直接给一段正文——不碰网络。
        self.service.read_original = lambda _uid, _mid: {
            "state": "ok",
            "message": {"subject": "Tuition", "sender_name": "Registry",
                        "sender_address": "registry@cityu.edu.hk",
                        "received": "2026-09-23T02:00:00+00:00",
                        "body": "Your tuition is due on 30 Sep 2026."},
        }
        with mock.patch("pilot_app.service.providers.generate", side_effect=fake):
            self.service.assist(user["id"], message_id, "translate")
        self.assertEqual(captured[0]["guard_task"], "summarize")
        self.assertNotEqual(captured[0]["guard_task"], "reply")

    def test_the_report_path_still_asks_for_a_summary(self):
        """报告那条路本来就是概括——别在这次改动里一起动它。"""
        self._two_tiers()
        user = self._account()
        captured: list[dict] = []

        def fake(**kwargs):
            captured.append(kwargs)
            return providers.Generation("## 1. 重点\nok", [], "none",
                                        {"input": 1, "output": 1, "total": 2}, "stop")

        with mock.patch("pilot_app.service.providers.generate", side_effect=fake):
            self.service._analyse(user["id"], self._message(user["id"]))
        self.assertEqual(captured[0]["guard_task"], "summarize")


class UsageArgumentShapeTests(ChainTestCase):
    """`_record_usage` 的参数形状：传错了不许**安静地**少一行账。

    2026-09-23 写测试时踩到一次：`assist()` 要的是 message **id**（字符串），我传了整条
    消息的 dict，于是 SQLite 抛 `InterfaceError`，而 `_record_usage` 的 `except` 把它吞成
    一行日志——**那一次调用在用量表里消失了**，而「我用了多少 / 谁付的」正是靠这张表。
    消息与 id 在调用方手上常常同时存在（`_analyse` / `_assist_call` 都是），所以这条值得钉。
    """

    def test_a_non_string_message_id_is_refused_loudly(self):
        self._two_tiers()
        user = self._account()
        with self.assertLogs(level="ERROR") as captured:
            self.service._record_usage(user["id"], "immediate", self._own(),
                                       {"input": 1, "output": 1, "total": 2},
                                       message_id={"id": "msg_1"})
        joined = "\n".join(captured.output)
        self.assertIn("message_id", joined)
        self.assertIn("dict", joined, "要说出收到的是什么类型，而不是 SQLite 的绑定错误")

    def test_a_normal_call_still_writes_its_row(self):
        """反向：合法调用照常落库（别把这条校验做成「谁都不记」）。"""
        self._two_tiers()
        user = self._account()
        self.service._record_usage(user["id"], "immediate", self._own(),
                                   {"input": 10, "output": 5, "total": 15},
                                   message_id="msg_ok")
        rows = self.db.usage_overview(days=1)["users"]
        row = next(item for item in rows if item["user_id"] == user["id"])
        self.assertEqual(row["models"][0]["calls"], 1)


class DegradationIsAlwaysVisibleTests(ChainTestCase):
    """**每一条**「主服务没干成活、兜底接手」的路都要留下痕迹。

    2026-09-23 在生产上验出来的：第一版只在**非瞬时**失败那一支盖了章，而主服务最常见的
    失败是「连不上」（隧道断了 / 那台关机了 / 端口没人听）——它们全是**瞬时**失败，
    走的是另一条换档的路。于是真出事的时候降级是**静默**的：用户无感、钱在花、
    面板上什么都不显示。这正是这个部署最该防的一件事。

    三条路各有各的入口，所以三条都要测：瞬时（连不上）、超时（卡住）、非瞬时（401）。
    """

    def _run(self, side_effect):
        self._two_tiers()
        os.environ["INFE_PILOT_MODEL_ATTEMPTS"] = "1"
        user = self._account()

        def flaky(**kwargs):
            if kwargs["provider"] == "local_openai":
                raise side_effect
            return providers.Generation("兜底答的", [], "none",
                                        {"input": 1, "output": 1, "total": 2}, "stop")

        with mock.patch("pilot_app.service.time.sleep"), \
                mock.patch("pilot_app.service.providers.generate", side_effect=flaky):
            result, used = self.service._generate_with_retry(
                user["id"], attempts=self.service.model_attempts(user["id"]), prompt="写周报")
        return result, used

    def _status(self):
        from pilot_app import tierhealth
        return tierhealth.reading(self.db)

    def test_a_transient_failure_still_leaves_a_trace(self):
        """**生产上抓到的那个**：连不上 = 瞬时失败，最容易漏掉盖章的一条。"""
        result, used = self._run(ConnectionRefusedError(111, "Connection refused"))
        self.assertEqual(used["provider"], "deepseek", "兜底要接手")
        status = self._status()
        self.assertIsNotNone(status, "瞬时失败换档也必须留痕，否则就是静默花钱")
        self.assertEqual(status["state"], "degraded")
        self.assertIn("ConnectionRefused", status["reason"])

    def test_a_timeout_leaves_a_trace(self):
        result, used = self._run(providers.ProviderTimeout("接口响应超时"))
        self.assertEqual(used["provider"], "deepseek")
        self.assertEqual(self._status()["state"], "degraded")

    def test_a_permanent_rejection_leaves_a_trace(self):
        result, used = self._run(providers.ProviderError('API 返回 HTTP 401: invalid api key'))
        self.assertEqual(used["provider"], "deepseek")
        status = self._status()
        self.assertEqual(status["state"], "degraded")
        self.assertIn("401", status["reason"])

    def test_a_healthy_primary_clears_it_again(self):
        """反向：主服务答话之后那枚章要消失，否则一次抖动挂一整天。"""
        self._run(ConnectionRefusedError(111, "Connection refused"))
        self.assertEqual(self._status()["state"], "degraded")
        self._two_tiers()          # 地址恢复
        user = self._account(email="second@example.com")
        with mock.patch("pilot_app.service.providers.generate",
                        return_value=providers.Generation("主服务答的", [], "none",
                                                          {"input": 1, "output": 1, "total": 2}, "stop")):
            _, used = self.service._generate_with_retry(
                user["id"], attempts=self.service.model_attempts(user["id"]), prompt="写周报")
        self.assertEqual(used["provider"], "local_openai")
        self.assertEqual(self._status()["state"], "ok")

    def test_a_users_own_key_failing_leaves_no_such_trace(self):
        """用户自己的 key 失败**不算主服务降级**：那时平台那两档根本没参与。"""
        self._two_tiers()
        user = self._account(own_key=True)
        with mock.patch("pilot_app.service.providers.generate",
                        side_effect=ConnectionRefusedError(111, "Connection refused")):
            with self.assertRaises(Exception):
                self.service._generate_with_retry(
                    user["id"], attempts=self.service.model_attempts(user["id"]), prompt="写周报")
        self.assertIsNone(self._status(), "他没走平台那两档，别把这件事记成主服务降级")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
