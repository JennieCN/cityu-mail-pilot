"""IMAP IDLE: notice new mail the moment it lands — **opt-in, and off here**.

Status of this module (read this before enabling it)
----------------------------------------------------
The mechanism works, and it was verified end to end rather than assumed:

* production is Python 3.14.4, where ``imaplib.IMAP4.idle()`` exists; the macOS
  dev box is 3.9, where it does not;
* both providers advertise ``IDLE`` in their pre-login ``CAPABILITY``;
* a plain IDLE connection to ``imap.qq.com`` receives ``EXISTS`` **~2 seconds**
  after a message is delivered (measured 2026-09-14).

**But it does not survive contact with our own poller.** Measured on
``imap.qq.com``, same mailbox, same script, only the surrounding traffic
differing:

===========================  ==================  ==================
IDLE window                  other connections   push received?
===========================  ==================  ==================
9 s, between poller ticks    poller active       **yes**, in 2 s
200 s, spanning 3 ticks      poller active       **no**
190 s, re-IDLE every 45 s    poller active       **no**
===========================  ==================  ==================

QQ appears to deliver unsolicited ``EXISTS`` only to the connection that most
recently selected the mailbox. Our 60-second poller re-selects on a fresh
connection every minute, so a long-lived IDLE connection is permanently deaf
after the first tick — and plain re-entry into IDLE (DONE, then IDLE again, which
is what this module does) does **not** win the channel back, because that is not
a ``SELECT``.

That leaves no good design here. Making IDLE the only connection to a mailbox
would mean giving up the poller, and with it the guarantee that mail is noticed
within one poll interval: a silent push failure would then cost
``INFE_PILOT_IDLE_SECONDS`` (300 s) instead of 60 s. Trading a proven bound for
an unproven one, to gain speed in the common case, is the wrong way round for a
product whose worst failure is "the user hears about a deadline too late".

So the switch defaults to **off**, and the code stays because it is tested,
harmless and cheap to re-evaluate. Enable it deliberately — ``INFE_PILOT_IDLE=1``
— and only after re-running the measurements above against the actual providers,
ideally with the concurrent poller stopped.

Why it is only a *trigger* when it is on
----------------------------------------
This module deliberately does **not** take over fetching. A notification wakes
it, and it then calls the ordinary ``PilotService.poll_mailbox`` — the same
cursor-based path the poller has always used, with its UIDVALIDITY handling,
same-mail de-duplication and sender filter. Reimplementing any of that here
would put the "never skip a mail" guarantee behind new, less-tested code.

Failure handling
----------------
A watcher that cannot connect backs off and retries; it never raises out of its
thread, because a dead watcher must not be able to take the worker down.
Connection errors are logged as warnings and anything else as an error with a
traceback, so a bug in this fail-soft module cannot hide as a flaky network.
"""

from __future__ import annotations

import imaplib
import logging
import os
import ssl
import threading
import time
from typing import Any, Callable, Optional

from . import mailio


# Everything a flaky provider can throw at us. Kept apart from the generic
# handler below so that "the network hiccuped" and "this module has a bug" are
# distinguishable in the journal — and in the tests.
_CONNECTION_ERRORS = (OSError, imaplib.IMAP4.error, ssl.SSLError, mailio.MailError)


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(os.environ.get(name, str(default)))))
    except (TypeError, ValueError):
        return default


# Off by default. On QQ Mail a concurrent SELECT on the same mailbox (which our
# own poller performs every minute) permanently stops pushes reaching this
# connection, so in this architecture IDLE buys nothing and costs an extra
# long-lived connection. See the module docstring for the measurements; enable
# only after re-verifying against the provider actually in use.
IDLE_ENABLED = os.environ.get("INFE_PILOT_IDLE", "0") == "1"
# One IDLE stretch. RFC 2177 suggests staying under 29 minutes so a server's
# inactivity timer does not drop us; it is also the worst-case gap if a
# notification is lost, and the cadence at which a dead connection is noticed.
IDLE_SECONDS = _int_env("INFE_PILOT_IDLE_SECONDS", 300, 30, 1740)
# Back-off after a failed connection attempt. Deliberately not zero: a provider
# that is refusing us must not be hammered in a tight reconnect loop.
RECONNECT_SECONDS = _int_env("INFE_PILOT_IDLE_RECONNECT_SECONDS", 30, 1, 3600)
# How often the supervisor re-reads the mailbox list, so adding, disabling or
# deleting an account starts or stops its watcher without a restart.
RECONCILE_SECONDS = _int_env("INFE_PILOT_IDLE_RECONCILE_SECONDS", 60, 5, 3600)


