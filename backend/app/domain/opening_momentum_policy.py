from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from datetime import date
from statistics import fmean, median
from typing import Literal, Sequence

from app.domain.opening_momentum import OpeningMomentumConfig
from app.domain.opening_momentum_comparison import (
    OpeningMomentumPairedComparison,
    compare_opening_momentum_variants,
)


OPENING_POLICY_DIAGNOSTIC_VERSION = (
    "opening-policy-chronological-holdout-v2"
)
OPENING_POLICY_COHORT_DIAGNOSTIC_VERSION = (
    "opening-policy-cohort-paired-baseline-anchored-holdout-v2"
)
OPENING_POLICY_HORIZON_DIAGNOSTIC_VERSION = (
    "opening-policy-horizon-paired-baseline-anchored-holdout-v2"
)
PRODUCTION_POLICY_NAME = "WEAK_BREADTH_PATH_CHALLENGER"
PRODUCTION_MINIMUM_PATH_EFFICIENCY = 0.70
PRODUCTION_MAXIMUM_MARKET_RETURN_BPS = 0.0
EXCEPTIONAL_PATH_POLICY_NAME = (
    "WEAK_BREADTH_EXCEPTIONAL_PATH_CHALLENGER"
)
EXCEPTIONAL_MINIMUM_PATH_EFFICIENCY = 0.90
EXCEPTIONAL_MAXIMUM_MARKET_RETURN_BPS = 5.0

OpeningPolicySliceName = Literal["ALL", "DISCOVERY", "HOLDOUT"]


def opening_execution_config(
    base: OpeningMomentumConfig | None = None,
) -> OpeningMomentumConfig:
    """Return the frozen broad execution configuration used in production."""

    return replace(
        base or OpeningMomentumConfig(),
        signal_minutes=3,
        execution_delay_minutes=1,
        holding_minutes=60,
        minimum_market_return_bps=-50.0,
        minimum_candidate_return_bps=50.0,
        minimum_excess_return_bps=25.0,
        stop_loss_pct=1.0,
    )


@dataclass(frozen=True)
class OpeningPolicySpec:
    name: str
    minimum_path_efficiency: float | None = None
    maximum_market_return_bps: float | None = None
    exceptional_minimum_path_efficiency: float | None = None
    exceptional_maximum_market_return_bps: float | None = None

    def __post_init__(self) -> None:
        normalized = self.name.strip().upper()
        if not normalized:
            raise ValueError("policy name is required")
        object.__setattr__(self, "name", normalized)
        if self.minimum_path_efficiency is not None and (
            not math.isfinite(self.minimum_path_efficiency)
            or not 0 <= self.minimum_path_efficiency <= 1
        ):
            raise ValueError(
                "minimum_path_efficiency must be in [0, 1] when set"
            )
        if self.maximum_market_return_bps is not None and not math.isfinite(
            self.maximum_market_return_bps
        ):
            raise ValueError(
                "maximum_market_return_bps must be finite when set"
            )
        exceptional_pair = (
            self.exceptional_minimum_path_efficiency,
            self.exceptional_maximum_market_return_bps,
        )
        if (exceptional_pair[0] is None) != (exceptional_pair[1] is None):
            raise ValueError(
                "exceptional path and market thresholds must be set together"
            )
        if self.exceptional_minimum_path_efficiency is not None:
            if (
                not math.isfinite(
                    self.exceptional_minimum_path_efficiency
                )
                or not 0
                <= self.exceptional_minimum_path_efficiency
                <= 1
            ):
                raise ValueError(
                    "exceptional_minimum_path_efficiency must be in [0, 1]"
                )
            if (
                self.minimum_path_efficiency is not None
                and self.exceptional_minimum_path_efficiency
                < self.minimum_path_efficiency
            ):
                raise ValueError(
                    "exceptional path threshold must not be below the base "
                    "path threshold"
                )
        if self.exceptional_maximum_market_return_bps is not None:
            if not math.isfinite(
                self.exceptional_maximum_market_return_bps
            ):
                raise ValueError(
                    "exceptional_maximum_market_return_bps must be finite"
                )
            if self.maximum_market_return_bps is None:
                raise ValueError(
                    "exceptional market threshold requires a base maximum"
                )
            if (
                self.exceptional_maximum_market_return_bps
                < self.maximum_market_return_bps
            ):
                raise ValueError(
                    "exceptional market threshold must not be below the "
                    "base maximum"
                )


