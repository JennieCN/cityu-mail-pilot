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

from . import alerts, budget, digest_synthesis, mailio, pricing, prompts, providers, reports, tierhealth, triage
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


def _collect_guard(collector: dict[str, Any] | None, generation: Any) -> None:
    """把本机护栏对这次生成的结论收进调用方给的字典里。

    为什么不改返回值：`_analyse` / `_analyse_brief` 的返回值被 `manage verify-e2e` 与
    一堆测试当字符串用，为了一行日志去改签名不划算；而一个**调用方给的字典**是调用内
    局部的（多线程下各写各的，没有共享状态），也不会漏掉「护栏没说话」这件事
    （空字典 = 没结论，与 `ok=true` 是两回事，见 `guard_note`）。
    """
    if collector is None or generation is None:
        return
    verdict = getattr(generation, "guard", None)
    if verdict:
        collector.update(verdict)


def guard_note(guard: dict[str, Any] | None) -> str:
    """护栏结论的一行摘要，只给日志用。

    ``none`` 与 ``ok`` 必须分开：``none`` = **这一次没有护栏结论**（比如用的是平台
    DeepSeek，那条路根本没有护栏），``ok`` = 有护栏而且它说没问题。混成一个字会让
    「护栏是不是在工作」这个问题永远答不出来。
    """
    if not guard:
        return "none"
    issues = guard.get("issues")
    count = len(issues) if isinstance(issues, (list, tuple)) else issues
    state = "ok" if guard.get("ok") else "ESCALATE"
    return (f"{state}(issues={count if count is not None else '?'}"
            f",retried={'yes' if guard.get('retried') else 'no'})")


def log_guard_escalation(guard: dict[str, Any] | None, *, user_id: str, report_id: str = "",
                         message_id: str = "", kind: str = "") -> None:
    """护栏说「这条该看一眼」而**我们照常交付**时，留一行能追到人的记录。

    **为什么要有它**（2026-09-26 用户问「网站侧到底按不按 `guard.ok=false` 分流」）：
    答案是**不按** —— `ok=false` 是业务升级不是错误（见 `providers.Generation.guard`），
    报告照发、照进 App。但在这一行之前，全仓只有 `manage check-model` 打印这个结论，
    于是**运营者事后连「哪个人哪封信被升级过」都查不到**：护栏的日志在**另一台**机器上，
    它只知道「有一批请求被 escalate」，不知道对应的是谁的报告。这一行把两边接上 ——
    `report=` 与 `reports` 行对齐，`user=`/`message=` 追到人和信。

    **它不做别的**：不改交付、不改重试、不写库（所以一次升级不会让报告变成失败）。
    要不要因此改行为是产品决定，见 `docs/guard-escalation-2026-09-26.md`。
    """
    if not guard or guard.get("ok") is not False:
        return
    logging.warning(
        "guard ESCALATE 已交付：report=%s user=%s message=%s kind=%s issues=%s retried=%s"
        " —— 本机护栏说这条要人看一眼，而网站侧**不按它分流**（照常发信/入库）；"
        "改不改这个行为是产品决定，见 docs/guard-escalation-2026-09-26.md",
        report_id or "-", user_id, message_id or "-", kind or "-",
        guard.get("issues"), bool(guard.get("retried")),
    )


