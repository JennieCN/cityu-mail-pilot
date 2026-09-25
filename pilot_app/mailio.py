"""Read-only IMAP ingestion and SMTP report delivery."""

from __future__ import annotations

import datetime as dt
import email
import email.policy
import email.utils
import html
import imaplib
import logging
import os
import re
import smtplib
import ssl
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from typing import Any

from .security import redact_secrets, validate_public_host


GENERATED_PREFIXES = ("【AI邮件摘要】", "【AI每日报告】", "[AI Mail Summary]", "[AI Daily Report]")

# 一封来信正文最多留多少字。生成报告与「看原信」共用这一个数：两处各写一个，
# 迟早出现「报告里看得到、点开原信反而被截掉」这种对不上的怪事。
MESSAGE_BODY_LIMIT = 20000

# Providers differ in how much polling they tolerate, and only Gmail publishes
# both the rule and the penalty. Its "Gmail server request limits" page states
# "When the limit is reached, the account is temporarily suspended", that a
# suspension "typically lasts an hour, but can last up to 24 hours", and that
# clients should "check for new messages less frequently. We recommend once
# every 15 minutes." That is a floor on how often we may open a Gmail mailbox,
# and it lives here rather than in the worker because the alert sentinel needs
# the same number to decide what "no poll for a while" means.
GMAIL_MIN_POLL_SECONDS = max(60, int(os.environ.get("INFE_PILOT_GMAIL_POLL_SECONDS", "900")))


def _client_id_payload() -> str:
    """Who we say we are, for the IMAP ``ID`` command (RFC 2971).

    The version comes from the package itself so the string cannot drift from
    what is actually installed.
    """
    from pilot_app import __version__  # local import: keeps this module import-light

    return (f'("name" "CityU Mail Pilot" "version" "{__version__}" '
            f'"vendor" "cityu-mail-pilot" "os" "Linux")')


# RFC 2971 ``ID`` is an *extension* command, and `imaplib` only knows a fixed
# list of verbs: `_command` looks the name up in `imaplib.Commands` and raises
# `KeyError` before a single byte leaves the socket. The first version of
# `identify_client` called `_simple_command("ID", …)` without registering it,
# swallowed the KeyError, and therefore did nothing at all -- and the fake
# server in the tests implemented `_simple_command` itself, so it never went
# near that lookup and the suite stayed green (production did not: the account
# was still refused). `IMAP4.xatom` does exactly this registration; we do it
# here for the three states we can be in.
if "ID" not in imaplib.Commands:  # pragma: no branch - one-time
    imaplib.Commands["ID"] = ("NONAUTH", "AUTH", "SELECTED")


def identify_client(client: Any) -> bool:
    """Announce this client to servers that ask, before touching the mailbox.

    **This is not politeness, it is the difference between working and not.**
    Measured on production 2026-09-18 against ``imap.163.com``: the same
    authorization code returns ``LOGIN completed``, and then *every*
    ``EXAMINE``/``SELECT INBOX`` answers

        NO [EXAMINE Unsafe Login. Please contact kefu@188.com for help]

    -- 163/126 (Coremail) refuses mailbox access to a client that logs in
    without first identifying itself. Send ``ID`` first and the very same
    session returns ``OK [READ-ONLY] Examine completed`` (A/B/A/B measured, so
    it is the command and not the anti-abuse cooling down). Python's ``imaplib``
    never sends ``ID`` on its own, which is why a 163 mailbox looked
    permanently broken while QQ and Gmail worked.

    Never fatal: a server that dislikes the command must still be pollable, so
    failures are swallowed and reported only through the return value.
    """
    capabilities = getattr(client, "capabilities", ()) or ()
    if "ID" not in capabilities:
        return False
    try:
        # `_simple_command` rather than `xatom`: the latter insists on an
        # untagged `* ID` reply, and a server asked *before* login answers with
        # a tagged OK only (measured on 163), so `xatom` would raise KeyError
        # after having sent the command.
        typ, _ = client._simple_command("ID", _client_id_payload())
        return typ == "OK"
    except Exception:  # pragma: no cover - depends on the server's mood
        return False


def _server_words(data: Any) -> str:
    """The server's own sentence, for a message the operator has to act on."""
    text = ""
    if isinstance(data, (list, tuple)) and data:
        first = data[0]
        text = first.decode("utf-8", "replace") if isinstance(first, bytes) else str(first)
    elif isinstance(data, bytes):
        text = data.decode("utf-8", "replace")
    elif data:
        text = str(data)
    text = " ".join(text.split())
    return text[:200]


