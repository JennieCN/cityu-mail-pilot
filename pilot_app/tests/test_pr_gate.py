# -*- coding: utf-8 -*-
"""`tools/pr_gate.py` 的判据 —— 「收一条 PR」那条命令的结论是怎么得出来的。

这一层值得测的不是「怎么跑测试」（那是 `preflight` 与两个运行器的事），而是**三件会静默出错的事**：

1. **什么算 GO**：GO 必须同时有单测全过与 20/20 套件；少一套、跳过一半、台账缺失
   都不算 —— 判错的代价是让人把一条红的 PR 收进树里。
2. **补丁打不上算不算 NO-GO**：打不上就**停**（连闸门都不跑），而且要**点得出哪几个文件冲突**，
   不能只说一句「打不上」。
3. **建议只有三种，且每种都要说得出理由**：收 / 让作者改 / 关掉。红的是工装（没跑到、
   播种失败、超时）时不许赖到作者头上 —— 那时候三选一里没有正确答案，就得说「先别收」。

还有一类是**边界**：这条命令**没有**碰生产/凭据/工作区的路（不是靠自觉，是源码里没有那几个入口），
以及**没有开着的 PR 时也自证得了**（`--patch FILE` 走的就是同一条路，测试用注入的 runner 离线跑完整条）。
"""

from __future__ import annotations  # 3.9 上 `dict | None` 会在 import 时炸（见 test_python_targets）

import contextlib
import difflib
import io
import json
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import pr_gate  # noqa: E402
import preflight  # noqa: E402

OLD_BODY = ("第一行\n"
            "第二行\n"
            "第三行\n")
NEW_BODY = ("第一行\n"
            "第二行（改过）\n"
            "第三行\n")


def make_patch(old: str = OLD_BODY, new: str = NEW_BODY,
               path: str = "docs/demo.md", *, new_file: bool = False) -> str:
    """一份**真的**补丁（difflib 生成，不手写 hunk 头）。"""
    diff = difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile="/dev/null" if new_file else f"a/{path}", tofile=f"b/{path}")
    text = "".join(line if line.endswith("\n") else line + "\n" for line in diff)
    return f"diff --git a/{path} b/{path}\n" + ("new file mode 100644\n" if new_file else "") + text


PATCH = make_patch()


# ---------------------------------------------------------------- 台账（闸门写下的那份）

def make_ledger(*, unit_count=2038, unit_failures=(), units_ok=None, skipped_units=False,
                failed=(), missing=(), seed_failed=(), passed=None, expected=None,
                planned=None, browsers_ok=None, rc=0, timed_out=False, dirty_before="",
                dirty_after="", verdict=None, exit_code=None) -> dict:
    """照着 `preflight.Ledger.write()` 写出来的 `summary.json` 的样子造一份台账。

    字段名与真实那份逐字一致（`phases` 里两项：单测 / 浏览器检查）——
    对不上的话这些测试就只是在测自己造的假数据。
    """
    suites = expected if expected is not None else preflight.expected_suites(ROOT)
    planned = planned if planned is not None else list(suites)
    if passed is None:
        passed = [name for name in planned if name not in failed and name not in missing]
    if units_ok is None:
        units_ok = not unit_failures and not skipped_units
    if browsers_ok is None:
        browsers_ok = not failed and not missing and not seed_failed and rc == 0 and not timed_out
    units = {"name": "单测", "ok": units_ok, "count": unit_count,
             "failures": list(unit_failures), "failure_count": len(unit_failures),
             "rc": 0 if units_ok else 1, "timed_out": timed_out and not skipped_units,
             "seconds": 226.0, "log": "/tmp/run/unit.log"}
    if skipped_units:
        units.update({"ok": True, "count": 0, "failures": [], "failure_count": 0,
                      "rc": 0, "timed_out": False, "skipped": True})
    browsers = {"name": "浏览器检查", "ok": browsers_ok, "passed": list(passed),
                "failed": list(failed), "missing": list(missing),
                "seed_failed": list(seed_failed), "expected": list(suites),
                "planned": list(planned), "rc": rc, "timed_out": timed_out, "seconds": 271.0,
                "log": "/tmp/run/browser.log", "suite_logs": "/tmp/run/logs", "copied": []}
    if not planned:
        browsers["skipped"] = True
        browsers["ok"] = True
    ledger = {"started_at": "2026-09-21T00:00:00+00:00", "finished_at": "2026-09-21T00:10:00+00:00",
              "root": str(ROOT), "tree": "/tmp/copy", "kind": "本地补丁副本",
              "patch": {"source": "本地补丁 x.diff", "sha256": "0" * 64, "files": ["docs/demo.md"],
                        "archive": "/tmp/copy/patch.diff", "sensitive": []},
              "version": "0.64.1", "commit": "abc1234",
              "dirty_before": dirty_before, "dirty_after": dirty_after,
              "phases": [units, browsers], "verdict": verdict, "exit": exit_code}
    if verdict is None:
        _, _, code = preflight.decide(units, browsers, bool(skipped_units or not planned))
        ledger["verdict"] = {"0": "GO ✅", "1": "NO-GO ❌", "4": "未完成（没跑全）"}[str(code)]
        ledger["exit"] = code
    return ledger


