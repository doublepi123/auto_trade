"""Pure signal-edge evidence before exit tuning.

Combines exact barrier first-passage with trade-weighted, day-clustered gross
and net significance. No I/O, database, or broker access.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final, Literal

from app.domain.strategy_v2.clustered_returns import (
    DEFAULT_T_CRITICAL,
    ClusteredTTestResult,
    clustered_t_test,
    day_cluster_t_critical,
)
from app.domain.strategy_v2.futility import (
    FUTILITY_BOUND_CRITICAL_VALUE,
    PREREGISTERED_COST_FLOOR_BPS,
    PREREGISTERED_POWER,
    PREREGISTERED_SIGMA_DAY_BPS,
    FutilityResult,
    FutilityStatusLabel,
    assess_futility,
    minimum_detectable_effect_bps,
)

DEFAULT_ALPHA = 0.05
# Minimum evidence before a verdict means anything. The day floor matters more
# than the trade floor: 200 trades spread over 5 days carry ~5 observations.
DEFAULT_MIN_RESOLVED_TRADES = 30
DEFAULT_MIN_DISTINCT_DAYS = 20
# PREREGISTRATION.md section 3: promotion floors, not analysis/reporting floors.
PROMOTION_MIN_DISTINCT_DAYS: Final = 60
PROMOTION_MIN_RESOLVED_BRACKETS: Final = 180

SignalEdgeVerdictLabel = Literal[
    "PASS",
    "FAIL",
    "FEE_BLOCKED",
    "INSUFFICIENT_DATA",
]

VERDICT_PASS: SignalEdgeVerdictLabel = "PASS"
VERDICT_FAIL: SignalEdgeVerdictLabel = "FAIL"
VERDICT_FEE_BLOCKED: SignalEdgeVerdictLabel = "FEE_BLOCKED"
VERDICT_INSUFFICIENT_DATA: SignalEdgeVerdictLabel = "INSUFFICIENT_DATA"


def first_passage_baseline(*, stop_pct: float, target_pct: float) -> float:
    """Return the driftless probability of reaching target before stop."""
    if not math.isfinite(stop_pct) or not math.isfinite(target_pct):
        raise ValueError("barrier distances must be finite")
    if stop_pct <= 0 or target_pct <= 0:
        raise ValueError("barrier distances must be positive")
    return stop_pct / (stop_pct + target_pct)


def binomial_p_upper(k: int, n: int, p: float) -> float:
    """Return exact one-sided ``P(X >= k)`` for ``X ~ Binomial(n, p)``."""
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


@dataclass(frozen=True, slots=True)
class FirstPassageResult:
    target_hits: int
    stop_hits: int
    resolved: int
    barrier_mismatch_excluded: int
    observed_rate: float | None
    baseline_rate: float
    edge_pp: float | None
    p_value: float | None
    beats_baseline: bool
    matched_versions: int = 0
    matched_trades: int = 0
    provenance_excluded_trades: int = 0
    missing_pnl_excluded: int = 0
    # The first-passage test conditions on price-barrier resolution, so trades
    # exiting on the TIME barrier leave the denominator. The statistic stays
    # valid under the driftless null (strong Markov property), but the reader
    # must be able to see how much evidence the conditioning removed, and
    # whether the verdict survives the extreme allocations of it.
    time_exit_excluded: int = 0
    time_exit_fraction: float | None = None
    observed_rate_floor: float | None = None
    observed_rate_ceiling: float | None = None


def assess_first_passage(
    *,
    target_hits: int,
    stop_hits: int,
    stop_pct: float,
    target_pct: float,
    alpha: float = DEFAULT_ALPHA,
    barrier_mismatch_excluded: int = 0,
    matched_versions: int = 0,
    matched_trades: int = 0,
    provenance_excluded_trades: int = 0,
    missing_pnl_excluded: int = 0,
    time_exit_excluded: int = 0,
) -> FirstPassageResult:
    """Compare the realised target-before-stop rate against a random walk."""
    counts = (
        target_hits,
        stop_hits,
        barrier_mismatch_excluded,
        matched_versions,
        matched_trades,
        provenance_excluded_trades,
        missing_pnl_excluded,
        time_exit_excluded,
    )
    if any(count < 0 for count in counts):
        raise ValueError("hit counts must be non-negative")
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")

    baseline = first_passage_baseline(stop_pct=stop_pct, target_pct=target_pct)
    resolved = target_hits + stop_hits
    cohort = resolved + time_exit_excluded
    result_fields = {
        "target_hits": target_hits,
        "stop_hits": stop_hits,
        "resolved": resolved,
        "barrier_mismatch_excluded": barrier_mismatch_excluded,
        "baseline_rate": baseline,
        "matched_versions": matched_versions,
        "matched_trades": matched_trades,
        "provenance_excluded_trades": provenance_excluded_trades,
        "missing_pnl_excluded": missing_pnl_excluded,
        "time_exit_excluded": time_exit_excluded,
        "time_exit_fraction": (
            time_exit_excluded / cohort if cohort > 0 else None
        ),
        # Sensitivity bounds: every time exit reallocated to STOP (floor) or to
        # TARGET (ceiling). They are reported alongside the conditional rate and
        # never feed the verdict.
        "observed_rate_floor": target_hits / cohort if cohort > 0 else None,
        "observed_rate_ceiling": (
            (target_hits + time_exit_excluded) / cohort if cohort > 0 else None
        ),
    }
    if resolved == 0:
        return FirstPassageResult(
            **result_fields,
            observed_rate=None,
            edge_pp=None,
            p_value=None,
            beats_baseline=False,
        )

    observed = target_hits / resolved
    p_value = binomial_p_upper(target_hits, resolved, baseline)
    return FirstPassageResult(
        **result_fields,
        observed_rate=observed,
        edge_pp=(observed - baseline) * 100.0,
        p_value=p_value,
        beats_baseline=observed > baseline and p_value < alpha,
    )


@dataclass(frozen=True)
class PromotionResult:
    net_significant: bool
    first_passage_beats_baseline: bool
    sample_size_met: bool
    deflated_sharpe_distinguishable: bool
    eligible: bool
    reasons: tuple[str, ...]
    distinct_days: int
    resolved_brackets: int
    required_distinct_days: int
    required_resolved_brackets: int


def assess_promotion(
    first_passage: FirstPassageResult,
    net: ClusteredTTestResult,
) -> PromotionResult:
    """Apply all four preregistered ANDs independently of reporting floors."""
    net_significant = (
        net.naive_mean is not None
        and net.clustered_standard_error is not None
        and net.naive_mean - day_cluster_t_critical(net.distinct_days) * net.clustered_standard_error > 0
    )
    first_passage_beats_baseline = (
        first_passage.observed_rate is not None
        and first_passage.p_value is not None
        and first_passage.observed_rate > first_passage.baseline_rate
        and first_passage.p_value < DEFAULT_ALPHA
    )
    sample_size_met = (
        net.distinct_days >= PROMOTION_MIN_DISTINCT_DAYS
        and first_passage.resolved >= PROMOTION_MIN_RESOLVED_BRACKETS
    )
    # Forward-cohort DSR is not computed. Future DSR must use T = distinct DAYS,
    # never trade count: correlated intraday trades understate Sharpe uncertainty.
    deflated_sharpe_distinguishable = False
    reasons: list[str] = []
    if not net_significant:
        reasons.append("AND #1: net day-clustered CI lower bound unavailable or not positive")
    if not first_passage_beats_baseline:
        reasons.append("AND #2: version-specific first-passage unavailable or does not beat baseline")
    if not sample_size_met:
        reasons.append(
            f"AND #3: {net.distinct_days} trading days (need {PROMOTION_MIN_DISTINCT_DAYS}), "
            + f"{first_passage.resolved} resolved brackets (need {PROMOTION_MIN_RESOLVED_BRACKETS})"
        )
    if not deflated_sharpe_distinguishable:
        reasons.append("AND #4: deflated Sharpe not yet computed for forward shadow cohort")
    return PromotionResult(
        net_significant=net_significant,
        first_passage_beats_baseline=first_passage_beats_baseline,
        sample_size_met=sample_size_met,
        deflated_sharpe_distinguishable=deflated_sharpe_distinguishable,
        eligible=all((net_significant, first_passage_beats_baseline,
                      sample_size_met, deflated_sharpe_distinguishable)),
        reasons=tuple(reasons),
        distinct_days=net.distinct_days,
        resolved_brackets=first_passage.resolved,
        required_distinct_days=PROMOTION_MIN_DISTINCT_DAYS,
        required_resolved_brackets=PROMOTION_MIN_RESOLVED_BRACKETS,
    )


@dataclass(frozen=True)
class SignalEdgeVerdict:
    verdict: SignalEdgeVerdictLabel
    reasons: tuple[str, ...]
    first_passage: FirstPassageResult
    gross: ClusteredTTestResult
    net: ClusteredTTestResult
    futility: FutilityResult
    promotion: PromotionResult

    @property
    def clustered(self) -> ClusteredTTestResult:
        """Return net evidence under the response field used before gross reporting."""
        return self.net


def assess_signal_edge(
    *,
    first_passage: FirstPassageResult,
    clustered: ClusteredTTestResult,
    gross: ClusteredTTestResult | None = None,
    min_resolved_trades: int = DEFAULT_MIN_RESOLVED_TRADES,
    min_distinct_days: int = DEFAULT_MIN_DISTINCT_DAYS,
) -> SignalEdgeVerdict:
    """Combine checks without collapsing insufficient evidence into failure."""
    if min_resolved_trades < 1 or min_distinct_days < 1:
        raise ValueError("evidence floors must be positive")

    gross_evidence = gross or clustered
    reasons: list[str] = []
    barrier_exclusion_reason: str | None = None
    cohort_diagnostic_reason: str | None = None
    if any((
        first_passage.matched_versions,
        first_passage.matched_trades,
        first_passage.provenance_excluded_trades,
        first_passage.missing_pnl_excluded,
    )):
        cohort_diagnostic_reason = (
            f"cohort matched_versions={first_passage.matched_versions}, "
            + f"matched_trades={first_passage.matched_trades}, "
            + "provenance_excluded_trades="
            + f"{first_passage.provenance_excluded_trades}, "
            + f"missing_pnl_excluded={first_passage.missing_pnl_excluded}"
        )
    if first_passage.barrier_mismatch_excluded > 0:
        trade_label = (
            "trade" if first_passage.barrier_mismatch_excluded == 1 else "trades"
        )
        barrier_exclusion_reason = (
            f"{first_passage.barrier_mismatch_excluded} bracket-resolved "
            + f"{trade_label} excluded because barrier provenance was unavailable "
            + "or did not match the tested barriers"
        )
    if first_passage.resolved < min_resolved_trades:
        reasons.append(
            f"only {first_passage.resolved} bracket-resolved trades "
            + f"(need {min_resolved_trades})"
        )
    for label, evidence in (("gross", gross_evidence), ("net", clustered)):
        if evidence.observations < min_resolved_trades:
            reasons.append(
                f"only {evidence.observations} paired {label} trades "
                + f"(need {min_resolved_trades})"
            )
        if evidence.distinct_days < min_distinct_days:
            reasons.append(
                f"only {evidence.distinct_days} {label} trading days "
                + f"(need {min_distinct_days})"
            )
    evidence_sufficient = not reasons
    futility = assess_futility(
        gross=gross_evidence,
        net=clustered,
        evidence_sufficient=evidence_sufficient,
    )
    promotion = assess_promotion(first_passage, clustered)
    if reasons:
        if barrier_exclusion_reason is not None:
            reasons.append(barrier_exclusion_reason)
        if cohort_diagnostic_reason is not None:
            reasons.append(cohort_diagnostic_reason)
        return SignalEdgeVerdict(
            verdict=VERDICT_INSUFFICIENT_DATA,
            reasons=tuple(reasons),
            first_passage=first_passage,
            gross=gross_evidence,
            net=clustered,
            futility=futility,
            promotion=promotion,
        )

    if not first_passage.beats_baseline:
        observed = first_passage.observed_rate
        reasons.append(
            "first-passage rate "
            +
            f"{observed * 100:.1f}% does not beat the random-walk baseline "
            +
            f"{first_passage.baseline_rate * 100:.1f}%"
            if observed is not None
            else "first-passage rate unavailable"
        )
    if not gross_evidence.significant:
        t = gross_evidence.clustered_t
        reasons.append(
            f"gross cluster-robust t={t:.2f} is not significant"
            if t is not None
            else "gross cluster-robust t unavailable"
        )

    if reasons:
        verdict = VERDICT_FAIL
    elif not clustered.significant:
        t = clustered.clustered_t
        reasons.append(
            f"net cluster-robust t={t:.2f} is not significant after fees"
            if t is not None
            else "net cluster-robust t unavailable after fees"
        )
        verdict = VERDICT_FEE_BLOCKED
    else:
        verdict = VERDICT_PASS

    if barrier_exclusion_reason is not None:
        reasons.append(barrier_exclusion_reason)
    if cohort_diagnostic_reason is not None:
        reasons.append(cohort_diagnostic_reason)

    return SignalEdgeVerdict(
        verdict=verdict,
        reasons=tuple(reasons),
        first_passage=first_passage,
        gross=gross_evidence,
        net=clustered,
        futility=futility,
        promotion=promotion,
    )


__all__ = [
    "ClusteredTTestResult", "DEFAULT_ALPHA", "DEFAULT_MIN_DISTINCT_DAYS",
    "DEFAULT_MIN_RESOLVED_TRADES", "DEFAULT_T_CRITICAL",
    "FUTILITY_BOUND_CRITICAL_VALUE", "FirstPassageResult", "FutilityResult",
    "FutilityStatusLabel", "PREREGISTERED_COST_FLOOR_BPS",
    "PREREGISTERED_POWER", "PREREGISTERED_SIGMA_DAY_BPS", "SignalEdgeVerdict",
    "SignalEdgeVerdictLabel", "VERDICT_FAIL", "VERDICT_FEE_BLOCKED",
    "VERDICT_INSUFFICIENT_DATA", "VERDICT_PASS", "assess_first_passage",
    "assess_futility", "assess_signal_edge", "binomial_p_upper",
    "clustered_t_test", "first_passage_baseline", "minimum_detectable_effect_bps",
]
