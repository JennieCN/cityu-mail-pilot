"""「供应商在限流我们」必须与「这个用户的授权码坏了」分开（2026-09-24）。

为什么值得一套测试：`mailbox_error:` 被**刻意**分到最安静的一档（面板红灯、不发邮件），
理由是"一个账号的授权码错了是他自己的问题"。单账号时那是对的；但**整家供应商开始限流时，
同一个 key 会把唯一重要的信号静音**——几百个邮箱一起被限流 = 几百条安静的红灯、零封邮件，
而运营者最可能的反应是"怎么这么多人的码都坏了"，去让用户重新生成授权码（**解决不了**）。

所以这里钉住四件事：

1. 同一家主机上 ≥3 个邮箱被限流 → **一条**走响档的 `provider_pushback:`，
   而那些单账号的红灯让位（同源不重复）；
2. 只有 2 个 → 不聚合（两个可能是巧合，三个互不相干的账号才指向主机）；
3. **凭据错误永远不产生这条告警**（否则真正的限流会被"用户自己的问题"淹没）；
4. 分主机聚合：QQ 三个、163 三个 → 两条，各自的账号名单不许串。
"""

from __future__ import annotations

import datetime as dt
import pathlib
import tempfile
import unittest
from unittest import mock

from pilot_app import alerting, mailio, providercheck
from pilot_app.database import Database
from pilot_app.security import SecretBox, hash_password, token_hash

#: 我们自己译出来的那句话（`explain_imap_failure` 的第 5 支），与服务器的原话各测一遍。
PUSHBACK_OURS = "登录尝试过于频繁，已被邮箱临时限制，请等 15–30 分钟后重试。"
PUSHBACK_RAW = "554 Ip is rejected, smtp auth error limit exceed,163 gzga-smtp-mtada-g0-4"
CREDENTIAL_OURS = "邮箱拒绝了这次登录：授权码不对或已失效。它不是你的邮箱登录密码。"
CREDENTIAL_RAW = "LOGIN Login error or password error"


class ClassifyTests(unittest.TestCase):
    """判据本身：只看文本（`mailboxes.last_error` 就是一句文本，异常早没了）。"""

    def test_the_strings_we_actually_saw(self):
        cases = [
            (PUSHBACK_RAW, "pushback"),
            ("Login frequency limited", "pushback"),
            (PUSHBACK_OURS, "pushback"),
            (CREDENTIAL_RAW, "credential"),
            (CREDENTIAL_OURS, "credential"),
            ("邮箱地址不存在，请检查「你的私人邮箱」是否填对。", "other"),
            ("TLS 证书校验失败，请确认收件服务器地址是否正确。", "network"),
            ("连接不上邮件服务器（网络不通或端口被拦）", "network"),
        ]
        for text, want in cases:
            with self.subTest(text=text[:30]):
                self.assertEqual(mailio.classify_imap_failure(text=text), want)

    def test_an_exception_object_still_classifies(self):
        self.assertEqual(
            mailio.classify_imap_failure(OSError("network is unreachable")), "network")


class PushbackAggregationTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.work.name) / "pilot.sqlite3")
        self.db.initialize()
        self.secrets = SecretBox(b"7" * 32)
        # 一台"健康"的服务器还得有服务商检查与主密钥核对记录，否则会多出无关的发现项
        # ——那不影响本文件的断言，但让失败信息干净。
        providercheck.save(self.db, [], when=dt.datetime.now(dt.timezone.utc))
        self.db.set_setting("master_key_verified_at",
                            dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
        self.db.set_setting("master_key_verified_fingerprint", self.secrets.fingerprint())
        self.env = mock.patch.dict("os.environ", {
            "INFE_PILOT_ADMIN_EMAILS": "boss@example.com"}, clear=False)
        self.env.start()
        self.now = dt.datetime.now(dt.timezone.utc)

    def tearDown(self):
        self.env.stop()
        self.work.cleanup()

    def _mailbox(self, index: int, *, host: str, error: str) -> dict:
        invite = self.db.create_invite(f"u{index}", 1)
        user = self.db.create_user(f"user{index}@example.com",
                                   hash_password("a-long-enough-password"),
                                   token_hash(invite))
        mailbox_id = self.db.upsert_mailbox(user["id"], {
            "email": f"box{index}@{host}", "report_to": f"user{index}@example.com",
            "imap_host": host, "imap_port": 993,
            "smtp_host": "smtp.example.com", "smtp_port": 465, "enabled": True,
            "encrypted_password": self.secrets.encrypt("pw", context=f"mailbox:{user['id']}"),
        })
        self.db.update_mailbox_poll(mailbox_id, last_uid=1, uid_validity="1", error=error)
        return {"user_id": user["id"], "mailbox_id": mailbox_id,
                "email": f"user{index}@example.com"}

    def _evaluate(self) -> dict[str, dict]:
        return {f["key"]: f for f in alerting.evaluate(self.db, now=self.now)}

    def test_three_mailboxes_on_one_host_become_one_loud_finding(self):
        for i in range(1, 4):
            self._mailbox(i, host="imap.qq.com", error=PUSHBACK_OURS)
        findings = self._evaluate()
        self.assertIn("provider_pushback:imap.qq.com", findings)
        self.assertEqual(findings["provider_pushback:imap.qq.com"]["severity"], "critical")
        # 单账号那三条让位：一个 IP 被限流会同时打中几十上百个邮箱，逐条报就淹了。
        self.assertEqual([k for k in findings if k.startswith("mailbox_error:")], [])
        # 而且它必须**会发邮件**——这正是 `mailbox_error:` 做不到的那一点。
        self.assertEqual(alerting.tier_for("provider_pushback:imap.qq.com"), alerting.TIER_MAIL)

    def test_two_mailboxes_are_a_coincidence_not_a_provider(self):
        for i in range(1, 3):
            self._mailbox(i, host="imap.qq.com", error=PUSHBACK_OURS)
        findings = self._evaluate()
        self.assertNotIn("provider_pushback:imap.qq.com", findings)
        self.assertEqual(len([k for k in findings if k.startswith("mailbox_error:")]), 2)

    def test_credential_failures_never_aggregate(self):
        """反向验证的那一半：三个账号的码都坏了，也**不许**说成供应商在限流我们。"""
        for i in range(1, 4):
            self._mailbox(i, host="imap.qq.com", error=CREDENTIAL_OURS)
        findings = self._evaluate()
        self.assertNotIn("provider_pushback:imap.qq.com", findings)
        self.assertEqual(len([k for k in findings if k.startswith("mailbox_error:")]), 3)

    def test_hosts_do_not_share_a_count(self):
        for i in range(1, 4):
            self._mailbox(i, host="imap.qq.com", error=PUSHBACK_RAW)
        for i in range(4, 7):
            self._mailbox(i, host="imap.163.com", error=PUSHBACK_OURS)
        findings = self._evaluate()
        self.assertIn("provider_pushback:imap.qq.com", findings)
        self.assertIn("provider_pushback:imap.163.com", findings)
        # 名单不许串：QQ 那条只说 QQ 的三个账号。
        detail = findings["provider_pushback:imap.qq.com"]["detail"]
        self.assertIn("user1@example.com", detail)
        self.assertNotIn("user4@example.com", detail)
        self.assertIn("3 个邮箱", findings["provider_pushback:imap.163.com"]["title"])


if __name__ == "__main__":
    unittest.main()
