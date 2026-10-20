"""Operator alerting: mail the admin when something is actually broken.

Why this lives in the worker instead of a monitoring stack
---------------------------------------------------------
The production box is 2 vCPU / 2 GB serving two real users, and the four things
worth watching are already computed here: ``web._service_health()`` derives
stale mailboxes, queue depth and failed reports, and ``metrics.host_metrics()``
derives disk usage. A monitoring daemon would add a process, a database and a
dependency tree to re-derive numbers this code already has. Every candidate was
measured and rejected on that basis (uptime-kuma's container was reported at
800 MB; netdata's own README claims 150 MiB resident; healthchecks wants
PostgreSQL and fifteen packages). The full comparison is in
``docs/open-source-recon-2026-09-14.md``.

The failure this is really about
--------------------------------
``certbot.timer`` and ``cityu-mail-pilot-backup.timer`` can fail silently. A
failed backup is a data-loss risk and an expired certificate takes the whole
site down, and until now neither would have told anybody. Those two are covered
by the systemd ``OnFailure=`` handler, which is a *separate* path because it
still works when this worker is the thing that died.

Design
------
:func:`evaluate` is the pure decision function: a database in, findings out.
Everything that touches the outside world (disk usage, the TLS handshake) is
passed *in*, so tests can drive every threshold without a full disk, a clock or
a socket.

:func:`run_checks` is what the worker calls. It de-duplicates: a problem mails
once, then stays quiet for ``INFE_PILOT_ALERT_REPEAT_SECONDS`` unless the detail
changed, and clearing it sends exactly one recovery notice. State lives in the
``alert_state`` table so a worker restart does not re-send everything.
"""

from __future__ import annotations

import datetime as dt
import html
import logging
import os
import re
import socket
import ssl
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Iterable

from . import agent, backup, budget, groupqr, mailio, metrics
from . import providercheck, tierhealth
from .database import Database, parse_utc
from .security import SecretBox


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(os.environ.get(name, str(default)))))
    except (TypeError, ValueError):
        return default


# Master switch, so an operator can silence the sentinel without a redeploy.
ALERTS_ENABLED = os.environ.get("INFE_PILOT_ALERTS", "1") != "0"
# How often the worker runs the checks. Seconds.
ALERT_CHECK_SECONDS = _int_env("INFE_PILOT_ALERT_CHECK_SECONDS", 300, 30, 86_400)
# A problem that stays broken re-mails at most this often. Six hours: frequent
# enough to survive a lost message, rare enough that a long outage cannot bury
# the operator in its own alerting.
ALERT_REPEAT_SECONDS = _int_env("INFE_PILOT_ALERT_REPEAT_SECONDS", 6 * 3600, 60, 30 * 86_400)
# No poll attempt at all for this long means the poller thread is wedged or the
# worker is gone, not that one IMAP login failed (a failure still stamps
# last_polled_at and is reported through mailbox_error instead).
ALERT_STALE_MINUTES = _int_env("INFE_PILOT_ALERT_STALE_MINUTES", 15, 5, 1440)
# A mailbox polled every 15 minutes is not "stalled" after 15 minutes, so the
# threshold has to scale with whatever interval that provider asks for. This
# multiple is what turns "no poll yet" into "the poller is probably wedged".
STALE_INTERVAL_MULTIPLE = _int_env("INFE_PILOT_ALERT_STALE_INTERVALS", 3, 2, 20)
ALERT_QUEUE_DEPTH = _int_env("INFE_PILOT_ALERT_QUEUE_DEPTH", 20, 1, 10_000)
ALERT_FAILED_REPORTS = _int_env("INFE_PILOT_ALERT_FAILED_REPORTS", 5, 1, 10_000)
# 多少个邮箱在同一家 IMAP 主机上同时被限流，才算「供应商在限制我们」而不是
# 「这几个账号各自有问题」。三个：两个可能是巧合（同一天改过密码、同一批被 163 要求重新登录），
# 三个互不相干的账号同时被拒，指向的就是主机侧。
PUSHBACK_MIN_MAILBOXES = _int_env("INFE_PILOT_PUSHBACK_MIN_MAILBOXES", 3, 2, 1000)
ALERT_DISK_PERCENT = _int_env("INFE_PILOT_ALERT_DISK_PERCENT", 90, 50, 100)
ALERT_CERT_DAYS = _int_env("INFE_PILOT_ALERT_CERT_DAYS", 14, 1, 365)
# Registered this long ago and still not finished. Twelve hours rather than a
# day: the operator invited these people personally, so "signed up this morning
# and never came back" is worth an evening nudge, not tomorrow's.
ALERT_SETUP_STALL_HOURS = _int_env("INFE_PILOT_ALERT_SETUP_STALL_HOURS", 12, 1, 24 * 30)
# A stalled signup is not an incident: it changes when the person acts or when
# the operator does, and it must not re-mail every six hours in the meantime.
# One reminder a day, and the key is per account so finishing one clears only
# that one.
ALERT_SETUP_REPEAT_SECONDS = _int_env("INFE_PILOT_ALERT_SETUP_REPEAT_SECONDS",
                                      24 * 3600, 3600, 30 * 86_400)
# The daily backup runs at 03:20, so 36 hours means one run was missed rather
# than one being a little late. `OnFailure=` on the unit only covers a run that
# happened and failed: a disabled timer, a host that stayed down, or a unit
# that was never re-enabled after maintenance produce no failure event at all.
# A backup that is stale stays stale for a while; re-mailing every six hours
# about the same unchanged fact is how an operator learns to skim past alerts.
ALERT_BACKUP_REPEAT_SECONDS = _int_env("INFE_PILOT_ALERT_BACKUP_REPEAT_SECONDS",
                                       24 * 3600, 3600, 30 * 86_400)
# An invite e-mail that failed leaves the applicant with nothing: no code, no
# error, no way to know they were approved. The sentinel notices within five
# minutes; this is only how often the same unchanged failure may re-mail, and
# a day is the operator's own phrasing ("每日巡检").
ALERT_INVITE_REPEAT_SECONDS = _int_env("INFE_PILOT_ALERT_INVITE_REPEAT_SECONDS",
                                       24 * 3600, 3600, 30 * 86_400)
ALERT_BACKUP_HOURS = _int_env("INFE_PILOT_ALERT_BACKUP_HOURS", 36, 2, 24 * 30)
# An offsite push that has stopped is quieter than a local backup that stopped:
# the local copy keeps succeeding, so nothing looks wrong until the day the
# machine is gone. Two days of slack, then say so.
ALERT_OFFSITE_HOURS = _int_env("INFE_PILOT_ALERT_OFFSITE_HOURS", 48, 2, 24 * 30)
# 管理员 key 的钱：金额在一个月里只增不减，一天说一次就够了（这也是运营者自己的说法，
# 「每日巡检」）。六小时一次会让一条已经知道的「本月花超了」占满收件箱。
# 客服群二维码到期：它有个固定的到期日，只在「快到了 / 过了」这两天说一次；一天一提醒足够。
ALERT_WECHAT_REPEAT_SECONDS = _int_env("INFE_PILOT_ALERT_WECHAT_REPEAT_SECONDS",
                                       24 * 3600, 3600, 30 * 86_400)
ALERT_PLATFORM_REPEAT_SECONDS = _int_env("INFE_PILOT_ALERT_PLATFORM_REPEAT_SECONDS",
                                         24 * 3600, 3600, 30 * 86_400)

