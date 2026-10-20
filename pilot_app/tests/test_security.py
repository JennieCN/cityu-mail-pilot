import base64
import os
import secrets
import unittest
from unittest import mock

from pilot_app import security
from pilot_app.security import (SecretBox, SecurityError, hash_password, spend_verification_time,
                                validate_outbound_https_url, validate_public_host, verify_password)


class SecurityTests(unittest.TestCase):
    def test_password_hash_is_salted_and_verifiable(self):
        first = hash_password("a sufficiently long password")
        second = hash_password("a sufficiently long password")
        self.assertNotEqual(first, second)
        self.assertTrue(verify_password("a sufficiently long password", first))
        self.assertFalse(verify_password("wrong password", first))

    def test_rejects_private_and_insecure_api_urls(self):
        for value in ("http://api.example.com/v1", "https://localhost/v1", "https://127.0.0.1/v1", "https://10.0.0.2/v1"):
            with self.subTest(value=value), self.assertRaises(SecurityError):
                validate_outbound_https_url(value, resolve_dns=False)

    def test_rejects_private_mail_hosts(self):
        for value in ("localhost", "127.0.0.1", "10.0.0.3", "bad_host"):
            with self.subTest(value=value), self.assertRaises(SecurityError):
                validate_public_host(value, resolve_dns=False)

    def test_secret_context_prevents_cross_user_decryption(self):
        try:
            import cryptography  # noqa: F401
        except ImportError:
            self.skipTest("cryptography is installed by requirements.txt")
        box = SecretBox(secrets.token_bytes(32))
        encrypted = box.encrypt("private-api-key", context="connection:user-a:model")
        self.assertEqual(box.decrypt(encrypted, context="connection:user-a:model"), "private-api-key")
        with self.assertRaises(SecurityError):
            box.decrypt(encrypted, context="connection:user-b:model")


if __name__ == "__main__":
    unittest.main()


class TimingEqualizerTests(unittest.TestCase):
    """账号不存在时也要**把一次 PBKDF2 的时间花掉**（2026-09-24）。

    为什么值得测：登录原来是 `if not user or not verify_password(...)`——短路让
    「账号不存在」比「密码错」快一个数量级，而文案恒定挡不住**计时**这条侧信道，
    那就是一个可用的账号枚举 oracle。判据不看时间（会飘），看它**真的调了一次校验**。
    """

    def test_it_runs_one_real_verification(self):
        seen = []

        def spy(password, encoded):
            seen.append((password, encoded))
            return False

        with mock.patch.object(security, "verify_password", side_effect=spy) as called:
            spend_verification_time("a-long-enough-password")
        called.assert_called_once()
        self.assertEqual(seen[0][1], security.TIMING_EQUALIZER_HASH)

    def test_the_equalizer_hash_is_a_real_600k_hash(self):
        """它的参数必须与真哈希同量级，否则"等时"是假的。"""
        algorithm, iterations, _salt, _digest = security.TIMING_EQUALIZER_HASH.split("$", 3)
        self.assertEqual(algorithm, "pbkdf2_sha256")
        self.assertGreaterEqual(int(iterations), 300_000)
        self.assertFalse(verify_password("随便什么口令", security.TIMING_EQUALIZER_HASH))
