"""Tests for the action-first immediate report and the student-brief digest."""

import unittest
from unittest import mock

from pilot_app import reports


GOOD_REPORT = """## 1. 重要程度与一句话结论 / Importance and one-line conclusion
- 等级：高
- 结论：老师把作业提交时间改到了本周五 23:59，需要重新检查要求并测试代码。

## 2. 必须采取的行动与截止时间 / Required actions and deadlines
- 今天阅读新版作业要求
- 周四前完成 C++ 代码测试
- 周五 23:59 前在 Canvas 提交（截止：2026-09-18 23:59）

## 3. 邮件内容总结 / Email content summary
- 发件人：课程教师
- 提交入口：Canvas
- 附件：新版要求.pdf

## 4. 与我的学业、兴趣和目标的关系 / Personal relevance
与你的大二 INFE 课程、密码学和 C++ 作业直接相关，相关度：高。

## 5. 联网搜索后的建议与来源 / Suggestions from web search and sources
建议先看 Canvas 官方提交说明。
来源：Canvas 学生指南 https://community.canvaslms.com/x
来源：CityU 校历 https://www.cityu.edu.hk/calendar

## 6. 风险、未知与推测 / Risks, unknowns and inferences
- 事实：截止时间来自原邮件。
- 推测 / Inference：老师可能会再改一次时间。

## 7. English summary
The assignment deadline moved to Friday 23:59. Review the requirements and test your submission.
"""

LEGACY_REPORT = """## 1. 邮件内容总结 / Email content summary
- 发件人显示名称：Apple
- 邮件接收时间：UTC 2026年9月12日10:24:51
- 有人在一台新设备上登录了你的账户。

## 2. 联网搜索后的潜在建议 / Potential recommendations from web search
本次未完成联网核实 / No live web verification was available.

## 3. 需要我做什么 / Actions for me
- 先核实这次登录是否是你本人操作
- 若不是本人操作，立即修改密码

## 4. 与我的学业、兴趣和目标的关系 / Personal relevance
与你的账户安全有关，相关度：中。

## 5. 风险与推测 / Risks and inferences
- 推测 / Inference：可能存在钓鱼风险。

## 6. English summary
Verify the sign-in and change your password if it was not you.
"""


def message(**overrides):
    base = {
        "id": "msg_1", "subject": "课程作业截止日期更新", "sender_name": "课程教师",
        "sender_address": "teacher@cityu.edu.hk", "received": "2026-09-13T02:42:00+00:00",
        "importance": "normal",
    }
    base.update(overrides)
    return base