# ---------------------------------------------------------------------------
# which channel a finding goes to
# ---------------------------------------------------------------------------
# Three tiers, and the rule that assigns them is **not** severity. Severity
# answers "how bad is this"; the question that decides whether to interrupt
# somebody is a different one, and on 2026-09-15 the operator got **74 alert
# e-mails in a day** from three long-known findings because the two questions
# had been conflated. The three that actually matter:
#
#   1. Will it get worse on its own?           (a certificate is on a clock)
#   2. Is it affecting users right now?        (a queue is backing up)
#   3. Will anyone else ever tell me?          (a failed invite is silent)
#
# Note where that puts things. `invite_failed` and `setup_stalled` are only
# *warnings*, but they are the two findings whose subject is a person who will
# never speak up: the applicant gets no code and no error, so silence is the
# whole symptom. They belong in the mail. `mailbox_error` for one account is
# *critical*, yet it is long-lived, already known, and now has a better home --
# it is the red 收信 light in the user list. It does not belong in the mail.
TIER_MAIL = "mail"        # send at once: on a clock, hitting users, or silent
TIER_DIGEST = "digest"    # one batched mail a day: real, but nothing waits on it
TIER_PANEL = "panel"      # console only: long-lived, known, and shown elsewhere

# Loudness, for the two places that have to put a mixed list in an order: the
# console, and the queue handed to the assistant. One definition, because "what
# does the operator see first" and "whose analysis gets one of three slots" are
# the same question asked of two readers.
_TIER_RANK = {TIER_MAIL: 0, TIER_DIGEST: 1, TIER_PANEL: 2}

# The whole digest tier shares one daily slot, so "three stalled signups" is one
# e-mail rather than three. Gated on the tier, not on each key, because the
# operator's ask was a daily summary -- not a per-finding daily reminder.
ALERT_DIGEST_SECONDS = _int_env("INFE_PILOT_ALERT_DIGEST_SECONDS", 24 * 3600, 3600, 30 * 86_400)


def tier_for(key: str) -> str:
    """Which channel this finding belongs to. The only definition, like `_repeat_for`.

    **The default is ``TIER_MAIL``, and that direction is deliberate.** A new
    check added by somebody who has not read this file must be loud rather than
    silently absent: the failure mode of an over-eager alert is an annoyed
    operator, and the failure mode of a missing one is an incident nobody hears
    about. Only the keys named here are ever quietened, so quietening is always
    an explicit act.
    """
    if key.startswith("mailbox_error:"):
        # One account's authorisation code is wrong. Long-lived, obviously the
        # account's own problem, and visible as the red 收信 light in the user
        # list -- which is a better place for it than an inbox.
        #
        # **`provider_pushback:` 故意不写在这里**（2026-09-24）：它看起来与这一类是同一件事
        # （都是"登录被拒"），处置却完全相反——那是供应商在限流我们，改用户的授权码一点用没有，
        # 而且一旦发生就是所有用户一起收不到信。所以它走默认的响档，见 `evaluate` 里的聚合。
        return TIER_PANEL
    if key == "provider_check_stale":
        # 检查没在跑：重要但没人正卡着，而且它与「某家真的关门了」是两件事。
        return TIER_PANEL
    if key == "platform_balance_stale":
        # 「余额检查没在跑」自己不会让谁少收一封信，修起来也只是跑一条命令，挂在面板上
        # 一直看得见就够了。**另外三条 platform_* 故意不在这里**：本月花费异常、余额低于
        # 警戒线、余额见底——每一条都是「不会有别人告诉你」的钱的事，走默认的响档。
        # 尤其最后一条：那时候用户已经在收不到报告了。
        return TIER_PANEL
    if key.startswith("provider_password_auth_back:"):
        # 门又开了：好消息，凑进每日汇总，不值得单独吵醒人。
        return TIER_DIGEST
    if key in ("master_key_copy_missing", "master_key_copy_stale"):
        # 主密钥的离线副本是**站着不动的准备事项**，不是正在发生的事故：钥匙不会因为没人核对
        # 而变坏，所以它不随时间产生新信息。给它每日汇总就等于一天提醒一次、连提一年——那正是
        # 2026-09-16 那次「一天 74 封」的教训。所以它落在**面板**档：每一轮都记进
        # `alert_state`（管理后台的「巡检」里一直看得到），但从不发信；要一句话的结论就跑
        # `manage backup --check`（它为此非零退出）。
        # `master_key_copy_mismatch` **故意不在这一支**：架子上那把与服务器上的不是同一把，
        # 是现在就打不开备份，走默认的响档。
        return TIER_PANEL
    if key.startswith("setup_stalled:") or key.startswith("invite_failed:"):
        # Silent to the person it is about, but it does not decay with time:
        # an hour later the applicant still has no code. Batched, never dropped.
        return TIER_DIGEST
    return TIER_MAIL


# Which hostname to inspect for certificate expiry. An explicit
# INFE_PILOT_TLS_HOST wins; otherwise it is derived from INFE_PILOT_ORIGIN,
# which every install already sets to the name users actually reach. Only an
# install with neither gets no certificate alerting — better than guessing a
# hostname and reporting on a certificate nobody serves.
def _tls_host_from_environment() -> str:
    explicit = os.environ.get("INFE_PILOT_TLS_HOST", "").strip()
    if explicit:
        return explicit
    origin = os.environ.get("INFE_PILOT_ORIGIN", "").strip()
    if not origin:
        return ""
    return urllib.parse.urlsplit(origin).hostname or ""


ALERT_TLS_HOST = _tls_host_from_environment()
ALERT_TLS_PORT = _int_env("INFE_PILOT_TLS_PORT", 443, 1, 65535)

# Every credential key in pilot.env matches this. Used to scrub command output
# before it is copied into an operator e-mail.
_SECRET_KEY_PATTERN = re.compile(r"(KEY|SECRET|PASSWORD|PASSWD|TOKEN|AUTH)", re.IGNORECASE)


def admin_emails() -> set[str]:
    """The operator set. Solely from the server environment, never the database.

    This is the same rule ``web._admin_emails`` enforces for incoming requests;
    keeping one implementation means "who is an admin" cannot drift between the
    HTTP surface and the mail surface.
    """
    raw = os.environ.get("INFE_PILOT_ADMIN_EMAILS", "")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


