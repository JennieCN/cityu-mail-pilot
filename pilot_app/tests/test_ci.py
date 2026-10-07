# -*- coding: utf-8 -*-
"""Tests for the CI setup and for the one thing that made CI impossible.

The browser suites used to open with a hard-coded
``require('/tmp/pw/node_modules/playwright')``: the scratch directory this
project installed Playwright into once, on one laptop. Everything else about the
suites was portable; that line was not, and it is why "run the checks" only ever
meant "run the checks on the machine where they were written".

The tests below are mostly about **reachability**, because that is the failure
mode a CI configuration has: a workflow that is not published never runs, one
that repeats the suite list drifts from it, and one that can reach production is
a liability rather than a check. None of these fail loudly on their own -- the
build is green either way.
"""

import ast
import pathlib
import re
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
TOOLS = ROOT / "tools"


class PlaywrightResolutionTests(unittest.TestCase):
    def test_nothing_hard_codes_the_scratch_install(self):
        """One line, once, decided that 20 suites could only run in one place."""
        offenders = sorted(
            path.name for path in TOOLS.glob("*.js")
            if "/tmp/pw/node_modules/playwright" in path.read_text(encoding="utf-8")
            # pw.js is allowed to name it: it is the file whose job is to look
            # there. Nothing else may.
            and path.name != "pw.js"
        )
        self.assertEqual(offenders, [], f"这些文件又写死了 Playwright 的路径：{offenders}")

    def test_every_suite_that_drives_a_browser_goes_through_the_resolver(self):
        users = [path.name for path in TOOLS.glob("*.js")
                 if "require('./pw')" in path.read_text(encoding="utf-8")
                 and path.name != "pw.js"]
        self.assertGreaterEqual(len(users), 20, f"只有 {len(users)} 个文件用了解析器")
        self.assertIn("landing_check.js", users)
        self.assertIn("metrics_check.js", users)

    def test_the_resolver_still_offers_the_documented_locations(self):
        text = (TOOLS / "pw.js").read_text(encoding="utf-8")
        self.assertIn("'playwright'", text, "普通 node_modules 必须排在第一位")
        self.assertIn("node_modules", text)
        # A resolver that fails with MODULE_NOT_FOUND teaches the reader nothing.
        self.assertIn("npx playwright install chromium", text)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(WORKFLOW.is_file(), f"缺少 {WORKFLOW}")
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def test_the_workflow_is_published_so_it_can_ever_run(self):
        """Not runnable from the repo it lives in is the same as not existing."""
        import sys
        sys.path.insert(0, str(ROOT))
        from tools import publish_export as export

        self.assertIn(".github", export.INCLUDE_DIRS)
        selected = {str(path) for path in export.iter_files()}
        self.assertIn(".github/workflows/ci.yml", selected)

    def test_it_runs_the_suite_script_instead_of_repeating_the_suite_list(self):
        """Two lists of 19 names drift; the second one drifts silently.

        这个下限 2026-09-24 由 **20 降到 19**：布告栏下线，`bulletin_check` 连同功能一起
        从运行器里去掉（运营者决定收下朋友那一版改版）。**棘轮降低是人工动作**，
        所以原因写在这里 —— 它是防「有人悄悄少跑一套」的，不是防「功能下线」的。
        """
        self.assertIn("tools/run_browser_checks.sh", self.text)
        runner = (TOOLS / "run_browser_checks.sh").read_text(encoding="utf-8")
        suites = re.findall(r"^\s{2}([a-z_]+_check)$", runner, re.MULTILINE)
        self.assertGreaterEqual(len(suites), 19)
        for name in suites:
            self.assertNotIn(
                f"{name}.js", self.text,
                f"工作流里又抄了一遍套件清单（{name}）——应该只调用 run_browser_checks.sh",
            )

    def test_the_python_matrix_covers_the_two_pythons_this_project_runs_on(self):
        server = re.search(r'python:\s*\[([^\]]*)\]', self.text)
        self.assertIsNotNone(server, "工作流里没有 python 矩阵")
        versions = {part.strip().strip('"\'') for part in server.group(1).split(",")}
        self.assertIn("3.14", versions, "生产是 3.14")
        self.assertIn("3.9", versions, "开发机是 3.9，而它只靠 future annotations 撑着")

    def test_ci_cannot_touch_production(self):
        """A pipeline that needs a secret is not one a contributor can run.

        Deploying is an operator action with a backup and a rollback attached;
        it has no business happening because someone opened a pull request.
        """
        for forbidden in ("secrets.", "ssh ", "scp ", "appleboy", "deploy_pilot.sh"):
            self.assertNotIn(forbidden, self.text, f"工作流里出现了 {forbidden!r}")
        self.assertIn("permissions:", self.text)
        self.assertIn("contents: read", self.text)

    def _jobs(self) -> dict[str, str]:
        """The workflow's jobs, keyed by name, split into their own blocks.

        No YAML parser on purpose: the tests have to run with the same four
        packages the product installs, and `yaml` is not one of them. Job keys
        are the only two-space keys **after** the `jobs:` line -- the keys under
        `on:`, `permissions:` and `concurrency:` are above it, and everything
        inside a job is indented further.
        """
        body = self.text.split("\njobs:\n", 1)[1]
        parts = re.split(r"^  ([a-z][a-z0-9_-]*):\s*$", body, flags=re.MULTILINE)
        return dict(zip(parts[1::2], parts[2::2]))

    def test_the_job_split_actually_finds_the_jobs(self):
        """Both tests below read this split; an empty split would silence them."""
        jobs = self._jobs()
        self.assertTrue({"unit", "browser", "release", "webkit"} <= set(jobs),
                        f"作业清单与预期不符（切分坏了？）：{sorted(jobs)}")
        self.assertTrue(all(jobs.values()), "有作业切出来是空的")

    def test_it_installs_only_the_browser_the_suites_use(self):
        """All 19 suites drive chromium; pulling three browsers triples the
        slowest step of the job for nothing.

        This assertion used to look at the **whole file** ("no `install
        --with-deps webkit` anywhere"). That stopped being true the moment a
        *separate* WebKit job was added, and the lazy way to make it pass again
        would have been to delete it -- so it is written per-job instead. It now
        pins what it always meant: **the 19-suite job** installs chromium and
        nothing else, and it does not ask for another engine.
        """
        browser = self._jobs()["browser"]
        self.assertIn("install --with-deps chromium", browser)
        for unused in ("firefox", "webkit"):
            self.assertNotIn(unused, browser, f"默认那条作业不该装 {unused}")
        # 默认引擎仍是 chromium：运行器不带 PILOT_BROWSER 时选的就是它。
        self.assertNotIn("PILOT_BROWSER", browser)

    def test_the_webkit_job_is_separate_and_really_runs_webkit(self):
        """A job that *installs* WebKit but runs the suites without asking for
        it would be worse than no job at all: it would say Safari was covered.

        The suite list is pinned rather than merely "a subset of the runner's
        list" for the same reason. These four were run one by one in real WebKit
        on 2026-09-21 and each one came back green (61 / 21 / 213 / 24
        assertions). ``tasks_check`` joined on 2026-09-22: that day a **WebKit-only**
        bug turned up (a native `<select>` ignores `min-height`/`padding`, so the
        priority picker was 21px on an iPhone) and this is the only suite that
        catches it. It was run green in real WebKit twice before being added --
        macOS (this machine) and Linux (the second dev machine) -- with its two
        clipboard assertions printing `skip` in both, and those two are run for
        real by the chromium `browser` job. Changing the list -- adding *or*
        removing -- means going back to a machine with WebKit and getting
        evidence first; quietly dropping one is how a red suite stops being a
        finding.
        """
        jobs = self._jobs()
        self.assertIn("webkit", jobs)
        job = jobs["webkit"]
        self.assertIn("WebKit", job, "作业名要一眼看出是 WebKit 那条")
        self.assertIn("install --with-deps webkit", job)
        self.assertIn("PILOT_BROWSER=webkit", job)
        self.assertIn("tools/run_browser_checks.sh", job)
        named = set(re.findall(r"\b([a-z_]+_check)\b", job))
        self.assertEqual(
            named,
            {"shell_check", "appearance_check", "admin_edit_check", "background_photo_check",
             "tasks_check"},
            "WebKit 作业点名的套件与真机取证过的那一批不一致；"
            "要改名单，先在真 WebKit 里把那一套跑出结论。",
        )
        runner = (TOOLS / "run_browser_checks.sh").read_text(encoding="utf-8")
        known = set(re.findall(r"^\s{2}([a-z_]+_check)$", runner, re.MULTILINE))
        self.assertLessEqual(named, known, f"运行器里没有这些套件：{sorted(named - known)}")

    def test_an_npm_install_inside_tools_cannot_be_published(self):
        """CI teaches people to run `npm install`, and npm installs where you
        run it. Without this exclusion, one `npm install` typed in tools/ would
        put tens of thousands of foreign files into the public tree."""
        import sys
        sys.path.insert(0, str(ROOT))
        from tools import publish_export as export

        self.assertIn("node_modules", export.EXCLUDE_NAMES)


