"""Web Push domain. Core imports require only the existing application runtime."""
from __future__ import annotations

import base64
import datetime as dt
import ipaddress
import json
import re
import secrets
import socket
import uuid
from urllib.parse import urlsplit

from . import calendar_reminders
from .security import token_hash


class NotificationConflict(ValueError):
    pass


class NotificationRateLimit(ValueError):
    pass


def stamp(now=None):
    return calendar_reminders.iso_utc(calendar_reminders.parse_utc(
        now or dt.datetime.now(dt.timezone.utc).isoformat()))


def after(now, seconds):
    return calendar_reminders.iso_utc(calendar_reminders.parse_utc(now) + dt.timedelta(seconds=seconds))


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def validate_endpoint(value, resolver=socket.getaddrinfo):
    """Return a public address set; callers must connect to these exact IPs."""
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 33 for c in value):
        raise ValueError("推送地址无效。")
    try:
        url = urlsplit(value)
        host = url.hostname or ""
        allowed = host in {"fcm.googleapis.com", "updates.push.services.mozilla.com"} or (
            host.endswith(".push.apple.com") and host != "push.apple.com")
        if url.scheme != "https" or url.port not in (None, 443) or not allowed or url.username or url.password or url.fragment or not url.path.startswith("/"):
            raise ValueError()
        addresses = sorted({item[4][0] for item in resolver(host, 443, type=socket.SOCK_STREAM)})
        parsed_addresses=[ipaddress.ip_address(a) for a in addresses]
        if not parsed_addresses or any(not a.is_global or a.is_multicast or a.is_reserved for a in parsed_addresses):
            raise ValueError()
    except (ValueError, OSError):
        raise ValueError("推送服务地址未通过安全检查。") from None
    return host, addresses


def validate_subscription(value, resolver=socket.getaddrinfo):
    if not isinstance(value, dict) or set(value) - {"endpoint", "keys", "expirationTime"}:
        raise ValueError("推送订阅无效。")
    validate_endpoint(value.get("endpoint"), resolver)
    keys = value.get("keys")
    if not isinstance(keys, dict) or set(keys) != {"p256dh", "auth"}:
        raise ValueError("推送订阅密钥无效。")
    decoded = {}
    for key, size in (("p256dh", 65), ("auth", 16)):
        raw = keys.get(key)
        if not isinstance(raw, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}={0,2}", raw):
            raise ValueError("推送订阅密钥无效。")
        try:
            data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        except ValueError:
            raise ValueError("推送订阅密钥无效。") from None
        if len(data) != size:
            raise ValueError("推送订阅密钥长度无效。")
        decoded[key] = data
    from cryptography.hazmat.primitives.asymmetric import ec
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), decoded["p256dh"])
    except ValueError:
        raise ValueError("推送订阅公钥无效。") from None
    return {"endpoint": value["endpoint"], "keys": {
        key: base64.urlsafe_b64encode(data).decode().rstrip("=") for key, data in decoded.items()}}


def migrate(connection):
    connection.execute("BEGIN IMMEDIATE")
    row = connection.execute("SELECT value FROM app_settings WHERE key='calendar_schema_version'").fetchone()
    if not row or int(row[0]) < 2:
        raise RuntimeError("日历提醒迁移尚未完成。")
    if int(row[0]) > 3:
        return
    cols = {x["name"] for x in connection.execute("PRAGMA table_info(calendar_reminders)")}
    if "dispatch_generation" not in cols:
        connection.execute("ALTER TABLE calendar_reminders ADD COLUMN dispatch_generation INTEGER NOT NULL DEFAULT 1")
    statements = [
        """CREATE TABLE IF NOT EXISTS notification_installations(
            id TEXT PRIMARY KEY, secret_digest TEXT NOT NULL, binding_generation INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS notification_devices(
            id TEXT PRIMARY KEY, installation_id TEXT NOT NULL UNIQUE REFERENCES notification_installations(id),
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, session_digest TEXT NOT NULL,
            binding_generation INTEGER NOT NULL, channel TEXT NOT NULL DEFAULT 'webpush' CHECK(channel='webpush'),
            encrypted_subscription BLOB, endpoint_hmac TEXT UNIQUE, state TEXT NOT NULL CHECK(state IN ('active','revoked','invalid')),
            permission_last_reported TEXT NOT NULL, label TEXT NOT NULL DEFAULT '',
            last_seen TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS notification_binding_challenges(
            id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            session_digest TEXT NOT NULL,installation_id TEXT NOT NULL REFERENCES notification_installations(id),
            nonce_digest TEXT NOT NULL,expected_generation INTEGER NOT NULL,expires_at TEXT NOT NULL,
            consumed_at TEXT,content_digest TEXT,device_id TEXT,result_generation INTEGER)""",
        """CREATE TABLE IF NOT EXISTS reminder_deliveries(
            id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            reminder_id TEXT NOT NULL REFERENCES calendar_reminders(id) ON DELETE CASCADE,
            dispatch_generation INTEGER NOT NULL,source_schedule_revision INTEGER NOT NULL,
            device_id TEXT NOT NULL REFERENCES notification_devices(id) ON DELETE CASCADE,
            binding_generation INTEGER NOT NULL,channel TEXT NOT NULL DEFAULT 'webpush' CHECK(channel='webpush'),
            status TEXT NOT NULL CHECK(status IN ('pending','claimed','retry','unknown','provider_accepted','cancelled','expired','permanent_failed')),
            cancel_reason TEXT NOT NULL DEFAULT '',attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3),
            unknown_retries INTEGER NOT NULL DEFAULT 0 CHECK(unknown_retries BETWEEN 0 AND 1),
            next_attempt_at TEXT NOT NULL,expires_at TEXT NOT NULL,claim_token TEXT,lease_until TEXT,call_started_at TEXT,
            provider_code INTEGER,last_outcome TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,terminal_at TEXT,
            UNIQUE(reminder_id,dispatch_generation,device_id,binding_generation,channel))""",
        """CREATE TABLE IF NOT EXISTS reminder_delivery_attempts(
            delivery_id TEXT NOT NULL REFERENCES reminder_deliveries(id) ON DELETE CASCADE,
            attempt INTEGER NOT NULL,claim_token TEXT NOT NULL UNIQUE,started_at TEXT NOT NULL,
            outcome TEXT NOT NULL DEFAULT 'started',provider_code INTEGER,finished_at TEXT,PRIMARY KEY(delivery_id,attempt))""",
        "CREATE INDEX IF NOT EXISTS idx_reminder_delivery_due ON reminder_deliveries(status,next_attempt_at,expires_at)",
        "CREATE INDEX IF NOT EXISTS idx_notification_device_session ON notification_devices(session_digest,state)",
        "CREATE INDEX IF NOT EXISTS idx_notification_challenge_owner ON notification_binding_challenges(user_id,expires_at)",
    ]
    for sql in statements:
        connection.execute(sql)
    connection.execute("""INSERT INTO app_settings(key,value,updated_at) VALUES('calendar_schema_version','3',?)
        ON CONFLICT(key) DO UPDATE SET value='3',updated_at=excluded.updated_at""", (stamp(),))


