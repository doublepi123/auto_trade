"""P282: build research bars from ticks."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


@dataclass(frozen=True)
class BarBuilderResult:
    mode: str
    bars: list[dict[str, Any]]
    bar_count: int

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "bars": self.bars, "bar_count": self.bar_count}


def build_bars(ticks: list[dict[str, Any]], *, mode: str = "tick", threshold: float = 100) -> BarBuilderResult:
    if mode not in {"tick", "volume", "dollar"}:
        raise ValueError("mode must be tick, volume, or dollar")
    bar_threshold = _finite(threshold, "threshold")
    if bar_threshold <= 0:
        raise ValueError("threshold must be positive")
    if not isinstance(ticks, list) or not ticks:
        raise ValueError("ticks must be non-empty")
    bars: list[dict[str, Any]] = []
    cur: list[dict[str, Any]] = []
    acc = 0.0
    for tick in ticks:
        if not isinstance(tick, dict):
            raise ValueError("ticks must contain dicts")
        if "price" not in tick:
            raise ValueError("tick price is required")
        price = _finite(tick["price"], "tick price")
        volume = _finite(tick.get("volume", 0.0), "tick volume")
        if price <= 0 or volume < 0:
            raise ValueError("tick price must be positive and volume non-negative")
        cur.append({"timestamp": str(tick.get("timestamp")), "price": price, "volume": volume})
        acc += 1.0 if mode == "tick" else volume if mode == "volume" else price * volume
        if acc >= bar_threshold:
            bars.append(_make_bar(cur))
            cur = []
            acc = 0.0
    if cur:
        bars.append(_make_bar(cur))
    return BarBuilderResult(mode, bars, len(bars))


def _make_bar(ticks: list[dict[str, Any]]) -> dict[str, Any]:
    prices = [float(t["price"]) for t in ticks]
    volume = sum(float(t["volume"]) for t in ticks)
    return {"start": ticks[0]["timestamp"], "end": ticks[-1]["timestamp"], "open": prices[0], "high": max(prices), "low": min(prices), "close": prices[-1], "volume": volume, "dollar_value": sum(float(t["price"]) * float(t["volume"]) for t in ticks), "tick_count": len(ticks)}


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


__all__ = ["BarBuilderResult", "build_bars"]
