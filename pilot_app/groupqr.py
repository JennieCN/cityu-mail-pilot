"""客服群二维码的**状态**：还在有效期吗、还有几天。

为什么单独一个模块：这件事有**两个读者**——介绍页要据此决定「出图 / 只留一句话」，
巡检哨兵要据此决定「要不要提醒运营者换码」。两边各写一遍日期判断，迟早会漂：
页面按一种算法认为过期了、哨兵按另一种算法认为还有效，于是**页面已经不出图、而没人被提醒**。

图片与到期日都来自环境（`pilot_app/web.py` 的 `render_wechat_section` 与这里读的是同一对变量）：

* ``INFE_PILOT_WECHAT_GROUP_IMG`` —— 图片路径（例如 ``/wechat-group.png``）
* ``INFE_PILOT_WECHAT_GROUP_UNTIL`` —— 到期日（``YYYY-MM-DD``，微信的群码只有 7 天）

**日期读不出来时按「过期」处理，不按「永久」**：猜错的方向只能是让访客去留言，
不能是让他扫一张可能已经作废的码。
"""

from __future__ import annotations

import datetime as dt
import os
from typing import Any

#: 与 `web.py` 同一对变量名（那里用它渲染，这里用它判断状态）。
IMG_ENV = "INFE_PILOT_WECHAT_GROUP_IMG"
UNTIL_ENV = "INFE_PILOT_WECHAT_GROUP_UNTIL"
#: 到期前几天开始提醒运营者。两天：够他重新生成一张码并上传，又不至于提前一周就吵。
SOON_DAYS = 2
#: 判断「今天」用的时区：运营者在香港，到期日也是照微信上那个日期写的。
HONG_KONG = dt.timezone(dt.timedelta(hours=8))


def state(now: dt.datetime | None = None, *, db=None, document=None) -> dict[str, Any]:
    """这一节当前的状态。``configured`` 为假时其余字段没有意义。"""
    image = (os.environ.get(IMG_ENV) or "").strip()
    raw = (os.environ.get(UNTIL_ENV) or "").strip()
    source = "environment"
    width, height = 280, 430
    expires_on = ""
    if document is None and db is not None:
        from . import website_content
        try:
            document = website_content.load(db)
        except website_content.ContentError:
            document = {"invalid": True}
    if document and document.get("invalid"):
        return {"configured": True, "image": "", "until": None, "until_text": "",
                "days_left": None, "expired": True, "soon": False, "expires_on": "",
                "source": "invalid", "width": width, "height": height}
    qr = document.get("qr") if document else None
    if qr:
        source = "database"
        image = "/website-qr/" + qr["id"]
        expires_on = qr.get("expires_on", "")
        width, height = qr["width"], qr["height"]
        try:
            raw = (dt.date.fromisoformat(expires_on) - dt.timedelta(days=1)).isoformat()
        except ValueError:
            raw = ""
    until: dt.date | None
    try:
        until = dt.date.fromisoformat(raw) if raw else None
    except ValueError:
        until = None
    if not expires_on and until is not None:
        expires_on = (until + dt.timedelta(days=1)).isoformat()
    today = (now or dt.datetime.now(dt.timezone.utc)).astimezone(HONG_KONG).date()
    days_left = None if until is None else (until - today).days
    return {
        "configured": bool(image),
        "image": image,
        "until": until,
        "until_text": until.isoformat() if until else "",
        "days_left": days_left,
        # 过期 = 日期过了**或**根本没写对（见模块开头那条：读不出来按过期处理）。
        "expired": bool(image) and (until is None or days_left < 0),
        "soon": bool(image) and until is not None and 0 <= days_left <= SOON_DAYS,
        "expires_on": expires_on, "source": source, "width": width, "height": height,
    }


def findings(now: dt.datetime | None = None, *, db=None) -> list[dict[str, str]]:
    """哨兵要的那一条（纯读取，不联网、不查库）。

    没配图片时**什么都不报**：没挂群二维码的实例（自建、或者运营者暂时不想挂）不该收到
    「二维码快过期」——那是一条与它无关的提醒。

    详情的形状是**常数**：到期日与阈值都不随今天变化，所以同一件事一天最多提醒一次，
    不会因为「还有 1 天」变成「还有 0 天」而重新发一封。
    """
    current = state(now=now, db=db)
    if current["source"] == "invalid":
        return [{"key": "website_content_invalid", "severity": "critical",
                 "title": "网站内容配置损坏", "detail": "客服群二维码已安全收起，未回退旧图；请管理员检查网站内容配置。其它巡检继续执行。"}]
    if not current["configured"]:
        return []
    how = ("在管理后台「网站内容」上传新群码并设置失效日期；旧环境配置可"
           "把新图放成服务器上的 `pilot_app/static/wechat-group.png`，"
           "再把 `INFE_PILOT_WECHAT_GROUP_UNTIL` 改成新的到期日（微信的群码只有 7 天）")
    if current["expired"]:
        return [{
            "key": "wechat_group_qr",
            "severity": "warning",
            "title": "客服群二维码已经过期",
            "detail": (f"介绍页上那张群二维码的到期日是 {current['until_text'] or '（没写或读不出来）'}，"
                       f"现在只显示一句「码过期了，去留言」——访客扫不到群，而**页面本身不会告诉你**。"
                       f"换一张新码：{how}。"),
        }]
    if current["soon"]:
        return [{
            "key": "wechat_group_qr",
            "severity": "warning",
            "title": "客服群二维码快过期了",
            "detail": (f"介绍页上那张群二维码 {current['until_text']} 到期（还剩不到 "
                       f"{SOON_DAYS + 1} 天）。到期那天起页面只会显示一句「码过期了，去留言」——"
                       f"先把新码备好：{how}。"),
        }]
    return []
