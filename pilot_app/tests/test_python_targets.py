# -*- coding: utf-8 -*-
"""两套解释器都必须能跑：**PEP 604 的 `X | Y` 注解在 3.9 上是运行时 TypeError**。

2026-09-21 真踩：宿舍机（Python 3.14）写完 `pilot_app/tests/test_pr_gate.py`，那台上
**2093 条全过**；Mac 这边（3.9.6）一 import 就炸：

    TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'

于是那个模块的 51 条测试变成 1 条 ERROR（整个模块加载失败），而在 3.14 上永远看不见。
**CI 跑 3.9 + 3.14**，所以这是一条会红的提交 —— 写它的人在自己的机器上没有这个反馈回路。
那台机器的 DSH 是 3.14，Mac 是 3.9，这条判据就是补这条缝：**谁都能在提交前自己跑一遍**。

判据只看**会被求值的注解**（3.9 上会当场抛的那种）：

* 函数/方法的**参数与返回值**注解 —— 定义时求值；
* **模块级与类级**的变量注解 —— 求值时写进 `__annotations__`；
* **函数体里的局部变量注解不求值**，3.9 上完全合法，所以放过
  （`test_shell.py` 里就有一处 `self.stack: list[...] | None = None`，它是无辜的，
  第一版扫描器把它一起报了 —— 错报比漏报更糟，会教人把判据关掉）；
* 已经写了 `from __future__ import annotations` 的文件**一律放过** —— 那正是本仓库
  推荐的写法（`tools/` 下一大批脚本都这么写）。
"""

import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]

SKIP_DIRS = {".git", "node_modules", ".venv", ".venv-pilot", "dist", "__pycache__",
             ".e2e", ".tools", "videogen"}

#: 求值的注解里出现 `|`（PEP 604）就等于「3.9 上 import 就炸」。
NEED = "要么写成 Optional[...]，要么在文件开头加 `from __future__ import annotations`。"


def python_files() -> list[pathlib.Path]:
    """仓库里的产品代码与工具（排除产物目录）；发布包里没有 `tools/`，所以它是可选的。"""
    found = []
    for base in ("pilot_app", "tools"):
        root = ROOT / base
        if not root.is_dir():
            continue
        found.extend(path for path in sorted(root.rglob("*.py"))
                     if not any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts))
    return found


def _has_future_annotations(tree: ast.Module) -> bool:
    return any(
        isinstance(node, ast.ImportFrom) and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in tree.body
    )


def _union(node: ast.AST) -> bool:
    return any(isinstance(inner, ast.BinOp) and isinstance(inner.op, ast.BitOr)
               for inner in ast.walk(node))


def _annotations_of(tree: ast.Module):
    """产出 `(行号, 说明, 注解节点)`，**只含 3.9 上会被求值的那些**。"""
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    def evaluated_variable_annotation(node: ast.AnnAssign) -> bool:
        cursor = parents.get(node)
        while cursor is not None:
            if isinstance(cursor, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                return False        # 局部变量注解：不求值
            if isinstance(cursor, (ast.Module, ast.ClassDef)):
                return True         # 模块级 / 类级：求值
            cursor = parents.get(cursor)
        return False

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for argument in (list(node.args.args) + list(node.args.posonlyargs)
                             + list(node.args.kwonlyargs)
                             + [node.args.vararg, node.args.kwarg]):
                if argument is not None and argument.annotation is not None:
                    yield argument.lineno, f"{node.name}() 的参数 {argument.arg}", argument.annotation
            if node.returns is not None:
                yield node.lineno, f"{node.name}() 的返回值", node.returns
        elif isinstance(node, ast.AnnAssign) and evaluated_variable_annotation(node):
            yield node.lineno, "模块级/类级变量注解", node.annotation


def pep604_hits(source: str) -> list[str]:
    """这段源码里「3.9 上会被求值、且用了 `X | Y`」的注解；坏样本测试也用它。"""
    tree = ast.parse(source)
    if _has_future_annotations(tree):
        return []
    return [f"第 {line} 行：{where}" for line, where, annotation in _annotations_of(tree)
            if _union(annotation)]


class PythonTargetTests(unittest.TestCase):
    def test_no_evaluated_annotation_needs_python_310(self):
        offenders = []
        for path in python_files():
            hits = pep604_hits(path.read_text(encoding="utf-8"))
            if hits:
                rel = path.relative_to(ROOT)
                offenders.extend(f"{rel} {hit}" for hit in hits)
        self.assertEqual(
            offenders, [],
            "这些注解用了 PEP 604 的 `X | Y`，而本机/CI 的 Python 3.9 会在**求值时**抛 "
            f"TypeError（整个模块加载失败，不是少一条测试）：{NEED}\n" + "\n".join(offenders),
        )

    def test_the_scanner_catches_the_shape_that_actually_broke(self):
        """反向验证：真正的坏样本必须报出来，三种无辜写法必须放过。"""
        broken = "def f(x: dict | None):\n    return x\n"
        self.assertEqual(len(pep604_hits(broken)), 1)

        fixed = "from __future__ import annotations\n" + broken
        self.assertEqual(pep604_hits(fixed), [])

        optional = "import typing\n\ndef f(x: typing.Optional[dict]):\n    return x\n"
        self.assertEqual(pep604_hits(optional), [])

        # 函数体里的局部变量注解不求值 —— 报了它就会教人把判据关掉
        local = "def f():\n    row: dict | None = None\n    return row\n"
        self.assertEqual(pep604_hits(local), [])

    def test_the_scanner_reads_the_real_tree(self):
        """扫描器看的是文件字节，而且真的扫到了产品代码（不然它只是个永远绿的摆设）。"""
        names = {path.name for path in python_files()}
        self.assertIn("mailio.py", names)
        self.assertIn("web.py", names)
        if (ROOT / "tools").is_dir():
            self.assertIn("preflight.py", names)


if __name__ == "__main__":
    unittest.main()
