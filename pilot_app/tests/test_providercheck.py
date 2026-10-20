"""服务商「还让不让用授权码」的判定与提前告警。

这个文件的存在理由是 2026-09-16 的**「outlook 那件事」**：微软个人版关掉了基础认证，
用户按向导拿到授权码、填进来、永远被拒；我们这边**一切正常**——灯是红的、信也发了，
可谁都说不清「是他的码不对，还是这条路已经没了」。当时是**用户先撞上、我们后知道**。

所以这里钉的是两件事：

* **判定要准**。分类器的输入是**真机上抓下来的那几行 CAPABILITY**（2026-09-16
  从生产服务器连出去抓的，原样抄在 ``REAL_CAPABILITIES`` 里）。其中最要紧的一条是
  **「没广播 AUTH=PLAIN」不等于「不能用密码」**：163 的 CAPABILITY 里根本没有 PLAIN，
  可真机用（假）凭据登录，它照常进入验密码那一步。把「没提到」当拒绝，会误杀一家的。
* **下一次要提前知道**。未封禁的服务商一旦变成 oauth_only，哨兵要报出来——
  而且报的是**我们存下来的那次探测结果**，`evaluate()` 依旧不联网（它的全部测试都依赖这一点）。
"""

from __future__ import annotations

import datetime as dt
import pathlib
import tempfile
import unittest
from unittest import mock

from pilot_app import alerting, providercheck
from pilot_app.database import Database

NOW = dt.datetime(2026, 9, 16, 12, 0, tzinfo=dt.timezone.utc)

# 2026-09-16 从生产服务器连出去抓的原始应答（只取 CAPABILITY 那一段）。
# 这些字符串是**证据**，不是编出来的例子：分类器要照着它们判。
REAL_CAPABILITIES = {
    "qq": "QQMail XMIMAP4Server ready * CAPABILITY IMAP4rev1 XLIST MOVE IDLE ID UIDPLUS "
          "SASL-IR AUTH=PLAIN AUTH=LOGIN AUTH=XOAUTH2 NAMESPACE CHILDREN",
    "163": "Coremail System IMap Server Ready * CAPABILITY IMAP4rev1 XLIST SPECIAL-USE ID "
           "LITERAL+ STARTTLS APPENDLIMIT=71680000 SASL-IR AUTH=XOAUTH2",
    "gmail": "Gimap ready for requests * CAPABILITY IMAP4rev1 UNSELECT IDLE NAMESPACE QUOTA "
             "ID XLIST CHILDREN SASL-IR AUTH=XOAUTH2 AUTH=PLAIN AUTH=OAUTHBEARER",
    "icloud": "OK [CAPABILITY XAPPLEPUSHSERVICE IMAP4 IMAP4rev1 SASL-IR AUTH=ATOKEN "
              "AUTH=PLAIN AUTH=ATOKEN2 AUTH=XOAUTH2]",
    "yahoo": "Welcome! IMAP Server up and ready * CAPABILITY IMAP4rev1 SASL-IR AUTH=PLAIN "
             "AUTH=XOAUTH2 AUTH=OAUTHBEARER ID MOVE NAMESPACE",
    # 微软个人版现在的样子：明确挂出 LOGINDISABLED，只留 OAuth。
    "outlook": "Microsoft Exchange IMAP4 service ready * CAPABILITY IMAP4 IMAP4rev1 "
               "AUTH=XOAUTH2 LOGINDISABLED SASL-IR UIDPLUS MOVE ID UNSELECT CHILDREN IDLE",
}


def fake_probe(mapping: dict[str, str]):
    """把「主机名 → 判定」做成一枚假探针；测试因此**不碰网络**。"""
    def probe(host: str, port: int = 993):
        for state, hosts in mapping.items():
            if any(host == item or host.endswith("." + item) for item in hosts):
                return state, f"fake:{host}"
        return providercheck.UNREACHABLE, f"fake-unreachable:{host}"
    return probe