def refused_inbox(data: Any = None) -> MailError:
    """The error for "the server would not let us read the mailbox".

    It carries the server's own words. Without them the stored error was the
    constant string 「无法以只读方式打开 INBOX。」, which is what the operator's
    panel, the alert mail and the AI operations assistant all repeated -- so a
    163 anti-abuse refusal ("Unsafe Login. Please contact kefu@188.com") was
    invisible to everybody, and the only way to learn it was a socket-level
    probe on the server.
    """
    words = _server_words(data)
    hint = ""
    if "unsafe login" in words.lower():
        hint = ("网易邮箱把这次登录判为「不安全登录」，因此拒绝打开收件箱；"
                "请到 163/126 网页版登录一次完成安全验证，或按它给的邮箱联系客服。")
    elif "authentication" in words.lower() or "login" in words.lower():
        hint = "邮箱服务商拒绝了这次访问，通常是授权码失效或该邮箱被限制登录。"
    detail = f"（邮箱服务器的原话：{words}）" if words else ""
    return MailError("无法以只读方式打开 INBOX。" + detail + hint)


def minimum_poll_seconds(config: dict[str, Any]) -> int:
    """The interval this provider asks for, or 0 when it publishes no figure.

    Zero means "no documented floor" — the caller should use its own default,
    which is deliberately the faster direction for providers that have never
    complained about us.
    """
    host = str((config or {}).get("imap_host") or "").strip().lower()
    if "gmail" in host or "googlemail" in host:
        return GMAIL_MIN_POLL_SECONDS
    return 0


class MailError(RuntimeError):
    pass


def _decode(value: str | None) -> str:
    try:
        return str(make_header(decode_header(value or "")))
    except (TypeError, ValueError):
        return value or ""


def _strip_html(value: str) -> str:
    value = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    value = re.sub(r"(?i)<br\s*/?>|</p\s*>", "\n", value)
    value = re.sub(r"(?s)<[^>]+>", " ", value)
    return re.sub(r"[ \t]+", " ", html.unescape(value).replace("\r", "")).strip()


def _part_text(part: Message) -> str:
    try:
        content = part.get_content()
        return content if isinstance(content, str) else str(content)
    except (LookupError, TypeError, UnicodeError):
        raw = part.get_payload(decode=True)
        return raw.decode(part.get_content_charset() or "utf-8", errors="replace") if isinstance(raw, bytes) else str(raw or "")


def _nested(message: Message) -> Message | None:
    for part in message.walk():
        if part.get_content_type() == "message/rfc822":
            payload = part.get_payload()
            if isinstance(payload, list) and payload and isinstance(payload[0], Message):
                return payload[0]
            if isinstance(payload, Message):
                return payload
    return None


def normalize_message(raw: bytes) -> dict[str, str]:
    outer = email.message_from_bytes(raw, policy=email.policy.default)
    message = _nested(outer) or outer
    plain: list[str] = []
    rich: list[str] = []
    attachments: list[str] = []
    for part in message.walk():
        if part.get_content_disposition() == "attachment":
            if part.get_filename():
                attachments.append(_decode(part.get_filename()))
            continue
        if part.get_content_maintype() == "multipart" or part.get_content_type() == "message/rfc822":
            continue
        if part.get_content_type() == "text/plain":
            plain.append(_part_text(part))
        elif part.get_content_type() == "text/html":
            rich.append(_part_text(part))
    body = "\n\n".join(plain) if plain else _strip_html("\n\n".join(rich) or _part_text(message))
    if attachments:
        body += "\n\n附件名称 / Attachments: " + ", ".join(attachments[:20])
    sender_name, sender_address = email.utils.parseaddr(_decode(message.get("From")))
    try:
        received = email.utils.parsedate_to_datetime(message.get("Date")).astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError):
        received = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    priority = str(message.get("X-Priority") or message.get("Importance") or "normal").lower()
    importance = "high" if priority.startswith(("1", "high")) else "low" if priority.startswith(("5", "low")) else "normal"
    # RFC 5322 Message-ID: the identity of the *mail*, independent of how many
    # times it was forwarded. A second copy carrying the same value is a
    # duplicate delivery, not a new message.
    message_id = re.sub(r"[\s\x00-\x1f]", "", _decode(message.get("Message-ID")))[:400]
    return {
        "subject": (_decode(message.get("Subject")) or "（无主题）")[:500],
        "sender_name": sender_name[:200], "sender_address": sender_address[:320],
        "received": received, "importance": importance, "body": _strip_html(body)[:MESSAGE_BODY_LIMIT],
        "message_key": message_id,
    }


def _checked_host(host: str) -> str:
    """连接之前再验一次目的地。

    保存邮箱时验过一次（`web.py` 的 `validate_public_host`），但那一次与真正连接之间隔着
    任意长的时间，而这中间 DNS 可以变（重绑定）。所以**每次连接前重验**：把窗口从
    「配置之后永远」缩到「解析到连接之间」，再把剩下那点竞态的诚实边界写在
    `docs/outbound-guard-2026-09-22.md` 里——这一次重验**不是**根治，它只是把口子关小。
    """
    try:
        # 解析不出来就放行：那是网络问题，连接自己会报；这里要拦的是
        # **解析到了内网/回环**（配置写错，或者保存之后 DNS 被改到内网）。
        return validate_public_host(host, allow_unresolved=True)
    except Exception as exc:
        raise MailError(f"邮件服务器地址不被允许：{redact_secrets(str(exc), [host])}") from None