@dataclass(frozen=True)
class OpeningPolicySession:
    session_date: date
    baseline_signal: bool
    gross_return_bps: float
    market_return_bps: float | None
    candidate_path_efficiency: float | None
    candidate_symbol: str | None = None
    stop_triggered: bool = False

    def __post_init__(self) -> None:
        if not math.isfinite(self.gross_return_bps):
            raise ValueError("gross_return_bps must be finite")
        if self.market_return_bps is not None and not math.isfinite(
            self.market_return_bps
        ):
            raise ValueError("market_return_bps must be finite when set")
        if self.candidate_path_efficiency is not None and (
            not math.isfinite(self.candidate_path_efficiency)
            or not 0 <= self.candidate_path_efficiency <= 1
        ):
            raise ValueError(
                "candidate_path_efficiency must be in [0, 1] when set"
            )
        symbol = (
            self.candidate_symbol.strip().upper()
            if self.candidate_symbol is not None
            else None
        )
        if symbol == "":
            raise ValueError("candidate_symbol must not be empty when set")
        object.__setattr__(self, "candidate_symbol", symbol)
        if self.baseline_signal and (
            symbol is None
            or self.market_return_bps is None
            or self.candidate_path_efficiency is None
        ):
            raise ValueError(
                "baseline signals require candidate, market, and path data"
            )
        if not self.baseline_signal and self.gross_return_bps != 0:
            raise ValueError(
                "non-signal sessions must have zero gross return"
            )
        if self.stop_triggered and not self.baseline_signal:
            raise ValueError("only baseline signals can trigger a stop")


@dataclass(frozen=True)
class OpeningPolicyReturnMetrics:
    sessions: int
    entries: int
    stop_exits: int
    wins: int
    win_rate: float
    cumulative_return_bps: float
    mean_session_return_bps: float
    mean_trade_return_bps: float
    median_trade_return_bps: float
    max_drawdown_bps: float
    profit_factor: float | None
    cumulative_without_best_3_bps: float


@dataclass(frozen=True)
class OpeningPolicyDisplacement:
    baseline_signal_sessions: int
    accepted_signal_sessions: int
    displaced_signal_sessions: int
    avoided_losing_signals: int
    avoided_winning_signals: int
    avoided_flat_signals: int
    cumulative_delta_bps: float
    mean_delta_bps: float
    outperformance_rate: float


@dataclass(frozen=True)
class OpeningPolicySlice:
    name: OpeningPolicySliceName
    start_date: date | None
    end_date: date | None
    metrics: OpeningPolicyReturnMetrics
    comparison_to_baseline: OpeningMomentumPairedComparison
    displacement: OpeningPolicyDisplacement


@dataclass(frozen=True)
class OpeningPolicyResult:
    policy: OpeningPolicySpec
    slices: tuple[OpeningPolicySlice, ...]


@dataclass(frozen=True)
class OpeningPolicyDiagnosticReport:
    algorithm_version: str
    discovery_ratio: float
    round_trip_cost_bps: float
    source_sessions: int
    discovery_sessions: int
    holdout_sessions: int
    discovery_end_date: date
    baseline_policy_name: str
    production_policy_name: str
    policies: tuple[OpeningPolicyResult, ...]
    automatic_promotion_allowed: Literal[False]

    def to_dict(self) -> dict[str, object]:
        return {
            str(key): _json_safe(value)
            for key, value in asdict(self).items()
        }


@dataclass(frozen=True)
class OpeningPolicyCohortSlice:
    name: OpeningPolicySliceName
    start_date: date | None
    end_date: date | None
    resolved_sessions: int
    candidate_displacement_sessions: int
    execution_displacement_sessions: int
    baseline_only_entry_sessions: int
    cohort_only_entry_sessions: int
    cohort_symbol_entry_sessions: int
    baseline: OpeningPolicyReturnMetrics
    cohort: OpeningPolicyReturnMetrics
    comparison: OpeningMomentumPairedComparison
    displacements: tuple[OpeningPolicyCohortDisplacement, ...]
    tail_robustness_available: bool
    tail_robustness_passed: bool

    def to_dict(self) -> dict[str, object]:
        return {
            str(key): _json_safe(value)
            for key, value in asdict(self).items()
        }


@dataclass(frozen=True)
class OpeningPolicyCohortDisplacement:
    session_date: date
    baseline_candidate_symbol: str | None
    cohort_candidate_symbol: str | None
    baseline_entered: bool
    cohort_entered: bool
    baseline_return_bps: float
    cohort_return_bps: float
    delta_bps: float


@dataclass(frozen=True)
class OpeningPolicyCohortReport:
    algorithm_version: str
    policy: OpeningPolicySpec
    discovery_ratio: float
    round_trip_cost_bps: float
    baseline_source_sessions: int
    cohort_source_sessions: int
    paired_sessions: int
    discovery_sessions: int
    holdout_sessions: int
    discovery_end_date: date
    cohort_symbols: tuple[str, ...]
    slices: tuple[OpeningPolicyCohortSlice, ...]
    diagnostic_only: Literal[True]
    automatic_promotion_allowed: Literal[False]

    def to_dict(self) -> dict[str, object]:
        return {
            str(key): _json_safe(value)
            for key, value in asdict(self).items()
        }


