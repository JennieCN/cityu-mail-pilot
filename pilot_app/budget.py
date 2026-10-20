"""管理员那把 key 的钱：还剩多少、这个月花了多少、花完了怎么办。

## 这件事的真实形状（2026-09-22 实测，不是推演）

DeepSeek 的账是**预付费**的：先充值，再按量扣。所以「一觉醒来收到天价账单」这件事
**本来就不存在**——真正的上限是账上那点余额，扣完了供应商直接拒（`402 Insufficient
Balance`）。那不是我们发明的上限，也就不会因为我们的 bug 而失效。

缺的从来不是上限，是**在它见底之前知道**，以及**见底那一刻我们说的话是人话**：

* 在此之前我们从不看余额。余额归零之后的第一封报告会以供应商的报错失败，运营者要等到
  `failed_reports` 那条告警才知道——而那时用户已经在丢报告了；
* 我们自己记着每一笔调用的钱（`token_usage.on_platform`），可那个数只进「用量」面板，
  没有人会一直盯着：某个循环把花费放大一百倍时，没有任何东西会响。

所以这个模块只有三件事，边界写在下面：

1. **读余额**：`GET /user/balance`（`providers.fetch_balance`），只读、免费、不产生模型调用；
2. **算本月代付**：`token_usage` 里 `on_platform=1` 的行，按**香港时间**自然月；
3. **见底时不再花**（`require_available`）：供应商说 `is_available=false` 时，借用管理员 key
   的账号不再发起调用，而是拿到一句中文错误——**用户自己的 key 永远不受影响**。

## 四条不许含糊的边界

* **余额是账上的币种，花费是我们价目表的币种。** 这个账号用人民币充值（¥），而
  `pricing.py` 的价目表是美元（$）。两个数**不能相减**，界面上也不许并排比大小。
* **我们只看得见自己花的钱。** 同一把 key 如果在别处也在用（本地脚本、别的机器、别人的
  工具），这里一分钱都看不见。所以「本月代付」= **本 app 花的**，不是「这把 key 花的」。
* **读不到账就放行。** 读数缺失、过期、或者这家供应商根本没有这个接口时不拦：一次网络抖动
  或一次数据库打嗝，不能变成所有人的报告停摆。宁可多花几毛钱，也不要一个自制的停摆。
* **发现项里不放会变的数字。** `agent.finding_fingerprint()` 把标题也算进去，所以标题里
  写一个每次都变的金额，会让 AI 运维助手每 5 分钟为同一个发现项重新分析一次——那正是
  这个模块要防的那种花法。金额在 `manage platform-cost` 与后台「用量」面板里，
  邮件只说「越过了哪条线」（线是常数，所以稳定）。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from typing import Any, Callable, Optional

from . import pricing, providers
from .database import Database, parse_utc

#: 余额读数存在 `app_settings` 的哪一行（形状与 `providercheck` 那行一样：读数 + 时间戳）。
STAMP_KEY = "platform_balance"

#: 香港时间。账期按它切（理由见 `month_window`）。
HONG_KONG = dt.timezone(dt.timedelta(hours=8))


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(os.environ.get(name, str(default)))))
    except (TypeError, ValueError):
        return default


#: 本月（香港时间自然月）由管理员 key 付掉的钱越过它就说一声。**美元**，0 = 关掉这一条。
#: 默认 5 美元：2026-09-22 实测这个月的代付是 **$0.165**（2454 次调用、13 个接好邮箱的账号），
#: 所以这条线是当前用量的 30 倍——它响的时候一定是出了别的事（循环、有人拿它跑别的活），
#: 而不是「这个月大家多用了几封」。
PLATFORM_COST_ALERT = _float_env("INFE_PILOT_PLATFORM_COST_ALERT", 5.0)
#: 余额警戒线，**按账上那个币种解读**（这个账号是人民币）。0 = 关掉这一条。
#: 默认 20：同样是照着实测值留的余量（¥52.18 大约能跑两年）。
PLATFORM_BALANCE_FLOOR = _float_env("INFE_PILOT_PLATFORM_BALANCE_FLOOR", 20.0)
#: 多久去读一次余额。一次读是几毫秒的 HTTPS，但它要过网络，所以不该每 5 分钟一次。
BALANCE_REFRESH_AFTER = dt.timedelta(
    minutes=_int_env("INFE_PILOT_PLATFORM_BALANCE_REFRESH_MINUTES", 30, 5, 24 * 60))
#: 读数超过这么久就算**过期**：闸门放行（模块开头第三条边界），管理命令里注明。
BALANCE_MAX_AGE = dt.timedelta(
    hours=_int_env("INFE_PILOT_PLATFORM_BALANCE_MAX_AGE_HOURS", 6, 1, 72))
#: 「余额检查没在跑」的阈值，与 `providercheck.STAMP_MAX_AGE` 同一个数、同一个理由。
BALANCE_STALE_AFTER = dt.timedelta(hours=36)

#: 余额见底时给用户看的话。**写给用户看**，所以不许出现内部名字，也不许把责任推给用户。
EXHAUSTED_MESSAGE = (
    "管理员代付的模型额度已经用尽（DeepSeek 账上余额不足）。"
    "可以在「设置 → 模型」里填自己的 API key 立刻恢复，或者联系管理员充值——"
    "你的邮件不会丢：它还在你的转发邮箱里，恢复后会自动补做。")

_SYMBOLS = {"CNY": "¥", "USD": "$"}


def currency_symbol(currency: str) -> str:
    code = str(currency or "").strip().upper()
    return _SYMBOLS.get(code, f"{code} " if code else "")


def money(amount: float | None, currency: str = "USD") -> str:
    """金额加币种符号，两位小数。读不出来的金额显示「?」而不是 0。"""
    if amount is None:
        return "?（金额读不出来）"
    return f"{currency_symbol(currency)}{amount:,.2f}"


# ---------------------------------------------------------------------------
# 账期与我们自己那本账
# ---------------------------------------------------------------------------
def month_window(now: dt.datetime) -> tuple[str, str]:
    """本月的起点（UTC ISO）与账期名字，按**香港时间**切。

    为什么不是 UTC：运营者说「这个月花了多少」时指的是本地那个月；用 UTC 切会把 9 月 30 日
    晚上 8 点之后的调用算进 10 月，于是「月初刚花了几毛钱就告警」。`usage_overview` 出于
    同一个理由也是按 `+8 小时` 分桶的。
    """
    local = now.astimezone(HONG_KONG)
    first = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return first.astimezone(dt.timezone.utc).isoformat(timespec="seconds"), first.strftime("%Y-%m")


def metered_providers() -> list[str]:
    """**会花钱**的供应商有哪些（= 定价表里认识的）。

    这是「哪家要花钱」的唯一定义：`pricing.lookup` 认不出单价 ⇒ 这次调用不花钱
    （本机那台就是），于是它不该出现在「管理员代付了多少钱」这本账里。
    认不出来的算不花钱是**故意**的保守方向：一块钱都不记，比把自家的电记成账单好。
    """
    return sorted({str(provider).lower() for provider, _ in pricing.DEFAULT_PRICES})


def spend(db: Database, *, now: dt.datetime | None = None) -> dict[str, Any]:
    """本月代付：**花钱的**调用次数、金额，以及三个说明覆盖面的计数。

    计数为什么不止两个：平台 2026-09-22 起有两档（本机那台不花钱的主服务 + 付费兜底），
    两档都算「借用运营者的服务」却不是都花钱。`local_calls` 把本机那些单独报出来，
    否则面板上会写成一句「N 次调用 · $0.00」，两个数各自都对、合起来是假话。
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    since, month = month_window(now)
    return {"month": month, **db.platform_key_spend(since, metered_providers())}