class LedgerTests(unittest.TestCase):
    """造出来的台账必须和真的一致 —— 否则下面每一条都在测假数据。"""

    def test_the_fixture_carries_what_the_verdict_reads(self):
        """`gate_result` 从台账里读的每一个字段都要在夹具里（少一个就静默算成绿的）。"""
        ledger = make_ledger()
        units, browsers = ledger["phases"]
        for key in ("name", "ok", "timed_out", "count", "failure_count", "failures"):
            self.assertIn(key, units)
        for key in ("name", "ok", "timed_out", "passed", "failed", "missing", "seed_failed",
                    "expected", "planned", "rc", "log", "suite_logs"):
            self.assertIn(key, browsers)
        for key in ("phases", "verdict", "exit", "dirty_before", "dirty_after", "kind"):
            self.assertIn(key, ledger)

    def test_the_fixture_matches_a_real_ledger_shape(self):
        real_path = (ROOT / ".e2e" / "preflight" / "runs" / "2026-09-20T015852-patch"
                     / "summary.json")
        if not real_path.exists():
            self.skipTest("这台机器上没有留存的真台账（.e2e/ 是本机目录，别的机器上会跳过）")
        real = json.loads(real_path.read_text(encoding="utf-8"))
        for ledger in (make_ledger(), make_ledger(skipped_units=True)):
            self.assertEqual([p["name"] for p in ledger["phases"]],
                             [p["name"] for p in real["phases"]])
            for mine, theirs in zip(ledger["phases"], real["phases"]):
                # 跳过的那一半会多一个 `skipped` 键（preflight 自己就是这么写的）
                self.assertEqual(set(mine) - {"skipped"}, set(theirs),
                                 f"{mine['name']} 的字段与真台账对不上")


# ---------------------------------------------------------------- ① 补丁还打得上吗

class PatchStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        (self.root / "docs").mkdir(parents=True)
        self.target = self.root / "docs" / "demo.md"
        self.target.write_text(OLD_BODY, encoding="utf-8")

    def test_a_patch_that_fits_is_operable(self):
        state = pr_gate.patch_state(PATCH, self.root)
        self.assertEqual(state["verdict"], "applies")
        self.assertEqual(state["conflicts"], [])
        self.assertEqual(state["files"], ["docs/demo.md"])

    def test_a_stale_context_names_the_conflicting_file(self):
        self.target.write_text("这里被彻底改过了\n", encoding="utf-8")
        state = pr_gate.patch_state(PATCH, self.root)
        self.assertEqual(state["verdict"], "conflicts")
        self.assertEqual(state["conflicts"], ["docs/demo.md"], "打不上必须点得出文件名")

    def test_only_the_files_that_really_conflict_are_named(self):
        """一份补丁里有的文件打得上、有的打不上：只许点名打不上的那个。"""
        (self.root / "docs" / "fine.md").write_text("甲\n乙\n丙\n", encoding="utf-8")
        good = make_patch("甲\n乙\n丙\n", "甲\n乙改\n丙\n", path="docs/fine.md")
        patch = good + PATCH.replace(OLD_BODY.splitlines(keepends=True)[1],
                                     "这一行在树里根本不存在\n")
        state = pr_gate.patch_state(patch, self.root)
        self.assertEqual(state["verdict"], "conflicts")
        self.assertEqual(state["conflicts"], ["docs/demo.md"])
        self.assertNotIn("docs/fine.md", state["conflicts"])

    def test_a_missing_file_is_named_and_does_not_hang(self):
        """补丁要改的文件树里没有时，`patch` 会问 `File to patch:` —— 关掉 stdin 才不挂住。"""
        patch = make_patch("hello\n", "hello\nworld\n", path="docs/no-such-file.md")
        state = pr_gate.patch_state(patch, self.root)
        self.assertEqual(state["verdict"], "conflicts")
        self.assertEqual(state["conflicts"], ["docs/no-such-file.md"])

    def test_a_new_file_is_not_a_conflict(self):
        patch = make_patch("", "全新的文件\n", path="docs/brand-new.md", new_file=True)
        state = pr_gate.patch_state(patch, self.root)
        self.assertEqual(state["verdict"], "applies")
        self.assertEqual(state["created"], ["docs/brand-new.md"])
        self.assertEqual(state["conflicts"], [])

    def test_something_already_merged_is_recognised(self):
        self.target.write_text(NEW_BODY, encoding="utf-8")
        state = pr_gate.patch_state(PATCH, self.root)
        self.assertEqual(state["verdict"], "already")
        self.assertEqual((state["found"], state["total"]), (1, 1))

    def test_an_empty_patch_says_so_instead_of_crashing(self):
        state = pr_gate.patch_state("", self.root)
        self.assertEqual(state["verdict"], "empty")
        self.assertFalse(state["applies"])

    def test_a_preview_writes_nothing(self):
        """底线：试打是**只读**的。一个字节、一个 mtime 都不许变。"""
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in sorted(self.root.rglob("*")) if path.is_file()}
        pr_gate.patch_state(PATCH, self.root)
        pr_gate.patch_state(PATCH.replace("第二行（改过）", "另一行"), self.root)
        after = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                 for path in sorted(self.root.rglob("*")) if path.is_file()}
        self.assertEqual(before, after, "试打动了工作区")

    def test_sensitive_files_are_flagged(self):
        patch = make_patch(OLD_BODY, NEW_BODY, path="pilot_app/mailio.py")
        self.assertEqual(pr_gate.patch_state(patch, self.root)["sensitive"],
                         ["pilot_app/mailio.py"])


# ---------------------------------------------------------------- ② 什么算 GO

