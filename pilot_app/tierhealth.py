"""主服务（本机那台）现在到底在不在干活 —— 一次调用就能更新的那个事实。

## 为什么需要这个模块

平台有两档：本机那台（不花钱）与付费兜底。**任何一档失败都会有日志**，但日志要人去翻
journal；而这件事的失败形状特别容易被忽略：

* 本机那把 key 被轮换 / 我们填错 / 对方重启后不认旧 key ⇒ 每一封报告都**静静地**走付费那档；
* 症状是「用户毫无感觉、报告照出、钱在花」——**这正是当初要做主服务想避免的那件事**；
* 只看月账单也不够：金额没过警戒线时，账单那一项一个字都不说。

所以这里只做一件事：把「上一次主服务答话是什么时候、上一次它为什么没答话」存进
`app_settings`，让哨兵能读、让运维面板能读。**它不碰网络、不在请求路径上加超时**——
写入点在 `Service._generate_with_retry` 里，那一次调用本来就已经发生过。

## 三条边界

* **只有主服务（本机那档）才盖章。** 付费那档的成败属于「钱」那一套（`budget.py`），
  两件事混在一起会让「主服务在不在」这个问题读不出来。
* **成功就清掉**：下一封信由主服务答话，那条「已降级」立刻消失。不清的话，一次抖动会
  在面板上挂一整天，人就学会忽略它了。
* **详情里不放会自己变的数字**（`alerting._should_send` 是「详情变了就重发」）：
  这里只存**时刻与一句原因**，不存耗时、不存计数。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import Any, Optional

from . import providers

#: `app_settings` 里那一行。形状与 `providercheck.STAMP_KEY` 一致：读数 + 时间戳。
STATUS_KEY = "local_model_status"

#: 多久没听见主服务答话就算「它现在不在干活」。比轮询周期长得多，也比人的耐心短：
#: 它只是决定面板上那条提示要不要挂着，不影响调用路径上的任何判断。
STALE_AFTER = dt.timedelta(hours=6)


def _history(db: Any) -> dict:
    """Keep timestamps only, without retaining old exception bodies."""
    try:
        old = json.loads(db.get_setting(STATUS_KEY, ""))
    except (ValueError, TypeError):
        old = {}
    if not isinstance(old, dict):
        old = {}
    return {"last_success_at": old.get("last_success_at") or (old.get("at") if old.get("state") == "ok" else None),
            "last_degraded_at": old.get("last_degraded_at") or (old.get("at") if old.get("state") == "degraded" else None)}


def note_success(db: Any, *, when: dt.datetime | None = None) -> None:
    """主服务答话了 —— 把「已降级」清掉，并记下这一次的时刻。

    容错：记账失败绝不能影响一封已经生成好的报告（`_record_usage` 同款理由）。
    """
    moment = when or dt.datetime.now(dt.timezone.utc)
    try:
        history = _history(db)
        history["last_success_at"] = moment.isoformat(timespec="seconds")
        db.set_setting(STATUS_KEY, json.dumps(
            {**history, "state": "ok", "at": moment.isoformat(timespec="seconds")},
            ensure_ascii=False), actor="local-model")
    except Exception:  # noqa: BLE001 - 见 docstring
        logging.warning("本机主服务的健康标记没写进去（不影响这封报告）", exc_info=True)


def note_degraded(db: Any, reason: str, *, when: dt.datetime | None = None) -> None:
    """主服务没能干活、**已经由兜底接手** —— 记下来，好让哨兵说得出话。

    ``reason`` 只放一句稳定的短句（不含金额、耗时、条数）：它会被当成详情渲染，
    而详情一变就会重新发一封信。
    """
    moment = when or dt.datetime.now(dt.timezone.utc)
    try:
        history = _history(db)
        history["last_degraded_at"] = moment.isoformat(timespec="seconds")
        db.set_setting(STATUS_KEY, json.dumps(
            {**history, "state": "degraded", "at": moment.isoformat(timespec="seconds"),
             "reason": str(reason)[:200]},
            ensure_ascii=False), actor="local-model")
    except Exception:  # noqa: BLE001 - 同上
        logging.warning("本机主服务的降级标记没写进去（不影响这封报告）", exc_info=True)


def reading(db: Any, *, now: dt.datetime | None = None) -> Optional[dict[str, Any]]:
    """上次那条标记 + 它的年龄。没有记录时返回 ``None``（「不知道」）。"""
    raw = db.get_setting(STATUS_KEY, "")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logging.warning("%s 那一行读不出来，当作没记过", STATUS_KEY)
        return None
    if not isinstance(data, dict):
        return None
    moment = (now or dt.datetime.now(dt.timezone.utc))
    at = data.get("at")
    age = None
    try:
        age = moment - dt.datetime.fromisoformat(str(at))
    except (TypeError, ValueError):
        age = None
    return {"state": str(data.get("state") or ""), "at": at, "age": age,
            "reason": str(data.get("reason") or "")}


def in_use() -> bool:
    """这台实例**有主服务这一档**吗（没配就是自建实例，一条都不该报）。"""
    return providers.platform_model_default() is not None


def local_is_primary() -> bool:
    """第一档是不是**本机那台**（而不是别的付费供应商）。

    与 `in_use()` 是两件事：付费兜底配着、而主档也是付费供应商的实例（老形状）里，
    「本机那台通不通」这个问题根本不存在，探测与告警都不该出现。
    """
    return providers.local_model_is_primary()


def probe(*, timeout: int = 5) -> Optional[bool]:
    """本机那台**现在**还有没有我们的服务在听 —— 一次轻量 `/health`，不调模型、不花钱。

    为什么需要它（2026-09-23 补）：`findings()` 读的那枚章只在**真的出过一封报告**时才更新，
    所以那台凌晨断掉、下一封信等到中午，中间几小时里没有任何东西会说一句话——报告全在
    走付费兜底，而运营者以为主服务在干活。这一条把「最早什么时候知道」从「下一封信」
    压到「下一轮巡检」。

    返回三种值，**它们不是一回事**：``True`` 探到了、``False`` 探不到、
    ``None`` 这台实例根本没有「本机那台作为主档」这回事（自建实例/付费主档）。

    **整个函数体都在 try 里**（2026-09-23 自查时补的）：调用它的 `alerting.run_checks`
    承诺「Never raises」，而它在文档里写的是「自己吞掉所有异常」——但第一版只包住了
    那次 HTTP，前面读配置的两行露在外面。配置读不出来（环境变量形状怪、预设表被改坏）
    时异常会一路冒到哨兵外面去，一个"看一眼那台在不在"的探测**有能力把整轮巡检带走**，
    这正是这条检查要防的那类事故。读不出来时返回 ``None``（当作「没有这一档」→ 不报），
    并把 traceback 留在日志里。
    """
    try:
        connection = providers.platform_model_default()
        if not connection or str(connection.get("provider") or "") != providers.LOCAL_MODEL_PROVIDER:
            return None
        try:
            providers.local_model_health(str(connection.get("base_url") or ""), timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - 探测失败就是 False，绝不让它带走整轮巡检
            logging.warning("本机服务的连通性探测没通过：%s: %s", type(exc).__name__, exc)
            return False
        return True
    except Exception:  # noqa: BLE001 - 连「有没有这一档」都读不出来：当没有，绝不抛
        logging.exception("本机服务的探测连配置都读不出来，这一轮跳过（不影响其他检查）")
        return None


def findings(db: Any, *, now: dt.datetime | None = None) -> list[dict[str, str]]:
    """哨兵用的纯读取版本（不联网）：主服务现在是不是靠兜底在顶着。

    只在**有人会被这件事影响**时报：这台实例配了主服务这一档（`in_use`），
    而且上一次的标记是「已降级」且不算太旧。开发机、预览库、没配主服务的实例一条都不报。
    """
    try:
        if not in_use():
            return []
        current = reading(db, now=now)
    except Exception:  # noqa: BLE001 - 这一项的失败不许带走整轮巡检（见 budget.findings）
        logging.exception("本机主服务的健康标记读不出来，跳过这一项")
        return []
    if not current or current["state"] != "degraded":
        return []
    if current["age"] is not None and current["age"] > STALE_AFTER:
        # 太久以前的降级：可能早就好了而没有人再出过报告（也就没人盖章）。宁可不报，
        # 也不要在人已经修好之后还挂着一条——挂着的那条会教人忽略这一类。
        return []
    seen = f"（{current['at']} 起）" if current["at"] else ""
    reason = current["reason"] or "没有记录原因"
    return [{
        "key": "local_model_degraded", "severity": "warning",
        "title": "主服务（本机那台）没在干活，报告正在走付费兜底",
        "detail": (f"最近一次调用主服务没有成功{seen}，已由付费兜底接手——用户那边没有感觉，"
                   f"**但每一封报告都在花管理员那把 key 的钱**，而主服务存在的意义正是不花这笔钱。"
                   f"记录到的原因：{reason}。"
                   "先跑一条命令看两档各自的现状："
                   "sudo systemd-run --uid=cityumail "
                   "--property=EnvironmentFile=/etc/cityu-mail-pilot/pilot.env "
                   "--working-directory=/opt/cityu-mail-pilot --pipe --wait --collect "
                   "/opt/cityu-mail-pilot/.venv/bin/python -m pilot_app.manage check-localmodel"
                   "（主服务那档若报 401，多半是对方轮换了 key——交付文档 §8 说会提前通知；"
                   "修法是重新装一次：set_platform_key.sh --provider local_openai）"),
    }]
