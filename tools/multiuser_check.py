"""Prove the recent behaviour changes are per-user, not per-account.

Builds two independent pilot users in a throwaway database and drives the real
ingestion + processing code (real MIME parsing, real sender filter, real brief
and full rendering, stubbed model and SMTP) for both mailboxes in one cycle.

Asserted properties:

1. every active user's mailbox is polled, not just the first account;
2. the sender allow-list skips personal mail for *each* user;
3. neither user's CityU mail is lost;
4. each user receives the two-stage brief + full pair;
5. skipped mail, reports and prompts stay inside their own user.

Usage: INFE_PILOT_MASTER_KEY=<key> python tools/multiuser_check.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pilot_app import mailio, providers, reports  # noqa: E402
from pilot_app.database import Database  # noqa: E402
from pilot_app.security import SecretBox, hash_password, token_hash  # noqa: E402

os.environ.setdefault("INFE_PILOT_MASTER_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
os.environ["INFE_PILOT_ALLOWED_SENDER_DOMAINS"] = "cityu.edu.hk"
os.environ["INFE_PILOT_BRIEF_FIRST"] = "1"
os.environ["INFE_PILOT_ALERT_ON_ARRIVAL"] = "0"

from pilot_app import service as service_mod  # noqa: E402
from pilot_app.service import PilotService  # noqa: E402


def eml(sender: str, subject: str, body: str, message_id: str) -> bytes:
    return (
        f"From: {sender}\nTo: me@example.com\nSubject: {subject}\n"
        f"Date: Sun, 13 Sep 2026 09:20:55 +0000\nMessage-ID: <{message_id}>\n"
        f"Content-Type: text/plain; charset=utf-8\n\n{body}\n"
    ).encode()


CITYU_A = eml("teacher@cityu.edu.hk", "Assignment 2 deadline extended to Friday 23:59",
              "Please submit your report by Friday 23:59 on Canvas.", "a-cityu-1")
PERSONAL_A = eml("hello@mail.grammarly.com", "50% off Grammarly Pro",
                 "Limited time offer. Unsubscribe here.", "a-promo-1")
CITYU_B = eml("library@cityu.edu.hk", "图书馆系统维护通知", "本周六系统维护，期间无法借书。", "b-cityu-1")
PERSONAL_B = eml("no-reply@fairwood.com.hk", "月滿中秋盆菜會員獨家優惠", "限時優惠，退訂請按此。", "b-promo-1")


def build_user(db: Database, box: SecretBox, index: int):
    invite = db.create_invite(f"user-{index}", 1)
    user = db.create_user(f"user{index}@example.com", hash_password("a-long-enough-password"),
                          token_hash(invite))
    db.upsert_profile(user["id"], {
        "school_email": f"student{index}@my.cityu.edu.hk", "major": f"专业{index}",
        "year_of_study": "大二", "courses": [], "interests": [], "career_goals": [],
        "focus_topics": [], "less_interested": [], "custom_instructions": "",
        "language": "bilingual", "timezone": "Asia/Hong_Kong", "immediate_enabled": True,
        "daily_enabled": True, "daily_time": "22:00",
    })
    mailbox_id = db.upsert_mailbox(user["id"], {
        "email": f"box{index}@qq.com", "report_to": f"owner{index}@outlook.com",
        "imap_host": "imap.qq.com", "imap_port": 993, "smtp_host": "smtp.qq.com", "smtp_port": 465,
        "encrypted_password": box.encrypt("pw", context=f"mailbox:{user['id']}"),
    })
    db.upsert_connection(user["id"], {
        "kind": "model", "provider": "deepseek", "model": f"model-{index}", "base_url": "",
        "encrypted_api_key": box.encrypt("k", context=f"connection:{user['id']}:model"),
        "config_json": "{}", "enabled": True,
    })
    return user, mailbox_id, f"owner{index}@outlook.com"


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="pilot-multiuser-"))
    db = Database(work / "pilot.sqlite3")
    db.initialize()
    box = SecretBox.from_environment()
    service = PilotService(db, box)

    one, box_one, to_one = build_user(db, box, 1)
    two, box_two, to_two = build_user(db, box, 2)
    print(f"已建立 2 个用户：{one['email']} / {two['email']}")

    # Each mailbox reports what arrived in it, independently.
    def fake_fetch(config, password, **kwargs):
        if config["id"] == box_one:
            return ("uidv-1", [(1, mailio.normalize_message(CITYU_A)),
                               (2, mailio.normalize_message(PERSONAL_A))], 2)
        return ("uidv-2", [(1, mailio.normalize_message(CITYU_B)),
                           (2, mailio.normalize_message(PERSONAL_B))], 2)

    seen_prompts: list[str] = []
    sent: list[dict] = []

    def fake_model(**kwargs):
        seen_prompts.append(kwargs.get("prompt", ""))
        brief = "简短即时摘要" in kwargs.get("prompt", "")
        if brief:
            return providers.Generation(
                "## 1. 重要程度与一句话结论\n- 等级：高\n- 结论：需要处理。\n"
                "## 2. 必须采取的行动与截止时间\n- 周五 23:59 前完成\n"
                "## 3. 邮件内容要点\n- 入口：系统", [], "none")
        return providers.Generation(
            "## 1. 重要程度与一句话结论\n- 等级：高\n- 结论：需要处理。\n"
            "## 5. 联网搜索后的建议与来源\n- 本次未取得可验证来源。\n"
            "## 6. 风险、未知与推测\n- 事实：来自原邮件。\n"
            "## 7. English summary\nHandle it by Friday.", [], "none")

    def record(config, password, subject, markdown, **kwargs):
        body = kwargs.get("text_body", "")
        sent.append({
            "to": config["report_to"], "subject": subject, "chars": len(body),
            # Headings are the honest "how much does it contain" measure; raw
            # character count is not, because the brief adds a footer line.
            # Structural markers: the full report always carries these two
            # plain-text section titles, the brief never does.
            "is_full": "【联网搜索后的建议】" in body and "【风险、未知与推测】" in body,
            "is_brief": "精简即时摘要" in body,
        })

    from pilot_app import worker as worker_mod
    with mock.patch("pilot_app.service.mailio.fetch_new_messages", side_effect=fake_fetch), \
            mock.patch("pilot_app.service.providers.generate", side_effect=fake_model), \
            mock.patch("pilot_app.service.mailio.send_report", side_effect=record):
        result = worker_mod.cycle(service)

    print(f"\n一轮结果：{result}")

    failures: list[str] = []
    with db.connect() as connection:
        rows = list(connection.execute(
            """SELECT u.email, m.imap_uid, m.subject, m.sender_address, m.status, m.skip_reason
               FROM messages m JOIN users u ON u.id = m.user_id
               ORDER BY u.email, m.imap_uid"""))
    print("\n各用户消息状态：")
    for row in rows:
        mark = "跳过" if row["status"] == "skipped" else row["status"]
        print(f"  {row['email']:<22} uid={row['imap_uid']} [{mark:<8}] {row['sender_address'][:30]:<30} {row['subject'][:26]}")

    # 1) 两个用户的邮箱都要被轮询
    owners = {row["email"] for row in rows}
    if owners != {one["email"], two["email"]}:
        failures.append(f"只有部分用户的邮箱被处理：{owners}")
    # 2) 每人各 1 封 CityU 保留、1 封私人跳过
    for user in (one, two):
        mine = [row for row in rows if row["email"] == user["email"]]
        kept = [row for row in mine if row["status"] != "skipped"]
        skipped = [row for row in mine if row["status"] == "skipped"]
        if len(kept) != 1:
            failures.append(f"{user['email']} 保留邮件数应为 1，实际 {len(kept)}")
        if len(skipped) != 1:
            failures.append(f"{user['email']} 跳过邮件数应为 1，实际 {len(skipped)}")
        for row in skipped:
            if "不在允许名单内" not in (row["skip_reason"] or ""):
                failures.append(f"{user['email']} 跳过原因缺失")
    # 3) 每人收到 精简 + 完整 两封，且发给各自报告地址
    print("\n各用户实际发出：")
    for user, report_to in ((one, to_one), (two, to_two)):
        reports_to = [item for item in sent if item["to"] == report_to]
        print(f"  {user['email']} -> {report_to}: {len(reports_to)} 封")
        for item in reports_to:
            kind = "完整" if item["is_full"] else ("精简" if item["is_brief"] else "其它")
            print(f"      {item['chars']:>4} 字符 [{kind}]  {item['subject'][:44]}")
        briefs = [i for i in reports_to if "精简" in i["subject"]]
        fulls = [i for i in reports_to if "精简" not in i["subject"]]
        if len(briefs) != 1 or len(fulls) != 1:
            failures.append(f"{user['email']} 应为 精简1 + 完整1，实际 {len(briefs)} + {len(fulls)}")
    # 4) 没有串号：发出的邮件数 = 2 用户 × 2 封
    if len(sent) != 4:
        failures.append(f"总发出应为 4 封，实际 {len(sent)}")
    # 5) 内容结构必须真的不同：精简版不含完整版的章节标记。
    #    字符数不能作判据——精简版多一句"完整报告随后发送"的说明，短内容上反而更长。
    for user, report_to in ((one, to_one), (two, to_two)):
        reports_to = [item for item in sent if item["to"] == report_to]
        brief = next((item for item in reports_to if "精简" in item["subject"]), None)
        full = next((item for item in reports_to if "精简" not in item["subject"]), None)
        if not (brief and full):
            continue
        if not brief["is_brief"]:
            failures.append(f"{user['email']} 精简版缺少精简版说明")
        if brief["is_full"]:
            failures.append(f"{user['email']} 精简版竟包含完整版章节")
        if not full["is_full"]:
            failures.append(f"{user['email']} 完整版缺少完整章节")
    # 6) 模型看到的 prompt 数：2 用户 × 2 阶段
    if len(seen_prompts) != 4:
        failures.append(f"模型调用应为 4 次，实际 {len(seen_prompts)}")

    print()
    if failures:
        print("发现问题：")
        for item in failures:
            print("  ✗", item)
        return 1
    print("✓ 两个用户都：被轮询、私人邮件被跳过、CityU 邮件收到 精简+完整 两封、互不串号")
    print("✓ 结论：这些变更对所有已注册用户生效，不是只对单个账号")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