# ---------------------------------------------------------------------------
# 余额读数：存下来，让哨兵不联网
# ---------------------------------------------------------------------------
def save(db: Any, reading: dict[str, Any], *, when: dt.datetime | None = None) -> None:
    stamp = (when or dt.datetime.now(dt.timezone.utc)).isoformat(timespec="seconds")
    db.set_setting(STAMP_KEY, json.dumps({"at": stamp, **reading}, ensure_ascii=False),
                   actor="platform-budget")


def load(db: Any) -> dict[str, Any] | None:
    raw = db.get_setting(STAMP_KEY, "")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logging.warning("platform_balance 那一行读不出来，当作没读过")
        return None
    return data if isinstance(data, dict) else None


def reading(db: Any, *, now: dt.datetime | None = None) -> dict[str, Any] | None:
    """上次那条读数 + 它的年龄与主要币种。没有任何记录时返回 ``None``。

    ``main`` 是**金额最大的那个币种**：DeepSeek 会同时返回充值的币种和另一个恒为 0 的币种，
    把两者相加是错的（我们没有汇率，也不该有一个）。警戒线按 ``main`` 解读。
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    data = load(db)
    if not data:
        return None
    when = parse_utc(data.get("at"))
    age = None if when is None else now - when
    balances = [item for item in (data.get("balances") or []) if isinstance(item, dict)]
    listed = [item for item in balances if item.get("total") is not None]
    main = max(listed, key=lambda item: float(item["total"])) if listed else None
    # 「账上没钱了」= 供应商自己说不可用，**或者**读到的每个币种都见了底。
    # 前者是权威（起付线与赠送额的有效期只有它知道），后者是没有那个字段时的兜底。
    exhausted = data.get("is_available") is False or (
        bool(listed) and all(float(item["total"]) <= 0 for item in listed))
    return {"at": data.get("at"), "age": age,
            "stale": age is None or age > BALANCE_MAX_AGE,
            "old": age is None or age > BALANCE_STALE_AFTER,
            "is_available": data.get("is_available"),
            "balances": balances, "main": main, "exhausted": bool(exhausted)}


def refresh(db: Any, *, now: dt.datetime | None = None,
            fetch: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
            ) -> dict[str, Any] | None:
    """真去读一次余额并存下来。``None`` = 没配平台 key，或者这家没有这个接口。

    网络失败**不算**一次读数：存下去会变成「余额是上个月那个数」的假账，而这条记录的
    全部价值就在于它是刚读到的。失败只记一行日志，让上一条读数自然变老。
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    # **读的是花管理员钱的那一档，不是「平台默认」那一档。** 2026-09-22 起平台默认
    # 是本机那台盒子（没有账户、没有余额接口），照旧读它会让这里永远返回 None——
    # 于是「余额快见底」的告警静默消失，而它守护的正是那把真会扣钱的 key。
    connection = providers.metered_model_connection()
    if connection is None or not providers.supports_balance(connection.get("provider")):
        return None
    fetch = fetch or (lambda conn: providers.fetch_balance(conn))
    try:
        result = fetch(connection)
    except Exception as exc:  # noqa: BLE001 - 一次读不成只是「这次没读到」
        logging.warning("读平台 key 余额失败（不影响出报告）：%s", exc)
        return None
    if result is None:
        return None
    if not isinstance(result, dict) or not isinstance(result.get("balances"), list):
        # 读数形状不对（比如把供应商那段**原始**应答直接交了上来）。存下去会让余额这一半
        # 永远静默：`reading()` 找不到 balances，就既不说低也不说够。工装出错不能变成产品
        # 不报，所以这里明说一句、并且什么都不存。
        logging.warning("余额读数的形状不对，没有存下来（需要 {is_available, balances[]}）")
        return None
    save(db, result, when=now)
    logging.info("平台 key 余额：可用=%s，%s", result.get("is_available"),
                 "；".join(f"{item.get('currency')} {item.get('total_text')}"
                           for item in result.get("balances") or []))
    return {"at": now.isoformat(timespec="seconds"), **result}


