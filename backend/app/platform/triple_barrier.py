"""P280: triple-barrier supervised labels."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from app.platform.factor_utils import mean, validate_series


@dataclass(frozen=True)
class TripleBarrierResult:
    labels: list[dict[str, Any]]
    summary: dict[str, float | int]

    def to_dict(self) -> dict[str, Any]:
        return {"labels": self.labels, "summary": self.summary}


def triple_barrier_report(prices: list[float], events: list[dict[str, Any]], *, profit_take_pct: float = 0.02, stop_loss_pct: float = 0.01, max_holding_bars: int = 5) -> TripleBarrierResult:
    px = validate_series(prices, name="prices", min_len=1)
    if any(price <= 0 for price in px):
        raise ValueError("prices must be positive")
    profit_take = _finite(profit_take_pct, "profit_take_pct")
    stop_loss = _finite(stop_loss_pct, "stop_loss_pct")
    if profit_take <= 0 or stop_loss <= 0:
        raise ValueError("barrier percentages must be positive")
    if isinstance(max_holding_bars, bool) or not isinstance(max_holding_bars, int) or max_holding_bars < 1:
        raise ValueError("max_holding_bars must be a positive int")
    if not isinstance(events, list) or not events:
        raise ValueError("events must be a non-empty list")
    labels: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("events must contain dicts")
        idx = event.get("index")
        if isinstance(idx, bool) or not isinstance(idx, int) or idx < 0 or idx >= len(px):
            raise ValueError("event index out of range")
        side = str(event.get("side", "long"))
        if side not in {"long", "short"}:
            raise ValueError("event side must be long or short")
        entry = px[idx]
        end = min(len(px) - 1, idx + max_holding_bars)
        label = 0
        hit = "timeout"
        exit_idx = end
        for j in range(idx + 1, end + 1):
            ret = (px[j] - entry) / entry if side == "long" else (entry - px[j]) / entry
            if ret >= profit_take:
                label, hit, exit_idx = 1, "profit_take", j
                break
            if ret <= -stop_loss:
                label, hit, exit_idx = -1, "stop_loss", j
                break
        final_ret = (px[exit_idx] - entry) / entry if side == "long" else (entry - px[exit_idx]) / entry
        labels.append({"event_index": idx, "side": side, "label": label, "hit": hit, "exit_index": exit_idx, "return_pct": final_ret, "holding_bars": exit_idx - idx})
    return TripleBarrierResult(labels, {"n_events": len(labels), "positive": sum(1 for x in labels if x["label"] == 1), "negative": sum(1 for x in labels if x["label"] == -1), "timeout": sum(1 for x in labels if x["label"] == 0), "avg_holding": mean([float(x["holding_bars"]) for x in labels])})


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


__all__ = ["TripleBarrierResult", "triple_barrier_report"]