class BuildScriptPortabilityTests(unittest.TestCase):
    """The first CI run failed here, and it was the same bug as `/tmp/pw`.

    `build_release.sh` read the version with ``"$ROOT_DIR/.venv-pilot/bin/python"``
    and wrote the checksum with ``shasum``. Both are facts about one laptop:
    on any other machine the build died on line 5, and on Linux `shasum` is a
    Perl script that may simply not be installed. A release script nobody else
    can run is the thing CI exists to notice.
    """

    def setUp(self):
        self.text = (ROOT / "pilot_app" / "build_release.sh").read_text(encoding="utf-8")

    def test_it_looks_for_an_interpreter_instead_of_naming_one(self):
        self.assertIn("find_python", self.text)
        self.assertIn("python3", self.text, "必须退到 python3，否则只有开发机能打包")
        # The developer's venv may be *tried*, but not relied on.
        self.assertIn('"${PYTHON:-}"', self.text)

    def test_the_checksum_tool_falls_back_to_coreutils(self):
        self.assertIn("sha256sum", self.text, "Linux 上要用 sha256sum")
        self.assertIn("shasum -a 256", self.text, "macOS 上要用 shasum")


class DeployScriptTests(unittest.TestCase):
    """`tools/deploy_prod.sh` is the one command that ships to production.

    It exists because the mistakes on this path are not in `tar` -- they are in
    the *order and the acceptance*: back up before swapping code, retry `/health`
    (the first request after a restart can be 502), look the six units up by
    their real names, compare static files by **bytes** rather than "the page
    loads". Each of those was learned the hard way and each of them is one
    deleted line away from coming back, which is what this class is for.

    Assertions are on the script's text on purpose: this is a guard against a
    future edit, and the behaviour itself is verified by running it (the round
    that added it deployed with it).
    """

    def setUp(self):
        self.text = (TOOLS / "deploy_prod.sh").read_text(encoding="utf-8")

    def test_it_checks_all_six_units_under_their_real_names(self):
        for unit in ("cityu-mail-pilot-web", "cityu-mail-pilot-worker", "nginx",
                     "certbot.timer", "cityu-mail-pilot-backup.timer",
                     "cityu-mail-pilot-backup-request.path"):
            self.assertIn(unit, self.text, f"{unit} 没查；少查一个就等于没查")

    def test_active_is_decided_by_the_whole_field(self):
        """`is-active` answers "inactive", which contains "active"."""
        self.assertIn('"${line##* }" == "active"', self.text)
        self.assertNotIn('== *" active"', self.text)

    def test_it_does_not_strip_the_release_directory(self):
        """The tarball's top level *is* `pilot_app/`.

        Extracting with `--strip-components=1` turns `pilot_app/deploy_pilot.sh`
        into `deploy_pilot.sh` and the install step cannot find the installer.

        The check is on the **command lines, not the text**: the script's own
        comments explain why the flag is absent, and a substring assertion would
        fire on that explanation. (This test said the script was broken the first
        time it ran, for exactly that reason -- the same "assert a substring, not
        the line" mistake this suite has made before.)
        """
        commands = [line.strip() for line in self.text.splitlines()
                    if line.strip().startswith("tar ")]
        self.assertTrue(commands, "一条 tar 命令都没有，说明这个断言找错了地方")
        for command in commands:
            self.assertNotIn("strip-components", command)

    def test_the_unit_tests_are_judged_by_exit_code(self):
        """`python -m unittest ... | tail -3` exits with *tail's* status.

        A run that dies on an import error prints three lines that look like
        ordinary output and the pipeline is green. The script keeps the exit
        code, and this asserts it does not go back to reading the last line.
        """
        self.assertIn("TEST_CODE", self.text)
        for line in self.text.splitlines():
            if "unittest" in line:
                self.assertNotIn("tail", line, "单测的判据又变成看最后一行了")

    def test_it_waits_for_health_instead_of_asking_once(self):
        self.assertIn("attempt -ge", self.text)
        self.assertIn("sleep 2", self.text)

    def test_the_anonymous_boundary_is_401_and_says_so(self):
        """Not 404: 404 is the answer for a *logged-in* non-admin.

        Conflating them is how a script ends up certifying a boundary it never
        actually touched.
        """
        self.assertIn('"401"', self.text)
        self.assertIn("404", self.text)

    def test_the_host_and_key_come_from_the_environment(self):
        self.assertIn("PILOT_HOST", self.text)
        self.assertIn("PILOT_SSH_KEY", self.text)

    def test_dry_run_stops_before_it_can_reach_production(self):
        """A dry run that uploads is worse than no dry run."""
        dry = self.text.index("dry-run：到此为止")
        self.assertLess(dry, self.text.index("scp -i"))
        self.assertLess(dry, self.text.index("bash -s --"))


