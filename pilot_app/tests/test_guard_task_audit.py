"""全树扫一遍 `guard_task=`：**每个调用点报的名字都得是四个之一**。

为什么值得一条独立的静态审计：这个参数是按**动作**分的（对方交付文档 §3.3），而「动作」在
看代码的人眼里经常变成「输出的文体」——2026-09-23 一天里就错了两次，而且是同一个错法：

* 看原信报了 `reply`（它只是翻译 / 概括，不是给用户写回信）；
* 运维助手报了 `extract`（它要求输出是合法 JSON，而那份分析的输出是散文）。

两次都不会崩、不会丢信，只会**每次留一条恒为真的噪音**，而恒为真的噪音会训练人忽略日志。

判据：四个合法取值，或空串（= 不发这个字段）。空串是给自检与日报综览留的——那两条路要回答的
是「这一档通不通」，不是「这段文字该不该拦」。
"""

from __future__ import annotations

import ast
import pathlib
import unittest

ALLOWED = ("classify", "extract", "summarize", "reply")


class GuardTaskSourceAuditTests(unittest.TestCase):
    def _call_sites(self) -> list[tuple[str, int, str]]:
        root = pathlib.Path(__file__).resolve().parents[1]
        found: list[tuple[str, int, str]] = []
        for source in sorted(root.glob("*.py")):
            tree = ast.parse(source.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for keyword in node.keywords:
                    if keyword.arg == "guard_task":
                        found.append((source.name, node.lineno, ast.unparse(keyword.value)))
        return found

    def _branches(self, value: str) -> list[str]:
        """一个 `guard_task=` 实参可能由几个分支拼成（`A if cond else B`），逐个取字面量。

        直接 `ast.unparse` 出来的是一整段源码文本，里面既有任务名也有**条件里的变量名**
        （`'classify' if provider == 'local_openai' else ''`）——把整段丢进白名单只会把
        `local_openai` 这种无关标识符也算进去，那正是第一版审计红的地方。
        """
        try:
            node = ast.parse(value, mode="eval").body
        except SyntaxError:  # pragma: no cover - ast.unparse 的输出总能解析
            return []
        leaves: list[ast.AST] = []
        pending = [node]
        while pending:
            current = pending.pop()
            if isinstance(current, ast.IfExp):
                pending.extend([current.body, current.orelse])
            else:
                leaves.append(current)
        out = []
        for leaf in leaves:
            if isinstance(leaf, ast.Constant) and isinstance(leaf.value, str):
                out.append(leaf.value)
        return out

    def test_every_call_site_reports_a_known_task(self):
        found = self._call_sites()
        self.assertGreaterEqual(len(found), 4, "扫到的调用点太少——审计本身可能坏了")
        for name, line, value in found:
            branches = self._branches(value)
            self.assertTrue(branches, f"{name}:{line} 的 guard_task 不是一个字符串字面量：{value}")
            for literal in branches:
                self.assertIn(literal, ALLOWED + ("",),
                              f"{name}:{line} 报了一个护栏不认识的任务名：{literal!r}")

    def test_at_least_one_call_site_actually_reports_a_task(self):
        """反向：别让审计在「所有调用点都没带这个名字」时也绿。"""
        literals = {lit for _, _, value in self._call_sites() for lit in self._branches(value)}
        self.assertTrue(literals & set(ALLOWED), f"没有任何调用点报真任务名：{literals}")

    def test_the_two_mistakes_this_audit_was_written_for_stay_fixed(self):
        """把 2026-09-23 那两次错法各钉一条源码级断言。

        它们的判据是「这个调用在做什么」，不是运行时数据，所以静态核得动，
        而且**只能**静态核——两条路都不会在单测里真的调用模型。
        """
        root = pathlib.Path(__file__).resolve().parents[1]
        service = (root / "service.py").read_text(encoding="utf-8")
        agent = (root / "agent.py").read_text(encoding="utf-8")
        self.assertNotIn('guard_task="reply"', service,
                         "看原信是翻译/概括，不是给用户写回信——别再用 reply 审它")
        self.assertNotIn('guard_task="extract"', agent,
                         "运维助手的输出是散文（SYSTEM_PROMPT 明写不要 Markdown），不是 JSON")
        self.assertIn('guard_task="summarize"', agent)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