#: 一次轮询最多取几封。为什么要有上限：`UID SEARCH` 会把所有匹配的 UID 一次性给出来，
#: 而一个积压了三千封的邮箱会在**一次**调用里逐封 `BODY.PEEK[]`——线程、内存和这一轮的
#: 时间全占住，同一个 worker 上别的用户跟着排队（2026-09-22 审查第四条 P2）。
#: 取不完不要紧：游标（`last_uid`）只推进到**真正处理过的**那一封，下一轮接着来。
MAX_MESSAGES_PER_POLL = int(os.environ.get("INFE_PILOT_MAX_MESSAGES_PER_POLL", "25"))

#: 单封邮件的字节上限（先用 `RFC822.SIZE` 问一句）。超过就**不取正文**：只取报头，
#: 把它记成一条**可见的**「过大」记录，而不是整个读进内存。25 MB 是带大附件邮件的量级；
#: 报告只需要正文，而附件往往是误转发进来的。
MAX_MESSAGE_BYTES = int(os.environ.get("INFE_PILOT_MAX_MESSAGE_BYTES", str(25 * 1024 * 1024)))


def _declared_size(client, uid: int) -> int:
    """问服务器这封多大（`RFC822.SIZE`）。问不出来就返回 0（照常取）。"""
    try:
        status, data = client.uid("fetch", str(uid), "(RFC822.SIZE)")
    except Exception:      # pragma: no cover - 服务器脾气，测试里由替身覆盖
        return 0
    if status != "OK" or not data:
        return 0
    match = re.search(rb"RFC822\.SIZE (\d+)", data[0] if isinstance(data[0], bytes) else b"")
    return int(match.group(1)) if match else 0


def _fetch_headers_only(client, uid: int) -> bytes:
    """只取报头（不取正文）。超大邮件走这条，好让那封信仍然有主题与发件人可记。"""
    status, content = client.uid("fetch", str(uid), "(BODY.PEEK[HEADER])")
    if status != "OK":
        return b""
    return next((item[1] for item in content if isinstance(item, tuple) and isinstance(item[1], bytes)), b"")


#: IMAP 的 TLS 上下文。**必须显式传给 `IMAP4_SSL`**。
#:
#: 为什么（2026-09-24 在生产上实测）：`IMAP4_SSL(..., ssl_context=None)` 会落到
#: `ssl._create_stdlib_context()`，而那个名字在 CPython 里**就是**
#: `_create_unverified_context` —— 生产是 Python 3.14.4，实测
#: `ssl._create_stdlib_context is ssl._create_unverified_context` → **True**，
#: `verify_mode=CERT_NONE`、`check_hostname=False`，拿 imap.qq.com 握手回来
#: `getpeercert()` 是空的。也就是说在那之前，用户邮箱的**授权码与全部邮件正文**
#: 跑在一条不校验证书的会话上，而下面 `explain_imap_failure` 里那句
#: 「TLS 证书校验失败」在这条路上**永远不会响**——它会让人以为校验是开着的。
#:
#: 进程内复用一份：`create_default_context()` 要读 CA 库，1500 个邮箱每分钟一次
#: 不值得每轮重建。线程间共享是安全的（SSLContext 本身可重入）。
_IMAP_SSL_CONTEXT: Optional[ssl.SSLContext] = None


def imap_ssl_context() -> ssl.SSLContext:
    """校验证书与主机名的 IMAP TLS 上下文（参见过滤注释）。"""
    global _IMAP_SSL_CONTEXT
    if _IMAP_SSL_CONTEXT is None:
        context = ssl.create_default_context()
        # 两行都写：`create_default_context()` 本来就是这两个值，但把它们写出来，
        # 是为了让"这条连接必须校验"成为一个**可被测试读到**的事实。
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        _IMAP_SSL_CONTEXT = context
    return _IMAP_SSL_CONTEXT