class PilotService:
    def __init__(self, database: Database, secrets: SecretBox):
        self.db = database
        self.secrets = secrets
        # (user_id, 简报日期) → (已试次数, 下次可试的 monotonic 时刻)。
        # 存在内存里而不是库里：它只是"别把同一个错误每 15 秒重发一遍"的节流，
        # 进程重启后多试一次无害；写进库反而要多一次迁移与一张会过期的表。
        self._digest_retry: dict[tuple[str, str], tuple[int, float]] = {}

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

    def require_budget_for(self, connection: dict) -> None:
        """要拿**管理员那把 key** 去花钱之前，唯一的一道闸（三条花钱路径都走它）。

        为什么抽出来：这道闸原来只写在 `_generate_with_retry`（出报告那条路）里，
        而「测试模型」按钮与日报综览各自直接调 `providers.generate`——于是**账上没钱时
        它们照样真花一次调用**（2026-06-24 的只读清点指出，`require_available` 全树只有
        一个调用点）。本机那台不过这道闸：它没有账户，问它只会「读不到 → 放行」，
        而它本来也不花钱。

        **故意不过闸的还有两处**（2026-09-24 宿舍机复验时问出来的；边界写死在这里，
        并由 `test_budget_gate_coverage` 按源码清点盯着）：AI 助手走自己的日上限
        （`agent.budget_state`，默认 30 次/天，调用前先读）；三个运维探针
        （`check-model` / `check-localmodel` / `check-search`）各花一次调用——
        它们是「这把 key 还活着吗」的诊断，余额见底时探针失败本身就是那个答案。
        """
        if connection.get("platform") and providers.is_metered(connection):
            budget.require_available(self.db)

    def _generate_with_retry(self, user_id: str, *,
                             attempts: Optional[list[dict[str, Any]]] = None,
                             **kwargs: Any) -> tuple[Any, dict[str, Any]]:
        """Run one generation, walking an ordered list of credentials.

        ``attempts`` 是**凭据候选**（第 0 个是首选），默认只试这一个。每条候选**各试两次**
        （`_attempts_per_credential`），两类失败各修各的：

        * **瞬时失败**（断连、429、5xx）：换的是同一把 key 的下一次尝试——一封信在一次
          「Remote end closed connection」上丢掉是最没必要的损失，中间那 5 秒退避就是为它留的；
        * **这一档不行**（超时用光预算、或上面两次都没成）：换**下一档**凭据……

          ……除了超时。超时已经花掉整个预算（实测一封真报告 234 s / 上限 300 s），再试
          下一条就是让这个生成位被占两倍时间，所以它直接交给队列退避。

        Non-transient failures (bad key, bad model name) are raised immediately.

        This is also the **only** place that decides whether a failure counts
        against the account's credential. That decision has to live in exactly
        one place: `_transient` is already this project's definition of "worth
        retrying", and the circuit breaker it feeds means "stop trying, this
        credential is wrong". If a timeout or a 429 were counted here, a
        provider's bad afternoon would suspend innocent accounts -- which is why
        the counting sits next to the classification instead of at the callers.

        返回 ``(结果, 真正答话的那个连接)``。第二条是新加的：兜底接手之后，
        「这次是谁答的」必须跟着结果走，否则用量记录会把 DeepSeek 的调用记在本机服务名下。
        """
        items = list(attempts or [])
        if not items:
            raise providers.ProviderError("没有可用的模型凭据。")
        per_credential = self._attempts_per_credential()
        last: Exception | None = None
        for index, connection in enumerate(items):
            call = dict(kwargs)
            call.update(provider=connection["provider"], model=connection["model"],
                        base_url=connection.get("base_url") or "",
                        api_key=self.connection_key(connection))
            self.require_budget_for(connection)
            for attempt in range(1, per_credential + 1):
                try:
                    result = providers.generate(**call)
                except Exception as exc:
                    last = exc
                    if isinstance(exc, providers.ProviderTimeout):
                        # 超时已经花掉这一档的整个预算（实测一封真报告 234 s / 上限 300 s），
                        # 所以**不重试同一档**：那会让这个生成位被占两倍时间。
                        #
                        # 但「还有下一档吗」决定了接下来是「换人」还是「放弃」——
                        # 这两件事在 2026-09-23 之前是一样的（一律 raise），对本机那台
                        # 主服务来说那是错的：它一卡，报告就整封失败去排队退避，
                        # 而**兜底那把 key 就在手边、用户本来可以无感**。
                        # 用户自己的 key 超时仍然不换档：那会让管理员替他的坏 key 付钱。
                        if index + 1 < len(items) and providers.is_local(connection):
                            logging.warning(
                                "本机主服务在 %ss 内没有答话（user %s）——直接换兜底那档",
                                providers.request_timeout(connection.get("provider")), user_id,
                            )
                            tierhealth.note_degraded(self.db, f"ProviderTimeout：{exc}"[:160])
                            break
                        logging.warning(
                            "model timed out for user %s; leaving it to the queue backoff", user_id,
                        )
                        raise
                    if not self._transient(exc):
                        if self._blames_credential(exc) and not connection.get("platform"):
                            # 只有**用户自己的** key 才计失败。平台那两档是运营者配的，
                            # 把它们算到用户头上会让一个无辜账号被熔断（而用户根本改不了它）。
                            self._note_bad_credential(user_id, exc)
                        # **主服务那一档坏掉时要换兜底**，哪怕这个错是「永久」的
                        # （401 key 被轮换、404 路径变了、400 它不认我们的请求体）。
                        # 2026-09-23 之前这里是一个裸 raise，于是「本机那把 key 被轮换」的
                        # 症状是**所有没自带 key 的账号一封报告都出不来**——而兜底那把
                        # 明明就在手边。判据是「这一档是不是我们自己维护的那台」：
                        # 用户自己的 key 坏掉仍然立刻抛（换档等于让管理员替他付钱）。
                        if index + 1 < len(items) and providers.is_local(connection):
                            logging.warning(
                                "本机主服务这一档失败（%s: %s），换兜底那档接手（user %s）",
                                type(exc).__name__, exc, user_id,
                            )
                            # 静默降级 = 悄悄花付费那把的钱，那正是这个部署要避免的事。
                            # 盖一枚章，让哨兵说得出话（`tierhealth.findings`）。
                            tierhealth.note_degraded(
                                self.db, f"{type(exc).__name__}：{exc}"[:160])
                            break
                        if not self._blames_credential(exc) or connection.get("platform"):
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
                    if attempt < per_credential:
                        delay = 5 * attempt
                        logging.warning(
                            "model attempt %s/%s failed for user %s on %s (%s); retrying it in %ss",
                            attempt, per_credential, user_id, connection.get("provider"),
                            type(exc).__name__, delay,
                        )
                        time.sleep(delay)
                        continue
                    if index + 1 >= len(items):
                        raise
                    logging.warning(
                        "model credential %s/%s exhausted for user %s on %s (%s); "
                        "trying the next credential: %s",
                        index + 1, len(items), user_id, connection.get("provider"), type(exc).__name__, exc,
                    )
                    if providers.is_local(connection):
                        # **这一支才是真实场景**（2026-09-23 在生产上验出来的）：
                        # 主服务最常见的失败是「连不上」——隧道断了、那台盒子关机了、
                        # 端口没人听——它们全是**瞬时**失败，走的就是这条 `continue`
                        # 换档的路。第一版只在**非瞬时**那一支盖了章，于是真出事的时候
                        # （隧道断）降级是**静默**的：用户无感、钱在花、面板上什么都不显示。
                        # 判据和另一支相同：只给「我们自己维护的那一台」盖章。
                        tierhealth.note_degraded(self.db, f"{type(exc).__name__}：{exc}"[:160])
                else:
                    # A real answer is the only proof the credential works, and it is
                    # what lets a window-expired account back in after one probe.
                    self.db.clear_key_failures(user_id, "model")
                    if providers.is_local(connection):
                        # 主服务答话了 —— 把「已降级」那枚章清掉，面板上那条提示立刻消失。
                        # 不清的话，一次抖动会在上面挂一整天，人就学会忽略它了。
                        tierhealth.note_success(self.db)
                    return result, connection
        raise last if last else providers.ProviderError("模型调用失败。")

    @staticmethod
    def _attempts_per_credential() -> int:
        """一把 key 允许试几次（默认 2）。与「有几档凭据」是两件事，别混。"""
        try:
            return max(1, min(4, int(os.environ.get("INFE_PILOT_MODEL_ATTEMPTS", "2"))))
        except ValueError:
            return 2

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
            # accounts at different vendors, so the kind decides which variable --
            # and since 2026-09-22 there are **two** model tiers (the local box and
            # the paid fallback), so the connection itself says which one it is.
            if connection.get("kind") == "search":
                return providers.platform_search_key()
            return providers.platform_connection_key(connection)
        return self.secrets.decrypt(connection["encrypted_api_key"], context=f"connection:{connection['user_id']}:{connection['kind']}")

    def model_connection(self, user_id: str) -> Optional[dict]:
        """The model credential to use for this user.

        Their own connection wins whenever they have one; the instance-wide pilot
        key is only a fallback for accounts that never configured a model. That
        order is what the landing page, the privacy policy and the in-app copy
        all promise, so inverting it would make three documents untrue at once.

        **它只回答「首选是哪一个」**，不做钱的闸、也不列出兜底：要出报告就走
        `model_attempts()`（候选表）与 `_generate_with_retry()`（那道闸在真正要花
        管理员那把 key 之前才落下）。这里保留单数形状是给「界面上显示什么」用的。
        """
        return next(iter(self.model_attempts(user_id)), None)

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

    def model_attempts(self, user_id: str) -> list[dict]:
        """这条账号可以依次尝试的模型凭据，**首选在前**。

        2026-09-22 起平台那一侧是两条：本机那台盒子（主）与管理员付费的 key（兜底）。
        顺序写在这里一处，出报告、日报综览、运维助手都读它，所以「谁是主服务」不会
        出现第二份互相矛盾的答案。

        候选**在这里不做预算闸**：`budget.require_available` 挡的是「账上没钱了还去花」，
        而排在前面的本机服务根本不花钱。真正的闸在 `_generate_with_retry` 里、紧挨着
        每一次「要拿管理员 key 去调用」之前——那才是这件事发生的地方。
        """
        own = self.db.get_connection(user_id, "model")
        if own:
            return [own]
        return list(providers.platform_model_connections())

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
            # 过大的邮件：正文根本没取回来（`mailio` 只取了报头），所以它既不该被加密
            # （空明文 `SecretBox` 会拒），也不该进队列——但要**留一行可见的记录**，
            # 让人在日报/后台看得到「这封信因为太大没处理」，而不是静默消失。
            oversized = bool(message.get("oversized"))
            storable = allowed and not oversized
            body = message["body"] if storable else b""
            protected = {**message, "body": self.secrets.encrypt(body, context=f"message:{mailbox['user_id']}")
                         if storable else b""}
            if self.db.insert_message(mailbox["user_id"], mailbox["id"], uid_validity, uid, protected) is None:
                # Same RFC 5322 Message-ID already stored: this is a second copy
                # of one mail (two forwarding rules), so it must not become a
                # second AI report.
                logging.info(
                    "skipping duplicate delivery of %s for mailbox %s (uid %s)",
                    message.get("message_key", "")[:80], mailbox["id"], uid,
                )
                continue
            if not storable:
                # Stored and marked, never queued: the row is what lets the
                # digest say honestly "N messages were skipped, here is why".
                if oversized:
                    reason = (f"邮件过大（{float(message.get('size_bytes') or 0) / 1048576:.1f} MB，"
                              f"上限 {mailio.MAX_MESSAGE_BYTES // 1048576} MB）：没有取回正文，也没有生成报告。"
                              "如果是误转发的大附件，直接删掉那封信即可。")
                else:
                    reason = (
                        f"发件人不在允许名单内（{sender or '未知发件人'}）；"
                        f"只处理：{', '.join(ALLOWED_SENDER_DOMAINS)}"
                    )
                self.db.mark_message_skipped_by_uid(mailbox["id"], uid_validity, uid, reason)
                skipped += 1
                logging.info(
                    "skipped %s (uid %s, mailbox %s, body discarded)",
                    "oversized message" if oversized else f"non-allowed sender {sender}",
                    uid, mailbox["id"],
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

    def _analyse(self, user_id: str, message: dict, *,
                 guard: dict[str, Any] | None = None) -> str:
        """生成一份完整报告。``guard`` 是**调用方给的收集器**（见 `_collect_guard`）。"""
        profile = self.db.get_profile(user_id)
        # 报告正文用哪种语言写（2026-09-23 起与界面语言合并成一个设置）。
        # 默认是中文，所以**存量用户一个字都不变**。
        locale = self.db.report_locale(user_id)
        # 候选**顺序**在这里定：用户自己的 key（有的话）最先，然后是平台那两档。
        # 兜底之所以能接手，靠的就是这个列表被一路带到 `_generate_with_retry`。
        candidates = self.model_attempts(user_id)
        model = candidates[0]
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
                triage_hint=hint if INCLUDE_TRIAGE_HINT else "", locale=locale,
            )
            started = time.monotonic()
            try:
                result, model = self._generate_with_retry(
                    user_id, attempts=candidates, config=config,
                    prompt=prompt, max_output_tokens=REPORT_MAX_TOKENS,
                    native_search=True, guard_task="summarize",
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
                    # 搜索是**按次**计费、与模型同一个账号，以前一行都不记（见该方法）。
                    self._record_search_usage(user_id, search, message_id=str(message.get("id") or ""))
                except Exception as exc:
                    # Search must never block the mail summary.
                    logging.warning("search failed for user %s: %s", user_id, exc)
                    search_status = "live search failed; no verification available"
                timings["search"] = time.monotonic() - started
            elif not query:
                search_status = "no privacy-safe public query could be derived"
            prompt = prompts.immediate_prompt(
                profile, message, search_results, search_status,
                triage_hint=hint if INCLUDE_TRIAGE_HINT else "", locale=locale,
            )
            started = time.monotonic()
            result, model = self._generate_with_retry(
                user_id, attempts=candidates, config=config,
                prompt=prompt, max_output_tokens=REPORT_MAX_TOKENS,
                native_search=False, guard_task="summarize",
            )
            generated = result.text
            usage = result.usage
            timings["generate"] = time.monotonic() - started

        _collect_guard(guard, result)
        # One line that makes the 5-minute question answerable from journalctl:
        # how long search took, how long generation took, and how many tokens.
        logging.info(
            "analysis for user %s used %s search with %d source(s); search=%.1fs generate=%.1fs "
            "prompt=%d chars answer=%d chars tokens=%s guard=%s",
            user_id, "native" if native else "external", len(search_results),
            timings.get("search", 0.0), timings.get("generate", 0.0),
            len(prompt), len(generated), usage or "n/a", guard_note(guard),
        )
        self._record_usage(user_id, "immediate", model, usage, message_id=str(message.get("id") or ""))
        generated = prompts.sanitize_calendar_dates(generated, prompt)
        return prompts.normalize_report(generated, allowed_source_urls={item["url"] for item in search_results},
                                        locale=locale)

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

        但**参数形状要在这里说清楚**（2026-09-23 补）：`message_id` 只接受字符串，
        传进来别的东西（比如整条消息的 dict）会被 SQLite 以 `InterfaceError` 拒掉——
        而上面那个 `except` 会把它吞成一行日志，于是**这一次调用在用量表里消失**，
        「我用了多少 / 谁付的」那张表开始少算。参考调用方是 `_analyse` 与 `_assist_call`，
        它们手上既有消息也有 id，很容易拿错。
        """
        try:
            if not usage:
                return
            if message_id and not isinstance(message_id, str):
                # 形状错误在这里就报出来（带类型），别等到 SQLite 的
                # 「Error binding parameter 2」——那句话说不清是谁传错了什么。
                raise TypeError(f"message_id 必须是字符串，收到 {type(message_id).__name__}")
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

    def _record_search_usage(self, user_id: str, connection: dict[str, Any] | None, *,
                             message_id: str = "") -> None:
        """把**搜索调用**也记一行 —— 在此之前它完全不在账本里。

        2026-09-26：用户问「平台 key 的余额为什么掉得比我们记的账快 45 倍」。查下来
        账本（`token_usage`）里只有模型调用，而**每次出报告都会调一次豆包联网搜索**
        （`providers.web_search`，与模型同一个火山账号计费）—— 这一类调用从来没写过行。
        运维助手（`agent`）那 30 次/天的额度同样没记。于是「钱花在哪」只能靠猜。

        **不编单价**：搜索是**按次**计费，而我们手上没有一份可核实的价目表，所以
        `cost` 记 NULL —— 用量页把 NULL 显示成「未计价」，那是诚实的，填个 0 才是撒谎。
        这一行首先提供的是**次数**：有了它，余额下降第一次能对着账本解释。

        也**不记查询词**：那个词是从用户邮件里推出来的（`prompts.public_search_query`），
        隐私政策只承诺把元数据留在我们这边。
        """
        try:
            self.db.record_usage(
                user_id=user_id, kind="search",
                provider=str((connection or {}).get("provider") or ""),
                model=str((connection or {}).get("model") or "web-search"),
                usage={"total": 0}, cost=None, message_id=message_id,
                on_platform=bool((connection or {}).get("platform")),
            )
        except Exception:
            logging.exception("could not record search usage for user %s", user_id)

    def _send_arrival_alert(self, mailbox: dict, password: str, message: dict, *,
                           full_follows: bool = True) -> bool:
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
            alert = alerts.build_alert({**message, "body": body},
                                       mailbox_email=mailbox.get("email", ""),
                                       full_follows=full_follows)
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

    def _analyse_brief(self, user_id: str, message: dict, *,
                       guard: dict[str, Any] | None = None) -> str:
        """Condensed three-section report: importance, actions, key points.

        Deliberately reuses the same provider plumbing and search fallback as
        the full analysis, so switching modes cannot change *which* provider or
        search is used — only how much the model is asked to write.
        """
        # 精简版与完整版用同一种语言（同一个账号设置），默认中文。
        locale = self.db.report_locale(user_id)
        profile = self.db.get_profile(user_id)
        candidates = self.model_attempts(user_id)
        model = candidates[0]
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
                                          native_search=True, triage_hint=hint, locale=locale)
            result, model = self._generate_with_retry(
                user_id, attempts=candidates, prompt=prompt, config=config,
                max_output_tokens=BRIEF_MAX_TOKENS, native_search=True, guard_task="summarize",
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
                    self._record_search_usage(user_id, search, message_id=str(message.get("id") or ""))
                except Exception as exc:
                    logging.warning("search failed for user %s: %s", user_id, exc)
                    search_status = "live search failed; no verification available"
            elif not query:
                search_status = "no privacy-safe public query could be derived"
            prompt = prompts.brief_prompt(profile, message, search_results, search_status,
                                          triage_hint=hint, locale=locale)
            result, model = self._generate_with_retry(
                user_id, attempts=candidates, prompt=prompt, config=config,
                max_output_tokens=BRIEF_MAX_TOKENS, native_search=False, guard_task="summarize",
            )
            generated = result.text
            usage = result.usage or {}
        _collect_guard(guard, result)
        self._record_usage(user_id, "brief", model, usage, message_id=str(message.get("id") or ""))
        logging.info("brief analysis for user %s produced %d chars guard=%s",
                     user_id, len(generated or ""), guard_note(guard))
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
                # 本机护栏对这次生成的结论（`ok=false` = 业务升级）。**只用来记日志**：
                # 交付行为一个字节都不改，见 `_collect_guard` 与 `guard_note`。
                guard: dict[str, Any] = {}
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
                    brief = self._analyse_brief(message["user_id"], payload, guard=guard)
                    report = brief
                    brief_mode = True
                    if want_full:
                        brief_subject = f"【AI邮件摘要·精简】{message['subject'][:110]}"
                        brief_rendered = reports.render_brief(
                            brief, message, subject=brief_subject,
                            timezone=(profile or {}).get("timezone"),
                            # 两段式里完整版随后就来 —— 这句话是真的。
                            full_follows=True,
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
                        report = self._analyse(message["user_id"], payload, guard=guard)
                        brief_mode = False
                else:
                    # Single-stage: instant rule alert, then the full report.
                    # The alert is mail like any other, so the switch covers it.
                    if deliver:
                        self._send_arrival_alert(mailbox, self.mailbox_password(mailbox), message,
                                                 full_follows=want_full)
                    report = self._analyse(message["user_id"], payload, guard=guard)
                subject = f"【AI邮件摘要】{message['subject'][:120]}"
                report_id = self.db.create_report(
                    user_id=message["user_id"], message_id=message["id"], kind="immediate",
                    subject=subject, body=self.encrypt_report(report, message["user_id"]), sent_to=mailbox["report_to"],
                )
                log_guard_escalation(guard, user_id=message["user_id"], report_id=report_id,
                                     message_id=message["id"], kind="brief" if brief_mode else "immediate")
            if brief_mode:
                rendered = reports.render_brief(report, message, subject=subject,
                                                timezone=(profile or {}).get("timezone"),
                                                # 只发精简版时不能说「完整版稍后单独发送」。
                                                full_follows=want_full,
                                                locale=self.db.report_locale(message["user_id"]))
            else:
                rendered = reports.render_immediate(report, message, subject=subject,
                                                    timezone=(profile or {}).get("timezone"),
                                                    locale=self.db.report_locale(message["user_id"]))
            if not deliver:
                # 报告已经生成（App 里的待办、按天回看、看原信都要用它），
                # 只是按主人的选择不发邮件。收尾记 held —— 不是 sent，也不是 failed。
                self.db.hold_message(message["id"])
                logging.info("report held for message %s (报告邮件已关闭)", message["id"])
                return True
            password = self.mailbox_password(mailbox)
            mailio.send_report(mailbox, password, subject, report,
                               html_body=rendered["html"], text_body=rendered["text"],
                               # 由报告 id 推出来的稳定 Message-ID：重试复用同一个。
                               message_id=mailio.stable_message_id(report_id, mailbox.get("email", "")))
            self.db.mark_report_sent(report_id)
            try:
                self.db.finish_message(message["id"])
            except Exception as exc:
                # **信已经发出去了**（SMTP 收下了，报告也记成 sent 了）。这里失败的是收尾
                # （清正文、清重试、置 sent），不是投递——所以：
                # ① 不把它并进下面那个「投递失败」的 except（那会把报告退回 failed，
                #    下一次重试就会**再发一封**，用户收到两封不同的信）；
                # ② 也不报成「处理失败」：返回值是给统计看的，说失败是假话；
                # ③ 错误照记在邮件那一行，但**写清楚是收尾**——否则运维读 `last_error`
                #    会以为这封信没发出去；下一次轮询看到报告已是 sent，只把收尾补完
                #    （见本函数开头那条守卫）。
                log_job_failure("message bookkeeping", message["id"], exc)
                retry_at = self._retry_at(message)
                self.db.fail_message(message["id"], f"报告已发出，但收尾失败：{exc}", retry_at)
                return True
            return True
        except Exception as exc:
            log_job_failure("message processing", message["id"], exc)
            retry_at = self._retry_at(message)
            self.db.fail_message(message["id"], str(exc), retry_at)
            existing = self.db.report_for_message(message["id"])
            if existing:
                self.db.fail_report(existing["id"], str(exc))
            return False

    @staticmethod
    def _retry_at(message: dict) -> str:
        """下一次重试的时刻（指数退避，上限一小时）。写法只此一处。"""
        attempts = int(message.get("attempts") or 0) + 1
        retry_seconds = min(3600, 60 * (2 ** min(attempts, 6)))
        return (dt.datetime.now(dt.timezone.utc)
                + dt.timedelta(seconds=retry_seconds)).isoformat(timespec="seconds")

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
        # 用户选的语言：正文（模型写的）与**固定标签**（我们写的）都跟它走。
        # 一处取值、三份正文（markdown / HTML / 纯文本）共用，三份不可能各说各话。
        locale = self.db.report_locale(user["id"])
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
        # 「稍后提醒」的那些行一起进去：它们只变成确定性清单里的一行，**不会多发一封
        # 邮件**（这一封本来就发），也永远不进模型写的那段综览。
        digest = reports.build_digest(messages, reports_by_id, timezone=user["timezone"],
                                      snoozed=list(self.db.task_states(user["id"]).values()))
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
            subject = reports.digest_subject(digest, locale=locale)
            report_id = self.db.create_report(
                user_id=user["id"], message_id=None, kind="daily", subject=subject,
                body=self.encrypt_report(reports.digest_markdown(digest, locale=locale), user["id"]),
                sent_to=user["report_to"], report_date=report_date,
            )
        mailbox = self.db.get_mailbox(user["id"])
        if not mailbox:
            raise mailio.MailError("邮箱配置不存在。")
        rendered = reports.render_digest(digest, subject=subject, locale=locale)
        try:
            mailio.send_report(mailbox, self.mailbox_password(mailbox), subject,
                               reports.digest_markdown(digest, locale=locale),
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
        # 账上没钱时**连测试也不该真花一次调用**（以前只有出报告那条路有这道闸）。
        self.require_budget_for(connection)
        answer = providers.generate(
            provider=connection["provider"], model=connection["model"], base_url=connection["base_url"],
            api_key=self.connection_key(connection), prompt="只回复：连接成功 / Connection successful",
            config=json.loads(connection.get("config_json") or "{}"), max_output_tokens=200,
        )
        # 记账：这一次点击是**一次真实的模型调用**，走平台兜底 key 时是运营者付的钱。
        # 以前它不记账，于是「我用了多少 / 谁付的」那张表看不见这些调用——而那张表
        # 正是用来回答「这笔钱算谁的」。搜索那侧暂时记不了：`web_search` 拿不到
        # token 用量，与其编一行 0 tokens，不如让它保持沉默（这条写在审查回执里）。
        self._record_usage(user_id, "test-model", connection, answer.usage)
        text = answer.text
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

        **也没有「太大的信也先读回来再说」这条捷径**（GPT 审计第二条 P1）：
        超过 `mailio.MAX_MESSAGE_BYTES` 的邮件在 `mailio` 里就被挡住（正文一个
        字节都不取），这里把它翻成一句用户能照着做的话。**明确拒绝，不静默截断**
        ——半封信显示出来像「信就到这里」，那是用成功的样子骗人。
        """
        row = self.db.message_for_user(user_id, message_id)
        if not row:
            raise KeyError(message_id)
        config = {"imap_host": row["imap_host"], "imap_port": row["imap_port"], "email": row["mailbox_email"]}
        result = mailio.fetch_message_by_uid(config, self.mailbox_password(row), int(row["imap_uid"]),
                                             uid_validity=row.get("uid_validity") or "")
        if result.get("state") == mailio.ORIGINAL_TOO_LARGE:
            limit_mb = int(result.get("limit_bytes") or mailio.MAX_MESSAGE_BYTES) // 1048576
            size = int(result.get("size_bytes") or 0)
            # 尺寸是精确值时说出来；只有下界时（有界 partial 拿满）说「至少」，
            # 不把「我们的上限+1」当成服务器报的大小。
            reported = (f"{size / 1048576:.1f} MB" if result.get("size_exact")
                        else f"至少 {size / 1048576:.0f} MB")
            raise mailio.MailError(
                f"这封邮件太大（{reported}），超过了 {limit_mb} MB 的上限，无法在网页里打开。"
                "请直接在你的邮箱里查看这封信。")
        return result

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
        """调一次模型，记一次用量。返回（文本, finish_reason, 是否截断）。

        这里**只给一条候选**（调用方挑好的那一个）：翻译/总结是用户正等着看结果的一次
        请求，悄悄换一把 key 去答会让他拿到两种不同模型的输出；而这条路上的失败会当场
        以错误回给他，不是「静静地少一封报告」。
        """
        result, model = self._generate_with_retry(
            user_id, attempts=[model],
            prompt=prompts.assist_prompt(kind, body, plain=plain),
            config=json.loads(model.get("config_json") or "{}"),
            max_output_tokens=budget or self._assist_budget(kind, body), native_search=False,
            # **`summarize` 而不是 `reply`**（2026-09-23 改对）。这一步的实质是
            # 「把手上这段文字译成中文 / 概括成要点」，两个 kind 都是（`prompts.assist_prompt`
            # 的指令就那两行）。原来报 `reply` 是照「输出是给人看的一段话」选的，但护栏那四个
            # 任务是按**动作**分的：`reply` 审的是「未授权承诺、索要敏感信息、疑似钓鱼没提示风险、
            # **信息不足没索取订单号**」——拿这几条去审一份译文，最可能的结局是因为最后那条
            # 被判定不合格，于是**每一封信都被重生成一次**（延迟翻倍），而用户只是想要个翻译。
            guard_task="summarize",
        )
        self._record_usage(user_id, f"assist-{kind}", model, result.usage, message_id=message_id)
        # 翻译/总结也是**当场交付给用户**的一段模型输出，所以同样留一行可追溯的记录
        # （没有 report 行 —— 这条路按设计不落库，用 message + kind 定位）。
        log_guard_escalation(getattr(result, "guard", None), user_id=user_id,
                             message_id=message_id, kind=f"assist-{kind}")
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
        # 过大那一档在 `read_original` 里当场抛 `MailError`（同样一句话），
        # 所以**翻译/总结也走同一道体积闸门**，不会把整封信拉进 web 进程。
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
