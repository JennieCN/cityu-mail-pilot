"""Sender-domain filter: only mail from allowed domains becomes a report.

The user's private mailbox also receives personal mail (shopping, banks,
newsletters). Summarising that is unwanted noise, but it must not become an
invisible deletion either: this suite pins both halves of that contract.
"""

import os
import unittest

from pilot_app import reports as reports_mod
from pilot_app import service as service_mod


class SenderFilterTests(unittest.TestCase):
    def tearDown(self):
        # Never leak an override into another test.
        service_mod.ALLOWED_SENDER_DOMAINS = self._original

    def setUp(self):
        self._original = service_mod.ALLOWED_SENDER_DOMAINS

    # -- pure classification ----------------------------------------------

    def test_cityu_address_is_allowed(self):
        for address in ("student@my.cityu.edu.hk", "noreply_cap275421@cityu.edu.hk",
                        "Teacher@CityU.edu.hk"):
            self.assertTrue(service_mod.is_allowed_sender(address), address)

    def test_personal_addresses_are_not_allowed(self):
        for address in ("getstarted@codefinity.com", "hello@mail.grammarly.com",
                        "account-security-noreply@accountprotection.microsoft.com",
                        "no-reply@fairwood.com.hk", "10000@qq.com"):
            self.assertFalse(service_mod.is_allowed_sender(address), address)

    def test_subdomain_matching_is_suffix_safe(self):
        # A lookalike domain must not pass just because it contains the string.
        self.assertFalse(service_mod.is_allowed_sender("attacker@notcityu.edu.hk"))
        self.assertFalse(service_mod.is_allowed_sender("attacker@cityu.edu.hk.evil.com"))
        self.assertTrue(service_mod.is_allowed_sender("x@dept.cityu.edu.hk"))

    def test_empty_or_missing_address_is_not_allowed(self):
        for address in ("", "   ", "not-an-address", None):
            self.assertFalse(service_mod.is_allowed_sender(address or ""))

    def test_empty_allow_list_means_process_everything(self):
        service_mod.ALLOWED_SENDER_DOMAINS = ()
        self.assertTrue(service_mod.is_allowed_sender("anyone@example.com"))

    def test_allow_list_is_configurable(self):
        service_mod.ALLOWED_SENDER_DOMAINS = ("cityu.edu.hk", "example.edu")
        self.assertTrue(service_mod.is_allowed_sender("a@example.edu"))
        self.assertFalse(service_mod.is_allowed_sender("a@other.edu"))


class SkippedMailStaysAuditableTests(unittest.TestCase):
    """Skipped mail must be countable and traceable, never silently dropped."""

    def _message(self, uid, subject, sender, status="skipped", reason="发件人不在允许名单内"):
        return {
            "id": f"msg_{uid}", "imap_uid": uid, "subject": subject, "sender_name": sender.split("@")[0],
            "sender_address": sender, "received_at": "2026-09-13T02:00:00+00:00", "importance": "normal",
            "status": status, "last_error": "", "skip_reason": reason, "attempts": 0,
        }

    def test_skipped_mail_is_counted_and_listed_separately(self):
        messages = [
            self._message(1, "Tutorial starts next week", "student@my.cityu.edu.hk", status="sent"),
            self._message(2, "50% off Grammarly Pro", "hello@mail.grammarly.com"),
            self._message(3, "月滿中秋盆菜", "no-reply@fairwood.com.hk"),
        ]
        digest = reports_mod.build_digest(messages, {}, timezone="Asia/Hong_Kong")

        self.assertEqual(digest["metrics"]["total"], 3, "总数必须包含被跳过的邮件")
        self.assertEqual(digest["metrics"]["skipped"], 2)
        self.assertEqual(len(digest["skipped"]), 2)
        # Skipped mail must NOT be reported as a failure.
        self.assertEqual(digest["metrics"]["failed"], 0)
        self.assertEqual(digest["sections"]["failed"], [])
        for entry in digest["skipped"]:
            self.assertTrue(entry["subject"])
            self.assertTrue(entry["sender"])
            self.assertIn("发件人不在允许名单内", entry["reason"])

    def test_markdown_lists_skipped_mail_with_reasons(self):
        messages = [
            self._message(2, "50% off Grammarly Pro", "hello@mail.grammarly.com"),
        ]
        digest = reports_mod.build_digest(messages, {}, timezone="Asia/Hong_Kong")
        markdown = reports_mod.digest_markdown(digest)
        self.assertIn("非允许发件人", markdown)
        self.assertIn("Grammarly", markdown)
        self.assertIn("hello@mail.grammarly.com", markdown)

    def test_skipped_mail_never_enters_a_category_section(self):
        messages = [self._message(2, "Promo", "promo@shop.example")]
        digest = reports_mod.build_digest(messages, {}, timezone="Asia/Hong_Kong")
        for name in reports_mod.CATEGORY_ORDER:
            self.assertEqual(digest["sections"][name], [], name)
        self.assertEqual(digest["metrics"]["actionable"], 0)


if __name__ == "__main__":
    unittest.main()
