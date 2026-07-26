from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date
from statistics import fmean, median
from typing import Literal, Sequence

from app.domain.opening_momentum import (
    OpeningMomentumConfig,
    OpeningMomentumObservation,
    evaluate_opening_momentum,
)
from app.domain.opening_momentum_comparison import (
    OpeningMomentumPairedComparison,
    compare_opening_momentum_variants,
)


OPENING_EXTENSION_RESEARCH_VERSION = (
    "opening-extension-causal-holdout-stop-aware-v2"
)
_SliceName = Literal["ALL", "DISCOVERY", "HOLDOUT"]


@dataclass(frozen=True)
class OpeningExtensionExitPrice:
    symbol: str
    price: float
    stop_triggered: bool = False

    def __post_init__(self) -> None:
        normalized = self.symbol.strip().upper()
        if not normalized:
            raise ValueError("exit symbol is required")
        if not math.isfinite(self.price) or self.price <= 0:
            raise ValueError("exit price must be positive")
        object.__setattr__(self, "symbol", normalized)


@dataclass(frozen=True)
class OpeningExtensionSession:
    session_date: date
    observations: tuple[OpeningMomentumObservation, ...]
    exit_prices: tuple[OpeningExtensionExitPrice, ...]

    def __post_init__(self) -> None:
        observation_symbols = [item.symbol for item in self.observations]
        exit_symbols = [item.symbol for item in self.exit_prices]
        if len(observation_symbols) != len(set(observation_symbols)):
            raise ValueError("session observations must have unique symbols")
        if len(exit_symbols) != len(set(exit_symbols)):
            raise ValueError("session exit prices must have unique symbols")


@dataclass(frozen=True)
class OpeningExtensionReturnMetrics:
    sessions: int
    signals: int
    stop_exits: int
    wins: int
    win_rate: float
    cumulative_return_bps: float
    mean_return_bps: float
    median_return_bps: float
    max_drawdown_bps: float
    profit_factor: float | None
    cumulative_without_best_3_bps: float


@dataclass(frozen=True)
class OpeningExtensionSlice:
    name: _SliceName
    start_date: date | None
    end_date: date | None
    resolved_sessions: int
    displaced_baseline_sessions: int
    extension_signal_sessions: int
    baseline: OpeningExtensionReturnMetrics
    challenger: OpeningExtensionReturnMetrics
    comparison: OpeningMomentumPairedComparison


@dataclass(frozen=True)
class OpeningExtensionCostStress:
    round_trip_cost_bps: float
    holdout_sessions: int
    baseline_cumulative_return_bps: float
    challenger_cumulative_return_bps: float
    cumulative_delta_bps: float
    mean_delta_bps: float
    challenger_max_drawdown_bps: float
    challenger_without_best_3_bps: float


@dataclass(frozen=True)
class OpeningExtensionCandidateReport:
    symbol: str
    comparable_sessions: int
    displaced_baseline_sessions: int
    candidate_signal_sessions: int
    slices: tuple[OpeningExtensionSlice, ...]
    cost_stress: tuple[OpeningExtensionCostStress, ...]


@dataclass(frozen=True)
class OpeningExtensionResearchReport:
    algorithm_version: str
    opening_config_version: str
    discovery_ratio: float
    minimum_data_coverage: float
    stop_loss_pct: float | None
    baseline_symbols: tuple[str, ...]
    extension_symbols: tuple[str, ...]
    source_sessions: int
    discovery_sessions: int
    holdout_sessions: int
    candidates: tuple[OpeningExtensionCandidateReport, ...]
    survivorship_bias: Literal["CURRENT_BASELINE_SYMBOLS"]
    automatic_promotion_allowed: Literal[False]

    def to_dict(self) -> dict[str, object]:
        return {
            str(key): _json_safe(value)
            for key, value in asdict(self).items()
        }


@dataclass(frozen=True)
class _PolicyOutcome:
    session_date: date
    resolved: bool
    signal: bool
    candidate_symbol: str | None
    reason: str
    gross_return_bps: float
    stop_triggered: bool = False


