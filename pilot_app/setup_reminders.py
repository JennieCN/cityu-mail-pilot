"""The reminder sent to accounts that registered but never finished setting up.

One definition, three consumers: the admin console's one-click button, the
`tools/notify_stalled.py` CLI, and the tests. They must agree, because the thing
being decided is *which sentence a person needs* -- and sending the wrong one is
worse than sending nothing: telling somebody who already configured a mailbox to
"go turn on IMAP" reads as "everything you did was pointless".

Four groups
-----------
* ``never``   -- no usable mailbox at all. They need to know the four steps exist
  and are short.
* ``refused`` -- a mailbox is configured but the mail server keeps rejecting it.
  They need to know that 授权码 is not their mailbox login password.
* ``provider`` -- the mailbox's provider stopped accepting app passwords
  altogether (Microsoft consumer accounts, 2026-09-16). No credential can fix
  this; the letter has to say so, or the person spends an evening generating
  authorisation codes that cannot work.
* ``no_mail`` -- the mailbox connects perfectly and nothing has ever arrived
  (added 2026-09-16). They need to know that the school side of the chain is the
  one nobody has proved: **forwarding cannot be tested from here, and the only
  evidence it exists is that a message actually arrived.** This is the third
  face of the same failure mode, and it was the last one still silent -- a
  fully-configured account that receives nothing forever looks, from the
  console, exactly like an account that is merely new.

The provider-specific steps are read from :mod:`pilot_app.mailpresets`, the same
source the in-app wizard renders, so this mail cannot drift from the product. A
reminder that sends someone to a menu that no longer exists is a support ticket.
The ``no_mail`` letter instead renders the *school* side (CityU's own published
forwarding steps, see :func:`_school_forward_steps`) -- the private-mailbox
steps are the ones that account has already finished.

Why this is not just the sentinel
---------------------------------
`setup_gap` already makes a stalled account *visible to the operator*, and the
sentinel already reports `setup_stalled`. Visible is not fixed: **the one person
who cannot see the problem is the person it belongs to.** They receive nothing,
nothing fails, and nothing tells them anything was expected of them.

Sending is deliberately awkward
-------------------------------
* **Never twice by accident.** Each delivery is recorded in `app_settings` under
  ``setup_reminder:<user_id>``; a second run skips anyone already written to.
* **Written after the send, not before.** The other order would mark somebody as
  told when the send had actually failed -- the one way to lose a person.
* **A brand-new account is left alone** (see :data:`MIN_AGE_HOURS`): somebody who
  registered ten minutes ago is not stuck, they are busy.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
from typing import Any, Collection

from . import mailpresets
from .alerting import send_as_operator
from .database import LAST_SEEN_SINCE_KEY, Database, parse_utc, utc_now
from .security import SecretBox

REMINDER_KEY = "setup_reminder:"
# Do not pounce on someone who is halfway through the wizard right now.
MIN_AGE_HOURS = 6.0
# How many letters one press of the button may send.
#
# nginx allows a 330s response (it was raised for the slow model calls), so the
# batch is bounded by what a *pathological* SMTP server can do inside that:
# `mailio.send_report` sets 30s per socket operation, so ten letters is the point
# where even a host that hangs on every step still answers before the proxy gives
# up. A 504 while mail is quietly going out is the worst outcome here -- the
# operator would press again, and "again" is exactly what the bookkeeping makes
# safe, but they would not know that.
#
# The idempotent bookkeeping is what makes a cap harmless: the next press
# continues with whoever is left instead of starting over.
BATCH_LIMIT = 10

GAP_NEVER = "never"
GAP_REFUSED = "refused"
GAP_NO_MAIL = "no_mail"
# 邮箱服务商自己不再允许用授权码收信（微软个人版：Basic auth 已关）。这不是用户
# 能修好的事——换授权码、开双重验证都没用，只能换一个邮箱。
GAP_PROVIDER = "provider"

# 邮箱接通之后多久还没收到过任何 CityU 来信，才值得自动打扰本人。
# 定义在 `Database`（设置向导那一格用的是同一个判断），这里只是取个好念的名字。
NO_MAIL_HOURS = Database.NO_SCHOOL_MAIL_HOURS


def app_url() -> str:
    origin = os.environ.get("INFE_PILOT_ORIGIN", "").rstrip("/")
    return f"{origin}/app" if origin else "/app"


#: 提醒信里的 ``{link}`` 指向哪个板块（`app.js` 的 `NAV` 键）。
LINK_SECTION = "mailbox"


def step_url() -> str:
    """``{link}`` 该填什么：**设置向导那一页**，不是首页。

    这四封信都指向同一个板块，而且这不是图省事——向导的 1–4 步（填这两个邮箱、
    让 CityU 转过来、拿授权码、保存并检查）**全都住在「邮箱设置」这一页**，
    四格进度就钉在它顶上：收信人一眼能看出自己灰着的是哪一格、下面哪一步还没做。
    指到首页只会让他再找一次「设置向导在哪」——那正是这封信想省掉的那一次。

    （"他卡在哪一步"的判据仍在 `group_for`/`setup_gap` 那一处；这里只负责把
    收信人送到那一步所在的地方。）
    """
    return f"{app_url()}#/{LINK_SECTION}"


def contact_wechat() -> str:
    """The operator's WeChat id, if this instance publishes one.

    Read from the environment rather than written into the code for the same
    reason `INFE_PILOT_SOURCE_URL` is: this repository is public, and a
    self-hosted copy must not tell its users to contact somebody else's personal
    account. Unset means the line is simply absent.
    """
    return os.environ.get("INFE_PILOT_CONTACT_WECHAT", "").strip()


def _contact_lines() -> str:
    lines = ["- 每一步都写清了在哪里点；也可以直接回这封邮件问我。"]
    wechat = contact_wechat()
    if wechat:
        lines.append(f"- 还是搞不定可以直接找我：微信 {wechat}（说明你用的是哪个邮箱就行）。")
    return "\n".join(lines)


# The wording is editable from the console. What is *not* editable is the shape
# of the message: the placeholders below are substituted at send time, and a
# template naming anything else is refused rather than sent with a literal
# "{linkk}" in it -- a typo that ships to a real person's inbox is not something
# they can report back to us.
TEMPLATE_KEYS = {GAP_NEVER: "reminder_template:never", GAP_REFUSED: "reminder_template:refused",
                 GAP_NO_MAIL: "reminder_template:no_mail",
                 GAP_PROVIDER: "reminder_template:provider"}
PLACEHOLDERS = ("{link}", "{wechat}", "{steps}", "{mailbox}")
TEMPLATE_MAX = 4000
# 面板上正文编辑区的顺序，也是预览的顺序（前端按它画，不再各写一份）。
GROUPS = (GAP_NEVER, GAP_REFUSED, GAP_NO_MAIL, GAP_PROVIDER)


def _never_default() -> str:
    """For somebody who never filled in a private mailbox."""
    return (
        "你好，\n\n"
        "你在 CityU Mail Pilot 注册了账号，但还没有填「私人邮箱」——"
        "所以到现在为止，一封信的摘要都没有发给你。\n\n"
        "整件事只有四步，大概 3 分钟：\n"
        "1. 在 CityU Outlook 里设一条转发规则，把学校邮箱转到一个你自己常用的私人邮箱"
        "（QQ / 163 / Gmail 都行）；\n"
        "2. 在那个私人邮箱里开启 IMAP/SMTP 服务，生成一个「授权码」——"
        "注意它不是你的邮箱登录密码；\n"
        "3. 打开设置页，填上私人邮箱和授权码，保存；\n"
        "4. 页面顶部有四个格子，灰着的那格就是还没完成的那一步。\n\n"
        "- 打开设置向导：{link}\n"
        "{wechat}\n\n"
        "目前免费：在另行通知前，模型调用默认用管理员提供的 key（费用由管理员承担），"
        "你也可以在「AI 模型」里换成自己的。\n\n"
        "如果暂时不打算用了，回一句「不用了」就行，我不会再打扰你。"
    )


def _refused_default() -> str:
    """For somebody whose mailbox exists but keeps refusing our login."""
    return (
        "你好，\n\n"
        "你的账号已经配好了私人邮箱，但那个邮箱一直拒绝我们登录"
        "（邮箱服务器返回的是「登录名或密码错误」）——"
        "所以到现在为止，一封信的摘要都没有发给你。\n\n"
        "最常见的原因是授权码填成了邮箱的登录密码，这两者不是一回事：\n\n"
        "授权码是专门发给程序用的另一套密码，要单独生成，"
        "而且随时可以在邮箱设置里作废重发。\n\n"
        "{steps}\n"
        "- 拿到新的授权码后，打开 {link} 的第 3 步重新填一次并保存。\n"
        "- 保存后页面顶部的四个格子会告诉你有没有接通。\n"
        "{wechat}\n\n"
        "如果你确认授权码没错，也可能是这个邮箱还没开启 IMAP 服务，"
        "页面上第 3 步有对应的说明。"
    )


def _no_mail_default() -> str:
    """For somebody whose mailbox works and has never delivered a school mail.

    The letter has to do three things at once, and the third is the one that
    makes it honest: say what was noticed, say what is most likely wrong, and
    admit the other explanation (the school simply has not written). Telling a
    person their forwarding is broken when the school is just quiet costs us the
    only thing this project has -- that a message from it is worth reading.
    """
    return (
        "你好，\n\n"
        "你的私人邮箱已经接好了，我也一直在只读地读它——"
        "但到现在为止，一封发件人是 CityU 的邮件都没有出现过，"
        "所以一封信的摘要都还没有发给你。\n\n"
        "这只有两种可能：\n\n"
        "一、CityU 那边的转发规则没有生效。最常见的是：规则没保存成功、"
        "后来被关掉了、或者转发地址填成了别的邮箱。官方步骤：\n\n"
        "{steps}\n"
        "二、学校这段时间确实没有给你发过邮件。假期或刚开学时很正常，"
        "那就先不用管，我会继续等。\n\n"
        "想马上确认转发通不通：用你的 CityU 邮箱给自己发一封测试邮件"
        "（寄给同一个学校地址就行），一分钟后再打开你的私人邮箱看看有没有到——"
        "到了就说明规则是好的，我也会在下一轮把它认出来。\n\n"
        "- 你的设置向导：{link}\n"
        "{wechat}\n\n"
        "我只统计发件人是 CityU 地址的邮件（@cityu.edu.hk / @my.cityu.edu.hk）："
        "同学用私人邮箱写给你的信会被我跳过，不算在「收到过」里面。\n"
        "如果转发一直是好的、只是学校没发过信，回一句「转发没问题」我就不再提这件事。"
    )


def _provider_default() -> str:
    """For somebody whose mailbox provider stopped accepting app passwords.

    The one thing this letter must NOT do is what the old single "refused"
    letter did: tell them their 授权码 is probably their login password. On a
    Microsoft consumer mailbox there is no app password that will ever work, so
    that sentence sends somebody to generate a credential that cannot succeed --
    and the conclusion they draw is that our software is broken.
    """
    return (
        "你好，\n\n"
        "你的账号已经配好了私人邮箱，但那个邮箱我们已经读不到了。\n\n"
        "原因不在你：{mailbox} 的服务商（微软 Outlook / Hotmail 属于这一类）"
        "已经停用了「账号密码 / 授权码」这种登录方式，只允许网页上用 OAuth 授权。"
        "所以无论你重新生成多少次授权码、开不开双重验证，我们都登不进去——"
        "这不是你的设置错了。\n\n"
        "要恢复收信，只能换一个还支持授权码的私人邮箱（QQ 邮箱、163、Gmail 都行）：\n\n"
        "{steps}\n"
        "换好之后，在设置页把「私人转发邮箱」改成新的那一个，重新填一次授权码。\n"
        "你原来那个 Outlook 邮箱里的信不会丢，也不会被我们动过——我们从来只读，不删不改。\n\n"
        "- 设置向导：{link}\n"
        "{wechat}"
    )


def default_template(group: str) -> str:
    if group == GAP_REFUSED:
        return _refused_default()
    if group == GAP_NO_MAIL:
        return _no_mail_default()
    if group == GAP_PROVIDER:
        return _provider_default()
    return _never_default()



def template_for(db: Database | None, group: str) -> str:
    """The template actually used: the operator's text, or ours."""
    key = TEMPLATE_KEYS.get(group)
    if db is not None and key:
        stored = str(db.get_setting(key) or "").strip()
        if stored:
            return stored
    return default_template(group)


