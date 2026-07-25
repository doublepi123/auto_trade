from __future__ import annotations

import math
from bisect import bisect_right
from dataclasses import asdict, dataclass, replace
from datetime import date
from typing import Mapping, Sequence

from app.core.holiday_calendar import is_market_closed
from app.core.market_calendar import get_session
from app.domain.universe_selection.catalog import IndexCandidate
from app.domain.universe_selection.rotation_walk_forward import (
    DIVERSIFIED_ROTATION_VARIANT,
    ROTATION_BENCHMARK_SYMBOLS,
    RotationVariant,
    rotation_target_weights,
)
from app.domain.universe_selection.selector import (
    ROTATION_ALGORITHM_VERSION,
    CandidateInput,
    CandidateSelection,
    DailyBar,
    RotationSelectionEvidence,
    UniverseSelectionConfig,
    liquidity_spread_proxy_bps,
    select_candidates,
)


ROTATION_FORWARD_VERSION = "rotation-monthly-open-forward-v2"


def _required_bool(value: object, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise ValueError(f"{field_name} must be a boolean")


@dataclass(frozen=True)
class RotationCohortSignal:
    symbol: str
    rank: int
    risk_group: str
    momentum_pct: float
    sma_price: float
    above_sma: bool
    score: float
    signal_spread_bps: float
    ranking_method: str = "raw_momentum"
    formation_realized_volatility: float | None = None
    ranking_metric: float | None = None
    target_weight_pct: float = 0.0

    def __post_init__(self) -> None:
        if not self.symbol.strip() or not self.risk_group.strip():
            raise ValueError("rotation signal identity is required")
        if self.rank < 1:
            raise ValueError("rotation signal rank must be positive")
        finite_values = (
            self.momentum_pct,
            self.sma_price,
            self.score,
            self.signal_spread_bps,
            self.target_weight_pct,
        )
        if any(not math.isfinite(value) for value in finite_values):
            raise ValueError("rotation signal values must be finite")
        if (
            self.momentum_pct <= 0
            or self.sma_price <= 0
            or not self.above_sma
            or self.score < 0
            or self.signal_spread_bps < 0
            or not 0 <= self.target_weight_pct <= 100
        ):
            raise ValueError("rotation signal values are invalid")
        if self.ranking_method not in {
            "raw_momentum",
            "return_to_variance",
        }:
            raise ValueError("rotation signal ranking method is invalid")
        if self.formation_realized_volatility is not None and (
            not math.isfinite(self.formation_realized_volatility)
            or self.formation_realized_volatility <= 0
        ):
            raise ValueError(
                "rotation signal formation volatility is invalid"
            )
        if self.ranking_metric is None:
            if self.ranking_method != "raw_momentum":
                raise ValueError(
                    "risk-adjusted rotation signal ranking is missing"
                )
            object.__setattr__(
                self,
                "ranking_metric",
                self.momentum_pct,
            )
        elif (
            not math.isfinite(self.ranking_metric)
            or self.ranking_metric <= 0
        ):
            raise ValueError("rotation signal ranking metric is invalid")
        if (
            self.ranking_method == "return_to_variance"
            and self.formation_realized_volatility is None
        ):
            raise ValueError(
                "return-to-variance signal volatility is missing"
            )

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
    ) -> RotationCohortSignal:
        return cls(
            symbol=str(payload["symbol"]),
            rank=int(str(payload["rank"])),
            risk_group=str(payload["risk_group"]),
            momentum_pct=float(str(payload["momentum_pct"])),
            sma_price=float(str(payload["sma_price"])),
            above_sma=_required_bool(
                payload["above_sma"],
                field_name="above_sma",
            ),
            score=float(str(payload["score"])),
            signal_spread_bps=float(
                str(payload["signal_spread_bps"])
            ),
            ranking_method=str(
                payload.get("ranking_method", "raw_momentum")
            ),
            formation_realized_volatility=(
                float(
                    str(
                        payload["formation_realized_volatility"]
                    )
                )
                if payload.get("formation_realized_volatility")
                is not None
                else None
            ),
            ranking_metric=(
                float(str(payload["ranking_metric"]))
                if payload.get("ranking_metric") is not None
                else None
            ),
            target_weight_pct=float(
                str(payload.get("target_weight_pct", 0.0))
            ),
        )


