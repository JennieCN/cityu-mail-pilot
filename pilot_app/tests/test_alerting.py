"""Tests for the operator alert sentinel.

The properties that matter, and why:

* a healthy pilot must be **silent** — an alerting system that cries wolf gets
  ignored, which is the same as not having one;
* a broken mailbox, a stalled poller, a backlog, a filling disk and an expiring
  certificate must each be **reported**, and each must keep being reported at
  most once per silence window rather than every loop;
* clearing a condition must produce exactly one recovery notice;
* a **failure to send** must leave the state untouched, so the next pass tries
  again instead of assuming the operator was told;
* nothing that leaves the machine may contain a credential.
"""

from __future__ import annotations

import base64
import datetime as dt
import pathlib
import tempfile
import unittest
from unittest import mock

from pilot_app import alerting, providercheck
from pilot_app.database import Database, utc_now
from pilot_app.security import SecretBox, hash_password, token_hash

NOW = dt.datetime(2026, 9, 14, 12, 0, tzinfo=dt.timezone.utc)


class AlertingTestCase(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.work.name) / "pilot.sqlite3")
        self.db.initialize()
        self.secrets = SecretBox(b"7" * 32)
        # 一台「健康」的服务器有一份**新鲜的**服务商检查记录：worker 每天探一次并存下来
        # （见 providercheck.refresh_if_due）。夹具里补上它，否则下面那些「没有任何
        # 告警」的断言测的其实是一个从没跑过检查的实例 —— 而那本身就该报。
        providercheck.save(self.db, [], when=dt.datetime.now(dt.timezone.utc))
        # 同理：一台「健康」的服务器还得有一份**记过的**主密钥离线副本核对
        # （`manage master-key-verified` 的产物）。缺了它，下面每一条「没有任何告警」的
        # 断言都会多出一条 `master_key_copy_missing` —— 那正是它该做的事：钥匙丢了是全
        # 项目里唯一「不会有人知道」的故障，所以它现在是一条真正的发现项，而不是一句
        # 要人主动去跑的命令。判据（含三种状态）在 `backup.key_copy_state`。
        self.db.set_setting("master_key_verified_at",
                            dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))
        self.db.set_setting("master_key_verified_fingerprint", self.secrets.fingerprint())
        # "Now" must be the real clock, not a constant: the mailbox stamps the
        # sentinel compares against are written by the database itself. A frozen
        # clock in the future makes every healthy mailbox look hours stale, and
        # a frozen clock in the past hides every fresh one.
        self.now = dt.datetime.now(dt.timezone.utc)
        self.env = mock.patch.dict("os.environ", {
            "INFE_PILOT_ADMIN_EMAILS": "boss@example.com",
        }, clear=False)
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.work.cleanup()

    def _user(self, email: str = "boss@example.com", *, index: int = 1,
              with_mailbox: bool = True, enabled: bool = True) -> dict:
        invite = self.db.create_invite(f"u{index}", 1)
        user = self.db.create_user(email, hash_password("a-long-enough-password"),
                                   token_hash(invite))
        mailbox_id = ""
        if with_mailbox:
            mailbox_id = self.db.upsert_mailbox(user["id"], {
                "email": f"box{index}@qq.com", "report_to": email,
                "imap_host": "imap.qq.com", "imap_port": 993,
                "smtp_host": "smtp.qq.com", "smtp_port": 465,
                "enabled": enabled,
                "encrypted_password": self.secrets.encrypt("pw", context=f"mailbox:{user['id']}"),
            })
        return {"user": user, "mailbox_id": mailbox_id}

    def _polled(self, mailbox_id: str, *, seconds_ago: int = 0, error: str = "") -> None:
        """Stamp a poll result, backdating last_polled_at when asked."""
        self.db.update_mailbox_poll(mailbox_id, last_uid=1, uid_validity="1", error=error)
        if seconds_ago:
            stamp = (dt.datetime.now(dt.timezone.utc)
                     - dt.timedelta(seconds=seconds_ago)).isoformat(timespec="seconds")
            with self.db.connect() as connection:
                connection.execute("UPDATE mailboxes SET last_polled_at=? WHERE id=?",
                                   (stamp, mailbox_id))

    def _queue(self, owner: dict, count: int) -> None:
        for uid in range(count):
            self.db.insert_message(owner["user"]["id"], owner["mailbox_id"], "1", uid + 1,
                                   {"subject": f"s{uid}", "message_key": f"k{uid}"})

    def _failed_reports(self, owner: dict, count: int) -> None:
        for index in range(count):
            report_id = self.db.create_report(
                user_id=owner["user"]["id"], message_id=None, kind="immediate",
                subject=f"r{index}", body="x", sent_to="a@b.c")
            self.db.fail_report(report_id, "boom")

    def _keys(self, findings: list[dict]) -> set:
        return {item["key"] for item in findings}


