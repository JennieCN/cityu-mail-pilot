"""Seed a throwaway preview database with an account, mail and reports.

    .venv-pilot/bin/python tools/seed_preview.py /tmp/preview.sqlite3 [--base URL]

Why this exists: several browser checks need a logged-in account that already
has reports, and more than one person has lost time to a check that timed out on
"#dashboard" because the account simply did not exist. Writing the same ad-hoc
script by hand each time also got the invite, the profile and the report
encoding subtly different every run.

It creates, idempotently:

* one invite and one account (``$PILOT_ADMIN``, default ``boss@example.com``)
* a mailbox with a verified-looking state
* three reports: two for today, one for yesterday
* one task already handled *yesterday*, so the "look back by day" view has a
  second day to show

Everything is encrypted with the same master key the preview server runs with,
so the reports render exactly as production reports do.

Never point this at a real database: it writes an account with a known password.
It refuses to run unless INFE_PILOT_PREVIEW=1 is set, so a stray path argument
cannot quietly seed something that matters.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pilot_app import database as database_mod  # noqa: E402
from pilot_app import pricing as pricing_mod  # noqa: E402
from pilot_app import reports as reports_mod  # noqa: E402
from pilot_app.security import SecretBox, token_hash  # noqa: E402

PASSWORD = "a-long-enough-password"
INVITE = "seed-invite"

TODAY_MAIL = [
    ("作业截止提醒", "t1@cityu.edu.hk", """## 1. 重要程度与一句话结论
- 等级：高
- 结论：本周五 23:59 前必须提交作业。

## 2. 必须采取的行动与截止时间
- 提交 CS3101 作业到 Canvas（截止：2026-09-18 23:59）
- 预习第六章并整理笔记

## 3. 邮件内容总结
- 老师提醒作业提交时间。
"""),
    ("图书馆逾期通知", "lib@cityu.edu.hk", """## 1. 重要程度与一句话结论
- 等级：中
- 结论：有两本书即将到期。

## 2. 必须采取的行动与截止时间
- 归还《计算机网络》与《算法导论》（截止：2026-09-20 18:00）

## 3. 邮件内容总结
- 图书馆催还。
"""),
]

def _filler(index: int) -> tuple[str, str, str]:
    """Extra reports so list behaviour is reachable in a preview.

    Paging, collapsing and "show the next N" only exercise themselves past the
    first page. A preview with three reports quietly cannot test the code path
    those features exist for, and the check that covered it failed as though the
    feature were broken.
    """
    return (
        f"课程通知 {index}",
        f"course{index}@cityu.edu.hk",
        f"""## 1. 重要程度与一句话结论
- 等级：{'高' if index % 3 == 0 else '中'}
- 结论：这是第 {index} 份用于演示的课程通知。

## 2. 必须采取的行动与截止时间
- 阅读第 {index} 章并整理笔记（截止：2026-10-{index:02d} 23:59）

## 3. 邮件内容总结
- 老师发布了新的课程材料。
""",
    )


YESTERDAY_MAIL = ("选课系统开放", "reg@cityu.edu.hk", """## 1. 重要程度与一句话结论
- 等级：中
- 结论：下学期选课已开放。

## 2. 必须采取的行动与截止时间
- 在选课系统里确认下学期的三门课