class GateResultTests(unittest.TestCase):
    def test_a_full_green_run_is_a_go(self):
        gate = pr_gate.gate_result(0, make_ledger())
        self.assertTrue(gate["ran"])
        self.assertEqual(gate["code"], 0)
        self.assertTrue(gate["verdict"].startswith("GO"))
        self.assertEqual((gate["author_reds"], gate["infra_reds"]), ([], []))

    def test_a_short_suite_list_is_not_a_go(self):
        """**少跑一套**：运行器被人改短、或某一套还没跑就崩了 —— 那不算 GO。"""
        suites = preflight.expected_suites(ROOT)
        gate = pr_gate.gate_result(0, make_ledger(missing=[suites[3]]))
        self.assertFalse(gate["verdict"].startswith("GO"))
        self.assertEqual(gate["code"], 1)
        self.assertTrue(any(suites[3] in line for line in gate["reds"]), gate["reds"])
        self.assertTrue(any(suites[3] in line for line in gate["infra_reds"]))

    def test_a_failing_suite_is_named(self):
        gate = pr_gate.gate_result(1, make_ledger(failed=["shell_check"], rc=1))
        self.assertEqual(gate["code"], 1)
        self.assertIn("浏览器检查 shell_check", gate["author_reds"])

    def test_a_failing_test_is_named(self):
        gate = pr_gate.gate_result(
            1, make_ledger(unit_failures=["FAIL: test_nav_order"], units_ok=False))
        self.assertIn("单测 FAIL: test_nav_order", gate["author_reds"])

    def test_a_skipped_half_is_never_a_go(self):
        gate = pr_gate.gate_result(4, make_ledger(skipped_units=True))
        self.assertEqual(gate["code"], 4)
        self.assertFalse(gate["verdict"].startswith("GO"))
        self.assertTrue(gate["partial"])

    def test_only_one_suite_is_never_a_go(self):
        suites = preflight.expected_suites(ROOT)
        gate = pr_gate.gate_result(4, make_ledger(expected=suites, planned=[suites[0]],
                                                  passed=[suites[0]]))
        self.assertEqual(gate["code"], 4)
        self.assertFalse(gate["verdict"].startswith("GO"))

    def test_a_seed_failure_is_an_infrastructure_red(self):
        gate = pr_gate.gate_result(0, make_ledger(seed_failed=["tasks_check"]))
        self.assertFalse(gate["verdict"].startswith("GO"))
        self.assertTrue(any("tasks_check" in line for line in gate["infra_reds"]))
        self.assertEqual(gate["author_reds"], [], "播种失败是我们的工装，不是作者的代码")

    def test_a_missing_ledger_is_not_a_go(self):
        for code in (0, 1, 3):
            gate = pr_gate.gate_result(code, None)
            self.assertFalse(gate["ran"])
            self.assertFalse(gate["verdict"].startswith("GO"))

    def test_a_ledger_that_says_no_go_beats_phases_that_look_green(self):
        """台账与逐条判据不一致时**以更坏的那个为准** —— 宁可多说一句，不能读成绿的。"""
        ledger = make_ledger()
        ledger["verdict"], ledger["exit"] = "NO-GO ❌", 1
        gate = pr_gate.gate_result(1, ledger)
        self.assertEqual(gate["code"], 1)
        self.assertTrue(any("台账" in line for line in gate["reds"]))

    def test_a_workspace_that_changed_mid_run_is_a_red(self):
        gate = pr_gate.gate_result(1, make_ledger(dirty_before="", dirty_after=" M x.py"))
        self.assertFalse(gate["dirty_same"])
        self.assertTrue(any("工作区" in line for line in gate["infra_reds"]))


# ---------------------------------------------------------------- ③ 建议

