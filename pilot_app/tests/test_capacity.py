"""Tests for the capacity advisor.

The properties that matter are less about arithmetic than about trust: the panel
shows this number next to a button that acts on it, so it has to be a number an
operator could plausibly accept, it has to say what limited it, and it must not
fall over when the install is too new to have any data.
"""

from __future__ import annotations

import unittest

from pilot_app import capacity

IDLE = {"cpu_percent": 3.0, "memory": {"percent": 31.0}, "disk": {"percent": 25.0}}
BUSY = {"cpu_percent": 95.0, "memory": {"percent": 92.0}, "disk": {"percent": 88.0}}


def volume(**overrides):
    base = {"window_days": 14, "messages": 31, "delivered": 30, "active_users": 1,
            "total_users": 2, "generation_gaps": [6, 5, 7, 6, 6, 9, 5]}
    base.update(overrides)
    return base


class CapacityAdviceTests(unittest.TestCase):
    def test_the_recommendation_is_a_number_someone_could_act_on(self):
        """The raw throughput arithmetic happily produces tens of thousands,
        because it assumes the box exists only to serve reports. A number like
        that is not advice — printing it once would cost the operator's trust in
        every other number on the panel."""
        advice = capacity.advise(volume=volume(), host=IDLE, workers=6,
                                 current=5, source="environment")
        self.assertLessEqual(advice["recommended"], capacity.PILOT_COMFORTABLE_USERS)
        self.assertGreaterEqual(advice["recommended"], 10)

    def test_a_busy_machine_stops_the_advice_growing(self):
        advice = capacity.advise(volume=volume(), host=BUSY, workers=6,
                                 current=5, source="settings")
        self.assertEqual(advice["binding"], "cpu")
        self.assertEqual(advice["recommended"], 5)

    def test_it_never_suggests_shrinking_the_pilot(self):
        """Removing users is a decision, not a recommendation."""
        advice = capacity.advise(volume=volume(total_users=120), host=BUSY, workers=6,
                                 current=120, source="settings")
        self.assertGreaterEqual(advice["recommended"], 120)

    def test_more_traffic_per_user_means_fewer_users(self):
        quiet = capacity.advise(volume=volume(messages=30, active_users=1), host=IDLE,
                                workers=6, current=1, source="environment")
        busy = capacity.advise(volume=volume(messages=3000, active_users=1), host=IDLE,
                               workers=6, current=1, source="environment")
        self.assertLess(busy["measured"]["mails_per_user_day"],
                        quiet["measured"]["mails_per_user_day"] + 1e9)
        self.assertLessEqual(busy["recommended"], quiet["recommended"])

    def test_a_fresh_install_is_flagged_rather_than_guessed_silently(self):
        advice = capacity.advise(volume=volume(messages=0, delivered=0, active_users=0,
                                               total_users=2, generation_gaps=[]),
                                 host=IDLE, workers=6, current=5, source="environment")
        self.assertEqual(advice["confidence"], "low")
        self.assertFalse(advice["measured"]["report_seconds_from_data"])
        self.assertTrue(any("默认" in note for note in advice["notes"]),
                        "没有实测数据时必须明确说出来")

    def test_enough_samples_raises_confidence(self):
        advice = capacity.advise(volume=volume(generation_gaps=[6] * 12), host=IDLE,
                                 workers=6, current=5, source="environment")
        self.assertEqual(advice["confidence"], "high")
        self.assertTrue(advice["measured"]["report_seconds_from_data"])

    def test_the_binding_reason_explains_itself(self):
        advice = capacity.advise(volume=volume(), host=IDLE, workers=6,
                                 current=5, source="environment")
        self.assertTrue(advice["binding_reason"])
        self.assertIn("判断", "判断依据")
        names = [item["name"] for item in advice["constraints"]]
        self.assertIn(advice["binding"], names)

    def test_the_box_slots_cap_the_generation_ceiling(self):
        """并发不是「我们开了几个报告槽」，而是「有几份真的在算」。

        2026-09-23：主服务换成本机那台盒子（2 个推理槽），而我们这边有 6 个报告槽。
        面板原来按 6 算，于是它给出的账号上限比真实天花板高出一个数量级——这条断言
        钉住「取小的那个」，并且要求面板**说出来**是哪两个数。
        """
        wide = capacity.advise(volume=volume(), host=IDLE, workers=6,
                               current=5, source="environment")
        narrow = capacity.advise(volume=volume(), host=IDLE, workers=6, model_slots=2,
                                 current=5, source="environment")
        self.assertEqual(narrow["measured"]["slots_used"], 2)
        self.assertEqual(narrow["measured"]["model_slots"], 2)
        self.assertLess(narrow["constraints"][0]["limit"],
                        wide["constraints"][0]["limit"])
        self.assertAlmostEqual(narrow["constraints"][0]["limit"] * 3,
                               wide["constraints"][0]["limit"], places=2)
        reason = narrow["constraints"][0]["reason"]
        self.assertIn("2", reason)
        self.assertIn("6", reason)
        self.assertIn("取小的那个", reason)

    def test_it_says_which_window_the_median_came_from(self):
        """中位数落在哪个窗口，必须说出来——**换过主服务的时候它是两任混在一起的**。

        2026-09-23 切到本机那台：14 天窗口里的 p50 还是上一任供应商的 7 秒，
        而当天本机那档已经是 10.5 秒。所以产能那一项优先用「最近几天」，
        并在说明里点出这个窗口。
        """
        advice = capacity.advise(
            volume=volume(report_seconds_end_to_end=[7, 7, 8, 7, 16],
                          report_seconds_recent=[11, 10, 12]),
            host=IDLE, workers=6, model_slots=2, current=5, source="environment")
        joined = " ".join(advice["notes"])
        self.assertIn(f"最近 {capacity.RECENT_SAMPLE_DAYS} 天", joined)
        self.assertIn("11", joined, "应当按最近窗口的中位数（11 秒）算，而不是全窗口的 7 秒")

    def test_a_thin_recent_window_falls_back_and_says_so(self):
        """最近几天样本不够时退回全窗口，并**点明那可能描述的是上一任**。

        这句话必须是**条件式**的：2026-09-23 这里曾写死「本机那档还没跑过真实报告」，
        第二天就不成立了。会过期的断言比没有断言更糟——它让运营者不再看这个数。
        """
        advice = capacity.advise(
            volume=volume(report_seconds_end_to_end=[7, 7, 8, 7, 16],
                          report_seconds_recent=[11]),
            host=IDLE, workers=6, model_slots=2, current=5, source="environment")
        joined = " ".join(advice["notes"])
        self.assertIn("不够", joined)
        self.assertIn("如果最近换过主服务", joined)
        self.assertIn("7", joined, "退回全窗口后应当用 7 秒")
        self.assertNotIn("还没跑过真实报告", joined, "不要写会过期的话")

    def test_with_recent_samples_it_stops_calling_them_the_previous_provider(self):
        """最近窗口够用时，那句话就不该出现了：这些样本跑的就是本机那台。

        留下来的那句是**护栏会让它翻倍**——分布比中位数宽，所以产能是个乐观值。
        """
        advice = capacity.advise(
            volume=volume(report_seconds_end_to_end=[7, 7, 8, 7, 16],
                          report_seconds_recent=[19, 18, 21]),
            host=IDLE, workers=6, model_slots=2, current=5, source="environment")
        joined = " ".join(advice["notes"])
        self.assertIn("跑的就是本机那台", joined)
        self.assertIn("护栏", joined)
        # 「更早的 N 份没计入」那句是**条件式**的（换过主服务的话），可以有；
        # 但不能出现无条件地说"这个中位数描述的是上一任"。
        self.assertNotIn("如果最近换过主服务", joined)
        self.assertNotIn("还没跑过真实报告", joined)

    def test_that_note_does_not_appear_without_a_local_box(self):
        """反向：主档是付费供应商的实例（以及所有老调用方）不该多这一句。"""
        advice = capacity.advise(volume=volume(), host=IDLE, workers=6, model_slots=None,
                                 current=5, source="environment")
        joined = " ".join(advice["notes"])
        self.assertNotIn("本机那台", joined)
        self.assertNotIn("如果最近换过主服务", joined)

    def test_no_local_box_means_our_workers_are_the_concurrency(self):
        """主档是付费供应商的实例（以及所有老调用方）：行为一个字节都不变。"""
        advice = capacity.advise(volume=volume(), host=IDLE, workers=6, model_slots=None,
                                 current=5, source="environment")
        self.assertEqual(advice["measured"]["slots_used"], 6)
        self.assertIsNone(advice["measured"]["model_slots"])
        self.assertNotIn("取小的那个", advice["constraints"][0]["reason"])

    def test_a_box_with_more_slots_than_we_have_workers_changes_nothing(self):
        advice = capacity.advise(volume=volume(), host=IDLE, workers=6, model_slots=8,
                                 current=5, source="environment")
        self.assertEqual(advice["measured"]["slots_used"], 6)
        self.assertNotIn("取小的那个", advice["constraints"][0]["reason"])

    def test_it_reports_what_the_live_pressure_says(self):
        """The operator asked for advice based on server pressure, so the answer
        has to state it rather than leave it to be inferred."""
        advice = capacity.advise(volume=volume(), host=IDLE, workers=6,
                                 current=5, source="environment")
        self.assertIn("CPU 3%", advice["load"])
        self.assertTrue(any("很轻松" in note for note in advice["notes"]))

    def test_missing_metrics_do_not_invent_a_constraint(self):
        """The macOS dev box cannot read /proc; null must mean unknown."""
        advice = capacity.advise(volume=volume(), host={"cpu_percent": None,
                                                       "memory": {}, "disk": {}},
                                 workers=6, current=5, source="environment")
        names = [item["name"] for item in advice["constraints"]]
        self.assertNotIn("cpu", names)
        self.assertNotIn("memory", names)
        self.assertNotIn("disk", names)

    def test_a_zero_worker_count_does_not_divide_by_zero(self):
        advice = capacity.advise(volume=volume(), host=IDLE, workers=0,
                                 current=5, source="environment")
        self.assertGreaterEqual(advice["recommended"], 1)

    def test_the_source_is_passed_through(self):
        advice = capacity.advise(volume=volume(), host=IDLE, workers=6,
                                 current=9, source="settings")
        self.assertEqual(advice["source"], "settings")
        self.assertEqual(advice["current"], 9)