def _needs_files_the_package_does_not_ship(path: pathlib.Path) -> bool:
    """Does this test module depend on the repository rather than the product?

    Derived from the source instead of a hand-written list, because a
    hand-written list is exactly what goes stale: the next test file to import
    `tools/` would silently join the release package and break it.
    """
    text = path.read_text(encoding="utf-8")
    if ".github" in text:
        return True
    tree = ast.parse(text)
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        if any(name == "tools" or name.startswith("tools.") for name in names):
            return True
    # The other shape: two modules load their tool by path,
    # `spec_from_file_location("x", ROOT / "tools" / "x.py")`, so there is no
    # import statement to find. Requiring both halves matters: `"tools"` alone is
    # an ordinary payload key in test_providers (the model API takes a list of
    # tools), and treating that as a dependency on tools/ would drag a perfectly
    # package-runnable test out of the package.
    if "spec_from_file_location" in text and any(
        isinstance(node, ast.Constant) and node.value == "tools" for node in ast.walk(tree)
    ):
        return True
    # The third shape, and the one that actually broke CI: the *screenshot
    # generators* are read through a plain path join --
    # `ROOT / "tools" / "forward_shots.js"`, `...parents[2] / "tools"` -- so there
    # is no import and no `spec_from_file_location` either. Three such modules
    # shipped inside the release package and failed there (2026-09-18, the
    # 「发布包能装也能跑」 job red for several pushes) while this derivation said
    # the list was complete.
    #
    # The test is narrow on purpose: a `/` whose **right operand is the literal
    # "tools"**. That is a path being built. Merely containing the word (the
    # payload key in test_providers) is not.
    # The fourth shape, found by CI on 2026-09-23: a test that reads a
    # repository-level document through the same kind of path join --
    # `ROOT / "docs" / "dependency-audit-2026-09-23.md"`. `docs/` is not in the
    # release package either, so such a test ships and fails there. Same narrow
    # rule: a `/` whose right operand is the literal "docs" is a path being
    # built, not a payload key.
    return any(
        isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
        and isinstance(node.right, ast.Constant) and isinstance(node.right.value, str)
        and node.right.value.split("/", 1)[0] in ("tools", "docs")
        for node in ast.walk(tree)
    )


