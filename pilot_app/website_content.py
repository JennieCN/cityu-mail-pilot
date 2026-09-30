"""Structured landing copy and QR publishing, stored atomically in app_settings.

No HTML input, new dependency, or schema migration. One bounded private draft
per administrator; publication consumes it in the same transaction as the copy.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import secrets
import re
import os
from pathlib import Path

from . import imageguard
from .database import utc_now, new_id

KEY = "website_content_v1"
DRAFT_PREFIX = "website_qr_draft:"
DEFAULTS = {
    "title_line1": "告别漏看",
    "title_line2": "即刻待办",
    "introduction": "只读你的 CityU 学校邮件，分清课程通知、行政通知、社团与校招；来信几分钟内先发一封，晚上再发当天汇总——写明要办什么、什么时候截止。",
    "customer_service": "用微信扫一下进客服群，随时问。",
    "expired_notice": "客服群的二维码到期了（微信的群码只有 7 天，我们每 7 天换一张）。",
}
LIMITS = dict(zip(DEFAULTS, (80, 80, 1000, 500, 500)))
HK = dt.timezone(dt.timedelta(hours=8))
MAX_IMAGE_BYTES = 1_500_000


class ContentError(ValueError):
    pass


class Conflict(ContentError):
    pass


def revision(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def decode(raw):
    if not raw:
        return {"fields": {}, "qr": None}
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {"fields", "qr"}:
            raise ValueError()
        validate_fields(value["fields"])
        qr = value["qr"]
        if qr is not None:
            if not isinstance(qr, dict) or set(qr) != {
                "id", "media_type", "width", "height", "data", "sha256", "created_at", "expires_on"
            }:
                raise ValueError()
            if (not isinstance(qr["id"], str) or not re.fullmatch(r"[0-9a-f]{32}", qr["id"])
                    or qr["media_type"] not in (imageguard.JPEG, imageguard.PNG)
                    or any(type(qr[k]) is not int or not 1 <= qr[k] <= 8192 for k in ("width", "height"))
                    or not isinstance(qr["data"], str) or len(qr["data"]) > 2_000_000):
                raise ValueError()
            data = base64.b64decode(qr["data"], validate=True)
            if hashlib.sha256(data).hexdigest() != qr["sha256"]:
                raise ValueError()
            dt.datetime.fromisoformat(qr["created_at"])
            if dt.date.fromisoformat(qr["expires_on"]).isoformat() != qr["expires_on"]:
                raise ValueError()
        return value
    except (ValueError, TypeError, KeyError) as exc:
        # Broken persisted config must never silently resurrect an expired env QR.
        raise ContentError("网站内容配置损坏；请管理员检查，不自动回退。") from exc


def load(db):
    return decode(db.get_setting(KEY))


def encode(document):
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"))


def fields(document):
    return {key: document.get("fields", {}).get(key, default)
            for key, default in DEFAULTS.items()}


def validate_fields(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULTS):
        raise ContentError("请提交完整的五个网站文字字段，不支持其他字段。")
    result = {}
    for key, limit in LIMITS.items():
        text = value[key]
        if not isinstance(text, str) or not text.strip() or len(text) > limit:
            raise ContentError("网站文字不能为空或超过字段长度限制。")
        if any(ord(c) < 32 and c not in "\n\r\t" for c in text):
            raise ContentError("网站文字含不支持的控制字符。")
        result[key] = text.strip()
    return result


def draft(db, actor):
    raw = db.get_setting(DRAFT_PREFIX + actor)
    if not raw:
        return None
    try:
        value = json.loads(raw)
        created = dt.datetime.fromisoformat(value["created_at"])
        if created.tzinfo is None:
            return None
    except (ValueError, TypeError, KeyError):
        return None
    if dt.datetime.now(dt.timezone.utc) - created > dt.timedelta(hours=6):
        return None
    return value


def upload(db, actor, data, declared):
    if len(data) > MAX_IMAGE_BYTES:
        raise ContentError("图片过大，请使用小于 1.5 MB 的图片。")
    media, width, height = imageguard.validate(data, declared)
    value = {"id": secrets.token_hex(16), "media_type": media,
             "width": width, "height": height,
             "data": base64.b64encode(data).decode("ascii"),
             "sha256": hashlib.sha256(data).hexdigest(), "created_at": utc_now()}
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=6)).isoformat()
    with db.connect() as connection:
        connection.execute("DELETE FROM app_settings WHERE substr(key,1,?)=? AND updated_at<?",
                           (len(DRAFT_PREFIX), DRAFT_PREFIX, cutoff))
        connection.execute(
            "INSERT INTO app_settings(key,value,updated_at,updated_by) VALUES(?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at,updated_by=excluded.updated_by",
            (DRAFT_PREFIX + actor, json.dumps(value), utc_now(), actor))
    return value


def prepare(db, actor, payload, *, current=None):
    if not isinstance(payload, dict) or set(payload) != {"fields", "qr", "revision"}:
        raise ContentError("网站内容请求字段不完整或含未知字段。")
    raw = db.get_setting(KEY) if current is None else current
    if not isinstance(payload["revision"], str) or payload["revision"] != revision(raw):
        raise Conflict("网站内容已被更新，请刷新后重新预览。")
    old = decode(raw)
    result = {"fields": validate_fields(payload["fields"]), "qr": old.get("qr")}
    qr = payload["qr"]
    if not isinstance(qr, dict) or set(qr) != {"image_id", "expires_on"}:
        raise ContentError("二维码请求字段不完整。")
    image_id, expires = qr["image_id"], qr["expires_on"]
    if not isinstance(expires, str) or image_id is not None and not isinstance(image_id, str):
        raise ContentError("二维码参数格式不正确。")
    if image_id:
        candidate = draft(db, actor)
        if not candidate or candidate["id"] != image_id:
            raise ContentError("二维码草稿不存在或已过期，请重新上传。")
        try:
            day = dt.date.fromisoformat(expires)
            if day.isoformat() != expires:
                raise ValueError()
        except ValueError as exc:
            raise ContentError("请填写有效的失效日期 YYYY-MM-DD。") from exc
        today = dt.datetime.now(dt.timezone.utc).astimezone(HK).date()
        if not today < day <= today + dt.timedelta(days=7):
            raise ContentError("新群码失效日期须在未来 7 天内（香港时间 00:00 起失效）。")
        old_qr = old.get("qr")
        if old_qr and old_qr.get("sha256") == candidate["sha256"]:
            raise ContentError("这仍是已发布的旧图，请重新生成群码后上传。")
        if not old_qr and os.environ.get("INFE_PILOT_WECHAT_GROUP_IMG", "").strip() == "/wechat-group.png":
            old_file = Path(__file__).resolve().parent / "static" / "wechat-group.png"
            if old_file.is_file() and hashlib.sha256(old_file.read_bytes()).hexdigest() == candidate["sha256"]:
                raise ContentError("这仍是环境配置中的旧图，请重新生成群码后上传。")
        result["qr"] = dict(candidate, expires_on=expires)
    else:
        from . import groupqr
        actual = groupqr.state(db=db)["expires_on"]
        if expires != actual:
            raise ContentError("不能单独修改旧码日期，请上传新群码后一起保存。")
    return result


def save(db, actor, payload, *, actor_email=""):
    # Acquire the write reservation BEFORE checking revision and draft, so two
    # requests cannot both validate the same revision and overwrite each other.
    with db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT value FROM app_settings WHERE key=?", (KEY,)).fetchone()
        raw = str(row["value"]) if row else ""
        document = prepare(db, actor, payload, current=raw)
        new_raw = encode(document)
        connection.execute(
            "INSERT INTO app_settings(key,value,updated_at,updated_by) VALUES(?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at,updated_by=excluded.updated_by",
            (KEY, new_raw, utc_now(), actor))
        if payload["qr"]["image_id"]:
            connection.execute("DELETE FROM app_settings WHERE key=?", (DRAFT_PREFIX + actor,))
        connection.execute(
            "INSERT INTO audit_log(id,created_at,actor_user_id,actor_email,action,"
            "target_user_id,target_email,detail,client) VALUES(?,?,?,?,?,?,?,?,?)",
            (new_id("aud"), utc_now(), actor[:60], actor_email[:254], "website_content_saved",
             "", "", "structured copy and QR saved", ""))
    return document