class EvaluateTests(AlertingTestCase):
    def test_a_healthy_pilot_produces_no_findings(self):
        owner = self._user()
        self._polled(owner["mailbox_id"])
        self.assertEqual(
            alerting.evaluate(self.db, now=self.now, disk_percent=10.0, certificate_days=80.0), [])

    def test_a_failed_poll_is_reported_even_though_it_stamps_last_polled_at(self):
        """A failing poll still updates last_polled_at, so staleness alone would
        never see it. Only the stored error exposes this state."""
        owner = self._user()
        self._polled(owner["mailbox_id"], error="IMAP 认证失败")
        findings = alerting.evaluate(self.db, now=self.now, disk_percent=1.0, certificate_days=90.0)
        self.assertEqual(self._keys(findings), {f"mailbox_error:{owner['user']['id']}"})
        self.assertEqual(findings[0]["severity"], "critical")
        self.assertIn("IMAP 认证失败", findings[0]["detail"])

    def test_a_stalled_poller_is_reported(self):
        owner = self._user()
        self._polled(owner["mailbox_id"], seconds_ago=3600)
        findings = alerting.evaluate(self.db, now=dt.datetime.now(dt.timezone.utc),
                                     disk_percent=1.0, certificate_days=90.0)
        self.assertEqual(self._keys(findings), {f"mailbox_stale:{owner['user']['id']}"})

    def test_a_mailbox_never_polled_is_reported(self):
        owner = self._user()
        findings = alerting.evaluate(self.db, now=self.now, disk_percent=1.0, certificate_days=90.0)
        self.assertIn(f"mailbox_stale:{owner['user']['id']}", self._keys(findings))

    def test_a_failed_mailbox_is_one_problem_not_two(self):
        """A failed poll is the cause; "no poll for N minutes" is the consequence.

        Reporting both counts one incident twice, and on 2026-09-15 the pair sent
        the operator about seventy mails in a day. The stale check now runs only
        when there is no error to report -- so a genuinely *silent* stall (the
        poller thread died with no error anywhere) is still caught.
        """
        owner = self._user()
        self._polled(owner["mailbox_id"], error="IMAP 认证失败")
        keys = self._keys(alerting.evaluate(self.db, now=self.now, disk_percent=1.0,
                                            certificate_days=90.0))
        self.assertIn(f"mailbox_error:{owner['user']['id']}", keys)
        self.assertNotIn(f"mailbox_stale:{owner['user']['id']}", keys,
                         "已经报了原因，就不要再报它的后果")

    def test_a_silent_stall_keeps_the_same_detail_between_passes(self):
        """The detail must not carry a live counter.

        `_should_send` re-mails a finding whose detail *changed*, so a detail
        like "已有 16 分钟"复-mailed on every pass -- the dedupe rule became a
        metronome. The threshold in the detail is stable; the exact age is in the
        console instead.
        """
        owner = self._user()
        self._polled(owner["mailbox_id"], seconds_ago=40 * 60)
        first = {item["key"]: item for item in alerting.evaluate(
            self.db, now=self.now, disk_percent=1.0, certificate_days=90.0)}
        later = {item["key"]: item for item in alerting.evaluate(
            self.db, now=self.now + dt.timedelta(minutes=5), disk_percent=1.0,
            certificate_days=90.0)}
        key = f"mailbox_stale:{owner['user']['id']}"
        self.assertIn(key, first)
        self.assertEqual(first[key]["detail"], later[key]["detail"],
                         "详情里不能有会随时间自己变化的数字，否则每轮都会重发")

    def test_a_paused_account_is_left_alone(self):
        """Pausing is an operator decision; alerting on it would train them to
        ignore the alerts."""
        owner = self._user()
        self._polled(owner["mailbox_id"], error="IMAP 认证失败")
        self.db.set_user_status(owner["user"]["id"], "paused")
        self.assertEqual(
            alerting.evaluate(self.db, now=self.now, disk_percent=1.0, certificate_days=90.0), [])

    def test_a_disabled_mailbox_is_left_alone(self):
        owner = self._user(enabled=False)
        self.assertEqual(
            alerting.evaluate(self.db, now=self.now, disk_percent=1.0, certificate_days=90.0), [])

    def test_a_backlog_is_reported_only_above_the_threshold(self):
        owner = self._user()
        self._polled(owner["mailbox_id"])
        self._queue(owner, alerting.ALERT_QUEUE_DEPTH)
        self.assertNotIn("queue_backlog", self._keys(
            alerting.evaluate(self.db, now=self.now, disk_percent=1.0, certificate_days=90.0)))
        self._queue(owner, alerting.ALERT_QUEUE_DEPTH + 1)
        # The second call re-inserts distinct message keys, so the depth grows.
        self.assertIn("queue_backlog", self._keys(
            alerting.evaluate(self.db, now=self.now, disk_percent=1.0, certificate_days=90.0)))

    def test_failed_reports_are_reported_only_above_the_threshold(self):
        owner = self._user()
        self._polled(owner["mailbox_id"])
        self._failed_reports(owner, alerting.ALERT_FAILED_REPORTS - 1)
        self.assertNotIn("failed_reports", self._keys(
            alerting.evaluate(self.db, now=self.now, disk_percent=1.0, certificate_days=90.0)))
        self._failed_reports(owner, 2)
        self.assertIn("failed_reports", self._keys(
            alerting.evaluate(self.db, now=self.now, disk_percent=1.0, certificate_days=90.0)))

    def test_a_recovered_account_goes_green_again(self):
        """**修好了就该灭**：数的是「自上次成功发出以来」的失败，不是历史全量。

        2026-09-22 生产实测：两个账号每天各失败一封简报，数字里还混着一封 9/16 的
        一次性失败（之后成功过三次）。全量计数只增不减，于是那盏灯永远变不绿——
        而一盏永远变不绿的灯，久了就是眼罩。
        """
        owner = self._user()
        self._polled(owner["mailbox_id"])
        self._failed_reports(owner, alerting.ALERT_FAILED_REPORTS + 3)
        self.assertIn("failed_reports", self._keys(
            alerting.evaluate(self.db, now=self.now, disk_percent=1.0, certificate_days=90.0)))

        later = self.db.create_report(user_id=owner["user"]["id"], message_id=None, kind="daily",
                                      subject="恢复了", body="x", sent_to="a@b.c")
        self.db.mark_report_sent(later)
        self.assertNotIn("failed_reports", self._keys(
            alerting.evaluate(self.db, now=self.now, disk_percent=1.0, certificate_days=90.0)),
            "成功发出之后，那之前的失败不该再让它亮着")

    def test_a_failure_after_a_success_counts_again(self):
        """反过来也要对：恢复之后又失败，照样报。"""
        owner = self._user()
        self._polled(owner["mailbox_id"])
        self._failed_reports(owner, alerting.ALERT_FAILED_REPORTS + 3)
        later = self.db.create_report(user_id=owner["user"]["id"], message_id=None, kind="daily",
                                      subject="恢复了", body="x", sent_to="a@b.c")
        self.db.mark_report_sent(later)
        self._failed_reports(owner, alerting.ALERT_FAILED_REPORTS + 1)
        # 时间戳只有秒精度：同秒内的「成功之后又失败」比不出来，所以把这两封显式挪后一秒。
        # （这不是在迁就实现——真实世界里一次成功与下一次失败不可能在同一秒。）
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE reports SET created_at=? WHERE user_id=? AND status='failed'",
                ((dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=2)).isoformat(timespec="seconds"),
                 owner["user"]["id"]))
        self.assertIn("failed_reports", self._keys(
            alerting.evaluate(self.db, now=self.now + dt.timedelta(seconds=3),
                              disk_percent=1.0, certificate_days=90.0)))

    def test_a_broken_mailbox_does_not_report_its_consequence_twice(self):
        """同一个账号：邮箱登录不上 + 它的报告发不出去 → **只报一条**。

        简报是**从用户自己的邮箱**发出去的，所以邮箱坏了必然让报告失败——那是因与果，
        不是两件事。与 `mailbox_stale` 那段注释同一条规矩。
        """
        owner = self._user()
        self.db.record_mailbox_verification(owner["mailbox_id"], error="授权码不对或已失效")
        self._failed_reports(owner, alerting.ALERT_FAILED_REPORTS + 3)
        keys = self._keys(alerting.evaluate(self.db, now=self.now, disk_percent=1.0, certificate_days=90.0))
        self.assertIn(f"mailbox_error:{owner['user']['id']}", keys)
        self.assertNotIn("failed_reports", keys, "同一件事不该报两遍；后果那条还永远不会灭")

    def test_another_account_s_failures_are_not_masked(self):
        """不同根因不许互相遮盖：另一个账号邮箱没问题，它的失败要照报。"""
        broken = self._user("broken@example.com", index=1)
        self.db.record_mailbox_verification(broken["mailbox_id"], error="授权码不对或已失效")
        self._failed_reports(broken, alerting.ALERT_FAILED_REPORTS + 3)
        healthy = self._user("healthy@example.com", index=2)
        self._polled(healthy["mailbox_id"])
        self._failed_reports(healthy, alerting.ALERT_FAILED_REPORTS + 1)
        findings = alerting.evaluate(self.db, now=self.now, disk_percent=1.0, certificate_days=90.0)
        keys = self._keys(findings)
        self.assertIn("failed_reports", keys)
        self.assertIn(f"mailbox_error:{broken['user']['id']}", keys)

    def test_the_wording_says_what_it_counts(self):
        """措辞说清数的是什么，否则没人相信它能变绿。"""
        owner = self._user()
        self._polled(owner["mailbox_id"])
        self._failed_reports(owner, alerting.ALERT_FAILED_REPORTS + 1)
        detail = next(item["detail"] for item in
                      alerting.evaluate(self.db, now=self.now, disk_percent=1.0, certificate_days=90.0)
                      if item["key"] == "failed_reports")
        self.assertIn("自上次成功发出以来", detail)
        self.assertIn("邮箱本身没有报错", detail)

    def test_disk_is_reported_at_the_threshold(self):
        owner = self._user()
        self._polled(owner["mailbox_id"])
        below = alerting.evaluate(self.db, now=self.now,
                                  disk_percent=alerting.ALERT_DISK_PERCENT - 0.1,
                                  certificate_days=90.0)
        self.assertNotIn("disk", self._keys(below))
        at = alerting.evaluate(self.db, now=self.now, disk_percent=float(alerting.ALERT_DISK_PERCENT),
                               certificate_days=90.0)
        self.assertIn("disk", self._keys(at))

    def test_certificate_is_reported_when_close_and_when_expired(self):
        owner = self._user()
        self._polled(owner["mailbox_id"])
        fresh = alerting.evaluate(self.db, now=self.now, disk_percent=1.0,
                                  certificate_days=float(alerting.ALERT_CERT_DAYS))
        self.assertNotIn("tls_cert", self._keys(fresh))
        soon = alerting.evaluate(self.db, now=self.now, disk_percent=1.0,
                                 certificate_days=float(alerting.ALERT_CERT_DAYS) - 1)
        self.assertIn("tls_cert", self._keys(soon))
        expired = alerting.evaluate(self.db, now=self.now, disk_percent=1.0, certificate_days=-3.0)
        self.assertIn("已过期", expired[0]["detail"])

    def test_unreadable_readings_do_not_invent_findings(self):
        """The macOS dev box cannot read /proc; None must mean "unknown", not
        "fine" and certainly not "broken"."""
        owner = self._user()
        self._polled(owner["mailbox_id"])
        self.assertEqual(
            alerting.evaluate(self.db, now=self.now, disk_percent=None, certificate_days=None), [])