def refresh_if_due(db: Any, *, now: dt.datetime | None = None,
                   fetch: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None,
                   force: bool = False) -> dict[str, Any] | None:
    """超过 `BALANCE_REFRESH_AFTER` 就真读一次；否则什么都不做（返回 None）。

    由 worker 的哨兵循环顺手调用：半小时一次只读 HTTPS，任何供应商都不会介意。
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    current = reading(db, now=now)
    if not force and current is not None and current["age"] is not None \
            and current["age"] < BALANCE_REFRESH_AFTER:
        return None
    return refresh(db, now=now, fetch=fetch)


# ---------------------------------------------------------------------------
# 判定：**只有这一处**，哨兵、管理命令、model 那道闸门都读它
# ---------------------------------------------------------------------------
def balance_state(db: Any, *, now: dt.datetime | None = None) -> dict[str, Any]:
    """只看**余额**那一半的判定。

    单独一支是有原因的：`Service.model_connection()` 出报告的路上只关心「账上还有没有钱」，
    不该为了这件事去算本月的账（那是另一支，而且它要查 `token_usage`）。两个问题分开问，
    出报告那条热路径就只多一次 `app_settings` 的读。
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    # 同 `refresh()`：这一支的全部问题都是「那个**账户**里还剩多少钱」，所以它读的是
    # 付费那一档。本机那台没有账户，也就没有「余额见底」这回事。
    connection = providers.metered_model_connection()
    configured = connection is not None
    readable = bool(configured and providers.supports_balance(connection.get("provider")))
    current = reading(db, now=now) if readable else None
    # 金额读不出来时是 ``None`` 而不是 0：0 会让「低于警戒线」永远成立。
    main_total = None
    if current and (current.get("main") or {}).get("total") is not None:
        main_total = float(current["main"]["total"])
    return {
        "now": now,
        "configured": configured,
        "provider": str((connection or {}).get("provider") or ""),
        "balance_readable": readable,
        "reading": current,
        "balance_floor": PLATFORM_BALANCE_FLOOR,
        "balance_currency": str(((current or {}).get("main") or {}).get("currency") or ""),
        "balance_low": bool(readable and current and PLATFORM_BALANCE_FLOOR > 0
                            and main_total is not None
                            and main_total < PLATFORM_BALANCE_FLOOR),
        "exhausted": bool(readable and current and current["exhausted"]),
        "stale": bool(readable and (current is None or current["old"])),
    }


