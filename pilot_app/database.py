"""SQLite storage with explicit per-user ownership on every sensitive record."""

from __future__ import annotations

import base64
import contextlib
import datetime as dt
import json
import logging
import re
import secrets
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from . import mailpresets
from .security import token_hash


# How many *credential-class* generation failures in a row suspend the account,
# and for how long. See `record_key_failure` for why a transient provider error
# must never count towards these numbers.
KEY_CIRCUIT_THRESHOLD = 3
KEY_CIRCUIT_SECONDS = 1800


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','deleted')),
    created_at TEXT NOT NULL,
    -- Operator rights granted from the admin console, on top of the ones the
    -- server's INFE_PILOT_ADMIN_EMAILS names. Kept on the account rather than in
    -- a separate table so that deleting the account deletes the grant with it:
    -- an admin row outliving its user would be a way back in for a deleted
    -- account, and would need a cleanup nobody would remember to run.
    is_admin INTEGER NOT NULL DEFAULT 0,
    -- A private memo the operator keeps about this account ("填错了授权码",
    -- "同学介绍来的", "2026-09 起停用"). It lives on `users` rather than on
    -- `profiles` on purpose: every user-facing read of a profile is a
    -- `SELECT *` (get_profile, export_user_data), so anything added there
    -- rides along into /api/me and the data export -- that is exactly how the
    -- background photo once nearly ended up in the profile JSON. The reads
    -- that touch `users` all name their columns, so a note here cannot reach
    -- the user by accident, and a test drives those endpoints to keep it that way.
    admin_note TEXT NOT NULL DEFAULT '',
    -- When this account last actually used the app (any authenticated request,
    -- written at most once every few minutes -- see `touch_last_seen`).
    --
    -- The question it answers is the operator's, not the user's: 「我发出去的那封
    -- 『你还差一步』他到底看没看到」. The session table cannot answer it -- a row
    -- there is deleted on logout, and two accounts that were reminded on 09-15 had
    -- no session row left at all, so "never saw the letter" and "saw it and did
    -- not finish" were indistinguishable. Same shape as the lesson this project
    -- keeps re-learning: a stamp records what *we* did, not what happened.
    last_seen_at TEXT NOT NULL DEFAULT '',
    -- 界面语言（2026-09-23）。'' = 还没选过，按浏览器 `Accept-Language` 猜；
    -- 否则是 `pilot_app/i18n.py` 里那个语言代码。放在 `users` 而不是 `profiles`：
    -- profiles 每次用户侧读取都是 `SELECT *`，加在那里它会跟着 `/api/me` 与数据
    -- 导出一起走（`admin_note` 上面那段注释讲的就是这件事）。
    ui_locale TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS invites (
    code_hash TEXT PRIMARY KEY,
    label TEXT NOT NULL DEFAULT '',
    expires_at TEXT NOT NULL,
    used_by TEXT REFERENCES users(id) ON DELETE SET NULL,
    used_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS profiles (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    school_email TEXT NOT NULL DEFAULT '',
    major TEXT NOT NULL DEFAULT '',
    year_of_study TEXT NOT NULL DEFAULT '',
    courses_json TEXT NOT NULL DEFAULT '[]',
    interests_json TEXT NOT NULL DEFAULT '[]',
    career_goals_json TEXT NOT NULL DEFAULT '[]',
    focus_topics_json TEXT NOT NULL DEFAULT '[]',
    less_interested_json TEXT NOT NULL DEFAULT '[]',
    custom_instructions TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT 'bilingual',
    timezone TEXT NOT NULL DEFAULT 'Asia/Hong_Kong',
    immediate_enabled INTEGER NOT NULL DEFAULT 1,
    daily_enabled INTEGER NOT NULL DEFAULT 1,
    daily_time TEXT NOT NULL DEFAULT '22:00',
    -- appearance is per user, not per browser, so a theme picked on the phone is
    -- still there on the laptop; '' means "follow the theme's own background"
    theme TEXT NOT NULL DEFAULT 'paper',
    background TEXT NOT NULL DEFAULT '',
    -- '' = follow the instance-wide setting (service.BRIEF_FIRST/FULL_REPORT);
    -- 'brief' / 'full' = this user has chosen, and the choice wins. Deliberately
    -- the same "'' means default" shape as `background`.
    report_mode TEXT NOT NULL DEFAULT '',
    -- 2026-09-23：**注册时填的三栏选填资料**（原来在首页那张申请表上，申请制取消后
    -- 挪到了 `/app` 的注册表单里，见 `docs/open-registration-2026-09-22.md`）。
    -- 为什么放 profiles 而不是 signup_requests：那一边是**历史申请**，新注册不该往里塞行。
    -- 为什么不怕 `PUT /api/profile` 把它们清零（铁律 4）：`upsert_profile` 只写它那份
    -- **允许清单**里的列，这三列**刻意不在清单里**，所以「只改一项」的请求碰不到它们。
    signup_nickname TEXT NOT NULL DEFAULT '',
    signup_identity TEXT NOT NULL DEFAULT '',
    signup_goals TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mailboxes (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    report_to TEXT NOT NULL,
    imap_host TEXT NOT NULL,
    imap_port INTEGER NOT NULL,
    smtp_host TEXT NOT NULL,
    smtp_port INTEGER NOT NULL,
    encrypted_password BLOB NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    uid_validity TEXT NOT NULL DEFAULT '',
    last_uid INTEGER NOT NULL DEFAULT 0,
    last_polled_at TEXT,
    last_verified_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    last_verify_error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS connections (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('model','search')),
    provider TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    base_url TEXT NOT NULL DEFAULT '',
    encrypted_api_key BLOB NOT NULL,
    config_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    last_test_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, kind)
);
CREATE TABLE IF NOT EXISTS key_circuits (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    failures INTEGER NOT NULL DEFAULT 0,
    open_until TEXT,
    reason TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY(user_id, kind)
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    mailbox_id TEXT NOT NULL REFERENCES mailboxes(id) ON DELETE CASCADE,
    uid_validity TEXT NOT NULL DEFAULT '',
    imap_uid INTEGER NOT NULL,
    subject TEXT NOT NULL,
    sender_name TEXT NOT NULL DEFAULT '',
    sender_address TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL,
    importance TEXT NOT NULL DEFAULT 'normal',
    message_key TEXT,
    body BLOB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','processing','sent','failed','skipped','held')),
    skip_reason TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(mailbox_id, uid_validity, imap_uid)
);
CREATE TABLE IF NOT EXISTS announcements (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    tone TEXT NOT NULL DEFAULT 'info' CHECK(tone IN ('info','warn','critical')),
    deliver_email INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    -- **历史列：2026-09-24 起没有任何代码读写它。** 官网布告栏下线了，公告只剩站内
    -- 广播这一种去向。留着不删是因为老库里那 6 条曾经贴过布告栏 —— 那是唯一记录
    -- 「它当时是公开的」的地方，而删列要重写整张表，不值当。新行一律落 0 / NULL。
    -- 老库的迁移还在：`initialize()` 会给缺列的库补上（见下面那个 ALTER）。
    is_public INTEGER NOT NULL DEFAULT 0,
    public_at TEXT,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    withdrawn_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_announcement_active ON announcements(active, created_at);
-- 广播的配图（2026-09-17）。**和 `background_images` 分开**：那张表是「一个人自己的
-- 照片，只有他能看」，这张是「运营者发给所有人的一张图，签名用户都能看，贴到官网
-- 布告栏时连没登录的人也能看」。两者的可见性规则相反，放一张表里迟早会串。
--
-- `announcement_id` 可空：运营者先上传、再决定发不发（预览、改文案），所以上传时
-- 还没有公告行。**草稿超过几小时就清掉**（见 `purge_draft_announcement_images`），
-- 否则「选了图又放弃」会永久占着库和备份。
CREATE TABLE IF NOT EXISTS announcement_images (
    id TEXT PRIMARY KEY,
    announcement_id TEXT REFERENCES announcements(id) ON DELETE CASCADE,
    media_type TEXT NOT NULL,
    bytes BLOB NOT NULL,
    width INTEGER NOT NULL DEFAULT 0,
    height INTEGER NOT NULL DEFAULT 0,
    byte_size INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_announcement_images_ann ON announcement_images(announcement_id);
CREATE TABLE IF NOT EXISTS announcement_dismissals (
    announcement_id TEXT NOT NULL REFERENCES announcements(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    dismissed_at TEXT NOT NULL,
    PRIMARY KEY (announcement_id, user_id)
);
CREATE TABLE IF NOT EXISTS announcement_deliveries (
    announcement_id TEXT NOT NULL REFERENCES announcements(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending',
    sent_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (announcement_id, user_id)
);
CREATE TABLE IF NOT EXISTS token_usage (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    message_id TEXT,
    report_id TEXT,
    kind TEXT NOT NULL DEFAULT 'immediate',
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT '',
    cost REAL,
    price_json TEXT NOT NULL DEFAULT '',
    -- Which key paid for this call: 1 = the instance-wide pilot key, 0 = the
    -- user's own. **Deliberately nullable, and NULL means "not recorded".**
    -- The pilot promises in four places that the operator pays, so a user-facing
    -- "what did I spend" view that guessed from "do they have a key now?" would
    -- be wrong for every row written before they added one -- and a default of 0
    -- would silently claim they had paid for calls the operator paid for. NULL
    -- keeps that honest: history that cannot answer the question says so.
    on_platform INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_user_created ON token_usage(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_created ON token_usage(created_at);
CREATE TABLE IF NOT EXISTS model_prices (
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    input_cache_hit REAL,
    input_cache_miss REAL,
    output REAL,
    peak_multiplier REAL NOT NULL DEFAULT 1,
    currency TEXT NOT NULL DEFAULT 'USD',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (provider, model)
);
CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
    kind TEXT NOT NULL CHECK(kind IN ('immediate','daily','test')),
    subject TEXT NOT NULL,
    body_markdown BLOB NOT NULL,
    status TEXT NOT NULL DEFAULT 'generated' CHECK(status IN ('generated','sent','failed')),
    sent_to TEXT NOT NULL DEFAULT '',
    report_date TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    sent_at TEXT,
    UNIQUE(user_id, kind, report_date, message_id)
);
CREATE TABLE IF NOT EXISTS feedback (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    report_id TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    rating TEXT NOT NULL CHECK(rating IN ('useful','not_useful')),
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(user_id, report_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_due ON messages(status, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_reports_user_created ON reports(user_id, created_at DESC);
-- Operator audit trail. Append-only by design: the web layer exposes listing
-- only, never editing or deleting, so the record can be trusted as history.
-- Admin e-mail is stored in clear because the admin identity itself comes from
-- the server environment and is not a secret; nothing else is copied in here.
CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    actor_user_id TEXT NOT NULL DEFAULT '',
    actor_email TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    target_user_id TEXT NOT NULL DEFAULT '',
    target_email TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    client TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log(created_at DESC);
-- Operator-alert de-duplication. One row per condition the sentinel watches, so
-- a worker restart does not re-send every standing alert, and so a cleared
-- condition can be announced once. `detail` is compared to detect a condition
-- that changed rather than merely persisted. No credential or mail body ever
-- reaches this table: `title`/`detail` are the phrases the sentinel wrote.
CREATE TABLE IF NOT EXISTS alert_state (
    key TEXT PRIMARY KEY,
    severity TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    last_sent_at TEXT NOT NULL,
    open INTEGER NOT NULL DEFAULT 1,
    -- Set when the operator presses "已知晓" on a finding: the condition is not
    -- fixed, but it is known and must stop mailing. Kept on this row rather than
    -- in a separate mute list so that clearing the condition clears the silence
    -- with it -- a mute that outlived its finding would hide the *next* problem
    -- arriving under the same key, which is how a known-issues list quietly
    -- becomes a blindfold.
    acknowledged_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_report_per_message ON reports(message_id, kind) WHERE message_id IS NOT NULL;
-- One row per AI analysis of one finding. `body` is encrypted with the same
-- envelope as everything else, because an analysis quotes operational detail
-- (counts, error strings) and a stolen database should not hand that over in
-- readable form. `fingerprint` lets the next pass tell "the same problem again"
-- from "the same problem, still there" without storing the detail twice.
--
-- There is deliberately no user_id column: the finding key already carries one
-- when it applies to a single account, and adding the column would put these
-- rows inside the per-user export, which is not what the export is for.
CREATE TABLE IF NOT EXISTS agent_reports (
    id TEXT PRIMARY KEY,
    finding_key TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    cost REAL,
    currency TEXT NOT NULL DEFAULT '',
    body BLOB NOT NULL,
    created_at TEXT NOT NULL,
    -- The action the model *suggested*, from the closed catalogue in
    -- `agent.ACTIONS`, or '' for none. Stored so the console can offer it for
    -- confirmation; storing it is not doing it, and nothing in this column is
    -- ever executed without an operator pressing a button.
    action TEXT NOT NULL DEFAULT ''
);
-- An operator asking for a suggested action to actually happen. A queue rather
-- than a direct call because the piece that can act is the worker (it runs as
-- `cityumail` and may restart *itself*); the web process can only ask.
CREATE TABLE IF NOT EXISTS agent_actions (
    id TEXT PRIMARY KEY,
    report_id TEXT REFERENCES agent_reports(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'requested' CHECK(status IN ('requested','done','failed')),
    requested_at TEXT NOT NULL,
    requested_by TEXT NOT NULL DEFAULT '',
    finished_at TEXT,
    result TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_agent_reports_key ON agent_reports(finding_key, created_at DESC);
-- Operator-adjustable settings that used to live only in pilot.env.
--
-- pilot.env is 0600 root and is read once at process start, so anything an
-- operator should be able to change while the service is running cannot live
-- there: changing it means editing a root-owned file over SSH and restarting.
-- Values here win over the environment, which stays as the install-time
-- default. Only operator-facing knobs belong in this table — secrets never do,
-- and there is a test asserting that.
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT ''
);
-- NOTE: the unique index that de-duplicates the same mail across two forwards
-- lives in Database.initialize(), not here: it depends on messages.message_key,
-- which older databases only gain through the additive migration. Putting it in
-- this script would break every upgrade with "no such column".
CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_per_user_date ON reports(user_id, kind, report_date) WHERE kind='daily';
-- A user's own decision about one action item: "handled" (hidden from today's
-- list) or back to open. Nothing here deletes anything, and no report or mail
-- row is touched -- this table only records a choice, so the task can always be
-- brought back.
--
-- The task itself is *not* stored as a row anywhere: `today_tasks` re-derives it
-- from the report text on every request, which is why the key is a hash of that
-- content rather than an id. `subject`/`action`/`deadline` are denormalised
-- copies kept for two reasons: the "look back at an earlier day" view can be
-- rendered without decrypting every report of that day, and a completed task
-- stays readable even if the mail it came from is later purged by a mailbox
-- re-scan (which deletes the message, and with it the join the live view needs).
--
-- `task_day` is the user's *local* date of the mail that produced the task, so
-- the archive groups by the day the user experienced, not by UTC.
CREATE TABLE IF NOT EXISTS task_states (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    task_key TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'done' CHECK(state IN ('done','open')),
    task_day TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    deadline TEXT NOT NULL DEFAULT '',
    priority TEXT NOT NULL DEFAULT '',
    -- The user's own ranking, which is a *different fact* from `priority` above
    -- (the model's reading of the mail). Keeping both is what lets the UI say
    -- "you set this" without losing what the report said, and lets a user who
    -- never touches the control keep the automatic ordering untouched.
    user_priority TEXT NOT NULL DEFAULT '',
    -- "Snooze": the task steps out of today's list until this moment (UTC ISO),
    -- then walks back in on its own. '' means "not snoozed".
    --
    -- A column of its own rather than a third value of `state`: that one is
    -- done/open and every query and the digest are built around it, so a
    -- "snoozed" state would silently change the meaning of all of them.
    snoozed_until TEXT NOT NULL DEFAULT '',
    sender TEXT NOT NULL DEFAULT '',
    message_id TEXT NOT NULL DEFAULT '',
    done_at TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, task_key)
);
CREATE INDEX IF NOT EXISTS idx_task_states_day ON task_states(user_id, task_day DESC);
-- People who asked for a pilot account from the public landing page.
--
-- Deliberately separate from `invites`: an application is a *request*, not a
-- credential, and keeping them apart means a flood of applications can never
-- hand anyone access. That separation outlives the 2026-09-22 change that made
-- registration open (no invite code needed): an application row still mints
-- nothing, and `POST /api/signup` still cannot create an account -- it only
-- queues a note for the operators, which is what keeps this table a list rather
-- than a back door.
--
-- The partial unique index stops one address queueing itself many times while
-- still allowing a fresh application after an earlier one was declined.
-- The public message board. Deliberately a separate table from
-- signup_requests: a message is not an application, and folding them together
-- would make "apply for the pilot" mean two different things at once -- with the
-- approval flow's tests quietly covering only one of them.
CREATE TABLE IF NOT EXISTS guest_messages (
    id TEXT PRIMARY KEY,
    body TEXT NOT NULL,
    nickname TEXT NOT NULL DEFAULT '',
    -- Optional, and never published even when it is there: it exists so the
    -- operator can answer, not so the page can show it. Stored encrypted for the
    -- same reason every other piece of personal data here is -- "only the
    -- operator sees it" is a statement about the screen, not about the disk.
    email TEXT NOT NULL DEFAULT '',
    -- Rate limiting and duplicate suppression only need to recognise the same
    -- client again. See SecretBox.anonymized for why this is a keyed digest.
    client_hash TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending','published','rejected','deleted')),
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_guest_status_created ON guest_messages(status, created_at);

CREATE TABLE IF NOT EXISTS signup_requests (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','invited','declined')),
    invite_label TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    decided_at TEXT,
    client TEXT NOT NULL DEFAULT '',
    -- Proof of what happened to the invite e-mail. Without these the only trace
    -- of a send was a log line and the response to the click that triggered it,
    -- so "did this applicant actually get their code?" was unanswerable a day
    -- later. Empty invite_sent_at with a non-empty error means the attempt
    -- failed; both empty means no attempt was made.
    invite_sent_at TEXT NOT NULL DEFAULT '',
    invite_send_error TEXT NOT NULL DEFAULT '',
    invite_message_id TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_signup_pending_email
    ON signup_requests(email) WHERE status='pending';
CREATE INDEX IF NOT EXISTS idx_signup_created ON signup_requests(created_at DESC);
-- 「我没收到邀请码」的自助请求（v0.63.72）。**入队，不是当场发信**：发一封信要几秒，
-- 而这是一个未认证端点——把 SMTP 挂在请求路径上，陌生人就能拖住 web 进程；而且
-- 「有这份申请」要一秒、「没有」只要几毫秒，耗时本身会把回执刻意抹掉的区别说出去。
-- worker 一分钟内取走并投递，顺便共用同一套重试与计数。
CREATE TABLE IF NOT EXISTS invite_resends (
    id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL,
    -- 键控摘要，不是地址：这一列只用来限流与排查，没有任何地方需要读回原值。
    client_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    handled_at TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_invite_resends_email ON invite_resends(email, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_invite_resends_open ON invite_resends(handled_at);
-- A user-chosen background photo. Deliberately its own table rather than a
-- column on profiles: both get_profile() and export_user_data() read profiles
-- with SELECT *, so a BLOB there would ride along into every /api/me response
-- and make json encoding fail on bytes. One row per user, replaced in place, and
-- the cascade is what makes "deleting the account deletes the photo" automatic
-- rather than a cleanup job somebody has to remember.
CREATE TABLE IF NOT EXISTS background_images (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    media_type TEXT NOT NULL,
    bytes BLOB NOT NULL,
    rev INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
-- One row per page view. `client_hash` is a keyed digest, never an address --
-- see analytics.py for why, and SecretBox.anonymized for the definition. The
-- country/city columns are resolved once, at insert time, from an offline
-- DB-IP Lite database: resolving them later would mean keeping the address,
-- which is the one thing this table deliberately does not have.
--
-- `bot` is stored rather than filtered on the way in, so the panel can show
-- both halves of the truth ("N people, plus M robots"), and `source` records
-- whether a row came from a live request or from importing nginx's own access
-- log -- which matters because an imported row has no live-buffer entry to go
-- with it.
CREATE TABLE IF NOT EXISTS page_views (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    path TEXT NOT NULL,
    status INTEGER NOT NULL DEFAULT 0,
    referrer TEXT NOT NULL DEFAULT '',
    client_hash TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL DEFAULT '',
    country_name TEXT NOT NULL DEFAULT '',
    city TEXT NOT NULL DEFAULT '',
    continent TEXT NOT NULL DEFAULT '',
    bot INTEGER NOT NULL DEFAULT 0,
    member INTEGER NOT NULL DEFAULT 0,
    -- 运营者看自己的站不算访客。这一列让统计把他排除在外，也让他能一键
    -- 删掉自己的记录（见 analytics.purge_mine）。
    admin INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'live'
);
CREATE INDEX IF NOT EXISTS idx_page_views_created ON page_views(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_page_views_client ON page_views(client_hash, created_at DESC);
-- Imported rows are deduplicated, live ones are not. Re-running the nginx import
-- must not double every number, and the natural key of an imported visit is
-- (when, what, who-digest) -- there is nothing else in a log line. The index is
-- partial because for a live request that same triple *can* legitimately repeat
-- (somebody reloading a page within the same second), and dropping those rows
-- would silently under-count real traffic.
CREATE UNIQUE INDEX IF NOT EXISTS idx_page_views_import_key
    ON page_views(created_at, path, client_hash) WHERE source='nginx';
-- 已经清掉的运营者地址。删除本身不够：导入的去重键就是 (时间, 页面, 摘要)，
-- 行删了以后重跑一次「manage analytics-import-nginx」会把他刚清掉的历史原样搬
-- 回来——而且看起来像是删除没生效。所以删除时把这些摘要记在这里，导入时跳过。
-- 只影响导入：实时那次知道是谁的会话，用不着靠地址猜。
CREATE TABLE IF NOT EXISTS page_view_ignored (
    client_hash TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
"""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def utc_now_fine() -> str:
    """``utc_now()`` with sub-second precision, for **markers** rather than records.

    Every stored timestamp in this file is second-precision, and that is fine for
    "when did this happen" -- but not for a watermark. `admin_activity` compares
    rows against "the moment you last looked", and with both sides rounded to the
    second an application that arrives in the *same second* as that look is
    neither counted now nor later: the marker has already moved past it. Asking
    for microseconds costs nothing and makes the window exact.

    ISO-8601 strings sort correctly as text here because the format is otherwise
    identical: a value without a fraction (`...:02+00:00`) compares *before* one
    with (`...:02.5+00:00`), which is exactly the real order.
    """
    return dt.datetime.now(dt.timezone.utc).isoformat()


_OFFSET_MODIFIER_RE = re.compile(r"^\s*([+-])(\d{1,4})\s*minutes\s*$")


def analytics_offset(value: str) -> dt.timedelta:
    """Turn a SQLite "minutes" modifier back into a timedelta.

    The same string is handed to SQLite (``date(created_at, ?)``) and used here
    to work out which UTC instant a local midnight is, so both halves of a
    "today" query agree by construction. Anything unrecognised falls back to
    +8 hours -- this instance's zone -- rather than raising inside a request.
    """
    match = _OFFSET_MODIFIER_RE.match(str(value or ""))
    if not match:
        return dt.timedelta(hours=8)
    sign = 1 if match.group(1) == "+" else -1
    return dt.timedelta(minutes=sign * int(match.group(2)))


def moment(value: Any) -> str:
    """One timestamp, however the caller happens to hold it.

    Half this file takes a ``datetime`` from the caller and half builds its own
    string with ``utc_now()``, and the two are indistinguishable until one of
    them reaches ``.isoformat()``. That happened in the path that records the
    *failure* of a confirmed action, so the crash landed inside the error
    handler -- the one place that must never raise. Annotating the parameter
    ``Any`` is what let it through; normalising here is what stops the next one.
    """
    if isinstance(value, dt.datetime):
        return value.isoformat(timespec="seconds")
    return str(value)


def parse_utc(value: Any) -> dt.datetime | None:
    """Parse one of our stored UTC timestamps, tolerating junk and ``None``.

    Shared by the alert sentinel and the capacity advisor rather than written
    twice: two parsers would eventually disagree about a naive timestamp, and
    the disagreement would show up as a wrong alert or a wrong recommendation.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _email_taken_message(status: str) -> str:
    """What to tell somebody whose address already has an account.

    One definition, two callers (the pre-check and the unique-constraint
    backstop), because the whole point of this sentence is that it must **never**
    be the generic 「服务器内部错误」 again -- see ``create_user``. It says three
    things in the order the reader needs them: what is wrong, what to do
    instead, and where to get help. There is no self-service password reset in
    this project (every outgoing message borrows a user's own mailbox), so
    "write to the operator" is the honest instruction.

    ``deleted`` is a legacy shape: older builds soft-deleted the account, and
    that row still occupies the address while being invisible to login.
    """
    if status == "deleted":
        return ("这个邮箱之前注销过，那条记录还占着它。想重新使用请联系运营者"
                "（邮箱见《隐私政策》第 6 节）。")
    if status == "paused":
        return ("这个邮箱已经注册过，账号目前被暂停了。要恢复请联系运营者"
                "（邮箱见《隐私政策》第 6 节）。")
    return ("这个邮箱已经注册过了——直接登录就行。忘了密码的话，写信给运营者"
            "（邮箱见《隐私政策》第 6 节）请他帮你重设。")


def human_hours(hours: float) -> str:
    """A duration in the coarsest unit a person would say out loud.

    Used in sentences like 「邮箱接通已经 3 天」 -- a raw ``72.0`` would be read as
    a measurement rather than as a wait, and the reader is being asked to act,
    not to verify arithmetic.
    """
    hours = max(0.0, float(hours))
    if hours < 24:
        return f"{max(1, int(round(hours)))} 小时"
    if hours < 240:
        return f"{hours / 24:.1f} 天"
    return f"{int(hours / 24)} 天"


# The public message board's shape limits. They are here rather than in the
# request handler because the table is what a future caller would bypass.
GUEST_BODY_LIMIT = 800
GUEST_NICKNAME_LIMIT = 40
GUEST_LINK_LIMIT = 2

# 「账号最后活跃时间」这一列从哪一刻开始记的（见 `users.last_seen_at`）。一条比它更早的
# 提醒，其「之后」没有任何人在看——那时候说「他没回来」是拿一个没有数据的时段当证据。
LAST_SEEN_SINCE_KEY = "last_seen_tracking_since"

# ---------------------------------------------------------------------------
# Connection-level settings, and why each one is where it is
# ---------------------------------------------------------------------------
#
# These are not tuning knobs for a benchmark: each is a decision about what a
# *single* request is allowed to do to every other request. Measured values come
# from `tools/sqlite_settings_probe.py` and `tools/sqlite_profile_calls.py` on a
# 100-account database; the numbers quoted here are from that run.

# How long SQLite waits for a competing writer before raising "database is
# locked". The previous value came from `sqlite3.connect(timeout=20)`, which is
# the same thing -- it is named here so it can be *seen*, because 20 s is the
# length of the web service's whole request budget (`MemoryMax=350M`,
# `TasksMax=96`): a request that waits the full 20 s has stopped being a page and
# become an outage. Under WAL a writer only blocks another writer, so this is the
# ceiling on the queue behind the nightly checkpoint, not on readers.
BUSY_TIMEOUT_MS = 20_000
BUSY_TIMEOUT_SECONDS = BUSY_TIMEOUT_MS / 1000

# 32 MiB of page cache instead of SQLite's 2 MiB default, and 256 MiB of address
# space mapped over the file instead of copying pages into the heap. Per
# connection, which is affordable only because this process runs one connection
# per operation rather than per thread. The reason it matters: the `messages`
# table is the largest object in the database and every operator screen scans
# part of it, so on the default cache each such query re-reads from disk.
# `mmap_size` is address space, not resident memory -- pages are faulted in on
# demand and are shared with the page cache the kernel already holds.
CACHE_KIB = -32_000  # negative means KiB, not pages
MMAP_BYTES = 268_435_456

# The schema this build expects. Bumping it is what makes the one expensive
# migration (the `messages` table rebuild) run again; a database already at this
# version skips it. **Bump it in the same commit as any change to `SCHEMA` or to
# `RETIRED_INDEXES`.**
SCHEMA_VERSION = 3

#: 端到端样本里「最近这一小段」有多长（天）。**为什么需要它**：中位数是用来回答
#: 「这一台现在多快」的，而换主服务会把整个延迟分布搬走——2026-09-23 切到本机那台时，
#: 14 天窗口里的 p50 仍是上一任供应商的 7 秒，而当天本机那档已经是 10.5 秒。
#: 窗口放长，容量建议就一直在描述一个已经不在干活的供应商；而**下一次换模型会再犯一次**，
#: 所以在这里收窄，而不是给"本机那台"硬编一个秒数。
RECENT_SAMPLE_DAYS = 3
SCHEMA_VERSION_KEY = "schema_version"

# Indexes that were **measured** to earn their keep, and the query each one was
# measured against (`tools/sqlite_index_audit.py` drops each index and re-runs its
# query, so the number is the index's entire effect).
#
# `messages` had no index on `user_id` at all -- its only unique key is
# (mailbox_id, uid_validity, imap_uid), and `mailbox_id` already implies the user.
# Every per-user read therefore scanned the whole table.
#
# Only two indexes cleared the bar. The gains quoted are from a 30 000-message
# database with 20 users sampled:
LATE_INDEXES = (
    # `COUNT(*) ... WHERE user_id=? AND status!='skipped'` -- the dashboard's "how
    # many mails have been analysed" (on **every** page load) and the console's
    # queue-depth counts. Measured across 20 users, as a page render pays it:
    #
    #     shipped 0.022 ms (covering index)  vs  0.183 ms without   = 8.3x
    #
    # The ratio understates it: with this dropped, SQLite falls back to another
    # index that this change *rejects*, so the real "before" is what the profiler
    # measured with no usable index at all -- **176 ms per call, a full scan of
    # `messages`**, against 1.8 ms here. Being a *covering* index is what makes it
    # an index-only scan: the count never touches a table page.
    ("idx_messages_user_status", "messages(user_id, status)"),
    # `list_messages_overview` -- the operator console's "every mail, newest first",
    # where the ordering must come from an index over the whole table rather than
    # from each user's slice:
    #
    #     shipped 0.028 ms  vs  6.642 ms without   = 237x
    #
    # The largest single win in this change, and it exists only because the console
    # sorts the *fleet*, not one user -- a per-user index cannot serve that ORDER BY.
    ("idx_messages_received", "messages(received_at DESC)"),
)

# Indexes that were added, measured, **and found not to be worth their keep**.
# They are not created. The list is kept because the reasoning is the valuable
# part: both looked obviously right, and both were wrong.
#
# **This list is the point of `tools/sqlite_index_audit.py`.** An index costs disk,
# backup size and a write on every `INSERT INTO messages`; "it cannot hurt" is not
# true, and the only way to know is to drop it and re-measure.
REJECTED_INDEXES = (
    # `messages(user_id, received_at DESC)`, for `messages_between` (the daily
    # digest's window read).
    #
    #     shipped 0.155 ms  vs  0.166 ms without   = 1.07x
    #
    # With it dropped, SQLite uses `idx_messages_user_status` and sorts the (small,
    # per-user, date-filtered) result in a temp B-tree -- and costs 4% more. There
    # is no size of database in which a per-user window read needs help here: the
    # `user_id` prefix is already doing all the work, and the sort is over that
    # user's rows only. Kept out rather than kept "just in case".
    "idx_messages_user_received",
    # `reports(message_id, kind)`, for `report_for_message` ("is there already a
    # report for this mail").
    #
    #     shipped 0.042 ms  vs  0.041 ms without   = 0.98x -- i.e. none
    #
    # The reason is visible in the plans: **the index already existed**.
    # `idx_report_per_message` is a pre-existing partial unique index on
    # `(message_id, kind) WHERE message_id IS NOT NULL`, and it serves this query
    # exactly. In both states SQLite used it. Adding the second one cost writes and
    # changed nothing -- the mistake was not checking what was already there before
    # writing `CREATE INDEX`.
    "idx_reports_message_kind",
)

# Indexes a previous build created, that measurement then retired. Dropped rather
# than merely removed from the lists above, because a database that already ran the
# version which created them would otherwise keep paying for them forever.
#
# `idx_messages_queue` was `messages(status, next_attempt_at, created_at)`, added on
# the theory that it could supply the worker's `ORDER BY created_at` as well as its
# filter, where the pre-existing `(status, next_attempt_at)` forces a temp B-tree.
# The theory was wrong twice over:
#
# * SQLite never chose it. With a 10 000-row backlog the plan still read
#   `idx_messages_due` + `USE TEMP B-TREE FOR ORDER BY`, and the same query with
#   every index disabled (`NOT INDEXED`) cost the same as the indexed plan -- the
#   sort is not where the query's ~345 ms goes.
# * Dropping it changed the query time by -0.61 ms, i.e. nothing.
#
# The one-off script that measured this (`tools/sqlite_queue_plan.py`) has been
# deleted, because the question is settled: the numbers above are the whole
# finding, and keeping an executable for a hypothesis that was disproved invites
# somebody to re-run it instead of reading the answer.
#
# Both of the others above are here too, because the version that shipped them in
# `LATE_INDEXES` may already have run on a real install.
RETIRED_INDEXES = ("idx_messages_queue",) + REJECTED_INDEXES


class CapacityFull(ValueError):
    """名额已满。

    为什么要有它（2026-09-24 的只读清点指出）：名额检查原来**只在 HTTP 层**做
    （`count_users() >= limit` → 403），而它与 `create_user` 里的 INSERT 之间是一个窗口——
    服务器是 `ThreadingHTTPServer`，两个同时到达的注册可以双双通过检查。
    数据层现在**在同一个事务里**再数一次，并用这个类型说清是哪一种拒绝
    （web 层据此翻成 403，而不是 400「注册失败」）。

    故意继承 `ValueError`：老的调用方按 ValueError 处理仍然说得通。
    """


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)

    @contextlib.contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """One short-lived connection for one operation.

        **What is deliberately not here.** `PRAGMA journal_mode = WAL` used to run
        on every connection -- fifteen times per dashboard request, once per queue
        pass, once per page view. Two things were wrong with that. The mode is a
        property of the *file*: it persists across connections and restarts, so on
        a database already in WAL the statement is a header read that cannot change
        anything. And it is not free to *ask*: it is answered through SQLite's lock
        table, so it is the one statement a **reader** executes that can be made to
        wait on a **writer**. Confirming an unchanged setting is worth neither.

        Note what the measurement did *and did not* show
        (`tools/sqlite_settings_probe.py`, every variant on its own fresh copy of
        the database, random order, median of three): single-threaded, keeping the
        PRAGMA per connection costs nothing measurable (~1.8 ms vs ~2.0 ms p50,
        inside the noise), because on a WAL database it is a header read. So the
        case for moving it is the **lock ordering** above and the fact that it
        cannot ever help -- not a 0.7 ms saving. Do not re-add it as a "speed fix";
        it was already measured.

        It now lives in `initialize()`, which every entry point calls before
        anything else and which warns loudly if the mode does not stick.

        `busy_timeout`, in contrast, *is* per connection, and so are the page
        cache and the mmap window. See the constants above for why these values.
        """
        connection = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_SECONDS)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA cache_size = {CACHE_KIB}")
            connection.execute(f"PRAGMA mmap_size = {MMAP_BYTES}")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        """Bring the file up to date and set the settings that belong to the file.

        Runs once per process, at startup, before any request is served. Three
        jobs, in an order that is load-bearing:

        1. `_apply_file_settings` -- settings that are properties of the database
           rather than of a connection (`journal_mode`), plus the assertion that
           they took. If WAL did not stick, say so at startup rather than meeting
           it later as a mystery 20-second request.
        2. `migrate` -- create what is missing, add the columns older databases
           lack, create the late indexes.
        3. `_apply_data_fixups` -- idempotent corrections to *rows*, run on every
           start rather than behind the version gate (which exists to skip
           expensive *schema* work, not to skip a compliance-relevant cleanup).

        **A first version of this gated everything on `SCHEMA_VERSION` and broke
        the upgrade path.** A database created by an older build reports version
        0, migrates, and is stamped -- fine. But a database that was already
        stamped while still missing an additive column (`token_usage.on_platform`,
        in the test that caught it) would then skip the very migration that adds
        it and fail at read time with "no such column". The gate now sits only in
        front of the one genuinely expensive operation, the `messages` table
        rebuild; the `ALTER TABLE ADD COLUMN` statements it used to guard are
        cheap, self-checking (`PRAGMA table_info`), and racing against them is
        harmless because they are idempotent.
        """
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._apply_file_settings()
        self.migrate()
        self._apply_data_fixups()
        # Uploaded-but-never-published broadcast images. Outside the migration
        # entirely: this is daily housekeeping, and a failure here must not stop
        # the process from starting.
        try:
            dropped = self.purge_draft_announcement_images()
            if dropped:
                logging.info("清理了 %s 张没发布的广播配图", dropped)
        except Exception:  # noqa: BLE001 - 清理失败不该拦住启动
            logging.warning("清理草稿配图失败", exc_info=True)

    def _apply_file_settings(self) -> None:
        """Settings that belong to the database *file*, applied exactly once.

        `journal_mode` is the one that matters, and it is the reason this method
        exists at all: it used to be set on every connection, which made every
        reader take a write lock to re-confirm a setting that cannot have changed.
        Re-asserting it here is enough -- it persists in the file header across
        connections and across restarts, so the only way it becomes unset is a
        deliberate external change (`sqlite3` on the CLI, a restore of an older
        copy), and the warning below is how that gets noticed.
        """
        connection = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_SECONDS)
        try:
            connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()
            mode = str(mode[0]).lower() if mode else ""
            if mode != "wal":
                # Not fatal -- SQLite refuses WAL on some network filesystems,
                # and a self-hoster's NFS mount should still run, just slower.
                logging.warning(
                    "journal_mode is %r, not 'wal': write concurrency will be much "
                    "worse and readers will block writers. If this database lives on "
                    "a network mount, move it to local storage.", mode)
            # Only local to this connection and to any connection that follows it
            # on a database already checkpointed this way, but harmless to assert.
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA wal_autocheckpoint = 1000")
        finally:
            connection.close()

    def _apply_data_fixups(self) -> None:
        """Idempotent data corrections, run on **every** start, not behind the gate.

        These are deliberately outside `migrate()`'s `SCHEMA_VERSION` gate. That
        gate exists to skip *expensive schema work*, and using it for data would
        be a correctness bug with a compliance flavour: the body purge below is
        what makes the published privacy promise ("a skipped mail keeps its
        metadata only") true, and a database that reports the current schema
        version while still holding skipped bodies would be one the policy lies
        about. Cheap when there is nothing to do, and there is nothing to do on a
        healthy install.
        """
        with self.connect() as connection:
            # Bodies of mail that was deliberately never analysed. Versions before
            # v0.40 stored the encrypted body first and only then applied the
            # sender filter, so skipped rows held a body nobody would ever read.
            # X'' (not '') keeps the column a BLOB, matching what the ingestion
            # path writes for a skipped mail.
            #
            # ``held`` 是同一件事的另一半：处理完、报告也生成了，但主人关掉了报告
            # 邮件。正文同样不许留——"服务器不留正文"不因为不发邮件而改变。
            # （这一行来自远端 v0.63.84；合并时它与本函数的重构撞在一起，
            # 两个意图都保留：状态列表取远端的，位置取重构后的这里。）
            #
            # The table may not exist yet -- on a brand-new file the first call
            # happens before `migrate()` has created anything -- so "no such
            # table" is the expected answer there and not a failure.
            if self._has_table(connection, "messages"):
                connection.execute(
                    "UPDATE messages SET body=X''"
                    " WHERE status IN ('skipped','held') AND body!=X''")
            # **这一列从什么时候开始记的**，和列一起落库。它决定面板能不能下结论：
            # 一条比它更早的提醒，其「之后」根本没人看着——那时说「他没回来」是拿
            # 一个没有数据的时间段当证据。空着就补一次，补过就不再动。
            if self._has_table(connection, "app_settings") and not connection.execute(
                    "SELECT 1 FROM app_settings WHERE key=?", (LAST_SEEN_SINCE_KEY,)).fetchone():
                connection.execute(
                    "INSERT INTO app_settings(key,value,updated_at,updated_by) VALUES(?,?,?,'')",
                    (LAST_SEEN_SINCE_KEY, utc_now(), utc_now()))

    @staticmethod
    def _has_table(connection: sqlite3.Connection, name: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    def migrate(self) -> None:
        """Create what is missing, add the columns, build the late indexes.

        Everything here is idempotent and self-checking, which is why it is not
        behind the `SCHEMA_VERSION` gate: the gate exists for the one expensive
        rebuild below, and putting the `ALTER TABLE` statements behind it made a
        database that was stamped while missing a column unable to ever gain it.

        The gate is still worth having for its own sake, and what it buys is now
        measured rather than assumed: on a 984 MiB / 30 000-message database the
        full pass takes ~0.2 s, and the second start of the same process takes
        0.03 s because the rebuild is skipped. Neither is large, which is the
        point -- this is startup work in front of the first request, and it should
        not grow with the size of the table it is not touching.
        """
        with self.connect() as connection:
            # Additive migrations MUST run before executescript(SCHEMA): the
            # schema contains an index on messages.message_key, and on a
            # database created before that column existed the CREATE INDEX
            # statement would fail with "no such column". SQLite has no
            # IF NOT EXISTS form for ADD COLUMN.
            # 1) Create anything missing (no-op for an existing database).
            connection.executescript(SCHEMA)
            # 2) Add columns that older databases do not have yet.
            columns = {row[1] for row in connection.execute("PRAGMA table_info(profiles)")}
            for name, definition in (
                ("school_email", "TEXT NOT NULL DEFAULT ''"),
                ("theme", "TEXT NOT NULL DEFAULT 'paper'"),
                ("background", "TEXT NOT NULL DEFAULT ''"),
                # '' means "follow the instance setting" -- the same shape as
                # `background`, and the reason this can ship without changing
                # anybody's mail: every existing row is already in the right
                # state, and a user who never opens the panel keeps getting
                # whatever the instance is configured to send.
                ("report_mode", "TEXT NOT NULL DEFAULT ''"),
                # 2026-09-23：注册时那三栏选填资料。空串 = 没填，与申请表上的语义一致。
                ("signup_nickname", "TEXT NOT NULL DEFAULT ''"),
                ("signup_identity", "TEXT NOT NULL DEFAULT ''"),
                ("signup_goals", "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in columns:
                    connection.execute(f"ALTER TABLE profiles ADD COLUMN {name} {definition}")
            # v0.63.38：page_views 是 CREATE TABLE IF NOT EXISTS 建的，老库里已经
            # 有这张表，所以新列要靠 ALTER 补——否则线上查询会直接报 no such column。
            view_columns = {row[1] for row in connection.execute("PRAGMA table_info(page_views)")}
            if view_columns and "admin" not in view_columns:
                connection.execute("ALTER TABLE page_views ADD COLUMN admin INTEGER NOT NULL DEFAULT 0")
            mailbox_columns = {row[1] for row in connection.execute("PRAGMA table_info(mailboxes)")}
            for name, definition in (
                ("last_verified_at", "TEXT"),
                ("last_verify_error", "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in mailbox_columns:
                    connection.execute(f"ALTER TABLE mailboxes ADD COLUMN {name} {definition}")
            signup_columns = {row[1] for row in connection.execute("PRAGMA table_info(signup_requests)")}
            for name, definition in (
                ("invite_sent_at", "TEXT NOT NULL DEFAULT ''"),
                ("invite_send_error", "TEXT NOT NULL DEFAULT ''"),
                ("invite_message_id", "TEXT NOT NULL DEFAULT ''"),
                # v0.63.72：投递尝试的次数与最后一次的时刻。B 计划（见
                # docs/invite-plan-b-2026-09-17.md）要按「试过几次、上次什么时候」
                # 决定还该不该自动重试——没有这两列，重试要么无限循环，要么每次
                # 重启都从头再来一遍。
                ("invite_attempts", "INTEGER NOT NULL DEFAULT 0"),
                ("invite_last_attempt_at", "TEXT NOT NULL DEFAULT ''"),
                # v1.0.1：申请表单上那三个**选填**项（怎么称呼你 / 身份 / 最想先解决什么）。
                # 加在这里而不是 `SCHEMA_VERSION` 闸门后面：它们只是加列，不重建表，
                # 而闸门存在的唯一理由是「别重复付那次昂贵的 messages 重建」。
                ("nickname", "TEXT NOT NULL DEFAULT ''"),
                ("identity", "TEXT NOT NULL DEFAULT ''"),
                ("goals", "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in signup_columns:
                    connection.execute(f"ALTER TABLE signup_requests ADD COLUMN {name} {definition}")
            user_columns = {row[1] for row in connection.execute("PRAGMA table_info(users)")}
            if "is_admin" not in user_columns:
                connection.execute("ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
            if "admin_note" not in user_columns:
                connection.execute("ALTER TABLE users ADD COLUMN admin_note TEXT NOT NULL DEFAULT ''")
            # v0.63.67：老库里没有这一列，`SELECT u.last_seen_at` 会当场 no such
            # column（和 page_views.admin、task_states.user_priority 同一个坑）。
            if "last_seen_at" not in user_columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN last_seen_at TEXT NOT NULL DEFAULT ''")
            # 2026-09-23：界面语言。'' = 还没选过（按浏览器 `Accept-Language` 猜），
            # 否则是 `pilot_app/i18n.py` 里那个语言代码。
            #
            # 放在 `users` 而不是 `profiles`：profiles 的每一次用户侧读取都是
            # `SELECT *`（`get_profile` / `export_user_data`），加在那里它会跟着
            # `/api/me` 与数据导出一起走——`admin_note` 上面那段注释就是为这件事写
            # 的。而这是个**界面偏好**，本来就该跟着账号走（换设备也记得）。
            if "ui_locale" not in user_columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN ui_locale TEXT NOT NULL DEFAULT ''")
            # **这一列从什么时候开始记的**，和列一起落库。它决定面板能不能下结论：
            # 一条比它更早的提醒，其「之后」根本没人看着——那时说「他没回来」是拿
            # 一个没有数据的时间段当证据。空着就让下面那句补一次。
            if not connection.execute(
                    "SELECT 1 FROM app_settings WHERE key=?", (LAST_SEEN_SINCE_KEY,)).fetchone():
                connection.execute(
                    "INSERT INTO app_settings(key,value,updated_at,updated_by) VALUES(?,?,?,'')",
                    (LAST_SEEN_SINCE_KEY, utc_now(), utc_now()))
                # INSERT **会**开一个隐式事务（DDL 不会），而下面的
                # `_relax_message_status_check` 自己要 `BEGIN`——不在这里收尾，
                # 老库升级到一半就会撞上 "cannot start a transaction within a
                # transaction"。
                connection.commit()
            # Nullable on purpose: NULL means "not acknowledged", so no default is
            # needed and every existing row is already in the right state.
            # v0.63.46：task_states 是老库里的表，用户自设的优先级靠 ALTER 补，
            # 否则线上一读就 no such column（和 page_views.admin 同一类坑）。
            task_columns = {row[1] for row in connection.execute("PRAGMA table_info(task_states)")}
            if task_columns and "user_priority" not in task_columns:
                connection.execute(
                    "ALTER TABLE task_states ADD COLUMN user_priority TEXT NOT NULL DEFAULT ''")
            # v1.3.0：同一张表、同一类坑（「稍后提醒」）。**不进 SCHEMA_VERSION**：
            # 那个闸门只挡唯一一件昂贵的重建，把加列放进去，老库被盖上版本号之后就
            # 再也补不上这一列了（上面 `initialize` 的 docstring 记着这条教训）。
            if task_columns and "snoozed_until" not in task_columns:
                connection.execute(
                    "ALTER TABLE task_states ADD COLUMN snoozed_until TEXT NOT NULL DEFAULT ''")
            alert_columns = {row[1] for row in connection.execute("PRAGMA table_info(alert_state)")}
            if "acknowledged_at" not in alert_columns:
                connection.execute("ALTER TABLE alert_state ADD COLUMN acknowledged_at TEXT")
            report_columns = {row[1] for row in connection.execute("PRAGMA table_info(agent_reports)")}
            if "action" not in report_columns:
                connection.execute("ALTER TABLE agent_reports ADD COLUMN action TEXT NOT NULL DEFAULT ''")
            announcement_columns = {row[1] for row in connection.execute(
                "PRAGMA table_info(announcements)")}
            if "is_public" not in announcement_columns:
                connection.execute(
                    "ALTER TABLE announcements ADD COLUMN is_public INTEGER NOT NULL DEFAULT 0")
            if "public_at" not in announcement_columns:
                connection.execute("ALTER TABLE announcements ADD COLUMN public_at TEXT")
            # 5) 把「预置写错的服务器」改回来：网易一个域名一台机器
            #    （`imap.163.com` / `imap.126.com` / `imap.yeah.net` / `imap.vip.*`），
            #    而预置曾经把五个域名都填成 `imap.163.com`。126 的账号因此被 163 的
            #    服务器拒登录，用户看到的是「授权码不对或已失效」——**让他去重新生成
            #    一个本来就是对的授权码**。2026-09-20 用真账号量到：同一个码在
            #    `imap.163.com` 上失败、在 `imap.126.com` 上成功。
            #    只改**同一家（网易）主机之间**的错配：其它自定义主机是用户自己填的，
            #    我们不能替他判断（他可能故意指向别的服务器）。幂等，每次启动跑一遍。
            self._repair_netease_hosts(connection)
            message_columns = {row[1] for row in connection.execute("PRAGMA table_info(messages)")}
            if "message_key" not in message_columns:
                connection.execute("ALTER TABLE messages ADD COLUMN message_key TEXT")
            if "skip_reason" not in message_columns:
                connection.execute("ALTER TABLE messages ADD COLUMN skip_reason TEXT NOT NULL DEFAULT ''")
            # No default on purpose: every existing row becomes NULL, which is
            # read as "this call predates the question" rather than "the user
            # paid". See the column comment in SCHEMA.
            usage_columns = {row[1] for row in connection.execute("PRAGMA table_info(token_usage)")}
            if "on_platform" not in usage_columns:
                connection.execute("ALTER TABLE token_usage ADD COLUMN on_platform INTEGER")
            # 3) The one genuinely expensive step, and the only thing the
            #    `SCHEMA_VERSION` gate guards. Rewriting `messages` copies every
            #    row and drops every index on it, so it must not run on a start
            #    that does not need it. `_relax_message_status_check` decides for
            #    itself whether the rebuild is needed (it reads the table's own
            #    `sqlite_master` definition), so the gate here is only about not
            #    paying for that decision's *consequences* twice.
            version = self._stored_schema_version(connection)
            if version < SCHEMA_VERSION:
                self._relax_message_status_check(connection)
            # 4) Now that the columns exist, enforce same-mail uniqueness per user.
            #    Two forwarding rules deliver one mail twice under different IMAP
            #    UIDs; without this the user gets two AI reports for one email.
            #    Created after the rebuild above, which drops what is on the table.
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_message_same_mail
                   ON messages(user_id, message_key) WHERE message_key IS NOT NULL"""
            )
            # 5) The indexes that make per-user reads a seek instead of a scan.
            for name, target in LATE_INDEXES:
                connection.execute(
                    f"CREATE INDEX IF NOT EXISTS {name} ON {target}")
            # ...and the ones a previous build added that measurement then retired.
            # `REJECTED_INDEXES` are never created, so on a fresh database this is a
            # no-op; on one that already ran the earlier version it is the cleanup.
            for name in RETIRED_INDEXES:
                connection.execute(f"DROP INDEX IF EXISTS {name}")
            self._record_schema_version(connection)

    #: 网易那几台 IMAP 主机；只有落在这个集合里的错配才会被自动改回来。
    NETEASE_IMAP_HOSTS = ("imap.163.com", "imap.126.com", "imap.yeah.net",
                          "imap.vip.163.com", "imap.vip.126.com")

    def _repair_netease_hosts(self, connection: Any) -> int:
        """Rewrite a NetEase mailbox that points at the wrong NetEase server.

        Why this is not "the user's 配置": the wrong value came from **our** preset,
        so it is our bug to clean up -- and there is no way for the user to fix it
        from the app either, because the error they see blames their authorization
        code. Returns how many rows changed (tests use it; the caller ignores it).
        """
        changed = 0
        rows = connection.execute(
            "SELECT id,email,imap_host,smtp_host FROM mailboxes").fetchall()
        for row in rows:
            wanted = mailpresets.hosts_for_email(str(row["email"]))
            if not wanted:
                continue
            host = str(row["imap_host"] or "")
            if host == wanted["imap_host"] or host not in self.NETEASE_IMAP_HOSTS:
                continue
            connection.execute(
                "UPDATE mailboxes SET imap_host=?,smtp_host=?,updated_at=? WHERE id=?",
                (wanted["imap_host"], wanted["smtp_host"], utc_now(), row["id"]),
            )
            changed += 1
        return changed

    @staticmethod
    def _stored_schema_version(connection: sqlite3.Connection) -> int:
        """The version this file reports, or 0 for "never migrated by this code".

        A file with no `app_settings` table at all is the first run; a file that
        has one but no version row predates the gate, and 0 sends it through the
        schema rebuild once. Anything unparseable is also treated as 0, because
        re-running an idempotent migration is cheap and skipping a needed one is
        not.
        """
        try:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key=?", (SCHEMA_VERSION_KEY,)).fetchone()
        except sqlite3.OperationalError:
            return 0
        if not row:
            return 0
        try:
            return int(str(row[0]))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _record_schema_version(connection: sqlite3.Connection) -> None:
        """Stamp the version **last**, so a half-finished migration is retried.

        Ordering is the whole point: if an index build fails, the file must not
        claim to be at a version whose migrations never completed.
        """
        connection.execute(
            """INSERT INTO app_settings(key,value,updated_at,updated_by) VALUES(?,?,?,'migration')
               ON CONFLICT(key) DO UPDATE SET
                 value=excluded.value, updated_at=excluded.updated_at,
                 updated_by=excluded.updated_by""",
            (SCHEMA_VERSION_KEY, str(SCHEMA_VERSION), utc_now()))

    @staticmethod
    def _relax_message_status_check(connection: sqlite3.Connection) -> None:
        """Widen the ``messages.status`` CHECK to the values this version writes.

        SQLite cannot ALTER a CHECK constraint, so an older database whose
        definition does not yet allow a status must be rebuilt. Rows are copied
        verbatim, indexes recreated, and the operation is idempotent: it only
        runs when the constraint is actually too narrow.

        Two widenings so far:

        * ``skipped`` -- mail from a sender we deliberately do not analyse;
        * ``held`` -- processed and summarised, but **not sent**, because the
          owner turned report mail off. That state has to exist and has to be
          distinct: ``sent`` would be a lie, ``failed`` would raise alerts, and
          ``skipped`` means "not our mail" (it feeds the digest's "other mail"
          list and the "has the forwarding rule ever worked" evidence).
        """
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='messages'"
        ).fetchone()
        definition = str(row[0]) if row else ""
        if "skipped" in definition and "held" in definition:
            return

        connection.execute("PRAGMA foreign_keys=OFF")
        try:
            connection.execute("BEGIN")
            connection.execute(
                """CREATE TABLE messages_migration_new(
                       id TEXT PRIMARY KEY, user_id TEXT NOT NULL, mailbox_id TEXT NOT NULL,
                       uid_validity TEXT NOT NULL DEFAULT '', imap_uid INTEGER NOT NULL,
                       subject TEXT NOT NULL, sender_name TEXT NOT NULL DEFAULT '',
                       sender_address TEXT NOT NULL DEFAULT '', received_at TEXT NOT NULL,
                       importance TEXT NOT NULL DEFAULT 'normal', message_key TEXT,
                       body BLOB NOT NULL,
                       status TEXT NOT NULL DEFAULT 'pending'
                           CHECK(status IN ('pending','processing','sent','failed','skipped','held')),
                       skip_reason TEXT NOT NULL DEFAULT '',
                       attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT,
                       last_error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                       UNIQUE(mailbox_id, uid_validity, imap_uid))"""
            )
            existing = {item[1] for item in connection.execute("PRAGMA table_info(messages)")}
            wanted = ("id", "user_id", "mailbox_id", "uid_validity", "imap_uid", "subject", "sender_name",
                      "sender_address", "received_at", "importance", "message_key", "body", "status",
                      "skip_reason", "attempts", "next_attempt_at", "last_error", "created_at")
            shared = [name for name in wanted if name in existing]
            columns = ",".join(shared)
            connection.execute(
                f"INSERT INTO messages_migration_new({columns}) SELECT {columns} FROM messages"
            )
            connection.execute("DROP TABLE messages")
            connection.execute("ALTER TABLE messages_migration_new RENAME TO messages")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_due ON messages(status, next_attempt_at)"
            )
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_message_same_mail
                   ON messages(user_id, message_key) WHERE message_key IS NOT NULL"""
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.execute("PRAGMA foreign_keys=ON")

    def create_user(self, email: str, password_hash: str, invite_hash: str = "",
                    *, signup_extras: Optional[dict[str, str]] = None,
                    max_users: Optional[int] = None) -> dict[str, Any]:
        """Create one account.

        ``invite_hash`` is **optional since 2026-09-22**: the invite system was
        dropped and registration is open, so the normal caller passes ``""`` and
        no invite row is touched at all. A non-empty value still takes the old
        path (it claims the code atomically) — the data layer keeps the ability
        because the codes that already exist stay meaningful, while no user
        interface offers it any more. See `docs/open-registration-2026-09-22.md`.

        ``signup_extras`` is the three **optional** things the register form asks
        for (怎么称呼你 / 你的身份 / 最想先解决什么). They are written into
        ``profiles`` and — deliberately — are **not** in ``upsert_profile``'s
        allow-list, so a later 「只改一项」 save cannot silently clear them
        (铁律 4). Absent or empty means "he skipped them", which is the normal
        case and must keep working.
        """
        user_id = new_id("usr")
        now = utc_now()
        address = email.strip().lower()
        with self.connect() as connection:
            if invite_hash:
                invite = connection.execute(
                    "SELECT * FROM invites WHERE code_hash=? AND used_by IS NULL AND expires_at>?",
                    (invite_hash, now),
                ).fetchone()
                if not invite:
                    raise ValueError("邀请码无效、已使用或已过期。")
            # 这个邮箱已经有账号了。**必须先问，不能靠 INSERT 去撞唯一约束**：
            # 撞上去抛的是 `sqlite3.IntegrityError`，而 `web.register` 只把
            # `ValueError` 翻成 400，于是用户看到的是「服务器内部错误」——
            # 2026-09-19 生产上真的这么报了两次（拿自己已注册的邮箱又点了一次注册）。
            # `users.email` 是 UNIQUE COLLATE NOCASE，所以这里也要按 NOCASE 找。
            taken = connection.execute(
                "SELECT status FROM users WHERE email=? COLLATE NOCASE", (address,)).fetchone()
            if taken:
                raise ValueError(_email_taken_message(str(taken["status"])))
            # 名额（传了 `max_users` 才管）：**一条语句里同时数名额与插行**。
            # 先 `SELECT COUNT(*)` 再 INSERT 是不够的——`SELECT` 不拿写锁，中间那一段窗口
            # 真的能被并发注册穿过去（实测 6 个线程抢 1 个名额，3 个成功；
            # 判据见 `test_registration_race.CapacityRaceTests`）。这一条是原子的：
            # 写语句执行时 SQLite 持有写锁，子查询在同一把锁下求值。
            insert_sql = "INSERT INTO users(id,email,password_hash,created_at) VALUES(?,?,?,?)"
            insert_params: tuple = (user_id, address, password_hash, now)
            if max_users is not None:
                # 子查询里的谓词必须与 `count_users()`（面板与快路径看到的那个数）
                # **逐字一致**：那边数的是 `status!='deleted'`。
                # 2026-09-24 宿舍机复验时发现这里原来数的是**全部行**——现在不可达（删除是真
                # DELETE，库里没有软删行），但一旦有软删行，就会变成「面板说有位置、注册 403」。
                insert_sql = ("INSERT INTO users(id,email,password_hash,created_at) "
                              "SELECT ?,?,?,? WHERE (SELECT COUNT(*) FROM users "
                              "WHERE status!='deleted') < ?")
                insert_params = (user_id, address, password_hash, now, int(max_users))
            try:
                inserted = connection.execute(insert_sql, insert_params)
                if max_users is not None and inserted.rowcount != 1:
                    raise CapacityFull("当前名额已满。")
            except sqlite3.IntegrityError as exc:
                # 兜底：两个人同一瞬间拿同一个邮箱注册时，上面那次检查会双双通过，
                # 唯一约束才是最后一道。这里必须给同一句话，不能再变成 500。
                raise ValueError(_email_taken_message("active")) from exc
            extras = signup_extras or {}
            connection.execute(
                """INSERT INTO profiles(user_id,signup_nickname,signup_identity,
                                       signup_goals,updated_at) VALUES(?,?,?,?,?)""",
                (user_id, str(extras.get("nickname") or ""), str(extras.get("identity") or ""),
                 str(extras.get("goals") or ""), now),
            )
            if invite_hash:
                # **认领邀请码必须是原子的**：上面那次 SELECT 只是给人一句好话，它挡不住并发——
                # 两个请求可以在对方提交之前双双读到「这张码没用过」，于是一张码开出两个账号。
                # 所以真正的判据是这条**带条件**的 UPDATE 的 rowcount：抢不到就抛，
                # 抛出去会把这一整个事务回滚（包括刚插进去的那个用户），码仍然属于抢先的那个人。
                # 位置也不能提前：`invites.used_by` 有指向 `users(id)` 的外键，用户行必须先存在。
                claimed = connection.execute(
                    "UPDATE invites SET used_by=?,used_at=? WHERE code_hash=? AND used_by IS NULL",
                    (user_id, now, invite_hash),
                )
                if claimed.rowcount != 1:
                    raise ValueError("邀请码无效、已使用或已过期。")
        return self.get_user(user_id)

    def get_user(self, user_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id,email,status,created_at FROM users WHERE id=? AND status!='deleted'", (user_id,)
            ).fetchone()
        if not row:
            raise KeyError("用户不存在。")
        return dict(row)

    def find_user_for_login(self, email: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE email=? COLLATE NOCASE AND status!='deleted'", (email.strip(),)
            ).fetchone()
        return dict(row) if row else None

    def count_users(self) -> int:
        with self.connect() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM users WHERE status!='deleted'").fetchone()[0])

    def upsert_profile(self, user_id: str, values: dict[str, Any]) -> None:
        allowed = {
            "school_email", "major", "year_of_study", "courses_json", "interests_json", "career_goals_json",
            "focus_topics_json", "less_interested_json", "custom_instructions", "language",
            "timezone", "immediate_enabled", "daily_enabled", "daily_time",
            "theme", "background", "report_mode",
        }
        selected = {key: value for key, value in values.items() if key in allowed}
        if not selected:
            return
        selected["updated_at"] = utc_now()
        assignments = ",".join(f"{key}=?" for key in selected)
        with self.connect() as connection:
            cursor = connection.execute(
                f"UPDATE profiles SET {assignments} WHERE user_id=?",
                (*selected.values(), user_id),
            )
            if cursor.rowcount != 1:
                raise KeyError("用户资料不存在。")

    def get_profile(self, user_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM profiles WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            raise KeyError("用户资料不存在。")
        value = dict(row)
        for key in ("courses_json", "interests_json", "career_goals_json", "focus_topics_json", "less_interested_json"):
            value[key.removesuffix("_json")] = json.loads(value.pop(key) or "[]")
        return value

    # ------------------------------------------------------- background photo

    def set_background_image(self, user_id: str, media_type: str, data: bytes) -> int:
        """Store one background photo per user, replacing any previous one.

        The revision counter is not bookkeeping for its own sake: it is what the
        frontend puts in the URL (`?v=3`) so a replaced image is actually
        re-fetched instead of being served from the browser's cache under an
        unchanged address.
        """
        now = utc_now()
        with self.connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM profiles WHERE user_id=?", (user_id,)
            ).fetchone()
            if not exists:
                raise KeyError("用户资料不存在。")
            connection.execute(
                """INSERT INTO background_images(user_id,media_type,bytes,rev,created_at,updated_at)
                   VALUES(?,?,?,1,?,?)
                   ON CONFLICT(user_id) DO UPDATE SET
                     media_type=excluded.media_type,
                     bytes=excluded.bytes,
                     rev=background_images.rev+1,
                     updated_at=excluded.updated_at""",
                (user_id, media_type, data, now, now),
            )
            row = connection.execute(
                "SELECT rev FROM background_images WHERE user_id=?", (user_id,)
            ).fetchone()
        return int(row["rev"])

    def clear_background_image(self, user_id: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM background_images WHERE user_id=?", (user_id,))

    def get_background_image(self, user_id: str) -> Optional[dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT media_type,bytes,rev,updated_at FROM background_images WHERE user_id=?",
                (user_id,),
            ).fetchone()
        return dict(row) if row else None

    def background_summary(self, user_id: str) -> dict[str, Any]:
        """What /api/me may say about the photo: never the bytes themselves."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT media_type,rev,length(bytes) AS size,updated_at FROM background_images WHERE user_id=?",
                (user_id,),
            ).fetchone()
        if not row:
            return {"present": False, "rev": 0, "media_type": "", "size": 0, "updated_at": ""}
        return {"present": True, **dict(row)}


    def upsert_mailbox(self, user_id: str, values: dict[str, Any]) -> str:
        now = utc_now()
        mailbox_id = new_id("mbx")
        with self.connect() as connection:
            current = connection.execute("SELECT * FROM mailboxes WHERE user_id=?", (user_id,)).fetchone()
            if current:
                mailbox_id = str(current["id"])
                identity_changed = (
                    str(current["email"]).strip().lower() != str(values["email"]).strip().lower()
                    or str(current["imap_host"]).strip().lower() != str(values["imap_host"]).strip().lower()
                    or int(current["imap_port"]) != int(values["imap_port"])
                )
                if identity_changed:
                    # UIDs are scoped to one IMAP mailbox. Reusing the old
                    # cursor or message unique keys for a different mailbox can
                    # skip mail. Reports remain for the user's history because
                    # reports.message_id uses ON DELETE SET NULL.
                    connection.execute("DELETE FROM messages WHERE mailbox_id=?", (mailbox_id,))
                connection.execute(
                    """UPDATE mailboxes SET email=?,report_to=?,imap_host=?,imap_port=?,smtp_host=?,smtp_port=?,
                       encrypted_password=?,enabled=?,uid_validity=?,last_uid=?,last_polled_at=?,last_error=?,updated_at=?
                       WHERE id=? AND user_id=?""",
                    (values["email"], values["report_to"], values["imap_host"], values["imap_port"],
                     values["smtp_host"], values["smtp_port"], values["encrypted_password"],
                     int(values.get("enabled", True)),
                     "" if identity_changed else current["uid_validity"],
                     0 if identity_changed else current["last_uid"],
                     None if identity_changed else current["last_polled_at"],
                     "" if identity_changed else current["last_error"],
                     now, mailbox_id, user_id),
                )
            else:
                connection.execute(
                    """INSERT INTO mailboxes(id,user_id,email,report_to,imap_host,imap_port,smtp_host,smtp_port,
                       encrypted_password,enabled,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (mailbox_id, user_id, values["email"], values["report_to"], values["imap_host"],
                     values["imap_port"], values["smtp_host"], values["smtp_port"],
                     values["encrypted_password"], int(values.get("enabled", True)), now),
                )
        return mailbox_id

    def get_mailbox(self, user_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM mailboxes WHERE user_id=?", (user_id,)).fetchone()
        return dict(row) if row else None

    def upsert_connection(self, user_id: str, values: dict[str, Any]) -> str:
        now = utc_now()
        connection_id = new_id("con")
        with self.connect() as connection:
            current = connection.execute(
                "SELECT id FROM connections WHERE user_id=? AND kind=?", (user_id, values["kind"])
            ).fetchone()
            if current:
                connection_id = str(current["id"])
                connection.execute(
                    """UPDATE connections SET provider=?,model=?,base_url=?,encrypted_api_key=?,config_json=?,
                       enabled=?,updated_at=? WHERE id=? AND user_id=?""",
                    (values["provider"], values.get("model", ""), values.get("base_url", ""),
                     values["encrypted_api_key"], values.get("config_json", "{}"),
                     int(values.get("enabled", True)), now, connection_id, user_id),
                )
            else:
                connection.execute(
                    """INSERT INTO connections(id,user_id,kind,provider,model,base_url,encrypted_api_key,config_json,
                       enabled,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (connection_id, user_id, values["kind"], values["provider"], values.get("model", ""),
                     values.get("base_url", ""), values["encrypted_api_key"], values.get("config_json", "{}"),
                     int(values.get("enabled", True)), now),
                )
            # Saving a credential clears any breaker against it, in the same
            # transaction as the write. The breaker is a statement about ONE
            # credential ("this key is wrong"), so replacing that credential
            # retires the reason entirely -- and the user who just fixed their
            # key should not have to wait out a 30-minute window to find out
            # whether it worked. Doing it here rather than at the callers means
            # no save path can forget: both the self-service form and the admin
            # "edit another user" form come through this method.
            connection.execute(
                "DELETE FROM key_circuits WHERE user_id=? AND kind=?", (user_id, values["kind"])
            )
        return connection_id

    def get_connection(self, user_id: str, kind: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM connections WHERE user_id=? AND kind=?", (user_id, kind)
            ).fetchone()
        return dict(row) if row else None

    # -- bad-credential circuit breaker ---------------------------------------
    #
    # A wrong model key is not a transient problem: every message for that
    # account will fail, and it fails *after* the worker has spent a generation
    # slot on it. Left alone, one dead key keeps taking slots and keeps writing
    # "generation failed" rows, while its owner is told nothing.
    #
    # Two things keep this honest rather than clever:
    #
    #   * Only *credential-class* failures count. `service._generate_with_retry`
    #     is the single place that classifies them, and a timeout or a 429 or a
    #     dropped connection never reaches this table -- otherwise a provider
    #     having a bad afternoon would lock users out, a far worse bug than the
    #     one being fixed.
    #   * The messages are **deferred, never dropped**. `due_messages` stops
    #     handing them out while the breaker is open, so they stay `pending` with
    #     their original attempt count and run normally once it closes.

    def record_key_failure(self, user_id: str, kind: str, reason: str, *,
                           now: dt.datetime | None = None) -> dict[str, Any]:
        """Count one credential failure; open the breaker at the threshold.

        Returns the resulting state, because "we have now stopped trying for this
        account" is a decision the caller and the console need to be able to see,
        not merely a row that changed.
        """
        moment = now or dt.datetime.now(dt.timezone.utc)
        moment_text = moment.isoformat(timespec="seconds")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT failures FROM key_circuits WHERE user_id=? AND kind=?", (user_id, kind)
            ).fetchone()
            failures = int(row["failures"] if row else 0) + 1
            open_until = None
            if failures >= KEY_CIRCUIT_THRESHOLD:
                open_until = (moment + dt.timedelta(seconds=KEY_CIRCUIT_SECONDS)).isoformat(timespec="seconds")
            connection.execute(
                """INSERT INTO key_circuits(user_id,kind,failures,open_until,reason,updated_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(user_id,kind) DO UPDATE SET
                     failures=excluded.failures, open_until=excluded.open_until,
                     reason=excluded.reason, updated_at=excluded.updated_at""",
                (user_id, kind, failures, open_until, str(reason)[:1000], moment_text),
            )
        return {"user_id": user_id, "kind": kind, "failures": failures, "open_until": open_until}

    def clear_key_failures(self, user_id: str, kind: str) -> None:
        """One success -- or a replaced credential -- proves the reason is gone.

        A full reset rather than a step down, because `failures` counts
        *consecutive* failures: the point of the half-open probe is that an
        account which has genuinely recovered is back in service immediately.
        """
        with self.connect() as connection:
            connection.execute("DELETE FROM key_circuits WHERE user_id=? AND kind=?", (user_id, kind))

    def key_circuit_open(self, user_id: str, kind: str = "model", *,
                         now: dt.datetime | None = None) -> bool:
        """True while this account's generation is suspended."""
        moment = (now or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT open_until FROM key_circuits WHERE user_id=? AND kind=?", (user_id, kind)
            ).fetchone()
        return bool(row and row["open_until"] and str(row["open_until"]) > moment)

    def open_key_circuits(self, kind: str = "model", *,
                          now: dt.datetime | None = None) -> list[dict[str, Any]]:
        """Accounts suspended right now, with what to tell the operator."""
        moment = (now or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds")
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT c.user_id, c.kind, c.failures, c.open_until, c.reason, c.updated_at,
                          u.email
                   FROM key_circuits c LEFT JOIN users u ON u.id = c.user_id
                   WHERE c.kind=? AND c.open_until IS NOT NULL AND c.open_until > ?
                   ORDER BY c.open_until""",
                (kind, moment),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_connection_result(self, user_id: str, kind: str, *, error: str = "") -> None:
        """Remember whether the user's last explicit model/search test worked."""
        with self.connect() as connection:
            connection.execute(
                "UPDATE connections SET last_test_at=?,last_error=? WHERE user_id=? AND kind=?",
                (utc_now(), error[:1000], user_id, kind),
            )

    def create_session(self, user_id: str, digest: str, expires_at: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO sessions(token_hash,user_id,expires_at,created_at) VALUES(?,?,?,?)",
                (digest, user_id, expires_at, utc_now()),
            )

    def session_user(self, digest: str) -> dict[str, Any] | None:
        """The account behind a session token.

        ``is_admin`` is read here, on every authenticated request, rather than
        being decided at login. That is what makes a grant or a revocation take
        effect at once: an operator who has just been removed stops being one on
        their very next click, instead of keeping the rights until their session
        happens to expire.
        """
        now = utc_now()
        with self.connect() as connection:
            row = connection.execute(
                """SELECT u.id,u.email,u.status,u.created_at,u.is_admin,u.ui_locale
                     FROM sessions s JOIN users u ON u.id=s.user_id
                    WHERE s.token_hash=? AND s.expires_at>? AND u.status IN ('active','paused')""",
                (digest, now),
            ).fetchone()
        return dict(row) if row else None

    # 同一账号的活跃时间最多几分钟写一次。用 SQL 里的条件更新，而不是「先读再写」：
    # 没到间隔时这条 UPDATE 什么都不改（也不产生写放大），两个请求同时到达时也不会
    # 都以为自己该写。
    LAST_SEEN_MIN_GAP_SECONDS = 300

    def touch_last_seen(self, user_id: str, *, now: dt.datetime | None = None) -> None:
        """Mark this account as having just used the app.

        Whoever reads this value wants one thing from it: **发出去的那封提醒有没有把
        人叫回来**（`setup_reminders.panel_rows` 用它给出一句话的结论）。所以它记的
        是「用过应用」，不是「登录过」——一个人可以几周不重新登录而天天在用（会话
        还活着），只看登录时间会把他说成「没回来」，而那正是这次要分开的两种情况之一。
        """
        moment = now or dt.datetime.now(dt.timezone.utc)
        stamp = moment.isoformat(timespec="seconds")
        cutoff = (moment - dt.timedelta(
            seconds=self.LAST_SEEN_MIN_GAP_SECONDS)).isoformat(timespec="seconds")
        with self.connect() as connection:
            connection.execute(
                "UPDATE users SET last_seen_at=? WHERE id=? AND last_seen_at<?",
                (stamp, user_id, cutoff))

    #: 老 `profiles.language` 的两个历史取值 → 现在的语言代码。
    #: `bilingual`（默认值）**不在这里**：它的含义正是「中文正文 + 英文小结」，
    #: 与现在的中文那条完全一致，所以它落在兜底上 —— 存量用户的报告一个字都不变。
    LEGACY_REPORT_LANGUAGES = {"zh": "zh-Hans", "en": "en"}
    DEFAULT_REPORT_LANGUAGE = "zh-Hans"

    def report_locale(self, user_id: str) -> str:
        """这个人希望**报告**用哪种语言写（`pilot_app/i18n.py` 的语言代码）。

        2026-09-23 用户拍板：界面语言与报告语言**合并成一个设置**，所以这里先看
        `users.ui_locale`（那个切换器写的），再看老资料里的 `profiles.language`
        ——老取值仍然认，免得选了 English 的人因为这次合并被悄悄换回中文。
        """
        with self.connect() as connection:
            row = connection.execute(
                """SELECT u.ui_locale AS ui, p.language AS legacy
                     FROM users u LEFT JOIN profiles p ON p.user_id = u.id
                    WHERE u.id = ?""", (user_id,)).fetchone()
        if row is None:
            return self.DEFAULT_REPORT_LANGUAGE
        chosen = str(row["ui"] or "").strip()
        if chosen:
            return chosen
        return self.LEGACY_REPORT_LANGUAGES.get(str(row["legacy"] or "").strip(),
                                                self.DEFAULT_REPORT_LANGUAGE)

    def set_ui_locale(self, user_id: str, locale: str) -> None:
        """记住这个人选的界面语言（`pilot_app/i18n.py` 里的语言代码）。

        存账号而不是只存 cookie，是为了**换设备也记得**：在手机上选了繁体的人，
        不该在笔记本上再选一次。cookie 仍是第一道——未登录的人只能靠它。

        这里**不校验**语言是否在清单里：校验属于请求边界（`web.set_locale` 做），
        数据层收下一个将来才加的语言代码不该出错——清单是文件，可能先有数据、
        后有那一行。读的时候查不到就回落中文，那是 `i18n.catalog()` 的既有行为。
        """
        with self.connect() as connection:
            connection.execute("UPDATE users SET ui_locale=? WHERE id=?", (locale, user_id))

    # ------------------------------------------------------------ operators

    def grant_admin(self, email: str) -> dict[str, Any]:
        """Give an existing account operator rights. Returns the account row.

        Only an account that already exists can be granted, and that is a
        deliberate restriction rather than an oversight. Accepting an address
        nobody has registered yet would make the grant a standing promise:
        whoever later signed up with a mistyped address would silently become an
        operator, and nobody would find out until they used it.
        """
        address = str(email or "").strip()
        if not address:
            raise ValueError("请填写邮箱。")
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id,email,status,is_admin FROM users WHERE email=? COLLATE NOCASE", (address,)
            ).fetchone()
            if row is None:
                raise KeyError("这个邮箱还没有注册过。先给他一个邀请码，注册之后再授予管理员。")
            if row["status"] == "deleted":
                raise ValueError("这个账号已经删除。")
            if int(row["is_admin"]):
                return dict(row)
            connection.execute("UPDATE users SET is_admin=1 WHERE id=?", (row["id"],))
        return {"id": row["id"], "email": row["email"], "status": row["status"], "is_admin": 1}

    def revoke_admin(self, user_id: str) -> dict[str, Any]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id,email,status,is_admin FROM users WHERE id=?", (user_id,)
            ).fetchone()
            if row is None:
                raise KeyError("账号不存在。")
            connection.execute("UPDATE users SET is_admin=0 WHERE id=?", (user_id,))
        return {"id": row["id"], "email": row["email"], "status": row["status"], "is_admin": 0}

    def database_admins(self) -> list[dict[str, Any]]:
        """Accounts granted operator rights from the console, newest first."""
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT id,email,status,created_at FROM users
                    WHERE is_admin=1 AND status!='deleted' ORDER BY email"""
            ).fetchall()
        return [dict(row) for row in rows]

    def count_admin_capable(self) -> int:
        """Accounts that are or could be operators right now.

        Used to refuse an action that would leave nobody able to administer the
        instance. Env-var operators are added by the caller, which is the only
        place that knows about them.
        """
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM users WHERE is_admin=1 AND status='active'").fetchone()
        return int(row["n"]) if row else 0

    def delete_session(self, digest: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash=?", (digest,))

    def list_reports(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM reports WHERE user_id=? ORDER BY created_at DESC LIMIT ?", (user_id, min(limit, 100))
            ).fetchall()
        return [dict(row) for row in rows]

    def export_user_data(self, user_id: str) -> dict[str, Any]:
        """Everything we hold about one user, minus every secret.

        The right to see your own data is only real if you can actually take it
        away, so this backs the "export" button. Two categories are deliberately
        absent: the encrypted mailbox password and API key blobs (they are
        credentials, not personal data the user needs back, and shipping
        ciphertext would undo the point of encrypting it), and message bodies
        (already deleted for delivered mail and never stored for skipped mail).

        Report bodies are included: they are the user's own content.
        """
        with self.connect() as connection:
            user = connection.execute(
                "SELECT id,email,status,created_at FROM users WHERE id=?", (user_id,)
            ).fetchone()
            if user is None:
                raise KeyError("用户不存在。")
            profile = connection.execute(
                "SELECT * FROM profiles WHERE user_id=?", (user_id,)
            ).fetchone()
            # The background photo is the user's own content, so it belongs in
            # the export like a report body does. It rides as base64 because the
            # export is JSON; a megabyte of it base64s to about 1.3 MB, which is a
            # fine price for an action the user takes on purpose, and far better
            # than an export that quietly omits something they uploaded.
            background = connection.execute(
                "SELECT media_type,bytes,rev,updated_at FROM background_images WHERE user_id=?",
                (user_id,),
            ).fetchone()

            mailboxes = connection.execute(
                """SELECT email,report_to,imap_host,imap_port,smtp_host,smtp_port,enabled,
                          last_polled_at,last_error,updated_at
                   FROM mailboxes WHERE user_id=? ORDER BY updated_at""", (user_id,)
            ).fetchall()
            connections = connection.execute(
                """SELECT kind,provider,model,base_url,enabled,last_test_at,last_error,updated_at
                   FROM connections WHERE user_id=? ORDER BY kind""", (user_id,)
            ).fetchall()
            messages = connection.execute(
                """SELECT id,subject,sender_name,sender_address,received_at,status,skip_reason,
                          importance,created_at
                   FROM messages WHERE user_id=? ORDER BY received_at""", (user_id,)
            ).fetchall()
            reports = connection.execute(
                """SELECT id,message_id,kind,subject,body_markdown,status,sent_to,report_date,
                          created_at,sent_at
                   FROM reports WHERE user_id=? ORDER BY created_at""", (user_id,)
            ).fetchall()
            feedback = connection.execute(
                "SELECT report_id,rating,note,created_at FROM feedback WHERE user_id=?",
                (user_id,),
            ).fetchall()
            tasks = connection.execute(
                """SELECT task_key,state,task_day,subject,action,deadline,priority,sender,
                          done_at,updated_at
                   FROM task_states WHERE user_id=? ORDER BY task_day, updated_at""", (user_id,)
            ).fetchall()
        return {
            "user": dict(user),
            "profile": dict(profile) if profile else None,
            "background_image": (
                {
                    "media_type": background["media_type"],
                    "rev": background["rev"],
                    "updated_at": background["updated_at"],
                    "encoding": "base64",
                    "data": base64.b64encode(background["bytes"]).decode("ascii"),
                }
                if background else None
            ),
            "mailboxes": [dict(row) for row in mailboxes],
            "connections": [dict(row) for row in connections],
            "messages": [dict(row) for row in messages],
            "reports": [dict(row) for row in reports],
            "feedback": [dict(row) for row in feedback],
            "task_states": [dict(row) for row in tasks],
        }

    # ------------------------------------------------------ pilot applications

    def create_signup_request(self, email: str, note: str = "", client: str = "",
                              nickname: str = "", identity: str = "",
                              goals: str = "") -> tuple[dict[str, Any], bool]:
        """Record a request for a pilot account. Returns (row, already_pending).

        Never raises for an address that already asked: telling someone "we
        already have your request" is useful, and the alternative (silently
        dropping it) leaves them thinking the form is broken.

        ``nickname``/``identity``/``goals`` 是 v1.0.1 起申请表单上的三个**选填**项。
        它们与 ``note`` 一样只是**给运营者看**的信息：不参与任何判定，也不进任何
        权限路径——批准与否仍然只由人点。长度在这里再截一次，因为这是最后一道
        写库的地方（路由层已经校验过，但 `manage` 或将来别的调用方可能绕过它）。
        """
        address = str(email or "").strip().lower()[:254]
        if not address:
            raise ValueError("请填写邮箱。")
        text = str(note or "").strip()[:500]
        nickname = str(nickname or "").strip()[:40]
        identity = str(identity or "").strip()[:20]
        goals = str(goals or "").strip()[:120]
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM signup_requests WHERE email=? AND status='pending'", (address,)
            ).fetchone()
            if existing:
                return dict(existing), True
            request_id = new_id("sgn")
            connection.execute(
                """INSERT INTO signup_requests(id,email,note,nickname,identity,goals,
                                             status,created_at,client)
                   VALUES(?,?,?,?,?,?,'pending',?,?)""",
                (request_id, address, text, nickname, identity, goals,
                 utc_now(), str(client or "")[:64]),
            )
            row = connection.execute("SELECT * FROM signup_requests WHERE id=?", (request_id,)).fetchone()
        return dict(row), False

    # -- the public message board ------------------------------------------
    #
    # Shape limits live here rather than in the request handler so that nothing
    # can reach the table with a longer body than the page promises to accept.
    # Over-length input is *refused*, never truncated: a cut-off sentence that
    # still gets published is worse than a rejection, because the author has no
    # way to tell that half of what they wrote was dropped.

    def create_guest_message(self, *, body: str, nickname: str = "", sealed_email: bytes = b"",
                             client_hash: str = "") -> dict[str, Any]:
        text = str(body or "").strip()
        if not text:
            raise ValueError("请先写点什么。")
        if len(text) > GUEST_BODY_LIMIT:
            raise ValueError(f"留言最多 {GUEST_BODY_LIMIT} 字，现在是 {len(text)} 字。")
        name = str(nickname or "").strip()
        if len(name) > GUEST_NICKNAME_LIMIT:
            raise ValueError(f"昵称最多 {GUEST_NICKNAME_LIMIT} 个字。")
        # Structural, not a comment: the column holds ciphertext, so a caller
        # cannot store a readable address even by accident. That is the shape
        # this project prefers for privacy rules -- the first version of this
        # method stringified the blob (`str(b"v1:...")`) and produced a row that
        # could never be decrypted again.
        if sealed_email and not isinstance(sealed_email, (bytes, bytearray)):
            raise ValueError("邮箱必须先加密再入库。")
        sealed = bytes(sealed_email or b"")
        message_id = new_id("msg")
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO guest_messages(id,body,nickname,email,client_hash,status,created_at)
                   VALUES(?,?,?,?,?,'pending',?)""",
                (message_id, text, name, sealed, str(client_hash or ""), utc_now()),
            )
            row = connection.execute("SELECT * FROM guest_messages WHERE id=?", (message_id,)).fetchone()
        return dict(row)

    def guest_messages(self, *, status: str = "", limit: int = 200) -> list[dict[str, Any]]:
        """Rows for the console. A blank status means everything, newest first."""
        query = "SELECT * FROM guest_messages"
        params: list[Any] = []
        if status:
            query += " WHERE status=?"
            params.append(status)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit or 200), 500)))
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(query, params)]

    def published_guest_messages(self, limit: int = 20) -> list[dict[str, Any]]:
        """What the landing page may show: published, newest first.

        Nothing else filters status for the public path, so this is the single
        place that decides what a stranger can read. `deleted` is a status rather
        than a DELETE so an operator's removal is visible in the console instead
        of the row silently vanishing.
        """
        return self.guest_messages(status="published", limit=limit)

    def set_guest_message_status(self, message_id: str, status: str,
                                 *, actor: str = "") -> dict[str, Any]:
        if status not in {"pending", "published", "rejected", "deleted"}:
            raise ValueError("未知的状态。")
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM guest_messages WHERE id=?", (message_id,)).fetchone()
            if row is None:
                raise KeyError(message_id)
            connection.execute(
                "UPDATE guest_messages SET status=?, decided_at=?, decided_by=? WHERE id=?",
                (status, utc_now(), str(actor or ""), message_id),
            )
            updated = connection.execute("SELECT * FROM guest_messages WHERE id=?", (message_id,)).fetchone()
        return dict(updated)

    def delete_guest_message(self, message_id: str) -> None:
        """Really remove one row. Used by the operator's 「删除」, and by the
        retention story: the privacy page says messages are kept until deleted,
        so there has to be a way to delete one."""
        with self.connect() as connection:
            connection.execute("DELETE FROM guest_messages WHERE id=?", (message_id,))

    # -- visitor statistics -------------------------------------------------
    #
    # Read and written by analytics.py. Nothing here ever stores an address:
    # `client_hash` arrives already digested by the caller, and the country
    # columns are resolved at insert time while the address is still in memory.

    def record_page_view(self, *, created_at: str, path: str, status: int = 200,
                         referrer: str = "", client_hash: str = "", country: str = "",
                         country_name: str = "", city: str = "", country_continent: str = "",
                         bot: bool = False, member: bool = False, admin: bool = False,
                         source: str = "live") -> str:
        view_id = new_id("pv")
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO page_views(id,created_at,path,status,referrer,client_hash,
                       country,country_name,city,continent,bot,member,admin,source)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (view_id, moment(created_at), str(path)[:300], int(status or 0),
                 str(referrer)[:120], str(client_hash)[:64], str(country)[:8],
                 str(country_name)[:60], str(city)[:80], str(country_continent)[:8],
                 1 if bot else 0, 1 if member else 0, 1 if admin else 0, str(source)[:16]),
            )
        return view_id

    def record_page_views(self, rows: list[dict[str, Any]]) -> tuple[int, int]:
        """Insert many imported visits in one transaction. Returns (stored, duplicates).

        One connection per row was the first version, and it lost two thirds of a
        1,919-row import on the live box: each insert was its own transaction
        racing the running web process, failures were swallowed by the caller, and
        the summary still said "done". One transaction for the batch is faster,
        atomic, and gives a number the caller can check.

        ``INSERT OR IGNORE`` plus the partial unique index makes a repeated import
        a no-op rather than a doubling -- and the row count is reported back so
        "nothing new" is visible instead of silent.
        """
        if not rows:
            return (0, 0)
        with self.connect() as connection:
            before = connection.execute("SELECT COUNT(*) FROM page_views").fetchone()[0]
            connection.executemany(
                """INSERT OR IGNORE INTO page_views(id,created_at,path,status,referrer,client_hash,
                       country,country_name,city,continent,bot,member,admin,source)
                   VALUES(:id,:created_at,:path,:status,:referrer,:client_hash,
                          :country,:country_name,:city,:continent,:bot,:member,:admin,:source)""",
                rows,
            )
            after = connection.execute("SELECT COUNT(*) FROM page_views").fetchone()[0]
        stored = int(after - before)
        return (stored, len(rows) - stored)

    def page_view_totals(self, *, offset: str = "+8 hours", days: int = 1) -> dict[str, Any]:
        """Views, unique visitors, robots and members over the last `days` days.

        "Unique visitors" counts distinct digests, so it is a count of *browsers
        seen from one address*, not of people: two people behind one NAT are one
        visitor, and a phone that changes address between pages is two. The
        panel says "估算" for exactly this reason.
        """
        since = self._analytics_since(offset=offset, days=days)
        with self.connect() as connection:
            row = connection.execute(
                """SELECT COUNT(*) AS pv,
                          COUNT(DISTINCT client_hash) AS uv,
                          -- COALESCE：窗口里一行都没有时 SUM 回的是 NULL，面板上
                          -- 就会印出「null 次」——空窗口该读作 0。
                          COALESCE(SUM(CASE WHEN bot=0 THEN 1 ELSE 0 END), 0) AS human_pv,
                          COALESCE(SUM(CASE WHEN bot=1 THEN 1 ELSE 0 END), 0) AS bot_pv,
                          COUNT(DISTINCT CASE WHEN bot=0 THEN client_hash END) AS human_uv,
                          COALESCE(SUM(CASE WHEN member=1 AND bot=0 THEN 1 ELSE 0 END), 0) AS member_pv
                     FROM page_views WHERE created_at >= ? AND admin=0""",
                (since,),
            ).fetchone()
        return dict(row) if row else {"pv": 0, "uv": 0, "human_pv": 0, "bot_pv": 0,
                                      "human_uv": 0, "member_pv": 0}

    # The only columns the panel may group by. A whitelist rather than a
    # parameter that is trusted: the column name cannot be bound as a value, and
    # string-building SQL from a query parameter is how injection starts.
    _ANALYTICS_GROUPS = {
        "path": "path",
        "referrer": "referrer",
        "country": "country_name",
        "city": "city",
    }

    def page_view_breakdown(self, group: str, *, offset: str = "+8 hours", days: int = 7,
                            limit: int = 12, humans_only: bool = True) -> list[dict[str, Any]]:
        column = self._ANALYTICS_GROUPS.get(str(group or ""))
        if column is None:
            raise ValueError("未知的分组。")
        since = self._analytics_since(offset=offset, days=days)
        where = "created_at >= ? AND admin=0 AND " + column + " <> ''"
        params: list[Any] = [since]
        if humans_only:
            where += " AND bot=0"
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT {column} AS label, COUNT(*) AS views,
                           COUNT(DISTINCT client_hash) AS visitors
                      FROM page_views WHERE {where}
                     GROUP BY {column} ORDER BY views DESC LIMIT ?""",
                (*params, max(1, min(int(limit or 12), 50))),
            ).fetchall()
        return [dict(row) for row in rows]

    def page_view_daily(self, *, offset: str = "+8 hours", days: int = 14) -> list[dict[str, Any]]:
        """One row per local day, oldest first, for the little bar chart."""
        since = self._analytics_since(offset=offset, days=days)
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT date(created_at, ?) AS day,
                           SUM(CASE WHEN bot=0 THEN 1 ELSE 0 END) AS human_pv,
                           COUNT(DISTINCT CASE WHEN bot=0 THEN client_hash END) AS human_uv,
                           SUM(CASE WHEN bot=1 THEN 1 ELSE 0 END) AS bot_pv
                      FROM page_views WHERE created_at >= ? AND admin=0
                     GROUP BY day ORDER BY day ASC""",
                (offset, since),
            ).fetchall()
        return [dict(row) for row in rows]

    def purge_operator_page_views(self, client_hash: str = "") -> int:
        """删掉运营者自己的访问记录：打过 admin 标记的，以及来自他现在这个 IP 摘要的。

        历史行没有办法事后分辨谁是运营者（当时还没记这个标记），但同一把摘要能
        认出「这个地址」——所以按钮删的是「admin=1 或 你这个地址」，并如实说出删了
        几条。别人的记录一行都不会碰。

        删掉的摘要会记进 `page_view_ignored`：导入的去重键就是
        (时间, 页面, 摘要)，只删行的话，下一次 `analytics-import-nginx` 会把他刚
        清掉的历史原样搬回来，而删除看起来像没生效。
        """
        wanted = str(client_hash or "")
        with self.connect() as connection:
            # 他现在这个地址无论眼下有没有行都要记下来：日志导入会带来库里本来
            # 就没有的历史，那正是「删了以后又冒出来」最容易发生的时刻。
            forgotten = {
                row["client_hash"]
                for row in connection.execute(
                    "SELECT DISTINCT client_hash FROM page_views WHERE admin=1 AND client_hash<>''"
                )
            }
            if wanted:
                forgotten.add(wanted)
            if forgotten:
                connection.executemany(
                    "INSERT OR IGNORE INTO page_view_ignored(client_hash, created_at) VALUES(?,?)",
                    [(value, utc_now()) for value in forgotten],
                )
            removed = connection.execute(
                "DELETE FROM page_views WHERE admin=1 OR (client_hash<>'' AND client_hash=?)",
                (wanted,),
            ).rowcount
        return int(removed or 0)

    def ignored_page_view_clients(self) -> set[str]:
        """清过的运营者地址摘要；`analytics-import-nginx` 按它跳过。"""
        with self.connect() as connection:
            return {
                row["client_hash"]
                for row in connection.execute("SELECT client_hash FROM page_view_ignored")
                if row["client_hash"]
            }

    def purge_page_views(self, before: str) -> int:
        """Delete visits older than the retention window. Returns how many went."""
        with self.connect() as connection:
            cursor = connection.execute("DELETE FROM page_views WHERE created_at < ?", (moment(before),))
            return int(cursor.rowcount or 0)

    def _analytics_since(self, *, offset: str, days: int) -> str:
        """The UTC instant that starts the window, counted in local days.

        `days=1` must mean "today where the operator lives", so the cutoff is
        local midnight rather than "24 hours ago": a rolling window would make
        the "today" figure change every time the panel was refreshed.
        """
        span = max(1, min(int(days or 1), 400))
        local_now = dt.datetime.now(dt.timezone.utc) + analytics_offset(offset)
        local_midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = local_midnight - dt.timedelta(days=span - 1)
        return (start - analytics_offset(offset)).isoformat(timespec="seconds")

    @staticmethod
    def invite_send_failed(row: dict[str, Any]) -> bool:
        """The code exists but the e-mail carrying it did not go out.

        Deliberately *not* the same as "no invite_sent_at": the operator can choose
        to skip the e-mail and hand the code over themselves (v0.27.0), and that
        leaves no timestamp either. Only a recorded error means something went
        wrong -- and that is the case where nobody will ever tell the applicant,
        because on their side nothing happened at all.
        """
        return (bool(row.get("invite_label"))
                and not row.get("invite_sent_at")
                and bool(row.get("invite_send_error")))

    def failed_invite_sends(self, limit: int = 100) -> list[dict[str, Any]]:
        """Approved applications whose invite e-mail failed to send.

        Shared by the console command and the sentinel so the two cannot disagree
        about who is waiting for a code.
        """
        return [row for row in self.list_signup_requests(limit)
                if self.invite_send_failed(row)]

    def list_signup_requests(self, limit: int = 100) -> list[dict[str, Any]]:
        """Applications, each joined to what became of the code that was issued.

        The invite is matched by label rather than by a stored id because the
        label is what the approval path already writes, and an invite row is the
        only place that knows whether the code was ever used. `used_by` is the
        strongest evidence available to us that the e-mail arrived: a code that
        was redeemed was, by definition, read by the person it was sent to.

        The join is deliberately one-way and read-only. Nothing here writes a
        delivery state that we cannot actually observe -- "the recipient's server
        accepted it" and "a human used it" are the two facts, and they are kept
        distinguishable.

        v0.63.72 adds two more columns, and both answer the same question from a
        different side. `registered_at` is **the account**, not the code: a
        re-issued code moves `invite_label` to the newest one, so a person who
        registered with an earlier code would otherwise read as 「尚未被使用」.
        `resend_count`/`resend_last_at` are the self-service requests -- the one
        fact about delivery that the applicant has and we do not.
        """
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT s.*,
                          i.used_by AS invite_used_by,
                          i.expires_at AS invite_expires_at,
                          i.used_at AS invite_used_at,
                          u.email AS redeemer_email,
                          u2.created_at AS registered_at,
                          (SELECT COUNT(*) FROM invite_resends r WHERE r.request_id = s.id)
                              AS resend_count,
                          (SELECT MAX(r.created_at) FROM invite_resends r WHERE r.request_id = s.id)
                              AS resend_last_at
                     FROM signup_requests s
                     LEFT JOIN invites i ON i.rowid = (
                         SELECT rowid FROM invites WHERE label = s.invite_label
                          ORDER BY expires_at DESC, rowid DESC LIMIT 1)
                     LEFT JOIN users u ON u.id = i.used_by
                     LEFT JOIN users u2 ON u2.email = s.email
                    WHERE s.invite_label <> ''
                       OR s.status = 'pending'
                    ORDER BY CASE s.status WHEN 'pending' THEN 0 ELSE 1 END, s.created_at DESC
                    LIMIT ?""", (max(1, min(int(limit), 500)),)
            ).fetchall()
        return [dict(row) for row in rows]

    def record_invite_email(self, request_id: str, *, sent: bool, error: str = "",
                            message_id: str = "") -> None:
        """Persist the outcome of one invite e-mail attempt.

        Written even on failure, and that is the point: an empty ``sent_at`` next
        to a non-empty error is the record that we tried and it did not work,
        which is a different thing from never having tried and needs a different
        response from whoever is looking.

        v0.63.72 also counts the attempt and stamps when it happened. The count is
        what makes an automatic retry terminate: without it the worker would keep
        re-issuing codes for an address whose mail server refuses us, once a
        minute, forever.
        """
        with self.connect() as connection:
            connection.execute(
                """UPDATE signup_requests
                      SET invite_sent_at=?, invite_send_error=?, invite_message_id=?,
                          invite_attempts=invite_attempts+1, invite_last_attempt_at=?
                    WHERE id=?""",
                (utc_now() if sent else "", str(error)[:200], str(message_id)[:200],
                 utc_now(), request_id),
            )

    # ------------------------------------------------------- 「没收到邀请码」

    def approved_signup_for(self, email: str) -> dict[str, Any] | None:
        """The newest **approved** application for this address, or None.

        This is the gate the self-service resend stands on, and it is deliberately
        narrow. It says nothing about whether the address exists, whether mail
        arrives, or what the operator thinks -- only that a human already pressed
        「发邀请码」 for this exact address. Everything that endpoint is allowed to
        do follows from that one fact, so it lives in one query in one place.
        """
        address = str(email or "").strip().lower()
        if not address:
            return None
        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM signup_requests
                    WHERE email=? AND status='invited'
                    ORDER BY decided_at DESC, created_at DESC LIMIT 1""", (address,)
            ).fetchone()
        return dict(row) if row else None

    def invite_eligible_for_resend(self, email: str) -> dict[str, Any] | None:
        """The application a self-service resend may act on, or None.

        Two conditions, each of which is a different person's situation:

        * **approved** -- a human already said yes (`approved_signup_for`);
        * **not registered** -- an account for this address means they got in, and
          another live code is a credential nobody needs. Note this asks about the
          *account*, not about the newest code: a re-issued code moves
          `invite_label`, so 「这张码没被用过」 is not the same question.
        """
        row = self.approved_signup_for(email)
        if row is None:
            return None
        if self.find_user_for_login(row["email"]):
            return None
        return row

    def queue_invite_resend(self, *, request_id: str, email: str, client_hash: str = "") -> str:
        """Record one self-service 「再发一次」. Returns the row id.

        Enqueued rather than sent here -- see the note on the table. The worker
        owns delivery, so the request path never touches SMTP.
        """
        resend_id = new_id("ires")
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO invite_resends(id, request_id, email, client_hash, created_at)
                   VALUES(?,?,?,?,?)""",
                (resend_id, request_id, str(email)[:254], str(client_hash)[:200], utc_now()),
            )
        return resend_id

    def open_invite_resends(self, limit: int = 20) -> list[dict[str, Any]]:
        """Queued self-service resends, oldest first."""
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM invite_resends WHERE handled_at=''
                    ORDER BY created_at ASC LIMIT ?""", (max(1, min(int(limit), 200)),)
            ).fetchall()
        return [dict(row) for row in rows]

    def finish_invite_resend(self, resend_id: str, outcome: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE invite_resends SET handled_at=?, outcome=? WHERE id=?",
                (utc_now(), str(outcome)[:200], resend_id),
            )

    def recent_invite_resends(self, email: str, hours: float = 24.0) -> int:
        """How many times this address has asked, inside the window.

        The second half of the rate limit: the first half is per client address
        (in memory, in web.py), and that one cannot see somebody asking from a
        phone, a laptop and a fresh browser profile.
        """
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=float(hours))
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS n FROM invite_resends WHERE email=? AND created_at>=?",
                (str(email or "").strip().lower(), since.isoformat(timespec="seconds")),
            ).fetchone()
        return int(row["n"] if row else 0)

    def signup_request_counts(self) -> dict[str, int]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS n FROM signup_requests GROUP BY status").fetchall()
        counts = {"pending": 0, "invited": 0, "declined": 0}
        for row in rows:
            counts[str(row["status"])] = int(row["n"])
        return counts

    def landing_user_count(self) -> int:
        """How many accounts the landing page may claim are using this.

        An account counts once it has an **enabled mailbox**, not when the row is
        created. Registering is a few seconds of work that commits nobody, and one
        of the four accounts on the pilot had done exactly that and nothing else;
        counting it would put a number on the public page that the product cannot
        back up. Paused and deleted accounts are out for the same reason.
        """
        with self.connect() as connection:
            row = connection.execute(
                """SELECT COUNT(DISTINCT m.user_id) AS n
                     FROM mailboxes m JOIN users u ON u.id = m.user_id
                    WHERE m.enabled = 1 AND u.status = 'active'""").fetchone()
        return int(row["n"]) if row else 0

    def get_signup_request(self, request_id: str) -> dict[str, Any]:
        """One application by id. Raises KeyError when there is no such row.

        Added in v0.63.72 because the re-send paths need the applicant's address
        *before* they decide anything -- minting a code for the wrong row is not a
        mistake that can be undone.
        """
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM signup_requests WHERE id=?", (request_id,)).fetchone()
        if row is None:
            raise KeyError("申请不存在。")
        return dict(row)

    def decide_signup_request(self, request_id: str, status: str, invite_label: str = "") -> dict[str, Any]:
        """Mark an application invited or declined. Idempotent on the same status."""
        if status not in {"invited", "declined", "pending"}:
            raise ValueError("无效的申请状态。")
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM signup_requests WHERE id=?", (request_id,)).fetchone()
            if row is None:
                raise KeyError("申请不存在。")
            connection.execute(
                "UPDATE signup_requests SET status=?,invite_label=?,decided_at=? WHERE id=?",
                (status, str(invite_label or "")[:200], utc_now() if status != "pending" else None, request_id),
            )
            updated = connection.execute("SELECT * FROM signup_requests WHERE id=?", (request_id,)).fetchone()
        return dict(updated)

    # --------------------------------------------------- task decisions ("handled")

    def task_states(self, user_id: str, day: str = "") -> dict[str, dict[str, Any]]:
        """The user's handled/open decisions, keyed by ``task_key``.

        With ``day`` set, only that local day. The live "today" view overlays
        these onto freshly derived tasks; the archive reads them directly.
        """
        clause = " AND task_day=?" if day else ""
        parameters: tuple[Any, ...] = (user_id, day) if day else (user_id,)
        with self.connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM task_states WHERE user_id=?{clause}", parameters).fetchall()
        return {row["task_key"]: dict(row) for row in rows}

    def task_day_summaries(self, user_id: str, limit: int = 30) -> list[dict[str, Any]]:
        """Local days with at least one recorded decision, newest first.

        Days where the user handled nothing are absent: an untended day has no
        decisions to archive, and the live view can still rebuild it from the
        reports on demand.
        """
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT task_day AS day,
                          COUNT(*) AS total,
                          SUM(CASE WHEN state='done' THEN 1 ELSE 0 END) AS done
                   FROM task_states
                   WHERE user_id=? AND task_day!=''
                   GROUP BY task_day ORDER BY task_day DESC LIMIT ?""",
                (user_id, max(1, min(int(limit), 200))),
            ).fetchall()
        return [dict(row) for row in rows]

    def set_task_state(self, user_id: str, task_key: str, state: str,
                       task: dict[str, Any] | None = None) -> dict[str, Any]:
        """Record "handled" (or reopen) one task. Returns the stored row.

        Reopening flips the state rather than deleting the row, so the archive
        stays truthful: "you handled 3 of 5 that day" must survive you changing
        your mind about one of them. Nothing here touches ``messages`` or
        ``reports`` — this table only ever records a choice.
        """
        if state not in {"done", "open"}:
            raise ValueError("无效的任务状态。")
        task = task or {}
        now = utc_now()
        done_at = now if state == "done" else None

        def keep(field: str, limit: int) -> str:
            return str(task.get(field) or "")[:limit]

        with self.connect() as connection:
            connection.execute(
                """INSERT INTO task_states(user_id,task_key,state,task_day,subject,action,deadline,
                                           priority,sender,message_id,done_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(user_id,task_key) DO UPDATE SET
                       state=excluded.state,
                       done_at=excluded.done_at,
                       updated_at=excluded.updated_at,
                       task_day=CASE WHEN excluded.task_day!='' THEN excluded.task_day ELSE task_states.task_day END,
                       subject=CASE WHEN excluded.subject!='' THEN excluded.subject ELSE task_states.subject END,
                       action=CASE WHEN excluded.action!='' THEN excluded.action ELSE task_states.action END,
                       deadline=CASE WHEN excluded.deadline!='' THEN excluded.deadline ELSE task_states.deadline END,
                       priority=CASE WHEN excluded.priority!='' THEN excluded.priority ELSE task_states.priority END,
                       sender=CASE WHEN excluded.sender!='' THEN excluded.sender ELSE task_states.sender END,
                       message_id=CASE WHEN excluded.message_id!='' THEN excluded.message_id ELSE task_states.message_id END""",
                (user_id, task_key, state, keep("task_day", 20), keep("subject", 300),
                 keep("action", 2000), keep("deadline", 100), keep("priority", 20),
                 keep("sender", 200), keep("message_id", 64), done_at, now),
            )
            row = connection.execute(
                "SELECT * FROM task_states WHERE user_id=? AND task_key=?", (user_id, task_key)
            ).fetchone()
        return dict(row)

    def set_task_priority(self, user_id: str, task_key: str, priority: str,
                          task: dict[str, Any] | None = None) -> dict[str, Any]:
        """Record the user's own ranking for one task; ``""`` clears it.

        A column of its own rather than reusing ``priority``: that one is the
        model's reading of the mail and the archive's snapshot of it, and
        overwriting it would make "the report said this was important" and "I
        decided it matters" indistinguishable afterwards.

        Creating the row here is safe for the same reason ``set_task_state``
        explains: the snapshot is written from the server's own derived task, and
        the state defaults to ``open`` because ranking something is not handling
        it. ``ON CONFLICT`` therefore must not touch ``state``/``done_at`` — a
        task that is already handled stays handled while you re-rank it.
        """
        if priority not in {"", "high", "medium", "low"}:
            raise ValueError("无效的优先级。")
        task = task or {}
        now = utc_now()

        def keep(field: str, limit: int) -> str:
            return str(task.get(field) or "")[:limit]

        with self.connect() as connection:
            connection.execute(
                """INSERT INTO task_states(user_id,task_key,state,task_day,subject,action,deadline,
                                           priority,user_priority,sender,message_id,done_at,updated_at)
                   VALUES(?,?,'open',?,?,?,?,?,?,?,?,NULL,?)
                   ON CONFLICT(user_id,task_key) DO UPDATE SET
                       user_priority=excluded.user_priority,
                       updated_at=excluded.updated_at,
                       task_day=CASE WHEN excluded.task_day!='' THEN excluded.task_day ELSE task_states.task_day END,
                       subject=CASE WHEN excluded.subject!='' THEN excluded.subject ELSE task_states.subject END,
                       action=CASE WHEN excluded.action!='' THEN excluded.action ELSE task_states.action END,
                       deadline=CASE WHEN excluded.deadline!='' THEN excluded.deadline ELSE task_states.deadline END,
                       priority=CASE WHEN excluded.priority!='' THEN excluded.priority ELSE task_states.priority END,
                       sender=CASE WHEN excluded.sender!='' THEN excluded.sender ELSE task_states.sender END,
                       message_id=CASE WHEN excluded.message_id!='' THEN excluded.message_id ELSE task_states.message_id END""",
                (user_id, task_key, keep("task_day", 20), keep("subject", 300),
                 keep("action", 2000), keep("deadline", 100), keep("priority", 20),
                 priority[:20], keep("sender", 200), keep("message_id", 64), now),
            )
            row = connection.execute(
                "SELECT * FROM task_states WHERE user_id=? AND task_key=?", (user_id, task_key)
            ).fetchone()
        return dict(row)

    def set_task_snooze(self, user_id: str, task_key: str, until: str,
                        task: dict[str, Any] | None = None) -> dict[str, Any]:
        """Send one task away until ``until`` (UTC ISO); ``""`` calls it back now.

        Same shape as :meth:`set_task_priority` and for the same reasons: the row
        is created from the server's own derived task (never from the request
        body), ``state`` is left alone -- being snoozed is not a second kind of
        done -- and there is **no timer anywhere**. "It comes back" is decided
        when the list is read, so a restart cannot lose the moment it was due.
        """
        task = task or {}
        now = utc_now()

        def keep(field: str, limit: int) -> str:
            return str(task.get(field) or "")[:limit]

        with self.connect() as connection:
            connection.execute(
                """INSERT INTO task_states(user_id,task_key,state,task_day,subject,action,deadline,
                                           priority,user_priority,snoozed_until,sender,message_id,
                                           done_at,updated_at)
                   VALUES(?,?,'open',?,?,?,?,?,?,?,?,?,NULL,?)
                   ON CONFLICT(user_id,task_key) DO UPDATE SET
                       snoozed_until=excluded.snoozed_until,
                       updated_at=excluded.updated_at,
                       task_day=CASE WHEN excluded.task_day!='' THEN excluded.task_day ELSE task_states.task_day END,
                       subject=CASE WHEN excluded.subject!='' THEN excluded.subject ELSE task_states.subject END,
                       action=CASE WHEN excluded.action!='' THEN excluded.action ELSE task_states.action END,
                       deadline=CASE WHEN excluded.deadline!='' THEN excluded.deadline ELSE task_states.deadline END,
                       priority=CASE WHEN excluded.priority!='' THEN excluded.priority ELSE task_states.priority END,
                       sender=CASE WHEN excluded.sender!='' THEN excluded.sender ELSE task_states.sender END,
                       message_id=CASE WHEN excluded.message_id!='' THEN excluded.message_id ELSE task_states.message_id END""",
                (user_id, task_key, keep("task_day", 20), keep("subject", 300),
                 keep("action", 2000), keep("deadline", 100), keep("priority", 20),
                 keep("user_priority", 20), str(until or "")[:40], keep("sender", 200),
                 keep("message_id", 64), now),
            )
            row = connection.execute(
                "SELECT * FROM task_states WHERE user_id=? AND task_key=?", (user_id, task_key)
            ).fetchone()
        return dict(row)

    # --------------------------------------------------------------- sessions

    def revoke_sessions(self, user_id: str, *, keep_digest: str | None = None) -> int:
        """Delete a user's sessions, optionally keeping the caller's own.

        Used both by "sign out all devices" and automatically after a password
        change: a stolen or borrowed device must lose access immediately, not
        when its 14-day cookie happens to expire.
        """
        with self.connect() as connection:
            if keep_digest:
                cursor = connection.execute(
                    "DELETE FROM sessions WHERE user_id=? AND token_hash!=?", (user_id, keep_digest)
                )
            else:
                cursor = connection.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        return cursor.rowcount or 0

    def count_sessions(self, user_id: str) -> int:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM sessions WHERE user_id=?", (user_id,)
            ).fetchone()
        return int(row[0]) if row else 0

    def set_password(self, user_id: str, password_hash: str) -> None:
        """Replace the password hash. Callers must revoke sessions themselves."""
        with self.connect() as connection:
            connection.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id))

    # ------------------------------------------------------------------ audit

    def record_audit(self, *, action: str, actor_user_id: str = "", actor_email: str = "",
                     target_user_id: str = "", target_email: str = "", detail: str = "",
                     client: str = "") -> str:
        audit_id = new_id("aud")
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO audit_log(id,created_at,actor_user_id,actor_email,action,
                       target_user_id,target_email,detail,client)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (audit_id, utc_now(), actor_user_id[:60], actor_email[:254], action[:60],
                 target_user_id[:60], target_email[:254], str(detail)[:500], client[:60]),
            )
        return audit_id

    # Message bodies are deliberately NOT part of this listing. They are erased
    # on delivery anyway (see finish_message), and "did it get processed and
    # delivered" is answerable from metadata alone — so the operator's console
    # never becomes a way to read other people's mail.
    # One SQL predicate per delivery state, kept in step with
    # ``web._delivery_state``: "delivered" is a property of the MESSAGE, not of
    # the stored report row. Messages migrated from the previous single-user
    # service carry status='sent' with no report record at all (41 of them on
    # 2026-09-14), and judging by the report row reported all of them as
    # undelivered — right in the database, wrong on screen.
    MESSAGE_FILTERS = {
        "all": "1=1",
        "sent": "(m.status='sent' OR r.status='sent')",
        "failed": "(m.status='failed' OR r.status='failed')",
        "skipped": "m.status='skipped'",
        # 处理成功、报告也生成了，但主人关掉了报告邮件：**不是"没送到"**。
        # 它必须能从 `undelivered` 里排除掉，否则"一键安静"的用户会在运营者面板上
        # 永远挂着一条看起来像故障的记录。
        "held": "m.status='held'",
        "pending": "m.status IN ('pending','processing')",
        # Everything that has neither reached the user nor been deliberately
        # skipped or held back: failures plus anything still in flight.
        "undelivered": ("m.status NOT IN ('skipped','sent','held')"
                        " AND (r.status IS NULL OR r.status!='sent')"),
    }

    # ----------------------------------------------------------- announcements

    def create_announcement(self, *, title: str, body: str, tone: str, deliver_email: bool,
                            created_by: str, image_id: str = "") -> str:
        """Publish one announcement, optionally queueing an email per user.

        Email is a queue, not a synchronous send: the worker owns outbound mail
        (it already has the retry and per-user error handling), so the operator's
        request returns immediately and a slow mailbox cannot make the console
        look broken.

        **没有 `is_public` 参数**（2026-09-24 起）：官网布告栏下线，一条公告不再有
        「给公众看」的那一半 —— 它只有站内广播这一种去向，所以也没有开关可给。
        表上那两列按上面的理由留着，但新行不会再写它。
        """
        announcement_id = new_id("ann")
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO announcements(id,title,body,tone,deliver_email,active,
                                             created_by,created_at)
                   VALUES(?,?,?,?,?,1,?,?)""",
                (announcement_id, title[:200], body[:4000], tone, 1 if deliver_email else 0,
                 created_by[:254], utc_now()),
            )
            # 写这条公告的人**不用向自己确认**：他刚写完，那个对话框对他没有任何
            # 新信息，而它盖住整页、锁住滚动，非要他点一下才放行——2026-09-17 用户
            # 报的「每次发完广播软件就不能滑动」里，最刺眼的就是这一步（他还得刷新
            # 一次才能继续用后台）。其他每个人照样必须点「确认收到」。
            connection.execute(
                """INSERT OR REPLACE INTO announcement_dismissals(announcement_id,user_id,dismissed_at)
                   SELECT ?, id, ? FROM users WHERE email=? COLLATE NOCASE""",
                (announcement_id, utc_now(), created_by[:254]),
            )
            if image_id:
                # 绑定在**同一个事务**里：先发布再挂图（两条请求）会出现「公告已经
                # 在用户屏幕上、图还没到」的窗口，而那个窗口里的对话框是要用户点
                # 「确认收到」的 —— 他确认的内容和几秒后看到的不一样。
                attached = connection.execute(
                    """UPDATE announcement_images SET announcement_id=?
                        WHERE id=? AND announcement_id IS NULL""",
                    (announcement_id, image_id),
                )
                if attached.rowcount != 1:
                    raise ValueError("这张图片不存在，或者已经用在别的公告上了。")
            if deliver_email:
                connection.execute(
                    """INSERT OR IGNORE INTO announcement_deliveries(announcement_id,user_id,status)
                       SELECT ?, u.id, 'pending' FROM users u
                       JOIN mailboxes m ON m.user_id=u.id
                       WHERE u.status='active'""",
                    (announcement_id,),
                )
        return announcement_id

    # ------------------------------------------------------- 广播的配图

    def create_announcement_image(self, media_type: str, data: bytes, width: int,
                                  height: int) -> str:
        """Store an uploaded broadcast image as a *draft* (no announcement yet)."""
        image_id = new_id("aimg")
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO announcement_images(id,announcement_id,media_type,bytes,width,
                                                   height,byte_size,created_at)
                   VALUES(?,NULL,?,?,?,?,?,?)""",
                (image_id, media_type, data, int(width), int(height), len(data), utc_now()),
            )
        return image_id

    def announcement_image(self, image_id: str) -> dict[str, Any] | None:
        """One image row, by its own id or by the announcement it belongs to.

        Both lookups are wanted: the console addresses a draft by its image id
        (before publishing), while `/announcement-image/<id>` addresses it by the
        same id it was published with.

        **不再 JOIN `announcements`**（2026-09-24）：那次 JOIN 只为带出
        `a.active / a.is_public / a.title`，而配图的可见性规则改成「草稿只有管理员、
        其余一律登录」之后，调用方一个都不读了。少一次 JOIN 也少一处让「谁看得见」
        散在两个地方的机会。
        """
        key = str(image_id or "").strip()
        if not key:
            return None
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM announcement_images WHERE id=? OR announcement_id=? LIMIT 1",
                (key, key),
            ).fetchone()
        return dict(row) if row else None

    def drop_announcement_image(self, image_id: str) -> bool:
        """Delete a *draft* image. One already attached to an announcement is left
        alone: removing the picture under a live broadcast is not a "cancel". """
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM announcement_images WHERE id=? AND announcement_id IS NULL",
                (str(image_id or "").strip(),),
            )
        return cursor.rowcount > 0

    def purge_draft_announcement_images(self, hours: float = 6.0) -> int:
        """Drop images that were uploaded and never published.

        Called from ``initialize()`` — the same place the skipped-mail bodies are
        purged. Without it, "试了一张图然后改主意" leaves a megabyte in the
        database, in every backup, and in every future restore, forever.
        """
        cutoff = (dt.datetime.now(dt.timezone.utc)
                  - dt.timedelta(hours=max(0.0, float(hours)))).isoformat(timespec="seconds")
        with self.connect() as connection:
            cursor = connection.execute(
                "DELETE FROM announcement_images WHERE announcement_id IS NULL AND created_at<?",
                (cutoff,),
            )
        return int(cursor.rowcount)

    def list_announcements(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT a.*,
                          (SELECT COUNT(*) FROM announcement_dismissals d WHERE d.announcement_id=a.id) AS dismissed,
                          (SELECT i.id FROM announcement_images i
                            WHERE i.announcement_id=a.id) AS image_id,
                          (SELECT COUNT(*) FROM announcement_deliveries v WHERE v.announcement_id=a.id) AS email_total,
                          (SELECT COUNT(*) FROM announcement_deliveries v
                             WHERE v.announcement_id=a.id AND v.status='sent') AS email_sent,
                          (SELECT COUNT(*) FROM announcement_deliveries v
                             WHERE v.announcement_id=a.id AND v.status='failed') AS email_failed
                   FROM announcements a ORDER BY a.created_at DESC, a.rowid DESC LIMIT ?""",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def active_announcement_for(self, user_id: str) -> dict[str, Any] | None:
        """The one announcement this user should see right now.

        Only the newest active announcement is returned: GitHub's banner guidance
        is explicit that two banners on one page is a stacking problem, and a
        pilot does not need a feed — it needs one message that is actually read.
        """
        with self.connect() as connection:
            row = connection.execute(
                """SELECT a.* FROM announcements a
                   WHERE a.active=1
                     AND NOT EXISTS (SELECT 1 FROM announcement_dismissals d
                                     WHERE d.announcement_id=a.id AND d.user_id=?)
                   ORDER BY a.created_at DESC, a.rowid DESC LIMIT 1""",
                (user_id,),
            ).fetchone()
        return dict(row) if row else None

    def count_pending_announcements(self, user_id: str) -> int:
        """How many active announcements this user has not confirmed yet.

        The modal shows one at a time, so the number is what lets the card say
        "还有 N 条" instead of looking like it refuses to close.
        """
        with self.connect() as connection:
            row = connection.execute(
                """SELECT COUNT(*) AS n FROM announcements a
                    WHERE a.active=1
                      AND NOT EXISTS (SELECT 1 FROM announcement_dismissals d
                                      WHERE d.announcement_id=a.id AND d.user_id=?)""",
                (user_id,),
            ).fetchone()
        return int(row["n"] if row else 0)

    def dismiss_announcement(self, announcement_id: str, user_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO announcement_dismissals(announcement_id,user_id,dismissed_at)
                   VALUES(?,?,?)""",
                (announcement_id, user_id, utc_now()),
            )

    def withdraw_announcement(self, announcement_id: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE announcements SET active=0, withdrawn_at=? WHERE id=? AND active=1",
                (utc_now(), announcement_id),
            )
            if cursor.rowcount != 1:
                raise KeyError("公告不存在或已经撤下。")
            # Nothing left to deliver for a withdrawn announcement.
            connection.execute(
                "DELETE FROM announcement_deliveries WHERE announcement_id=? AND status='pending'",
                (announcement_id,),
            )
        return True

    def pending_announcement_deliveries(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT v.announcement_id, v.user_id, a.title, a.body, a.tone, a.created_at,
                          u.email, m.report_to, m.smtp_host, m.smtp_port, m.encrypted_password,
                          m.id AS mailbox_id
                   FROM announcement_deliveries v
                   JOIN announcements a ON a.id=v.announcement_id
                   JOIN users u ON u.id=v.user_id
                   JOIN mailboxes m ON m.user_id=v.user_id
                   WHERE v.status='pending' AND a.active=1 AND m.enabled=1
                   ORDER BY v.announcement_id, v.user_id LIMIT ?""",
                (max(1, min(int(limit), 200)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def finish_announcement_delivery(self, announcement_id: str, user_id: str,
                                     error: str = "") -> None:
        with self.connect() as connection:
            connection.execute(
                """UPDATE announcement_deliveries SET status=?, sent_at=?, last_error=?
                   WHERE announcement_id=? AND user_id=?""",
                ("failed" if error else "sent", None if error else utc_now(),
                 str(error)[:300], announcement_id, user_id),
            )

    # ------------------------------------------------------------------ usage

    def record_usage(self, *, user_id: str, kind: str, provider: str, model: str,
                     usage: dict[str, Any] | None, cost: dict[str, Any] | None,
                     price: dict[str, Any] | None = None, message_id: str = "",
                     report_id: str = "", on_platform: bool | None = None) -> str:
        """One row per model call.

        The rates are frozen into ``price_json`` so that editing a price later
        cannot rewrite what a past call actually cost.

        ``on_platform`` records *whose key paid*, and it has to be captured here
        rather than derived later: whether an account rides the instance key is a
        fact about the moment of the call, and the user adding their own key
        afterwards would silently re-attribute every earlier row. ``None`` means
        "unknown" and is stored as NULL -- see the column comment.
        """
        usage = usage or {}
        row_id = new_id("use")
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO token_usage(id,user_id,message_id,report_id,kind,provider,model,
                       input_tokens,cached_input_tokens,output_tokens,reasoning_tokens,total_tokens,
                       currency,cost,price_json,on_platform,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row_id, user_id, message_id or None, report_id or None, kind[:20], provider[:60], model[:120],
                 int(usage.get("input") or 0), int(usage.get("cached_input") or 0),
                 int(usage.get("output") or 0), int(usage.get("reasoning") or 0),
                 int(usage.get("total") or 0),
                 (cost or {}).get("currency") or (price or {}).get("currency") or "",
                 None if not cost else float(cost.get("total_cost") or 0.0),
                 json.dumps(price or {}, ensure_ascii=False) if price else "",
                 None if on_platform is None else (1 if on_platform else 0),
                 utc_now()),
            )
        return row_id

    def usage_for_user(self, user_id: str, *, days: int = 30,
                       timezone_offset_hours: int = 8) -> dict[str, Any]:
        """One account's own model usage, split by whose key paid.

        The user-facing counterpart of :meth:`usage_overview`, and the split is
        the point of it. During the pilot the operator pays, so a single "you
        spent $0.42" would be false for most accounts -- and a single "you spent
        $0" would be false for the ones who brought their own key. Three buckets,
        and a fourth for rows written before the question was being asked.
        """
        days = max(1, min(int(days), 365))
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat(timespec="seconds")
        local_day = f"date(created_at, '{int(timezone_offset_hours):+d} hours')"
        with self.connect() as connection:
            totals = connection.execute(
                """SELECT COUNT(*) AS calls,
                          COALESCE(SUM(input_tokens),0) AS input_tokens,
                          COALESCE(SUM(cached_input_tokens),0) AS cached_input_tokens,
                          COALESCE(SUM(output_tokens),0) AS output_tokens,
                          COALESCE(SUM(reasoning_tokens),0) AS reasoning_tokens,
                          COALESCE(SUM(total_tokens),0) AS total_tokens,
                          SUM(CASE WHEN cost IS NULL THEN 1 ELSE 0 END) AS unpriced_calls,
                          COALESCE(SUM(cost),0) AS cost,
                          MAX(currency) AS currency,
                          MAX(created_at) AS last_call_at
                   FROM token_usage WHERE user_id=? AND created_at>=?""",
                (user_id, since)).fetchone()
            # `cost IS NULL` rows get NULL for the sum, and max() keeps them out
            # of the bucket entirely rather than folding them into "own key".
            payers = connection.execute(
                """SELECT CASE WHEN on_platform IS NULL THEN 'unknown'
                               WHEN on_platform=1 THEN 'platform' ELSE 'own' END AS payer,
                          COUNT(*) AS calls,
                          COALESCE(SUM(total_tokens),0) AS total_tokens,
                          SUM(CASE WHEN cost IS NULL THEN 1 ELSE 0 END) AS unpriced_calls,
                          COALESCE(SUM(cost),0) AS cost
                   FROM token_usage WHERE user_id=? AND created_at>=?
                   GROUP BY payer""", (user_id, since)).fetchall()
            daily = connection.execute(
                f"""SELECT {local_day} AS day, COUNT(*) AS calls,
                           COALESCE(SUM(total_tokens),0) AS total_tokens,
                           SUM(CASE WHEN cost IS NULL THEN 1 ELSE 0 END) AS unpriced_calls,
                           COALESCE(SUM(cost),0) AS cost
                    FROM token_usage WHERE user_id=? AND created_at>=?
                    GROUP BY day ORDER BY day DESC""", (user_id, since)).fetchall()
            models = connection.execute(
                """SELECT provider, model, COUNT(*) AS calls,
                          COALESCE(SUM(total_tokens),0) AS total_tokens,
                          SUM(CASE WHEN cost IS NULL THEN 1 ELSE 0 END) AS unpriced_calls,
                          COALESCE(SUM(cost),0) AS cost
                   FROM token_usage WHERE user_id=? AND created_at>=?
                   GROUP BY provider, model ORDER BY total_tokens DESC""", (user_id, since)).fetchall()
        by_payer = {row["payer"]: dict(row) for row in payers}
        return {
            "days": days,
            "since": since,
            "timezone": f"UTC{int(timezone_offset_hours):+d}",
            "totals": dict(totals),
            "by_payer": by_payer,
            # Explicitly listed so the console never has to invent a bucket it
            # forgot: an absent key and a zero-call bucket are different things.
            "payers": ["platform", "own", "unknown"],
            "daily": [dict(row) for row in daily],
            "models": [dict(row) for row in models],
        }

    def platform_key_spend(self, since: str,
                           metered_providers: Iterable[str] | None = None) -> dict[str, Any]:
        """**管理员那把 key** 在 ``since`` 之后花掉的钱，我们自己记的那本账。

        只数 ``on_platform=1`` 的行：``on_platform`` 是**写入时**记下的「这一笔是谁的 key
        付的」，不是事后推算的（见 `record_usage`）。所以用户今天换成自己的 key，也不会把
        上个月由管理员付掉的那些行改写成他自己的。

        三个必须分开数的桶，混进来会让这个数说假话：

        * ``unpriced_calls`` —— 有调用但**没有单价**（`pricing.lookup` 认不出这个模型名）。
          它们的 ``cost`` 是 NULL，`SUM` 会把它们当 0，于是「花了多少」被系统性地低估。
        * ``unknown_calls`` —— 早于本列存在的行（``on_platform IS NULL``）：**不知道**是谁付的，
          不能算成管理员付的，也不能算成没花。单独报出来，让读的人自己判断。
        * ``local_calls``（2026-09-22 新增）—— 平台成了两档（本机那台主服务 + 付费兜底），
          两者都写 ``on_platform=1``，但只有后者真的在花钱。于是「次数」与「钱」要分开数：
          ``calls`` / ``cost`` 只数**会花钱的那些供应商**的行（``metered_providers`` 说是哪些），
          本机那些单独报成 ``local_calls``。

        为什么 ``metered_providers`` 由调用方传：**「哪家要花钱」是定价表的知识，不是数据库的
        知识**（`pricing.lookup` 不认识的供应商 = 不花钱，这个判断属于 `budget`/`pricing`）。
        不传时退回「按 on_platform 全算」的旧行为，老调用方与测试不受影响。
        """
        metered = [str(item).strip().lower() for item in (metered_providers or []) if str(item).strip()]
        only = ""
        params: tuple = (since,)
        if metered:
            only = f" AND provider IN ({','.join('?' for _ in metered)})"
            params = (since, *metered)
        local_only = (" AND provider NOT IN ({})".format(",".join("?" for _ in metered))
                      if metered else "")
        with self.connect() as connection:
            row = connection.execute(
                f"""SELECT COUNT(*) AS calls,
                           COALESCE(SUM(cost),0) AS cost,
                           SUM(CASE WHEN cost IS NULL THEN 1 ELSE 0 END) AS unpriced_calls,
                           MAX(currency) AS currency
                    FROM token_usage WHERE on_platform=1 AND created_at >= ?{only}""",
                params).fetchone()
            local = connection.execute(
                f"""SELECT COUNT(*) AS calls FROM token_usage
                    WHERE on_platform=1 AND created_at >= ?{local_only}""",
                params).fetchone()
            unknown = connection.execute(
                """SELECT COUNT(*) AS calls, COALESCE(SUM(cost),0) AS cost
                   FROM token_usage WHERE on_platform IS NULL AND created_at >= ?""",
                (since,)).fetchone()
        return {"since": since, "calls": int(row["calls"] or 0),
                "cost": round(float(row["cost"] or 0.0), 4),
                "unpriced_calls": int(row["unpriced_calls"] or 0),
                "local_calls": int(local["calls"] or 0),
                "currency": str(row["currency"] or "USD"),
                "unknown_calls": int(unknown["calls"] or 0),
                "unknown_cost": round(float(unknown["cost"] or 0.0), 4)}

    def usage_overview(self, days: int = 30, timezone_offset_hours: int = 8) -> dict[str, Any]:
        """Per-user token totals and cost, with daily and per-model breakdowns.

        Days are bucketed in Hong Kong time (the pilot's timezone) rather than
        UTC: an operator reading "9月14日" means the local day, and a UTC bucket
        would silently move the evening's usage into the next date.
        """
        days = max(1, min(int(days), 365))
        since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).isoformat(timespec="seconds")
        local_day = f"date(created_at, '{int(timezone_offset_hours):+d} hours')"
        with self.connect() as connection:
            totals = connection.execute(
                f"""SELECT u.id AS user_id, u.email,
                           COUNT(t.id) AS calls,
                           COALESCE(SUM(t.input_tokens),0) AS input_tokens,
                           COALESCE(SUM(t.cached_input_tokens),0) AS cached_input_tokens,
                           COALESCE(SUM(t.output_tokens),0) AS output_tokens,
                           COALESCE(SUM(t.reasoning_tokens),0) AS reasoning_tokens,
                           COALESCE(SUM(t.total_tokens),0) AS total_tokens,
                           -- t.id IS NOT NULL matters: the LEFT JOIN gives a
                           -- user with no calls one all-NULL row, and counting
                           -- that as "1 unpriced call" made every idle account
                           -- look like it had a billing problem.
                           SUM(CASE WHEN t.id IS NOT NULL AND t.cost IS NULL THEN 1 ELSE 0 END) AS unpriced_calls,
                           COALESCE(SUM(t.cost),0) AS cost,
                           MAX(t.currency) AS currency,
                           MAX(t.created_at) AS last_call_at
                    FROM users u LEFT JOIN token_usage t
                         ON t.user_id=u.id AND t.created_at >= ?
                    WHERE u.status!='deleted'
                    GROUP BY u.id ORDER BY cost DESC, total_tokens DESC""", (since,)).fetchall()
            daily = connection.execute(
                f"""SELECT user_id, {local_day} AS day, COUNT(*) AS calls,
                           COALESCE(SUM(input_tokens),0) AS input_tokens,
                           COALESCE(SUM(cached_input_tokens),0) AS cached_input_tokens,
                           COALESCE(SUM(output_tokens),0) AS output_tokens,
                           COALESCE(SUM(reasoning_tokens),0) AS reasoning_tokens,
                           COALESCE(SUM(total_tokens),0) AS total_tokens,
                           SUM(CASE WHEN cost IS NULL THEN 1 ELSE 0 END) AS unpriced_calls,
                           COALESCE(SUM(cost),0) AS cost
                    FROM token_usage WHERE created_at >= ?
                    GROUP BY user_id, day ORDER BY day DESC""", (since,)).fetchall()
            models = connection.execute(
                """SELECT user_id, provider, model, COUNT(*) AS calls,
                          COALESCE(SUM(input_tokens),0) AS input_tokens,
                          COALESCE(SUM(cached_input_tokens),0) AS cached_input_tokens,
                          COALESCE(SUM(output_tokens),0) AS output_tokens,
                          COALESCE(SUM(reasoning_tokens),0) AS reasoning_tokens,
                          COALESCE(SUM(total_tokens),0) AS total_tokens,
                          SUM(CASE WHEN cost IS NULL THEN 1 ELSE 0 END) AS unpriced_calls,
                          COALESCE(SUM(cost),0) AS cost
                   FROM token_usage WHERE created_at >= ?
                   GROUP BY user_id, provider, model ORDER BY total_tokens DESC""", (since,)).fetchall()
        daily_by_user: dict[str, list[dict[str, Any]]] = {}
        for row in daily:
            daily_by_user.setdefault(row["user_id"], []).append(dict(row))
        models_by_user: dict[str, list[dict[str, Any]]] = {}
        for row in models:
            models_by_user.setdefault(row["user_id"], []).append(dict(row))
        users = []
        for row in totals:
            entry = dict(row)
            entry["daily"] = daily_by_user.get(row["user_id"], [])
            entry["models"] = models_by_user.get(row["user_id"], [])
            users.append(entry)
        return {
            "days": days,
            "since": since,
            "timezone": f"UTC{int(timezone_offset_hours):+d}",
            "users": users,
            "grand_total": {
                "calls": sum(int(row["calls"]) for row in totals),
                "total_tokens": sum(int(row["total_tokens"]) for row in totals),
                "input_tokens": sum(int(row["input_tokens"]) for row in totals),
                "cached_input_tokens": sum(int(row["cached_input_tokens"]) for row in totals),
                "output_tokens": sum(int(row["output_tokens"]) for row in totals),
                "reasoning_tokens": sum(int(row["reasoning_tokens"]) for row in totals),
                "unpriced_calls": sum(int(row["unpriced_calls"] or 0) for row in totals),
                "cost": round(sum(float(row["cost"] or 0) for row in totals), 6),
                "currency": next((row["currency"] for row in totals if row["currency"]), ""),
            },
        }

    def list_model_prices(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM model_prices ORDER BY provider, model").fetchall()
        return [dict(row) for row in rows]

    def set_model_price(self, provider: str, model: str, *, input_cache_hit: float,
                        input_cache_miss: float, output: float,
                        peak_multiplier: float = 1.0, currency: str = "USD") -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO model_prices(provider,model,input_cache_hit,input_cache_miss,output,
                       peak_multiplier,currency,updated_at) VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(provider,model) DO UPDATE SET
                       input_cache_hit=excluded.input_cache_hit,
                       input_cache_miss=excluded.input_cache_miss,
                       output=excluded.output,
                       peak_multiplier=excluded.peak_multiplier,
                       currency=excluded.currency,
                       updated_at=excluded.updated_at""",
                (provider[:60], model[:120], float(input_cache_hit), float(input_cache_miss),
                 float(output), float(peak_multiplier), currency[:8], utc_now()))

    def delete_model_price(self, provider: str, model: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM model_prices WHERE provider=? AND model=?",
                               (provider, model))

    def list_messages_overview(self, *, limit: int = 50, offset: int = 0,
                               status: str = "all", user_id: str = "") -> dict[str, Any]:
        """Every incoming mail with what happened to it, newest first."""
        clause = self.MESSAGE_FILTERS.get(status, "1=1")
        owner = "AND m.user_id=?" if user_id else ""
        parameters: list[Any] = ([user_id] if user_id else [])
        base = f"""FROM messages m JOIN users u ON u.id=m.user_id
                   LEFT JOIN reports r ON r.message_id=m.id AND r.kind='immediate'
                   WHERE {clause} {owner}"""
        with self.connect() as connection:
            total = int(connection.execute(
                f"SELECT COUNT(*) {base}", tuple(parameters)).fetchone()[0])
            counts = {
                key: int(connection.execute(
                    f"""SELECT COUNT(*) FROM messages m JOIN users u ON u.id=m.user_id
                        LEFT JOIN reports r ON r.message_id=m.id AND r.kind='immediate'
                        WHERE {value} {owner}""", tuple(parameters)).fetchone()[0])
                for key, value in self.MESSAGE_FILTERS.items()
            }
            rows = connection.execute(
                f"""SELECT m.id,m.subject,m.sender_name,m.sender_address,m.received_at,
                           m.status,m.skip_reason,m.attempts,m.last_error,m.next_attempt_at,
                           m.importance,m.imap_uid,m.message_key,
                           u.email AS user_email,u.id AS user_id,
                           r.id AS report_id,r.status AS report_status,r.subject AS report_subject,
                           r.sent_at,r.sent_to,r.created_at AS report_created_at,
                           r.last_error AS report_error {base}
                    ORDER BY m.received_at DESC, m.rowid DESC LIMIT ? OFFSET ?""",
                tuple(parameters) + (max(1, min(int(limit), 200)), max(0, int(offset))),
            ).fetchall()
        return {"messages": [dict(row) for row in rows], "total": total, "counts": counts}

    def list_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        # created_at only has second resolution, so several actions in the same
        # second tie. Without the rowid tiebreak SQLite may return them in any
        # order, which showed up as "the newest entry is not the one just made"
        # — both in the console and in a flaky test.
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT created_at,actor_email,action,target_email,detail FROM audit_log "
                "ORDER BY created_at DESC, rowid DESC LIMIT ?", (min(int(limit), 200),)
            ).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------ alerts    #
    # De-duplication state for the operator sentinel in ``pilot_app.alerting``.
    # Deliberately its own table rather than a flag on ``users``: a paused
    # account means "an operator chose to pause this user", which is a different
    # thing from "the sentinel is currently reporting a problem", and mixing
    # them would make the admin panel's pause reason unreadable.

    # ----------------------------------------------------------------- settings    #
    # Operator-facing knobs that must be changeable while the service runs.
    # Deliberately generic (one table, no schema change per new knob) and
    # deliberately string-valued: the reader owns parsing and validation, so a
    # bad value can be rejected before it is ever stored.

    def get_setting(self, key: str, default: str = "") -> str:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str, *, actor: str = "") -> None:
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO app_settings(key,value,updated_at,updated_by) VALUES(?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                       value=excluded.value, updated_at=excluded.updated_at,
                       updated_by=excluded.updated_by""",
                (key, value, utc_now(), actor[:320]),
            )

    def delete_setting(self, key: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM app_settings WHERE key=?", (key,))

    def list_settings(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM app_settings ORDER BY key").fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------- 「上次看过之后」有什么动静
    #
    # 用户原话（2026-09-17，问了三遍）：「我刷新后台界面应该要可以显示新的通知，
    # 有人申请了邀请码等等」。
    #
    # 后台里那些数字一直都在（面板摘要行、以及 v0.63.71 加的「需要你处理」），但它们
    # 回答的是「现在有什么要我处理」。用户问的是另一件事：**我不在的时候发生了什么**。
    # 两者的差别很实在 —— 一件已经自己解决掉的事（有人申请又被批准、留言被处理掉）
    # 在「需要你处理」里会消失，而那恰恰是运营者想知道的事。
    #
    # 所以这里按**每个管理员**记一个「上次打开后台的时刻」，下次打开时把这段时间里的
    # 动静数出来。上一个时刻由服务端在每次打开时前移，客户端不参与 —— 它只是把拿到
    # 的数字说出来。

    @staticmethod
    def admin_seen_key(user_id: str) -> str:
        return f"admin_seen:{user_id}"

    def admin_activity(self, user_id: str) -> dict[str, Any]:
        """What happened since this admin last had the console open.

        Two details, both about the seam between "now" and a stored record:

        * The marker moves **before** the counts are taken (``now`` is fixed first
          and the queries are bounded by it), so something arriving while this
          response is being built is counted next time rather than lost between
          the two.
        * Records carry **second** precision and the marker carries microseconds,
          so the lower bound is the marker's *second* (``>=``, not ``>``).  That
          errs towards repeating: a row created in the same second as the previous
          look may be reported on two consecutive looks. The other direction --
          ``>`` on the exact marker -- would silently drop anything that arrived
          in that second, and a notification that misses things is not a
          notification. The panel still lists it once; only the sentence repeats.

        The first call has nothing to compare against, and says so instead of
        claiming that nothing happened.
        """
        key = self.admin_seen_key(user_id)
        previous = self.get_setting(key, "")
        now = utc_now_fine()
        if not previous:
            self.set_setting(key, now, actor=user_id)
            return {"first": True, "at": now, "signups": 0, "applicants": [],
                    "guest": 0, "users": 0, "alerts": 0, "since": ""}
        with self.connect() as connection:
            def count(sql: str, args: tuple[Any, ...] = ()) -> int:
                row = connection.execute(sql, args).fetchone()
                return int(row["n"] if row else 0)

            # 左边按**整秒**算（见 docstring：记录是秒精度，宁可重复也不能漏）。
            window = (previous[:19], now)
            signups = count("SELECT COUNT(*) AS n FROM signup_requests"
                            " WHERE created_at>? AND created_at<=?", window)
            # 名字比数字有用：运营者要决定的是「现在看一眼还是待会儿」。最多报三个，
            # 再多他也要去面板里看。
            applicants = [row["email"] for row in connection.execute(
                "SELECT email FROM signup_requests WHERE created_at>? AND created_at<=?"
                " ORDER BY created_at DESC LIMIT 3", window).fetchall()]
            guest = count("SELECT COUNT(*) AS n FROM guest_messages"
                          " WHERE created_at>? AND created_at<=? AND status<>'deleted'", window)
            users = count("SELECT COUNT(*) AS n FROM users"
                          " WHERE created_at>? AND created_at<=?", window)
            alerts = count("SELECT COUNT(*) AS n FROM alert_state"
                           " WHERE first_seen_at>? AND first_seen_at<=?", window)
        self.set_setting(key, now, actor=user_id)
        return {"first": False, "at": now, "since": previous, "signups": signups,
                "applicants": applicants, "guest": guest, "users": users, "alerts": alerts}

    # ---------------------------------------------------------------- capacity
    #
    # Everything the capacity advisor measures, as one read-only query set. It
    # lives here rather than in the advisor so the advisor stays a pure function
    # of numbers, which is what makes its arithmetic testable.

    def recent_volume(self, days: int = 14) -> dict[str, Any]:
        """Arrivals and generation times over a recent window.

        ``generation_gaps`` is the seconds between consecutive reports for the
        *same* user. The scheduler serialises a user's reports, so this bounds
        the per-report cost from above — but it is an **upper bound, not a
        measurement of generation**. When a user has nothing else queued, the gap
        is mostly the time until their next mail arrives. On production it reads
        about 207 s, while an instrumented brief takes 5-9 s. That is why the
        throughput it feeds is conservative rather than optimistic, and why the
        panel labels it "报告间隔（上界）" rather than "每份报告耗时". Measuring
        generation properly needs start/stop timestamps around the provider call.
        """
        since = (dt.datetime.now(dt.timezone.utc)
                 - dt.timedelta(days=max(1, days))).isoformat(timespec="seconds")
        with self.connect() as connection:
            messages = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE created_at>=?", (since,)).fetchone()[0]
            delivered = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE created_at>=? AND status='sent'",
                (since,)).fetchone()[0]
            active_users = connection.execute(
                "SELECT COUNT(DISTINCT user_id) FROM messages WHERE created_at>=?",
                (since,)).fetchone()[0]
            rows = connection.execute(
                """SELECT user_id, created_at FROM reports
                   WHERE kind='immediate' AND created_at>=? ORDER BY user_id, created_at""",
                (since,)).fetchall()
            total_users = connection.execute(
                "SELECT COUNT(*) FROM users WHERE status!='deleted'").fetchone()[0]
            # 端到端：这封信落库 → 它的报告落库，中间包含了排队与生成。比「同用户相邻
            # 报告间隔」真得多（后者在有邮件排队时才是间隔，没排队时几乎是下一封信什么时候来）。
            # 2026-09-22 实测：间隔 3440 秒 vs 端到端 p50 7 秒（p90 16 秒）。
            durations = connection.execute(
                """SELECT m.created_at AS arrived, r.created_at AS produced
                     FROM reports r JOIN messages m ON m.id = r.message_id
                    WHERE r.created_at IS NOT NULL AND m.created_at IS NOT NULL
                      AND r.created_at>=?
                    ORDER BY r.created_at DESC LIMIT 300""",
                (since,)).fetchall()

        gaps: list[float] = []
        previous_user = None
        previous_at = None
        for row in rows:
            current = parse_utc(row["created_at"])
            if current is None:
                continue
            if row["user_id"] == previous_user and previous_at is not None:
                delta = (current - previous_at).total_seconds()
                # Guard against clock jumps and legacy rows: anything outside a
                # plausible range would poison the median.
                if 0 < delta < 6 * 3600:
                    gaps.append(delta)
            previous_user = row["user_id"]
            previous_at = current

        end_to_end: list[float] = []
        recent: list[float] = []
        recent_since = (dt.datetime.now(dt.timezone.utc)
                        - dt.timedelta(days=RECENT_SAMPLE_DAYS)).isoformat(timespec="seconds")
        for row in durations:
            arrived = parse_utc(row["arrived"])
            produced = parse_utc(row["produced"])
            if arrived is None or produced is None:
                continue
            delta = (produced - arrived).total_seconds()
            # 同上：只收合理区间（一秒以内是同一批写入，一小时以上多半是排队/重试/时钟问题）。
            if 1 <= delta < 3600:
                end_to_end.append(delta)
                if row["produced"] >= recent_since:
                    recent.append(delta)

        return {
            "window_days": max(1, days),
            "messages": int(messages),
            "delivered": int(delivered),
            "active_users": int(active_users),
            "total_users": int(total_users),
            "generation_gaps": gaps,
            "report_seconds_end_to_end": end_to_end,
            # **最近**那一小段的样本，用来算「这一台现在多快」。见 `capacity.py`
            # 里 `RECENT_SAMPLE_DAYS` 的注释：换主服务会把分布整个搬走，而 14 天窗口
            # 里的中位数会继续描述上一任。
            "report_seconds_recent": recent,
        }

    def list_alert_states(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM alert_state").fetchall()
        return [dict(row) for row in rows]

    def record_alert(self, key: str, severity: str, detail: str, title: str, when: Any) -> None:
        """Remember that we told the operator, keeping the original first sighting."""
        stamp = when.isoformat(timespec="seconds")
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO alert_state(key,severity,title,detail,first_seen_at,last_sent_at,open)
                   VALUES(?,?,?,?,?,?,1)
                   ON CONFLICT(key) DO UPDATE SET
                       severity=excluded.severity, title=excluded.title, detail=excluded.detail,
                       last_sent_at=excluded.last_sent_at, open=1""",
                (key, severity[:20], title[:200], detail[:500], stamp, stamp),
            )

    def clear_alert(self, key: str, when: Any) -> None:
        """Mark a condition as no longer present, after announcing its recovery."""
        with self.connect() as connection:
            connection.execute(
                # `acknowledged_at` goes back to NULL with the condition. The
                # acknowledgment meant "I know about *this* problem"; if the same
                # key fires again later it is a new problem, and it has to be
                # able to reach the operator.
                "UPDATE alert_state SET open=0, acknowledged_at=NULL, last_sent_at=? WHERE key=?",
                (when.isoformat(timespec="seconds"), key),
            )

    def acknowledge_alert(self, key: str, when: Any) -> None:
        """Stop reminding about one finding the operator has seen.

        Deliberately not a delete and not a close: the condition is still there
        and the console must keep showing it. This only says "stop mailing me
        about this one", which is the difference between a known-issue list and
        a blindfold.
        """
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE alert_state SET acknowledged_at=? WHERE key=?",
                (when.isoformat(timespec="seconds"), key),
            )
            if cursor.rowcount == 0:
                raise KeyError("没有这条巡检记录。")

    def unacknowledge_alert(self, key: str) -> None:
        """Undo :meth:`acknowledge_alert` so the finding can mail again."""
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE alert_state SET acknowledged_at=NULL WHERE key=?", (key,))
            if cursor.rowcount == 0:
                raise KeyError("没有这条巡检记录。")

    # ------------------------------------------------------- agent analyses
    #
    # Storage for the AI operations assistant. Reads never select `body` except
    # through the dedicated accessor, so a caller that only wants metadata cannot
    # accidentally decrypt (or serialise) an analysis.

    def record_agent_report(self, *, finding_key: str, severity: str, title: str, fingerprint: str,
                            provider: str, model: str, tokens: dict[str, Any], cost: Optional[float],
                            currency: str, body: bytes, created_at: Any,
                            action: str = "") -> str:
        row_id = new_id("agt")
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO agent_reports(id,finding_key,severity,title,fingerprint,provider,model,
                       input_tokens,output_tokens,total_tokens,cost,currency,body,created_at,action)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (row_id, str(finding_key)[:200], str(severity)[:20], str(title)[:200],
                 str(fingerprint)[:64], str(provider)[:60], str(model)[:120],
                 int((tokens or {}).get("input") or 0), int((tokens or {}).get("output") or 0),
                 int((tokens or {}).get("total") or 0), cost, str(currency or "")[:8],
                 body, created_at.isoformat(timespec="seconds"), str(action or "")[:40]),
            )
        return row_id

    def request_agent_action(self, report_id: str, *, requested_by: str,
                             now: dt.datetime | str) -> dict[str, Any]:
        """Queue the action the assistant proposed on one report.

        The action is read back off the *report*, never taken from the request
        body. An operator confirms a proposal; they do not get to name an
        action. That keeps the closed catalogue in `agent.ACTIONS` closed -- a
        caller who could pass an arbitrary key would have turned one confirm
        button into a general-purpose remote control.

        Queueing is not doing: the worker picks this up on its next pass. The
        web process could not act even if it wanted to.
        """
        with self.connect() as connection:
            row = connection.execute(
                "SELECT action FROM agent_reports WHERE id=?", (report_id,)).fetchone()
            if row is None:
                raise KeyError("没有这条分析记录。")
            action = str(row["action"] or "")
            if not action:
                raise ValueError("这条分析没有建议任何动作。")
            already = connection.execute(
                "SELECT id FROM agent_actions WHERE report_id=? AND status='requested'",
                (report_id,)).fetchone()
            if already is not None:
                raise ValueError("这条建议已经确认过了，正在等 worker 执行。")
            row_id = new_id("act")
            connection.execute(
                """INSERT INTO agent_actions(id,report_id,action,status,requested_at,requested_by)
                   VALUES(?,?,?,'requested',?,?)""",
                (row_id, report_id, action, moment(now), requested_by))
        return {"id": row_id, "report_id": report_id, "action": action}

    def pending_agent_actions(self, *, limit: int = 5) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM agent_actions WHERE status='requested'
                    ORDER BY requested_at, rowid LIMIT ?""",
                (max(1, min(int(limit), 50)),)).fetchall()
        return [dict(row) for row in rows]

    def finish_agent_action(self, action_id: str, *, ok: bool, result: str,
                            now: dt.datetime | str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE agent_actions SET status=?, finished_at=?, result=? WHERE id=?",
                ("done" if ok else "failed", moment(now),
                 str(result or "")[:500], action_id))

    def list_agent_actions(self, *, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT a.*, r.title AS report_title FROM agent_actions a
                     LEFT JOIN agent_reports r ON r.id = a.report_id
                    ORDER BY a.requested_at DESC, a.rowid DESC LIMIT ?""",
                (max(1, min(int(limit), 100)),)).fetchall()
        return [dict(row) for row in rows]

    def count_agent_reports_since(self, since: str) -> int:
        """Calls made in the window -- the budget gate reads this before calling."""
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM agent_reports WHERE created_at >= ?", (since,)).fetchone()
        return int(row[0]) if row else 0

    def latest_agent_report(self, finding_key: str) -> Optional[dict[str, Any]]:
        with self.connect() as connection:
            row = connection.execute(
                """SELECT * FROM agent_reports WHERE finding_key=?
                   ORDER BY created_at DESC, rowid DESC LIMIT 1""", (finding_key,)).fetchone()
        return dict(row) if row else None

    def list_agent_reports(self, *, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM agent_reports ORDER BY created_at DESC, rowid DESC LIMIT ?""",
                (max(1, min(int(limit), 100)),)).fetchall()
        return [dict(row) for row in rows]

    def latest_agent_fingerprints(self) -> dict[str, str]:
        """The newest analysis fingerprint per finding, without decrypting anything.

        Exists so the sentinel can ask "which findings have no analysis that
        matches their *current* shape?" in one query. Doing it per finding would
        be a query per finding every five minutes, and doing it in Python would
        mean decrypting every body -- both are reasons this would quietly stop
        being called.
        """
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT r.finding_key AS key, r.fingerprint AS fingerprint
                     FROM agent_reports r
                     JOIN (SELECT finding_key, MAX(rowid) AS newest FROM agent_reports
                            GROUP BY finding_key) last
                       ON last.newest = r.rowid""").fetchall()
        return {str(row["key"]): str(row["fingerprint"]) for row in rows}

    # ------------------------------------------------------------------ admin
    #
    # These queries deliberately select only non-secret columns. Encrypted
    # mailbox passwords and API keys are never selected, so no admin surface can
    # leak them even by accident.

    # Someone who registered and never came back is invisible from inside the
    # product: nothing fails, nothing is queued, and the account simply produces
    # no mail. Three of the first six pilot accounts stalled at this step, so the
    # definition lives here once and is shared by the console and the sentinel --
    # two places disagreeing about who is "unfinished" would be worse than either
    # one being wrong.
    SETUP_GAP_LABELS = {
        "no_mailbox": "还没配私人转发邮箱",
        "unreachable": "配了邮箱，但从来没有连通成功过",
    }

    @staticmethod
    def setup_gap(row: dict[str, Any]) -> str:
        """Why this account is not finished, or "" when it is.

        "Finished" is the one step nobody can do for them: a private mailbox that
        has answered at least once. **A model or search key is deliberately not
        counted** -- since 2026-09-14 the instance has its own fallback
        credentials, so treating a missing personal key as unfinished would
        report working accounts as stuck.
        """
        if str(row.get("status") or "") not in ("active", "paused"):
            return ""
        if not row.get("mailbox_email") or not int(row.get("mailbox_enabled") or 0):
            return "no_mailbox"
        if not row.get("last_verified_at") and not row.get("last_polled_at"):
            # Configured but never reached: the usual cause is a wrong IMAP
            # authorisation code, which fails silently until someone looks.
            return "unreachable"
        return ""

    def stalled_setups(self, *, hours: float = 12.0,
                       now: dt.datetime | None = None) -> list[dict[str, Any]]:
        """Accounts that registered long enough ago and still are not finished.

        Paused accounts are included on purpose: they may have been paused *by*
        the stall, and the operator is the one who decides what to do about it.
        Deleted accounts are excluded by the underlying query.
        """
        now = now or dt.datetime.now(dt.timezone.utc)
        stalled: list[dict[str, Any]] = []
        for row in self.list_users_overview():
            gap = self.setup_gap(row)
            if not gap:
                continue
            registered = parse_utc(row.get("created_at"))
            if registered is None:
                continue
            age = now - registered
            if age.total_seconds() < hours * 3600:
                continue
            stalled.append({**row, "setup_gap": gap, "age_hours": age.total_seconds() / 3600})
        stalled.sort(key=lambda item: item["created_at"])
        return stalled

    def list_users_overview(self, *, failure_window_days: int = 7) -> list[dict[str, Any]]:
        """One row per registered account, with the state an operator needs.

        ``failure_window_days`` 只影响 `failed_reports_since_success` 那一列（哨兵用它）：
        更早的失败属于历史，不该让今天的灯变红。
        """
        window_start = (dt.datetime.now(dt.timezone.utc)
                        - dt.timedelta(days=max(1, int(failure_window_days)))
                        ).isoformat(timespec="seconds")
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT
                       u.id, u.email, u.status, u.created_at, u.admin_note, u.last_seen_at,
                       p.school_email, p.major, p.year_of_study,
                       -- 注册时他自己填的三栏（2026-09-23 从申请表挪到注册表单）。
                       p.signup_nickname, p.signup_identity, p.signup_goals,
                       p.immediate_enabled, p.daily_enabled, p.daily_time, p.timezone,
                       m.email AS mailbox_email, m.report_to, m.imap_host,
                       m.id AS mailbox_id,
                       m.enabled AS mailbox_enabled, m.uid_validity, m.last_uid,
                       m.last_polled_at, m.last_verified_at, m.last_error AS mailbox_error,
                       m.last_verify_error, m.updated_at AS mailbox_updated_at,
                       mo.provider AS model_provider, mo.model AS model_name,
                       mo.last_test_at AS model_last_test_at, mo.last_error AS model_error,
                       se.provider AS search_provider,
                       -- The search side records exactly the same evidence as the
                       -- model side and it was simply never selected here, so the
                       -- console had no way to tell a working search key from a
                       -- guessed one. Both kinds are in one `connections` table
                       -- for precisely this reason.
                       se.last_test_at AS search_last_test_at,
                       se.last_error AS search_error,
                       (SELECT COUNT(*) FROM messages WHERE user_id = u.id) AS message_count,
                       -- Mail from an allowed sender: the only evidence there is
                       -- that the school's forwarding rule actually delivers.
                       -- Deliberately *not* `message_count`: a newsletter sent
                       -- straight to the private address is not a forwarded
                       -- school mail, and counting it would make this evidence
                       -- mean "your inbox is not empty" instead.
                       (SELECT COUNT(*) FROM messages WHERE user_id = u.id
                          AND status != 'skipped') AS analysed_count,
                       (SELECT COUNT(*) FROM messages WHERE user_id = u.id
                          AND status IN ('pending','processing','failed')) AS queue_depth,
                       (SELECT COUNT(*) FROM reports WHERE user_id = u.id) AS report_count,
                       (SELECT MAX(created_at) FROM reports WHERE user_id = u.id) AS last_report_at,
                       (SELECT MAX(sent_at) FROM reports WHERE user_id = u.id
                          AND status='sent') AS last_sent_at,
                       -- 真正被分析过的来信所发出的报告（不含每日简报）。这两件事
                       -- 在灯上是同一盏，但它们证明的东西不一样：简报证明收信与
                       -- 发信通，只有它才证明模型那一段也通过。
                       (SELECT COUNT(*) FROM reports WHERE user_id = u.id
                          AND status='sent' AND kind != 'daily') AS mailed_reports,
                       (SELECT COUNT(*) FROM reports WHERE user_id = u.id AND status='failed') AS failed_reports,
                       -- **自上次成功发出以来**的失败数。哨兵用它，不用上面那个历史全量：
                       -- 全量只增不减，于是「修好了」这件事永远反映不出来——2026-09-22
                       -- 实测过：两个账号每天各失败一封日常简报，而其中一封是 9/16 的
                       -- 一次性失败、之后成功过三次，却还挂在同一个数字里。这里还加一个
                       -- 7 天窗口：再往前的失败属于历史，不该让今天的灯变红。
                       -- 自上次成功发出以来的失败数，只算窗口内的（窗口由 Python 传进来，
                       -- 格式与 `created_at` 一致——`datetime('now')` 那种空格分隔的写法
                       -- 和 ISO 的 `T` 分隔混在一起比大小，是个只有到某一天才会发作的坑）。
                       (SELECT COUNT(*) FROM reports r WHERE r.user_id = u.id AND r.status='failed'
                          AND r.created_at >= ?
                          AND r.created_at > COALESCE((SELECT MAX(s.sent_at) FROM reports s
                                 WHERE s.user_id = u.id AND s.status='sent'), '')
                       ) AS failed_reports_since_success,
                       -- 最近一次失败是什么时候。红灯必须能说出它有多旧：一个账号
                       -- 在主人换掉邮箱**之前**失败过一次，之后一直没再发过报告，那盏
                       -- 灯会一直是红的，而卡片上看不出它说的是旧事还是现在的事。
                       (SELECT MAX(created_at) FROM reports WHERE user_id = u.id
                          AND status='failed') AS last_failed_at
                   FROM users u
                   LEFT JOIN profiles  p  ON p.user_id  = u.id
                   LEFT JOIN mailboxes m  ON m.user_id  = u.id
                   LEFT JOIN connections mo ON mo.user_id = u.id AND mo.kind='model'
                   LEFT JOIN connections se ON se.user_id = u.id AND se.kind='search'
                   WHERE u.status != 'deleted'
                   ORDER BY u.created_at""", (window_start,)).fetchall()
        return [dict(row) for row in rows]

    # ---------------------------------------------------- what actually works

    # How long a note may be. Long enough for the kind of thing an operator
    # writes down months later, short enough that the list stays a list.
    ADMIN_NOTE_LIMIT = 500

    def set_admin_note(self, user_id: str, note: str) -> str:
        """Store the operator's private memo about one account; return it.

        The note is stripped and length-capped here rather than at the route, so
        that a second caller (a script, a future bulk import) cannot store
        something the console would then render unbounded.
        """
        cleaned = (note or "").strip()[:self.ADMIN_NOTE_LIMIT]
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE users SET admin_note=? WHERE id=? AND status!='deleted'", (cleaned, user_id))
            if cursor.rowcount == 0:
                raise KeyError("用户不存在。")
        return cleaned

    # 微软对个人版 Outlook / Hotmail 已经停用「账号密码 / 授权码」登录。这不是用户
    # 填错了什么：**换一个授权码也永远不会成功**，只能换一个邮箱服务商。把它单独判出来，
    # 是因为对付它的那句话和「授权码填错了」完全是两件事，而后者会让人白忙一场。
    # **派生的，不是抄的**：清单住在 `mailpresets.BLOCKED_PROVIDER_HOSTS`（知道服务商
    # 知识的那一层），这里只是给它一个数据库层的名字。抄一份就会漂一份，而漂的方向是
    # 「界面说这家不能用了、后台却不认」或者反过来。
    _PROVIDER_BLOCK_HOSTS = mailpresets.BLOCKED_PROVIDER_HOSTS

    @classmethod
    def mailbox_needs_another_provider(cls, row: dict[str, Any]) -> bool:
        """True when this mailbox's *provider* can no longer be read with a password.

        Two signals, because either one alone is reachable in production: the host
        we stored when they configured it, and the error text we wrote ourselves
        from the server's refusal. Judging on the error text alone would miss the
        account whose failure predates that message; judging on the host alone
        would miss a provider that closes the door later.
        """
        error = str(row.get("mailbox_error") or "")
        if "OAuth" in error or "已强制改用" in error:
            return True
        host = str(row.get("imap_host") or "").strip().lower()
        return any(host == item or host.endswith("." + item) for item in cls._PROVIDER_BLOCK_HOSTS)

    @staticmethod
    def verification_lights(row: dict[str, Any]) -> list[dict[str, Any]]:
        """Which parts of this account have been *proven* to work, and which have not.

        This is the one definition of "跑通过" in the project, and the rule it
        encodes is the whole point: **a light is green only when something
        actually succeeded for this account.** Everything else is red, including
        "never tried" -- an indicator that goes green because a row exists is
        exactly the kind of dashboard that lies, and the operator stops reading it.

        The trap this avoids, and it is a real one in this schema: every one of
        these timestamps is written **whether the attempt succeeded or failed**.
        ``record_connection_result`` sets ``last_test_at`` unconditionally, and
        ``record_mailbox_verification`` / ``update_mailbox_poll`` set
        ``last_verified_at`` / ``last_polled_at`` the same way -- the *error*
        column is what carries the outcome. So "has a timestamp" means "somebody
        tried", not "it worked". Judging on the timestamp alone would show a
        broken key as a green light, which is the failure mode the operator
        would never catch by eye.

        ``state`` is still one of three, even though only green and red are
        drawn: ``untested`` and ``failed`` are both red, but they need different
        actions from the reader (go run a test vs. go fix a credential), so the
        label has to say which. Collapsing them into one red word is how a
        status panel turns into a shrug. Uptime Kuma keeps a separate PENDING
        state for the same reason -- "waiting for the first check" is not "down".
        """
        lights: list[dict[str, Any]] = []

        def light(key: str, label: str, attempted: Any, error: Any, ok_detail: str) -> None:
            attempted_at = str(attempted or "")
            problem = str(error or "").strip()
            if not attempted_at:
                lights.append({"key": key, "label": label, "ok": False,
                               "state": "untested", "detail": "从没测过"})
            elif problem:
                # `failed_at` is the time of the *failure*, and it is a separate
                # field from `at` on purpose: `at` means "this is when it worked"
                # and a red light must never carry one. Without the failure time
                # a stale red light is indistinguishable from a current one --
                # 2026-09-16 an account stayed red for a daily digest that failed
                # *before* its owner replaced the mailbox that caused it, and
                # nothing on the card said the failure was already history.
                lights.append({"key": key, "label": label, "ok": False, "state": "failed",
                               "detail": problem[:200], "failed_at": attempted_at})
            else:
                lights.append({"key": key, "label": label, "ok": True,
                               "state": "ok", "detail": ok_detail, "at": attempted_at})

        # 收信: a poll or an explicit read-only IMAP test finished cleanly.
        # `last_error` alone, and deliberately *not* `or last_verify_error`: both
        # writers share that one column and clear it on success, so it always
        # describes the latest attempt. OR-ing in the verify-only error would let
        # a stale failure from last week keep the light red after the mailbox
        # started working -- a red light that cannot be cleared is one the
        # operator learns to ignore.
        light("mailbox", "收信",
              row.get("last_verified_at") or row.get("last_polled_at"),
              row.get("mailbox_error"), "轮询或验证成功过")

        # 模型 / 搜索: two different questions wear one label, and mixing them is
        # what made the operator ask "why is it still red after I refreshed it"
        # (2026-09-16, after he pressed 刷新状态 on every account).
        #
        #   * the account has **its own key** -> the light is about that key:
        #     green only when it was tested and worked, red otherwise. That red is
        #     actionable and the refresh button clears it.
        #   * the account has **no key of its own** and the instance has a
        #     fallback -> this light is not about anything the owner can fix. It
        #     cannot ever turn green (there is no row to stamp: the test result is
        #     written with `UPDATE connections`, and there is no `connections` row
        #     to update), so painting it red is a red light that no click can
        #     clear -- and a red light that cannot be cleared is one the reader
        #     learns to ignore. It is drawn as the neutral 「走平台 key」 instead.
        #     Note what is *not* claimed: the platform key is not proved to work
        #     here, it is simply the account's real configuration, and the user's
        #     own dashboard already calls the same fact 「平台代付」 and ok.
        #   * neither -> red, and this one is actionable: nothing can generate a
        #     report for this account until somebody configures a key.
        for kind, label, own_field, platform_field in (
                ("model", "模型", "model_provider", "platform_model"),
                ("search", "搜索", "search_provider", "platform_search")):
            own_key = str(row.get(own_field) or "").strip()
            if not own_key and row.get(platform_field):
                lights.append({
                    "key": kind, "label": label, "ok": False, "state": "shared",
                    "detail": "走平台兜底 key（平台出钱）",
                    "hint": "这个账号没有配自己的 key，用的是平台兜底 key，所以这盏灯不适用"
                            "——它要证明的是「他自己配的 key 能不能用」，因此它不会变绿。"
                            "平台 key 在这个账号上到底通不通：点「刷新状态」当场就知道，"
                            "结果写在上面的刷新结果里。",
                })
            else:
                light(kind, label, row.get(f"{kind}_last_test_at"), row.get(f"{kind}_error"),
                      "按这个账号测通过")

        # 出报告: a report was generated *and* handed to SMTP successfully.
        #
        # It is the only light that can prove the chain end to end -- **but only
        # when the report came from a mail**. A daily digest is generated from the
        # deterministic list and goes out even with zero analysed messages, so it
        # proves the mailbox was read and SMTP works, while proving nothing about
        # whether a single incoming mail was ever understood and answered.
        # (Whether it also touches the model depends on the optional synthesis
        # paragraph -- which is exactly why "the light is green" must not be read
        # as "the mail-to-report chain works".) Telling those two apart is the
        # difference between "this account works" and "this account has never
        # received anything", which is the confusion the no_mail group removes.
        #
        # Written out by hand rather than through the helper: "tried and it
        # failed" is a different red from "never tried", and unlike the other
        # three there is no single error column to read it from -- the evidence
        # is a count of failed report rows.
        sent_at = str(row.get("last_sent_at") or "")
        failures = int(row.get("failed_reports") or 0)
        if sent_at:
            # 每日简报也算「发出去了」，但它证明的东西少一段：它由确定性清单生成，
            # 零封来信时也发得出去，所以它证明收信与发信通，**不证明模型那段通过**。
            # 2026-09-16 在一个真实账号上看到的就是这一格：绿灯、而他一封信都没收到过。
            mailed = int(row.get("mailed_reports") or 0)
            detail = ("报告真的发出去了" if mailed
                      else "只发出过每日简报：收信和发信是通的，还没有任何一封来信被分析过")
            lights.append({"key": "report", "label": "出报告", "ok": True,
                           "state": "ok", "detail": detail, "at": sent_at})
        elif failures:
            lights.append({"key": "report", "label": "出报告", "ok": False, "state": "failed",
                           "detail": f"{failures} 封报告生成失败",
                           "failed_at": str(row.get("last_failed_at") or "")})
        elif row.get("last_report_at"):
            lights.append({"key": "report", "label": "出报告", "ok": False, "state": "failed",
                           "detail": "生成了但没发出去",
                           "failed_at": str(row.get("last_report_at") or "")})
        else:
            lights.append({"key": "report", "label": "出报告", "ok": False,
                           "state": "untested", "detail": "还没出过报告"})

        return lights

    def create_invite(self, label: str, days: int = 7) -> str:
        """Create a single-use invite and return the code exactly once.

        Only the hash is stored (same as the ``create-invite`` command), so the
        plaintext code cannot be recovered from the database afterwards.
        """
        code = secrets.token_urlsafe(18)
        expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=max(1, min(int(days), 90)))
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO invites(code_hash,label,expires_at) VALUES(?,?,?)",
                (token_hash(code), str(label)[:100], expires.isoformat(timespec="seconds")),
            )
        return code

    def list_invites(self, limit: int = 50) -> list[dict[str, Any]]:
        """Outstanding and used invites. Never returns hashes or plaintext codes."""
        now = utc_now()
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT i.label, i.expires_at, i.used_at, u.email AS used_by_email
                   FROM invites i LEFT JOIN users u ON u.id = i.used_by
                   ORDER BY i.expires_at DESC LIMIT ?""", (min(int(limit), 200),)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            if item["used_at"]:
                item["state"] = "used"
            elif str(item["expires_at"]) <= now:
                item["state"] = "expired"
            else:
                item["state"] = "available"
            result.append(item)
        return result

    def expire_invite(self, label: str) -> int:
        """Retire every unused invite with this label so it can no longer register."""
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                "UPDATE invites SET expires_at=? WHERE label=? AND used_by IS NULL AND expires_at>?",
                (now, str(label)[:100], now),
            )
        return cursor.rowcount or 0

    def active_mailboxes(self) -> list[dict[str, Any]]:
        """Mailboxes we should be reading: enabled, owned by an active account.

        **``immediate_enabled`` deliberately does NOT appear here.** It used to,
        and that made the switch a trap: turning off "send me a summary for each
        new mail" silently stopped us from reading the mailbox at all, so the
        user lost their task list, the daily digest's evidence and the whole
        point of the app -- while the setting was worded as a mail preference.
        Whether we deliver mail and whether we read mail are separate questions;
        "stop reading my mailbox" is ``mailboxes.enabled``.
        """
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT m.* FROM mailboxes m JOIN users u ON u.id=m.user_id
                   WHERE m.enabled=1 AND u.status='active'"""
            ).fetchall()
        return [dict(row) for row in rows]

    def all_mailboxes(self) -> list[dict[str, Any]]:
        """**每一个**已配置的邮箱，含暂停了的——`manage check-mailboxes` 的输入。

        与 `active_mailboxes` 的差别只有两处，都是为了「检查」而不是「取信」：
        暂停的也算（用户可能只是关掉了收信，他的配置仍然是我们要能回答的问题），
        被删掉的账号不算（那一行已经没有主了，而删除流程本来就会把行删掉，
        这里只是和其它十来处一样防御性地再过滤一次）。

        只读：这条路径不写任何一行，所以可以反复跑。
        """
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT m.* FROM mailboxes m JOIN users u ON u.id=m.user_id
                   WHERE u.status != 'deleted' ORDER BY m.email"""
            ).fetchall()
        return [dict(row) for row in rows]

    def report_delivery(self, user_id: str) -> dict[str, bool]:
        """这个账号要不要收我们的邮件：``immediate``（随信发出）与 ``daily``（简报）。

        一处定义，发信路径与界面都读它。缺 profile 时**默认发**（保持现状）——
        新增的开关不许在升级当天改变任何人的邮件。
        """
        with self.connect() as connection:
            row = connection.execute(
                "SELECT immediate_enabled,daily_enabled FROM profiles WHERE user_id=?", (user_id,)
            ).fetchone()
        return {"immediate": bool(row["immediate_enabled"]) if row else True,
                "daily": bool(row["daily_enabled"]) if row else True}

    def update_mailbox_poll(self, mailbox_id: str, *, last_uid: int, uid_validity: str, error: str = "") -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE mailboxes SET last_uid=?,uid_validity=?,last_polled_at=?,last_error=? WHERE id=?",
                (last_uid, uid_validity, utc_now(), error[:1000], mailbox_id),
            )

    def seed_legacy_processed_uids(self, user_id: str, uid_validity: str, uids: list[int],
                                   *, verification: str) -> dict[str, int]:
        """Import exact old-worker UID membership without creating reports.

        The legacy worker stored a sparse set of successful UIDs.  Converting
        that set to only ``max(uid)`` could silently skip an older UID that
        failed while a newer one succeeded.  Sent placeholder rows preserve
        the full set; the next poll deliberately starts with the lookback
        window, where SQLite's mailbox/UID unique key suppresses duplicates
        and any holes are queued normally.

        ``verification`` is required and records how UIDVALIDITY was
        established.  A wrong UIDVALIDITY makes every placeholder unmatchable
        during the rescan, which resends all historic mail, so this fails
        closed rather than trusting the caller's intent.
        Allowed: ``"server"`` (compared against the live mailbox) or
        ``"operator-override"`` (an explicit, deliberate override).
        """
        if verification not in {"server", "operator-override"}:
            raise ValueError(
                "迁移前必须校验 UIDVALIDITY：调用方需声明 verification='server'（已与服务器比对）"
                "或 verification='operator-override'（操作者显式强制）。"
            )
        if not uid_validity or not uid_validity.isdigit() or int(uid_validity) <= 0:
            raise ValueError("UIDVALIDITY 必须是邮箱连接测试返回的正整数。")
        normalized = sorted({int(uid) for uid in uids})
        if not normalized or normalized[0] <= 0:
            raise ValueError("旧状态中没有可迁移的正整数 UID。")
        now = utc_now()
        with self.connect() as connection:
            user = connection.execute(
                "SELECT status FROM users WHERE id=?", (user_id,)
            ).fetchone()
            if not user:
                raise KeyError("用户不存在。")
            if user["status"] != "paused":
                raise ValueError("迁移前必须先在网页中暂停此账户，避免 worker 同时收取邮件。")
            mailbox = connection.execute(
                "SELECT * FROM mailboxes WHERE user_id=?", (user_id,)
            ).fetchone()
            if not mailbox:
                raise KeyError("该用户尚未配置私人邮箱。")
            existing_validity = str(mailbox["uid_validity"] or "")
            if existing_validity and existing_validity != uid_validity:
                raise ValueError(
                    "新数据库记录的 UIDVALIDITY 与本次值不一致；邮箱可能已重建，不能自动迁移。"
                )
            before = connection.total_changes
            for uid in normalized:
                connection.execute(
                    """INSERT OR IGNORE INTO messages(
                           id,user_id,mailbox_id,uid_validity,imap_uid,subject,sender_name,
                           sender_address,received_at,importance,body,status,created_at
                       ) VALUES(?,?,?,?,?,'[legacy processed]','','','','normal',?,'sent',?)""",
                    (new_id("msg"), user_id, mailbox["id"], uid_validity, uid, b"", now),
                )
            inserted = connection.total_changes - before
            # Keep the cursor at zero so the first new-worker cycle performs
            # its bounded lookback and recovers any sparse holes safely.
            connection.execute(
                """UPDATE mailboxes SET last_uid=0,uid_validity=?,last_error='',updated_at=?
                   WHERE id=? AND user_id=?""",
                (uid_validity, now, mailbox["id"], user_id),
            )
        return {"seen": len(normalized), "inserted": inserted, "already_present": len(normalized) - inserted}

    def insert_message(self, user_id: str, mailbox_id: str, uid_validity: str, imap_uid: int,
                       message: dict[str, Any]) -> str | None:
        """Store one message, ignoring a re-delivery of the same original mail.

        Two copies that arrive through two forwarding rules have different IMAP
        UIDs, so UID de-duplication cannot see them. ``message_key`` (the RFC
        5322 Message-ID) makes them the same mail, so the second copy returns
        ``None`` and the caller must not queue it again. That is what stops a
        duplicate forward from producing a duplicate AI report.
        """
        now = utc_now()
        body_value = message.get("body", "")
        if not isinstance(body_value, bytes):
            body_value = str(body_value)[:20000]
        key = str(message.get("message_key") or "").strip()[:400] or None
        with self.connect() as connection:
            if key:
                existing = connection.execute(
                    "SELECT id FROM messages WHERE user_id=? AND message_key=?", (user_id, key)
                ).fetchone()
                if existing:
                    return None
            connection.execute(
                """INSERT OR IGNORE INTO messages(id,user_id,mailbox_id,uid_validity,imap_uid,message_key,subject,
                   sender_name,sender_address,received_at,importance,skip_reason,body,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (new_id("msg"), user_id, mailbox_id, uid_validity, imap_uid, key,
                 str(message.get("subject", ""))[:500], str(message.get("sender_name", ""))[:200],
                 str(message.get("sender_address", ""))[:320], str(message.get("received", ""))[:80],
                 str(message.get("importance", "normal"))[:40],
                 str(message.get("skip_reason", ""))[:200], body_value, now),
            )
            row = connection.execute(
                "SELECT id FROM messages WHERE mailbox_id=? AND uid_validity=? AND imap_uid=?",
                (mailbox_id, uid_validity, imap_uid),
            ).fetchone()
            if row:
                return str(row["id"])
            if key:
                row = connection.execute(
                    "SELECT id FROM messages WHERE user_id=? AND message_key=?", (user_id, key)
                ).fetchone()
                return str(row["id"]) if row else None
        return None

    def due_messages(self, limit: int = 20) -> list[dict[str, Any]]:
        """Messages ready to be analysed, minus accounts whose key is suspended.

        The `NOT EXISTS` clause is the queue-side half of the circuit breaker.
        Excluded rows keep `status='pending'` and their original `attempts`, so
        nothing is lost or marked failed: they simply become due again when
        `open_until` passes, which is also what makes the probe after the window
        cost exactly one generation slot rather than the whole backlog.
        """
        now = utc_now()
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM messages
                   WHERE status IN ('pending','failed')
                     AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                     AND NOT EXISTS (
                       SELECT 1 FROM key_circuits c
                       WHERE c.user_id = messages.user_id AND c.kind='model'
                         AND c.open_until IS NOT NULL AND c.open_until > ?
                     )
                   ORDER BY created_at LIMIT ?""",
                (now, now, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def message_for_user(self, user_id: str, message_id: str) -> dict[str, Any] | None:
        """One message row, plus the mailbox settings needed to re-read it.

        Scoped by ``user_id`` **in SQL**, not by a caller-side comparison: this
        backs a route that takes an id straight from the browser, and "no such
        message" and "somebody else's message" must be indistinguishable — both
        come back as ``None`` so the route can answer 404 to either.

        The join is safe to make because ``mailboxes.user_id`` is UNIQUE (one
        mailbox per account), so it cannot multiply the row.
        """
        with self.connect() as connection:
            row = connection.execute(
                """SELECT m.id, m.user_id, m.mailbox_id, m.uid_validity, m.imap_uid, m.subject,
                          m.sender_name, m.sender_address, m.received_at, m.status, m.message_key,
                          b.imap_host, b.imap_port, b.email AS mailbox_email, b.encrypted_password
                     FROM messages m JOIN mailboxes b ON b.id = m.mailbox_id
                    WHERE m.id=? AND m.user_id=?""",
                (message_id, user_id),
            ).fetchone()
        return dict(row) if row else None

    def setup_progress(self, user_id: str) -> dict[str, Any]:
        """The four things a new user has to get right, and which of them are done.

        Built for the setup page, which had no way to say *what was still
        missing*: four of the seven production accounts stalled with no mailbox
        at all, and the page they were looking at could not tell them so.

        Two of the four come straight from `verification_lights` -- the one
        definition of "跑通过" in this project -- rather than from a second set of
        rules written for this screen. The two that are not lights are the two a
        light cannot express:

        * **转发** is the step we cannot perform and cannot test from our side.
          The only evidence that the school's forwarding rule exists is that a
          message actually arrived, which is why it is stated as "还没有收到过"
          rather than "未配置" -- a quiet week is a normal week.
        * **报告** is `verification_lights["report"]`, i.e. generated *and* handed
          to SMTP, which is the only end-to-end proof.
        """
        with self.connect() as connection:
            row = connection.execute(
                """SELECT p.school_email, m.email AS mailbox_email,
                          m.last_polled_at, m.last_verified_at, m.updated_at AS mailbox_updated_at,
                          m.last_error AS mailbox_error,
                          (SELECT COUNT(*) FROM messages WHERE user_id = ? AND status != 'skipped')
                              AS analysed,
                          (SELECT MAX(sent_at) FROM reports WHERE user_id = ? AND status='sent')
                              AS last_sent_at,
                          (SELECT COUNT(*) FROM reports WHERE user_id = ? AND status='failed')
                              AS failed_reports,
                          (SELECT MAX(created_at) FROM reports WHERE user_id = ? AND status='failed')
                              AS last_failed_at
                     FROM users u
                     LEFT JOIN profiles  p ON p.user_id = u.id
                     LEFT JOIN mailboxes m ON m.user_id = u.id
                    WHERE u.id = ?""",
                (user_id, user_id, user_id, user_id, user_id),
            ).fetchone()
        row = dict(row) if row else {}
        lights = {item["key"]: item for item in self.verification_lights(row)}

        school = str(row.get("school_email") or "").strip()
        mailbox = str(row.get("mailbox_email") or "").strip()
        analysed = int(row.get("analysed") or 0)

        if not school and not mailbox:
            emails = {"ok": False, "state": "todo", "detail": "还没填写学校邮箱和私人转发邮箱。"}
        elif not school:
            emails = {"ok": False, "state": "todo", "detail": "还差 CityU 学校邮箱。"}
        elif not mailbox:
            emails = {"ok": False, "state": "todo", "detail": "还差私人转发邮箱（邮件要转到这里）。"}
        else:
            emails = {"ok": True, "state": "ok", "detail": f"报告会发到 {mailbox}。"}

        return {
            "emails": emails,
            "mailbox": lights["mailbox"],
            "forwarding": self.forwarding_step(row, analysed),
            "report": lights["report"],
        }

    # 邮箱接通之后多久还没有收到过任何一封 CityU 来信，才算「不对劲」。
    #
    # 一个安静的周末不是故障：学校没发信的时候，这一格本来就该一直是灰的。
    # 但也不能永远不说话——唯一看不见「转发的证据一直缺席」的人，正是本人。
    # 一天是刻意的折中：够长，不至于把周末当成故障；够短，不至于让人白等一周。
    NO_SCHOOL_MAIL_HOURS = 24.0

    @classmethod
    def forwarding_step(cls, row: dict[str, Any], analysed: int) -> dict[str, Any]:
        """The one judgement about whether the school's forwarding rule works.

        The rule cannot be tested from our side and the school will not tell us:
        **the only evidence that forwarding exists is that a message actually
        arrived**. So this step has three states, and the middle one is the one
        that used to be silent -- a mailbox that connects perfectly and then
        delivers nothing, forever, with nobody told.

        What counts as "arrived" is mail from an allowed sender
        (:meth:`count_analysed_messages`), not "the inbox is not empty": a
        newsletter sent straight to the private address proves nothing about the
        school's rule. The wording says since when and what is counted, because
        both are things the reader can check and contradict.
        """
        if analysed > 0:
            return {"ok": True, "state": "ok",
                    "detail": f"已经处理过 {analysed} 封从 CityU 转来的邮件。"}
        connected = parse_utc(row.get("mailbox_updated_at"))
        if connected is None:
            return {"ok": False, "state": "todo",
                    "detail": "还没有收到过任何 CityU 邮件——如果第 2 步还没做，现在去做。"}
        hours = (dt.datetime.now(dt.timezone.utc) - connected).total_seconds() / 3600
        if hours < cls.NO_SCHOOL_MAIL_HOURS:
            return {"ok": False, "state": "todo",
                    "detail": "还没有收到过任何 CityU 邮件——如果第 2 步还没做，现在去做。"}
        return {
            "ok": False, "state": "warn",
            "detail": (f"邮箱接通已经 {human_hours(hours)}，但一封 CityU 来信都没到过。"
                       "回第 2 步检查那条转发规则是不是还开着——最常见的原因是规则没保存成功、"
                       "被关掉了、或者转发地址填成了别的邮箱。（这里只统计发件人是 CityU 地址的邮件；"
                       "别人用私人邮箱写给你的信不算。）"),
        }

    def school_mail_evidence(self, since: Optional[str] = None) -> dict[str, dict[str, Any]]:
        """Per-mailbox proof that school mail actually arrived, keyed by mailbox id.

        This is the only evidence the *forwarding* half of the product works --
        we can see our own poll succeed, we cannot see the rule the user set in
        CityU's webmail. ``skipped`` rows are mail from senders outside the
        allowed domains (someone's newsletter landing in the same inbox), so
        counting them would turn "your inbox is not empty" into "forwarding
        works". Same rule as :meth:`count_analysed_messages`, computed in one
        query for every mailbox instead of one query per mailbox.

        ``since=None`` means "ever", which is what answers 「从没收到过」 versus
        「最近一封是三天前」.
        """
        sql = ("SELECT mailbox_id, COUNT(*) AS n, MAX(received_at) AS last_at"
               "  FROM messages WHERE status != 'skipped'")
        params: tuple[Any, ...] = ()
        if since:
            sql += " AND received_at >= ?"
            params = (since,)
        sql += " GROUP BY mailbox_id"
        with self.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return {str(row["mailbox_id"]): {"count": int(row["n"]), "last_at": row["last_at"]}
                for row in rows}

    def count_analysed_messages(self, user_id: str) -> int:
        """How many messages ever passed the sender filter for this user.

        ``skipped`` rows are mail we deliberately did not analyse (someone
        else's newsletter arriving in the same inbox), so they must not count as
        "your forwarding works". Everything else — pending, processing, sent,
        failed — came from an allowed sender.
        """
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE user_id=? AND status!='skipped'",
                (user_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    def mark_message_skipped(self, message_id: str, reason: str) -> None:
        """Record that a message was deliberately not analysed (kept for audit).

        Used by the sender-domain filter: mail outside the allowed domains keeps
        a row and a human-readable reason, so "we did not process it" is
        provable and reportable instead of a silent deletion.
        """
        with self.connect() as connection:
            connection.execute(
                "UPDATE messages SET status='skipped',skip_reason=?,next_attempt_at=NULL WHERE id=?",
                (str(reason)[:200], message_id),
            )

    def mark_message_skipped_by_uid(self, mailbox_id: str, uid_validity: str, imap_uid: int,
                                    reason: str) -> None:
        """Mark a freshly stored message as deliberately not analysed."""
        with self.connect() as connection:
            connection.execute(
                """UPDATE messages SET status='skipped',skip_reason=?,next_attempt_at=NULL
                   WHERE mailbox_id=? AND uid_validity=? AND imap_uid=?""",
                (str(reason)[:200], mailbox_id, uid_validity, imap_uid),
            )

    def mark_message_processing(self, message_id: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """UPDATE messages SET status='processing',attempts=attempts+1,last_error=''
                   WHERE id=? AND status IN ('pending','failed')""", (message_id,)
            )
        return cursor.rowcount == 1

    def recover_inflight(self) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE messages SET status='failed',last_error='worker restarted before completion',next_attempt_at=? WHERE status='processing'",
                (utc_now(),),
            )

    def fail_message(self, message_id: str, error: str, retry_at: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE messages SET status='failed',last_error=?,next_attempt_at=? WHERE id=?",
                (error[:1000], retry_at, message_id),
            )

    def finish_message(self, message_id: str) -> None:
        # Raw body is no longer needed after delivery. Keeping metadata and the
        # derived report allows daily summaries without retaining full mail.
        self._finish_message_with(message_id, "sent")

    def hold_message(self, message_id: str) -> None:
        """The mail is done -- report generated -- but nothing was sent.

        The owner turned report mail off. This must be its own status rather
        than ``sent`` (nothing was sent), ``failed`` (nothing went wrong) or
        ``skipped`` (that one means "not our mail" and is counted as such in
        the digest and in the evidence that forwarding ever worked).

        The body is wiped exactly as on delivery: not sending a report is a
        delivery preference, never a reason to keep someone's mail.
        """
        self._finish_message_with(message_id, "held")

    def _finish_message_with(self, message_id: str, status: str) -> None:
        if status not in {"sent", "held"}:
            raise ValueError("状态只能是 sent 或 held。")
        with self.connect() as connection:
            connection.execute(
                "UPDATE messages SET status=?,body='',next_attempt_at=NULL,last_error='' WHERE id=?",
                (status, message_id),
            )

    def create_report(self, *, user_id: str, message_id: str | None, kind: str, subject: str,
                      body: str | bytes, sent_to: str, report_date: str = "") -> str:
        report_id = new_id("rpt")
        with self.connect() as connection:
            connection.execute(
                """INSERT INTO reports(id,user_id,message_id,kind,subject,body_markdown,sent_to,report_date,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (report_id, user_id, message_id, kind, subject[:500], body, sent_to[:320], report_date, utc_now()),
            )
        return report_id

    def mark_report_sent(self, report_id: str) -> None:
        with self.connect() as connection:
            connection.execute("UPDATE reports SET status='sent',sent_at=?,last_error='' WHERE id=?", (utc_now(), report_id))

    def fail_report(self, report_id: str, error: str) -> None:
        """把一份报告记成失败——**但不许把已经发出去的那份退回失败**。

        为什么要有这个 `WHERE`：`PilotService.process_message` 的顺序是
        SMTP → `mark_report_sent` → `finish_message`。最后那一步（收尾：清正文、清重试）
        出错时，统一异常处理会走到这里，于是一份**用户已经收到的**报告被改回 `failed`、
        邮件同时被放回队列；下一轮重试看到 `status != 'sent'`，就**再发一封**
        （`send_report` 每次还新生成一个 Message-ID，用户那边是两封不同的信）。
        2026-09-22 那份安全/可靠性审查把它列成 B2，本地故障注入复现过。

        `sent` 是终态：SMTP 已经收下了。收尾失败是「我们的记账没做完」，不是「投递失败」——
        那种情况错误记在**邮件**那一行（`fail_message` 已经做了），报告保持 `sent`，
        下一次轮询只把收尾补完（`process_message` 开头那条 `existing['status'] == 'sent'`
        的守卫就是干这个的），不会再发一次。
        """
        with self.connect() as connection:
            connection.execute(
                "UPDATE reports SET status='failed',last_error=? WHERE id=? AND status!='sent'",
                (error[:1000], report_id))

    def immediate_reports_between(self, user_id: str, start_utc: str, end_utc: str) -> list[str | bytes]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT r.body_markdown FROM reports r JOIN messages m ON m.id=r.message_id
                   WHERE r.user_id=? AND r.kind='immediate' AND r.status='sent'
                   AND m.received_at>=? AND m.received_at<? ORDER BY m.received_at""", (user_id, start_utc, end_utc)
            ).fetchall()
        return [row["body_markdown"] for row in rows]

    def messages_between(self, user_id: str, start_utc: str, end_utc: str) -> list[dict[str, Any]]:
        """Every message received in a window, with its immediate report body.

        The daily digest uses this instead of a model re-summary so a mail can
        never disappear from the brief: rows without a report surface as
        failures/unprocessed rather than being skipped.
        """
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT m.id,m.id AS message_id,m.subject,m.sender_name,m.sender_address,
                          m.received_at,m.importance,m.status,m.last_error,m.attempts,
                          r.id AS report_id, r.body_markdown, r.status AS report_status
                   FROM messages m
                   LEFT JOIN reports r ON r.message_id=m.id AND r.kind='immediate'
                   WHERE m.user_id=? AND m.received_at>=? AND m.received_at<?
                   ORDER BY m.received_at""", (user_id, start_utc, end_utc)
            ).fetchall()
        return [dict(row) for row in rows]

    def today_reports(self, user_id: str, start_utc: str, end_utc: str) -> list[dict[str, Any]]:
        """Immediate reports for the dashboard's "what must I do today" list."""
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT r.id,r.subject,r.body_markdown,r.status,r.created_at,
                          m.id AS message_id,m.subject AS message_subject,m.sender_name,
                          m.sender_address,m.received_at,m.importance,m.status AS message_status
                   FROM reports r JOIN messages m ON m.id=r.message_id
                   WHERE r.user_id=? AND r.kind='immediate'
                     AND m.received_at>=? AND m.received_at<?
                   ORDER BY m.received_at DESC""", (user_id, start_utc, end_utc)
            ).fetchall()
        return [dict(row) for row in rows]

    def messages_by_ids(self, user_id: str, message_ids: list[str]) -> list[dict[str, Any]]:
        if not message_ids:
            return []
        placeholders = ",".join("?" for _ in message_ids)
        with self.connect() as connection:
            rows = connection.execute(
                f"""SELECT id,subject,sender_name,sender_address,received_at,importance,status,last_error
                    FROM messages WHERE user_id=? AND id IN ({placeholders})""",
                (user_id, *message_ids),
            ).fetchall()
        return [dict(row) for row in rows]

    def record_mailbox_verification(self, mailbox_id: str, *, error: str = "") -> None:
        """Remember the result of an explicit read-only IMAP test.

        Never touches ``last_uid``/``uid_validity``: verifying a mailbox must not
        change which messages the worker will consume.
        """
        with self.connect() as connection:
            connection.execute(
                "UPDATE mailboxes SET last_verified_at=?,last_verify_error=?,last_error=? WHERE id=?",
                (utc_now(), error[:1000], error[:1000], mailbox_id),
            )

    def daily_users(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT u.id,u.email,p.timezone,p.daily_time,m.report_to FROM users u
                   JOIN profiles p ON p.user_id=u.id JOIN mailboxes m ON m.user_id=u.id
                   WHERE u.status='active' AND p.daily_enabled=1 AND m.enabled=1"""
            ).fetchall()
        return [dict(row) for row in rows]

    def failed_reports_summary(self) -> dict[str, int]:
        """失败的报告拆成两个数：**逐封邮件的** 与 **每日简报的**。

        为什么要拆：2026-09-18 用户报「后台显示 5 个报告失败，但我刷新下发情况又没有」。
        两个数字都是对的，错的是它们被当成一回事——「下发情况」是一行一封**邮件**，
        而每日简报按设计没有 `message_id`（它汇总一整天），所以简报失败永远不可能出现在
        那张表里。一个数字里混着两种东西，就必然有人对不上账。
        """
        with self.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total,"
                " SUM(CASE WHEN kind='daily' THEN 1 ELSE 0 END) AS digests"
                " FROM reports WHERE status='failed'").fetchone()
        total = int(row["total"] or 0)
        digests = int(row["digests"] or 0)
        return {"total": total, "digests": digests, "per_mail": total - digests}

    def failed_digests(self, limit: int = 20) -> list[dict[str, Any]]:
        """失败的那几封每日简报：日期、收件地址、错误——给「下发情况」一个交代。

        没有这些行，运营者能看到的只有健康卡上那个数字和一个空的列表——那正是这一轮
        用户报上来的困惑。地址是运营者自己管的账号，缩到域名之外的完整地址只出现在
        管理端（和邮件面板其它行一样）。
        """
        with self.connect() as connection:
            rows = connection.execute(
                """SELECT r.report_date, r.sent_to, r.last_error, r.created_at
                     FROM reports r WHERE r.status='failed' AND r.kind='daily'
                    ORDER BY r.created_at DESC LIMIT ?""",
                (max(1, min(int(limit), 100)),)).fetchall()
        return [dict(row) for row in rows]

    def daily_report_exists(self, user_id: str, report_date: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM reports WHERE user_id=? AND kind='daily' AND report_date=? AND status='sent'", (user_id, report_date)
            ).fetchone()
        return bool(row)

    def daily_report_for_date(self, user_id: str, report_date: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM reports WHERE user_id=? AND kind='daily' AND report_date=?", (user_id, report_date)
            ).fetchone()
        return dict(row) if row else None

    def report_for_message(self, message_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM reports WHERE message_id=? AND kind='immediate'", (message_id,)
            ).fetchone()
        return dict(row) if row else None

    def set_user_status(self, user_id: str, status: str) -> None:
        if status not in {"active", "paused", "deleted"}:
            raise ValueError("无效的用户状态。")
        with self.connect() as connection:
            if status == "deleted":
                # Detach and retire the invite first. Databases created before
                # invites.used_by gained ON DELETE SET NULL still enforce the
                # plain reference, so the delete would fail; and a retired code
                # must not become reusable just because its user left.
                connection.execute(
                    "UPDATE invites SET used_by=NULL, expires_at=? WHERE used_by=?",
                    (utc_now(), user_id),
                )
                # Foreign-key cascades then remove encrypted mailbox/API
                # secrets, sessions, profiles, messages, reports and feedback
                # as part of privacy deletion.
                connection.execute("DELETE FROM users WHERE id=?", (user_id,))
            else:
                connection.execute("UPDATE users SET status=? WHERE id=?", (status, user_id))

    def upsert_feedback(self, user_id: str, report_id: str, rating: str, note: str) -> None:
        if rating not in {"useful", "not_useful"}:
            raise ValueError("无效的反馈值。")
        with self.connect() as connection:
            owned = connection.execute("SELECT 1 FROM reports WHERE id=? AND user_id=?", (report_id, user_id)).fetchone()
            if not owned:
                raise KeyError("报告不存在。")
            connection.execute(
                """INSERT INTO feedback(id,user_id,report_id,rating,note,created_at) VALUES(?,?,?,?,?,?)
                   ON CONFLICT(user_id,report_id) DO UPDATE SET rating=excluded.rating,note=excluded.note,created_at=excluded.created_at""",
                (new_id("fb"), user_id, report_id, rating, note[:1000], utc_now()),
            )