@dataclass(frozen=True)
class RotationCohortRegistration:
    cohort_month: date
    rotation_algorithm_version: str
    variant_name: str
    signal_date: date
    registered_as_of_date: date
    forward_eligible: bool
    target_signals: tuple[RotationCohortSignal, ...]

    def __post_init__(self) -> None:
        if self.cohort_month.day != 1:
            raise ValueError("cohort_month must be the first of month")
        if not self.rotation_algorithm_version or not self.variant_name:
            raise ValueError("rotation registration versions are required")
        if self.signal_date >= self.cohort_month:
            raise ValueError("signal_date must precede cohort_month")
        if self.registered_as_of_date < self.signal_date:
            raise ValueError(
                "rotation registration cannot precede its signal"
            )
        if self.forward_eligible != (
            self.registered_as_of_date == self.signal_date
        ):
            raise ValueError("forward_eligible is inconsistent")
        symbols = [signal.symbol for signal in self.target_signals]
        if len(set(symbols)) != len(symbols):
            raise ValueError("rotation target symbols must be unique")
        ranks = [signal.rank for signal in self.target_signals]
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("rotation target ranks must be contiguous")
        if self.target_signals and all(
            signal.target_weight_pct == 0
            for signal in self.target_signals
        ):
            equal_weight = (
                1.0 / len(self.target_signals)
            ) * 100
            object.__setattr__(
                self,
                "target_signals",
                tuple(
                    replace(
                        signal,
                        target_weight_pct=equal_weight,
                    )
                    for signal in self.target_signals
                ),
            )
        elif any(
            signal.target_weight_pct <= 0
            for signal in self.target_signals
        ):
            raise ValueError(
                "rotation target weights must all be positive"
            )
        total_weight = sum(
            signal.target_weight_pct
            for signal in self.target_signals
        )
        if total_weight > 100.0 + 1e-9:
            raise ValueError(
                "rotation target weights exceed 100 percent"
            )

    @property
    def target_symbols(self) -> tuple[str, ...]:
        return tuple(signal.symbol for signal in self.target_signals)

    def to_dict(self) -> dict[str, object]:
        return {
            **asdict(self),
            "cohort_month": self.cohort_month.isoformat(),
            "signal_date": self.signal_date.isoformat(),
            "registered_as_of_date": (
                self.registered_as_of_date.isoformat()
            ),
            "target_signals": [
                asdict(signal)
                for signal in self.target_signals
            ],
        }

    @classmethod
    def from_dict(
        cls,
        payload: Mapping[str, object],
    ) -> RotationCohortRegistration:
        raw_signals = payload.get("target_signals")
        if not isinstance(raw_signals, list):
            raise ValueError("rotation target_signals must be a list")
        signals: list[RotationCohortSignal] = []
        for raw_signal in raw_signals:
            if not isinstance(raw_signal, dict):
                raise ValueError("rotation target signal is invalid")
            signals.append(RotationCohortSignal.from_dict(raw_signal))
        return cls(
            cohort_month=date.fromisoformat(
                str(payload["cohort_month"])
            ),
            rotation_algorithm_version=str(
                payload["rotation_algorithm_version"]
            ),
            variant_name=str(payload["variant_name"]),
            signal_date=date.fromisoformat(
                str(payload["signal_date"])
            ),
            registered_as_of_date=date.fromisoformat(
                str(payload["registered_as_of_date"])
            ),
            forward_eligible=_required_bool(
                payload["forward_eligible"],
                field_name="forward_eligible",
            ),
            target_signals=tuple(signals),
        )


