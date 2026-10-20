"""`i18n.t()`：**查不到译文时，占位符也必须被替换**。

这条测试存在的唯一理由是一次真实的生产事故（2026-09-23）：`t()` 在「词典里没有这条
文案」时直接 `return text`，把参数替换整段跳过了。而中文是原文语、`catalog("zh")`
按设计是空词典——**那正是绝大多数访问者走的那条路**，于是：

* 客服群那一节印出 `这张码 {when}有效`（少了「9 月 29 日前」）；
* 安卓按钮印出 `下载安卓安装包（{size}）`。

判据写成三条，因为它们各自会被一种"看起来像修好了"的改法绕过：

1. 空词典（= 中文原文）下带占位符 → 占位符必须没了；
2. 命中了译文时 → 译文里的占位符照样要替换（这条原来就是对的，别改坏）；
3. 文案里出现**不是占位符**的花括号（CSS/模板片段）→ 原样保留、**不抛异常**。
   这是 `t()` 刻意不用 `str.format` 的理由，别为了图省事换回去。
"""

from __future__ import annotations

import unittest

from pilot_app import i18n


class PlaceholderTests(unittest.TestCase):
    def test_a_missing_translation_still_gets_its_parameters_filled_in(self):
        """空词典是**正常路径**（中文原文），不是异常分支。"""
        self.assertEqual(i18n.catalog(i18n.DEFAULT_LOCALE), {},
                         "中文词典按设计是空的；这条测试的前提就是它")
        self.assertEqual(
            i18n.t("这张码 {when}有效", i18n.DEFAULT_LOCALE, when="9 月 29 日前"),
            "这张码 9 月 29 日前有效")
        self.assertEqual(
            i18n.t("下载安卓安装包（{size}）", i18n.DEFAULT_LOCALE, size="8.4 MB"),
            "下载安卓安装包（8.4 MB）")

    def test_a_translated_string_also_gets_its_parameters_filled_in(self):
        """命中了译文时同样要替换——这条原来就是对的，钉住它别被改回去。"""
        original = i18n.catalog
        i18n.catalog = lambda locale: {"你好 {name}": "Hello {name}"}  # type: ignore[assignment]
        try:
            self.assertEqual(i18n.t("你好 {name}", "en", name="小明"), "Hello 小明")
        finally:
            i18n.catalog = original  # type: ignore[assignment]

    def test_a_brace_that_is_not_a_placeholder_survives(self):
        """不是占位符的花括号原样留下，而且**不许抛异常**（`str.format` 会 500）。"""
        text = "html[data-theme=\"dark\"] { --ink: #111 }"
        self.assertEqual(i18n.t(text, i18n.DEFAULT_LOCALE), text)
        self.assertEqual(i18n.t(text, i18n.DEFAULT_LOCALE, unused="x"), text)


class IncludeInsideSkipTests(unittest.TestCase):
    """`data-i18n-include`：在被划出去的子树里给**一块**重新开口子。

    起因：「报告与账户」里那张「你的邮件走这条路」的卡片已经译齐四门语言，而
    `#dashboard` 整块带着 `data-i18n-skip`（登录后的界面第二轮再翻）。为了这一张卡
    把整块 skip 摘掉，棘轮会立刻要求补齐剩下三百多句——所以需要的是**局部反向**。

    三条判据各自挡一种改法：

    1. 光有 skip 的子块（哪怕里面再嵌套）必须继续不抽、不翻——否则「整块 skip」
       就被这一次改动悄悄拆了；
    2. 带 include 的块要抽得到（`unit_keys`）**且**翻得了（`translate_html`）——
       两条走法用的是同一个 `_child_skip`，分开写迟早会各说各话；
    3. 中文（空词典）下逐字节等于原文——这是这套机制的地基，别被开口子弄塌。
    """

    HTML = (
        '<div data-i18n-skip>'
        '<p>这段不翻</p>'
        '<div data-i18n-include><h3>这一段要翻</h3>'
        '<p>里面嵌套的也不许再划出去</p></div>'
        '<div><p>兄弟节点仍然不翻</p>'
        '<div data-i18n-skip><p>开口子里面自己再划出去的也不翻</p></div></div>'
        '</div>')

    def test_unit_keys_resumes_inside_a_skipped_block(self):
        must, _fallback = i18n.unit_keys(self.HTML)
        self.assertIn("这一段要翻", must)
        self.assertIn("里面嵌套的也不许再划出去", must)
        self.assertNotIn("这段不翻", must)
        self.assertNotIn("兄弟节点仍然不翻", must)
        self.assertNotIn("开口子里面自己再划出去的也不翻", must)

    def test_the_rewriter_resumes_exactly_the_same_block(self):
        table = {"这一段要翻": "Translate this", "里面嵌套的也不许再划出去": "This too"}
        out = i18n.translate_html(self.HTML, "en", table)
        self.assertIn("Translate this", out)
        self.assertIn("This too", out)
        self.assertIn("这段不翻", out)
        self.assertIn("兄弟节点仍然不翻", out)
        self.assertIn("开口子里面自己再划出去的也不翻", out)

    def test_chinese_still_comes_back_byte_for_byte(self):
        self.assertEqual(i18n.translate_html(self.HTML, i18n.DEFAULT_LOCALE), self.HTML)

    def test_the_real_reports_card_is_covered_and_the_rest_of_the_app_is_not(self):
        """真页面上的那条边界：卡片翻了，同一屏的其它按钮仍是中文。"""
        from pathlib import Path
        source = Path("pilot_app/static/index.html").read_text(encoding="utf-8")
        en = i18n.translate_html(source, "en")
        self.assertIn("How your email travels", en)
        self.assertIn("Pause service", en)
        # 同一屏里没接的东西**必须**还是中文——否则「第二轮再翻」那句承诺就是假的
        self.assertIn("刷新报告", en)
        self.assertIn("我用了多少", en)
        self.assertEqual(i18n.translate_html(source, i18n.DEFAULT_LOCALE), source)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
