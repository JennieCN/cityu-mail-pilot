"""Getting an invite code to the person it was approved for.

Plan A is one function: the operator clicks 「发邀请码」, we mint a single-use code
and mail it from the operator's own mailbox. It has a failure surface we can never
see -- the message is sent, the server accepts it, and it lands in a junk folder
where nothing on our side changes. The applicant simply never hears from us.

This module is plan B. Two paths, both of which end in the same place:

* **the applicant asks** (`process_resend_queue`) -- the landing page's 「没收到
  邀请码？」 form used to enqueue a request, and the worker delivered it a minute
  later. **That form and its endpoint were removed on 2026-09-22** (registration
  is open, so there is nothing to re-send; see `docs/open-registration-2026-09-22.md`).
  The queue is still drained here because rows may already exist in the database:
  dropping this half would leave them unanswered forever.
* **we retry by ourselves** (`retry_failed_sends`) -- when the operator's own SMTP
  said no, nobody has to notice: the worker tries again, with a count and a
  backoff so it also knows when to stop.

Both are held to the same three conditions, and they live in one place so they
cannot drift apart: the application must be **approved by a human**, the address
must have **no account**, and we never mint for anything else.

**Every re-send mints a new code, and the old one is left alone.** Only the hash
is stored (deliberately: the database alone cannot reveal a code), so re-sending
the original message is not possible. The old code is *not* retired because we
cannot know whether it arrived: an SMTP error can be raised after the server has
already taken the message, and killing a code that is sitting in somebody's junk
folder would turn our delivery problem into their dead end. Both codes are
single-use and go to the same mailbox; whoever uses one first wins.

See `docs/invite-plan-b-2026-09-17.md`.
"""

from __future__ import annotations

import datetime as dt
import logging
import secrets
from typing import Any, Optional

from . import alerting
from .database import Database, parse_utc

#: How many times one application's invite e-mail may be re-attempted
#: automatically. Three total means: the first click, a retry while the network
#: hiccup is fresh, and one more for the case where the provider was rate-limiting
#: us. Past that the address is refusing us, and a fourth try is noise.
MAX_AUTO_ATTEMPTS = 3

#: Backoff before the n-th automatic retry, in seconds: 10 minutes, then an hour.
#: Deliberately coarse -- an invite code is not urgent to the second, and a slower
#: second try has a better chance of not hitting the same transient fault.
RETRY_BACKOFF_SECONDS = (600, 3600)

#: How long an invite code stays valid. Same 14 days the operator's path uses.
INVITE_DAYS = 14


def send_invite(service: Any, row: dict[str, Any], code: str) -> tuple[bool, str, str]:
    """Mail one applicant their invite code. Never raises.

    Returns ``(sent, error, message_id)``. This is the only place an invite code
    is put on the wire, so the operator's approval and both plan-B paths cannot
    end up with different wording, different error handling, or a different idea
    of what "sent" means.

    The message itself is `web.invite_letter` -- imported lazily because web.py
    imports this module, and a cycle at import time is a real bug rather than a
    style question.
    """
    from .web import invite_letter  # noqa: PLC0415 - see the docstring

    subject, body = invite_letter(code)
    try:
        receipt = alerting.send_as_operator(
            service.db, service.secrets, row["email"], subject, body)
        logging.info("invite emailed to %s from %s id=%s",
                     row["email"], receipt.get("from", ""), receipt.get("message_id", ""))
        if receipt.get("refused"):
            # send_message only raises when *every* recipient is refused; a
            # partial refusal comes back as a map. Treating that as success would
            # record a delivery that did not happen.
            return False, f"收件人被拒绝：{receipt['refused']}", receipt.get("message_id", "")
        return True, "", receipt.get("message_id", "")
    except Exception as exc:  # noqa: BLE001 - the code is still returned to the caller
        logging.warning("could not email the invite to %s", row["email"], exc_info=True)
        return False, str(exc)[:200], ""


def issue_and_send(db: Database, service: Any, request_id: str, *,
                   send: bool = True, reason: str = "") -> dict[str, Any]:
    """Mint a fresh code for an approved application and (optionally) mail it.

    The single definition of "give this applicant a code". `reason` is only used
    for the log line -- it says who asked (the operator, the applicant, or the
    retry pass), which is the first question anyone reading the journal will have.

    Returns ``{row, code, emailed, email_error}``. The code comes back **even when
    the send failed**, because the operator's path hands it over as a last resort:
    losing a freshly minted single-use code to an SMTP hiccup would be worse than
    losing the e-mail.
    """
    row = db.get_signup_request(request_id)
    # One label per issuance, so the record of what was sent to whom stays one row
    # per attempt instead of several invites sharing one name and a join that
    # cannot tell them apart.
    label = f"signup-{str(row['email'])[:40]}-{secrets.token_hex(3)}"
    code = db.create_invite(label, days=INVITE_DAYS)
    row = db.decide_signup_request(request_id, "invited", invite_label=label)
    emailed, error, message_id = (False, "", "")
    if send:
        emailed, error, message_id = send_invite(service, row, code)
    db.record_invite_email(request_id, sent=emailed, error=error, message_id=message_id)
    logging.info("invite issued for %s (%s) emailed=%s", row["email"], reason or "operator", emailed)
    return {"row": row, "code": code, "emailed": emailed, "email_error": error}