def evaluate_opening_extension_candidates(
    sessions: Sequence[OpeningExtensionSession],
    *,
    baseline_symbols: Sequence[str],
    extension_symbols: Sequence[str],
    config: OpeningMomentumConfig,
    discovery_ratio: float = 0.60,
    minimum_data_coverage: float = 0.95,
    round_trip_cost_scenarios_bps: Sequence[float] = (
        14.0,
        20.0,
        30.0,
    ),
) -> OpeningExtensionResearchReport:
    """Evaluate single-symbol additions on frozen chronological sessions.

    Each extension is compared with the baseline only on dates where both
    policies are causally resolved. Legitimate gate skips count as zero-return
    policy sessions; missing signal, entry, or exit data are excluded from the
    paired comparison. The result is research-only and cannot promote a live
    strategy.
    """

    if not 0 < discovery_ratio < 1:
        raise ValueError("discovery_ratio must be in (0, 1)")
    if not 0 < minimum_data_coverage <= 1:
        raise ValueError("minimum_data_coverage must be in (0, 1]")

    baseline = _normalized_unique_symbols(
        baseline_symbols,
        field_name="baseline_symbols",
    )
    extensions = _normalized_unique_symbols(
        extension_symbols,
        field_name="extension_symbols",
    )
    if len(baseline) < config.minimum_universe_size:
        raise ValueError(
            "baseline_symbols must satisfy minimum_universe_size"
        )
    overlap = set(baseline).intersection(extensions)
    if overlap:
        raise ValueError(
            "extension symbols already exist in baseline: "
            + ", ".join(sorted(overlap))
        )
    costs = tuple(float(value) for value in round_trip_cost_scenarios_bps)
    if not costs:
        raise ValueError("at least one cost scenario is required")
    if any(not math.isfinite(value) or value < 0 for value in costs):
        raise ValueError("cost scenarios must be finite and non-negative")
    if len(costs) != len(set(costs)):
        raise ValueError("cost scenarios must be unique")

    ordered_sessions = tuple(sorted(
        sessions,
        key=lambda item: item.session_date,
    ))
    if len({item.session_date for item in ordered_sessions}) != len(
        ordered_sessions
    ):
        raise ValueError("session dates must be unique")
    if len(ordered_sessions) < 2:
        raise ValueError("at least two sessions are required")

    split_index = max(
        1,
        min(
            len(ordered_sessions) - 1,
            math.floor(len(ordered_sessions) * discovery_ratio),
        ),
    )
    discovery_dates = {
        item.session_date for item in ordered_sessions[:split_index]
    }
    holdout_dates = {
        item.session_date for item in ordered_sessions[split_index:]
    }
    baseline_outcomes = {
        session.session_date: _evaluate_policy(
            session,
            symbols=baseline,
            required_symbols=(),
            config=config,
            minimum_data_coverage=minimum_data_coverage,
        )
        for session in ordered_sessions
    }

    candidate_reports: list[OpeningExtensionCandidateReport] = []
    for extension_symbol in extensions:
        challenger_symbols = (*baseline, extension_symbol)
        challenger_outcomes = {
            session.session_date: _evaluate_policy(
                session,
                symbols=challenger_symbols,
                required_symbols=(extension_symbol,),
                config=config,
                minimum_data_coverage=minimum_data_coverage,
            )
            for session in ordered_sessions
        }
        comparable_dates = tuple(
            session.session_date
            for session in ordered_sessions
            if (
                baseline_outcomes[session.session_date].resolved
                and challenger_outcomes[session.session_date].resolved
            )
        )
        slice_specs: tuple[tuple[_SliceName, set[date]], ...] = (
            ("ALL", set(comparable_dates)),
            ("DISCOVERY", discovery_dates),
            ("HOLDOUT", holdout_dates),
        )
        slices = tuple(
            _evaluation_slice(
                name,
                dates,
                extension_symbol=extension_symbol,
                baseline_outcomes=baseline_outcomes,
                challenger_outcomes=challenger_outcomes,
                round_trip_cost_bps=config.round_trip_cost_bps,
            )
            for name, dates in slice_specs
        )
        cost_stress = tuple(
            _cost_stress(
                cost,
                dates=holdout_dates,
                baseline_outcomes=baseline_outcomes,
                challenger_outcomes=challenger_outcomes,
            )
            for cost in costs
        )
        displaced = sum(
            baseline_outcomes[session_date].candidate_symbol
            != challenger_outcomes[session_date].candidate_symbol
            for session_date in comparable_dates
        )
        candidate_reports.append(OpeningExtensionCandidateReport(
            symbol=extension_symbol,
            comparable_sessions=len(comparable_dates),
            displaced_baseline_sessions=displaced,
            candidate_signal_sessions=sum(
                challenger_outcomes[session_date].signal
                and (
                    challenger_outcomes[session_date].candidate_symbol
                    == extension_symbol
                )
                for session_date in comparable_dates
            ),
            slices=slices,
            cost_stress=cost_stress,
        ))

    return OpeningExtensionResearchReport(
        algorithm_version=OPENING_EXTENSION_RESEARCH_VERSION,
        opening_config_version=config.version_hash(),
        discovery_ratio=discovery_ratio,
        minimum_data_coverage=minimum_data_coverage,
        stop_loss_pct=config.stop_loss_pct,
        baseline_symbols=baseline,
        extension_symbols=extensions,
        source_sessions=len(ordered_sessions),
        discovery_sessions=split_index,
        holdout_sessions=len(ordered_sessions) - split_index,
        candidates=tuple(candidate_reports),
        survivorship_bias="CURRENT_BASELINE_SYMBOLS",
        automatic_promotion_allowed=False,
    )


