"""Volatility-targeted leverage and inverse-risk portfolio weights.

Realized volatility follows the annualized sample-standard-deviation
convention used by vectorbt and empyrical. Inverse-volatility and
inverse-variance weights follow Riskfolio-Lib's risk-based allocation
conventions. The exponentially weighted estimate uses the RiskMetrics (1996)
daily decay convention.

Pure Python, with no numpy/scipy dependency.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence, TypedDict

__all__ = [
    "VolTargetReport",
    "ewma_vol",
    "inverse_variance_weights",
    "inverse_vol_weights",
    "realized_vol",
    "vol_target_leverage",
    "vol_target_report",
]


class _VolTargetReportDict(TypedDict):
    realized_vol: float
    ewma_vol: float
    leverage: float
    n_periods: int


def _finite_values(values: Sequence[float]) -> list[float]:
    return [value for value in values if math.isfinite(value)]


def realized_vol(
    returns: Sequence[float],
    ann_factor: float = 252,
    ddof: int = 1,
    min_periods: int = 2,
) -> float:
    """Return annualized standard deviation after dropping non-finite returns."""
    if ann_factor <= 0.0:
        raise ValueError("ann_factor must be > 0")
    if ddof < 0:
        raise ValueError("ddof must be >= 0")
    if min_periods < 1:
        raise ValueError("min_periods must be >= 1")

    finite_returns = _finite_values(returns)
    n_periods = len(finite_returns)
    if n_periods == 0 or n_periods < min_periods:
        raise ValueError("insufficient finite returns")
    if n_periods - ddof <= 0:
        raise ValueError("number of finite returns must exceed ddof")

    mean_return = sum(finite_returns) / n_periods
    variance = sum((value - mean_return) ** 2 for value in finite_returns) / (
        n_periods - ddof
    )
    return math.sqrt(variance) * math.sqrt(ann_factor)


def vol_target_leverage(
    realized_vol: float,
    target_vol: float,
    max_leverage: float = 1.0,
    min_vol_floor: float = 1e-8,
) -> float:
    """Scale exposure to a volatility target, capped at ``max_leverage``."""
    if target_vol < 0.0:
        raise ValueError("target_vol must be >= 0")
    if max_leverage <= 0.0:
        raise ValueError("max_leverage must be > 0")
    if min_vol_floor < 0.0:
        raise ValueError("min_vol_floor must be >= 0")
    if realized_vol <= min_vol_floor:
        return 0.0
    return min(target_vol / realized_vol, max_leverage)


def _inverse_risk_weights(
    realized_vols: Sequence[float], floor: float, power: int
) -> list[float]:
    if len(realized_vols) == 0:
        raise ValueError("realized_vols must not be empty")
    if floor < 0.0 or not math.isfinite(floor):
        raise ValueError("floor must be finite and >= 0")

    scores = [
        1.0 / (volatility**power)
        if math.isfinite(volatility) and volatility > floor
        else 0.0
        for volatility in realized_vols
    ]
    total = sum(scores)
    if total <= 0.0 or not math.isfinite(total):
        raise ValueError("at least one volatility must exceed floor")
    return [score / total for score in scores]


def inverse_vol_weights(
    realized_vols: Sequence[float], floor: float = 1e-8
) -> list[float]:
    """Return normalized inverse-volatility weights."""
    return _inverse_risk_weights(realized_vols, floor, power=1)


def inverse_variance_weights(
    realized_vols: Sequence[float], floor: float = 1e-8
) -> list[float]:
    """Return normalized inverse-variance weights."""
    return _inverse_risk_weights(realized_vols, floor, power=2)


def ewma_vol(
    returns: Sequence[float], decay: float = 0.94, ann_factor: float = 252
) -> float:
    """Return annualized EWMA volatility using an adjust=False recursion."""
    if not 0.0 < decay < 1.0:
        raise ValueError("decay must be in (0, 1)")
    if ann_factor <= 0.0:
        raise ValueError("ann_factor must be > 0")

    finite_returns = _finite_values(returns)
    n_periods = len(finite_returns)
    if n_periods < 2:
        raise ValueError("need at least two finite returns")

    mean_return = sum(finite_returns) / n_periods
    variance = sum((value - mean_return) ** 2 for value in finite_returns) / n_periods
    alpha = 1.0 - decay
    for value in finite_returns:
        difference = value - mean_return
        mean_return += alpha * difference
        variance = decay * (variance + alpha * difference * difference)

    return math.sqrt(max(variance, 0.0)) * math.sqrt(ann_factor)


@dataclass(frozen=True, slots=True)
class VolTargetReport:
    """Combined realized/EWMA volatility and target-leverage diagnostics."""

    realized_vol: float
    ewma_vol: float
    leverage: float
    n_periods: int

    def to_dict(self) -> _VolTargetReportDict:
        """Return a plain dictionary suitable for serialization."""
        return {
            "realized_vol": self.realized_vol,
            "ewma_vol": self.ewma_vol,
            "leverage": self.leverage,
            "n_periods": self.n_periods,
        }


def vol_target_report(
    returns: Sequence[float],
    target_vol: float,
    ann_factor: float = 252,
    max_leverage: float = 1.0,
    decay: float = 0.94,
) -> VolTargetReport:
    """Return realized/EWMA volatility and realized-vol target leverage."""
    finite_returns = _finite_values(returns)
    realized = realized_vol(finite_returns, ann_factor=ann_factor)
    exponentially_weighted = ewma_vol(
        finite_returns, decay=decay, ann_factor=ann_factor
    )
    leverage = vol_target_leverage(
        realized, target_vol=target_vol, max_leverage=max_leverage
    )
    return VolTargetReport(
        realized_vol=realized,
        ewma_vol=exponentially_weighted,
        leverage=leverage,
        n_periods=len(finite_returns),
    )