class ClassifierTests(unittest.TestCase):
    def test_every_provider_is_classified_from_its_real_capability_line(self):
        expected = {"qq": providercheck.PASSWORD_OK, "163": providercheck.PASSWORD_OK,
                    "gmail": providercheck.PASSWORD_OK, "icloud": providercheck.PASSWORD_OK,
                    "yahoo": providercheck.PASSWORD_OK, "outlook": providercheck.OAUTH_ONLY}
        for name, capability in REAL_CAPABILITIES.items():
            with self.subTest(provider=name):
                self.assertEqual(providercheck.classify(capability), expected[name])

    def test_silence_is_not_a_refusal(self):
        """163 那一行里没有 AUTH=PLAIN —— **不能**因此判它不支持密码。"""
        self.assertEqual(providercheck.classify(REAL_CAPABILITIES["163"]),
                         providercheck.PASSWORD_OK)
        self.assertEqual(providercheck.classify(""), providercheck.PASSWORD_OK)

    def test_the_explicit_refusal_is_what_counts(self):
        self.assertEqual(providercheck.classify("AUTH=XOAUTH2 LOGINDISABLED"),
                         providercheck.OAUTH_ONLY)


class DriftTests(unittest.TestCase):
    def test_the_microsoft_block_matches_reality(self):
        """封禁与真机一致：它现在**确实**只提供 OAuth，所以不该报漂移。"""
        results = providercheck.check_all(fake_probe({
            providercheck.OAUTH_ONLY: ["outlook.office365.com"],
            providercheck.PASSWORD_OK: ["imap.qq.com", "imap.163.com", "imap.gmail.com",
                                        "imap.mail.me.com", "imap.mail.yahoo.com"],
        }))
        self.assertEqual(providercheck.drift(results), [])

    def test_a_second_outlook_is_reported(self):
        """QQ 哪天也关掉密码登录 —— 这就是必须提前知道的事。"""
        results = providercheck.check_all(fake_probe({
            providercheck.OAUTH_ONLY: ["imap.qq.com", "outlook.office365.com"],
            providercheck.PASSWORD_OK: ["imap.163.com", "imap.gmail.com",
                                        "imap.mail.me.com", "imap.mail.yahoo.com"],
        }))
        bad = providercheck.drift(results)
        self.assertEqual([item["id"] for item in bad], ["qq"])

    def test_an_unreachable_provider_is_not_a_closed_door(self):
        """连不上只有一种解释：这次没问到。据此改产品是错的。"""
        results = providercheck.check_all(fake_probe({
            providercheck.UNREACHABLE: ["imap.qq.com"],
            providercheck.OAUTH_ONLY: ["outlook.office365.com"],
            providercheck.PASSWORD_OK: ["imap.163.com", "imap.gmail.com",
                                        "imap.mail.me.com", "imap.mail.yahoo.com"],
        }))
        self.assertEqual(providercheck.drift(results), [])


class StampAndSentinelTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.work.name) / "pilot.sqlite3")
        self.db.initialize()

    def tearDown(self):
        self.work.cleanup()

    def _someone_is_using_it(self) -> None:
        """建一个配好邮箱的账号 —— 「检查没跑」只在**有人会被影响**时才报。

        这台服务器上有人用，才谈得上「服务商的授权码通道」；开发机、预览库、
        刚装好还没人注册的实例都不该在面板上挂那一条（那是教人忽略告警）。
        """
        from pilot_app.security import SecretBox, hash_password, token_hash
        invite = self.db.create_invite("provider-check", 1)
        user = self.db.create_user("someone@example.com", hash_password("a-long-enough-password"),
                                   token_hash(invite))
        self.db.upsert_mailbox(user["id"], {
            "email": "box@qq.com", "report_to": "someone@example.com",
            "imap_host": "imap.qq.com", "imap_port": 993,
            "smtp_host": "smtp.qq.com", "smtp_port": 465, "enabled": True,
            "encrypted_password": SecretBox(b"7" * 32).encrypt(
                "pw", context=f"mailbox:{user['id']}"),
        })

    def _all_good(self):
        return providercheck.check_all(fake_probe({
            providercheck.OAUTH_ONLY: ["outlook.office365.com"],
            providercheck.PASSWORD_OK: ["imap.qq.com", "imap.163.com", "imap.gmail.com",
                                        "imap.mail.me.com", "imap.mail.yahoo.com"],
        }))

    def test_the_sentinel_never_touches_the_network(self):
        """`evaluate()` 是纯的：它只读那条记录，探测是 worker 一天跑一次的事。"""
        providercheck.save(self.db, self._all_good(), when=NOW - dt.timedelta(hours=1))
        with mock.patch.object(providercheck, "probe_imap",
                               side_effect=AssertionError("evaluate 不该联网")):
            findings = alerting.evaluate(self.db, now=NOW)
        self.assertEqual([f for f in findings if "provider" in f["key"]], [])

    def test_a_closed_door_becomes_a_finding(self):
        results = self._all_good()
        for item in results:
            if item["id"] == "gmail":
                item["state"] = providercheck.OAUTH_ONLY
        providercheck.save(self.db, results, when=NOW - dt.timedelta(hours=1))
        findings = alerting.evaluate(self.db, now=NOW)
        hit = [f for f in findings if f["key"] == "provider_password_auth:gmail"]
        self.assertEqual(len(hit), 1, findings)
        self.assertIn("OAuth", hit[0]["detail"])
        self.assertEqual(alerting.tier_for(hit[0]["key"]), alerting.TIER_MAIL)

    def test_a_stale_check_is_reported_but_quietly(self):
        self._someone_is_using_it()
        providercheck.save(self.db, self._all_good(), when=NOW - dt.timedelta(days=4))
        findings = alerting.evaluate(self.db, now=NOW)
        hit = [f for f in findings if f["key"] == "provider_check_stale"]
        self.assertEqual(len(hit), 1, findings)
        # 没人正卡着，而且它与「某家真的关门了」是两件事 —— 只放面板。
        self.assertEqual(alerting.tier_for("provider_check_stale"), alerting.TIER_PANEL)

    def test_never_having_run_is_reported_too(self):
        self._someone_is_using_it()
        findings = alerting.evaluate(self.db, now=NOW)
        self.assertTrue([f for f in findings if f["key"] == "provider_check_stale"])

    def test_an_idle_instance_is_not_told_about_the_probe(self):
        """没人在用的实例（开发机、预览库）不该看到这一条 —— 那是噪音，不是信号。"""
        providercheck.save(self.db, [], when=NOW - dt.timedelta(days=9))
        findings = alerting.evaluate(self.db, now=NOW)
        self.assertEqual([f for f in findings if "provider" in f["key"]], [])

    def test_the_probe_runs_once_a_day_not_once_a_pass(self):
        calls = []

        def probe(host, port=993):
            calls.append(host)
            return providercheck.PASSWORD_OK, "fake"

        first = providercheck.refresh_if_due(self.db, now=NOW, probe=probe)
        self.assertIsNotNone(first)
        probed_once = len(calls)
        # 五分钟后再来一次：什么都不做（哨兵每 5 分钟跑一次，不能每 5 分钟连五家邮箱）。
        self.assertIsNone(providercheck.refresh_if_due(
            self.db, now=NOW + dt.timedelta(minutes=5), probe=probe))
        self.assertEqual(len(calls), probed_once)
        self.assertIsNotNone(providercheck.refresh_if_due(
            self.db, now=NOW + dt.timedelta(hours=25), probe=probe))
        self.assertGreater(len(calls), probed_once)


class ManageCommandTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.work.name) / "pilot.sqlite3")
        self.db.initialize()

    def tearDown(self):
        self.work.cleanup()

    def _run(self, state: str) -> int:
        from pilot_app import manage
        # 注意别写成字面量里两个同名键：后一个会静默覆盖前一个（第一版就是这样，
        # QQ 掉进了「连不上」，于是这条测试测的是别的东西）。
        mapping = {
            providercheck.OAUTH_ONLY: ["outlook.office365.com"],
            providercheck.PASSWORD_OK: ["imap.163.com", "imap.gmail.com",
                                        "imap.mail.me.com", "imap.mail.yahoo.com"],
        }
        mapping[state] = list(mapping.get(state, [])) + ["imap.qq.com"]
        # 先把真的 check_all 抓在手里：lambda 里再调 providercheck.check_all 会调到
        # 被替换后的那个，于是无限递归（第一版就是这样，RecursionError）。
        real_check_all = providercheck.check_all
        with mock.patch.object(providercheck, "check_all",
                               side_effect=lambda probe=providercheck.probe_imap:
                               real_check_all(fake_probe(mapping))):
            return manage.check_providers(self.db)

    def test_it_exits_non_zero_when_a_provider_closed_the_door(self):
        self.assertEqual(self._run(providercheck.OAUTH_ONLY), 1)

    def test_it_exits_zero_when_everything_matches(self):
        self.assertEqual(self._run(providercheck.PASSWORD_OK), 0)


if __name__ == "__main__":
    unittest.main()