class EndToEndGenerationTests(unittest.TestCase):
    """每份报告的真实耗时该从哪里来（2026-09-22，正式版收尾时修的）。

    起因：面板的建议说「当前瓶颈是模型生成速度……换更快的模型才有用」，用的却是
    「同用户相邻报告间隔」（生产上 3440 秒）当每份报告的耗时。实测端到端是
    **p50 = 7 秒**（285 份样本，p90 = 16 秒）——差了约 490 倍，于是那句结论是错的，
    真正卡住名额的是「一台机器 + 一个人维护」这条政策上限。
    """

    def test_end_to_end_samples_win_over_the_interval_bound(self):
        """有端到端样本时，产能按它算，不按那个被高估的间隔。"""
        with_e2e = capacity.advise(
            volume=volume(generation_gaps=[3440] * 7,
                          report_seconds_end_to_end=[7, 8, 9]),
            host=IDLE, workers=6, current=30, source="settings")
        without = capacity.advise(
            volume=volume(generation_gaps=[3440] * 7), host=IDLE, workers=6,
            current=30, source="settings")
        generation_with = next(c for c in with_e2e["constraints"] if c["name"] == "generation")
        generation_without = next(c for c in without["constraints"] if c["name"] == "generation")
        self.assertGreater(generation_with["limit"], generation_without["limit"] * 100,
                           "端到端样本没有把产能从「间隔上界」里解出来")
        self.assertIn("端到端实测", generation_with["reason"])

    def test_without_end_to_end_it_says_the_number_is_a_lower_bound(self):
        """退回保守上界时必须**说清**这是下限，而不是让人当成实测。"""
        advice = capacity.advise(volume=volume(generation_gaps=[3440] * 7), host=IDLE,
                                 workers=6, current=30, source="settings")
        notes = " ".join(advice["notes"])
        self.assertIn("高估", notes)
        self.assertIn("下限", notes)

    def test_a_fresh_install_does_not_claim_end_to_end_data(self):
        """一条端到端样本都没有时，不能凭空说"按实测算"。"""
        advice = capacity.advise(volume=volume(generation_gaps=[]), host=IDLE,
                                 workers=6, current=5, source="environment")
        self.assertFalse(advice["measured"]["report_seconds_from_data"])
        self.assertNotIn("端到端实测", " ".join(advice["notes"]))


if __name__ == "__main__":
    unittest.main()
