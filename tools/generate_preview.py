"""Seed a local pilot database and preview files for UI/visual verification.

This never talks to the network: it writes a throwaway SQLite database under the
path given by ``INFE_PILOT_DB`` plus two standalone HTML previews of the report
emails. It is a development tool, not part of the shipped runtime.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pilot_app import reports
from pilot_app.database import Database
from pilot_app.security import SecretBox, hash_password, token_hash

INVITE = "preview-invite-code"
EMAIL = "student@example.com"
PASSWORD = "a-long-preview-password"

IMMEDIATE = """## 1. 重要程度与一句话结论 / Importance and one-line conclusion
- 等级：高
- 结论：老师把作业提交时间改到了本周五 23:59，需要重新检查要求并测试代码。

## 2. 必须采取的行动与截止时间 / Required actions and deadlines
- 今天阅读新版作业要求
- 周四前完成 C++ 代码测试
- 周五 23:59 前在 Canvas 提交（截止：2026-09-18 23:59）

## 3. 邮件内容总结 / Email content summary
- 发件人：课程教师
- 提交入口：Canvas 作业页面
- 附件：新版要求.pdf
- 与上次相比，最大改动是允许使用标准库以外的第三方库。

## 4. 与我的学业、兴趣和目标的关系 / Personal relevance
与你的大二 INFE 课程、密码学和 C++ 作业直接相关，相关度：高。

## 5. 联网搜索后的建议与来源 / Suggestions from web search and sources
先看官方提交说明，再按课程论坛里同学整理的测试清单自检。
来源：Canvas 学生指南 https://community.canvaslms.com/t5/Student-Guide/tkb-p/student
来源：CityU 校历 https://www.cityu.edu.hk/calendar

## 6. 风险、未知与推测 / Risks, unknowns and inferences
- 事实：截止时间与提交入口来自原邮件。
- 推测 / Inference：老师可能会在周四再发一次补充说明，建议留意邮箱。

## 7. English summary
The assignment deadline moved to Friday 23:59. Review the updated requirements and test your C++ submission before uploading it to Canvas.
"""

LOW = """## 1. 重要程度与一句话结论
- 等级：低
- 结论：编程学习平台的限时折扣推广，与当前课程无关。

## 2. 必须采取的行动与截止时间
- 无需行动。

## 3. 邮件内容总结
- 营销邮件，包含折扣码和退订链接。

## 4. 与我的学业、兴趣和目标的关系
相关度：低，与当前课程无关。

## 5. 联网搜索后的建议与来源
本次未取得可验证来源 / No verifiable source was retrieved。

## 6. 风险、未知与推测
- 推测 / Inference：折扣码可能随时失效。
"""

ANNOUNCE = """## 1. 重要程度与一句话结论
- 等级：中
- 结论：Career Centre 下周举办实习招聘宣讲会，需要提前报名。

## 2. 必须采取的行动与截止时间
- 周三 18:00 前在系统里报名（截止：2026-09-16 18:00）

## 3. 邮件内容总结
- 时间：9 月 17 日 15:00
- 地点：AC1 讲堂
- 面向通信与计算机相关专业

## 4. 与我的学业、兴趣和目标的关系
与你的职业目标和实习兴趣高度相关。

## 5. 联网搜索后的建议与来源
来源：Career Centre 活动页 https://www.cityu.edu.hk/careers/