@dataclass(frozen=True)
class OpeningPolicyHorizonDelta:
    session_date: date
    candidate_symbol: str | None
    baseline_return_bps: float
    challenger_return_bps: float
    delta_bps: float
    baseline_stop_triggered: bool
    challenger_stop_triggered: bool


@dataclass(frozen=True)
class OpeningPolicyHorizonSlice:
    name: OpeningPolicySliceName
    start_date: date | None
    end_date: date | None
    resolved_sessions: int
    changed_return_sessions: int
    baseline: OpeningPolicyReturnMetrics
    challenger: OpeningPolicyReturnMetrics
    comparison: OpeningMomentumPairedComparison
    deltas: tuple[OpeningPolicyHorizonDelta, ...]
    tail_robustness_available: bool
    tail_robustness_passed: bool

    def to_dict(self) -> dict[str, object]:
        return {
            str(key): _json_safe(value)
            for key, value in asdict(self).items()
        }


@dataclass(frozen=True)
class OpeningPolicyHorizonResult:
    holding_minutes: int
    slices: tuple[OpeningPolicyHorizonSlice, ...]


@dataclass(frozen=True)
class OpeningPolicyHorizonSource:
    holding_minutes: int
    source_sessions: int


@dataclass(frozen=True)
class OpeningPolicyHorizonReport:
    algorithm_version: str
    policy: OpeningPolicySpec
    discovery_ratio: float
    round_trip_cost_bps: float
    baseline_holding_minutes: int
    sources: tuple[OpeningPolicyHorizonSource, ...]
    paired_sessions: int
    discovery_sessions: int
    holdout_sessions: int
    discovery_end_date: date
    results: tuple[OpeningPolicyHorizonResult, ...]
    diagnostic_only: Literal[True]
    automatic_promotion_allowed: Literal[False]

    def to_dict(self) -> dict[str, object]:
        return {
            str(key): _json_safe(value)
            for key, value in asdict(self).items()
        }


def evaluate_opening_policy_grid(
    sessions: Sequence[OpeningPolicySession],
    *,
    policies: Sequence[OpeningPolicySpec],
    round_trip_cost_bps: float,
    baseline_policy_name: str = "BROAD",
    production_policy_name: str = PRODUCTION_POLICY_NAME,
    discovery_ratio: float = 0.60,
) -> OpeningPolicyDiagnosticReport:
    """Evaluate frozen post-signal gates on chronological paired sessions."""

    if not math.isfinite(round_trip_cost_bps) or round_trip_cost_bps < 0:
        raise ValueError("round_trip_cost_bps must be finite and non-negative")
    if not 0 < discovery_ratio < 1:
        raise ValueError("discovery_ratio must be in (0, 1)")
    ordered_sessions = tuple(sorted(
        sessions,
        key=lambda value: value.session_date,
    ))
    if len(ordered_sessions) < 2:
        raise ValueError("at least two sessions are required")
    if len({value.session_date for value in ordered_sessions}) != len(
        ordered_sessions
    ):
        raise ValueError("session dates must be unique")

    policy_specs = tuple(policies)
    if not policy_specs:
        raise ValueError("at least one policy is required")
    policy_by_name = {value.name: value for value in policy_specs}
    if len(policy_by_name) != len(policy_specs):
        raise ValueError("policy names must be unique")
    baseline_name = baseline_policy_name.strip().upper()
    production_name = production_policy_name.strip().upper()
    baseline = policy_by_name.get(baseline_name)
    if baseline is None:
        raise ValueError("baseline policy is missing")
    if (
        baseline.minimum_path_efficiency is not None
        or baseline.maximum_market_return_bps is not None
        or baseline.exceptional_minimum_path_efficiency is not None
        or baseline.exceptional_maximum_market_return_bps is not None
    ):
        raise ValueError("baseline policy must not define post-signal gates")
    if production_name not in policy_by_name:
        raise ValueError("production policy is missing")

    discovery_end_date, discovery_dates, _ = (
        _baseline_anchored_split(
            tuple(value.session_date for value in ordered_sessions),
            tuple(value.session_date for value in ordered_sessions),
            discovery_ratio=discovery_ratio,
            comparison_name="policy",
        )
    )
    discovery_sessions = ordered_sessions[:len(discovery_dates)]
    holdout_sessions = ordered_sessions[len(discovery_dates):]
    slice_specs: tuple[
        tuple[OpeningPolicySliceName, tuple[OpeningPolicySession, ...]], ...
    ] = (
        ("ALL", ordered_sessions),
        ("DISCOVERY", discovery_sessions),
        ("HOLDOUT", holdout_sessions),
    )
    results = tuple(
        OpeningPolicyResult(
            policy=policy,
            slices=tuple(
                _evaluation_slice(
                    name,
                    values,
                    policy=policy,
                    round_trip_cost_bps=round_trip_cost_bps,
                )
                for name, values in slice_specs
            ),
        )
        for policy in policy_specs
    )
    return OpeningPolicyDiagnosticReport(
        algorithm_version=OPENING_POLICY_DIAGNOSTIC_VERSION,
        discovery_ratio=discovery_ratio,
        round_trip_cost_bps=round_trip_cost_bps,
        source_sessions=len(ordered_sessions),
        discovery_sessions=len(discovery_sessions),
        holdout_sessions=len(holdout_sessions),
        discovery_end_date=discovery_end_date,
        baseline_policy_name=baseline_name,
        production_policy_name=production_name,
        policies=results,
        automatic_promotion_allowed=False,
    )


