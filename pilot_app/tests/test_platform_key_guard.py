"""两档平台凭据不许「跨供应商共用同一把 key」——2026-09-23 真发生过一次。

**起因（不是假想的风险）**：要把主服务从「管理员那把付费 key」换成本机那台盒子时，
只改了 `INFE_PILOT_DEFAULT_MODEL_PROVIDER=local_openai` 而没换 key。于是**每一次调用**
都会把付费凭据当成主服务的凭据，发给第三方那台盒子——而且症状是「钱照花、报告照出」，
单测、面板、哨兵没有一处会红。当场还真的发生了一次同形状的事故：探测本机服务时用
`systemd-run --setenv` 想喂一把假 key，而 `EnvironmentFile=` 会覆盖 `--setenv`，
于是那把真 key 被当成了探测凭据（现场记录见 `docs/local-model-wiring-2026-09-23.md`）。

四条性质，每条都有理由：

* **判据必须是「同一把 key **且**不同供应商」**：过渡期两档都是 deepseek、同一把 key，
  那是合法的（生产上 2026-09-23 就是这个状态），不能把它拦掉。
* **摘掉的是本机那一档**（或两档都不是本机时留第一档）：宁可少一次不花钱的调用，
  也不能把付费凭据发到别人的机器上；剩下那档照样出报告，用户无感。
* **装错了要能被单独问出来**：闸门摘掉之后列表看起来很正常，所以 `platform_key_conflict()`
  要能在不花一分钱的时候就回答「现在是不是装错了」。
* **发现项里每个字符串都必须是常数**：`agent.finding_fingerprint()` 把标题也算进去，
  详情变了就重发——放一个会变的读数进去，等于每轮为同一件事重新付一次模型分析的钱。
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import tempfile
import unittest

from pilot_app import budget, providers
from pilot_app.database import Database

#: 两档各自会用到的八个环境变量。测试**逐个恢复**（含「原来没有」这种情形）：
#: `test_agent` 会在导入时就设一把平台 key，漏恢复会让别的文件里的判断跟着变。
PLATFORM_ENV_VARS = (
    providers.PLATFORM_KEY_ENV, providers.PLATFORM_PROVIDER_ENV,
    providers.PLATFORM_MODEL_ENV, providers.PLATFORM_BASE_ENV,
    providers.PLATFORM_FALLBACK_KEY_ENV, providers.PLATFORM_FALLBACK_PROVIDER_ENV,
    providers.PLATFORM_FALLBACK_MODEL_ENV, providers.PLATFORM_FALLBACK_BASE_ENV,
)

#: 夹具形状的 key：**故意不是真 key 的形状**，`publish_export.py` 的凭据闸门扫到真形状
#: 会拒绝导出整棵树（与 `test_platform_budget.FIXTURE_KEY` 同一条规矩）。
LOCAL_KEY = "fixture-local-model-key"
PAID_KEY = "fixture-paid-deepseek-key"


class PlatformKeyGuardTests(unittest.TestCase):
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

    # -- 装配：两档都是「不同供应商 + 同一把 key」的几种方向 ---------------------

    def _primary_local_paid_key(self) -> None:
        """出事的那个形状：主服务写着本机那台，key 却还是付费那把。"""
        os.environ[providers.PLATFORM_PROVIDER_ENV] = "local_openai"
        os.environ[providers.PLATFORM_KEY_ENV] = PAID_KEY
        os.environ[providers.PLATFORM_FALLBACK_PROVIDER_ENV] = "deepseek"
        os.environ[providers.PLATFORM_FALLBACK_KEY_ENV] = PAID_KEY

    def _local_fallback_with_paid_key(self) -> None:
        """反方向：本机那台被配成了兜底，key 仍是付费那把。"""
        os.environ[providers.PLATFORM_PROVIDER_ENV] = "deepseek"
        os.environ[providers.PLATFORM_KEY_ENV] = PAID_KEY
        os.environ[providers.PLATFORM_FALLBACK_PROVIDER_ENV] = "local_openai"
        os.environ[providers.PLATFORM_FALLBACK_KEY_ENV] = PAID_KEY

    def test_the_paid_key_is_never_handed_to_the_local_box(self) -> None:
        self._primary_local_paid_key()
        # 反向验证：**装配这一层本身是有两档的**（两档都真的拼出来了），
        # 所以下面看到的「只剩一档」确实是这道闸门摘的，不是配置压根没生效。
        self.assertEqual(len(providers._platform_connections_raw()), 2)
        connections = providers.platform_model_connections()
        self.assertEqual([item["provider"] for item in connections], ["deepseek"])
        # 剩下那一档拿的仍然是它自己那把 key —— 修好之前报告照出，只是钱照花。
        self.assertEqual(providers.platform_connection_key(connections[0]), PAID_KEY)

    def test_the_local_tier_is_dropped_whichever_end_it_sits_on(self) -> None:
        self._local_fallback_with_paid_key()
        connections = providers.platform_model_connections()
        self.assertEqual([item["provider"] for item in connections], ["deepseek"])

    def test_a_cross_provider_clash_between_two_paid_providers_keeps_the_first(self) -> None:
        os.environ[providers.PLATFORM_PROVIDER_ENV] = "deepseek"
        os.environ[providers.PLATFORM_KEY_ENV] = PAID_KEY
        os.environ[providers.PLATFORM_FALLBACK_PROVIDER_ENV] = "openrouter"
        os.environ[providers.PLATFORM_FALLBACK_MODEL_ENV] = "openrouter/auto"
        os.environ[providers.PLATFORM_FALLBACK_KEY_ENV] = PAID_KEY
        connections = providers.platform_model_connections()
        self.assertEqual([item["provider"] for item in connections], ["deepseek"])

    # -- 不能误伤：合法配置必须原样通过 ----------------------------------------

    def test_the_intended_two_tier_shape_survives(self) -> None:
        """主服务是本机、兜底是付费那把 —— 这是上线时要达到的形状。"""
        os.environ[providers.PLATFORM_PROVIDER_ENV] = "local_openai"
        os.environ[providers.PLATFORM_KEY_ENV] = LOCAL_KEY
        os.environ[providers.PLATFORM_FALLBACK_PROVIDER_ENV] = "deepseek"
        os.environ[providers.PLATFORM_FALLBACK_KEY_ENV] = PAID_KEY
        connections = providers.platform_model_connections()
        self.assertEqual([item["provider"] for item in connections],
                         ["local_openai", "deepseek"])
        # 顺序与「谁拿哪把 key」是两件事：第一档必须拿本机那把，第二档必须拿付费那把。
        self.assertEqual(providers.platform_connection_key(connections[0]), LOCAL_KEY)
        self.assertEqual(providers.platform_connection_key(connections[1]), PAID_KEY)
        self.assertFalse(providers.platform_key_conflict())

    def test_one_provider_with_the_same_key_on_both_tiers_is_legal(self) -> None:
        """过渡期的真实状态（生产 2026-09-23 装兜底时就是这样），不许被拦。"""
        os.environ[providers.PLATFORM_PROVIDER_ENV] = "deepseek"
        os.environ[providers.PLATFORM_KEY_ENV] = PAID_KEY
        os.environ[providers.PLATFORM_FALLBACK_PROVIDER_ENV] = "deepseek"
        os.environ[providers.PLATFORM_FALLBACK_KEY_ENV] = PAID_KEY
        connections = providers.platform_model_connections()
        self.assertEqual(len(connections), 2)
        self.assertFalse(providers.platform_key_conflict())

    def test_a_single_tier_is_never_a_conflict(self) -> None:
        os.environ[providers.PLATFORM_KEY_ENV] = PAID_KEY
        self.assertEqual(len(providers.platform_model_connections()), 1)
        self.assertFalse(providers.platform_key_conflict())

    def test_the_conflict_can_be_asked_about_without_reading_the_connections(self) -> None:
        """闸门摘掉了那一档，所以「装错了」必须能被单独问一次。"""
        self._primary_local_paid_key()
        self.assertTrue(providers.platform_key_conflict())
        os.environ[providers.PLATFORM_KEY_ENV] = LOCAL_KEY
        self.assertFalse(providers.platform_key_conflict())

    # -- 哨兵：没花钱的时候也要报（那正是它最像「一切正常」的时刻） -------------

    def test_the_sentinel_reports_it_before_a_single_cent_is_spent(self) -> None:
        self._primary_local_paid_key()
        rows = self.db.list_users_overview()
        found = budget.findings(self.db, rows=rows)
        keys = [item["key"] for item in found]
        self.assertIn("platform_key_shared_across_providers", keys)
        entry = next(item for item in found
                     if item["key"] == "platform_key_shared_across_providers")
        self.assertEqual(entry["severity"], "warning")
        self.assertIn("同一把 key", entry["title"])

    def test_the_finding_is_silent_when_the_two_tiers_are_configured_correctly(self) -> None:
        os.environ[providers.PLATFORM_PROVIDER_ENV] = "local_openai"
        os.environ[providers.PLATFORM_KEY_ENV] = LOCAL_KEY
        os.environ[providers.PLATFORM_FALLBACK_PROVIDER_ENV] = "deepseek"
        os.environ[providers.PLATFORM_FALLBACK_KEY_ENV] = PAID_KEY
        found = budget.findings(self.db, rows=self.db.list_users_overview())
        self.assertNotIn("platform_key_shared_across_providers",
                         [item["key"] for item in found])

    def test_the_finding_is_byte_identical_as_time_passes(self) -> None:
        """详情里不许有会自己变的东西：`_should_send` 是「变了就重发」。"""
        self._primary_local_paid_key()
        rows = self.db.list_users_overview()
        first = budget.findings(self.db, now=dt.datetime(2026, 9, 23, 1, 0, tzinfo=dt.timezone.utc),
                                rows=rows)
        later = budget.findings(self.db, now=dt.datetime(2026, 9, 23, 7, 30, tzinfo=dt.timezone.utc),
                                rows=rows)
        pick = lambda items: next(item for item in items  # noqa: E731 - 测试里的取一条
                                  if item["key"] == "platform_key_shared_across_providers")
        self.assertEqual(pick(first), pick(later))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