class TemplateError(ValueError):
    """Refused before it can reach anybody: an unknown or missing placeholder."""


def check_template(text: str) -> str:
    """Validate an operator's template, or raise with the reason."""
    body = str(text or "")
    if not body.strip():
        raise TemplateError("正文不能是空的。")
    if len(body) > TEMPLATE_MAX:
        raise TemplateError(f"正文太长了（{len(body)} 字，上限 {TEMPLATE_MAX}）。")
    for name in re.findall(r"\{[a-z_]*\}", body):
        if name not in PLACEHOLDERS:
            raise TemplateError(
                f"不认识的占位符 {name}；可用的只有 {'、'.join(PLACEHOLDERS)}。")
    if "{link}" not in body:
        raise TemplateError("正文里必须保留 {link}，否则收信人不知道该去哪里设置。")
    return body


def set_template(db: Database, group: str, text: str, *, actor: str = "console") -> str:
    """Save (or with an empty string, reset) one template. Returns what is stored."""
    if group not in TEMPLATE_KEYS:
        raise TemplateError("未知的模板。")
    body = str(text or "").strip()
    if body:
        check_template(body)
        db.set_setting(TEMPLATE_KEYS[group], body, actor=actor)
        return body
    db.delete_setting(TEMPLATE_KEYS[group])
    return default_template(group)


