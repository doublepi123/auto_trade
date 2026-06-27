"""P325: Capacity frontier — AUM impact-degraded Sharpe curve.

Estimate how strategy Sharpe decays as AUM increases due to market impact.
For each AUM level, compute an impact penalty proportional to
sqrt(aum / adv) × turnover, then subtract from the base Sharpe. Returns the
per-level curve plus the optimal AUM (largest AUM with < 10% Sharpe degradation).

Reference: Grinold & Kahn "Active Portfolio Management" Ch.16; Kissell & Glantz
"Optimal Trading Strategies" Ch.4. Pure Python, no new deps.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CapacityFrontierLevel:
    aum: float
    degraded_sharpe: float
    impact_penalty: float


@dataclass(frozen=True)
class CapacityFrontierResult:
    base_sharpe: float
    signal_autocorr: float
    adv: float
    turnover: float
    levels: list[CapacityFrontierLevel]
    optimal_aum: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "base_sharpe": self.base_sharpe,
            "signal_autocorr": self.signal_autocorr,
            "adv": self.adv,
            "turnover": self.turnover,
            "levels": [
                {
                    "aum": lv.aum,
                    "degraded_sharpe": lv.degraded_sharpe,
                    "impact_penalty": lv.impact_penalty,
                }
                for lv in self.levels
            ],
            "optimal_aum": self.optimal_aum,
        }


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    return number


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


def _impact_penalty(aum: float, adv: float, turnover: float, signal_autocorr: float = 0.0) -> float:
    """Impact penalty ∝ sqrt(aum / adv) × turnover × IQ-decay(signal_autocorr)."""
    if adv <= 0:
        return 0.0
    participation = aum / adv
    if participation <= 0:
        return 0.0
    # Scale factor: typical sqrt-impact coefficient ~ 0.5 in bps terms,
    # converted to Sharpe units via a conservative scaling.
    k = 1.58  # calibrated heuristic
    # Grinold-Kahn IQ decay: high-autocorrelation signals retain less
    # independent information per rebalance, amplifying capacity decay.
    iq_decay = 1.0 / max(1e-6, 1.0 - signal_autocorr * signal_autocorr)
    return k * math.sqrt(participation) * turnover * iq_decay


def capacity_frontier_report(
    base_sharpe: float,
    signal_autocorr: float,
    adv: float,
    turnover: float,
    *,
    aum_levels: list[float] | None = None,
) -> CapacityFrontierResult:
    """Compute AUM impact-degraded Sharpe curve.

    Args:
        base_sharpe: Pre-cost strategy Sharpe ratio.
        signal_autocorr: Signal autocorrelation ∈ [-1, 1].
        adv: Average daily volume (currency units).
        turnover: Daily turnover fraction ∈ [0, ∞).
        aum_levels: Custom AUM levels; defaults to [0.01, 0.05, 0.1, 0.2, 0.5] × adv.

    Returns:
        CapacityFrontierResult with per-level degraded Sharpe and optimal AUM.
    """
    base_sharpe_f = _finite(base_sharpe, "base_sharpe")
    autocorr_f = _finite(signal_autocorr, "signal_autocorr")
    if autocorr_f < -1.0 or autocorr_f > 1.0:
        raise ValueError("signal_autocorr must be in [-1, 1]")
    adv_f = _positive(adv, "adv")
    turnover_f = _non_negative(turnover, "turnover")

    if aum_levels is None:
        multipliers = [0.01, 0.05, 0.1, 0.2, 0.5]
        aum_levels_used = [m * adv_f for m in multipliers]
    else:
        if not aum_levels:
            raise ValueError("aum_levels must be non-empty if provided")
        aum_levels_used = [_positive(v, f"aum_levels[{i}]") for i, v in enumerate(aum_levels)]

    levels: list[CapacityFrontierLevel] = []
    optimal_aum = 0.0
    degradation_threshold = 0.10  # 10% max acceptable degradation

    for aum in aum_levels_used:
        penalty = _impact_penalty(aum, adv_f, turnover_f, autocorr_f)
        degraded = max(0.0, base_sharpe_f - penalty)
        levels.append(CapacityFrontierLevel(
            aum=aum,
            degraded_sharpe=degraded,
            impact_penalty=penalty,
        ))
        # Update optimal_aum: largest AUM where degradation < 10%
        if degraded >= base_sharpe_f * (1.0 - degradation_threshold):
            optimal_aum = aum

    return CapacityFrontierResult(
        base_sharpe=base_sharpe_f,
        signal_autocorr=autocorr_f,
        adv=adv_f,
        turnover=turnover_f,
        levels=levels,
        optimal_aum=optimal_aum,
    )


__all__ = [
    "CapacityFrontierLevel",
    "CapacityFrontierResult",
    "capacity_frontier_report",
]