class AdviceTests(unittest.TestCase):
    def state(self, verdict="applies", **overrides):
        base = {"verdict": verdict, "files": ["docs/demo.md"], "added": 1, "removed": 1,
                "created": [], "sha256": "0" * 64, "sensitive": [], "found": 0, "total": 1,
                "conflicts": [], "output": "", "applies": verdict == "applies"}
        base.update(overrides)
        return base

    def test_a_green_gate_says_take_it(self):
        advice = pr_gate.advise(self.state(), pr_gate.gate_result(0, make_ledger()))
        self.assertEqual(advice["pick"], "收下")
        self.assertEqual(advice["exit"], 0)
        self.assertTrue(advice["options"]["收下"][0])
        self.assertFalse(advice["options"]["让作者改"][0])

    def test_every_option_always_has_a_reason(self):
        """「各一句理由」是这条命令的输出契约，任何分支都不许少。"""
        cases = [
            (self.state(), pr_gate.gate_result(0, make_ledger())),
            (self.state(verdict="conflicts", conflicts=["docs/demo.md"]),
             pr_gate.gate_result(None, None)),
            (self.state(verdict="already", found=1, total=1), pr_gate.gate_result(None, None)),
            (self.state(verdict="empty", files=[], added=0, removed=0),
             pr_gate.gate_result(None, None)),
            (self.state(), pr_gate.gate_result(1, make_ledger(
                unit_failures=["FAIL: test_x"], units_ok=False))),
            (self.state(), pr_gate.gate_result(3, None)),
            (self.state(), pr_gate.gate_result(4, make_ledger(skipped_units=True))),
            (self.state(), pr_gate.gate_result(1, make_ledger(missing=["shell_check"]))),
        ]
        for state, gate in cases:
            advice = pr_gate.advise(state, gate)
            self.assertIn(advice["pick"], pr_gate.OPTIONS + ("先别收",))
            self.assertTrue(advice["why"].strip(), "建议必须说得出理由")
            self.assertEqual(set(advice["options"]), set(pr_gate.OPTIONS))
            for name, (_, reason) in advice["options"].items():
                self.assertTrue(reason.strip(), f"{name} 没有理由")

    def test_a_patch_that_will_not_apply_stops_and_asks_the_author(self):
        advice = pr_gate.advise(
            self.state(verdict="conflicts", conflicts=["docs/a.md", "docs/b.md"]),
            pr_gate.gate_result(None, None))
        self.assertEqual(advice["pick"], "让作者改")
        self.assertEqual(advice["exit"], 1)
        for name in ("docs/a.md", "docs/b.md"):
            self.assertIn(name, advice["why"], "冲突的文件名要出现在建议里")

    def test_a_partially_matched_patch_mentions_the_close_route(self):
        """打不上但有一半行在树里：要提醒「可能是同一件事我们用自己的话做过」。"""
        advice = pr_gate.advise(
            self.state(verdict="conflicts", conflicts=["docs/a.md"], found=5, total=8),
            pr_gate.gate_result(None, None))
        self.assertIn("5", advice["why"])
        self.assertIn("关掉", advice["why"])

    def test_already_merged_says_close(self):
        advice = pr_gate.advise(self.state(verdict="already", found=2, total=2),
                                pr_gate.gate_result(None, None))
        self.assertEqual(advice["pick"], "关掉")
        self.assertEqual(advice["exit"], 1)

    def test_an_empty_patch_says_close(self):
        advice = pr_gate.advise(self.state(verdict="empty", files=[], added=0, removed=0),
                                pr_gate.gate_result(None, None))
        self.assertEqual(advice["pick"], "关掉")

    def test_a_no_go_names_the_failing_assertion_in_the_advice(self):
        advice = pr_gate.advise(self.state(), pr_gate.gate_result(
            1, make_ledger(unit_failures=["FAIL: test_shell_nav"], units_ok=False)))
        self.assertEqual(advice["pick"], "让作者改")
        self.assertIn("test_shell_nav", advice["why"])

    def test_a_no_go_admits_it_has_no_baseline(self):
        """这一遍只跑过带补丁的树：树本来就红的时候，这条红与作者无关。

        工具分不出这两种，所以必须把「没有基线可比」说出来，而不是替作者认领别人的红。
        """
        advice = pr_gate.advise(self.state(), pr_gate.gate_result(
            1, make_ledger(unit_failures=["FAIL: test_x"], units_ok=False)))
        self.assertIn("基线", advice["why"])

    def test_a_failing_browser_suite_is_named_too(self):
        advice = pr_gate.advise(self.state(), pr_gate.gate_result(
            1, make_ledger(failed=["appearance_check"], rc=1)))
        self.assertEqual(advice["pick"], "让作者改")
        self.assertIn("appearance_check", advice["why"])

    def test_infrastructure_reds_do_not_blame_the_author(self):
        """没跑到/播种失败不是作者的错 —— 那时三选一里没有正确答案，只能「先别收」。"""
        advice = pr_gate.advise(self.state(), pr_gate.gate_result(
            1, make_ledger(missing=["tasks_check"])))
        self.assertEqual(advice["pick"], "先别收")
        self.assertFalse(advice["options"]["收下"][0])
        self.assertFalse(advice["options"]["让作者改"][0])

    def test_a_gate_that_never_started_gives_no_advice(self):
        advice = pr_gate.advise(self.state(), pr_gate.gate_result(3, None))
        self.assertEqual(advice["pick"], "先别收")
        self.assertEqual(advice["exit"], 4)

    def test_a_run_whose_tree_changed_mid_way_gives_no_advice(self):
        """跑的过程中工作区被改了（另一个写者）：那一轮的结论不算数，别拿去指作者的鼻子。

        这条判据是被真事撞出来的（2026-09-21）：一个会话在跑这一遍时，另一个会话正在
        改 `tools/*.js` —— preflight 自己报了「工作区在跑的过程中被改了」，而当时
        工具还在建议「让作者改」，把别人的手误算到了作者头上。
        """
        advice = pr_gate.advise(self.state(), pr_gate.gate_result(
            1, make_ledger(dirty_before="", dirty_after=" M tools/pw.js")))
        self.assertEqual(advice["pick"], "先别收")
        self.assertIn("工作区", advice["why"])
        self.assertFalse(advice["options"]["让作者改"][0])

    def test_a_partial_run_gives_no_advice(self):
        advice = pr_gate.advise(self.state(), pr_gate.gate_result(
            4, make_ledger(skipped_units=True)))
        self.assertEqual(advice["pick"], "先别收")
        self.assertNotEqual(advice["exit"], 0)
        self.assertIn("没跑全", advice["why"])

    def test_sensitive_files_warn_but_do_not_block_a_green_gate(self):
        advice = pr_gate.advise(self.state(sensitive=["pilot_app/mailio.py"]),
                                pr_gate.gate_result(0, make_ledger()))
        self.assertEqual(advice["pick"], "收下")
        self.assertIn("pilot_app/mailio.py", advice["why"])


