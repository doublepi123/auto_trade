from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

DEFAULT_T_CRITICAL = 2.0


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _stdev(values: Sequence[float]) -> float:
    n = len(values)
    mean = _mean(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / (n - 1))


def _t_statistic(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    standard_deviation = _stdev(values)
    if standard_deviation <= 0:
        return None
    return _mean(values) / (standard_deviation / math.sqrt(len(values)))


@dataclass(frozen=True)
class ClusteredTTestResult:
    observations: int
    distinct_days: int
    naive_t: float | None
    clustered_t: float | None
    naive_mean: float | None
    day_mean: float | None
    clustered_standard_error: float | None
    ci_lower: float | None
    ci_upper: float | None
    inflation_factor: float | None
    significant: bool


def clustered_t_test(
    observations: Sequence[tuple[date, float]],
    *,
    t_critical: float = DEFAULT_T_CRITICAL,
) -> ClusteredTTestResult:
    """Test the per-trade mean with a day-clustered robust standard error.

    For the intercept-only per-trade estimand, each cluster contributes the sum
    of its trade residuals. The CR1 ``G / (G - 1)`` correction keeps cluster
    influence proportional to trade count instead of estimating an average day.
    """
    if t_critical <= 0:
        raise ValueError("t_critical must be positive")

    values = [value for _, value in observations]
    if not values:
        return ClusteredTTestResult(
            observations=0,
            distinct_days=0,
            naive_t=None,
            clustered_t=None,
            naive_mean=None,
            day_mean=None,
            clustered_standard_error=None,
            ci_lower=None,
            ci_upper=None,
            inflation_factor=None,
            significant=False,
        )

    by_day: dict[date, list[float]] = {}
    for day, value in observations:
        by_day.setdefault(day, []).append(value)
    day_means = [_mean(day_values) for day_values in by_day.values()]

    trade_mean = _mean(values)
    naive_t = _t_statistic(values)
    distinct_days = len(by_day)
    clustered_standard_error: float | None = None
    if distinct_days >= 2:
        cluster_score_squares = sum(
            sum(value - trade_mean for value in day_values) ** 2
            for day_values in by_day.values()
        )
        variance = (
            distinct_days
            / (distinct_days - 1)
            * cluster_score_squares
            / (len(values) ** 2)
        )
        if variance > 0:
            clustered_standard_error = math.sqrt(variance)
    clustered_t = (
        trade_mean / clustered_standard_error
        if clustered_standard_error is not None
        else None
    )
    ci_lower = (
        trade_mean - t_critical * clustered_standard_error
        if clustered_standard_error is not None
        else None
    )
    ci_upper = (
        trade_mean + t_critical * clustered_standard_error
        if clustered_standard_error is not None
        else None
    )
    return ClusteredTTestResult(
        observations=len(values),
        distinct_days=distinct_days,
        naive_t=naive_t,
        clustered_t=clustered_t,
        naive_mean=trade_mean,
        day_mean=_mean(day_means),
        clustered_standard_error=clustered_standard_error,
        ci_lower=ci_lower,
        ci_upper=ci_upper,
        inflation_factor=math.sqrt(len(values) / distinct_days),
        significant=clustered_t is not None and clustered_t > t_critical,
    )


__all__ = ["ClusteredTTestResult", "DEFAULT_T_CRITICAL", "clustered_t_test"]
