"""客服群二维码的状态与哨兵提醒。

微信的群码**只有 7 天**，而它过期的后果是**静默的**：介绍页从那天起只显示一句
「码过期了，去留言」，访客扫不到群，而我们这边一点动静都没有——这正是哨兵存在的理由。
这个文件钉四件事：

* **没配就不报**：没挂群二维码的实例（自建、或运营者暂时不想挂）不该收到「快过期」；
* **快到了要报**（提前 2 天），**过了也要报**（那时页面已经不出图了）；
* **日期读不出来按「过期」处理**：猜错的方向只能是让访客去留言，不能是让他扫一张作废的码；
* **详情是常数**：到期日与阈值都不随今天变，所以同一件事一天最多一封，不会因为
  「还有 2 天」变成「还有 1 天」而重新发。
"""

from __future__ import annotations

import datetime as dt
import os
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from pilot_app import groupqr  # noqa: E402

IMG = {groupqr.IMG_ENV: "/wechat-group.png"}


def at(text: str) -> dt.datetime:
    """一个 UTC 时刻；`text` 按香港时间理解（到期日就是照微信上那个日期写的）。"""
    hk = dt.datetime.fromisoformat(text).replace(tzinfo=groupqr.HONG_KONG)
    return hk.astimezone(dt.timezone.utc)


class StateTests(unittest.TestCase):
    def test_not_configured_means_nothing_to_watch(self):
        with mock.patch.dict(os.environ, {groupqr.IMG_ENV: "", groupqr.UNTIL_ENV: ""}):
            self.assertFalse(groupqr.state()["configured"])
            self.assertEqual(groupqr.findings(), [])

    def test_days_left_and_the_two_windows(self):
        with mock.patch.dict(os.environ, {**IMG, groupqr.UNTIL_ENV: "2026-09-29"}):
            far = groupqr.state(now=at("2026-09-25 09:00"))
            self.assertEqual(far["days_left"], 4)
            self.assertFalse(far["soon"] or far["expired"])
            self.assertEqual(groupqr.findings(now=at("2026-09-25 09:00")), [])

            soon = groupqr.state(now=at("2026-09-27 09:00"))
            self.assertTrue(soon["soon"] and not soon["expired"])
            self.assertEqual(groupqr.findings(now=at("2026-09-27 09:00"))[0]["title"],
                             "客服群二维码快过期了")

            last = groupqr.state(now=at("2026-09-29 23:00"))
            self.assertTrue(last["soon"] and not last["expired"], "到期当天还能扫，算「快过期」")

            gone = groupqr.state(now=at("2026-09-30 00:30"))
            self.assertTrue(gone["expired"])
            self.assertEqual(groupqr.findings(now=at("2026-09-30 00:30"))[0]["title"],
                             "客服群二维码已经过期")

    def test_a_missing_or_unparsable_date_counts_as_expired(self):
        for raw in ("", "下个月", "29/09/2026"):
            with mock.patch.dict(os.environ, {**IMG, groupqr.UNTIL_ENV: raw}):
                self.assertTrue(groupqr.state(now=at("2026-09-25 09:00"))["expired"], raw)
                self.assertIn("过期", groupqr.findings(now=at("2026-09-25 09:00"))[0]["title"])

    def test_the_detail_is_a_constant_across_the_window(self):
        """「还有 2 天」与「还有 1 天」必须是同一段详情：不然一天会变成两封。"""
        with mock.patch.dict(os.environ, {**IMG, groupqr.UNTIL_ENV: "2026-09-29"}):
            first = groupqr.findings(now=at("2026-09-27 09:00"))
            second = groupqr.findings(now=at("2026-09-28 09:00"))
            self.assertEqual(first, second)

    def test_the_reminder_says_how_to_replace_it(self):
        with mock.patch.dict(os.environ, {**IMG, groupqr.UNTIL_ENV: "2026-09-29"}):
            detail = groupqr.findings(now=at("2026-09-28 09:00"))[0]["detail"]
            self.assertIn("wechat-group.png", detail)
            self.assertIn(groupqr.UNTIL_ENV, detail)

    def test_one_key_for_both_states_so_escalation_is_one_more_mail(self):
        with mock.patch.dict(os.environ, {**IMG, groupqr.UNTIL_ENV: "2026-09-29"}):
            self.assertEqual(groupqr.findings(now=at("2026-09-27 09:00"))[0]["key"],
                             groupqr.findings(now=at("2026-09-30 09:00"))[0]["key"])

    def test_the_sentinel_mails_this_at_most_once_a_day(self):
        from pilot_app import alerting
        self.assertEqual(alerting._repeat_for("wechat_group_qr"),
                         alerting.ALERT_WECHAT_REPEAT_SECONDS)
        self.assertEqual(alerting.tier_for("wechat_group_qr"), alerting.TIER_MAIL)


if __name__ == "__main__":
    unittest.main()