def _school_forward_steps() -> str:
    """The school side of the chain, as CityU itself documents it.

    Same principle as :func:`_provider_steps` and the same reason: a reminder
    that sends somebody to a menu that has moved is a support ticket. The URL is
    CityU's own published FAQ (checked 2026-09-16), and the two warnings at the
    end are theirs, not ours -- a forwarding loop looks exactly like "forwarding
    never worked" from the outside.
    """
    return (
        "1. 用 CityU 账号登录 Outlook 网页版：\n"
        "   https://email.cityu.edu.hk/home/weblogon_o365_student.htm\n"
        "2. 点右上角的齿轮 → 「查看全部 Outlook 设置」(View all Outlook settings)。\n"
        "3. 「邮件」→「转发」(Forwarding)：勾选「启用转发」，填上你的私人邮箱，\n"
        "   并勾选「保留已转发邮件的副本」。\n"
        "4. 点保存，弹出的确认框选「是」。\n"
        "   CityU 官方说明："
        "https://www.cityu.edu.hk/csc/deptweb/support/faq/email/o365/autoforward.htm\n"
        "   注意：不要转给自己，也不要在两个邮箱之间互转——会造成循环，结果反而是收不到信。\n"
    )


def _switch_mailbox_steps() -> str:
    """How to move the chain onto a mailbox that still works.

    Deliberately *not* `_provider_steps`: those tell you how to generate an
    authorisation code at the provider you already have, which is the step that
    cannot work here. The school side is reused, because the forwarding rule has
    to end up pointing at the new address either way.
    """
    return (
        "1. 选一个新的私人邮箱（QQ 邮箱、163、Gmail 都可以），在它的网页版里开启 IMAP/SMTP。\n"
        "2. 生成一个「客户端授权码」（不是登录密码），先复制下来。\n"
        "3. 回 CityU Outlook，把那条转发规则的目标改成这个新邮箱"
        "（规则不用删，直接改地址就行）。\n"
    )