# ---------------------------------------------------------------------------
# pure decision function
# ---------------------------------------------------------------------------
def _human_age(delta: dt.timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 3600:
        return f"{seconds // 60} 分钟"
    if seconds < 86400:
        return f"{seconds // 3600} 小时"
    return f"{seconds // 86400} 天"


def _finding(key: str, severity: str, title: str, detail: str) -> dict[str, str]:
    return {"key": key, "severity": severity, "title": title, "detail": detail}


def stale_after_for(mailbox: dict[str, Any]) -> dt.timedelta:
    """How long without a poll counts as stalled for *this* mailbox.

    Gmail is only polled every 15 minutes because Google documents that as its
    limit, so a flat 15-minute threshold would flag a perfectly healthy mailbox
    on every single pass — the fastest way to teach an operator to ignore
    alerts. The threshold therefore scales with whatever floor the provider
    publishes.
    """
    minutes = ALERT_STALE_MINUTES
    floor = mailio.minimum_poll_seconds(mailbox)
    if floor:
        minutes = max(minutes, (floor * STALE_INTERVAL_MULTIPLE) // 60)
    return dt.timedelta(minutes=minutes)


def evaluate(
    db: Database,
    *,
    now: dt.datetime | None = None,
    disk_percent: float | None = None,
    certificate_days: float | None = None,
    backup_dir: "Path | None" = None,
    master_key_fingerprint: str | None = None,
    local_model_reachable: bool | None = None,
) -> list[dict[str, str]]:
    """Return every condition that currently deserves the operator's attention.

    Reads the database, the injected readings, and the backup directory (also
    injectable, for the same reason). Deterministic for a given ``now``, which is
    what makes the thresholds testable. ``local_model_reachable`` is the one
    reading that comes from a socket, so the *caller* performs it -- this function
    stays pure, and a test can drive every branch without a network.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    findings: list[dict[str, str]] = []
    rows = db.list_users_overview()

    # -- 「供应商在限流我们」不是「这个用户的码坏了」 --------------------------
    # 两件事在库里长得一模一样（都只是 `mailboxes.last_error` 的一句文本），处置却相反：
    # 前者要降频/换出口，后者要用户重新生成授权码。更糟的是下面 `mailbox_error:` 被
    # **刻意**分到最安静的一档（面板红灯、不发邮件）——单账号时那是对的，
    # **整家供应商限流时它会把唯一重要的信号静音**（300 个邮箱 = 300 条安静的红灯、零封邮件）。
    # 所以先聚合：同一家主机上 ≥ PUSHBACK_MIN_MAILBOXES 个邮箱被限流，就出一条走响档的告警，
    # 并让那些单账号的红灯让位给它（同源不重复，与下面 `mailbox_error`/`mailbox_stale` 同一条规矩）。
    pushback_hosts: dict[str, list[str]] = {}
    for row in rows:
        if str(row.get("status") or "") != "active" or not row.get("mailbox_enabled"):
            continue
        error = str(row.get("mailbox_error") or "").strip()
        if not error or mailio.classify_imap_failure(text=error) != "pushback":
            continue
        host = str(row.get("imap_host") or "").strip().lower() or "（主机未知）"
        pushback_hosts.setdefault(host, []).append(str(row.get("email") or ""))
    pushback_accounts: set[str] = set()
    for host, accounts in sorted(pushback_hosts.items()):
        if len(accounts) < PUSHBACK_MIN_MAILBOXES:
            continue
        pushback_accounts.update(accounts)
        shown = "、".join(accounts[:5]) + ("…" if len(accounts) > 5 else "")
        findings.append(_finding(
            f"provider_pushback:{host}", "critical",
            f"{host} 在限制我们的登录（{len(accounts)} 个邮箱）",
            f"**不是这些用户填错了授权码**：同一家主机上 {len(accounts)} 个互不相干的邮箱"
            f"同时被拒，指向供应商侧的限流/封禁。先降频（`INFE_PILOT_POLL_SECONDS`）"
            f"或换出口 IP，别让用户去重新生成授权码（那解决不了）。"
            f"受影响账号：{shown}",
        ))

    queue_depth = 0
    # **不是** `failed_reports`（历史全量）：那个数只增不减，于是「修好了」永远反映不出来。
    # 用 `failed_reports_since_success`——自这个账号上一次成功发出以来、窗口内的失败数。
    # 2026-09-22 的实测：两个账号每天各失败一封简报，而 oncall 看到的数字里还混着一封
    # 9/16 的一次性失败（之后成功过三次），于是谁也说不清这盏灯说的到底是旧事还是现在。
    failed_reports = 0
    for row in rows:
        # A paused account is *meant* to stop polling, so exclude it rather than
        # waking the operator up for a state they chose.
        if str(row.get("status") or "") != "active":
            continue
        queue_depth += int(row.get("queue_depth") or 0)
        if not row.get("mailbox_email") or not row.get("mailbox_enabled"):
            continue

        account = str(row.get("email") or "")
        label = f"{account} 的转发邮箱"

        error = str(row.get("mailbox_error") or "").strip()
        # 同源不重复：一个登录不上的邮箱**必然**让它的报告发不出去（简报是从用户自己的
        # 邮箱发出去的），所以那批失败是上面那条 `mailbox_error` 的**后果**。这与
        # `mailbox_stale` 那段注释是同一条规矩：因与果只报一条，否则一个账号出事、两条告警，
        # 而且后果那条还永远不消。**只对同一个账号去重**——别的账号、别的原因照报。
        if not error:
            failed_reports += int(row.get("failed_reports_since_success") or 0)
        if error:
            # 供应商在限流时，这条让位给上面那条 `provider_pushback:` 聚合告警
            # （同源不重复：一个 IP 被限流会同时打中几十上百个邮箱，逐条报就淹了）。
            if account not in pushback_accounts:
                findings.append(_finding(
                    f"mailbox_error:{row['id']}", "critical",
                    f"收信失败：{account}",
                    f"最近一次轮询报错：{error}（这不会丢邮件，恢复后会自动补做）",
                ))
        else:
            # Only look for a *silent* stall when there is no error to report.
            # A failed poll is the cause and "no poll for N minutes" is its
            # consequence: reporting both counts one incident twice, and the
            # consequence is the noisier of the two. On 2026-09-15 that pair sent
            # the operator ~70 mails in a day, one every five minutes.
            last_poll = parse_utc(row.get("last_polled_at"))
            stale_after = stale_after_for(row)
            stale_minutes = int(stale_after.total_seconds() // 60)
            if last_poll is None:
                findings.append(_finding(
                    f"mailbox_stale:{row['id']}", "critical", f"从未轮询成功：{account}",
                    f"{label} 没有任何轮询记录，后台轮询线程可能没有启动。",
                ))
            elif now - last_poll > stale_after:
                # No live counter in the detail. `_should_send` deliberately
                # re-mails a condition whose detail *changed*, so an "N minutes
                # ago" that grows every minute re-mailed every pass and turned a
                # dedupe rule into a metronome. The threshold is stable, and the
                # exact age is one click away in the console.
                findings.append(_finding(
                    f"mailbox_stale:{row['id']}", "critical", f"收信停顿：{account}",
                    f"{label} 已超过 {stale_minutes} 分钟没有轮询。",
                ))

    if queue_depth > ALERT_QUEUE_DEPTH:
        findings.append(_finding(
            "queue_backlog", "warning", "生成队列堆积",
            f"有 {queue_depth} 封邮件在排队（阈值 {ALERT_QUEUE_DEPTH}），"
            "通常意味着模型 key 被限流或供应商变慢。",
        ))

    if failed_reports > ALERT_FAILED_REPORTS:
        findings.append(_finding(
            "failed_reports", "warning", "报告持续失败",
            # 措辞要说清数的是什么：**自上次成功发出以来**、且只看最近几天的失败。
            # 说成「处于失败状态」会让人以为是历史累计，于是没人相信它能变绿。
            f"有 {failed_reports} 份报告自上次成功发出以来一直失败（阈值 {ALERT_FAILED_REPORTS}）；"
            "这些账号的邮箱本身没有报错，所以失败出在生成或投递那一段。",
        ))

    if disk_percent is not None and disk_percent >= ALERT_DISK_PERCENT:
        findings.append(_finding(
            "disk", "critical", "磁盘空间不足",
            f"根分区已用 {disk_percent}%（阈值 {ALERT_DISK_PERCENT}%）。",
        ))

    # Two different failures look identical from here: someone who never set the
    # mailbox up, and someone whose authorisation code is wrong. Both end with a
    # user who receives nothing and no error anywhere, so both are reported --
    # with different wording, because the fix is different.
    for row in db.stalled_setups(hours=ALERT_SETUP_STALL_HOURS, now=now):
        account = str(row.get("email") or "")
        reason = db.SETUP_GAP_LABELS.get(row["setup_gap"], row["setup_gap"])
        # No live "已 N 小时" counter here, for the same reason `mailbox_stale`
        # has none: `_should_send` re-mails a finding whose detail *changed*, so
        # a number that grows on its own re-sends on its own. This one carried
        # `age_hours`, which ticks over every hour -- so the 24-hour repeat set
        # for this key never once applied, and a stalled signup mailed hourly
        # instead of daily. Measured on 2026-09-15; the exact age now lives in
        # the console, where printing it costs nothing.
        findings.append(_finding(
            f"setup_stalled:{row['id']}", "warning",
            f"注册后没配完：{account}",
            f"注册超过 {ALERT_SETUP_STALL_HOURS} 小时仍未完成，{reason}。"
            "这样的人不会收到任何报告，也不会产生任何错误——需要你去问一句。",
        ))

    # Invitations. The applicant is the one party who cannot see this failure:
    # from their side nothing happened at all, so silence is the whole symptom.
    for row in db.failed_invite_sends(100):
        account = str(row.get("email") or "")
        findings.append(_finding(
            f"invite_failed:{row['id']}", "warning",
            f"邀请码邮件发不出去：{account}",
            f"给 {account} 的邀请码已经生成，但邮件发送失败："
            f"{str(row.get('invite_send_error') or '')[:200]}。"
            "他那边什么都不会发生，也就不会来问你——需要你手动把码转达给他，或者重发。",
        ))

    # Backups. A database with no fresh copy is the one failure nobody notices
    # until it is unrecoverable, because everything keeps working right up to the
    # moment it does not.
    # `Path(...)` even when a path was injected: a caller passing a string is an
    # ordinary mistake, and a sentinel that dies on it takes every other check down
    # with it -- which is exactly the failure this block was just fixed for.
    directory = (Path(backup_dir) if backup_dir
                 else Path(os.environ.get("INFE_PILOT_BACKUP_DIR",
                                          "/var/backups/cityu-mail-pilot")))
    # `is_dir()` on a path whose *parent* is not searchable raises rather than
    # returning False -- `/var/backups` is root-only on macOS and on a host where
    # the installer has not run yet. A sentinel that dies on that is worse than
    # one that checks less, and "I cannot look" must not be reported as "there is
    # nothing there": the first alerts on every developer machine, the second
    # silently skips a real problem. So: unreadable => say nothing about backups.
    try:
        can_look = directory.is_dir() and os.access(directory, os.R_OK)
    except OSError:
        can_look = False
    if can_look:
        age = backup.newest_backup_age_hours(directory, now=now)
        if age is None:
            findings.append(_finding(
                "backup_missing", "critical", "没有任何数据库备份",
                f"{directory} 里一份备份都没有。备份定时器可能从没跑过，"
                "或者备份目录被清空了。",
            ))
        elif age > ALERT_BACKUP_HOURS:
            findings.append(_finding(
                "backup_stale", "critical", "数据库备份已经过期",
                f"最新一份备份是 {age:.0f} 小时前的（阈值 {ALERT_BACKUP_HOURS} 小时）。"
                "备份定时器可能被停用了——注意 `OnFailure=` 只在「跑了但失败」时触发，"
                "「根本没跑」不会触发。",
            ))
        # No "everything is fine" finding when the copy is fresh. It used to emit
        # one ("备份正常", severity info) and it behaved exactly like an alert:
        # `_should_send` re-mails a finding whose detail changed, and this one's
        # detail carried the age in whole hours, so the operator got a cheerful
        # e-mail every hour. Every other check here is silent while healthy, and
        # a stale-then-fresh backup still produces one recovery notice through
        # the normal path. Observability is not lost: `manage backup --check`
        # reports the state on demand, and a *dead* backup check is a problem no
        # e-mail could reveal anyway.
        offsite = backup.read_offsite_state(directory)
        if offsite and offsite.get("configured"):
            if not offsite.get("ok"):
                findings.append(_finding(
                    "offsite_failed", "warning", "异地备份推送失败",
                    f"最近一次推送失败（{offsite.get('at') or '时间未知'}）："
                    f"{str(offsite.get('error') or '未知错误')[:200]}",
                ))
            else:
                pushed = parse_utc(offsite.get("at"))
                if pushed and (now - pushed).total_seconds() > ALERT_OFFSITE_HOURS * 3600:
                    findings.append(_finding(
                        "offsite_stale", "warning", "异地备份已经过期",
                        f"最后一次成功推送是 {(now - pushed).total_seconds() / 3600:.0f} 小时前"
                        f"（阈值 {ALERT_OFFSITE_HOURS} 小时）。本地备份还在成功，"
                        "所以这件事只有这里会告诉你。",
                    ))

    # 主密钥的离线副本。**这一条和其他检查不是一回事**：别的检查在测系统，这一条在测
    # 「你还拿不拿得到钥匙」——机器看不到操作者的密码管理器，能记的只有「人最后一次说核对过」。
    # 以前它只在 `backup --check` 里出现，而那是一句要人主动去跑的命令；钥匙丢了在全项目里是
    # 唯一「不会有人知道」的故障，所以它现在是一条真正的发现项（见 `backup.key_copy_state`，
    # 判据只有那一处）。指纹由调用方注入：`evaluate()` 依旧不读环境、不联网、可注入。
    if master_key_fingerprint is not None:
        state, sentence = backup.key_copy_state(
            {"at": db.get_setting("master_key_verified_at"),
             "fingerprint": db.get_setting("master_key_verified_fingerprint")},
            now, master_key_fingerprint)
        if state == "missing":
            findings.append(_finding("master_key_copy_missing", "warning",
                                     "主密钥离线副本从未核对", sentence))
        elif state == "stale":
            findings.append(_finding("master_key_copy_stale", "warning",
                                     "主密钥离线副本超过一年没核对", sentence))
        elif state != "ok":
            # 架子上那把打不开现在的备份，这不是提醒而是正在流血的事故，所以它是响的那一档
            # （`tier_for` 的默认值就是它，这里不写分支正是为了不把它悄悄降级）。
            findings.append(_finding("master_key_copy_mismatch", "critical",
                                     "主密钥离线副本是另一把钥匙", sentence))

    # 服务商的授权码通道（outlook 那件事的**提前版**）：探测由 worker 一天跑一次并记在
    # app_settings 里，这里**只读那条记录**，所以 evaluate() 依旧确定、可注入、不联网。
    findings.extend(providercheck.findings(db, now=now, rows=rows))

    # 客服群二维码到期（v1.1.3）：微信的群码只有 7 天，而**过期的后果是静默的**——
    # 页面从那天起只显示一句「码过期了，去留言」，访客扫不到群，我们这边毫无察觉。
    # 状态判据只有 `groupqr.state()` 一处（介绍页渲染读的也是它），免得两边算出不同的日子。
    findings.extend(groupqr.findings(now=now, db=db))

    # 管理员那把 key 的钱。同样的形状：worker 半小时读一次余额记在 app_settings 里，
    # 这里**只读那条记录**。两件事分开报（本月的花费 / 余额见底），因为修法不同：
    # 前者要去看是谁在花，后者要去充值。判据只有 `budget.state()` 一处，
    # `manage platform-cost` 与 model 那道闸门读的也是它。
    findings.extend(budget.findings(db, now=now, rows=rows))

    # 主服务（本机那台）是不是在干活。写入点在**每一次调用**里（`Service._generate_with_retry`
    # 成功/降级时各盖一枚章），这里只读那一行。为什么值得一条独立的告警：降级之后
    # **用户毫无感觉、报告照出、钱在花**——而那正是「把主服务接进来」想避免的事。
    # 金额那一项救不了它：没过警戒线时，账单一个字都不说。
    findings.extend(tierhealth.findings(db, now=now))

    # 那台**现在**还通不通。上面那条读的是「上一次真出报告时它有没有答话」——两件事，
    # 因为报告是稀疏事件：那台凌晨断了、下一封信等到中午，中间几小时上面那条一个字
    # 都不说，而报告全在走付费兜底。探测结果由调用方传进来（`run_checks` 里真的去探），
    # 所以 `evaluate()` 本身仍然不碰网络、仍然是纯函数。
    #
    # **同源不重复**（与 `mailbox_error` / `mailbox_stale` 那对同一条规矩）：同一场故障里
    # 上面那条（已经降级过：真发生过、还花了钱）和这一条（现在还连不上）会同时成立，
    # 两条一起发就是**一次事故两封信**。所以章还是新鲜 degraded 时只留那一条；
    # 章是 ok/过期/没有的时候，这一条是**唯一**会说话的——凌晨断了、下一封信还没来，
    # 那正是它存在的理由。
    if local_model_reachable is False and tierhealth.local_is_primary():
        stamp = tierhealth.reading(db, now=now)
        already_degraded = bool(stamp and stamp["state"] == "degraded"
                                and (stamp["age"] is None or stamp["age"] <= tierhealth.STALE_AFTER))
        if not already_degraded:
            findings.append(_finding(
                "local_model_unreachable", "warning",
                "连不上主服务（本机那台）",
                "轻量探测（对方文档 §9 的 /health，不调模型、不花钱）没有应答：那台盒子、那条隧道"
                "或那张证书至少有一处不通。**报告不会丢**——付费兜底会接手，用户那边没有感觉，"
                "但在修好之前每一封报告都在花管理员那把 key 的钱，而主服务存在的意义正是不花这笔钱。"
                "恢复后这条会自己消失（不需要手工清）。检查顺序：先看那条反向隧道还在不在"
                "（生产上 `sudo ss -ltnp | grep 59851`），再看那台盒子上的模型与护栏服务，"
                "最后跑一次 `python -m pilot_app.manage check-localmodel` 看是哪一跳。",
            ))

    if certificate_days is not None and certificate_days < ALERT_CERT_DAYS:
        if certificate_days < 0:
            detail = "已过期，站点随时会报证书错误。"
        else:
            detail = f"还有 {certificate_days:.0f} 天到期（阈值 {ALERT_CERT_DAYS} 天）。"
        findings.append(_finding(
            "tls_cert", "critical", "HTTPS 证书即将失效",
            f"{ALERT_TLS_HOST} 的证书{detail}请检查 certbot 续期是否失败。",
        ))

    return findings


# ---------------------------------------------------------------------------
# outside-world readings
# ---------------------------------------------------------------------------
def certificate_days_remaining(*, host: str | None = None, port: int | None = None,
                               timeout: float = 8.0) -> float | None:
    """Days until the served certificate expires, or ``None`` if unreadable.

    Verification stays **on** deliberately. The tempting alternative — disable
    it so an expired certificate still yields a readable ``notAfter`` — does not
    even work: ``getpeercert()`` returns an empty dict when the peer was not
    validated, so the one case worth reporting would come back as "unknown".
    A failed handshake is therefore reported as a negative number, which is the
    honest reading: users reaching this host get a certificate error. A genuine
    network problem still returns ``None``, because that is not the site's
    certificate being wrong.
    """
    host = ALERT_TLS_HOST if host is None else host
    if not host:
        return None
    port = ALERT_TLS_PORT if port is None else port
    context = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=host) as tls:
                certificate = tls.getpeercert()
    except ssl.SSLCertVerificationError as exc:
        logging.warning("certificate for %s:%s failed verification (%s)", host, port, exc)
        return -1.0
    except (OSError, ssl.SSLError) as exc:
        logging.warning("certificate check could not reach %s:%s (%s)", host, port, exc)
        return None
    not_after = str((certificate or {}).get("notAfter") or "")
    if not not_after:
        logging.warning("certificate for %s:%s carried no notAfter field", host, port)
        return None
    try:
        expiry = dt.datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None
    return (expiry - dt.datetime.now(dt.timezone.utc)).total_seconds() / 86400


def disk_percent() -> float | None:
    """Root-filesystem usage, via the same reader the admin panel uses."""
    try:
        return metrics.host_metrics()["disk"]["percent"]
    except Exception:  # a metrics failure must never take the sentinel down
        logging.exception("disk reading failed for the alert sentinel")
        return None


# ---------------------------------------------------------------------------
# de-duplication
# ---------------------------------------------------------------------------
def _repeat_for(key: str) -> int | None:
    """How often a particular kind of finding may re-mail, or None for the default.

    Kept as a prefix lookup rather than a field on the finding so the finding
    stays a flat dict of strings -- the shape the e-mail templates and the alert
    state table both assume.
    """
    if key.startswith("setup_stalled:"):
        return ALERT_SETUP_REPEAT_SECONDS
    if key.startswith("wechat_"):
        # 换码是运营者的动作，不是时间的函数：一天说一次就够。
        return ALERT_WECHAT_REPEAT_SECONDS
    if key.startswith("platform_"):
        return ALERT_PLATFORM_REPEAT_SECONDS
    if key.startswith("backup_"):
        return ALERT_BACKUP_REPEAT_SECONDS
    if key.startswith("invite_failed:"):
        return ALERT_INVITE_REPEAT_SECONDS
    return None


def _should_send(previous: dict[str, Any] | None, finding: dict[str, str],
                 now: dt.datetime, repeat_seconds: int | None = None) -> bool:
    """Whether this finding is due for another e-mail right now."""
    if previous is None:
        return True
    if not previous.get("open"):
        # It was reported, then cleared. Seeing it again is news again.
        return True
    if str(previous.get("detail") or "") != finding["detail"]:
        return True
    last_sent = parse_utc(previous.get("last_sent_at"))
    if last_sent is None:
        return True
    return (now - last_sent).total_seconds() >= (ALERT_REPEAT_SECONDS if repeat_seconds is None
                                                 else repeat_seconds)


def _digest_window_open(known: dict[str, dict[str, Any]], now: dt.datetime) -> bool:
    """Whether the digest tier's shared daily slot is free.

    Gated on the *tier* rather than on each key, so that "three stalled signups"
    arrives as one e-mail instead of three. The most recent send among the digest
    keys is what starts the window: if anything in the tier was mailed recently,
    everything else waits for the next slot. A tier that has never mailed is
    always open, so the first one still goes out promptly.
    """
    newest: dt.datetime | None = None
    for key, row in known.items():
        # `open` matters: a finding that already recovered must not hold the slot
        # shut, or the next one to appear under that key would wait a day for a
        # window nobody is actually using.
        if tier_for(key) != TIER_DIGEST or not row.get("open"):
            continue
        stamp = parse_utc(row.get("last_sent_at"))
        if stamp is not None and (newest is None or stamp > newest):
            newest = stamp
    if newest is None:
        return True
    return (now - newest).total_seconds() >= ALERT_DIGEST_SECONDS


# 一次巡检**会做什么**，逐条说清楚。`manage check-alerts --dry-run` 用它来回答
# 「现在有几项会告警」——在这之前它只是把 `evaluate()` 的活跃异常全部列出来就收工，
# 于是**按过「已知晓」的、只在面板显示的、汇总今天已经发过的**都被算成「会告警」。
# 2026-09-16 在生产上就印出过「2 项会告警」，其中一项是运营者自己静音掉的。
PLAN_LABELS = {
    "mail": "会立刻发信",
    "digest": "会进今天的汇总",
    "digest_wait": "汇总今天已经发过，等下一封",
    "panel": "只在面板显示（本来就不发信）",
    "muted": "你按过「已知晓」，不再发信",
    "repeat_wait": "这一轮不发（重复窗口内，或详情没变）",
}
# 只有这两种真的会产生一封邮件。别处要用「会不会发信」的判断，从这里取。
MAILING_PLAN_STATES = ("mail", "digest")


def plan(findings: list[dict[str, Any]], known: dict[str, dict[str, Any]],
         now: dt.datetime | None = None) -> list[dict[str, Any]]:
    """What the sentinel would do with each active finding, right now.

    One definition of "会发信吗", used by the dry run so that a diagnostic cannot
    disagree with the thing it is diagnosing: it is built from the same
    ``_should_send`` / tier / digest-window helpers `run_checks` uses, not from a
    second reading of the same fields.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    digest_open = _digest_window_open(known, now)
    rows: list[dict[str, Any]] = []
    for item in findings:
        key = str(item.get("key") or "")
        tier = tier_for(key)
        previous = known.get(key) or None
        if (previous or {}).get("acknowledged_at"):
            state = "muted"
        elif not _should_send(previous, item, now, _repeat_for(key)):
            state = "repeat_wait"
        elif tier == TIER_PANEL:
            state = "panel"
        elif tier == TIER_DIGEST and not digest_open:
            state = "digest_wait"
        elif tier == TIER_DIGEST:
            state = "digest"
        else:
            state = "mail"
        rows.append({"key": key, "tier": tier, "state": state, "label": PLAN_LABELS[state]})
    return rows


def panel_rows(states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The sentinel's stored state, shaped for the console.

    Reads ``alert_state`` instead of calling :func:`evaluate`, and that is a
    deliberate choice rather than a shortcut. The console is opened often, while
    ``evaluate`` reaches the network for the certificate check -- an eight-second
    timeout whenever the site is unreachable -- plus a query per mailbox. The
    sentinel already runs all of that every five minutes and writes the verdict
    here, so the panel shows *its* answer: at most one interval old, cheap to
    render, and never a second source of truth that could disagree with the mail
    that actually went out.
    """
    rows: list[dict[str, Any]] = []
    for row in states:
        rows.append({
            "key": str(row.get("key") or ""),
            "severity": str(row.get("severity") or ""),
            "title": str(row.get("title") or ""),
            "detail": str(row.get("detail") or ""),
            "tier": tier_for(str(row.get("key") or "")),
            "open": bool(row.get("open")),
            "acknowledged": bool(row.get("acknowledged_at")),
            "first_seen_at": row.get("first_seen_at"),
            "last_sent_at": row.get("last_sent_at"),
        })
    # Open first, and within those the ones that will actually mail: a panel in
    # insertion order buries this morning's incident under last week's
    # known-broken account.
    rows.sort(key=lambda item: (not item["open"], item["acknowledged"],
                                _TIER_RANK.get(item["tier"], 9), str(item["first_seen_at"] or "")))
    return rows


# ---------------------------------------------------------------------------
# rendering and delivery
# ---------------------------------------------------------------------------
def _render_text(due: list[dict[str, str]], recovered: list[dict[str, Any]],
                 analyses: list[dict[str, Any]] | None = None) -> str:
    lines: list[str] = []
    if due:
        lines.append("需要处理：")
        for item in due:
            lines.append(f"  - [{item['severity']}] {item['title']}")
            if item["detail"]:
                lines.append(f"      {item['detail']}")
        lines.append("")
    if recovered:
        lines.append("已恢复：")
        for item in recovered:
            lines.append(f"  - {item.get('title') or item['key']}")
        lines.append("")
    lines.append(agent.render_text_section(analyses or []))
    lines.append("这封邮件由 CityU Mail Pilot 的哨兵自动发出；登录管理后台可看到实时指标。")
    return "\n".join(lines)


def _render_html(due: list[dict[str, str]], recovered: list[dict[str, Any]],
                 analyses: list[dict[str, Any]] | None = None) -> str:
    """600 px single-column table, inline CSS, no script, no external images.

    The same constraints the report mails live under (see AGENTS.md invariant
    5); an operator e-mail is not an excuse to relax them.
    """
    rows: list[str] = []
    for item in due:
        colour = "#b42318" if item["severity"] == "critical" else "#b54708"
        rows.append(
            '<tr><td style="padding:12px 20px;border-bottom:1px solid #e4e7ec">'
            f'<div style="font-weight:bold;color:{colour};font-size:14px">{html.escape(item["title"])}</div>'
            f'<div style="color:#475467;font-size:13px;line-height:1.5;margin-top:4px">'
            f'{html.escape(item["detail"])}</div></td></tr>'
        )
    for item in recovered:
        rows.append(
            '<tr><td style="padding:12px 20px;border-bottom:1px solid #e4e7ec">'
            '<div style="font-weight:bold;color:#027a48;font-size:14px">已恢复：'
            f'{html.escape(str(item.get("title") or item["key"]))}</div></td></tr>'
        )
    heading = []
    if due:
        heading.append(f"{len(due)} 项异常")
    if recovered:
        heading.append(f"{len(recovered)} 项已恢复")
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="background:#f4f7fb;padding:24px 12px"><tr><td align="center">'
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" '
        'style="width:600px;background:#ffffff;border:1px solid #d9e2ec">'
        '<tr><td style="padding:18px 20px;background:#123b63;color:#ffffff">'
        '<div style="font-size:11px;letter-spacing:.08em">CITYU MAIL PILOT</div>'
        f'<div style="font-size:18px;margin-top:6px">{"，".join(heading)}</div></td></tr>'
        + "".join(rows) + agent.render_html_section(analyses or []) +
        '<tr><td style="padding:12px 20px;color:#64748b;font-size:11px">'
        '由巡检哨兵自动发出，请勿直接回复。</td></tr>'
        '</table></td></tr></table>'
    )


def _subject(due: list[dict[str, str]], recovered: list[dict[str, Any]]) -> str:
    parts = []
    if due:
        parts.append(f"{len(due)} 项异常")
    if recovered:
        parts.append(f"{len(recovered)} 项已恢复")
    return "[CityU Mail Pilot] " + "，".join(parts)


def send_admin_mail(db: Database, secrets: SecretBox, subject: str, text_body: str,
                    html_body: str | None = None,
                    also: Iterable[str] = ()) -> list[str]:
    """Send one operational e-mail from the admin's own mailbox to the admin.

    There is no system mailbox: every send in this project goes out through a
    user's own SMTP credentials, and the operator's account is no exception.
    Returns the addresses the message was sent to; raises if no admin has a
    usable mailbox, so the caller can report the failure instead of silently
    dropping the alert.

    ``also`` adds recipients **on top of** ``admin_emails()`` (the installer
    set). The default is empty, so every existing caller keeps mailing exactly
    who it used to; today only the "someone applied for the pilot" notice passes
    anything, and it passes the admins the operator ticked by hand
    (``signup_notice``). A caller can add to the installer list but never replace
    it -- that is the whole reason this is a separate argument.
    """
    recipients = admin_emails() | {str(item).strip().lower() for item in also if str(item).strip()}
    if not recipients:
        raise RuntimeError("INFE_PILOT_ADMIN_EMAILS 未配置，无法发送管理员告警。")
    delivered: list[str] = []
    failures: list[str] = []
    for address in sorted(recipients):
        user = db.find_user_for_login(address)
        if not user:
            failures.append(f"{address}（没有这个账号）")
            continue
        mailbox = db.get_mailbox(user["id"])
        if not mailbox or not mailbox.get("enabled"):
            failures.append(f"{address}（没有可用的转发邮箱，无法借它发信）")
            continue
        config = {
            "email": mailbox["email"],
            "report_to": address,
            "smtp_host": mailbox["smtp_host"],
            "smtp_port": mailbox["smtp_port"],
        }
        password = secrets.decrypt(mailbox["encrypted_password"], context=f"mailbox:{user['id']}")
        mailio.send_report(config, password, subject, text_body,
                           html_body=html_body, text_body=text_body)
        delivered.append(address)
    if not delivered:
        raise RuntimeError("没有任何管理员账号能发出告警邮件：" + "；".join(failures))
    return delivered


#: 发信中继（2026-09-24）。五个都要设，或者一个都别设。
RELAY_ENV = ("INFE_PILOT_RELAY_HOST", "INFE_PILOT_RELAY_PORT",
             "INFE_PILOT_RELAY_PASSWORD", "INFE_PILOT_RELAY_FROM")


def _operator_relay() -> tuple[dict[str, Any], str, str] | None:
    """把运营者的信交给一个 SMTP 中继发（例如 Resend），设了才生效。

    为什么要有它：`_operator_sender` 借的是某个管理员的**邮箱行**，而那一行的
    IMAP 与 SMTP **共用同一个密码字段** —— 为了换发信服务商去改那一行，会把**收信**
    一起弄断。中继走独立的环境变量：设了用它，没设就逐字退回原来的行为，想回退
    只要删掉这几个变量（不用改数据库）。

    只设一半是**配置错误**，直接抛出来 —— 静默忽略会把人带到「以为已经换过去了、
    其实还在用旧邮箱发」那个坑里（那正是我们修过好几次的那类 bug）。
    """
    host = os.environ.get("INFE_PILOT_RELAY_HOST", "").strip()
    present = [name for name in RELAY_ENV if os.environ.get(name, "").strip()]
    if not host:
        if present:
            raise RuntimeError(
                "发信中继只配了一半：设了 " + "、".join(present) + " 但没设 INFE_PILOT_RELAY_HOST。"
                "要么五个都设，要么一个都别设（不设就走管理员自己的邮箱）。")
        return None
    missing = [name for name in RELAY_ENV if not os.environ.get(name, "").strip()]
    if missing:
        raise RuntimeError("发信中继缺这些环境变量：" + "、".join(missing))
    sender = os.environ["INFE_PILOT_RELAY_FROM"].strip()
    config = {
        "email": sender,
        "report_to": sender,
        "smtp_host": host,
        "smtp_port": int(os.environ.get("INFE_PILOT_RELAY_PORT", "465").strip() or 465),
        # 中继的用户名常常与发件地址不同（Resend 固定是 "resend"）。
        "smtp_user": os.environ.get("INFE_PILOT_RELAY_USER", "resend").strip() or "resend",
    }
    return config, os.environ["INFE_PILOT_RELAY_PASSWORD"], sender


def _operator_sender(db: Database, secrets: SecretBox) -> tuple[dict[str, Any], str, str]:
    """Find an admin account that can actually send, and unlock it.

    Returns ``(smtp_config, password, address)``. There is no system mailbox in
    this project -- every outgoing message borrows a user's own SMTP
    credentials -- so anything that needs to mail an arbitrary person has to go
    through the operator's account.
    """
    relay = _operator_relay()
    if relay is not None:
        return relay
    for address in sorted(admin_emails()):
        user = db.find_user_for_login(address)
        if not user:
            continue
        mailbox = db.get_mailbox(user["id"])
        if not mailbox or not mailbox.get("enabled"):
            continue
        config = {
            "email": mailbox["email"],
            "report_to": address,
            "smtp_host": mailbox["smtp_host"],
            "smtp_port": mailbox["smtp_port"],
        }
        password = secrets.decrypt(mailbox["encrypted_password"], context=f"mailbox:{user['id']}")
        return config, password, address
    raise RuntimeError("没有可用作发件人的管理员邮箱：需要 INFE_PILOT_ADMIN_EMAILS 里至少有一个"
                       "已配置好转发邮箱的管理员账号。")


# The name shown next to the sender address. Defaults to what the applicant saw
# on the website, so the message is recognisable; a self-hoster running this for
# another school sets their own.
SENDER_NAME_ENV = "INFE_PILOT_SENDER_NAME"


def sender_name() -> str:
    """The display name, sanitised.

    It goes into a header, so anything that could end a header line is removed
    rather than escaped -- an environment value with a CRLF in it would
    otherwise be a header-injection primitive, and the value is operator-set
    text that nothing else validates.
    """
    raw = (os.environ.get(SENDER_NAME_ENV) or "")
    cleaned = "".join(ch for ch in raw if ch.isprintable() and ch not in "\r\n").strip()
    return cleaned[:60] or "CityU 邮件助手"


def send_as_operator(db: Database, secrets: SecretBox, to: str, subject: str, text_body: str,
                     html_body: str | None = None) -> dict[str, Any]:
    """Send one message from the operator's own mailbox to any address.

    Only the approval path uses this, and only an operator can reach it: the
    recipient is an address a stranger typed into the public form, so an
    automatic caller here would be an open relay.

    Raises on failure so the caller can tell the operator their click did not
    deliver -- the invite code is still shown either way, because losing the
    code would be worse than losing the e-mail.

    Returns a receipt (``from``, ``message_id``, ``refused``) rather than just
    the sender address. The caller stores it: whether an invite was actually
    handed to the mail server is the one part of "did they get it?" this side can
    answer, and answering it only in the moment meant it was unanswerable a day
    later.
    """
    config, password, sender = _operator_sender(db, secrets)
    config = {**config, "report_to": to}
    receipt = mailio.send_report(config, password, subject, text_body,
                                 html_body=html_body, text_body=text_body,
                                 from_name=sender_name(), reply_to=config["email"])
    return {"from": sender, **receipt}


# ---------------------------------------------------------------------------
# worker entry point
# ---------------------------------------------------------------------------
def run_checks(
    db: Database,
    secrets: SecretBox,
    *,
    now: dt.datetime | None = None,
    disk: float | None = None,
    certificate_days: float | None = None,
    local_model_reachable: bool | None = None,
    sender: Callable[..., list[str]] = send_admin_mail,
) -> dict[str, Any]:
    """Evaluate, de-duplicate, and mail. Never raises.

    ``disk``/``certificate_days``/``local_model_reachable``/``sender`` are
    injectable so a test can drive every branch without a socket, a full disk or
    an SMTP server.
    """
    if not ALERTS_ENABLED:
        return {"enabled": False, "findings": 0, "sent": 0, "errors": []}

    now = now or dt.datetime.now(dt.timezone.utc)
    if disk is None:
        disk = disk_percent()
    if certificate_days is None:
        certificate_days = certificate_days_remaining()
    if local_model_reachable is None:
        # 只在「本机那台是主档」的实例上探（`probe()` 自己返回 None，不猜、也不产生噪音）。
        # 它自己吞掉所有异常：一次探测不该有能力让整轮巡检变成 "alert evaluation failed"。
        local_model_reachable = tierhealth.probe()

    try:
        # 指纹从**这个进程正在用的那把钥匙**算，不是从环境文件里再读一遍：两者本该相同，
        # 不同的时候（比如服务读的是另一份 env）要暴露出来的正是前者。算不出来就当作
        # 「没法判断」，绝不让一次哈希失败把整轮巡检变成 "alert evaluation failed"。
        try:
            fingerprint = secrets.fingerprint()
        except Exception:  # noqa: BLE001 - a fingerprint must never stop the sentinel
            logging.warning("无法计算主密钥指纹，这一轮不检查离线副本", exc_info=True)
            fingerprint = None
        findings = evaluate(db, now=now, disk_percent=disk, certificate_days=certificate_days,
                            master_key_fingerprint=fingerprint,
                            local_model_reachable=local_model_reachable)
    except Exception as exc:
        logging.exception("alert evaluation failed")
        return {"enabled": True, "findings": 0, "sent": 0, "errors": [str(exc)]}

    active = {item["key"]: item for item in findings}
    known = {row["key"]: row for row in db.list_alert_states()}

    # Keys carried over from an earlier pass are cleared here too, so a finding
    # that stops being true (the account finished, or the operator paused it)
    # arrives as a recovery notice instead of silently disappearing.
    candidates = [item for item in findings
                  if _should_send(known.get(item["key"]), item, now, _repeat_for(item["key"]))]
    # Acknowledged findings stop mailing but stay in the console -- that is the
    # entire point of acknowledging rather than fixing. The flag is cleared when
    # the finding itself clears, so it can never permanently hide a problem that
    # later came back.
    candidates = [item for item in candidates
                  if not (known.get(item["key"]) or {}).get("acknowledged_at")]

    # Split by channel before sending, because the tiers are different *channels*
    # and not different wording. TIER_PANEL falls out of both lists below, which
    # is what makes it console-only -- but note that it is still *recorded*, in
    # the loop at the bottom. Quiet must never mean dropped: the panel reads
    # `alert_state`, so a tier that is not written there would be invisible
    # everywhere, which is worse than the noise it was meant to remove.
    immediate = [item for item in candidates if tier_for(item["key"]) == TIER_MAIL]
    digest = [item for item in candidates if tier_for(item["key"]) == TIER_DIGEST]
    if digest and not _digest_window_open(known, now):
        # The daily slot is spent: these wait for the next one rather than
        # turning into one mail each. Nothing in this tier gets worse by waiting,
        # which is exactly why it is in this tier.
        digest = []
    mailing = immediate + digest

    gone = [row for row in known.values() if row.get("open") and row["key"] not in active]
    # Only the loud tier announces recoveries. A console-only finding must not be
    # able to ring the phone by *going away* either, and a digest finding's
    # comeback is visible in the panel without a mail of its own. Every recovered
    # row is closed regardless -- see the loop at the bottom.
    recovered = [row for row in gone if tier_for(row["key"]) == TIER_MAIL]

    # What the assistant is asked to look at: **every active finding that has no
    # analysis of its current shape**, not just the ones being mailed right now.
    # `agent.pending` holds the reasoning; the short version is that the mail
    # queue is capped and one-shot, so anything beyond the cap used to stay
    # unexplained for the whole repeat window (six hours) or forever. The panel
    # shows every finding, so "nobody looked at this one" is visible to the
    # operator as an assistant that lags reality (2026-09-16).
    #
    # Ordered loud tier first, because the per-pass cap is a fixed number of
    # *slots* and the tiers compete for it. Without this ordering a quiet finding
    # that happens to come earlier in `evaluate()` would spend a slot the
    # mail-tier finding needed, and the operator would get an alert with the
    # analysis missing from the one finding it was attached to.
    queue = sorted(agent.pending(db, findings, states=known),
                   key=lambda item: _TIER_RANK.get(tier_for(item["key"]), 9)) \
        if agent.enabled(db) else []

    # What this pass will write down even if it mails nothing. Console-tier
    # findings never mail, so this is the *only* place they are recorded, and
    # `alert_state` is exactly what the panel reads: a pass that skipped this
    # would make a quiet finding invisible everywhere, which is worse than the
    # noise the tiering removed.
    #
    # A digest finding that was deferred is deliberately **not** here: this row's
    # `last_sent_at` is what starts both its own repeat window and the shared
    # digest slot, so writing one down for a mail that never went out would push
    # the next real digest a day further away, every pass, forever.
    to_record = mailing + [item for item in candidates if tier_for(item["key"]) == TIER_PANEL]

    # `gone` belongs in this condition, and leaving it out was a real bug
    # (2026-09-18, 用户原话「为什么巡检和 ai 运维还显示有问题」). A **console-tier**
    # finding -- `mailbox_error:*` is one: it never mails, it only appears on the
    # panel -- that stopped being true never reached the `clear_alert` loop at the
    # bottom, because that loop sits *after* this early return and nothing else in
    # the pass counted as work. A repaired mailbox therefore stayed on the panel
    # as `open=1`「收信失败」until some unrelated pass happened to have mail to
    # send -- which is precisely the complaint that the panel keeps showing a
    # problem that is already fixed. **Closing a condition is work in its own
    # right.**
    if not to_record and not recovered and not queue and not gone:
        return {"enabled": True, "findings": len(active), "sent": 0, "errors": [], "analyses": 0}

    # Explain before sending, so the analysis rides in the same message as the
    # finding it is about -- two e-mails for one incident is exactly the noise the
    # de-duplication above exists to prevent. `analyse_many` cannot raise and has
    # its own budget gate; if the model is down or the ceiling was reached the
    # alert still goes out, because a model outage must never be able to silence
    # the sentinel.
    analyses: list[dict[str, Any]] = []
    if queue:
        # Analysed across **every** tier, attached to the mail only for the loud
        # one. That is shadow mode, and it is what makes this assistant
        # evaluable: the operator can read a week of its conclusions in the
        # console and decide whether to trust it, instead of judging it while it
        # is already writing into their inbox. The quiet tiers are exactly where
        # nothing is waiting on the answer, so their conclusions cost nothing to
        # hold back.
        #
        # The switch is checked here as well as inside `analyse`, so a disabled
        # assistant leaves the alert byte-for-byte what it always was: no
        # "未分析：未开启" line in every mail, and no work at all.
        try:
            analyses = agent.analyse_many(db, queue, secrets=secrets, now=now)
        except Exception:  # pragma: no cover - defensive; analyse_many swallows its own
            logging.exception("agent analysis failed")
            analyses = []
        analyses = [item for item in analyses
                    if tier_for(str((item.get("finding") or {}).get("key") or "")) == TIER_MAIL]

    errors: list[str] = []
    if mailing or recovered:
        # Nothing in `mailing` and nothing recovered means this pass carries only
        # console-tier news: record it and stay quiet.
        try:
            sender(db, secrets, _subject(mailing, recovered),
                   _render_text(mailing, recovered, analyses),
                   _render_html(mailing, recovered, analyses))
        except Exception as exc:
            # Leave the state untouched: the next pass tries again rather than
            # pretending the operator was told.
            logging.warning("admin alert could not be sent: %s", exc)
            errors.append(str(exc))
            return {"enabled": True, "findings": len(active), "sent": 0, "errors": errors,
                    "analyses": 0}

    # Written down only now, after the send: a row whose mail failed must not
    # look delivered (see `to_record` above for what is in this list and why).
    for item in to_record:
        db.record_alert(item["key"], item["severity"], item["detail"], item["title"], now)
    for row in gone:
        db.clear_alert(row["key"], now)
    if mailing or recovered:
        logging.info("admin alert sent: %s new, %s recovered", len(mailing), len(recovered))
    return {"enabled": True, "findings": len(active), "sent": len(mailing) + len(recovered),
            "analyses": len(analyses), "errors": errors}


# ---------------------------------------------------------------------------
# systemd OnFailure= handler support
# ---------------------------------------------------------------------------
def collect_secret_values(env_path: str = "/etc/cityu-mail-pilot/pilot.env") -> set[str]:
    """Credential values from the environment file, for output scrubbing.

    Only *values* are returned, never keys, and they are used solely to blank
    themselves out of text that is about to be e-mailed.
    """
    values: set[str] = set()
    try:
        with open(env_path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                name, _, value = line.partition("=")
                value = value.strip().strip('"').strip("'")
                if value and _SECRET_KEY_PATTERN.search(name):
                    values.add(value)
    except OSError:
        pass
    return values


def redact(text: str, secrets_to_hide: set[str] | None = None) -> str:
    """Blank out known credentials and any ``NAME=value`` secret assignment.

    The handler copies ``systemctl status`` output into a message that leaves
    the machine, so this is the backstop for invariant 2. Called with no
    arguments it still catches inline ``KEY=...``/``PASSWORD=...`` text, which
    is how systemd prints an ``Environment=`` line.
    """
    cleaned = text
    for value in sorted(secrets_to_hide or set(), key=len, reverse=True):
        if len(value) >= 6:
            cleaned = cleaned.replace(value, "***")
    return re.sub(
        r"(?i)\b([A-Za-z0-9_]*(?:KEY|SECRET|PASSWORD|PASSWD|TOKEN|AUTH)[A-Za-z0-9_]*)\s*=\s*\S+",
        r"\1=***",
        cleaned,
    )