def _evaluate_policy(
    session: OpeningExtensionSession,
    *,
    symbols: tuple[str, ...],
    required_symbols: tuple[str, ...],
    config: OpeningMomentumConfig,
    minimum_data_coverage: float,
) -> _PolicyOutcome:
    symbol_set = set(symbols)
    observations = tuple(
        item for item in session.observations if item.symbol in symbol_set
    )
    observed_symbols = {item.symbol for item in observations}
    required_count = max(
        config.minimum_universe_size,
        math.ceil(len(symbols) * minimum_data_coverage),
    )
    if (
        len(observations) < required_count
        or not set(required_symbols).issubset(observed_symbols)
    ):
        return _PolicyOutcome(
            session_date=session.session_date,
            resolved=False,
            signal=False,
            candidate_symbol=None,
            reason="DATA_INCOMPLETE",
            gross_return_bps=0.0,
        )

    decision = evaluate_opening_momentum(observations, config)
    if decision.reason == "ENTRY_BAR_MISSING":
        return _PolicyOutcome(
            session_date=session.session_date,
            resolved=False,
            signal=False,
            candidate_symbol=decision.candidate_symbol,
            reason=decision.reason,
            gross_return_bps=0.0,
        )
    if decision.action != "ENTER_LONG":
        return _PolicyOutcome(
            session_date=session.session_date,
            resolved=True,
            signal=False,
            candidate_symbol=decision.candidate_symbol,
            reason=decision.reason,
            gross_return_bps=0.0,
        )

    exit_by_symbol = {
        item.symbol: item for item in session.exit_prices
    }
    candidate_symbol = decision.candidate_symbol
    exit_outcome = exit_by_symbol.get(candidate_symbol or "")
    if (
        candidate_symbol is None
        or decision.entry_price is None
        or exit_outcome is None
    ):
        return _PolicyOutcome(
            session_date=session.session_date,
            resolved=False,
            signal=False,
            candidate_symbol=candidate_symbol,
            reason="EXIT_BAR_MISSING",
            gross_return_bps=0.0,
        )
    gross_return_bps = (
        exit_outcome.price / decision.entry_price - 1
    ) * 10_000
    return _PolicyOutcome(
        session_date=session.session_date,
        resolved=True,
        signal=True,
        candidate_symbol=candidate_symbol,
        reason=decision.reason,
        gross_return_bps=gross_return_bps,
        stop_triggered=exit_outcome.stop_triggered,
    )


def _evaluation_slice(
    name: _SliceName,
    dates: set[date],
    *,
    extension_symbol: str,
    baseline_outcomes: dict[date, _PolicyOutcome],
    challenger_outcomes: dict[date, _PolicyOutcome],
    round_trip_cost_bps: float,
) -> OpeningExtensionSlice:
    resolved_dates = tuple(
        session_date
        for session_date in sorted(dates)
        if (
            baseline_outcomes[session_date].resolved
            and challenger_outcomes[session_date].resolved
        )
    )
    baseline_returns = _net_returns(
        resolved_dates,
        baseline_outcomes,
        round_trip_cost_bps=round_trip_cost_bps,
    )
    challenger_returns = _net_returns(
        resolved_dates,
        challenger_outcomes,
        round_trip_cost_bps=round_trip_cost_bps,
    )
    return OpeningExtensionSlice(
        name=name,
        start_date=resolved_dates[0] if resolved_dates else None,
        end_date=resolved_dates[-1] if resolved_dates else None,
        resolved_sessions=len(resolved_dates),
        displaced_baseline_sessions=sum(
            baseline_outcomes[value].candidate_symbol
            != challenger_outcomes[value].candidate_symbol
            for value in resolved_dates
        ),
        extension_signal_sessions=sum(
            challenger_outcomes[value].signal
            and (
                challenger_outcomes[value].candidate_symbol
                == extension_symbol
            )
            for value in resolved_dates
        ),
        baseline=_return_metrics(
            baseline_returns,
            tuple(
                baseline_outcomes[value].signal
                for value in resolved_dates
            ),
            tuple(
                baseline_outcomes[value].stop_triggered
                for value in resolved_dates
            ),
        ),
        challenger=_return_metrics(
            challenger_returns,
            tuple(
                challenger_outcomes[value].signal
                for value in resolved_dates
            ),
            tuple(
                challenger_outcomes[value].stop_triggered
                for value in resolved_dates
            ),
        ),
        comparison=compare_opening_momentum_variants(
            baseline_returns,
            challenger_returns,
        ),
    )


