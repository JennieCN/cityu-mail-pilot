"""Turn token counts into money, without ever pretending to know a stale price.

Two rules shape this module:

1. **A price is data, not code.** Provider prices change and they vary by model,
   by cache hit, and even by time of day. Only prices verified from a provider's
   own pricing page are shipped here, each with the date it was read; anything
   else stays unpriced and the console shows "价格未配置" instead of a confident
   wrong number. An operator can override any of them from the admin console
   without touching code.
2. **The price used for a call is frozen onto that call.** ``token_usage`` rows
   store the rates that applied when they were written, so editing a price today
   never rewrites what last month actually cost.

Verified sources (read 2026-09-14):

* DeepSeek — https://api-docs.deepseek.com/quick_start/pricing
  Off-peak is half of peak. Peak = 01:00-04:00 and 06:00-10:00 UTC, Mon-Fri.
  Cache-hit input is ~50x cheaper than cache-miss input, which is why the cache
  number is tracked separately rather than folded into the input total.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Optional

PRICE_SOURCE = "DeepSeek 官方价目表，读取于 2026-09-14"

# USD per 1,000,000 tokens. ``None`` means "we have not verified a price", and
# the console must say so rather than assume zero.
DEFAULT_PRICES: dict[tuple[str, str], dict[str, Optional[float]]] = {
    # DeepSeek publishes off-peak rates and doubles them at peak.
    ("deepseek", "deepseek-flash"): {
        "input_cache_hit": 0.003, "input_cache_miss": 0.15, "output": 0.6,
        "peak_multiplier": 2.0, "currency": "USD", "source": PRICE_SOURCE,
    },
    ("deepseek", "deepseek-v4-pro"): {
        "input_cache_hit": 0.022, "input_cache_miss": 0.66, "output": 1.98,
        "peak_multiplier": 2.0, "currency": "USD", "source": PRICE_SOURCE,
    },
    # ``deepseek-chat`` is a legacy alias. Verified 2026-09-14 by reading the
    # response body: requesting deepseek-chat comes back with
    # "model": "deepseek-flash", and /models lists only deepseek-flash and
    # deepseek-v4-pro. So the alias is billed at the Flash rate — this is a
    # measured fact, not a fallback guess.
    #
    # Re-checked 2026-09-15 on the production install (the same key the pilot
    # uses): GET /models returned exactly ['deepseek-flash', 'deepseek-v4-pro'],
    # and deepseek-chat / deepseek-flash answered in 0.5 s / 0.6 s with the same
    # 10 tokens and the same reply. The official docs no longer list
    # deepseek-chat at all, which is why the platform default moved to
    # deepseek-flash the same day — the price row stays because old rows in
    # token_usage still carry the alias and must keep costing the right money.
    ("deepseek", "deepseek-chat"): {
        "input_cache_hit": 0.003, "input_cache_miss": 0.15, "output": 0.6,
        "peak_multiplier": 2.0, "currency": "USD",
        "source": PRICE_SOURCE + "（deepseek-chat 经实测由 deepseek-flash 承接，按 Flash 价目计）",
    },
}

# DeepSeek peak windows, in UTC. Monday=0 .. Sunday=6.
_PEAK_WINDOWS = ((1, 4), (6, 10))
_PEAK_WEEKDAYS = (0, 1, 2, 3, 4)


def is_peak(moment: dt.datetime) -> bool:
    """True inside DeepSeek's peak-pricing windows (used by the default table)."""
    moment = moment.astimezone(dt.timezone.utc)
    if moment.weekday() not in _PEAK_WEEKDAYS:
        return False
    return any(start <= moment.hour < end for start, end in _PEAK_WINDOWS)


def normalize(price: dict[str, Any] | None) -> Optional[dict[str, Any]]:
    """Keep only usable numbers; a price missing its output rate is no price."""
    if not price:
        return None
    try:
        output = float(price["output"])
        cache_miss = float(price.get("input_cache_miss", price.get("input", 0)) or 0)
        cache_hit = float(price.get("input_cache_hit", cache_miss) or 0)
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "input_cache_hit": max(0.0, cache_hit),
        "input_cache_miss": max(0.0, cache_miss),
        "output": max(0.0, output),
        "peak_multiplier": float(price.get("peak_multiplier") or 1.0),
        "currency": str(price.get("currency") or "USD")[:8],
        "source": str(price.get("source") or "")[:200],
    }


def lookup(provider: str, model: str, overrides: dict[tuple[str, str], dict] | None = None):
    """Operator override first, then the verified built-in table."""
    key = (str(provider or "").lower(), str(model or ""))
    if overrides and key in overrides:
        normalized = normalize(overrides[key])
        if normalized:
            return normalized
    return normalize(DEFAULT_PRICES.get(key))


def estimate(usage: dict[str, Any] | None, price: dict[str, Any] | None,
             at: dt.datetime | None = None) -> Optional[dict[str, Any]]:
    """Cost of one call, split so the console can explain where it went.

    Returns ``None`` when there is no price: showing "0.00" for an unknown model
    would make the total wrong in the most damaging direction (too low).
    """
    if not usage or not price:
        return None
    at = at or dt.datetime.now(dt.timezone.utc)
    multiplier = price["peak_multiplier"] if is_peak(at) else 1.0

    total_input = int(usage.get("input") or 0)
    cached = min(int(usage.get("cached_input") or 0), total_input) if total_input else 0
    uncached = max(0, total_input - cached)
    output = int(usage.get("output") or 0)

    per_million = 1_000_000
    cache_hit_cost = cached / per_million * price["input_cache_hit"] * multiplier
    cache_miss_cost = uncached / per_million * price["input_cache_miss"] * multiplier
    output_cost = output / per_million * price["output"] * multiplier
    return {
        "currency": price["currency"],
        "peak": multiplier > 1.0,
        "input_cache_hit_cost": round(cache_hit_cost, 8),
        "input_cache_miss_cost": round(cache_miss_cost, 8),
        "output_cost": round(output_cost, 8),
        "total_cost": round(cache_hit_cost + cache_miss_cost + output_cost, 8),
    }
