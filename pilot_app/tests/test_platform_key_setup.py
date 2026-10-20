"""Tests for installing and verifying the instance-wide fallback model key.

Two things can go wrong here and neither is visible in the product:

* **The key leaks.** `check-model` prints provider errors, and a provider error can
  quote the request back. A verifier that leaks the credential it verifies is
  worse than no verifier, so "the key never appears in stdout" is asserted
  directly -- including for the failure path.
* **The edit corrupts the file.** `pilot.env` also holds the master key. The
  installer rewrites it by hand, and the obvious `sed -i` breaks on a key
  containing `/`, `&` or `\\`. One of these tests uses a key with all three.

handoff-security-scan: fixtures
Every `sk-`-shaped string below is invented here and never used anywhere: they are
the *input* to a script whose job is to store them verbatim, and to a checker whose
job is to never print them. Testing those two properties requires strings that look
like credentials. The marker above skips this one file in `handoff.py snapshot`, and
the packer prints that it did.
"""

import io
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import contextlib
import tempfile
import unittest
from unittest import mock

from pilot_app import manage, providers

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "pilot_app" / "set_platform_key.sh"

BASE_ENV = """INFE_PILOT_DB=/var/lib/cityu-mail-pilot/pilot.sqlite3
INFE_PILOT_MASTER_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=
INFE_PILOT_ADMIN_EMAILS=boss@example.com
"""


