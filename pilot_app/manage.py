"""Administrative commands that never print or accept service secrets.

Service secrets are the master key, the platform model/search keys and mailbox
app passwords; none of them are ever printed here, and nothing here takes one as
an argument. One command is a deliberate exception to the sentence above:
``reset-password`` prints a **new user credential** -- the temporary password it
just minted -- to stdout exactly once, because handing it to the person is the
entire point of the command. It is never mailed, logged or audited.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import getpass
from getpass import getpass as _prompt_password  # 见 create-admin：main() 里有一处局部 import 遮蔽了模块名
import hashlib
import os
import json
import secrets
import socket
import shutil
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from . import alerting, analytics, budget, deps, geoip, mailio, mailboxcheck, nginxlog, providers, reports
from . import providercheck
from . import tierhealth
from .database import Database, parse_utc, utc_now
from .migration import read_legacy_processed_uids
from .security import (SecretBox, generate_temporary_password, hash_password,
                       token_hash)


def _mask(address: str) -> str:
    """Enough of an address to identify the account, never the full mailbox."""
    local, _, domain = str(address or "").partition("@")
    if not domain:
        return "***"
    return f"{local[:2]}***@{domain}"


STORE_TARGET_ENV = "INFE_PILOT_E2E_STORE_DB"
# One definition, used by both main() and the guard below. Two spellings of the
# production path would drift, and the guard is only as good as its agreement
# with the file that actually gets opened.
DEFAULT_DB_PATH = "/var/lib/cityu-mail-pilot/pilot.sqlite3"


def _same_path(left: str, right: str) -> bool:
    """Whether two paths name the same file, without requiring either to exist."""
    return os.path.abspath(str(left or "")) == os.path.abspath(str(right or ""))


def _store_rows_refusal(store: bool, pull_date: str, db_path: str) -> tuple[int, str] | None:
    """Say no to ``--store`` before it can write fake "已下发" rows.

    ``--store`` exists to exercise the daily-digest path over *real* mail, and it
    does that by inserting rows that claim to have been delivered:
    ``_store_message`` sets ``status='sent'`` and ``_store_report`` calls
    ``mark_report_sent``. That is correct in a throwaway database and harmful in
    the live one -- rule 8 makes ``messages.status`` the record of what was
    delivered, so one stray run would leave a real account's mail looking
    already reported, and the worker would never report it again. Nothing used
    to enforce the "throwaway" part: ``main()`` opens ``INFE_PILOT_DB``, which on
    the server *is* the production database.

    So writing requires naming the target out loud, the same way ``handoff.py
    snapshot`` and ``tools/seed_preview.py`` require it -- and the name must
    agree with the database this run will actually open. Requiring the variable
    to merely *exist* would have left the hazard intact: the server's unit file
    need not set ``INFE_PILOT_DB`` at all, so ``E2E_STORE_DB=/tmp/x`` plus an
    unset ``INFE_PILOT_DB`` would still have written to the production default.
    Returns ``(code, message)`` when the run must stop, ``None`` to proceed.
    """
    if not store:
        return None
    if not pull_date:
        # Today ``--store`` alone silently skipped every write while still
        # printing "已落库到 …（仅限本次验证数据库）", so it looked like it had
        # written something. Refusing is the honest answer.
        return 2, ("--store 必须配 --pull-date YYYY-MM-DD：验证的是「这一天的当日简报」，"
                   "没有日期就没有可验证的目标，什么也不会写。")
    try:
        dt.date.fromisoformat(pull_date)
    except ValueError:
        return 2, f"--pull-date 不是合法日期：{pull_date}"
    target = os.environ.get(STORE_TARGET_ENV, "")
    if not target:
        return 2, (f"拒绝执行：--store 会把真实邮件与报告写成「已下发」的行。\n"
                   f"  确认目标库确实是丢弃用的，再显式指名它：\n"
                   f"    {STORE_TARGET_ENV}=/tmp/e2e.sqlite3 INFE_PILOT_DB=/tmp/e2e.sqlite3 "
                   f"… --store --pull-date {pull_date}\n"
                   f"  想验生产数据就先拷一份库（cp pilot.sqlite3 /tmp/e2e.sqlite3），"
                   f"把 INFE_PILOT_DB 指到副本上跑——不要往生产库写。")
    if _same_path(target, db_path):
        return None
    return 2, (f"拒绝执行：指名的是 {target}，但这轮真正要打开的是 {db_path}。\n"
               f"  --store 会写成「已下发」的行，所以这两者必须是同一个丢弃用的库：\n"
               f"    {STORE_TARGET_ENV}={db_path} INFE_PILOT_DB={db_path} … --store "
               f"--pull-date {pull_date}\n"
               f"  （只设 {STORE_TARGET_ENV} 而把 INFE_PILOT_DB 留空，"
               f"在服务器上就等于对着生产库跑。）")


def _scrub(text: str, secrets_: list[str], addresses: list[str]) -> str:
    """Second gate before printing: no app password, no whole address.

    ``check-mailboxes`` prints sentences that came off a mail server, and a
    server *should* never echo the credential it just rejected — but "should"
    is not a property anybody can verify from here. So every string that is
    about to be printed goes through this: each app password becomes ``***``
    and each full address becomes its masked form. ``test_mailbox_check`` pins
    it with a fake server that deliberately echoes the password back.
    """
    out = str(text or "")
    for secret in secrets_:
        if secret:
            out = out.replace(secret, "***")
    for address in addresses:
        if address and "@" in address:
            out = out.replace(address, _mask(address))
    return out


def _pad(text: str, width: int) -> str:
    """Pad to ``width`` **display** columns (CJK counts as two), at least one space.

    The table's first columns are ASCII in practice, but an operator pasting a
    Chinese local part into a mailbox field is not a bug worth a misaligned
    table -- and a value longer than its column still needs a gap, or two cells
    run together into one unreadable string.
    """
    import unicodedata

    text = str(text or "")
    shown = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)
    return text + " " * max(1, width - shown)


#: 「登录」那一列怎么显示（按探针状态，不是按分档——分档还要看主机）。
_LOGIN_MARKS = {
    mailboxcheck.OK: "✓",
    mailboxcheck.AUTH_REJECTED: "拒绝",
    mailboxcheck.PROVIDER_BLOCKED: "拒绝",
    mailboxcheck.NETWORK: "连不上",
    mailboxcheck.INBOX_REFUSED: "开不了箱",
    mailboxcheck.LOGIN_REFUSED: "拒绝",
    mailboxcheck.CHECK_FAILED: "未探",
}


def verify_e2e(db: Database, user_email: str, limit: int, send: bool, show_body: bool,
               *, force_resend: bool = False, send_digest: bool = False, pause: bool = False,
               pull: int = 0, pull_date: str = "", store: bool = False,
               measure: bool = False, measure_model: str = "") -> int:
    """Drive the real pipeline against one user without risking duplicate mail.

    Guarantees, in order of importance:

    * **Never sends a duplicate.** Any message whose immediate report already
      reached ``status='sent'`` is skipped and reported as a reuse, and a
      ``--send`` run refuses to send when the previous report exists but failed.
    * **Never moves the IMAP cursor.** Messages are fetched through
      ``fetch_new_messages`` for inspection only; ``update_mailbox_poll`` is not
      called, so the worker's view of the mailbox is unchanged and nothing can be
      skipped later.
    * **Never prints secrets.** Output shows masked addresses, message counts,
      timings, sizes and section names only — never bodies, keys or app passwords
      unless ``--show-body`` is explicitly passed.
    * **Never writes "已下发" rows unless the target is named on purpose.**
      ``--store`` claims delivery (``status='sent'``), so it is refused unless
      ``INFE_PILOT_E2E_STORE_DB`` says which throwaway database is meant; see
      ``_store_rows_refusal``. Everything else here is read-only.

    Sending is opt-in (``--send``); the default mode renders both the immediate
    report and the daily digest so the real chain is proven end to end.
    """
    # 登录用的查询已经把 status='deleted' 过滤掉了，而删除流程本来就是**删行**
    # （`set_user_status` 里 deleted 走的是 DELETE），所以下面那一支只在「库里留着
    # 历史 deleted 行」的老库上才可能走到——留着是因为全项目都是这个防御形状
    # （database.py 里有十来处同款过滤），不是因为它今天可达。
    user = db.find_user_for_login(user_email)
    if not user:
        print(f"找不到试点用户：{_mask(user_email)}")
        return 2
    if user["status"] == "deleted":
        print("该账户已删除（数据已按删除流程清掉，不需要再验证）。")
        return 2
    mailbox = db.get_mailbox(user["id"])
    if not mailbox:
        print(f"{_mask(user['email'])} 还没有配置邮箱。")
        return 2
    # Before the master key and before the first write: --store is the one flag
    # here that can leave the live database claiming something was delivered.
    refusal = _store_rows_refusal(store, pull_date, db.path)
    if refusal is not None:
        print(refusal[1])
        return refusal[0]
    target_day = None
    if pull_date:
        target_day = dt.date.fromisoformat(pull_date)
    profile = db.get_profile(user["id"]) or {}
    model = db.get_connection(user["id"], "model")
    search = db.get_connection(user["id"], "search")
    mode = "发送" if send else "只渲染（不发信）"
    print(f"用户 {_mask(user['email'])} · 账户状态={user['status']} · 转发邮箱={_mask(mailbox['email'])}")
    print(f"模型={model['provider'] if model else '未配置'} · 搜索={search['provider'] if search else '未配置'}"
          f" · 模式={mode}")

    started = time.time()
    box = SecretBox.from_environment()
    if pause:
        # Only this mailbox is touched; the user's own row is left alone so a
        # --send run still has report_to. A second worker can therefore never
        # consume the same messages while this verification runs.
        with db.connect() as connection:
            connection.execute("UPDATE mailboxes SET enabled=0 WHERE id=?", (mailbox["id"],))
        print("已临时禁用该邮箱（验证结束后恢复），避免第二个 worker 同时消费。")
    try:
        return _e2e_run(db, box, user, mailbox, profile, model, search, limit, send, show_body,
                        force_resend, send_digest, started, pull, target_day, store, measure,
                        measure_model)
    finally:
        if pause:
            with db.connect() as connection:
                connection.execute("UPDATE mailboxes SET enabled=1 WHERE id=?", (mailbox["id"],))
            print("已恢复该邮箱。")
    return 0



def _override_model(connection, kind: str, model_name: str):
    """Return a copy of a model connection with a different model name."""
    if connection is None or kind != "model":
        return connection
    return {**connection, "model": model_name}


def _store_message(db: Database, box: SecretBox, user, mailbox, target_day, uid: int, message: dict,
                   index: int) -> str:
    """Insert one real message into the throwaway verification database."""
    received = dt.datetime.combine(target_day, dt.time(3, 0), tzinfo=dt.timezone.utc)
    received = received + dt.timedelta(minutes=7 * index)
    stamp = received.isoformat(timespec="seconds")
    message_id = db.insert_message(user["id"], mailbox["id"], "e2e", uid, {
        "subject": message["subject"], "sender_name": message["sender_name"],
        "sender_address": message["sender_address"], "received": stamp,
        "importance": message["importance"],
        "body": box.encrypt(message["body"], context=f"message:{user['id']}"),
    })
    with db.connect() as connection:
        connection.execute("UPDATE messages SET received_at=?,status='sent' WHERE id=?", (stamp, message_id))
    return message_id


def _store_report(db: Database, box: SecretBox, user, message_id: str, markdown: str, report_to: str) -> str:
    with db.connect() as connection:
        row = connection.execute("SELECT subject FROM messages WHERE id=?", (message_id,)).fetchone()
    subject = str(row["subject"]) if row else "验证邮件"
    report_id = db.create_report(
        user_id=user["id"], message_id=message_id, kind="immediate",
        subject=f"【AI邮件摘要】{subject[:120]}",
        body=box.encrypt(markdown, context=f"report:{user['id']}"), sent_to=report_to,
    )
    db.mark_report_sent(report_id)
    return report_id


def _e2e_run(db: Database, box: SecretBox, user, mailbox, profile, model, search, limit: int,
             send: bool, show_body: bool, force_resend: bool, send_digest: bool, started: float,
             pull: int = 0, target_day: "dt.date | None" = None, store: bool = False,
             measure: bool = False, measure_model: str = "") -> int:
    password = box.decrypt(mailbox["encrypted_password"], context=f"mailbox:{user['id']}")
    try:
        if pull:
            messages = mailio.fetch_recent_messages(mailbox, password, count=pull)
            uid_validity = str(mailbox.get("uid_validity") or "")
            print(f"只读连接成功 · UIDVALIDITY={uid_validity} · 为验证取样最近 {len(messages)} 封真实邮件"
                  f" · 当前游标 last_uid={mailbox.get('last_uid')}（未改动）")
        else:
            uid_validity, messages, highest = mailio.fetch_new_messages(mailbox, password)
            print(f"只读连接成功 · UIDVALIDITY={uid_validity} · 新邮件={len(messages)}"
                  f" · 当前游标 last_uid={mailbox.get('last_uid')}（未改动）")
    except Exception as exc:
        print(f"IMAP 失败：{mailio.explain_imap_failure(exc)}")
        return 1

    from .service import PilotService

    service = PilotService(db, box)
    sent = reused = failed = 0
    timed: list[float] = []
    stored = []
    for uid, message in messages[: max(1, limit)]:
        existing = None
        with db.connect() as connection:
            row = connection.execute(
                """SELECT r.id,r.status,r.subject,r.body_markdown,r.sent_at
                   FROM reports r JOIN messages m ON m.id=r.message_id
                   WHERE m.mailbox_id=? AND m.imap_uid=? AND r.kind='immediate'""",
                (mailbox["id"], uid),
            ).fetchone()
            existing = dict(row) if row else None
        subject_preview = str(message["subject"])[:60]
        if measure:
            print(f"[量测] UID {uid} 「{subject_preview}」强制重新生成（不发信、不落库）…")
            # 2026-09-23 的教训：主服务是**本机那台**时，量测这一发也是在跟真实报告抢槽位。
            # 那天连着量了几次，把两封真实报告挤到了付费兜底（各多花一次钱）。
            # 这句话不拦人（量测本来就有用），只是让下一个人先看一眼有没有信在排队。
            _slots = providers.local_model_slots()
            if _slots is not None:
                print(f"  ⚠️  主服务是本机那台，只有 {_slots} 个推理槽 —— 这一发和**真实报告**抢槽位。"
                      "先确认没有信正在排队：`journalctl -u cityu-mail-pilot-worker -n 20`"
                      "（2026-09-23 就是在这里连跑，把两封真报告挤到了付费兜底）。")
            began = time.time()
            if measure_model:
                # In-memory only: the user's stored model choice is not touched.
                original = service.db.get_connection
                service.db.get_connection = (
                    lambda uid, kind, _o=original: _override_model(_o(uid, kind), kind, measure_model)
                )
                print(f"  临时模型覆盖：{measure_model}（不改用户配置）")
            markdown = service._analyse(user["id"], {
                "subject": message["subject"], "sender_name": message["sender_name"],
                "sender_address": message["sender_address"], "received": message["received"],
                "importance": message["importance"], "body": message["body"],
            })
            markdown = markdown or ""
            elapsed = time.time() - began
            rendered = reports.render_immediate(
                markdown, message, subject=f"【AI邮件摘要】{message['subject'][:120]}",
                timezone=profile.get("timezone"))
            parsed = reports.parse_report(markdown, message=message, timezone=profile.get("timezone"))
            # 生成耗时只做统计，不写入数据库，也不会发出任何邮件
            print(f"  端到端生成耗时 {elapsed:.1f}s · 报告 {len(markdown)} 字符"
                  f" · HTML {len(rendered['html'])} 字节 · 行动 {len(parsed['actions'])} 条"
                  f" · 来源 {len(parsed['sources'])} 个")
            timed.append(elapsed)
            continue
        if existing and existing["status"] == "sent" and not (send and force_resend):
            print(f"[跳过] UID {uid} 「{subject_preview}」已有成功报告（{existing['sent_at']}）→ 不会重发")
            reused += 1
            continue
        if existing and existing["status"] == "sent" and send and force_resend:
            print(f"[重发取证] UID {uid} 已有成功报告，因 --force-resend 明确允许，重发一封用于验收。")
        if existing and existing["status"] != "sent" and send and not force_resend:
            print(f"[拒绝] UID {uid} 已有失败报告，先人工确认，避免重复发送。")
            failed += 1
            continue

        stored_id = None
        if store and target_day:
            # Land the real mail first: even if generation fails, the digest must
            # still list it (that is exactly the "no silent drop" guarantee).
            stored_id = _store_message(db, box, user, mailbox, target_day, uid, message, len(stored))
            stored.append(stored_id)
            print(f"  已落库到 {target_day.isoformat()}：真实邮件（status 记为已下发）")

        print(f"[处理] UID {uid} 「{subject_preview}」…（模型调用较慢，实测可达 180 秒）")
        began = time.time()
        try:
            markdown = service._analyse(user["id"], {
                "subject": message["subject"], "sender_name": message["sender_name"],
                "sender_address": message["sender_address"], "received": message["received"],
                "importance": message["importance"], "body": message["body"],
            })
        except Exception as exc:
            print(f"  分析失败：{exc}")
            failed += 1
            continue
        elapsed = time.time() - began
        if stored_id:
            _store_report(db, box, user, stored_id, markdown, mailbox["report_to"])
            print("  已落库：本次生成的报告")
        rendered = reports.render_immediate(
            markdown, message, subject=f"【AI邮件摘要】{message['subject'][:120]}",
            timezone=profile.get("timezone"),
        )
        parsed = reports.parse_report(markdown, message=message, timezone=profile.get("timezone"))
        print(f"  完成：{elapsed:.0f}s · 报告 {len(rendered['text'])} 字符 / HTML {len(rendered['html'])} 字节"
              f" · 优先级={parsed['priority_label']} · 行动 {len(parsed['actions'])} 条"
              f" · 来源 {len(parsed['sources'])} 个 · 搜索可用={not parsed['search_unavailable']}")
        if show_body:
            print("  ---- 报告预览（含邮件内容，注意隐私）----")
            print("  " + rendered["text"].replace("\n", "\n  "))
            print("  ---- 预览结束 ----")
        if send:
            try:
                mailio.send_report(
                    mailbox, box.decrypt(mailbox["encrypted_password"], context=f"mailbox:{user['id']}"),
                    rendered["subject"], markdown,
                    html_body=rendered["html"], text_body=rendered["text"],
                )
                print(f"  已发送到 {_mask(mailbox['report_to'])}（未写入数据库，避免与 worker 争用）")
                sent += 1
            except Exception as exc:
                print(f"  发送失败：{exc}")
                failed += 1

    # Prove the 22:00 brief still covers the whole day from stored reports.
    now = dt.datetime.now(dt.timezone.utc)
    zone = profile.get("timezone") or "Asia/Hong_Kong"
    try:
        from zoneinfo import ZoneInfo
        local = now.astimezone(ZoneInfo(zone))
    except Exception:
        local = now
    start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(dt.timezone.utc)
    end = start + dt.timedelta(days=1)
    if store and target_day and stored:
        # Exercise the production daily-report path (send_daily) end to end.
        daily_date = target_day.isoformat()
        try:
            sent_daily = PilotService(db, box).send_daily(
                {**user, "timezone": zone, "report_to": mailbox["report_to"]}, daily_date)
            print(f"每日简报（真实代码路径 send_daily，{daily_date}）发送结果={bool(sent_daily)}")
        except Exception as exc:
            print(f"每日简报发送失败：{exc}")
            failed += 1
        start = dt.datetime.combine(target_day, dt.time(0, 0), tzinfo=dt.timezone.utc)
        end = start + dt.timedelta(days=1)
    rows = db.messages_between(user["id"], start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds"))
    reports_by_id = {
        row["message_id"]: box.decrypt(row["body_markdown"], context=f"report:{user['id']}")
        for row in rows if row.get("body_markdown") is not None
    }
    digest = reports.build_digest(rows, reports_by_id, timezone=zone,
                                  snoozed=list(db.task_states(user["id"]).values()))
    reports.with_digest_header(digest, local.date().isoformat(), now.isoformat(timespec="seconds"))
    digest_html = reports.render_digest_html(digest, subject=reports.digest_subject(digest))
    digest_text = reports.render_digest_text(digest, subject=reports.digest_subject(digest))
    titles = [reports.parse_report(body, message=row, timezone=zone)["subject"]
              for row, body in ((row, reports_by_id.get(row["message_id"])) for row in rows)
              if body is not None]
    print(f"每日简报（{local.date().isoformat()} · {zone}）：邮件 {digest['metrics']['total']} 封"
          f" · 合并后 {digest['metrics']['merged_total']} 条 · 待办 {digest['metrics']['actionable']} 件"
          f" · 失败 {digest['metrics']['failed']} 封 · HTML {len(digest_html)} 字节"
          f" · 纯文本 {len(digest_text)} 字符")
    print(f"  标题：{' | '.join(title[:40] for title in titles[:6]) or '（今天还没有邮件）'}")
    # "No summary" is two different things and only one of them is a problem.
    # Skipped mail (sender outside the allow-list) is the filter working as
    # designed -- calling it "失败/未完成" described a report the user would never
    # see, and on production that was 82 of 86 mails in one day. Anything that is
    # neither summarised nor skipped is the thing worth a warning.
    skipped = int(digest["metrics"].get("skipped") or 0)
    unexplained = digest["metrics"]["total"] - len(titles) - skipped
    if skipped:
        print(f"  说明：另有 {skipped} 封被发件人白名单跳过（简报里写「被跳过」，不算失败）")
    if unexplained > 0:
        print(f"  警告：有 {unexplained} 封邮件既没有摘要也不是被跳过（简报里会标为失败/未完成，不会静默丢弃）")
    if send and send_digest and not (store and target_day):
        try:
            mailio.send_report(
                mailbox, box.decrypt(mailbox["encrypted_password"], context=f"mailbox:{user['id']}"),
                reports.digest_subject(digest), reports.digest_markdown(digest),
                html_body=digest_html, text_body=digest_text,
            )
            print(f"  每日简报已发送到 {_mask(mailbox['report_to'])}（未写入数据库，避免与 worker 争用）")
            sent += 1
        except Exception as exc:
            print(f"  每日简报发送失败：{exc}")
            failed += 1
    if timed:
        ordered = sorted(timed)
        print(f"生成耗时统计：次数 {len(ordered)} · 最快 {ordered[0]:.1f}s · 中位 {ordered[len(ordered)//2]:.1f}s"
              f" · 最慢 {ordered[-1]:.1f}s")
    print(f"总计：用时 {time.time() - started:.0f}s · 发送 {sent} · 复用已发报告 {reused}"
          f" · 新处理 {len(messages[: max(1, limit)]) - reused - failed} · 失败 {failed}"
          f" · 游标未改动={mailbox.get('last_uid')}")
    return 1 if failed else 0



def _load_env_file(path: str) -> None:
    """Read INFE_* defaults from a local pilot env file without echoing values."""
    try:
        text = Path(path).expanduser().read_text(encoding="utf-8")
    except OSError as exc:
        print(f"读取环境文件失败：{exc}")
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("INFE_") and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")



def read_credential_file(path: str) -> tuple[str, str]:
    """Read (email, password) from a local 0600 file. Never prints the contents.

    Accepts two shapes so the user can choose either:
      * a single line holding only the app password, or
      * ``email=`` / ``password=`` lines (``user=`` and ``code=`` also work).
    """
    target = Path(path).expanduser()
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"读取凭据文件失败：{exc}")
        return "", ""
    email_value = ""
    password_value = ""
    lone: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if sep and key.strip().lower() in {"email", "user", "username", "account"}:
            email_value = value.strip().strip('"').strip("'")
            continue
        if sep and key.strip().lower() in {"password", "pass", "code", "authcode", "app_password"}:
            password_value = value.strip().strip('"').strip("'")
            continue
        lone.append(line)
    if not password_value and lone:
        password_value = lone[-1]
    return email_value, password_value


def diagnose_forwarding(email_address: str, password: str, *, host: str = "imap.qq.com", port: int = 993,
                        folder: str = "INBOX", limit: int = 40) -> int:
    """Read-only duplicate-delivery diagnosis for one real mailbox.

    Prints **headers only** (never bodies) and no credentials, and never writes,
    moves, marks or deletes anything. It answers one question: do two copies of
    the same original mail carry the same ``Message-ID`` (one mail delivered
    twice) or different ones (two genuinely different forwards)?
    """
    import collections
    import email as email_mod
    import email.policy
    import email.utils
    import imaplib

    local, _, domain = email_address.partition("@")
    print(f"目标邮箱 {local[:3]}***@{domain} · 只读 · 仅打印邮件头（不打印正文、不修改任何邮件）")
    try:
        client = imaplib.IMAP4_SSL(host, int(port), timeout=30,
                               ssl_context=mailio.imap_ssl_context())
        mailio.identify_client(client)
        client.login(email_address, password)
    except Exception as exc:
        print(f"登录失败：{mailio.explain_imap_failure(exc)}")
        return 1
    try:
        status, data = client.select(folder, readonly=True)
        if status != "OK":
            print(mailio.refused_inbox(data))
            return 1
        status, data = client.uid("search", None, "ALL")
        uids = [value.decode() for value in (data[0].split() if data and data[0] else [])]
        print(f"{folder} 共 {len(uids)} 封，检查最近 {min(limit, len(uids))} 封")
        wanted = ("MESSAGE-ID", "SUBJECT", "DATE", "FROM", "TO", "DELIVERED-TO", "X-ORIGINAL-TO",
                  "X-FORWARDED-TO", "X-FORWARDED-FOR", "RETURN-PATH", "X-MS-EXCHANGE-FORWARDINGLOOP",
                  "AUTO-SUBMITTED", "X-GM-THRID")
        records: list[dict[str, Any]] = []
        for uid in uids[-max(1, limit):]:
            status, content = client.uid(
                "fetch", uid, "(BODY.PEEK[HEADER.FIELDS (" + " ".join(wanted) + ")])"
            )
            raw = next((item[1] for item in content if isinstance(item, tuple) and isinstance(item[1], bytes)), b"")
            message = email_mod.message_from_bytes(raw, policy=email.policy.default)
            records.append({
                "uid": uid,
                "message_id": (message.get("Message-ID") or "").strip(),
                "subject": str(message.get("Subject") or "")[:52],
                "date": str(message.get("Date") or "")[:31],
                "to": str(message.get("To") or "")[:46],
                "delivered_to": str(message.get("Delivered-To") or "")[:46],
                "original_to": str(message.get("X-Original-To") or "")[:46],
                "forwarded_to": str(message.get("X-Forwarded-To") or "")[:46],
                "loop": str(message.get("X-MS-Exchange-ForwardingLoop") or "")[:34],
                "return_path": str(message.get("Return-Path") or "")[:40],
            })
        for row in records:
            print(f"  UID {row['uid']:>6} | {row['date']:<31} | {row['subject']:<52}")
            print(f"          To={row['to']} | Delivered-To={row['delivered_to']} | X-Original-To={row['original_to']}")
            print(f"          X-Forwarded-To={row['forwarded_to']} | loop={row['loop']} | Return-Path={row['return_path']}")
            print(f"          Message-ID={row['message_id']}")

        grouped: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
        for row in records:
            grouped[row["message_id"] or f"(no-id-{row['uid']})"].append(row)
        duplicates = {key: value for key, value in grouped.items() if len(value) > 1}
        print(f"\n重复 Message-ID：{len(duplicates)} 组")
        for key, rows in list(duplicates.items())[:10]:
            gaps = ""
            try:
                times = [email.utils.parsedate_to_datetime(r["date"]) for r in rows if r["date"]]
                if len(times) > 1:
                    gaps = f" 间隔={(max(times) - min(times)).total_seconds():.0f}s"
            except Exception:
                gaps = ""
            print(f"  {key}")
            print(f"    UID {[r['uid'] for r in rows]} · 主题「{rows[0]['subject']}」{gaps}")
            for row in rows:
                print(f"      UID {row['uid']}: Delivered-To={row['delivered_to'] or '-'} "
                      f"X-Original-To={row['original_to'] or '-'} loop={row['loop'] or '-'}")
        print("\n判定：")
        if duplicates:
            print("  同一 Message-ID 出现多次 → 同一封邮件被投递了两遍（不是新转发产生的另一封）。")
            print("  说明转发路径里有两个投递动作：Outlook「转发」与「重定向」同时生效，或邮箱侧另加了一条转发规则。")
        else:
            print("  未发现相同 Message-ID 的重复投递（那么两封是不同批次/不同规则产生的不同邮件）。")
    finally:
        try:
            client.close()
        except Exception:
            pass
        try:
            client.logout()
        except Exception:
            pass
    return 0


def _run(command: list[str]) -> str:
    """Run a reporting command, never raising: a missing tool is not a failure."""
    try:
        completed = subprocess.run(command, capture_output=True, text=True,
                                   timeout=15, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"（无法运行 {' '.join(command)}：{exc}）"
    return (completed.stdout or "") + (completed.stderr or "")


def notify_unit_failure(db: Database, unit: str, *, lines: int = 30) -> int:
    """Mail the operator that a systemd unit failed. Used by ``OnFailure=``.

    Deliberately independent of the worker's sentinel: this path has to work
    when the worker itself is the unit that died. It borrows the admin's own
    mailbox to send, the same way every other message in this project is sent.
    """
    unit = unit.strip()
    if not unit:
        print("缺少 --unit。")
        return 2
    status = _run(["systemctl", "status", "--full", "--no-pager", unit])
    journal = _run(["journalctl", "-u", unit, "--no-pager", "-o", "short-iso",
                    "-n", str(max(1, min(lines, 200)))])
    # Invariant 2: command output leaves the machine inside an e-mail, so it is
    # scrubbed of every credential value in pilot.env before it is embedded.
    body = alerting.redact("\n".join(part for part in (status, journal) if part).strip(),
                           alerting.collect_secret_values())[:8000]
    subject = f"[CityU Mail Pilot] 单元失败：{unit}"
    text = (f"{unit} 进入了 failed 状态。\n\n{body}\n\n"
            f"处理：ssh 到服务器执行 systemctl status --full {unit}\n"
            "（以上输出已自动脱敏；不会打印任何密钥。）")
    try:
        box = SecretBox.from_environment()
        delivered = alerting.send_admin_mail(db, box, subject, text)
    except Exception as exc:
        # A failure handler that raises would obscure the original failure, and
        # OnFailure= must not recurse. The journal keeps the reason instead.
        print(f"无法发出单元失败告警：{exc}", file=sys.stderr)
        return 1
    print(f"已告警：{unit} -> {', '.join(delivered)}")
    return 0


def master_key_verified(db: Database, *, note: str = "", show: bool = False) -> int:
    """Record that a human just compared the offline copy with this server.

    The master key is in **no** backup on purpose (a backup that carries the key
    is not a backup, it is a second copy of the secret), which makes "do I still
    have the offline copy?" the one question no machine can answer. What can be
    recorded is *when someone last asked*, and that is what this does:
    `backup --check` then ages it and starts complaining after a year.

    It also stores the fingerprint that was confirmed. If the server's key is
    ever rotated or a different one is restored, the recorded value stops
    matching and `--check` says so — because the copy on the shelf is then the
    wrong key for every backup in the directory.

    Run it **after** comparing, never instead of comparing: this command cannot
    see the offline copy, and a record that is written without looking is worse
    than no record at all.
    """
    recorded_at = db.get_setting("master_key_verified_at")
    recorded_print = db.get_setting("master_key_verified_fingerprint")
    recorded_note = db.get_setting("master_key_verified_note")
    box = SecretBox.from_environment()
    fingerprint = box.fingerprint()

    if show:
        print(f"当前主密钥指纹：{fingerprint}")
        print(f"上次核对：{recorded_at or '（从未记录）'}"
              + (f" · 指纹 {recorded_print}" if recorded_print else "")
              + (f" · 备注 {recorded_note}" if recorded_note else ""))
        return 0

    db.set_setting("master_key_verified_at", utc_now(), actor="manage master-key-verified")
    db.set_setting("master_key_verified_fingerprint", fingerprint, actor="manage master-key-verified")
    if note.strip():
        db.set_setting("master_key_verified_note", note.strip()[:200],
                       actor="manage master-key-verified")
    print(f"已记下：{utc_now()} 核对过主密钥离线副本。")
    print(f"服务器这把的指纹是 {fingerprint}——"
          "请确认你刚才比的就是它（离线副本不是由这台机器保管的，这里记不下它）。")
    if recorded_print and recorded_print != fingerprint:
        print(f"注意：上一次记下的是 {recorded_print}，**与现在这把不同**——"
              "换过主密钥或恢复过旧备份的话，架上那份副本可能已经对不上了。")
    print("`python -m pilot_app.backup --check` 会显示这次记录有多旧，超过一年会提醒你。")
    return 0


def invitations(db: Database, *, limit: int = 100) -> int:
    """Report what happened to every applicant's invite, and what still cannot be known.

    Answers one question -- "did this person get their code?" -- by putting the
    three observable facts next to each other:

    * the decision and when it was taken;
    * whether the e-mail was handed to the mail server, with the Message-ID to
      quote when asking the provider what they did with it;
    * whether the code was ever **redeemed**, which is the only evidence on this
      side that a human actually read the message.

    The last line states the limit out loud. Delivery to an inbox is not
    observable from here: a 250 from the relay means our provider accepted the
    message, not that it landed. The honest ways to close that gap are the
    redemption record and asking the person. A tracking pixel would close it and
    is refused: the landing page and the privacy policy both promise there is no
    telemetry, and a read receipt is exactly that.

    Exit code is 1 when something needs a human: a failed send, or an invite that
    has been sitting unredeemed well past the point where people normally act.
    """
    rows = db.list_signup_requests(limit)
    if not rows:
        print("还没有任何邀请申请。")
        return 0

    now = dt.datetime.now(dt.timezone.utc)
    actionable = 0
    print(f"{'申请邮箱':<34}{'状态':<8}{'发码':<6}{'邮件':<8}{'已使用':<8}{'批准时间'}")
    print("-" * 96)
    for row in rows:
        status = row.get("status", "")
        if status == "pending":
            print(f"{row['email']:<34}{'待处理':<8}{'—':<6}{'—':<8}{'—':<8}{''}")
            continue
        if status == "declined":
            print(f"{row['email']:<34}{'已婉拒':<8}{'—':<6}{'—':<8}{'—':<8}{row.get('decided_at') or ''}")
            continue

        issued = "有" if row.get("invite_label") else "无"
        if db.invite_send_failed(row):
            # Same predicate the sentinel uses; see Database.invite_send_failed.
            mailed = "失败"
            actionable += 1
        elif row.get("invite_sent_at"):
            mailed = "已投递"
        else:
            mailed = "未发送（可能是有意跳过）"
        redeemer = row.get("redeemer_email") or ""
        used = "是" if row.get("invite_used_by") else "否"

        print(f"{row['email']:<34}{'已发码':<8}{issued:<6}{mailed:<8}{used:<8}{row.get('decided_at') or ''}")
        if row.get("invite_send_error"):
            print(f"    发送错误：{row['invite_send_error']}")
        if row.get("invite_message_id"):
            print(f"    Message-ID：{row['invite_message_id']}")
        if redeemer and redeemer != row["email"]:
            print(f"    由这个账号使用：{redeemer}")

        # A code nobody used after three days is usually one of three things: the
        # mail went to spam, the address was mistyped, or the person changed
        # their mind. All three are worth a look and none of them is visible from
        # a status column alone.
        decided = parse_utc(row.get("decided_at") or "")
        if not row.get("invite_used_by") and decided and (now - decided) > dt.timedelta(days=3):
            age = (now - decided).days
            print(f"    ⚠ 已发出 {age} 天仍未使用；若是首次发送，先确认他有没有收到（垃圾邮件箱最常见）")
            actionable += 1

    counts = db.signup_request_counts()
    print()
    print("统计：" + "，".join(f"{key} {value}" for key, value in sorted(counts.items())))
    print()
    print("这一页能证明什么：")
    print("  · 已投递 = 邮件服务器接受了这封信（不是「已送达」）")
    print("  · 已使用 = 本人拿到码并注册成功，这是这一侧能拿到的最强证据")
    print("  · 只有收件人本人能确认「我收到了」；要问就问，不要靠猜")
    print("  · 没有已读回执、没有追踪像素——官网和隐私政策都写着没有任何遥测")
    return 1 if actionable else 0


def check_deps(lock: str = "", timeout: int = 20) -> int:
    """按 `requirements.lock` 里**确切版本**逐个问 OSV：我们装的那一份有没有公告。

    为什么是命令而不是一句「我查过了」：结论文档不会自己变旧，包会。这条随时能重跑。

    三条判据（见 `pilot_app/deps.py` 的 docstring）：只问 lock 里的确切版本；
    **「没查到公告」与「没查成」严格分开**（后者非零退出）；只读，不升级任何东西。
    """
    path = lock or str(Path(__file__).resolve().parent / "requirements.lock")
    if not os.path.exists(path):
        print(f"找不到 lock 文件：{path}")
        return 2
    packages = deps.locked_packages(path)
    total_lines = sum(1 for line in Path(path).read_text(encoding="utf-8").splitlines()
                      if line.strip() and not line.strip().startswith("#"))
    print(f"依赖：{path}")
    print(f"  lock 里 {total_lines} 行，其中 {len(packages)} 个能按确切版本查"
          "（其余是注释或不带 == 的行，不猜）")
    if not packages:
        print("没有任何可查的钉死版本——这本身值得看一眼。")
        return 2

    result = deps.audit(packages, timeout=timeout)
    for item in result["checked"]:
        mark = "✅" if not item["advisories"] else f"⚠️  {item['advisories']} 条公告"
        print(f"  {item['package']:<20s} {item['version']:<12s} {mark}")
    for item in result["failed"]:
        print(f"  {item['package']:<20s} {item['version']:<12s} ✗ 没查成：{item['why']}")
    if result["advisories"]:
        print()
        print("公告：")
        for item in result["advisories"]:
            print(f"  · {item['package']} {item['version']}  {item['id']}  {item['severity']}")
            if item["summary"]:
                print(f"      {item['summary']}")
    print()
    if result["failed"]:
        # **「没查成」绝不当成「干净」**：与 budget 那条边界方向相反，因为代价不对称
        # ——漏报一条真公告比多让人跑一次命令严重得多。
        print(f"结论：有 {len(result['failed'])} 个包**没查成**，所以这次核对不算通过。"
              "（可能是网络、也可能是 OSV 那边的问题；隔一会儿重跑。）")
        return 1
    if result["advisories"]:
        print(f"结论：{len(result['advisories'])} 条公告要处理——升级前先看 "
              "docs/dependency-audit-2026-09-23.md 里的判据，升完把那张表与 "
              "test_dependency_audit.AUDITED 一起更新。")
        return 1
    print(f"结论：{len(packages)} 个包、按确切版本查过，0 条公告。"
          "许可证那半是人工核的（见 docs/dependency-audit-2026-09-23.md），这条命令不管。")
    return 0


def check_model(prompt: str = "只回答两个字：可用", timeout: int = 60) -> int:
    """Try the instance-wide fallback key for real.

    This key is set in an environment file and there is no button anywhere that
    exercises it, so until now the only way to learn whether it worked was for a
    real user's report to fail. A minimal call answers it in a second.

    Two rules, both from the iron list: the key is never printed, and neither is
    anything that might contain it. Provider errors sometimes quote the request
    back, so every line printed here goes through ``_scrub`` before it reaches
    the terminal -- a checker that leaks the credential it is checking would be
    worse than no checker.
    """
    connection = providers.platform_model_default()
    if connection is None:
        print("没有配置平台兜底 key（INFE_PILOT_DEFAULT_MODEL_KEY 为空或无效）。")
        print("现状：每个用户都必须自带 key。官网/隐私政策/条款/应用内若写着")
        print("「在另行通知前由管理员出钱」，那句话目前与事实不符。")
        print("配置方法：sudo bash /opt/cityu-mail-pilot/pilot_app/set_platform_key.sh")
        return 1
    key = providers.platform_model_key()
    provider = connection["provider"]
    model = connection["model"]
    base_url = connection.get("base_url") or ""
    print(f"平台兜底 key：已配置（长度 {len(key)}，内容不显示）")
    print(f"调用：{provider} / {model}" + (f" @ {base_url}" if base_url else ""))

    def _scrub(text: str) -> str:
        value = str(text)
        return value.replace(key, "***已隐藏***") if key else value

    # A three-token probe should not inherit the 300 s report budget. The module
    # global is read inside `generate`, so the only way to shorten it here is to
    # set it -- and it is put back in a `finally` because leaving it changed was
    # not a theoretical problem: it leaked into the next test module in the same
    # process, where an existing guard (`MODEL_TIMEOUT_SECONDS >= 180`) caught it.
    saved_timeout = providers.MODEL_TIMEOUT_SECONDS
    if timeout > 0:
        providers.MODEL_TIMEOUT_SECONDS = int(timeout)
    started = time.monotonic()
    try:
        try:
            result = providers.generate(
                provider=provider, model=model, api_key=key, prompt=prompt,
                base_url=base_url, max_output_tokens=64,
            )
        except Exception as exc:  # noqa: BLE001 - the CLI reports, it does not raise
            print(f"调用失败（{time.monotonic() - started:.1f}s）：{_scrub(exc)}")
            print("key 已写入但不可用。检查账号余额、key 是否被禁用、以及供应商/模型名是否匹配。")
            return 1
    finally:
        providers.MODEL_TIMEOUT_SECONDS = saved_timeout
    elapsed = time.monotonic() - started
    reply = _scrub((result.text or "").strip())
    print(f"调用成功：{elapsed:.1f}s，返回 {len(result.text or '')} 字")
    if result.usage:
        print(f"用量：{result.usage}")
    print(f"模型回复：{reply[:200] or '（空）'}")
    if key and key in reply:
        # Cannot happen after _scrub, but if the primitive is ever changed this
        # is the line that notices.
        print("警告：返回内容里出现了 key 的片段，已隐藏；请检查 provider 实现。")
    print("结论：平台兜底 key 可用，用户没配 key 时会自动用它。")
    return 0


def check_localmodel(prompt: str = "", timeout: int = 90, skip_tls: bool = False) -> int:
    """真去调一次本机大模型服务（主服务），并把这一条链的每一跳都点出来。

    为什么要有这条命令：这个服务跑在**别人的**机器 + 租来的隧道上，「服务在不在」不是
    我们这一侧能断言的事（2026-09-22 接入时 `/health` 通、`401` 形状对，都是当场实测的）。
    出报告那条路每天都用它，但没有一处能在**用户之前**说出「它现在不通」。所以：

    * 每一跳都真的发一句话（主服务，以及配了的话：付费兜底）；
    * 把 TLS 那三件事（CA 文件、钉扎指纹、证书有效期）打出来——「通」与「验过」是两件事，
      自签证书只在**指纹对上**时才算通；
    * 结论按档说清谁该去修：主服务不通 = 维护侧的事，兜底不通 = 我们自己的 key。

    不打印 key，也不打印任何可能含 key 的东西（照 `check_model` 的两条规矩）。
    """
    if skip_tls:
        # 只用于联调期判断「到底是证书不对还是服务不通」；上线不许用（交付文档 §5 同款）。
        print("⚠️  已跳过证书校验（仅联调期可用）：结论只能说明服务在应答，不能说明它是我们的服务。")
    connections = providers.platform_model_connections()
    if not connections:
        print("没有配置平台模型 key（INFE_PILOT_DEFAULT_MODEL_KEY 为空或无效）。")
        print("配置方法：sudo bash /opt/cityu-mail-pilot/pilot_app/set_platform_key.sh "
              "--provider local_openai")
        return 1

    failures: list[str] = []
    for index, connection in enumerate(connections):
        provider = str(connection.get("provider") or "")
        model = str(connection.get("model") or "")
        base_url = connection.get("base_url") or ""
        role = "主服务" if index == 0 else "兜底"
        key = providers.platform_model_key() if index == 0 else providers.platform_model_fallback_key()
        print(f"[{role}] {provider} / {model} @ {base_url or '（预设地址）'}"
              f"（key 长度 {len(key)}，内容不显示）")
        if not key:
            print(f"  ✗ 没有 key：{role}这一档不会生效。")
            failures.append(f"{role}没有 key")
            continue
        if provider == "local_openai":
            cert = providers.local_model_cert_path()
            exists = os.path.exists(cert)
            print(f"  CA 文件：{cert}（{'存在' if exists else '**读不到**'}）")
            if not exists:
                failures.append("本机服务的 CA 文件不存在")
                continue
            print(f"  钉扎指纹：{providers.local_model_fingerprint()}")
            for line in _certificate_lines(cert):
                print(f"  证书：{line}")

        saved = providers.MODEL_TIMEOUT_SECONDS
        if timeout > 0:
            providers.MODEL_TIMEOUT_SECONDS = int(timeout)
        started = time.monotonic()
        try:
            with _no_verify(skip_tls):
                result = providers.generate(
                    provider=provider, model=model, api_key=key, base_url=base_url,
                    prompt=prompt or "只回答两个字：可用", max_output_tokens=64,
                    guard_task="classify" if provider == "local_openai" else "",
                )
        except Exception as exc:  # noqa: BLE001 - CLI 报账，不抛
            print(f"  ✗ 调用失败（{time.monotonic() - started:.1f}s）：{_scrub_key(exc, key)}")
            failures.append(f"{role}调用失败：{type(exc).__name__}")
            continue
        finally:
            providers.MODEL_TIMEOUT_SECONDS = saved
        elapsed = time.monotonic() - started
        reply = _scrub_key((result.text or "").strip(), key)
        print(f"  ✓ 调用成功：{elapsed:.1f}s，返回 {len(result.text or '')} 字")
        if result.usage:
            print(f"    用量：{result.usage}")
        print(f"    模型回复：{reply[:120] or '（空）'}")
        if result.guard:
            issues = result.guard.get("issues")
            print(f"    护栏：ok={bool(result.guard.get('ok'))} task={result.guard.get('task')} "
                  f"issues={len(issues) if isinstance(issues, list) else 0} "
                  f"retried={bool(result.guard.get('retried'))} latency={result.guard.get('latency_s')}s")
        elif provider == "local_openai":
            # 护栏字段缺失不算调用失败（老版本服务可能没有），但要说出来：`x_guard.task`
            # 没人回话时，「分类/摘要被按错的任务审」这件事就没有任何证据。
            print("    护栏：响应里没有 guard 字段（服务端可能未启用护栏，或版本较旧）")

    if failures:
        print()
        print("结论：这条链上有 " + str(len(failures)) + " 档不通——" + "；".join(failures))
        print("  主服务不通 = 维护侧的事（那台盒子 / 隧道 / 证书）；兜底不通 = 我们自己的 key。")
        return 1
    print()
    print("结论：平台模型这条链每一跳都真的答话了。")
    return 0


def _certificate_lines(path: str) -> list[str]:
    """证书的 subject / 有效期 / SHA-256，读不出来就一行说明。

    用标准库解析（`ssl._ssl._test_decode_cert` 是有文档记录的用途：PEM 文件 → dict），
    不引第三方库、也不调用 openssl 二进制——这条命令要在生产机上跑，那里只有 Python。
    """
    try:
        info = ssl._ssl._test_decode_cert(path)  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001
        return [f"读不出来（{type(exc).__name__}）"]
    subject = "/".join(value for group in (info.get("subject") or ()) for key, value in group)
    lines = [f"subject={subject}"]
    lines.append(f"有效期：{info.get('notBefore', '?')} ~ {info.get('notAfter', '?')}")
    try:
        with open(path, "rb") as handle:
            pem = handle.read()
        der = ssl.PEM_cert_to_DER_cert(pem.decode())
        # 指纹算的是 DER 本身（与 `_PinnedHTTPSConnection` 比对的那份一致）。
        digest = hashlib.sha256(der).hexdigest().upper()
        lines.append("SHA-256：" + ":".join(digest[i:i + 2] for i in range(0, len(digest), 2)))
    except Exception:  # noqa: BLE001 - 指纹读不出来不影响「证书在不在」
        pass
    return lines


def _scrub_key(text: Any, key: str) -> str:
    value = str(text)
    return value.replace(key, "***已隐藏***") if key else value


def _no_verify(skip: bool):
    """临时把出站 TLS 降级成「不校验」的上下文管理器（只在 `--skip-tls` 时生效）。"""
    import contextlib

    @contextlib.contextmanager
    def _cm():
        if not skip:
            yield
            return
        original = providers.local_model_tls
        original_context = providers._context_for

        def _loose(provider: str):  # noqa: ANN001 - 只有联调期那一条路径
            return {"ca_file": "", "pin": ""} if provider == "local_openai" else original(provider)

        providers.local_model_tls = _loose
        providers._context_for = lambda ca_file: ssl._create_unverified_context()  # noqa: SLF001
        providers.reset_tls_cache()
        try:
            yield
        finally:
            providers.local_model_tls = original
            providers._context_for = original_context
            providers.reset_tls_cache()

    return _cm()


def check_native_search(provider: str = "", model: str = "", query: str = "City University of Hong Kong",
                        timeout: int = 120, keyword_limit: int = 0) -> int:
    """Prove that a provider's *own* API can search the web, in one command.

    Why this exists: the 火山方舟 path (第 16 项) could not be verified when it was
    written. Its 联网内容插件 only exists on `/api/v3/responses`, and none of this
    installation's keys is an Ark key -- checked against the real endpoint, which
    answered `AuthenticationError: The API key format is incorrect` for all of
    them. A feature whose success path was never executed is a claim, not a
    fact, so the operator (or any user with an Ark key) gets this instead of our
    assurance: one call, and the count of citations that came back.

    "The call succeeded" is not the assertion. **Zero citations is a failure
    here** -- a model that answers without searching looks exactly like a working
    one until you count the sources.
    """
    connection = providers.platform_model_default()
    provider = (provider or "").strip()
    model = (model or "").strip()
    if not provider:
        # Nothing named: check whatever the platform fallback is, model and all.
        provider = ((connection or {}).get("provider") or "volcengine_ark_responses").strip()
        model = model or ((connection or {}).get("model") or "").strip()
    # A provider named on the command line must NOT inherit the platform
    # provider's model name: sending "deepseek-flash" to Ark's endpoint is a 404
    # that reads like a broken key.
    preset = providers.MODEL_PRESETS.get(provider)
    base_url = (preset.base_url if preset else "") or (connection or {}).get("base_url") or ""
    key = providers.platform_model_key()
    if not providers.supports_native_search(provider):
        print(f"{provider} 不支持原生联网搜索（会搜索的预设：" +
              "、".join(sorted(item.id for item in providers.MODEL_PRESETS.values() if item.native_search)) + "）")
        return 2
    if not model:
        print("需要 --model：方舟要用你在控制台开通的模型 ID 或接入点 ID。")
        return 2
    if not key:
        print("没有可用的 key：这条命令用平台兜底模型 key（INFE_PILOT_DEFAULT_MODEL_KEY）。")
        return 1
    print(f"平台模型 key：已配置（长度 {len(key)}，内容不显示）")
    print(f"调用：{provider} / {model}" + (f" @ {base_url}" if base_url else ""))
    print(f"工具：web_search" + (f"（max_keyword={keyword_limit}）" if keyword_limit else "") +
          " · 查询词是固定诊断串，不取自任何用户邮件")

    def _scrub(text: str) -> str:
        value = str(text)
        return value.replace(key, "***已隐藏***") if key else value

    saved_timeout = providers.MODEL_TIMEOUT_SECONDS
    if timeout > 0:
        providers.MODEL_TIMEOUT_SECONDS = int(timeout)
    started = time.monotonic()
    try:
        try:
            result = providers.generate(
                provider=provider, model=model, api_key=key, prompt=query, base_url=base_url,
                config={"search_max_keyword": keyword_limit} if keyword_limit else None,
                max_output_tokens=256, native_search=True,
            )
        except Exception as exc:  # noqa: BLE001 - the CLI reports, it does not raise
            print(f"调用失败（{time.monotonic() - started:.1f}s）：{_scrub(exc)}")
            print("常见原因：key 不是方舟的 key、账号没开通「联网内容插件」、模型 ID 不对。")
            return 1
    finally:
        providers.MODEL_TIMEOUT_SECONDS = saved_timeout
    elapsed = time.monotonic() - started
    text = _scrub((result.text or "").strip())
    print(f"调用成功：{elapsed:.1f}s，返回 {len(text)} 字，来源 {len(result.sources)} 条")
    if result.usage:
        print(f"用量：{result.usage}")
    print(f"模型回复：{text[:200] or '（空）'}")
    for item in result.sources[:3]:
        print(f"  · {item.get('title', '')[:60]} — {item.get('url', '')}")
    if not result.sources:
        print("结论：**没有拿到任何引用来源**，所以这次通话没有证明它会联网搜索。")
        print("  可能是：没开通联网内容插件、模型不支持 web_search、或来源的返回形状与我们解析的不一样。")
        return 1
    print("结论：原生联网搜索可用（这次调用真的带回了引用来源）。")
    return 0


def check_search(query: str = "City University of Hong Kong", timeout: int = 60) -> int:
    """Try the instance-wide search key for real, with one query.

    The sibling of :func:`check_model` and it exists for the same reason: the key
    lives in an environment file and nothing in the UI exercises it. The query is
    a fixed public string, not anything taken from a user's mail -- a diagnostic
    must not be the one thing that sends somebody's subject line to a third party.

    Same rule as the model checker: nothing printed here may contain the key.
    """
    connection = providers.platform_search_default()
    if connection is None:
        print("没有配置平台搜索 key（INFE_PILOT_DEFAULT_SEARCH_KEY 为空或无效）。")
        print("现状：每个用户都必须自带搜索 key，没配的人报告照常生成、只是没有联网核实。")
        print("官网/隐私政策/条款/应用内若写着「管理员提供搜索 key」，那句话目前与事实不符。")
        print("配置方法：sudo bash /opt/cityu-mail-pilot/pilot_app/set_platform_key.sh --search")
        return 1
    key = providers.platform_search_key()
    provider = connection["provider"]
    print(f"平台搜索 key：已配置（长度 {len(key)}，内容不显示）")
    print(f"调用：{provider} · 查询词是固定诊断串，不取自任何用户邮件")

    def _scrub(text: str) -> str:
        value = str(text)
        return value.replace(key, "***已隐藏***") if key else value

    saved_timeout = providers.MODEL_TIMEOUT_SECONDS
    if timeout > 0:
        providers.MODEL_TIMEOUT_SECONDS = int(timeout)
    started = time.monotonic()
    try:
        try:
            results = providers.web_search(provider, key, query, count=3)
        except Exception as exc:  # noqa: BLE001 - the CLI reports, it does not raise
            print(f"调用失败（{time.monotonic() - started:.1f}s）：{_scrub(exc)}")
            print("key 已写入但不可用。检查这个 key 是否开通了搜索服务、以及供应商是否选对。")
            return 1
    finally:
        providers.MODEL_TIMEOUT_SECONDS = saved_timeout
    elapsed = time.monotonic() - started
    print(f"调用成功：{elapsed:.1f}s，返回 {len(results)} 条结果")
    for item in results[:3]:
        print(f"  · {_scrub(item.get('title', ''))[:60]} — {_scrub(item.get('url', ''))[:70]}")
    if not results:
        print("调用通了但没返回结果：多半是查询词或计费额度的问题，不是 key 坏了。")
    print("结论：平台搜索 key 可用，没配搜索的用户会自动用它。")
    return 0


def check_alerts(db: Database, *, dry_run: bool = False) -> int:
    """Run the sentinel by hand. Useful for verifying thresholds after a deploy."""
    if dry_run:
        # 主密钥指纹要像哨兵那样算出来再喂进去，否则这条命令**漏报**离线副本那条发现项
        # （`evaluate()` 拿不到指纹时不猜结论，而真实的那一轮是有指纹的）。诊断与被诊断的
        # 东西必须看到同一批事实——上一版 `--dry-run` 把「已知晓」的算成「会告警」，就是同
        # 一类错的另一面。密钥读不到（比如没带环境文件）时才退回 None：那时它确实判断不了。
        try:
            fingerprint: Optional[str] = SecretBox.from_environment().fingerprint()
        except Exception:  # noqa: BLE001 - a diagnostic must still print everything else
            fingerprint = None
        if fingerprint is None:
            print("（读不到主密钥：这一轮不含「离线副本」那条检查）")
        # 连通性探测也要**真的做一遍**：这一条与别的不一样，它读的是"此刻那台在不在"，
        # 而 `run_checks` 每 5 分钟做的就是它。不探的话 `--dry-run` 会漏掉这一条，
        # 而"漏掉"在这里的表现正是**看着一切正常**——诊断与被诊断必须看到同一批事实。
        reachable = tierhealth.probe()
        if reachable is None:
            print("（这一档实例没有「本机那台作为主服务」：跳过连通性探测）")
        findings = alerting.evaluate(db, master_key_fingerprint=fingerprint,
                                     local_model_reachable=reachable)
        known = {row["key"]: row for row in db.list_alert_states()}
        verdicts = {row["key"]: row for row in alerting.plan(findings, known)}
        for item in findings:
            verdict = verdicts.get(item["key"], {}).get("label", "")
            print(f"[{item['severity']}] {item['key']}\t{item['title']}\t{item['detail']}"
                  + (f"\t→ {verdict}" if verdict else ""))
        mailing = [row for row in verdicts.values()
                   if row["state"] in alerting.MAILING_PLAN_STATES]
        muted = [row for row in verdicts.values() if row["state"] == "muted"]
        quiet = len(verdicts) - len(mailing) - len(muted)
        print(f"DRY RUN：{len(findings)} 项活跃异常——{len(mailing)} 项现在会发信，"
              f"{len(muted)} 项已被「已知晓」静音，{quiet} 项这一轮不发（只在面板 / 等汇总窗口 / 没变化）；"
              "未发送邮件，也未写入 alert_state。")
        return 0
    box = SecretBox.from_environment()
    result = alerting.run_checks(db, box)
    print(f"哨兵结果：{result}")
    return 1 if result["errors"] else 0


def platform_cost(db: Database, *, refresh_now: bool = False, as_json: bool = False,
                  now: Optional[dt.datetime] = None) -> int:
    """管理员那把 key 的钱：本月代付了多少、账上还剩多少、见底时会不会拦。

    这是在服务器上回答「这个月的钱花到哪了、够不够」的那一条命令。默认**只读我们存下来的
    那条余额记录**（worker 每半小时刷新一次）；``--refresh`` 才当场去读一次（几毫秒的只读
    HTTPS，不产生模型调用，也不打印任何 key）。

    非零退出的条件与哨兵那几条发现项**完全一致**——判据在 `budget.state()` 里只写一处，
    所以命令行与告警不可能给出两个答案。``now`` 只给测试用（命令行不暴露它）：
    「本月」与「读数多旧」都相对于它。余额过期不算失败之外的任何事：那只是「这个数有点旧」。
    """
    if refresh_now:
        fresh = budget.refresh(db, now=now)
        if fresh is None:
            print("这次没读到余额：没配平台 key、这家供应商没有余额接口，或者请求失败了"
                  "（原因在日志里；这不影响出报告）。")
    current = budget.state(db, now=now)
    month = current["spend"]
    if as_json:
        print(json.dumps({
            "configured": current["configured"], "provider": current["provider"],
            "month": month["month"], "since": month["since"],
            "platform": {key: month[key] for key in
                         ("calls", "cost", "currency", "unpriced_calls",
                          "unknown_calls", "unknown_cost")},
            "balance": ({"at": (current["reading"] or {}).get("at"),
                         "age_seconds": int(((current["reading"] or {}).get("age")
                                             or dt.timedelta()).total_seconds()),
                         "is_available": (current["reading"] or {}).get("is_available"),
                         "balances": (current["reading"] or {}).get("balances")}
                        if current["reading"] else None),
            "thresholds": {"cost_alert_usd": current["cost_alert"],
                           "balance_floor": current["balance_floor"],
                           "balance_currency": current["balance_currency"]},
            "verdicts": {key: current[key] for key in
                         ("over_cost", "balance_low", "exhausted", "stale")},
        }, ensure_ascii=False, indent=2))
        return _platform_cost_exit(current)

    print(f"平台 key 的钱（香港时间账期 {month['month']}）")
    # 「配了吗」与「花了吗」是两件事（2026-09-22 起平台有两档：本机那台不花钱、
    # 付费兜底才花钱）。两边都没有才是真的没什么可看——只按 `configured` 判断的话，
    # 一个本机服务排在第一位的实例会在这里说「没有管理员代付这回事」，
    # 而同一件事在 `token_usage` 里明明白白记着钱。
    if not current["configured"] and int(month.get("calls") or 0) <= 0:
        print("  这台机器没配平台兜底模型 key：没有「管理员代付」这回事，"
              "调用只会记在用户自己的 key 上。")
        return 0
    line = (f"  本月代付        {month['calls']} 次调用 · "
            f"{budget.money(month['cost'], month['currency'])}")
    if month["unpriced_calls"]:
        line += f"（其中 {month['unpriced_calls']} 次没有单价，实际更高）"
    print(line)
    if month.get("local_calls"):
        # 本机那台主服务不花钱，所以它不在上面那个金额里；不单独说一句的话，
        # 「次数」会被读成「就这么几次调用」。
        print(f"                  另有 {month['local_calls']} 次走的是运营者自建的模型服务"
              "（不产生供应商账单），不计入上面这个数")
    if month["unknown_calls"]:
        print(f"                  另有 {month['unknown_calls']} 次调用没记是谁的 key 付的"
              f"（{budget.money(month['unknown_cost'], month['currency'])}），不计入上面这个数")
    if month.get("search_calls"):
        # 2026-09-26：这一类调用**以前一行都不记**，于是「余额为什么掉得比账本快」说不清。
        # 搜索按次计费、我们手上没有可核实的价目 → 不编金额，只报次数。
        print(f"                  另有 {month['search_calls']} 次联网搜索（同一个账号按次计费，"
              "我们手上没有可核实的价目 → **未计价**，所以上面那个金额里没有它）")
    if month.get("agent_calls"):
        print(f"                  另有 {month['agent_calls']} 次是运维助手"
              f"（{budget.money(month['agent_cost'], month['currency'])}，记在 agent_reports）"
              "，同一把 key 花的钱，不计入上面这个数")
    if month.get("search_calls") or month.get("agent_calls"):
        print("                  说明：上面「本月代付」只数**模型调用**；把这三行加起来才接近"
              "余额实际掉的速度（对不上就说明还有别的调用方在用这个账号）。")
    reading_now = current["reading"]
    if not current["balance_readable"]:
        print(f"  账上余额        读不到：供应商「{current['provider']}」没有余额查询接口"
              "（目前只有 DeepSeek 有），所以余额这一项在这里没有数据。")
    elif reading_now is None:
        print("  账上余额        还没有任何读数 —— worker 每半小时读一次；"
              "现在可以跑 `platform-cost --refresh` 读一次。")
    else:
        age = reading_now["age"]
        minutes = "" if age is None else f"{int(age.total_seconds() // 60)} 分钟前"
        print(f"  账上余额        {reading_now['at']} 读到（{minutes}）· "
              f"可用={reading_now['is_available']}")
        for item in reading_now["balances"]:
            print(f"                  {item['currency']}  {item['total_text']}"
                  f"（充值 {item['topped_up']} + 赠送 {item['granted']}）")
    print("  ── 警戒线 ──")
    print(f"  本月费用        {budget.money(current['cost_alert'])}"
          "（INFE_PILOT_PLATFORM_COST_ALERT，0 = 关掉这一条）")
    print(f"  余额            {budget.money(current['balance_floor'], current['balance_currency'])}"
          "（INFE_PILOT_PLATFORM_BALANCE_FLOOR，0 = 关掉；按账上那个币种解读，"
          "与上面那个美元的数不能相减）")
    verdicts = []
    if current["over_cost"]:
        verdicts.append("本月费用越过警戒线（去看后台「用量」面板是谁在花）")
    if current["exhausted"]:
        verdicts.append("余额已见底——借用管理员 key 的账号现在会被拦下，不再花钱")
    elif current["balance_low"]:
        verdicts.append("余额低于警戒线（还没拦，先去充值）")
    if current["stale"]:
        verdicts.append("余额读数过期或还没读到（过期时闸门放行）")
    print(f"  判定            {'；'.join(verdicts) if verdicts else '一切正常'}")
    return _platform_cost_exit(current)


def _platform_cost_exit(current: dict[str, Any]) -> int:
    """与哨兵同一批评据：有话说就非零退出（好接进别的脚本/告警）。"""
    return 1 if any(current[key] for key in
                    ("over_cost", "balance_low", "exhausted", "stale")) else 0


def restore_drill(db: Database, backup_path: str = "") -> int:
    """Prove a backup can actually be restored — without touching live data.

    "We have backups" and "we can restore" are different claims, and only the
    second one matters on the day it matters. This opens a copy of the newest
    backup, checks SQLite's own integrity verdict, and then decrypts a real
    mailbox credential out of it with the *current* master key. That last step
    is the one that catches the failure a structural check cannot see: a backup
    taken under a master key that no longer exists restores perfectly and is
    still worthless.

    Read-only with respect to production: everything happens on a copy in a
    temporary directory.
    """
    backup_dir = Path(os.environ.get("INFE_PILOT_BACKUP_DIR", "/var/backups/cityu-mail-pilot"))
    if backup_path:
        candidate = Path(backup_path)
    else:
        copies = sorted(backup_dir.glob("pilot-*.sqlite3"), reverse=True)
        if not copies:
            print(f"在 {backup_dir} 里找不到 pilot-*.sqlite3。")
            return 1
        candidate = copies[0]
    if not candidate.is_file():
        print(f"找不到备份文件：{candidate}")
        return 1

    print(f"备份文件：{candidate}")
    print(f"大小    ：{candidate.stat().st_size / 1024:.0f} KB")
    # The fingerprint is printed first and always, because it is the one thing the
    # operator can compare against the copy in their hand. It identifies the key
    # without revealing it -- see security.key_fingerprint.
    try:
        print(f"主密钥指纹：{SecretBox.from_environment().fingerprint()}"
              "（与你自己保存的那份对照；它不等于密钥本身）")
    except Exception as exc:  # noqa: BLE001 - the drill below reports the real problem
        print(f"主密钥指纹：读不出来（{exc}）")

    with tempfile.TemporaryDirectory() as work:
        copy = Path(work) / "restored.sqlite3"
        shutil.copy2(candidate, copy)

        try:
            with sqlite3.connect(copy) as connection:
                verdict = connection.execute("PRAGMA integrity_check").fetchone()[0]
                if verdict != "ok":
                    print(f"✗ 完整性检查未通过：{verdict}")
                    return 1
                print("✓ 完整性检查：ok")
                restored_counts = {
                    table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("users", "messages", "reports")
                }
        except sqlite3.DatabaseError as exc:
            # A badly truncated file makes integrity_check *raise* rather than
            # return a verdict — and a corrupt backup is exactly when someone
            # runs this, so it must report, not traceback.
            print(f"✗ 打不开这份备份：{exc}")
            return 1

        # The decisive step: can the running configuration still read the data?
        restored = Database(str(copy))
        box = SecretBox.from_environment()
        secrets_read = 0
        unreadable = 0
        for row in restored.list_users_overview():
            mailbox = restored.get_mailbox(row["id"])
            if not mailbox or not mailbox.get("encrypted_password"):
                continue
            try:
                box.decrypt(mailbox["encrypted_password"], context=f"mailbox:{row['id']}")
                secrets_read += 1
            except Exception as exc:
                unreadable += 1
                print(f"✗ 无法用当前主密钥解密 {row['email']} 的邮箱授权码：{type(exc).__name__}")
        if unreadable:
            print("✗ 这份备份解不开——很可能它是在另一把主密钥下生成的。")
            return 1
        print(f"✓ 用当前主密钥成功解密 {secrets_read} 个邮箱授权码（未打印任何内容）")

        live = {
            "users": len(db.list_users_overview()),
        }
        print(f"  用户  备份 {restored_counts['users']} / 线上 {live['users']}")
        print(f"  邮件  备份 {restored_counts['messages']}")
        print(f"  报告  备份 {restored_counts['reports']}")

    print("\n✓ 恢复演练通过：这份备份可以被读回，并且与当前主密钥匹配。")
    print("  真正的恢复步骤见 docs/restore-drill.md（停服务 → 换库 → 起服务 → 核对）。")
    return 0


# Readings that must exist wherever /proc does. A null here means the parser or
# the host is broken -- not "nothing to report", which is what a missing number
# in the panel would otherwise look like.
REQUIRED_READINGS = (
    "host.cpu_percent",
    "host.memory.total_mb",
    "host.disk.total_gb",
    "host.uptime_seconds",
    "host.network.rx_kbps",
    "host.network.tx_kbps",
    "process.rss_mb",
    "process.threads",
)


def _dig(snapshot: dict, path: str):
    value: Any = snapshot
    for key in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def check_providers(db: Database, *, timeout: int = providercheck.PROBE_TIMEOUT) -> int:
    """问每一家邮箱：你现在还让人用授权码登录吗？（不发凭据，只看服务器怎么说）

    2026-09-16 的「outlook 那件事」是**用户先撞上**的：微软个人版关掉了基础认证，
    用户按向导拿到授权码、填进来、永远被拒，而我们这边一切正常。这个命令把顺序倒过来
    ——定期问一次，谁把门关了就提前知道，然后要么把它标成「用不了」（带一条出路），
    要么补上 OAuth 那条路。

    判定只看**明确的拒绝**（``LOGINDISABLED``）：163 的 CAPABILITY 里根本没有
    ``AUTH=PLAIN``，可真机用密码登录它是照常受理的 —— 把「没提到」当拒绝会误杀一家
    好端端的服务商。连不上既不算通过也不算关门，单独报出来。
    """
    results = providercheck.check_all(
        lambda host, port: providercheck.probe_imap(host, port, timeout=timeout))
    providercheck.save(db, results)
    print("服务商授权码通道检查（" + dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds") + " UTC）")
    for item in results:
        mark = {providercheck.PASSWORD_OK: "✓ 还能用授权码",
                providercheck.OAUTH_ONLY: "✗ 只提供 OAuth（用授权码永远连不上）",
                providercheck.UNREACHABLE: "? 这次没连上"}[item["state"]]
        expect = providercheck.expected_state(item["blocked"])
        note = ""
        if item["state"] != providercheck.UNREACHABLE and item["state"] != expect:
            note = "  ← 和我们写的不一样"
        print(f"  {item['label']:<22} {item['host']:<26} {mark}{note}")
        if item["state"] != expect:
            print(f"      原始应答：{item['raw'][:150]}")
    bad = providercheck.drift(results)
    unreachable = [item for item in results if item["state"] == providercheck.UNREACHABLE]
    if bad:
        print(f"\n有 {len(bad)} 家的实际状态与预期不符 —— 上面带「←」的那些。")
        for item in bad:
            if item["state"] == providercheck.OAUTH_ONLY:
                print(f"  · {item['label']} 不再支持授权码：要么在 mailpresets 里给它加 blocked_reason"
                      f"（像 outlook 那样，向导会给出一条出路），要么补 OAuth。")
            else:
                print(f"  · {item['label']} 又能用密码登录了：如果那条封禁是我们加的，可以撤掉。")
    if unreachable:
        print(f"\n{len(unreachable)} 家这次没连上（不算结论，下一天会再问）："
              + "、".join(item["label"] for item in unreachable))
    return 1 if bad else 0


def check_mailboxes(db: Database, *, timeout: int = mailboxcheck.PROBE_TIMEOUT) -> int:
    """逐个检查**每一个已配置的转发邮箱**：这个授权码现在到底还能不能用。

    2026-09-18 到 09-20 的三次「授权码用不了」是**三件不同的事**（我们的主机填错 /
    163 真的拒了那串码 / 微软根本不给用授权码）。混成一句话就会一直修不好，所以这条
    命令的输出是**按人一行、按档收尾**的：主机填错单独一档，绝不掉进「授权码被拒」。

    只读到底：探针是 `ID → LOGIN → EXAMINE INBOX → LOGOUT`（`EXAMINE` 就是只读打开），
    不开箱取信、不 STORE、不 DELETE；**这个函数一行都不写库**，所以可以反复跑。

    输出里没有授权码、没有主密钥、没有密文；地址一律走 `_mask`。服务器原话在打印前
    还会再过一遍 `_scrub`（它会把授权码和完整地址擦掉）——IMAP 服务器不会回显密码，
    但「不会」不是一条能被验证的性质。
    """
    rows = db.all_mailboxes()
    try:
        box = SecretBox.from_environment()
    except Exception as exc:  # noqa: BLE001 - 缺主密钥时要说人话，不要抛栈
        print("无法读取主密钥（" + str(exc) + "）。这条命令要解开每个邮箱的授权码，"
              "所以必须带着 INFE_PILOT_MASTER_KEY 运行："
              "sudo systemd-run --pipe --wait --collect --uid=cityumail "
              "--property=EnvironmentFile=/etc/cityu-mail-pilot/pilot.env "
              "--working-directory=/opt/cityu-mail-pilot "
              "/opt/cityu-mail-pilot/.venv/bin/python -m pilot_app.manage check-mailboxes")
        return 2

    entries: list[dict[str, Any]] = []
    secrets_: list[str] = []
    addresses: list[str] = []
    for row in rows:
        address = str(row.get("email") or "")
        addresses.append(address)
        password, problem = "", ""
        try:
            password = box.decrypt(row["encrypted_password"], context=f"mailbox:{row['user_id']}")
            secrets_.append(password)
        except Exception as exc:  # noqa: BLE001 - 一行的密文坏了不该让整条命令挂掉
            problem = f"存着的授权码解不开：{exc}"
        entries.append({
            "email": address, "imap_host": row["imap_host"], "imap_port": row["imap_port"],
            "password": password, "enabled": bool(row.get("enabled", 1)), "problem": problem,
        })

    results = mailboxcheck.check_all(entries, timeout=timeout)
    for item in results:
        item["reason"] = _scrub(item["reason"], secrets_, addresses)
        item["words"] = _scrub(item["words"], secrets_, addresses)

    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    paused = sum(1 for item in results if not item["enabled"])
    print(f"转发邮箱授权码检查（{stamp} UTC）· {len(results)} 个已配置邮箱"
          + (f"（其中 {paused} 个已暂停）" if paused else ""))
    print()
    header = ("地址", "域名", "主机", "登录")
    widths = (26, 16, 6, 10)
    print("  " + "".join(_pad(cell, width) for cell, width in zip(header, widths)) + "结论")
    print("  " + "─" * 72)
    for item in results:
        host_mark = {True: "✓", False: "✗", None: "?"}[item["host_ok"]]
        row = ("  " + _pad(_mask(item["email"]), widths[0])
               + _pad(item["domain"] or "—", widths[1])
               + _pad(host_mark, widths[2])
               + _pad(_LOGIN_MARKS.get(item["probe_state"], item["probe_state"]), widths[3])
               + mailboxcheck.LABELS.get(item["tier"], item["tier"])
               + ("" if item["enabled"] else "（已暂停）"))
        print(row)

    counts: dict[str, int] = {}
    for item in results:
        counts[item["tier"]] = counts.get(item["tier"], 0) + 1
    if counts:
        print()
        print("  合计：" + " · ".join(
            f"{mailboxcheck.LABELS[tier]} {count}"
            for tier, count in sorted(counts.items(), key=lambda kv: -kv[1])))

    # 域名不在任何预置里：**单独报一档**。我们判断不了用户自己填的主机对不对，
    # 所以这一档的每一行都要点名，让人工看一眼——哪怕它这次登录成功了。
    unknown = [item for item in results if item["host_ok"] is None]
    if unknown:
        print()
        print("  域名不在任何预置里（用户自己填的服务器，我们无法判断对错，人工看一眼）：")
        for item in unknown:
            detail = ""
            if item["probe_state"] != mailboxcheck.OK:
                detail = " —— " + (item["words"] or item["reason"])
            print(f"   · {_mask(item['email'])}  主机 {item['imap_host']}:{item['imap_port']}"
                  f"  登录 {_LOGIN_MARKS.get(item['probe_state'], item['probe_state'])}{detail}")

    broken = [item for item in results
              if item["tier"] != mailboxcheck.OK and item["host_ok"] is not None]
    if broken:
        print()
        print("  明细（不是「能用」的那些）：")
        for item in broken:
            line = f"   · {_mask(item['email'])}：{mailboxcheck.LABELS.get(item['tier'])}"
            if item["reason"]:
                line += " —— " + item["reason"]
            print(line)
            print(f"       主机 {item['imap_host']}:{item['imap_port']}"
                  f" · 服务器原话：{item['words'] or '（没有原话）'}")
            print(f"       下一步：{mailboxcheck.NEXT_STEPS.get(item['tier'], '')}")

    print()
    print("结论：" + mailboxcheck.summarize(results))
    if unknown:
        print(f"      另外 {len(unknown)} 个域名的服务器不在预置里，已单独列出，"
              f"建议人工看一眼主机填得对不对。")
    bad = [item for item in results if item["tier"] != mailboxcheck.OK]
    return 1 if bad else 0


def check_metrics(db: Database) -> int:
    """Take one real reading on this machine and say whether it is plausible.

    The browser suite skips the CPU/memory assertions on a Mac because there is
    no /proc there, so those code paths were only ever exercised against a
    fixture tree -- never against the kernel they exist to read. This is the
    other half of that pair: run it on a box that really has /proc, print every
    reading, and fail loudly if one that must exist is missing.

    It reads the live database for the application numbers, but only with the
    same read-only queries the admin panel uses. Nothing here writes.
    """
    from pilot_app import metrics as metrics_mod

    with db.connect() as connection:
        snapshot = metrics_mod.collect(connection)
    host = snapshot.get("host", {})
    process = snapshot.get("process", {})
    application = snapshot.get("application", {})
    memory = host.get("memory", {}) or {}
    disk = host.get("disk", {}) or {}
    network = host.get("network", {}) or {}

    print(f"平台        {host.get('platform')} · {host.get('cpu_count')} 核 · Python {host.get('python')}")
    print(f"CPU         {host.get('cpu_percent')} %   负载 {host.get('load')}")
    print(f"内存        {memory.get('used_mb')} / {memory.get('total_mb')} MB"
          f"（{memory.get('percent')} %）· 交换 {memory.get('swap_used_mb')} / {memory.get('swap_total_mb')} MB")
    print(f"磁盘 /      {disk.get('used_gb')} / {disk.get('total_gb')} GB（{disk.get('percent')} %）")
    print(f"网络        rx {network.get('rx_kbps')} KB/s · tx {network.get('tx_kbps')} KB/s")
    print(f"开机时长    {host.get('uptime_seconds')} s（本进程 {process.get('uptime_seconds')} s）")
    print(f"本进程      rss {process.get('rss_mb')} MB · 线程 {process.get('threads')}"
          f" · 打开文件 {process.get('open_files')}")
    print(f"应用        近 1 小时邮件 {application.get('messages_1h')} · 队列 {application.get('queue')}"
          f" · 失败 {application.get('failed')} · 中位耗时 {application.get('median_latency_seconds')} s")
    print(f"数据库      {application.get('database_size_mb')} MB（WAL {application.get('wal_size_mb')} MB）")

    problems = [f"host 整块报错：{host['error']}"] if "error" in host else []
    for path in REQUIRED_READINGS:
        if _dig(snapshot, path) is None:
            problems.append(f"{path} 是 None —— 这台机器有 /proc，不该读不到")
    if problems:
        print("\n不合格：")
        for item in problems:
            print(f"  - {item}")
        return 1
    print("\n✓ 读数齐全，没有一个是 None —— 这台机器上的 /proc 路径是通的。")
    return 0


def poll_interval(db: Database, *, as_json: bool = False) -> int:
    """把「轮询间隔」这一个旋钮的账算出来：现在多快、代价多大、规模上来会怎样。

    **为什么要有这条命令**（2026-09-26）：生产在 2026-09-24 把这一个值从 60 秒改成 300 秒，
    理由是按 **1500 个邮箱**算的（1500 ÷ 60 = 25 次登录/秒，一轮 94 秒 > 60 秒跑不完），
    而当时真实的规模是 15 个邮箱。**为 80 倍于当时的规模提前付的代价，账是用户在日常里付的**：
    2026-09-26 用户报「从收到转发邮件到收到处理好的邮件太久了」，实测那一段就是
    「等下一次轮询」（中位 113–659 秒；端到端里模型只占 6–10 秒）。

    这个值是 `INFE_PILOT_POLL_SECONDS`，落在 `/etc/cityu-mail-pilot/pilot.env`，改完要重启
    **worker 与 web 两个单元**（后台「正常收信」的新鲜度窗口按它算，只重启 worker 会让那个数
    显示错 —— 2026-09-24 真的发生过）。而在那之前，**没有任何地方告诉你现在这个值意味着
    多少次登录、一轮跑不跑得完**。这条命令就是那个地方。

    只读：只数库里的邮箱。不连任何邮箱、不写库、不需要主密钥。
    """
    from pilot_app import mailio as mailio_mod
    from pilot_app import worker as worker_mod

    rows = [row for row in db.all_mailboxes() if int(row.get("enabled") or 0)]
    interval = worker_mod.POLL_SECONDS
    slower = [row for row in rows if mailio_mod.minimum_poll_seconds(row) > interval]
    budget = worker_mod.poll_budget(len(rows), interval=interval,
                                    workers=worker_mod.POLL_WORKERS)
    scale = worker_mod.poll_budget(worker_mod.SCALE_TARGET_MAILBOXES, interval=interval,
                                   workers=worker_mod.POLL_WORKERS)
    if as_json:
        print(json.dumps({"now": budget, "at_scale_target": scale,
                          "paused_or_disabled_ignored": True,
                          "slower_provider_floor": len(slower)}, ensure_ascii=False))
        return 0 if budget["fits"] else 1

    print(f"轮询间隔    {interval} 秒（INFE_PILOT_POLL_SECONDS；代码默认 60）"
          f" · 轮询线程 {budget['workers']}")
    print(f"在用邮箱    {budget['mailboxes']} 个"
          + (f"（其中 {len(slower)} 个有更慢的供应商下限，Gmail 是 900 秒）" if slower else ""))
    print(f"登录量      {budget['logins_per_second']:.2f} 次/秒"
          f" ≈ {budget['logins_per_day']:,.0f} 次/天（按 QQ/163 那一档算；Gmail 更少）")
    print(f"一轮轮询    约 {budget['round_seconds']} 秒"
          f"（ceil({budget['mailboxes']} ÷ {budget['workers']}) × 每邮箱约 "
          f"{budget['cost_seconds']} 秒的估计）"
          + ("≤ 间隔 ✓" if budget["fits"] else " > 间隔 ✗ **跑不完**"))
    print(f"发现延迟    0–{budget['worst_delay_seconds']:.0f} 秒"
          f"（均匀到达时中位约 {budget['median_delay_seconds']:.0f} 秒）"
          f" + 模型 6–10 秒")
    print(f"到 {scale['mailboxes']} 个邮箱、同样的设置："
          f"{scale['logins_per_second']:.0f} 次/秒 · 一轮约 {scale['round_seconds']} 秒"
          + ("≤ 间隔 ✓" if scale["fits"] else " > 间隔 ✗ —— 那时要么抬 POLL_WORKERS，"
             "要么把间隔拉长（2026-09-24 就是拉了间隔，2026-09-26 又拉回来了）"))
    if not budget["fits"]:
        print("\n✗ 一轮轮询比间隔还长：实际间隔会被拉成一轮的真实耗时，"
              "邮箱会一路显示成「轮询停了」。先把间隔调大或把轮询线程调多。")
        return 1
    print("\n✓ 一轮跑得完。要更快就把间隔调小（代价是登录量按比例上去）；"
          "供应商侧的风控阈值四家都没公布，见 docs/poll-latency-2026-09-26.md。")
    return 0


# Identifies this program to the download host. See the note where it is used:
# DB-IP refuses urllib's default agent with a 403.
_DOWNLOAD_USER_AGENT = ("Mozilla/5.0 (compatible; cityu-mail-pilot; "
                        "+https://github.com/JennieCN/cityu-mail-pilot)")


def geoip_update(dataset: str, *, month: str = "", source: str = "", out: str = "") -> int:
    """Download DB-IP Lite and build the offline lookup database.

    Run on the machine that serves the site (``systemd-run`` as the service
    user), because the file it writes is what ``analytics`` reads on the request
    path. The build goes to a temp file first: a truncated download must not be
    able to leave a half-written database where a lookup would read a wrong
    country out of it.

    Needs no account and no key -- DB-IP Lite is free under CC BY 4.0, and the
    only obligation is the attribution line the console already shows.
    """
    import gzip
    import shutil as _shutil
    import urllib.request

    target = out or geoip.default_path()
    stamp = str(month or "").strip()
    temporary = tempfile.mkdtemp(prefix="geoip-")
    try:
        if source:
            csv_path = source
            if not os.path.isfile(csv_path):
                print(f"找不到输入文件：{csv_path}")
                return 1
            print(f"数据源      : {csv_path}（本地文件）")
        else:
            if not stamp:
                today = dt.datetime.now(dt.timezone.utc).date()
                stamp = today.strftime("%Y-%m")
            candidates = [stamp]
            # DB-IP publishes on the 1st; on the 1st (or a missed month) the
            # current month may not exist yet, so fall back one month rather
            # than failing the update.
            year, mon = int(stamp[:4]), int(stamp[5:7])
            previous = (year - 1, 12) if mon == 1 else (year, mon - 1)
            candidates.append(f"{previous[0]:04d}-{previous[1]:02d}")
            csv_path = ""
            for candidate in candidates:
                url = geoip.fetch_url(dataset, candidate)
                csv_path = os.path.join(temporary, f"dbip-{dataset}-lite-{candidate}.csv.gz")
                print(f"下载        : {url}")
                request = urllib.request.Request(url)
                # DB-IP answers 403 to urllib's default user agent and 200 to a
                # browser's -- found on the production box, because the local
                # test had used --source and never exercised this line. The
                # string stays honest about what it is (the "compatible" form is
                # the conventional way for a non-browser to identify itself)
                # rather than pretending to be Chrome.
                request.add_header("User-Agent", _DOWNLOAD_USER_AGENT)
                try:
                    with urllib.request.urlopen(request, timeout=180) as response, \
                            open(csv_path, "wb") as handle:
                        _shutil.copyfileobj(response, handle)
                except Exception as exc:
                    print(f"  取不到（{type(exc).__name__}：{exc}），试上一个月")
                    csv_path = ""
                    continue
                print(f"  已下载 {os.path.getsize(csv_path) / 1e6:.1f} MB")
                stamp = candidate
                break
            if not csv_path:
                print("下载失败：两个月都取不到。检查这台机器能不能出网。")
                return 1

        # DB-IP ships both address families in one file, and the builder packs
        # whichever it finds, so the second path stays empty on purpose.
        counts = geoip.build(csv_path, "", target, dataset=dataset)
    except Exception as exc:
        print(f"构建失败：{type(exc).__name__}: {exc}")
        return 1
    finally:
        _shutil.rmtree(temporary, ignore_errors=True)

    size_mb = os.path.getsize(target) / 1e6
    print(f"数据集      : {dataset}" + (f"（{stamp}）" if stamp else ""))
    print(f"写入        : {target}（{size_mb:.1f} MB）")
    print(f"区段        : {counts['rows']} 条（IPv4 {counts['v4']} / IPv6 {counts['v6']}），"
          f"跳过 {counts['skipped']} 行")
    print("归属        : IP Geolocation by DB-IP (https://db-ip.com) —— CC BY 4.0，控制台已署名")
    probe = geoip.lookup("8.8.8.8", target)
    print(f"自检        : 8.8.8.8 -> {probe.get('country_name') or '（查不到，数据可能不对）'}")
    if not probe:
        return 1
    print("下个月同日再跑一次即可更新（数据每月 1 号发布）。")
    return 0


def analytics_import_nginx(database: Database, *, paths: list[str], since: str = "",
                           until: str = "", limit: int = 0, apply: bool = False) -> int:
    """Import nginx's own access log into the visit statistics.

    Preview by default, like every other command here that writes: the operator
    should be able to see "this would add N visits, M of them robots" before
    anything lands in the database.

    The addresses in the log are digested on the way in and never stored; the
    country/city is resolved now, while the address is still in hand, which is
    the only moment it can be done at all.
    """
    wanted = list(paths) or sorted(
        str(p) for p in Path("/var/log/nginx").glob("access.log*") if p.is_file())
    if not wanted:
        print("没有找到日志文件。用 --path 指定，或确认 /var/log/nginx/access.log 存在。")
        return 1
    missing = [p for p in wanted if not os.path.isfile(p)]
    if missing:
        # Say it plainly instead of importing the subset and reporting success:
        # a silently skipped file is a silently wrong total.
        print("这些日志读不到（权限？路径？）：")
        for item in missing:
            print(f"  · {item}")
        print("提示：/var/log/nginx 通常只有 root 和 adm 组能读，用 sudo 跑，或用 systemd-run --uid=cityumail"
              " 配合把日志复制出来。")
        return 1

    stats: dict[str, Any] = {}
    imported = skipped_pages = robots = 0
    countries: dict[str, int] = {}
    try:
        secrets_box = SecretBox.from_environment()
    except Exception as exc:
        print(f"读不到主密钥（导入需要它来算访客摘要）：{exc}")
        return 1

    # Files that exist but cannot be opened are the common case here -- nginx
    # logs are root:adm 640 -- and "read 0 of 3" with no reason is the kind of
    # output that sends somebody hunting for a bug in the importer instead.
    unreadable = [path for path in wanted if not os.access(path, os.R_OK)]
    if unreadable:
        print("这些日志存在但读不到（多半是权限）：")
        for item in unreadable:
            print(f"  · {item}")
        print("  nginx 的日志通常是 root:adm 640。做法：sudo cp 到 /tmp 再 chown 给服务账号，"
              "然后用 --path 指过去（AGENTS.md §5 有现成命令）。")
        if len(unreadable) == len(wanted):
            return 1

    geo_ready = geoip.available()
    # 运营者清掉的地址：导入不能把它们搬回来（日志里没有会话，只有地址，所以这里
    # 是唯一能认出他的地方）。这不算「重复」，单独报一条，否则数字对不上。
    ignored = database.ignored_page_view_clients()
    batch: list[dict[str, Any]] = []
    stored = duplicates = operators = 0
    for record in nginxlog.iter_records(wanted, since=since, until=until, limit=limit, stats=stats):
        row = analytics.import_row(
            secrets_box, ip=record["ip"], path=record["path"], status=record["status"],
            method=record["method"], referrer=record["referrer"],
            user_agent=record["user_agent"], created_at=record["time"],
        )
        if row is None:
            skipped_pages += 1
            continue
        if ignored and row["client_hash"] in ignored:
            operators += 1
            continue
        imported += 1
        if row["bot"]:
            robots += 1
        elif geo_ready:
            name = row["country_name"] or "（未知）"
            countries[name] = countries.get(name, 0) + 1
        if apply:
            batch.append(row)
            if len(batch) >= 500:
                added, skipped = database.record_page_views(batch)
                stored += added
                duplicates += skipped
                batch = []
    if apply and batch:
        added, skipped = database.record_page_views(batch)
        stored += added
        duplicates += skipped

    print(f"日志文件    : {len(wanted)} 个（读成功 {stats.get('files', 0)} 个）")
    print(f"行数        : {stats.get('lines', 0)}（解析 {stats.get('parsed', 0)}，"
          f"跳过无法解析 {stats.get('skipped', 0)}）")
    print(f"时间范围    : {stats.get('oldest') or '—'} → {stats.get('newest') or '—'}（UTC）")
    print(f"页面访问    : {imported} 条（其中机器人 {robots} 条，非页面请求 {skipped_pages} 条被丢弃）")
    if operators:
        print(f"运营者      : 跳过 {operators} 条——这些地址已经在「访问统计」里被清掉了，"
              "导入不会把它们带回来")
    if not geo_ready:
        print("地理        : 未配置——先跑 manage geoip-update，否则国家和城市这两列会是空的")
    elif countries:
        top = sorted(countries.items(), key=lambda item: item[1], reverse=True)[:8]
        print("国家（人）  : " + "、".join(f"{name} {count}" for name, count in top))
    if apply:
        # New / already-there are reported separately because "导入完成" hiding a
        # re-import that added nothing is how a doubled number gets believed.
        print(f"写库        : 新增 {stored} 条，已存在 {duplicates} 条（重复导入不会重复计数）")
    else:
        print("写库        : 预演，没有写任何东西（加 --apply 才写）")
    return 0


def _operator_identity() -> str:
    """Name the *shell* that ran a write, because there is no session to name.

    The console records which admin pressed a button; a command run over SSH has
    no such person attached. Inventing one (say the first admin address) would
    put a name in the audit log that nobody verified, so the row carries what is
    actually known: the OS account and the host the command ran on.
    """
    try:
        name = os.environ.get("SUDO_USER") or getpass.getuser()
    except Exception:  # no passwd entry / no LOGNAME: still not worth failing a reset
        name = "unknown"
    try:
        host = socket.gethostname()
    except Exception:
        host = "unknown"
    return f"{name}@{host}"[:120]


def _stdout_is_a_journal() -> bool:
    """True when our **own** stdout is the systemd journal, not just any unit's.

    A transient unit started **without** ``--pipe`` has its output appended to
    the journal: it outlives the operator's terminal and is readable by anyone
    who can read logs. A temporary password printed there would survive long
    after the user changed it, so this command refuses instead.

    The variable alone is not proof of that. systemd sets ``JOURNAL_STREAM``
    for *its* service, and children **inherit the environment**: GitHub's runner
    agent is itself a systemd unit, so every CI step sees ``JOURNAL_STREAM``
    while its stdout is really an ordinary pipe. Trusting the variable made the
    command refuse to write on CI while it behaved correctly on the server
    (2026-09-19: eight tests red on the runner, all green locally). So the check
    is on the file descriptor: systemd formats the value as ``设备:inode``, and
    the journal connection is the socket with exactly that inode. Measured on
    the production server: fd1 is ``socket:[15215159]`` with
    ``JOURNAL_STREAM=10:15215159`` when run without ``--pipe``, and an ordinary
    ``pipe:[…]`` with the variable unset when run with it.
    """
    stream = os.environ.get("JOURNAL_STREAM", "")
    if not stream:
        return False
    try:
        fd1 = os.readlink("/proc/self/fd/1")
    except OSError:  # no /proc (macOS): there is no journal there either
        return False
    inode = stream.rpartition(":")[2]
    return bool(inode) and fd1 == f"socket:[{inode}]"


def create_admin(database: Database, user_email: str, password: str, *, apply: bool = False) -> int:
    """给环境变量点名的**保留地址**建号（开放注册已经被闸门堵住了）。

    为什么要有这条命令：**开放注册绝不能产生管理员**（2026-09-26 外部审计的 P1）。
    `_is_admin()` 按邮箱**字符串**认人，而注册不验证邮箱归属 —— 所以「抢在主人之前
    用他的地址注册」曾经是一条真的提权路。`web.register` 里的闸门把那条路堵了，
    这条命令是留给**真正拥有这台机器的人**的出路：它需要 shell 权限，
    而 shell 权限正是我们唯一能当作"归属证明"的东西。

    与 `Database.grant_admin` 的分工：那边只给**已有**账号加权限（它的 docstring 已经
    写明了同一个道理：给一个还没注册的地址授权，等于给"以后可能有人用错拼的地址注册"
    留了一个静默的承诺）；这边负责**先把账号建出来**，正是那条规则堵住的那一步。

    不做的事：不打印密码、不把密码写进审计、不接受密码出现在 argv 里
    （CLI 从 stdin 读，见下）。默认只预演，`--apply` 才写。
    """
    address = str(user_email or "").strip()
    if "@" not in address or len(address) > 254:
        print("请给一个像邮箱的地址（--email）。")
        return 2
    if not password:
        print("没有读到密码。密码从 stdin 读一行（不回显），不要写在命令行上。")
        return 2
    from .security import hash_password  # 与 web 层同一个哈希函数，不另写一份

    existing = database.find_user_for_login(address)
    if existing:
        print(f"{_mask(address)} 已经有账号了。")
        plan = "把管理员权限记到它的 is_admin 上（环境变量那份本来就是按邮箱生效的）。"
    else:
        plan = "建一个新账号，并把它记成管理员。"
    print(f"将要做：{plan}")
    print("  写库前会先备份（部署脚本负责）；这条命令自己只写 users 一行 + profiles 一行。")
    if not apply:
        print("这是预演（没有加 --apply）。")
        return 0
    if not existing:
        try:
            # `max_users=None`：名额上限管的是"还能收多少用户"，**不该拦管理员自己的号**。
            user = database.create_user(address, hash_password(password), "")
        except ValueError as exc:
            print(f"建号失败：{exc}")
            return 2
        print(f"已建号：{_mask(address)}")
    else:
        user = existing
    try:
        row = database.grant_admin(address)
    except (KeyError, ValueError) as exc:
        print(f"授权失败：{exc}")
        return 2
    if int(row.get("is_admin") or 0):
        print(f"{_mask(address)} 现在是管理员（is_admin=1）。")
    print("别忘了：环境变量 INFE_PILOT_ADMIN_EMAILS 里也要有它，或者走后台的授权按钮。")
    return 0


def reset_password(database: Database, user_email: str, *, note: str = "",
                   apply: bool = False) -> int:
    """Give one existing user a fresh temporary password, from the shell.

    Why this exists: the service deliberately has **no** self-service reset loop.
    There is no second channel to a user that we have verified — mailing a reset
    token to the private mailbox would turn the mailbox we only ever *read* into
    an authentication factor for the account, and the school address is not ours
    to write to either. So the honest answer to "我忘了密码" is a human, and this
    command is that human's tool.

    Why the operator's shell and not the admin console: this hands out a
    credential to somebody else's account. Console access is a web session, and
    a stolen admin cookie must not be enough to take over an account silently --
    it costs the same thing the master key costs, which is shell access to the
    server. Every run leaves an audit row either way.

    What it will not do: print a password hash, mail a password, write one into
    the audit log, or accept one as an argument. The plaintext exists only in
    this process's stdout, once, and the operator is told to hand it over in
    person. The old password stops working in the same transaction, and every
    session of that user is revoked -- "I reset it but the old phone still shows
    his mail" is the failure this prevents.

    Writes nothing without ``--apply`` (house rule: writes default to a preview).
    """
    if apply and _stdout_is_a_journal():
        # 先拒绝、再写库：密码写进去了却没能交到人手里，账号就变成了谁也进不去的状态。
        print("这次输出正被 systemd 写进 journal（日志），临时密码会留在那里——比你这块终端活得久，"
              "任何能看日志的人都读得到。已拒绝执行，什么都没有改。")
        print("请加上 --pipe 重跑，让输出只回到你的终端：")
        print("  sudo systemd-run --pipe --wait --collect --uid=cityumail \\")
        print("    --property=EnvironmentFile=/etc/cityu-mail-pilot/pilot.env \\")
        print("    --working-directory=/opt/cityu-mail-pilot \\")
        print("    /opt/cityu-mail-pilot/.venv/bin/python -m pilot_app.manage reset-password \\")
        print(f"    --user-email {user_email} --apply")
        return 2
    user = database.find_user_for_login(user_email)
    if not user:
        # Deleted accounts land here too (the lookup skips them): a reset cannot
        # resurrect anything, so it must not look like it did.
        print(f"没有用 {_mask(user_email)} 注册的账号（已删除的账号也不会在这里找回）。"
              "先确认邮箱拼写，或用后台的用户列表核对。")
        return 2
    status = str(user.get("status") or "active")
    status_text = {"active": "启用", "paused": "已暂停"}.get(status, status)
    mailbox = database.get_mailbox(user["id"])
    sessions = database.count_sessions(user["id"])
    print(f"账户          : {_mask(user['email'])}（{status_text}）")
    print(f"注册于        : {user.get('created_at') or '—'}（UTC）")
    seen = user.get("last_seen_at")
    print(f"上次登录      : {seen + '（UTC）' if seen else '没有记录（只统计注册之后的活动）'}")
    print(f"已登录会话    : {sessions} 个")
    print(f"私人邮箱      : {'已配置' if mailbox else '还没配'}")
    if not apply:
        print("结果          : 预演，没有改任何东西（加 --apply 才真的重设并撤销会话）")
        return 0

    password = generate_temporary_password()
    database.set_password(user["id"], hash_password(password))
    removed = database.revoke_sessions(user["id"])
    database.record_audit(
        action="password_reset_by_operator",
        actor_email=f"命令行（{_operator_identity()}）",
        target_user_id=user["id"], target_email=user["email"],
        detail=f"revoked={removed}" + (f"；备注={note}" if note else ""),
        client="cli",
    )
    print(f"临时密码      : {password}")
    print("结果          : 已写入（旧密码立刻失效）")
    print("")
    print("下一步：")
    print("  1. 把上面那行临时密码当面/微信/短信发给本人——不要发到群里，它现在就是账号本身。")
    print(f"  2. 他用原来的邮箱 + 这个临时密码登录（已撤销 {removed} 个已登录会话，旧设备要重新登录）。")
    print("  3. 进去以后到「更多 → 账户安全」把它改成自己的密码。")
    print("")
    print("上面那行密码只在这次输出里出现，库里存的是哈希，事后找不回来；没抄下来就再跑一次。")
    if status == "paused":
        print("⚠️  这个账号是「已暂停」：登录进去也收不到信、没有报告。他可以在「账户安全」里"
              "自己点「恢复」，或由你在后台点「恢复用户」。")
    if not mailbox:
        print("⚠️  这个账号还没配私人邮箱：登录后先走完设置向导，否则不会有任何报告。")
    print("ℹ️  如果他刚才连续输错 8 次以上，网页在 15 分钟内会一律回「登录尝试过多」——"
          "那不是密码又不对，等一刻钟即可。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    invite = sub.add_parser("create-invite")
    invite.add_argument("--label", default="pilot")
    invite.add_argument("--days", type=int, default=7)
    create_admin_parser = sub.add_parser(
        "create-admin",
        help="给环境变量点名的保留地址建号（开放注册进不来；默认只预演）",
    )
    create_admin_parser.add_argument("--email", required=True,
                                     help="管理员邮箱；应当与 INFE_PILOT_ADMIN_EMAILS 里的一致")
    create_admin_parser.add_argument("--apply", action="store_true", help="真的写库；省略时只预演")
    reset = sub.add_parser(
        "reset-password",
        help="给某个已注册用户重设一个临时密码（只有能登服务器的人用得了；默认只预演）",
    )
    reset.add_argument("--user-email", required=True, help="用户注册时用的私人邮箱")
    reset.add_argument("--note", default="", help="记进审计的备注（例如用户是在哪儿求助的）")
    reset.add_argument("--apply", action="store_true", help="真的重设；省略时只预演")
    migrate = sub.add_parser(
        "migrate-legacy-imap-state",
        help="把旧 imap-state.json 的精确 UID 集合安全迁入已暂停的试点账户",
    )
    migrate.add_argument("--user-email", required=True, help="试点网页登录邮箱")
    migrate.add_argument("--state", required=True, help="旧 imap-state.json 的只读副本")
    migrate.add_argument("--uid-validity", required=True, help="网页只读邮箱测试返回的 UIDVALIDITY")
    migrate.add_argument("--apply", action="store_true", help="实际写入；省略时只预览")
    migrate.add_argument(
        "--lookback-hours", type=int,
        default=int(os.environ.get("INFE_PILOT_INITIAL_LOOKBACK_HOURS", "48")),
        help="迁移后第一次回扫的窗口小时数（默认取 INFE_PILOT_INITIAL_LOOKBACK_HOURS 或 48）；"
             "用于判断有多少空洞会被覆盖",
    )
    migrate.add_argument(
        "--allow-unverified-uid-validity", action="store_true",
        help="服务器读不到 UIDVALIDITY 或与传入值不一致时仍强制继续（不推荐，会让去重失效）",
    )
    sub.add_parser("generate-master-key")
    e2e = sub.add_parser(
        "verify-e2e",
        help="真实链路验证：只读抓信 → 模型/搜索 → 渲染，默认不发信、不写游标",
    )
    e2e.add_argument("--user-email", required=True, help="试点网页登录邮箱（不是转发邮箱）")
    e2e.add_argument("--limit", type=int, default=1, help="最多处理几封新邮件")
    e2e.add_argument("--pull", type=int, default=0,
                     help="忽略游标，从邮箱里取最近 N 封真实邮件做验证（只读，不推进游标）")
    e2e.add_argument("--pull-date", default="",
                     help="配合 --pull/--store：把这些真实邮件落成这一天的邮件（YYYY-MM-DD），"
                          "以便真实地验证当日简报")
    e2e.add_argument("--store", action="store_true",
                     help="配合 --pull-date：把真实邮件与即时报告写入数据库（默认不落库）。"
                          f"会写成「已下发」的行，因此必须先设 {STORE_TARGET_ENV} 指名丢弃用的库")
    e2e.add_argument("--send", action="store_true",
                     help="真的发送报告（默认只渲染；已有成功报告的邮件永不重发）")
    e2e.add_argument("--force-resend", action="store_true",
                     help="配合 --send：允许重发已有成功报告的邮件，仅用于验收取证")
    e2e.add_argument("--send-digest", action="store_true",
                     help="配合 --send：把今天的每日简报也真实发一次（--store --pull-date 时改用那一天）")
    e2e.add_argument("--pause", action="store_true",
                     help="验证期间临时禁用该邮箱，结束后恢复，确保没有第二个 worker 消费")
    e2e.add_argument("--measure", action="store_true",
                     help="强制重新生成以测量耗时/Token，但不发送、不落库（用于延迟调优）")
    e2e.add_argument("--measure-model", default="",
                     help="配合 --measure：临时用这个模型名量测（不修改用户配置）")
    e2e.add_argument("--show-body", action="store_true", help="额外打印报告纯文本预览（含邮件内容，注意隐私）")
    diag = sub.add_parser(
        "diagnose-forwarding",
        help="只读检查某个邮箱是否重复收到同一封邮件（只打印邮件头，不打印正文）",
    )
    diag.add_argument("--email", default="", help="要检查的邮箱地址（例如 operator@example.com）；用 --password-file 时可省略")
    diag.add_argument("--host", default="imap.qq.com")
    diag.add_argument("--port", type=int, default=993)
    diag.add_argument("--folder", default="INBOX")
    diag.add_argument("--limit", type=int, default=40)
    diag.add_argument("--password-env", default="INFE_DIAG_PASSWORD",
                      help="从该环境变量读取授权码；省略时用隐藏输入提示")
    diag.add_argument("--env-file", default="", help="可选的 pilot.env，用于读取 INFE_PILOT_* 默认值")
    diag.add_argument("--password-file", default="",
                      help="只读该文件取授权码（0600）；支持单行只放密码，或 email=/password= 两行格式")
    unit_fail = sub.add_parser(
        "notify-unit-failure",
        help="systemd OnFailure= 处理器：把某个单元失败的事实发给管理员（输出已脱敏）",
    )
    unit_fail.add_argument("--unit", required=True, help="失败的单元名，由 OnFailure=...@%%n 传入")
    unit_fail.add_argument("--lines", type=int, default=30, help="附带的日志行数上限")
    alerts_parser = sub.add_parser(
        "check-alerts",
        help="手动跑一次巡检哨兵；--dry-run 只打印会告警什么，不发信也不写状态",
    )
    alerts_parser.add_argument("--dry-run", action="store_true",
                               help="只列出当前判定结果，不发送、不改动 alert_state")
    providers_parser = sub.add_parser(
        "check-providers",
        help="问每一家邮箱「还让不让用授权码登录」（不发凭据；不一致时非零退出）",
    )
    providers_parser.add_argument("--timeout", type=int, default=providercheck.PROBE_TIMEOUT,
                                  help=f"单次连接超时秒数（默认 {providercheck.PROBE_TIMEOUT}）")
    mailboxes_parser = sub.add_parser(
        "check-mailboxes",
        help="逐个真探每一个已配置的转发邮箱（只读）：这个授权码还能不能用、"
             "主机填对没有、该谁去修",
    )
    mailboxes_parser.add_argument("--timeout", type=int, default=mailboxcheck.PROBE_TIMEOUT,
                                  help=f"单个邮箱的连接超时秒数（默认 {mailboxcheck.PROBE_TIMEOUT}）")
    invitations_parser = sub.add_parser(
        "invitations",
        help="查每个申请者的邀请码到底发出去了没有、有没有被用掉",
    )
    invitations_parser.add_argument("--limit", type=int, default=100, help="最多看多少条申请")
    cost_parser = sub.add_parser(
        "platform-cost",
        help="管理员那把 key 的钱：本月代付了多少、账上还剩多少、见底时会不会拦（只读）",
    )
    cost_parser.add_argument("--refresh", action="store_true",
                             help="现在真去读一次余额（默认只打印 worker 上次读到的那个数）")
    cost_parser.add_argument("--json", action="store_true", help="输出 JSON，给脚本用")
    key_copy = sub.add_parser(
        "master-key-verified",
        help="记下「今天拿离线副本和服务器比过指纹」——主密钥不在任何备份里，这是唯一能老化的一件事",
    )
    key_copy.add_argument("--note", default="", help="可选备注（存在哪里、谁核对的）")
    key_copy.add_argument("--show", action="store_true",
                          help="只打印当前记录的核对时间与指纹，不写任何东西")
    drill = sub.add_parser(
        "restore-drill",
        help="验证最新备份能不能真的恢复：完整性 + 用当前主密钥解密（只读，不动线上数据）",
    )
    drill.add_argument("--backup", default="", help="指定某个备份文件；省略时用备份目录里最新的那份")
    # Named with a suffix on purpose: a bare `check_model` here shadows the
    # function of the same name and the dispatch then calls the parser.
    check_model_parser = sub.add_parser(
        "check-model",
        help="验证实例级兜底模型 key 能不能真的调用（只发一句、不打印 key）",
    )
    check_model_parser.add_argument("--prompt", default="只回答两个字：可用",
                                    help="发给模型的最小提示词")
    check_model_parser.add_argument("--timeout", type=int, default=60, help="最长等待秒数")
    deps_parser = sub.add_parser(
        "check-deps",
        help="按 requirements.lock 的确切版本逐个问 OSV：我们装的那一份有没有公告（只读）",
    )
    deps_parser.add_argument("--lock", default="", help="默认用 pilot_app/requirements.lock")
    deps_parser.add_argument("--timeout", type=int, default=20, help="每个包最长等待秒数")
    local_parser = sub.add_parser(
        "check-localmodel",
        help="真调一次本机大模型服务（主服务）与付费兜底，逐跳报出结论（不打印 key）",
    )
    local_parser.add_argument("--prompt", default="", help="发给模型的最小提示词（默认只问「可用」）")
    local_parser.add_argument("--timeout", type=int, default=90, help="最长等待秒数（本机服务建议 ≥90）")
    local_parser.add_argument("--skip-tls", action="store_true",
                              help="仅联调期：跳过证书校验（上线不许用，见交接文档 §5）")
    native_parser = sub.add_parser(
        "check-native-search",
        help="验证某个供应商自己的联网搜索能不能真的带回引用来源（方舟的原生联网插件）",
    )
    native_parser.add_argument("--provider", default="",
                               help="默认取平台兜底 key 的供应商；方舟用 volcengine_ark_responses")
    native_parser.add_argument("--model", default="",
                               help="模型 ID 或接入点 ID（方舟必填）")
    native_parser.add_argument("--query", default="City University of Hong Kong",
                               help="诊断用的固定查询词（不要填用户邮件里的内容）")
    native_parser.add_argument("--timeout", type=int, default=120, help="最长等待秒数")
    native_parser.add_argument("--keyword-limit", type=int, default=0,
                               help="可选：方舟联网插件的单轮关键词上限（1–50），0 表示不发这个字段")
    check_search_parser = sub.add_parser(
        "check-search",
        help="验证实例级兜底搜索 key 能不能真的调用（固定诊断查询，不打印 key）",
    )
    check_search_parser.add_argument("--query", default="City University of Hong Kong",
                                     help="诊断用的固定查询词（不要填用户邮件里的内容）")
    check_search_parser.add_argument("--timeout", type=int, default=60, help="最长等待秒数")
    sub.add_parser(
        "check-metrics",
        help="在真机上采一次主机指标并核对读数（浏览器套件跳过的那几条由它负责）",
    )
    poll_parser = sub.add_parser(
        "poll-interval",
        help="算「轮询间隔」这一个旋钮的账：登录量、一轮跑不跑得完、发现延迟、规模上来会怎样",
    )
    poll_parser.add_argument("--json", action="store_true", help="给脚本读的机器可读输出")
    geo = sub.add_parser(
        "geoip-update",
        help="下载 DB-IP Lite 并建出离线国家/城市库（访问统计的“地址”靠它，不用注册）",
    )
    geo.add_argument("--dataset", choices=["country", "city"], default="country",
                     help="country=国家（4.5 MB，默认）；city=国家+城市（85 MB，慢很多）")
    geo.add_argument("--month", default="", help="下载哪个月份，YYYY-MM；默认本月，取不到就退上个月")
    geo.add_argument("--source", default="", help="用本地已有的 CSV（.csv 或 .csv.gz），不联网")
    geo.add_argument("--out", default="", help="输出路径；默认 INFE_PILOT_GEOIP_DB 或 /var/lib/...")
    importer = sub.add_parser(
        "analytics-import-nginx",
        help="把 nginx 访问日志导进访问统计（默认只预演，--apply 才写）",
    )
    importer.add_argument("--path", action="append", default=[],
                          help="日志路径，可重复；默认 /var/log/nginx/access.log*")
    importer.add_argument("--since", default="", help="只导入这一天及以后（UTC，YYYY-MM-DD）")
    importer.add_argument("--until", default="", help="只导入这一天及以前（UTC，YYYY-MM-DD）")
    importer.add_argument("--limit", type=int, default=0, help="最多导入多少条（0=不限）")
    importer.add_argument("--apply", action="store_true", help="真的写库；省略时只预演")
    args = parser.parse_args()
    if args.command == "generate-master-key":
        print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())
        return 0
    if args.command == "diagnose-forwarding":
        if args.env_file:
            _load_env_file(args.env_file)
        email_address = args.email
        password = os.environ.get(args.password_env, "")
        if args.password_file:
            file_email, file_password = read_credential_file(args.password_file)
            email_address = email_address or file_email
            password = password or file_password
        if not email_address:
            print("请用 --email 指定要检查的邮箱，或在凭据文件里写 email=。")
            return 2
        if not password:
            import getpass
            password = getpass.getpass("请输入该邮箱的 IMAP 授权码（输入时不显示）：")
        if not password:
            print("没有提供授权码。")
            return 2
        return diagnose_forwarding(email_address, password, host=args.host, port=args.port,
                                   folder=args.folder, limit=args.limit)
    if args.command == "check-model":
        # Dispatched before the database is opened: this check needs no storage,
        # and requiring one made it fail on a host where the app was not
        # installed yet -- which is exactly when someone is setting a key.
        return check_model(args.prompt, args.timeout)
    if args.command == "check-deps":
        return check_deps(args.lock, args.timeout)
    if args.command == "check-localmodel":
        return check_localmodel(args.prompt, args.timeout, skip_tls=args.skip_tls)
    if args.command == "check-search":
        return check_search(args.query, args.timeout)
    if args.command == "check-native-search":
        return check_native_search(args.provider, args.model, args.query, args.timeout,
                                   max(0, min(args.keyword_limit, 50)))
    if args.command == "geoip-update":
        # Before the database is opened: this builds a file of its own and must
        # work on a host where the application database cannot be reached.
        return geoip_update(args.dataset, month=args.month, source=args.source, out=args.out)
    db = Database(os.environ.get("INFE_PILOT_DB", DEFAULT_DB_PATH))
    db.initialize()
    if args.command == "invitations":
        return invitations(db, limit=args.limit)
    if args.command == "platform-cost":
        return platform_cost(db, refresh_now=args.refresh, as_json=args.json)
    if args.command == "create-admin":
        # 密码**只能**从标准输入读：写进 argv 就会进进程表、shell 历史与 journal。
        # 预演时不需要真密码，但也不能是空串（免得被"没读到密码"那条拒绝挡住预演）。
        password = _prompt_password("管理员密码（不回显）：") if args.apply else "x" * 12
        return create_admin(db, args.email, password, apply=args.apply)
    if args.command == "reset-password":
        return reset_password(db, args.user_email, note=args.note, apply=args.apply)
    if args.command == "master-key-verified":
        return master_key_verified(db, note=args.note, show=args.show)
    if args.command == "check-alerts":
        return check_alerts(db, dry_run=args.dry_run)
    if args.command == "restore-drill":
        return restore_drill(db, args.backup)
    if args.command == "check-metrics":
        return check_metrics(db)
    if args.command == "poll-interval":
        return poll_interval(db, as_json=args.json)
    if args.command == "check-providers":
        return check_providers(db, timeout=max(3, int(args.timeout or providercheck.PROBE_TIMEOUT)))
    if args.command == "check-mailboxes":
        return check_mailboxes(db, timeout=max(3, int(args.timeout or mailboxcheck.PROBE_TIMEOUT)))
    if args.command == "analytics-import-nginx":
        return analytics_import_nginx(db, paths=args.path, since=args.since, until=args.until,
                                      limit=max(0, int(args.limit or 0)), apply=args.apply)
    if args.command == "notify-unit-failure":
        return notify_unit_failure(db, args.unit, lines=args.lines)
    if args.command == "verify-e2e":
        return verify_e2e(db, args.user_email, args.limit, args.send, args.show_body,
                          force_resend=args.force_resend, send_digest=args.send_digest,
                          pause=args.pause, pull=args.pull, pull_date=args.pull_date,
                          store=args.store, measure=args.measure,
                          measure_model=args.measure_model)
    if args.command == "migrate-legacy-imap-state":
        user = db.find_user_for_login(args.user_email)
        if not user:
            parser.error("找不到该试点用户。")
        uids = read_legacy_processed_uids(args.state)
        mailbox = db.get_mailbox(user["id"])
        if not mailbox:
            parser.error("该用户还没有配置私人邮箱，无法核对 UIDVALIDITY。")

        # 缺口 B：把 UIDVALIDITY 与服务器真实值比对。占位记录按 UIDVALIDITY 参与唯一键，
        # 填错会让去重整体失效并把旧邮件全部重发，因此这里必须 fail-closed。
        verified = ""
        holes: list[int] = []
        holes_outside = 0
        probe_error = ""
        try:
            box = SecretBox.from_environment()
            password = box.decrypt(mailbox["encrypted_password"], context=f"mailbox:{user['id']}")
            probe = mailio.probe_mailbox(mailbox, password, lookback_hours=args.lookback_hours)
            verified = str(probe["uid_validity"])
            processed = set(uids)
            holes = [uid for uid in probe["present"] if uid not in processed]
            recent = set(probe["recent"])
            holes_outside = sum(1 for uid in holes if uid not in recent)
        except Exception as exc:  # 网络/凭据问题不应该伪装成"校验通过"
            probe_error = str(exc)

        print(f"用户            : {user['email']}")
        print(f"旧 UID          : {len(uids)} 个，范围 {uids[0]}..{uids[-1]}")
        print(f"传入 UIDVALIDITY: {args.uid_validity}")
        print(f"服务器 UIDVALIDITY: {verified or '（无法读取：' + probe_error + '）'}")

        if verified:
            if verified != args.uid_validity and not args.allow_unverified_uid_validity:
                parser.error(
                    "传入的 --uid-validity 与服务器实际值不一致——这会让占位记录落在错误的去重空间，"
                    "导致旧邮件被重新处理并重复发送。请改用服务器返回值；确有必要时加 "
                    "--allow-unverified-uid-validity 强制继续。"
                )
            print(f"空洞（存在但未处理）: {len(holes)} 个，其中 {len(holes) - holes_outside} 个在 "
                  f"{args.lookback_hours} 小时回扫窗口内")
            if holes_outside:
                print(f"⚠️  有 {holes_outside} 个空洞早于回扫窗口，迁移后不会被补做。"
                      f"如需覆盖，请把 INFE_PILOT_INITIAL_LOOKBACK_HOURS 调到能覆盖最老空洞的值，"
                      f"或手工补做这些 UID：{holes[:20]}")
        else:
            if not args.allow_unverified_uid_validity:
                parser.error(
                    "无法从服务器读取 UIDVALIDITY（" + probe_error + "）。为避免重复发信，"
                    "请确认该邮箱可只读连接后重试；确有必要时加 --allow-unverified-uid-validity 强制继续。"
                )
            print("⚠️  未能校验 UIDVALIDITY（已显式允许）；去重是否成立取决于你填写的值。")

        if not args.apply:
            print("DRY RUN：确认账户已暂停、UIDVALIDITY 与上面一致后，加 --apply 执行。")
            return 0
        result = db.seed_legacy_processed_uids(
            user["id"], args.uid_validity, uids,
            verification="server" if verified == args.uid_validity else "operator-override",
        )
        print(
            "迁移完成：读取 {seen} 个，新增 {inserted} 个，已存在 {already_present} 个。"
            "邮箱游标已设为安全重扫模式；现在可恢复账户。".format(**result)
        )
        return 0
    code = secrets.token_urlsafe(18)
    expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=max(1, args.days))
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO invites(code_hash,label,expires_at) VALUES(?,?,?)",
            (token_hash(code), args.label[:100], expires.isoformat(timespec="seconds")),
        )
    print(code)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