# ---------------------------------------------------------------- ③ 闸门那一遍怎么被调起来

class RunGateTests(unittest.TestCase):
    """`run_gate()` 是唯一会跑闸门的地方：它读的是 preflight 写下的**新**台账。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = pathlib.Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.original_main = preflight.main

    def tearDown(self):
        preflight.main = self.original_main

    def _run_dir(self, name: str, ledger: dict | None):
        path = self.home / "runs" / name
        path.mkdir(parents=True)
        if ledger is not None:
            (path / "summary.json").write_text(json.dumps(ledger), encoding="utf-8")
        return path

    def test_the_new_ledger_is_picked_up(self):
        self._run_dir("2026-09-20T000000", make_ledger(verdict="GO ✅", exit_code=0))
        preflight.main = lambda argv: (self._run_dir("2026-09-21T000000", make_ledger()), 0)[1]
        result = pr_gate.run_gate(["--patch", "x.diff"], home=self.home)
        self.assertEqual(result["code"], 0)
        self.assertEqual(result["ledger"]["verdict"], "GO ✅")
        self.assertTrue(result["run_dir"].endswith("2026-09-21T000000"))

    def test_a_gate_that_died_before_the_ledger_returns_nothing(self):
        self._run_dir("2026-09-20T000000", make_ledger(verdict="GO ✅", exit_code=0))

        def dying(argv):
            self._run_dir("2026-09-21T000000", None)
            raise SystemExit(3)

        preflight.main = dying
        result = pr_gate.run_gate(["--patch", "x.diff"], home=self.home)
        self.assertEqual(result["code"], 3)
        self.assertIsNone(result["ledger"], "上一轮的台账不算这一轮的结论")

    def test_a_dying_gate_never_takes_the_old_ledger(self):
        self._run_dir("2026-09-20T000000", make_ledger(verdict="GO ✅", exit_code=0))
        preflight.main = lambda argv: (_ for _ in ()).throw(SystemExit(3))
        result = pr_gate.run_gate(["--patch", "x.diff"], home=self.home)
        self.assertIsNone(result["ledger"])
        self.assertEqual(pr_gate.gate_result(result["code"], result["ledger"])["code"], 3)


# ---------------------------------------------------------------- 整条命令（离线自证）

class CommandTests(unittest.TestCase):
    """没有开着的 PR 时也自证得了：`--patch FILE` 走的是同一条路（闸门用注入的 runner 假装）。"""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.home = self.root / "home"
        self.addCleanup(self.temporary.cleanup)
        (self.root / "docs").mkdir(parents=True)
        (self.root / "docs" / "demo.md").write_text(OLD_BODY, encoding="utf-8")
        self.patch_file = self.root / "incoming.diff"
        self.patch_file.write_text(PATCH, encoding="utf-8")

    def _run(self, patch_file=None, runner=None, extra=(), patch=None):
        said = io.StringIO()
        with contextlib.redirect_stdout(said):
            code = pr_gate.mode_gate(0, patch_file=str(patch_file or self.patch_file),
                                     root=self.root, runner=runner or self._never_called,
                                     extra=tuple(extra), home=self.home, patch=patch)
        return code, said.getvalue()

    def _never_called(self, argv):
        raise AssertionError(f"这一步不该去跑闸门：{argv}")

    def test_a_green_patch_ends_in_take_it(self):
        seen = {}

        def runner(argv):
            seen["argv"] = argv
            return {"code": 0, "ledger": make_ledger(), "run_dir": "/tmp/fake-run"}

        code, out = self._run(runner=runner)
        self.assertEqual(code, 0)
        self.assertIn("收下", out)
        self.assertIn("GO", out)
        # 闸门跑的就是写进 incoming.diff 的**同一份字节**，而不是又去网上取一次
        self.assertEqual(seen["argv"][0], "--patch")
        self.assertEqual(pathlib.Path(seen["argv"][1]).read_bytes(), self.patch_file.read_bytes())

    def test_a_patch_that_will_not_apply_never_starts_the_gate(self):
        (self.root / "docs" / "demo.md").write_text("这里被彻底改过了\n", encoding="utf-8")
        code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("让作者改", out)
        self.assertIn("docs/demo.md", out)

    def test_an_already_merged_patch_never_starts_the_gate(self):
        (self.root / "docs" / "demo.md").write_text(NEW_BODY, encoding="utf-8")
        code, out = self._run()
        self.assertEqual(code, 1)
        self.assertIn("关掉", out)

    def test_a_no_go_brings_the_failing_assertion_to_the_conclusion(self):
        def runner(argv):
            return {"code": 1, "ledger": make_ledger(unit_failures=["FAIL: test_shell_nav"],
                                                     units_ok=False),
                    "run_dir": "/tmp/fake-run"}

        code, out = self._run(runner=runner)
        self.assertEqual(code, 1)
        self.assertIn("NO-GO", out)
        self.assertIn("test_shell_nav", out)

    def test_the_workspace_is_untouched(self):
        def runner(argv):
            return {"code": 0, "ledger": make_ledger(), "run_dir": "/tmp/fake-run"}

        before = (self.root / "docs" / "demo.md").read_bytes()
        self._run(runner=runner)
        self.assertEqual((self.root / "docs" / "demo.md").read_bytes(), before)

    def test_the_patch_bytes_are_archived_next_to_the_run(self):
        def runner(argv):
            return {"code": 0, "ledger": make_ledger(), "run_dir": "/tmp/fake-run"}

        self._run(runner=runner)
        archived = list((self.home / "gate").glob("*/incoming.diff"))
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0].read_bytes(), self.patch_file.read_bytes())

    def test_it_refuses_a_pr_number_together_with_a_patch_file(self):
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                pr_gate.main(["3", "--patch", "x.diff"])
        self.assertEqual(caught.exception.code, 3)

    def test_it_refuses_to_run_with_neither(self):
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stderr(io.StringIO()):
                pr_gate.main([])
        self.assertEqual(caught.exception.code, 3)

    def test_a_patch_file_that_is_not_there_is_a_clean_stop(self):
        with self.assertRaises(SystemExit) as caught:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                pr_gate.mode_gate(0, patch_file="/nonexistent/x.diff", root=self.root,
                                  runner=self._never_called, home=self.home)
        self.assertEqual(caught.exception.code, 3)


# ---------------------------------------------------------------- 底线

class SafetyTests(unittest.TestCase):
    """底线不是「我们不会那么干」，是源码里没有那条路。"""

    SOURCE = (ROOT / "tools" / "pr_gate.py").read_text(encoding="utf-8")

    def test_it_has_no_road_to_production(self):
        for forbidden in ("deploy_prod", "publish_push", "smtplib", "send_as_operator",
                          "systemctl", "203.0.113.10", "/etc/cityu-mail-pilot",
                          "/opt/cityu-mail-pilot", "pilot.env"):
            self.assertNotIn(forbidden, self.SOURCE,
                             f"收 PR 的工具里出现了 {forbidden} —— 它不该有碰生产的路")

    def test_it_never_shells_out_on_its_own(self):
        """它自己一个进程都不起：试打走 pr_triage，闸门走 preflight —— 两块现成的料。"""
        self.assertNotIn("subprocess", self.SOURCE)
        self.assertIn("pr_triage.preview_apply", self.SOURCE)
        self.assertIn("preflight.main", self.SOURCE)

    def test_it_cannot_apply_a_patch_to_the_workspace(self):
        for forbidden in ("mode_apply", "--forward", "shutil.copy", "os.replace"):
            self.assertNotIn(forbidden, self.SOURCE,
                             f"{forbidden} 在收 PR 的工具里 —— 它只许碰临时副本")

    def test_it_never_writes_to_github(self):
        """只读公开仓库：没有 POST/PATCH/PUT/DELETE，也没有那三件要凭据的事。"""
        for verb in ('"POST"', "'POST'", '"PATCH"', '"PUT"', '"DELETE"', "merge("):
            self.assertNotIn(verb, self.SOURCE)

    def test_go_is_decided_in_exactly_one_place(self):
        """「什么算 GO」只由 preflight 的判据说了算 —— 这里不许再写一套阈值。"""
        self.assertIn("preflight.decide(", self.SOURCE)


class ConflictWordingTests(unittest.TestCase):
    """两家 `patch` 的措辞都要认（2026-09-21 在 Mac 上踩到）。

    上面那几条走的是**真的调 `patch`**，所以它们只验得到本机那一家的措辞：Linux 上过、
    macOS 上把好文件也点成冲突（第一版只认 GNU 的 `Hunk #N FAILED`，而 Apple 说的是
    `1 out of 1 hunks failed while patching 'x'`，还带引号）。这里把两家的话都钉住，
    于是**两台机器上跑的都是同一个判据**。
    """

    GNU = ("checking file docs/fine.md\n"
           "checking file docs/demo.md\n"
           "Hunk #1 FAILED at 2.\n"
           "1 out of 1 hunk FAILED -- saving rejects to file docs/demo.md.rej\n")
    APPLE = ("patching file 'docs/fine.md'\n"
             "patching file 'docs/demo.md'\n"
             "1 out of 1 hunks failed while patching 'docs/demo.md'\n")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        (self.root / "docs").mkdir(parents=True)
        (self.root / "docs" / "fine.md").write_text("甲\n乙\n丙\n", encoding="utf-8")
        (self.root / "docs" / "demo.md").write_text(OLD_BODY, encoding="utf-8")
        # 一份含两个文件的补丁：规则② 由此取文件名，两个文件在树里都存在，所以它不该点名
        self.patch = (make_patch("甲\n乙\n丙\n", "甲\n乙改\n丙\n", path="docs/fine.md")
                      + make_patch())

    def test_gnu_wording_names_only_the_broken_file(self):
        self.assertEqual(pr_gate.conflict_files(self.patch, self.root, self.GNU),
                         ["docs/demo.md"])

    def test_apple_wording_names_only_the_broken_file(self):
        self.assertEqual(pr_gate.conflict_files(self.patch, self.root, self.APPLE),
                         ["docs/demo.md"])

    def test_a_missing_file_is_still_named_without_any_output(self):
        """`can't find file to patch` 那句话里没有文件名 —— 名字只能从补丁里取，别退化。"""
        (self.root / "docs" / "demo.md").unlink()
        for output in ("", "can't find file to patch at input line 3\n"):
            self.assertIn("docs/demo.md", pr_gate.conflict_files(self.patch, self.root, output))


if __name__ == "__main__":
    unittest.main()
