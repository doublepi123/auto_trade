from __future__ import annotations

import math
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
    "opening-policy-chronological-holdout-v1"
)
PRODUCTION_POLICY_NAME = "WEAK_BREADTH_PATH_CHALLENGER"
PRODUCTION_MINIMUM_PATH_EFFICIENCY = 0.70
PRODUCTION_MAXIMUM_MARKET_RETURN_BPS = 0.0

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
    baseline_policy_name: str
    production_policy_name: str
    policies: tuple[OpeningPolicyResult, ...]
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
    ):
        raise ValueError("baseline policy must not define post-signal gates")
    if production_name not in policy_by_name:
        raise ValueError("production policy is missing")

    split_index = max(
        1,
        min(
            len(ordered_sessions) - 1,
            math.floor(len(ordered_sessions) * discovery_ratio),
        ),
    )
    slice_specs: tuple[
        tuple[OpeningPolicySliceName, tuple[OpeningPolicySession, ...]], ...
    ] = (
        ("ALL", ordered_sessions),
        ("DISCOVERY", ordered_sessions[:split_index]),
        ("HOLDOUT", ordered_sessions[split_index:]),
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
        discovery_sessions=split_index,
        holdout_sessions=len(ordered_sessions) - split_index,
        baseline_policy_name=baseline_name,
        production_policy_name=production_name,
        policies=results,
        automatic_promotion_allowed=False,
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
    return not (
        policy.maximum_market_return_bps is not None
        and (
            session.market_return_bps is None
            or session.market_return_bps
            > policy.maximum_market_return_bps
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
