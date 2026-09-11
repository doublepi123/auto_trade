"""Pure normal-approximation selection power and exact reach-gate math."""
from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from statistics import NormalDist, stdev
from typing import Final, Literal

from app.domain.strategy_v2.signal_edge import binomial_p_upper

DEFAULT_ALPHA: Final = 0.05
DEFAULT_POWER: Final = 0.80
MIN_SIGMA_OBSERVATIONS: Final = 10


def _validate_design(delta_bps: float, alpha: float, power: float) -> None:
    """Reject invalid effect sizes and normal-approximation probabilities."""
    if not math.isfinite(delta_bps) or delta_bps <= 0:
        message = "delta_bps must be finite and positive"
        raise ValueError(message)
    for name, value in (("alpha", alpha), ("power", power)):
        if not math.isfinite(value) or not 0.0 < value < 1.0:
            message = f"{name} must be finite and in (0, 1)"
            raise ValueError(message)


def required_trades_one_sample(
    *, sigma_bps: float, delta_bps: float,
    alpha: float = DEFAULT_ALPHA, power: float = DEFAULT_POWER,
) -> int:
    """Return the one-sided normal-approximation trade requirement."""
    _validate_design(delta_bps, alpha, power)
    if not math.isfinite(sigma_bps) or sigma_bps <= 0:
        message = "sigma_bps must be finite and positive"
        raise ValueError(message)
    normal = NormalDist()
    z_total = normal.inv_cdf(1.0 - alpha) + normal.inv_cdf(power)
    return math.ceil((z_total * sigma_bps / delta_bps) ** 2)


def required_trades_two_sample(
    *, sigma_bps: float, delta_bps: float,
    alpha: float = DEFAULT_ALPHA, power: float = DEFAULT_POWER,
) -> int:
    """Return exactly twice the rounded one-sample trade requirement."""
    return 2 * required_trades_one_sample(
        sigma_bps=sigma_bps, delta_bps=delta_bps, alpha=alpha, power=power,
    )


@dataclass(frozen=True, slots=True)
class SelectionPowerResult:
    """Read-only power estimate using pooled dispersion and symbol counts."""

    sigma_bps: float | None
    sigma_observations: int
    delta_bps: float
    alpha: float
    power: float
    required_one_sample: int | None
    required_two_sample: int | None
    max_trades_held: int
    max_trades_symbol: str
    shortfall_factor: float | None
    verdict: Literal["POWERED", "UNPOWERED", "UNMEASURABLE"]


def assess_selection_power(
    *, per_trade_returns_bps: Sequence[float], held_by_symbol: Mapping[str, int],
    delta_bps: float, alpha: float = DEFAULT_ALPHA, power: float = DEFAULT_POWER,
) -> SelectionPowerResult:
    """Assess nominal per-symbol power; this does not correct for clustering.

    Empty holdings use symbol ``""`` and count zero. A zero denominator has
    no shortfall ratio. Tied maxima retain mapping iteration order. Constant
    returns fail the positive-sigma requirement once the observation floor is met.
    """
    _validate_design(delta_bps, alpha, power)
    if any(not math.isfinite(value) for value in per_trade_returns_bps):
        message = "per_trade_returns_bps must contain only finite values"
        raise ValueError(message)
    if any(count < 0 for count in held_by_symbol.values()):
        message = "held_by_symbol counts must be non-negative"
        raise ValueError(message)
    max_symbol, max_held = max(
        held_by_symbol.items(), key=lambda item: item[1], default=("", 0),
    )
    observations = len(per_trade_returns_bps)
    sigma = stdev(per_trade_returns_bps) if observations >= MIN_SIGMA_OBSERVATIONS else None
    required = (
        required_trades_one_sample(
            sigma_bps=sigma, delta_bps=delta_bps, alpha=alpha, power=power,
        )
        if sigma is not None else None
    )
    verdict: Literal["POWERED", "UNPOWERED", "UNMEASURABLE"] = "UNMEASURABLE"
    if required is not None:
        verdict = "POWERED" if max_held >= required else "UNPOWERED"
    return SelectionPowerResult(
        sigma_bps=sigma,
        sigma_observations=observations,
        delta_bps=delta_bps,
        alpha=alpha,
        power=power,
        required_one_sample=required,
        required_two_sample=2 * required if required is not None else None,
        max_trades_held=max_held,
        max_trades_symbol=max_symbol,
        shortfall_factor=required / max_held if required is not None and max_held > 0 else None,
        verdict=verdict,
    )


@dataclass(frozen=True, slots=True)
class ReachGateOperatingPoint:
    """Exact probabilities of passing a fixed reach-rate gate."""

    n: int
    k_min: int
    alpha_at_loser: float
    power_at_winner: float


def reach_gate_operating_point(
    *, n: int, min_rate_pct: float, loser_reach_p: float, winner_reach_p: float,
) -> ReachGateOperatingPoint:
    """Return exact binomial tails for the inclusive percentage threshold."""
    if n <= 0:
        message = "n must be positive"
        raise ValueError(message)
    if not math.isfinite(min_rate_pct) or not 0.0 <= min_rate_pct <= 100.0:
        message = "min_rate_pct must be finite and in [0, 100]"
        raise ValueError(message)
    k_min = math.ceil(n * (min_rate_pct / 100.0))
    # Match the specified floating-point comparison at exact rate boundaries.
    while k_min > 0 and (k_min - 1) / n * 100 >= min_rate_pct:
        k_min -= 1
    while k_min / n * 100 < min_rate_pct:
        k_min += 1
    return ReachGateOperatingPoint(
        n=n,
        k_min=k_min,
        alpha_at_loser=binomial_p_upper(k_min, n, loser_reach_p),
        power_at_winner=binomial_p_upper(k_min, n, winner_reach_p),
    )
