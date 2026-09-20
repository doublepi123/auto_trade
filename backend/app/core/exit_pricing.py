from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite
from typing import Any


@dataclass(frozen=True)
class ReferencePrice:
    price: float
    source: str
    source_timestamp: datetime
    observed_at: datetime


def parse_quote_source_timestamp(value: object) -> datetime | None:
    """Parse quote source time with the runner's existing UTC semantics."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if raw.replace(".", "", 1).isdigit():
            numeric = float(raw)
            if numeric > 10_000_000_000:
                numeric /= 1000
            source_time = datetime.fromtimestamp(numeric, tz=timezone.utc)
        else:
            source_time = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if source_time.tzinfo is None:
                source_time = source_time.replace(tzinfo=timezone.utc)
            else:
                source_time = source_time.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return source_time


def select_reference_price(
    recent_quotes: Sequence[Mapping[str, Any]],
    *,
    side: str,
    now: datetime,
    max_age_seconds: float,
) -> ReferencePrice | None:
    """Scan append-ordered history newest-first for a fresh executable price.

    Missing observation metadata falls back to source time. Freshness always
    uses source time, so a recent observation cannot rejuvenate an old quote.
    """
    match side:
        case "LONG" | "SELL":
            field, source = "bid", "trusted_bid"
        case "SHORT" | "BUY_TO_COVER":
            field, source = "ask", "trusted_ask"
        case _:
            return None
    for entry in reversed(recent_quotes):
        if entry.get("trusted") is not True:
            continue
        price: object = entry.get(field)
        if not isinstance(price, (int, float)) or not isfinite(price) or price <= 0:
            continue
        source_time = parse_quote_source_timestamp(entry.get("timestamp"))
        if source_time is None:
            continue
        age_seconds = (now - source_time).total_seconds()
        if not -5.0 <= age_seconds <= max_age_seconds:
            continue
        observed_at = parse_quote_source_timestamp(entry.get("observed_at"))
        return ReferencePrice(float(price), source, source_time, observed_at or source_time)
    return None


@dataclass(frozen=True)
class DegradedExitPrice:
    limit_price: float
    floor_price: float
    marketable: bool
    reference: ReferencePrice


def degraded_exit_limit(
    *,
    side: str,
    bid: float,
    ask: float,
    reference: ReferencePrice,
    max_adverse_deviation_pct: float,
) -> DegradedExitPrice | None:
    """Bound adverse exit prices without inventing executable liquidity."""
    if (
        not isfinite(reference.price)
        or reference.price <= 0
        or not isfinite(max_adverse_deviation_pct)
        or max_adverse_deviation_pct <= 0
    ):
        return None
    match side:
        case "LONG" | "SELL":
            if not isfinite(bid) or bid <= 0:
                return None
            floor = reference.price * (1 - max_adverse_deviation_pct / 100)
            return DegradedExitPrice(max(bid, floor), floor, bid >= floor, reference)
        case "SHORT" | "BUY_TO_COVER":
            if not isfinite(ask) or ask <= 0:
                return None
            floor = reference.price * (1 + max_adverse_deviation_pct / 100)
            return DegradedExitPrice(min(ask, floor), floor, ask <= floor, reference)
        case _:
            return None