def _provider_steps(mailbox_email: str) -> str:
    preset_id = mailpresets.preset_id_for_email(mailbox_email)
    preset = mailpresets.PRESETS_BY_ID.get(preset_id) or {}
    steps = preset.get("steps") or []
    if steps:
        return f"以{preset.get('label', '这个邮箱')}为例：\n" + "".join(
            f"{index}. {step}\n" for index, step in enumerate(steps, start=1))
    return ("在你邮箱网页版的「设置」里找到 IMAP/SMTP 服务，开启它，"
            "然后按提示生成一个「客户端授权码」。\n")


def render_body(db: Database | None, group: str, mailbox_email: str = "") -> str:
    """One message body, with the operator's text and our values filled in."""
    text = template_for(db, group)
    # `{steps}` 是「这个人还差的那几步」：还没接上邮箱的人需要授权码教程，
    # 邮箱已经通了、只是没有信的人需要的是**学校那一边**的步骤。
    if group == GAP_NO_MAIL:
        steps = _school_forward_steps()
    elif group == GAP_PROVIDER:
        steps = _switch_mailbox_steps()
    else:
        steps = _provider_steps(mailbox_email)
    return (text.replace("{link}", step_url())
                .replace("{wechat}", _contact_lines())
                .replace("{steps}", steps)
                .replace("{mailbox}", mailbox_email or "你的私人邮箱"))


