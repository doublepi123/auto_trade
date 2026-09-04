"""Pure preregistered futility assessment for signal-edge evidence."""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist
from typing import Final, Literal

from app.domain.strategy_v2.clustered_returns import (
    DEFAULT_T_CRITICAL,
    ClusteredTTestResult,
)

PREREGISTERED_COST_FLOOR_BPS: Final = 10.0
PREREGISTERED_SIGMA_DAY_BPS: Final = 20.0
PREREGISTERED_POWER: Final = 0.80
FUTILITY_BOUND_CRITICAL_VALUE: Final = DEFAULT_T_CRITICAL
_PERCENT_TO_BPS: Final = 100.0

FutilityStatusLabel = Literal["ALIVE", "FUTILE", "INSUFFICIENT_DATA"]


def minimum_detectable_effect_bps(
    *,
    sigma_day_bps: float,
    distinct_days: int,
    alpha: float = 0.05,
    power: float = PREREGISTERED_POWER,
) -> float:
    """Return the one-sided normal-approximation MDE in basis points."""
    if not math.isfinite(sigma_day_bps) or sigma_day_bps <= 0:
        message = "sigma_day_bps must be finite and positive"
        raise ValueError(message)
    if distinct_days < 1:
        message = "distinct_days must be positive"
        raise ValueError(message)
    if not math.isfinite(alpha) or not 0.0 < alpha < 1.0:
        message = "alpha must be finite and in (0, 1)"
        raise ValueError(message)
    if not math.isfinite(power) or not 0.0 < power < 1.0:
        message = "power must be finite and in (0, 1)"
        raise ValueError(message)

    normal = NormalDist()
    z_total = normal.inv_cdf(1.0 - alpha) + normal.inv_cdf(power)
    return z_total * sigma_day_bps / math.sqrt(distinct_days)


@dataclass(frozen=True, slots=True)
class FutilityResult:
    """Read-only result of the preregistered three-way stopping rule."""

    status: FutilityStatusLabel
    reasons: tuple[str, ...]
    distinct_days: int
    cost_floor_bps: float
    sigma_day_bps: float
    alpha: float
    power: float
    bound_critical_value: float
    gross_mean_bps: float | None
    gross_upper_bound_bps: float | None
    net_mean_bps: float | None
    measured_cost_bps: float | None
    sigma_day_measured_bps: float | None
    mde_bps: float | None
    required_effect_bps: float | None
    upper_bound_below_cost_floor: bool
    powered_for_required_effect: bool


def assess_futility(
    *,
    gross: ClusteredTTestResult,
    net: ClusteredTTestResult,
    evidence_sufficient: bool,
    cost_floor_bps: float = PREREGISTERED_COST_FLOOR_BPS,
    sigma_day_bps: float = PREREGISTERED_SIGMA_DAY_BPS,
    alpha: float = 0.05,
    power: float = PREREGISTERED_POWER,
    bound_critical_value: float = FUTILITY_BOUND_CRITICAL_VALUE,
) -> FutilityResult:
    """Apply the preregistered report-only futility rule to clustered returns."""
    if not math.isfinite(cost_floor_bps) or cost_floor_bps <= 0:
        message = "cost_floor_bps must be finite and positive"
        raise ValueError(message)
    if not math.isfinite(bound_critical_value) or bound_critical_value <= 0:
        message = "bound_critical_value must be finite and positive"
        raise ValueError(message)
    if not math.isfinite(sigma_day_bps) or sigma_day_bps <= 0:
        message = "sigma_day_bps must be finite and positive"
        raise ValueError(message)
    if not math.isfinite(alpha) or not 0.0 < alpha < 1.0:
        message = "alpha must be finite and in (0, 1)"
        raise ValueError(message)
    if not math.isfinite(power) or not 0.0 < power < 1.0:
        message = "power must be finite and in (0, 1)"
        raise ValueError(message)

    distinct_days = gross.distinct_days
    gross_mean_bps = (
        gross.naive_mean * _PERCENT_TO_BPS
        if gross.naive_mean is not None
        else None
    )
    gross_upper_bound_bps = (
        (
            gross.naive_mean
            + bound_critical_value * gross.clustered_standard_error
        )
        * _PERCENT_TO_BPS
        if gross.naive_mean is not None
        and gross.clustered_standard_error is not None
        else None
    )
    net_mean_bps = (
        net.naive_mean * _PERCENT_TO_BPS
        if net.naive_mean is not None
        else None
    )
    measured_cost_bps = (
        gross_mean_bps - net_mean_bps
        if gross_mean_bps is not None and net_mean_bps is not None
        else None
    )
    sigma_day_measured_bps = (
        gross.clustered_standard_error
        * math.sqrt(distinct_days)
        * _PERCENT_TO_BPS
        if gross.clustered_standard_error is not None and distinct_days > 0
        else None
    )
    mde_bps = (
        minimum_detectable_effect_bps(
            sigma_day_bps=sigma_day_bps,
            distinct_days=distinct_days,
            alpha=alpha,
            power=power,
        )
        if distinct_days > 0
        else None
    )
    required_effect_bps = (
        cost_floor_bps - gross_mean_bps
        if gross_mean_bps is not None
        else None
    )
    upper_bound_below_cost_floor = (
        gross_upper_bound_bps is not None
        and gross_upper_bound_bps < cost_floor_bps
    )
    powered_for_required_effect = (
        mde_bps is not None
        and required_effect_bps is not None
        and mde_bps <= required_effect_bps
    )

    unavailable = (
        gross.naive_mean is None
        or gross.clustered_standard_error is None
        or net.naive_mean is None
    )
    if unavailable:
        status: FutilityStatusLabel = "INSUFFICIENT_DATA"
        reasons = ("gross or net mean or clustered standard error unavailable",)
    elif not evidence_sufficient:
        status = "INSUFFICIENT_DATA"
        reasons = ("verdict evidence floors not met",)
    elif not upper_bound_below_cost_floor:
        status = "ALIVE"
        reasons = ("gross upper bound still reaches the preregistered cost floor",)
    elif powered_for_required_effect:
        status = "FUTILE"
        reasons = (
            "cost-clearing gross edge is excluded; abandonment requires human ratification",
        )
    else:
        status = "INSUFFICIENT_DATA"
        reasons = ("underpowered for the required cost-clearing effect",)

    return FutilityResult(
        status=status,
        reasons=reasons,
        distinct_days=distinct_days,
        cost_floor_bps=cost_floor_bps,
        sigma_day_bps=sigma_day_bps,
        alpha=alpha,
        power=power,
        bound_critical_value=bound_critical_value,
        gross_mean_bps=gross_mean_bps,
        gross_upper_bound_bps=gross_upper_bound_bps,
        net_mean_bps=net_mean_bps,
        measured_cost_bps=measured_cost_bps,
        sigma_day_measured_bps=sigma_day_measured_bps,
        mde_bps=mde_bps,
        required_effect_bps=required_effect_bps,
        upper_bound_below_cost_floor=upper_bound_below_cost_floor,
        powered_for_required_effect=powered_for_required_effect,
    )


__all__ = [
    "FUTILITY_BOUND_CRITICAL_VALUE",
    "PREREGISTERED_COST_FLOOR_BPS",
    "PREREGISTERED_POWER",
    "PREREGISTERED_SIGMA_DAY_BPS",
    "FutilityResult",
    "FutilityStatusLabel",
    "assess_futility",
    "minimum_detectable_effect_bps",
]