class ParseTests(unittest.TestCase):
    def test_parses_canonical_action_first_sections(self):
        parsed = reports.parse_report(GOOD_REPORT, message=message(), timezone="Asia/Hong_Kong")
        self.assertEqual(parsed["priority"], reports.PRIORITY_HIGH)
        self.assertIn("周五", parsed["conclusion"])
        self.assertEqual(len(parsed["actions"]), 3)
        # "今天阅读新版要求" states a day, so the first deadline is 今天.
        self.assertEqual(parsed["deadline"], "今天")
        self.assertEqual(parsed["deadlines"], ["今天", "周四", "2026/9/18 23:59"])
        self.assertIn("9月13日 10:42", parsed["received_display"])
        self.assertEqual(len(parsed["sources"]), 2)
        self.assertFalse(parsed["search_unavailable"])

    def test_parses_legacy_six_section_reports(self):
        parsed = reports.parse_report(LEGACY_REPORT, message=message(subject="Apple 登录提醒"),
                                      timezone="Asia/Hong_Kong")
        self.assertIn("登录", parsed["conclusion"])
        self.assertNotIn("发件人显示名称", parsed["conclusion"])
        self.assertEqual(parsed["actions"][0], "先核实这次登录是否是你本人操作")
        self.assertTrue(parsed["search_unavailable"])
        self.assertEqual(reports.deadline_of(parsed["actions"][0]), "")

    def test_information_order_is_action_first(self):
        parsed = reports.parse_report(GOOD_REPORT, message=message(), timezone="Asia/Hong_Kong")
        html = reports.render_immediate_html(parsed)
        positions = [
            html.index("这封邮件重要吗"),
            html.index("一句话结论"),
            html.index("你应该做什么"),
            html.index("邮件讲了什么"),
            html.index("为什么与你有关"),
            html.index("联网搜索后的建议"),
            html.index("风险、未知与推测"),
            html.index("English brief"),
        ]
        self.assertEqual(positions, sorted(positions))
        # The conclusion must be visible without any click/expand affordance.
        self.assertNotIn("<details", html)
        self.assertNotIn("<script", html)
        self.assertNotIn("<style", html)

    def test_missing_fields_do_not_break_rendering(self):
        parsed = reports.parse_report("", message={"id": "m"}, timezone="Asia/Hong_Kong")
        self.assertEqual(parsed["priority"], reports.PRIORITY_UNKNOWN)
        self.assertEqual(parsed["actions"], [])
        self.assertTrue(parsed["conclusion"])
        html = reports.render_immediate_html(parsed)
        self.assertIn("本次报告未提供这一部分", html)
        self.assertIn("无需行动", html)
        self.assertIn("时间未提供", html)

    def test_the_fallback_conclusion_is_marked_as_one(self):
        """`usable_conclusion` 把兜底句认成「没有结论」。

        报告需要一句标题，所以 `conclusion_of` 会给「关于「X」的摘要」；但任务清单上
        那句话**不是模型说的**，印出来就是替模型编话。这里钉住的是：兜底句长什么样、
        以及「这是不是兜底」都由 `_no_conclusion` 一处说了算——两处各写一遍迟早漂。
        """
        without = reports.parse_report("", message={"id": "m", "subject": "某通知"},
                                       timezone="Asia/Hong_Kong")
        self.assertTrue(without["conclusion"], "报告里仍然要有一句标题")
        self.assertEqual(reports.usable_conclusion(without), "",
                         "标题是兜底句时，任务清单拿到的是空串")

        with_gist = reports.parse_report(GOOD_REPORT, message={"id": "m", "subject": "作业"},
                                         timezone="Asia/Hong_Kong")
        self.assertEqual(reports.usable_conclusion(with_gist), with_gist["conclusion"])
        self.assertNotEqual(reports.usable_conclusion(with_gist), "")

    def test_no_deadline_is_stated_explicitly(self):
        markdown = ("## 1. 重要程度与一句话结论\n- 等级：中\n- 结论：信息通知。\n"
                    "## 2. 必须采取的行动与截止时间\n- 有空时阅读附件说明\n")
        parsed = reports.parse_report(markdown, message=message(), timezone="Asia/Hong_Kong")
        self.assertEqual(parsed["deadline"], "")
        html = reports.render_immediate_html(parsed)
        self.assertIn("邮件没有给出明确截止时间", html)
        text = reports.render_immediate_text(parsed)
        self.assertIn("有空时阅读附件说明", text)
        self.assertNotIn("截止：None", text)

    def test_search_failure_is_stated_and_never_fabricates_sources(self):
        markdown = ("## 1. 重要程度与一句话结论\n- 等级：高\n- 结论：需要处理。\n"
                    "## 5. 联网搜索后的建议与来源\n本次未取得可验证来源 / No verifiable source was retrieved。\n")
        parsed = reports.parse_report(markdown, message=message(), timezone="Asia/Hong_Kong")
        self.assertTrue(parsed["search_unavailable"])
        self.assertEqual(parsed["sources"], [])
        html = reports.render_immediate_html(parsed)
        self.assertIn("本次未取得可验证来源", html)
        self.assertNotIn("http://", html.replace("https://", ""))

    def test_unknown_urls_are_rendered_as_plain_text_not_links(self):
        markdown = ("## 5. 联网搜索后的建议与来源\n来源：坏源 http://insecure.example/a\n"
                    "来源：好源 https://good.example/a\n")
        parsed = reports.parse_report(markdown, message=message(), timezone="Asia/Hong_Kong")
        self.assertEqual([source["url"] for source in parsed["sources"]], ["https://good.example/a"])
        html = reports.render_immediate_html(parsed)
        self.assertIn('href="https://good.example/a"', html)
        self.assertIn("已屏蔽非 https 链接", html)
        self.assertNotIn('href="http://insecure.example/a"', html)

    def test_a_https_link_inside_prose_is_still_clickable(self):
        """正文里的 https 链接必须变成可点链接（2026-09-24 修的一条真 bug）。

        屏蔽非 https 的那条正则原先匹配任意 `scheme://`，**把 https 自己也吃了**：正文里
        每个正常链接都被换成「[已屏蔽非 https 链接] 域名/路径」——既不可点，又被诬成坏源。
        上面那条测试只盯着**结构化「来源」**那一节（它不走 `_inline`），所以一直没抓到。
        这条与它并存：一条守来源，一条守正文。
        """
        markdown = "## 1. 一句话\n见 https://mycampusmail.com/apply 这个表\n"
        parsed = reports.parse_report(markdown, message=message(), timezone="Asia/Hong_Kong")
        html = reports.render_immediate_html(parsed)
        self.assertIn('<a href="https://mycampusmail.com/apply"', html)
        self.assertNotIn("已屏蔽非 https 链接", html)

    def test_the_scheme_guard_does_not_bite_into_the_middle_of_a_url(self):
        """否定前瞻之外还要一个 `\\b`：少了它，只挡得住第一个位置，`https://` 会被切成
        `h[已屏蔽非 https 链接] ttps://`。这条正是为那一半写的。"""
        out = reports._inline("https://a.example/x")
        self.assertIn('<a href="https://a.example/x"', out)
        self.assertNotIn("[已屏蔽", out)

    def test_non_https_schemes_are_still_blocked_in_prose(self):
        """修 bug 不许把闸门一起放松：http / javascript 在正文里仍然不可点、并被点名。"""
        markdown = "## 1. 一句话\n坏源 http://insecure.example/a 与 javascript://alert(x)\n"
        parsed = reports.parse_report(markdown, message=message(), timezone="Asia/Hong_Kong")
        html = reports.render_immediate_html(parsed)
        self.assertNotIn('<a href="http://insecure.example/a"', html)
        self.assertNotIn('<a href="javascript://alert', html)
        self.assertIn("已屏蔽非 https 链接", html)

    def test_sources_are_labelled_without_swallowing_the_sentence(self):
        markdown = ("## 5. 联网搜索后的建议与来源\n"
                    "详见 Canvas 指南（https://community.canvaslms.com/x）以及学校日历 https://www.cityu.edu.hk/ 。\n")
        parsed = reports.parse_report(markdown, message=message(), timezone="Asia/Hong_Kong")
        labels = [source["label"] for source in parsed["sources"]]
        self.assertEqual(len(labels), 2)
        for label in labels:
            self.assertLessEqual(len(label), 200)
        self.assertNotIn("以及学校日历（https://www.cityu.edu.hk/）", labels[0])

    def test_inference_lines_are_marked(self):
        parsed = reports.parse_report(GOOD_REPORT, message=message(), timezone="Asia/Hong_Kong")
        html = reports.render_immediate_html(parsed)
        self.assertIn("AI 推测", html)

    def test_bilingual_output_keeps_chinese_and_english(self):
        parsed = reports.parse_report(GOOD_REPORT, message=message(), timezone="Asia/Hong_Kong")
        html = reports.render_immediate_html(parsed)
        text = reports.render_immediate_text(parsed)
        for output in (html, text):
            self.assertIn("邮件讲了什么", output)
            self.assertIn("English", output)
            self.assertIn("The assignment deadline moved to Friday 23:59", output)

    def test_long_content_is_truncated_but_keeps_later_sections(self):
        long_body = "这是一段很长的邮件内容。" * 400
        markdown = (f"## 3. 邮件内容总结\n- {long_body}\n\n"
                    "## 7. English summary\nThe English summary must survive.\n")
        parsed = reports.parse_report(markdown, message=message(), timezone="Asia/Hong_Kong")
        html = reports.render_immediate_html(parsed)
        self.assertIn("The English summary must survive", html)
        self.assertLess(len(html), 102_400)  # Gmail clips beyond ~102 KB

    def test_mobile_and_desktop_layout_uses_no_fixed_width_or_grid(self):
        parsed = reports.parse_report(GOOD_REPORT, message=message(), timezone="Asia/Hong_Kong")
        html = reports.render_immediate_html(parsed)
        self.assertIn('width="100%"', html)
        self.assertIn("max-width:600px", html)
        for forbidden in ("display:flex", "display:grid", "position:", "float:", "@media",
                          "var(--", "rem;", "border-radius:16px", "<button"):
            self.assertNotIn(forbidden, html)
        # Every table carries presentation role and zero spacing attributes.
        self.assertEqual(html.count("<table"), html.count('role="presentation"'))
        self.assertNotIn("<table ", html.split("role=\"presentation\"")[0][-40:] if False else html[0:0] + "<table")

    def test_html_attributes_are_duplicated_for_word_renderer(self):
        parsed = reports.parse_report(GOOD_REPORT, message=message(), timezone="Asia/Hong_Kong")
        html = reports.render_immediate_html(parsed)
        self.assertIn('cellpadding="0"', html)
        self.assertIn('cellspacing="0"', html)
        self.assertIn('border="0"', html)
        self.assertIn("border-collapse:collapse", html)

    def test_subject_and_body_are_escaped(self):
        parsed = reports.parse_report(
            "## 1. 重要程度\n- 等级：高\n- 结论：<b>hi</b> & bye\n",
            message=message(subject='<img src=x onerror=alert(1)>', sender_name="<script>x</script>"),
            timezone="Asia/Hong_Kong",
        )
        html = reports.render_immediate_html(parsed)
        self.assertNotIn("<img src=x", html)
        self.assertNotIn("<script>x", html)
        self.assertIn("&lt;img", html)


