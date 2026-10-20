"""Local end-to-end demonstration of the arrival alert vs. the full report.

Runs the real ingestion and processing code (real MIME parsing, real sender
filter, real triage, real report rendering) against a throwaway database, with
the model call replaced by a stub that sleeps, so the *ordering* and *latency
split* can be measured without contacting any provider or mailbox.

Usage:
    INFE_PILOT_MASTER_KEY=<key> python tools/latency_demo.py [--model-delay 200]
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pilot_app import mailio, providers, reports, triage  # noqa: E402
from pilot_app.database import Database  # noqa: E402
from pilot_app.security import SecretBox, hash_password, token_hash  # noqa: E402
from pilot_app.service import PilotService  # noqa: E402

CITYU_MAIL = b"""From: "YU Jianchi" <student@my.cityu.edu.hk>
To: "private" <me@qq.com>
Subject: =?utf-8?b?6L2s5Y+ROiBbRUUyMDAwXSBBc3NpZ25tZW50IGRlYWRsaW5lIGV4dGVuZGVk?=
Date: Sun, 13 Sep 2026 09:20:55 +0000
Message-ID: <demo-cityu-1@smtp82.ad.cityu.edu.hk>
Content-Type: text/plain; charset=utf-8

Dear student,

The deadline for Assignment 2 has been extended. Please submit your report on
Canvas by Friday 23:59. Late submissions lose marks.
"""

PERSONAL_MAIL = b"""From: "Grammarly" <hello@mail.grammarly.com>
To: "private" <me@qq.com>
Subject: 50% off Grammarly Pro
Date: Sun, 13 Sep 2026 09:21:55 +0000
Message-ID: <demo-personal-1@mail.grammarly.com>
Content-Type: text/plain; charset=utf-8

Limited time offer. Unsubscribe here.
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-delay", type=float, default=200.0,
                        help="seconds the stub model 'takes' (measured real range: 107–423s)")
    args = parser.parse_args()

    os.environ.setdefault("INFE_PILOT_MASTER_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    os.environ["INFE_PILOT_ALERT_ON_ARRIVAL"] = "1"
    os.environ["INFE_PILOT_ALLOWED_SENDER_DOMAINS"] = "cityu.edu.hk"

    workdir = Path(tempfile.mkdtemp(prefix="pilot-latency-"))
    db = Database(workdir / "pilot.sqlite3")
    db.initialize()
    box = SecretBox.from_environment()

    invite = db.create_invite("demo", 1)
    user = db.create_user("student@example.com", hash_password("a-long-enough-password"), token_hash(invite))
    db.upsert_profile(user["id"], {
        "school_email": "student@my.cityu.edu.hk", "major": "INFE", "year_of_study": "大二",
        "courses": ["密码学"], "interests": [], "career_goals": [], "focus_topics": [],
        "less_interested": [], "custom_instructions": "", "language": "bilingual",
        "timezone": "Asia/Hong_Kong", "immediate_enabled": True, "daily_enabled": True,
        "daily_time": "22:00",
    })
    mailbox_id = db.upsert_mailbox(user["id"], {
        "email": "me@qq.com", "report_to": "me@qq.com", "imap_host": "imap.qq.com", "imap_port": 993,
        "smtp_host": "smtp.qq.com", "smtp_port": 465,
        "encrypted_password": box.encrypt("pw", context=f"mailbox:{user['id']}"),
    })
    db.upsert_connection(user["id"], {
        "kind": "model", "provider": "deepseek", "model": "demo", "base_url": "",
        "encrypted_api_key": box.encrypt("k", context=f"connection:{user['id']}:model"),
        "config_json": "{}", "enabled": True,
    })
    mailbox = db.get_mailbox(user["id"])
    service = PilotService(db, box)

    # --- real ingestion: real MIME parsing, real sender filter ---------------
    incoming = [(1001, mailio.normalize_message(CITYU_MAIL)),
                (1002, mailio.normalize_message(PERSONAL_MAIL))]
    with mock.patch("pilot_app.service.mailio.fetch_new_messages",
                    return_value=("v1", incoming, 1002)):
        stored = service.poll_mailbox(mailbox)
    print(f"抓信：入库 {stored} 封（CityU 1 封，个人邮件被跳过）")

    with db.connect() as connection:
        rows = list(connection.execute(
            "SELECT imap_uid,subject,sender_address,status,skip_reason FROM messages ORDER BY imap_uid"))
    for row in rows:
        mark = "跳过" if row["status"] == "skipped" else "待处理"
        print(f"  uid={row['imap_uid']} [{mark}] {row['sender_address'][:34]:<34} {row['subject'][:34]}")
        if row["skip_reason"]:
            print(f"      原因：{row['skip_reason'][:70]}")

    # --- the model is slow; the alert must not wait for it -------------------
    def slow_model(**kwargs):
        time.sleep(args.model_delay)
        return providers.Generation(
            "## 1. 重要程度与一句话结论\n- 等级：高\n- 结论：作业截止时间延后到周五 23:59，需要重新提交。\n"
            "## 2. 必须采取的行动与截止时间\n- 周五 23:59 前在 Canvas 提交（截止：2026-09-18 23:59）\n"
            "## 3. 邮件内容总结\n- 课程组通知作业截止时间延后。\n"
            "## 4. 与我的学业、兴趣和目标的关系\n相关度：高。\n"
            "## 5. 联网搜索后的建议与来源\n本次未取得可验证来源。\n"
            "## 6. 风险、未知与推测\n- 事实：来自原邮件。\n"
            "## 7. English summary\nThe deadline moved to Friday 23:59.\n", [], "none")

    sent: list[tuple[float, str]] = []
    started = time.monotonic()

    def record_send(config, password, subject, markdown, **kwargs):
        sent.append((time.monotonic() - started, subject))

    with mock.patch("pilot_app.service.providers.generate", side_effect=slow_model), \
            mock.patch("pilot_app.service.mailio.send_report", side_effect=record_send):
        ok = service.process_due(limit=5)

    print(f"\n处理完成：success={ok}（模型延迟设为 {args.model_delay:.0f}s）")
    print("\n实际发送时序（相对开处理时刻）：")
    for elapsed, subject in sent:
        print(f"  +{elapsed:6.1f}s  {subject[:64]}")

    if len(sent) >= 2:
        alert_at, report_at = sent[0][0], sent[-1][0]
        print(f"\n回执在 +{alert_at:.1f}s 到达，完整报告在 +{report_at:.1f}s 到达"
              f" → 感知延迟从约 {report_at:.0f}s 降到约 {alert_at:.0f}s")
        print(f"（提升倍数约 {report_at / max(alert_at, 0.1):.0f}×）")
    else:
        print("\n注意：没有观察到两封邮件（回执 + 报告），请检查配置")

    with db.connect() as connection:
        states = {row[0]: row[1] for row in connection.execute(
            "SELECT status, COUNT(*) FROM messages GROUP BY status")}
    print("\n最终消息状态:", states)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