def _cost_stress(
    round_trip_cost_bps: float,
    *,
    dates: set[date],
    baseline_outcomes: dict[date, _PolicyOutcome],
    challenger_outcomes: dict[date, _PolicyOutcome],
) -> OpeningExtensionCostStress:
    resolved_dates = tuple(
        session_date
        for session_date in sorted(dates)
        if (
            baseline_outcomes[session_date].resolved
            and challenger_outcomes[session_date].resolved
        )
    )
    baseline_returns = _net_returns(
        resolved_dates,
        baseline_outcomes,
        round_trip_cost_bps=round_trip_cost_bps,
    )
    challenger_returns = _net_returns(
        resolved_dates,
        challenger_outcomes,
        round_trip_cost_bps=round_trip_cost_bps,
    )
    deltas = tuple(
        challenger - baseline
        for baseline, challenger in zip(
            baseline_returns,
            challenger_returns,
            strict=True,
        )
    )
    challenger_metrics = _return_metrics(
        challenger_returns,
        tuple(
            challenger_outcomes[value].signal
            for value in resolved_dates
        ),
        tuple(
            challenger_outcomes[value].stop_triggered
            for value in resolved_dates
        ),
    )
    return OpeningExtensionCostStress(
        round_trip_cost_bps=round_trip_cost_bps,
        holdout_sessions=len(resolved_dates),
        baseline_cumulative_return_bps=sum(baseline_returns),
        challenger_cumulative_return_bps=sum(challenger_returns),
        cumulative_delta_bps=sum(deltas),
        mean_delta_bps=fmean(deltas) if deltas else 0.0,
        challenger_max_drawdown_bps=(
            challenger_metrics.max_drawdown_bps
        ),
        challenger_without_best_3_bps=(
            challenger_metrics.cumulative_without_best_3_bps
        ),
    )


def _net_returns(
    dates: Sequence[date],
    outcomes: dict[date, _PolicyOutcome],
    *,
    round_trip_cost_bps: float,
) -> tuple[float, ...]:
    return tuple(
        outcome.gross_return_bps
        - (round_trip_cost_bps if outcome.signal else 0.0)
        for outcome in (outcomes[value] for value in dates)
    )


def _return_metrics(
    returns_bps: Sequence[float],
    signals: Sequence[bool],
    stop_exits: Sequence[bool],
) -> OpeningExtensionReturnMetrics:
    if not len(returns_bps) == len(signals) == len(stop_exits):
        raise ValueError(
            "return, signal, and stop series must have equal length"
        )
    returns = tuple(float(value) for value in returns_bps)
    signal_returns = tuple(
        value for value, signal in zip(returns, signals, strict=True)
        if signal
    )
    positive = sum(value for value in signal_returns if value > 0)
    negative = abs(sum(value for value in signal_returns if value < 0))
    profit_factor = (
        positive / negative
        if negative > 0
        else None
    )
    best_three = sorted(returns, reverse=True)[:3]
    return OpeningExtensionReturnMetrics(
        sessions=len(returns),
        signals=len(signal_returns),
        stop_exits=sum(stop_exits),
        wins=sum(value > 0 for value in signal_returns),
        win_rate=(
            sum(value > 0 for value in signal_returns)
            / len(signal_returns)
            if signal_returns
            else 0.0
        ),
        cumulative_return_bps=sum(returns),
        mean_return_bps=fmean(returns) if returns else 0.0,
        median_return_bps=median(returns) if returns else 0.0,
        max_drawdown_bps=_max_drawdown_bps(returns),
        profit_factor=profit_factor,
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


def _normalized_unique_symbols(
    symbols: Sequence[str],
    *,
    field_name: str,
) -> tuple[str, ...]:
    normalized = tuple(value.strip().upper() for value in symbols)
    if not normalized or any(not value for value in normalized):
        raise ValueError(f"{field_name} must contain symbols")
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field_name} must contain unique symbols")
    return normalized


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
