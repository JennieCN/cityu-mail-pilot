"""Independent bounded Web Push worker; never migrates the shared database."""
from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

from . import push
from .database import Database
from .push_transport import PinnedSession
from .security import SecretBox


def response_outcome(code,retry_after="",now=None):
    delay=30
    if retry_after:
        try:
            delay=int(retry_after)
        except (ValueError,TypeError):
            try:
                parsed=email.utils.parsedate_to_datetime(retry_after)
                if parsed.tzinfo is None:
                    parsed=parsed.replace(tzinfo=dt.timezone.utc)
                delay=int((parsed-push.calendar_reminders.parse_utc(push.stamp(now))).total_seconds())
            except (ValueError,TypeError,OverflowError):
                delay=30
    if code in (200,201,202):
        return "provider_accepted",code,0
    if code in (404,410):
        return "invalid",code,0
    if code in (401,403):
        return "auth_failed",code,0
    if code==429 or 500<=code<=599:
        return "retry",code,max(1,min(86400,delay))
    return "permanent_failed",code,0


class WebPushProvider:
    def __init__(self,private_key,subject,*,session=None):
        # Import only inside the independent sender, never during core startup.
        from pywebpush import webpush,WebPushException
        from py_vapid import Vapid
        self.webpush=webpush
        self.exception=WebPushException
        self.vapid=Vapid.from_file(private_key)
        self.subject=subject
        self.session=session or PinnedSession()

    def __call__(self,message):
        try:
            response=self.webpush(message["subscription"],data=message["payload"],
                vapid_private_key=self.vapid,vapid_claims={"sub":self.subject},
                content_encoding="aes128gcm",ttl=message["ttl"],timeout=10,
                verbose=False,requests_session=self.session)
        except self.exception as exc:
            response=exc.response
            if response is None:
                return "unknown",None,30
        except ValueError:
            return "permanent_failed",None,0
        except Exception:
            # Provider exceptions can contain endpoints, credentials or response
            # text. Never log/interpolate the exception at this boundary.
            return "unknown",None,30
        return response_outcome(response.status_code,response.headers.get("Retry-After",""))


def run_once(db,box,provider,*,now=None,max_deliveries=20,slots=4):
    now_override=now is not None
    now=push.stamp(now)
    push.cleanup(db,now)
    push.expand_ready(db,now)
    def execute_slot(count):
        handled=0
        for _ in range(count):
            # Claim in a running slot, so no waiting task consumes its lease.
            claim=push.claim_delivery(db,now if now_override else None)
            if not claim:
                break
            try:
                message=push.start_attempt(db,box,claim,now if now_override else None)
            except (ValueError,TypeError):
                # No network has been attempted; revoke unreadable subscription.
                push.revoke_device(db,claim["user_id"],claim["device_id"],claim["binding_generation"],now)
                continue
            if message is None:
                continue
            try:
                outcome,code,delay=provider(message)
            except Exception:
                outcome,code,delay="unknown",None,30
            push.finish_attempt(db,claim,outcome,code,delay,now if now_override else None)
            handled+=1
        return handled
    slots=max(1,min(int(slots),4))
    max_deliveries=max(1,min(int(max_deliveries),20))
    counts=[max_deliveries//slots+int(i<max_deliveries%slots) for i in range(slots)]
    with ThreadPoolExecutor(max_workers=slots) as pool:
        return sum(pool.map(execute_slot,counts))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once",action="store_true")
    args=parser.parse_args(argv)
    if os.environ.get("INFE_PILOT_PUSH_ENABLED")!="1":
        return 0
    path=os.environ.get("INFE_PILOT_DB","")
    if not path or not os.path.isfile(path):
        raise SystemExit("Existing calendar database required")
    db=Database(path)
    if db.get_setting("calendar_schema_version")!="3":
        raise SystemExit("Calendar schema 3 required; migrate through the application upgrade")
    try:
        provider=WebPushProvider(os.environ["INFE_PILOT_VAPID_PRIVATE_KEY_FILE"],
                                 os.environ["INFE_PILOT_VAPID_SUBJECT"])
        import base64
        from cryptography.hazmat.primitives import serialization
        public_key=base64.urlsafe_b64encode(provider.vapid.public_key.public_bytes(
            serialization.Encoding.X962,serialization.PublicFormat.UncompressedPoint)).decode().rstrip("=")
        if public_key!=os.environ.get("INFE_PILOT_VAPID_PUBLIC_KEY","").rstrip("="):
            raise ValueError("VAPID key configuration mismatch")
        box=SecretBox.from_environment()
    except Exception:
        raise SystemExit("Push configuration unavailable") from None
    while True:
        try:
            if db.get_setting("notification_channel_paused")!="1":
                run_once(db,box,provider)
        except Exception:
            logging.warning("calendar_push_cycle_failed")
        if args.once:
            return 0
        time.sleep(5)


if __name__=="__main__":
    main()