@dataclass(frozen=True)
class RotationForwardHolding:
    symbol: str
    rank: int
    risk_group: str
    weight_pct: float
    momentum_pct: float
    ranking_method: str
    formation_realized_volatility: float | None
    ranking_metric: float | None
    entry_price: float | None
    mark_price: float | None
    gross_return_pct: float | None
    signal_spread_bps: float
    mark_spread_bps: float | None
    data_status: str


@dataclass(frozen=True)
class RotationForwardSnapshot:
    algorithm_version: str
    rotation_algorithm_version: str
    status: str
    evidence_mode: str
    cohort_month: date | None
    variant_name: str
    signal_date: date | None
    entry_date: date | None
    mark_date: date | None
    registered_as_of_date: date | None
    forward_eligible: bool
    selection_drift_detected: bool
    target_symbols: tuple[str, ...]
    holdings: tuple[RotationForwardHolding, ...]
    elapsed_sessions: int
    forward_observation_sessions: int
    gross_return_pct: float | None
    entry_cost_pct: float | None
    estimated_exit_cost_pct: float | None
    total_estimated_cost_pct: float | None
    net_liquidation_return_pct: float | None
    qqq_return_pct: float | None
    dia_return_pct: float | None
    excess_return_vs_qqq_pct: float | None
    excess_return_vs_dia_pct: float | None
    survivorship_bias: bool
    order_execution_allowed: bool
    automatic_promotion_allowed: bool
    blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        for field_name in (
            "cohort_month",
            "signal_date",
            "entry_date",
            "mark_date",
            "registered_as_of_date",
        ):
            value = getattr(self, field_name)
            payload[field_name] = (
                value.isoformat()
                if isinstance(value, date)
                else None
            )
        return payload


@dataclass(frozen=True)
class RotationForwardEvaluation:
    registration: RotationCohortRegistration | None
    snapshot: RotationForwardSnapshot
    selections: tuple[CandidateSelection, ...]


def _bar_date(bar: DailyBar) -> date:
    return get_session("US").local(bar.timestamp).date()


def _bar_map(bars: Sequence[DailyBar]) -> dict[date, DailyBar]:
    result: dict[date, DailyBar] = {}
    for bar in sorted(bars, key=lambda item: item.timestamp):
        values = (
            float(bar.open),
            float(bar.high),
            float(bar.low),
            float(bar.close),
        )
        if (
            all(math.isfinite(value) and value > 0 for value in values)
            and float(bar.high) >= max(
                values[0],
                values[2],
                values[3],
            )
            and float(bar.low) <= min(
                values[0],
                values[1],
                values[3],
            )
        ):
            result[_bar_date(bar)] = bar
    return result


def _histories(
    bars_by_symbol: Mapping[str, Sequence[DailyBar]],
) -> dict[
    str,
    tuple[tuple[date, ...], tuple[DailyBar, ...]],
]:
    result: dict[
        str,
        tuple[tuple[date, ...], tuple[DailyBar, ...]],
    ] = {}
    for symbol, bars in bars_by_symbol.items():
        ordered = tuple(sorted(bars, key=lambda bar: bar.timestamp))
        result[symbol] = (
            tuple(_bar_date(bar) for bar in ordered),
            ordered,
        )
    return result


def _inputs_at(
    *,
    candidates: Sequence[IndexCandidate],
    histories_by_symbol: Mapping[
        str,
        tuple[tuple[date, ...], tuple[DailyBar, ...]],
    ],
    signal_date: date,
    max_bars: int,
) -> list[CandidateInput]:
    inputs: list[CandidateInput] = []
    for candidate in candidates:
        dates, ordered = histories_by_symbol.get(
            candidate.symbol,
            ((), ()),
        )
        end = bisect_right(dates, signal_date)
        start = max(0, end - max_bars)
        bars = ordered[start:end]
        errors: tuple[str, ...] = ()
        if not bars or dates[end - 1] != signal_date:
            errors = ("DATA_STALE_SESSION_DATE",)
        inputs.append(
            CandidateInput(
                candidate=candidate,
                completed_daily_bars=bars,
                bid=None,
                ask=None,
                estimated_spread_bps=liquidity_spread_proxy_bps(
                    bars
                ),
                data_errors=errors,
            )
        )
    return inputs