def state(db: Database, *, now: dt.datetime | None = None) -> dict[str, Any]:
    """完整判定：余额那一半 + 本月代付那一半。哨兵的发现项与 `manage platform-cost` 读它。"""
    now = now or dt.datetime.now(dt.timezone.utc)
    month = spend(db, now=now)
    return {
        **balance_state(db, now=now),
        "spend": month,
        "cost_alert": PLATFORM_COST_ALERT,
        "over_cost": PLATFORM_COST_ALERT > 0 and month["cost"] >= PLATFORM_COST_ALERT,
    }


def require_available(db: Any, *, now: dt.datetime | None = None) -> None:
    """借用管理员 key 之前的一道闸：账上没钱就**不再花**，抛一句人话。

    只看 `balance_state`（不查本月花费：出报告的路上不该多算一本账）。只在读数**新鲜且
    明确说不够**时拦（见模块开头第三条边界）。抛的是 `providers.ProviderError`：它不是
    「稍后重试就能好」，但报文会原样进这一封报告的错误里，用户与运营者都能看懂该做什么；
    而报告本身仍按退避重试，充值之后会自动补做。
    """
    current = balance_state(db, now=now)
    if not current["exhausted"]:
        return
    current_reading = current["reading"] or {}
    if current_reading.get("stale"):
        # 一条过期的读数不足以停掉所有人的报告：放行，并留下痕迹。
        logging.warning("余额读数是 %s 前的，过期了；这一轮不拦（见 budget 的第三条边界）",
                        current_reading.get("age"))
        return
    raise providers.ProviderError(EXHAUSTED_MESSAGE)


def riding(rows: list[dict[str, Any]] | None) -> int:
    """有多少个**活跃**账号没有自己的模型 key——他们就是「管理员代付」的全部含义。

    这笔钱只有在**有人靠它**的时候才值得报：开发机、预览库、CI 里可能也配着一把平台 key，
    那里没有账号会因为它见底而少收一封信，报出来只是噪音（这和 `providercheck.in_use`
    是同一条规矩：新装的实例不该挂一条「检查没在跑」）。
    """
    count = 0
    for row in rows or []:
        if str((row or {}).get("status") or "") != "active":
            continue          # 停用的账号本来就不出报告，它不靠这把 key
        if not str((row or {}).get("model_provider") or "").strip():
            count += 1
    return count


