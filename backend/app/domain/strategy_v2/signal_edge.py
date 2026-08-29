"""Signal edge gate — prove a signal has edge BEFORE tuning its parameters.

Tuning exits on a signal that carries no directional information is second-order
optimisation of noise: it produces a best-looking parameter set that is pure
overfit. This module answers the prior question with two independent checks.

1. First-passage test
   For driftless Brownian motion with an upper barrier at ``+target`` and a
   lower barrier at ``-stop``, the probability of touching the upper barrier
   first is ``stop / (stop + target)`` (the gambler's-ruin result). A signal
   whose realised target-before-stop rate does not exceed that baseline supplies
   no directional information, so no exit re-parameterisation can make it
   profitable. The p-value here is exact.

2. Cluster-robust significance
   Trades cluster on the same calendar days across correlated instruments: on a
   broad up-day every open position wins together, so trades are not
   independent observations. A per-trade t-statistic therefore overstates
   significance by roughly ``sqrt(trades / days)``. Collapsing to one
   observation per day is the standard remedy for that dependence.

Pure computation: no I/O, no database, no broker access.
"""
from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

# |t| at which a two-sided test is roughly 5% significant. The normal value is
# 1.96; with the modest day counts seen here the t-distribution has slightly
# fatter tails (df=25 needs ~2.06), so 2.0 is the pragmatic middle. The
# statistic itself is reported so a reviewer can apply a stricter bar.
DEFAULT_T_CRITICAL = 2.0
DEFAULT_ALPHA = 0.05
# Minimum evidence before a verdict means anything. The day floor matters more
# than the trade floor: 200 trades spread over 5 days carry ~5 observations.
DEFAULT_MIN_RESOLVED_TRADES = 30
DEFAULT_MIN_DISTINCT_DAYS = 20

SignalEdgeVerdictLabel = Literal["PASS", "FAIL", "INSUFFICIENT_DATA"]

VERDICT_PASS: SignalEdgeVerdictLabel = "PASS"
VERDICT_FAIL: SignalEdgeVerdictLabel = "FAIL"
VERDICT_INSUFFICIENT_DATA: SignalEdgeVerdictLabel = "INSUFFICIENT_DATA"


def first_passage_baseline(*, stop_pct: float, target_pct: float) -> float:
    """Driftless probability of reaching ``+target_pct`` before ``-stop_pct``."""
    if not math.isfinite(stop_pct) or not math.isfinite(target_pct):
        raise ValueError("barrier distances must be finite")
    if stop_pct <= 0 or target_pct <= 0:
        raise ValueError("barrier distances must be positive")
    return stop_pct / (stop_pct + target_pct)


def binomial_p_upper(k: int, n: int, p: float) -> float:
    """Exact ``P(X >= k)`` for ``X ~ Binomial(n, p)``.

    One-sided by construction: the question is whether the signal BEATS the
    random-walk baseline, not whether it merely differs from it.
    """
    if n < 0 or k < 0:
        raise ValueError("counts must be non-negative")
    if not 0.0 <= p <= 1.0:
        raise ValueError("probability must be in [0, 1]")
    if n == 0:
        return float("nan")
    if k > n:
        return 0.0
    return sum(
        math.comb(n, i) * (p**i) * ((1.0 - p) ** (n - i)) for i in range(k, n + 1)
    )


@dataclass(frozen=True)
class FirstPassageResult:
    target_hits: int
    stop_hits: int
    resolved: int
    observed_rate: float | None
    baseline_rate: float
    edge_pp: float | None
    p_value: float | None
    beats_baseline: bool


def assess_first_passage(
    *,
    target_hits: int,
    stop_hits: int,
    stop_pct: float,
    target_pct: float,
    alpha: float = DEFAULT_ALPHA,
) -> FirstPassageResult:
    """Compare the realised target-before-stop rate against a random walk."""
    if target_hits < 0 or stop_hits < 0:
        raise ValueError("hit counts must be non-negative")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")

    baseline = first_passage_baseline(stop_pct=stop_pct, target_pct=target_pct)
    resolved = target_hits + stop_hits
    if resolved == 0:
        return FirstPassageResult(
            target_hits=target_hits,
            stop_hits=stop_hits,
            resolved=0,
            observed_rate=None,
            baseline_rate=baseline,
            edge_pp=None,
            p_value=None,
            beats_baseline=False,
        )

    observed = target_hits / resolved
    p_value = binomial_p_upper(target_hits, resolved, baseline)
    return FirstPassageResult(
        target_hits=target_hits,
        stop_hits=stop_hits,
        resolved=resolved,
        observed_rate=observed,
        baseline_rate=baseline,
        edge_pp=(observed - baseline) * 100.0,
        p_value=p_value,
        beats_baseline=observed > baseline and p_value < alpha,
    )


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _stdev(values: Sequence[float]) -> float:
    """Sample standard deviation; requires at least two observations."""
    n = len(values)
    mean = _mean(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))


