"""主服务（本机那台）**现在**通不通 —— 一条不等真信的探测。

## 为什么值得单独一条

`tierhealth.findings()` 读的那枚章只在**真的出过一封报告**时才更新。报告是稀疏事件：
那台盒子凌晨断掉、下一封信等到中午，中间几个小时里哨兵一句话都不说——报告全在走付费
兜底，而运营者以为主服务在干活。这条探测把「最早什么时候知道」从「下一封信」压到
「下一轮巡检（5 分钟）」。

四条性质，每条都有理由：

* **不调模型、不花钱、不要 key**：它只 `GET /health`（对方交付文档 §9 的自测第一条），
  所以每 5 分钟问一次是免费的；用 `generate()` 来"探活"会真的产生一次推理。
* **带那张自签证书与指纹钉扎**：`通` 与 `验过` 是两件事——地址上应答的必须**是**我们的服务。
* **`evaluate()` 不碰网络**：探测由 `run_checks` 做，结果当读数传进去；这样阈值与分支
  在没有 socket 的测试里全都可驱动（和 `disk` / `certificate_days` 同一个形状）。
* **三种返回值不是一回事**：`True` 探到了、`False` 探不到、`None` 这台实例根本没有
  「本机那台作为主档」这回事（自建实例、或主档是付费供应商）——`None` 一条都不该报。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from pilot_app import alerting, providers, tierhealth
from pilot_app.database import Database

LOCAL_KEY = "fixture-local-key"
PAID_KEY = "fixture-paid-key"

#: 两档会用到的八个环境变量：逐个恢复（含「原来没有」这种情形）。
PLATFORM_ENV_VARS = (
    providers.PLATFORM_KEY_ENV, providers.PLATFORM_PROVIDER_ENV,
    providers.PLATFORM_MODEL_ENV, providers.PLATFORM_BASE_ENV,
    providers.PLATFORM_FALLBACK_KEY_ENV, providers.PLATFORM_FALLBACK_PROVIDER_ENV,
    providers.PLATFORM_FALLBACK_MODEL_ENV, providers.PLATFORM_FALLBACK_BASE_ENV,
    "INFE_PILOT_LOCAL_MODEL_BASE_URL",
)


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


class LocalModelProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        for name in PLATFORM_ENV_VARS:
            original = os.environ.get(name)
            if original is None:
                self.addCleanup(os.environ.pop, name, None)
            else:
                self.addCleanup(os.environ.__setitem__, name, original)
            os.environ.pop(name, None)
        workdir = tempfile.TemporaryDirectory()
        self.addCleanup(workdir.cleanup)
        self.db = Database(pathlib.Path(workdir.name) / "pilot.sqlite3")
        self.db.initialize()

    def _local_primary(self) -> None:
        os.environ.update({
            providers.PLATFORM_KEY_ENV: LOCAL_KEY,
            providers.PLATFORM_PROVIDER_ENV: "local_openai",
            "INFE_PILOT_LOCAL_MODEL_BASE_URL": "https://local.example:59851/v1",
        })

    def _paid_primary_only(self) -> None:
        os.environ.update({providers.PLATFORM_KEY_ENV: PAID_KEY,
                           providers.PLATFORM_PROVIDER_ENV: "deepseek"})

    # -- 地址推导 ---------------------------------------------------------------

    def test_the_health_url_is_the_same_origin_as_the_model_base(self) -> None:
        self.assertEqual(providers.local_model_health_url("https://127.0.0.1:59851/v1"),
                         "https://127.0.0.1:59851/health")
        self.assertEqual(providers.local_model_health_url("https://box.example:59851/v1/"),
                         "https://box.example:59851/health")
        # 没有 /v1 后缀时也照样能用（换通道/换写法都不该让探测变成"地址是空的"）
        self.assertEqual(providers.local_model_health_url("https://box.example:59851"),
                         "https://box.example:59851/health")
        self.assertEqual(providers.local_model_health_url(""), "")

    # -- 三种返回值 -------------------------------------------------------------

    def test_no_local_primary_means_the_probe_does_not_apply(self) -> None:
        """自建实例 / 主档是付费供应商：这个问题根本不存在，别去探、也别报。"""
        self._paid_primary_only()
        self.assertIsNone(tierhealth.probe())
        self.assertFalse(tierhealth.local_is_primary())

    def test_a_healthy_answer_is_true_and_a_dead_endpoint_is_false(self) -> None:
        self._local_primary()
        self.assertTrue(tierhealth.local_is_primary())

        recorder = mock.Mock(return_value=_Response({"proxy": "ok", "upstream": "http://127.0.0.1:8080"}))
        with mock.patch.object(providers, "_outbound_open", recorder):
            self.assertIs(tierhealth.probe(), True)
        # 探的必须是 `/health`（不调模型、不要 key），且带上那张自签证书
        request = recorder.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/health"), request.full_url)
        tls = recorder.call_args.kwargs.get("tls")
        self.assertIsNotNone(tls, "本机那档必须带自签证书与指纹钉扎")

        with mock.patch.object(providers, "_outbound_open",
                               mock.Mock(side_effect=providers.ProviderError("connection refused"))):
            self.assertIs(tierhealth.probe(), False)

    def test_a_probe_failure_never_escapes(self) -> None:
        """探测失败必须是 `False`，不是异常：它没有能力把整轮巡检带走。"""
        self._local_primary()
        with mock.patch.object(tierhealth.providers, "local_model_health",
                               mock.Mock(side_effect=RuntimeError("boom"))):
            self.assertIs(tierhealth.probe(), False)

    def test_even_reading_the_configuration_cannot_raise(self) -> None:
        """**连配置都读不出来时也不许抛**（2026-09-23 自查补的那条边界）。

        第一版只把那次 HTTP 包在 try 里，前面读配置的两行露在外面——配置一旦读炸，
        异常会冒到 `alerting.run_checks` 外面去，而那个函数的承诺是「Never raises」。
        一个"看一眼那台在不在"的探测不该有能力让**所有**告警静默，所以这里钉住：
        读配置炸了 → 返回 `None`（当作没有这一档，不报），一个异常都不往外扔。
        """
        with mock.patch.object(tierhealth.providers, "platform_model_default",
                               mock.Mock(side_effect=RuntimeError("环境变量形状不对"))):
            self.assertIsNone(tierhealth.probe())

    # -- 发现项 -----------------------------------------------------------------

    def test_the_finding_appears_only_when_a_local_primary_is_unreachable(self) -> None:
        self._local_primary()
        reachable = alerting.evaluate(self.db, local_model_reachable=True)
        self.assertNotIn("local_model_unreachable", [item["key"] for item in reachable])

        unreachable = alerting.evaluate(self.db, local_model_reachable=False)
        keys = [item["key"] for item in unreachable]
        self.assertIn("local_model_unreachable", keys)
        entry = next(item for item in unreachable if item["key"] == "local_model_unreachable")
        self.assertEqual(entry["severity"], "warning")
        self.assertIn("主服务", entry["title"])
        # 出路要写在详情里：先看哪一条隧道、再看那台机器、最后逐跳核
        self.assertIn("59851", entry["detail"])
        self.assertIn("check-localmodel", entry["detail"])

    def test_not_probed_is_not_a_finding(self) -> None:
        """`None` = 没探（或这台实例没有本机那一档），绝不当成"探不到"。"""
        self._local_primary()
        self.assertNotIn("local_model_unreachable",
                         [item["key"] for item in alerting.evaluate(self.db)])

    def test_a_fresh_degraded_stamp_is_not_reported_twice(self) -> None:
        """同一场故障只许有一封信：章已经是新鲜的 `degraded` 时，探测那条闭上嘴。

        `tierhealth.findings()` 说的是「已经降级过」（真发生过、还花了钱），这一条说的是
        「现在还连不上」——同一场故障里两条都成立，一起发就是**一次事故两封信**。
        这个项目为「因与果只报一条」立过规矩（`mailbox_error` / `mailbox_stale` 那对），
        这里照同一条办：**已经发生**的那条留下，探测这条让位。

        反过来（章是 ok / 过期 / 没有）探测这条必须说话——那正是它存在的理由：
        凌晨断了、下一封信还没来，没有别的检查会发现。
        """
        self._local_primary()
        tierhealth.note_degraded(self.db, "TransientProviderError：无法连接 API")

        unreachable = alerting.evaluate(self.db, local_model_reachable=False)
        keys = [item["key"] for item in unreachable]
        self.assertIn("local_model_degraded", keys, "已经发生过的那条要留下")
        self.assertNotIn("local_model_unreachable", keys, "同一场故障不许两条一起发")

        # 章回到 ok（主服务又答话了）：探测这条立刻接手说话。
        tierhealth.note_success(self.db)
        again = alerting.evaluate(self.db, local_model_reachable=False)
        self.assertIn("local_model_unreachable", [item["key"] for item in again])

    def test_a_stale_degraded_stamp_does_not_mute_the_probe(self) -> None:
        """太旧的降级章（`tierhealth` 自己就不报了）不能让探测这条也闭嘴——否则谁都不说话。"""
        self._local_primary()
        old = dt.datetime(2026, 9, 23, 0, 0, tzinfo=dt.timezone.utc)
        tierhealth.note_degraded(self.db, "TransientProviderError：无法连接 API", when=old)

        later = old + tierhealth.STALE_AFTER + dt.timedelta(minutes=5)
        keys = [item["key"] for item in alerting.evaluate(self.db, now=later,
                                                          local_model_reachable=False)]
        self.assertNotIn("local_model_degraded", keys)
        self.assertIn("local_model_unreachable", keys)

    def test_a_paid_only_install_never_gets_this_finding(self) -> None:
        """主档是付费供应商的老形状：就算传进来一个 False 也不该报（那台不存在）。"""
        self._paid_primary_only()
        self.assertNotIn("local_model_unreachable",
                         [item["key"] for item in alerting.evaluate(self.db,
                                                                    local_model_reachable=False)])

    def test_the_finding_is_byte_identical_as_time_passes(self) -> None:
        """详情里不许有会自己变的东西：`_should_send` 是「详情变了就重发」。"""
        self._local_primary()
        first = alerting.evaluate(self.db, now=dt.datetime(2026, 9, 23, 2, 0, tzinfo=dt.timezone.utc),
                                  local_model_reachable=False)
        later = alerting.evaluate(self.db, now=dt.datetime(2026, 9, 23, 9, 30, tzinfo=dt.timezone.utc),
                                  local_model_reachable=False)
        pick = lambda items: next(item for item in items  # noqa: E731 - 测试里的取一条
                                  if item["key"] == "local_model_unreachable")
        self.assertEqual(pick(first), pick(later))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