class DeDuplicationTests(AlertingTestCase):
    def test_a_new_finding_is_sent(self):
        self.assertTrue(alerting._should_send(None, {"detail": "d"}, NOW))

    def test_a_repeat_inside_the_window_is_suppressed(self):
        previous = {"open": 1, "detail": "d",
                    "last_sent_at": (NOW - dt.timedelta(minutes=5)).isoformat()}
        self.assertFalse(alerting._should_send(previous, {"detail": "d"}, NOW))

    def test_a_repeat_after_the_window_is_sent(self):
        previous = {"open": 1, "detail": "d",
                    "last_sent_at": (NOW - dt.timedelta(
                        seconds=alerting.ALERT_REPEAT_SECONDS + 1)).isoformat()}
        self.assertTrue(alerting._should_send(previous, {"detail": "d"}, NOW))

    def test_a_changed_detail_is_sent_immediately(self):
        previous = {"open": 1, "detail": "old",
                    "last_sent_at": (NOW - dt.timedelta(seconds=5)).isoformat()}
        self.assertTrue(alerting._should_send(previous, {"detail": "new"}, NOW))

    def test_a_cleared_then_returning_finding_is_sent_again(self):
        previous = {"open": 0, "detail": "d",
                    "last_sent_at": (NOW - dt.timedelta(seconds=5)).isoformat()}
        self.assertTrue(alerting._should_send(previous, {"detail": "d"}, NOW))