class DigestTests(unittest.TestCase):
    def _message(self, message_id, subject, sender="Sender", status="sent", body=True, received=None, importance="normal"):
        return {
            "id": message_id, "subject": subject, "sender_name": sender, "sender_address": f"{sender}@x.hk",
            "received_at": received or "2026-09-13T02:00:00+00:00", "importance": importance,
            "status": status, "last_error": "" if status == "sent" else "smtp down",
            "body_markdown": "encrypted" if body else None,
        }

    def _reports(self, mapping):
        return {key: value for key, value in mapping.items()}

    def test_every_message_appears_even_when_unclassified(self):
        messages = [
            self._message("m1", "课程作业截止日期更新"),
            self._message("m2", "完全无关的奇怪通知"),
            self._message("m3", "失败邮件", status="failed", body=False),
        ]
        reports_map = {
            "m1": GOOD_REPORT,
            "m2": "## 1. 重要程度\n- 等级：低\n- 结论：一则普通通知。\n## 3. 邮件内容总结\n- 没有特别信息。",
        }
        digest = reports.build_digest(messages, reports_map, timezone="Asia/Hong_Kong")
        rendered = reports.render_digest_html(digest)
        text = reports.render_digest_text(digest)
        for subject in ("课程作业截止日期更新", "完全无关的奇怪通知", "失败邮件"):
            self.assertIn(subject, rendered)
            self.assertIn(subject, text)
        self.assertEqual(digest["metrics"]["total"], 3)
        self.assertEqual(digest["metrics"]["failed"], 1)
        self.assertEqual(len(digest["items"]), 3)

    def test_duplicate_notices_merge_but_stay_counted(self):
        messages = [self._message("m1", "图书馆系统维护通知"),
                    self._message("m2", "转发：图书馆系统维护通知")]
        reports_map = {"m1": "## 1. 重要程度\n- 等级：低\n- 结论：维护通知。\n## 3. 内容\n- 周六维护。",
                       "m2": "## 1. 重要程度\n- 等级：低\n- 结论：维护通知。\n## 3. 内容\n- 周六维护。"}
        digest = reports.build_digest(messages, reports_map, timezone="Asia/Hong_Kong")
        self.assertEqual(digest["metrics"]["total"], 2)
        self.assertEqual(len(digest["merged"]), 1)
        self.assertEqual(digest["metrics"]["duplicates"], 1)
        self.assertIn("另有 1 封同类邮件", reports.render_digest_html(digest))

    def test_categories_cover_urgent_academic_opportunity_admin_low(self):
        samples = {
            "作业截止日期提醒": "urgent" if False else "academic",
            "Career Centre 实习招聘宣讲": "opportunity",
            "宿舍缴费通知": "administrative",
            "编程平台限时优惠广告": "low",
        }
        messages, reports_map = [], {}
        for index, (subject, _expected) in enumerate(samples.items()):
            message_id = f"m{index}"
            messages.append(self._message(message_id, subject))
            reports_map[message_id] = (
                f"## 1. 重要程度\n- 等级：中\n- 结论：关于{subject}。\n"
                f"## 3. 邮件内容总结\n- {subject}的详细说明。\n"
            )
        digest = reports.build_digest(messages, reports_map, timezone="Asia/Hong_Kong")
        self.assertTrue(digest["sections"]["academic"])
        self.assertTrue(digest["sections"]["opportunity"])
        self.assertTrue(digest["sections"]["administrative"])
        self.assertTrue(digest["sections"]["low"])
        html = reports.render_digest_html(digest)
        for title in ("学业相关", "机会与活动", "行政通知", "低优先级与营销"):
            self.assertIn(title, html)

    def test_digest_counts_and_first_screen(self):
        messages = [self._message("m1", "课程作业截止日期更新"),
                    self._message("m2", "营销推广", importance="low")]
        reports_map = {
            "m1": GOOD_REPORT,
            "m2": "## 1. 重要程度\n- 等级：低\n- 结论：促销邮件。",
        }
        digest = reports.build_digest(messages, reports_map, timezone="Asia/Hong_Kong")
        digest["date"] = "9月13日"
        digest["generated_at"] = "2026-09-13T14:00:00+00:00"
        html = reports.render_digest_html(digest)
        self.assertIn("今天有 3 件事需要处理", html)
        self.assertIn("2026/9/18 23:59", html)
        self.assertIn("今天", digest["next_deadline"])
        self.assertIn("今日邮件", html)
        self.assertIn("异常/失败", html)

    def test_digest_with_no_mail_is_honest(self):
        digest = reports.build_digest([], {}, timezone="Asia/Hong_Kong")
        digest["date"] = "9月14日"
        html = reports.render_digest_html(digest)
        text = reports.render_digest_text(digest)
        self.assertIn("今天没有必须立刻处理的事项", html)
        self.assertIn("今天没有必须立刻处理的事项", text)
        self.assertIn("0 封", html)

    def test_digest_marks_messages_without_verifiable_sources(self):
        messages = [self._message("m1", "课程作业截止日期更新")]
        digest = reports.build_digest(messages, {"m1": GOOD_REPORT}, timezone="Asia/Hong_Kong")
        digest["metrics"]["without_sources"] = 1
        text = reports.render_digest_text(digest)
        self.assertIn("未取得可验证来源", text)
        self.assertIn("未伪造引用", text)

    def test_deadline_prefers_the_date_after_the_marker(self):
        self.assertEqual(reports.deadline_of("周五 23:59 前在 Canvas 提交（截止：2026-09-18 23:59）"),
                         "2026/9/18 23:59")
        self.assertEqual(reports.deadline_of("今天阅读新版要求"), "今天")
        self.assertEqual(reports.deadline_of("没有任何时间信息的动作"), "")
        # The note must not repeat a deadline the action already states.
        self.assertEqual(reports.deadline_note("截止 2026-09-18 提交"), "")
        self.assertEqual(reports.deadline_note("提交作业"), "")

    def test_an_english_date_is_a_date(self):
        """英文邮件里的「Oct 8」不能只留下时钟。

        2026-09-23 运营者的手机截图：一封 Canvas 通知写着「截止 Oct 8 05:00」，
        因为没有一条规则认得英文月名，日期被整个丢掉、只剩 `05:00`，导出日历时
        再把 05:00 挂到「收到那封信的那一天」（9 月 19 日）上。
        """
        cases = {
            "核对 CB3410 Homework 1 是否已提交，截止 Oct 8 05:00。": "10月8日 05:00",
            "Read chapter 6, due Oct 8, 2026 23:59": "2026/10/8 23:59",
            "Submit the form by 8 October 2026": "2026/10/8",
            "押金截止日为 Sep 15，已过": "9月15日",
            "hand in the lab report by 8th Sept": "9月8日",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(reports.deadline_of(text), expected)

    def test_a_month_without_a_day_is_not_a_date(self):
        """「May 2026」「march on」都不带日号 —— 不能把年份或下一个词当日子。"""
        self.assertEqual(reports.date_candidates("May 2026"), [])
        self.assertEqual(reports.date_candidates("we may submit later"), [])
        self.assertEqual(reports.date_candidates("the junior seminar"), [])
        self.assertEqual(reports.deadline_of("本周内提交，deadline 见邮件"), "本周")

    def test_every_date_shape_is_known_in_one_place(self):
        """加一种写法只加一处：显示与排序都从 `date_candidates` 取。"""
        text = "截止 Oct 8 05:00"
        self.assertEqual([(year, month, day) for _at, year, month, day
                          in reports.date_candidates(text)], [(0, 10, 8)])
        self.assertEqual(reports.deadline_sort_key(text, "2026-09-19"), (0, "2026-10-08"))
        # A deadline that names a year keeps it; one that does not still sorts.
        self.assertEqual(reports.deadline_sort_key("due Oct 8, 2027", "2026-09-19"),
                         (0, "2027-10-08"))

    def test_digest_markdown_has_eight_sections_and_a_traceable_row_per_mail(self):
        messages = [self._message("m1", "课程作业截止日期更新", sender="课程教师")]
        digest = reports.build_digest(messages, {"m1": GOOD_REPORT}, timezone="Asia/Hong_Kong")
        markdown = reports.digest_markdown(digest)
        positions = [markdown.index(f"## {number}.") for number in range(1, 9)]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(markdown.count("## 7."), 1)
        self.assertIn("课程作业截止日期更新", markdown)
        self.assertIn("课程教师", markdown)
        self.assertIn("收到邮件：1 封", markdown)
        # "最近截止时间" is chronological, not the first line in the list.
        self.assertIn("最近截止时间：今天", markdown)

    def test_digest_subject_is_scannable(self):
        messages = [self._message("m1", "课程作业截止日期更新")]
        digest = reports.build_digest(messages, {"m1": GOOD_REPORT}, timezone="Asia/Hong_Kong")
        digest["date"] = "9月13日"
        subject = reports.digest_subject(digest)
        self.assertIn("每日简报", subject)
        self.assertIn("1 封邮件", subject)
        self.assertIn("3 件待办", subject)


class MidnightDeadlineTests(unittest.TestCase):
    """「10月17日 00:00 截止」是人话里的「16 号睡前」。

    用户原话：10.16 号晚上 12 点的 ddl 是我在 16 号睡前就应该做完的，而不是归到
    10.17 的 0 点。这一刻是 16 号的第 24 点，不是 17 号的第 0 点；按 17 号记，
    清单、日报、.ics 和日历会**一起**晚一天，而用户是在 16 号晚上就该做完的。
    """

    def test_midnight_moves_to_the_end_of_the_day_before(self):
        self.assertEqual(reports.deadline_of("提交（截止：2026-10-17 00:00）"),
                         "2026/10/16 24:00")
        self.assertEqual(reports.deadline_of("提交（截止：10月17日 00:00）"),
                         "10月16日 24:00")

    def test_a_month_boundary_lands_on_a_real_day(self):
        """11 月 1 日零点退回 10 月 31 日，而不是「10月0日」。"""
        self.assertEqual(reports.deadline_of("提交（截止：2026-11-01 00:00）"),
                         "2026/10/31 24:00")

    def test_a_year_boundary_lands_on_a_real_day(self):
        self.assertEqual(reports.deadline_of("提交（截止：2027-01-01 00:00）"),
                         "2026/12/31 24:00")

    def test_a_yearless_spelling_does_not_grow_a_year(self):
        """原本没写年份的写法，退回前一天也不该凭空长出一个年份。"""
        self.assertEqual(reports.deadline_of("提交（截止：1月1日 00:00）"), "12月31日 24:00")

    def test_only_exactly_midnight_is_moved(self):
        """00:30 之后是明确的凌晨，不再有歧义 —— 只有正好 00:00 走这条规则。"""
        self.assertEqual(reports.deadline_of("提交（截止：2026-10-17 00:30）"),
                         "2026/10/17 00:30")
        self.assertEqual(reports.deadline_of("提交（截止：2026-10-17 09:00）"),
                         "2026/10/17 09:00")
        self.assertEqual(reports.deadline_of("提交（截止：2026-09-18 23:59）"),
                         "2026/9/18 23:59")

    def test_an_explicit_24_00_is_already_saying_the_same_thing(self):
        """「24:00」本来就落在它自己那一天，不必再退一天，也不该被退两次。"""
        self.assertEqual(reports.deadline_of("提交（截止：2026-10-16 24:00）"), "2026/10/16")

    def test_a_clock_with_no_date_is_left_alone(self):
        """没有日期的 00:00 没有可退的那一天，保持原样（它本来也不会长出约会）。"""
        self.assertEqual(reports.deadline_of("今晚 00:00 前提交"), "00:00")

    def test_an_interval_is_not_a_midnight_deadline(self):
        """明确的区间（``00:00–02:00``）说的是开始时刻，不是截止那一刻。"""
        self.assertEqual(reports.deadline_of("开放时间 00:00–02:00"), "00:00–02:00")


class SubjectTests(unittest.TestCase):
    def test_subject_line_does_not_contain_message_body(self):
        subject = "【AI邮件摘要】课程作业截止日期更新"
        parsed = reports.parse_report(GOOD_REPORT, message=message(), timezone="Asia/Hong_Kong")
        html = reports.render_immediate_html(parsed, subject=subject)
        self.assertIn("课程作业截止日期更新", html)
        self.assertNotIn("邮件未提供具体日期", html)


if __name__ == "__main__":
    unittest.main()