def idle_supported() -> bool:
    """Whether this interpreter can do IDLE at all (Python 3.14+)."""
    return hasattr(imaplib.IMAP4, "idle")


def enabled() -> bool:
    """Both the operator switch and the interpreter capability."""
    return IDLE_ENABLED and idle_supported()


def _connect(mailbox: dict[str, Any], password: str, *, timeout: int = 30) -> Any:
    """Open a read-only IMAP connection, or raise.

    Read-only is not optional here (invariant 2): ``select(readonly=True)`` means
    the server will not even let this connection set a flag, so the watcher
    cannot mark mail as seen no matter what it does next.
    """
    client = imaplib.IMAP4_SSL(mailbox["imap_host"], int(mailbox["imap_port"]), timeout=timeout,
                       ssl_context=mailio.imap_ssl_context())
    try:
        # 先报上名号：163/126 不认没发过 ID 的客户端（见 mailio.identify_client）。
        identified = mailio.identify_client(client)
        client.login(mailbox["email"], password)
        if not identified:
            mailio.identify_client(client)
        status, data = client.select("INBOX", readonly=True)
        if status != "OK":
            raise mailio.refused_inbox(data)
    except Exception:
        _close(client)
        raise
    return client


def _close(client: Any) -> None:
    """Log out, tolerating an already-dead socket.

    Only ``logout``: ``close`` is the IMAP CLOSE command, which expunges deleted
    messages on a writable mailbox. This one is read-only so nothing could be
    expunged, but the read-only promise is worth keeping unconditional — and QQ
    Mail does not advertise UNSELECT, so there is no middle ground to use.
    """
    if client is None:
        return
    try:
        client.logout()
    except Exception:
        pass


def _advertises_idle(client: Any) -> bool:
    capabilities = getattr(client, "capabilities", ()) or ()
    return any(str(item).upper() == "IDLE" for item in capabilities)


def watch_mailbox(service: Any, user_id: str, stop: threading.Event, *,
                  connect: Optional[Callable[..., Any]] = None) -> None:
    """Keep one mailbox live until ``stop`` is set. Never raises.

    Keyed by **user id**, not mailbox id: ``Database.get_mailbox`` looks a
    mailbox up by its owner, and every user has exactly one. The full row —
    including the mailbox id that messages reference — is re-read before every
    poll, so the watcher always works from current values.

    Runs in its own thread. Any failure is logged and retried after
    ``RECONNECT_SECONDS``; the caller's plain poller covers the gap.
    """
    connect = connect or _connect
    while not stop.is_set():
        client = None
        try:
            mailbox = service.db.get_mailbox(user_id)
            if not mailbox or not mailbox.get("enabled"):
                return
            client = connect(mailbox, service.mailbox_password(mailbox))
            if not _advertises_idle(client):
                # Not an error: this provider just cannot push. Say so once and
                # leave the mailbox to the ordinary poller.
                logging.info("mailbox %s: server does not advertise IDLE, polling only",
                             mailbox["id"])
                return
            logging.info("idle watcher connected for mailbox %s (%s)", mailbox["id"], mailbox["email"])
            _wait_loop(service, user_id, client, stop)
        except _CONNECTION_ERRORS as exc:
            # Expected: providers drop connections. Quiet, and retried.
            logging.warning("idle watcher for %s disconnected: %s", user_id, exc)
        except Exception as exc:
            # Unexpected, i.e. a bug in this module. ERROR with a traceback, not
            # a quiet warning: this method is deliberately fail-soft, so a bug
            # would otherwise look exactly like a flaky provider and hide
            # forever. A missing `import time` cost a silent 30-second stall on
            # every call before this was loud enough to notice.
            logging.exception("idle watcher for %s hit an unexpected error (%s)", user_id, exc)
        finally:
            _close(client)
        if not stop.is_set():
            stop.wait(RECONNECT_SECONDS)


