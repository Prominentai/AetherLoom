"""Public subscription fields for display, without retaining account secrets.

Field semantics verified against image.novelai.net/docs/doc.json and NovelAI's
public web client, 2026-09-22. Usage percent is the remaining Opus allowance;
timeUntilNextPercent is the seconds needed to recover one percentage point,
not a countdown to the next integer boundary. No request or persistence here.
"""
from collections.abc import Mapping
import math


_TIER_NAMES = {0: "无订阅", 1: "Tablet", 2: "Scroll", 3: "Opus"}


def _numeric(value, *, integer=False):
    """Accept nonnegative, finite JSON numbers; never coerce strings or bools."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value) or value < 0:
            return None
    except (OverflowError, ValueError):
        return None
    if integer:
        if isinstance(value, float) and not value.is_integer():
            return None
        return int(value)
    return value


def _mapping(value):
    return value if isinstance(value, Mapping) else {}


def parse_account(data):
    """Return only safe display values from a direct or wrapped subscription.

    Missing or malformed values stay None, including a total with either
    balance missing. Numeric JSON strings are deliberately not coerced.
    `percent` preserves values above 100 and is not overridden by `negative`;
    the view can show zero when the latter is true, matching the web client.
    """
    source = _mapping(data)
    if "subscription" in source:
        source = _mapping(source["subscription"])
    tier = _numeric(source.get("tier"), integer=True)
    active = source.get("active")
    active = active if isinstance(active, bool) else None
    balance = _mapping(source.get("trainingStepsLeft"))
    fixed = _numeric(balance.get("fixedTrainingStepsLeft"), integer=True)
    purchased = _numeric(balance.get("purchasedTrainingSteps"), integer=True)
    total = fixed + purchased if fixed is not None and purchased is not None else None
    usage = _mapping(source.get("usage"))
    percent = _numeric(usage.get("percent"))
    percent = float(percent) if percent is not None else None
    negative = usage.get("isNegative")
    negative = negative if isinstance(negative, bool) else None
    refill = _numeric(usage.get("timeUntilNextPercent"))
    daily = None
    if refill == 0:
        daily = 0.0
    elif refill is not None:
        rate = 86400 / refill
        # Match the web client's Math.round(rate * 10) / 10 for positive rates.
        if math.isfinite(rate) and math.isfinite(rate * 10):
            daily = math.floor(rate * 10 + 0.5) / 10
    result = {
        "tier": tier,
        "tier_name": _TIER_NAMES.get(tier, "未知" if tier is None else f"未知等级 {tier}"),
        "active": active,
        "fixed": fixed,
        "purchased": purchased,
        "total": total,
        "percent": percent,
        "negative": negative,
        "refill_seconds": refill,
        "daily_refill": daily,
        "expires_at": _numeric(source.get("expiresAt")),
    }
    if 'accountType' in source:
        result['account_type'] = _numeric(source.get('accountType'), integer=True)
    return result


def duration(seconds):
    """Format an interval approximately in Chinese hours and minutes."""
    value = _numeric(seconds)
    if value is None:
        return "—"
    if value == 0:
        return "0 分钟"
    if value < 60:
        return "不到 1 分钟"
    minutes = math.floor(value / 60 + 0.5)
    hours, minutes = divmod(minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours:,} 小时")
    if minutes:
        parts.append(f"{minutes} 分钟")
    return "约 " + " ".join(parts)


def number(value):
    """Format a finite nonnegative number with grouping, or an unknown dash."""
    value = _numeric(value)
    if value is None:
        return "—"
    if isinstance(value, int) or value.is_integer():
        return f"{int(value):,}"
    return f"{value:,.2f}".rstrip("0").rstrip(".")