def _session(connection, user, digest, now):
    if not connection.execute("""SELECT 1 FROM sessions s JOIN users u ON u.id=s.user_id
        WHERE s.token_hash=? AND s.user_id=? AND s.expires_at>? AND u.status='active'""",
        (digest, user, now)).fetchone():
        raise PermissionError("请重新登录后连接通知。")


def _installation(connection, installation_id, secret):
    if not isinstance(installation_id,str) or not isinstance(secret,str) or not 40<=len(secret)<=100:
        raise PermissionError("设备身份无效。")
    row = connection.execute("SELECT * FROM notification_installations WHERE id=?", (installation_id,)).fetchone()
    if not row or not secrets.compare_digest(row["secret_digest"], token_hash(secret)):
        raise PermissionError("设备身份无效，请重新连接通知。")
    return row


def create_challenge(db, user, digest, installation_id, secret, expected_generation, now=None):
    now = stamp(now)
    if not isinstance(installation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", installation_id):
        raise ValueError("设备身份无效。")
    if not isinstance(secret, str) or not re.fullmatch(r"[A-Za-z0-9_-]{40,100}", secret):
        raise ValueError("设备凭据无效。")
    if type(expected_generation) is not int or expected_generation < 0:
        raise ValueError("设备版本无效。")
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        _session(c, user, digest, now)
        c.execute("DELETE FROM notification_binding_challenges WHERE expires_at<=?", (now,))
        if c.execute("SELECT COUNT(*) FROM notification_binding_challenges WHERE user_id=? AND consumed_at IS NULL", (user,)).fetchone()[0] >= 5:
            raise NotificationRateLimit("请稍后再试。")
        c.execute("""INSERT INTO notification_installations(id,secret_digest,created_at,updated_at)
            VALUES(?,?,?,?) ON CONFLICT(id) DO NOTHING""", (installation_id,token_hash(secret),now,now))
        row = _installation(c, installation_id, secret)
        if row["binding_generation"] != expected_generation:
            exc=NotificationConflict("设备已在另一处变更，请重新载入。")
            exc.current_generation=row["binding_generation"]
            raise exc
        nonce = secrets.token_urlsafe(32)
        challenge_id = uuid.uuid4().hex
        c.execute("""INSERT INTO notification_binding_challenges(id,user_id,session_digest,installation_id,nonce_digest,expected_generation,expires_at)
            VALUES(?,?,?,?,?,?,?)""", (challenge_id,user,digest,installation_id,token_hash(nonce),expected_generation,after(now,300)))
    return {"challenge_id": challenge_id, "nonce": nonce, "expires_at": after(now,300)}


def _public_device(row):
    return {key: row[key] for key in ("id","user_id","binding_generation","channel","state",
        "permission_last_reported","label","last_seen","created_at")}


def bind_device(db, box, user, digest, *, challenge_id, nonce, installation_id, secret,
                expected_generation, subscription, label="", now=None, resolver=socket.getaddrinfo):
    now = stamp(now)
    if not isinstance(challenge_id,str) or not re.fullmatch(r"[a-f0-9]{32}",challenge_id) or not isinstance(nonce,str) or not 32<=len(nonce)<=100 or type(expected_generation) is not int or expected_generation<0:
        raise ValueError("设备绑定信息无效。")
    subscription = validate_subscription(subscription, resolver)
    content_digest = box.anonymized(canonical(subscription))
    if not isinstance(label,str) or len(label) > 80:
        raise ValueError("设备名称最多80字。")
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        _session(c,user,digest,now)
        inst = _installation(c,installation_id,secret)
        ch = c.execute("SELECT * FROM notification_binding_challenges WHERE id=?",(challenge_id,)).fetchone()
        if not ch or ch["user_id"] != user or ch["session_digest"] != digest or ch["installation_id"] != installation_id or not secrets.compare_digest(ch["nonce_digest"],token_hash(nonce)):
            raise PermissionError("设备绑定请求无效。")
        if ch["expires_at"] <= now or ch["expected_generation"] != expected_generation:
            raise NotificationConflict("设备绑定请求已过期或版本已变更。")
        if ch["consumed_at"]:
            row = c.execute("SELECT * FROM notification_devices WHERE id=? AND user_id=?",(ch["device_id"],user)).fetchone()
            if ch["content_digest"] != content_digest or not row or row["state"] != "active" or row["binding_generation"] != ch["result_generation"] or inst["binding_generation"] != ch["result_generation"]:
                raise NotificationConflict("设备绑定已变更，请重新连接。")
            return _public_device(row)
        if inst["binding_generation"] != expected_generation:
            raise NotificationConflict("设备已在另一处变更。")
        endpoint_hmac = box.anonymized(subscription["endpoint"])
        duplicate = c.execute("SELECT installation_id FROM notification_devices WHERE endpoint_hmac=?",(endpoint_hmac,)).fetchone()
        if duplicate and duplicate[0] != installation_id:
            raise NotificationConflict("此订阅已连接其他安装，请先撤销原连接。")
        old = c.execute("SELECT * FROM notification_devices WHERE installation_id=?",(installation_id,)).fetchone()
        device_id = old["id"] if old else uuid.uuid4().hex
        generation = expected_generation + 1
        if old:
            _cancel_device(c,device_id,now,"rebound")
        encrypted = box.encrypt(canonical(subscription),context=f"notification-device:{user}:{device_id}:{generation}")
        c.execute("""INSERT INTO notification_devices(id,installation_id,user_id,session_digest,binding_generation,encrypted_subscription,endpoint_hmac,state,permission_last_reported,label,last_seen,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,'active','granted',?,?,?,?) ON CONFLICT(installation_id) DO UPDATE SET
            user_id=excluded.user_id,session_digest=excluded.session_digest,binding_generation=excluded.binding_generation,
            encrypted_subscription=excluded.encrypted_subscription,endpoint_hmac=excluded.endpoint_hmac,state='active',
            permission_last_reported='granted',label=excluded.label,last_seen=excluded.last_seen,updated_at=excluded.updated_at""",
            (device_id,installation_id,user,digest,generation,encrypted,endpoint_hmac,label.strip(),now,now,now))
        c.execute("UPDATE notification_installations SET binding_generation=?,updated_at=? WHERE id=?",(generation,now,installation_id))
        c.execute("""UPDATE notification_binding_challenges SET consumed_at=?,content_digest=?,device_id=?,result_generation=? WHERE id=?""",
            (now,content_digest,device_id,generation,challenge_id))
        return _public_device(c.execute("SELECT * FROM notification_devices WHERE id=?",(device_id,)).fetchone())


def list_devices(db,user):
    with db.connect() as c:
        return [_public_device(row) for row in c.execute("SELECT * FROM notification_devices WHERE user_id=? ORDER BY created_at,id",(user,))]


def _cancel_device(c,device_id,now,reason):
    c.execute("""UPDATE reminder_deliveries SET status='cancelled',cancel_reason=?,updated_at=?,terminal_at=?
        WHERE device_id=? AND status IN ('pending','claimed','retry','unknown')""",(reason,now,now,device_id))


def revoke_device(db,user,device_id,expected_generation,now=None):
    now=stamp(now)
    if type(expected_generation) is not int or expected_generation<0:
        raise ValueError("设备版本无效。")
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        row=c.execute("SELECT * FROM notification_devices WHERE id=? AND user_id=?",(device_id,user)).fetchone()
        if not row:
            raise KeyError(device_id)
        if row["state"] == "revoked" and row["binding_generation"] == expected_generation+1:
            return _public_device(row)
        if row["binding_generation"] != expected_generation:
            raise NotificationConflict("设备已变更，请重新载入。")
        _revoke_row(c,row,now)
        return _public_device(c.execute("SELECT * FROM notification_devices WHERE id=?",(device_id,)).fetchone())


def _revoke_row(c,row,now):
    _cancel_device(c,row["id"],now,"device_revoked")
    generation=row["binding_generation"]+1
    c.execute("""UPDATE notification_devices SET state='revoked',binding_generation=?,encrypted_subscription=NULL,endpoint_hmac=NULL,updated_at=? WHERE id=?""",(generation,now,row["id"]))
    c.execute("UPDATE notification_installations SET binding_generation=?,updated_at=? WHERE id=?",(generation,now,row["installation_id"]))


def revoke_sessions(c, *, user=None, digest=None, keep_digest=None):
    """Participate in the caller's session deletion transaction."""
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='notification_devices'").fetchone():
        return
    clauses=["state='active'"];args=[]
    if user is not None:
        clauses.append("user_id=?");args.append(user)
    if digest is not None:
        clauses.append("session_digest=?");args.append(digest)
    if keep_digest is not None:
        clauses.append("session_digest!=?");args.append(keep_digest)
    if user is None and digest is None:
        raise ValueError("撤销范围不能为空。")
    rows=c.execute("SELECT * FROM notification_devices WHERE "+" AND ".join(clauses),args).fetchall()
    now=stamp()
    for row in rows:
        _revoke_row(c,row,now)


def eligible(c,delivery,now,*,check_gate=True):
    paused=c.execute("SELECT value FROM app_settings WHERE key='notification_channel_paused'").fetchone()
    if check_gate and paused and paused[0]=="1":
        return False
    return bool(c.execute("""SELECT 1 FROM calendar_reminders r
        JOIN users u ON u.id=r.user_id JOIN notification_devices d ON d.id=? AND d.user_id=r.user_id
        JOIN notification_installations i ON i.id=d.installation_id
        JOIN sessions s ON s.token_hash=d.session_digest AND s.user_id=d.user_id
        LEFT JOIN calendar_events me ON me.id=r.manual_event_id AND me.user_id=r.user_id
        LEFT JOIN calendar_mail_sources ms ON ms.id=r.mail_source_id AND ms.user_id=r.user_id
        WHERE r.id=? AND r.user_id=? AND r.enabled=1 AND r.confirmation_required=0
        AND r.dispatch_generation=? AND r.source_schedule_revision=?
        AND (r.status='ready' OR (?=0 AND r.status='pending'))
        AND r.due_at_utc<=? AND r.expires_at>? AND u.status='active'
        AND d.state='active' AND d.permission_last_reported='granted' AND d.binding_generation=?
        AND i.binding_generation=d.binding_generation AND s.expires_at>?
        AND (?=0 OR EXISTS(SELECT 1 FROM calendar_recovery_state WHERE singleton_id=1 AND status='complete'))
        AND ((r.source_kind='manual' AND me.state='open' AND me.schedule_revision=r.source_schedule_revision)
        OR (r.source_kind='mail' AND ms.state='active' AND ms.user_state='open' AND ms.schedule_revision=r.source_schedule_revision
        AND ms.report_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM calendar_reconcile_outbox o WHERE o.report_id=ms.report_id AND o.status!='done')))""",
        (delivery["device_id"],delivery["reminder_id"],delivery["user_id"],delivery["dispatch_generation"],
         delivery["source_schedule_revision"],int(check_gate),now,now,delivery["binding_generation"],now,int(check_gate))).fetchone())


def cancel_ineligible(c, *, user=None, reason="source_changed", now=None):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='reminder_deliveries'").fetchone():
        return
    now=stamp(now)
    sql="SELECT * FROM reminder_deliveries WHERE status IN ('pending','claimed','retry','unknown')"
    rows=c.execute(sql+(" AND user_id=?" if user else ""),(user,) if user else ()).fetchall()
    for row in rows:
        if not eligible(c,row,now,check_gate=False):
            c.execute("UPDATE reminder_deliveries SET status='cancelled',cancel_reason=?,updated_at=?,terminal_at=? WHERE id=?",(reason,now,now,row["id"]))


def expand_ready(db,now=None,limit=100):
    now=stamp(now);created=0
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        rows=c.execute("""SELECT r.id AS reminder_id,r.user_id,r.dispatch_generation,r.source_schedule_revision,r.expires_at,
            d.id AS device_id,d.binding_generation FROM calendar_reminders r JOIN notification_devices d ON d.user_id=r.user_id
            WHERE r.status='ready' AND r.expires_at>? AND d.state='active' AND r.enabled=1 AND r.confirmation_required=0
            AND EXISTS(SELECT 1 FROM users u JOIN sessions s ON s.user_id=u.id WHERE u.id=r.user_id
                AND u.status='active' AND s.token_hash=d.session_digest AND s.expires_at>?)
            AND EXISTS(SELECT 1 FROM calendar_recovery_state WHERE singleton_id=1 AND status='complete')
            AND ((r.source_kind='manual' AND EXISTS(SELECT 1 FROM calendar_events e WHERE e.id=r.manual_event_id
                AND e.user_id=r.user_id AND e.state='open' AND e.schedule_revision=r.source_schedule_revision))
                OR (r.source_kind='mail' AND EXISTS(SELECT 1 FROM calendar_mail_sources m WHERE m.id=r.mail_source_id
                AND m.user_id=r.user_id AND m.state='active' AND m.user_state='open' AND m.schedule_revision=r.source_schedule_revision
                AND m.report_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM calendar_reconcile_outbox o WHERE o.report_id=m.report_id AND o.status!='done'))))
            AND NOT EXISTS(SELECT 1 FROM reminder_deliveries x WHERE x.reminder_id=r.id
                AND x.dispatch_generation=r.dispatch_generation AND x.device_id=d.id AND x.binding_generation=d.binding_generation
                AND NOT(x.status='cancelled' AND x.attempts=0 AND x.cancel_reason IN ('done','paused','source_changed')))
            ORDER BY r.due_at_utc,r.id,d.id LIMIT ?""",(now,now,max(1,min(limit,500)))).fetchall()
        for row in rows:
            if not eligible(c,row,now):
                continue
            old=c.execute("""SELECT * FROM reminder_deliveries WHERE reminder_id=? AND dispatch_generation=?
                AND device_id=? AND binding_generation=? AND channel='webpush'""",
                (row["reminder_id"],row["dispatch_generation"],row["device_id"],row["binding_generation"])).fetchone()
            if old:
                if old["status"]=="cancelled" and old["attempts"]==0 and old["cancel_reason"] in {"done","paused","source_changed"}:
                    c.execute("""UPDATE reminder_deliveries SET status='pending',cancel_reason='',source_schedule_revision=?,
                        next_attempt_at=?,expires_at=?,updated_at=?,terminal_at=NULL WHERE id=?""",(row["source_schedule_revision"],now,row["expires_at"],now,old["id"]))
                continue
            c.execute("""INSERT INTO reminder_deliveries(id,user_id,reminder_id,dispatch_generation,source_schedule_revision,device_id,binding_generation,status,next_attempt_at,expires_at,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,'pending',?,?,?,?)""",(uuid.uuid4().hex,row["user_id"],row["reminder_id"],row["dispatch_generation"],row["source_schedule_revision"],row["device_id"],row["binding_generation"],now,row["expires_at"],now,now))
            created+=1
    return created


def claim_delivery(db,now=None):
    """Call only from an available execution slot, never prefetch a queue."""
    now=stamp(now)
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        if c.execute("SELECT 1 FROM app_settings WHERE key='notification_channel_paused' AND value='1'").fetchone() or not c.execute("SELECT 1 FROM calendar_recovery_state WHERE singleton_id=1 AND status='complete'").fetchone():
            return None
        c.execute("""UPDATE reminder_deliveries SET status='expired',terminal_at=?,updated_at=?
            WHERE expires_at<=? AND status IN ('pending','retry','unknown')""",(now,now,now))
        expired=c.execute("SELECT * FROM reminder_deliveries WHERE status='claimed' AND lease_until<=?",(now,)).fetchall()
        for row in expired:
            state="unknown" if row["call_started_at"] else "pending"
            c.execute("UPDATE reminder_deliveries SET status=?,last_outcome=?,next_attempt_at=?,updated_at=? WHERE id=?",(state,state,after(now,30) if state=="unknown" else now,now,row["id"]))
            if row["call_started_at"]:
                c.execute("UPDATE reminder_delivery_attempts SET outcome='unknown',finished_at=? WHERE claim_token=? AND outcome='started'",(now,row["claim_token"]))
        rows=c.execute("""SELECT * FROM reminder_deliveries WHERE status IN ('pending','retry','unknown')
            AND next_attempt_at<=? AND expires_at>? AND attempts<3 AND (status!='unknown' OR unknown_retries<1)
            ORDER BY next_attempt_at,id LIMIT 100""",(now,now)).fetchall()
        for row in rows:
            if not eligible(c,row,now):
                if eligible(c,row,now,check_gate=False):
                    continue
                c.execute("UPDATE reminder_deliveries SET status='cancelled',cancel_reason='source_changed',updated_at=?,terminal_at=? WHERE id=?",(now,now,row["id"]))
                continue
            token=secrets.token_urlsafe(24)
            c.execute("""UPDATE reminder_deliveries SET status='claimed',claim_token=?,lease_until=?,call_started_at=NULL,
                unknown_retries=unknown_retries+?,updated_at=? WHERE id=?""",(token,after(now,30),int(row["status"]=="unknown"),now,row["id"]))
            return dict(c.execute("SELECT * FROM reminder_deliveries WHERE id=?",(row["id"],)).fetchone())
    return None


def start_attempt(db,box,claim,now=None):
    now=stamp(now)
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        row=c.execute("SELECT * FROM reminder_deliveries WHERE id=?",(claim["id"],)).fetchone()
        if not row or row["status"]!="claimed" or row["claim_token"]!=claim["claim_token"] or row["lease_until"]<=now or row["call_started_at"] or not eligible(c,row,now):
            return None
        if c.execute("SELECT 1 FROM reminder_delivery_attempts WHERE delivery_id=? AND outcome='provider_accepted'",(row["id"],)).fetchone():
            c.execute("UPDATE reminder_deliveries SET status='cancelled',last_outcome='provider_accepted',cancel_reason='already_accepted',updated_at=?,terminal_at=? WHERE id=?",(now,now,row["id"]))
            return None
        reminder=c.execute("SELECT * FROM calendar_reminders WHERE id=?",(row["reminder_id"],)).fetchone()
        if reminder["source_kind"]=="mail":
            try:
                _source(c,box,row["user_id"],"mail",reminder["mail_source_id"])
            except (ValueError,KeyError,TypeError):
                # Current encrypted report is authoritative, even if a writer
                # failed to enqueue its projection refresh. No network attempt.
                c.execute("UPDATE reminder_deliveries SET status='cancelled',cancel_reason='source_changed',updated_at=?,terminal_at=? WHERE id=?",(now,now,row["id"]))
                return None
        device=c.execute("SELECT * FROM notification_devices WHERE id=?",(row["device_id"],)).fetchone()
        subscription=json.loads(box.decrypt(device["encrypted_subscription"],context=f"notification-device:{row['user_id']}:{device['id']}:{device['binding_generation']}"))
        attempt=row["attempts"]+1
        c.execute("UPDATE reminder_deliveries SET call_started_at=?,attempts=?,updated_at=? WHERE id=?",(now,attempt,now,row["id"]))
        c.execute("INSERT INTO reminder_delivery_attempts(delivery_id,attempt,claim_token,started_at) VALUES(?,?,?,?)",(row["id"],attempt,row["claim_token"],now))
        payload={"schema_version":1,"delivery_id":row["id"],"device_id":row["device_id"],
                 "binding_generation":row["binding_generation"],"expires_at":row["expires_at"],"message":"calendar_reminder"}
        ttl=max(0,min(900,int((calendar_reminders.parse_utc(row["expires_at"])-calendar_reminders.parse_utc(now)).total_seconds())))
        return {"subscription":subscription,"payload":canonical(payload),"ttl":ttl}


def finish_attempt(db,claim,outcome,code=None,retry_seconds=30,now=None):
    if outcome not in {"provider_accepted","retry","unknown","invalid","auth_failed","permanent_failed"}:
        raise ValueError("无效的投递结果。")
    now=stamp(now)
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        attempt=c.execute("SELECT * FROM reminder_delivery_attempts WHERE claim_token=? AND delivery_id=?",(claim["claim_token"],claim["id"])).fetchone()
        if not attempt:
            return False
        # Preserve a received external fact even if its business claim was cancelled.
        if attempt["outcome"] in {"started","unknown"}:
            c.execute("UPDATE reminder_delivery_attempts SET outcome=?,provider_code=?,finished_at=? WHERE claim_token=?",(outcome,code,now,claim["claim_token"]))
        row=c.execute("SELECT * FROM reminder_deliveries WHERE id=?",(claim["id"],)).fetchone()
        if not row:
            return False
        if outcome=="provider_accepted":
            if row["claim_token"]!=claim["claim_token"]:
                # The old attempt fact is preserved above; never rewrite a newer claim.
                return False
            state="provider_accepted" if eligible(c,row,now) and row["status"]!="cancelled" else "cancelled"
            c.execute("UPDATE reminder_deliveries SET status=?,last_outcome='provider_accepted',provider_code=?,updated_at=?,terminal_at=? WHERE id=?",(state,code,now,now,row["id"]))
            return True
        if row["claim_token"]!=claim["claim_token"] or row["status"]!="claimed":
            return False
        if row["lease_until"]<=now:
            return False
        if outcome=="invalid":
            device=c.execute("SELECT * FROM notification_devices WHERE id=? AND binding_generation=?",(row["device_id"],row["binding_generation"])).fetchone()
            if device:
                _revoke_row(c,device,now)
                c.execute("UPDATE notification_devices SET state='invalid' WHERE id=?",(device["id"],))
        if outcome=="auth_failed":
            c.execute("""INSERT INTO app_settings(key,value,updated_at) VALUES('notification_channel_paused','1',?)
                ON CONFLICT(key) DO UPDATE SET value='1',updated_at=excluded.updated_at""",(now,))
        state=outcome if outcome in {"unknown","retry"} else "permanent_failed"
        next_at=after(now,max(1,min(86400,int(retry_seconds))))
        if not eligible(c,row,now,check_gate=False):
            state="cancelled"
        elif row["expires_at"]<=next_at and state in {"unknown","retry"}:
            state="expired"
        elif row["attempts"]>=3 and state=="retry":
            state="permanent_failed"
        c.execute("""UPDATE reminder_deliveries SET status=?,last_outcome=?,provider_code=?,next_attempt_at=?,
            updated_at=?,terminal_at=CASE WHEN ? IN ('cancelled','expired','permanent_failed') THEN ? ELSE NULL END WHERE id=?""",
            (state,outcome,code,next_at,now,state,now,row["id"]))
        return True


def _source(c,box,user,kind,source_id):
    if kind=="manual":
        row=c.execute("SELECT * FROM calendar_events WHERE id=? AND user_id=? AND state!='deleted'",(source_id,user)).fetchone()
        if not row:
            raise KeyError(source_id)
        return {"local_date":row["local_date"],"local_time":row["local_time"],
                "timezone":row["timezone"],"all_day":row["all_day"],"starts_at_utc":row["starts_at_utc"],
                "schedule_revision":row["schedule_revision"],"state":row["state"]}
    if kind!="mail":
        raise ValueError("无效的提醒来源。")
    row=c.execute("SELECT * FROM calendar_mail_sources WHERE id=? AND user_id=? AND state!='removed'",(source_id,user)).fetchone()
    if not row:
        raise KeyError(source_id)
    if row["state"]!="active" or not row["report_id"] or c.execute("SELECT 1 FROM calendar_reconcile_outbox WHERE report_id=? AND status!='done'",(row["report_id"],)).fetchone():
        raise NotificationConflict("邮件事项正在重新核对，请稍后再试。")
    fields=json.loads(box.decrypt(row["encrypted_schedule"],context=f"calendar-mail-source:{user}:{row['task_key']}"))
    if len(fields)!=8 or fields[0]!=row["report_id"] or fields[1]!=row["task_key"]:
        raise NotificationConflict("邮件事项来源无法核对。")
    report=c.execute("""SELECT r.id,r.message_id,r.body_markdown,r.subject,r.created_at,
        m.subject AS message_subject,m.sender_name,m.sender_address,m.received_at,m.importance
        FROM reports r LEFT JOIN messages m ON m.id=r.message_id WHERE r.id=? AND r.user_id=? AND r.kind='immediate'""",
        (row["report_id"],user)).fetchone()
    if not report:
        raise NotificationConflict("邮件事项来源已不可用。")
    body=box.decrypt(report["body_markdown"],context=f"report:{user}") if isinstance(report["body_markdown"],bytes) else str(report["body_markdown"])
    tasks=calendar_reminders.report_tasks(box,user,dict(report),body,fields[5],fallback_day=fields[3])
    task=next((task for task in tasks if task["task_key"]==row["task_key"]),None)
    if not task or task["source_fingerprint"]!=row["source_fingerprint"]:
        raise NotificationConflict("邮件时间已变更，等待后台重新核对。")
    return {"local_date":fields[3],"local_time":fields[4],"timezone":fields[5],
            "all_day":fields[6],"starts_at_utc":fields[7],"schedule_revision":row["schedule_revision"],"state":row["user_state"]}


def _public_reminder(row):
    if row is None:
        return None
    return {key:row[key] for key in ("id","source_kind","source_schedule_revision","rule_kind","rule_value",
        "due_at_utc","expires_at","enabled","status","revision","confirmation_required","dispatch_generation")}


def reminder_view(db,box,user,kind,source_id):
    with db.connect() as c:
        source=_source(c,box,user,kind,source_id)
        column="manual_event_id" if kind=="manual" else "mail_source_id"
        row=c.execute(f"SELECT * FROM calendar_reminders WHERE user_id=? AND {column}=?",(user,source_id)).fetchone()
        return {"source":source,"reminder":_public_reminder(row)}


def save_rule(db,box,user,kind,source_id,*,expected_source_revision,expected_reminder_revision,
              rule_kind,rule_value,enabled=True,confirm=False,now=None):
    now=stamp(now)
    if type(enabled) is not bool or type(rule_value) is not int or type(expected_source_revision) is not int or (expected_reminder_revision is not None and type(expected_reminder_revision) is not int):
        raise ValueError("提醒设置无效。")
    with db.connect() as c:
        c.execute("BEGIN IMMEDIATE")
        source=_source(c,box,user,kind,source_id)
        if source["schedule_revision"]!=expected_source_revision:
            raise NotificationConflict("事项时间已变更，请重新查看。")
        column="manual_event_id" if kind=="manual" else "mail_source_id"
        other="mail_source_id" if kind=="manual" else "manual_event_id"
        old=c.execute(f"SELECT * FROM calendar_reminders WHERE user_id=? AND {column}=?",(user,source_id)).fetchone()
        if (old and old["revision"]!=expected_reminder_revision) or (not old and expected_reminder_revision is not None):
            raise NotificationConflict("提醒设置已变更，请重新载入。")
        if confirm and (not old or not old["confirmation_required"] or not enabled):
            raise NotificationConflict("此提醒当前无需确认。")
        if confirm and not c.execute("SELECT 1 FROM calendar_recovery_state WHERE singleton_id=1 AND status='complete'").fetchone():
            raise NotificationConflict("日历正在恢复，请完成核对后再确认。")
        if not enabled and old:
            c.execute("UPDATE calendar_reminders SET enabled=0,status='cancelled_user',revision=revision+1,updated_at=? WHERE id=?",(now,old["id"]))
            cancel_ineligible(c,user=user,now=now)
            return _public_reminder(c.execute("SELECT * FROM calendar_reminders WHERE id=?",(old["id"],)).fetchone())
        due,expires=calendar_reminders.due_for_rule(source,rule_kind=rule_kind,rule_value=rule_value,now=now)
        required=int(bool(old and old["confirmation_required"] and not confirm))
        status="cancelled_done" if source["state"]=="done" else ("needs_confirmation" if required else "pending")
        if old and not confirm and enabled and old["enabled"] and not required and source["state"]=="open" and old["rule_kind"]==rule_kind and old["rule_value"]==rule_value and old["source_schedule_revision"]==expected_source_revision:
            status=old["status"]
        if not enabled:
            status="cancelled_user"
        if old:
            changed=confirm or old["rule_kind"]!=rule_kind or old["rule_value"]!=rule_value or not old["enabled"]
            c.execute("""UPDATE calendar_reminders SET source_schedule_revision=?,rule_kind=?,rule_value=?,due_at_utc=?,expires_at=?,
                enabled=?,status=?,confirmation_required=?,dispatch_generation=dispatch_generation+?,revision=revision+1,updated_at=? WHERE id=?""",
                (expected_source_revision,rule_kind,rule_value,due,expires,int(enabled),status,required,int(changed),now,old["id"]))
            reminder_id=old["id"]
        else:
            reminder_id=uuid.uuid4().hex
            c.execute(f"""INSERT INTO calendar_reminders(id,user_id,source_kind,{column},{other},source_schedule_revision,
                rule_kind,rule_value,due_at_utc,expires_at,enabled,status,revision,created_at,updated_at)
                VALUES(?,?,?,?,NULL,?,?,?,?,?,?,?,1,?,?)""",
                (reminder_id,user,kind,source_id,expected_source_revision,rule_kind,rule_value,due,expires,int(enabled),status,now,now))
        cancel_ineligible(c,user=user,now=now)
        return _public_reminder(c.execute("SELECT * FROM calendar_reminders WHERE id=?",(reminder_id,)).fetchone())


def delivery_target(db,user,delivery_id):
    with db.connect() as c:
        row=c.execute("""SELECT r.source_kind,r.manual_event_id,r.mail_source_id FROM reminder_deliveries d
            JOIN calendar_reminders r ON r.id=d.reminder_id AND r.user_id=d.user_id
            WHERE d.id=? AND d.user_id=?""",(delivery_id,user)).fetchone()
        if not row:
            raise KeyError(delivery_id)
        if row["source_kind"]=="manual":
            source=c.execute("SELECT id,local_date FROM calendar_events WHERE id=? AND user_id=? AND state!='deleted'",(row["manual_event_id"],user)).fetchone()
            if source:
                return {"source_kind":"manual","event_id":source["id"],"day":source["local_date"]}
        else:
            source=c.execute("SELECT id,task_key,report_id FROM calendar_mail_sources WHERE id=? AND user_id=? AND state='active'",(row["mail_source_id"],user)).fetchone()
            if source:
                return {"source_kind":"mail","source_id":source["id"],"task_key":source["task_key"]}
        raise KeyError(delivery_id)


def preferences(db,user,payload=None,now=None):
    now=stamp(now)
    with db.connect() as c:
        if payload is not None:
            enabled=payload.get("mail_default_enabled")
            offset=payload.get("timed_offset_minutes")
            minute=payload.get("all_day_minute")
            if type(enabled) is not bool or type(offset) is not int or not 0<=offset<=10080 or type(minute) is not int or not 0<=minute<=1439:
                raise ValueError("默认提醒设置无效。")
            c.execute("BEGIN IMMEDIATE")
            c.execute("""INSERT INTO calendar_preferences(user_id,mail_default_enabled,timed_offset_minutes,all_day_minute,enabled_at,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET mail_default_enabled=excluded.mail_default_enabled,
                timed_offset_minutes=excluded.timed_offset_minutes,all_day_minute=excluded.all_day_minute,
                enabled_at=CASE WHEN calendar_preferences.mail_default_enabled=0 AND excluded.mail_default_enabled=1 THEN excluded.enabled_at ELSE calendar_preferences.enabled_at END,
                revision=calendar_preferences.revision+1,updated_at=excluded.updated_at""",(user,int(enabled),offset,minute,now if enabled else None,now,now))
        row=c.execute("SELECT mail_default_enabled,timed_offset_minutes,all_day_minute,enabled_at,revision FROM calendar_preferences WHERE user_id=?",(user,)).fetchone()
        return dict(row) if row else {"mail_default_enabled":0,"timed_offset_minutes":60,"all_day_minute":540,"enabled_at":None,"revision":0}


def cleanup(db,now=None):
    """Remove expired challenge secrets and bounded terminal history."""
    now=stamp(now);cutoff=after(now,-30*86400)
    with db.connect() as c:
        c.execute("DELETE FROM notification_binding_challenges WHERE expires_at<=?",(now,))
        c.execute("DELETE FROM reminder_deliveries WHERE expires_at<? AND updated_at<? AND status IN ('provider_accepted','cancelled','expired','permanent_failed','unknown')",(cutoff,cutoff))
        c.execute("DELETE FROM notification_installations WHERE updated_at<? AND NOT EXISTS(SELECT 1 FROM notification_devices d WHERE d.installation_id=notification_installations.id) AND NOT EXISTS(SELECT 1 FROM notification_binding_challenges ch WHERE ch.installation_id=notification_installations.id)",(cutoff,))
