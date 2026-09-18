"""Orchestration for polling, personalised analysis, delivery, and daily reports."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import socket
import time
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import alerts, digest_synthesis, mailio, pricing, prompts, providers, reports, triage
from .database import Database
from .security import SecretBox

# Migration safety valve: after the legacy UID set is imported as placeholders,
# the first poll deliberately re-scans a bounded window so any hole the old
# worker left is picked up. A hole older than the window would be skipped
# forever, so this is configurable and reported by `migrate-legacy-imap-state`.
INITIAL_LOOKBACK_HOURS = max(1, int(os.environ.get("INFE_PILOT_INITIAL_LOOKBACK_HOURS", "48")))

# How many tokens the model may spend on one report. Measured reports land
# around 2.2k-2.8k characters, so the old hard-coded 4000 was mostly headroom;
# making it explicit and tunable is what lets the latency work bound the
# worst-case generation time. Raise it with INFE_PILOT_REPORT_MAX_TOKENS.
REPORT_MAX_TOKENS = max(600, int(os.environ.get("INFE_PILOT_REPORT_MAX_TOKENS", "4000")))

# 每日简报失败之后的重试节奏（秒），以及"今天最多试几次"。
#
# 为什么要这道闸门：2026-09-18 用户报「后台显示 5 个报告失败，但刷新下发情况又没有」，
# 查下去发现底下压着真问题——`run_daily_due` 由主循环每 15 秒调一次，而
# `daily_report_exists` 只认 `status='sent'`，所以一封发不出去的简报会被**每 15 秒
# 重发一次**，一直到当天结束：一个授权码坏掉的账号，一晚上 **1185 次** SMTP 尝试
# （而 163 回给我们的原话里就写着「IP is rejected」——我们可能正在自己把这条路走坏）。
# 邮件那条路有重试与退避，简报这条路当时什么都没有。
#
# 一次失败几乎总是"这个账号自己的设置不对"（授权码错、服务商停用授权码），重试救不了它；
# 但网络抖动值得再试。所以：5 分钟、30 分钟、2 小时，之后当天不再试，第二天日期一变
# 就重新开始。
DIGEST_RETRY_BACKOFF = (300, 1800, 7200)
# The condensed first report is deliberately small; a short cap keeps a chatty
# model from turning "brief" into another long generation.
BRIEF_MAX_TOKENS = max(300, int(os.environ.get("INFE_PILOT_BRIEF_MAX_TOKENS", "1200")))
# 「看原信」里的翻译/总结：预算按下限给，翻译再按原文字数放大。
# 为什么不是固定 1500：一处真机实测——一封 6374 字的信，1500 的预算下模型
# finish_reason=length（译文被砍在半路），按原文字数给（6374）就 finish=stop。
# 中文译文大约是英文字数的 0.4–0.5 倍，而一个中文字≈一个 token，所以「原文字数」
# 这个预算有一倍余量。上限是可配的：它是按次计费里最贵的一项。
ASSIST_MIN_TOKENS = max(400, int(os.environ.get("INFE_PILOT_ASSIST_MIN_TOKENS", "1500")))
ASSIST_MAX_TOKENS = max(ASSIST_MIN_TOKENS, int(os.environ.get("INFE_PILOT_ASSIST_MAX_TOKENS", "8000")))

# 一次「翻译」最多分几段重来。超出这个数就不再切了——宁可如实说没翻出来，
# 也不要让一次点击变成二十次模型调用。
ASSIST_MAX_CHUNKS = 8
# 分段翻译时每段的目标长度。
ASSIST_CHUNK_CHARS = 1200

# Only mail from these sender domains becomes a report. The user's private
# mailbox also receives their personal mail (shopping, banks, newsletters);
# summarising those is unwanted noise. Empty value disables the filter and
# falls back to the old "process everything" behaviour.
# Two-stage instant delivery: send a condensed report first, then the full one.
# Purely a behaviour switch (no schema or data change), so rolling back is
# "set INFE_PILOT_BRIEF_FIRST=0 and restart" — nothing to migrate.
BRIEF_FIRST = os.environ.get("INFE_PILOT_BRIEF_FIRST", "0") == "1"
# With FULL_REPORT=0 the instant notification is *only* the condensed report.
# The full analysis still runs as a safety net when the brief fails, so a
# message can never end up with no report at all. The 22:00 digest is unaffected
# either way: it aggregates whatever reports exist for the day.
FULL_REPORT = os.environ.get("INFE_PILOT_FULL_REPORT", "1") != "0"


def instance_report_mode() -> str:
    """What a user who has not chosen for themselves gets, as one word.

    One definition, used by the send path's readers and by the panel that has to
    explain it. ``both`` is the two-stage mode (brief first, then the full
    report); it only ever happens when an operator turns it on for the whole
    instance, and the panel says so plainly instead of pretending it is one of
    the choices a user can make.
    """
    if BRIEF_FIRST and FULL_REPORT:
        return "both"
    return "brief" if BRIEF_FIRST else "full"

# Feed the deterministic triage verdict to the model as a hint. Rationale: the
# model is a heavy reasoner and re-deriving the category is part of that hidden
# cost. Only kept if a real A/B shows it does not hurt quality or latency.
INCLUDE_TRIAGE_HINT = os.environ.get("INFE_PILOT_TRIAGE_HINT", "1") != "0"

# Send an instant, model-free arrival alert so the user is not left waiting for
# a slow report. Turn off with INFE_PILOT_ALERT_ON_ARRIVAL=0; restrict it to
# deadline-ish mail with INFE_PILOT_ALERT_URGENT_ONLY=1.
ALERT_ON_ARRIVAL = os.environ.get("INFE_PILOT_ALERT_ON_ARRIVAL", "1") != "0"
ALERT_URGENT_ONLY = os.environ.get("INFE_PILOT_ALERT_URGENT_ONLY", "0") == "1"

ALLOWED_SENDER_DOMAINS = tuple(
    item.strip().lower().lstrip("@")
    for item in os.environ.get("INFE_PILOT_ALLOWED_SENDER_DOMAINS", "cityu.edu.hk").split(",")
    if item.strip()
)


def log_job_failure(what: str, subject: Any, exc: Exception) -> None:
    """Log a failed job at the severity it actually has.

    A ``mailio.MailError`` is the mail server rejecting *this account's settings*
    (wrong auth code, IMAP/SMTP not enabled, unreachable host). It is an expected
    outcome that the account owner is already told about inside the app, that the
    admin panel shows as a red light and that the sentinel reports as
    ``mailbox_error`` -- so it gets one line. On 2026-09-15 a single account with
    a mistyped auth code produced ~750 lines of identical traceback a day in
    ``journalctl -u cityu-mail-pilot-worker``, which is enough to bury a real
    error and makes the log useless for the one question it is read for.

    Anything that is *not* a ``MailError`` is a failure nobody anticipated, and
    that one keeps its traceback.

    Deliberately one definition, next to the code that raises ``MailError``: this
    rule has four call sites (the thread-pool poller, the queue, and both
    single-threaded reference paths), and a version of this fix that taught only
    the poller would repeat the mistake recorded in v0.59.1 -- *one rule, several
    consumers*.

    The boundary is *per-cycle*: this covers the paths that run every poll or
    every message. Paths that run once a day (the digest, the announcement pass)
    keep their traceback -- there the volume is one line per user per day, and a
    once-a-day failure is worth the detail.
    """
    if isinstance(exc, mailio.MailError):
        logging.warning("%s failed for %s: %s", what, subject, exc)
    else:
        logging.error("%s failed for %s", what, subject, exc_info=exc)


def sender_domain(value: str) -> str:
    """The bare domain of a From/sender address, lower-cased."""
    address = str(value or "").strip().strip("<>").lower()
    return address.rsplit("@", 1)[-1] if "@" in address else ""


def is_allowed_sender(address: str) -> bool:
    """True when this sender may be analysed (no allow-list = allow all)."""
    if not ALLOWED_SENDER_DOMAINS:
        return True
    domain = sender_domain(address)
    if not domain:
        return False
    return any(domain == allowed or domain.endswith("." + allowed) for allowed in ALLOWED_SENDER_DOMAINS)


class PilotService:
    def __init__(self, database: Database, secrets: SecretBox):
        self.db = database
        self.secrets = secrets
        # (user_id, 简报日期) → (已试次数, 下次可试的 monotonic 时刻)。
        # 存在内存里而不是库里：它只是"别把同一个错误每 15 秒重发一遍"的节流，
        # 进程重启后多试一次无害；写进库反而要多一次迁移与一张会过期的表。
        self._digest_retry: dict[tuple[str, str], tuple[int, float]] = {}

    @staticmethod
    def _model_attempts() -> int:
        """How many times one generation may be attempted (default 2)."""
        try:
            return max(1, min(4, int(os.environ.get("INFE_PILOT_MODEL_ATTEMPTS", "2"))))
        except ValueError:
            return 2

    @staticmethod
    def _transient(exc: Exception) -> bool:
        if isinstance(exc, providers.TransientProviderError):
            return True
        # Stubs and third-party adapters may raise raw socket errors.
        return isinstance(exc, (TimeoutError, socket.timeout, OSError))

    @staticmethod
    def _blames_credential(exc: Exception) -> bool:
        """True only when the *provider* rejected this credential or model.

        An allow-list, and deliberately not "anything that is not transient".
        The breaker's whole sentence is 「这把 key 是坏的」, so it may only speak
        when the far end actually said so -- a non-retryable answer from the
        provider (HTTP 401/403/404/400, a wrong model name).

        The deny-list version of this was wrong, and a real run caught it: an
        unresolvable API host raises `SecurityError` from the outbound-URL gate,
        which is neither a `ProviderError` nor on the transient list, so a DNS
        hiccup counted towards a *credential* verdict and three of them
        suspended the account. The same trap waits for any of our own parsing
        bugs: filing them against the user's key locks out somebody whose key is
        fine, which is worse than the queue waste this feature removes.
        """
        return (isinstance(exc, providers.ProviderError)
                and not isinstance(exc, providers.TransientProviderError))

    def _generate_with_retry(self, user_id: str, **kwargs: Any) -> Any:
        """Run one generation, retrying only transient provider failures.

        A long report can be cut off mid-flight (a real 240s run ended with
        "Remote end closed connection without response"), and a single retry
        turns that from a lost email into a slightly slower one. Non-transient
        failures (bad key, bad model name) are raised immediately.

        This is also the **only** place that decides whether a failure counts
        against the account's credential. That decision has to live in exactly
        one place: `_transient` is already this project's definition of "worth
        retrying", and the circuit breaker it feeds means "stop trying, this
        credential is wrong". If a timeout or a 429 were counted here, a
        provider's bad afternoon would suspend innocent accounts -- which is why
        the counting sits next to the classification instead of at the callers.
        """
        attempts = self._model_attempts()
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                result = providers.generate(**kwargs)
            except Exception as exc:
                last = exc
                if isinstance(exc, providers.ProviderTimeout):
                    # A timeout has already spent the whole budget (measured:
                    # ~234 s for a real report against a 300 s ceiling).
                    # Retrying it now would hold this generation slot for twice
                    # as long, for a provider that is evidently struggling. The
                    # message is marked failed and the queue retries it with
                    # exponential backoff instead.
                    logging.warning(
                        "model timed out for user %s; leaving it to the queue backoff", user_id,
                    )
                    raise
                if not self._transient(exc):
                    if self._blames_credential(exc):
                        self._note_bad_credential(user_id, exc)
                    else:
                        # Something else broke -- our own code, an unmapped error
                        # from an adapter, a host we refuse to call. Logged and
                        # left to the queue's ordinary backoff; never counted
                        # against the user's key.
                        logging.warning(
                            "model call for user %s failed for a reason that is not the "
                            "credential's fault (%s: %s); leaving it to the queue backoff",
                            user_id, type(exc).__name__, exc,
                        )
                    raise
                if attempt == attempts:
                    raise
                delay = 5 * attempt
                logging.warning(
                    "model attempt %s/%s failed for user %s (%s); retrying in %ss",
                    attempt, attempts, user_id, exc, delay,
                )
                time.sleep(delay)
            else:
                # A real answer is the only proof the credential works, and it is
                # what lets a window-expired account back in after one probe.
                self.db.clear_key_failures(user_id, "model")
                return result
        raise last if last else providers.ProviderError("模型调用失败。")

    def _note_bad_credential(self, user_id: str, exc: Exception) -> None:
        """Record one credential-class failure, and say so when we stop trying.

        Logged at WARNING with the account id and no key material, so an operator
        can explain afterwards why a user's reports stopped.
        """
        state = self.db.record_key_failure(user_id, "model", str(exc))
        if state["open_until"]:
            logging.warning(
                "model credential for user %s failed %s times in a row; pausing generation "
                "until %s and leaving its messages queued. Last error: %s",
                user_id, state["failures"], state["open_until"], exc,
            )


    def mailbox_password(self, mailbox: dict) -> str:
        return self.secrets.decrypt(mailbox["encrypted_password"], context=f"mailbox:{mailbox['user_id']}")

    def connection_key(self, connection: dict) -> str:
        if connection.get("platform"):
            # The pilot's shared credential is read from the environment on
            # demand rather than decrypted, so it never has to exist as ciphertext
            # in a row that backups copy around. Model and search live in different
            # accounts at different vendors, so the kind decides which variable.
            if connection.get("kind") == "search":
                return providers.platform_search_key()
            return providers.platform_model_key()
        return self.secrets.decrypt(connection["encrypted_api_key"], context=f"connection:{connection['user_id']}:{connection['kind']}")

    def model_connection(self, user_id: str) -> Optional[dict]:
        """The model credential to use for this user.

        Their own connection wins whenever they have one; the instance-wide pilot
        key is only a fallback for accounts that never configured a model. That
        order is what the landing page, the privacy policy and the in-app copy
        all promise, so inverting it would make three documents untrue at once.
        """
        own = self.db.get_connection(user_id, "model")
        if own:
            return own
        return providers.platform_model_default()

    def search_connection(self, user_id: str) -> Optional[dict]:
        """The search credential to use for this user, on the same terms.

        Their own wins; the fallback only covers accounts that never configured
        one. Source-checking is optional, and the operator used to hand the key out
        by hand -- so a user who never got it silently lost citations without
        anything saying so.
        """
        own = self.db.get_connection(user_id, "search")
        if own:
            return own
        return providers.platform_search_default()

    def decrypt_report(self, value: str | bytes, user_id: str) -> str:
        # String support allows a controlled migration from early pilot data.
        return self.secrets.decrypt(value, context=f"report:{user_id}") if isinstance(value, bytes) else str(value)

    def encrypt_report(self, value: str, user_id: str) -> bytes:
        return self.secrets.encrypt(value, context=f"report:{user_id}")

    def decrypt_message(self, value: str | bytes, user_id: str) -> str:
        # A skipped mail stores an empty body on purpose; it is never queued, so
        # this is a safety net rather than a normal path.
        if not value:
            return ""
        return self.secrets.decrypt(value, context=f"message:{user_id}") if isinstance(value, bytes) else str(value)

    def poll_mailbox(self, mailbox: dict) -> int:
        password = self.mailbox_password(mailbox)
        uid_validity, messages, highest = mailio.fetch_new_messages(
            mailbox, password, initial_lookback_hours=INITIAL_LOOKBACK_HOURS
        )
        stored = 0
        skipped = 0
        for uid, message in messages:
            sender = message.get("sender_address", "")
            allowed = is_allowed_sender(sender)
            # Mail outside the allowed domains is recorded for audit but must
            # not keep its body: the privacy policy promises that a skipped row
            # stores metadata only, and an unread body sitting in the database
            # would make that promise false. The sender check therefore runs
            # *before* the body is encrypted, so a body we will never analyse
            # never reaches storage at all. The empty body is stored as-is
            # rather than encrypted, because encrypting nothing is meaningless
            # (and SecretBox refuses an empty plaintext); a skipped row is never
            # queued, so nothing ever tries to decrypt it.
            body = message["body"] if allowed else b""
            protected = {**message, "body": self.secrets.encrypt(body, context=f"message:{mailbox['user_id']}")
                         if allowed else b""}
            if self.db.insert_message(mailbox["user_id"], mailbox["id"], uid_validity, uid, protected) is None:
                # Same RFC 5322 Message-ID already stored: this is a second copy
                # of one mail (two forwarding rules), so it must not become a
                # second AI report.
                logging.info(
                    "skipping duplicate delivery of %s for mailbox %s (uid %s)",
                    message.get("message_key", "")[:80], mailbox["id"], uid,
                )
                continue
            if not allowed:
                # Stored and marked, never queued: the row is what lets the
                # digest say honestly "N messages were skipped, here is why".
                reason = (
                    f"发件人不在允许名单内（{sender or '未知发件人'}）；"
                    f"只处理：{', '.join(ALLOWED_SENDER_DOMAINS)}"
                )
                self.db.mark_message_skipped_by_uid(mailbox["id"], uid_validity, uid, reason)
                skipped += 1
                logging.info(
                    "skipped non-allowed sender %s (uid %s, mailbox %s, body discarded)",
                    sender, uid, mailbox["id"],
                )
                continue
            stored += 1
        self.db.update_mailbox_poll(mailbox["id"], last_uid=highest, uid_validity=uid_validity)
        if skipped:
            logging.info("mailbox %s: stored %s, skipped %s by sender filter", mailbox["id"], stored, skipped)
        return stored

    def poll_all(self) -> tuple[int, list[str]]:
        """Single-threaded reference path; ``worker.cycle`` is the production one.

        Kept as the simple fallback for diagnostics on a machine where the
        thread pool is not wanted.
        """
        total = 0
        errors: list[str] = []
        for mailbox in self.db.active_mailboxes():
            try:
                total += self.poll_mailbox(mailbox)
            except Exception as exc:
                log_job_failure("mailbox poll", mailbox["id"], exc)
                errors.append(f"{mailbox['id']}: {exc}")
                self.db.update_mailbox_poll(
                    mailbox["id"], last_uid=int(mailbox.get("last_uid") or 0),
                    uid_validity=str(mailbox.get("uid_validity") or ""), error=str(exc),
                )
        return total, errors

    def _analyse(self, user_id: str, message: dict) -> str:
        profile = self.db.get_profile(user_id)
        model = self.model_connection(user_id)
        if not model or not model["enabled"]:
            raise providers.ProviderError("尚未配置可用的模型 API。")
        config = json.loads(model.get("config_json") or "{}")
        search_results: list[dict[str, str]] = []
        native = providers.supports_native_search(model["provider"])
        generated = ""
        prompt = ""
        search_status = ""
        usage: dict[str, int] = {}
        timings: dict[str, float] = {}
        try:
            hint = triage.prompt_hint(message)
        except Exception:
            hint = ""

        if native:
            # This provider searches the web on its own, so the user does not
            # need a second, separately billed search API. A search failure must
            # never abort the summary: we fall through to the external path.
            prompt = prompts.immediate_prompt(
                profile, message, [], "模型内置联网搜索已开启", native_search=True,
                triage_hint=hint if INCLUDE_TRIAGE_HINT else "",
            )
            started = time.monotonic()
            try:
                result = self._generate_with_retry(
                    user_id,
                    provider=model["provider"], model=model["model"], base_url=model["base_url"],
                    api_key=self.connection_key(model), prompt=prompt, config=config,
                    max_output_tokens=REPORT_MAX_TOKENS, native_search=True,
                )
                generated = result.text
                search_results = result.sources
                usage = result.usage
                search_status = "模型内置联网搜索已提供来源" if search_results else "模型内置联网搜索未返回可引用来源"
            except Exception as exc:
                logging.warning("native search failed for user %s: %s", user_id, exc)
                native = False  # degrade to the user's own search API, if any
            timings["generate"] = time.monotonic() - started

        if not native:
            search_status = "no search connection configured"
            search = self.search_connection(user_id)
            query = prompts.public_search_query(message)
            if search and search["enabled"] and query:
                started = time.monotonic()
                try:
                    search_results = providers.web_search(search["provider"], self.connection_key(search), query, count=5)
                    search_status = "live results supplied" if search_results else "provider returned no results"
                except Exception as exc:
                    # Search must never block the mail summary.
                    logging.warning("search failed for user %s: %s", user_id, exc)
                    search_status = "live search failed; no verification available"
                timings["search"] = time.monotonic() - started
            elif not query:
                search_status = "no privacy-safe public query could be derived"
            prompt = prompts.immediate_prompt(
                profile, message, search_results, search_status,
                triage_hint=hint if INCLUDE_TRIAGE_HINT else "",
            )
            started = time.monotonic()
            result = self._generate_with_retry(
                user_id,
                provider=model["provider"], model=model["model"], base_url=model["base_url"],
                api_key=self.connection_key(model), prompt=prompt, config=config,
                max_output_tokens=REPORT_MAX_TOKENS, native_search=False,
            )
            generated = result.text
            usage = result.usage
            timings["generate"] = time.monotonic() - started

        # One line that makes the 5-minute question answerable from journalctl:
        # how long search took, how long generation took, and how many tokens.
        logging.info(
            "analysis for user %s used %s search with %d source(s); search=%.1fs generate=%.1fs "
            "prompt=%d chars answer=%d chars tokens=%s",
            user_id, "native" if native else "external", len(search_results),
            timings.get("search", 0.0), timings.get("generate", 0.0),
            len(prompt), len(generated), usage or "n/a",
        )
        self._record_usage(user_id, "immediate", model, usage, message_id=str(message.get("id") or ""))
        generated = prompts.sanitize_calendar_dates(generated, prompt)
        return prompts.normalize_report(generated, allowed_source_urls={item["url"] for item in search_results})

    def send_announcement_emails(self, limit: int = 20) -> dict[str, Any]:
        """Deliver queued broadcast emails, one per user, through their own mailbox.

        Each user's mailbox credentials are the only SMTP the system has, so a
        broadcast goes out the same way reports do — from the user's own mailbox
        to the address they already read. Failures are recorded per user and
        never stop the rest: one broken mailbox must not silence the broadcast
        for everybody else.
        """
        sent = failed = 0
        rows = self.db.pending_announcement_deliveries(limit)
        for row in rows:
            try:
                password = self.secrets.decrypt(row["encrypted_password"],
                                                context=f"mailbox:{row['user_id']}")
                # 配图（如果有）内嵌在这一封信里，Content-ID 用公告 id 派生 —— 同一封
                # 广播发给每个人时它都一样，但**不能**用固定字符串：一封带图的信躺在
                # 收件箱里、另一封也用它，某些客户端会把两张图串起来（cid 是全局的）。
                image = self.db.announcement_image(row["announcement_id"])
                # 短一点：`Content-ID` 头一行装得下就不会被折行（折行是合法的，但
                # 少一个让客户端去「先展开再比对」的机会）。
                image_cid = f"bcast-{row['announcement_id']}@pilot" if image else ""
                mailio.send_report(
                    row, password,
                    reports.announcement_subject(row["title"]),
                    "",
                    html_body=reports.render_announcement_html(
                        row["title"], row["body"], row["tone"], image_cid=image_cid),
                    text_body=reports.render_announcement_text(
                        row["title"], row["body"], row["tone"], has_image=bool(image)),
                    inline_image=(
                        (image["bytes"], str(image["media_type"]).split("/")[-1], image_cid)
                        if image else None),
                )
            except Exception as exc:
                failed += 1
                logging.warning("announcement %s could not be mailed to %s: %s",
                                row["announcement_id"], row["user_id"], exc)
                self.db.finish_announcement_delivery(row["announcement_id"], row["user_id"], error=str(exc))
            else:
                sent += 1
                self.db.finish_announcement_delivery(row["announcement_id"], row["user_id"])
        return {"sent": sent, "failed": failed, "queued": len(rows)}

    def _record_usage(self, user_id: str, kind: str, connection: dict[str, Any] | None,
                      usage: dict[str, Any] | None, *, message_id: str = "") -> None:
        """Log one model call's token use and what it cost.

        Never raises: token accounting is bookkeeping, and a bookkeeping failure
        must not lose a report the user is waiting for.
        """
        try:
            if not usage:
                return
            provider = str((connection or {}).get("provider") or "")
            # Record the name we actually send, not the name the user typed: an
            # alias mapped forward at the provider boundary must not leave the
            # usage row disagreeing with the request it describes.
            model = providers.official_model_name(
                provider, str((connection or {}).get("model") or ""))
            overrides = {(row["provider"].lower(), row["model"]): row
                         for row in self.db.list_model_prices()}
            price = pricing.lookup(provider, model, overrides)
            cost = pricing.estimate(usage, price)
            self.db.record_usage(user_id=user_id, kind=kind, provider=provider, model=model,
                                 usage=usage, cost=cost, price=price, message_id=message_id,
                                 # Captured now, not derived later: "did this
                                 # account ride the pilot key" is a fact about the
                                 # moment of the call, and a user adding their own
                                 # key tomorrow must not re-attribute today's rows
                                 # to them on the page that tells them what they owe.
                                 on_platform=bool((connection or {}).get("platform")))
        except Exception:
            logging.exception("could not record token usage for user %s", user_id)

    def _send_arrival_alert(self, mailbox: dict, password: str, message: dict) -> bool:
        """Send the instant heads-up, before the slow report is generated.

        Never raises: an alert that fails must not stop the report, and a sender
        outside the allow-list must not produce an alert either.
        """
        if not ALERT_ON_ARRIVAL:
            return False
        if not is_allowed_sender(message.get("sender_address", "")):
            return False
        try:
            body = self.decrypt_message(message.get("body", ""), mailbox["user_id"])
            alert = alerts.build_alert({**message, "body": body}, mailbox_email=mailbox.get("email", ""))
            from . import triage as _triage
            if not alerts.should_alert(_triage.triage({**message, "body": body}), urgent_only=ALERT_URGENT_ONLY):
                return False
            mailio.send_report(mailbox, password, alert["subject"], alert["text"],
                               html_body=alert["html"], text_body=alert["text"])
            logging.info(
                "arrival alert sent for user %s (category=%s urgent=%s)",
                mailbox["user_id"], alert["category"], alert["urgent"],
            )
            return True
        except Exception as exc:
            logging.warning("arrival alert failed for message %s: %s", message.get("id"), exc)
            return False

    def _analyse_brief(self, user_id: str, message: dict) -> str:
        """Condensed three-section report: importance, actions, key points.

        Deliberately reuses the same provider plumbing and search fallback as
        the full analysis, so switching modes cannot change *which* provider or
        search is used — only how much the model is asked to write.
        """
        profile = self.db.get_profile(user_id)
        model = self.model_connection(user_id)
        if not model or not model["enabled"]:
            raise providers.ProviderError("尚未配置可用的模型 API。")
        config = json.loads(model.get("config_json") or "{}")
        try:
            hint = triage.prompt_hint(message) if INCLUDE_TRIAGE_HINT else ""
        except Exception:
            hint = ""
        search_results: list[dict[str, str]] = []
        usage: dict[str, Any] = {}
        if providers.supports_native_search(model["provider"]):
            prompt = prompts.brief_prompt(profile, message, [], "模型内置联网搜索已开启",
                                          native_search=True, triage_hint=hint)
            result = self._generate_with_retry(
                user_id, provider=model["provider"], model=model["model"], base_url=model["base_url"],
                api_key=self.connection_key(model), prompt=prompt, config=config,
                max_output_tokens=BRIEF_MAX_TOKENS, native_search=True,
            )
            generated, search_results = result.text, result.sources
            usage = result.usage or {}
        else:
            search = self.search_connection(user_id)
            query = prompts.public_search_query(message)
            search_status = "no search connection configured"
            if search and search["enabled"] and query:
                try:
                    search_results = providers.web_search(
                        search["provider"], self.connection_key(search), query, count=3)
                    search_status = "live results supplied" if search_results else "provider returned no results"
                except Exception as exc:
                    logging.warning("search failed for user %s: %s", user_id, exc)
                    search_status = "live search failed; no verification available"
            elif not query:
                search_status = "no privacy-safe public query could be derived"
            prompt = prompts.brief_prompt(profile, message, search_results, search_status, triage_hint=hint)
            brief = self._generate_with_retry(
                user_id, provider=model["provider"], model=model["model"], base_url=model["base_url"],
                api_key=self.connection_key(model), prompt=prompt, config=config,
                max_output_tokens=BRIEF_MAX_TOKENS, native_search=False,
            )
            generated = brief.text
            usage = brief.usage or {}
        self._record_usage(user_id, "brief", model, usage, message_id=str(message.get("id") or ""))
        logging.info("brief analysis for user %s produced %d chars", user_id, len(generated or ""))
        return prompts.normalize_brief_report(prompts.sanitize_calendar_dates(generated, prompt))

    def process_message(self, message: dict) -> bool:
        if not self.db.mark_message_processing(message["id"]):
            return False
        try:
            mailbox = self.db.get_mailbox(message["user_id"])
            if not mailbox:
                raise mailio.MailError("邮箱配置已不存在。")
            profile = self.db.get_profile(message["user_id"])
            # 这个账号要不要收我们的邮件（一处定义：`Database.report_delivery`）。
            # 关掉的是**投递**，不是处理：报告照生成、待办照出现、看原信与翻译总结照用，
            # 只是不往他邮箱里发东西，收尾记 `held` 而不是 `sent`（没发就是没发）。
            deliver = self.db.report_delivery(message["user_id"]).get("immediate", True)
            existing = self.db.report_for_message(message["id"])
            if existing and existing["status"] == "sent":
                self.db.finish_message(message["id"])
                return True
            if existing and not deliver:
                # 上一轮可能是在开关打开时生成的、还没发出去就走了。现在关掉了，
                # 直接收尾成 held，不必再花一次模型钱。
                self.db.hold_message(message["id"])
                return True
            brief_mode = False
            # The user's own choice, if they made one. '' means "follow the
            # instance" and leaves the env-driven behaviour below untouched --
            # including the two-stage mode, which nobody reaches by accident
            # because it is deliberately NOT offered as a per-user option
            # (two emails per mail is noise, not a preference).
            chosen = str((profile or {}).get("report_mode") or "").strip()
            want_brief = chosen == "brief" or (not chosen and BRIEF_FIRST)
            want_full = chosen == "full" or (not chosen and FULL_REPORT)
            if existing:
                report = self.decrypt_report(existing["body_markdown"], message["user_id"])
                subject, report_id = existing["subject"], existing["id"]
                brief_mode = reports.is_brief(report)
            else:
                payload = {
                    "subject": message["subject"], "sender_name": message["sender_name"],
                    "sender_address": message["sender_address"], "received": message["received_at"],
                    "importance": message["importance"], "body": self.decrypt_message(message["body"], message["user_id"]),
                }
                if want_brief:
                    # Stage 1 exists so the FIRST message the user sees already
                    # carries the essentials. In two-stage mode it is sent here
                    # and then replaced by the full report; in brief-only mode it
                    # becomes the report itself, so this path must not also fall
                    # through to the common send below (that sent it twice).
                    brief = self._analyse_brief(message["user_id"], payload)
                    report = brief
                    brief_mode = True
                    if want_full:
                        brief_subject = f"【AI邮件摘要·精简】{message['subject'][:110]}"
                        brief_rendered = reports.render_brief(
                            brief, message, subject=brief_subject,
                            timezone=(profile or {}).get("timezone"),
                        )
                        try:
                            if deliver:
                                mailio.send_report(mailbox, self.mailbox_password(mailbox), brief_subject, brief,
                                                   html_body=brief_rendered["html"], text_body=brief_rendered["text"])
                                logging.info("brief report sent for message %s", message["id"])
                            else:
                                logging.info("brief report held for message %s (报告邮件已关闭)", message["id"])
                        except Exception as exc:
                            # A failed brief send must not stop the full report.
                            logging.warning("brief report failed for message %s: %s", message["id"], exc)
                        # Stage 2: the full seven-section analysis, sent below.
                        report = self._analyse(message["user_id"], payload)
                        brief_mode = False
                else:
                    # Single-stage: instant rule alert, then the full report.
                    # The alert is mail like any other, so the switch covers it.
                    if deliver:
                        self._send_arrival_alert(mailbox, self.mailbox_password(mailbox), message)
                    report = self._analyse(message["user_id"], payload)
                subject = f"【AI邮件摘要】{message['subject'][:120]}"
                report_id = self.db.create_report(
                    user_id=message["user_id"], message_id=message["id"], kind="immediate",
                    subject=subject, body=self.encrypt_report(report, message["user_id"]), sent_to=mailbox["report_to"],
                )
            if brief_mode:
                rendered = reports.render_brief(report, message, subject=subject,
                                                timezone=(profile or {}).get("timezone"))
            else:
                rendered = reports.render_immediate(report, message, subject=subject,
                                                    timezone=(profile or {}).get("timezone"))
            if not deliver:
                # 报告已经生成（App 里的待办、按天回看、看原信都要用它），
                # 只是按主人的选择不发邮件。收尾记 held —— 不是 sent，也不是 failed。
                self.db.hold_message(message["id"])
                logging.info("report held for message %s (报告邮件已关闭)", message["id"])
                return True
            password = self.mailbox_password(mailbox)
            mailio.send_report(mailbox, password, subject, report,
                               html_body=rendered["html"], text_body=rendered["text"])
            self.db.mark_report_sent(report_id)
            self.db.finish_message(message["id"])
            return True
        except Exception as exc:
            log_job_failure("message processing", message["id"], exc)
            attempts = int(message.get("attempts") or 0) + 1
            retry_seconds = min(3600, 60 * (2 ** min(attempts, 6)))
            retry_at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=retry_seconds)).isoformat(timespec="seconds")
            self.db.fail_message(message["id"], str(exc), retry_at)
            existing = self.db.report_for_message(message["id"])
            if existing:
                self.db.fail_report(existing["id"], str(exc))
            return False

    def process_due(self, limit: int = 20) -> tuple[int, int]:
        """Single-threaded reference path.

        The worker groups the queue by user and runs those groups in parallel
        (see ``pilot_app.worker.process_due``); this stays as the simple,
        sequential version used by manual tooling and as a readable statement of
        what the parallel version is supposed to do.

        Suspended accounts are skipped here too, for the same reason as in the
        worker: the window expiring is meant to buy exactly one probe, so a
        message whose account was suspended by the probe that just failed must
        not be attempted as well.
        """
        succeeded = failed = 0
        blocked: dict[str, bool] = {}
        for message in self.db.due_messages(limit):
            user_id = str(message.get("user_id") or "")
            # `is True` for the same reason as in the worker: an unanswered check
            # means "try it", not "skip this person's mail".
            if blocked.get(user_id) or self.db.key_circuit_open(user_id) is True:
                blocked[user_id] = True
                continue
            if self.process_message(message): succeeded += 1
            else: failed += 1
        return succeeded, failed

    def daily_due(self, user: dict, now_utc: dt.datetime | None = None) -> tuple[bool, str]:
        now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
        try:
            local = now_utc.astimezone(ZoneInfo(user["timezone"]))
        except ZoneInfoNotFoundError:
            local = now_utc.astimezone(ZoneInfo("Asia/Hong_Kong"))
        try:
            hour, minute = [int(item) for item in user["daily_time"].split(":", 1)]
        except (ValueError, AttributeError):
            hour, minute = 22, 0
        report_date = local.date().isoformat()
        return (local.hour, local.minute) >= (hour, minute) and not self.db.daily_report_exists(user["id"], report_date), report_date

    def send_daily(self, user: dict, report_date: str) -> bool:
        """Build and send the student-brief digest for one local day.

        The digest is composed locally from the stored immediate reports instead
        of asking a model to re-summarise them. That guarantees two product
        promises: no email can be silently dropped by a model, and every row
        keeps a traceable sender, subject, received time and source URLs.
        """
        profile = self.db.get_profile(user["id"]) or {}
        try:
            zone = ZoneInfo(user["timezone"])
        except ZoneInfoNotFoundError:
            zone = ZoneInfo("Asia/Hong_Kong")
        local_start = dt.datetime.fromisoformat(report_date).replace(tzinfo=zone)
        start_utc = local_start.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
        end_utc = (local_start + dt.timedelta(days=1)).astimezone(dt.timezone.utc).isoformat(timespec="seconds")

        rows = self.db.messages_between(user["id"], start_utc, end_utc)
        reports_by_id = {}
        messages = []
        for row in rows:
            message_id = row.get("message_id") or row.get("id")
            if not message_id:
                continue
            messages.append(row)
            if row.get("body_markdown") is not None:
                reports_by_id[message_id] = self.decrypt_report(row["body_markdown"], user["id"])
        digest = reports.build_digest(messages, reports_by_id, timezone=user["timezone"])
        reports.with_digest_header(
            digest, report_date, dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        )
        # The optional synthesis. It is attached to the digest, never merged into
        # it: the list below stays the report, and a model that is unavailable,
        # slow or wrong leaves the digest exactly as it is today.
        if digest_synthesis.enabled(self.db):
            text, usage, connection = digest_synthesis.synthesize(self, user, digest)
            if text:
                digest_synthesis.attach(digest, text)
                self._record_usage(user["id"], "digest", connection, usage)

        existing = self.db.daily_report_for_date(user["id"], report_date)
        if existing:
            subject, report_id = existing["subject"], existing["id"]
        else:
            subject = reports.digest_subject(digest)
            report_id = self.db.create_report(
                user_id=user["id"], message_id=None, kind="daily", subject=subject,
                body=self.encrypt_report(reports.digest_markdown(digest), user["id"]),
                sent_to=user["report_to"], report_date=report_date,
            )
        mailbox = self.db.get_mailbox(user["id"])
        if not mailbox:
            raise mailio.MailError("邮箱配置不存在。")
        rendered = reports.render_digest(digest, subject=subject)
        try:
            mailio.send_report(mailbox, self.mailbox_password(mailbox), subject,
                               reports.digest_markdown(digest),
                               html_body=rendered["html"], text_body=rendered["text"])
            self.db.mark_report_sent(report_id)
            return True
        except Exception as exc:
            self.db.fail_report(report_id, str(exc))
            raise

    def run_daily_due(self) -> tuple[int, list[str]]:
        """Send the digests that are due this pass -- with a retry budget per day.

        The budget is the point (see ``DIGEST_RETRY_BACKOFF``): without it a digest
        that cannot be delivered is retried every 15 seconds until midnight.
        """
        sent = 0
        errors: list[str] = []
        now = time.monotonic()
        for user in self.db.daily_users():
            due, report_date = self.daily_due(user)
            if not due:
                continue
            key = (user["id"], report_date)
            attempts, ready_at = self._digest_retry.get(key, (0, 0.0))
            if attempts > len(DIGEST_RETRY_BACKOFF) or now < ready_at:
                continue          # 今天已经试够了，或者还在退避窗口里
            try:
                sent += int(self.send_daily(user, report_date))
            except Exception as exc:
                # 与其它任务同一个口径：账号自己的设置不对是一行 WARNING，
                # 想不到的才留堆栈（`log_job_failure` 是这条规则的唯一定义）。
                log_job_failure("daily report", user["id"], exc)
                errors.append(f"{user['id']}: {exc}")
                delay = DIGEST_RETRY_BACKOFF[min(attempts, len(DIGEST_RETRY_BACKOFF) - 1)]
                self._digest_retry[key] = (attempts + 1, now + delay)
                self._prune_digest_retries(report_date)
            else:
                self._digest_retry.pop(key, None)
        return sent, errors

    def _prune_digest_retries(self, today: str) -> None:
        """Drop entries from older days so the map cannot grow without bound."""
        try:
            cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=2)).isoformat()
        except ValueError:
            return
        for key in [key for key in self._digest_retry if key[1] < cutoff]:
            self._digest_retry.pop(key, None)

    def test_model(self, user_id: str) -> str:
        connection = self.model_connection(user_id)
        if not connection:
            raise providers.ProviderError("尚未配置模型 API。")
        text = providers.generate_text(
            provider=connection["provider"], model=connection["model"], base_url=connection["base_url"],
            api_key=self.connection_key(connection), prompt="只回复：连接成功 / Connection successful",
            config=json.loads(connection.get("config_json") or "{}"), max_output_tokens=200,
        )
        # A provider that answers every real prompt with an empty string used to
        # pass this test, which is how a reasoning model that consumed its whole
        # budget on hidden thinking went unnoticed until a real report came out
        # empty. An empty answer is not a working connection.
        if not str(text or "").strip():
            raise providers.ProviderError("模型连接成功，但没有返回任何文本；请换一个模型名再试。")
        return text

    def test_search(self, user_id: str) -> list[dict[str, str]]:
        connection = self.search_connection(user_id)
        if not connection:
            raise providers.ProviderError("尚未配置搜索 API。")
        return providers.web_search(connection["provider"], self.connection_key(connection), "City University of Hong Kong", count=3)

    def test_mailbox(self, user_id: str) -> dict[str, str]:
        mailbox = self.db.get_mailbox(user_id)
        if not mailbox:
            raise mailio.MailError("尚未配置邮箱。")
        # Read-only search tests IMAP. SMTP is tested with an explicit report in
        # the UI, avoiding an unexpected outbound email from a connection test.
        validity, _, _ = mailio.fetch_new_messages({**mailbox, "last_uid": 2**31 - 1}, self.mailbox_password(mailbox))
        return {"imap": "ok", "uid_validity": validity}

    def read_original(self, user_id: str, message_id: str) -> dict[str, Any]:
        """Read one original mail back from the mailbox, read-only, on demand.

        The body was wiped when the report went out (the privacy policy says so,
        and ``Database.finish_message`` is where it happens), so this cannot be
        answered from our own tables — it goes back to the mailbox for that one
        message and keeps nothing.

        **This method is the one place that could quietly make the privacy
        promise untrue.** Nothing here may write: no cache, no copy in the row,
        no body in a log line. If a future change wants to "speed this up by
        caching it", that is a privacy decision, not an optimisation.
        """
        row = self.db.message_for_user(user_id, message_id)
        if not row:
            raise KeyError(message_id)
        config = {"imap_host": row["imap_host"], "imap_port": row["imap_port"], "email": row["mailbox_email"]}
        return mailio.fetch_message_by_uid(config, self.mailbox_password(row), int(row["imap_uid"]),
                                           uid_validity=row.get("uid_validity") or "")

    ASSIST_KINDS = ("translate", "summary")

    # 中文（含中日韩标点之外的字）在整段文字里的占比。用来判断「模型到底说中文了没有」。
    _CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

    @classmethod
    def _cjk_ratio(cls, text: str) -> float:
        value = str(text or "")
        return len(cls._CJK.findall(value)) / len(value) if value else 0.0

    @classmethod
    def _assist_unanswered(cls, body: str, answer: str) -> bool:
        """模型是不是**没回答这个任务**（把原文抄了回来，或者用英文写了一段）。

        这不是杞人忧天：真机上抓到过整整 5 封里的 2 封——「翻译」原样返回英文原文，
        「总结」返回一段英文摘录。两种情况下界面都会显示得像成功，用户点开一看是英文，
        那就是**用成功的样子骗人**。判据只用两个比值，不猜内容：

        * 原文本身就是中文（占比 ≥ 0.10）时**不判**——抄回来在那种情况下是对的；
        * 其余情况：答案里的中文占比要 ≥ 0.20，或者明显比原文高（+0.10），
          否则算没回答。第二条是给「原文大半是链接/表格」那种信留的余地。
        """
        source_ratio = cls._cjk_ratio(body)
        if source_ratio >= 0.10:
            return False
        answer_ratio = cls._cjk_ratio(answer)
        if answer_ratio >= 0.20:
            return False
        if answer.strip() and answer.strip() != body.strip() and answer_ratio >= source_ratio + 0.10:
            return False
        return True

    @staticmethod
    def _assist_budget(kind: str, body: str) -> int:
        """这一次要给模型多少输出预算（见 ASSIST_MIN_TOKENS 上面那段实测）。"""
        if kind != "translate":
            return ASSIST_MIN_TOKENS
        return max(ASSIST_MIN_TOKENS, min(ASSIST_MAX_TOKENS, len(body)))

    @staticmethod
    def _assist_chunks(body: str, size: int = ASSIST_CHUNK_CHARS) -> list[str]:
        """按空行把正文切成几段（不切断段落），每段约 ``size`` 字。

        这是「整封翻不动」时的最后一招：同一段文字，整封发过去模型会照抄，
        拆成小段它就翻（真机上 2 封顽固的信、13 段全部翻出来了）。
        """
        pieces: list[str] = []
        current = ""
        for block in str(body or "").split("\n\n"):
            block = block.strip("\n")
            if not block:
                continue
            while len(block) > size * 2:            # 单个超长段落只能硬切
                pieces.append(block[:size])
                block = block[size:]
            if current and len(current) + len(block) + 2 > size:
                pieces.append(current)
                current = block
            else:
                current = f"{current}\n\n{block}" if current else block
        if current:
            pieces.append(current)
        return pieces

    def _assist_call(self, user_id: str, message_id: str, model: dict, kind: str, body: str,
                     *, plain: bool = False, budget: int | None = None) -> tuple[str, str, bool]:
        """调一次模型，记一次用量。返回（文本, finish_reason, 是否截断）。"""
        result = self._generate_with_retry(
            user_id,
            provider=model["provider"], model=model["model"], base_url=model["base_url"],
            api_key=self.connection_key(model),
            prompt=prompts.assist_prompt(kind, body, plain=plain),
            config=json.loads(model.get("config_json") or "{}"),
            max_output_tokens=budget or self._assist_budget(kind, body), native_search=False,
        )
        self._record_usage(user_id, f"assist-{kind}", model, result.usage, message_id=message_id)
        return (result.text or "").strip(), str(getattr(result, "finish", "") or ""), getattr(result, "finish", "") == "length"

    def assist(self, user_id: str, message_id: str, kind: str) -> dict[str, Any]:
        """翻译 / 总结**这一封原信**，按需、不保存。

        和「看原信」共用同一条取信路径（`read_original`：只读、核 UIDVALIDITY、不落库），
        多出来的一步是把正文发给模型。这一步**用户点一次才发生一次**，而且它有两重代价：
        钱（走平台 key 时是运营者出）和正文离开我们的服务器（隐私政策里「正文会发给模型
        服务商」那一段同样适用）。所以三条规矩：

        ① **结果不写库**——只回给这一次请求，关掉就没了；正文也不写进任何一行；
        ② 走**同一个**连接选择（用户自己的 key 优先）与**同一个**熔断器，不另开一条通道；
        ③ 用量照记（`_record_usage`），否则「我用了多少」那个面板会开始说假话。

        另外两条是从真机上学的（第一版没有，于是「翻译」把英文原文抄回来还报成功）：

        * **答复要检查**（`_assist_unanswered`）：没翻出来就换一种说法再问一次，
          再不行就分小段翻，都不行就如实说「这次没翻出来」——**绝不把原文当译文递给用户**；
        * **译文被砍了要说**：预算不够时模型会 `finish_reason=length`，界面要写清
          「可能被截断」，而不是让用户以为信就到这里。
        """
        if kind not in self.ASSIST_KINDS:
            raise ValueError("不支持的助手动作。")
        # 取不到就照实把状态（gone/moved）交回给路由，和「看原信」说一样的话。
        fetched = self.read_original(user_id, message_id)
        if fetched.get("state") != "ok":
            return fetched
        body = prompts.assist_body(fetched.get("message") or {})
        model = self.model_connection(user_id)
        if not model:
            raise providers.ProviderError("还没有配置 AI 模型——先在「设置」里选一个，或让管理员配平台 key。")
        where = f'{model["provider"]} / {model["model"]}'

        text, finish, clipped = self._assist_call(user_id, message_id, model, kind, body)
        note = ""
        if self._assist_unanswered(body, text):
            # 第二种说法：不提「邮件」，只当一段文字。实测这一换能把顽固的信翻出来。
            text, finish, clipped = self._assist_call(user_id, message_id, model, kind, body, plain=True)
            if self._assist_unanswered(body, text) and kind == "translate":
                # 最后一招：分段翻再拼起来。整封翻不动时，小段是翻得动的。
                pieces = self._assist_chunks(body)[:ASSIST_MAX_CHUNKS]
                translated, failed = [], 0
                for piece in pieces:
                    part, _, part_clipped = self._assist_call(
                        user_id, message_id, model, kind, piece, plain=True)
                    if self._assist_unanswered(piece, part):
                        failed += 1
                    translated.append(part)
                    clipped = clipped or part_clipped
                if len(pieces) > 1 and failed < len(pieces):
                    text = "\n\n".join(translated)
                    note = "这封信太长，是分段翻译后拼起来的。"
                    if failed:
                        note = f"这封信太长，是分段翻译后拼起来的；有 {failed} 段没翻出来。"
                else:
                    text = ""
            if self._assist_unanswered(body, text):
                # 三种说法都没换来中文。**不把原文当译文**：说清这次没成，原文就在上面。
                return {"state": "unanswered", "kind": kind, "text": "", "model": where,
                        "note": "这次没翻出来：模型把原文抄了回来。可以再点一次，或者直接看上面的原文。"}
        if clipped:
            note = (note + " " if note else "") + "译文可能被截断（模型输出到了上限），再点一次通常能拿全。"
        return {"state": "partial" if note else "ok", "kind": kind, "text": text,
                "model": where, "note": note}
