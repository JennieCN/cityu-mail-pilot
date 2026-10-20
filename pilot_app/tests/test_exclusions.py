"""账号自己的收信排除名单：解析、说明、判定。

这个文件钉的是三件事，每一件都对应一条**会被误做的选择**：

* **只用报头，不用正文。** 关键词只匹配主题。正文在「是不是本校来信」那道判断之前
  根本不入库（隐私政策承诺过），为了筛一封信去解密正文就是反着做。所以有一条测试
  专门断言「正文里的字命不中」——那不是漏洞，是设计。
* **排除 ≠ 静默丢弃。** 判定返回的是**给用户看的原因**，调用方把它写进 `skipped`
  那一行与日报；这里断言原因里带着命中的那条规则，好让人能自己解释「为什么这封没处理」。
* **写错了当场说。** 空规则、太短的关键词、超长、超过 50 条，一律报错并点到第几行——
  一份「以为生效了其实被忽略」的名单比没有名单更糟。
"""

from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from pilot_app import exclusions  # noqa: E402


def message(sender: str = "", subject: str = "", body: str = "") -> dict:
    return {"sender_address": sender, "subject": subject, "body": body}


class ParseTests(unittest.TestCase):
    def test_prefixes_and_bare_addresses_are_both_understood(self):
        rules = exclusions.parse_rules(
            "发件人:promo@cityu.edu.hk\n"
            "关键词:实习\n"
            "@qq.com\n"
            "keyword:促销\n"
            "  \n"
            "# 这行是注释\n")
        self.assertEqual([r["kind"] for r in rules],
                         ["sender", "keyword", "sender", "keyword"])
        self.assertEqual([r["pattern"] for r in rules],
                         ["promo@cityu.edu.hk", "实习", "@qq.com", "促销"])

    def test_a_subject_that_contains_a_colon_is_a_keyword_not_a_broken_prefix(self):
        """`Re: 期中` 这种行必须当关键词。拿「有冒号」当语法，会把一条正常规则读成格式错误。"""
        rules = exclusions.parse_rules("Re: 期中")
        self.assertEqual(rules, [{"kind": "keyword", "pattern": "re: 期中"}])

    def test_a_bad_line_names_the_line_number(self):
        for text, needle in (
            ("发件人:", "第 1 行"),
            ("关键词:a", "第 1 行"),
            ("\n" + "关键词:" + "长" * 200, "第 2 行"),
        ):
            with self.assertRaises(exclusions.RuleError) as caught:
                exclusions.parse_rules(text)
            self.assertIn(needle, str(caught.exception))

    def test_too_many_rules_is_refused(self):
        text = "\n".join(f"关键词:规则{i}" for i in range(exclusions.MAX_RULES + 1))
        with self.assertRaises(exclusions.RuleError) as caught:
            exclusions.parse_rules(text)
        self.assertIn(str(exclusions.MAX_RULES), str(caught.exception))

    def test_describe_says_what_is_in_force_or_what_is_wrong(self):
        self.assertIn("还没有排除规则", exclusions.describe(""))
        self.assertIn("已生效 3 条", exclusions.describe("a@b.com\n关键词:实习\n@qq.com"))
        self.assertIn("发件人 2 条", exclusions.describe("a@b.com\n关键词:实习\n@qq.com"))
        self.assertIn("还不能用", exclusions.describe("关键词:a"))


class MatchTests(unittest.TestCase):
    def setUp(self):
        self.rules = exclusions.parse_rules(
            "promo@cityu.edu.hk\n@promo.cityu.edu.hk\n关键词:实习\n关键词:讲座")

    def test_an_exact_sender_is_excluded(self):
        reason = exclusions.matches(message("promo@cityu.edu.hk", "选课通知"), self.rules)
        self.assertIn("promo@cityu.edu.hk", reason)

    def test_the_match_is_case_insensitive(self):
        self.assertTrue(exclusions.matches(message("PROMO@CityU.edu.HK", "x"), self.rules))

    def test_a_domain_rule_covers_that_domain_and_its_subdomains(self):
        self.assertTrue(exclusions.matches(message("a@promo.cityu.edu.hk", "x"), self.rules))
        self.assertTrue(exclusions.matches(message("b@mail.promo.cityu.edu.hk", "x"), self.rules))
        self.assertFalse(exclusions.matches(message("b@cityu.edu.hk", "x"), self.rules),
                         "只写 @promo.cityu.edu.hk 不该把整个 cityu.edu.hk 排掉")

    def test_a_keyword_matches_the_subject_only(self):
        self.assertIn("实习", exclusions.matches(message("hr@cityu.edu.hk", "春季实习宣讲会"), self.rules))
        self.assertFalse(
            exclusions.matches(message("hr@cityu.edu.hk", "选课通知", body="实习"), self.rules),
            "正文一个字都不看——这不是漏洞，是设计（正文在发件人判断前不入库）")

    def test_an_ordinary_mail_is_not_touched(self):
        self.assertEqual(exclusions.matches(message("library@cityu.edu.hk", "成绩公布"), self.rules), "")

    def test_no_rules_means_nothing_is_excluded(self):
        self.assertEqual(exclusions.matches(message("a@b.com", "实习"), []), "")


if __name__ == "__main__":
    unittest.main()