def never_configured_body(db: Database | None = None) -> str:
    return render_body(db, GAP_NEVER)


def refused_login_body(mailbox_email: str, db: Database | None = None) -> str:
    return render_body(db, GAP_REFUSED, mailbox_email)


def message_for(row: dict[str, Any], db: Database | None = None) -> tuple[str, str]:
    """(subject, body) for one account -- the only place the wording is chosen."""
    if row["group"] == GAP_PROVIDER:
        return ("你的 CityU Mail Pilot：那个邮箱已经不能用了，需要换一个",
                render_body(db, GAP_PROVIDER, str(row.get("mailbox_email") or "")))
    if row["group"] == GAP_NEVER:
        return ("你的 CityU Mail Pilot 还差一步：把私人邮箱接上",
                render_body(db, GAP_NEVER, str(row.get("mailbox_email") or "")))
    if row["group"] == GAP_NO_MAIL:
        return ("你的 CityU Mail Pilot 一直没收到过 CityU 的邮件",
                render_body(db, GAP_NO_MAIL, str(row.get("mailbox_email") or "")))
    return ("你的 CityU Mail Pilot 收不到信：邮箱登录被拒绝了",
            render_body(db, GAP_REFUSED, str(row.get("mailbox_email") or "")))


def group_for(db: Database, row: dict[str, Any]) -> str:
    """``never`` | ``refused`` | ``no_mail`` | ``""`` -- which sentence this account needs.

    Three judgements, not one, and the second and third are easy to miss: an
    account whose auth code is wrong has a `last_polled_at` (the stamp is written
    on failure too), so its `setup_gap` is **empty** and `stalled_setups` never
    lists it. Only the receive light can see it. And an account whose mailbox is
    perfectly healthy but which has never received a single allowed-sender
    message looks finished from every other angle -- `setup_gap` is empty, all
    four lights except the last are green, and the person it belongs to is told
    nothing, forever.

    ``analysed_count`` is read from the row rather than queried here so that a
    list of accounts costs one query. A row without that column (a bare `users`
    row, or a caller that built one by hand) is deliberately *not* judged on it:
    guessing "no mail ever arrived" from a missing field is exactly the kind of
    false verdict that costs somebody an unnecessary letter.
    """
    if db.setup_gap(row) == "no_mailbox":
        return GAP_NEVER
    lights = [light for light in db.verification_lights(row) if light.get("key") == "mailbox"]
    if lights and not lights[0].get("ok"):
        # 「登不进去」有两种，句子完全不同：一种是授权码填错（他改得动），
        # 另一种是服务商不再允许用授权码（他改不动，只能换邮箱）。
        if db.mailbox_needs_another_provider(row):
            return GAP_PROVIDER
        return GAP_REFUSED
    if "analysed_count" in row and int(row.get("analysed_count") or 0) == 0:
        return GAP_NO_MAIL
    return ""


