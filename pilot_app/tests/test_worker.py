"""How the worker logs a failure -- which is a decision about what a log is for.

A log is read to answer one question: *what is wrong that I did not already
know?* A mailbox whose owner typed the wrong auth code is not that. The owner is
told inside the app, the admin panel shows it as a red light, and the sentinel
reports `mailbox_error`; the worker re-discovering it every poll cycle adds
nothing except volume.

On 2026-09-15 one such account produced ~750 lines of identical traceback per
day in `journalctl -u cityu-mail-pilot-worker`. That is not merely untidy: the
whole point of a traceback is that it is rare enough to be worth reading, and a
familiar wall of it is how a real one gets scrolled past.

So the rule is narrow and structural, not a level tweak: a `MailError` means the
mail server rejected *this account* -- one line. Anything else is a failure
nobody anticipated, and it keeps its traceback.

The rule has four consumers and this file checks all four. That is deliberate:
v0.59.1 is the round where the same "timestamp is not success" rule had been
taught to the panel and the sentinel and *not* to the health card, and the bug
survived every test. Teaching the poller alone would be that mistake again.
"""

import logging
import secrets
import unittest
from unittest import mock

from pilot_app import mailio, worker
from pilot_app.security import SecretBox
from pilot_app.service import PilotService


class PollFailureLoggingTests(unittest.TestCase):
    def _poll_raising(self, exc: Exception) -> list[logging.LogRecord]:
        service = mock.Mock()
        service.db.active_mailboxes.return_value = [{"id": "mbx_1"}]
        service.poll_mailbox.side_effect = exc
        with self.assertLogs(level=logging.WARNING) as captured:
            worker.poll_all(service)
        return captured.records

    def test_a_rejected_account_is_one_quiet_line(self):
        """The observed case: a wrong auth code, every cycle."""
        records = self._poll_raising(
            mailio.MailError("授权码（应用专用密码）不正确或已失效，请在邮箱设置里重新生成一个再试。"))
        self.assertEqual(len(records), 1, "一个已知的账号问题不该产生多条日志")
        self.assertEqual(records[0].levelno, logging.WARNING)
        self.assertIsNone(records[0].exc_info, "这里是堆栈唯一真正多余的地方")
        self.assertIn("mbx_1", records[0].getMessage())
        self.assertIn("授权码", records[0].getMessage(), "原因要留在那一行里")

    def test_an_unexpected_failure_keeps_its_traceback(self):
        """The other half. Downgrading everything would be the same bug mirrored:
        a genuine bug would become one line nobody can act on."""
        records = self._poll_raising(AttributeError("boom"))
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].levelno, logging.ERROR)
        self.assertIsNotNone(records[0].exc_info, "没想到的失败必须留堆栈")
        traceback_text = logging.Formatter().formatException(records[0].exc_info)
        self.assertIn("AttributeError", traceback_text)

    def test_the_failed_mailbox_is_still_recorded_and_reported(self):
        """Downgrading the log line must not downgrade the bookkeeping: the
        panel, the red light and the backoff all read these values."""
        service = mock.Mock()
        service.db.active_mailboxes.return_value = [{"id": "mbx_1"}]
        service.poll_mailbox.side_effect = mailio.MailError("授权码不正确。")
        with self.assertLogs(level=logging.WARNING):
            result = worker.poll_all(service)
        self.assertEqual(result["failed"], ["mbx_1"])
        self.assertEqual(len(result["errors"]), 1)
        recorded = service.db.update_mailbox_poll.call_args.kwargs
        self.assertEqual(recorded["error"], "授权码不正确。")


class EveryConsumerIsTaughtTests(unittest.TestCase):
    """Each per-cycle path that can see a `MailError` must use the same rule."""

    def setUp(self):
        self.db = mock.MagicMock()
        self.service = PilotService(self.db, SecretBox(secrets.token_bytes(32)))

    def test_the_thread_pool_queue_path(self):
        service = mock.Mock()
        service.db.due_messages.return_value = [{"id": "msg_1", "user_id": "usr_1"}]
        service.process_message.side_effect = mailio.MailError("邮箱配置已不存在。")
        with self.assertLogs(level=logging.WARNING) as captured:
            worker.process_due(service)
        self.assertEqual(len(captured.records), 1)
        self.assertEqual(captured.records[0].levelno, logging.WARNING)
        self.assertIsNone(captured.records[0].exc_info)

    def test_the_single_threaded_poll_reference_path(self):
        self.db.active_mailboxes.return_value = [{"id": "mbx_1"}]
        self.service.poll_mailbox = mock.Mock(side_effect=mailio.MailError("授权码不正确。"))
        with self.assertLogs(level=logging.WARNING) as captured:
            self.service.poll_all()
        self.assertEqual(len(captured.records), 1)
        self.assertEqual(captured.records[0].levelno, logging.WARNING)
        self.assertIsNone(captured.records[0].exc_info)

    def test_the_message_processing_path_itself(self):
        """`service.process_message` swallows the exception and returns False, so
        the worker's own `except` above never sees the common case -- if only the
        worker were fixed, production would look unchanged."""
        self.db.mark_message_processing.return_value = True
        self.db.get_mailbox.return_value = None  # -> MailError("邮箱配置已不存在。")
        with self.assertLogs(level=logging.WARNING) as captured:
            self.assertFalse(self.service.process_message(
                {"id": "msg_1", "user_id": "usr_1", "attempts": 0}))
        self.assertEqual(len(captured.records), 1)
        self.assertEqual(captured.records[0].levelno, logging.WARNING)
        self.assertIsNone(captured.records[0].exc_info)

    def test_an_unexpected_failure_on_the_queue_path_is_not_swallowed_quietly(self):
        """The rule must not become "the queue never logs loudly"."""
        self.db.mark_message_processing.return_value = True
        self.db.get_mailbox.side_effect = RuntimeError("db is on fire")
        with self.assertLogs(level=logging.WARNING) as captured:
            self.assertFalse(self.service.process_message(
                {"id": "msg_1", "user_id": "usr_1", "attempts": 0}))
        record = captured.records[0]
        self.assertEqual(record.levelno, logging.ERROR)
        self.assertIsNotNone(record.exc_info)