class ReleasePackageTests(unittest.TestCase):
    """The runtime package must not ship a test suite that cannot pass in it.

    The first CI run of the release job unpacked the tarball and ran the suite
    inside it: six modules failed to import, because they test `tools/` and the
    package ships `pilot_app/` only. A package whose own tests fail on unpack is
    worse than one with no tests -- it teaches the reader to ignore red.
    """

    def setUp(self):
        self.script = (ROOT / "pilot_app" / "build_release.sh").read_text(encoding="utf-8")

    def _excluded(self) -> set[str]:
        head = self.script.split("REPO_ONLY_TESTS=(", 1)[1].split(")", 1)[0]
        # `[a-z0-9_]`，不是 `[a-z_]`：少了数字这一档，`test_i18n_proofread.py`
        # 会**整条匹配不上**（"test_i" 后面接的是 "18"），于是名单里明明写了它，
        # 这里读出来却像没写 —— 2026-09-23 加那个测试时就是这么红的。
        return set(re.findall(r"(test_[a-z0-9_]+\.py)", head))

    def _repo_only(self) -> set[str]:
        return {path.name for path in (ROOT / "pilot_app" / "tests").glob("test_*.py")
                if _needs_files_the_package_does_not_ship(path)}

    def test_every_repo_only_test_is_kept_out_of_the_package(self):
        repo_only = self._repo_only()
        self.assertTrue(repo_only, "一个都认不出来，说明这个判定坏了")
        missing = sorted(repo_only - self._excluded())
        self.assertEqual(missing, [], f"这些测试要仓库级文件，却没被排除出发布包：{missing}")

    def test_combined_repository_paths_are_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'sample.py'
            for folder in ('tools', 'docs'):
                path.write_text(f'file = ROOT / "{folder}/sample.py"\n', encoding='utf-8')
                self.assertTrue(_needs_files_the_package_does_not_ship(path))
            path.write_text('payload = {"tools": []}\nfile = ROOT / "pilot_app/sample.py"\n', encoding='utf-8')
            self.assertFalse(_needs_files_the_package_does_not_ship(path))

    def test_the_exclusion_list_has_no_stale_entries(self):
        """An entry naming a file that no longer needs excluding is a comment
        pretending to be a rule.

        Only files that are actually here count. `test_handoff.py` is not
        published -- it tests a tool that is not published either -- so it is
        present in the development tree and absent from the public one, and this
        test runs in both. Comparing against the directory listing alone made the
        list look stale in exactly one of the two, which is how the first version
        of this check failed CI on the public tree.
        """
        tests = ROOT / "pilot_app" / "tests"
        repo_only = self._repo_only()
        stale = sorted(name for name in self._excluded()
                       if (tests / name).exists() and name not in repo_only)
        self.assertEqual(stale, [], f"这些文件已经不需要排除了：{stale}")

    def test_the_exclusions_actually_reach_tar(self):
        self.assertIn('"${REPO_ONLY_EXCLUDES[@]}"', self.script)