def evaluate_opening_policy_cohort(
    baseline_sessions: Sequence[OpeningPolicySession],
    cohort_sessions: Sequence[OpeningPolicySession],
    *,
    policy: OpeningPolicySpec,
    cohort_symbols: Sequence[str],
    round_trip_cost_bps: float,
    discovery_ratio: float = 0.60,
) -> OpeningPolicyCohortReport:
    """Pair two frozen universes under the same post-signal policy.

    Only dates resolved by both universes enter the comparison. The baseline
    date series freezes the chronological boundary before unpaired dates are
    removed, so candidate data gaps cannot move holdout observations into the
    discovery slice. The result remains diagnostic-only even when historical
    metrics appear favorable.
    """

    if not math.isfinite(round_trip_cost_bps) or round_trip_cost_bps < 0:
        raise ValueError("round_trip_cost_bps must be finite and non-negative")
    if not 0 < discovery_ratio < 1:
        raise ValueError("discovery_ratio must be in (0, 1)")
    normalized_cohort = tuple(dict.fromkeys(
        value.strip().upper() for value in cohort_symbols
    ))
    if not normalized_cohort or any(not value for value in normalized_cohort):
        raise ValueError("cohort_symbols must contain non-empty symbols")

    baseline_by_date = _sessions_by_date(
        baseline_sessions,
        field_name="baseline_sessions",
    )
    cohort_by_date = _sessions_by_date(
        cohort_sessions,
        field_name="cohort_sessions",
    )
    paired_dates = tuple(sorted(
        set(baseline_by_date).intersection(cohort_by_date)
    ))
    if len(paired_dates) < 2:
        raise ValueError("at least two paired cohort sessions are required")

    discovery_end_date, discovery_dates, holdout_dates = (
        _baseline_anchored_split(
            tuple(sorted(baseline_by_date)),
            paired_dates,
            discovery_ratio=discovery_ratio,
            comparison_name="cohort",
        )
    )
    paired_by_date = {
        value: (baseline_by_date[value], cohort_by_date[value])
        for value in paired_dates
    }
    paired = tuple(
        (baseline_by_date[value], cohort_by_date[value])
        for value in paired_dates
    )
    slice_specs: tuple[
        tuple[
            OpeningPolicySliceName,
            tuple[tuple[OpeningPolicySession, OpeningPolicySession], ...],
        ],
        ...,
    ] = (
        ("ALL", paired),
        (
            "DISCOVERY",
            tuple(paired_by_date[value] for value in discovery_dates),
        ),
        (
            "HOLDOUT",
            tuple(paired_by_date[value] for value in holdout_dates),
        ),
    )
    return OpeningPolicyCohortReport(
        algorithm_version=OPENING_POLICY_COHORT_DIAGNOSTIC_VERSION,
        policy=policy,
        discovery_ratio=discovery_ratio,
        round_trip_cost_bps=round_trip_cost_bps,
        baseline_source_sessions=len(baseline_by_date),
        cohort_source_sessions=len(cohort_by_date),
        paired_sessions=len(paired_dates),
        discovery_sessions=len(discovery_dates),
        holdout_sessions=len(holdout_dates),
        discovery_end_date=discovery_end_date,
        cohort_symbols=normalized_cohort,
        slices=tuple(
            _cohort_evaluation_slice(
                name,
                values,
                policy=policy,
                cohort_symbols=set(normalized_cohort),
                round_trip_cost_bps=round_trip_cost_bps,
            )
            for name, values in slice_specs
        ),
        diagnostic_only=True,
        automatic_promotion_allowed=False,
    )