## 6. 风险、未知与推测
- 事实：时间地点来自原邮件。
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--out", default="preview")
    parser.add_argument("--fresh", action="store_true", help="delete an existing preview database first")
    args = parser.parse_args()

    if args.fresh:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(args.db) + suffix)
            if candidate.exists():
                candidate.unlink()

    os.environ.setdefault("INFE_PILOT_MASTER_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
    db = Database(args.db)
    db.initialize()
    box = SecretBox.from_environment()

    with db.connect() as connection:
        connection.execute("DELETE FROM invites")
    expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30)).isoformat(timespec="seconds")
    with db.connect() as connection:
        connection.execute("INSERT INTO invites(code_hash,expires_at) VALUES(?,?)", (token_hash(INVITE), expiry))
    user = db.create_user(EMAIL, hash_password(PASSWORD), token_hash(INVITE))
    user_id = user["id"]

    db.upsert_profile(user_id, {
        "school_email": "student@my.cityu.edu.hk", "major": "通信工程 / INFE", "year_of_study": "大二",
        "courses": ["密码学", "C++", "数字信号处理"], "interests": ["网络安全", "嵌入式系统"],
        "career_goals": ["通信工程师", "安全研究"], "focus_topics": ["比赛", "实习", "截止日期"],
        "less_interested": ["广告"], "custom_instructions": "优先指出对密码学和编程课程有帮助的机会",
        "language": "bilingual", "timezone": "Asia/Hong_Kong", "immediate_enabled": True,
        "daily_enabled": True, "daily_time": "22:00",
    })
    mailbox_id = db.upsert_mailbox(user_id, {
        "email": "student@qq.com", "report_to": "student@qq.com", "imap_host": "imap.qq.com",
        "imap_port": 993, "smtp_host": "smtp.qq.com", "smtp_port": 465,
        "encrypted_password": box.encrypt("preview-app-password", context=f"mailbox:{user_id}"),
    })
    db.record_mailbox_verification(mailbox_id)
    db.upsert_connection(user_id, {
        "kind": "model", "provider": "deepseek", "model": "deepseek-flash", "base_url": "",
        "encrypted_api_key": box.encrypt("preview-key", context=f"connection:{user_id}:model"),
        "config_json": "{}", "enabled": True,
    })

    now = dt.datetime.now(dt.timezone.utc)
    samples = [
        ("课程作业截止日期更新：改为本周五 23:59", "课程教师", "teacher@cityu.edu.hk", IMMEDIATE, 1.5),
        ("Career Centre 实习招聘宣讲会报名", "Career Centre", "career@cityu.edu.hk", ANNOUNCE, 3.2),
        ("Codefinity 限时学习优惠", "Codefinity", "promo@codefinity.example", LOW, 5.1),
    ]
    messages = []
    for index, (subject, sender, address, body, hours_ago) in enumerate(samples):
        received = (now - dt.timedelta(hours=hours_ago)).isoformat(timespec="seconds")
        message_id = db.insert_message(user_id, mailbox_id, "1000", 100 + index, {
            "subject": subject, "sender_name": sender, "sender_address": address,
            "received": received, "importance": "normal",
            "body": box.encrypt("preview body", context=f"message:{user_id}"),
        })
        with db.connect() as connection:
            connection.execute("UPDATE messages SET received_at=?,status='sent' WHERE id=?", (received, message_id))
        report_id = db.create_report(
            user_id=user_id, message_id=message_id, kind="immediate",
            subject=f"【AI邮件摘要】{subject[:60]}",
            body=box.encrypt(body, context=f"report:{user_id}"), sent_to="student@qq.com",
        )
        db.mark_report_sent(report_id)
        messages.append({
            "id": message_id, "subject": subject, "sender_name": sender, "sender_address": address,
            "received_at": received, "importance": "normal", "status": "sent", "last_error": "",
        })

    # Preview files for the email layouts (never sent as mail).
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    parsed = reports.parse_report(IMMEDIATE, message=messages[0], timezone="Asia/Hong_Kong")
    blocks = [("即时报告（行动优先）", reports.render_immediate_html(parsed))]
    digest = reports.build_digest(messages, {
        messages[0]["id"]: IMMEDIATE, messages[1]["id"]: ANNOUNCE, messages[2]["id"]: LOW,
    }, timezone="Asia/Hong_Kong")
    reports.with_digest_header(digest, "2026-09-13", now.isoformat(timespec="seconds"))
    blocks.append(("每日简报（学生简报版）", reports.render_digest_html(digest, subject="【CityU 每日简报】9月13日")))
    (out / "report-preview.html").write_text(reports.preview_document(blocks), encoding="utf-8")

    # A deliberately hostile payload: long URLs, CJK run-on text, unbroken tokens.
    stress = reports.parse_report(
        "## 1. 重要程度与一句话结论\n- 等级：高\n- 结论："
        + "这是一段特别长的中文结论" * 30
        + "\n## 2. 必须采取的行动与截止时间\n- 处理一个非常长的链接 https://example.com/"
        + "a" * 240 + "?query=" + "b" * 120 + "\n- 处理一个超长英文单词 "
        + "Supercalifragilisticexpialidocious" * 8 + "\n"
        "## 5. 联网搜索后的建议与来源\n来源：超长来源标题"
        + "标题" * 40 + " https://example.com/" + "c" * 200,
        message={"subject": "超长内容压力测试" + "主题" * 60,
                 "sender_name": "A" * 120, "sender_address": "verylongaddress@example.com",
                 "received": now.isoformat(timespec="seconds"), "importance": "high"},
        timezone="Asia/Hong_Kong",
    )
    digest_blocks = [("长内容压力测试", reports.render_immediate_html(stress))]
    (out / "report-preview-long.html").write_text(reports.preview_document(digest_blocks), encoding="utf-8")
    print(f"seeded {args.db}")
    print(f"login: {EMAIL} / {PASSWORD}  invite: {INVITE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
