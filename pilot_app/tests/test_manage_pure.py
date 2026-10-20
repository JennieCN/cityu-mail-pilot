# -*- coding: utf-8 -*-
"""`manage.py` 里那些**纯函数**：不连网、不读盘、不开库，喂进去就能断言。

发布前审计留下的欠账是「`manage` 系列工具没有单测，全靠真机验证，所以措辞/分类
错了只有跑到线上才发现」。这个文件补的是其中**不需要真环境**的那一半：打码、擦洗、
表格对齐、`check-*` 那一列的分档措辞、退出码的分档。要真连 IMAP/SMTP/模型的那些
（`verify-e2e`、`check-mailboxes` 的探针本身）不在这里——它们各有自己的套件，
而这个文件一分钟就能跑完。

两条性质是这份文件存在的理由，其余都是顺手：

* **打码与擦洗既不漏授权码/完整地址，也不把一句正常的话擦烂。** 空字符串和「不像
  地址的 token」必须原样放过：`str.replace("", …)` 会在每个字符之间插一串星号，
  那不是"更安全"，那是把运维看到的那句话毁掉；而把主机名也换成 `***`，出错时就
  没人看得出是哪台服务器了。
* **「主机填错」与「授权码被拒」永远是两句不同的话。** 2026-09-18 那次就是一个 126 的
  地址被填成了 163 的服务器，服务器回的是通用的「密码错误」，我们却翻译成「授权码
  不对」，于是用户一遍遍重新生成一串**本来就是对的**授权码。

断言尽量写成**逐字相等**，而不是 `assertIn`：判据被改反时要能变红，而不是"看着
差不多就过"。凡是靠这一点成立的地方，docstring 里都标了「反向验证」。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

_TMP = tempfile.mkdtemp(prefix="manage-pure-")
# 这个文件里的函数一个都不读库、不碰主密钥；两行环境变量只是不让 import 有机会
# 看见某个真实部署的路径（写法照 test_agent.py：库用 setdefault，主密钥给个假的）。
os.environ.setdefault("INFE_PILOT_DB", _TMP + "/manage-pure.sqlite3")
os.environ["INFE_PILOT_MASTER_KEY"] = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="

from pilot_app import mailboxcheck, manage  # noqa: E402


class PadTests(unittest.TestCase):
    """`_pad`：表格第一列对不齐，运维就会看错行——而这些表是给人念的。"""

    def test_ascii_cell_is_padded_to_the_column_width(self):
        """ASCII 一格算一列：宽度 6 的列里放 "abc" 要补满 3 个空格。"""
        self.assertEqual(manage._pad("abc", 6), "abc   ")

    def test_a_cjk_cell_is_measured_in_two_column_units(self):
        """中文一格算两列：`邮件` 占 4 列，宽度 6 的列里只补 2 个空格。

        **反向验证**：断言是逐字相等。把 `2 if east_asian_width(...) in "WF" else 1`
        改成一律算 1 列，这里会补成 4 个空格，本条立刻变红——而那正是中文列在真机
        表格里"看着多了一截"的样子。
        """
        self.assertEqual(manage._pad("邮件", 6), "邮件  ")

    def test_a_cell_wider_than_its_column_keeps_one_separating_space(self):
        """比列还宽的值也必须留一个空格，否则相邻两格会粘成一个读不出来的串。

        **反向验证**：断言是逐字相等。把 `max(1, width - shown)` 的下限改成
        `max(0, …)`（也就是"超宽就不补"），结果是 `一口气八个字`，本条变红。
        """
        self.assertEqual(manage._pad("一口气八个字", 4), "一口气八个字 ")


class ScrubTests(unittest.TestCase):
    """`_scrub`：打印前的第二道闸——授权码变 `***`，完整地址变打码形式。"""

    def test_every_app_password_in_the_text_becomes_three_stars(self):
        """两个授权码各擦各的，一个都不许留下——留下的那个会进终端和聊天记录。"""
        self.assertEqual(
            manage._scrub("first=aaa111 second=bbb222", ["aaa111", "bbb222"], []),
            "first=*** second=***")

    def test_a_full_address_becomes_its_masked_form_and_never_the_whole_one(self):
        """完整地址换成打码形式：能认出是谁，但读不回整个邮箱。"""
        text = manage._scrub("收件人 someone@qq.com 被拒", [], ["someone@qq.com"])
        self.assertEqual(text, "收件人 so***@qq.com 被拒")
        self.assertNotIn("someone@qq.com", text)

    def test_an_empty_secret_cannot_shred_the_line(self):
        """空字符串**不算**一个要擦的秘密（列表里常常有占位空串）。

        **反向验证**：把 `if secret:` 这个判断去掉（或改成 `if True:`），
        `"abc".replace("", "***")` 会变成 `***a***b***c***`——本条断言的是逐字相等，
        所以必红。判据不是"擦干净了没有"，而是"只擦该擦的"。
        """
        self.assertEqual(manage._scrub("abc", [""], [""]), "abc")

    def test_a_token_without_an_at_sign_is_not_treated_as_an_address(self):
        """不像地址的 token（这里是主机名）原样保留：出错时要看得出是哪台服务器。

        **反向验证**：把 `if address and "@" in address` 里的 `"@" in address` 去掉，
        `_mask("imap.qq.com")` 得到的是 `***`（它没有域名部分），这句话会变成
        `host *** is stale`——本条是逐字相等，必红。
        """
        self.assertEqual(manage._scrub("host imap.qq.com is stale", [], ["imap.qq.com"]),
                         "host imap.qq.com is stale")


class LoginMarkTests(unittest.TestCase):
    """`_LOGIN_MARKS`：`check-mailboxes` 表格里「登录」那一列的措辞。"""

    def test_every_probe_state_has_a_login_mark(self):
        """`check_all` 可能给出的 7 个探针状态，每一个都得有中文说法。

        少一个的后果不是崩，而是那张表里冒出一个英文 slug（`mailboxcheck` 的
        `_LOGIN_MARKS.get(..., item["probe_state"])` 回退成原值）——运维看表时
        最需要读懂的就是坏掉的那几行。**这个集合就是要维护的那份清单。**
        """
        states = {
            mailboxcheck.OK, mailboxcheck.AUTH_REJECTED, mailboxcheck.PROVIDER_BLOCKED,
            mailboxcheck.NETWORK, mailboxcheck.INBOX_REFUSED, mailboxcheck.LOGIN_REFUSED,
            mailboxcheck.CHECK_FAILED,
        }
        self.assertEqual(states - set(manage._LOGIN_MARKS), set())
        for state in states:
            self.assertTrue(manage._LOGIN_MARKS[state].strip(),
                            f"{state} 的说法不能是空的，空单元格会把整行挪位")

    def test_only_the_working_state_is_shown_as_a_tick(self):
        """勾只有一个含义：只读打开收件箱成功了。其余各档各有各的说法。

        **反向验证**：`assertEqual([...], [mailboxcheck.OK])` 钉住「有且仅有 OK 打勾」；
        把任何一个坏档（例如 `check_failed`）也映射成 `✓`，列表多一项，本条变红。
        """
        ticks = [state for state, mark in manage._LOGIN_MARKS.items() if mark == "✓"]
        self.assertEqual(ticks, [mailboxcheck.OK])
        self.assertEqual(manage._LOGIN_MARKS[mailboxcheck.NETWORK], "连不上")
        self.assertEqual(manage._LOGIN_MARKS[mailboxcheck.CHECK_FAILED], "未探")


class TierWordingTests(unittest.TestCase):
    """分档措辞：`manage.check_mailboxes` 印的就是下面这两张表，一个字都不改。

    这三档混起来就是 2026-09-18 到 09-20 那三天：① 我们的主机填错被说成 ② 授权码
    被拒（用户白折腾），③ 微软那条死路被说成 ②（用户死在一条本来就没有出口的路上）。
    """

    def test_host_mismatch_and_a_refused_code_are_two_different_sentences(self):
        """「主机填错」不许用「授权码被拒」那句话，也不许让用户去重新生成码。

        **反向验证**：把主机不一致那一档并进 `AUTH_REJECTED`（这是当年真实发生过的
        误报），两个 `LABELS` 就相等，`assertNotEqual` 与 `assertIn(不要去让用户…)`
        两条同时变红。
        """
        self.assertNotEqual(mailboxcheck.LABELS[mailboxcheck.HOST_MISMATCH],
                            mailboxcheck.LABELS[mailboxcheck.AUTH_REJECTED])
        self.assertTrue(mailboxcheck.LABELS[mailboxcheck.HOST_MISMATCH].strip())
        # 「主机填错」这一档里不许出现"授权码"三个字：它一出现，用户就会去重新生成。
        self.assertNotIn("授权码", mailboxcheck.LABELS[mailboxcheck.HOST_MISMATCH])
        self.assertIn("不要去让用户重新生成授权码",
                      mailboxcheck.NEXT_STEPS[mailboxcheck.HOST_MISMATCH])
        self.assertIn("重新生成一个授权码",
                      mailboxcheck.NEXT_STEPS[mailboxcheck.AUTH_REJECTED])

    def test_a_refused_code_and_a_blocked_provider_do_not_share_a_next_step(self):
        """「重新生成一次」与「这条路根本没有」不能给同一句下一步。

        两档在「登录」那一列都显示"拒绝"（探针状态相同），分开它们的只有分档那一列，
        所以这里钉的是分档的说法与下一步，而不是登录标记。
        """
        self.assertNotEqual(mailboxcheck.LABELS[mailboxcheck.AUTH_REJECTED],
                            mailboxcheck.LABELS[mailboxcheck.PROVIDER_BLOCKED])
        self.assertNotEqual(mailboxcheck.NEXT_STEPS[mailboxcheck.AUTH_REJECTED],
                            mailboxcheck.NEXT_STEPS[mailboxcheck.PROVIDER_BLOCKED])
        self.assertIn("换一个支持授权码的邮箱",
                      mailboxcheck.NEXT_STEPS[mailboxcheck.PROVIDER_BLOCKED])


class PlatformCostExitTests(unittest.TestCase):
    """`_platform_cost_exit`：`platform-cost` 的退出码分档（接脚本/告警的那一半）。"""

    FLAGS = ("over_cost", "balance_low", "exhausted", "stale")

    def _state(self, **verdicts) -> dict:
        return {flag: bool(verdicts.get(flag, False)) for flag in self.FLAGS}

    def test_any_single_verdict_makes_the_command_exit_nonzero(self):
        """四个判定里**任何一个**成立就要非零退出——不能等四个都成立。

        **反向验证**：把 `any(...)` 改成 `all(...)`，下面每一个只点亮一个判定的用例
        都会拿到 0，四条子用例全部变红（这正是"判据被改反"最省事的写法）。
        """
        for flag in self.FLAGS:
            with self.subTest(flag=flag):
                self.assertEqual(manage._platform_cost_exit(self._state(**{flag: True})), 1)

    def test_nothing_to_report_exits_zero(self):
        """四项都不成立才是 0——退出码是给脚本看的，不许"没事也非零"。"""
        self.assertEqual(manage._platform_cost_exit(self._state()), 0)


class OperatorIdentityTests(unittest.TestCase):
    """`_operator_identity`：审计行里写的是**哪个 shell**跑了这次写入。"""

    def test_the_audit_row_names_the_shell_that_ran_sudo(self):
        """`SUDO_USER` 赢过登录账号：`sudo` 下它是人，`getpass` 那边是 root。

        **反向验证**：把 `SUDO_USER or getpass.getuser()` 的优先顺序调过来，
        结果是 `root@test-host`，这条逐字相等的断言变红。
        """
        with mock.patch.dict(os.environ, {"SUDO_USER": "cityumail"}, clear=False), \
                mock.patch.object(manage.getpass, "getuser", return_value="root"), \
                mock.patch.object(manage.socket, "gethostname",
                                           return_value="test-host"):
            self.assertEqual(manage._operator_identity(), "cityumail@test-host")

    def test_a_very_long_operator_name_is_capped(self):
        """审计行有长度上限（120）：一个超长的 USER 不该把整行撑坏。"""
        with mock.patch.dict(os.environ, {"SUDO_USER": "u" * 300}, clear=False), \
                mock.patch.object(manage.socket, "gethostname",
                                           return_value="test-host"):
            self.assertEqual(len(manage._operator_identity()), 120)


if __name__ == "__main__":
    unittest.main()