def process_resend_queue(db: Database, service: Any, limit: int = 10) -> dict[str, Any]:
    """Deliver the 「我没收到」 requests the landing page queued.

    Eligibility is re-checked here rather than trusted from the request: between
    the click and this pass the operator may have declined the application, or the
    applicant may have registered with a code that arrived after all. Both mean
    "do not send", and both are cheap to ask again.
    """
    result: dict[str, Any] = {"sent": 0, "skipped": 0, "failed": 0, "errors": []}
    for queued in db.open_invite_resends(limit):
        try:
            row = db.invite_eligible_for_resend(queued["email"])
            if row is None:
                # Nothing was sent, and that is the normal outcome for most
                # addresses in the queue (the endpoint enqueues only for approved
                # ones, but the world moves between the click and this pass).
                db.finish_invite_resend(queued["id"], "not-eligible")
                result["skipped"] += 1
                continue
            outcome = issue_and_send(db, service, row["id"], reason="applicant")
            db.finish_invite_resend(
                queued["id"], "sent" if outcome["emailed"] else f"failed: {outcome['email_error']}")
            result["sent" if outcome["emailed"] else "failed"] += 1
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the rest
            logging.exception("could not handle a queued invite resend")
            db.finish_invite_resend(queued["id"], f"error: {exc}"[:200])
            result["errors"].append(str(exc)[:200])
    return result


def retry_failed_sends(db: Database, service: Any, limit: int = 5,
                       now: Optional[dt.datetime] = None) -> dict[str, Any]:
    """Re-attempt invites whose e-mail failed, without anybody asking.

    This is the half of plan B that needs no user action at all -- and it is the
    only failure we can actually detect, because the applicant's silence and a
    junked message look identical from here.

    Stops on its own: `MAX_AUTO_ATTEMPTS` attempts and the backoff in
    `RETRY_BACKOFF_SECONDS`. An address that has refused us three times over three
    hours is not a transient fault, and the sentinel (`invite_failed:<id>`) is the
    right channel for it -- a fourth silent retry is not.
    """
    result: dict[str, Any] = {"retried": 0, "sent": 0, "errors": []}
    moment = now or dt.datetime.now(dt.timezone.utc)
    for row in db.failed_invite_sends(200):
        if result["retried"] >= limit:
            break
        attempts = int(row.get("invite_attempts") or 0)
        if attempts >= MAX_AUTO_ATTEMPTS:
            continue
        # Somebody already registered with a code that did arrive after all.
        if db.find_user_for_login(row["email"]):
            continue
        last = parse_utc(row.get("invite_last_attempt_at") or row.get("decided_at"))
        wait = RETRY_BACKOFF_SECONDS[min(max(0, attempts - 1), len(RETRY_BACKOFF_SECONDS) - 1)]
        if last and (moment - last).total_seconds() < wait:
            continue
        try:
            outcome = issue_and_send(db, service, row["id"], reason="auto-retry")
            result["retried"] += 1
            if outcome["emailed"]:
                result["sent"] += 1
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the rest
            logging.exception("could not retry an invite delivery")
            result["errors"].append(str(exc)[:200])
    return result


def delivery_pass(db: Database, service: Any) -> dict[str, Any]:
    """One worker pass of both halves. Never raises."""
    summary: dict[str, Any] = {"queued_sent": 0, "queued_skipped": 0,
                               "retried": 0, "retry_sent": 0, "errors": []}
    try:
        queue = process_resend_queue(db, service)
        summary["queued_sent"] = queue["sent"]
        summary["queued_skipped"] = queue["skipped"]
        summary["errors"] += queue["errors"]
    except Exception as exc:  # noqa: BLE001
        logging.exception("invite resend queue pass failed")
        summary["errors"].append(str(exc)[:200])
    try:
        retry = retry_failed_sends(db, service)
        summary["retried"] = retry["retried"]
        summary["retry_sent"] = retry["sent"]
        summary["errors"] += retry["errors"]
    except Exception as exc:  # noqa: BLE001
        logging.exception("invite retry pass failed")
        summary["errors"].append(str(exc)[:200])
    return summary
