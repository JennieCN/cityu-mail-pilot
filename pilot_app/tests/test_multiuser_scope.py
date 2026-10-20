"""Scope guarantee: the recent behaviour is per-user, not per-account.

A single-account pilot cannot tell the difference between "the filter works"
and "the filter works for the one account that happens to exist". These tests
run two independent users through one real worker cycle and assert that every
user gets the same treatment, and that nothing crosses between them.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pilot_app import mailio, providers, worker
from pilot_app.database import Database
from pilot_app.security import SecretBox, hash_password, token_hash
from pilot_app import service as service_mod
from pilot_app.service import PilotService


def eml(sender: str, subject: str, body: str, message_id: str) -> bytes:
    return (
        f"From: {sender}\nTo: me@example.com\nSubject: {subject}\n"
        f"Date: Sun, 13 Sep 2026 09:20:55 +0000\nMessage-ID: <{message_id}>\n"
        f"Content-Type: text/plain; charset=utf-8\n\n{body}\n"
    ).encode()


CITYU_A = eml("teacher@cityu.edu.hk", "Assignment 2 deadline extended",
              "Please submit by Friday 23:59 on Canvas.", "a-cityu")
PROMO_A = eml("hello@mail.grammarly.com", "50% off Grammarly Pro",
              "Limited time offer. Unsubscribe here.", "a-promo")
CITYU_B = eml("library@cityu.edu.hk", "图书馆系统维护通知", "本周六系统维护。", "b-cityu")
PROMO_B = eml("no-reply@fairwood.com.hk", "月滿中秋盆菜會員獨家優惠", "限時優惠，退訂請按此。", "b-promo")


BRIEF_TEXT = ("## 1. 重要程度与一句话结论\n- 等级：高\n- 结论：需要处理。\n"
              "## 2. 必须采取的行动与截止时间\n- 周五 23:59 前完成\n"
              "## 3. 邮件内容要点\n- 入口：系统")
FULL_TEXT = ("## 1. 重要程度与一句话结论\n- 等级：高\n- 结论：需要处理。\n"
             "## 5. 联网搜索后的建议与来源\n- 本次未取得可验证来源。\n"
             "## 6. 风险、未知与推测\n- 事实：来自原邮件。\n"
             "## 7. English summary\nHandle it by Friday.")


class MultiUserScopeTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.work.name) / "pilot.sqlite3")
        self.db.initialize()
        self.box = SecretBox(b"7" * 32)
        self.service = PilotService(self.db, self.box)
        self.users = [self._build(index) for index in (1, 2)]

    def tearDown(self):
        self.work.cleanup()

    def _build(self, index: int) -> dict:
        invite = self.db.create_invite(f"user-{index}", 1)
        user = self.db.create_user(f"user{index}@example.com",
                                   hash_password("a-long-enough-password"), token_hash(invite))
        self.db.upsert_profile(user["id"], {
            "school_email": f"s{index}@my.cityu.edu.hk", "major": f"专业{index}",
            "year_of_study": "大二", "courses": [], "interests": [], "career_goals": [],
            "focus_topics": [], "less_interested": [], "custom_instructions": "",
            "language": "bilingual", "timezone": "Asia/Hong_Kong", "immediate_enabled": True,
            # The digest is deliberately off here: "is it due?" depends on the
            # wall clock (>= daily_time in the user's zone), so leaving it on
            # made this file pass all afternoon and fail after 22:00 HKT, when
            # two extra digest emails appeared in the mocked outbox. These tests
            # are about the per-user immediate path; the digest has its own
            # tests.
            "daily_enabled": False, "daily_time": "22:00",
        })
        mailbox_id = self.db.upsert_mailbox(user["id"], {
            "email": f"box{index}@qq.com", "report_to": f"owner{index}@outlook.com",
            "imap_host": "imap.qq.com", "imap_port": 993, "smtp_host": "smtp.qq.com", "smtp_port": 465,
            "encrypted_password": self.box.encrypt("pw", context=f"mailbox:{user['id']}"),
        })
        self.db.upsert_connection(user["id"], {
            "kind": "model", "provider": "deepseek", "model": f"model-{index}", "base_url": "",
            "encrypted_api_key": self.box.encrypt("k", context=f"connection:{user['id']}:model"),
            "config_json": "{}", "enabled": True,
        })
        return {"user": user, "mailbox_id": mailbox_id, "report_to": f"owner{index}@outlook.com"}

    def _run_cycle(self):
        by_mailbox = {
            self.users[0]["mailbox_id"]: (CITYU_A, PROMO_A),
            self.users[1]["mailbox_id"]: (CITYU_B, PROMO_B),
        }

        def fake_fetch(config, password, **kwargs):
            first, second = by_mailbox[config["id"]]
            return (f"uidv-{config['id']}", [(1, mailio.normalize_message(first)),
                                             (2, mailio.normalize_message(second))], 2)

        sent: list[dict] = []

        def fake_model(**kwargs):
            brief = "简短即时摘要" in kwargs.get("prompt", "")
            return providers.Generation(BRIEF_TEXT if brief else FULL_TEXT, [], "none")

        def record(config, password, subject, markdown, **kwargs):
            body = kwargs.get("text_body", "")
            sent.append({"to": config["report_to"], "subject": subject,
                         "is_brief": "精简即时摘要" in body,
                         "has_full_sections": "【联网搜索后的建议】" in body})

        with mock.patch.object(service_mod, "ALLOWED_SENDER_DOMAINS", ("cityu.edu.hk",)), \
                mock.patch.object(service_mod, "BRIEF_FIRST", True), \
                mock.patch.object(service_mod, "ALERT_ON_ARRIVAL", False), \
                mock.patch("pilot_app.service.mailio.fetch_new_messages", side_effect=fake_fetch), \
                mock.patch("pilot_app.service.providers.generate", side_effect=fake_model), \
                mock.patch("pilot_app.service.mailio.send_report", side_effect=record):
            result = worker.cycle(self.service)
        return result, sent

    def test_every_active_user_is_polled_not_only_the_first(self):
        result, _ = self._run_cycle()
        with self.db.connect() as connection:
            owners = {row[0] for row in connection.execute(
                "SELECT DISTINCT u.email FROM messages m JOIN users u ON u.id=m.user_id")}
        self.assertEqual(owners, {"user1@example.com", "user2@example.com"})
        # Two CityU mails processed in one cycle; personal mail never queued.
        self.assertEqual(result["ingested"], 2)
        self.assertEqual(result["failed"], 0)

    def test_sender_filter_applies_to_every_user(self):
        self._run_cycle()
        with self.db.connect() as connection:
            rows = list(connection.execute(
                """SELECT u.email, m.sender_address, m.status, m.skip_reason
                   FROM messages m JOIN users u ON u.id=m.user_id
                   ORDER BY u.email, m.imap_uid"""))
        for email in ("user1@example.com", "user2@example.com"):
            mine = [row for row in rows if row[0] == email]
            kept = [row for row in mine if row[2] != "skipped"]
            skipped = [row for row in mine if row[2] == "skipped"]
            self.assertEqual(len(kept), 1, f"{email} 应保留 1 封 CityU 邮件")
            self.assertEqual(len(skipped), 1, f"{email} 应跳过 1 封私人邮件")
            self.assertIn("不在允许名单内", skipped[0][3])

    def test_every_user_gets_brief_then_full_without_crossing(self):
        _, sent = self._run_cycle()
        self.assertEqual(len(sent), 4, "两用户各两封")
        for entry in self.users:
            mine = [item for item in sent if item["to"] == entry["report_to"]]
            briefs = [item for item in mine if item["is_brief"]]
            fulls = [item for item in mine if item["has_full_sections"]]
            self.assertEqual(len(briefs), 1, f"{entry['report_to']} 应有 1 封精简版")
            self.assertEqual(len(fulls), 1, f"{entry['report_to']} 应有 1 封完整版")
            # 精简版不得包含完整版章节
            self.assertFalse(briefs[0]["has_full_sections"])
            # 精简版永远排在完整版之前
            self.assertEqual(mine[0]["is_brief"], True)

    def test_paused_user_is_not_polled_while_others_continue(self):
        """A paused account must not stop the others from being served."""
        self.db.set_user_status(self.users[1]["user"]["id"], "paused")
        result, sent = self._run_cycle()
        self.assertEqual(result["ingested"], 1, "只剩 user1 的邮件")
        self.assertEqual({item["to"] for item in sent}, {self.users[0]["report_to"]})


if __name__ == "__main__":
    unittest.main()