class SetPlatformKeyScriptTests(unittest.TestCase):
    """The installer, exercised as a program (that is how an operator runs it)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.env_file = pathlib.Path(self.dir) / "pilot.env"
        self.env_file.write_text(BASE_ENV, encoding="utf-8")
        self.env_file.chmod(0o600)
        self.backups = pathlib.Path(self.dir) / "backups"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_script(self, *args, stdin=""):
        environment = dict(os.environ)
        environment["INFE_PILOT_PREDEPLOY_DIR"] = str(self.backups)
        # Bytes in, bytes out, decoded here with `replace`. `text=True` decodes with
        # the parent's locale, which in this environment is C -- and that turned a
        # genuine bug into a confusing UnicodeDecodeError instead of the assertion
        # that pins it. The LC_CTYPE=C run is the point: bash swallows a byte of the
        # following CJK character into the variable name, so `$VAR（` needs braces.
        environment["LC_CTYPE"] = "C"
        done = subprocess.run(
            ["bash", str(SCRIPT), "--env-file", str(self.env_file),
             "--no-restart", "--no-verify", *args],
            input=stdin.encode("utf-8"), capture_output=True, env=environment, timeout=60,
        )
        return subprocess.CompletedProcess(
            done.args, done.returncode,
            done.stdout.decode("utf-8", "replace"), done.stderr.decode("utf-8", "replace"))

    def lines(self):
        return [line for line in self.env_file.read_text(encoding="utf-8").splitlines() if line]

    def value(self, name):
        for line in self.lines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1]
        return None

    def test_a_key_is_written_and_the_rest_of_the_file_survives(self):
        result = self.run_script("--stdin", stdin="sk-pilot-example-not-a-real-key\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.value("INFE_PILOT_DEFAULT_MODEL_KEY"),
                         "sk-pilot-example-not-a-real-key")
        self.assertEqual(self.value("INFE_PILOT_MASTER_KEY"),
                         "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
        self.assertEqual(self.value("INFE_PILOT_ADMIN_EMAILS"), "boss@example.com")

    def test_shell_metacharacters_in_the_key_survive_verbatim(self):
        """`sed -i` would eat these; the script must not use sed for this."""
        tricky = "sk-a/b&c\\d=e+f"
        result = self.run_script("--stdin", stdin=tricky + "\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.value("INFE_PILOT_DEFAULT_MODEL_KEY"), tricky)

    def test_every_line_in_the_file_is_still_a_plain_assignment(self):
        self.run_script("--stdin", stdin="sk-a/b&c\\d\n")
        for line in self.lines():
            self.assertRegex(line, r"^[A-Z_]+=.*$", line)

    def test_the_file_stays_private(self):
        self.run_script("--stdin", stdin="sk-pilot-example-not-a-real-key\n")
        mode = stat.S_IMODE(self.env_file.stat().st_mode)
        self.assertEqual(mode, 0o600, oct(mode))

    def test_running_twice_leaves_exactly_one_key_line(self):
        self.run_script("--stdin", stdin="sk-first-value\n")
        self.run_script("--stdin", stdin="sk-second-value\n")
        self.assertEqual(sum(1 for line in self.lines()
                             if line.startswith("INFE_PILOT_DEFAULT_MODEL_KEY=")), 1)
        self.assertEqual(self.value("INFE_PILOT_DEFAULT_MODEL_KEY"), "sk-second-value")

    def test_a_non_deepseek_provider_without_a_model_is_refused(self):
        result = self.run_script("--stdin", "--provider", "volcengine_ark",
                                 stdin="sk-value\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--model", result.stderr)
        self.assertIsNone(self.value("INFE_PILOT_DEFAULT_MODEL_KEY"))

    def test_the_provider_details_are_written_alongside_the_key(self):
        result = self.run_script("--stdin", "--provider", "volcengine_ark",
                                 "--model", "doubao-1-5-pro-32k",
                                 "--base-url", "https://ark.example/api/v3",
                                 stdin="sk-value\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.value("INFE_PILOT_DEFAULT_MODEL_PROVIDER"), "volcengine_ark")
        self.assertEqual(self.value("INFE_PILOT_DEFAULT_MODEL_NAME"), "doubao-1-5-pro-32k")
        self.assertEqual(self.value("INFE_PILOT_DEFAULT_MODEL_BASE_URL"),
                         "https://ark.example/api/v3")

    def test_an_empty_key_changes_nothing(self):
        result = self.run_script("--stdin", stdin="\n")
        self.assertEqual(result.returncode, 1)
        self.assertIsNone(self.value("INFE_PILOT_DEFAULT_MODEL_KEY"))

    def test_a_key_with_whitespace_is_refused_rather_than_trimmed_silently(self):
        """A pasted key often carries a stray space; writing it would fail later."""
        result = self.run_script("--stdin", stdin="sk-value with space\n")
        self.assertEqual(result.returncode, 1)
        self.assertIsNone(self.value("INFE_PILOT_DEFAULT_MODEL_KEY"))

    def test_remove_deletes_all_four_lines(self):
        self.run_script("--stdin", "--provider", "volcengine_ark", "--model", "m",
                        "--base-url", "https://example/api", stdin="sk-value\n")
        result = self.run_script("--remove")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([line for line in self.lines() if "DEFAULT_MODEL" in line], [])
        self.assertEqual(self.value("INFE_PILOT_MASTER_KEY"),
                         "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")

    def test_the_original_file_is_backed_up_before_the_rewrite(self):
        self.run_script("--stdin", stdin="sk-value\n")
        copies = list(self.backups.glob("pilot-*.env"))
        self.assertEqual(len(copies), 1)
        self.assertEqual(copies[0].read_text(encoding="utf-8"), BASE_ENV)

    def test_the_key_is_not_echoed(self):
        secret = "sk-secret-value-1234567890"
        result = self.run_script("--stdin", stdin=secret + "\n")
        self.assertNotIn(secret, result.stdout)
        self.assertNotIn(secret, result.stderr)
        # The length is computed, not written down: a hard-coded number here was
        # wrong the first time and said nothing about the behaviour.
        self.assertIn(f"长度 {len(secret)}", result.stdout)

    def test_an_unwritable_file_is_reported_as_a_permission_problem(self):
        self.env_file.chmod(0o400)
        if os.geteuid() == 0:  # root can write anything; the check cannot be exercised
            self.skipTest("以 root 运行，写权限检查无法触发")
        result = self.run_script("--stdin", stdin="sk-value\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("没有写权限", result.stderr)


class SetPlatformSearchKeyTests(unittest.TestCase):
    """`--search` writes the other pair of variables, and only those."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.env_file = pathlib.Path(self.dir) / "pilot.env"
        self.env_file.write_text(BASE_ENV, encoding="utf-8")
        self.env_file.chmod(0o600)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_script(self, *args, stdin=""):
        environment = dict(os.environ)
        environment["INFE_PILOT_PREDEPLOY_DIR"] = str(pathlib.Path(self.dir) / "backups")
        environment["LC_CTYPE"] = "C"
        done = subprocess.run(
            ["bash", str(SCRIPT), "--env-file", str(self.env_file),
             "--no-restart", "--no-verify", *args],
            input=stdin.encode("utf-8"), capture_output=True, env=environment, timeout=60,
        )
        return subprocess.CompletedProcess(
            done.args, done.returncode,
            done.stdout.decode("utf-8", "replace"), done.stderr.decode("utf-8", "replace"))

    def text(self):
        return self.env_file.read_text(encoding="utf-8")

    def test_a_search_key_writes_the_search_variables_only(self):
        result = self.run_script("--search", "--stdin", stdin="sk-search-not-real\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("INFE_PILOT_DEFAULT_SEARCH_KEY=sk-search-not-real", self.text())
        self.assertNotIn("INFE_PILOT_DEFAULT_MODEL_KEY", self.text())
        # The load-bearing assertion. The first version of this branch built a
        # `grep -vE` pattern with an empty alternative (`(A|B||C)`), grep refused
        # to run, and the `|| true` after it turned that error into "no lines
        # matched" -- so the rewrite replaced the whole file, master key included,
        # with the single new line.
        self.assertIn("INFE_PILOT_MASTER_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
                      self.text())
        self.assertIn("INFE_PILOT_ADMIN_EMAILS=boss@example.com", self.text())

    def test_the_search_default_provider_is_left_to_the_code(self):
        """No provider line is written, so the documented default applies.

        Written down as a test because the alternative (writing doubao into the
        file) looks tidier and would silently change what an operator sees in
        their own env file.
        """
        self.run_script("--search", "--stdin", stdin="sk-search-not-real\n")
        self.assertNotIn("INFE_PILOT_DEFAULT_SEARCH_PROVIDER", self.text())

    def test_search_refuses_a_model_name(self):
        """A search vendor has no model parameter; accepting one would send it."""
        result = self.run_script("--search", "--model", "gpt-4o", "--stdin", stdin="sk-x\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--model", result.stderr)
        self.assertNotIn("INFE_PILOT_DEFAULT_SEARCH_KEY", self.text())

    def test_setting_search_does_not_touch_an_existing_model_key(self):
        self.run_script("--stdin", stdin="sk-model-not-real\n")
        self.run_script("--search", "--stdin", stdin="sk-search-not-real\n")
        self.assertIn("INFE_PILOT_DEFAULT_MODEL_KEY=sk-model-not-real", self.text())
        self.assertIn("INFE_PILOT_DEFAULT_SEARCH_KEY=sk-search-not-real", self.text())

    def test_removing_search_leaves_the_model_key_alone(self):
        self.run_script("--stdin", stdin="sk-model-not-real\n")
        self.run_script("--search", "--stdin", stdin="sk-search-not-real\n")
        result = self.run_script("--search", "--remove")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("INFE_PILOT_DEFAULT_SEARCH", self.text())
        self.assertIn("INFE_PILOT_DEFAULT_MODEL_KEY=sk-model-not-real", self.text())

    def test_the_search_key_is_not_echoed(self):
        secret = "sk-search-secret-abcdefghij"
        result = self.run_script("--search", "--stdin", stdin=secret + "\n")
        self.assertNotIn(secret, result.stdout)
        self.assertNotIn(secret, result.stderr)
        self.assertIn(f"长度 {len(secret)}", result.stdout)


class CheckSearchCommandTests(unittest.TestCase):
    """The only thing that ever exercises the pilot's search key."""

    def setUp(self):
        self.key = "sk-check-search-secret-1234567890"
        self._saved = os.environ.get("INFE_PILOT_DEFAULT_SEARCH_KEY")
        os.environ["INFE_PILOT_DEFAULT_SEARCH_KEY"] = self.key
        os.environ.pop("INFE_PILOT_DEFAULT_SEARCH_PROVIDER", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("INFE_PILOT_DEFAULT_SEARCH_KEY", None)
        else:
            os.environ["INFE_PILOT_DEFAULT_SEARCH_KEY"] = self._saved

    @contextlib.contextmanager
    def captured(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            yield buffer

    def test_without_a_key_it_says_so_and_fails(self):
        os.environ.pop("INFE_PILOT_DEFAULT_SEARCH_KEY", None)
        with self.captured() as out:
            code = manage.check_search()
        self.assertEqual(code, 1)
        self.assertIn("没有配置平台搜索 key", out.getvalue())
        self.assertIn("与事实不符", out.getvalue())

    def test_a_working_key_returns_zero_and_lists_results(self):
        hits = [{"title": "CityU", "url": "https://www.cityu.edu.hk/", "summary": ""}]
        with mock.patch.object(providers, "web_search", return_value=hits) as call:
            with self.captured() as out:
                code = manage.check_search()
        self.assertEqual(code, 0)
        self.assertIn("调用成功", out.getvalue())
        self.assertEqual(call.call_args.args[1], self.key)

    def test_the_query_is_a_fixed_diagnostic_not_user_mail(self):
        """A diagnostic must not be the one thing that leaks a subject line."""
        with mock.patch.object(providers, "web_search", return_value=[]) as call:
            with self.captured():
                manage.check_search()
        self.assertEqual(call.call_args.args[2], "City University of Hong Kong")

    def test_a_failing_key_returns_one(self):
        with mock.patch.object(providers, "web_search",
                               side_effect=providers.ProviderError("401 未授权")):
            with self.captured() as out:
                code = manage.check_search()
        self.assertEqual(code, 1)
        self.assertIn("不可用", out.getvalue())

    def test_the_key_never_reaches_stdout_even_when_the_error_quotes_it(self):
        with mock.patch.object(providers, "web_search",
                               side_effect=providers.ProviderError(f"Bearer {self.key} 被拒绝")):
            with self.captured() as out:
                manage.check_search()
        self.assertNotIn(self.key, out.getvalue())
        self.assertIn("已隐藏", out.getvalue())

    def test_results_that_quote_the_key_are_scrubbed_too(self):
        hits = [{"title": f"echo {self.key}", "url": "https://example.com/", "summary": ""}]
        with mock.patch.object(providers, "web_search", return_value=hits):
            with self.captured() as out:
                manage.check_search()
        self.assertNotIn(self.key, out.getvalue())

    def test_empty_results_still_report_success(self):
        """The call answered; an empty result set is a query problem, not a key one."""
        with mock.patch.object(providers, "web_search", return_value=[]):
            with self.captured() as out:
                code = manage.check_search()
        self.assertEqual(code, 0)
        self.assertIn("没返回结果", out.getvalue())


class CheckModelCommandTests(unittest.TestCase):
    """`manage.py check-model`: the only thing that ever exercises the pilot key."""

    def setUp(self):
        self.key = "sk-check-model-secret-1234567890"
        self._saved = os.environ.get("INFE_PILOT_DEFAULT_MODEL_KEY")
        os.environ["INFE_PILOT_DEFAULT_MODEL_KEY"] = self.key
        os.environ.pop("INFE_PILOT_DEFAULT_MODEL_PROVIDER", None)
        os.environ.pop("INFE_PILOT_DEFAULT_MODEL_NAME", None)

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("INFE_PILOT_DEFAULT_MODEL_KEY", None)
        else:
            os.environ["INFE_PILOT_DEFAULT_MODEL_KEY"] = self._saved

    @contextlib.contextmanager
    def captured(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            yield buffer

    def test_without_a_key_it_says_so_and_fails(self):
        os.environ.pop("INFE_PILOT_DEFAULT_MODEL_KEY", None)
        with self.captured() as out:
            code = manage.check_model()
        self.assertEqual(code, 1)
        self.assertIn("没有配置平台兜底 key", out.getvalue())
        self.assertIn("与事实不符", out.getvalue())

    def test_a_working_key_returns_zero_and_reports_the_reply(self):
        fake = providers.Generation(text="可用", sources=[], usage={"total_tokens": 7})
        with mock.patch.object(providers, "generate", return_value=fake) as call:
            with self.captured() as out:
                code = manage.check_model()
        self.assertEqual(code, 0)
        self.assertIn("调用成功", out.getvalue())
        self.assertIn("可用", out.getvalue())
        self.assertEqual(call.call_args.kwargs["api_key"], self.key)
        self.assertEqual(call.call_args.kwargs["model"], "deepseek-flash")

    def test_a_failing_key_returns_one_and_says_what_to_check(self):
        with mock.patch.object(providers, "generate",
                               side_effect=providers.ProviderError("401 未授权")):
            with self.captured() as out:
                code = manage.check_model()
        self.assertEqual(code, 1)
        self.assertIn("401", out.getvalue())
        self.assertIn("不可用", out.getvalue())

    def test_the_key_never_reaches_stdout_even_when_the_error_quotes_it(self):
        """Provider errors can quote the request back. That must not leak."""
        with mock.patch.object(providers, "generate",
                               side_effect=providers.ProviderError(
                                   f"请求被拒绝：{{\"Authorization\": \"Bearer {self.key}\"}}")):
            with self.captured() as out:
                manage.check_model()
        self.assertNotIn(self.key, out.getvalue())
        self.assertIn("已隐藏", out.getvalue())

    def test_the_success_path_does_not_print_the_key_either(self):
        # The provider echoes the request into the answer; only the reply is printed,
        # and even that goes through the scrubber.
        fake = providers.Generation(text=f"echo {self.key}", sources=[])
        with mock.patch.object(providers, "generate", return_value=fake):
            with self.captured() as out:
                manage.check_model()
        self.assertNotIn(self.key, out.getvalue())

    def test_only_the_length_of_the_key_is_reported(self):
        fake = providers.Generation(text="可用", sources=[])
        with mock.patch.object(providers, "generate", return_value=fake):
            with self.captured() as out:
                manage.check_model()
        self.assertIn(f"长度 {len(self.key)}", out.getvalue())

    def test_it_runs_without_a_database(self):
        """Dispatched before the DB is opened: it must work on a bare host."""
        # The key is removed from the child environment on purpose: `setUp` put a
        # fake one in this process, and leaving it there made the child attempt a
        # real HTTPS call to DeepSeek from inside a unit test.
        child_env = {**os.environ, "INFE_PILOT_DB": "/nonexistent-dir/pilot.sqlite3"}
        child_env.pop("INFE_PILOT_DEFAULT_MODEL_KEY", None)
        result = subprocess.run(
            [sys.executable, "-m", "pilot_app.manage",
             "check-model", "--prompt", "x"],
            capture_output=True, text=True, cwd=str(ROOT), timeout=120, env=child_env,
        )
        self.assertNotIn("PermissionError", result.stderr)
        self.assertNotIn("no such table", result.stderr)
        # It reports the missing key, not a storage problem.
        self.assertIn("没有配置平台兜底 key", result.stdout)

    def test_the_subparser_does_not_shadow_the_function(self):
        """A bare `check_model` parser variable made the dispatch call the parser."""
        with mock.patch.object(manage, "check_model", return_value=0) as called:
            with mock.patch("sys.argv", ["manage.py", "check-model"]):
                manage.main()
        self.assertTrue(called.called)


if __name__ == "__main__":
    unittest.main()
