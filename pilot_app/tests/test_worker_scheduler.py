"""Tests for the worker scheduler: two loops, fair queueing.

The scheduler used to run polling and report generation as two phases of one
loop, so a slow generation batch stopped mail from being noticed at all. These
tests pin the properties that fix depends on:

* the poller keeps polling while reports are being generated;
* a user's own messages are analysed one at a time, in arrival order (they share
  that user's API key, and their reports should read in the order they arrived);
* one user with a large backlog cannot starve another user with a single mail.
"""

from __future__ import annotations

import pathlib
import tempfile
import threading
import time
import unittest
from unittest import mock

from pilot_app import mailio, worker
from pilot_app.database import Database, utc_now
from pilot_app.security import SecretBox, hash_password, token_hash
from pilot_app.service import PilotService


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.db = Database(pathlib.Path(self.work.name) / "pilot.sqlite3")
        self.db.initialize()
        self.service = PilotService(self.db, SecretBox(b"7" * 32))
        self.users: list[dict] = []

    def tearDown(self):
        self.work.cleanup()

    def _user(self, index: int, *, imap_host: str = "imap.qq.com") -> dict:
        invite = self.db.create_invite(f"u{index}", 1)
        user = self.db.create_user(f"u{index}@example.com",
                                   hash_password("a-long-enough-password"), token_hash(invite))
        self.db.upsert_profile(user["id"], {
            "school_email": f"s{index}@my.cityu.edu.hk", "major": "通信", "year_of_study": "大二",
            "courses": [], "interests": [], "career_goals": [], "focus_topics": [],
            "less_interested": [], "custom_instructions": "", "language": "bilingual",
            "timezone": "Asia/Hong_Kong", "immediate_enabled": True,
            "daily_enabled": False, "daily_time": "22:00",
        })
        mailbox_id = self.db.upsert_mailbox(user["id"], {
            "email": f"box{index}@qq.com", "report_to": f"owner{index}@outlook.com",
            "imap_host": imap_host, "imap_port": 993, "smtp_host": "smtp.qq.com",
            "smtp_port": 465,
            "encrypted_password": self.service.secrets.encrypt(
                "pw", context=f"mailbox:{user['id']}"),
        })
        owner = {"user": user, "mailbox_id": mailbox_id}
        self.users.append(owner)
        return owner

    def _queue(self, owner: dict, count: int, prefix: str) -> list[str]:
        """Insert ``count`` due messages for one user, oldest first."""
        ids = []
        for index in range(count):
            message_id = self.db.insert_message(
                owner["user"]["id"], owner["mailbox_id"], "1", 100 + index,
                {"subject": f"{prefix} {index}", "sender_name": "teacher",
                 "sender_address": "teacher@cityu.edu.hk", "received": utc_now(),
                 "body": f"body {index}".encode()},
            )
            ids.append(message_id)
        return ids

    def _due_ids(self) -> list[str]:
        return [row["id"] for row in self.db.due_messages(500)]

    # -- the decoupling -----------------------------------------------------

    def test_polling_continues_while_reports_are_being_generated(self):
        """With one loop this fails: the poller could only run between batches."""
        self._user(1)
        polls: list[float] = []

        def fake_poll(mailbox):
            polls.append(time.monotonic())
            return 0

        with mock.patch.object(worker, "POLL_SECONDS", 1), \
                mock.patch.object(self.service, "poll_mailbox", side_effect=fake_poll), \
                mock.patch.object(self.service, "process_message",
                                  side_effect=lambda message: time.sleep(0.35) or True):
            stop = threading.Event()
            worker._start_poller(self.service, stop)
            try:
                self._queue(self.users[0], 6, "slow")  # 6 messages, USER_BATCH apart
                with mock.patch.object(worker, "USER_BATCH", 6), \
                        mock.patch.object(worker, "REPORT_WORKERS", 1):
                    started = time.monotonic()
                    worker.process_due(self.service)
                    elapsed = time.monotonic() - started
            finally:
                stop.set()
            self.assertGreater(elapsed, 1.0, "这一轮生成应当耗时超过一个轮询周期")
            during = [stamp for stamp in polls if started <= stamp <= started + elapsed]
            self.assertGreaterEqual(len(during), 1,
                                    "生成报告期间轮询必须继续（旧实现这里会是 0 次）")

    # -- fair, ordered queueing --------------------------------------------

    def test_each_user_is_processed_one_message_at_a_time_in_order(self):
        first = self._user(1)
        second = self._user(2)
        self._queue(first, 3, "a")
        self._queue(second, 3, "b")
        order: list[tuple[str, str]] = []
        in_flight: dict[str, int] = {}
        peak: dict[str, int] = {}

        def fake_process(message):
            user = message["user_id"]
            in_flight[user] = in_flight.get(user, 0) + 1
            peak[user] = max(peak.get(user, 0), in_flight[user])
            time.sleep(0.05)
            order.append((user, message["subject"]))
            in_flight[user] -= 1
            return True

        with mock.patch.object(self.service, "process_message", side_effect=fake_process), \
                mock.patch.object(worker, "USER_BATCH", 3), \
                mock.patch.object(worker, "REPORT_WORKERS", 4):
            result = worker.process_due(self.service)

        self.assertEqual(result["sent"], 6)
        self.assertEqual(result["users"], 2)
        for user_id, high in peak.items():
            self.assertEqual(high, 1, f"{user_id} 同一用户的报告不能并发生成")
        for user_id in (first["user"]["id"], second["user"]["id"]):
            subjects = [subject for owner, subject in order if owner == user_id]
            self.assertEqual(subjects, sorted(subjects), "同一用户的报告应按到达顺序生成")

    def test_one_users_burst_cannot_starve_another(self):
        burst = self._user(1)
        single = self._user(2)
        self._queue(burst, 12, "burst")
        self._queue(single, 1, "one")
        seen: list[str] = []

        with mock.patch.object(self.service, "process_message",
                               side_effect=lambda message: seen.append(message["user_id"]) or True), \
                mock.patch.object(worker, "USER_BATCH", 3), \
                mock.patch.object(worker, "REPORT_WORKERS", 2):
            result = worker.process_due(self.service)

        self.assertEqual(result["sent"], 4, "这一轮应当只处理积压用户的 3 封 + 另一用户的 1 封")
        self.assertIn(single["user"]["id"], seen, "单个邮件的用户不能被积压用户饿死")
        self.assertLessEqual(seen.count(burst["user"]["id"]), 3)
        # 13 were due and only 4 were taken, so the next pass continues the
        # backlog instead of the pool being monopolised by it. (A real
        # process_message also clears the row; here it is mocked, so the queue
        # is checked through the counts instead.)
        self.assertEqual(result["queued"], 13)
        self.assertEqual(seen.count(single["user"]["id"]), 1)

    def test_an_empty_queue_is_a_no_op(self):
        self._user(1)
        with mock.patch.object(self.service, "process_message") as process:
            result = worker.process_due(self.service)
        process.assert_not_called()
        self.assertEqual(result["queued"], 0)

    # -- one pass for --once and tests -------------------------------------

    def test_cycle_reports_every_phase(self):
        owner = self._user(1)
        self._queue(owner, 1, "c")
        with mock.patch.object(self.service, "poll_mailbox", return_value=2), \
                mock.patch.object(self.service, "process_message", return_value=True):
            result = worker.cycle(self.service)
        self.assertEqual(result["ingested"], 2)
        self.assertEqual(result["sent"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["errors"], [])


class PollingIntervalTests(unittest.TestCase):
    """One interval for every provider let the least tolerant set the rate.

    Gmail documents both the rule ("We recommend once every 15 minutes") and the
    penalty ("the account is temporarily suspended", up to 24 hours). Polling it
    every 60 seconds was 15x that, and the failure mode is the user's mailbox
    going dark rather than a merely late report.
    """

    def test_gmail_gets_the_providers_floor_and_nobody_else_does(self):
        with mock.patch.object(worker, "POLL_SECONDS", 60), \
                mock.patch.object(mailio, "GMAIL_MIN_POLL_SECONDS", 900):
            self.assertEqual(worker.poll_interval_for({"imap_host": "imap.gmail.com"}), 900)
            self.assertEqual(worker.poll_interval_for({"imap_host": "imap.googlemail.com"}), 900)
            self.assertEqual(worker.poll_interval_for({"imap_host": "imap.qq.com"}), 60)
            self.assertEqual(worker.poll_interval_for({"imap_host": "imap.163.com"}), 60)
            # An unrecognised host must fall back to the faster base interval:
            # guessing "slow" would silently delay mail for every new provider.
            self.assertEqual(worker.poll_interval_for({"imap_host": "mail.example.com"}), 60)
            self.assertEqual(worker.poll_interval_for({}), 60)

    def test_an_operator_can_raise_the_base_interval_past_the_floor(self):
        with mock.patch.object(worker, "POLL_SECONDS", 1800), \
                mock.patch.object(mailio, "GMAIL_MIN_POLL_SECONDS", 900):
            self.assertEqual(worker.poll_interval_for({"imap_host": "imap.gmail.com"}), 1800)
            self.assertEqual(worker.poll_interval_for({"imap_host": "imap.qq.com"}), 1800)

    def test_a_failing_mailbox_backs_off_and_recovers(self):
        with mock.patch.object(worker, "POLL_SECONDS", 60), \
                mock.patch.object(mailio, "GMAIL_MIN_POLL_SECONDS", 900), \
                mock.patch.object(worker, "MAX_POLL_BACKOFF_SECONDS", 3600), \
                mock.patch.object(worker, "MIN_RETRY_BACKOFF_SECONDS", 600):
            qq = {"imap_host": "imap.qq.com"}
            self.assertEqual(worker.next_poll_delay(qq, 0), 60, "首次失败前不打退避")
            self.assertEqual(worker.next_poll_delay(qq, 1), 600, "第一次失败就至少 10 分钟")
            self.assertEqual(worker.next_poll_delay(qq, 2), 1200)
            self.assertEqual(worker.next_poll_delay(qq, 10), 3600, "必须有上限")
            # Backing off must never make a mailbox poll faster than its floor.
            gmail = {"imap_host": "imap.gmail.com"}
            for failures in (0, 1, 2, 20):
                self.assertGreaterEqual(worker.next_poll_delay(gmail, failures), 900)

    def test_the_shipped_retry_floor_is_at_least_ten_minutes(self):
        """**默认值本身也要钉住**：机制对、但默认被调回 60 秒，等于没修。

        下面那条测的是「下限不跟间隔走」，而它自己 `mock.patch` 了那个常量 ——
        所以它证明不了出厂值是多少。这条补上：生产那个数至少是 10 分钟。
        """
        self.assertGreaterEqual(worker.MIN_RETRY_BACKOFF_SECONDS, 600)

    def test_the_retry_floor_does_not_follow_the_poll_interval_down(self):
        """**间隔是给健康邮箱调的，退避起点不是**（2026-09-26 踩到的那条）。

        2026-09-26 把轮询间隔从 300 秒调到 60 秒之后，一个「授权码被拒」的邮箱从
        **每 10 分钟**一次重试变成**每 2 分钟**一次 —— 实测那 3 个坏邮箱一小时贡献
        **33 次失败登录**，而供应商风控最敏感的就是这个形状（QQ 官方点名的
        「脚本 / 批量 / 频繁」）。这条钉住：**间隔调到多小，失败重试都不会跟着变小**。
        """
        qq = {"imap_host": "imap.qq.com"}
        with mock.patch.object(worker, "MIN_RETRY_BACKOFF_SECONDS", 600):
            for interval in (60, 120, 300, 900):
                with mock.patch.object(worker, "POLL_SECONDS", interval):
                    self.assertGreaterEqual(
                        worker.next_poll_delay(qq, 1), 600,
                        f"间隔 {interval} 秒时，第一次失败的重试间隔仍不能低于 10 分钟")

    def test_the_stale_threshold_follows_the_providers_interval(self):
        """Alerting and polling must agree, or a healthy Gmail mailbox would be
        reported as stalled on every single pass."""
        from pilot_app import alerting
        with mock.patch.object(alerting, "ALERT_STALE_MINUTES", 15), \
                mock.patch.object(mailio, "GMAIL_MIN_POLL_SECONDS", 900):
            qq = alerting.stale_after_for({"imap_host": "imap.qq.com"})
            gmail = alerting.stale_after_for({"imap_host": "imap.gmail.com"})
        self.assertEqual(qq.total_seconds(), 15 * 60)
        self.assertEqual(gmail.total_seconds(), 45 * 60)
        self.assertGreater(gmail.total_seconds(), 900,
                           "阈值必须大于该邮箱自身的轮询间隔，否则每轮都误报")

    def test_a_scheduler_polls_each_mailbox_on_its_own_clock(self):
        work = tempfile.TemporaryDirectory()
        self.addCleanup(work.cleanup)
        db = Database(pathlib.Path(work.name) / "pilot.sqlite3")
        db.initialize()
        service = PilotService(db, SecretBox(b"7" * 32))
        self._make(service, db, "qq", "imap.qq.com")
        self._make(service, db, "gm", "imap.gmail.com")

        polls: list = []

        def fake_poll(mailbox):
            polls.append(str(mailbox["imap_host"]))
            return 0

        stop = threading.Event()
        with mock.patch.object(worker, "POLL_SECONDS", 1), \
                mock.patch.object(worker, "POLL_TICK_SECONDS", 1), \
                mock.patch.object(mailio, "GMAIL_MIN_POLL_SECONDS", 30), \
                mock.patch.object(service, "poll_mailbox", side_effect=fake_poll):
            worker._start_poller(service, stop)
            time.sleep(3.2)
            stop.set()
        quick = polls.count("imap.qq.com")
        slow = polls.count("imap.gmail.com")
        self.assertGreaterEqual(quick, 2, f"QQ 应按 1 秒节奏被轮询（实际 {quick} 次）")
        self.assertLessEqual(slow, 1, f"Gmail 不应跟着 QQ 的节奏跑（实际 {slow} 次）")

    @staticmethod
    def _make(service, db, index: str, imap_host: str) -> None:
        invite = db.create_invite(index, 1)
        user = db.create_user(f"{index}@example.com", hash_password("a-long-enough-password"),
                              token_hash(invite))
        db.upsert_profile(user["id"], {
            "school_email": "student@my.cityu.edu.hk", "major": "通信", "year_of_study": "大二",
            "courses": [], "interests": [], "career_goals": [], "focus_topics": [],
            "less_interested": [], "custom_instructions": "", "language": "bilingual",
            "timezone": "Asia/Hong_Kong", "immediate_enabled": True,
            "daily_enabled": False, "daily_time": "22:00",
        })
        db.upsert_mailbox(user["id"], {
            "email": f"{index}@qq.com", "report_to": f"{index}@outlook.com",
            "imap_host": imap_host, "imap_port": 993, "smtp_host": "smtp.qq.com",
            "smtp_port": 465,
            "encrypted_password": service.secrets.encrypt("pw", context=f"mailbox:{user['id']}"),
        })


class ConfigTests(unittest.TestCase):
    def test_environment_values_are_clamped_not_trusted(self):
        cases = [
            ("INFE_PILOT_POLL_WORKERS", "999", 16),
            ("INFE_PILOT_POLL_WORKERS", "0", 1),
            ("INFE_PILOT_REPORT_WORKERS", "-3", 1),
            ("INFE_PILOT_USER_BATCH", "abc", 3),
            ("INFE_PILOT_POLL_SECONDS", "1", 15),
        ]
        for name, value, expected in cases:
            with mock.patch.dict("os.environ", {name: value}):
                fresh = {"INFE_PILOT_POLL_WORKERS": ("INFE_PILOT_POLL_WORKERS", 4, 1, 16),
                         "INFE_PILOT_REPORT_WORKERS": ("INFE_PILOT_REPORT_WORKERS", 6, 1, 32),
                         "INFE_PILOT_USER_BATCH": ("INFE_PILOT_USER_BATCH", 3, 1, 50),
                         "INFE_PILOT_POLL_SECONDS": ("INFE_PILOT_POLL_SECONDS", 60, 15, 3600)}[name]
                self.assertEqual(worker._int_env(*fresh), expected, f"{name}={value}")


if __name__ == "__main__":
    unittest.main()