def fetch_new_messages(config: dict[str, Any], password: str, *, initial_lookback_hours: int = 48) -> tuple[str, list[tuple[int, dict[str, str]]], int]:
    try:
        client = imaplib.IMAP4_SSL(_checked_host(config["imap_host"]),
                                   int(config["imap_port"]), timeout=30,
                                   ssl_context=imap_ssl_context())
        identified = identify_client(client)
        client.login(config["email"], password)
        if not identified:
            # 有些服务器只认登录之后的 ID；两种顺序在 163 上实测都有效。
            identify_client(client)
        status, data = client.select("INBOX", readonly=True)
        if status != "OK":
            raise refused_inbox(data)
        uid_validity = ""
        status, response = client.response("UIDVALIDITY")
        if status == "UIDVALIDITY" and response:
            uid_validity = response[0].decode(errors="ignore") if isinstance(response[0], bytes) else str(response[0])
        last_uid = int(config.get("last_uid") or 0) if not config.get("uid_validity") or config.get("uid_validity") == uid_validity else 0
        if last_uid:
            criteria = ("UID", f"{last_uid + 1}:*")
        else:
            since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=initial_lookback_hours)).strftime("%d-%b-%Y")
            criteria = ("SINCE", since)
        status, rows = client.uid("search", None, *criteria)
        if status != "OK":
            raise MailError("IMAP 搜索新邮件失败。")
        found: list[tuple[int, dict[str, str]]] = []
        highest_seen = last_uid
        processed = 0
        for raw_uid in (rows[0].split() if rows and rows[0] else []):
            uid = int(raw_uid)
            if uid <= last_uid:
                continue
            if processed >= MAX_MESSAGES_PER_POLL:
                # 剩下的**下一轮再来**：游标只推到处理过的位置，所以不会漏，也不会
                # 在一次轮询里被一个积压邮箱占住。UID SEARCH 的结果是升序的，
                # 所以「取前 N 封」就是「取最早的 N 封」。
                break
            processed += 1
            highest_seen = max(highest_seen, uid)
            size = _declared_size(client, uid)
            if size and size > MAX_MESSAGE_BYTES:
                # **不取正文**：整个读进内存才是这条要防的事。仍然留下一行记录，
                # 由 `service` 记成可见的「过大」跳过——静默丢掉是最坏的选择。
                raw = _fetch_headers_only(client, uid)
                message = normalize_message(raw) if raw else {
                    "subject": "（过大的邮件）", "sender_name": "", "sender_address": "",
                    "received": "", "importance": "normal", "body": "", "message_key": "",
                }
                message["body"] = ""
                message["oversized"] = True
                message["size_bytes"] = size
                found.append((uid, message))
                logging.info("message uid %s in %s is %.1f MB — header only, no body fetched",
                             uid, config.get("email", ""), size / 1048576)
                continue
            status, content = client.uid("fetch", str(uid), "(BODY.PEEK[])")
            if status != "OK":
                raise MailError(f"读取邮件 UID {uid} 失败。")
            raw = next((item[1] for item in content if isinstance(item, tuple) and isinstance(item[1], bytes)), None)
            if raw is None:
                raise MailError(f"邮件 UID {uid} 没有正文。")
            message = normalize_message(raw)
            if not message["subject"].startswith(GENERATED_PREFIXES):
                found.append((uid, message))
        return uid_validity, found, highest_seen
    except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
        raise MailError(explain_imap_failure(exc, secret=password)) from None
    finally:
        if "client" in locals():
            try:
                client.close()
            except Exception:
                pass
            try:
                client.logout()
            except Exception:
                pass


def fetch_recent_messages(config: dict[str, Any], password: str, *, count: int = 2,
                          lookback_days: int = 30) -> list[tuple[int, dict[str, str]]]:
    """Read the newest real messages from a mailbox, for verification only.

    Deliberately independent of ``last_uid``: this is how ``verify-e2e`` gets a
    real message corpus when the worker's cursor is already caught up. It is
    read-only (``BODY.PEEK[]``, ``readonly=True``) and returns at most ``count``
    messages, newest last.
    """
    try:
        client = imaplib.IMAP4_SSL(_checked_host(config["imap_host"]),
                                   int(config["imap_port"]), timeout=30,
                                   ssl_context=imap_ssl_context())
        identified = identify_client(client)
        client.login(config["email"], password)
        if not identified:
            identify_client(client)
        status, data = client.select("INBOX", readonly=True)
        if status != "OK":
            raise refused_inbox(data)
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max(1, lookback_days))).strftime("%d-%b-%Y")
        status, rows = client.uid("search", None, "SINCE", since)
        if status != "OK":
            raise MailError("IMAP 搜索历史邮件失败。")
        uids = [int(value) for value in (rows[0].split() if rows and rows[0] else [])]
        found: list[tuple[int, dict[str, str]]] = []
        for uid in reversed(uids):
            if len(found) >= max(1, count):
                break
            status, content = client.uid("fetch", str(uid), "(BODY.PEEK[])")
            if status != "OK":
                continue
            raw = next((item[1] for item in content if isinstance(item, tuple) and isinstance(item[1], bytes)), None)
            if raw is None:
                continue
            message = normalize_message(raw)
            if message["subject"].startswith(GENERATED_PREFIXES):
                continue
            found.append((uid, message))
        return list(reversed(found))
    except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
        raise MailError(explain_imap_failure(exc, secret=password)) from None
    finally:
        if "client" in locals():
            try:
                client.close()
            except Exception:
                pass
            try:
                client.logout()
            except Exception:
                pass


