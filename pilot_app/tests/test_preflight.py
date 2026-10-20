# -*- coding: utf-8 -*-
"""Tests for `tools/preflight.py` —— 「上线前那一遍」的判据本身。

这一层最值得测的不是「怎么跑测试」（那是两个运行器的事），而是**结论是怎么得出来的**：
什么时候算 GO、什么时候算 NO-GO、红的东西有没有被点出名字。这些判断错了不会报错，
只会让一次红的运行看起来是绿的 —— 而那正是「上线前跑一遍」存在的唯一理由。

另外两类测试是**边界**，不是功能：

* 这个工具**不许有碰生产的路**（不部署、不发信、不读真 key）。它不是靠自觉，
  是靠源码里根本没有那几个入口。
* 预发自检**必须复用**那 20 套现成的浏览器检查，不许另起炉灶写一套新的"冒烟测试"——
  第二套检查迟早和第一套说不一样的话。
"""

import fnmatch
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import preflight  # noqa: E402


class SuiteListTests(unittest.TestCase):
    def test_names_come_from_the_runner_and_every_one_has_a_script(self):
        # 下限 2026-09-24 由 20 降到 19：布告栏下线，`bulletin_check` 连同功能一起去掉
        # （运营者决定收下朋友那一版改版）。**棘轮降低是人工动作**，原因写在这里。
        names = preflight.expected_suites(ROOT)
        self.assertGreaterEqual(
            len(names), 19,
            "套件名单比 19 少 —— 少了的那几个会在没人看见的地方不跑，而结论照样叫「全绿」")
        for name in names:
            self.assertTrue((ROOT / "tools" / f"{name}.js").exists(),
                            f"{name} 在运行器的名单里，却没有 tools/{name}.js")

    def test_a_runner_that_changed_shape_stops_the_run(self):
        """读不到名单时**必须停下来**：静默返回空名单等于「没有一条要跑」，那是最绿的假象。"""
        with tempfile.TemporaryDirectory() as tmp:
            tree = pathlib.Path(tmp)
            (tree / "tools").mkdir()
            (tree / "tools" / "run_browser_checks.sh").write_text(
                "#!/usr/bin/env bash\necho hello\n", encoding="utf-8")
            with self.assertRaises(SystemExit):
                preflight.expected_suites(tree)

    def test_staging_self_check_reuses_real_suites(self):
        names = preflight.expected_suites(ROOT)
        for name in preflight.STAGING_SMOKE:
            self.assertIn(name, names, f"{name} 不在那 19 套里 —— 预发自检不许另写一套")


class UnitParsingTests(unittest.TestCase):
    def test_ok_needs_the_ok_line_and_a_real_count(self):
        self.assertTrue(preflight.parse_unit_log("Ran 3 tests in 0.1s\n\nOK\n", 0)["ok"])
        # 退出码 0、一条都没跑到：不算绿（收集阶段出错时正是这个样子）
        self.assertFalse(preflight.parse_unit_log("Ran 0 tests in 0.0s\n\nOK\n", 0)["ok"])
        # 有 OK 行但退出码不是 0：不算绿
        self.assertFalse(preflight.parse_unit_log("Ran 3 tests in 0.1s\n\nOK\n", 1)["ok"])
        self.assertFalse(preflight.parse_unit_log("", 0)["ok"])

    def test_failing_tests_are_named(self):
        text = ("Ran 1940 tests in 200.0s\n\nFAILED (failures=2, errors=1)\n"
                "FAIL: test_a (pilot_app.tests.test_x.C)\n"
                "ERROR: test_b (pilot_app.tests.test_x.C)\n")
        result = preflight.parse_unit_log(text, 1)
        self.assertEqual(result["count"], 1940)
        self.assertEqual(result["failures"], ["FAIL: test_a", "ERROR: test_b"])


