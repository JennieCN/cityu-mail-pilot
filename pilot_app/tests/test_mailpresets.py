"""Mailbox preset and onboarding-help tests."""

import unittest

from pilot_app import mailpresets
from pilot_app.security import SecurityError, validate_public_host


class MailboxPresetTests(unittest.TestCase):
    def test_domain_lookup_matches_known_providers(self):
        cases = {
            "someone@qq.com": "qq",
            "Someone@Foxmail.com": "qq",
            "a@163.com": "163",
            "a@yeah.net": "163",
            "a@gmail.com": "gmail",
            "a@icloud.com": "icloud",
            "a@outlook.com": "outlook",
            "a@yahoo.com.hk": "yahoo",
            "student@my.cityu.edu.hk": "custom",
            "a@some-company.example": "custom",
            "": "custom",
            "not-an-email": "custom",
        }
        for email, expected in cases.items():
            self.assertEqual(mailpresets.preset_id_for_email(email), expected, email)

    def test_every_preset_is_complete_and_public(self):
        ids = [item["id"] for item in mailpresets.MAILBOX_PRESETS]
        self.assertEqual(len(ids), len(set(ids)), "preset ids must be unique")
        self.assertIn("custom", ids)
        for item in mailpresets.MAILBOX_PRESETS:
            self.assertTrue(item["label"], item["id"])
            self.assertTrue(item["steps"], item["id"])
            for key in ("imap_port", "smtp_port"):
                self.assertIsInstance(item[key], int, item["id"])
                self.assertTrue(1 <= item[key] <= 65535, item["id"])
        # Every field exposed to the browser must be safe to publish: assert the
        # exact key set rather than keyword-scanning (the glossary legitimately
        # has a key named "password").
        published = mailpresets.public_mailbox_help()
        # `blocked_reason` / `recommended` are what the "换一个邮箱" box is built
        # from: a non-empty reason means this provider cannot work at all, and
        # the recommended ones are the alternatives it offers.
        # `where` 是「在你邮箱的哪一块」那句（设置 → 账户 → IMAP/SMTP 服务）。
        # 第 3 步的示意图上画的就是这同一句，`test_appcode_shots` 逐字比对两边。
        allowed = {"id", "label", "short_label", "domains", "imap_host", "imap_port",
                   "smtp_host", "smtp_port", "steps", "help_url", "help_label", "caution",
                   "blocked_reason", "recommended", "where"}
        # `hosts_by_domain` 是可选的：同一家供应商里**域名 → 服务器**的覆盖表
        # （网易五个域名五台机器）。只有真有覆盖的预置才带它——所以判据是
        # 「必填都在，且不许多出别的」，而不是「字段恰好等于某一份清单」。
        optional = {"hosts_by_domain"}
        for item in published["presets"]:
            self.assertTrue(allowed <= set(item) <= allowed | optional,
                            f"{item['id']}：字段是 {sorted(set(item) - allowed)}，"
                            f"少了 {sorted(allowed - set(item))}")
        # `alternatives` 是「换一个邮箱」那几个按钮的数据源（id/短名/域名），
        # 与 presets 同源，不是另一份清单。
        self.assertEqual(set(published), {"presets", "glossary", "alternatives"})

    def test_known_hosts_pass_outbound_validation_without_dns(self):
        for item in mailpresets.MAILBOX_PRESETS:
            if item["id"] == "custom":
                continue  # intentionally blank: the user fills their own
            for key in ("imap_host", "smtp_host"):
                host = item[key]
                self.assertEqual(validate_public_host(host, resolve_dns=False), host, host)

    def test_private_hosts_are_still_rejected(self):
        for bad in ("localhost", "imap.local", "127.0.0.1"):
            with self.assertRaises(SecurityError):
                validate_public_host(bad, resolve_dns=False)

    def test_glossary_explains_jargon_in_plain_language(self):
        glossary = mailpresets.GLOSSARY
        for key in ("imap", "smtp", "password"):
            self.assertIn(key, glossary)
            self.assertTrue(glossary[key]["term"])
            self.assertTrue(glossary[key]["plain"])
        # The point of the glossary is that it is *not* the technical label.
        self.assertEqual(glossary["imap"]["term"], "收件服务器")
        self.assertNotEqual(glossary["imap"]["term"], glossary["imap"]["technical"])


if __name__ == "__main__":
    unittest.main()