# 「看原信」的两种「取不到」。放在 mailio 里是因为**只有这里知道为什么取不到**：
# 网页层要按这两种分别说人话，而不是一律「加载失败」。
ORIGINAL_GONE = "gone"      # 邮箱里已经没有这一封了（被删/被移走）
ORIGINAL_MOVED = "moved"    # 邮箱被重建过（UIDVALIDITY 变了），这串 UID 指的是别的信


def fetch_message_by_uid(config: dict[str, Any], password: str, uid: int, *,
                         uid_validity: str = "") -> dict[str, Any]:
    """Read exactly one message back by UID. Read-only; **nothing is stored**.

    Why this exists: the raw body is wiped the moment the report is delivered
    (``Database.finish_message``, and the privacy policy promises it), so "show
    me that mail again" cannot be answered from our own database. The mailbox
    still has it — we kept ``uid_validity`` and ``imap_uid`` for exactly this.

    ``uid_validity`` is a door number, not a detail: once the mailbox is rebuilt,
    the same UID string points at a *different* message. Showing that other
    message under this task would be worse than showing nothing, so a mismatch
    comes back as ``ORIGINAL_MOVED`` and we never fetch in that case.
    """
    client = None
    try:
        client = imaplib.IMAP4_SSL(_checked_host(config["imap_host"]),
                                   int(config["imap_port"]), timeout=30,
                                   ssl_context=imap_ssl_context())
        identified = identify_client(client)
        client.login(config["email"], password)
        if not identified:
            identify_client(client)
        status, data = client.select("INBOX", readonly=True)
        if status != "OK":
            raise refused_inbox(data)
        live = ""
        status, response = client.response("UIDVALIDITY")
        if status == "UIDVALIDITY" and response:
            first = response[0]
            live = first.decode(errors="ignore") if isinstance(first, bytes) else str(first)
        if uid_validity and live and live != str(uid_validity):
            return {"state": ORIGINAL_MOVED, "uid_validity": live}
        status, content = client.uid("fetch", str(uid), "(BODY.PEEK[])")
        if status != "OK":
            raise MailError("IMAP 取回这一封失败。")
        raw = next((item[1] for item in content if isinstance(item, tuple) and isinstance(item[1], bytes)), None)
        if raw is None:
            return {"state": ORIGINAL_GONE}
        message = normalize_message(raw)
        return {"state": "ok", "message": message,
                "truncated": len(message["body"]) >= MESSAGE_BODY_LIMIT}
    except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
        raise MailError(explain_imap_failure(exc, secret=password)) from None
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
            try:
                client.logout()
            except Exception:
                pass


def probe_mailbox(config: dict[str, Any], password: str, *, lookback_hours: int = 48) -> dict[str, Any]:
    """Read-only reconnaissance for migration safety checks.

    Returns the live UIDVALIDITY, every UID currently in the mailbox, and the
    subset inside the lookback window. Fetches no bodies and never moves the
    cursor. Callers use it to verify a supplied UIDVALIDITY and to report
    "holes" (mail present in the mailbox but absent from the legacy processed
    set) before applying a migration.
    """
    client = None
    try:
        client = imaplib.IMAP4_SSL(_checked_host(config["imap_host"]),
                                   int(config["imap_port"]), timeout=30,
                                   ssl_context=imap_ssl_context())
        identified = identify_client(client)
        client.login(config["email"], password)
        if not identified:
            identify_client(client)
        status, data = client.select("INBOX", readonly=True)
        if status != "OK":
            raise refused_inbox(data)
        uid_validity = ""
        status, response = client.response("UIDVALIDITY")
        if status == "UIDVALIDITY" and response:
            uid_validity = response[0].decode(errors="ignore") if isinstance(response[0], bytes) else str(response[0])
        status, rows = client.uid("search", None, "ALL")
        if status != "OK":
            raise MailError("IMAP 搜索全部邮件失败。")
        present = sorted(int(value) for value in (rows[0].split() if rows and rows[0] else []))
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=max(1, lookback_hours))).strftime("%d-%b-%Y")
        status, rows = client.uid("search", None, "SINCE", since)
        if status != "OK":
            raise MailError("IMAP 搜索最近邮件失败。")
        recent = sorted(int(value) for value in (rows[0].split() if rows and rows[0] else []))
        return {"uid_validity": uid_validity, "present": present, "recent": recent}
    except (imaplib.IMAP4.error, OSError, ssl.SSLError) as exc:
        raise MailError(explain_imap_failure(exc, secret=password)) from None
    finally:
        if client is not None:
            for closer in (client.close, client.logout):
                try:
                    closer()
                except Exception:
                    pass