def _variant_config(
    base_config: UniverseSelectionConfig,
    variant: RotationVariant,
) -> UniverseSelectionConfig:
    return replace(
        base_config,
        rotation_lookback_bars=variant.lookback_bars,
        rotation_skip_bars=variant.skip_bars,
        rotation_sma_bars=variant.sma_bars,
        rotation_ranking=variant.ranking,
        rotation_max_selected=variant.max_selected,
        rotation_max_per_risk_group=(
            variant.max_per_risk_group
        ),
    )


def _registration_and_selections(
    *,
    candidates: Sequence[IndexCandidate],
    bars_by_symbol: Mapping[str, Sequence[DailyBar]],
    base_config: UniverseSelectionConfig,
    variant: RotationVariant,
    cohort_month: date,
    signal_date: date,
    registered_as_of_date: date,
) -> tuple[
    RotationCohortRegistration,
    tuple[CandidateSelection, ...],
]:
    selection_config = _variant_config(base_config, variant)
    required_bars = max(
        selection_config.min_completed_bars,
        selection_config.rotation_lookback_bars + 1,
        selection_config.rotation_sma_bars,
    )
    selections = tuple(
        select_candidates(
            _inputs_at(
                candidates=candidates,
                histories_by_symbol=_histories(bars_by_symbol),
                signal_date=signal_date,
                max_bars=required_bars,
            ),
            selection_config,
        )
    )
    selected = sorted(
        (
            row
            for row in selections
            if row.rotation.selected
        ),
        key=lambda row: row.rotation.rank or 10_000,
    )
    target_weights = rotation_target_weights(
        selected,
        variant,
    )
    signals: list[RotationCohortSignal] = []
    for row in selected:
        evidence = row.rotation
        spread = row.metrics.relative_spread_bps
        if (
            evidence.rank is None
            or evidence.momentum_pct is None
            or evidence.sma_price is None
            or evidence.above_sma is None
            or spread is None
        ):
            raise ValueError("selected rotation evidence is incomplete")
        signals.append(
            RotationCohortSignal(
                symbol=row.candidate.symbol,
                rank=evidence.rank,
                risk_group=row.candidate.risk_group,
                momentum_pct=evidence.momentum_pct,
                sma_price=evidence.sma_price,
                above_sma=evidence.above_sma,
                score=evidence.score,
                signal_spread_bps=spread,
                ranking_method=evidence.ranking_method,
                formation_realized_volatility=(
                    evidence.formation_realized_volatility
                ),
                ranking_metric=evidence.ranking_metric,
                target_weight_pct=(
                    target_weights.get(
                        row.candidate.symbol,
                        0.0,
                    )
                    * 100
                ),
            )
        )
    registration = RotationCohortRegistration(
        cohort_month=cohort_month,
        rotation_algorithm_version=ROTATION_ALGORITHM_VERSION,
        variant_name=variant.name,
        signal_date=signal_date,
        registered_as_of_date=registered_as_of_date,
        forward_eligible=registered_as_of_date == signal_date,
        target_signals=tuple(signals),
    )
    return registration, selections


def build_rotation_cohort_registration(
    *,
    candidates: Sequence[IndexCandidate],
    bars_by_symbol: Mapping[str, Sequence[DailyBar]],
    base_config: UniverseSelectionConfig,
    cohort_month: date,
    signal_date: date,
    registered_as_of_date: date,
    variant: RotationVariant = DIVERSIFIED_ROTATION_VARIANT,
) -> RotationCohortRegistration:
    registration, _ = _registration_and_selections(
        candidates=candidates,
        bars_by_symbol=bars_by_symbol,
        base_config=base_config,
        variant=variant,
        cohort_month=cohort_month,
        signal_date=signal_date,
        registered_as_of_date=registered_as_of_date,
    )
    return registration