def findings(db: Database, *, now: dt.datetime | None = None,
             rows: list[dict[str, Any]] | None = None) -> list[dict[str, str]]:
    """哨兵用的纯读取版本（**不联网**，只读存下来的那条读数）。

    **一件与直觉相反的事（2026-09-22 改）**：这里不再以「配了平台 key 吗」当总开关。
    那天起平台默认是本机那台盒子——它没有账户、没有余额接口，而**花钱那一档**（付费兜底）
    可能配着、也可能没配。于是 `state()["configured"]` 为假并不等于「没人花管理员的钱」：
    真的花过（`token_usage.on_platform=1` 有行）就必须报，否则一次「花超了」会静默掉，
    只因为那台不花钱的盒子排在了第一位。

    没配也没花过时一条都不报：这个模块整件事都不存在（自建实例就该长这样）。
    余额那部分只在这家供应商读得到余额、且**真的有人靠它**时才报，否则我们不是没有这个
    信息，就是这件事与谁都不相干。``rows`` 由调用方传进来（`evaluate` 本身就查过一遍
    账号列表），省一次查询，也保证两处看的是同一份数据。

    **整段包在 try 里，是这个模块唯一一处故意吞异常的地方。** 理由：`evaluate()` 一旦抛，
    `run_checks` 会把整轮巡检记成 `alert evaluation failed` —— 一个新加的检查有能力让
    **所有**告警静默（备份、证书、收信全在内）。宁可这一轮少报钱这一项，也不许它把别的
    检查带走；真出问题时那行 `logging.exception` 会带完整 traceback 进 journal。
    """
    try:
        current = state(db, now=now)
        # 装错了（同一把 key 发给两家）必须报，**哪怕一分钱都还没花**：那正是它最容易被
        # 当成「一切正常」的时刻——主服务看起来配好了，其实每一次调用都在花付费那把 key。
        conflict = providers.platform_key_conflict()
        if not conflict and not current["configured"] and int(current["spend"].get("calls") or 0) <= 0:
            return []
        if rows is None:
            rows = db.list_users_overview()
    except Exception:  # noqa: BLE001 - 见上面那段：这一项的失败不许带走整轮巡检
        logging.exception("平台 key 的费用检查这一轮读不出来，跳过（不影响其他检查）")
        return []
    out: list[dict[str, str]] = []
    month = current["spend"]
    in_use = riding(rows)

    if conflict:
        # 详情里**不放任何会自己变的数字**：`_should_send` 是「详情变了就重发」，
        # 往这里放一个实时读数会把它变成节拍器（2026-09-15 那 70 封邮件就是这么来的）。
        out.append({
            "key": "platform_key_shared_across_providers", "severity": "warning",
            "title": "主服务与付费兜底用的是同一把 key",
            "detail": ("两档配了不同的供应商，却是同一把 key。本机那一档已经被摘掉，"
                       "不会把这把凭据发到那台盒子上——但在修好之前，没自带 key 的账号"
                       "都在走付费那一档：**钱照花，而主服务其实没生效**。多半是改了 "
                       "INFE_PILOT_DEFAULT_MODEL_PROVIDER 却没换 INFE_PILOT_DEFAULT_MODEL_KEY。"
                       "修法：sudo bash /opt/cityu-mail-pilot/pilot_app/set_platform_key.sh "
                       "--provider local_openai（粘贴主服务那把 key），装完它会自己跑 "
                       "check-localmodel 自检。现场记录见 docs/local-model-wiring-2026-09-23.md")})

    if current["over_cost"]:
        line = money(current["cost_alert"])
        detail = (f"本月（香港时间 {month['month']}）由管理员 key 付掉的模型调用已经越过警戒线 "
                  f"{line}。这不是硬上限——真正的上限是账上余额（见余额那一条）。"
                  "现在的金额与逐人明细在后台「用量」面板，或跑 "
                  "`python -m pilot_app.manage platform-cost`。")
        if month["unpriced_calls"]:
            # 这一句让详情的形状会变（第一次出现没单价的调用时），但那正是新信息，
            # 值一封邮件；而且它在一个月内只增不减，不会来回抖。
            detail += (f"注意：这个月还有 {month['unpriced_calls']} 次调用**没有配单价**，"
                       "所以真实的金额比这个数更高。")
        if month["unknown_calls"]:
            detail += (f"另有 {month['unknown_calls']} 次调用没有记录是谁的 key 付的"
                       "（早于我们开始记这件事），既没算进管理员头上也没算进用户头上。")
        if month.get("local_calls"):
            # 本机那台主服务也不花钱，所以它不进上面那个金额；但一个只报「花了多少」的
            # 数字会让人以为「这个月就调用了这么几次」——次数与金额在这里是两件事。
            detail += (f"这个月另有 {month['local_calls']} 次调用走的是运营者自建的模型服务"
                       "（不产生供应商账单），没有算进上面这个金额。")
        out.append({"key": "platform_cost_high", "severity": "warning",
                    "title": "本月管理员代付的模型费用越过警戒线", "detail": detail})

    reading_now = current["reading"]
    if in_use and current["balance_readable"] and reading_now is not None:
        where = ("充值在 platform.deepseek.com；这条线是 INFE_PILOT_PLATFORM_BALANCE_FLOOR"
                 "（0 = 关掉这一条），按**账上那个币种**解读——这个账号是人民币，"
                 "而我们记的花费是美元，两个数不能相减。")
        if current["exhausted"]:
            out.append({
                "key": "platform_balance_empty", "severity": "critical",
                "title": "平台 key 余额已用尽：代付已经暂停",
                "detail": (f"DeepSeek 说这把 key 的余额已经不够继续调用（读数是 "
                           f"{reading_now.get('at')}）。借用管理员 key 的账号现在不会再发起调用，"
                           f"用户会看到一句「代付额度已用尽」；他们的邮件仍在自己的转发邮箱里，"
                           f"报告会自动重试，充值后就会补做，不需要手工重放。{where}")})
        elif current["balance_low"]:
            line = money(current["balance_floor"], current["balance_currency"])
            out.append({
                "key": "platform_balance_low", "severity": "warning",
                "title": "平台 key 余额低于警戒线",
                "detail": (f"DeepSeek 账上的余额已经低于警戒线 {line}。余额见底之后，"
                           f"借用管理员 key 的账号会暂停生成报告——**信不会丢**：邮件还在用户"
                           f"自己的转发邮箱里，充值后自动补做，用户也可以自己配一把 key 立刻恢复。"
                           f"{where}")})
    # 「余额检查没在跑」还要一个条件：**这个月真的通过这把 key 花过钱**。
    # 为什么：读不到余额本身不伤人，伤人的是「一边在花、一边没人看着」。开发机、预览库、
    # CI 里都可能在环境里配着一把平台 key（`test_agent` 就是这么起测试的），那里一分钱
    # 都不会花，报一条「检查没在跑」纯属噪音——而**钱一旦真的动起来**，第一次调用就会让
    # 这一项现身，延迟上界是「下一封报告」，不是一个月。
    # 上面那三条（花超了 / 余额低 / 余额见底）不受这个条件限制：只要有人**靠**这把 key
    # 过日子（`in_use`），余额见底就必须一直报，哪怕这个月因为没钱而一次都没调用成。
    if in_use and month["calls"] > 0 and current["stale"]:
        out.append({
            "key": "platform_balance_stale", "severity": "warning",
            "title": "平台 key 的余额检查没在跑",
            "detail": ("读不到余额时账还是照样花，只是没人提前告诉你什么时候会见底。"
                       "在服务器上跑：sudo systemd-run --uid=cityumail "
                       "--property=EnvironmentFile=/etc/cityu-mail-pilot/pilot.env "
                       "--working-directory=/opt/cityu-mail-pilot --pipe --wait --collect "
                       "/opt/cityu-mail-pilot/.venv/bin/python -m pilot_app.manage "
                       "platform-cost --refresh")})
    return out
