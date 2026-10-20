"""Who gets the "someone applied for the pilot" e-mail.

The request is the only place a stranger reaches us, and today the notice goes to
whoever is named in ``INFE_PILOT_ADMIN_EMAILS`` -- the people who installed the
instance. Admins granted from the console (``users.is_admin``, v0.34.0) are
**admins for everything else** but were not told when somebody applied, which is
exactly the gap the operator reported on 2026-09-20: 「可以让邀请申请的发邮件通知
不只是通知我，还可以选择通知其他管理员」.

Two halves, and they are deliberately different:

* **The environment list is always notified.** It is the installer's own address,
  it cannot be revoked from the console, and a notice that silently stopped
  reaching the person who owns the server would be a bug, not a preference.
* **Console-granted admins are opt-in**, one by one, by the installer. Turning it
  into "mail every admin by default" would start sending applicants' addresses to
  people who never asked for them -- a change nobody consented to.

Selection is stored as a list of addresses in ``app_settings`` (the same place,
and the same pattern, as ``agent.py``/``digest_synthesis.py``). It is read at
**send** time and intersected with who is an admin *then*, so an account that
loses the flag stops receiving immediately, without anyone having to remember to
edit a list.
"""

from __future__ import annotations

from typing import Any, Iterable

from . import alerting

#: ``app_settings`` key. Comma-separated lower-cased addresses; empty = nobody.
SETTING_KEY = "signup_notify_admins"


def _clean(addresses: Iterable[str]) -> list[str]:
    seen: list[str] = []
    for item in addresses:
        address = str(item or "").strip().lower()
        if address and address not in seen:
            seen.append(address)
    return seen


def admin_addresses(database: Any) -> set[str]:
    """Everyone who can administer this instance right now: env ∪ console.

    Deliberately not ``alerting.admin_emails()`` alone -- that is the *installer*
    set. This is the set the console shows under 「管理员」, and the set that may
    be selected below.
    """
    stored = {row["email"].strip().lower() for row in database.database_admins()
              if str(row.get("status") or "active") == "active"}
    return alerting.admin_emails() | stored


def selected(database: Any) -> list[str]:
    """The addresses the operator picked, minus anyone who is no longer an admin.

    Filtering here rather than at write time is what makes revocation work: see
    the module docstring.
    """
    chosen = _clean(str(database.get_setting(SETTING_KEY, "") or "").split(","))
    allowed = admin_addresses(database)
    return [address for address in chosen if address in allowed]


def extra_recipients(database: Any) -> list[str]:
    """Exactly the addresses that should be added to the installer's own.

    The subtraction matters: an installer address that was also ticked must not
    turn into a second copy of the same e-mail.
    """
    installers = alerting.admin_emails()
    return [address for address in selected(database) if address not in installers]


def set_selected(database: Any, addresses: Iterable[str], *, actor: str = "") -> list[str]:
    """Store the selection. **Only current admins may be selected.**

    Refusing unknown addresses is the security property, not a nicety: this value
    decides who receives a message containing an applicant's address, so a
    request that could name ''anyone@example.com'' would turn an admin-only
    endpoint into a way to mail arbitrary people from the operator's mailbox.
    """
    wanted = _clean(addresses)
    allowed = admin_addresses(database)
    unknown = [address for address in wanted if address not in allowed]
    if unknown:
        raise ValueError("不是管理员，不能选：" + "、".join(unknown))
    database.set_setting(SETTING_KEY, ",".join(wanted), actor=actor)
    return wanted


def candidates(database: Any, installers: Iterable[str]) -> list[dict[str, Any]]:
    """The admins the console may offer, each with whether it could receive.

    "Can it receive" is not a guess: ``send_admin_mail`` borrows *the recipient's
    own* mailbox to reach them (there is no system mailbox), so an admin who has
    never finished setup is skipped at send time. The console says so up front
    instead of letting the operator tick a name that will never get the mail.
    """
    rows: list[dict[str, Any]] = []
    for address in sorted(admin_addresses(database)):
        user = database.find_user_for_login(address)
        mailbox = database.get_mailbox(user["id"]) if user else None
        rows.append({
            "email": address,
            "source": "env" if address in {a.lower() for a in installers} else "database",
            "can_receive": bool(mailbox and mailbox.get("enabled")),
        })
    return rows