class PollBudgetTests(unittest.TestCase):
    """「轮询间隔」这一个旋钮的账，必须能被算出来（`manage poll-interval` 的心脏）。

    2026-09-24 为了 1500 个邮箱的规模，生产把这一个值从 60 秒改成 300 秒 —— 而当时真实
    规模是 15 个邮箱。**为 80 倍于当时的规模提前付的代价，账是用户在日常里付的**：
    2026-09-26 用户报「从收到转发邮件到收到处理好的邮件太久了」，实测就是「等下一次轮询」。

    这几条钉的是那两个式子本身（`docs/scale-1500-2026-09-24.md` §1.4），
    以及那条唯一的硬判据：**一轮要跑得完**（`round_seconds ≤ interval`）。跑不完不会报错，
    它只是把实际间隔悄悄拉成一轮的真实耗时。
    """

    def test_a_small_fleet_polls_every_minute_with_room_to_spare(self):
        budget = worker.poll_budget(20, interval=60, workers=4, cost_seconds=2)
        self.assertEqual(budget["rounds"], 5)          # ceil(20 / 4)
        self.assertEqual(budget["round_seconds"], 10)
        self.assertTrue(budget["fits"])
        self.assertAlmostEqual(budget["logins_per_second"], 20 / 60, places=6)
        self.assertEqual(budget["logins_per_day"], 20 / 60 * 86400)   # 28 800
        self.assertEqual(budget["median_delay_seconds"], 30.0)
        self.assertEqual(budget["worst_delay_seconds"], 60.0)

    def test_the_fifteen_hundred_mailbox_target_is_why_the_interval_went_up(self):
        """60 秒 × 1500 个邮箱跑不完 —— 这就是 2026-09-24 那次降频的理由，不是拍脑袋。"""
        tight = worker.poll_budget(worker.SCALE_TARGET_MAILBOXES, interval=60, workers=4,
                                   cost_seconds=2)
        self.assertFalse(tight["fits"])
        self.assertEqual(tight["round_seconds"], 750)   # ceil(1500 / 4) × 2
        # 同一个目标换成 16 个线程 + 300 秒就装得下 —— 也就是当初那两个数字的来历。
        roomy = worker.poll_budget(worker.SCALE_TARGET_MAILBOXES, interval=300, workers=16,
                                   cost_seconds=2)
        self.assertTrue(roomy["fits"])
        self.assertEqual(roomy["round_seconds"], 188)   # ceil(1500 / 16) × 2

    def test_an_empty_fleet_does_not_divide_by_zero(self):
        budget = worker.poll_budget(0, interval=60, workers=4)
        self.assertTrue(budget["fits"])
        self.assertEqual(budget["logins_per_day"], 0)
        self.assertEqual(budget["median_delay_seconds"], 30.0)

    def test_a_nonsense_input_is_clamped_rather_than_raising(self):
        """操作者手滑（0 个线程、0 秒间隔）不该让诊断命令自己崩掉。"""
        budget = worker.poll_budget(5, interval=0, workers=0, cost_seconds=0)
        self.assertEqual(budget["workers"], 1)
        self.assertEqual(budget["interval"], 1)
        self.assertEqual(budget["cost_seconds"], 1)

    def test_the_budget_describes_the_interval_the_poller_really_uses(self):
        """算的是**轮询器真会用的那个间隔**，不是另写一份常量。

        `poll_interval_for` 是唯一的事实源（web 的新鲜度窗口、告警阈值都从它取）；
        这条把 `poll_budget` 的入参和它钉在一起，防止将来有人在这里另起一个数。
        """
        with mock.patch.object(worker, "POLL_SECONDS", 60):
            self.assertEqual(worker.poll_interval_for({"imap_host": "imap.qq.com"}), 60)
            self.assertEqual(
                worker.poll_budget(20, interval=worker.POLL_SECONDS,
                                   workers=worker.POLL_WORKERS)["interval"], 60)
        gmail = {"imap_host": "imap.gmail.com"}
        self.assertEqual(worker.poll_interval_for(gmail), mailio.GMAIL_MIN_POLL_SECONDS)
        self.assertGreater(worker.poll_interval_for(gmail), 60)


if __name__ == "__main__":
    unittest.main()