def needs_notice(row: dict[str, Any]) -> bool:
    """Whether the automatic buttons still owe this account a letter.

    A stamp is a record of *which* sentence was sent, not just that one was:
    somebody who was told 「你还没配好」, then configured everything and now
    receives nothing, needs a different letter about a different problem. The old
    stamp must not cover it -- "already notified" is about the person having been
    told, and they have not been told *this*.

    ``collect`` hands out the raw stored value (``时间|组``) and ``panel_rows``
    hands out the split one, so both shapes are accepted here rather than at the
    call sites -- the first version of this function read only the split keys and
    therefore re-sent to everybody whose stamp it could not parse. A stamp with
    no group at all is from before the group was recorded: it counts as "already
    told" (the old behaviour), because guessing which letter somebody received is
    exactly the kind of guess that mails a person twice.
    """
    stamp, _, stored = str(row.get("notified_at") or "").partition("|")
    if not stamp:
        return True
    group = str(row.get("notified_group") or stored or "")
    return bool(group) and group != str(row.get("group") or "")




def collect(db: Database, now: dt.datetime | None = None, *,
            include_recent: bool = False,
            only_ids: Collection[str] | None = None) -> list[dict[str, Any]]:
    """Every account that needs a reminder, oldest first.

    ``include_recent`` drops the ``MIN_AGE_HOURS`` filter, so an account that
    registered ten minutes ago is included too. The filter exists so the
    automatic first nudge does not land while somebody is still typing; the
    operator asking for "everyone who has not finished" means everyone, and
    without this the console could not reach a person who signed up today --
    which was the complaint.

    ``only_ids`` is the operator picking people by hand. It is a **filter, not a
    permission**: ids that no longer need a reminder (they finished setting up
    since the panel was drawn) or that never existed simply produce no row, so
    the caller can report "这些已经不用发了" instead of mailing somebody the
    sentence "你还没配好" when they have.

    Two different clocks, deliberately: ``never``/``refused`` are gated on how
    long ago the account *registered*, while ``no_mail`` is gated on how long ago
    the mailbox *started working*. Somebody who registered last week and
    connected their mailbox five minutes ago is not withholding an explanation
    from us -- there has been no time for one.
    """
    now = now or parse_utc(utc_now())
    wanted = {str(value) for value in only_ids} if only_ids is not None else None
    out: list[dict[str, Any]] = []
    for row in db.list_users_overview():
        if wanted is not None and str(row.get("id")) not in wanted:
            continue
        if str(row.get("status") or "") not in ("active", "paused"):
            continue
        group = group_for(db, row)
        if not group:
            continue
        registered = parse_utc(row.get("created_at"))
        if registered is None:
            continue
        age_hours = (now - registered).total_seconds() / 3600
        connected = parse_utc(row.get("mailbox_updated_at"))
        mailbox_hours = ((now - connected).total_seconds() / 3600) if connected else 0.0
        if group == GAP_NO_MAIL:
            if mailbox_hours < NO_MAIL_HOURS and not (include_recent or wanted is not None):
                continue
        # 手选的人不受「别打扰刚注册的」那道门槛限制：运营者点名要他。
        elif age_hours < MIN_AGE_HOURS and not (include_recent or wanted is not None):
            continue
        out.append({**row, "group": group, "age_hours": age_hours,
                    "mailbox_hours": mailbox_hours,
                    "notified_at": db.get_setting(REMINDER_KEY + row["id"])})
    out.sort(key=lambda item: item["created_at"])
    return out


