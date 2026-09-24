"""花钱的每一条路：要么过余额闸，要么**在这份清点里写明为什么不过**（2026-09-24）。

为什么写这条测试：余额闸（`PilotService.require_budget_for`）原来只写在出报告那条路上，
于是「测试模型」按钮与日报综览**账上没钱时照样真花一次调用**——这是审查报告 B 段的一条。
补上之后，宿舍机复验时又问出一个同类边界：AI 助手与三个运维探针也不走这道闸。

它俩的答案不同：
  * AI 助手是**故意的**（`agent.budget_state`，默认 30 次/天，调用前先读）；
  * 三个运维探针各花**一次**调用，它们是「这把 key 还活着吗」的诊断——
    余额见底时探针失败**本身就是那个答案**，拦住它反而让人查不出问题。

但「哪条路为什么不过闸」这种知识如果只活在注释里，下一个人新加一条花钱的路时
没有任何东西会提醒他。所以这里**按源码清点**：`providers.generate(` 的每一个调用点，
上面 8 行内必须有 `require_budget_for(`，否则必须出现在下面的豁免表里。
已反向验证：把 `digest_synthesis.py` 里那行闸删掉，这条测试会红。
"""

import pathlib
import unittest

APP_DIR = pathlib.Path(__file__).resolve().parent.parent

# 允许**不过闸**的调用点。键是「文件:所属函数」，值是理由——理由必须是人话，
# 因为这份表就是「为什么它可以花用户的钱」的唯一记录。
EXEMPT = {
    "agent.py:analyse":
        "AI 助手有自己的日上限（agent.budget_state 读 AGENT_DAILY_CALLS，默认 30 次/天），"
        "调用前就查过；它与报告的余额闸是两套计数，见 agent.py 的注释。",
    "manage.py:check_model":
        "运维探针：真调一次平台兜底 key——「这把 key 还活着吗」只能靠真调回答。",
    "manage.py:check_localmodel":
        "运维探针：本机那台不过余额闸（它没有账户，本来也不花钱），这条只是逐跳核 TLS/钉扎。",
    "manage.py:check_native_search":
        "运维探针：用的是搜索 key，不是模型余额那本账。",
}

# 闸必须在调用点上方这么多行以内。抽函数时若把它挪远了，这条测试会红——
# 那正是我们要的：闸离调用越远，越容易被后来的改动绕过去。
NEIGHBOURHOOD = 8


def _enclosing_function(lines: list[str], index: int) -> str:
    name = "<module>"
    for line in lines[:index + 1]:
        if line.startswith("def "):
            name = line[4:].split("(")[0]
    return name


def _call_sites() -> list[tuple[str, str, bool]]:
    found = []
    for path in sorted(APP_DIR.glob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if "providers.generate(" not in line:
                continue
            window = "\n".join(lines[max(0, index - NEIGHBOURHOOD):index])
            found.append((path.name, _enclosing_function(lines, index),
                          "require_budget_for(" in window))
    return found


class BudgetGateCoverageTests(unittest.TestCase):
    def test_every_spending_call_site_is_gated_or_explained(self):
        sites = _call_sites()
        self.assertGreaterEqual(len(sites), 6,
                                f"清点到的花钱点太少（{len(sites)}），八成是匹配式写错了：{sites}")
        unexplained = []
        for filename, function, gated in sites:
            key = f"{filename}:{function}"
            if gated:
                if key in EXEMPT:
                    unexplained.append(f"{key}：既挂了闸又躺在豁免表里，豁免表该删掉这一行")
                continue
            if key not in EXEMPT:
                unexplained.append(f"{key}：不过余额闸，也没在 EXEMPT 里写明理由")
        self.assertEqual(unexplained, [], "有花钱的调用点没人管：\n" + "\n".join(unexplained))

    def test_the_exempt_table_does_not_rot(self):
        """豁免表里不能留已经不存在（或已经挂上闸）的条目——那种表会慢慢变成谎话。"""
        sites = {f"{filename}:{function}": gated for filename, function, gated in _call_sites()}
        stale = [key for key in EXEMPT if key not in sites or sites[key]]
        self.assertEqual(stale, [], f"这些豁免条目已经对不上现实了，删掉它们：{stale}")


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