#: 供应商「在限流我们 / 封我们」这类措辞——**我们自己的译文与服务器的原话都在内**。
#: 为什么需要这个分类（2026-09-24）：`alerting.tier_for` 把 `mailbox_error:` 刻意分到
#: 最安静的一档（面板红灯、**不发邮件**），理由是"一个账号的授权码错了是他自己的问题"。
#: 单账号时那是对的；**但整家供应商开始限流时，同一个 key 会把唯一重要的信号静音**
#: ——300 个邮箱一起被限流 = 300 条安静的红灯、零封邮件。所以两类必须分开。
PUSHBACK_MARKERS = (
    "login frequency limited", "frequency limit", "too many", "rate limit",
    "rate-limit", "ip is rejected", "ip banned", "temporarily blocked",
    "temporarily limited", "try again later",
    "登录尝试过于频繁", "被邮箱临时限制", "登录频率", "超过限制",
)
#: 凭据类的措辞。与 `explain_imap_failure` 的那几支**共用同一批字符串**，
#: 这样"它是怎么被解释的"与"它是怎么被分类的"不会各说一套。
CREDENTIAL_MARKERS = (
    "authenticationfailed", "authentication failed", "invalid credentials",
    "login failed", "password error", "login fail", "account is abnormal",
    "service is not open", "授权码不对或已失效",
)


def classify_imap_failure(exc: Exception | None = None, *, text: str = "") -> str:
    """``"credential"`` / ``"pushback"`` / ``"network"`` / ``"other"``.

    只看**文本**：`mailboxes.last_error` 存的就是一句文本，异常对象早就没了。

    顺序与 `explain_imap_failure` 一致（先凭据、后限流）——两处判断同一件事，
    顺序不同就会在"两句话都沾"的罕见文本上给出互相矛盾的答案。
    """
    blob = f"{text} {exc if exc is not None else ''}".lower()
    if any(marker in blob for marker in CREDENTIAL_MARKERS):
        return "credential"
    if any(marker in blob for marker in PUSHBACK_MARKERS):
        return "pushback"
    if isinstance(exc, (ssl.SSLError, OSError)) or "certificate" in blob or "证书" in blob \
            or "连接不上" in blob:
        return "network"
    return "other"


def explain_imap_failure(exc: Exception, *, secret: str = "") -> str:
    """Turn opaque server errors into something a pilot user can act on.

    Microsoft already forces OAuth on personal Outlook/Hotmail mailboxes, so a
    correct app password still fails with "Basic authentication is disabled".
    That is not a user mistake and must not be reported as one.
    """
    text = str(exc)
    lowered = text.lower()
    if "basic authentication is disabled" in lowered or "logon is denied" in lowered:
        return (
            "这个邮箱的服务商已经停用「账号密码 / 授权码」登录（微软 Outlook、Hotmail 已强制改用 OAuth）。"
            "请换一个支持授权码的邮箱作为转发邮箱，例如 QQ 邮箱、Gmail 或 163 邮箱。"
        )
    if "unknown user" in lowered or "user is unknown" in lowered:
        return "邮箱地址不存在，请检查「你的私人邮箱」是否填对。"
    # 163/126 的原话是 `LOGIN Login error or password error`（IMAP）与
    # `535 Error: authentication failed`（SMTP）——两种拼法都不含下面那些常见的
    # 关键词，所以 2026-09-18 那天，一个真实用户看到的是**原始异常**
    # 「IMAP 连接失败：b'LOGIN Login error or password error'」：一句英文、
    # 没有下一步。这条通道上最常见的一种失败，却给了最没用的一句话。
    if ("authenticationfailed" in lowered or "authentication failed" in lowered
            or "invalid credentials" in lowered or "login failed" in lowered
            or "password error" in lowered):
        return (
            "邮箱拒绝了这次登录：**授权码不对或已失效**。它**不是邮箱的登录密码**"
            "（QQ/163 叫「授权码」「客户端授权密码」，Gmail 叫「应用专用密码」）。"
            "请到邮箱网页版的设置里重新生成一个，复制时不要带空格；"
            "如果那里显示 IMAP/SMTP 服务还没开启，先开启它再生成。"
        )
    if "login fail" in lowered or "account is abnormal" in lowered or "service is not open" in lowered:
        return (
            "邮箱拒绝了这次登录。常见原因：① 授权码不对或已被重置；② 该邮箱还没在设置里开启 "
            "IMAP/SMTP 服务；③ 邮箱被临时限制登录（例如改过密码或异地登录）。"
            "请到邮箱网页版的「设置 → 账户 / 客户端」确认服务已开启，并重新生成授权码。"
        )
    if "login frequency limited" in lowered or "too many" in lowered:
        return "登录尝试过于频繁，已被邮箱临时限制，请等 15–30 分钟后重试。"
    if isinstance(exc, ssl.SSLError) or "certificate" in lowered:
        return "TLS 证书校验失败，请确认收件服务器地址是否正确。"
    if isinstance(exc, OSError):
        # 与下面那条兜底一样要过脱敏：这一句会进 `mailboxes.last_error`（明文列）。
        return f"连接不上邮件服务器（网络不通或端口被拦）：{redact_secrets(text, [secret])}"
    # 兜底那句会把服务器的原话端出去——**先抹掉我们发出去的那串口令**：
    # 这句话会进 `mailboxes.last_error` 并显示在界面上，而它是明文列。
    return f"IMAP 连接失败：{redact_secrets(text, [secret])}"