## 3. 邮件内容总结
- 教务处通知选课开放。
""")

# The two failure shapes the operator's console is built to surface:
# (subject, sender, status, skip_reason, last_error)
ADMIN_MAIL = [
    ("学费缴纳通知", "fees@cityu.edu.hk", "failed", "", "SMTP 550 User has no permission"),
    ("限时优惠", "promo@example.com", "skipped", "非允许发件域", ""),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed a preview database for browser checks.")
    parser.add_argument("db", help="path to the preview sqlite file")
    parser.add_argument("--base", default="", help="preview server URL, to register through HTTP")
    parser.add_argument("--email", default=os.environ.get("PILOT_ADMIN", "boss@example.com"))
    parser.add_argument("--reports", type=int, default=8,
                        help="今天的报告份数；超过首页会把分页也带上（默认 8）")
    parser.add_argument("--admin-fixtures", action="store_true",
                        help="额外造一封没发出去的邮件、一封被跳过的邮件，以及 token 用量")
    args = parser.parse_args()

    if os.environ.get("INFE_PILOT_PREVIEW") != "1":
        print("拒绝执行：请设置 INFE_PILOT_PREVIEW=1 以确认这是预览库，不是生产库。", file=sys.stderr)
        return 2

    master = os.environ.get("INFE_PILOT_MASTER_KEY", "")
    if not master:
        print("缺少 INFE_PILOT_MASTER_KEY（要和预览服务用同一把）。", file=sys.stderr)
        return 2
    box = SecretBox.from_base64(master)
    db = database_mod.Database(args.db)
    db.initialize()

    expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)).isoformat()
    with db.connect() as connection:
        connection.execute("INSERT OR IGNORE INTO invites(code_hash,expires_at) VALUES(?,?)",
                           (token_hash(INVITE), expiry))

    user_id = _ensure_account(db, args.email, args.base)
    if user_id is None:
        print(f"账户 {args.email} 建不出来（注册被拒、写库也失败）。", file=sys.stderr)
        return 1

    now = dt.datetime.now(dt.timezone.utc)
    _ensure_mailbox(db, user_id, now.isoformat(timespec="seconds"))

    created = 0
    today = list(TODAY_MAIL) + [_filler(i) for i in range(1, max(0, args.reports - len(TODAY_MAIL)) + 1)]
    for index, (subject, sender, body) in enumerate(today):
        created += _add_report(db, box, user_id, subject, sender, body, now, 100 + index)
    yesterday = now - dt.timedelta(days=1)
    if _add_report(db, box, user_id, *YESTERDAY_MAIL, yesterday, 200):
        created += 1
        _handle_one_task(db, box, user_id, yesterday)

    if args.admin_fixtures:
        # 这一套夹具要 7 个账号才说得清（两个卡住的形状 + 两个「提醒过」的形状 +
        # 一个刚注册的 + 一个后台授权的管理员），而套件自己还要再注册**两个**用户
        # （被管理的那个，和广播那一段的另一个读者）。容量是实例配置（默认 5，
        # 环境变量给的），不把它抬起来的话每一个套件里的注册都会变成「当前试点名额
        # 已满」——而那看起来像注册坏了，不像夹具挤满了。
        db.set_setting("max_users", "10")
        _add_admin_fixtures(db, box, user_id, now)

    print(f"seeded {args.email} ({user_id}): {created} new report(s)")
    if args.base:
        print(f"sign in at {args.base} with {args.email} / {PASSWORD}")
    return 0


def _ensure_account(db: database_mod.Database, email: str, base: str) -> str | None:
    """拿到夹具账号的 id：先走真的注册端点，被闸门拒了就**直接建号 + 授权**。

    **为什么要有这条兜底**（2026-09-26）：`POST /api/auth/register` 现在拒收命中
    `INFE_PILOT_ADMIN_EMAILS` 的地址（GPT 审计 P1 —— 不验证邮箱归属，谁抢在主人之前
    注册谁当场就是管理员）。而这一套夹具要的正是**管理员会话**，地址又正是那个保留地址
    （`run_browser_checks.sh` 给预览服务的是 `INFE_PILOT_ADMIN_EMAILS=boss@example.com`），
    于是播种 403、**19 个浏览器套件红了 15 个**（公开仓库那两个 CI 作业一起红）。
    闸门本身是对的，错的是夹具还在演攻击者的做法。

    兜底走的是与 `manage create-admin --apply` 同一条路（`Database.create_user` +
    `Database.grant_admin`），各套件仍然走**真的登录端点**拿会话。
    **没有绕过任何闸门**：`/api/auth/register` 一次都不再碰。
    Python 那边的同类夹具在 `pilot_app/tests/admin_fixture.py`，两边是同一个理由。
    """
    if base:
        user_id = _register(base, email)
        if user_id:
            return user_id
    with db.connect() as connection:
        row = connection.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if row is not None:
        return row["id"]
    from pilot_app.security import hash_password

    try:
        user = db.create_user(email, hash_password(PASSWORD), "")
        # 夹具地址就是预览服务的 `INFE_PILOT_ADMIN_EMAILS`；授权这一下让它**不依赖**
        # 那个环境变量也仍然是管理员（后台「授权」这条路 v0.34.0 起就有）。
        db.grant_admin(email)
    except Exception as exc:  # noqa: BLE001 - 播种失败要说人话，不要抛栈
        print(f"直接建号失败：{exc}", file=sys.stderr)
        return None
    print(f"注册被拒（多半是保留地址闸门），已直接建号 + 授权：{email}", file=sys.stderr)
    return user["id"]


def _register(base: str, email: str) -> str | None:
    if not base:
        return None
    request = urllib.request.Request(
        base.rstrip("/") + "/api/auth/register",
        data=json.dumps({"email": email, "password": PASSWORD, "invite_code": INVITE,
                         "accepted_terms": True}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read())["id"]
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:200]
        # Already registered is the normal case on a second run.
        if "已被注册" in detail or "已注册" in detail:
            return None
        print(f"注册失败：{error.code} {detail}", file=sys.stderr)
        return None


def _ensure_mailbox(db: database_mod.Database, user_id: str, now: str) -> None:
    with db.connect() as connection:
        existing = connection.execute("SELECT id FROM mailboxes WHERE user_id=?", (user_id,)).fetchone()
        if existing:
            connection.execute("UPDATE mailboxes SET last_verified_at=?,last_verify_error='' WHERE user_id=?",
                               (now, user_id))
            return
        connection.execute(
            """INSERT INTO mailboxes(id,user_id,email,report_to,imap_host,imap_port,smtp_host,
               smtp_port,encrypted_password,last_verified_at,updated_at)
               VALUES('mbx_seed',?,'me@example.com','me@example.com','h',993,'h',465,?,?,?)""",
            (user_id, b"\x00", now, now))


def _add_report(db: database_mod.Database, box: SecretBox, user_id: str, subject: str, sender: str,
                body: str, moment: dt.datetime, uid: int) -> int:
    """Add one message + report, unless a message with this subject already exists."""
    with db.connect() as connection:
        already = connection.execute("SELECT 1 FROM messages WHERE user_id=? AND subject=?",
                                     (user_id, subject)).fetchone()
    if already:
        return 0
    received = moment.isoformat(timespec="seconds")
    message_id = db.insert_message(user_id, "mbx_seed", "1", uid, {
        "subject": subject, "sender_name": subject[:4], "sender_address": sender,
        "received": received, "importance": "normal",
        "body": box.encrypt("原始邮件正文", context=f"message:{user_id}"),
    })
    if message_id is None:
        return 0
    with db.connect() as connection:
        connection.execute("UPDATE messages SET received_at=?,status='sent' WHERE id=?",
                           (received, message_id))
    db.create_report(user_id=user_id, message_id=message_id, kind="immediate",
                     subject=f"【AI邮件摘要】{subject}",
                     body=box.encrypt(body, context=f"report:{user_id}"), sent_to="me@example.com")
    return 1


def _add_admin_fixtures(db: database_mod.Database, box: SecretBox, user_id: str,
                        now: dt.datetime) -> None:
    """The states the admin console can only show when something went wrong.

    A happy-path seed makes every summary row read zero, so assertions in
    ``admin_edit_check`` had nothing to look at: the collapsed mail row colours
    itself only when a mail did NOT reach the user, the spend panel is uniformly
    "$0.000000" with no per-model or per-day rows to expand, and the "已跳过"
    filter passed *vacuously* because the filtered result was empty.

    Deliberately not part of the default seed. Other suites assert on the
    happy-path numbers ("已下发 9"), so a failed mail appearing in every preview
    would make those counts depend on whether this function had run.
    """
    with db.connect() as connection:
        already = connection.execute(
            "SELECT 1 FROM messages WHERE user_id=? AND subject=?",
            (user_id, ADMIN_MAIL[0][0])).fetchone()
    if already:
        return

    for index, (subject, sender, status, reason, error) in enumerate(ADMIN_MAIL, start=1):
        message_id = db.insert_message(user_id, "mbx_seed", "1", 300 + index, {
            "subject": subject, "sender_name": subject[:4], "sender_address": sender,
            "received": (now - dt.timedelta(minutes=5 * index)).isoformat(timespec="seconds"),
            "importance": "normal",
        })
        if not message_id:
            continue
        with db.connect() as connection:
            connection.execute(
                "UPDATE messages SET status=?,skip_reason=?,last_error=? WHERE id=?",
                (status, reason, error, message_id))

    # One sentinel finding per tier. The panel's whole point is that the three
    # channels differ and that "quiet" does not mean "dropped", and neither is
    # visible in a browser check whose alert table is empty -- the panel would
    # just say 一切正常 and pass without rendering a single row.
    # 这四条的 `key/title/detail` 与下面的分析记录共用同一份值：分析记录里的
    # `fingerprint` 必须是**这些字符串**算出来的那个，否则控制台会把每一行都标成
    # 「详情已经变了」——那既是假警报，也让人看不出这个标记本来要说什么。
    open_findings = [
        ("disk", "critical", "磁盘空间不足", "根分区已用 95%（阈值 90%）。"),
        (f"mailbox_error:{user_id}", "critical", "收信失败：preview@example.com",
         "最近一次轮询报错：IMAP 认证失败。"),
        (f"setup_stalled:{user_id}", "warning", "注册后没配完：preview@example.com",
         "注册超过 12 小时仍未完成，还没配私人转发邮箱。"),
    ]
    # 一条已经恢复的：面板必须说得出「现在已经不在了」，否则读者分不清旧账与现状。
    cleared = ("backup_stale", "warning", "备份过期",
               "最新一份备份是 30 小时前的（阈值 36 小时）。")
    for key, severity, title, detail in open_findings + [cleared]:
        db.record_alert(key, severity, detail, title, now)
    db.clear_alert(cleared[0], now - dt.timedelta(minutes=40))

    # 四条分析，分别代表面板上那四种「现在还成不成立」。这一栏是用户问出来的
    # （原话：「ai运维是不是不会及时同步情况」）：以前只有历史没有现状，于是早就修好
    # 的旧账和现在还在坏长得一模一样。夹具要能把四种都摆出来，否则浏览器套件只会
    # 测到其中一种。
    #
    # 其中有动作的那一条：确认按钮只对它出现——一个每行都有按钮的面板，会让「按钮在」
    # 这条断言什么也证明不了。按钮是模型的措辞唯一能变成能力的地方，所以它前面必须有
    # 东西真的挡着。
    if not db.list_agent_reports(limit=1):
        from pilot_app import agent as agent_mod  # noqa: PLC0415 -- 只给夹具算指纹用

        def seeded(finding, text, action, *, fingerprint=None):
            """A row whose fingerprint matches **this** shape of the finding.

            The console decides 「还在不在 / 是不是旧结论」 by comparing this
            fingerprint with the finding's current `key|title|detail`, so a
            fixture that wrote a made-up string here would make every row read
            「详情已经变了」 -- a false alarm that would also hide what the column
            is for. Pass a fingerprint only to stand for "it changed after the
            analysis" (which is what a spent budget looks like).
            """
            key, severity, title, detail = finding
            return {
                "key": key, "severity": severity, "title": title, "action": action,
                "text": text,
                "fingerprint": fingerprint or agent_mod.finding_fingerprint(
                    {"key": key, "title": title, "detail": detail}),
            }

        disk, mailfail, stalled = open_findings
        # 正文写成**现在的模板**是有意的：控制台把它渲染成带小标题的分段，
        # 用旧散文形状的夹具会让套件在解析器坏掉时照样绿。
        rows = [
            seeded(disk, "【结论】根分区已用到 95%，离满了不远\n"
                         "【依据】\n- 根分区已用 95%\n- 阈值是 90%\n"
                         "- 备份与日志都在这个分区上\n"
                         "【可能的原因】\n- 日志与备份文件累积。依据：磁盘读数持续上升\n"
                         "【建议】\n- 确认执行「重启邮件工作进程」，让它释放已删除文件的句柄\n"
                         "【怎么验证】\n- 下一封告警里的磁盘读数应低于 90%\n"
                         "【建议动作】restart_worker", "restart_worker"),
            seeded(mailfail, "【结论】这个账号的收信一直在失败，收信一次都没通过\n"
                             "【依据】\n- 最近一次轮询报错：IMAP 认证失败\n"
                             "- 配置进度：配了转发邮箱，但一次都没连通过\n"
                             "【可能的原因】\n- 授权码过期或被邮箱服务商吊销。"
                             "依据：报错是认证失败而不是网络超时\n"
                             "【建议】\n- 让这个账号的用户重新生成一次 IMAP 授权码\n"
                             "【怎么验证】\n- 用户列表里这个账号的「收信」灯变绿\n"
                             "【建议动作】无", ""),
            seeded(cleared, "【结论】最新一份备份已经 30 小时没更新\n"
                            "【依据】\n- 最新备份 30 小时前\n- 阈值 36 小时\n"
                            "【可能的原因】\n- 定时器被停用。依据：备份目录没有新文件\n"
                            "【建议】\n- 看一眼备份定时器\n"
                            "【怎么验证】\n- 下一份备份出现\n"
                            "【建议动作】run_backup", ""),
            # 详情在分析之后变过、还没重新分析（额度用完、或没有 key 时就是这样）。
            # 面板必须自己把这条标出来，而不是把它当成现在的判断。
            seeded(stalled, "【结论】注册超过一天还没配好邮箱\n"
                            "【依据】\n- 注册已 13 小时\n"
                            "【可能的原因】\n- 用户没看懂第 2 步\n"
                            "【建议】\n- 问他一句\n"
                            "【怎么验证】\n- 他配好了邮箱\n"
                            "【建议动作】无", "",
                   fingerprint=agent_mod.finding_fingerprint({
                       "key": stalled[0], "title": stalled[2],
                       "detail": "注册已 13 小时（阈值 12 小时）"})),
        ]
        for row in rows:
            db.record_agent_report(
                finding_key=row["key"], severity=row["severity"], title=row["title"],
                fingerprint=row["fingerprint"],
                provider="deepseek", model="deepseek-flash",
                tokens={"input": 900, "output": 200, "total": 1100},
                cost=0.00032, currency="USD",
                body=box.encrypt(row["text"], context="agent"), created_at=now, action=row["action"])

    # One account whose mailbox we can reach but cannot log in to. It exists so
    # the console's health card has a *broken* mailbox to describe: the reported
    # bug was 「imap 授权码都没有填对，为什么后台显示他正在跑」, and with a
    # healthy-only seed there was nothing in the browser suite that could have
    # noticed -- the card's own unit test asserted the wrong semantics.
    #
    # A raw insert rather than a registration: the seed's invite is single-use.
    stamp = now.isoformat(timespec="seconds")
    with db.connect() as connection:
        if not connection.execute("SELECT 1 FROM users WHERE email=?",
                                  ("wrongcode@example.com",)).fetchone():
            connection.execute(
                "INSERT INTO users(id,email,password_hash,status,created_at) VALUES(?,?,?,?,?)",
                ("usr_wrongcode", "wrongcode@example.com", "x", "active", stamp))
            connection.execute(
                """INSERT INTO mailboxes(id,user_id,email,report_to,imap_host,imap_port,
                       smtp_host,smtp_port,encrypted_password,enabled,last_polled_at,
                       last_error,last_verify_error,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?,?)""",
                # example.com, not a real provider: the fixture only needs an
                # address the privacy gate already treats as fictional, and the
                # first version used a 163.com one that made the export refuse.
                ("mbx_wrongcode", "usr_wrongcode", "wrongcode@example.com",
                 "wrongcode@example.com",
                 "imap.example.com", 993, "smtp.example.com", 465, b"\x00", stamp,
                 # Both columns carry it, because that is what a failed explicit
                 # verification really writes (`record_mailbox_verification` sets
                 # `last_verify_error` *and* `last_error`). The console must still
                 # print the sentence once -- see the assertion in
                 # tools/admin_edit_check.js.
                 "IMAP 连接失败：b'LOGIN Login error or password error'",
                 "IMAP 连接失败：b'LOGIN Login error or password error'", stamp))

    # 一位**后台授权的管理员**，邮箱可用（v0.63.93）。「邀请申请到了还能通知谁」那
    # 一段需要有一个可勾的人：环境里的 boss@example.com 永远收得到、界面上也撤不掉，
    # 所以只有这一个候选能证明「勾上 → 保存 → 刷新之后那个勾还在」不是画出来的。
    # 他不在环境文件里，所以 `source` 必须是 `database`，而在后台授权出来的人正是
    # 这个功能存在的理由（用户原话：通知不「只是」通知我）。
    with db.connect() as connection:
        if not connection.execute("SELECT 1 FROM users WHERE email=?",
                                  ("deputy@example.com",)).fetchone():
            connection.execute(
                "INSERT INTO users(id,email,password_hash,status,created_at,is_admin)"
                " VALUES(?,?,?,?,?,1)",
                ("usr_deputy", "deputy@example.com", "x", "active", stamp))
            connection.execute(
                """INSERT INTO mailboxes(id,user_id,email,report_to,imap_host,imap_port,
                       smtp_host,smtp_port,encrypted_password,enabled,last_polled_at,
                       last_error,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?)""",
                ("mbx_deputy", "usr_deputy", "deputy@example.com", "deputy@example.com",
                 "imap.example.com", 993, "smtp.example.com", 465, b"\x00", stamp, "", stamp))

    # Two accounts for the "one-click reminder" panel, both registered long
    # enough ago to count as stuck. The panel decides *which sentence* each of
    # them needs, so the seed has to contain one of each kind -- a single stuck
    # account would let a broken grouping pass.
    #
    # `usr_wrongcode` above is deliberately left fresh: it is younger than
    # `setup_reminders.MIN_AGE_HOURS`, so it must NOT appear in the panel. That
    # is the "somebody who registered ten minutes ago is busy, not stuck" rule,
    # and this fixture is what makes it assertable in a browser.
    old_stamp = (now - dt.timedelta(hours=30)).isoformat(timespec="seconds")
    with db.connect() as connection:
        if not connection.execute("SELECT 1 FROM users WHERE email=?",
                                  ("stalled@example.com",)).fetchone():
            # never configured a mailbox at all -> `no_mailbox`
            connection.execute(
                "INSERT INTO users(id,email,password_hash,status,created_at) VALUES(?,?,?,?,?)",
                ("usr_stalled_never", "stalled@example.com", "x", "active", old_stamp))
        if not connection.execute("SELECT 1 FROM users WHERE email=?",
                                  ("stalledcode@example.com",)).fetchone():
            # configured, polled, and rejected -> `setup_gap` is "" and only the
            # receive light can see it, which is the trap this panel exists for
            connection.execute(
                "INSERT INTO users(id,email,password_hash,status,created_at) VALUES(?,?,?,?,?)",
                ("usr_stalled_refused", "stalledcode@example.com", "x", "active", old_stamp))
            connection.execute(
                """INSERT INTO mailboxes(id,user_id,email,report_to,imap_host,imap_port,
                       smtp_host,smtp_port,encrypted_password,enabled,last_polled_at,
                       last_error,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,1,?,?,?)""",
                # `last_polled_at` is *now*, not 30h ago: the poller stamps it on
                # every failed attempt, and that is what makes this account the trap
                # it is -- a recent timestamp that says "we are polling" while the
                # error column says "and it never works". Backdating it would quietly
                # make the fixture a *stale* mailbox instead, which is a different
                # (already covered) failure.
                ("mbx_stalled_refused", "usr_stalled_refused", "stalledcode@example.com",
                 "stalledcode@example.com", "imap.example.com", 993,
                 "smtp.example.com", 465, b"\x00", stamp,
                 "IMAP 连接失败：b'LOGIN Login error or password error'", stamp))

    # 「提醒之后他回来过没有」（2026-09-17）：印章本身答不了这个问题（它只说明我们
    # 做了什么），所以要两个形状各一份——一个提醒后回来过、一个从没打开过应用。
    # 两个账号的印章都盖在两天前，`last_seen_at` 才是区别所在。
    #
    # 这两个是**另外两个账号**，不是上面那两个：上面的 stalled/stalledcode 必须保持
    # 「还没提醒过」，否则套件里那一段（按发送 → 全部失败 → 失败的账号不许被盖章）
    # 就没有可发的人，而「失败没被记成已提醒」这条断言也会被预置的印章带偏。
    told_stamp = (now - dt.timedelta(days=2)).isoformat(timespec="seconds")
    with db.connect() as connection:
        for uid, address in (("usr_told_back", "cameback@example.com"),
                             ("usr_told_silent", "nevercame@example.com")):
            if not connection.execute("SELECT 1 FROM users WHERE email=?",
                                      (address,)).fetchone():
                connection.execute(
                    "INSERT INTO users(id,email,password_hash,status,created_at) VALUES(?,?,?,?,?)",
                    (uid, address, "x", "active", old_stamp))
        # 前者回来过（就在刚刚），后者一次都没有（'' 就是「从没用过」）。
        connection.execute("UPDATE users SET last_seen_at=? WHERE id=?",
                           (stamp, "usr_told_back"))
        connection.execute("UPDATE users SET last_seen_at='' WHERE id=?",
                           ("usr_told_silent",))
    db.set_setting("setup_reminder:usr_told_back", f"{told_stamp}|never")
    db.set_setting("setup_reminder:usr_told_silent", f"{told_stamp}|never")
    # 这台实例「从三天前就在记活跃时间」——比那两封提醒还早，所以这两行才判得出来。
    # 不写这一句，夹具会落在「那次提醒早于活跃时间上线」那一档（正确但测不到判据）。
    db.set_setting("last_seen_tracking_since",
                   (now - dt.timedelta(days=3)).isoformat(timespec="seconds"))

    # Two local days and two models: a one-row "按天" breakdown would satisfy the
    # assertion while proving nothing about the grouping.
    #
    # The third element is **who paid**, and all three states are here on purpose:
    # the account's own usage view has to keep "the instance key paid", "the user's
    # own key paid" and "recorded before we tracked it" apart, and a fixture with
    # only one of them would let a wrong bucket pass.
    calls = [
        ("deepseek", "deepseek-flash", {"input": 12000, "cached_input": 4000,
                                       "output": 900, "reasoning": 0}, now, True),
        ("deepseek", "deepseek-v4-pro", {"input": 8000, "cached_input": 0,
                                         "output": 600, "reasoning": 0},
         now - dt.timedelta(days=1), False),
        ("deepseek", "deepseek-flash", {"input": 3000, "cached_input": 1000,
                                       "output": 200, "reasoning": 0},
         now - dt.timedelta(days=1), None),
    ]
    for provider, model, usage, moment, on_platform in calls:
        usage = dict(usage, total=usage["input"] + usage["output"])
        price = pricing_mod.lookup(provider, model)
        cost = pricing_mod.estimate(usage, price, at=moment)
        row_id = db.record_usage(user_id=user_id, kind="immediate", provider=provider,
                                 on_platform=on_platform,
                                 model=model, usage=usage, cost=cost, price=price)
        # record_usage stamps "now"; the day grouping needs one row older, so the
        # timestamp is corrected the same way _add_report corrects received_at.
        if moment.date() != now.date():
            with db.connect() as connection:
                connection.execute("UPDATE token_usage SET created_at=? WHERE id=?",
                                   (moment.isoformat(timespec="seconds"), row_id))

    # One account whose model key was rejected three times and is therefore
    # suspended. The breaker keeps that account's messages out of the queue, and
    # the health card's red line can only be looked at in a real browser if
    # something in the fixture is actually suspended -- otherwise the assertion
    # is about a branch that never runs.
    #
    # Deliberately `usr_stalled_never` rather than a new account: it has no
    # mailbox at all, so it appears on the health card exactly once, in the
    # suspension line, and cannot be mistaken for either of the two mailbox
    # warnings that are already asserted. (The pilot capacity is raised for this
    # fixture in `main`, so another account would be possible -- it is the
    # ambiguity, not the seat, that this one avoids.)
    for _ in range(3):
        db.record_key_failure("usr_stalled_never", "model", "API 返回 HTTP 401：invalid api key")


def _handle_one_task(db: database_mod.Database, box: SecretBox, user_id: str,
                     moment: dt.datetime) -> None:
    """Mark yesterday's first action as handled, so the day archive has content."""
    start, end, _now, day = _window(moment)
    rows = db.today_reports(user_id, start, end)
    if not rows:
        return
    row = rows[0]
    markdown = box.decrypt(row["body_markdown"], context=f"report:{user_id}")
    tasks = reports_mod.today_tasks(
        [(row["id"], markdown, row["message_id"])],
        [{"id": row["message_id"], "subject": row["message_subject"], "sender_name": row["sender_name"],
          "sender_address": row["sender_address"], "received": row["received_at"],
          "importance": row["importance"]}],
        timezone="Asia/Hong_Kong",
    )
    if tasks:
        db.set_task_state(user_id, tasks[0]["task_key"], "done", tasks[0])


def _window(moment: dt.datetime) -> tuple[str, str, dt.datetime, str]:
    from zoneinfo import ZoneInfo
    local = moment.astimezone(ZoneInfo("Asia/Hong_Kong"))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(days=1)
    return (start.astimezone(dt.timezone.utc).isoformat(timespec="seconds"),
            end.astimezone(dt.timezone.utc).isoformat(timespec="seconds"), local,
            start.date().isoformat())


if __name__ == "__main__":
    raise SystemExit(main())