def panel_rows(db: Database, now: dt.datetime | None = None, *,
               include_recent: bool = False,
               only_ids: Collection[str] | None = None) -> list[dict[str, Any]]:
    """What the admin console draws. Full addresses -- this is the operator."""
    rows = []
    tracking_since = db.get_setting(LAST_SEEN_SINCE_KEY, "")
    for row in collect(db, now, include_recent=include_recent, only_ids=only_ids):
        notified_at, _, group = str(row.get("notified_at") or "").partition("|")
        rows.append({
            "user_id": row["id"], "email": row.get("email"), "status": row.get("status"),
            "group": row["group"], "age_hours": round(row["age_hours"], 1),
            "mailbox_hours": round(row.get("mailbox_hours") or 0.0, 1),
            "mailbox_email": row.get("mailbox_email"),
            "notified_at": notified_at or "", "notified_group": group or "",
            "too_new": row["age_hours"] < MIN_AGE_HOURS,
            # 「这封信他还需不需要」与「他已经收过没有」是两件事：正文里那句话
            # 变了，旧印章就不该继续算数（见 needs_notice）。
            "needs_notice": needs_notice({**row, "notified_at": notified_at,
                                          "notified_group": group}),
            "body": message_for(row, db)[1],
            **seen_verdict(row, notified_at, tracking_since),
        })
    return rows


def seen_verdict(row: dict[str, Any], notified_at: str,
                 tracking_since: str = "") -> dict[str, Any]:
    """「提醒之后他回来过没有」——印章只说明我们做了什么，这个说的是发生了什么。

    没有印章就没有结论（``came_back_after_notice`` 是 ``None``）：对着一个还没被
    提醒过的人说「他没回来」是把我们自己的动作算在他头上。

    边界写清楚：活跃时间与印章**恰好同一秒**算「回来过」——两个时间戳都只精确到
    秒，而点开提醒信里的链接紧接着打开应用正是我们要认出来的那个动作。
    """
    last_seen = str(row.get("last_seen_at") or "")
    reason = ""
    if not notified_at:
        came_back: bool | None = None
    elif tracking_since and notified_at < tracking_since:
        # 那次提醒比「开始记活跃时间」还早：它之后的这段时间**没人看着**，
        # 所以既不能说「他回来了」也不能说「他没回来」——不知道就说不知道。
        came_back = None
        reason = "before_tracking"
    else:
        came_back = bool(last_seen) and last_seen >= notified_at
    return {
        "last_seen_at": last_seen,
        "came_back_after_notice": came_back,
        "ever_seen": bool(last_seen),
        "verdict_reason": reason,
    }


def preview(db: Database | None = None) -> dict[str, str]:
    """The letters exactly as they would be sent, one per group.

    The console shows this before the button is pressed. "Send mail to real
    people" is not an action anybody should take on a label alone -- and the
    operator is the one who has to live with the wording.
    """
    lines: dict[str, str] = {}
    for group in GROUPS:
        subject, body = message_for(
            {"group": group, "mailbox_email": "someone@example.com"}, db)
        lines[group] = f"{subject}\n\n{body}"
    lines["wechat"] = contact_wechat()
    return lines