class TierTests(AlertingTestCase):
    """Which channel each finding takes, and what "已知晓" does.

    The operator's complaint was **74 alert e-mails in a day**, all of them about
    three findings they already knew about. The fix was not a longer timer -- it
    was admitting that "how bad is it" and "should this interrupt me" are two
    different questions. These tests pin the second one.
    """

    def _stalled(self, email: str, *, age_hours: float = 40) -> str:
        """An account that registered long ago and never finished setting up."""
        user_id = f"usr_{email.split('@')[0]}"
        created = (dt.datetime.now(dt.timezone.utc)
                   - dt.timedelta(hours=age_hours)).isoformat(timespec="seconds")
        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO users(id,email,password_hash,status,created_at) VALUES(?,?,?,?,?)",
                (user_id, email, "x", "active", created))
        return user_id

    def _run(self, *, now=None, disk=10.0, sent=None):
        sent = sent if sent is not None else []
        result = alerting.run_checks(
            self.db, self.secrets, now=now or self.now, disk=disk, certificate_days=90.0,
            sender=lambda *args: sent.append(args) or ["boss@example.com"])
        return result, sent

    # -- tier assignment ---------------------------------------------------

    # -- 主密钥的离线副本：机器看不到，但能记住「人最后一次说核对过」 ----------

    def _keys(self, findings):
        return {item["key"] for item in findings}

    def _evaluate(self):
        return alerting.evaluate(self.db, now=self.now, disk_percent=10.0,
                                 certificate_days=90.0,
                                 master_key_fingerprint=self.secrets.fingerprint())

    def test_a_pilot_that_never_recorded_a_copy_is_told_once_a_day(self):
        """**全项目里唯一「丢了也没人会知道」的故障**，所以它不能只写在一条要人主动去跑
        的命令里。夹具默认已经记过一次（见 setUp），这里把它抹掉还原真实状态。"""
        with self.db.connect() as connection:
            connection.execute("DELETE FROM app_settings WHERE key LIKE 'master_key_verified%'")
        findings = self._evaluate()
        self.assertIn("master_key_copy_missing", self._keys(findings))
        self.assertEqual(alerting.tier_for("master_key_copy_missing"), alerting.TIER_PANEL,
                         "站着不动的准备事项：面板里一直看得到，但不天天发信")

    def test_a_copy_confirmed_with_another_key_is_loud(self):
        """架子上那把打不开现在的备份——这不是提醒，是正在流血。"""
        from pilot_app.security import key_fingerprint
        other = base64.urlsafe_b64encode(bytes(range(1, 33))).decode()
        self.db.set_setting("master_key_verified_at", self.now.isoformat(timespec="seconds"))
        self.db.set_setting("master_key_verified_fingerprint", key_fingerprint(other))
        findings = self._evaluate()
        self.assertIn("master_key_copy_mismatch", self._keys(findings))
        self.assertEqual(alerting.tier_for("master_key_copy_mismatch"), alerting.TIER_MAIL,
                         "与当前主密钥不一致必须是响的那一档")
        item = next(f for f in findings if f["key"] == "master_key_copy_mismatch")
        self.assertIn("另一把钥匙", item["detail"])

    def test_a_copy_confirmed_long_ago_joins_the_digest(self):
        old = (self.now - dt.timedelta(days=400)).isoformat(timespec="seconds")
        self.db.set_setting("master_key_verified_at", old)
        self.db.set_setting("master_key_verified_fingerprint", self.secrets.fingerprint())
        self.assertIn("master_key_copy_stale", self._keys(self._evaluate()))

    def test_a_fresh_matching_record_produces_nothing(self):
        self.assertEqual([f for f in self._evaluate()
                          if f["key"].startswith("master_key_copy")], [])

    def test_without_a_fingerprint_the_check_says_nothing(self):
        """算不出指纹时说「不知道」，而不是猜一个结论——`evaluate()` 保持可注入。"""
        with self.db.connect() as connection:
            connection.execute("DELETE FROM app_settings WHERE key LIKE 'master_key_verified%'")
        findings = alerting.evaluate(self.db, now=self.now, disk_percent=10.0,
                                     certificate_days=90.0)
        self.assertEqual([f for f in findings if f["key"].startswith("master_key_copy")], [])

    def test_an_unknown_key_defaults_to_mailing(self):
        """A new check must be loud, not silently absent.

        The failure mode of an over-eager alert is an annoyed operator; the
        failure mode of a missing one is an incident nobody hears about. So the
        default is the loud direction, and quietening is always an explicit act.
        """
        self.assertEqual(alerting.tier_for("some_check_nobody_has_written_yet"),
                         alerting.TIER_MAIL)

    def test_the_three_tiers_land_where_they_were_meant_to(self):
        self.assertEqual(alerting.tier_for("mailbox_error:usr_1"), alerting.TIER_PANEL)
        self.assertEqual(alerting.tier_for("setup_stalled:usr_1"), alerting.TIER_DIGEST)
        self.assertEqual(alerting.tier_for("invite_failed:req_1"), alerting.TIER_DIGEST)
        for key in ("tls_cert", "disk", "backup_missing", "backup_stale", "offsite_failed",
                    "offsite_stale", "queue_backlog", "failed_reports", "mailbox_stale:usr_1"):
            self.assertEqual(alerting.tier_for(key), alerting.TIER_MAIL, key)

    # -- panel tier: recorded, never mailed --------------------------------

    def test_a_mailbox_error_never_mails(self):
        owner = self._user()
        self._polled(owner["mailbox_id"], error="IMAP 认证失败")
        result, sent = self._run()
        self.assertEqual(sent, [], "单账号收信失败不该发邮件——它已经是后台的一盏红灯")
        self.assertEqual(result["sent"], 0)

    def test_a_mailbox_error_is_still_recorded_and_shown(self):
        """Quiet is not the same as dropped: the console must keep showing it."""
        owner = self._user()
        self._polled(owner["mailbox_id"], error="IMAP 认证失败")
        self._run()
        rows = alerting.panel_rows(self.db.list_alert_states())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["key"], f"mailbox_error:{owner['user']['id']}")
        self.assertTrue(rows[0]["open"])
        self.assertEqual(rows[0]["tier"], alerting.TIER_PANEL)

    def test_a_quiet_finding_does_not_suppress_an_urgent_one(self):
        """The tiers are independent: one quiet finding must not quieten others."""
        owner = self._user()
        self._polled(owner["mailbox_id"], error="IMAP 认证失败")
        result, sent = self._run(disk=95.0)
        self.assertEqual(result["sent"], 1, "磁盘那条仍然要发")
        self.assertIn("磁盘", sent[0][3], "正文里要有磁盘那条")

    # -- digest tier -------------------------------------------------------

    def test_a_stalled_signup_is_not_re_mailed_within_the_hour(self):
        """The bug that made the 24-hour repeat meaningless.

        The detail used to carry `注册已 N 小时`, and `_should_send` re-mails a
        finding whose detail changed -- so a number that grew on its own re-sent
        on its own, hourly, and the configured daily repeat never once applied.
        """
        self._stalled("stuck@example.com")
        first, sent = self._run()
        self.assertEqual(first["sent"], 1, "第一次要发：汇总档也要让人知道")
        self._run(now=self.now + dt.timedelta(hours=1), sent=sent)
        self.assertEqual(len(sent), 1, "一小时之内不该再发第二封")

    def test_two_stalled_signups_are_one_mail(self):
        """The point of batching: three people is one e-mail, not three."""
        self._stalled("one@example.com")
        self._stalled("two@example.com")
        result, sent = self._run()
        self.assertEqual(len(sent), 1, "两个人都应该进同一封")
        self.assertEqual(result["sent"], 2, "但两件都要记进状态")

    def test_a_second_stalled_signup_waits_for_the_slot(self):
        """The slot belongs to the tier, so a newcomer waits for it."""
        self._stalled("first@example.com")
        self._run()
        sent: list = []
        self._stalled("second@example.com")
        self._run(now=self.now + dt.timedelta(hours=2), sent=sent)
        self.assertEqual(sent, [])
        self._run(now=self.now + dt.timedelta(seconds=alerting.ALERT_DIGEST_SECONDS + 60),
                  sent=sent)
        self.assertEqual(len(sent), 1, "窗口一开就补上")

    def test_the_digest_window_is_a_day(self):
        self.assertEqual(alerting.ALERT_DIGEST_SECONDS, 24 * 3600)

    # -- acknowledging -----------------------------------------------------

    def test_acknowledging_stops_the_mail(self):
        self._stalled("stuck@example.com")
        self._run()
        key = self.db.list_alert_states()[0]["key"]
        self.db.acknowledge_alert(key, self.now)
        result, sent = self._run(
            now=self.now + dt.timedelta(seconds=alerting.ALERT_DIGEST_SECONDS + 60))
        self.assertEqual(sent, [], "已知晓之后就不该再打扰")
        self.assertEqual(result["sent"], 0)

    def test_acknowledging_does_not_hide_it_from_the_panel(self):
        """Not a delete and not a close: the problem is still there."""
        self._stalled("stuck@example.com")
        self._run()
        key = self.db.list_alert_states()[0]["key"]
        self.db.acknowledge_alert(key, self.now)
        rows = alerting.panel_rows(self.db.list_alert_states())
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["open"], "还在，只是不吵了")
        self.assertTrue(rows[0]["acknowledged"])

    def test_a_problem_that_returns_after_clearing_mails_again(self):
        """The acknowledgment must not outlive the problem it was about.

        If it did, the *next* problem arriving under the same key would be
        silent -- which is how a known-issues list becomes a blindfold.
        """
        self._stalled("stuck@example.com")
        self._run()
        key = self.db.list_alert_states()[0]["key"]
        self.db.acknowledge_alert(key, self.now)
        self.db.clear_alert(key, self.now)
        self.assertIsNone(self.db.list_alert_states()[0]["acknowledged_at"],
                          "问题清掉时静音必须一起清掉")
        _, sent = self._run(now=self.now + dt.timedelta(hours=1))
        self.assertEqual(len(sent), 1, "它回来了，就该再说一次")

    def test_unacknowledging_works(self):
        self._stalled("stuck@example.com")
        self._run()
        key = self.db.list_alert_states()[0]["key"]
        self.db.acknowledge_alert(key, self.now)
        self.db.unacknowledge_alert(key)
        self.assertFalse(alerting.panel_rows(self.db.list_alert_states())[0]["acknowledged"])

    def test_acknowledging_something_that_is_not_there_is_an_error(self):
        with self.assertRaises(KeyError):
            self.db.acknowledge_alert("no_such_key", self.now)

    # -- the detail must not change on its own -----------------------------

    def test_the_stalled_detail_carries_no_self_updating_number(self):
        """Regression guard for the hourly re-send.

        Asserted against the *real* finding rather than a fixture, so that
        rewriting the wording is covered too.
        """
        self._stalled("stuck@example.com", age_hours=40)
        findings = alerting.evaluate(self.db, now=self.now, disk_percent=10.0,
                                     certificate_days=90.0)
        stalled = [item for item in findings if item["key"].startswith("setup_stalled:")]
        self.assertEqual(len(stalled), 1)
        detail = stalled[0]["detail"]
        self.assertNotIn("40", detail, "年龄数字不该出现在细节里：它会自己长大")
        later = alerting.evaluate(self.db, now=self.now + dt.timedelta(hours=1),
                                  disk_percent=10.0, certificate_days=90.0)
        self.assertEqual(
            [item for item in later if item["key"].startswith("setup_stalled:")][0]["detail"],
            detail, "一小时后文字必须一字不差")


