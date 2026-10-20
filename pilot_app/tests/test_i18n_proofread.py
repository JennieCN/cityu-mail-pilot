# -*- coding: utf-8 -*-
"""`tools/i18n_proofread.py` 自己的判据 —— 尤其是它**故意报 0** 的那两条。

为什么要单独测这个工具：2026-09-23 的校准把「与原文逐字相同」和「长度比例离谱」
从 **39 + 20 行假红**收成 0 行（实测分布与依据写在那个文件的 `RATIO_MAX` 注释里）。
**「0 行」和「这条检查坏了」在报告上长得一模一样**，而报告是给人看的 ——
所以这里必须反向验证：故意造一个错，看它报不报；造一个**合法**的，看它闭不闭嘴。

这里只动 `tempfile` 里的副本，**不碰仓库里的词典**。
"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import shutil
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "i18n_proofread_tool", ROOT / "tools" / "i18n_proofread.py")
proofread = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(proofread)

REAL = ROOT / "pilot_app" / "static" / "i18n"


class _Copy:
    """一份可以随便改的词典副本（`with _Copy() as c: c.docter(...)`）。"""

    def __enter__(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="i18n-rev-")) / "i18n"
        shutil.copytree(ROOT / "pilot_app" / "static" / "i18n", self.dir)
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.dir, ignore_errors=True)
        proofread.I18N = REAL          # 别把模块级路径留在已删掉的副本上
        return False

    def table(self, lang: str) -> dict:
        return json.loads((self.dir / f"{lang}.json").read_text(encoding="utf-8"))

    def save(self, lang: str, table: dict) -> None:
        (self.dir / f"{lang}.json").write_text(
            json.dumps(table, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def report(self) -> dict:
        proofread.I18N = self.dir
        return proofread.check()["report"]


def real_report() -> dict:
    proofread.I18N = REAL
    return proofread.check()["report"]


def _longest(table: dict, *, over: int = 20) -> str:
    return next(k for k in table if len(table[k]) > over)


class CalibrationTests(unittest.TestCase):
    """那两条「按实测校准」的检查，今天必须是 0 —— 而且是**活着**的 0。"""

    def test_todays_dictionary_is_clean(self):
        """全 0 是校准后的正常值。这条红了先看新加的那条译文，别先改上限。

        上限怎么来的（714 条实测、按语言分开）写在 `tools/i18n_proofread.py` 的
        `RATIO_MAX` 注释里：ja/ko 原来用 2.0，正好压在 p95 上，于是每 20 行报 1 行。
        真要放宽，得把新的实测分布一起写进去 —— 否则下一个人只会看到「上限被调大了」。
        """
        report = real_report()
        for name, items in report.items():
            self.assertEqual(items, [], f"「{name}」不该有内容：{items[:3]}")

    def test_it_still_catches_a_broken_tag(self):
        with _Copy() as copy:
            table = copy.table("en")
            key = next(k for k in table if "<b>" in table[k])
            table[key] = table[key].replace("<b>", "", 1)
            copy.save("en", table)
            self.assertTrue(copy.report()["tokens"])

    def test_it_still_catches_a_simplified_character_in_japanese(self):
        """2026-09-23 宿舍机就是这样抓到混进日文里的那处「即时」。

        这条**必须**继续报 —— 豁免表 `GLYPH_OK_WORDS` 是**按词**豁免的，不是按字。
        """
        with _Copy() as copy:
            table = copy.table("ja")
            key = _longest(table)
            table[key] = table[key] + "（即时）"
            copy.save("ja", table)
            self.assertTrue(copy.report()["glyph"])

    def test_a_real_japanese_word_is_not_a_simplified_character(self):
        """`延滞`/`提携`/`雇用` 这些是日文正字，不是混进来的简体。"""
        with _Copy() as copy:
            table = copy.table("ja")
            key = _longest(table)
            table[key] = table[key] + "（延滞しています。提携、雇用。）"
            copy.save("ja", table)
            self.assertEqual(copy.report()["glyph"], [])

    def test_an_untranslated_traditional_line_is_caught(self):
        with _Copy() as copy:
            table = copy.table("zh-Hant")
            key = next(k for k in table if len(k) > 8 and any(
                c in proofread.SIMPLIFIED_ONLY and c not in proofread.CONTEXTUAL_OK for c in k))
            table[key] = key
            copy.save("zh-Hant", table)
            self.assertTrue(copy.report()["same"])

    def test_a_shared_form_traditional_line_is_not_flagged(self):
        """`重要`/`截止 2026/9/24 23:59` 在繁体里**本来就与原文逐字相同**。"""
        with _Copy() as copy:
            table = copy.table("zh-Hant")
            keys = [k for k in table if k == table[k] and len(k) > 8 and not any(
                c in proofread.SIMPLIFIED_ONLY and c not in proofread.CONTEXTUAL_OK for c in k)]
            self.assertTrue(keys, "找不到「共享字形」的样本，这条测试就没在测东西")
            self.assertEqual(copy.report()["same"], [])

    def test_an_untranslated_japanese_sentence_is_caught(self):
        """短标签（`重要`/`低`）豁免，**句子级**的整条没翻仍然要报。"""
        with _Copy() as copy:
            table = copy.table("ja")
            key = _longest(table, over=12)
            table[key] = key
            copy.save("ja", table)
            self.assertTrue(copy.report()["same"])

    def test_a_paragraph_dropped_into_one_line_is_caught(self):
        with _Copy() as copy:
            table = copy.table("ja")
            key = _longest(table, over=12)
            table[key] = table[key] * 4
            copy.save("ja", table)
            self.assertTrue(copy.report()["ratio"])

    def test_the_longest_legitimate_lines_stay_under_the_cap(self):
        """英语法条是 5.60 倍、日语最长 2.80 倍、韩语 2.62 倍 —— 都在上限（6.0/3.2/3.0）里。

        这三个数字是**实测**的：它们就是上一版被报出来的那 40 行里的极端值。
        下界也钉住：如果最长的一条突然掉到 2.0 以下，多半是键或译文被换过，
        那这条「校准」就不再是拿这批数据算出来的了。
        """
        floors = {"en": 5.0, "ja": 2.5, "ko": 2.3}
        with _Copy() as copy:
            for lang, floor in floors.items():
                table = copy.table(lang)
                top = max(len(table[k]) / len(k) for k in table if len(k) > 12)
                self.assertLess(top, proofread.RATIO_MAX[lang],
                                f"{lang} 的自然上限涨到 {top:.2f} 了 —— 上限 "
                                f"{proofread.RATIO_MAX[lang]} 要按新的实测重新量，别只把数字调大")
                self.assertGreater(top, floor,
                                   f"{lang} 最长只有 {top:.2f} 倍？样本变了，校准的依据也就变了")
            self.assertEqual(copy.report()["ratio"], [])


class CoverageTests(unittest.TestCase):
    """**每一句会出现在页面上的字**都要进体检，包括那 8 条兜底碎片。

    2026-09-23 之前这个工具只读 `keys.json` 的 `keys`（714 条），而那 8 条
    「整块没译时按碎片取」的安装步骤碎片也在页面上 —— 重建词典时丢过一次的
    恰好就是它们。这条测试盯住「体检范围 = 页面上真会出现的那一批」。
    """

    def _document(self) -> dict:
        return json.loads((REAL / "keys.json").read_text(encoding="utf-8"))

    def test_it_checks_the_fallback_fragments_too(self):
        document = self._document()
        required = len(document["keys"])
        fallback = len(document["fallback"])
        self.assertGreater(fallback, 0, "兜底碎片没了？那这条测试就没在测东西")
        summary = proofread.check()
        self.assertEqual(summary["keys"], required + fallback)
        self.assertEqual(summary["required"], required)
        self.assertEqual(sum(len(items) for items in summary["report"].values()), 0)

    def test_an_untranslated_fallback_fragment_is_caught(self):
        with _Copy() as copy:
            document = json.loads((copy.dir / "keys.json").read_text(encoding="utf-8"))
            fragment = document["fallback"][0]
            table = copy.table("en")
            self.assertIn(fragment, table, "英文词典里没有这条碎片？体检范围就算错了")
            table[fragment] = fragment
            copy.save("en", table)
            self.assertTrue(copy.report()["same"])


class ReadOnlyTests(unittest.TestCase):
    """它只读。上面那些测试之所以敢随便跑，就是因为这个工具不写任何文件。"""

    def test_it_has_no_write_path(self):
        source = (ROOT / "tools" / "i18n_proofread.py").read_text(encoding="utf-8")
        for forbidden in ("write_text", 'open("', "open('", "mkdir", "unlink", "shutil"):
            self.assertNotIn(forbidden, source,
                             f"i18n_proofread 是只读工具，不该出现 {forbidden}")


if __name__ == "__main__":
    unittest.main()
