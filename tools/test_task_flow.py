"""Isolated tests; no dorm, production, model calls or external dependencies."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from tools import task_flow as flow


class TaskFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        for command in (["git", "init", "-q"], ["git", "config", "user.name", "Test"],
                        ["git", "config", "user.email", "test@example.invalid"]):
            subprocess.run(command, cwd=self.root, check=True, capture_output=True)
        (self.root / "code.py").write_text("value = 1\n")
        subprocess.run(["git", "add", "code.py"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=self.root, check=True)
        self.spec = {"workspace": str(self.root), "head": flow.run(["git", "rev-parse", "HEAD"], self.root),
                     "goal": "Fix fixture", "scope": ["code.py"], "checks": ["unit tests"],
                     "forbidden": ["production"]}

    def preflight(self):
        with patch.object(Path, "cwd", return_value=self.root):
            return flow.preflight(self.spec, self.root)

    def test_success_and_probe_cleanup(self):
        before = set(self.root.iterdir())
        self.assertEqual(self.preflight()["status"], "preflight_passed")
        self.assertEqual(before, set(self.root.iterdir()))

    def test_wrong_cwd(self):
        with self.assertRaises(ValueError):
            flow.preflight(self.spec, self.root)

    def test_wrong_head(self):
        self.spec["head"] = "0" * 40
        with self.assertRaises(ValueError):
            self.preflight()

    def test_dirty_tracked(self):
        (self.root / "code.py").write_text("changed")
        with self.assertRaises(ValueError):
            self.preflight()

    def test_hash_drift(self):
        self.spec["inputs"] = {"code.py": "0" * 64}
        with self.assertRaises(ValueError):
            self.preflight()

    def test_scope_escape_and_symlink(self):
        with self.assertRaises(ValueError):
            flow.inside(self.root, "../elsewhere")
        (self.root / "link").symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            flow.inside(self.root, "link/elsewhere")

    def test_write_denied(self):
        with patch.object(tempfile, "TemporaryFile", side_effect=PermissionError("denied")):
            with self.assertRaises(PermissionError):
                self.preflight()

    def test_existing_directory_is_probed(self):
        target = self.root / "target"
        target.mkdir()
        self.spec["scope"] = ["target"]
        with patch.object(tempfile, "TemporaryFile", wraps=tempfile.TemporaryFile) as probe:
            self.preflight()
        self.assertIn(str(target), [call.kwargs["dir"] for call in probe.call_args_list])

    def test_relative_workspace_and_invalid_paths(self):
        path = self.root / "spec.json"
        for change in ({"workspace": "."}, {"scope": [""]},
                       {"scope": [str(self.root / "code.py")]},
                       {"inputs": {"../file": "0" * 64}}):
            path.write_text(json.dumps(dict(self.spec, **change)))
            with self.assertRaises(ValueError):
                flow.load(path)

    def test_brief_includes_input_hashes(self):
        self.spec["inputs"] = {"code.py": "1" * 64}
        self.assertIn("code.py: " + "1" * 64, flow.brief(self.spec))

    def test_summary_and_zero_tests(self):
        log = self.root / "log"
        for text, count, skipped, ok in (("Ran 2 tests in 0.1s\n\nOK (skipped=1)\n", 2, 1, True),
                                        ("Ran 1 test in 0.1s\n\nFAILED (errors=1)\n", 1, 0, False),
                                        ("unfinished", None, None, False)):
            log.write_text(text)
            self.assertEqual(flow.test_summary(log), {"tests": count, "skipped": skipped, "summary_ok": ok})
        (self.root / "pilot_app/tests").mkdir(parents=True)
        with contextlib.redirect_stdout(io.StringIO()):
            # Python 3.14 unittest itself returns 5; 3.9 needs our explicit guard.
            self.assertIn(flow.tests(self.root, "full", [], ".empty"), (3, 5))

    def test_workspace_root_probe_never_escapes(self):
        self.spec["scope"] = ["."]
        with patch.object(tempfile, "TemporaryFile", wraps=tempfile.TemporaryFile) as probe:
            self.preflight()
        self.assertEqual({call.kwargs["dir"] for call in probe.call_args_list}, {str(self.root)})

    def test_all_skipped_is_not_green(self):
        folder = self.root / "pilot_app/tests"
        folder.mkdir(parents=True)
        (folder / "test_skipped.py").write_text("import unittest\n@unittest.skip('missing corpus')\nclass T(unittest.TestCase):\n def test_it(self): pass\n")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(flow.tests(self.root, "full", [], ".skipped"), 3)
        receipt = json.loads(next((self.root / ".skipped").glob("*/receipt.json")).read_text())
        self.assertEqual(receipt["exit_code"], 0)
        self.assertEqual(receipt["runner_exit_code"], 3)

    def test_not_git_does_not_create_output(self):
        folder = self.root / "subdir"
        folder.mkdir()
        with self.assertRaises(ValueError):
            flow.tests(folder, "full", [], ".results")
        self.assertFalse((folder / ".results").exists())

    def test_brief_limit(self):
        self.assertIn("单一写者", flow.brief(self.spec))
        self.spec["goal"] = "a" * 6001
        with self.assertRaises(ValueError):
            flow.brief(self.spec)

    def test_malformed_spec(self):
        path = self.root / "spec.json"
        for value in ([], {}, dict(self.spec, inputs=[]), dict(self.spec, head=42)):
            path.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                flow.load(path)

    def test_layer_filters(self):
        for layer, modules in (("targeted", []), ("targeted", ["--help"]), ("full", ["a.b"])):
            with self.assertRaises(ValueError):
                flow.tests(self.root, layer, modules, ".results")

    def test_real_failure_and_success_receipts(self):
        folder = self.root / "pilot_app/tests"
        folder.mkdir(parents=True)
        sample = folder / "test_sample.py"
        for passing in (False, True):
            sample.write_text("import unittest\nclass T(unittest.TestCase):\n def test_it(self): self.assertTrue(%s)\n" % passing)
            with contextlib.redirect_stdout(io.StringIO()):
                code = flow.tests(self.root, "full", [], ".results")
            self.assertEqual(code, 0 if passing else 1)
        receipts = list((self.root / ".results").glob("*/receipt.json"))
        self.assertEqual(len(receipts), 2)
        self.assertEqual({json.loads(p.read_text())["exit_code"] for p in receipts}, {0, 1})
        self.assertTrue(all(not json.loads(p.read_text())["acceptance"] for p in receipts))


if __name__ == "__main__":
    unittest.main()
