"""报告语言：**正文**用哪种语言写（2026-09-23）。

背景：界面上早就有「报告语言」这个下拉，它也真的作为 `preferred_language` 进了资料块
—— 但**没有任何一句指令要求模型照它写**，所以那个下拉基本是装饰。这一轮：

* 语言合并成一个设置：`users.ui_locale`（界面切换器写的）就是报告语言；
  老的 `profiles.language` 仍然认（选了 English 的人不该被悄悄换回中文）；
* `prompts.language_block()` 把这件事**写成指令**；
* 第 7 节从「固定的 English summary」改成「**另一种语言**的简短小结」。

三条判据都钉在「默认路径一个字都没变」上：不传 `locale` 时，prompt 的章节标题、
`normalize_report` 发射的标题、`report_locale` 的答案全部与改造前一致 ——
两千多条既有测试与 13 位存量用户的报告都不该因为这一轮改动而动。
"""

import json
import os
import re
import tempfile
import unittest

_TMP = tempfile.mkdtemp()
os.environ.setdefault("INFE_PILOT_DB", _TMP + "/reportlang.sqlite3")
os.environ.setdefault("INFE_PILOT_MASTER_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
os.environ.setdefault("INFE_PILOT_COOKIE_SECURE", "0")
os.environ.pop("INFE_PILOT_ORIGIN", None)

from pilot_app import database as database_mod  # noqa: E402
from pilot_app import prompts, reports  # noqa: E402

PROFILE_DEFAULTS = {
    "school_email": "", "major": "", "year_of_study": "", "courses": [], "interests": [],
    "career_goals": [], "focus_topics": [], "less_interested": [], "custom_instructions": "",
    "language": "bilingual", "timezone": "Asia/Hong_Kong", "theme": "paper", "background": "",
    "report_mode": "",
}


class PromptLanguageTests(unittest.TestCase):
    def test_the_language_is_actually_an_instruction_now(self):
        """**这是这一轮的重点**：语言必须出现在指令里，而不只是资料里的一行 JSON。"""
        for locale, name in (("zh-Hans", "简体中文"), ("zh-Hant", "繁體中文"), ("en", "English"),
                             ("ja", "日本語"), ("ko", "한국어")):
            block = prompts.language_block(locale)
            self.assertIn("<OUTPUT_LANGUAGE>", block)
            self.assertIn(name, block, locale)
        # 认不出的语言回落中文，而不是造出一句「用 None 写」。
        self.assertIn("简体中文", prompts.language_block("klingon"))

    def test_the_prompts_carry_it(self):
        profile = {"language": "bilingual"}
        message = {"subject": "s", "sender_name": "n", "sender_address": "a@cityu.edu.hk",
                   "received": "2026-09-23T00:00:00+00:00"}
        prompt = prompts.immediate_prompt(profile, message, [], "no search", locale="ja")
        self.assertIn("<OUTPUT_LANGUAGE>", prompt)
        self.assertIn("日本語", prompt)
        brief = prompts.brief_prompt(profile, message, [], "no search", locale="en")
        self.assertIn("<OUTPUT_LANGUAGE>", brief)

    def test_the_other_language_section_follows_the_report_language(self):
        """第 7 节是**另一种语言**的小结，不是固定的英文。

        选英文的人原来会看到一节叫「English summary」的英文小结 —— 重复，而且标题
        是假话。现在中文报告附英文、英文报告附中文。
        """
        self.assertEqual(prompts.immediate_sections("zh-Hans")[-1], "## 7. English summary")
        self.assertEqual(prompts.immediate_sections("zh-Hant")[-1], "## 7. English summary")
        self.assertEqual(prompts.immediate_sections("ja")[-1], "## 7. English summary")
        self.assertEqual(prompts.immediate_sections("ko")[-1], "## 7. English summary")
        self.assertIn("中文", prompts.immediate_sections("en")[-1])
        # 前 6 节一个不动：改的只有第 7 节那一行标题。
        self.assertEqual(prompts.immediate_sections("en")[:-1], prompts.IMMEDIATE_SECTIONS[:-1])

    def test_the_normaliser_emits_the_matching_heading(self):
        """`normalize_report` 会把标题**重写回**它认定的那一套，所以它也得知道语言。

        忘了传的话，英文报告的第 7 节会被改回「English summary」—— 标题与内容对不上。
        """
        raw = ("## 1. 重要程度与一句话结论\n- 等级：高\n\n"
               "## 7. English summary\n- Submit by Friday.\n")
        default = prompts.normalize_report(raw)
        english = prompts.normalize_report(raw, locale="en")
        self.assertIn("## 7. English summary", default)
        self.assertIn("中文", english)
        self.assertNotIn("## 7. English summary", english)

    def test_the_default_path_is_byte_identical(self):
        """不传 `locale` 时，章节标题与改造前逐字相同。"""
        self.assertEqual(prompts.immediate_sections(), prompts.IMMEDIATE_SECTIONS)
        self.assertEqual(prompts.DEFAULT_REPORT_LANGUAGE, "zh-Hans")


class ReportLocaleTests(unittest.TestCase):
    """一个设置管两件事：界面语言与报告语言。"""

    @classmethod
    def setUpClass(cls):
        cls.db = database_mod.Database(os.path.join(_TMP, "report-locale.sqlite3"))
        cls.db.initialize()

    def user(self, email: str) -> str:
        return self.db.create_user(email, "x" * 20, "")["id"]

    def test_nothing_chosen_means_chinese(self):
        """**存量用户一个字都不变**：老默认 `bilingual` 的含义正是中文正文 + 英文小结。"""
        user_id = self.user("a@example.com")
        self.assertEqual(self.db.report_locale(user_id), "zh-Hans")

    def test_the_ui_switch_decides_the_report_language(self):
        user_id = self.user("b@example.com")
        for code in ("en", "zh-Hant", "ja", "ko", "zh-Hans"):
            self.db.set_ui_locale(user_id, code)
            self.assertEqual(self.db.report_locale(user_id), code)

    def test_the_old_profile_choice_is_still_honoured(self):
        """选了 English 的人不该因为这次合并被悄悄换回中文。"""
        user_id = self.user("c@example.com")
        self.db.upsert_profile(user_id, dict(PROFILE_DEFAULTS, language="en"))
        self.assertEqual(self.db.report_locale(user_id), "en")
        # 而新切换器一旦表过态，它说了算。
        self.db.set_ui_locale(user_id, "ja")
        self.assertEqual(self.db.report_locale(user_id), "ja")
        self.db.set_ui_locale(user_id, "")
        self.assertEqual(self.db.report_locale(user_id), "en")
        # `bilingual` 是历史默认值，落在兜底上（= 中文），不是「英文」。
        self.db.upsert_profile(user_id, dict(PROFILE_DEFAULTS, language="bilingual"))
        self.assertEqual(self.db.report_locale(user_id), "zh-Hans")

    def test_an_unknown_account_does_not_crash(self):
        self.assertEqual(self.db.report_locale("usr_nope"), "zh-Hans")


class EmailLabelTests(unittest.TestCase):
    """邮件里的**固定标签**也跟着语言走（2026-09-23 下午，同一轮的 B 部分）。

    判据是**渲染出来的那一封信**，不是抽取器。这一轮真栽过一次：把常量写成
    `mark(CONTENT_DISCLAIMER)` —— 实参不是字面量，抽取器看不见，于是覆盖率报
    「0 缺」，而每封邮件的免责声明、优先级徽章、精简版尾句仍是中文。
    抽取器只能告诉你「源码里有几条 t()」，能告诉你「用户看见了什么」的只有渲染结果。
    """

    REPORT = (
        "## 1. 重要程度与一句话结论\n- 等级：高\n- 结论：周五前交作业。\n\n"
        "## 2. 必须采取的行动与截止时间\n- 在 Canvas 提交作业\n\n"
        "## 3. 邮件内容总结\n- 老师提醒了截止时间。\n\n"
        "## 4. 与我的学业的相关性\n- 影响成绩。\n\n"
        "## 5. 联网搜索后的建议\n- 见 https://example.com/policy\n\n"
        "## 6. 风险、未知与推测\n- 暂无疑问。\n\n"
        "## 7. English summary\n- Submit before Friday.\n"
    )

    #: 这些是**我们写的**标签（不是模型写的正文）。选了英文之后一个都不该再出现。
    LABELS = (
        "重要 · 需要尽快处理", "AI 生成内容可能出错", "【你应该做什么】", "你应该做什么 / What to do",
        "邮件讲了什么 / What the email says", "发件人：", "收件时间：", "邮件头优先级：",
        "【你要做什么】", "无需行动", "未知发件人", "这是精简即时摘要", "邮件内容要点",
        "今日邮件", "需要行动", "最近截止", "异常/失败", "紧急待办", "学业相关", "机会与活动",
        "行政通知", "低优先级与营销", "处理失败", "异常与整体说明", "每日简报",
    )

    def _message(self, **overrides):
        base = {"id": "msg_1", "subject": "作业截止", "sender_name": "老师",
                "sender_address": "teacher@cityu.edu.hk",
                "received": "2026-09-23T02:42:00+00:00", "importance": "normal"}
        base.update(overrides)
        return base

    def _renders(self, locale):
        message = self._message()
        immediate = reports.render_immediate(self.REPORT, message, subject="【AI邮件摘要】作业截止",
                                             timezone="Asia/Hong_Kong", locale=locale)
        brief = reports.render_brief(self.REPORT, message, subject="【AI邮件摘要·精简】作业截止",
                                     timezone="Asia/Hong_Kong", locale=locale)
        digest_message = self._message(status="sent", received_at="2026-09-23T02:00:00+00:00",
                                       body_markdown="encrypted", last_error="")
        digest = reports.build_digest([digest_message], {"msg_1": self.REPORT},
                                      timezone="Asia/Hong_Kong")
        subject = reports.digest_subject(digest, locale=locale)
        daily = reports.render_digest(digest, subject=subject, locale=locale)
        return {
            "immediate.html": immediate["html"], "immediate.text": immediate["text"],
            "brief.html": brief["html"], "brief.text": brief["text"],
            "digest.html": daily["html"], "digest.text": daily["text"],
            "digest.subject": subject,
            "digest.markdown": reports.digest_markdown(digest, locale=locale),
        }

    def test_the_english_email_has_no_chinese_labels(self):
        rendered = self._renders("en")
        for name, text in rendered.items():
            for label in self.LABELS:
                self.assertNotIn(label, text, f"{name} 里还有中文标签：{label}")
        self.assertIn("What to do", rendered["immediate.html"])
        self.assertIn("Important · handle soon", rendered["immediate.html"])
        self.assertIn("Urgent", rendered["digest.markdown"])

    def test_the_other_three_languages_really_change_the_labels(self):
        for locale, sample in (("zh-Hant", "你應該做什麼"), ("ja", "やること"), ("ko", "해야 할 일")):
            rendered = self._renders(locale)
            self.assertIn(sample, rendered["immediate.html"], locale)
            self.assertNotIn("你应该做什么", rendered["immediate.html"], locale)
            self.assertNotIn("紧急待办", rendered["digest.markdown"], locale)

    def test_the_chinese_path_is_untouched(self):
        """不传 `locale`（存量用户那条路）与传 `zh-Hans` 必须**逐字节相同**。"""
        message = self._message()
        parsed = reports.parse_report(self.REPORT, message=message, timezone="Asia/Hong_Kong")
        self.assertEqual(reports.render_immediate_text(parsed, subject="s"),
                         reports.render_immediate_text(parsed, subject="s", locale="zh-Hans"))
        text = reports.render_immediate_text(parsed, subject="s")
        self.assertIn("【你应该做什么】", text)
        self.assertIn("重要程度：重要 · 需要尽快处理", text)
        self.assertIn("发件人：老师", text)

    def test_no_placeholder_survives_rendering(self):
        """译文里多一个 `{…}`，用户看到的就是「{n} emails」——覆盖率看不出来，这条能。"""
        for locale in ("zh-Hans", "zh-Hant", "en", "ja", "ko"):
            for name, text in self._renders(locale).items():
                self.assertEqual(re.findall(r"\{[a-z_]+\}", text), [], f"{locale} {name}")

    def test_the_registered_labels_match_the_constants(self):
        """`_REGISTERED_EMAIL_LABELS` 是给抽取器看的**副本**：常量改了它没改，
        覆盖率就会「0 缺」地骗人。所以拿常量逐个对一遍。"""
        registered = set(reports._REGISTERED_EMAIL_LABELS)
        for text in (reports.CONTENT_DISCLAIMER, reports.SYNTHESIS_HEADING,
                     reports.BRIEF_TRAILER, reports.BRIEF_TRAILER_ONLY):
            self.assertIn(text, registered)
        for label, _english in reports._PRIORITY_LABELS.values():
            self.assertIn(label, registered)
        for title in reports.CATEGORY_TITLES.values():
            self.assertIn(title, registered)


class MomentFormatTests(unittest.TestCase):
    """**收件时刻**也跟着语言走（2026-09-23 深夜，`docs/report-language-2026-09-23.md` §四那件）。

    为什么单独一条：这个日期是**库里存下来的一段文本**（`parse_report` / `build_digest`
    落库时写的是中文写法），而它又被塞进 `t("收件时间：{when}")` 当参数 ——
    信封翻了、里面的日期没翻，英文用户看到的是一句英文里夹着「9月13日 10:42」。
    修法不是改库里那份（落库时还不知道读的人选哪种语言），而是渲染时从 ISO 原值现算：
    所以判据要**看渲染出来的那封信**，光测 `format_moment()` 挡不住
    「某一条渲染路径忘了走 `display_when()`」。
    """

    REPORT = (
        "## 1. 重要程度与一句话结论\n- 等级：高\n- 结论：周五前交作业。\n\n"
        "## 2. 必须采取的行动与截止时间\n- 在 Canvas 提交作业\n\n"
        "## 3. 邮件内容总结\n- 老师提醒了截止时间。\n"
    )
    MESSAGE = {"subject": "作业提醒", "sender_name": "教务处", "sender_address": "reg@cityu.edu.hk",
               "received": "2026-09-13T02:42:00+00:00"}

    def _parsed(self):
        return reports.parse_report(self.REPORT, message=self.MESSAGE,
                                    timezone="Asia/Hong_Kong", subject="作业提醒")

    def test_each_language_gets_its_own_date_shape(self):
        value = "2026-09-13T02:42:00+00:00"
        cases = {"zh-Hans": "9月13日 10:42", "zh-Hant": "9月13日 10:42",
                 "ja": "9月13日 10:42", "ko": "9월 13일 10:42", "en": "Sep 13, 10:42"}
        for locale, want in cases.items():
            self.assertEqual(reports.format_moment(value, "Asia/Hong_Kong", locale=locale), want, locale)

    def test_the_stored_value_stays_chinese_so_sorting_does_not_move(self):
        """库里那份**不动**：它同时被用来给待办排序（`tasks.sort(...)`），
        换个语言就换一次顺序。渲染时才换写法。"""
        parsed = self._parsed()
        self.assertEqual(parsed["received_display"], "9月13日 10:42")
        self.assertEqual(reports.display_when(parsed, locale="en"), "Sep 13, 10:42")

    def test_the_english_email_carries_an_english_moment(self):
        parsed = self._parsed()
        for name, text in (("immediate-html", reports.render_immediate_html(parsed, locale="en")),
                           ("immediate-text", reports.render_immediate_text(parsed, locale="en")),
                           ("brief-html", reports.render_brief_html(self.REPORT, self.MESSAGE,
                                                                    subject="作业提醒", locale="en"))):
            self.assertIn("Sep 13, 10:42", text, name)
            self.assertNotIn("9月13日", text, name)

    def test_chinese_still_reads_the_way_it_always_did(self):
        parsed = self._parsed()
        self.assertIn("9月13日 10:42", reports.render_immediate_html(parsed, locale="zh-Hans"))

    def test_a_missing_moment_is_translated_rather_than_chinese(self):
        self.assertEqual(reports.format_moment(None, locale="en"), "Time not provided")
        self.assertEqual(reports.format_moment(None, locale="zh-Hans"), "时间未提供")


if __name__ == "__main__":
    unittest.main()