def evaluate_opening_policy_horizons(
    sessions_by_holding_minutes: Mapping[
        int,
        Sequence[OpeningPolicySession],
    ],
    *,
    baseline_holding_minutes: int,
    policy: OpeningPolicySpec,
    round_trip_cost_bps: float,
    discovery_ratio: float = 0.60,
) -> OpeningPolicyHorizonReport:
    """Compare fixed exits under one baseline-anchored chronological split."""

    if not math.isfinite(round_trip_cost_bps) or round_trip_cost_bps < 0:
        raise ValueError("round_trip_cost_bps must be finite and non-negative")
    if not 0 < discovery_ratio < 1:
        raise ValueError("discovery_ratio must be in (0, 1)")
    if baseline_holding_minutes <= 0:
        raise ValueError("baseline_holding_minutes must be positive")
    if len(sessions_by_holding_minutes) < 2:
        raise ValueError("at least two holding horizons are required")

    sessions_by_horizon: dict[
        int,
        dict[date, OpeningPolicySession],
    ] = {}
    for holding_minutes, sessions in sessions_by_holding_minutes.items():
        if (
            isinstance(holding_minutes, bool)
            or not isinstance(holding_minutes, int)
            or holding_minutes <= 0
        ):
            raise ValueError("holding horizons must be positive integers")
        sessions_by_horizon[holding_minutes] = _sessions_by_date(
            sessions,
            field_name=f"sessions_by_holding_minutes[{holding_minutes}]",
        )
    baseline_by_date = sessions_by_horizon.get(
        baseline_holding_minutes
    )
    if baseline_by_date is None:
        raise ValueError("baseline holding horizon is missing")

    paired_dates = tuple(sorted(set.intersection(
        *(set(values) for values in sessions_by_horizon.values())
    )))
    if len(paired_dates) < 2:
        raise ValueError("at least two paired horizon sessions are required")

    for holding_minutes, sessions in sessions_by_horizon.items():
        if holding_minutes == baseline_holding_minutes:
            continue
        for session_date in paired_dates:
            _validate_same_horizon_decision(
                baseline_by_date[session_date],
                sessions[session_date],
                holding_minutes=holding_minutes,
            )

    discovery_end_date, discovery_dates, holdout_dates = (
        _baseline_anchored_split(
            tuple(sorted(baseline_by_date)),
            paired_dates,
            discovery_ratio=discovery_ratio,
            comparison_name="holding horizon",
        )
    )
    slice_specs: tuple[
        tuple[OpeningPolicySliceName, tuple[date, ...]], ...
    ] = (
        ("ALL", paired_dates),
        ("DISCOVERY", discovery_dates),
        ("HOLDOUT", holdout_dates),
    )
    return OpeningPolicyHorizonReport(
        algorithm_version=OPENING_POLICY_HORIZON_DIAGNOSTIC_VERSION,
        policy=policy,
        discovery_ratio=discovery_ratio,
        round_trip_cost_bps=round_trip_cost_bps,
        baseline_holding_minutes=baseline_holding_minutes,
        sources=tuple(
            OpeningPolicyHorizonSource(
                holding_minutes=holding_minutes,
                source_sessions=len(sessions),
            )
            for holding_minutes, sessions in sorted(
                sessions_by_horizon.items()
            )
        ),
        paired_sessions=len(paired_dates),
        discovery_sessions=len(discovery_dates),
        holdout_sessions=len(holdout_dates),
        discovery_end_date=discovery_end_date,
        results=tuple(
            OpeningPolicyHorizonResult(
                holding_minutes=holding_minutes,
                slices=tuple(
                    _horizon_evaluation_slice(
                        name,
                        dates,
                        baseline_by_date=baseline_by_date,
                        challenger_by_date=sessions,
                        policy=policy,
                        round_trip_cost_bps=round_trip_cost_bps,
                    )
                    for name, dates in slice_specs
                ),
            )
            for holding_minutes, sessions in sorted(
                sessions_by_horizon.items()
            )
            if holding_minutes != baseline_holding_minutes
        ),
        diagnostic_only=True,
        automatic_promotion_allowed=False,
    )


def _sessions_by_date(
    sessions: Sequence[OpeningPolicySession],
    *,
    field_name: str,
) -> dict[date, OpeningPolicySession]:
    result: dict[date, OpeningPolicySession] = {}
    for value in sessions:
        if value.session_date in result:
            raise ValueError(f"{field_name} dates must be unique")
        result[value.session_date] = value
    return result


def _baseline_anchored_split(
    baseline_dates: Sequence[date],
    paired_dates: Sequence[date],
    *,
    discovery_ratio: float,
    comparison_name: str,
) -> tuple[date, tuple[date, ...], tuple[date, ...]]:
    """Freeze the split on the baseline before dropping unpaired dates."""

    ordered_baseline = tuple(sorted(baseline_dates))
    if len(ordered_baseline) < 2:
        raise ValueError("at least two baseline sessions are required")
    split_index = max(
        1,
        min(
            len(ordered_baseline) - 1,
            math.floor(len(ordered_baseline) * discovery_ratio),
        ),
    )
    discovery_end_date = ordered_baseline[split_index - 1]
    ordered_paired = tuple(sorted(paired_dates))
    discovery_dates = tuple(
        value
        for value in ordered_paired
        if value <= discovery_end_date
    )
    holdout_dates = tuple(
        value
        for value in ordered_paired
        if value > discovery_end_date
    )
    if not discovery_dates or not holdout_dates:
        raise ValueError(
            f"paired {comparison_name} sessions must cover both the "
            "baseline discovery and holdout periods"
        )
    return discovery_end_date, discovery_dates, holdout_dates