class RunChecksTests(AlertingTestCase):
    def _healthy(self) -> dict:
        owner = self._user()
        self._polled(owner["mailbox_id"])
        return owner

    def test_a_healthy_system_sends_nothing(self):
        self._healthy()
        sent: list = []
        result = alerting.run_checks(self.db, self.secrets, now=self.now, disk=1.0,
                                     certificate_days=90.0,
                                     sender=lambda *args: sent.append(args) or ["boss@example.com"])
        self.assertEqual(result["sent"], 0)
        self.assertEqual(sent, [])

    def test_the_same_problem_is_mailed_once_then_suppressed(self):
        # A mail-tier finding: these rules are about the mailing, and
        # `mailbox_error` is console-only now (see TierTests below).
        self._healthy()
        sent: list = []

        def sender(*args):
            sent.append(args)
            return ["boss@example.com"]

        first = alerting.run_checks(self.db, self.secrets, now=self.now, disk=95.0,
                                    certificate_days=90.0, sender=sender)
        self.assertEqual(first["sent"], 1)
        second = alerting.run_checks(self.db, self.secrets,
                                     now=self.now + dt.timedelta(minutes=1), disk=95.0,
                                     certificate_days=90.0, sender=sender)
        self.assertEqual(second["sent"], 0)
        self.assertEqual(len(sent), 1)

    def test_a_persistent_problem_re_mails_after_the_window(self):
        self._healthy()
        sent: list = []
        sender = lambda *args: sent.append(args) or ["boss@example.com"]

        alerting.run_checks(self.db, self.secrets, now=self.now, disk=95.0,
                            certificate_days=90.0, sender=sender)
        later = self.now + dt.timedelta(seconds=alerting.ALERT_REPEAT_SECONDS + 60)
        alerting.run_checks(self.db, self.secrets, now=later, disk=95.0,
                            certificate_days=90.0, sender=sender)
        self.assertEqual(len(sent), 2)

    def test_clearing_a_problem_sends_exactly_one_recovery(self):
        self._healthy()
        sent: list = []
        sender = lambda *args: sent.append(args) or ["boss@example.com"]

        alerting.run_checks(self.db, self.secrets, now=self.now, disk=95.0,
                            certificate_days=90.0, sender=sender)
        recovery = alerting.run_checks(self.db, self.secrets, now=self.now + dt.timedelta(minutes=1),
                                       disk=10.0, certificate_days=90.0, sender=sender)
        self.assertEqual(recovery["sent"], 1)
        self.assertIn("已恢复", sent[-1][2])
        # And it is not repeated once the state is closed.
        again = alerting.run_checks(self.db, self.secrets, now=self.now + dt.timedelta(minutes=2),
                                    disk=10.0, certificate_days=90.0, sender=sender)
        self.assertEqual(again["sent"], 0)
        self.assertEqual(len(sent), 2)

    def test_a_quiet_finding_that_goes_away_is_still_marked_closed(self):
        """修好的旧账必须在面板上关掉，哪怕这一轮没有别的事要做。

        2026-09-18 实测的 bug：`mailbox_error:*` 是 **panel 档**（不写信、只上面板），
        而 `run_checks` 在「没有要发的信、没有要恢复的邮件档、没有要分析的」时**提前返回**，
        把最后那段 `clear_alert` 一起跳过了。于是收信恢复正常之后，巡检面板上那条
        「收信失败：某个 163 账号」一直挂着 `open=1`，看起来像还没修好 —— 直到某一轮
        恰好有别的事可做才被顺手关掉。用户原话就是「为什么巡检和 ai 运维还显示有问题」。
        """
        owner = self._user()
        self._polled(owner["mailbox_id"], error="IMAP 认证失败")
        alerting.run_checks(self.db, self.secrets, now=self.now, disk=1.0,
                            certificate_days=90.0, sender=lambda *a: ["boss@example.com"])
        self.assertTrue([r for r in self.db.list_alert_states() if r["open"]],
                        "先得有一条开着的发现项")

        # 修好了：邮箱不再报错。这一轮**没有任何**要发的信、要恢复的邮件档、
        # 也没有要分析的（这就是提前返回成立的那一轮）。
        self._polled(owner["mailbox_id"])
        self.assertEqual(alerting.run_checks(self.db, self.secrets,
                                             now=self.now + dt.timedelta(minutes=5),
                                             disk=1.0, certificate_days=90.0,
                                             sender=lambda *a: ["boss@example.com"])["sent"], 0)
        open_rows = [r for r in self.db.list_alert_states() if r["open"]]
        self.assertEqual(open_rows, [], "安静档的旧账也必须被关掉")

    def test_a_digest_finding_that_goes_away_is_marked_closed_too(self):
        """digest 档同理：恢复邮件只发给邮件档，但**关账**是每一档都要做的。"""
        owner = self._user()
        self.db.set_setting("alerts_digest_last_at", "")
        self._polled(owner["mailbox_id"], error="IMAP 认证失败")
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE alert_state SET open=1, first_seen_at=?, last_sent_at=? WHERE key=?",
                (self.now.isoformat(timespec="seconds"),) * 2
                + (f"mailbox_error:{owner['user']['id']}",)) if False else None
        alerting.run_checks(self.db, self.secrets, now=self.now, disk=1.0,
                            certificate_days=90.0, sender=lambda *a: ["boss@example.com"])
        self._polled(owner["mailbox_id"], error="")
        # 手工把这一行降级成 digest 档的 key：断言的是「关闭」这件事与档位无关。
        with self.db.connect() as connection:
            connection.execute("UPDATE alert_state SET key=? WHERE key=?",
                               ("setup_stalled:usr_x", f"mailbox_error:{owner['user']['id']}"))
        alerting.run_checks(self.db, self.secrets, now=self.now + dt.timedelta(minutes=5),
                            disk=1.0, certificate_days=90.0,
                            sender=lambda *a: ["boss@example.com"])
        self.assertEqual([r for r in self.db.list_alert_states() if r["open"]], [])

    def test_a_send_failure_leaves_the_state_untouched_so_it_retries(self):
        self._healthy()

        def broken(*args):
            raise RuntimeError("SMTP 挂了")

        result = alerting.run_checks(self.db, self.secrets, now=self.now, disk=95.0,
                                     certificate_days=90.0, sender=broken)
        self.assertEqual(result["sent"], 0)
        self.assertTrue(result["errors"])
        # No state was recorded, so the very next pass tries again.
        self.assertEqual(self.db.list_alert_states(), [])
        sent: list = []
        retry = alerting.run_checks(self.db, self.secrets, now=self.now + dt.timedelta(minutes=1),
                                    disk=95.0, certificate_days=90.0,
                                    sender=lambda *a: sent.append(a) or ["boss@example.com"])
        self.assertEqual(retry["sent"], 1)

    def test_state_survives_a_process_restart(self):
        """The whole reason state lives in SQLite: a redeploy must not replay
        every standing alert."""
        self._healthy()
        alerting.run_checks(self.db, self.secrets, now=self.now, disk=95.0, certificate_days=90.0,
                            sender=lambda *a: ["boss@example.com"])
        reopened = Database(pathlib.Path(self.work.name) / "pilot.sqlite3")
        reopened.initialize()
        sent: list = []
        result = alerting.run_checks(reopened, self.secrets, now=self.now + dt.timedelta(minutes=1),
                                     disk=95.0, certificate_days=90.0,
                                     sender=lambda *a: sent.append(a) or ["boss@example.com"])
        self.assertEqual(result["sent"], 0)
        self.assertEqual(sent, [])

    def test_the_kill_switch_silences_everything(self):
        owner = self._user()
        self._polled(owner["mailbox_id"], error="IMAP 认证失败")
        sent: list = []
        with mock.patch.object(alerting, "ALERTS_ENABLED", False):
            result = alerting.run_checks(self.db, self.secrets, now=self.now, disk=1.0,
                                         certificate_days=90.0,
                                         sender=lambda *a: sent.append(a) or ["x@y.z"])
        self.assertFalse(result["enabled"])
        self.assertEqual(sent, [])

    def test_an_evaluation_crash_is_contained(self):
        with mock.patch.object(alerting, "evaluate", side_effect=RuntimeError("db gone")):
            result = alerting.run_checks(self.db, self.secrets, now=self.now, disk=1.0,
                                         certificate_days=90.0,
                                         sender=lambda *a: ["x@y.z"])
        self.assertEqual(result["sent"], 0)
        self.assertTrue(result["errors"])