def markdown_to_html(markdown: str, subject: str) -> str:
    safe_subject = html.escape(subject)
    parts = [f'<div style="background:#f4f7fb;padding:24px 12px;font-family:Arial,sans-serif"><div style="max-width:680px;margin:auto;background:#fff;border:1px solid #d9e2ec"><div style="padding:22px 28px;background:#123b63;color:#fff"><div style="font-size:12px;letter-spacing:.08em">CITYU MAIL PILOT</div><h1 style="font-size:21px;margin:8px 0 0">{safe_subject}</h1></div><div style="padding:22px 28px;color:#243447;line-height:1.6">']
    list_open = False
    for raw in markdown.replace("\r", "").splitlines():
        line = raw.strip()
        if not line:
            if list_open:
                parts.append("</ul>"); list_open = False
            continue
        heading = re.match(r"^#{1,4}\s+(.+)$", line)
        bullet = re.match(r"^[-*•]\s+(.+)$", line)
        if heading:
            if list_open: parts.append("</ul>"); list_open = False
            parts.append(f'<h2 style="font-size:17px;color:#123b63;margin:20px 0 8px">{html.escape(heading.group(1))}</h2>')
        elif bullet:
            if not list_open: parts.append('<ul style="padding-left:22px">'); list_open = True
            value = html.escape(bullet.group(1))
            value = re.sub(r"(https://[^\s&]+)", r'<a href="\1">\1</a>', value)
            parts.append(f"<li>{value}</li>")
        else:
            if list_open: parts.append("</ul>"); list_open = False
            parts.append(f"<p>{html.escape(line)}</p>")
    if list_open: parts.append("</ul>")
    # No AI disclaimer here, deliberately. This converter is the *fallback* used
    # when a caller supplies no `html_body`, and the HTML shell is all a mail
    # needs from its transport -- a transport must not assert anything about
    # content it did not produce. It used to append 「AI 生成内容可能出错…」, and
    # because every report and alert path passes its own rendering, the only
    # messages that ever reached this line were the four the operator writes by
    # hand: the invite code, the setup reminder, the unit-failure alert and the
    # new-application notice. Each of them was denying responsibility for text
    # no model had touched. The claim still exists where it is true --
    # `reports.CONTENT_DISCLAIMER`, owned by the code that composes the report.
    parts.append("</div></div></div>")
    return "".join(parts)


def stable_message_id(key: str, sender: str) -> str:
    """由我们自己的 id 推出来的 Message-ID（重试复用同一个）。

    `email.utils.make_msgid()` 每次都给一个新的：同一份报告重试两次，收件人那边就是两封
    **不同的**信。这里用报告 id 生成稳定的那个——重复投递（SMTP 收了但应答丢了）时，
    至少两封信带同一个 Message-ID，人能看出是同一件事，将来真做去重也有了前提。
    域名仍取自发件地址，不泄露服务器主机名。
    """
    domain = str(sender).split("@")[-1].strip() or "localhost"
    return f"<{key}@{domain}>"