def _validate_same_horizon_decision(
    baseline: OpeningPolicySession,
    challenger: OpeningPolicySession,
    *,
    holding_minutes: int,
) -> None:
    if (
        baseline.baseline_signal != challenger.baseline_signal
        or baseline.candidate_symbol != challenger.candidate_symbol
        or not _optional_float_matches(
            baseline.market_return_bps,
            challenger.market_return_bps,
        )
        or not _optional_float_matches(
            baseline.candidate_path_efficiency,
            challenger.candidate_path_efficiency,
        )
    ):
        raise ValueError(
            "holding horizon changed the signal decision for "
            f"{baseline.session_date.isoformat()} at {holding_minutes}m"
        )


def _optional_float_matches(
    left: float | None,
    right: float | None,
) -> bool:
    if left is None or right is None:
        return left is right
    return math.isclose(left, right, rel_tol=1e-12, abs_tol=1e-12)


def _horizon_evaluation_slice(
    name: OpeningPolicySliceName,
    dates: Sequence[date],
    *,
    baseline_by_date: Mapping[date, OpeningPolicySession],
    challenger_by_date: Mapping[date, OpeningPolicySession],
    policy: OpeningPolicySpec,
    round_trip_cost_bps: float,
) -> OpeningPolicyHorizonSlice:
    baseline_sessions = tuple(baseline_by_date[value] for value in dates)
    challenger_sessions = tuple(
        challenger_by_date[value] for value in dates
    )
    accepted = tuple(
        _policy_accepts(policy, value) for value in baseline_sessions
    )
    challenger_accepted = tuple(
        _policy_accepts(policy, value) for value in challenger_sessions
    )
    if accepted != challenger_accepted:
        raise ValueError("holding horizon changed policy acceptance")

    baseline_returns = tuple(
        _baseline_net_return(
            value,
            round_trip_cost_bps=round_trip_cost_bps,
        )
        if is_accepted
        else 0.0
        for value, is_accepted in zip(
            baseline_sessions,
            accepted,
            strict=True,
        )
    )
    challenger_returns = tuple(
        _baseline_net_return(
            value,
            round_trip_cost_bps=round_trip_cost_bps,
        )
        if is_accepted
        else 0.0
        for value, is_accepted in zip(
            challenger_sessions,
            accepted,
            strict=True,
        )
    )
    baseline_stops = tuple(
        value.stop_triggered and is_accepted
        for value, is_accepted in zip(
            baseline_sessions,
            accepted,
            strict=True,
        )
    )
    challenger_stops = tuple(
        value.stop_triggered and is_accepted
        for value, is_accepted in zip(
            challenger_sessions,
            accepted,
            strict=True,
        )
    )
    baseline_metrics = _return_metrics(
        baseline_returns,
        accepted,
        baseline_stops,
    )
    challenger_metrics = _return_metrics(
        challenger_returns,
        accepted,
        challenger_stops,
    )
    deltas = tuple(
        OpeningPolicyHorizonDelta(
            session_date=baseline.session_date,
            candidate_symbol=baseline.candidate_symbol,
            baseline_return_bps=baseline_return,
            challenger_return_bps=challenger_return,
            delta_bps=challenger_return - baseline_return,
            baseline_stop_triggered=baseline_stop,
            challenger_stop_triggered=challenger_stop,
        )
        for (
            baseline,
            baseline_return,
            challenger_return,
            baseline_stop,
            challenger_stop,
        ) in zip(
            baseline_sessions,
            baseline_returns,
            challenger_returns,
            baseline_stops,
            challenger_stops,
            strict=True,
        )
        if (
            not math.isclose(
                baseline_return,
                challenger_return,
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
            or baseline_stop != challenger_stop
        )
    )
    tail_robustness_available = (
        baseline_metrics.entries >= 4
        and challenger_metrics.entries >= 4
    )
    return OpeningPolicyHorizonSlice(
        name=name,
        start_date=dates[0] if dates else None,
        end_date=dates[-1] if dates else None,
        resolved_sessions=len(dates),
        changed_return_sessions=len(deltas),
        baseline=baseline_metrics,
        challenger=challenger_metrics,
        comparison=compare_opening_momentum_variants(
            baseline_returns,
            challenger_returns,
        ),
        deltas=deltas,
        tail_robustness_available=tail_robustness_available,
        tail_robustness_passed=(
            tail_robustness_available
            and challenger_metrics.cumulative_without_best_3_bps
            >= baseline_metrics.cumulative_without_best_3_bps
        ),
    )


def _cohort_evaluation_slice(
    name: OpeningPolicySliceName,
    sessions: Sequence[
        tuple[OpeningPolicySession, OpeningPolicySession]
    ],
    *,
    policy: OpeningPolicySpec,
    cohort_symbols: set[str],
    round_trip_cost_bps: float,
) -> OpeningPolicyCohortSlice:
    baseline_accepted = tuple(
        _policy_accepts(policy, baseline)
        for baseline, _ in sessions
    )
    cohort_accepted = tuple(
        _policy_accepts(policy, cohort)
        for _, cohort in sessions
    )
    baseline_returns = tuple(
        _baseline_net_return(
            baseline,
            round_trip_cost_bps=round_trip_cost_bps,
        )
        if accepted
        else 0.0
        for (baseline, _), accepted in zip(
            sessions,
            baseline_accepted,
            strict=True,
        )
    )
    cohort_returns = tuple(
        _baseline_net_return(
            cohort,
            round_trip_cost_bps=round_trip_cost_bps,
        )
        if accepted
        else 0.0
        for (_, cohort), accepted in zip(
            sessions,
            cohort_accepted,
            strict=True,
        )
    )
    baseline_metrics = _return_metrics(
        baseline_returns,
        baseline_accepted,
        tuple(
            baseline.stop_triggered and accepted
            for (baseline, _), accepted in zip(
                sessions,
                baseline_accepted,
                strict=True,
            )
        ),
    )
    cohort_metrics = _return_metrics(
        cohort_returns,
        cohort_accepted,
        tuple(
            cohort.stop_triggered and accepted
            for (_, cohort), accepted in zip(
                sessions,
                cohort_accepted,
                strict=True,
            )
        ),
    )
    comparison = compare_opening_momentum_variants(
        baseline_returns,
        cohort_returns,
    )
    execution_displacements = tuple(
        OpeningPolicyCohortDisplacement(
            session_date=baseline.session_date,
            baseline_candidate_symbol=baseline.candidate_symbol,
            cohort_candidate_symbol=cohort.candidate_symbol,
            baseline_entered=baseline_entry,
            cohort_entered=cohort_entry,
            baseline_return_bps=baseline_return,
            cohort_return_bps=cohort_return,
            delta_bps=cohort_return - baseline_return,
        )
        for (
            baseline,
            cohort,
        ), baseline_entry, cohort_entry, baseline_return, cohort_return in zip(
            sessions,
            baseline_accepted,
            cohort_accepted,
            baseline_returns,
            cohort_returns,
            strict=True,
        )
        if (
            baseline_entry != cohort_entry
            or (
                baseline_entry
                and cohort_entry
                and baseline.candidate_symbol != cohort.candidate_symbol
            )
        )
    )
    tail_robustness_available = (
        baseline_metrics.entries >= 4
        and cohort_metrics.entries >= 4
    )
    return OpeningPolicyCohortSlice(
        name=name,
        start_date=(sessions[0][0].session_date if sessions else None),
        end_date=(sessions[-1][0].session_date if sessions else None),
        resolved_sessions=len(sessions),
        candidate_displacement_sessions=sum(
            baseline.candidate_symbol != cohort.candidate_symbol
            for baseline, cohort in sessions
        ),
        execution_displacement_sessions=len(execution_displacements),
        baseline_only_entry_sessions=sum(
            baseline_entry and not cohort_entry
            for baseline_entry, cohort_entry in zip(
                baseline_accepted,
                cohort_accepted,
                strict=True,
            )
        ),
        cohort_only_entry_sessions=sum(
            cohort_entry and not baseline_entry
            for baseline_entry, cohort_entry in zip(
                baseline_accepted,
                cohort_accepted,
                strict=True,
            )
        ),
        cohort_symbol_entry_sessions=sum(
            cohort_entry and cohort.candidate_symbol in cohort_symbols
            for (_, cohort), cohort_entry in zip(
                sessions,
                cohort_accepted,
                strict=True,
            )
        ),
        baseline=baseline_metrics,
        cohort=cohort_metrics,
        comparison=comparison,
        displacements=execution_displacements,
        tail_robustness_available=tail_robustness_available,
        tail_robustness_passed=(
            tail_robustness_available
            and cohort_metrics.cumulative_without_best_3_bps
            >= baseline_metrics.cumulative_without_best_3_bps
        ),
    )


def _evaluation_slice(
    name: OpeningPolicySliceName,
    sessions: Sequence[OpeningPolicySession],
    *,
    policy: OpeningPolicySpec,
    round_trip_cost_bps: float,
) -> OpeningPolicySlice:
    baseline_returns = tuple(
        _baseline_net_return(
            value,
            round_trip_cost_bps=round_trip_cost_bps,
        )
        for value in sessions
    )
    accepted = tuple(_policy_accepts(policy, value) for value in sessions)
    policy_returns = tuple(
        baseline_return if is_accepted else 0.0
        for baseline_return, is_accepted in zip(
            baseline_returns,
            accepted,
            strict=True,
        )
    )
    stop_exits = tuple(
        value.stop_triggered and is_accepted
        for value, is_accepted in zip(sessions, accepted, strict=True)
    )
    displaced_returns = tuple(
        baseline_return
        for value, baseline_return, is_accepted in zip(
            sessions,
            baseline_returns,
            accepted,
            strict=True,
        )
        if value.baseline_signal and not is_accepted
    )
    displacement_deltas = tuple(-value for value in displaced_returns)
    return OpeningPolicySlice(
        name=name,
        start_date=sessions[0].session_date if sessions else None,
        end_date=sessions[-1].session_date if sessions else None,
        metrics=_return_metrics(
            policy_returns,
            accepted,
            stop_exits,
        ),
        comparison_to_baseline=compare_opening_momentum_variants(
            baseline_returns,
            policy_returns,
        ),
        displacement=OpeningPolicyDisplacement(
            baseline_signal_sessions=sum(
                value.baseline_signal for value in sessions
            ),
            accepted_signal_sessions=sum(accepted),
            displaced_signal_sessions=len(displaced_returns),
            avoided_losing_signals=sum(
                value < 0 for value in displaced_returns
            ),
            avoided_winning_signals=sum(
                value > 0 for value in displaced_returns
            ),
            avoided_flat_signals=sum(
                value == 0 for value in displaced_returns
            ),
            cumulative_delta_bps=sum(displacement_deltas),
            mean_delta_bps=(
                fmean(displacement_deltas)
                if displacement_deltas
                else 0.0
            ),
            outperformance_rate=(
                sum(value > 0 for value in displacement_deltas)
                / len(displacement_deltas)
                if displacement_deltas
                else 0.0
            ),
        ),
    )


def _policy_accepts(
    policy: OpeningPolicySpec,
    session: OpeningPolicySession,
) -> bool:
    if not session.baseline_signal:
        return False
    if (
        policy.minimum_path_efficiency is not None
        and (
            session.candidate_path_efficiency is None
            or session.candidate_path_efficiency
            < policy.minimum_path_efficiency
        )
    ):
        return False
    maximum_market_return_bps = policy.maximum_market_return_bps
    if (
        policy.exceptional_minimum_path_efficiency is not None
        and policy.exceptional_maximum_market_return_bps is not None
        and session.candidate_path_efficiency is not None
        and session.candidate_path_efficiency
        >= policy.exceptional_minimum_path_efficiency
    ):
        maximum_market_return_bps = (
            policy.exceptional_maximum_market_return_bps
        )
    return not (
        maximum_market_return_bps is not None
        and (
            session.market_return_bps is None
            or session.market_return_bps > maximum_market_return_bps
        )
    )


def _baseline_net_return(
    session: OpeningPolicySession,
    *,
    round_trip_cost_bps: float,
) -> float:
    if not session.baseline_signal:
        return 0.0
    return session.gross_return_bps - round_trip_cost_bps


def _return_metrics(
    returns_bps: Sequence[float],
    entries: Sequence[bool],
    stop_exits: Sequence[bool],
) -> OpeningPolicyReturnMetrics:
    if not len(returns_bps) == len(entries) == len(stop_exits):
        raise ValueError("return, entry, and stop series must have equal length")
    returns = tuple(float(value) for value in returns_bps)
    trade_returns = tuple(
        value
        for value, entry in zip(returns, entries, strict=True)
        if entry
    )
    positive = sum(value for value in trade_returns if value > 0)
    negative = abs(sum(value for value in trade_returns if value < 0))
    best_three = sorted(trade_returns, reverse=True)[:3]
    return OpeningPolicyReturnMetrics(
        sessions=len(returns),
        entries=len(trade_returns),
        stop_exits=sum(stop_exits),
        wins=sum(value > 0 for value in trade_returns),
        win_rate=(
            sum(value > 0 for value in trade_returns) / len(trade_returns)
            if trade_returns
            else 0.0
        ),
        cumulative_return_bps=sum(returns),
        mean_session_return_bps=fmean(returns) if returns else 0.0,
        mean_trade_return_bps=(
            fmean(trade_returns) if trade_returns else 0.0
        ),
        median_trade_return_bps=(
            median(trade_returns) if trade_returns else 0.0
        ),
        max_drawdown_bps=_max_drawdown_bps(returns),
        profit_factor=(positive / negative if negative > 0 else None),
        cumulative_without_best_3_bps=(
            sum(returns) - sum(best_three)
        ),
    )


def _max_drawdown_bps(returns_bps: Sequence[float]) -> float:
    cumulative = 0.0
    peak = 0.0
    maximum = 0.0
    for value in returns_bps:
        cumulative += value
        peak = max(peak, cumulative)
        maximum = max(maximum, peak - cumulative)
    return maximum


def _json_safe(value: object) -> object:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {
            str(key): _json_safe(item)
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value