def _month_context(
    benchmark_bars_by_symbol: Mapping[
        str,
        Sequence[DailyBar],
    ],
    *,
    as_of_date: date,
) -> tuple[date, date, date, tuple[date, ...]] | None:
    benchmark_maps = {
        symbol: _bar_map(bars)
        for symbol, bars in benchmark_bars_by_symbol.items()
    }
    if any(
        symbol not in benchmark_maps
        for symbol in ROTATION_BENCHMARK_SYMBOLS
    ):
        return None
    common_dates = tuple(
        sorted(
            session_date
            for session_date in (
                set(benchmark_maps["QQQ.US"])
                & set(benchmark_maps["DIA.US"])
            )
            if session_date <= as_of_date
        )
    )
    if not common_dates:
        return None
    mark_date = common_dates[-1]
    cohort_month = date(mark_date.year, mark_date.month, 1)
    month_dates = [
        session_date
        for session_date in common_dates
        if (
            session_date.year == cohort_month.year
            and session_date.month == cohort_month.month
        )
    ]
    if not month_dates:
        return None
    entry_date = month_dates[0]
    entry_position = common_dates.index(entry_date)
    if entry_position == 0:
        return None
    signal_date = common_dates[entry_position - 1]
    return (
        cohort_month,
        signal_date,
        entry_date,
        common_dates,
    )


def rotation_cohort_month(
    benchmark_bars_by_symbol: Mapping[
        str,
        Sequence[DailyBar],
    ],
    *,
    as_of_date: date,
) -> date | None:
    context = _month_context(
        benchmark_bars_by_symbol,
        as_of_date=as_of_date,
    )
    return context[0] if context is not None else None


def _apply_frozen_registration(
    selections: Sequence[CandidateSelection],
    registration: RotationCohortRegistration,
    variant: RotationVariant,
) -> tuple[CandidateSelection, ...]:
    signals = {
        signal.symbol: signal
        for signal in registration.target_signals
    }
    frozen: list[CandidateSelection] = []
    for row in selections:
        symbol = row.candidate.symbol
        signal = signals.get(symbol)
        if signal is not None:
            evidence = RotationSelectionEvidence(
                algorithm_version=(
                    registration.rotation_algorithm_version
                ),
                lookback_bars=variant.lookback_bars,
                skip_bars=variant.skip_bars,
                sma_bars=variant.sma_bars,
                ranking_method=signal.ranking_method,
                momentum_pct=signal.momentum_pct,
                formation_realized_volatility=(
                    signal.formation_realized_volatility
                ),
                ranking_metric=signal.ranking_metric,
                sma_price=signal.sma_price,
                above_sma=signal.above_sma,
                eligible=True,
                selected=True,
                rank=signal.rank,
                score=signal.score,
                exclusion_reasons=(),
            )
        else:
            reasons = list(row.rotation.exclusion_reasons)
            if row.rotation.selected:
                reasons.append("ROTATION_FROZEN_MONTHLY_COHORT")
            evidence = replace(
                row.rotation,
                algorithm_version=(
                    registration.rotation_algorithm_version
                ),
                selected=False,
                rank=None,
                exclusion_reasons=tuple(dict.fromkeys(reasons)),
            )
        frozen.append(replace(row, rotation=evidence))
    return tuple(frozen)


def unavailable_rotation_forward_snapshot(
    status: str,
    *,
    blocker: str,
    variant: RotationVariant = DIVERSIFIED_ROTATION_VARIANT,
) -> RotationForwardSnapshot:
    return RotationForwardSnapshot(
        algorithm_version=ROTATION_FORWARD_VERSION,
        rotation_algorithm_version=ROTATION_ALGORITHM_VERSION,
        status=status,
        evidence_mode="UNAVAILABLE",
        cohort_month=None,
        variant_name=variant.name,
        signal_date=None,
        entry_date=None,
        mark_date=None,
        registered_as_of_date=None,
        forward_eligible=False,
        selection_drift_detected=False,
        target_symbols=(),
        holdings=(),
        elapsed_sessions=0,
        forward_observation_sessions=0,
        gross_return_pct=None,
        entry_cost_pct=None,
        estimated_exit_cost_pct=None,
        total_estimated_cost_pct=None,
        net_liquidation_return_pct=None,
        qqq_return_pct=None,
        dia_return_pct=None,
        excess_return_vs_qqq_pct=None,
        excess_return_vs_dia_pct=None,
        survivorship_bias=True,
        order_execution_allowed=False,
        automatic_promotion_allowed=False,
        blockers=(
            blocker,
            "CURRENT_CONSTITUENTS_SURVIVORSHIP_BIAS",
            "ROTATION_FORWARD_OBSERVATIONS_REQUIRED",
        ),
    )


