# -*- coding: utf-8 -*-
"""段标题与解析表必须一致 —— 2026-09-23 的「第 3 段永远是 - 无。」就是这个不一致。

## 当时的形状（值得记住）

`normalize_brief_report()` 按 `BRIEF_SECTIONS` 拼输出，缺哪段就填 `- 无。`。
而"缺"不是模型没写，是 `_section_key()` **认不出那个标题**：

* 精简版第 3 段叫「邮件内容要点 / Key points」，`SECTION_KEYWORDS` 里只有
  「邮件内容总结 / email content summary」——**两套标题，只登记了一套**；
* 认不出 → 落到 `LEGACY_POSITION_KEYS[3]` = `"actions"` → 而 `"actions"` 已被第 2 段
  占用（`key not in captured`）→ **第 3 段正文被整段丢掉** → 再补一个 `- 无。`。

所以症状是"两家供应商的报告第 3 段全是 `- 无。`"，看起来像模型能力问题。A/B 实测
（同提示词、直接取原文）证明模型写得好好的：演讲者、主办方、咨询邮箱都在。

## 这里钉什么

不只钉「要点」这一个词，而是**结构性**地钉住这类 bug：凡是会出现在提示词里的段标题，
都必须能被 `_section_key()` 认出来。以后谁加了新标题却忘了登记，这里当场变红。
"""

from __future__ import annotations

import re
import unittest

from pilot_app import prompts

# 精简版（BRIEF_SECTIONS）与完整版（IMMEDIATE_SECTIONS）就是"我们会发给模型的标题"，
# 从模块里现取，不在这里抄第二份 —— 抄一份就会漂一份。
BRIEF_HEADINGS = list(prompts.BRIEF_SECTIONS)
FULL_HEADINGS = list(prompts.IMMEDIATE_SECTIONS)


def heading_body(heading: str) -> str:
    """`## 3. 邮件内容要点 / Key points` → `邮件内容要点 / Key points`。"""
    return re.sub(r"^#+\s*\d+\s*[.)、]?\s*", "", heading).strip()


class EveryHeadingIsRecognisedTests(unittest.TestCase):
    """任何会发出去的段标题都必须有对应的 key（这条才是防复发的）。"""

    def test_every_brief_heading_maps_to_a_key(self) -> None:
        for position, heading in enumerate(BRIEF_HEADINGS, start=1):
            key = prompts._section_key(heading_body(heading), position)
            self.assertTrue(key, f"精简版第 {position} 段的标题没被认出来：{heading!r}")

    def test_every_full_heading_maps_to_a_key(self) -> None:
        for position, heading in enumerate(FULL_HEADINGS, start=1):
            key = prompts._section_key(heading_body(heading), position)
            self.assertTrue(key, f"完整版第 {position} 段的标题没被认出来：{heading!r}")

    def test_the_two_layouts_do_not_collide(self) -> None:
        """两套标题要映到**各自的** key，不能一个盖住另一个。

        这条正是当时失败的机制：精简版第 3 段映成了 `actions`，与第 2 段撞车，
        撞车的结果是内容被静默丢掉。
        """
        brief_keys = [prompts._section_key(heading_body(h), i)
                      for i, h in enumerate(BRIEF_HEADINGS, start=1)]
        self.assertEqual(brief_keys, list(prompts.BRIEF_SECTION_KEYS),
                         "精简版标题应逐一映到 BRIEF_SECTION_KEYS，且不重复")


class BriefSummarySurvivesTests(unittest.TestCase):
    """端到端：模型吐出的三段，规范化之后一个字都不许少。"""

    MODEL_OUTPUT = """## 1. 重要程度与一句话结论 / Importance and one-line conclusion
等级：低
结论：这是一封研讨会通知。

## 2. 必须采取的行动与截止时间 / Required actions and deadlines
- 无需行动。

## 3. 邮件内容要点 / Key points
- 研讨会主题：Why Linguists and Language Scientists Should Care About Voice Recognition
- 演讲者：Prof. Volker Dellwo
- 主办方：Department of Linguistics and Translation"""

    def test_the_key_points_are_not_replaced_by_none(self) -> None:
        out = prompts.normalize_brief_report(self.MODEL_OUTPUT)
        self.assertNotIn("## 3. 邮件内容要点 / Key points\n- 无。", out,
                         "第 3 段又被「- 无。」顶掉了 —— 标题解析表漏词了")
        for kept in ("Prof. Volker Dellwo", "研讨会主题", "Department of Linguistics"):
            self.assertIn(kept, out, f"第 3 段的内容被丢掉了：{kept}")

    def test_all_three_sections_are_captured(self) -> None:
        captured, _ = prompts._split_sections(self.MODEL_OUTPUT)
        self.assertEqual(sorted(captured), ["actions", "importance", "summary"])

    def test_a_genuinely_empty_section_still_says_none(self) -> None:
        """模型真的没写第 3 段时，`- 无。` 仍然是正确的兜底 —— 不要连它一起删掉。"""
        two_sections = self.MODEL_OUTPUT.split("## 3.")[0].strip()
        out = prompts.normalize_brief_report(two_sections)
        self.assertIn("## 3. 邮件内容要点 / Key points\n- 无。", out)


class FullReportStillParsesTests(unittest.TestCase):
    """加了精简版的词之后，完整版不能认错段。"""

    FULL_OUTPUT = """## 1. 重要程度与一句话结论 / Importance and one-line conclusion
- 等级：高

## 3. 邮件内容总结 / Email content summary
- 老师更新了截止时间。

## 5. 联网搜索后的建议与来源 / Recommendations
来源：Canvas 指南 https://community.canvaslms.com/x"""

    def test_the_three_sections_land_on_their_own_keys(self) -> None:
        captured, _ = prompts._split_sections(self.FULL_OUTPUT)
        self.assertIn("老师更新了截止时间", captured.get("summary", ""))
        self.assertIn("Canvas", captured.get("recommendations", ""))

    def test_the_full_summary_marker_is_not_swallowed_by_the_brief_one(self) -> None:
        # 「邮件内容总结」与「邮件内容要点」只差一个字，词表顺序一旦反过来就可能认错段。
        self.assertEqual(prompts._section_key("邮件内容总结 / Email content summary", 3), "summary")
        self.assertEqual(prompts._section_key("邮件内容要点 / Key points", 3), "summary")


if __name__ == "__main__":
    unittest.main()