def _wait_loop(service: Any, user_id: str, client: Any, stop: threading.Event) -> None:
    """Poll, then idle for the next arrival, until stopped or the link breaks."""
    while not stop.is_set():
        # Re-read the row every time: the snapshot from when this watcher
        # started goes stale as soon as any poll advances the UID cursor, and a
        # stale cursor makes the next fetch re-download everything since then.
        mailbox = service.db.get_mailbox(user_id)
        if not mailbox or not mailbox.get("enabled"):
            return
        try:
            stored = service.poll_mailbox(mailbox)
            if stored:
                logging.info("idle: %s new message(s) for mailbox %s", stored, mailbox["id"])
        except Exception:
            # The fetch path opens its own connection, so a failure here says
            # nothing about the IDLE link. Keep waiting; the poller and the
            # sentinel are already reporting this independently.
            logging.exception("idle-triggered poll failed for mailbox %s", mailbox["id"])
        if stop.is_set():
            return
        notified = False
        started = time.monotonic()
        with client.idle(duration=IDLE_SECONDS) as idler:
            for response_type, data in idler:
                if response_type in ("EXISTS", "RECENT"):
                    notified = True
                    # Logged unconditionally, and that is the point: without a
                    # line here there is no way — in the journal or in an
                    # acceptance test — to tell a working push path from one
                    # that silently fell back to the 60-second poll. Whether the
                    # notification leads to a new message is a separate fact,
                    # reported by the poll below.
                    logging.info("idle notification for mailbox %s (%s %s)",
                                 mailbox["id"], response_type, data)
                    break
        # A server that accepts IDLE and then returns immediately would turn
        # this loop into a tight reconnect-and-poll cycle against the provider.
        # One second of floor is invisible to a real notification and enough to
        # stop a misbehaving server from being hammered.
        if not notified and time.monotonic() - started < 1.0:
            stop.wait(1.0)


def start_supervisor(service: Any, stop: threading.Event) -> Optional[threading.Thread]:
    """Start (and keep reconciled) one watcher thread per active mailbox.

    Returns the supervisor thread, or ``None`` when IDLE is switched off or the
    interpreter cannot do it — in which case the caller keeps its plain poller
    and nothing else changes.
    """
    if not enabled():
        logging.info("idle disabled (INFE_PILOT_IDLE=%s, imaplib support=%s); polling only",
                     "1" if IDLE_ENABLED else "0", idle_supported())
        return None

    def supervise() -> None:
        watchers: dict[str, tuple[threading.Thread, threading.Event]] = {}
        try:
            while not stop.is_set():
                try:
                    mailboxes = {row["user_id"]: row for row in service.db.active_mailboxes()}
                except Exception:
                    logging.exception("idle supervisor could not list mailboxes")
                    mailboxes = {}
                for user_id in mailboxes:
                    if user_id in watchers and watchers[user_id][0].is_alive():
                        continue
                    if user_id in watchers:
                        # A watcher that exited on its own (for example the
                        # mailbox was disabled then re-enabled) gets a fresh one.
                        watchers.pop(user_id)[1].set()
                    event = threading.Event()
                    thread = threading.Thread(target=watch_mailbox, args=(service, user_id, event),
                                              name=f"idle-{user_id[:12]}", daemon=True)
                    thread.start()
                    watchers[user_id] = (thread, event)
                for user_id in list(watchers):
                    if user_id not in mailboxes:
                        watchers.pop(user_id)[1].set()
                stop.wait(RECONCILE_SECONDS)
        finally:
            # Shutting the supervisor down must shut its watchers down too.
            # Without this they outlive the worker's stop signal and keep polling
            # a database that is on its way out.
            for _thread, event in watchers.values():
                event.set()

    thread = threading.Thread(target=supervise, name="idle-supervisor", daemon=True)
    thread.start()
    return thread