class RunnerSummaryTests(unittest.TestCase):
    EXPECTED = ["landing_check", "shell_check", "tasks_check"]

    def test_all_green(self):
        text = "  passed: 3  landing_check shell_check tasks_check\n"
        result = preflight.parse_runner_summary(text, self.EXPECTED, rc=0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["failed"], [])
        self.assertEqual(result["missing"], [])

    def test_a_failed_suite_is_named(self):
        text = "  passed: 2  landing_check shell_check\n  FAILED: 1  tasks_check\n"
        result = preflight.parse_runner_summary(text, self.EXPECTED, rc=1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed"], ["tasks_check"])
        self.assertEqual(result["missing"], [])

    def test_a_suite_that_never_ran_is_not_a_pass(self):
        """运行器被人改短、或者某一套还没跑就崩了：`passed` 数得再对也不能算全绿。"""
        text = "  passed: 2  landing_check shell_check\n"
        result = preflight.parse_runner_summary(text, self.EXPECTED, rc=0)
        self.assertFalse(result["ok"])
        self.assertEqual(result["missing"], ["tasks_check"])

    def test_seed_failure_is_a_red_even_when_the_suite_passes(self):
        text = ("  tasks_check   (port 9001, fresh database)\n"
                "  播种失败，先看 /tmp/seed-tasks_check.log：\n"
                "  passed: 3  landing_check shell_check tasks_check\n")
        result = preflight.parse_runner_summary(text, self.EXPECTED, rc=0)
        self.assertFalse(result["ok"])
        # 只许点出真的播种失败的那一个，不许因为套件名在同一份输出里出现就全点名
        self.assertEqual(result["seed_failed"], ["tasks_check"])

    def test_a_suites_own_output_cannot_fake_the_summary(self):
        """套件失败时运行器会贴它的输出尾部。尾部里长得像汇总的行不算汇总。"""
        text = ("  tasks_check   (port 9002, fresh database)\n"
                "──── tasks_check 失败，输出尾部 ────\n"
                "  passed: 3  landing_check shell_check tasks_check\n"
                "───────────────────────────────\n"
                "══════════════════════════════════════════════════════════════\n"
                "  passed: 2  landing_check shell_check\n"
                "  FAILED: 1  tasks_check\n"
                "══════════════════════════════════════════════════════════════\n")
        result = preflight.parse_runner_summary(text, self.EXPECTED, rc=1)
        self.assertEqual(result["passed"], ["landing_check", "shell_check"])
        self.assertEqual(result["failed"], ["tasks_check"])
        self.assertEqual(result["missing"], [])
        self.assertFalse(result["ok"])


class VerdictTests(unittest.TestCase):
    def setUp(self):
        self.units = preflight.parse_unit_log("Ran 1940 tests in 200.0s\n\nOK\n", 0)

    def browsers(self, ok=True, **overrides):
        base = {"name": "浏览器检查", "ok": ok, "passed": ["a"], "failed": [], "missing": [],
                "seed_failed": [], "expected": ["a"], "planned": ["a"], "suite_logs": "/tmp/run/logs",
                "rc": 0 if ok else 1, "timed_out": False}
        base.update(overrides)
        return base

    def test_go_requires_both_halves(self):
        verdict, reds, code = preflight.decide(self.units, self.browsers(), False)
        self.assertEqual(code, 0)
        self.assertEqual(reds, [])
        self.assertTrue(verdict.startswith("GO"))

    def test_no_go_names_the_failing_suite(self):
        verdict, reds, code = preflight.decide(
            self.units, self.browsers(ok=False, failed=["tasks_check"]), False)
        self.assertEqual(code, 1)
        self.assertTrue(verdict.startswith("NO-GO"))
        self.assertTrue(any("tasks_check" in line for line in reds), reds)

    def test_no_go_names_the_failing_test(self):
        units = preflight.parse_unit_log(
            "Ran 5 tests in 1.0s\n\nFAILED (failures=1)\nFAIL: test_x (pilot_app.tests.test_m.C)\n", 1)
        verdict, reds, code = preflight.decide(units, self.browsers(), False)
        self.assertEqual(code, 1)
        self.assertTrue(any("test_x" in line for line in reds), reds)

    def test_missing_suites_are_named(self):
        verdict, reds, code = preflight.decide(
            self.units, self.browsers(ok=False, passed=[], missing=["tasks_check"]), False)
        self.assertEqual(code, 1)
        self.assertTrue(any("tasks_check" in line for line in reds), reds)

    def test_a_partial_run_is_never_a_go(self):
        """`--no-browser` / `--only` 是给自己迭代用的，它**不构成上线依据**。"""
        verdict, reds, code = preflight.decide(self.units, self.browsers(), True)
        self.assertEqual(code, 4)
        self.assertNotIn("GO", verdict)
        self.assertTrue(reds)

    def test_a_timeout_is_a_no_go(self):
        units = preflight.parse_unit_log("", 124)
        verdict, reds, code = preflight.decide(units, self.browsers(), False)
        self.assertEqual(code, 1)
        self.assertTrue(any("超时" in line for line in reds), reds)

    def test_skipped_units_do_not_count_as_green(self):
        units = {"name": "单测", "ok": True, "count": 0, "failures": [], "failure_count": 0,
                 "rc": 0, "timed_out": False, "skipped": True}
        verdict, reds, code = preflight.decide(units, self.browsers(), True)
        self.assertEqual(code, 4)
        self.assertNotIn("GO", verdict)


class FailureTailTests(unittest.TestCase):
    """红了要点名 —— 名字还不够，得让「哪条断言」出现在同一屏里。"""

    def test_the_tail_comes_from_the_copied_suite_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = pathlib.Path(tmp)
            (logs / "tasks_check.log").write_text(
                "一堆正常输出\n--- FAILURES ---\n  首页应当是 9 件，现在是 8 件\n", encoding="utf-8")
            tail = preflight.failure_tail("tasks_check", {"suite_logs": str(logs)})
            self.assertEqual(tail[-1], "首页应当是 9 件，现在是 8 件")

    def test_no_log_at_all_is_not_a_crash(self):
        self.assertEqual(preflight.failure_tail("nope_check", {"suite_logs": "/nonexistent"}), [])


class CommandLineTests(unittest.TestCase):
    """参数自相矛盾要在**动手之前**拦掉：跑了一半才发现，等于白等十几分钟。"""

    def test_skipping_both_halves_is_refused(self):
        with self.assertRaises(SystemExit) as caught:
            preflight.main(["--no-unit", "--no-browser"])
        self.assertEqual(caught.exception.code, 3)

    def test_only_together_with_no_browser_is_refused(self):
        with self.assertRaises(SystemExit) as caught:
            preflight.main(["--no-browser", "--only", "shell_check"])
        self.assertEqual(caught.exception.code, 3)


class SafetyTests(unittest.TestCase):
    """底线 1/2 的代码化：不是「我们不会那么干」，是源码里没有那条路。"""

    SOURCE = (ROOT / "tools" / "preflight.py").read_text(encoding="utf-8")

    def test_it_has_no_road_to_production(self):
        for forbidden in ("deploy_prod", "smtplib", "send_as_operator", "systemctl",
                          "203.0.113.10", "/etc/cityu-mail-pilot", "/opt/cityu-mail-pilot",
                          "pilot.env"):
            self.assertNotIn(forbidden, self.SOURCE,
                             f"预检工具里出现了 {forbidden} —— 它不该有碰生产的路")

    def test_it_starts_the_web_half_and_never_the_worker(self):
        """会发信的是 worker。预发站点只起 web，所以「不发信」是结构上的，不是配置上的。"""
        self.assertIn("pilot_app.web", self.SOURCE)
        self.assertNotIn("pilot_app.worker", self.SOURCE)

    def test_staging_home_refuses_places_that_hold_real_things(self):
        original = preflight.PREFLIGHT_HOME
        try:
            for bad in ("/etc/cityu-mail-pilot", "/opt/cityu-mail-pilot", "/var/lib/whatever",
                        "/srv", "/root", "/usr/local"):
                preflight.PREFLIGHT_HOME = pathlib.Path(bad)
                with self.assertRaises(SystemExit):
                    preflight._assert_safe_home()
        finally:
            preflight.PREFLIGHT_HOME = original

    def test_the_fake_credentials_are_the_published_ones(self):
        self.assertEqual(preflight.FAKE_MASTER, "A" * 43 + "=")
        self.assertEqual(preflight.ADMIN_EMAIL, "boss@example.com")
        self.assertEqual(preflight.ADMIN_PASSWORD, "a-long-enough-password")

    def test_the_staging_site_binds_loopback_and_opens_through_a_tunnel(self):
        self.assertIn('"--host", "127.0.0.1"', self.SOURCE)   # 不对外监听
        self.assertIn('"-N", "-L"', self.SOURCE)              # Mac 那边走 ssh 端口转发


class PidTests(unittest.TestCase):
    """`--stop` 要发信号，所以「这个 pid 还是不是我们的 web」必须问过再动。"""

    def test_a_pid_that_is_not_the_web_process_is_not_ours(self):
        import os
        self.assertFalse(preflight.pid_is_our_web(0))
        self.assertFalse(preflight.pid_is_our_web(-1))
        # 这个测试进程是 unittest，不是 pilot_app.web —— 台账里写着它就不该被当成站点
        self.assertFalse(preflight.pid_is_our_web(os.getpid()))


class CopyTests(unittest.TestCase):
    """`--pr` 的前提：副本是干净的一份，秘密与生成物都不进去。"""

    def _source_tree(self, tmp):
        src = tmp / "src"
        (src / ".secrets").mkdir(parents=True)
        (src / ".secrets" / "prod.env").write_text("MASTER=real", encoding="utf-8")
        (src / ".git").mkdir()
        (src / ".git" / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")
        (src / "dist").mkdir()
        (src / "dist" / "release.tar.gz").write_bytes(b"x" * 32)
        (src / "handoff").mkdir()
        (src / "handoff" / "snapshot.tar.gz").write_bytes(b"x" * 32)
        (src / "pilot_app").mkdir()
        (src / "pilot_app" / "__init__.py").write_text("__version__ = '0'\n", encoding="utf-8")
        (src / "publish-private.json").write_text("{}", encoding="utf-8")
        (src / "preview.sqlite3").write_bytes(b"db")
        return src

    def test_secrets_and_generated_things_stay_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            src = self._source_tree(tmp)
            dst = tmp / "copy"
            original = preflight.PREFLIGHT_HOME
            preflight.PREFLIGHT_HOME = tmp / "home"
            try:
                preflight.copy_tree(src, dst)
            finally:
                preflight.PREFLIGHT_HOME = original
            self.assertTrue((dst / "pilot_app" / "__init__.py").exists(), "该带上的没带上")
            # `handoff/` 不在这张名单里：它里面那三个小文件是单测要读的（见下一条测试），
            # 该挡的是它里面几百 MB 的快照。
            for gone in (".secrets", ".git", "dist"):
                self.assertFalse((dst / gone).exists(), f"{gone} 不该进副本")
            for gone in ("publish-private.json", "preview.sqlite3"):
                self.assertFalse((dst / gone).exists(), f"{gone} 不该进副本")

    @unittest.skipUnless((ROOT / ".venv-pilot").exists() and (ROOT / "node_modules").exists(),
                         "这台机器上没有 .venv-pilot / node_modules —— CI 上就是这样，"
                         "副本里也就没有链接可接（那不是 bug）")
    def test_the_interpreter_and_playwright_are_linked_not_copied(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            src = self._source_tree(tmp)
            dst = tmp / "copy"
            original = preflight.PREFLIGHT_HOME
            preflight.PREFLIGHT_HOME = tmp / "home"
            try:
                preflight.copy_tree(src, dst)
            finally:
                preflight.PREFLIGHT_HOME = original
            for name in (".venv-pilot", "node_modules"):
                link = dst / name
                self.assertTrue(link.is_symlink(), f"{name} 应该是符号链接（几百 MB 的东西不复制）")
                self.assertEqual(link.resolve(), (ROOT / name).resolve())

    def test_small_handoff_files_ride_along_but_the_archives_do_not(self):
        """`handoff/` 不能整目录挡掉：单测要读里面那三个小文件，而快照是几百 MB。

        这一条是被真实的一轮预检抓出来的（2026-09-20）：试打补丁的副本里
        `test_handoff` 红在「少带了 handoff/HANDOFF.md 那三个文件」。
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            src = tmp / "src"
            (src / "handoff").mkdir(parents=True)
            for name in ("HANDOFF.md", "LEDGER.jsonl", "cloud-pilot.env.sanitized"):
                (src / "handoff" / name).write_text("x", encoding="utf-8")
            (src / "handoff" / "cityu-mail-pilot-source-0.0.1.tar.gz").write_bytes(b"x" * 32)
            (src / "pilot_app").mkdir()
            (src / "pilot_app" / ".env").write_text("SECRET=1", encoding="utf-8")
            original = preflight.PREFLIGHT_HOME
            preflight.PREFLIGHT_HOME = tmp / "home"
            try:
                preflight.copy_tree(src, tmp / "copy")
            finally:
                preflight.PREFLIGHT_HOME = original
            for name in ("HANDOFF.md", "LEDGER.jsonl", "cloud-pilot.env.sanitized"):
                self.assertTrue((tmp / "copy" / "handoff" / name).exists(), f"少了 handoff/{name}")
            self.assertFalse((tmp / "copy" / "handoff" / "cityu-mail-pilot-source-0.0.1.tar.gz").exists(),
                             "几百 MB 的快照不该进副本")
            self.assertFalse((tmp / "copy" / "pilot_app" / ".env").exists(), ".env 是秘密，不进副本")

    def test_the_local_toolchains_never_ride_along(self):
        """本机视频生成工具链有 **65 GB**（ComfyUI + 模型权重，已 gitignore、与产品无关）。

        不挡的话 `--pr/--patch` 会试着把它整个拷进副本，然后死在「磁盘满」上 ——
        那是工装故障，看起来却像补丁打不上。
        """
        self.assertIn("videogen", preflight.COPY_EXCLUDE)

    def test_the_run_home_is_never_copied_into_its_own_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = pathlib.Path(tmp)
            src = tmp / "src"
            (src / "pilot_app").mkdir(parents=True)
            home = src / ".e2e" / "preflight"
            home.mkdir(parents=True)
            (home / "staging.json").write_text("{}", encoding="utf-8")
            original = preflight.PREFLIGHT_HOME
            preflight.PREFLIGHT_HOME = home
            try:
                preflight.copy_tree(src, tmp / "copy")
            finally:
                preflight.PREFLIGHT_HOME = original
            self.assertFalse((tmp / "copy" / ".e2e").exists(), "运行记录被拷进了副本（会自己套自己）")


class PrivateFileBackupTests(unittest.TestCase):
    """给秘密文件做备份，别让「保险」变成新的泄漏面（2026-09-23 宿舍机实测）。

    那台机器给 `publish-private.json` 建了一份 `.bak-日期` 副本，随后发现：**精确名挡不住
    它** —— `.gitignore` 只写了原文件名，`git add -A` 一条命令就能把那份副本（生产域名/IP、
    运营者与用户的邮箱、主密钥指纹、部署密钥名）提交进仓库。铁律 2 说的就是这件事。

    真正的纪律是**把备份放在仓库外**；这两条断言只是最后一道网，所以它们盯的是
    「规则本身能不能盖住备份的写法」，而不是某一个具体文件名。
    """

    BACKUP_SPELLINGS = ("publish-private.json",
                        "publish-private.json.bak-20260923",
                        "publish-private.json.old")

    def test_preflight_keeps_every_spelling_out_of_its_copy(self):
        for name in self.BACKUP_SPELLINGS:
            with self.subTest(name=name):
                self.assertTrue(preflight.copy_name_is_excluded(name), name)
        # 反面：正儿八经的示例文件仍然要进副本（挡住了测试就会在副本里红）
        self.assertFalse(preflight.copy_name_is_excluded("pilot_app/.env.example"))
        self.assertFalse(preflight.copy_name_is_excluded("app.js"))

    def test_gitignore_covers_the_backup_spellings_too(self):
        patterns = [line.strip() for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.strip().startswith("#")]
        for name in self.BACKUP_SPELLINGS:
            with self.subTest(name=name):
                self.assertTrue(any(fnmatch.fnmatch(name, pattern) for pattern in patterns),
                                f"{name} 不在 .gitignore 里：{patterns}")


if __name__ == "__main__":
    unittest.main()
