"""Admin-only observations and bounded synthetic diagnostics, never OS control.

No raw upstream body, URL, credential, mail or model output leaves this module.
GET probes only the guard listener. POST uses a fixed synthetic report through
the primary adapter, never Service retries/fallback, and never stamps real use.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import secrets
import threading
import time
from typing import Any

from . import prompts, providers, tierhealth
from .database import new_id

KEY = "model_console_diagnostic"
COOLDOWN = 60
PROBE_TTL = 30
_probe_lock = threading.Lock()
_probe_cache: tuple[Any, float, dict] | None = None


class ConsoleError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _stamp(value: Any) -> str | None:
    try:
        parsed = dt.datetime.fromisoformat(str(value))
        if parsed.tzinfo is not None:
            return parsed.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    except (ValueError, TypeError):
        pass
    return None


def _load(raw: str) -> dict:
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _number(value: Any) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else 0
    except (ValueError, TypeError):
        return 0


def error_code(exc: Any) -> str:
    # Only a closed catalog is persisted/returned; NEVER str(exc) or body text.
    text = str(exc).lower()
    if "tls" in text or "certificate" in text or "指纹" in text or "证书" in text:
        return "tls"
    if "401" in text or "403" in text:
        return "auth"
    if isinstance(exc, providers.ProviderTimeout) or "timeout" in text or "超时" in text:
        return "timeout"
    if "500" in text or "peg-native" in text:
        return "upstream"
    return "unavailable"


def _primary() -> dict | None:
    # Preserve the existing cross-provider shared-key fence. Raw default config
    # may be a local tier deliberately removed by the validated catalog.
    connections = providers.platform_model_connections()
    connection = connections[0] if connections else None
    return connection if connection and connection.get("provider") == providers.LOCAL_MODEL_PROVIDER and providers.platform_tier(connection) != "fallback" else None


def listener(connection: dict | None) -> dict:
    """A 30-second cache coalesces tabs/operators; no credential/model request."""
    global _probe_cache
    if connection is None:
        return {"state": "not_configured", "at": None}
    identity = (connection.get("base_url"), providers.local_model_fingerprint(),
                providers.local_model_cert_path())
    with _probe_lock:
        now = time.monotonic()
        if _probe_cache and _probe_cache[0] == identity and now - _probe_cache[1] < PROBE_TTL:
            return dict(_probe_cache[2])
        result = {"state": "unknown", "at": _now().isoformat(timespec="seconds")}
        try:
            health = providers.local_model_health(str(connection.get("base_url") or ""), timeout=5)
            # Installed proxy returns upstream *address*, not upstream health.
            result["state"] = "reachable" if isinstance(health, dict) and health.get("proxy") == "ok" else "unexpected"
        except Exception as exc:
            result["state"] = "failed"
            result["error"] = error_code(exc)
        _probe_cache = (identity, time.monotonic(), result)
        return dict(result)


def diagnostic_reading(database: Any) -> dict:
    raw = _load(database.get_setting(KEY, ""))
    state = raw.get("state")
    if state not in {"running", "passed", "failed"}:
        return {"state": "not_run"}
    now = time.time()
    if state == "running" and _number(raw.get("lease_until")) <= now:
        state = "interrupted"
    result = {"state": state, "started_at": _stamp(raw.get("started_at")),
              "finished_at": _stamp(raw.get("finished_at")),
              "cooldown_seconds": max(0, math.ceil(_number(raw.get("next_after")) - now)),
              "elapsed_s": max(0, round(_number(raw.get("elapsed_s")), 3)),
              "error": raw.get("error") if raw.get("error") in {"tls", "auth", "timeout", "upstream", "unavailable", "incomplete", "guard", "start_failed"} else None}
    for name in ("generation_ok", "complete", "guard_present", "guard_ok", "guard_retried"):
        result[name] = raw.get(name) if type(raw.get(name)) is bool else None
    return result


def snapshot(database: Any) -> dict:
    connection = _primary()
    raw = _load(database.get_setting(tierhealth.STATUS_KEY, ""))
    current = raw.get("state") if raw.get("state") in {"ok", "degraded"} else "unknown"
    at = _stamp(raw.get("at"))
    age = (_now() - dt.datetime.fromisoformat(at)).total_seconds() if at else None
    production = {"state": current, "at": at, "stale": age is None or age < 0 or age > tierhealth.STALE_AFTER.total_seconds(),
                  "last_success_at": _stamp(raw.get("last_success_at") or (at if current == "ok" else None)),
                  "last_degraded_at": _stamp(raw.get("last_degraded_at") or (at if current == "degraded" else None))}
    since = (_now() - dt.timedelta(hours=24)).isoformat(timespec="seconds")
    with database.connect() as db:
        rows = db.execute("SELECT provider, count(*) AS calls, max(created_at) AS last_at "
                          "FROM token_usage WHERE on_platform=1 AND created_at>=? GROUP BY provider", (since,)).fetchall()
    usage = {"local_calls": 0, "other_platform_calls": 0, "last_local_at": None, "last_other_at": None}
    for row in rows:
        local = row["provider"] == providers.LOCAL_MODEL_PROVIDER
        prefix = "local" if local else "other"
        usage["local_calls" if local else "other_platform_calls"] += row["calls"]
        key = "last_" + prefix + "_at"
        stamp = _stamp(row["last_at"])
        if stamp and (not usage[key] or stamp > usage[key]):
            usage[key] = stamp
    return {"collected_at": _now().isoformat(timespec="seconds"),
            "configured": connection is not None,
            "model": str(connection.get("model") or "")[:120] if connection else "",
            "configured_slots": providers.local_model_slots() if connection else None,
            "listener": listener(connection), "production": production,
            "usage_24h": usage, "diagnostic": diagnostic_reading(database),
            "host_resources": None, "restart_available": False}


def _save(db: Any, value: dict, actor: str) -> None:
    stamp = _now().isoformat(timespec="seconds")
    db.execute("INSERT INTO app_settings(key,value,updated_at,updated_by) VALUES(?,?,?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at,updated_by=excluded.updated_by",
               (KEY, json.dumps(value), stamp, actor[:320]))


def _audit(db: Any, action: str, actor: dict, client: str, detail: str) -> None:
    db.execute("INSERT INTO audit_log(id,created_at,actor_user_id,actor_email,action,detail,client) VALUES(?,?,?,?,?,?,?)",
               (new_id("aud"), _now().isoformat(timespec="seconds"), actor["id"], actor["email"], action, detail, client[:60]))


def _finish(database: Any, record: dict, actor: dict, client: str) -> None:
    with database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT value FROM app_settings WHERE key=?", (KEY,)).fetchone()
        if not row or _load(row["value"]).get("token") != record["token"]:
            return  # A replaced expired lease must never clobber the new job.
        _save(db, record, actor["id"])
        _audit(db, "model_diagnostic_finished", actor, client, record["state"] + ":" + record.get("error", ""))


def _run(database: Any, connection: dict, record: dict, actor: dict, client: str) -> None:
    start = time.monotonic()
    try:
        prompt = prompts.immediate_prompt({"major": "Engineering", "year_of_study": "2"}, {
            "subject": "Synthetic diagnostic library notice", "sender_name": "Synthetic Library",
            "sender_address": "synthetic@example.invalid",
            "body": "Synthetic notice: a quiet library study area is available. No registration, payment, deadline or task. Do not invent dates or links. 中文 English 日本語 한국어 😀."}, [], "unavailable")
        answer = providers.generate(provider=providers.LOCAL_MODEL_PROVIDER,
            model=connection["model"], api_key=providers.platform_connection_key(connection),
            base_url=connection.get("base_url", ""), prompt=prompt,
            max_output_tokens=2000, guard_task="summarize")
        guard = answer.guard if isinstance(answer.guard, dict) else {}
        record.update(generation_ok=bool(answer.text.strip()), complete=answer.finish == "stop",
                      guard_present=isinstance(answer.guard, dict), guard_ok=guard.get("ok") is True,
                      guard_retried=guard.get("retried") is True)
        if not record["generation_ok"] or not record["complete"]:
            record.update(state="failed", error="incomplete")
        elif not record["guard_present"] or not record["guard_ok"]:
            record.update(state="failed", error="guard")
        else:
            record["state"] = "passed"
    except Exception as exc:
        record.update(state="failed", error=error_code(exc))
    record.update(finished_at=_now().isoformat(timespec="seconds"),
                  elapsed_s=round(time.monotonic() - start, 3), next_after=time.time() + COOLDOWN)
    _finish(database, record, actor, client)


def start_diagnostic(database: Any, actor: dict, client: str) -> dict:
    connection = _primary()
    if connection is None:
        raise ConsoleError(409, "未配置本机主服务，不能执行此诊断。")
    now = time.time()
    record = {"state": "running", "token": secrets.token_hex(16),
              "started_at": _now().isoformat(timespec="seconds"),
              "lease_until": now + max(600, 3 * providers.request_timeout(providers.LOCAL_MODEL_PROVIDER) + 60),
              "next_after": now + COOLDOWN}
    with database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT value FROM app_settings WHERE key=?", (KEY,)).fetchone()
        old = _load(row["value"]) if row else {}
        if old.get("state") == "running" and _number(old.get("lease_until")) > now:
            raise ConsoleError(429, "全站已有诊断运行中，请等待结果。")
        if _number(old.get("next_after")) > now:
            raise ConsoleError(429, "诊断冷却中，完成后至少等 60 秒再试。")
        _save(db, record, actor["id"])
        _audit(db, "model_diagnostic_started", actor, client, "fixed synthetic; primary only; no fallback")
    try:
        threading.Thread(target=_run, args=(database, connection, dict(record), dict(actor), client), daemon=True).start()
    except Exception:
        record.update(state="failed", error="start_failed", finished_at=_now().isoformat(timespec="seconds"))
        _finish(database, record, actor, client)
        raise ConsoleError(503, "诊断未能启动，请稍后重试。") from None
    return {"state": "running", "started_at": record["started_at"]}
