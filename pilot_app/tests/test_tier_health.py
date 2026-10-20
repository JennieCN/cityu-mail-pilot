"""主服务降级那条告警：**静默花钱**是这个部署最该防的一件事。

背景（2026-09-23）：本机那档失败时，兜底会接手，用户毫无感觉、报告照出——但每一封都在
花管理员那把 key 的钱，而主服务存在的意义正是不花这笔钱。金额那一项救不了它：没过警戒线
时账单一个字都不说。所以这里钉的是「这件事必须看得见，而且修好之后立刻闭嘴」。
"""

from __future__ import annotations

import datetime as dt
import pathlib
import tempfile
import unittest
from unittest import mock

from pilot_app import providers, tierhealth
from pilot_app.database import Database

NOW = dt.datetime(2026, 9, 23, 4, 0, tzinfo=dt.timezone.utc)
LOCAL_KEY = "fixture-local-model-key"


class TierHealthTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.addCleanup(self.work.cleanup)
        self.db = Database(pathlib.Path(self.work.name) / "pilot.sqlite3")
        self.db.initialize()
        self.saved = {key: __import__("os").environ.get(key) for key in (
            providers.PLATFORM_KEY_ENV, providers.PLATFORM_PROVIDER_ENV,
            providers.PLATFORM_MODEL_ENV, providers.PLATFORM_BASE_ENV,
            providers.PLATFORM_FALLBACK_KEY_ENV, providers.PLATFORM_FALLBACK_PROVIDER_ENV,
        )}
        self.addCleanup(self._restore)
        for key in self.saved:
            __import__("os").environ.pop(key, None)

    def _restore(self):
        import os
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _local_configured(self):
        import os
        os.environ[providers.PLATFORM_KEY_ENV] = LOCAL_KEY
        os.environ[providers.PLATFORM_PROVIDER_ENV] = "local_openai"

    def _keys(self) -> list[str]:
        return [item["key"] for item in tierhealth.findings(self.db, now=NOW)]

    # -- 正常的时候一句话都不说 ------------------------------------------------

    def test_nothing_is_reported_before_anything_happened(self):
        self._local_configured()
        self.assertIsNone(tierhealth.reading(self.db))
        self.assertEqual(self._keys(), [])

    def test_a_self_hosted_instance_without_a_local_tier_says_nothing(self):
        """没配主服务这一档的实例：这件事与它无关，报出来只是噪音。"""
        tierhealth.note_degraded(self.db, "401", when=NOW)
        self.assertEqual(self._keys(), [])

    def test_a_success_clears_the_flag(self):
        self._local_configured()
        tierhealth.note_degraded(self.db, "401", when=NOW)
        self.assertEqual(self._keys(), ["local_model_degraded"])
        tierhealth.note_success(self.db, when=NOW)
        self.assertEqual(self._keys(), [], "修好之后要立刻闭嘴——否则一次抖动挂一整天")

    # -- 降级的时候要说清楚 ----------------------------------------------------

    def test_a_degraded_primary_is_reported_with_its_reason(self):
        self._local_configured()
        tierhealth.note_degraded(self.db, "ProviderError：API 返回 HTTP 401", when=NOW)
        found = {item["key"]: item for item in tierhealth.findings(self.db, now=NOW)}
        self.assertIn("local_model_degraded", found)
        detail = found["local_model_degraded"]["detail"]
        self.assertIn("付费兜底", detail)
        self.assertIn("401", detail)
        self.assertIn("check-localmodel", detail, "要给出下一步该跑哪条命令")

    def test_a_stale_flag_goes_quiet(self):
        """太久以前的降级不报：可能早修好了，只是还没有人出过报告来盖章。"""
        self._local_configured()
        tierhealth.note_degraded(self.db, "401", when=NOW - tierhealth.STALE_AFTER - dt.timedelta(hours=1))
        self.assertEqual(self._keys(), [])

    def test_the_detail_does_not_contain_numbers_that_keep_changing(self):
        """`alerting._should_send` 是「详情变了就重发」——放实时读数会把告警变成节拍器。

        （2026-09-15 那 70 封邮件就是这么来的：详情里写了「注册已 N 小时」。）
        """
        self._local_configured()
        tierhealth.note_degraded(self.db, "ProviderError：超时", when=NOW)
        first = tierhealth.findings(self.db, now=NOW)[0]["detail"]
        second = tierhealth.findings(self.db, now=NOW + dt.timedelta(minutes=30))[0]["detail"]
        self.assertEqual(first, second, "同一件事的详情必须逐字节相同")

    def test_an_unreadable_row_is_treated_as_no_record(self):
        self._local_configured()
        self.db.set_setting(tierhealth.STATUS_KEY, "{not json", actor="test")
        self.assertIsNone(tierhealth.reading(self.db))
        self.assertEqual(self._keys(), [])

    def test_writing_the_flag_can_never_break_a_report(self):
        """记账失败不许影响一封已经生成好的报告（与 `_record_usage` 同款理由）。"""
        broken = mock.MagicMock()
        broken.set_setting.side_effect = RuntimeError("disk full")
        with self.assertLogs(level="WARNING"):
            tierhealth.note_success(broken, when=NOW)
            tierhealth.note_degraded(broken, "401", when=NOW)


class SentinelWiringTests(TierHealthTests):
    def test_the_flag_reaches_the_sentinel_as_a_finding(self):
        """真正要证明的是：这条痕迹能被 `alerting.evaluate` 读出来（不只是模块自己有）。"""
        from pilot_app import alerting
        self._local_configured()
        tierhealth.note_degraded(self.db, "ProviderError：API 返回 HTTP 401", when=NOW)
        self.db.set_setting("master_key_verified_at", NOW.isoformat(timespec="seconds"))
        self.db.set_setting("master_key_verified_fingerprint", "X")
        rows = alerting.evaluate(self.db, now=NOW, disk_percent=10.0, certificate_days=90.0,
                                 backup_dir=pathlib.Path(self.work.name) / "no-backups",
                                 master_key_fingerprint="X")
        self.assertIn("local_model_degraded", {item["key"] for item in rows})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