def evaluate_rotation_forward(
    *,
    candidates: Sequence[IndexCandidate],
    bars_by_symbol: Mapping[str, Sequence[DailyBar]],
    benchmark_bars_by_symbol: Mapping[
        str,
        Sequence[DailyBar],
    ],
    base_config: UniverseSelectionConfig,
    as_of_date: date,
    frozen_registration: RotationCohortRegistration | None = None,
    variant: RotationVariant = DIVERSIFIED_ROTATION_VARIANT,
) -> RotationForwardEvaluation:
    if not candidates:
        raise ValueError("candidates must not be empty")
    context = _month_context(
        benchmark_bars_by_symbol,
        as_of_date=as_of_date,
    )
    if context is None:
        return RotationForwardEvaluation(
            registration=None,
            snapshot=unavailable_rotation_forward_snapshot(
                "BENCHMARK_HISTORY_UNAVAILABLE",
                blocker="ROTATION_BENCHMARK_HISTORY_UNAVAILABLE",
                variant=variant,
            ),
            selections=(),
        )
    cohort_month, signal_date, entry_date, common_dates = context
    computed_registration, computed_selections = (
        _registration_and_selections(
            candidates=candidates,
            bars_by_symbol=bars_by_symbol,
            base_config=base_config,
            variant=variant,
            cohort_month=cohort_month,
            signal_date=signal_date,
            registered_as_of_date=as_of_date,
        )
    )
    registration = frozen_registration or computed_registration
    if (
        registration.cohort_month != cohort_month
        or registration.signal_date != signal_date
        or registration.variant_name != variant.name
        or registration.rotation_algorithm_version
        != ROTATION_ALGORITHM_VERSION
    ):
        raise ValueError("frozen rotation registration is incompatible")
    computed_symbols = computed_registration.target_symbols
    selection_drift = computed_symbols != registration.target_symbols
    selections = _apply_frozen_registration(
        computed_selections,
        registration,
        variant,
    )
    candidate_maps = {
        symbol: _bar_map(bars)
        for symbol, bars in bars_by_symbol.items()
    }
    benchmark_maps = {
        symbol: _bar_map(bars)
        for symbol, bars in benchmark_bars_by_symbol.items()
    }
    mark_date = common_dates[-1]
    blockers = [
        "CURRENT_CONSTITUENTS_SURVIVORSHIP_BIAS",
        "ROTATION_FORWARD_OBSERVATIONS_REQUIRED",
    ]
    if not registration.forward_eligible:
        blockers.append("COHORT_REGISTERED_AFTER_SIGNAL")
    if selection_drift:
        blockers.append("PRECOMMITTED_COHORT_SELECTION_DRIFT")

    holdings: list[RotationForwardHolding] = []
    holding_weights: list[float] = []
    returns: list[float] = []
    entry_side_costs: list[float] = []
    exit_side_costs: list[float] = []
    data_incomplete = False
    round_trip_cost_bps = (
        base_config.round_trip_fee_bps
        + base_config.round_trip_slippage_bps
    )
    for signal in registration.target_signals:
        weight = signal.target_weight_pct / 100
        symbol_map = candidate_maps.get(signal.symbol, {})
        entry_bar = symbol_map.get(entry_date)
        mark_bar = symbol_map.get(mark_date)
        entry_price = (
            float(entry_bar.open)
            if entry_bar is not None
            else None
        )
        mark_price = (
            float(mark_bar.close)
            if mark_bar is not None
            else None
        )
        symbol_bars = [
            bar
            for bar in bars_by_symbol.get(signal.symbol, ())
            if _bar_date(bar) <= mark_date
        ]
        mark_spread = liquidity_spread_proxy_bps(symbol_bars)
        gross_return = (
            mark_price / entry_price - 1.0
            if entry_price is not None
            and mark_price is not None
            and entry_price > 0
            else None
        )
        data_status = "COMPLETE"
        if entry_price is None:
            data_status = "ENTRY_UNAVAILABLE"
        elif mark_price is None:
            data_status = "MARK_UNAVAILABLE"
        elif mark_spread is None:
            data_status = "SPREAD_UNAVAILABLE"
        if data_status != "COMPLETE" or gross_return is None:
            data_incomplete = True
            blockers.append(
                f"{signal.symbol}:{data_status}"
            )
        else:
            assert mark_spread is not None
            holding_weights.append(weight)
            returns.append(gross_return)
            entry_side_costs.append(
                (
                    round_trip_cost_bps
                    + signal.signal_spread_bps
                )
                / 2
                / 10_000
            )
            exit_side_costs.append(
                (
                    round_trip_cost_bps
                    + mark_spread
                )
                / 2
                / 10_000
            )
        holdings.append(
            RotationForwardHolding(
                symbol=signal.symbol,
                rank=signal.rank,
                risk_group=signal.risk_group,
                weight_pct=weight * 100,
                momentum_pct=signal.momentum_pct,
                ranking_method=signal.ranking_method,
                formation_realized_volatility=(
                    signal.formation_realized_volatility
                ),
                ranking_metric=signal.ranking_metric,
                entry_price=entry_price,
                mark_price=mark_price,
                gross_return_pct=(
                    gross_return * 100
                    if gross_return is not None
                    else None
                ),
                signal_spread_bps=signal.signal_spread_bps,
                mark_spread_bps=mark_spread,
                data_status=data_status,
            )
        )

    gross_return_pct: float | None
    entry_cost_pct: float | None
    exit_cost_pct: float | None
    total_cost_pct: float | None
    net_return_pct: float | None
    if data_incomplete:
        status = "DATA_INCOMPLETE"
        gross_return_pct = None
        entry_cost_pct = None
        exit_cost_pct = None
        total_cost_pct = None
        net_return_pct = None
    elif not registration.target_signals:
        status = (
            "FORWARD_CASH"
            if registration.forward_eligible
            else "BACKFILLED_CASH"
        )
        gross_return_pct = 0.0
        entry_cost_pct = 0.0
        exit_cost_pct = 0.0
        total_cost_pct = 0.0
        net_return_pct = 0.0
    else:
        status = (
            "FORWARD_OPEN"
            if registration.forward_eligible
            else "BACKFILLED_OPEN"
        )
        gross_return = sum(
            weight * asset_return
            for weight, asset_return in zip(
                holding_weights,
                returns,
            )
        )
        entry_cost = sum(
            weight * side_cost
            for weight, side_cost in zip(
                holding_weights,
                entry_side_costs,
            )
        )
        ending_values = [
            weight * (1.0 + asset_return)
            for weight, asset_return in zip(
                holding_weights,
                returns,
            )
        ]
        cash_weight = max(
            0.0,
            1.0 - sum(holding_weights),
        )
        ending_total = cash_weight + sum(ending_values)
        exit_cost_amount = sum(
            value * side_cost
            for value, side_cost in zip(
                ending_values,
                exit_side_costs,
            )
        )
        exit_cost = (
            exit_cost_amount / ending_total
            if ending_total > 0
            else 0.0
        )
        net_return = (
            cash_weight
            + sum(
                weight
                * (1.0 - entry_side_cost)
                * (1.0 + asset_return)
                * (1.0 - exit_side_cost)
                for (
                    weight,
                    asset_return,
                    entry_side_cost,
                    exit_side_cost,
                ) in zip(
                    holding_weights,
                    returns,
                    entry_side_costs,
                    exit_side_costs,
                )
            )
            - 1.0
        )
        gross_return_pct = gross_return * 100
        entry_cost_pct = entry_cost * 100
        exit_cost_pct = exit_cost * 100
        total_cost_pct = (gross_return - net_return) * 100
        net_return_pct = net_return * 100

    qqq_entry = benchmark_maps["QQQ.US"].get(entry_date)
    qqq_mark = benchmark_maps["QQQ.US"].get(mark_date)
    dia_entry = benchmark_maps["DIA.US"].get(entry_date)
    dia_mark = benchmark_maps["DIA.US"].get(mark_date)
    if any(
        bar is None
        for bar in (qqq_entry, qqq_mark, dia_entry, dia_mark)
    ):
        qqq_return_pct = None
        dia_return_pct = None
        blockers.append("ROTATION_BENCHMARK_MARK_UNAVAILABLE")
    else:
        assert qqq_entry is not None
        assert qqq_mark is not None
        assert dia_entry is not None
        assert dia_mark is not None
        qqq_return_pct = (
            float(qqq_mark.close) / float(qqq_entry.open) - 1.0
        ) * 100
        dia_return_pct = (
            float(dia_mark.close) / float(dia_entry.open) - 1.0
        ) * 100
    elapsed_sessions = sum(
        entry_date <= session_date <= mark_date
        for session_date in common_dates
    )
    snapshot = RotationForwardSnapshot(
        algorithm_version=ROTATION_FORWARD_VERSION,
        rotation_algorithm_version=ROTATION_ALGORITHM_VERSION,
        status=status,
        evidence_mode=(
            "FORWARD_PRECOMMITTED"
            if registration.forward_eligible
            else "BACKFILLED_AFTER_ENTRY"
        ),
        cohort_month=cohort_month,
        variant_name=variant.name,
        signal_date=signal_date,
        entry_date=entry_date,
        mark_date=mark_date,
        registered_as_of_date=(
            registration.registered_as_of_date
        ),
        forward_eligible=registration.forward_eligible,
        selection_drift_detected=selection_drift,
        target_symbols=registration.target_symbols,
        holdings=tuple(holdings),
        elapsed_sessions=elapsed_sessions,
        forward_observation_sessions=(
            elapsed_sessions
            if registration.forward_eligible
            else 0
        ),
        gross_return_pct=gross_return_pct,
        entry_cost_pct=entry_cost_pct,
        estimated_exit_cost_pct=exit_cost_pct,
        total_estimated_cost_pct=total_cost_pct,
        net_liquidation_return_pct=net_return_pct,
        qqq_return_pct=qqq_return_pct,
        dia_return_pct=dia_return_pct,
        excess_return_vs_qqq_pct=(
            net_return_pct - qqq_return_pct
            if net_return_pct is not None
            and qqq_return_pct is not None
            else None
        ),
        excess_return_vs_dia_pct=(
            net_return_pct - dia_return_pct
            if net_return_pct is not None
            and dia_return_pct is not None
            else None
        ),
        survivorship_bias=True,
        order_execution_allowed=False,
        automatic_promotion_allowed=False,
        blockers=tuple(dict.fromkeys(blockers)),
    )
    return RotationForwardEvaluation(
        registration=registration,
        snapshot=snapshot,
        selections=selections,
    )


def is_last_us_session_of_month(session_date: date) -> bool:
    if (
        session_date.weekday() >= 5
        or is_market_closed("US", session_date)
    ):
        return False
    cursor = date.fromordinal(session_date.toordinal() + 1)
    while cursor.month == session_date.month:
        if (
            cursor.weekday() < 5
            and not is_market_closed("US", cursor)
        ):
            return False
        cursor = date.fromordinal(cursor.toordinal() + 1)
    return True


def next_cohort_month(session_date: date) -> date:
    if session_date.month == 12:
        return date(session_date.year + 1, 1, 1)
    return date(session_date.year, session_date.month + 1, 1)