class RedactionTests(unittest.TestCase):
    def test_known_credential_values_are_removed(self):
        self.assertEqual(
            alerting.redact("key is hunter2hunter2 ok", {"hunter2hunter2"}),
            "key is *** ok")

    def test_inline_secret_assignments_are_removed(self):
        text = "INFE_PILOT_MASTER_KEY=abc123\nINFE_PILOT_DB=/var/lib/x.sqlite3\n"
        cleaned = alerting.redact(text)
        self.assertNotIn("abc123", cleaned)
        self.assertIn("INFE_PILOT_DB=/var/lib/x.sqlite3", cleaned)

    def test_short_values_are_not_treated_as_secrets(self):
        """Replacing a 3-character "secret" would corrupt unrelated text."""
        self.assertEqual(alerting.redact("a short value", {"abc"}), "a short value")

    def test_collect_secret_values_reads_only_secret_named_keys(self):
        with tempfile.TemporaryDirectory() as work:
            path = pathlib.Path(work) / "pilot.env"
            path.write_text(
                "INFE_PILOT_DB=/var/lib/cityu-mail-pilot/pilot.sqlite3\n"
                "INFE_PILOT_MASTER_KEY=topsecretvalue\n"
                "# comment\n"
                "INFE_PILOT_ORIGIN=https://example.test\n"
                "SOME_PASSWORD='quotedsecret'\n",
                encoding="utf-8")
            values = alerting.collect_secret_values(str(path))
        self.assertIn("topsecretvalue", values)
        self.assertIn("quotedsecret", values)
        self.assertNotIn("/var/lib/cityu-mail-pilot/pilot.sqlite3", values)
        self.assertNotIn("https://example.test", values)

    def test_collect_secret_values_tolerates_a_missing_file(self):
        self.assertEqual(alerting.collect_secret_values("/nonexistent/pilot.env"), set())


