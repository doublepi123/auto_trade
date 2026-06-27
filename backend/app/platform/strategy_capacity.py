"""P306: strategy capacity diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


@dataclass(frozen=True)
class StrategyCapacityResult:
    max_aum: float
    impact_at_max_aum_bps: float
    capacity_score: float

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def strategy_capacity_report(*, signal_autocorr: float, adv: float, turnover: float, impact_threshold_bps: float = 10.0) -> StrategyCapacityResult:
    corr = _finite(signal_autocorr, "signal_autocorr")
    if corr < -1 or corr > 1:
        raise ValueError("signal_autocorr must be in [-1, 1]")
    daily_volume = _positive(adv, "adv")
    turn = _non_negative(turnover, "turnover")
    threshold = _positive(impact_threshold_bps, "impact_threshold_bps")
    # Heuristic capacity: participation such that sqrt-impact bps reaches threshold.
    # impact_bps ~ volatility_proxy * sqrt(participation) * 10000 * 0.5; volatility_proxy from signal decay.
    vol_proxy = max(1e-6, 1.0 - corr)
    # participation at threshold: threshold = vol_proxy * sqrt(p) * 10000 * 0.5
    p_threshold = (threshold / (vol_proxy * 10000.0 * 0.5)) ** 2
    p_threshold = min(0.5, p_threshold)
    max_aum = p_threshold * daily_volume / max(turn, 1e-6)
    capacity_score = max(0.0, min(1.0, corr))  # higher autocorr = more capacity
    return StrategyCapacityResult(max_aum, threshold, capacity_score)


def _positive(value: Any, name: str) -> float:
    number = _finite(value, name)
    if number <= 0:
        raise ValueError(f"{name} must be positive")
    return number


def _non_negative(value: Any, name: str) -> float:
    number = _finite(value, name)
    if number < 0:
        raise ValueError(f"{name} must be non-negative")
    return number


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


__all__ = ["StrategyCapacityResult", "strategy_capacity_report"]
