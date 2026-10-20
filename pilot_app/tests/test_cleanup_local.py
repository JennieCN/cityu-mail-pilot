"""The cleanup tool's safety rule, tested without deleting anything.

Item 20 of the ledger is an irreversible delete, and the tool guarding it is
only worth having if the refusal path works. `decide()` is separated from
`main()` for exactly this: the interesting cases are "does it stop when the
key it was told to protect is in the way", and running that against a real
home directory to find out is not a test anybody should write.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools import cleanup_local  # noqa: E402
from pilot_app.security import key_fingerprint  # noqa: E402

LIVE_KEY = "B" * 43 + "="
OTHER_KEY = "C" * 43 + "="


class DecideTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def env_file(self, name: str, key: str) -> Path:
        path = self.root / name
        path.write_text(f"INFE_PILOT_MASTER_KEY={key}\nLOG_LEVEL=INFO\n", encoding="utf-8")
        return path

    def test_it_refuses_without_the_fingerprint(self):
        """The fingerprint has to come from the copy in the operator's hand.

        Requiring it is not ceremony: it is what makes "I have an offline copy"
        a claim with a value attached instead of a yes.
        """
        allowed, notes = cleanup_local.decide([self.root], "")
        self.assertFalse(allowed)
        self.assertTrue(any("PILOT_MASTER_KEY_FINGERPRINT" in line for line in notes))

    def test_it_refuses_when_the_protected_key_is_inside_the_target(self):
        """The one outcome that turns a tidy-up into a catastrophe."""
        self.env_file("pilot.env", LIVE_KEY)
        allowed, notes = cleanup_local.decide([self.root], key_fingerprint(LIVE_KEY))
        self.assertFalse(allowed)
        self.assertTrue(any("拒绝执行" in line for line in notes), notes)
        self.assertTrue(any("pilot.env" in line for line in notes), notes)

    def test_it_allows_a_different_key_and_says_which_one_it_saw(self):
        self.env_file("pilot.env", OTHER_KEY)
        allowed, notes = cleanup_local.decide([self.root], key_fingerprint(LIVE_KEY))
        self.assertTrue(allowed)
        self.assertTrue(any(key_fingerprint(OTHER_KEY) in line for line in notes), notes)

    def test_it_allows_a_target_with_no_key_at_all(self):
        (self.root / "notes.txt").write_text("nothing secret here", encoding="utf-8")
        allowed, notes = cleanup_local.decide([self.root], key_fingerprint(LIVE_KEY))
        self.assertTrue(allowed)
        self.assertTrue(any("没有任何主密钥" in line for line in notes), notes)

    def test_a_missing_target_is_reported_not_fatal(self):
        allowed, notes = cleanup_local.decide([self.root / "gone"], key_fingerprint(LIVE_KEY))
        self.assertTrue(allowed)
        self.assertTrue(any("本来就不存在" in line for line in notes), notes)

    def test_database_sidecars_are_not_parsed_as_config(self):
        """A stray key-looking string inside a binary must not be read as a key.

        Reading a 160 KB SQLite file as UTF-8 with errors ignored can produce
        anything, and a false match would block a legitimate cleanup forever.
        """
        (self.root / "pilot.sqlite3").write_bytes(
            b"INFE_PILOT_MASTER_KEY=" + LIVE_KEY.encode() + b"\x00\x01\x02")
        self.assertEqual(cleanup_local.keys_in([self.root]), [])

    def test_the_real_targets_are_the_three_the_ledger_names(self):
        names = {str(path) for path in cleanup_local.TARGETS}
        self.assertTrue(any(name.endswith("/.secrets") for name in names), names)
        self.assertTrue(any(name.endswith("pilot-local") for name in names), names)
        self.assertTrue(any(name.endswith("probe_gen.py") for name in names), names)


class DryRunTests(unittest.TestCase):
    """`main` must not delete unless it is explicitly told to."""

    def test_a_dry_run_deletes_nothing(self):
        with tempfile.TemporaryDirectory() as temporary:
            victim = Path(temporary) / "leftover"
            victim.mkdir()
            (victim / "pilot.env").write_text(f"INFE_PILOT_MASTER_KEY={OTHER_KEY}\n", encoding="utf-8")
            original = cleanup_local.TARGETS
            cleanup_local.TARGETS = (victim,)
            self.addCleanup(setattr, cleanup_local, "TARGETS", original)

            previous = {k: os.environ.get(k) for k in
                        ("PILOT_MASTER_KEY_FINGERPRINT", "PILOT_CLEANUP_CONFIRM")}
            self.addCleanup(lambda: [os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                                     for k, v in previous.items()])

            os.environ["PILOT_MASTER_KEY_FINGERPRINT"] = key_fingerprint(LIVE_KEY)
            os.environ.pop("PILOT_CLEANUP_CONFIRM", None)
            self.assertEqual(cleanup_local.main(), 0)
            self.assertTrue(victim.exists(), "预演居然删了东西")


if __name__ == "__main__":
    unittest.main()