class RenderingTests(AlertingTestCase):
    def test_the_alert_mail_obeys_the_email_html_rules(self):
        """Invariant 5: 600 px single-column table, inline CSS, no script, no
        <style>, no @media, and no links other than https."""
        due = [{"key": "k", "severity": "critical", "title": "标题", "detail": "详情"}]
        recovered = [{"key": "r", "title": "已恢复项"}]
        rendered = alerting._render_html(due, recovered)
        self.assertIn('width="600"', rendered)
        self.assertNotIn("<script", rendered.lower())
        self.assertNotIn("<style", rendered.lower())
        self.assertNotIn("@media", rendered.lower())
        self.assertNotIn("<img", rendered.lower())
        self.assertNotIn("http://", rendered.lower())

    def test_html_escapes_operator_facing_text(self):
        due = [{"key": "k", "severity": "critical",
                "title": "<script>alert(1)</script>", "detail": "&<>"}]
        rendered = alerting._render_html(due, [])
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)


class SendAdminMailTests(AlertingTestCase):
    def test_it_sends_from_the_admins_own_mailbox(self):
        self._user("boss@example.com")
        captured: dict = {}

        def fake_send(config, password, subject, markdown, **kwargs):
            captured.update(config)
            captured["password"] = password
            captured["subject"] = subject

        with mock.patch.object(alerting.mailio, "send_report", fake_send):
            delivered = alerting.send_admin_mail(self.db, self.secrets, "主题", "正文")
        self.assertEqual(delivered, ["boss@example.com"])
        self.assertEqual(captured["email"], "box1@qq.com")
        self.assertEqual(captured["report_to"], "boss@example.com")
        self.assertEqual(captured["smtp_host"], "smtp.qq.com")
        self.assertEqual(captured["password"], "pw")

    def test_it_raises_rather_than_silently_dropping_when_nobody_can_send(self):
        self._user("boss@example.com", with_mailbox=False)
        with self.assertRaises(RuntimeError):
            alerting.send_admin_mail(self.db, self.secrets, "主题", "正文")

    def test_it_raises_when_no_admin_is_configured(self):
        with mock.patch.dict("os.environ", {"INFE_PILOT_ADMIN_EMAILS": ""}):
            with self.assertRaises(RuntimeError):
                alerting.send_admin_mail(self.db, self.secrets, "主题", "正文")