def send_pending(db: Database, secrets: SecretBox, *, include_notified: bool = False,
                 include_recent: bool = False, limit: int = 0,
                 actor: str = "console",
                 only_ids: Collection[str] | None = None) -> dict[str, Any]:
    """Mail everyone who still needs it. Never raises; one failure cannot stop the rest.

    ``include_notified`` re-sends to people who already got one, which is only
    ever right when the wording changed or the first one clearly did not arrive.

    ``only_ids`` is the operator picking recipients by hand (用户原话：「我要可以
    自己选给谁发卡住的邮件提醒」）。点名**覆盖那两道门槛**——「已经提醒过」与
    「刚注册不到 6 小时」——因为手选是一个决定，不是启发式。它**不覆盖**的是
    「这个人现在还需不需要这封信」：中途已经配好的人会被列进 ``skipped``，
    而不是收到一句「你还没配好」——那既不真，也很难听。
    """
    rows = collect(db, include_recent=include_recent, only_ids=only_ids)
    if only_ids is not None:
        pending = list(rows)
    else:
        pending = [row for row in rows if include_notified or needs_notice(row)]
    cap = limit or BATCH_LIMIT
    remaining = max(0, len(pending) - cap)
    pending = pending[:cap]

    sent: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    for row in pending:
        subject, body = message_for(row, db)
        try:
            receipt = send_as_operator(db, secrets, row["email"], subject, body)
        except Exception as exc:  # noqa: BLE001 -- one bad address must not stop the rest
            logging.warning("setup reminder to %s failed: %s", row["id"], exc)
            failed.append({"user_id": row["id"], "email": row["email"],
                           "error": f"{type(exc).__name__}: {exc}"[:200]})
            continue
        if receipt.get("refused"):
            # A partial refusal comes back as a map instead of an exception, and
            # recording it as delivered is the one unrecoverable mistake here.
            logging.warning("setup reminder to %s refused: %s", row["id"], receipt["refused"])
            failed.append({"user_id": row["id"], "email": row["email"],
                           "error": f"收件人被拒绝：{receipt['refused']}"[:200]})
            continue
        db.set_setting(REMINDER_KEY + row["id"], f"{utc_now()}|{row['group']}", actor=actor)
        sent.append({"user_id": row["id"], "email": row["email"], "group": row["group"],
                     "message_id": str(receipt.get("message_id") or "")})
        logging.info("setup reminder sent to %s (%s)", row["id"], row["group"])
    result: dict[str, Any] = {"considered": len(rows), "attempted": len(pending),
                              "remaining": remaining, "sent": sent, "failed": failed}
    if only_ids is not None:
        # 点了名却没轮到的：已经配好了，或者本来就不在名单里（删号、拼错的 id）。
        # 面板必须把这件事说出来 —— 否则「我选了 5 个，只发出去 3 封」没人知道为什么。
        handled = {row["id"] for row in pending}
        requested = list(dict.fromkeys(str(value) for value in only_ids))
        result["requested"] = len(requested)
        result["skipped"] = [value for value in requested if value not in handled]
    return result


def whats_left(db: Database) -> dict[str, int]:
    """Counts for the console's summary line, computed the same way as the send."""
    rows = collect(db)
    everything = collect(db, include_recent=True)
    noted = [row for row in rows if not needs_notice(row)]
    return {"stalled": len(rows), "pending": len(rows) - len(noted),
            "notified": len(noted),
            # "所有人" 那一档：含还没满 MIN_AGE_HOURS 的新账号。
            "all": len(everything),
            "recent": len(everything) - len(rows),
            "never": len([row for row in rows if row["group"] == GAP_NEVER]),
            "refused": len([row for row in rows if row["group"] == GAP_REFUSED]),
            "provider": len([row for row in rows if row["group"] == GAP_PROVIDER]),
            "no_mail": len([row for row in rows if row["group"] == GAP_NO_MAIL])}


__all__ = ["REMINDER_KEY", "MIN_AGE_HOURS", "NO_MAIL_HOURS", "BATCH_LIMIT", "GROUPS",
           "GAP_NEVER", "GAP_REFUSED", "GAP_NO_MAIL", "GAP_PROVIDER", "app_url",
           "step_url", "LINK_SECTION",
           "contact_wechat", "never_configured_body", "refused_login_body", "message_for",
           "group_for", "needs_notice", "collect", "panel_rows", "preview", "send_pending",
           "whats_left", "TEMPLATE_KEYS", "PLACEHOLDERS", "TemplateError", "check_template",
           "default_template", "template_for", "set_template", "render_body"]