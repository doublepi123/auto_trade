"""P301: event study diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

from app.platform.factor_utils import mean, std, validate_pair


@dataclass(frozen=True)
class EventStudyResult:
    events: list[dict[str, float]]
    summary: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {"events": self.events, "summary": self.summary}


def event_study_report(market_returns: list[float], stock_returns: list[float], event_indices: list[int], *, window_before: int = 5, window_after: int = 5) -> EventStudyResult:
    market, stock = validate_pair(market_returns, stock_returns, x_name="market_returns", y_name="stock_returns")
    if len(market) < 3:
        raise ValueError("series must contain at least 3 values")
    if isinstance(window_before, bool) or not isinstance(window_before, int) or window_before < 0:
        raise ValueError("window_before must be a non-negative int")
    if isinstance(window_after, bool) or not isinstance(window_after, int) or window_after < 1:
        raise ValueError("window_after must be a positive int")
    if not isinstance(event_indices, list) or not event_indices:
        raise ValueError("event_indices must be a non-empty list")
    alpha, beta = _regression(market, stock)
    events: list[dict[str, float]] = []
    cars: list[float] = []
    for idx in event_indices:
        if isinstance(idx, bool) or not isinstance(idx, int) or idx < window_before or idx + window_after >= len(stock):
            raise ValueError("event index out of event window range")
        ars: list[float] = []
        for j in range(idx - window_before, idx + window_after + 1):
            expected = alpha + beta * market[j]
            ars.append(stock[j] - expected)
        car = sum(ars)
        sigma = std(ars, sample=True) if len(ars) > 1 else 0.0
        n_ars = len(ars)
        t_stat = 0.0 if sigma == 0 else car / (sigma * math.sqrt(n_ars))
        events.append({"event_index": float(idx), "ar_at_event": ars[window_before], "car": car, "t_stat": t_stat})
        cars.append(car)
    summary = {"mean_car": mean(cars), "std_car": std(cars), "n_events": float(len(events))}
    return EventStudyResult(events, summary)


def _regression(x: list[float], y: list[float]) -> tuple[float, float]:
    mx = mean(x)
    my = mean(y)
    num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
    den = sum((xi - mx) ** 2 for xi in x)
    beta = num / den if den != 0 else 0.0
    alpha = my - beta * mx
    return alpha, beta


__all__ = ["EventStudyResult", "event_study_report"]