def test_clients_without_proxy_handler(paths=None) -> list:
    """测试模块里**没带 `ProxyHandler`** 的 `build_opener(...)` 调用点。

    2026-09-21 真踩：macOS 的**系统代理开着但没在服务**（HTTP/HTTPS/SOCKS 都指 127.0.0.1，
    而本机那个代理软件关着）时，`urllib` 连**本机测试服务器**也会走代理 —— 表现是 5 条测试
    报 `502 != 200`，而且 `handoff.py write` **一卡半小时**（每个请求都等代理超时）。
    **产品没问题，是工装**：测试打的是 127.0.0.1，本来就该显式绕开代理。

    **按调用点判，不按行判**（2026-09-23 改）。原来是一行一行看
    `"build_opener(" in line and "ProxyHandler" not in line`，于是把调用**折成两行**写的
    客户端报成违规——`test_i18n_pages.py` 就是这么被误报的（它确实带了
    `ProxyHandler({})`，只是写在下一行）。**假红比不报更贵**：它会让人去"修"一个没坏的东西，
    或者干脆把这条判据调松。所以这里解析 AST、取**整个调用**的源码来看。
    """
    import ast

    hits = []
    for path in sorted(paths if paths is not None
                       else (ROOT / "pilot_app" / "tests").glob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover - 语法都不对的话别的测试会先红
            hits.append(f"{path.name}:0")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name != "build_opener":
                continue
            if "ProxyHandler" in ast.get_source_segment(source, node):
                continue
            hits.append(f"{path.name}:{node.lineno}")
    return hits


class TestClientProxyTests(unittest.TestCase):
    def test_every_test_client_bypasses_proxies(self):
        self.assertEqual(
            test_clients_without_proxy_handler(), [],
            "这些测试客户端的 build_opener 没带 ProxyHandler({})：本机代理开着但不服务时，"
            "连 127.0.0.1 也会走代理（502、一卡半小时）。写法："
            "`build_opener(urllib.request.ProxyHandler({}), …)`",
        )

    def test_the_scanner_actually_catches_the_broken_shape(self):
        """反向验证：坏样本必须报出来（否则这条判据只是「永远绿」）。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            good = pathlib.Path(tmp) / "test_ok.py"
            good.write_text("import urllib.request\n"
                            "op = urllib.request.build_opener(urllib.request.ProxyHandler({}))\n",
                            encoding="utf-8")
            bad = pathlib.Path(tmp) / "test_bad.py"
            # 坏样本**拆开拼**：不然判据会抓到自己这个 fixture（第一版就是这样红的）。
            broken = "op = urllib.request." + "build_opener" + "(urllib.request.HTTPCookieProcessor(None))\n"
            bad.write_text("import urllib.request\n" + broken, encoding="utf-8")
            self.assertEqual(test_clients_without_proxy_handler([good]), [])
            self.assertEqual(len(test_clients_without_proxy_handler([bad])), 1)

    def test_a_call_wrapped_across_lines_is_not_a_hit(self):
        """**折行的调用不该被误报**（2026-09-23 的真实假红）。

        `test_i18n_pages.py` 的客户端把 `build_opener(` 与 `ProxyHandler({})` 写在两行上，
        而旧扫描器逐行看，于是把**已经写对**的它报成违规。假红的代价不小：
        人会去修一个没坏的东西，或者把判据调松到不再有用。
        """
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            wrapped = pathlib.Path(tmp) / "test_wrapped.py"
            wrapped.write_text(
                "import urllib.request\n"
                "op = urllib.request.build_opener(\n"
                "    urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(None))\n",
                encoding="utf-8")
            self.assertEqual(test_clients_without_proxy_handler([wrapped]), [],
                             "折行但确实带了 ProxyHandler —— 不该报")

    def test_a_call_wrapped_across_lines_without_the_handler_still_reports(self):
        """反向：折行的**坏**样本仍然要被抓到（别把这条判据改成"折行就不看"）。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            broken = pathlib.Path(tmp) / "test_wrapped_bad.py"
            broken.write_text(
                "import urllib.request\n"
                "op = urllib.request.build_opener(\n"
                "    urllib.request.HTTPCookieProcessor(None))\n",
                encoding="utf-8")
            self.assertEqual(len(test_clients_without_proxy_handler([broken])), 1)


if __name__ == "__main__":
    unittest.main()