def _t_statistic(values: Sequence[float]) -> float | None:
    if len(values) < 2:
        return None
    sd = _stdev(values)
    if sd <= 0:
        return None
    return _mean(values) / (sd / math.sqrt(len(values)))


@dataclass(frozen=True)
class ClusteredTTestResult:
    observations: int
    distinct_days: int
    naive_t: float | None
    clustered_t: float | None
    naive_mean: float | None
    day_mean: float | None
    inflation_factor: float | None
    significant: bool


def clustered_t_test(
    observations: Sequence[tuple[date, float]],
    *,
    t_critical: float = DEFAULT_T_CRITICAL,
) -> ClusteredTTestResult:
    """Test whether returns are positive once same-day dependence is removed.

    ``observations`` pairs each return with the calendar day it belongs to.
    Returns are averaged within a day so that a day contributes exactly one
    observation regardless of how many positions were open that day.
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
            inflation_factor=None,
            significant=False,
        )

    by_day: dict[date, list[float]] = {}
    for day, value in observations:
        by_day.setdefault(day, []).append(value)
    day_means = [_mean(v) for v in by_day.values()]

    naive_t = _t_statistic(values)
    clustered_t = _t_statistic(day_means)
    inflation = (
        math.sqrt(len(values) / len(day_means)) if day_means else None
    )
    return ClusteredTTestResult(
        observations=len(values),
        distinct_days=len(day_means),
        naive_t=naive_t,
        clustered_t=clustered_t,
        naive_mean=_mean(values),
        day_mean=_mean(day_means),
        inflation_factor=inflation,
        significant=clustered_t is not None and clustered_t > t_critical,
    )


@dataclass(frozen=True)
class SignalEdgeVerdict:
    verdict: SignalEdgeVerdictLabel
    reasons: tuple[str, ...]
    first_passage: FirstPassageResult
    clustered: ClusteredTTestResult


def assess_signal_edge(
    *,
    first_passage: FirstPassageResult,
    clustered: ClusteredTTestResult,
    min_resolved_trades: int = DEFAULT_MIN_RESOLVED_TRADES,
    min_distinct_days: int = DEFAULT_MIN_DISTINCT_DAYS,
) -> SignalEdgeVerdict:
    """Combine both checks into one promotion-facing verdict.

    Insufficient evidence is reported as its own verdict rather than as a
    failure: "not yet provable" and "proven absent" call for different actions,
    and collapsing them would let a thin sample masquerade as a rejection.
    """
    if min_resolved_trades < 1 or min_distinct_days < 1:
        raise ValueError("evidence floors must be positive")

    reasons: list[str] = []
    if first_passage.resolved < min_resolved_trades:
        reasons.append(
            f"only {first_passage.resolved} bracket-resolved trades "
            f"(need {min_resolved_trades})"
        )
    if clustered.distinct_days < min_distinct_days:
        reasons.append(
            f"only {clustered.distinct_days} distinct trading days "
            f"(need {min_distinct_days})"
        )
    if reasons:
        return SignalEdgeVerdict(
            verdict=VERDICT_INSUFFICIENT_DATA,
            reasons=tuple(reasons),
            first_passage=first_passage,
            clustered=clustered,
        )

    if not first_passage.beats_baseline:
        observed = first_passage.observed_rate
        reasons.append(
            "first-passage rate "
            f"{observed * 100:.1f}% does not beat the random-walk baseline "
            f"{first_passage.baseline_rate * 100:.1f}%"
            if observed is not None
            else "first-passage rate unavailable"
        )
    if not clustered.significant:
        t = clustered.clustered_t
        reasons.append(
            f"cluster-robust t={t:.2f} is not significant"
            if t is not None
            else "cluster-robust t unavailable"
        )

    return SignalEdgeVerdict(
        verdict=VERDICT_FAIL if reasons else VERDICT_PASS,
        reasons=tuple(reasons),
        first_passage=first_passage,
        clustered=clustered,
    )


__all__ = [
    "ClusteredTTestResult",
    "DEFAULT_ALPHA",
    "DEFAULT_MIN_DISTINCT_DAYS",
    "DEFAULT_MIN_RESOLVED_TRADES",
    "DEFAULT_T_CRITICAL",
    "FirstPassageResult",
    "SignalEdgeVerdict",
    "SignalEdgeVerdictLabel",
    "VERDICT_FAIL",
    "VERDICT_INSUFFICIENT_DATA",
    "VERDICT_PASS",
    "assess_first_passage",
    "assess_signal_edge",
    "binomial_p_upper",
    "clustered_t_test",
    "first_passage_baseline",
]
