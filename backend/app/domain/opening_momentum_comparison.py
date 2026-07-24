from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import fmean, stdev
from typing import Literal, Sequence


OpeningMomentumRecommendation = Literal[
    "COLLECTING",
    "EARLY_LEADER",
    "LAGGING",
    "INCONCLUSIVE",
    "UNDERPERFORMING",
    "PROMOTION_CANDIDATE",
]


@dataclass(frozen=True)
class OpeningMomentumPairedComparison:
    resolved_sessions: int
    cumulative_delta_bps: float
    mean_delta_bps: float
    outperformance_rate: float
    confidence_lower_bps: float | None
    confidence_upper_bps: float | None
    max_drawdown_delta_bps: float
    risk_guard_passed: bool
    minimum_promotion_sessions: int
    promotion_ready: bool
    recommendation: OpeningMomentumRecommendation


def compare_opening_momentum_variants(
    incumbent_returns_bps: Sequence[float],
    challenger_returns_bps: Sequence[float],
    *,
    minimum_promotion_sessions: int = 20,
    early_read_sessions: int = 5,
    confidence_multiplier: float = 2.1,
    max_drawdown_tolerance_bps: float = 25.0,
    minimum_outperformance_rate: float = 0.55,
) -> OpeningMomentumPairedComparison:
    """Compare same-session policy returns without enabling promotion.

    Skipped policy sessions should be supplied as zero returns by the caller.
    Data-quality failures and unresolved sessions should be excluded before
    calling this pure computation.
    """

    if len(incumbent_returns_bps) != len(challenger_returns_bps):
        raise ValueError("paired return series must have equal length")
    if minimum_promotion_sessions < 2:
        raise ValueError("minimum_promotion_sessions must be at least 2")
    if early_read_sessions < 2:
        raise ValueError("early_read_sessions must be at least 2")
    if early_read_sessions >= minimum_promotion_sessions:
        raise ValueError(
            "early_read_sessions must be below minimum_promotion_sessions"
        )
    numeric_parameters = (
        confidence_multiplier,
        max_drawdown_tolerance_bps,
        minimum_outperformance_rate,
    )
    if any(not math.isfinite(value) for value in numeric_parameters):
        raise ValueError("comparison parameters must be finite")
    if min(confidence_multiplier, max_drawdown_tolerance_bps) < 0:
        raise ValueError(
            "drawdown and confidence parameters must be non-negative"
        )
    if not 0.0 <= minimum_outperformance_rate <= 1.0:
        raise ValueError("minimum_outperformance_rate must be in [0, 1]")

    incumbent = [float(value) for value in incumbent_returns_bps]
    challenger = [float(value) for value in challenger_returns_bps]
    if any(
        not math.isfinite(value)
        for value in (*incumbent, *challenger)
    ):
        raise ValueError("paired returns must be finite")

    deltas = [
        challenger_value - incumbent_value
        for incumbent_value, challenger_value in zip(
            incumbent,
            challenger,
            strict=True,
        )
    ]
    sample_count = len(deltas)
    cumulative_delta = sum(deltas)
    mean_delta = fmean(deltas) if deltas else 0.0
    outperformance_rate = (
        sum(value > 0 for value in deltas) / sample_count
        if sample_count
        else 0.0
    )
    confidence_lower: float | None = None
    confidence_upper: float | None = None
    if sample_count >= 2:
        standard_error = stdev(deltas) / math.sqrt(sample_count)
        confidence_lower = (
            mean_delta - confidence_multiplier * standard_error
        )
        confidence_upper = (
            mean_delta + confidence_multiplier * standard_error
        )

    incumbent_max_drawdown = _max_drawdown_bps(incumbent)
    challenger_max_drawdown = _max_drawdown_bps(challenger)
    max_drawdown_delta = challenger_max_drawdown - incumbent_max_drawdown
    risk_guard_passed = (
        max_drawdown_delta <= max_drawdown_tolerance_bps
    )
    promotion_ready = (
        sample_count >= minimum_promotion_sessions
        and confidence_lower is not None
        and confidence_lower > 0
        and outperformance_rate >= minimum_outperformance_rate
        and risk_guard_passed
    )

    if promotion_ready:
        recommendation: OpeningMomentumRecommendation = (
            "PROMOTION_CANDIDATE"
        )
    elif sample_count < early_read_sessions:
        recommendation = "COLLECTING"
    elif (
        sample_count >= minimum_promotion_sessions
        and confidence_upper is not None
        and confidence_upper < 0
    ):
        recommendation = "UNDERPERFORMING"
    elif sample_count < minimum_promotion_sessions and mean_delta > 0:
        recommendation = "EARLY_LEADER"
    elif mean_delta < 0:
        recommendation = "LAGGING"
    else:
        recommendation = "INCONCLUSIVE"

    return OpeningMomentumPairedComparison(
        resolved_sessions=sample_count,
        cumulative_delta_bps=cumulative_delta,
        mean_delta_bps=mean_delta,
        outperformance_rate=outperformance_rate,
        confidence_lower_bps=confidence_lower,
        confidence_upper_bps=confidence_upper,
        max_drawdown_delta_bps=max_drawdown_delta,
        risk_guard_passed=risk_guard_passed,
        minimum_promotion_sessions=minimum_promotion_sessions,
        promotion_ready=promotion_ready,
        recommendation=recommendation,
    )


def _max_drawdown_bps(returns_bps: Sequence[float]) -> float:
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in returns_bps:
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    return max_drawdown
