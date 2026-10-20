# -*- coding: utf-8 -*-
"""不许承诺一封不会来的邮件 —— 2026-09-23 实测踩到。

## 形状

站上开的是「只发精简版」（`INFE_PILOT_FULL_REPORT=0`），而两处文案是**写死**的：

* `reports.BRIEF_TRAILER`：精简报告页脚「完整的中英双语报告（…）稍后单独发送」；
* `alerts.py` 的到达提醒：「完整的中英双语报告（含行动项…）正在生成，稍后单独发送」。

于是每一封精简报告、每一条到达提醒都在承诺一封**永远不会到**的邮件。用户等不到，
只会以为东西丢了。这与「句子不许比数字说得更满」是同一条规矩：**说的话必须与配置一致。**

修法不是改文案，而是把这个事实**从调用方传进来**（`full_follows`）：两段式模式下它是
True（完整版确实随后就来），只发精简版时是 False。

## 这里**没**覆盖的一格（如实记下）

反向验证过：把 `service.py` 里 `full_follows=want_full` 那一行删掉，**本文件的测试全绿**
—— 因为这里测的是**渲染层的行为**（`render_brief` / `build_alert` 收到 True/False 之后
说什么），而「服务层有没有把真实情况传下来」不在这层的射程内。补它要先 mock 掉
`process_message` 的收信与投递两条边，成本高于这一次改动的风险（改动只有 4 行）。
所以：**这一格靠人读那 4 行，不靠测试**；下一轮若要更强的保证，从这里开始加。
"""

from __future__ import annotations

import pathlib
import unittest

from pilot_app import alerts, reports

ROOT = pathlib.Path(__file__).resolve().parents[2]

BRIEF = """## 1. 重要程度与一句话结论 / Importance and one-line conclusion
等级：低
结论：测试用的一封邮件。

## 2. 必须采取的行动与截止时间 / Required actions and deadlines
- 无需行动。

## 3. 邮件内容要点 / Key points
- 一条真实要点。"""

MESSAGE = {"subject": "测试", "sender_name": "发件人", "sender_address": "a@cityu.edu.hk",
           "received": "2026-09-23T03:00:00+00:00", "importance": "normal"}

URGENT_MAIL = {"subject": "作业提醒", "sender_name": "老师", "sender_address": "t@cityu.edu.hk",
               "body": "请于明日 23:59 前提交作业。", "importance": "high"}

PROMISE = "稍后单独发送"


class BriefDoesNotOverpromiseTests(unittest.TestCase):
    def test_only_mode_never_promises_a_full_report(self) -> None:
        for follows, want in ((True, True), (False, False)):
            with self.subTest(full_follows=follows):
                rendered = reports.render_brief(BRIEF, MESSAGE, subject="x", full_follows=follows)
                self.assertEqual(PROMISE in rendered["text"], want)
                self.assertEqual(PROMISE in rendered["html"], want)

    def test_only_mode_says_what_actually_happens(self) -> None:
        rendered = reports.render_brief(BRIEF, MESSAGE, subject="x", full_follows=False)
        self.assertIn("只发送这一份", rendered["text"])
        self.assertNotIn("稍后单独发送", rendered["text"])

    def test_the_default_still_promises_it(self) -> None:
        """两段式是既有行为 —— 默认值必须保持「完整版会来」，否则会静默改掉老路径。"""
        rendered = reports.render_brief(BRIEF, MESSAGE, subject="x")
        self.assertIn(PROMISE, rendered["text"])


class ArrivalAlertDoesNotOverpromiseTests(unittest.TestCase):
    def test_the_alert_only_promises_when_a_full_report_follows(self) -> None:
        for follows, want in ((True, True), (False, False)):
            with self.subTest(full_follows=follows):
                alert = alerts.build_alert(URGENT_MAIL, full_follows=follows)
                self.assertEqual(PROMISE in alert["text"], want)
                self.assertEqual(PROMISE in alert["html"], want)

    def test_the_default_still_promises_it(self) -> None:
        self.assertIn(PROMISE, alerts.build_alert(URGENT_MAIL)["text"])


class ThePromiseIsNeverHardcodedTests(unittest.TestCase):
    """结构性防复发：这句承诺只能出现在三元/条件里，不许再变成无条件模板。

    上面那些行为测试盯着「传 True/False 的结果对不对」；这一条盯的是**下一个人
    把文案写死回去**——那时行为测试会因为默认参数而全绿，只有这里会红。
    """

    def test_reports_module_has_no_unconditional_trailer(self) -> None:
        text = (ROOT / "pilot_app" / "reports.py").read_text(encoding="utf-8")
        # 只允许出现在 brief_trailer() 的两个常量里，且其中一个不含这句承诺。
        self.assertIn("def brief_trailer(", text)
        self.assertIn('return BRIEF_TRAILER if full_follows else BRIEF_TRAILER_ONLY', text)
        # 断言不再有任何地方直接打印 BRIEF_TRAILER（必须经 brief_trailer）
        self.assertNotIn("+ _paragraph_html(BRIEF_TRAILER", text)
        self.assertNotIn('out += ["", BRIEF_TRAILER', text)

    def test_alerts_module_gates_the_promise_on_full_follows(self) -> None:
        text = (ROOT / "pilot_app" / "alerts.py").read_text(encoding="utf-8")
        # 这句承诺必须挂在三元表达式上（条件在前、承诺在后），而不是裸的一行。
        # 不去"扫描附近有没有 full_follows" —— 那种窗口搜索会误报（第一版就是这么错的），
        # 而钉住这个**确切的形状**既简单又不会说谎。
        self.assertIn(
            '("完整的中英双语报告（含行动项、联网核实来源与风险提示）正在生成，稍后单独发送。"\n'
            "         if full_follows else",
            text,
            "到达提醒里那句承诺又变成无条件的了 —— 站上关掉完整版时它会骗用户",
        )


if __name__ == "__main__":
    unittest.main()
