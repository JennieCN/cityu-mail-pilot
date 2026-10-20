# -*- coding: utf-8 -*-
"""Tests for the "is the key in my hand the right one?" helper.

Three things matter about that tool and nothing else: it prints the same
fingerprint the server prints for the same key, it says so loudly when the two
disagree (that is the answer an operator acts on), and it refuses to pretend
when it read nothing at all.
"""

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from pilot_app.security import key_fingerprint
from tools import check_master_key

TEST_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


def run(argv=None, stdin=""):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(check_master_key.sys, "stdin", io.StringIO(stdin)):
        with redirect_stdout(out), redirect_stderr(err):
            code = check_master_key.main(argv or [])
    return code, out.getvalue(), err.getvalue()


class CheckMasterKeyTests(unittest.TestCase):
    def test_it_prints_exactly_what_the_server_prints(self):
        """Same function, so the two values are comparable by construction."""
        code, out, _ = run(stdin=TEST_KEY + "\n")
        self.assertEqual(code, 0)
        self.assertIn(key_fingerprint(TEST_KEY), out)

    def test_a_matching_expectation_succeeds(self):
        code, out, _ = run(["--expect", key_fingerprint(TEST_KEY)], stdin=TEST_KEY + "\n")
        self.assertEqual(code, 0)
        self.assertIn("一致", out)

    def test_a_mismatch_fails_loudly(self):
        """"Different key" is the one answer that must not read like a success."""
        code, _, err = run(["--expect", "<主密钥指纹见运营者离线副本>"], stdin=TEST_KEY + "\n")
        self.assertEqual(code, 1)
        self.assertIn("不一致", err)

    def test_reading_nothing_is_refused_rather_than_guessed(self):
        code, _, err = run(stdin="\n")
        self.assertEqual(code, 2)
        self.assertIn("没有读到内容", err)

    def test_the_text_and_the_bytes_form_of_one_key_agree(self):
        """`pilot.env` holds base64 text, `SecretBox.key` holds 32 bytes.

        If those two produced different fingerprints the whole exercise would be
        useless -- the operator writes down one string and the server prints
        another. The key this file uses decodes to 32 zero bytes.
        """
        import base64

        raw = base64.urlsafe_b64decode(TEST_KEY)
        self.assertEqual(len(raw), 32)
        self.assertEqual(key_fingerprint(TEST_KEY), key_fingerprint(raw))
        code, out, _ = run(stdin=TEST_KEY + "\n")
        self.assertEqual(code, 0)
        self.assertIn(key_fingerprint(raw), out)


if __name__ == "__main__":
    unittest.main()