def send_report(config: dict[str, Any], password: str, subject: str, markdown: str,
                *, html_body: str | None = None, text_body: str | None = None,
                from_name: str | None = None, reply_to: str | None = None,
                inline_image: tuple[bytes, str, str] | None = None,
                message_id: str | None = None) -> dict[str, Any]:
    """Send one message and return a receipt for it.

    ``html_body``/``text_body`` let callers supply the structured, action-first
    renderings from :mod:`pilot_app.reports`. Without them the plain-text part is
    the raw markdown and the HTML is the legacy converter, which keeps old call
    sites (and tests) working.

    The return value exists so a caller can *record* that a send happened rather
    than only observe it in the moment. Two fields:

    ``message_id``
        Set explicitly and returned. Until this existed outgoing mail carried no
        Message-ID at all, which is both a deliverability smell and the loss of
        the one identifier that lets a human correlate our send with a line in
        the provider's log or a mail header on the recipient's side. The domain
        is taken from the sender address rather than the machine, so the id does
        not leak the server's hostname.

    ``inline_image``
        ``(data, subtype, cid)`` for **one** picture embedded in the HTML part
        (broadcasts only, today). Embedded rather than linked: an image hosted on
        our server is a remote image, and every mainstream client blocks those by
        default — the reader would get an empty box. Embedded, it travels with the
        message and still renders years later. The cost is size, which is why the
        caller re-encodes before uploading (2048px / 1.4MB) and why reports never
        carry one.

    ``refused``
        ``smtplib``'s per-recipient refusal map. Empty means every recipient was
        accepted. With a single recipient an all-refused result arrives as an
        exception instead, so this is normally empty; it is returned anyway
        because "accepted for this person" is exactly the claim being made, and a
        non-empty map must never be mistaken for success.
    """
    message = EmailMessage()
    # A display name is what the recipient's client shows next to the address.
    # For a first-contact message from a personal mailbox that is the difference
    # between "some address I do not know" and the name they just saw on the
    # website -- and a bare address is one of the shapes spam is made of.
    # `formataddr` handles the RFC 2047 encoding a Chinese name needs.
    message["From"] = (email.utils.formataddr((from_name, config["email"]))
                       if from_name else config["email"])
    message["To"] = config["report_to"]
    if reply_to:
        # Set explicitly rather than left implicit: the recipient is being asked
        # to reply ("回这封信就行"), and a reply is the strongest signal a
        # provider has that the message was wanted.
        message["Reply-To"] = reply_to
    message["Subject"] = subject
    # **每一封都要有 Date**：RFC 5322 §3.6 里只有 `Date` 与 `From` 是必填的两项，
    # 而 `EmailMessage()` **不会**替你补（`smtplib` 也不补）。
    #
    # 这一条是 2026-09-26 从**收件方留存的报头**里读出来的，不是从我们自己的代码里推的：
    # 用户在 QQ 邮箱里说「刚刚那封 AI 摘要没收到」，把 INBOX 里那一封（uid 2646）的报头
    # 拉下来看，`Received` 是 QQ 自己盖的，而 `Date` **一行都没有** —— 也就是说从
    # 2026-09-13 上线以来，报告、邀请码、提醒、告警**每一封**都缺这个字段，
    # 收件端只是恰好都替我们兜住了（QQ 用 Received 排序，所以没人看出来）。
    # 代价是白白吃垃圾邮件的 MISSING_DATE 一类规则，在别的收件方不一定有人兜。
    #
    # 用本地时间带数值偏移（`+0800`/`+0000`），而不是 `-0000`：后者按 RFC 的意思是
    # 「不知道时区」，我们把知道的那部分如实写出来。重试会生成新的 Date，这是对的
    # ——重试是另一次投递，不像 Message-ID 那样必须稳定。
    message["Date"] = email.utils.formatdate(localtime=True)
    domain = str(config["email"]).split("@")[-1] or None
    # 调用方给了就用它：**重试同一份报告要复用同一个 Message-ID**，否则同一件事在收件人
    # 那边是两封不同的信（客户端不去重，但人看得出来是同一封，运维对日志也有个可比的 id）。
    # 这不是 exactly-once——SMTP 收了而应答丢了那一档本来就无法从这一侧证明，
    # 真正的幂等要 outbox。这里只是把「能稳定的那部分」稳定下来。
    message_id = message_id or email.utils.make_msgid(domain=domain)
    message["Message-ID"] = message_id
    message.set_content(text_body if text_body is not None else markdown, charset="utf-8")
    message.add_alternative(html_body or markdown_to_html(markdown, subject), subtype="html", charset="utf-8")
    if inline_image:
        # 把图片挂到 **HTML 那一部分**上（而不是整封信）：`add_related` 会把那个
        # text/html 部分变成 multipart/related，里面装 HTML + 图。挂在顶层就会
        # 变成「纯文本或图」的二选一，那正是 `multipart/alternative` 的语义。
        data, subtype, cid = inline_image
        message.get_payload()[-1].add_related(
            data, maintype="image", subtype=subtype or "jpeg",
            cid=f"<{cid}>" if not str(cid).startswith("<") else str(cid),
            filename="notice.jpg", disposition="inline")
    context = ssl.create_default_context()
    refused: dict[str, Any] = {}
    try:
        if int(config["smtp_port"]) == 465:
            with smtplib.SMTP_SSL(_checked_host(config["smtp_host"]), int(config["smtp_port"]),
                                  context=context, timeout=30) as client:
                client.login(config.get("smtp_user") or config["email"], password)
                refused = client.send_message(message)
        else:
            with smtplib.SMTP(_checked_host(config["smtp_host"]), int(config["smtp_port"]),
                              timeout=30) as client:
                client.ehlo(); client.starttls(context=context); client.ehlo()
                client.login(config.get("smtp_user") or config["email"], password)
                refused = client.send_message(message)
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        # 断链 + 脱敏：SMTP 的报错原文由服务器给，可能带上我们发出去的口令或用户名；
        # 我们的 message 抹过，而 `__cause__` 会原样保留它（日志的 traceback 会印）。
        raise MailError(f"SMTP 发送失败：{redact_secrets(str(exc), [password])}") from None
    return {"message_id": message_id, "refused": refused or {}}