class AdminIdentityTests(unittest.TestCase):
    def test_admin_emails_are_lower_cased_and_trimmed(self):
        with mock.patch.dict("os.environ", {"INFE_PILOT_ADMIN_EMAILS": " A@B.com , c@d.com "}):
            self.assertEqual(alerting.admin_emails(), {"a@b.com", "c@d.com"})

    def test_the_web_surface_and_the_mailer_share_one_definition(self):
        from pilot_app import web
        with mock.patch.dict("os.environ", {"INFE_PILOT_ADMIN_EMAILS": "X@Y.com"}):
            self.assertEqual(web._admin_emails(), alerting.admin_emails())


class CertificateTests(unittest.TestCase):
    def _handshake(self, *, certificate=None, error=None):
        """Patch out the socket so the parsing and the two failure modes can be
        driven without a network or a real certificate."""
        tls = mock.MagicMock()
        tls.getpeercert.return_value = certificate
        tls.__enter__ = lambda self: tls
        tls.__exit__ = lambda *args: False
        context = mock.MagicMock()
        if error is not None:
            context.wrap_socket.side_effect = error
        else:
            context.wrap_socket.return_value = tls
        raw = mock.MagicMock()
        raw.__enter__ = lambda self: raw
        raw.__exit__ = lambda *args: False
        return mock.MagicMock(return_value=raw), context

    def _days(self, certificate=None, error=None, **kwargs):
        connection, context = self._handshake(certificate=certificate, error=error)
        with mock.patch.object(alerting.socket, "create_connection", connection), \
                mock.patch.object(alerting.ssl, "create_default_context", lambda *a, **k: context):
            return alerting.certificate_days_remaining(**kwargs)

    def test_a_valid_certificate_yields_days_remaining(self):
        """The bug this replaces: verification was disabled to read notAfter,
        but getpeercert() returns an empty dict for an unvalidated peer, so the
        reading was always None and the check could never fire."""
        not_after = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=88))
        days = self._days(certificate={"notAfter": not_after.strftime("%b %d %H:%M:%S %Y GMT")},
                          host="mail.example.test")
        self.assertIsNotNone(days)
        self.assertAlmostEqual(days, 88, delta=1)

    def test_an_expired_certificate_is_reported_as_negative(self):
        not_after = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2))
        days = self._days(certificate={"notAfter": not_after.strftime("%b %d %H:%M:%S %Y GMT")},
                          host="mail.example.test")
        self.assertLess(days, 0)

    def test_a_failed_verification_is_reported_not_swallowed(self):
        error = alerting.ssl.SSLCertVerificationError("certificate has expired")
        days = self._days(error=error, host="mail.example.test")
        self.assertIsNotNone(days)
        self.assertLess(days, 0)

    def test_a_certificate_without_notafter_is_unknown(self):
        self.assertIsNone(self._days(certificate={}, host="mail.example.test"))

    def test_an_unreachable_host_reports_unknown_rather_than_expiring(self):
        days = alerting.certificate_days_remaining(host="127.0.0.1", port=1, timeout=2.0)
        self.assertIsNone(days)

    def test_no_configured_host_means_no_check(self):
        self.assertIsNone(alerting.certificate_days_remaining(host=""))

    def test_the_host_falls_back_to_the_configured_origin(self):
        with mock.patch.dict("os.environ", {"INFE_PILOT_ORIGIN": "https://mail.example.test/",
                                            "INFE_PILOT_TLS_HOST": ""}):
            self.assertEqual(alerting._tls_host_from_environment(), "mail.example.test")
        with mock.patch.dict("os.environ", {"INFE_PILOT_ORIGIN": "https://mail.example.test/",
                                            "INFE_PILOT_TLS_HOST": "other.test"}):
            self.assertEqual(alerting._tls_host_from_environment(), "other.test")


if __name__ == "__main__":
    unittest.main()
